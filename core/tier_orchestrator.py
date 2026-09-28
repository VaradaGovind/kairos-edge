from dataclasses import dataclass
from typing import Any, Dict, List, Tuple

class PrefetchStats:
    def __init__(self, hidden_latency_s: float = 0.0):
        self.hidden_latency_s = hidden_latency_s

    def as_dict(self) -> Dict[str, Any]:
        return {"hidden_latency_s": self.hidden_latency_s}

@dataclass
class PrefetchPlan:
    page_id: int

@dataclass
class TenantTask:
    tenant_id: str
    priority: int
    sla_latency_s: float
    accumulated_deficit: float = 0.0

class TierOrchestrator:
    def __init__(self, decode_horizon_steps: int, pcie_rtt_s: float, pcie_transfer_s_per_mb: float):
        self.decode_horizon_steps = decode_horizon_steps
        self.pcie_rtt_s = pcie_rtt_s
        self.pcie_transfer_s_per_mb = pcie_transfer_s_per_mb
        self.active_tasks: List[TenantTask] = []

    def register_task(self, tenant_id: str, priority: int, sla_latency_s: float) -> None:
        self.active_tasks.append(TenantTask(tenant_id, priority, sla_latency_s))

    def synthesize_decode_working_set(self, prompt_tokens: int, decode_tokens: int, page_size_tokens: int) -> Any:
        # Stub implementation
        return []

    def schedule_decode_prefetch(self, working_set: Any, decode_cursor_step: int, decode_steps_per_second: float) -> Tuple[List[PrefetchPlan], PrefetchStats]:
        # Stub implementation
        return [], PrefetchStats(hidden_latency_s=0.0)

    def update_deficits(self, measured_latencies: Dict[str, float]) -> None:
        """Update thermal deficit (latency debt) for each task."""
        for task in self.active_tasks:
            if task.tenant_id in measured_latencies:
                debt = measured_latencies[task.tenant_id] - task.sla_latency_s
                if debt > 0:
                    task.accumulated_deficit += debt

    def thermal_deficit_round_robin_schedule(self, thermal_budget_available: float) -> List[str]:
        """
        Enforce DRR scheduling (L3-C5).
        Prioritize tasks with highest accumulated latency debt when thermal saturation approaches.
        """
        if thermal_budget_available > 0.8:
            # Plenty of thermal headroom, run all or standard priority
            self.active_tasks.sort(key=lambda t: t.priority, reverse=True)
            return [t.tenant_id for t in self.active_tasks]

        # Thermal stress! Prioritize by highest accumulated latency debt (Deficit)
        self.active_tasks.sort(key=lambda t: t.accumulated_deficit, reverse=True)
        
        # Only allow the top tasks that fit in the thermal budget to proceed.
        # For simplicity in this emulator, we just return the ordered list and let caller throttle.
        return [t.tenant_id for t in self.active_tasks]
