---
name: digital-ic-suite
description: >
  RTL-to-GDS digital IC front-end engineering: hardware-first microarchitecture,
  timing-clean synthesizable Verilog/SystemVerilog RTL, bit-accurate testbenches,
  SVA/formal, CDC/RDC, adaptive local lint & Yosys elaboration/QoR checks,
  Synopsys DC/PT synthesis/STA, SDC constraints, low-power/DFT readiness, LEC,
  and commercial EDA runner flows. Use for RTL design, review, verification,
  synthesis/timing debug, constraints, or EDA flows.
---

# Digital IC Suite

Build front-end RTL that synthesizes cleanly and closes timing by construction,
verified through an **environment-adaptive local inner loop** before touching heavy
commercial signoff.

## Four Core Laws

1. **Think Hardware Before Code**: Before writing `module`, know where data comes
   from, where it goes, what happens each cycle, how backpressure flows, how many
   bits/transactions move per cycle (`II` & `Latency`), and what maps to flops vs.
   SRAM vs. operators. If you cannot draw the pipeline and handshake, do not code yet.
2. **Timing & Synthesis Cleanliness Up Front**: Estimate logic levels while writing
   RTL (budget ~8–12 NAND2-equivalent gate levels at 1 GHz; scale proportionally).
   Prevent latches, ripple adder chains, deep priority `if-else` ladders, combinational
   `valid <- ready` loops, datapath resets, and flop-exploded memories at the source.
3. **Interfaces & Boundaries Are Law**: Lock `valid/ready` stability, sideband/error
   semantics, explicit bit-widths/rounding/saturation, and CDC/RDC topology first.
   Internal logic can be refactored cheaply; broken interfaces infect the whole chip.
4. **Readable RTL Is Part of Done**: Never trade readability or comments for speed,
   even when asked to be quick. Every module ships with a header (function,
   `Latency`/`II`, clocks/resets, interfaces, numerics, parameter ranges), grouped and
   commented ports, banner-separated sections, labeled pipeline stages, and a
   *why*-comment at every non-obvious hardware decision. If time is short, cut scope,
   not documentation. Start from [`templates/rtl_module_template.sv`](templates/rtl_module_template.sv)
   and pass the checklist in [`references/rtl-readability-and-comments.md`](references/rtl-readability-and-comments.md).

## Adaptive Execution Loop (Probe First, Use Fastest Available Path)

`<suite>` below is the directory containing this `SKILL.md` (resolve relative to this file across Codex, Claude Code, Antigravity, Cursor, or project-local installs).

### Step 0: Probe Available Toolchains (Always Run First on a New Machine/Project)

Never assume optional local tools (`verilator`, `sv2v`, `verible-verilog-lint`, `sby`,
`cocotb`, `iverilog`) or commercial licenses exist. Probe first so you accelerate with
whatever is installed and degrade gracefully when something is missing:

```bash
python3 "<suite>/rtl_check/scripts/probe_toolchain.py"
```

### Step 0b: First-Run EDA Server Onboarding (When Commercial Tools Are Needed)

Trigger when the probe prints `Commercial EDA : not configured`, the needed tool or
server is missing from the listed targets, or the user asks to connect a server.

1. **Ask once, in one message**, for: the SSH address (`user@host` or a
   `~/.ssh/config` alias), the port if not 22, a jump host if any, and a short name
   for the server. Optionally: license server (`port@host`) and non-standard install
   directories. **Never ask for or store passwords**: login must be key-based. For a
   password-only server, ask the user to run `ssh-copy-id <host>` themselves first.
2. **Run the onboarding wizard** with the answers:
   ```bash
   python3 "<suite>/eda_runner/scripts/eda_setup.py" init --target <name> \
     --host <user@host> [--port N] [--jump user@bastion] [--add-search-root /dir] --default
   ```
   It verifies non-interactive SSH, reads the remote login environment (license
   variables, `PATH`), searches standard install roots for DC/PT/FM/LC/VCS/Verdi/
   SpyGlass/Xcelium/Genus/Innovus/Questa, and saves
   `~/.config/digital-ic-suite/tools.d/<name>.yaml` (mode 600). A hand-written
   `tools.yaml` is never rewritten; only its `default_target:` line changes (backup first).
