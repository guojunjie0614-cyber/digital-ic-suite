// =============================================================================
// Module      : rtl_module_template
// Description : One-stage valid/ready register slice. Copy this file as the
//               starting skeleton for new RTL: keep every section, replace the
//               contents, and delete sections that truly do not apply.
//
// Function    : Registers valid and payload of a valid/ready stream so the
//               downstream logic starts at a flop. This cuts the in->out data
//               and valid timing paths. ready is still combinational
//               (out_ready_i -> in_ready_o); use prim_skid_buf when the ready
//               path must be cut as well.
//
// Timing      : Latency = 1 cycle (in_valid_i && in_ready_o -> out_valid_o)
//               II      = 1 (one beat per cycle while out_ready_i stays high)
// Clocks      : clk_i  - single clock domain, rising edge.
// Resets      : rst_ni - asynchronous assert, synchronous deassert (provided
//               by prim_rst_sync upstream). Resets control state only.
// Interfaces  : in_*   - valid/ready sink:   payload qualified by in_valid_i.
//               out_*  - valid/ready source: out_valid_o never depends on
//                        out_ready_i combinationally.
// Parameters  : DATA_W - payload width in bits, legal range >= 1.
// Assumptions : Upstream holds in_valid_i and in_data_i stable until accepted.
// =============================================================================

module rtl_module_template #(
  parameter int unsigned DATA_W = 32  // Payload width in bits (>= 1)
) (
  // ---------------------------------------------------------------------------
  // Clock and reset
  // ---------------------------------------------------------------------------
  input  logic              clk_i,        // Core clock, rising edge
  input  logic              rst_ni,       // Active-low reset, async assert / sync deassert

  // ---------------------------------------------------------------------------
  // Upstream stream (sink side)
  // ---------------------------------------------------------------------------
  input  logic              in_valid_i,   // in_data_i carries a beat this cycle
  output logic              in_ready_o,   // Slice accepts the beat this cycle
  input  logic [DATA_W-1:0] in_data_i,    // Payload, meaningful only when in_valid_i

  // ---------------------------------------------------------------------------
  // Downstream stream (source side)
  // ---------------------------------------------------------------------------
  output logic              out_valid_o,  // Registered valid
  input  logic              out_ready_i,  // Downstream accepts the beat this cycle
  output logic [DATA_W-1:0] out_data_o    // Registered payload, meaningful only when out_valid_o
);

  // ===========================================================================
  // Signal declarations
  // ===========================================================================
  logic              valid_q;  // Output-stage valid   (control: has reset)
  logic [DATA_W-1:0] data_q;   // Output-stage payload (datapath: no reset)
  logic              load_en;  // A new beat is captured into data_q this cycle

  // ===========================================================================
  // Handshake control
  // ===========================================================================
  // Accept a beat when the register is empty or is drained in the same cycle.
  // This keeps II = 1 at the cost of the combinational out_ready_i ->
  // in_ready_o path described in the header.
  assign in_ready_o = out_ready_i || !valid_q;
  assign load_en    = in_valid_i && in_ready_o;

  // ===========================================================================
  // Sequential logic
  // ===========================================================================
  // valid_q is control state: it must leave reset low so no phantom beat is
  // emitted. It only updates when the stage can move (in_ready_o).
  always_ff @(posedge clk_i or negedge rst_ni) begin : p_valid_q
    if (!rst_ni) begin
      valid_q <= 1'b0;
    end else if (in_ready_o) begin
      valid_q <= in_valid_i;
    end
  end

  // data_q has no reset on purpose: it is observed only while valid_q is high.
  // Skipping the reset saves area and lets synthesis map load_en to a clock
  // gate (ICG) instead of a feedback mux.
  always_ff @(posedge clk_i) begin : p_data_q
    if (load_en) begin
      data_q <= in_data_i;
    end
  end

  // ===========================================================================
  // Outputs
  // ===========================================================================
  assign out_valid_o = valid_q;
  assign out_data_o  = data_q;

  // ===========================================================================
  // Assertions (simulation only; macros from templates/prim_cells.sv)
  // ===========================================================================
  // valid must never be X after reset, and payload must be known while valid.
  `ASSERT_KNOWN(a_out_valid_known, out_valid_o, clk_i, rst_ni)
  `ASSERT_KNOWN(a_out_data_known, out_valid_o ? out_data_o : '0, clk_i, rst_ni)

  // Protocol: a beat that is offered but not taken stays put until accepted.
  // Waiver: the property samples rst_ni synchronously (disable iff), which the
  // lint tool flags as SYNCASYNCNET. It is assertion-only, never hardware.
`ifndef SYNTHESIS
  /* verilator lint_off SYNCASYNCNET */
  a_out_hold_until_accepted : assert property (
    @(posedge clk_i) disable iff (!rst_ni)
    (out_valid_o && !out_ready_i) |=> (out_valid_o && $stable(out_data_o))
  ) else $fatal(1, "out beat changed or dropped before out_ready_i");
  /* verilator lint_on SYNCASYNCNET */
`endif

endmodule
