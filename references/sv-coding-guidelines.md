# Synthesizable RTL-to-GDS Craftsmanship

Every rule below prevents silicon bugs, sim-synth mismatches, X-optimism masks,
or cross-tool compile failures across Verilator, VCS, Xcelium, Yosys, and DC.
Reusable silicon-safe building blocks live in `templates/prim_cells.sv`.

---

## 1. Procedural Blocks & Zero-Latch Discipline

### 1.1 Separate Sequential and Combinational Logic Strictly
- **Sequential**: `always_ff @(posedge clk or negedge rst_n)` (or `always_ff @(posedge clk)` for reset-free datapath) — use **non-blocking `<=` exclusively**.
- **Combinational**: `always_comb` — use **blocking `=` exclusively**.
- Never mix `=` and `<=` on the same signal, and never drive a variable from multiple `always` blocks (VCS `ICPD` fatal error!).

### 1.2 Four Habits That Eliminate Accidental Latches
1. **Assign default values to every output at the very top** of `always_comb` before any `if` or `case`.
2. Every `case` includes `default:`; any combinational `if` without a top-of-block default includes `else`.
3. **Never write self-assignment (`a = a;`)** inside `always_comb` — that explicitly asks synthesis for a latch or combinational loop.
4. Confirm `Latch bits == 0` in `pre_synth_check.py` (enforced by `-Werror-LATCH`).

---

## 2. Seven Cross-Tool SystemVerilog Traps & X-State Defense

Rules distilled from lowRISC/OpenTitan style and Sutherland/Cummings/Mills synthesis traps:

1. **Strictly 4-state `logic` in RTL (no `int` / `bit` / `byte` on hardware paths)**:
   - 2-state types initialize to `0` and silently turn `X` into `0`, hiding uninitialized datapaths and SRAM collisions in RTL sim until gate-level fails.
   - Reserve `int` / `int unsigned` strictly for `parameter`s and `for (int i = 0; ...)` loop indices. Always base enums on `logic`: `typedef enum logic [1:0] { ... } state_e;`.
2. **Never write `logic x = expr;` inline initialization in RTL**:
   - `wire [7:0] y = a + b;` is continuous assignment; **`logic [7:0] y = a + b;`** (or `logic q = 1'b0;`) is a time-0 `initial` procedural init ignored by DC/Genus — guaranteeing pre-sim vs. netlist mismatch! Always separate declaration (`logic [7:0] y;`) from `assign y = a + b;`.
3. **Fixed declaration order & commercial simulator hygiene**:
   - Declare inside a module in strict order so Cadence Xcelium (IEEE 1800 §12.5) never rejects forward references: `parameters -> ports -> explicit imports -> localparams/typedefs -> signal declarations -> assign/instances/always_comb/always_ff/assertions`.
   - In testbenches, if a variable is initialized in `initial`, drive it in `always @(posedge clk)` instead of `always_ff` (VCS raises `ICPD` on `initial` + `always_ff` multi-drivers).
4. **No `casex`, no `full_case` / `parallel_case` pragmas**:
   - `casex` treats `X` and `Z` on inputs as don't-cares; `full_case` / `parallel_case` comments alter synthesis without telling the simulator.
   - Use **`unique case (...)`** for exact matching, or **`unique case (expr) inside`** for masked/wildcard decode (asymmetric matching: `?` only in case items, never matches `X`/`Z` on input!), always with `default:`.
5. **Explicit bounds on non-power-of-2 dynamic indexing & explicit boolean compares**:
   - Guard dynamic indexing when depth is not $2^k$: `val = (idx < DEPTH) ? arr[idx] : '0;`.
   - Never rely on implicit multi-bit truthiness (`if (bus)` or `!bus`): write `if (bus != '0)` and keep logical operators (`&&`, `||`, `!`) strictly separate from bitwise operators (`&`, `|`, `~`).
6. **Package scoping, explicit ports, and named `generate` blocks**:
   - Never put `import pkg::*;` outside `module` (`$unit` pollution). Prefer explicit `import pkg::type_t;` or `module foo import pkg::*; (...)`.
   - Avoid `interface` on synthesizable RTL module ports for maximum Yosys/sv2v/DC portability; use `typedef struct packed` + flat ports.
   - Ban `.*` implicit port connections; tie unused outputs explicitly (`.unused_o()`). Avoid redundant full-range slices (`a[7:0]` hides width-mismatch lints). Always name `generate` blocks (`begin : gen_lane`) so SDC/LEC/Waiver paths stay deterministic.
