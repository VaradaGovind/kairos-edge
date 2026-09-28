#!/usr/bin/env python3
"""Phase 2 Kairos emulator harness (trace-driven tier orchestration).

This harness prioritizes HBM-capacity and AXI-utilization pressure events for
eviction/prefetch decisions, with thermal treated as secondary advisory input.
"""

from __future__ import annotations

import argparse
import csv
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List

ROOT_DIR = Path(__file__).resolve().parents[1]
import sys

if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from core.dynamic_eviction import DynamicEvictionFSM, TierPressureSample  # noqa: E402
from core.kv_manager import ChunkPrefillConfig, KVManager  # noqa: E402
from core.tier_orchestrator import TierOrchestrator  # noqa: E402


@dataclass(frozen=True)
class TraceRow:
    cycle: int
    hbm_util_pct: float
    axi_util_pct: float
    temperature_c: float
    phase_state: int


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Kairos Phase 2 trace-driven emulator.")
    parser.add_argument(
        "--trace-csv",
        default=str(ROOT_DIR / "hw_tmu" / "traces" / "thermal_critical.csv"),
        help="Trace CSV path (new or legacy format).",
    )
    parser.add_argument(
        "--output",
        default=str(ROOT_DIR / "logs" / "emulator" / "phase2_tiering_log.json"),
        help="Output JSON log path.",
    )
    parser.add_argument(
        "--cycles-per-second",
        type=float,
        default=4096.0,
        help="Conversion ratio from cycle to trace timestamp.",
    )
    parser.add_argument(
        "--hbm-total-gb",
        type=float,
        default=16.0,
        help="HBM capacity used to map utilization percent to bytes.",
    )
    parser.add_argument(
        "--prompt-tokens",
        type=int,
        default=2048,
        help="Synthetic prompt size for chunked-prefill overlap estimates.",
    )
    parser.add_argument(
        "--decode-tokens",
        type=int,
        default=256,
        help="Synthetic decode size for prefetch latency-hiding estimates.",
    )
    return parser.parse_args()


def _clamp_pct(v: float) -> float:
    return max(0.0, min(100.0, v))


