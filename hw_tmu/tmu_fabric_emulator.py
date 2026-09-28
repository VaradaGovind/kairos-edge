"""Software emulator for the Kairos OOB Thermal Fabric and hierarchical TMU.

The model is intentionally deterministic and keeps the control-path semantics
close to the v4.0 specification:
- 3-tier thermal control topology
- lateral chiplet coupling with a simple resistance/capacitance update
- 256-entry OOB FIFO for macro advisories
- Layer 1 micro events bypass the FIFO entirely
- FIFO overflow uses the "Drop-Newest Macro, Preserve Micro" policy
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, List, Mapping, Sequence


@dataclass(frozen=True)
class MacroAdvisory:
    timestamp_s: float
    tenant_id: str
    priority: int
    layer: int
    payload: Mapping[str, Any]


@dataclass(frozen=True)
class MicroSafetyEvent:
    timestamp_s: float
    tenant_id: str
    event_kind: str
    executed_immediately: bool = True
    payload: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class FabricRecord:
    kind: str
    timestamp_s: float
    executed_at_s: float
    stall_s: float
    dropped: bool
    payload: Mapping[str, Any]


@dataclass
class OOBThermalFabric:
    capacity: int = 256
    interrupt_coalescing_hz: float = 1000.0
    queue: Deque[MacroAdvisory] = field(default_factory=deque)
    host_interrupt_log: List[float] = field(default_factory=list)
    executed_records: List[FabricRecord] = field(default_factory=list)
    drop_count: int = 0
    coalesced_interrupt_count: int = 0
    last_host_interrupt_s: float = 0.0

    def _should_interrupt(self, now_s: float) -> bool:
        if self.interrupt_coalescing_hz <= 0:
            return True
        min_interval_s = 1.0 / self.interrupt_coalescing_hz
        return (now_s - self.last_host_interrupt_s) >= min_interval_s

    def submit_macro_advisory(self, advisory: MacroAdvisory) -> FabricRecord:
        if len(self.queue) >= self.capacity:
            self.drop_count += 1
            record = FabricRecord(
                kind="macro",
                timestamp_s=advisory.timestamp_s,
                executed_at_s=advisory.timestamp_s,
                stall_s=0.0,
                dropped=True,
                payload=dict(advisory.payload),
            )
            self.executed_records.append(record)
            return record

        self.queue.append(advisory)
        if self._should_interrupt(advisory.timestamp_s):
            self.last_host_interrupt_s = advisory.timestamp_s
            self.host_interrupt_log.append(advisory.timestamp_s)
        else:
            self.coalesced_interrupt_count += 1

        record = FabricRecord(
            kind="macro",
            timestamp_s=advisory.timestamp_s,
            executed_at_s=advisory.timestamp_s,
            stall_s=0.0,
            dropped=False,
            payload=dict(advisory.payload),
        )
        self.executed_records.append(record)
        return record

    def submit_micro_event(self, event: MicroSafetyEvent) -> FabricRecord:
        record = FabricRecord(
            kind="micro",
            timestamp_s=event.timestamp_s,
            executed_at_s=event.timestamp_s,
            stall_s=0.0,
            dropped=False,
            payload={
                "tenant_id": event.tenant_id,
                "event_kind": event.event_kind,
                **(dict(event.payload) if event.payload else {}),
            },
        )
        self.executed_records.append(record)
        return record

    def flush_host_interrupts(self, now_s: float) -> int:
        if not self.queue:
            return 0
        if not self._should_interrupt(now_s):
            self.coalesced_interrupt_count += 1
            return 0

        self.last_host_interrupt_s = now_s
        self.host_interrupt_log.append(now_s)
        count = len(self.queue)
        self.queue.clear()
        return count


@dataclass
class HierarchicalMIMOTMU:
    ambient_c: float = 25.0
    lateral_resistance_ohm: Sequence[Sequence[float]] = field(default_factory=list)
    capacitance_j_per_c: Sequence[float] = field(default_factory=list)
    chiplet_temps_c: List[float] = field(default_factory=lambda: [25.0, 25.0, 25.0])
    alpha: float = 0.5

    def __post_init__(self) -> None:
        if not self.chiplet_temps_c:
            self.chiplet_temps_c = [self.ambient_c, self.ambient_c, self.ambient_c]
        if not self.capacitance_j_per_c:
            self.capacitance_j_per_c = [32.0] * len(self.chiplet_temps_c)

    def step(self, *, dt_s: float, compute_power_w: float, memory_power_w: float) -> List[float]:
        if dt_s <= 0:
            dt_s = 1e-6

        n = len(self.chiplet_temps_c)
        if n == 0:
            return []

        next_temps = list(self.chiplet_temps_c)
        for idx in range(n):
            power_share = compute_power_w if idx == 0 else memory_power_w / max(1, n - 1)
            leakage = 0.0
            for peer_idx in range(n):
                if peer_idx == idx:
                    continue
                resistance = 1.0
                if self.lateral_resistance_ohm and idx < len(self.lateral_resistance_ohm):
                    row = self.lateral_resistance_ohm[idx]
                    if peer_idx < len(row) and row[peer_idx] > 0:
                        resistance = float(row[peer_idx])
                capacitance = float(self.capacitance_j_per_c[min(idx, len(self.capacitance_j_per_c) - 1)])
                leakage += (self.chiplet_temps_c[peer_idx] - self.chiplet_temps_c[idx]) / max(1e-9, resistance * capacitance)

            capacitance = float(self.capacitance_j_per_c[min(idx, len(self.capacitance_j_per_c) - 1)])
            thermal_drive = (power_share / max(1e-9, capacitance)) * dt_s
            next_temps[idx] = self.chiplet_temps_c[idx] + thermal_drive + (leakage * dt_s)

        self.chiplet_temps_c = next_temps
        compute_activity = max(0.0, compute_power_w)
        memory_activity = max(0.0, memory_power_w)
        total_activity = compute_activity + memory_activity
        if total_activity > 0:
            self.alpha = compute_activity / total_activity
        return list(self.chiplet_temps_c)

    def biphasic_alpha(self, *, compute_activity: float, memory_activity: float) -> float:
        total = max(1e-9, compute_activity + memory_activity)
        return max(0.0, min(1.0, compute_activity / total))

    def predict_future_temp(self, horizon_s: float, dt_s: float, compute_power_w: float, memory_power_w: float) -> List[float]:
        """Predicts T_pred over a horizon without modifying current state."""
        if horizon_s <= 0 or dt_s <= 0:
            return list(self.chiplet_temps_c)
            
        n = len(self.chiplet_temps_c)
        if n == 0:
            return []
            
        pred_temps = list(self.chiplet_temps_c)
        steps = max(1, int(horizon_s / dt_s))
        
        for _ in range(steps):
            next_temps = list(pred_temps)
            for idx in range(n):
                power_share = compute_power_w if idx == 0 else memory_power_w / max(1, n - 1)
                leakage = 0.0
                for peer_idx in range(n):
                    if peer_idx == idx:
                        continue
                    resistance = 1.0
                    if self.lateral_resistance_ohm and idx < len(self.lateral_resistance_ohm):
                        row = self.lateral_resistance_ohm[idx]
                        if peer_idx < len(row) and row[peer_idx] > 0:
                            resistance = float(row[peer_idx])
                    capacitance = float(self.capacitance_j_per_c[min(idx, len(self.capacitance_j_per_c) - 1)])
                    leakage += (pred_temps[peer_idx] - pred_temps[idx]) / max(1e-9, resistance * capacitance)

                capacitance = float(self.capacitance_j_per_c[min(idx, len(self.capacitance_j_per_c) - 1)])
                thermal_drive = (power_share / max(1e-9, capacitance)) * dt_s
                next_temps[idx] = pred_temps[idx] + thermal_drive + (leakage * dt_s)
            pred_temps = next_temps
            
        return pred_temps

    def check_anticipatory_evict(self, horizon_s: float, dt_s: float, compute_power_w: float, memory_power_w: float, thermal_trip_threshold: float) -> bool:
        """Forecasts if thermal trip will be reached, triggering anticipatory evacuation."""
        pred_temps = self.predict_future_temp(horizon_s, dt_s, compute_power_w, memory_power_w)
        if not pred_temps:
            return False
        return max(pred_temps) >= thermal_trip_threshold
