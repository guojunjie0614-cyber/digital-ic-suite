# CDC/RDC, SDC Constraints, PPA Triage, and LEC Signoff

---

## 1. Clock-Domain & Reset-Domain Crossing (CDC / RDC)

Every crossing requires explicit source/destination clocks, signal semantics,
arrival rate vs. destination clock ratio, and reset behavior. Use standardized
wrappers (`prim_flop_2sync`, `prim_rst_sync` in `templates/prim_cells.sv`) so
synthesis keeps `(* ASYNC_REG = "TRUE" *)` placement intact and CDC tools recognize
boundaries cleanly.

### 1.1 Approved CDC Topologies

| Signal Type | Safe Hardware Mechanism | Forbidden Anti-Pattern |
|---|---|---|
| Single-bit slow level | `prim_flop_2sync` (2-FF or 3-FF) driven **directly by a source-domain FF** (zero combinational logic before sync!) | 1-FF sync, or combinational glitch logic feeding the synchronizer D pin |
| Single-bit 1-cycle pulse | Toggle synchronizer + edge detect, or closed-loop req/ack handshake (when `f_dst < f_src` or back-to-back pulses possible) | Passing a fast-domain 1-cycle pulse straight into a 2-FF sync |
| Multi-bit config / low-rate bus | Hold multi-bit data stable in source FFs -> synchronize single-bit `load_toggle` / `req` (MCP / req-ack qualifier) -> sample data in destination domain | **Independent 2-FF synchronizers per bit of a multi-bit bus** (skew causes incoherent word sampling!) |
| Monotonic step-by-1 pointer | Binary-to-Gray encode -> register in source domain -> 2-FF sync -> Gray-to-binary in destination domain | Synchronizing binary counters directly, or Gray-encoding counters that jump by $>1$ |
| High-throughput data stream | Dual-clock Asynchronous FIFO (registered Gray pointers + conservative full/empty + power-of-2 depth) | Ad-hoc multi-cycle enable strobes without backpressure |
| Asynchronous reset (RDC) | Per-domain `prim_rst_sync` (async assert, sync deassert) + verify behavior when one side is in reset while peer runs | Sharing one domain's synchronized reset directly in another clock domain |

- **Combinational convergence (reconvergence)**: Never recombine two independently synchronized signals in destination combinational logic — differential 1-cycle metastability resolution skew creates glitches/illegal states.

---

## 2. SpyGlass CDC/RDC 7-Step Triage Funnel & Auditable Waivers

Do not blindly waive SpyGlass violations. Triage reports in order:

1. **Verify clocks & resets first**: Confirm every `create_clock`, `create_generated_clock`, and reset polarity in `.sgdc` matches RTL; mis-declared clocks cause 80% of false CDC errors.
2. **Separate functional vs. DFT/scan paths**: Constrain `scan_enable = 0` / `test_mode = 0` in functional CDC runs (test mode is checked separately).
3. **Check source-FF isolation**: Confirm no combinational logic sits before a `prim_flop_2sync` D-pin (`combo_before_sync`).
4. **Collapse multi-bit buses by protocol**: Group MCP/req-ack data buses under their qualifier instead of treating bits independently.
5. **Inspect reconvergence**: Trace synchronized signals that merge downstream in logic or muxes; redesign into a single Gray/FIFO/handshake crossing if glitch-sensitive.
6. **Verify RDC power/reset ordering**: Ensure an upstream reset assertion cannot corrupt a running downstream domain without gating/squelching.
7. **Write surgical, auditable waivers ONLY for proven-safe exceptions**:
   - **Never use broad wildcard waivers** (`waive -rule *` or module-wide skips).
   - Every waiver must record: **Rule ID + Exact Instance/Signal Path + Operating Mode + Protocol/SVA Proof** (e.g., quasi-static CSR written only while datapath disabled) + **Review Owner/Expiry Condition**.

---

## 3. Trustworthy SDC Constraints

