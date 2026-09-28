`timescale 1ns / 1ps
module tb_Kairos_Thermal_Controller;

    logic clk;
    logic rst_n;
    
    logic [95:0] t_sensor_flat;
    logic [95:0] power_proxy_flat;
    logic [95:0] compute_ops_flat;
    logic [95:0] mcycle_flat;
    logic [95:0] uma_miss_flat;
    logic [95:0] uma_hit_flat;
    
    logic [31:0] k_f, k_b, k_g, k_q, k_r, t_warn, t_crit;
    logic [31:0] g_01, g_02, g_10, g_12, g_20, g_21;
    logic [31:0] pdn_ramp_cycles;
    
    wire [2:0]  clk_en;
    wire [95:0] power_budget_flat;
    wire [5:0]  thermal_state_flat;
    wire [95:0] t_predicted_flat;

    Kairos_Thermal_Controller dut (
        .clk(clk), .rst_n(rst_n),
        .t_sensor_flat(t_sensor_flat), .power_proxy_flat(power_proxy_flat),
        .compute_ops_flat(compute_ops_flat), .mcycle_flat(mcycle_flat),
        .uma_miss_flat(uma_miss_flat), .uma_hit_flat(uma_hit_flat),
        .k_f(k_f), .k_b(k_b), .k_g(k_g), .k_q(k_q), .k_r(k_r),
        .t_warn(t_warn), .t_crit(t_crit),
        .g_01(g_01), .g_02(g_02), .g_10(g_10), .g_12(g_12), .g_20(g_20), .g_21(g_21),
        .pdn_ramp_cycles(pdn_ramp_cycles),
        .clk_en(clk_en), .power_budget_flat(power_budget_flat),
        .thermal_state_flat(thermal_state_flat), .t_predicted_flat(t_predicted_flat)
    );

    always #5 clk = ~clk;

    initial begin
        clk = 0; rst_n = 0;
        t_sensor_flat = 0; power_proxy_flat = 0;
        compute_ops_flat = 0; mcycle_flat = {32'd1000, 32'd1000, 32'd1000};
        uma_miss_flat = 0; uma_hit_flat = 0;
        
        k_f = 32'h00010000; k_b = 32'h00001000; k_g = 32'h00001000;
        k_q = 32'h00000100; k_r = 32'h00000100;
        t_warn = 32'h00500000; t_crit = 32'h005A0000; // 80C, 90C
        
        g_01 = 32'h00001000; g_02 = 32'h00001000; 
        g_10 = 32'h00001000; g_12 = 32'h00001000; 
        g_20 = 32'h00001000; g_21 = 32'h00001000;
        pdn_ramp_cycles = 100;
        
        #20 rst_n = 1;
        
        // Stimulus
        // Node 0 running hot
        t_sensor_flat = {32'h00190000, 32'h00200000, 32'h00450000}; // N2: 25C, N1: 32C, N0: 69C
        power_proxy_flat = {32'h00010000, 32'h00010000, 32'h00050000};
        
        #200;
        $display("Test finished. clk_en=%b, thermal_state=%x", clk_en, thermal_state_flat);
        $finish;
    end
endmodule
