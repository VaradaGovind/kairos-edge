from dataclasses import dataclass, field
from typing import List

@dataclass
class TierPressureSample:
    timestamp_s: float
    hbm_used_bytes: int
    hbm_total_bytes: int
    axi_read_util_pct: float
    axi_write_util_pct: float
    thermal_c: float
    tmu_forecast_c: float = 0.0

@dataclass
class EvictionDecision:
    should_evict: bool = False
    reason_codes: List[str] = field(default_factory=list)
    bytes_to_free: int = 0
    target_tier: str = "nvme"
    should_compress: bool = False

class DynamicEvictionFSM:
    def step(self, sample: TierPressureSample, tmu_anticipatory_evict: bool = False) -> EvictionDecision:
        decision = EvictionDecision()
        
        # Trigger on memory pressure
        if sample.hbm_used_bytes > sample.hbm_total_bytes * 0.9:
            decision.should_evict = True
            decision.reason_codes.append("CAPACITY_EXHAUSTED")
            decision.bytes_to_free = int(sample.hbm_total_bytes * 0.1)
            
        # Trigger on anticipatory TMU forecast (Novel cross-layer co-design)
        if tmu_anticipatory_evict:
            decision.should_evict = True
            decision.reason_codes.append("TMU_ANTICIPATORY_EVICT")
            # Proactively clear space to reduce future memory power draw
            decision.bytes_to_free = max(decision.bytes_to_free, int(sample.hbm_total_bytes * 0.2))
            
        return decision
