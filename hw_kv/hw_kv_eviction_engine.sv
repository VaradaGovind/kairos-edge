`timescale 1ns / 1ps

// ============================================================================
// Kairos Hardware-Native KV Eviction Engine
//
// This block models the Level 2-C2 path from the v4.0 spec: cold KV page
// selection and DMA flush are handled inside the memory-controller/DMA sideband
// path. The host CPU is not interrupted; software observes progress later by
// reconciling the dirty-page bitmap.
// ============================================================================

module hw_kv_eviction_engine #(
    parameter integer NUM_PAGES = 16,
    parameter integer PAGE_ID_WIDTH = 4,
    parameter integer PAGE_COUNT_WIDTH = 5,
    parameter integer ADDR_WIDTH = 64,
    parameter integer PAGE_BYTES = 4096,
    parameter integer DMA_PAGE_LATENCY_CYCLES = 4
) (
    input  logic                              clk,
    input  logic                              rst_n,

    input  logic                              page_fill_valid,
    input  logic [PAGE_ID_WIDTH-1:0]          page_fill_id,
    input  logic                              page_access_valid,
    input  logic [PAGE_ID_WIDTH-1:0]          page_access_id,
    input  logic                              age_tick,

    input  logic                              capacity_exhausted,
    input  logic                              l1_thermal_trip,
    input  logic                              tmu_anticipatory_evict_trigger,
    input  logic [PAGE_COUNT_WIDTH-1:0]       flush_target_pages,
    input  logic [ADDR_WIDTH-1:0]             host_dram_base_addr,

    input  logic                              dirty_clear_valid,
    input  logic [PAGE_ID_WIDTH-1:0]          dirty_clear_id,

    output logic                              busy,
    output logic                              eviction_done,
    output logic                              dma_cmd_valid,
    output logic [PAGE_ID_WIDTH-1:0]          dma_page_id,
    output logic [ADDR_WIDTH-1:0]             dma_host_addr,
    output logic                              dma_to_host_dram,
    output logic [PAGE_COUNT_WIDTH-1:0]       flushed_pages,
    output logic [NUM_PAGES-1:0]              dirty_page_bitmap,
    output logic [NUM_PAGES-1:0]              resident_page_bitmap,
    output logic [(NUM_PAGES*2)-1:0]          reuse_counters_flat,
    output logic                              host_interrupt
);
    typedef enum logic [2:0] {
        ST_IDLE      = 3'd0,
        ST_SCAN      = 3'd1,
        ST_DMA_ISSUE = 3'd2,
        ST_DMA_WAIT  = 3'd3,
        ST_DONE      = 3'd4
    } state_t;

    state_t state_q;

    logic [1:0] reuse_counter_q [0:NUM_PAGES-1];
    logic [NUM_PAGES-1:0] resident_q;
    logic [NUM_PAGES-1:0] dirty_q;

    logic [PAGE_COUNT_WIDTH-1:0] scan_idx_q;
    logic [PAGE_COUNT_WIDTH-1:0] target_q;
    logic [PAGE_COUNT_WIDTH-1:0] flushed_pages_q;
    logic [PAGE_ID_WIDTH-1:0] candidate_page_q;
    logic [ADDR_WIDTH-1:0] candidate_host_addr_q;
    logic [31:0] dma_wait_q;
    logic cold_candidate;

    integer i;

    function automatic [1:0] sat_inc(input [1:0] value);
        begin
            sat_inc = (value == 2'b11) ? 2'b11 : (value + 2'b01);
        end
    endfunction

    function automatic [1:0] sat_dec(input [1:0] value);
        begin
            sat_dec = (value == 2'b00) ? 2'b00 : (value - 2'b01);
        end
    endfunction

    always_comb begin
        cold_candidate = 1'b0;
        if (scan_idx_q < NUM_PAGES) begin
            cold_candidate =
                resident_q[scan_idx_q]
                && !dirty_q[scan_idx_q]
                && (reuse_counter_q[scan_idx_q] == 2'b00);
        end
    end

    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            state_q <= ST_IDLE;
            resident_q <= {NUM_PAGES{1'b0}};
            dirty_q <= {NUM_PAGES{1'b0}};
            scan_idx_q <= {PAGE_COUNT_WIDTH{1'b0}};
            target_q <= {PAGE_COUNT_WIDTH{1'b0}};
            flushed_pages_q <= {PAGE_COUNT_WIDTH{1'b0}};
            candidate_page_q <= {PAGE_ID_WIDTH{1'b0}};
            candidate_host_addr_q <= {ADDR_WIDTH{1'b0}};
            dma_wait_q <= 32'd0;
            eviction_done <= 1'b0;
            dma_cmd_valid <= 1'b0;
            dma_page_id <= {PAGE_ID_WIDTH{1'b0}};
            dma_host_addr <= {ADDR_WIDTH{1'b0}};

            for (i = 0; i < NUM_PAGES; i = i + 1) begin
                reuse_counter_q[i] <= 2'b00;
            end
        end else begin
            eviction_done <= 1'b0;
            dma_cmd_valid <= 1'b0;

            if (age_tick) begin
                for (i = 0; i < NUM_PAGES; i = i + 1) begin
                    if (resident_q[i]) begin
                        reuse_counter_q[i] <= sat_dec(reuse_counter_q[i]);
                    end
                end
            end

            if (page_fill_valid && (page_fill_id < NUM_PAGES)) begin
                resident_q[page_fill_id] <= 1'b1;
                dirty_q[page_fill_id] <= 1'b0;
                reuse_counter_q[page_fill_id] <= 2'b01;
            end

            if (page_access_valid && (page_access_id < NUM_PAGES) && resident_q[page_access_id]) begin
                reuse_counter_q[page_access_id] <= sat_inc(reuse_counter_q[page_access_id]);
            end

            if (dirty_clear_valid && (dirty_clear_id < NUM_PAGES)) begin
                dirty_q[dirty_clear_id] <= 1'b0;
            end

            case (state_q)
                ST_IDLE: begin
                    if ((capacity_exhausted || l1_thermal_trip || tmu_anticipatory_evict_trigger) && (flush_target_pages != 0)) begin
                        state_q <= ST_SCAN;
                        scan_idx_q <= {PAGE_COUNT_WIDTH{1'b0}};
                        flushed_pages_q <= {PAGE_COUNT_WIDTH{1'b0}};
                        if (flush_target_pages > NUM_PAGES) begin
                            target_q <= NUM_PAGES;
                        end else begin
                            target_q <= flush_target_pages;
                        end
                    end
                end

                ST_SCAN: begin
                    if ((flushed_pages_q >= target_q) || (scan_idx_q >= NUM_PAGES)) begin
                        state_q <= ST_DONE;
                    end else if (cold_candidate) begin
                        candidate_page_q <= scan_idx_q[PAGE_ID_WIDTH-1:0];
                        candidate_host_addr_q <= host_dram_base_addr + (scan_idx_q * PAGE_BYTES);
                        state_q <= ST_DMA_ISSUE;
                    end else begin
                        scan_idx_q <= scan_idx_q + 1'b1;
                    end
                end

                ST_DMA_ISSUE: begin
                    dma_cmd_valid <= 1'b1;
                    dma_page_id <= candidate_page_q;
                    dma_host_addr <= candidate_host_addr_q;
                    if (DMA_PAGE_LATENCY_CYCLES <= 1) begin
                        resident_q[candidate_page_q] <= 1'b0;
                        dirty_q[candidate_page_q] <= 1'b1;
                        flushed_pages_q <= flushed_pages_q + 1'b1;
                        scan_idx_q <= scan_idx_q + 1'b1;
                        state_q <= ((flushed_pages_q + 1'b1) >= target_q) ? ST_DONE : ST_SCAN;
                    end else begin
                        dma_wait_q <= DMA_PAGE_LATENCY_CYCLES - 1;
                        state_q <= ST_DMA_WAIT;
                    end
                end

                ST_DMA_WAIT: begin
                    if (dma_wait_q <= 1) begin
                        resident_q[candidate_page_q] <= 1'b0;
                        dirty_q[candidate_page_q] <= 1'b1;
                        flushed_pages_q <= flushed_pages_q + 1'b1;
                        scan_idx_q <= scan_idx_q + 1'b1;
                        state_q <= ((flushed_pages_q + 1'b1) >= target_q) ? ST_DONE : ST_SCAN;
                    end else begin
                        dma_wait_q <= dma_wait_q - 1'b1;
                    end
                end

                ST_DONE: begin
                    eviction_done <= 1'b1;
                    state_q <= ST_IDLE;
                end

                default: begin
                    state_q <= ST_IDLE;
                end
            endcase
        end
    end

    genvar g;
    generate
        for (g = 0; g < NUM_PAGES; g = g + 1) begin : gen_counter_flatten
            assign reuse_counters_flat[(g * 2) +: 2] = reuse_counter_q[g];
        end
    endgenerate

    assign busy = (state_q != ST_IDLE);
    assign dma_to_host_dram = dma_cmd_valid;
    assign flushed_pages = flushed_pages_q;
    assign dirty_page_bitmap = dirty_q;
    assign resident_page_bitmap = resident_q;
    assign host_interrupt = 1'b0;
endmodule
