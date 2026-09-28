`timescale 1ns / 1ps

// ============================================================================
// Kairos-Edge - Workload Phase Detector (Synthesis-Safe)
// Adapted from Prolepsis for L3-C4 PDN Pacing.
//
// Classifies each node's workload phase using EMA-filtered HPC data.
// All division replaced with shift-based ratio estimation for timing closure.
//
// Phase classifications:
//   0: COMPUTE_BOUND  - High IPC, low cache/UMA miss rate
//   1: MEMORY_BOUND   - Low IPC, high cache/UMA miss rate
//   2: BALANCED        - Moderate activity
//   3: IDLE            - Near-zero activity
//
// Detects phase transitions for PDN (di/dt) pacing limiters.
// ============================================================================

module Kairos_Phase_Detector (
    input  wire        clk,
    input  wire        rst_n,
    
    // Core/NPU HPC deltas (sampled every N cycles by top-level)
    input  wire [31:0] compute_ops_delta, // Compute operations or retired instructions
    input  wire [31:0] mcycle_delta,      // Cycles elapsed in window (should be ~N)
    input  wire [31:0] uma_miss_delta,    // UMA memory fetch misses
    input  wire [31:0] uma_hit_delta,     // UMA memory hits (cache hits)
    
    // Output Phase Classification
    output wire [1:0]  phase,             // 0: COMPUTE, 1: MEMORY, 2: BALANCED, 3: IDLE
    output wire        phase_changed      // Pulse when classification changes
);

    reg [1:0] current_phase;
    reg [1:0] previous_phase;
    
    // EMA smoothers (Q16.16 format for fractional precision)
    reg [31:0] ema_long_compute;
    reg [31:0] ema_short_compute;
    reg [31:0] ema_long_miss;
    reg [31:0] ema_short_miss;
    
    // Hysteresis counter — phase must persist for M samples before change
    reg [3:0]  hysteresis_cnt;
    reg [1:0]  candidate_phase;

    wire [31:0] total_uma_access = uma_hit_delta + uma_miss_delta;
    
    // Compute Intensity category signals
    wire compute_high = (compute_ops_delta > (mcycle_delta >> 1));   // intensity > 0.5
    wire compute_low  = (compute_ops_delta < (mcycle_delta >> 3));   // intensity < 0.125
    wire compute_idle = (compute_ops_delta < (mcycle_delta >> 6));   // intensity < 0.016
    
    // Miss rate category signals
    wire miss_high = (uma_miss_delta > (total_uma_access >> 2));      // Miss > 25%
    wire miss_low  = (uma_miss_delta < (total_uma_access >> 4));      // Miss < 6%

    // EMA-filtered versions for trend detection
    wire [31:0] compute_proxy = compute_ops_delta;
    wire        ema_compute_high   = (ema_short_compute  > (mcycle_delta >> 1));
    wire        ema_compute_low    = (ema_short_compute  < (mcycle_delta >> 3));
    wire        ema_miss_high      = (ema_short_miss > (total_uma_access >> 2));
    wire        ema_miss_low       = (ema_short_miss < (total_uma_access >> 4));
    wire        compute_trend_up   = (ema_short_compute  >= ema_long_compute);
    wire        miss_trend_up      = (ema_short_miss >  ema_long_miss);

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            ema_long_compute   <= 32'd0;
            ema_short_compute  <= 32'd0;
            ema_long_miss      <= 32'd0;
            ema_short_miss     <= 32'd0;
            current_phase      <= 2'd3; // Start IDLE
            previous_phase     <= 2'd3;
            candidate_phase    <= 2'd3;
            hysteresis_cnt     <= 4'd0;
        end else begin
            // EMA updates
            ema_short_compute <= ema_short_compute + (($signed(compute_proxy)  - $signed(ema_short_compute)) >>> 3);
            ema_long_compute  <= ema_long_compute  + (($signed(compute_proxy)  - $signed(ema_long_compute))  >>> 6);
            ema_short_miss    <= ema_short_miss    + (($signed(uma_miss_delta) - $signed(ema_short_miss))    >>> 3);
            ema_long_miss     <= ema_long_miss     + (($signed(uma_miss_delta) - $signed(ema_long_miss))     >>> 6);

            // Determine candidate phase
            if (compute_idle && (ema_short_compute < (mcycle_delta >> 5))) begin
                candidate_phase <= 2'd3; // IDLE
            end else if ((compute_high || ema_compute_high || compute_trend_up) &&
                         (miss_low || ema_miss_low) &&
                         !miss_trend_up) begin
                candidate_phase <= 2'd0; // COMPUTE_BOUND
            end else if ((compute_low || ema_compute_low) &&
                         (miss_high || ema_miss_high || miss_trend_up)) begin
                candidate_phase <= 2'd1; // MEMORY_BOUND
            end else begin
                candidate_phase <= 2'd2; // BALANCED
            end
            
            // Hysteresis: require 8 consecutive matching samples
            if (candidate_phase == current_phase) begin
                hysteresis_cnt <= 4'd0;
            end else begin
                if (hysteresis_cnt >= 4'd7) begin
                    previous_phase <= current_phase;
                    current_phase  <= candidate_phase;
                    hysteresis_cnt <= 4'd0;
                end else begin
                    hysteresis_cnt <= hysteresis_cnt + 4'd1;
                end
            end
        end
    end
    
    assign phase = current_phase;
    assign phase_changed = (current_phase != previous_phase);

endmodule
