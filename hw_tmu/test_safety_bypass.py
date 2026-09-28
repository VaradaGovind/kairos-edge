#!/usr/bin/env python3
"""Deterministic safety-bypass test for the Kairos OOB Thermal Fabric emulator."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from hw_tmu.tmu_fabric_emulator import (  # noqa: E402
    MacroAdvisory,
    MicroSafetyEvent,
    OOBThermalFabric,
)


def main() -> int:
    fabric = OOBThermalFabric(capacity=256, interrupt_coalescing_hz=1000.0)
    base_ts = 1000.0

    for idx in range(300):
        advisory = MacroAdvisory(
            timestamp_s=base_ts + (idx * 1e-6),
            tenant_id="tenant-a",
            priority=1,
            layer=2,
            payload={"macro_index": idx},
        )
        fabric.submit_macro_advisory(advisory)

    if fabric.drop_count != 44:
        raise AssertionError(f"Expected 44 dropped macro advisories, observed {fabric.drop_count}.")

    if len(fabric.queue) != 256:
        raise AssertionError(f"Expected FIFO occupancy to saturate at 256, observed {len(fabric.queue)}.")

    micro_event = MicroSafetyEvent(
        timestamp_s=base_ts + 0.5,
        tenant_id="tenant-a",
        event_kind="dvfs_trigger",
        executed_immediately=True,
        payload={"temperature_c": 91.0},
    )
    record = fabric.submit_micro_event(micro_event)

    if record.dropped:
        raise AssertionError("Micro safety event must never be dropped.")
    if record.stall_s != 0.0:
        raise AssertionError(f"Micro safety event stalled unexpectedly: {record.stall_s}")
    if record.executed_at_s != micro_event.timestamp_s:
        raise AssertionError("Micro safety event must execute at its dispatch timestamp.")

    flushed = fabric.flush_host_interrupts(base_ts + 1.0)
    if flushed != 256:
        raise AssertionError(f"Expected flush of 256 queued macro advisories, observed {flushed}.")

    summary = {
        "drop_count": fabric.drop_count,
        "queue_depth_after_flush": len(fabric.queue),
        "micro_event_stall_s": record.stall_s,
        "micro_event_executed_at_s": record.executed_at_s,
        "coalesced_interrupts": fabric.coalesced_interrupt_count,
        "host_interrupts": len(fabric.host_interrupt_log),
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
