#!/usr/bin/env python3
"""Kairos baseline benchmarking harness (Level 1 reproduction)."""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import requests

try:
    import yaml
except Exception:  # pragma: no cover - optional at runtime
    yaml = None

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from core.config_logger import initialize_deterministic_run  # noqa: E402
from hw_tmu.tmu_fabric_emulator import (  # noqa: E402
    HierarchicalMIMOTMU,
    MacroAdvisory,
    MicroSafetyEvent,
    OOBThermalFabric,
)


@dataclass
class RequestMetric:
    request_id: int
    prompt_index: int
    ttft_s: float
    tpot_s: float
    latency_s: float
    completion_tokens: int
    prompt_tokens_proxy: int
    output_text: str
    prefill_hidden_latency_s: float = 0.0
    prefill_exposed_latency_s: float = 0.0
    decode_prefetch_hidden_latency_s: float = 0.0
    decode_prefetch_exposed_latency_s: float = 0.0
    ttft_offload_penalty_s: float = 0.0
    tpot_offload_penalty_s: float = 0.0
    prefetch_hit_rate: float = 0.0
    eviction_triggered: bool = False
    eviction_reasons: List[str] | None = None


class GPUMemorySampler:
    """Polls nvidia-smi and records memory plus optional power snapshots."""

    def __init__(self, interval_s: float = 0.2) -> None:
        self.interval_s = interval_s
        self.samples: List[Dict[str, float]] = []
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        self._thread.join(timeout=2.0)

    @staticmethod
    def _query_nvidia_smi() -> Optional[Dict[str, float]]:
        cmd = [
            "nvidia-smi",
            "--query-gpu=memory.used,memory.total,power.draw",
            "--format=csv,noheader,nounits",
        ]
        try:
            result = subprocess.run(cmd, check=True, capture_output=True, text=True)
        except (FileNotFoundError, subprocess.CalledProcessError):
            return None

        total_used = 0.0
        total_mem = 0.0
        total_power = 0.0
        power_samples = 0
        for line in result.stdout.strip().splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) < 2:
                continue
            used_mb = float(parts[0])
            total_mb = float(parts[1])
            total_used += used_mb
            total_mem += total_mb
            if len(parts) >= 3 and parts[2] and parts[2].lower() not in {"n/a", "[not supported]"}:
                try:
                    total_power += float(parts[2])
                    power_samples += 1
                except ValueError:
                    pass

        if total_mem <= 0:
            return None
        sample = {
            "used_mb": total_used,
            "total_mb": total_mem,
            "utilization_pct": (100.0 * total_used / total_mem),
        }
        if power_samples > 0:
            sample["power_draw_w"] = total_power
        return sample

    def _run(self) -> None:
        while not self._stop_event.is_set():
            sample = self._query_nvidia_smi()
            if sample is not None:
                sample["timestamp_s"] = time.time()
                self.samples.append(sample)
            self._stop_event.wait(self.interval_s)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run Kairos baseline benchmark harness.")
    parser.add_argument(
        "--config",
        default=str(ROOT_DIR / "configs" / "baseline_full_cache.yaml"),
        help="Path to benchmark config YAML/JSON.",
    )
    parser.add_argument(
        "--output-dir",
        default=None,
        help="Optional output directory override.",
    )
    return parser.parse_args()