3. **Report the summary** to the user. Ask a follow-up only when a license was not
   found or several installed versions compete; then rerun with
   `--license synopsys=27000@lic-host`, `--tool syn-dc=/path/bin/dc_shell`, and `--force`.
4. **Verify** before the first real job:
   `python3 "<suite>/eda_runner/scripts/eda_preflight.py" <tool> --target <name> --level probe`.
   `eda_setup.py list` shows configured servers; `eda_setup.py remove --target <name>` forgets one.

`rc=20` from the wizard means SSH/auth is blocked: report the printed guidance and stop.

### 1. Adaptive Local Inner Loop (Run on Every Meaningful RTL Edit)

1. **Functional & Bit-Accurate Simulation** (pick fastest installed simulator):
   - If `verilator` is installed:
     ```bash
     verilator --binary --timing -Wall -Wno-fatal --top-module tb_<top> -f rtl.f tb/tb_<top>.sv
     ./obj_dir/Vtb_<top>
     ```
   - If `cocotb` is also installed for fixed-point DSP/protocol scoreboards: drive
     Verilator/Icarus from Python via `cocotb_tools.runner` (see `references/dv-and-formal.md`).
   - If `verilator` is missing but `iverilog` + `vvp` exist: fall back to `iverilog -g2012 -f rtl.f`.
   - If no local simulator exists and `eda_runner` is configured: use `syn-vcs` / `cad-irun`.
2. **Adaptive Pre-Synthesis Check, Dead-Logic Guard & QoR Trend (`pre_synth_check.py`)**:
   ```bash
   python3 "<suite>/rtl_check/scripts/pre_synth_check.py" \
     --top <top> --filelist rtl.f --out-dir build/rtl_check
   ```
   - **Auto-adapts to your machine**: uses `verilator` with fatal hardware rules
     (`LATCH`, `BLKSEQ`, `COMBDLY`, `MULTIDRIVEN`, `UNOPTFLAT`, `IMPLICIT`) or falls
     back to `iverilog`; runs `verible-verilog-lint` style advisories when present
     (`--style auto`); bridges complex multi-package SV via `sv2v` automatically if
     native Yosys `-sv` fails (`--sv2v auto`).
   - **Catches fake PPA wins**: flags **constant-tied/undriven top outputs
     (`stuck_output_bits`)**, **excessive opt-folded FFs (`pruned_ff_pct`)**,
     **combinational loops (`comb_loops` via `scc`)**, **non-clock high-fanout nets
     (`max_fanout`)**, plus FF/Latch/SRAM/operator/gate-depth **`Prev -> Current` trends**.
   - Pass `--budget qor_budget.yaml` (template: `templates/qor_budget.yaml`)
     when enforcing explicit resource, SRAM, fanout, or depth budgets.
3. **Formal Invariants** (pick installed prover):
   - If `sby` + `z3`/`boolector` exist: run single-file `.sby` (`bmc` + `prove` + `cover`
     with automatic `.vcd` counterexamples).
   - Else if `yosys` + `yosys-smtbmc` + `z3` exist: use direct `yosys-smtbmc` commands.
   - Else: rely on inline `ASSERT*` macros (`templates/prim_cells.sv`) in simulation.

### 2. Commercial Signoff Outer Loop (`eda_runner/`)

Escalate when local structure & simulation are clean and mapped QoR, multi-corner STA,
SpyGlass CDC, VCS/Verdi debug, or Formality LEC is needed.

- Server targets, SSH aliases, licenses, and vendor paths live **outside** this skill
  in `~/.config/digital-ic-suite/tools.yaml` plus onboarded servers in
  `~/.config/digital-ic-suite/tools.d/` (or project `./.eda_tools.yaml`). Create them
  with Step 0b; the schema is shown in `templates/eda_tools.example.yaml`.