def load_trace_rows(path: Path) -> List[TraceRow]:
    rows: List[TraceRow] = []
    with path.open("r", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        fields = set(reader.fieldnames or [])
        has_new = {"hbm_util_pct", "axi_util_pct", "temperature_c", "phase_state"}.issubset(fields)
        has_legacy = {"temperature_c", "bandwidth_gbps", "phase_state"}.issubset(fields)
        if not has_new and not has_legacy:
            raise ValueError(
                "Trace must contain either new columns "
                "(cycle,hbm_util_pct,axi_util_pct,temperature_c,phase_state) "
                "or legacy columns (cycle,temperature_c,bandwidth_gbps,phase_state)."
            )

        for row in reader:
            cycle = int(float(row["cycle"]))
            if has_new:
                hbm_util = _clamp_pct(float(row["hbm_util_pct"]))
                axi_util = _clamp_pct(float(row["axi_util_pct"]))
            else:
                bw = float(row["bandwidth_gbps"])
                hbm_util = _clamp_pct(3.0 * bw)
                axi_util = _clamp_pct(2.5 * bw)
            rows.append(
                TraceRow(
                    cycle=cycle,
                    hbm_util_pct=hbm_util,
                    axi_util_pct=axi_util,
                    temperature_c=float(row["temperature_c"]),
                    phase_state=int(float(row["phase_state"])),
                )
            )
    rows.sort(key=lambda r: r.cycle)
    return rows


def run_emulator(
    *,
    rows: List[TraceRow],
    cycles_per_second: float,
    hbm_total_gb: float,
    prompt_tokens: int,
    decode_tokens: int,
) -> Dict[str, Any]:
    hbm_total_bytes = int(max(1.0, hbm_total_gb) * 1024 * 1024 * 1024)
    eviction_fsm = DynamicEvictionFSM()
    kv_manager = KVManager(
        ChunkPrefillConfig(
            chunk_size_tokens=384,
            compute_s_per_token=0.00035,
            nvme_serialize_s_per_token=0.00018,
            nvme_setup_latency_s=0.004,
        )
    )
    orchestrator = TierOrchestrator(
        decode_horizon_steps=128,
        pcie_rtt_s=0.0032,
        pcie_transfer_s_per_mb=0.00058,
    )

    action_log: List[Dict[str, Any]] = []
    primary_count = 0
    thermal_only_count = 0

    for row in rows:
        hbm_used_bytes = int((row.hbm_util_pct / 100.0) * hbm_total_bytes)
        sample = TierPressureSample(
            timestamp_s=float(row.cycle) / max(cycles_per_second, 1.0),
            hbm_used_bytes=hbm_used_bytes,
            hbm_total_bytes=hbm_total_bytes,
            axi_read_util_pct=row.axi_util_pct,
            axi_write_util_pct=row.axi_util_pct * 0.9,
            thermal_c=row.temperature_c,
        )
        decision = eviction_fsm.step(sample)
        if not decision.should_evict:
            continue

        reasons = decision.reason_codes
        has_primary = any(code.startswith("HBM") or code.startswith("AXI") for code in reasons)
        trigger_class = "capacity_or_bus_pressure" if has_primary else "thermal_secondary"
        priority = 0 if has_primary else 1
        if has_primary:
            primary_count += 1
        else:
            thermal_only_count += 1

        action_log.append(
            {
                "cycle": row.cycle,
                "timestamp_s": sample.timestamp_s,
                "priority": priority,
                "trigger_class": trigger_class,
                "reasons": reasons,
                "bytes_to_free": decision.bytes_to_free,
                "target_tier": decision.target_tier,
                "compress": decision.should_compress,
                "hbm_util_pct": row.hbm_util_pct,
                "axi_util_pct": row.axi_util_pct,
                "temperature_c": row.temperature_c,
            }
        )

    # Priority order: HBM/AXI events first, thermal-only after.
    action_log.sort(key=lambda item: (item["priority"], item["cycle"]))

    prefill = kv_manager.chunked_prefill_orchestration(prompt_tokens)
    working_set = orchestrator.synthesize_decode_working_set(
        prompt_tokens=prompt_tokens, decode_tokens=decode_tokens, page_size_tokens=128
    )
    plans, prefetch_stats = orchestrator.schedule_decode_prefetch(
        working_set,
        decode_cursor_step=prompt_tokens,
        decode_steps_per_second=45.0,
    )

    return {
        "summary": {
            "trace_rows": len(rows),
            "actions_total": len(action_log),
            "actions_primary": primary_count,
            "actions_thermal_secondary": thermal_only_count,
            "primary_priority_enabled": True,
        },
        "chunked_prefill_orchestration": prefill.as_dict(),
        "decode_prefetch": {
            "stats": prefetch_stats.as_dict(),
            "scheduled_pages": [asdict(p) for p in plans[:64]],
            "scheduled_pages_truncated": len(plans) > 64,
        },
        "actions": action_log,
    }


def main() -> None:
    args = parse_args()
    trace_path = Path(args.trace_csv).expanduser().resolve()
    output_path = Path(args.output).expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    rows = load_trace_rows(trace_path)
    report = run_emulator(
        rows=rows,
        cycles_per_second=float(args.cycles_per_second),
        hbm_total_gb=float(args.hbm_total_gb),
        prompt_tokens=int(args.prompt_tokens),
        decode_tokens=int(args.decode_tokens),
    )
    report["trace_csv"] = str(trace_path)

    with output_path.open("w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, sort_keys=True)
        handle.write("\n")

    print(f"Phase 2 emulator complete. Output: {output_path}")
    print(f"Actions (primary/total): {report['summary']['actions_primary']}/{report['summary']['actions_total']}")
    print(
        "Hidden latency (prefill+prefetch): "
        f"{report['chunked_prefill_orchestration']['hidden_nvme_latency_s'] + report['decode_prefetch']['stats']['hidden_latency_s']:.6f}s"
    )


if __name__ == "__main__":
    main()