7. **Inline X-safety assertions on reset-free datapaths**:
   - Keep datapath flops reset-free for area/ICG wins, but guard control & valid-qualified outputs with `ASSERT_KNOWN` macros (`templates/prim_cells.sv` inside `` `ifndef SYNTHESIS ``) so uninitialized reads fail immediately in sim.

---

## 3. Bit-Width & Signedness Discipline

- **Explicit concatenation widths**: Every operand inside `{a, b}` must have a known width; never write unsized literals (`{1, a}`). Use `{1'b1, a}` or `{'0, a}`.
- **Explicit carry/sign extension**:
  ```systemverilog
  assign sum    = {1'b0, a} + {1'b0, b};             // explicit 1-bit unsigned carry growth
  assign diff_s = {a_s[W-1], a_s} - {b_s[W-1], b_s}; // explicit signed growth
  ```
- **Never mix `signed` and `unsigned` in one expression**: One unsigned operand forces the entire comparison unsigned (`-1 < 3'd2` evaluates false!). Wrap unsigned operands with `$signed({1'b0, u_val})`.
- **Variable shifts (`a << shamt`)**: Synthesize expensive barrel-shifter mux trees; check if a smaller shift range or constant shifts suffice.

---

## 4. Naming, Clocks & Resets

- **Signal naming**: `[src2dst_]<name>_<attr>[_n]`: `_n` active-low, `_nxt`/`_d` next-state, `_q`/`_p1` registered, `_pulse` 1-cycle strobe. One module per file (`<module_name>.sv`), with `Latency: N cycles` in the module header.
- **Clocks**: Never gate or mux clocks in combinational `assign` logic (`clk & en`). Instantiate `prim_icg` (`templates/prim_cells.sv`) or foundry `icg_cell` / `clk_mux_cell` with `test_en_i`.
- **Resets**:
  - Control/FSM/valid/pointers: async-assert, sync-deassert via `prim_rst_sync` (with DFT `test_mode_i` bypass!).
  - Datapath payloads: **no reset** — qualify downstream usage with `valid`.
  - Never gate async reset pins with combinational logic (`negedge (rst_n & clr_n)`); keep software clear synchronous.

---

## 5. `valid / ready` Handshakes, AXI Rules & CSR `W1C`

### 5.1 Single-Channel `valid / ready` & Skid Buffer
- **`valid` MUST NEVER depend combinatorially on `ready`**.
- Once `valid` asserts while `ready == 0`, both `valid` and payload must stay stable until `valid && ready`.
- Cut long combinational `ready` backpressure paths at module boundaries with **`prim_skid_buf`** (`templates/prim_cells.sv`).

### 5.2 AXI4 / AXI-Lite Cross-Channel Deadlock Rules
- Beyond single-channel `VALID` independence, **never create cross-channel circular dependencies**:
  - A subordinate must NEVER stall `AWREADY` waiting for `WVALID`, nor stall `WREADY` waiting for `AWVALID` in a way that locks an interconnect where `AW` and `W` arrive independently or out of order.
  - A write response (`BVALID`) must only assert after both `AW` and the final `W` (`WLAST`) beat have been accepted.

### 5.3 CSR Status Register (`W1C` Race-Free Pattern)
When software writes `1` to clear an interrupt/status bit (`W1C`) on the exact same cycle hardware pulses `hw_set_i`, **hardware set must win** so events are never silently lost:

```systemverilog
always_ff @(posedge clk_i or negedge rst_ni) begin
  if (!rst_ni) begin
    status_q <= '0;
  end else begin
    for (int i = 0; i < W; i++) begin
      if (hw_set_i[i])                    status_q[i] <= 1'b1; // HW set takes priority
      else if (sw_wr_en_i && sw_wdata_i[i]) status_q[i] <= 1'b0; // SW write-1-to-clear
    end
  end
end
```

---

## 6. FSMs, Arithmetic Trees, Storage & Low-Power/DFT

- **FSMs**: Use 2-segment or 3-segment FSMs; prefer Moore or registered outputs on boundaries; one-hot for `< 8` states, binary for `>= 8` states. Always include `default: state_nxt = ST_IDLE;` and never assign `state_nxt = 'x`.
- **Adder trees vs. serial loops**: Replace `for (...) sum = sum + a[i];` with balanced binary trees (`O(log2 N)` depth instead of `O(N)`).
- **Parallel mux (`unique case`) vs. `if-else` ladder**: Use `unique case` for mutually exclusive conditions; count down to zero (`cnt_q == '0`) instead of dynamic `>=` comparators.
- **Storage**: Depth <= 8 -> flop regfile with per-entry ICG enable; Depth >= 16–32 & multi-Kb -> `prim_sram_1rw_wrap` / foundry SRAM wrapper (never reset memory arrays, handle 1-cycle read latency & RW same-addr collision explicitly); cross-clock streams -> power-of-2 Gray-coded Async FIFO.
- **Low-power & DFT**: Register ICG enables (`if (en_q)`), suppress operand toggling when idle, keep `test_en` on all ICGs and `test_mode` mux bypass on reset/generated clocks and black-box SRAM outputs, and verify `Latch bits == 0`.
