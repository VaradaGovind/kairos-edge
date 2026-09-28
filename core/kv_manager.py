from dataclasses import dataclass
from typing import Any, Dict

@dataclass
class ChunkPrefillConfig:
    chunk_size_tokens: int
    compute_s_per_token: float
    nvme_serialize_s_per_token: float
    nvme_setup_latency_s: float

class PrefillOrchestrationResult:
    def __init__(self, hidden_nvme_latency_s: float = 0.0):
        self.hidden_nvme_latency_s = hidden_nvme_latency_s

    def as_dict(self) -> Dict[str, Any]:
        return {"hidden_nvme_latency_s": self.hidden_nvme_latency_s}

class KVManager:
    def __init__(self, config: ChunkPrefillConfig):
        self.config = config

    def chunked_prefill_orchestration(self, prompt_tokens: int) -> PrefillOrchestrationResult:
        # Stub implementation
        return PrefillOrchestrationResult(hidden_nvme_latency_s=0.0)
