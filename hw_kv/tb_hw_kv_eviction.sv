`timescale 1ns / 1ps

module tb_hw_kv_eviction;
    localparam time CLK_PERIOD_NS = 10ns;
    localparam integer NUM_PAGES = 16;
    localparam integer PAGE_ID_WIDTH = 4;
    localparam integer PAGE_COUNT_WIDTH = 5;
    localparam integer ADDR_WIDTH = 64;
    localparam integer PAGE_BYTES = 4096;
    localparam integer DMA_PAGE_LATENCY_CYCLES = 4;
    localparam integer TARGET_FLUSH_PAGES = 6;

    localparam integer SW_INTERRUPT_ENTRY_CYCLES = 900;
    localparam integer SW_ISR_TO_DRIVER_CYCLES = 1400;
    localparam integer SW_PAGE_TABLE_SCAN_CYCLES = NUM_PAGES * 25;
    localparam integer SW_DRIVER_METADATA_CYCLES = TARGET_FLUSH_PAGES * 120;
    localparam integer SW_DMA_DOORBELL_CYCLES = 350;
    localparam integer SW_DMA_TRANSFER_CYCLES = TARGET_FLUSH_PAGES * DMA_PAGE_LATENCY_CYCLES;
    localparam integer SW_COMPLETION_RECONCILE_CYCLES = 850;
    localparam integer SW_TOTAL_CYCLES =
        SW_INTERRUPT_ENTRY_CYCLES
        + SW_ISR_TO_DRIVER_CYCLES
        + SW_PAGE_TABLE_SCAN_CYCLES
        + SW_DRIVER_METADATA_CYCLES
        + SW_DMA_DOORBELL_CYCLES
        + SW_DMA_TRANSFER_CYCLES
        + SW_COMPLETION_RECONCILE_CYCLES;

    logic clk;
    logic rst_n;
    logic page_fill_valid;
    logic [PAGE_ID_WIDTH-1:0] page_fill_id;
    logic page_access_valid;
    logic [PAGE_ID_WIDTH-1:0] page_access_id;
    logic age_tick;
    logic capacity_exhausted;
    logic l1_thermal_trip;
    logic [PAGE_COUNT_WIDTH-1:0] flush_target_pages;
    logic [ADDR_WIDTH-1:0] host_dram_base_addr;
    logic dirty_clear_valid;
    logic [PAGE_ID_WIDTH-1:0] dirty_clear_id;
    logic busy;
    logic eviction_done;
    logic dma_cmd_valid;
    logic [PAGE_ID_WIDTH-1:0] dma_page_id;
    logic [ADDR_WIDTH-1:0] dma_host_addr;
    logic dma_to_host_dram;
    logic [PAGE_COUNT_WIDTH-1:0] flushed_pages;
    logic [NUM_PAGES-1:0] dirty_page_bitmap;
    logic [NUM_PAGES-1:0] resident_page_bitmap;
    logic [(NUM_PAGES*2)-1:0] reuse_counters_flat;
    logic host_interrupt;

    logic start_sw;
    logic sw_busy;
    logic sw_done;
    integer sw_remaining;

    longint unsigned cycle_q;
    longint unsigned trigger_cycle;
    longint unsigned hw_done_cycle;
    longint unsigned sw_done_cycle;
    longint unsigned hw_cycles;
    longint unsigned sw_cycles;
    longint unsigned cycles_saved;
    integer host_interrupt_count;
    integer dma_cmd_count;
    integer timeout_cycles;
    integer dirty_count;
    integer i;
    bit hw_done_seen;
    bit sw_done_seen;

    hw_kv_eviction_engine #(
        .NUM_PAGES(NUM_PAGES),
        .PAGE_ID_WIDTH(PAGE_ID_WIDTH),
        .PAGE_COUNT_WIDTH(PAGE_COUNT_WIDTH),
        .ADDR_WIDTH(ADDR_WIDTH),
        .PAGE_BYTES(PAGE_BYTES),
        .DMA_PAGE_LATENCY_CYCLES(DMA_PAGE_LATENCY_CYCLES)
    ) dut (
        .clk(clk),
        .rst_n(rst_n),
        .page_fill_valid(page_fill_valid),
        .page_fill_id(page_fill_id),
        .page_access_valid(page_access_valid),
        .page_access_id(page_access_id),
        .age_tick(age_tick),
        .capacity_exhausted(capacity_exhausted),
        .l1_thermal_trip(l1_thermal_trip),
        .flush_target_pages(flush_target_pages),
        .host_dram_base_addr(host_dram_base_addr),
        .dirty_clear_valid(dirty_clear_valid),
        .dirty_clear_id(dirty_clear_id),
        .busy(busy),
        .eviction_done(eviction_done),
        .dma_cmd_valid(dma_cmd_valid),
        .dma_page_id(dma_page_id),
        .dma_host_addr(dma_host_addr),
        .dma_to_host_dram(dma_to_host_dram),
        .flushed_pages(flushed_pages),
        .dirty_page_bitmap(dirty_page_bitmap),
        .resident_page_bitmap(resident_page_bitmap),
        .reuse_counters_flat(reuse_counters_flat),
        .host_interrupt(host_interrupt)
    );

    always #(CLK_PERIOD_NS / 2) clk = ~clk;

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            cycle_q <= 0;
        end else begin
            cycle_q <= cycle_q + 1;
        end
    end

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            sw_busy <= 1'b0;
            sw_done <= 1'b0;
            sw_remaining <= 0;
        end else begin
            sw_done <= 1'b0;
            if (start_sw) begin
                sw_busy <= 1'b1;
                sw_remaining <= SW_TOTAL_CYCLES;
            end else if (sw_busy) begin
                if (sw_remaining <= 1) begin
                    sw_busy <= 1'b0;
                    sw_done <= 1'b1;
                    sw_remaining <= 0;
                end else begin
                    sw_remaining <= sw_remaining - 1;
                end
            end
        end
    end

    always @(posedge clk) begin
        if (rst_n) begin
            if (host_interrupt) begin
                host_interrupt_count = host_interrupt_count + 1;
            end
            if (dma_cmd_valid) begin
                dma_cmd_count = dma_cmd_count + 1;
                if (!dma_to_host_dram) begin
                    $display("FAIL: DMA command was not marked as host-DRAM bound.");
                    $finish;
                end
            end
            if (eviction_done && !hw_done_seen) begin
                hw_done_seen = 1'b1;
                hw_done_cycle = cycle_q;
            end
            if (sw_done && !sw_done_seen) begin
                sw_done_seen = 1'b1;
                sw_done_cycle = cycle_q;
            end
        end
    end

    task automatic touch_page(input integer page_id, input integer repeats);
        integer k;
        begin
            for (k = 0; k < repeats; k = k + 1) begin
                @(negedge clk);
                page_access_id = page_id[PAGE_ID_WIDTH-1:0];
                page_access_valid = 1'b1;
                @(negedge clk);
                page_access_valid = 1'b0;
            end
        end
    endtask

    task automatic fill_page(input integer page_id);
        begin
            @(negedge clk);
            page_fill_id = page_id[PAGE_ID_WIDTH-1:0];
            page_fill_valid = 1'b1;
            @(negedge clk);
            page_fill_valid = 1'b0;
        end
    endtask

    task automatic age_all_pages_once();
        begin
            @(negedge clk);
            age_tick = 1'b1;
            @(negedge clk);
            age_tick = 1'b0;
        end
    endtask

    task automatic fail_if(input bit condition, input string message);
        begin
            if (condition) begin
                $display("FAIL: %s", message);
                $finish;
            end
        end
    endtask

    initial begin
        clk = 1'b0;
        rst_n = 1'b0;
        page_fill_valid = 1'b0;
        page_fill_id = {PAGE_ID_WIDTH{1'b0}};
        page_access_valid = 1'b0;
        page_access_id = {PAGE_ID_WIDTH{1'b0}};
        age_tick = 1'b0;
        capacity_exhausted = 1'b0;
        l1_thermal_trip = 1'b0;
        flush_target_pages = TARGET_FLUSH_PAGES[PAGE_COUNT_WIDTH-1:0];
        host_dram_base_addr = 64'h0000_8000_0000_0000;
        dirty_clear_valid = 1'b0;
        dirty_clear_id = {PAGE_ID_WIDTH{1'b0}};
        start_sw = 1'b0;
        host_interrupt_count = 0;
        dma_cmd_count = 0;
        hw_done_seen = 1'b0;
        sw_done_seen = 1'b0;
        trigger_cycle = 0;
        hw_done_cycle = 0;
        sw_done_cycle = 0;

        repeat (5) @(posedge clk);
        rst_n = 1'b1;
        repeat (2) @(posedge clk);

        // Fill HBM to capacity. Each fill installs a resident KV page with a
        // weakly warm 01 counter; one age epoch then lets untouched pages decay.
        for (i = 0; i < NUM_PAGES; i = i + 1) begin
            fill_page(i);
        end
        age_all_pages_once();

        // Pages 0, 3, 5, and 11 are recently reused. All other resident pages
        // remain at the coldest 2-bit counter value, 00.
        touch_page(0, 3);
        touch_page(3, 2);
        touch_page(5, 3);
        touch_page(11, 1);

        @(negedge clk);
        trigger_cycle = cycle_q;
        capacity_exhausted = 1'b1;
        start_sw = 1'b1;
        @(negedge clk);
        capacity_exhausted = 1'b0;
        start_sw = 1'b0;

        timeout_cycles = 0;
        while ((!hw_done_seen || !sw_done_seen) && (timeout_cycles < 20000)) begin
            @(posedge clk);
            timeout_cycles = timeout_cycles + 1;
        end

        fail_if(!hw_done_seen, "hardware-native eviction did not complete before timeout");
        fail_if(!sw_done_seen, "software-interrupt eviction model did not complete before timeout");
        fail_if(host_interrupt_count != 0, "hardware eviction asserted a host interrupt");
        fail_if(dma_cmd_count != TARGET_FLUSH_PAGES, "unexpected number of DMA flush commands");
        fail_if(flushed_pages != TARGET_FLUSH_PAGES[PAGE_COUNT_WIDTH-1:0], "flushed_pages does not match target");
        fail_if(dirty_page_bitmap != 16'h01D6, "dirty bitmap did not mark the expected cold pages");
        fail_if((dirty_page_bitmap & 16'h0829) != 16'h0000, "a hot page was evicted");
        fail_if(resident_page_bitmap != 16'hFE29, "resident bitmap did not clear flushed cold pages");

        dirty_count = 0;
        for (i = 0; i < NUM_PAGES; i = i + 1) begin
            if (dirty_page_bitmap[i]) begin
                dirty_count = dirty_count + 1;
            end
        end

        hw_cycles = hw_done_cycle - trigger_cycle;
        sw_cycles = sw_done_cycle - trigger_cycle;
        cycles_saved = sw_cycles - hw_cycles;

        $display("PASS: HBM capacity exhaustion drove autonomous hardware-native KV eviction.");
        $display("Cycle-accurate eviction comparison:");
        $display("  Cold pages requested/flushed: %0d/%0d", TARGET_FLUSH_PAGES, dirty_count);
        $display("  Standard software-interrupt OS eviction cycles: %0d", sw_cycles);
        $display("  Hardware-native DMA eviction cycles: %0d", hw_cycles);
        $display("  Cycles saved: %0d", cycles_saved);
        $display("  Host CPU interrupts observed: %0d", host_interrupt_count);
        $write("  Dirty page IDs:");
        for (i = 0; i < NUM_PAGES; i = i + 1) begin
            if (dirty_page_bitmap[i]) begin
                $write(" %0d", i);
            end
        end
        $write("\n");
        $display("  Dirty-page bitmap: 0x%0h", dirty_page_bitmap);
        $display("  Resident-page bitmap after flush: 0x%0h", resident_page_bitmap);
        $finish;
    end
endmodule
