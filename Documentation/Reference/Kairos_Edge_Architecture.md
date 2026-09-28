# Kairos-Edge System Architecture & Theoretical Foundations

This document provides the architectural reference and mathematical foundations for the Kairos-Edge framework.

---

## 1. System Architecture

Kairos-Edge links low-level hardware co-processors with application-layer inference orchestration to solve thermal and memory bottlenecks on Unified Memory Architecture (UMA) edge silicon.

```mermaid
graph TD
    subgraph Host Software Runtime
        RM[Edge-Native Request Manager] --> TO[Tier Orchestrator / DRR Scheduler]
        TB[Thermal Token Bucket] --> TO
        SF[Safety Preemption Fallback]
    end

    subgraph Memory Management
        KV[KV Cache Manager] --> ASE[Async Shadow Evacuation]
        ASE --> DMA[DMA Controller]
    end

    subgraph Hardware Co-Processors RTL
        HWE[Hardware KV Eviction Engine] -->|Autonomous Flush| DMA
        PD[Hardware Phase Detector] -->|SYS_STATE[0]| SM[Execution Cores]
        KP[Q16.16 Kalman Predictor] --> TC[Thermal Controller]
        TC -->|Clock Gating / Warning| SM
        TC -->|Thermal Forecast| TB
    end

    TO -->|Batch Dispatch| KV
    TC -.->|Safety Preemption| SF
```

---

## 2. Mathematical Models

### 2.1 Coupled SoC Lateral Thermal Model
Because edge SoCs integrate CPU, GPU/NPU, and LPDDR memory on a single compact substrate, heat diffusion between functional blocks is tightly coupled. The hardware TMU models lateral thermal diffusion across die regions:

$$T_{soc\_i}^{k+1} = A T_{soc\_i}^{k} + B P_{soc\_i}^{k} + \left( \sum_{j \neq i} \frac{T_{soc\_j}^{k} - T_{soc\_i}^{k}}{R_{lateral\_ij} C_i} \right) \Delta t$$

where:
- $T_{soc\_i}^{k}$: Temperature of functional block $i$ at step $k$.
- $P_{soc\_i}^{k}$: Dynamic power consumption of block $i$.
- $R_{lateral\_ij}$: Lateral thermal resistance between adjacent functional units $i$ and $j$.
- $C_i$: Thermal capacitance of block $i$.
- $\Delta t$: Sampling epoch duration.

### 2.2 Thermal Token Bucket Dynamics
The runtime translates projected thermal headroom into an execution token budget:
- When thermal headroom exceeds warning thresholds ($T_{margin} \ge 15^\circ\text{C}$), the bucket replenishes at the maximum rate $R_{max}$.
- As headroom narrows ($0 < T_{margin} < 15^\circ\text{C}$), the replenishment rate decays proportionally to throttle memory bandwidth and token generation before the silicon hits thermal trip points.
- If headroom reaches zero ($T_{margin} \le 0^\circ\text{C}$), replenishment halts entirely.

---

## 3. Formal Starvation-Freedom Liveness Proof

**Theorem (Starvation Freedom):**
*Under continuous thermal saturation where the system enforces a minimum forward-progress throughput $B_{min} > 0$, any critical stream $c \in N$ will suffer starvation for at most $M$ consecutive steps, where $M$ is finite and bounded.*

### Proof
1. Assume, for contradiction, that a critical stream $c$ with service level agreement $SLA_c$ is starved indefinitely starting at step $t_0$.
2. Because stream $c$ is not scheduled, its measured latency $L_c(t)$ grows strictly monotonically:
   $$L_c(t) \ge L_c(t-1) + \Delta t$$
   where $\Delta t$ is the scheduling period.
3. For $t > t_0$, $L_c(t)$ will inevitably exceed $SLA_c$ at some finite step $t_{sla}$.
4. Beyond $t_{sla}$, the accumulated latency deficit $D_c(t)$ updates as:
   $$D_c(t) = D_c(t-1) + (L_c(t) - SLA_c)$$
5. Since $L_c(t)$ increases linearly with elapsed time, $D_c(t)$ grows **quadratically** with respect to the number of starved steps:
   $$D_c(t_0 + k) \approx D_c(t_0) + \frac{1}{2} k^2 \Delta t - k \cdot SLA_c$$
6. Let $S$ be the set of streams currently being scheduled instead of $c$. Because the minimum forward-progress budget $B_{min}$ is allocated among streams in $S$, their latencies remain bounded, meaning their deficits $D_s(t)$ grow at most linearly (or decrease when meeting their SLAs).
7. Because $D_c(t)$ grows quadratically while $D_s(t)$ for $s \in S$ is bounded or grows at most linearly, there exists a finite step $t_{sched} > t_{sla}$ such that:
   $$D_c(t_{sched}) > \max_{s \in S} D_s(t_{sched})$$
8. By definition of the Deficit Round Robin (DRR) scheduler, stream $c$ achieves the highest priority and is strictly selected, breaking the starvation condition.

**Conclusion:** The assumption of indefinite starvation produces a contradiction. Any critical stream $c$ is guaranteed execution within a bounded time frame $M$. $\blacksquare$

---

## 4. Power & Thermal Baseline Envelopes

Experiments are audited against fixed hardware envelopes specified in `configs/power_matched_budget.yaml`:
- **Board Power Cap:** ≤ 30.0 Watts (standard PoE / industrial battery envelope).
- **Thermal Warning Threshold:** 85.0 °C.
- **Thermal Critical Trip Point:** 90.0 °C.
- **PDN Stability Guardbands:** Max $di/dt \le 150.0\text{ mA}/\mu\text{s}$, Max voltage droop $\le 40.0\text{ mV}$.
