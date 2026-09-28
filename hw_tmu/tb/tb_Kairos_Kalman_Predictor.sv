`timescale 1ns / 1ps
module tb_Kairos_Kalman_Predictor;

    logic clk;
    logic rst_n;
    
    logic [31:0] t_sensor;
    logic [31:0] power_proxy;
    logic [31:0] k_f, k_b, k_g, k_q, k_r, t_warn, t_crit, t_amb;
    
    wire [31:0] t_estimated;
    wire [31:0] t_predicted;
    wire [31:0] p_uncertainty;
    wire [1:0]  thermal_state;
    wire [31:0] innovation;
    
    Kairos_Kalman_Predictor dut (
        .clk(clk), .rst_n(rst_n),
        .t_sensor(t_sensor), .power_proxy(power_proxy),
        .k_f(k_f), .k_b(k_b), .k_g(k_g), .k_q(k_q), .k_r(k_r),
        .t_warn(t_warn), .t_crit(t_crit), .t_amb(t_amb),
        .t_estimated(t_estimated), .t_predicted(t_predicted),
        .p_uncertainty(p_uncertainty), .thermal_state(thermal_state), .innovation(innovation)
    );

    always #5 clk = ~clk;

    initial begin
        clk = 0; rst_n = 0;
        t_sensor = 0; power_proxy = 0;
        k_f = 32'h00010000; k_b = 32'h00001000; k_g = 32'h00001000;
        k_q = 32'h00000100; k_r = 32'h00000100;
        t_warn = 32'h00500000; t_crit = 32'h005A0000; t_amb = 32'h00190000; // 80C, 90C, 25C
        
        #20 rst_n = 1;
        
        // Stimulus
        t_sensor = 32'h00200000; // 32C
        power_proxy = 32'h00010000;
        
        #100;
        t_sensor = 32'h00400000; // 64C
        #100;
        
        $display("Test finished. predicted=%x, estimated=%x, state=%d", t_predicted, t_estimated, thermal_state);
        $finish;
    end
endmodule
