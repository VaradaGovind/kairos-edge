# Kairos-Edge Evaluation & Experimental Benchmarks

This document records experimental benchmarks and architectural ablation results for the Kairos-Edge framework.

---

## 1. Workload Definitions

- **W1 (AIoT Sensor Fusion):** Continuous, bursty prompt ingestion from real-time multi-camera telemetry streams. Target SLA: 50 ms.
- **W2 (Local Conversational & Control):** Low-latency interactive on-device queries. Target SLA: 100 ms.
- **W3 (Mixed-Tenant):** High-priority anomaly detection alerts running concurrently with low-priority background log processing. Target SLA: 500 ms for background tasks.

---

## 2. Architectural Ablation Results

The ablation matrix compares Full Kairos-Edge against three degraded configurations:
1. **Reactive TMU:** Predictive Kalman modeling disabled; standard reactive threshold throttling enabled.
2. **Synchronous Polling:** Asynchronous DMA shadow evacuation disabled; synchronous host CPU page eviction.
3. **No PDN Pacing:** Microsecond-scale linear dispatch ramping disabled during phase shifts.

### Measured Latency & Energy

| Workload | Configuration | TBT P50 (s) | TBT P95 (s) | Energy (J/Token) |
| :--- | :--- | :---: | :---: | :---: |
| **W1 Sensor Fusion** | **Full Kairos-Edge** | **0.03** | **0.05** | **1.07** |
| | Reactive TMU | 0.03 | 0.18 | 2.96 |
| | Synchronous Polling | 0.03 | 0.05 | 1.07 |
| | No PDN Pacing | 0.08 | 0.10 | 3.32 |
| **W2 Conversational** | **Full Kairos-Edge** | **0.05** | **0.07** | **1.25** |
| | Reactive TMU | 0.05 | 0.20 | 2.89 |
| | Synchronous Polling | 0.05 | 0.07 | 1.25 |
| | No PDN Pacing | 0.10 | 0.12 | 3.40 |
| **W3 Mixed-Tenant** | **Full Kairos-Edge** | **0.08** | **0.10** | **2.24** |
| | Reactive TMU | 0.08 | 0.23 | 4.18 |
| | Synchronous Polling | 0.08 | 0.10 | 2.24 |
| | No PDN Pacing | 0.13 | 0.15 | 4.89 |

---

## 3. Sustained 30-Minute Thermal Stress Test

A 30-minute stress test (3600 timesteps) evaluated mixed-tenant fairness during an acute thermal crisis between $t=5\text{ min}$ and $t=25\text{ min}$:
- **T1 (Sensor Fusion, SLA = 50 ms):** Preserved throughout the stress period; accumulated deficit strictly capped at 4.80s.
- **T2 (Conversational, SLA = 100 ms):** Mild degradation; accumulated deficit reached 24.01s.
- **T3 (Background Logs, SLA = 500 ms):** Absorbed the required thermal throttling; accumulated deficit reached 72.03s, yielding compute bandwidth to protect critical streams.

![Mixed-Tenant Thermal Deficit](../../Images/l5_deficits.png)

---

## 4. Hardware Simulation Results

### Hardware KV Eviction Engine (`hw_kv/tb_hw_kv_eviction.sv`)
- **Eviction Mode:** Autonomous hardware DMA stream triggered by HBM/UMA capacity exhaustion.
- **Cycle Count Comparison:**
  - Standard OS software-interrupt eviction: 4,645 cycles
  - Hardware-native DMA eviction: 35 cycles
  - **Cycles Saved:** 4,610 cycles (99.2% reduction in host CPU overhead)
  - **Host Interrupts Observed During Scan:** 0
