`timescale 1ns / 1ps
module tb_Kairos_Phase_Detector;

    logic clk;
    logic rst_n;
    
    logic [31:0] compute_ops_delta;
    logic [31:0] mcycle_delta;
    logic [31:0] uma_miss_delta;
    logic [31:0] uma_hit_delta;
    
    wire [1:0] phase;
    wire       phase_changed;
    
    Kairos_Phase_Detector dut (
        .clk(clk), .rst_n(rst_n),
        .compute_ops_delta(compute_ops_delta),
        .mcycle_delta(mcycle_delta),
        .uma_miss_delta(uma_miss_delta),
        .uma_hit_delta(uma_hit_delta),
        .phase(phase),
        .phase_changed(phase_changed)
    );

    always #5 clk = ~clk;

    initial begin
        clk = 0; rst_n = 0;
        compute_ops_delta = 0; mcycle_delta = 1000;
        uma_miss_delta = 0; uma_hit_delta = 0;
        
        #20 rst_n = 1;
        
        // Compute Bound
        compute_ops_delta = 800; // IPC 0.8
        uma_miss_delta = 10; uma_hit_delta = 500;
        #200;
        
        // Memory Bound
        compute_ops_delta = 50; // IPC 0.05
        uma_miss_delta = 300; uma_hit_delta = 100; // 75% miss rate
        #200;
        
        $display("Test finished. Phase=%d", phase);
        $finish;
    end
endmodule
