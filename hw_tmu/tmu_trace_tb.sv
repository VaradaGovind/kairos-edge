`timescale 1ns / 1ps

// ============================================================================
// Kairos Tier-Pressure Trace-Driven Testbench
//
// Primary trigger path:
//   - HBM capacity utilization
//   - AXI utilization
//
// Secondary advisory:
//   - thermal warning
//
// Trace format (preferred):
//   cycle,hbm_util_pct,axi_util_pct,temperature_c,phase_state
//
// Legacy compatibility format:
//   cycle,temperature_c,bandwidth_gbps,phase_state
// ============================================================================

module kairos_dynamic_eviction_fsm (
    input  logic               clk,
    input  logic               rst_n,
    input  logic               sample_en,
    input  logic signed [31:0] hbm_util_q16,
    input  logic signed [31:0] axi_util_q16,
    input  logic signed [31:0] thermal_c_q16,
    input  logic        [1:0]  phase_state,
    input  logic        [63:0] sim_cycle,
    output logic               tier_event_irq,
    output logic               event_fired,
    output logic        [63:0] event_cycle,
    output logic        [3:0]  event_cause,
    output logic               compression_enable,
    output logic signed [31:0] pressure_score_q16,
    output logic signed [31:0] shadow_hbm_util_q16,
    output logic signed [31:0] shadow_axi_util_q16,
    output logic signed [31:0] shadow_thermal_c_q16,
    output logic        [1:0]  state_dbg
);
    localparam logic signed [31:0] HBM_WARN_Q16  = 32'sh0058_0000; // 88%
    localparam logic signed [31:0] HBM_CRIT_Q16  = 32'sh005E_0000; // 94%
    localparam logic signed [31:0] AXI_WARN_Q16  = 32'sh0048_0000; // 72%
    localparam logic signed [31:0] AXI_CRIT_Q16  = 32'sh0058_0000; // 88%
    localparam logic signed [31:0] THERM_WARN_Q16 = 32'sh0055_0000; // 85C

    typedef enum logic [1:0] {
        ST_IDLE     = 2'd0,
        ST_MONITOR  = 2'd1,
        ST_EVICT    = 2'd2,
        ST_COOLDOWN = 2'd3
    } fsm_state_t;

    fsm_state_t state_q;
    logic [3:0] cooldown_q;

    logic hbm_warn;
    logic hbm_crit;
    logic axi_warn;
    logic axi_crit;
    logic therm_warn;
    logic [3:0] cause_candidate;

    always_comb begin
        hbm_warn = (hbm_util_q16 >= HBM_WARN_Q16);
        hbm_crit = (hbm_util_q16 >= HBM_CRIT_Q16);
        axi_warn = (axi_util_q16 >= AXI_WARN_Q16);
        axi_crit = (axi_util_q16 >= AXI_CRIT_Q16);
        therm_warn = (thermal_c_q16 >= THERM_WARN_Q16);

        cause_candidate = 4'b0000;
        if (hbm_warn)  cause_candidate[0] = 1'b1;
        if (axi_warn)  cause_candidate[1] = 1'b1;
        if (therm_warn) cause_candidate[2] = 1'b1;
        if (hbm_crit || axi_crit) cause_candidate[3] = 1'b1;
    end

    always_ff @(posedge clk or negedge rst_n) begin
        longint signed pressure_mix;
        if (!rst_n) begin
            tier_event_irq         <= 1'b0;
            event_fired            <= 1'b0;
            event_cycle            <= 64'd0;
            event_cause            <= 4'b0000;
            compression_enable     <= 1'b0;
            pressure_score_q16     <= 32'sd0;
            shadow_hbm_util_q16    <= 32'sd0;
            shadow_axi_util_q16    <= 32'sd0;
            shadow_thermal_c_q16   <= 32'sd0;
            state_q                <= ST_IDLE;
            cooldown_q             <= 4'd0;
        end else begin
            tier_event_irq <= 1'b0;
            compression_enable <= 1'b0;

            if (sample_en) begin
                // 44% HBM + 56% AXI weighting to bias interconnect pressure.
                pressure_mix = ($signed(hbm_util_q16) * 11) + ($signed(axi_util_q16) * 14);
                pressure_score_q16 <= pressure_mix / 25;

                shadow_hbm_util_q16  <= hbm_util_q16;
                shadow_axi_util_q16  <= axi_util_q16;
                shadow_thermal_c_q16 <= thermal_c_q16;

                case (state_q)
                    ST_IDLE: begin
                        if (hbm_warn || axi_warn) begin
                            state_q <= ST_MONITOR;
                        end else if (therm_warn) begin
                            // Thermal alone is advisory; it can arm but not dominate.
                            state_q <= ST_MONITOR;
                        end
                    end

                    ST_MONITOR: begin
                        if (hbm_warn || axi_warn || therm_warn) begin
                            state_q <= ST_EVICT;
                        end else begin
                            state_q <= ST_IDLE;
                        end
                    end

                    ST_EVICT: begin
                        tier_event_irq <= 1'b1;
                        if (!event_fired) begin
                            event_fired <= 1'b1;
                            event_cycle <= sim_cycle;
                            event_cause <= cause_candidate;
                        end
                        compression_enable <= (axi_warn || axi_crit || hbm_crit);
                        cooldown_q <= 4'd2;
                        state_q <= ST_COOLDOWN;
                    end

                    ST_COOLDOWN: begin
                        if (hbm_crit || axi_crit) begin
                            state_q <= ST_EVICT;
                        end else if (cooldown_q == 0) begin
                            state_q <= ST_IDLE;
                        end else begin
                            cooldown_q <= cooldown_q - 1'b1;
                        end
                    end

                    default: begin
                        state_q <= ST_IDLE;
                    end
                endcase
            end
        end
    end

    always_comb begin
        state_dbg = state_q;
    end
endmodule


module tmu_trace_tb;
    localparam time CLK_PERIOD_NS = 10ns;

    logic clk;
    logic rst_n;
    logic sample_event;

    logic signed [31:0] hbm_util_q16;
    logic signed [31:0] axi_util_q16;
    logic signed [31:0] thermal_c_q16;
    logic [1:0] phase_state;
    logic [63:0] sim_cycle_q;

    logic tier_event_irq;
    logic event_fired;
    logic [63:0] event_cycle;
    logic [3:0] event_cause;
    logic compression_enable;
    logic signed [31:0] pressure_score_q16;
    logic signed [31:0] shadow_hbm_util_q16;
    logic signed [31:0] shadow_axi_util_q16;
    logic signed [31:0] shadow_thermal_c_q16;
    logic [1:0] state_dbg;

    string trace_csv_path;
    string report_path;
    string legacy_report_path;
    longint unsigned cycles_per_second;
    int unsigned l2_window_seconds;

    longint unsigned trace_cycle_q[$];
    int signed trace_hbm_q16_q[$];
    int signed trace_axi_q16_q[$];
    int signed trace_temp_q16_q[$];
    logic [1:0] trace_phase_q[$];

    int signed current_hbm_q16;
    int signed current_axi_q16;
    int signed current_temp_q16;
    logic [1:0] current_phase;
    int unsigned tier_event_count;

    kairos_dynamic_eviction_fsm dut (
        .clk(clk),
        .rst_n(rst_n),
        .sample_en(sample_event),
        .hbm_util_q16(hbm_util_q16),
        .axi_util_q16(axi_util_q16),
        .thermal_c_q16(thermal_c_q16),
        .phase_state(phase_state),
        .sim_cycle(sim_cycle_q),
        .tier_event_irq(tier_event_irq),
        .event_fired(event_fired),
        .event_cycle(event_cycle),
        .event_cause(event_cause),
        .compression_enable(compression_enable),
        .pressure_score_q16(pressure_score_q16),
        .shadow_hbm_util_q16(shadow_hbm_util_q16),
        .shadow_axi_util_q16(shadow_axi_util_q16),
        .shadow_thermal_c_q16(shadow_thermal_c_q16),
        .state_dbg(state_dbg)
    );

    always #(CLK_PERIOD_NS / 2) clk = ~clk;

    function automatic int signed real_to_q16(input real v);
        real scaled;
        begin
            scaled = v * 65536.0;
            if (scaled >= 0.0) begin
                real_to_q16 = $rtoi(scaled + 0.5);
            end else begin
                real_to_q16 = $rtoi(scaled - 0.5);
            end
        end
    endfunction

    function automatic real q16_to_real(input int signed v);
        q16_to_real = $itor(v) / 65536.0;
    endfunction

    function automatic real clamp_pct(input real v);
        begin
            if (v < 0.0) begin
                clamp_pct = 0.0;
            end else if (v > 100.0) begin
                clamp_pct = 100.0;
            end else begin
                clamp_pct = v;
            end
        end
    endfunction

    task automatic push_trace_row(
        input longint unsigned cyc,
        input real hbm_pct,
        input real axi_pct,
        input real temp_c,
        input int phase_i
    );
        logic [1:0] phase_clamped;
        begin
            trace_cycle_q.push_back(cyc);
            trace_hbm_q16_q.push_back(real_to_q16(clamp_pct(hbm_pct)));
            trace_axi_q16_q.push_back(real_to_q16(clamp_pct(axi_pct)));
            trace_temp_q16_q.push_back(real_to_q16(temp_c));
            if (phase_i < 0) begin
                phase_clamped = 2'd0;
            end else if (phase_i > 3) begin
                phase_clamped = 2'd3;
            end else begin
                phase_clamped = phase_i[1:0];
            end
            trace_phase_q.push_back(phase_clamped);
        end
    endtask

    task automatic load_trace_csv(input string csv_path);
        int fd;
        reg [4095:0] line_buf;
        int parsed;
        longint unsigned cyc;
        real hbm_pct;
        real axi_pct;
        real temp_c;
        real legacy_bw;
        int phase_i;
        begin
            fd = $fopen(csv_path, "r");
            if (fd == 0) begin
                $fatal(1, "Unable to open trace CSV: %s", csv_path);
            end

            while (!$feof(fd)) begin
                line_buf = "";
                void'($fgets(line_buf, fd));

                // Preferred format:
                // cycle,hbm_util_pct,axi_util_pct,temperature_c,phase_state
                parsed = $sscanf(line_buf, "%d,%f,%f,%f,%d", cyc, hbm_pct, axi_pct, temp_c, phase_i);
                if (parsed == 5) begin
                    push_trace_row(cyc, hbm_pct, axi_pct, temp_c, phase_i);
                end else begin
                    // Legacy format:
                    // cycle,temperature_c,bandwidth_gbps,phase_state
                    parsed = $sscanf(line_buf, "%d,%f,%f,%d", cyc, temp_c, legacy_bw, phase_i);
                    if (parsed == 4) begin
                        hbm_pct = legacy_bw * 3.0;
                        axi_pct = legacy_bw * 2.5;
                        push_trace_row(cyc, hbm_pct, axi_pct, temp_c, phase_i);
                    end
                end
            end
            $fclose(fd);

            if (trace_cycle_q.size() == 0) begin
                $fatal(1, "Trace CSV has no parsable rows: %s", csv_path);
            end
        end
    endtask

    task automatic step_one_cycle();
        begin
            sample_event   = 1'b1;
            hbm_util_q16   = current_hbm_q16;
            axi_util_q16   = current_axi_q16;
            thermal_c_q16  = current_temp_q16;
            phase_state    = current_phase;

            @(posedge clk);

            if (tier_event_irq) begin
                tier_event_count = tier_event_count + 1;
                $display(
                    "Tier event at cycle %0d | cause=0x%0h | hbm=%.2f%% axi=%.2f%% temp=%.2fC | compress=%0d",
                    sim_cycle_q,
                    event_cause,
                    q16_to_real(shadow_hbm_util_q16),
                    q16_to_real(shadow_axi_util_q16),
                    q16_to_real(shadow_thermal_c_q16),
                    compression_enable
                );
            end

            sim_cycle_q  = sim_cycle_q + 64'd1;
            sample_event = 1'b0;
        end
    endtask

    task automatic replay_trace();
        int i;
        longint unsigned tail_cycles;
        begin
            current_hbm_q16  = trace_hbm_q16_q[0];
            current_axi_q16  = trace_axi_q16_q[0];
            current_temp_q16 = trace_temp_q16_q[0];
            current_phase    = trace_phase_q[0];

            while (sim_cycle_q < trace_cycle_q[0]) begin
                step_one_cycle();
            end

            for (i = 1; i < trace_cycle_q.size(); i = i + 1) begin
                if (trace_cycle_q[i] < trace_cycle_q[i - 1]) begin
                    $fatal(1, "Trace cycle sequence must be non-decreasing.");
                end
                while (sim_cycle_q < trace_cycle_q[i]) begin
                    step_one_cycle();
                end

                current_hbm_q16  = trace_hbm_q16_q[i];
                current_axi_q16  = trace_axi_q16_q[i];
                current_temp_q16 = trace_temp_q16_q[i];
                current_phase    = trace_phase_q[i];
            end

            // Small tail window to catch pending monitor->evict transitions.
            tail_cycles = cycles_per_second * l2_window_seconds;
            for (i = 0; i < tail_cycles; i = i + 1) begin
                step_one_cycle();
            end
        end
    endtask

    task automatic write_report(input string out_path);
        int fd;
        begin
            fd = $fopen(out_path, "w");
            if (fd == 0) begin
                $fatal(1, "Unable to create tier event report: %s", out_path);
            end

            $fdisplay(fd, "trace_csv,%s", trace_csv_path);
            $fdisplay(fd, "cycles_per_second,%0d", cycles_per_second);
            $fdisplay(fd, "sample_period_cycles,1");
            $fdisplay(fd, "tier_event_count,%0d", tier_event_count);
            if (event_fired) begin
                $fdisplay(fd, "tier_event_cycle,%0d", event_cycle);
            end else begin
                $fdisplay(fd, "tier_event_cycle,-1");
            end
            $fdisplay(fd, "tier_event_cause_bits,0x%0h", event_cause);
            $fdisplay(fd, "final_hbm_util_pct,%.6f", q16_to_real(shadow_hbm_util_q16));
            $fdisplay(fd, "final_axi_util_pct,%.6f", q16_to_real(shadow_axi_util_q16));
            $fdisplay(fd, "final_thermal_c,%.6f", q16_to_real(shadow_thermal_c_q16));
            $fdisplay(fd, "final_pressure_score,%.6f", q16_to_real(pressure_score_q16));

            // Compatibility field consumed by old scripts.
            if (event_fired) begin
                $fdisplay(fd, "msix_cycle,%0d", event_cycle);
            end else begin
                $fdisplay(fd, "msix_cycle,-1");
            end

            $fclose(fd);
        end
    endtask

    initial begin
        clk            = 1'b0;
        rst_n          = 1'b0;
        sample_event   = 1'b0;
        hbm_util_q16   = 32'sd0;
        axi_util_q16   = 32'sd0;
        thermal_c_q16  = 32'sh0019_0000; // 25C
        phase_state    = 2'd3;
        sim_cycle_q    = 64'd0;
        tier_event_count = 0;

        trace_csv_path    = "hw_tmu/traces/thermal_critical.csv";
        report_path       = "hw_tmu/outputs/tiering_event_report.csv";
        legacy_report_path = "";
        cycles_per_second = 4096;
        l2_window_seconds = 1;

        void'($value$plusargs("TRACE_CSV=%s", trace_csv_path));
        if ($value$plusargs("TIER_REPORT=%s", report_path)) begin
            // preferred output path already captured
        end else if ($value$plusargs("MSIX_REPORT=%s", legacy_report_path)) begin
            report_path = legacy_report_path;
        end
        void'($value$plusargs("CYCLES_PER_SECOND=%d", cycles_per_second));
        void'($value$plusargs("L2_WINDOW_SECONDS=%d", l2_window_seconds));

        if (cycles_per_second == 0) begin
            $fatal(1, "CYCLES_PER_SECOND must be > 0.");
        end
        if (l2_window_seconds < 1 || l2_window_seconds > 30) begin
            $fatal(1, "L2_WINDOW_SECONDS must be in [1, 30].");
        end

        load_trace_csv(trace_csv_path);
        $display(
            "Loaded %0d trace rows. Priority triggers: HBM/AXI. Thermal is advisory.",
            trace_cycle_q.size()
        );

        repeat (4) @(posedge clk);
        rst_n = 1'b1;

        replay_trace();
        write_report(report_path);

        if (event_fired) begin
            $display(
                "PASS: tier event cycle=%0d cause=0x%0h events=%0d",
                event_cycle, event_cause, tier_event_count
            );
        end else begin
            $display("PASS: no tier event fired for this trace.");
        end

        $finish;
    end
endmodule
