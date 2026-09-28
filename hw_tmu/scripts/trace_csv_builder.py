#!/usr/bin/env python3
"""Build Kairos tier-pressure replay traces from host telemetry CSV."""

from __future__ import annotations

import argparse
import csv
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Convert telemetry CSV to tier-pressure trace CSV.")
    parser.add_argument(
        "--input",
        required=True,
        help="Input CSV with timestamp plus temperature/HBM/AXI columns.",
    )
    parser.add_argument("--output", required=True, help="Output CSV path.")
    parser.add_argument(
        "--clock-hz",
        type=float,
        default=1_000_000.0,
        help="Target cycle clock for timestamp-to-cycle conversion.",
    )
    parser.add_argument(
        "--timestamp-col",
        default="timestamp_s",
        help="Timestamp column name in seconds (optional if cycle column exists).",
    )
    parser.add_argument(
        "--cycle-col",
        default="cycle",
        help="Cycle column name (used when timestamp column is absent).",
    )
    parser.add_argument("--temperature-col", default="temperature_c", help="Temperature column name in Celsius.")
    parser.add_argument(
        "--hbm-util-col",
        default="hbm_util_pct",
        help="HBM utilization percent column (0..100).",
    )
    parser.add_argument(
        "--axi-util-col",
        default="axi_util_pct",
        help="AXI utilization percent column (0..100).",
    )
    parser.add_argument(
        "--bandwidth-col",
        default="bandwidth_gbps",
        help="Optional legacy bandwidth column if AXI/HBM utilization is absent.",
    )
    parser.add_argument("--phase-col", default="phase_state", help="Phase-state column name (0..3).")
    parser.add_argument(
        "--hbm-from-bandwidth-scale",
        type=float,
        default=3.0,
        help="Fallback scale to infer HBM utilization from legacy bandwidth.",
    )
    parser.add_argument(
        "--axi-from-bandwidth-scale",
        type=float,
        default=2.5,
        help="Fallback scale to infer AXI utilization from legacy bandwidth.",
    )
    return parser.parse_args()


def clamp_phase(value: str) -> int:
    phase = int(float(value))
    if phase < 0:
        return 0
    if phase > 3:
        return 3
    return phase


def clamp_util(value: float) -> float:
    if value < 0.0:
        return 0.0
    if value > 100.0:
        return 100.0
    return value


def main() -> None:
    args = parse_args()
    in_path = Path(args.input).expanduser().resolve()
    out_path = Path(args.output).expanduser().resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    rows = []
    with in_path.open("r", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        required = [args.temperature_col, args.phase_col]
        missing = [name for name in required if name not in reader.fieldnames]
        if missing:
            missing_str = ", ".join(missing)
            raise ValueError(f"Input CSV is missing columns: {missing_str}")

        has_timestamp = args.timestamp_col in (reader.fieldnames or [])
        has_cycle = args.cycle_col in (reader.fieldnames or [])
        if not has_timestamp and not has_cycle:
            raise ValueError(
                "Input CSV must include either a timestamp column or a cycle column."
            )

        has_hbm = args.hbm_util_col in (reader.fieldnames or [])
        has_axi = args.axi_util_col in (reader.fieldnames or [])
        has_bandwidth = args.bandwidth_col in (reader.fieldnames or [])
        if not (has_hbm and has_axi) and not has_bandwidth:
            raise ValueError(
                "Input CSV must provide either both HBM/AXI utilization columns "
                "or the legacy bandwidth column for fallback inference."
            )

        for row in reader:
            if has_timestamp:
                timestamp_s = float(row[args.timestamp_col])
                cycle = int(round(timestamp_s * args.clock_hz))
            else:
                cycle = int(round(float(row[args.cycle_col])))
            temperature_c = float(row[args.temperature_col])
            phase_state = clamp_phase(row[args.phase_col])
            if has_hbm and has_axi:
                hbm_util_pct = clamp_util(float(row[args.hbm_util_col]))
                axi_util_pct = clamp_util(float(row[args.axi_util_col]))
            else:
                bandwidth = float(row[args.bandwidth_col])
                hbm_util_pct = clamp_util(bandwidth * args.hbm_from_bandwidth_scale)
                axi_util_pct = clamp_util(bandwidth * args.axi_from_bandwidth_scale)
            rows.append((cycle, hbm_util_pct, axi_util_pct, temperature_c, phase_state))

    rows.sort(key=lambda item: item[0])
    dedup_rows = []
    last_cycle = None
    for item in rows:
        if last_cycle is not None and item[0] < last_cycle:
            raise ValueError("Cycle values must be monotonic after sorting.")
        if last_cycle is not None and item[0] == last_cycle:
            dedup_rows[-1] = item
        else:
            dedup_rows.append(item)
            last_cycle = item[0]

    with out_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            ["cycle", "hbm_util_pct", "axi_util_pct", "temperature_c", "phase_state"]
        )
        writer.writerows(dedup_rows)

    print(f"Wrote {len(dedup_rows)} tier-pressure trace rows to {out_path}")


if __name__ == "__main__":
    main()
