import time
import torch
import pprint
import random
import bittensor as bt
from typing import List
from targon.validator.config import env_config
from targon.validator import check_uid_availability
from targon.validator.crawler import VectorController
from targon.protocol import  TargonLinkPrediction, TargonSearchResult, TargonSearchResultStream

def get_random_uids(self, k: int, exclude: List[int] = None) -> torch.LongTensor:
    """Returns k available random uids from the metagraph.
    Args:
        k (int): Number of uids to return.
        exclude (List[int]): List of uids to exclude from the random sampling.
    Returns:
        uids (torch.LongTensor): Randomly sampled available uids.
    Notes:
        If `k` is larger than the number of available `uids`, set `k` to the number of available `uids`.
    """
    candidate_uids = []
    avail_uids = []

    for uid in range(self.metagraph.n.item()):
        uid_is_available = check_uid_availability(self.metagraph, uid, self.config.neuron.vpermit_tao_limit)
        uid_is_not_excluded = exclude is None or uid not in exclude

        if self.metagraph.axons[uid].coldkey in self.blacklisted_coldkeys:
            uid_is_available = False
            bt.logging.trace('blacklisted uid! not available', uid)

        if uid_is_available:
            avail_uids.append(uid)
            if uid_is_not_excluded:
                candidate_uids.append(uid)
                
    # Check if candidate_uids contain enough for querying, if not grab all avaliable uids
    available_uids = candidate_uids
    if len(candidate_uids) < k:
        available_uids += random.sample([uid for uid in avail_uids if uid not in candidate_uids], k-len(candidate_uids))

    uids = torch.tensor(random.sample(available_uids, k), dtype=torch.int64)
    return uids


async def fetch(self, synapse, uids):
    responses = await self.dendrite_pool.async_forward(
        uids = uids,
        synapse = synapse,
        timeout = 20
    )
    return responses


async def _search_result_forward(self, question: str, sources: List[dict], uids: List[int]):
    """Queries a list of uids for a question.
    Args:
        question (str): Question to query.
        uids (torch.LongTensor): Uids to query.
        timeout (float): Timeout for the query.
    Returns:
        responses (List[TargonQA]): List of responses.
    """
    # Check if we have any uids to query.
    search_synapse = TargonSearchResultStream( query=question, sources=sources, stream=True )
    responses = await fetch( self, search_synapse, uids )

    completions = [response.completion for response in responses]

    return completions


def select_qa(self):
    '''Returns a question from the different tasks
    
    '''

    # randomly select which dataset to use self.coding_dataset, self.qa_dataset, self.reasoning_dataset
    dataset = random.choice([self.coding_dataset, self.qa_dataset, self.reasoning_dataset])
    data = next(dataset)
    return data


def get_new_link( self ):
    """
    Returns a new link from the queue that hasn't been seen before.
    
    Args:
    seen_urls (set): A set of URLs that have already been seen.
    url_queue (deque): A queue of URLs to be crawled.

    Returns:
    str: A new link that hasn't been seen before.
    """
    while self.url_queue:
        potential_link = self.url_queue.popleft()
        if potential_link not in self.seen_urls:
            self.seen_urls.add(potential_link)
            return potential_link
    return None  # Return None if no new link is found

async def forward_fn(self, validation=True, stream=False):
    """Queries a list of uids for a question.
    Args:
        question (str): Question to query.
        uids (torch.LongTensor): Uids to query.
        timeout (float): Timeout for the query.
    Returns:
        responses (List[TargonQA]): List of responses.
    """
    k = 20
    if validation:
        uids = get_random_uids(self, k=k).to(self.device)
        urls = [get_new_link(self) for _ in range(k)]
        urls = [url for url in urls if url is not None]

        # Crawl the internet
        link_synapses = [TargonLinkPrediction(url=url) for url in urls]
        responses = [await self.dendrite_pool.async_forward(
            uids=[uid],
            synapse=link_synapse,
            timeout=12
        ) for link_synapse, uid in zip(link_synapses, uids)]

        # Process and print responses
        for response, uid in zip(responses, uids):
            if not response:
                continue

            processed_response = {
                'uid': uid.item(),
                'full_text': response[0].full_text,
                'title': response[0].title,
                'query': response[0].query,
                'new_links': response[0].new_links
            }

            if not (processed_response['full_text'] and processed_response['title'] and processed_response['query']):
                continue

            pprint.pprint(processed_response)

            # Handle new links
            unique_new_links = set(processed_response['new_links'])
            new_unseen_links = unique_new_links - self.seen_urls
            self.seen_urls.update(new_unseen_links)
            self.url_queue.extend(new_unseen_links)

            # Handle submissions
            api_key = env_config.get('SYBIL_API_KEY', None)
            if api_key is not None:
                embedding = self.embedding_model.encode(processed_response['full_text'])
                VectorController().submit(processed_response['uid'], processed_response['title'], processed_response['full_text'], processed_response['query'], embedding)
                bt.logging.debug('submitted url', processed_response['uid'])
        # validate Search Result responses
        data = select_qa(self)

        question = data['question']
        task = data['task']
        solution = data['solution']
        

        bt.logging.trace('question', question)
        bt.logging.trace('task', task)
        bt.logging.trace('solution', solution)


        # Search Result
        # TODO: add support for sources
        sources = []
        completions = await _search_result_forward(self, question, sources, uids)
        bt.logging.info("completions", completions)

        # Compute the rewards for the responses given the prompt.
        rewards: torch.FloatTensor = torch.zeros(len(completions), dtype=torch.float32).to(self.device)
        for weight_i, reward_fn_i in zip(self.reward_weights, self.reward_functions):
            reward_i, reward_i_normalized = reward_fn_i.apply(question, completions, task, solution)
            rewards += weight_i * reward_i_normalized.to(self.device)
            bt.logging.trace(str(reward_fn_i.name), reward_i.tolist())
            bt.logging.trace(str(reward_fn_i.name), reward_i_normalized.tolist())

        for masking_fn_i in self.masking_functions:
            mask_i, mask_i_normalized = masking_fn_i.apply(question, completions, task, solution)
            rewards *= mask_i_normalized.to(self.device)  # includes diversity
            bt.logging.trace(str(masking_fn_i.name), mask_i_normalized.tolist())

        
        scattered_rewards: torch.FloatTensor = self.moving_averaged_scores.scatter(0, uids, rewards).to(self.device)

        # Update moving_averaged_scores with rewards produced by this step.
        # shape: [ metagraph.n ]
        alpha: float = self.config.neuron.moving_average_alpha
        self.moving_averaged_scores: torch.FloatTensor = alpha * scattered_rewards + (1 - alpha) * self.moving_averaged_scores.to(
            self.device
        )

        bt.logging.info("rewards", rewards.tolist())
        for i in range(10):
            bt.logging.info("sleeping for", i)
            time.sleep(1)
        





            






