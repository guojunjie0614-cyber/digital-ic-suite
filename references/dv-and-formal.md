# Adaptive Verification, Bit-Accurate Modeling, Formal & Handoff

Verify against **functional requirements**, never by copying RTL internals into the testbench.
Run `python3 "<suite>/rtl_check/scripts/probe_toolchain.py"` first to pick the fastest
installed simulator and prover on the current machine.

---

## 1. Self-Checking Testbench Rules (All Simulators)

- **Independent Reference Model**: Compute expected outputs from transaction-level
  behavioral rules. For fixed-point DSP/arithmetic blocks, build a bit-accurate
  integer reference model exercising positive/negative extrema (`MIN_INT`, `MAX_INT`),
  zero crossings, exact `+/- 0.5 LSB` rounding ties, overflow saturation rails, and
  back-to-back pipeline hazards.
- **Protocol & Backpressure Stress**: Drive randomized `in_valid` gaps AND aggressive
  downstream `out_ready` stalls (single-cycle toggles, long stalls, burst drains) to
  catch off-by-1 SRAM read alignment and skid-buffer drops/duplicates.
- **Zero-Visual-Waveforms-First**: Checkers must fail with `$fatal(1, ...)` printing
  cycle, seed, expected vs. actual values; open waveforms (`surfer` / `gtkwave` / Verdi)
  only when assertions need cycle-by-cycle root-cause isolation.
- **Simulator-Safe TB Coding**: Variables initialized in `initial` must be driven in
  `always @(posedge clk)` rather than `always_ff` (avoids VCS `ICPD` multi-driver errors).

### 1.1 Adaptive Simulator Selection

- **Path A — `verilator` installed (Fastest default)**:
  ```bash
  verilator --binary --timing -Wall -Wno-fatal \
    --top-module tb_<top> -f rtl.f tb/tb_<top>.sv
  ./obj_dir/Vtb_<top>
  ```
  Add `--trace-fst` for waveforms, or `--coverage` when checking branch/line/toggle closure:
  ```bash
  verilator --binary --timing --coverage -Wall -Wno-fatal --top-module tb_<top> -f rtl.f tb/tb_<top>.sv
  ./obj_dir/Vtb_<top>
  verilator_coverage --annotate build/cov_annotated coverage.dat
  ```
- **Path B — `cocotb` (2.0+) installed for Fixed-Point / Protocol Scoreboards**:
  Ideal when a Python fixed-point or golden behavioral model is much cleaner and less
  error-prone than writing reference math in SystemVerilog. Use zero-Makefile `cocotb_tools.runner`:
  ```python
  from pathlib import Path
  from cocotb_tools.runner import get_runner

  runner = get_runner("verilator")  # or "icarus" if verilator is not installed
  runner.build(sources=[Path("rtl/my_dsp.sv")], hdl_toplevel="my_dsp", always=True)
  runner.test(hdl_toplevel="my_dsp", test_module="tb_my_dsp")
  ```
  (In `tb_my_dsp.py`, use `cocotb.start_soon(Clock(dut.clk_i, 10, unit="ns").start())`
  and `dut.sig.value.to_signed()` / `to_unsigned()` per cocotb 2.0 API.)
- **Path C — `iverilog` + `vvp` fallback (when `verilator` is absent)**:
  ```bash
  iverilog -g2012 -Wall -s tb_<top> -f rtl.f tb/tb_<top>.sv -o build/sim.vvp
  vvp build/sim.vvp
  ```
- **Path D — Remote Commercial Simulators (when configured in `tools.yaml`)**:
  Use `eda_job.sh run <job> syn-vcs ...` (or `cad-irun`) for full SystemVerilog / UVM / X-prop.

---

## 2. Local Formal Verification (Adaptive: `sby` Preferred, `yosys-smtbmc` Fallback)

Use open-source formal locally to **prove** (not just sample) control-plane invariants:
FIFO never overflows/underflows, `prim_skid_buf` never drops/reorders beats, arbiter mutual
exclusion (`$onehot0(gnt)`), and FSM reachability.

### 2.1 Critical Rules for Open-Source Yosys/SBY Formal
1. **Immediate SV assertions on external ports**: Open-source `read_verilog -formal -sv`
   supports immediate `assert(...)`, `assume(...)`, `cover(...)`, `$past(...)`,
   `$stable(...)`, `$onehot0(...)`, and `(* anyseq *)`. **Never place `$past(...)`
   inside an async-reset `always_ff` block** (keep formal scoreboards on `always_ff @(posedge clk)`).
2. **Use `async2sync; dffunmap` (or `multiclock off` in `.sby`) for single-clock formal**:
   Converts async resets cleanly without `clk2fflogic` sub-cycle artifacts (use
   `clk2fflogic` / `multiclock on` only for true multi-clock CDC formal).
3. **True $k$-Induction Requires Base BMC + Induction + `cover()`**:
   Without `cover(...)`, over-constrained `assume(...)` causes silent vacuous passes!