def load_config(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        if path.suffix.lower() in {".yaml", ".yml"}:
            if yaml is None:
                raise RuntimeError(
                    "PyYAML is required for YAML configs. Install with: pip install pyyaml"
                )
            return yaml.safe_load(handle)
        return json.load(handle)


def resolve_power_budget_profile(config: Dict[str, Any], config_path: Path) -> Dict[str, Any]:
    budget_path = config.get("experiment", {}).get("power_budget_file")
    if not budget_path:
        budget_path = str(ROOT_DIR / "configs" / "power_matched_budget.yaml")

    candidate = Path(budget_path)
    if not candidate.is_absolute():
        candidate = (ROOT_DIR / candidate).resolve()

    with candidate.open("r", encoding="utf-8") as handle:
        if candidate.suffix.lower() in {".yaml", ".yml"}:
            if yaml is None:
                raise RuntimeError("PyYAML is required to read the power budget profile.")
            payload = yaml.safe_load(handle)
        else:
            payload = json.load(handle)

    if not isinstance(payload, dict):
        raise ValueError(f"Power budget profile must be a mapping: {candidate}")
    payload["__path__"] = str(candidate)
    return payload


def compute_workload_hash(prompts: Sequence[str]) -> str:
    payload = json.dumps(list(prompts), ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def linear_trend_slope(values: Sequence[float]) -> float:
    if len(values) < 2:
        return 0.0
    x_mean = (len(values) - 1) / 2.0
    y_mean = statistics.mean(values)
    numerator = 0.0
    denominator = 0.0
    for idx, value in enumerate(values):
        dx = float(idx) - x_mean
        dy = float(value) - y_mean
        numerator += dx * dy
        denominator += dx * dx
    if denominator == 0.0:
        return 0.0
    return numerator / denominator


def percentile(values: Sequence[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = (len(ordered) - 1) * p
    lo = int(idx)
    hi = min(lo + 1, len(ordered) - 1)
    weight = idx - lo
    return ordered[lo] * (1.0 - weight) + ordered[hi] * weight


def normalize_text(text: str) -> str:
    return " ".join(text.strip().lower().split())


def lcs_length(a: List[str], b: List[str]) -> int:
    if not a or not b:
        return 0
    dp = [[0] * (len(b) + 1) for _ in range(len(a) + 1)]
    for i in range(1, len(a) + 1):
        for j in range(1, len(b) + 1):
            if a[i - 1] == b[j - 1]:
                dp[i][j] = dp[i - 1][j - 1] + 1
            else:
                dp[i][j] = max(dp[i - 1][j], dp[i][j - 1])
    return dp[-1][-1]


def rouge_l_f1(hypothesis: str, reference: str) -> float:
    hyp_tokens = normalize_text(hypothesis).split()
    ref_tokens = normalize_text(reference).split()
    if not hyp_tokens or not ref_tokens:
        return 0.0
    lcs = lcs_length(hyp_tokens, ref_tokens)
    precision = lcs / len(hyp_tokens)
    recall = lcs / len(ref_tokens)
    if precision + recall == 0:
        return 0.0
    return 2 * precision * recall / (precision + recall)


def count_tokens_proxy(text: str) -> int:
    normalized = normalize_text(text)
    if not normalized:
        return 0
    return len(normalized.split())


def query_server_model_id(base_url: str, api_key: str, timeout_s: int) -> str:
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    response = requests.get(f"{base_url}/v1/models", headers=headers, timeout=timeout_s)
    response.raise_for_status()
    body = response.json()
    data = body.get("data", [])
    if not data:
        raise RuntimeError("Server returned no models at /v1/models")
    return str(data[0].get("id", ""))


def assert_fairness_constraints(
    config: Dict[str, Any],
    server_model_id: str,
    power_budget_profile: Dict[str, Any],
) -> None:
    model_cfg = config["model"]
    runtime_cfg = config["runtime"]
    workload_cfg = config["workload"]
    expected = config["fairness"]["expected"]
    baseline_cfg = config.get("baseline_profile", {})
    throttling_cfg = config.get("hardware_throttling", {})
    batch_rules_cfg = config.get("batch_rules", {})

    computed_workload_hash = compute_workload_hash(workload_cfg["prompts"])

    checks = [
        ("model_id", model_cfg["id"], expected["model_id"]),
        ("model_revision", model_cfg["revision"], expected["model_revision"]),
        ("precision", model_cfg["precision"], expected["precision"]),
        ("baseline_profile.name", baseline_cfg.get("name"), expected.get("baseline_profile_name")),
        (
            "baseline_profile.enable_chunked_prefill",
            baseline_cfg.get("vllm_server_args", {}).get("enable_chunked_prefill"),
            expected.get("baseline_enable_chunked_prefill"),
        ),
        (
            "baseline_profile.gpu_memory_utilization",
            baseline_cfg.get("vllm_server_args", {}).get("gpu_memory_utilization"),
            expected.get("baseline_gpu_memory_utilization"),
        ),
        (
            "baseline_profile.max_num_batched_tokens",
            baseline_cfg.get("vllm_server_args", {}).get("max_num_batched_tokens"),
            expected.get("baseline_max_num_batched_tokens"),
        ),
        ("batch_rules.max_tokens", batch_rules_cfg.get("max_tokens"), expected.get("batch_rules", {}).get("max_tokens")),
        (
            "batch_rules.temperature",
            batch_rules_cfg.get("temperature"),
            expected.get("batch_rules", {}).get("temperature"),
        ),
        ("batch_rules.top_p", batch_rules_cfg.get("top_p"), expected.get("batch_rules", {}).get("top_p")),
        (
            "batch_rules.warmup_requests",
            batch_rules_cfg.get("warmup_requests"),
            expected.get("batch_rules", {}).get("warmup_requests"),
        ),
        (
            "batch_rules.measurement_requests",
            batch_rules_cfg.get("measurement_requests"),
            expected.get("batch_rules", {}).get("measurement_requests"),
        ),
        ("workload_hash", computed_workload_hash, expected["workload_hash"]),
    ]

    workload_settings_expected = expected.get("workload_settings", {})
    for key, expected_val in workload_settings_expected.items():
        if key not in runtime_cfg:
            raise ValueError(f"Missing runtime setting required by fairness check: {key}")
        checks.append((f"runtime.{key}", runtime_cfg[key], expected_val))

    for label, actual, expected_value in checks:
        if actual != expected_value:
            raise ValueError(
                f"Fairness check failed for {label}: actual={actual!r} expected={expected_value!r}"
            )

    for field_name, expected_value in expected.get("hardware_throttling", {}).items():
        actual_value = throttling_cfg.get(field_name)
        if actual_value != expected_value:
            raise ValueError(
                f"Fairness check failed for hardware_throttling.{field_name}: actual={actual_value!r} expected={expected_value!r}"
            )

    power_budget = power_budget_profile.get("power_budget", {})
    for field_name in ("board_power_cap_w", "dvfs_step_limit_mv", "thermal_guardrail_c", "thermal_critical_c"):
        if field_name in power_budget and field_name in throttling_cfg:
            if throttling_cfg[field_name] != power_budget[field_name]:
                raise ValueError(
                    f"Power budget mismatch for {field_name}: actual={throttling_cfg[field_name]!r} expected={power_budget[field_name]!r}"
                )

    # Server path can include a namespace alias; keep exact-match first then suffix fallback.
    expected_model_id = expected["model_id"]
    if server_model_id != expected_model_id and not server_model_id.endswith(expected_model_id):
        raise ValueError(
            "Fairness check failed for served model id: "
            f"server={server_model_id!r}, expected={expected_model_id!r}"
        )


def stream_completion_request(
    *,
    base_url: str,
    endpoint: str,
    api_key: str,
    model_id: str,
    prompt: str,
    max_tokens: int,
    temperature: float,
    top_p: float,
    timeout_s: int,
) -> RequestMetric:
    url = f"{base_url}{endpoint}"
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    payload = {
        "model": model_id,
        "prompt": prompt,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "top_p": top_p,
        "stream": True,
        "stream_options": {"include_usage": True},
    }

    start_t = time.perf_counter()
    first_token_t: Optional[float] = None
    completion_tokens: Optional[int] = None
    generated_chunks: List[str] = []

    with requests.post(url, headers=headers, json=payload, stream=True, timeout=timeout_s) as response:
        response.raise_for_status()
        for raw_line in response.iter_lines(decode_unicode=True):
            if not raw_line:
                continue
            if not raw_line.startswith("data:"):
                continue

            content = raw_line[5:].strip()
            if content == "[DONE]":
                break

            chunk = json.loads(content)
            usage = chunk.get("usage")
            if usage and "completion_tokens" in usage:
                completion_tokens = int(usage["completion_tokens"])

            choices = chunk.get("choices", [])
            if choices:
                text_piece = choices[0].get("text", "")
                if text_piece:
                    if first_token_t is None:
                        first_token_t = time.perf_counter()
                    generated_chunks.append(text_piece)

    end_t = time.perf_counter()
    output_text = "".join(generated_chunks)

    if completion_tokens is None:
        completion_tokens = max(1, count_tokens_proxy(output_text))

    ttft_s = (first_token_t - start_t) if first_token_t is not None else (end_t - start_t)
    decode_window_s = max(end_t - (first_token_t or start_t), 1e-9)
    tpot_s = decode_window_s / max(completion_tokens, 1)

    return RequestMetric(
        request_id=-1,
        prompt_index=-1,
        ttft_s=ttft_s,
        tpot_s=tpot_s,
        latency_s=end_t - start_t,
        completion_tokens=completion_tokens,
        prompt_tokens_proxy=count_tokens_proxy(prompt),
        output_text=output_text,
    )


def compute_quality_metrics(
    request_metrics: Sequence[RequestMetric],
    expected_answers: Sequence[str],
) -> Dict[str, Any]:
    if not expected_answers:
        return {
            "status": "skipped_no_references",
            "exact_match": None,
            "rouge_l_f1": None,
        }

    exact_matches: List[float] = []
    rouge_scores: List[float] = []

    for metric in request_metrics:
        if metric.prompt_index >= len(expected_answers):
            continue
        reference = expected_answers[metric.prompt_index]
        if not reference:
            continue

        predicted = normalize_text(metric.output_text)
        expected = normalize_text(reference)

        exact_matches.append(1.0 if predicted == expected else 0.0)
        rouge_scores.append(rouge_l_f1(metric.output_text, reference))

    if not rouge_scores:
        return {
            "status": "skipped_missing_expected_for_prompts",
            "exact_match": None,
            "rouge_l_f1": None,
        }

    return {
        "status": "computed",
        "exact_match": statistics.mean(exact_matches),
        "rouge_l_f1": statistics.mean(rouge_scores),
    }


def safe_mean(values: Sequence[float]) -> float:
    return statistics.mean(values) if values else 0.0


def normalized_latency_score(value: float, target: float) -> float:
    if target <= 0:
        return 0.0
    return min(2.0, value / target)


def compute_weighted_eval(
    *,
    ttft_p95_s: float,
    tpot_p95_s: float,
    throughput_tps: float,
    power_proxy_w: Optional[float],
    eval_cfg: Dict[str, Any],
) -> Dict[str, Any]:
    weights = eval_cfg.get(
        "weights",
        {
            "ttft": 0.45,
            "tpot": 0.45,
            "throughput": 0.08,
            "power": 0.02,
        },
    )
    targets = eval_cfg.get(
        "targets",
        {
            "ttft_p95_s": 1.0,
            "tpot_p95_s": 1.0,
            "throughput_tps": 1.0,
            "power_proxy_w": 250.0,
        },
    )

    ttft_component = normalized_latency_score(ttft_p95_s, float(targets["ttft_p95_s"]))
    tpot_component = normalized_latency_score(tpot_p95_s, float(targets["tpot_p95_s"]))
    throughput_component = min(
        2.0, float(targets["throughput_tps"]) / max(throughput_tps, 1e-9)
    )
    if power_proxy_w is None:
        power_component = 0.0
    else:
        power_component = normalized_latency_score(power_proxy_w, float(targets["power_proxy_w"]))

    # Lower is better. TTFT/TPOT dominate the weighted objective.
    weighted_cost = (
        float(weights["ttft"]) * ttft_component
        + float(weights["tpot"]) * tpot_component
        + float(weights["throughput"]) * throughput_component
        + float(weights["power"]) * power_component
    )
    return {
        "weights": weights,
        "targets": targets,
        "components": {
            "ttft_cost": ttft_component,
            "tpot_cost": tpot_component,
            "throughput_cost": throughput_component,
            "power_cost": power_component,
        },
        "weighted_cost": weighted_cost,
    }


def assert_latency_prioritized_constraints(
    *,
    ttft_p95_s: float,
    tpot_p95_s: float,
    weighted_cost: float,
    eval_cfg: Dict[str, Any],
) -> None:
    asserts_cfg = eval_cfg.get("asserts", {})
    max_ttft = asserts_cfg.get("max_ttft_p95_s")
    max_tpot = asserts_cfg.get("max_tpot_p95_s")
    max_cost = asserts_cfg.get("max_weighted_cost")
    if max_ttft is not None and ttft_p95_s > float(max_ttft):
        raise ValueError(
            f"Latency assert failed: ttft_p95_s={ttft_p95_s:.6f} exceeds max_ttft_p95_s={float(max_ttft):.6f}"
        )
    if max_tpot is not None and tpot_p95_s > float(max_tpot):
        raise ValueError(
            f"Latency assert failed: tpot_p95_s={tpot_p95_s:.6f} exceeds max_tpot_p95_s={float(max_tpot):.6f}"
        )
    if max_cost is not None and weighted_cost > float(max_cost):
        raise ValueError(
            "Latency-prioritized weighted assert failed: "
            f"cost={weighted_cost:.6f} exceeds max_weighted_cost={float(max_cost):.6f}"
        )


def main() -> None:
    args = parse_args()
    config_path = Path(args.config).expanduser().resolve()
    config = load_config(config_path)
    power_budget_profile = resolve_power_budget_profile(config, config_path)

    experiment_cfg = config["experiment"]
    server_cfg = config["server"]
    model_cfg = config["model"]
    runtime_cfg = config["runtime"]
    workload_cfg = config["workload"]
    kairos_sim_cfg = config.get("kairos_latency_hiding", {})
    eval_cfg = config.get("evaluation", {})

    output_dir = Path(args.output_dir or experiment_cfg["output_dir"]).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    det_report, config_log_path = initialize_deterministic_run(
        config,
        output_dir=output_dir / "config_logs",
        run_label=experiment_cfg["name"],
    )

    server_model_id = query_server_model_id(
        base_url=server_cfg["base_url"],
        api_key=server_cfg.get("api_key", ""),
        timeout_s=int(runtime_cfg["request_timeout_s"]),
    )
    assert_fairness_constraints(config, server_model_id, power_budget_profile)
    prompts = list(workload_cfg["prompts"])
    expected_answers = list(workload_cfg.get("expected_answers", []))

    warmup_requests = int(runtime_cfg["warmup_requests"])
    measurement_requests = int(runtime_cfg["measurement_requests"])

    for i in range(warmup_requests):
        prompt = prompts[i % len(prompts)]
        _ = stream_completion_request(
            base_url=server_cfg["base_url"],
            endpoint=server_cfg["endpoint"],
            api_key=server_cfg.get("api_key", ""),
            model_id=model_cfg["id"],
            prompt=prompt,
            max_tokens=int(runtime_cfg["max_tokens"]),
            temperature=float(runtime_cfg["temperature"]),
            top_p=float(runtime_cfg["top_p"]),
            timeout_s=int(runtime_cfg["request_timeout_s"]),
        )

    sampler = GPUMemorySampler(interval_s=float(runtime_cfg.get("gpu_mem_poll_interval_s", 0.2)))
    sampler.start()

    fabric = OOBThermalFabric(capacity=256, interrupt_coalescing_hz=1000.0)
    thermal_model = HierarchicalMIMOTMU(
        ambient_c=25.0,
        lateral_resistance_ohm=[
            [0.0, 4.0, 6.0],
            [4.0, 0.0, 4.0],
            [6.0, 4.0, 0.0],
        ],
        capacitance_j_per_c=[32.0, 28.0, 36.0],
    )


    measured: List[RequestMetric] = []
    
    measurement_start = time.perf_counter()
    for i in range(measurement_requests):
        prompt_index = i % len(prompts)
        prompt = prompts[prompt_index]
        metric = stream_completion_request(
            base_url=server_cfg["base_url"],
            endpoint=server_cfg["endpoint"],
            api_key=server_cfg.get("api_key", ""),
            model_id=model_cfg["id"],
            prompt=prompt,
            max_tokens=int(runtime_cfg["max_tokens"]),
            temperature=float(runtime_cfg["temperature"]),
            top_p=float(runtime_cfg["top_p"]),
            timeout_s=int(runtime_cfg["request_timeout_s"]),
        )

        metric.request_id = i
        metric.prompt_index = prompt_index
        measured.append(metric)
    measurement_end = time.perf_counter()
    sampler.stop()
    fabric.flush_host_interrupts(time.time())

    total_tokens = sum(m.completion_tokens for m in measured)
    total_latency_window = max(measurement_end - measurement_start, 1e-9)
    throughput_tps = total_tokens / total_latency_window

    ttft_values = [m.ttft_s for m in measured]
    tpot_values = [m.tpot_s for m in measured]
    ttft_offload_penalty_values = [m.ttft_offload_penalty_s for m in measured]
    tpot_offload_penalty_values = [m.tpot_offload_penalty_s for m in measured]

    mem_used_samples = [s["used_mb"] for s in sampler.samples]
    mem_util_samples = [s["utilization_pct"] for s in sampler.samples]
    power_samples = [s["power_draw_w"] for s in sampler.samples if "power_draw_w" in s]

    quality = compute_quality_metrics(measured, expected_answers)

    ttft_p50 = percentile(ttft_values, 0.50)
    ttft_p95 = percentile(ttft_values, 0.95)
    ttft_p99 = percentile(ttft_values, 0.99)
    tpot_p50 = percentile(tpot_values, 0.50)
    tpot_p95 = percentile(tpot_values, 0.95)
    ttft_trend_slope = linear_trend_slope(ttft_values)
    tpot_trend_slope = linear_trend_slope(tpot_values)
    energy_j_estimate = None
    energy_per_generated_token_j = None
    if power_samples:
        energy_j_estimate = safe_mean(power_samples) * total_latency_window
        energy_per_generated_token_j = energy_j_estimate / max(total_tokens, 1)

    weighted_eval = compute_weighted_eval(
        ttft_p95_s=ttft_p95,
        tpot_p95_s=tpot_p95,
        throughput_tps=throughput_tps,
        power_proxy_w=safe_mean(power_samples) if power_samples else None,
        eval_cfg=eval_cfg,
    )
    assert_latency_prioritized_constraints(
        ttft_p95_s=ttft_p95,
        tpot_p95_s=tpot_p95,
        weighted_cost=float(weighted_eval["weighted_cost"]),
        eval_cfg=eval_cfg,
    )

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    results = {
        "timestamp_utc": timestamp,
        "experiment_name": experiment_cfg["name"],
        "config_path": str(config_path),
        "config_log_path": str(config_log_path),
        "determinism": det_report.as_dict(),
        "served_model_id": server_model_id,
        "fairness_passed": True,
        "metrics": {
            "ttft_s": {
                "p50": ttft_p50,
                "p95": ttft_p95,
                "p99": ttft_p99,
            },
            "tpot_s": {
                "p50": tpot_p50,
                "p95": tpot_p95,
            },
            "throughput_tokens_per_s": throughput_tps,
            "memory_used_mb_peak": max(mem_used_samples) if mem_used_samples else None,
            "memory_used_mb_mean": statistics.mean(mem_used_samples) if mem_used_samples else None,
            "memory_utilization_pct_peak": max(mem_util_samples) if mem_util_samples else None,
            "memory_utilization_pct_mean": statistics.mean(mem_util_samples) if mem_util_samples else None,
            "power_draw_w_mean": safe_mean(power_samples) if power_samples else None,
            "power_draw_w_peak": max(power_samples) if power_samples else None,
            "energy_j_estimate": energy_j_estimate,
            "energy_per_generated_token_j": energy_per_generated_token_j,

            "latency_prioritized_eval": weighted_eval,
            "requests_measured": measurement_requests,
            "tokens_generated_total": total_tokens,
            "measurement_window_s": total_latency_window,
        },
        "quality": quality,
        "per_request": [asdict(m) for m in measured],
    }

    output_path = output_dir / f"{timestamp}_{experiment_cfg['name']}_metrics.json"
    with output_path.open("w", encoding="utf-8") as handle:
        json.dump(results, handle, indent=2, sort_keys=True)
        handle.write("\n")

    print(f"Benchmark complete. Results: {output_path}")
    print(f"TTFT p50: {results['metrics']['ttft_s']['p50']:.6f}s")
    print(f"TTFT p95: {results['metrics']['ttft_s']['p95']:.6f}s")
    print(f"TTFT p99: {results['metrics']['ttft_s']['p99']:.6f}s")
    print(f"TPOT p50: {results['metrics']['tpot_s']['p50']:.6f}s")
    print(f"TPOT p95: {results['metrics']['tpot_s']['p95']:.6f}s")
    print(f"Throughput: {results['metrics']['throughput_tokens_per_s']:.3f} tokens/s")


if __name__ == "__main__":
    main()
