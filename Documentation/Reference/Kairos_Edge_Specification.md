# Kairos-Edge Technical Specification

## 1. System Overview & Problem Statement

Kairos-Edge is a hardware-software co-designed inference orchestration framework tailored for running quantized Small Language Models (SLMs) and medium-sized LLMs on resource-constrained Edge AI nodes (e.g., embedded AIoT gateways, robotics SoCs).

Edge Systems-on-Chip (SoCs) operate under physical silicon constraints that break traditional datacenter inference engines:
- **Unified Memory Architecture (UMA) Capacity:** Shared CPU/GPU/NPU memory pools are limited. Unbounded Key-Value (KV) cache expansion causes Out-Of-Memory (OOM) aborts and high OS page-lock contention.
- **The Thermal Wall:** Passively cooled or restricted-airflow enclosures hit thermal saturation quickly during continuous autoregressive token decoding. Reactive OS DVFS throttling drops clock frequencies sharply, causing multi-second latency cliffs.
- **Power Delivery Network (PDN) Voltage Droops:** Rapid phase shifts between memory-bound token decoding and compute-heavy prompt prefilling cause steep current spikes ($di/dt$) that can trigger transient voltage drops and system crashes.

---

## 2. Functional & Non-Functional Requirements

### Functional Requirements
- **FR-1 (Two-Stage Asynchronous Shadow Evacuation):** The memory subsystem must stream cold KV-cache pages via DMA to secondary non-volatile storage (eMMC/NVMe) without blocking concurrent attention execution kernels.
- **FR-2 (Predictive TMU):** Dedicated hardware co-processors must monitor thermal execution telemetry and predict upcoming thermal saturation events ahead of hard OS trip points.
- **FR-3 (PDN Pacing):** The scheduler must enforce microsecond-scale linear execution ramping during phase transitions to bound $di/dt$ within the power regulator Safe Operating Area (SOA).

### Non-Functional Requirements
- **NFR-1 (Liveness Guarantee):** The system must guarantee starvation-freedom and bounded tail latency for critical real-time sensor streams under prolonged thermal stress.
- **NFR-2 (Hardware Safety Preemption):** Hardware-level thermal and electrical safety trip signals must strictly preempt user-space software directives.

---

## 3. Microarchitectural State Matrix & Signals

Hardware co-processors are coordinated via the centralized hardware control register: **`SYS_STATE[3:0]`**.

| Bit | Signal Name | Function |
| :--- | :--- | :--- |
| `SYS_STATE[0]` | **Phase Mode** | `0` = Token Decode (memory-bound); `1` = Prompt Prefill (compute-dense). Modulates prefetch strategy and issue priority. |
| `SYS_STATE[1]` | **Thermal Warning** | Asserted by the Predictive TMU when projected die temperature breaches warning threshold. Directs execution pacing and voltage step-down. |
| `SYS_STATE[2]` | **UMA Memory Pressure** | Asserted when active KV cache occupancy exceeds high-water mark. Triggers autonomous DMA evacuation. |
| `SYS_STATE[3]` | **Workload Bypass** | Enables compute bypass for zero-detection, speculative token-bypass confidence, and compressed-KV replay. |

---

## 4. Hardware Co-Processor Blocks

### 4.1 Hardware KV Eviction Engine (`hw_kv/hw_kv_eviction_engine.sv`)
- **Autonomous Page Tracking:** Employs 2-bit hardware saturation counters (`reuse_counter_q`) per physical page to track access frequency autonomously.
- **Non-Interrupt DMA Trigger:** Upon capacity pressure or an advisory trigger from the TMU, the hardware FSM scans the resident bitmap for cold pages (`2'b00`) and issues DMA flush commands directly to the DMA controller.
- **Nanosecond Commit:** The host OS is only notified via interrupt upon DMA transfer completion to update the block table, avoiding host OS lock contention during page streaming.

### 4.2 Hardware Phase Detector (`hw_tmu/rtl/Kairos_Phase_Detector.v`)
- Tracks the ratio of compute instructions to memory requests over a rolling 128-cycle window.
- Employs a 4-bit hysteresis counter to avoid phase jitter during chunked prefill batches.
- Directly drives `SYS_STATE[0]`.

### 4.3 Hardware Predictive TMU (`hw_tmu/rtl/`)
- **Kalman Predictor (`Kairos_Kalman_Predictor.v`):** Synthesizable Q16.16 fixed-point Kalman predictor computing projected die temperature:
  $$\hat{T}_{pred} = \hat{T}_{est} + K \cdot (T_{raw} - \hat{T}_{est})$$
- **Thermal Controller (`Kairos_Thermal_Controller.v`):** Generates multi-level thermal warning states and clock-enable gating (`clk_en`) to reduce active power before the silicon reaches critical trip points.

---

## 5. Memory Management & Scheduling Protocols

### 5.1 Two-Stage Asynchronous Shadow Evacuation
1. **Stage 1 (Closed-Allocation Tagging):** Eviction candidates are strictly restricted to *closed sequence allocations* (historical sequence blocks whose token span is locked). Active sequence heads continue to read these pages without page-fault hazards.
2. **Stage 2 (DMA Streaming & Commit):** Cold frames are streamed to secondary storage in the background. The OS acquires an atomic pointer-swap lock only upon transfer completion.

### 5.2 Thermal Deficit Round Robin (DRR) Scheduler
When multiple tenants compete for compute during thermal saturation:
- Each tenant $i$ tracks an accumulated latency deficit:
  $$D_i(t) = D_i(t-1) + \max(0, L_i(t) - SLA_i)$$
- The arbiter prioritizes tasks with the highest latency debt $D_i(t)$, ensuring critical real-time streams are protected while background tasks yield bandwidth.
