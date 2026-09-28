from dataclasses import dataclass
from typing import List

@dataclass
class EdgeRequest:
    request_id: str
    arrival_time_s: float
    prompt_tokens: int
    is_bursty: bool = False

class EdgeRequestManager:
    """
    L2-C1: Edge-Native Request Manager API.
    Tailored for bursty AIoT inputs by prioritizing small, latency-sensitive requests.
    """
    def __init__(self, max_batch_size: int = 8, burst_threshold_tokens: int = 128):
        self.max_batch_size = max_batch_size
        self.burst_threshold_tokens = burst_threshold_tokens
        self.queue: List[EdgeRequest] = []
        self.clock_s = 0.0

    def step(self, dt_s: float) -> None:
        self.clock_s += dt_s

    def submit_request(self, req: EdgeRequest) -> None:
        # Determine if bursty based on threshold
        req.is_bursty = req.prompt_tokens <= self.burst_threshold_tokens
        self.queue.append(req)
        # Prioritize bursty (small) requests for AIoT
        self.queue.sort(key=lambda r: (not r.is_bursty, r.arrival_time_s))

    def get_next_batch(self) -> List[EdgeRequest]:
        batch_size = min(len(self.queue), self.max_batch_size)
        batch = self.queue[:batch_size]
        self.queue = self.queue[batch_size:]
        return batch
