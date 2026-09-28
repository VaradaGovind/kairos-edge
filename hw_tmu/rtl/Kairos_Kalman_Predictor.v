`timescale 1ns / 1ps

// ============================================================================
// Kairos-Edge - Kalman Predictor (Pipelined / Synthesis-Safe)
// Adapted from Prolepsis.
// 
// Uses shift-based approximation for Kalman gain to avoid division.
// ============================================================================

module Kairos_Kalman_Predictor (
    input  wire        clk,
    input  wire        rst_n,
    
    // Sensor Input (Q16.16 format)
    input  wire [31:0] t_sensor,
    
    // Performance/Power Proxy Input
    input  wire [31:0] power_proxy,
    
    // Configurable Parameters (MMIO)
    input  wire [31:0] k_f,      // State Transition Matrix F
    input  wire [31:0] k_b,      // Control-Input Model B
    input  wire [31:0] k_g,      // Environment Model G (can be used for lateral coupling)
    input  wire [31:0] k_q,      // Process Noise Covariance Q
    input  wire [31:0] k_r,      // Measurement Noise R
    input  wire [31:0] t_warn,   // Warning temp
    input  wire [31:0] t_crit,   // Critical temp
    input  wire [31:0] t_amb,    // Ambient or Effective Lateral Temp
    
    // Output
    output wire [31:0] t_estimated,
    output wire [31:0] t_predicted,
    output wire [31:0] p_uncertainty,
    output wire [1:0]  thermal_state, // 0: Normal, 1: Warning, 2: Critical
    output wire [31:0] innovation
);

    // =================================================================
    // State registers
    // =================================================================
    reg [31:0] t_est;   // Current optimal estimate (Q16.16)
    reg [31:0] p_est;   // Current estimate covariance (Q16.16)

    // =================================================================
    // Pipeline Stage 1: Prediction multiplications (1 mul each)
    // =================================================================
    (* keep = "true" *) reg [47:0] pipe1_f_t_est;
    (* keep = "true" *) reg [47:0] pipe1_b_power;
    (* keep = "true" *) reg [47:0] pipe1_g_t_amb;
    (* keep = "true" *) reg [47:0] pipe1_f_p_est;
    reg [31:0] pipe1_t_sensor;
    reg [31:0] pipe1_k_q;
    reg [31:0] pipe1_k_r;
    reg [31:0] pipe1_k_f;

    wire signed [31:0] signed_k_f      = k_f;
    wire signed [31:0] signed_t_est    = t_est;
    wire signed [31:0] signed_k_b      = k_b;
    wire signed [31:0] signed_power    = power_proxy;
    wire signed [31:0] signed_k_g      = k_g;
    wire signed [31:0] signed_t_amb    = t_amb;
    wire signed [31:0] signed_p_est    = p_est;

    always @(posedge clk) begin
        pipe1_f_t_est  <= $signed(signed_k_f) * $signed(signed_t_est);
        pipe1_b_power  <= $signed(signed_k_b) * $signed(signed_power);
        pipe1_g_t_amb  <= $signed(signed_k_g) * $signed(signed_t_amb);
        pipe1_f_p_est  <= $signed(signed_k_f) * $signed(signed_p_est);
        pipe1_t_sensor <= t_sensor;
        pipe1_k_q      <= k_q;
        pipe1_k_r      <= k_r;
        pipe1_k_f      <= k_f;
    end

    // =================================================================
    // Pipeline Stage 2: Second multiply for P prediction + sums
    // =================================================================
    reg [31:0] pipe2_t_pred;
    reg [31:0] pipe2_fp_f_q16;
    reg [31:0] pipe2_t_sensor;
    reg [31:0] pipe2_k_q;
    reg [31:0] pipe2_k_r;

    always @(posedge clk) begin
        // t_pred = F*t_est + B*power + G*t_amb  (all Q16.16)
        pipe2_t_pred   <= pipe1_f_t_est[47:16]
                        + pipe1_b_power[47:16]
                        + pipe1_g_t_amb[47:16];
        pipe2_fp_f_q16 <= ($signed(pipe1_f_p_est[47:16]) * $signed(pipe1_k_f)) >>> 16;
        pipe2_t_sensor <= pipe1_t_sensor;
        pipe2_k_q      <= pipe1_k_q;
        pipe2_k_r      <= pipe1_k_r;
    end

    // =================================================================
    // Pipeline Stage 3: Kalman gain/correction precompute
    // =================================================================
    wire [31:0] p_pred_w  = pipe2_fp_f_q16 + pipe2_k_q;
    wire [31:0] s_err_w   = p_pred_w + pipe2_k_r;
    wire [31:0] innov_w   = pipe2_t_sensor - pipe2_t_pred;

    reg [31:0] pipe3_t_pred;
    reg [31:0] pipe3_p_pred;
    reg [31:0] pipe3_s_err;
    reg [31:0] pipe3_innov;
    reg [3:0]  pipe_valid;

    wire [15:0] gain_numerator_cmp = pipe3_p_pred[31:16];
    wire [15:0] s_err_cmp          = pipe3_s_err[31:16];
    wire [3:0]  gain_shift3_w;

    assign gain_shift3_w = (pipe3_s_err == 0)                 ? 4'd0 :
                           (gain_numerator_cmp >= s_err_cmp)        ? 4'd0 :  // K ≈ 1.0
                           (gain_numerator_cmp >= (s_err_cmp >> 1)) ? 4'd1 :  // K ≈ 0.5
                           (gain_numerator_cmp >= (s_err_cmp >> 2)) ? 4'd2 :  // K ≈ 0.25
                           (gain_numerator_cmp >= (s_err_cmp >> 3)) ? 4'd3 :  // K ≈ 0.125
                           (gain_numerator_cmp >= (s_err_cmp >> 4)) ? 4'd4 :  // K ≈ 0.0625
                                                               4'd5;   // K ≈ small

    reg [31:0] pipe4_t_pred;
    reg [31:0] pipe4_p_pred;
    reg [31:0] pipe4_innov;
    reg [3:0]  pipe4_gain_shift;

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            pipe_valid      <= 4'b0000;
            pipe3_t_pred    <= 32'b0;
            pipe3_p_pred    <= 32'b0;
            pipe3_s_err     <= 32'b0;
            pipe3_innov     <= 32'b0;
            pipe4_t_pred    <= 32'b0;
            pipe4_p_pred    <= 32'b0;
            pipe4_innov     <= 32'b0;
            pipe4_gain_shift<= 4'b0;
        end else begin
            pipe_valid       <= {pipe_valid[2:0], 1'b1};
            pipe3_t_pred     <= pipe2_t_pred;
            pipe3_p_pred     <= p_pred_w;
            pipe3_s_err      <= s_err_w;
            pipe3_innov      <= innov_w;

            pipe4_t_pred     <= pipe3_t_pred;
            pipe4_p_pred     <= pipe3_p_pred;
            pipe4_innov      <= pipe3_innov;
            pipe4_gain_shift <= gain_shift3_w;
        end
    end

    wire signed [31:0] correction_w   = $signed(pipe4_innov) >>> pipe4_gain_shift;
    wire [31:0]        p_correction_w = pipe4_p_pred >> pipe4_gain_shift;
    wire [31:0]        p_est_next_w   = pipe4_p_pred - p_correction_w;

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            t_est <= t_amb;
            p_est <= 32'h00010000;  // Q16.16 = 1.0
        end else if (pipe_valid[3]) begin
            t_est <= pipe4_t_pred + correction_w;
            p_est <= p_est_next_w;
        end
    end

    // =================================================================
    // Outputs
    // =================================================================
    assign t_estimated   = t_est;
    assign t_predicted   = pipe4_t_pred;
    assign p_uncertainty = p_est;
    assign innovation    = pipe4_innov;

    reg [1:0] thermal_state_reg;
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            thermal_state_reg <= 2'd0;
        end else begin
            thermal_state_reg <= (pipe4_t_pred >= t_crit) ? 2'd2 :
                                 (pipe4_t_pred >= t_warn) ? 2'd1 : 2'd0;
        end
    end
    assign thermal_state = thermal_state_reg;

endmodule