- **Primary & Generated Clocks**: Define all root clocks (`create_clock`), explicit clock uncertainty (`set_clock_uncertainty`), I/O budgets (`set_input_delay` / `set_output_delay`), transition/load limits, and generated clocks (`create_generated_clock`).
- **Path Grouping (`group_path`)**: Isolate `IN2REG`, `REG2OUT`, and `IN2OUT` from internal `REG2REG` (built into `templates/syn_dc.tcl`) so rough top-level IO estimates never starve core `REG2REG` optimization.
- **Reset Timing Discipline**:
  - **Synchronous resets MUST BE TIMED** like normal control inputs (`DC_SYNC_RESET_PORTS` in `templates/syn_dc.tcl`) — **never** slap `set_false_path` on a synchronous reset port!
  - **Asynchronous resets**: Only the raw external pad assertion path into `prim_rst_sync` may be cut (`DC_ASYNC_RESET_PORTS`); internal synchronized reset deassertion (`recovery` / `removal`) remains timed.
- **Timing Exceptions Are Not Cosmetics**:
  - **Never invent `set_false_path` or `set_multicycle_path` to make WNS=0.** Multicycle setup $N$ must pair with multicycle hold $N-1$.
  - `set_clock_groups -asynchronous` tells STA not to time async paths; **it proves zero CDC safety** — run CDC verification first.

---

## 4. Synthesis Baseline & 4-Layer PPA Hotspot Triage

Use `templates/syn_dc.tcl` via `eda_runner/scripts/eda_job.sh` when establishing a
Synopsys Design Compiler baseline. Before trusting any WNS/area/power number, audit the 6 hygiene gates:

1. `check_design`: Zero unresolved references, unintended black boxes, or multi-driven nets.
2. `check_timing` (pre & post, including `no_clock unconstrained_endpoints loops generated_clocks`): Zero unclocked registers or combinational loops.
3. `latches.rpt`: Zero unintended level-sensitive latches.
4. `clock_gating.rpt` & `removed_flops.rpt`: Verify ICG coverage `%` meets target and no functional state was accidentally swept away as constant.
5. `report_constraint -all_violators`: Inspect setup (`max`), hold (`min`), and DRV (`max_transition` / `max_capacitance`) violations.
6. **Corner sanity**: TT corner is only a quick structural check; real signoff setup uses SS worst-case timing views and hold uses FF corners.

### Classify Every PPA Bottleneck Before Editing RTL (4-Layer Triage)

1. **Layer 1 — Constraint / Corner Artifact**: Over-tight IO delay masking `REG2REG`, missing clock group on verified async CDC, missing macro `.db`, wrong operating condition -> fix SDC/setup, not RTL.
2. **Layer 2 — Synthesis Mapping / Compile Strategy**: Suboptimal DesignWare architecture, missing register retiming or fanout replication -> test controlled DC switches (`compile_ultra -retime`, `set_max_fanout`) on identical RTL.
3. **Layer 3 — RTL Idiom / Inference Issue**: Serial ripple chain instead of balanced tree, priority `if-else` instead of `unique case`, combinational `ready` chain needing `prim_skid_buf`, reset on wide datapath flops blocking ICG/mapping -> refactor RTL and verify locally with `pre_synth_check.py` first!
4. **Layer 4 — Microarchitecture Limit**: True arithmetic/SRAM delay exceeds clock period even with clean balanced RTL -> add pipeline cut (`Latency + 1`), split SRAM banks, or widen parallelism.

---

## 5. Formal Equivalence Checking (Formality / LEC)

- **Use `templates/lec_fm.tcl` after DC synthesis**: Automatically loads `out/${TOP}.svf`, binds `scan_enable=0` / `test_mode=0`, and runs `match` + `verify` against Golden RTL.
- **Triage LEC failures in order**:
  1. Missing `scan_enable=0` / `test_mode=0` constants (DFT scan muxes cause compare failures if unconstrained).
  2. Missing `.svf` guidance or black-box / SRAM wrapper port mismatch.
  3. Unmatched compare points -> inspect phase inversion / clock-gating latch settings.
  4. **Real failing compare points** (`reports/lec_failing.rpt` + `analyze_points -failing`) -> isolate minimal cone counterexample and fix the RTL/netlist bug (never waive a real compare failure because simulation passed!).