### 2.2 Path A — SymbiYosys Single-File `.sby` (Preferred if `sby` is installed)
One `.sby` file runs `bmc`, unbounded `prove` (k-induction), and `cover` reachability,
and automatically writes `.vcd` counterexample traces on failure:

```ini
[tasks]
bmc
prove
cover

[options]
bmc:   mode bmc
prove: mode prove
cover: mode cover
depth 15
multiclock off

[engines]
smtbmc z3

[script]
read -formal -sv prim_skid_buf.sv skid_formal_tb.sv
prep -top skid_formal_tb

[files]
prim_skid_buf.sv
skid_formal_tb.sv
```

Run all three proofs: `sby -f skid.sby` (inspect `skid_bmc/engine_0/trace.vcd` if it fails).

### 2.3 Path B — Direct `yosys-smtbmc` Fallback (when `sby` is not installed)
```bash
yosys -q -p "read_verilog -formal -sv prim_skid_buf.sv skid_formal_tb.sv; prep -top skid_formal_tb; async2sync; dffunmap; write_smt2 -wires skid.smt2"
yosys-smtbmc -s z3 -t 15 skid.smt2       # 1. Base case BMC
yosys-smtbmc -s z3 -i -t 15 skid.smt2    # 2. k-Induction (unbounded proof)
yosys-smtbmc -s z3 -c -t 15 skid.smt2    # 3. Reachability cover (proves non-vacuity)
```

<details>
<summary>Complete Verified Formal Harness Template (skid_formal_tb.sv)</summary>

```systemverilog
module skid_formal_tb (
  input logic clk_i,
  input logic rst_ni
);
  (* anyseq *) logic       in_valid;
  (* anyseq *) logic [7:0] in_data;
  (* anyseq *) logic       out_ready;
  logic       in_ready;
  logic       out_valid;
  logic [7:0] out_data;

  prim_skid_buf #(.DATA_W(8)) dut (
    .clk_i      (clk_i),
    .rst_ni     (rst_ni),
    .in_valid_i (in_valid),
    .in_ready_o (in_ready),
    .in_data_i  (in_data),
    .out_valid_o(out_valid),
    .out_ready_i(out_ready),
    .out_data_o (out_data)
  );

  logic f_past_valid = 1'b0;
  always_ff @(posedge clk_i) f_past_valid <= 1'b1;

  always_comb begin
    if (!f_past_valid) assume (!rst_ni);
    else               assume (rst_ni);
  end

  logic       ref_full = 1'b0;
  logic [7:0] ref_data = 8'h00;

  wire in_fire  = in_valid  && in_ready;
  wire out_fire = out_valid && out_ready;

  always_comb begin
    if (f_past_valid && rst_ni) begin
      assert (in_ready  == ~ref_full);
      assert (out_valid == (ref_full | in_valid));
      if (out_valid) begin
        assert (out_data == (ref_full ? ref_data : in_data));
      end
    end
  end

  always_ff @(posedge clk_i) begin
    if (!rst_ni) begin
      ref_full <= 1'b0;
    end else begin
      if (f_past_valid && $past(rst_ni && in_valid && !in_ready)) begin
        assume (in_valid);
        assume ($stable(in_data));
      end
      case ({in_fire, out_fire})
        2'b10: begin ref_full <= 1'b1; ref_data <= in_data; end
        2'b01: begin ref_full <= 1'b0;                      end
        default: ;
      endcase
      cover (f_past_valid && ref_full && out_fire);
    end
  end
endmodule
```

</details>

---

## 3. X-Propagation, GLS & Front-End Handoff / ECO Gate

- **X-Propagation (VCS `-xprop=tmerge` / `xmerge`)**: Catches uninitialized control flops or `if (cond)` X-optimism — critical for verifying reset-free datapaths!
- **Targeted GLS**: Run zero-delay GLS smoke after synthesis to verify reset sequence, scan tie-offs (`scan_enable=0`, `test_mode=0`), and SRAM wrapper connectivity.

### Minimal Front-End Handoff & ECO Checklist
Before releasing RTL/netlist to PnR or signing off an RTL/netlist ECO:
1. **Lint & Structural Hygiene**: Zero fatal lint errors, zero unintended latches, zero `comb_loops`, zero unexpected `stuck_output_bits` in `pre_synth_check.py`.
2. **CDC/RDC Clean**: Zero unwaived SpyGlass CDC/RDC errors; every waiver carries an instance path and protocol proof.
3. **Synthesis & STA Hygiene**: `check_design`, `check_timing`, `latches.rpt`, `clock_gating.rpt`, and worst-corner SS/FF violations reviewed cleanly.
4. **LEC Clean (`lec_fm.tcl`)**: `verify` passes between Golden RTL and synthesized netlist with `.svf`. **ECO Rule**: Always prove the final post-ECO netlist against **Golden RTL** (never compare only broken-netlist vs. patched-netlist!).
