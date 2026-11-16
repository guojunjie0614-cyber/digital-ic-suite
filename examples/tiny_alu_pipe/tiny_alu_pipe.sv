// Two-stage pipelined multiply-add datapath with valid/ready backpressure.
// Latency: 2 cycles | Initiation Interval (II): 1

module tiny_alu_pipe #(
  parameter int unsigned W = 8
) (
  input  logic             clk_i,
  input  logic             rst_ni,
  input  logic             in_valid_i,
  output logic             in_ready_o,
  input  logic [W-1:0]     a_i,
  input  logic [W-1:0]     b_i,
  input  logic [2*W-1:0]   c_i,
  output logic             out_valid_o,
  input  logic             out_ready_i,
  output logic [2*W:0]     y_o
);
  logic             vld_s1_q, vld_s2_q;
  logic [2*W-1:0]   prod_s1_q;
  logic [2*W-1:0]   c_s1_q;
  logic [2*W:0]     y_q;

  logic             s2_ready;
  logic             s1_en, s2_en;
  logic [2*W-1:0]   prod_s1_d;
  logic [2*W:0]     sum_s2_d;

  assign s2_ready   = out_ready_i || !vld_s2_q;
  assign in_ready_o = s2_ready    || !vld_s1_q;

  assign s1_en = in_valid_i && in_ready_o;
  assign s2_en = vld_s1_q   && s2_ready;

  assign prod_s1_d = a_i * b_i;
  assign sum_s2_d  = {1'b0, prod_s1_q} + {1'b0, c_s1_q};

  // Control/valid flops use async-assert, sync-deassert reset.
  always_ff @(posedge clk_i or negedge rst_ni) begin
    if (!rst_ni) begin
      vld_s1_q <= 1'b0;
      vld_s2_q <= 1'b0;
    end else begin
      if (in_ready_o) vld_s1_q <= in_valid_i;
      if (s2_ready)   vld_s2_q <= vld_s1_q;
    end
  end

  // Reset-free datapath flops gated by valid/ready enable.
  always_ff @(posedge clk_i) begin
    if (s1_en) begin
      prod_s1_q <= prod_s1_d;
      c_s1_q    <= c_i;
    end
    if (s2_en) begin
      y_q <= sum_s2_d;
    end
  end

  assign out_valid_o = vld_s2_q;
  assign y_o         = y_q;

  `ASSERT_KNOWN(u_out_valid_known, out_valid_o, clk_i, rst_ni)
  `ASSERT_KNOWN(u_out_data_known,  out_valid_o ? y_o : '0, clk_i, rst_ni)
endmodule
