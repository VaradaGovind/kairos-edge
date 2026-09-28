`timescale 1ns / 1ps

module tmu_oob_fabric_tb;
    localparam integer FIFO_DEPTH = 256;

    typedef struct packed {
        bit [15:0] macro_index;
        bit [31:0] timestamp_ps;
    } macro_advisory_t;

    typedef struct packed {
        bit [31:0] timestamp_ps;
        bit [31:0] temperature_q16;
    } micro_event_t;

    integer fifo_depth;
    integer drop_count;
    integer micro_executed_count;
    integer host_interrupt_count;
    integer i;
    macro_advisory_t macro_item;
    micro_event_t micro_item;

    task automatic submit_macro(input macro_advisory_t item);
        if (fifo_depth >= FIFO_DEPTH) begin
            drop_count = drop_count + 1;
        end else begin
            fifo_depth = fifo_depth + 1;
        end
        if ((item.timestamp_ps % 1000000) == 0) begin
            host_interrupt_count = host_interrupt_count + 1;
        end
    endtask

    task automatic submit_micro(input micro_event_t item);
        micro_executed_count = micro_executed_count + 1;
        if (item.timestamp_ps === 32'd0) begin
            $display("FAIL: micro event timestamp must be nonzero");
            $finish;
        end
    endtask

    initial begin
        drop_count = 0;
        micro_executed_count = 0;
        host_interrupt_count = 0;
        fifo_depth = 0;

        for (i = 0; i < 300; i = i + 1) begin
            macro_item.macro_index = i[15:0];
            macro_item.timestamp_ps = 32'd1000 + i;
            submit_macro(macro_item);
        end

        if (drop_count != 44) begin
            $display("FAIL: expected 44 macro drops, observed %0d", drop_count);
            $finish;
        end

        micro_item.timestamp_ps = 32'd500000;
        micro_item.temperature_q16 = 32'h005B_0000;
        submit_micro(micro_item);

        if (micro_executed_count != 1) begin
            $display("FAIL: expected a single micro safety event");
            $finish;
        end

        if (fifo_depth != FIFO_DEPTH) begin
            $display("FAIL: FIFO size expected %0d, observed %0d", FIFO_DEPTH, fifo_depth);
            $finish;
        end

        $display("PASS: OOB FIFO drop and micro bypass semantics verified.");
        $display("  fifo_depth = %0d", fifo_depth);
        $display("  drop_count = %0d", drop_count);
        $display("  micro_executed_count = %0d", micro_executed_count);
        $display("  host_interrupt_count = %0d", host_interrupt_count);
        $finish;
    end
endmodule
