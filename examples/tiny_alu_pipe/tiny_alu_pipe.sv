// =============================================================================
// Module      : tiny_alu_pipe
// Description : Two-stage pipelined multiply-add, y = a * b + c, on a
//               valid/ready stream.
//
// Pipeline    :   in_*  --> [S1: a*b, carry c] --> [S2: prod + c] --> out_*
//               Stage 1 registers the product and delays c to stay aligned.
//               Stage 2 registers the sum, which drives the outputs directly.
//
// Timing      : Latency = 2 cycles (in handshake -> out_valid_o)
//               II      = 1 (one beat per cycle without backpressure)
// Clocks      : clk_i  - single clock domain, rising edge.
// Resets      : rst_ni - async assert, sync deassert. Only the two valid
//               flops are reset; datapath flops are reset-free.
// Interfaces  : in_*   - valid/ready sink, operands unsigned.
//               out_*  - valid/ready source, out_valid_o is registered.
// Numerics    : a_i, b_i : unsigned W bits      -> product : 2W bits (exact)
//               c_i      : unsigned 2W bits     -> sum     : 2W+1 bits (exact,
//               one carry bit, so no overflow or saturation is needed).
// Parameters  : W - operand width, legal range >= 1.
// =============================================================================

module tiny_alu_pipe #(
  parameter int unsigned W = 8  // Operand width in bits (>= 1)
) (
  // ---------------------------------------------------------------------------
  // Clock and reset
  // ---------------------------------------------------------------------------
  input  logic           clk_i,        // Core clock, rising edge
  input  logic           rst_ni,       // Active-low reset, async assert / sync deassert

  // ---------------------------------------------------------------------------
  // Operand stream (sink side)
  // ---------------------------------------------------------------------------
  input  logic           in_valid_i,   // a_i, b_i, c_i carry a beat this cycle
  output logic           in_ready_o,   // Pipeline accepts the beat this cycle
  input  logic [W-1:0]   a_i,          // Multiplicand, unsigned
  input  logic [W-1:0]   b_i,          // Multiplier, unsigned
  input  logic [2*W-1:0] c_i,          // Addend, unsigned, aligned to the product

  // ---------------------------------------------------------------------------
  // Result stream (source side)
  // ---------------------------------------------------------------------------
  output logic           out_valid_o,  // y_o carries a result this cycle
  input  logic           out_ready_i,  // Downstream accepts the result
  output logic [2*W:0]   y_o           // a*b + c, unsigned, full precision
);

  // ===========================================================================
  // Signal declarations (grouped by pipeline stage)
  // ===========================================================================
  // Stage 1: product register
  logic             vld_s1_q;   // Stage 1 holds a valid beat
  logic             s1_en;      // Capture a new beat into stage 1
  logic [2*W-1:0]   prod_s1_d;  // a_i * b_i, combinational
  logic [2*W-1:0]   prod_s1_q;  // Registered product
  logic [2*W-1:0]   c_s1_q;     // c_i delayed one cycle to align with prod_s1_q

  // Stage 2: sum register (drives the outputs)
  logic             vld_s2_q;   // Stage 2 holds a valid result
  logic             s2_ready;   // Stage 2 can take a beat from stage 1
  logic             s2_en;      // Move stage 1 into stage 2 this cycle
  logic [2*W:0]     sum_s2_d;   // prod_s1_q + c_s1_q, combinational
  logic [2*W:0]     y_q;        // Registered result

  // ===========================================================================
  // Pipeline control
  // ===========================================================================
  // A stage can load when it is empty or when its content leaves this cycle.
  // Ready therefore ripples back combinationally from out_ready_i through both
  // stages; for deeper pipelines insert prim_skid_buf to cut that path.
  assign s2_ready   = out_ready_i || !vld_s2_q;
  assign in_ready_o = s2_ready    || !vld_s1_q;

  assign s1_en = in_valid_i && in_ready_o;
  assign s2_en = vld_s1_q   && s2_ready;

  // Valid flops are control state and must come out of reset low.
  always_ff @(posedge clk_i or negedge rst_ni) begin : p_valid
    if (!rst_ni) begin
      vld_s1_q <= 1'b0;
      vld_s2_q <= 1'b0;
    end else begin
      if (in_ready_o) vld_s1_q <= in_valid_i;
      if (s2_ready)   vld_s2_q <= vld_s1_q;
    end
  end

  // ===========================================================================
  // Stage 1: multiply
  // ===========================================================================
  // Exact unsigned product: W x W -> 2W bits, no truncation.
  assign prod_s1_d = a_i * b_i;

  // Datapath flops have no reset: they are only read while the matching valid
  // is high, and enable-only flops map to clock gating instead of muxes.
  always_ff @(posedge clk_i) begin : p_stage1
    if (s1_en) begin
      prod_s1_q <= prod_s1_d;
      c_s1_q    <= c_i;
    end
  end

  // ===========================================================================
  // Stage 2: add
  // ===========================================================================
  // Zero-extend both operands by one bit so the carry is kept (2W+1 bits).
  assign sum_s2_d = {1'b0, prod_s1_q} + {1'b0, c_s1_q};

  always_ff @(posedge clk_i) begin : p_stage2
    if (s2_en) begin
      y_q <= sum_s2_d;
    end
  end

  // ===========================================================================
  // Outputs
  // ===========================================================================
  assign out_valid_o = vld_s2_q;
  assign y_o         = y_q;

  // ===========================================================================
  // Assertions (simulation only; macros from templates/prim_cells.sv)
  // ===========================================================================
  `ASSERT_KNOWN(a_out_valid_known, out_valid_o, clk_i, rst_ni)
  `ASSERT_KNOWN(a_out_data_known, out_valid_o ? y_o : '0, clk_i, rst_ni)

endmodule
