`timescale 1ns / 1ps

// ============================================================================
// Kairos-Edge - Thermal Controller (MIMO 3-Node Topology)
// Nodes: 0=CPU, 1=GPU/NPU, 2=LPDDR
//
// Integrates 3 Kalman Predictors and Phase Detectors.
// Handles Dynamic Lateral Impedance (R_lateral -> G_lateral) and PDN Pacing.
// ============================================================================

module Kairos_Thermal_Controller (
    input  wire        clk,
    input  wire        rst_n,
    
    // Core Array (3 Nodes: CPU, NPU, LPDDR)
    input  wire [95:0] t_sensor_flat,     // 3 x 32-bit (Q16.16)
    input  wire [95:0] power_proxy_flat,  // 3 x 32-bit (Q16.16)
    
    // HPC for Phase Detection
    input  wire [95:0] compute_ops_flat,  // 3 x 32-bit
    input  wire [95:0] mcycle_flat,       // 3 x 32-bit
    input  wire [95:0] uma_miss_flat,     // 3 x 32-bit
    input  wire [95:0] uma_hit_flat,      // 3 x 32-bit
    
    // MMIO Parameters - Kalman Models
    input  wire [31:0] k_f,
    input  wire [31:0] k_b,
    input  wire [31:0] k_g,
    input  wire [31:0] k_q,
    input  wire [31:0] k_r,
    input  wire [31:0] t_warn,
    input  wire [31:0] t_crit,
    
    // MMIO Parameters - Lateral Conductance (G = 1/R) (Q16.16)
    input  wire [31:0] g_01, // CPU to NPU
    input  wire [31:0] g_02, // CPU to LPDDR
    input  wire [31:0] g_10, // NPU to CPU
    input  wire [31:0] g_12, // NPU to LPDDR
    input  wire [31:0] g_20, // LPDDR to CPU
    input  wire [31:0] g_21, // LPDDR to NPU
    
    // MMIO Parameters - PDN Pacing
    input  wire [31:0] pdn_ramp_cycles,   // Microsecond-scale pacing duration
    
    // Output Throttle & Thermal State
    output wire [2:0]  clk_en,
    output wire [95:0] power_budget_flat,
    output wire [5:0]  thermal_state_flat, // 3 x 2-bit
    output wire [95:0] t_predicted_flat    // 3 x 32-bit
);

    wire [31:0] t_sens [0:2];
    wire [31:0] p_prox [0:2];
    
    wire [31:0] t_pred [0:2];
    wire [31:0] t_est  [0:2];
    wire [1:0]  th_st  [0:2];

    wire [1:0]  phase  [0:2];
    wire        phase_chg [0:2];
    
    genvar i;
    generate
        for (i = 0; i < 3; i = i + 1) begin : UNFLATTEN
            assign t_sens[i] = t_sensor_flat[i*32 +: 32];
            assign p_prox[i] = power_proxy_flat[i*32 +: 32];
            assign thermal_state_flat[i*2 +: 2] = th_st[i];
            assign t_predicted_flat[i*32 +: 32] = t_pred[i];
        end
    endgenerate

    // -----------------------------------------------------
    // Lateral Thermal Leakage Computation
    // leakage_i = sum( (T_j - T_i) * G_ij )
    // -----------------------------------------------------
    wire signed [31:0] diff_01 = $signed(t_est[1]) - $signed(t_est[0]);
    wire signed [31:0] diff_02 = $signed(t_est[2]) - $signed(t_est[0]);
    wire signed [31:0] leak_0 = ((diff_01 * $signed(g_01)) >>> 16) + ((diff_02 * $signed(g_02)) >>> 16);

    wire signed [31:0] diff_10 = $signed(t_est[0]) - $signed(t_est[1]);
    wire signed [31:0] diff_12 = $signed(t_est[2]) - $signed(t_est[1]);
    wire signed [31:0] leak_1 = ((diff_10 * $signed(g_10)) >>> 16) + ((diff_12 * $signed(g_12)) >>> 16);

    wire signed [31:0] diff_20 = $signed(t_est[0]) - $signed(t_est[2]);
    wire signed [31:0] diff_21 = $signed(t_est[1]) - $signed(t_est[2]);
    wire signed [31:0] leak_2 = ((diff_20 * $signed(g_20)) >>> 16) + ((diff_21 * $signed(g_21)) >>> 16);

    wire [31:0] t_amb_dyn [0:2];
    assign t_amb_dyn[0] = leak_0;
    assign t_amb_dyn[1] = leak_1;
    assign t_amb_dyn[2] = leak_2;

    // -----------------------------------------------------
    // Instantiate Predictors & Phase Detectors
    // -----------------------------------------------------
    generate
        for (i = 0; i < 3; i = i + 1) begin : G_NODES
            Kairos_Kalman_Predictor predictor_inst (
                .clk(clk),
                .rst_n(rst_n),
                .t_sensor(t_sens[i]),
                .power_proxy(p_prox[i]),
                .k_f(k_f),
                .k_b(k_b),
                .k_g(k_g),
                .k_q(k_q),
                .k_r(k_r),
                .t_warn(t_warn),
                .t_crit(t_crit),
                .t_amb(t_amb_dyn[i]),
                .t_estimated(t_est[i]),
                .t_predicted(t_pred[i]),
                .p_uncertainty(),
                .thermal_state(th_st[i]),
                .innovation()
            );

            Kairos_Phase_Detector phase_inst (
                .clk(clk),
                .rst_n(rst_n),
                .compute_ops_delta(compute_ops_flat[i*32 +: 32]),
                .mcycle_delta(mcycle_flat[i*32 +: 32]),
                .uma_miss_delta(uma_miss_flat[i*32 +: 32]),
                .uma_hit_delta(uma_hit_flat[i*32 +: 32]),
                .phase(phase[i]),
                .phase_changed(phase_chg[i])
            );
            
            // -----------------------------------------------------
            // PDN Pacing Logic (di/dt limitation)
            // -----------------------------------------------------
            reg [31:0] pacing_counter;
            reg        is_pacing;

            always @(posedge clk or negedge rst_n) begin
                if (!rst_n) begin
                    pacing_counter <= 32'd0;
                    is_pacing <= 1'b0;
                end else begin
                    // Trigger pacing when moving from MEMORY (1) to COMPUTE (0)
                    if (phase_chg[i] && phase[i] == 2'd0) begin
                        is_pacing <= 1'b1;
                        pacing_counter <= pdn_ramp_cycles;
                    end else if (is_pacing) begin
                        if (pacing_counter > 0)
                            pacing_counter <= pacing_counter - 1'b1;
                        else
                            is_pacing <= 1'b0;
                    end
                end
            end

            // -----------------------------------------------------
            // Clock Gating & Budgeting
            // -----------------------------------------------------
            wire l1_halt = (t_sens[i] > t_crit);
            
            // Duty cycle for thermal throttling
            reg [3:0] throttle_cnt;
            always @(posedge clk or negedge rst_n) begin
                if (!rst_n) throttle_cnt <= 4'd0;
                else throttle_cnt <= throttle_cnt + 1'b1;
            end
            
            // If predict WARNING, throttle to 75% (3 of 4 cycles)
            // If pacing, throttle to 50% temporarily to limit di/dt ramp up
            wire th_en = (th_st[i] == 2'd1) ? (throttle_cnt[1:0] != 2'b11) : 1'b1;
            wire pdn_en = is_pacing ? throttle_cnt[0] : 1'b1; 
            
            assign clk_en[i] = l1_halt ? 1'b0 : (th_en & pdn_en);
            
            // Dynamic Power Budget (Q16.16)
            // Ramp linearly if pacing
            wire [31:0] headroom = (t_warn > t_pred[i]) ? (t_warn - t_pred[i]) : 32'b0;
            assign power_budget_flat[i*32 +: 32] = is_pacing ? (headroom >> 1) : headroom;
        end
    endgenerate

endmodule
