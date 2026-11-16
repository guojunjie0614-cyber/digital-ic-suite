# RTL Readability and Commenting Standard

RTL is read far more often than it is written: by DV engineers building
scoreboards, by synthesis/STA engineers chasing a critical path, by CDC
reviewers, by physical designers, and by whoever does the ECO two years later.
Readable RTL is part of "done", not polish added afterwards.

> **Speed rule:** when time is short, cut scope (fewer features, fewer
> parameters), never cut structure or comments. "Quick prototype" still ships
> with a header, commented ports, and labeled stages. Uncommented RTL is
> unfinished RTL.

Start every new module from [`templates/rtl_module_template.sv`](../templates/rtl_module_template.sv).
[`examples/tiny_alu_pipe/tiny_alu_pipe.sv`](../examples/tiny_alu_pipe/tiny_alu_pipe.sv)
shows the same rules on a two-stage pipeline.

---

## 1. Non-Negotiables (apply to every module, every time)

1. **File header block** stating, in this order:
   - `Module` and one-line `Description`.
   - `Function` or `Pipeline`: what the block does; for pipelines a one-line
     stage diagram (`in -> [S1: ...] -> [S2: ...] -> out`).
   - `Timing`: `Latency = N cycles` (measured from which handshake to which
     output) and `II`.
   - `Clocks` and `Resets`: every clock domain, reset polarity, sync/async,
     and which state is reset.
   - `Interfaces`: protocol per port group (valid/ready, AXI4-Lite, req/ack).
   - `Numerics` for any arithmetic: input/output formats and where rounding or
     saturation happens.
   - `Parameters`: legal ranges. `Assumptions`: what the block relies on
     from its neighbours.
2. **Ports grouped by interface** under banner comments (clock/reset first),
   and **every port has a trailing comment**: meaning, unit or format, and the
   qualifying valid (`// Payload, meaningful only when in_valid_i`).
3. **Fixed section order with banners**, matching the declaration-order rule in
   [`sv-coding-guidelines.md`](sv-coding-guidelines.md) section 2:
   parameters/localparams -> signal declarations -> control -> per-stage
   datapath -> outputs -> assertions.
4. **Every pipeline stage is labeled** (`// Stage 2: add`) and its signals carry
   the stage suffix (`prod_s1_q`, `sum_s2_d`). Declarations are grouped per
   stage with a one-line comment per signal.
5. **Comment the why, not the what.** Every non-obvious hardware decision gets
   a reason: why a flop has no reset, why a path is registered, why priority
   order matters, why a width grows by one bit, why a crossing is safe.
6. **No magic numbers.** Anything other than `0`, `1`, `'0`, `'1` becomes a
   named `localparam` with a comment (`localparam int unsigned TIMEOUT_CYCLES = 1024;  // ~1 us at 1 GHz`).
7. **Every CDC crossing is commented at the crossing**: source clock,
   destination clock, mechanism (2-FF level, toggle+edge, gray pointer,
   req/ack), and why that mechanism is safe for this signal.
8. **Every FSM** has a comment per state and a transition summary above the
   next-state logic.
9. **Named blocks**: every `generate` block (`begin : gen_lane`) and every
   `always_ff`/`always_comb` longer than a few lines (`begin : p_stage1`), so
   SDC, LEC, waiver, and waveform paths stay stable and readable.
10. **Assertions document intent**: each assertion has a one-line comment saying
    which rule it protects; any lint waiver states why it is safe.

---

## 2. Comment Content: Good vs. Bad

| Bad (restates syntax) | Good (states hardware intent) |
|---|---|
| `// assign ready` | `// Accept when empty or drained this cycle; keeps II = 1.` |
| `// reset counter` | `// Counter restarts on every new frame so frame_len is per frame.` |
| `// shift right 3` | `// Divide by 8 (frame has 8 beats), floor toward -inf; see Numerics.` |
| `// no reset` | `// No reset: read only while vld_s1_q, saves area and maps to ICG.` |
| `// sync` | `// clk_a -> clk_b level crossing, 2-FF; src is a flop, held >= 3 dst cycles.` |

- Keep comments true. When behaviour changes, the comment changes in the same
  edit. A wrong comment is worse than none.
- Put a block comment above a group of related lines rather than a trailing
  comment on every line of logic. Trailing comments are for ports and
  declarations.
- `// TODO:` must say what is missing and its hardware impact
  (`// TODO: add parity on rdata; today a flipped SRAM bit goes undetected.`).
  Never leave commented-out code; version control keeps history.

---

## 3. Layout and Naming

- 2-space indentation, no tabs, lines <= 100 characters.
- One statement per line; one declaration per line for anything with a comment.
- Align port lists, declarations, and trailing comments into columns.
- Always use `begin`/`end` for `if`/`else`/`case` bodies longer than one
  short statement.
- Naming follows [`sv-coding-guidelines.md`](sv-coding-guidelines.md)
  section 4: `_i`/`_o` ports, `_n` active-low, `_d`/`_q` next/registered,
  `_sN` pipeline stage, `ALL_CAPS` parameters and localparams, `lower_snake_case`
  signals, `p_`/`gen_`/`a_` prefixes for process, generate, and assertion labels.
- Names describe function, not type: `rd_addr_q`, not `reg3` or `tmp`.
- Optional formatter: `verible-verilog-format --indentation_spaces=2 --column_limit=100`.

---

## 4. Tool-Safety Rules for Comments

- **Never start a comment with `verilator`, `synopsys`, `cadence`, `pragma`, or
  `translate_off`/`translate_on`.** Tools parse such comments as directives
  (`// synopsys translate_off` really removes code from synthesis). Rephrase:
  `// The lint tool flags ...`.
- Use the project's comment language. Default to English ASCII: some legacy
  EDA parsers and terminals mangle UTF-8 in source files.
- Do not put `*/` inside block comments or rely on nested `/* */`.

---

## 5. Readability Review Checklist

Run before declaring an RTL change done (together with the structural checks in
`pre_synth_check.py`):

- [ ] Header present and accurate: function, Latency/II, clocks/resets,
      interfaces, numerics, parameter ranges, assumptions.
- [ ] Every port grouped and commented; every declaration commented.
- [ ] Sections in the standard order with banners; stages labeled and suffixed.
- [ ] Every non-obvious decision (no reset, extra pipeline cut, width growth,
      priority, CDC mechanism) has a why-comment.
- [ ] No magic numbers, no commented-out code, no stale comments.
- [ ] Named generate/always blocks; assertions and waivers explained.
- [ ] Lines <= 100 characters; consistent alignment.
- [ ] A reader who has not seen the spec can explain the block from the header
      and comments alone.
