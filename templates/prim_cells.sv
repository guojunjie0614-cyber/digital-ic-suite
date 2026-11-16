// Silicon-grade reusable RTL primitive building blocks & inline SVA macros.
// Copy needed primitives or split into one-module-per-file in production projects.
/* verilator lint_off DECLFILENAME */

`ifndef PRIM_ASSERT_MACROS_DEFINED
`define PRIM_ASSERT_MACROS_DEFINED

`ifndef SYNTHESIS
`define ASSERT(__name, __prop, __clk, __rst_n) \
  always @(posedge (__clk)) begin \
    if ((__rst_n) === 1'b1) begin \
      __name: assert (__prop) else $fatal(1, "ASSERT FAILED [%s] @ %0t", `"__name`", $time); \
    end \
  end

`define ASSERT_NEVER(__name, __cond, __clk, __rst_n) \
  `ASSERT(__name, !(__cond), __clk, __rst_n)

`define ASSERT_KNOWN(__name, __sig, __clk, __rst_n) \
  `ASSERT(__name, !$isunknown(__sig), __clk, __rst_n)

`define ASSERT_ONEHOT0(__name, __sig, __clk, __rst_n) \
  `ASSERT(__name, $onehot0(__sig), __clk, __rst_n)
`else
`define ASSERT(__name, __prop, __clk, __rst_n)
`define ASSERT_NEVER(__name, __cond, __clk, __rst_n)
`define ASSERT_KNOWN(__name, __sig, __clk, __rst_n)
`define ASSERT_ONEHOT0(__name, __sig, __clk, __rst_n)
`endif

`endif

// 1. Two-stage CDC synchronizer for a single-bit FF-driven level signal.
//    NEVER feed combinational logic directly into d_i.
module prim_flop_2sync #(
  parameter int unsigned WIDTH = 1,
  parameter logic [WIDTH-1:0] RESET_VAL = '0
) (
  input  logic             clk_i,
  input  logic             rst_ni,
  input  logic [WIDTH-1:0] d_i,
  output logic [WIDTH-1:0] q_o
);
  (* ASYNC_REG = "TRUE", dont_touch = "true" *) logic [WIDTH-1:0] sync_q0;
  (* ASYNC_REG = "TRUE", dont_touch = "true" *) logic [WIDTH-1:0] sync_q1;

  always_ff @(posedge clk_i or negedge rst_ni) begin
    if (!rst_ni) begin
      sync_q0 <= RESET_VAL;
      sync_q1 <= RESET_VAL;
    end else begin
      sync_q0 <= d_i;
      sync_q1 <= sync_q0;
    end
  end

  assign q_o = sync_q1;
endmodule

// 2. Async-assert, sync-deassert reset synchronizer with DFT scan bypass.
module prim_rst_sync #(
  parameter int unsigned STAGES = 2
) (
  input  logic clk_i,
  input  logic rst_ni,
  input  logic test_mode_i,
  input  logic test_rst_ni,
  output logic rst_no
);
  (* ASYNC_REG = "TRUE", dont_touch = "true" *) logic [STAGES-1:0] rst_sync_q;

  always_ff @(posedge clk_i or negedge rst_ni) begin
    if (!rst_ni) rst_sync_q <= '0;
    else         rst_sync_q <= {rst_sync_q[STAGES-2:0], 1'b1};
  end

  assign rst_no = test_mode_i ? test_rst_ni : rst_sync_q[STAGES-1];
endmodule

// 3. Glitch-free Integrated Clock Gating (ICG) wrapper with DFT test_en hook.
//    Replace synthesis branch with foundry CKLNQD/ICG cell in technology mapping.
module prim_icg (
  input  logic clk_i,
  input  logic en_i,
  input  logic test_en_i,
  output logic gclk_o
);
  logic en_lat_q;

  always_latch begin
    if (!clk_i) en_lat_q = en_i | test_en_i;
  end

  assign gclk_o = clk_i & en_lat_q;
endmodule

// 4. Zero-bubble 2-slot Skid Buffer (cuts combinational ready backpressure paths).
module prim_skid_buf #(
  parameter int unsigned DATA_W = 32
) (
  input  logic              clk_i,
  input  logic              rst_ni,
  input  logic              in_valid_i,
  output logic              in_ready_o,
  input  logic [DATA_W-1:0] in_data_i,
  output logic              out_valid_o,
  input  logic              out_ready_i,
  output logic [DATA_W-1:0] out_data_o
);
  logic              buf_valid_q;
  logic [DATA_W-1:0] buf_data_q;

  assign in_ready_o  = ~buf_valid_q;
  assign out_valid_o = in_valid_i | buf_valid_q;
  assign out_data_o  = buf_valid_q ? buf_data_q : in_data_i;

  always_ff @(posedge clk_i or negedge rst_ni) begin
    if (!rst_ni) begin
      buf_valid_q <= 1'b0;
    end else if (buf_valid_q) begin
      if (out_ready_i) buf_valid_q <= 1'b0;
    end else if (in_valid_i && !out_ready_i) begin
      buf_valid_q <= 1'b1;
    end
  end

  // Reset-free datapath flop for area/ICG friendliness
  always_ff @(posedge clk_i) begin
    if (!buf_valid_q && in_valid_i && !out_ready_i) begin
      buf_data_q <= in_data_i;
    end
  end
endmodule

// 5. Synchronous 1RW SRAM wrapper (1-cycle read latency; swap body for foundry macro).
module prim_sram_1rw_wrap #(
  parameter int unsigned DEPTH  = 256,
  parameter int unsigned DATA_W = 32,
  localparam int unsigned ADDR_W = $clog2(DEPTH)
) (
  input  logic              clk_i,
  input  logic              req_i,
  input  logic              we_i,
  input  logic [ADDR_W-1:0] addr_i,
  input  logic [DATA_W-1:0] wdata_i,
  output logic [DATA_W-1:0] rdata_o
);
  logic [DATA_W-1:0] mem [0:DEPTH-1];

  always_ff @(posedge clk_i) begin
    if (req_i) begin
      if (we_i) begin
        mem[addr_i] <= wdata_i;
      end else begin
        rdata_o <= mem[addr_i];
      end
    end
  end
endmodule
/* verilator lint_on DECLFILENAME */
