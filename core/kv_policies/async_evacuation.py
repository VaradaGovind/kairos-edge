from dataclasses import dataclass
from typing import Dict, List

@dataclass
class KVAllocation:
    page_id: int
    is_open: bool
    is_evicted: bool = False

class AsyncShadowEvacuator:
    """
    L2-C2: Two-Stage Asynchronous Shadow Evacuation engine prototype.
    Validates Closed-Allocation Eviction Safety.
    """
    def __init__(self):
        self.allocations: Dict[int, KVAllocation] = {}
        self.pending_dma_transfers: List[int] = []

    def register_allocation(self, alloc: KVAllocation) -> None:
        self.allocations[alloc.page_id] = alloc

    def mark_and_evacuate(self, page_ids: List[int]) -> List[int]:
        evicted = []
        for pid in page_ids:
            if pid not in self.allocations:
                continue
            alloc = self.allocations[pid]
            # Safety Check: Closed-Allocation Eviction Safety
            # Only targets fully closed/saturated historical KV spans
            if not alloc.is_open and not alloc.is_evicted:
                # Stage 1: Mark PTE as read-only/evicting (simulated)
                self.pending_dma_transfers.append(pid)
                evicted.append(pid)
        return evicted

    def commit_dma_transfers(self) -> None:
        """
        Stage 2: Background streaming complete, nanosecond commit.
        """
        for pid in self.pending_dma_transfers:
            self.allocations[pid].is_evicted = True
        self.pending_dma_transfers.clear()