- Execute in a single-SSH-session preflight + job flow:
  ```bash
  EDA_SCRIPTS="<suite>/eda_runner/scripts"
  python3 "$EDA_SCRIPTS/eda_preflight.py" syn-dc --level probe
  bash "$EDA_SCRIPTS/eda_job.sh" init <job> --reuse
  bash "$EDA_SCRIPTS/eda_job.sh" sync <job> ./rtl src
  bash "$EDA_SCRIPTS/eda_job.sh" run <job> syn-dc --cmd 'cd "$EDA_JOB_DIR/src/rtl" && "$EDA_TOOL_BIN" -64bit -f run.tcl'
  bash "$EDA_SCRIPTS/eda_job.sh" fetch <job> ./results reports
  ```
  Switch configured servers anytime with `--target <name>` (or `EDA_TARGET=<name>`).
- **Hard Isolation**: If preflight, SSH, or license fails (`rc=20`), stop and report
  the infrastructure error. Never modify RTL, SDC, or technology libraries to work
  around an environment failure.

## Pragmatic Decision Rules (When to Ask vs. Act)

- **Stop & Ask Precisely** ONLY when missing intent changes hardware topology,
  protocol compatibility, numeric bit-accuracy, or crossing safety (e.g., stall vs.
  drop under backpressure, rounding/tie/saturation mode, multi-bit CDC update protocol).
  Ask one focused question with your recommended hardware default and its area/latency impact.
- **Act Directly with Industry Defaults** on standard implementation choices:
  - Control/state flops: async-assert, sync-deassert active-low reset (`rst_n`);
    datapath payload flops: **no reset** (gated by valid/enable, guarded by `ASSERT_KNOWN`).
  - Handshakes: `valid` never depends combinatorially on `ready`; insert `prim_skid_buf`
    when cutting long `ready` backpressure paths.
  - Reuse silicon-safe building blocks from [`templates/prim_cells.sv`](templates/prim_cells.sv)
    (`ASSERT*`, `prim_flop_2sync`, `prim_rst_sync`, `prim_icg`, `prim_skid_buf`, `prim_sram_1rw_wrap`).
  - Document module pipeline latency (`Latency: N cycles`) in the module header,
    following the full header layout of [`templates/rtl_module_template.sv`](templates/rtl_module_template.sv).

## Domain References & Templates

Read the matching reference before non-trivial work in that area:

| Task or Engineering Question | Reference / Template |
|---|---|
| Writing or reviewing any RTL: header block, port/section comments, why-comments, naming, layout, comment tool-safety, readability checklist | [`references/rtl-readability-and-comments.md`](references/rtl-readability-and-comments.md), [`templates/rtl_module_template.sv`](templates/rtl_module_template.sv) |
| Synthesizable SV subset, 7 cross-tool traps, X-safety, AXI/CSR `W1C`, `valid/ready` + Skid Buffer, FSMs, adder trees/mux cost, SRAM/FIFO, ICG/DFT | [`references/sv-coding-guidelines.md`](references/sv-coding-guidelines.md), [`templates/prim_cells.sv`](templates/prim_cells.sv) |
| Microarchitecture trade-offs, cycle/II scheduling, fixed-point quantization/saturation, Foundry SRAM wrapper, QoR budgets | [`references/microarch-datapath-sram.md`](references/microarch-datapath-sram.md), [`templates/qor_budget.yaml`](templates/qor_budget.yaml) |
| Clocks/resets, CDC/RDC mechanisms, SpyGlass 7-step triage & waivers, SDC authoring, 4-layer PPA triage, DC & Formality LEC templates | [`references/cdc-sta-lec-signoff.md`](references/cdc-sta-lec-signoff.md), [`templates/syn_dc.tcl`](templates/syn_dc.tcl), [`templates/lec_fm.tcl`](templates/lec_fm.tcl) |
| Adaptive verification (`Verilator`/`iverilog`/`cocotb`), `sby` & `yosys-smtbmc` formal proofs, coverage, X-prop, Handoff/ECO gates | [`references/dv-and-formal.md`](references/dv-and-formal.md) |
| Connecting a new EDA server (Step 0b wizard), per-server tool/license overrides, config schema (`ssh` vs. helper command targets, FlexLM/file licenses) | `eda_runner/scripts/eda_setup.py --help`, [`templates/eda_tools.example.yaml`](templates/eda_tools.example.yaml) |
