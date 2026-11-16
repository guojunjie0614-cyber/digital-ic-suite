# Digital IC Suite

[English](README.md) | [简体中文](README_zh.md)

**Digital IC Suite** is an Agent Skill (`SKILL.md` + verification scripts + silicon-safe RTL/Tcl templates) for **RTL-to-GDS digital IC front-end engineering**.

Instead of writing unconstrained Verilog and hoping synthesis succeeds, it forces coding agents through an **environment-adaptive local inner loop** (toolchain probing, hardware-fatal linting, Yosys elaboration QoR tracking, anti-fake-PPA guards, and open-source formal proofs) before escalating to a **decoupled commercial EDA signoff outer loop** (Design Compiler, PrimeTime, Formality, VCS, SpyGlass).

---

## Core Capabilities

- **Toolchain Auto-Probing (`rtl_check/scripts/probe_toolchain.py`)**
  Inspects local open-source tools (`verilator`, `iverilog`, `yosys`, `sv2v`, `verible-verilog-lint`, `sby`, `yosys-smtbmc`, `z3`, `boolector`, `cocotb`, `surfer`, `gtkwave`) and optional external commercial EDA targets, selecting the fastest available verification path and degrading cleanly when optional binaries are missing.
- **Pre-Synthesis QoR, Structural Budgets & Anti-Fake-PPA Guards (`rtl_check/scripts/pre_synth_check.py`)**
  - Runs Verilator with hardware-fatal rules (`LATCH`, `BLKSEQ`, `COMBDLY`, `MULTIDRIVEN`, `UNOPTFLAT`, `IMPLICIT`), falling back to `iverilog` when Verilator is absent.
  - Bridges complex multi-package SystemVerilog automatically via `sv2v` (`--sv2v auto`) if native Yosys `read_verilog -sv` fails.
  - Detects fake PPA improvements automatically: **constant-tied or undriven top output ports** (`stuck_output_bits`, `undriven_output_bits`), **over-pruned flip-flops** (`pruned_ff_pct`), **combinational SCC loops** (`comb_loops`), and **high-fanout non-clock nets** (`max_fanout`).
  - Tracks `Prev -> Current` resource deltas (`qor_summary.json` and `qor_history.jsonl`) and enforces machine-checkable operator, SRAM inference, fanout, and logic-depth limits via [`templates/qor_budget.yaml`](templates/qor_budget.yaml).
- **Open-Source Formal & Silicon-Safe Primitives**
  - Ready-to-reuse building blocks and inline SVA macros in [`templates/prim_cells.sv`](templates/prim_cells.sv): `prim_flop_2sync`, `prim_rst_sync`, `prim_icg`, `prim_skid_buf`, `prim_sram_1rw_wrap`, and `ASSERT*`.
  - Adaptive `sby` (SymbiYosys) and `yosys-smtbmc` single-clock BMC + unbounded k-induction + non-vacuity `cover()` flows documented in [`references/dv-and-formal.md`](references/dv-and-formal.md).
- **Decoupled Commercial EDA Runner (`eda_runner/`)**
  - Keeps server hosts, SSH aliases, license servers, and tool paths strictly outside the skill repository in `~/.config/digital-ic-suite/tools.yaml` or project `./.eda_tools.yaml` ([`templates/eda_tools.example.yaml`](templates/eda_tools.example.yaml)).
  - Wraps remote invocations in `bash -c` so servers with `tcsh`/`csh`/`dash` default login shells work reliably while preserving binary `tar` streams.
  - Executes preflight checks (`eda_preflight.py`, including FlexLM TCP & license file reachability), single-SSH tool runs (`eda_exec.sh`), and atomic `init` / `sync` / `run` / `fetch` job directories (`eda_job.sh`) with `rc=20` environment hard isolation.
  - Includes baseline Synopsys DC synthesis ([`templates/syn_dc.tcl`](templates/syn_dc.tcl)) and Formality LEC ([`templates/lec_fm.tcl`](templates/lec_fm.tcl)) templates.

---

## Installation

### Install as an Agent Skill

```bash
npx skills add guojunjie0614/digital-ic-suite
```

Or clone and symlink into your agent's skill directory:

```bash
git clone https://github.com/guojunjie0614/digital-ic-suite.git
mkdir -p ~/.claude/skills ~/.codex/skills ~/.gemini/config/skills ~/.agents/skills

# Link to whichever coding agent(s) you use:
ln -s "$(pwd)/digital-ic-suite" ~/.claude/skills/digital-ic-suite
ln -s "$(pwd)/digital-ic-suite" ~/.codex/skills/digital-ic-suite
ln -s "$(pwd)/digital-ic-suite" ~/.gemini/config/skills/digital-ic-suite
ln -s "$(pwd)/digital-ic-suite" ~/.agents/skills/digital-ic-suite
```

### Python & Local Open-Source Toolchain

- **Python dependencies**: Core local lint/QoR checks run on the **Python 3 standard library** with zero third-party packages. Install `PyYAML` when enforcing `--budget qor_budget.yaml` or running `eda_runner/`:

  ```bash
  pip install -r requirements.txt
  ```

- **Local RTL & Formal Tools (Optional, Auto-Detected)**:
  - **macOS (Homebrew)**:
    ```bash
    brew install verilator yosys sv2v z3
    ```
  - **Ubuntu / Debian**:
    ```bash
    sudo apt-get update && sudo apt-get install -y verilator yosys iverilog z3
    ```
  - **Full Open-Source Bundle (includes `sby`, `sv2v`, `verible-verilog-lint`, `surfer`)**: Install [YosysHQ OSS CAD Suite](https://github.com/YosysHQ/oss-cad-suite-build/releases) and add its `bin` directory to `PATH`.

Probe what is available on your machine at any time:

```bash
python3 rtl_check/scripts/probe_toolchain.py
```

---

## Quick Start (`examples/tiny_alu_pipe`)

The repository includes a self-contained 2-stage pipelined multiply-add example ([`examples/tiny_alu_pipe/tiny_alu_pipe.sv`](examples/tiny_alu_pipe/tiny_alu_pipe.sv)) with `valid/ready` backpressure and a [`qor_budget.yaml`](examples/tiny_alu_pipe/qor_budget.yaml) structural budget.

Run the local pre-synthesis check and budget gate:

```bash
python3 rtl_check/scripts/pre_synth_check.py \
  --top tiny_alu_pipe \
  -f examples/tiny_alu_pipe/rtl.f \
  --budget examples/tiny_alu_pipe/qor_budget.yaml \
  --out-dir /tmp/tiny_alu_check
```

Sample output (Verilator + Yosys):

```text
=== Pre-Synthesis RTL Check: tiny_alu_pipe [PASS] ===
Lint (Verilator): PASS (0 warnings) | Elab/Synth (Yosys/native): PASS

  Metric                      Value
  ---------------------- ----------
  FF bits                        51
  Max non-clk fanout             32
  Gate depth (techmap)           22
  Word depth (RTL ops)            4
  Mux data bits                   0
  Op: multiply                    1
  Op: add/sub                     1

Top Fanout Nets: s1_en(32), s2_en(17), a_i(8)

QoR Budget Check: PASS (7 rules)
```

Re-running the same command after modifying the RTL automatically prints a `Prev -> Current -> Delta` comparison table against the previous run in `--out-dir`.

---

## Commercial EDA Signoff Configuration (`eda_runner/`)

When escalating to commercial synthesis, STA, CDC, simulation, or LEC:

1. Copy [`templates/eda_tools.example.yaml`](templates/eda_tools.example.yaml) outside your project or skill directory:

   ```bash
   mkdir -p ~/.config/digital-ic-suite
   cp templates/eda_tools.example.yaml ~/.config/digital-ic-suite/tools.yaml
   ```

2. Fill in your SSH host alias (or custom jump-host `command`), remote paths, license server (`SNPSLMD_LICENSE_FILE`), and vendor binary paths in `~/.config/digital-ic-suite/tools.yaml` (or `./.eda_tools.yaml` inside a project; `.gitignore` ignores `.eda_tools.yaml` and `tools.yaml` by default).
3. Validate connectivity, environment variables, license reachability, and tool launch in a single SSH round-trip:

   ```bash
   python3 eda_runner/scripts/eda_preflight.py syn-dc --level probe
   ```

4. Run an isolated remote job using [`templates/syn_dc.tcl`](templates/syn_dc.tcl) or [`templates/lec_fm.tcl`](templates/lec_fm.tcl):

   ```bash
   bash eda_runner/scripts/eda_job.sh init my_block --reuse
   bash eda_runner/scripts/eda_job.sh sync my_block ./rtl src
   bash eda_runner/scripts/eda_job.sh run my_block syn-dc \
     --cmd 'cd "$EDA_JOB_DIR/src/rtl" && "$EDA_TOOL_BIN" -64bit -f run.tcl'
   bash eda_runner/scripts/eda_job.sh fetch my_block ./results reports
   ```

---

## Repository Structure

```text
digital-ic-suite/
├── SKILL.md                          # Agent skill entry point and execution rules
├── agents/openai.yaml                # UI metadata for Codex & compatible clients
├── rtl_check/scripts/
│   ├── probe_toolchain.py            # Local & commercial toolchain detector
│   ├── pre_synth_check.py            # Adaptive lint + Yosys QoR + dead-logic guards
│   ├── flist_parser.py               # VCS/Verilator .f parser with lazy -y libdir
│   ├── netlist_qor.py                # Hierarchy-aware netlist QoR & fanout analyzer
│   └── qor_budget.py                 # Structural budget & baseline trend evaluator
├── eda_runner/scripts/
│   ├── eda_env.py                    # External tools.yaml loader & remote bash wrapper
│   ├── eda_preflight.py              # Single-session SSH/license/probe checker
│   ├── eda_exec.sh                   # Single-session remote command executor
│   └── eda_job.sh                    # Atomic init/sync/run/fetch job lifecycle
├── references/
│   ├── sv-coding-guidelines.md       # Synthesizable SV subset & cross-tool traps
│   ├── microarch-datapath-sram.md    # Cycle/II scheduling, fixed-point, SRAM, budgets
│   ├── cdc-sta-lec-signoff.md        # CDC/RDC, SpyGlass triage, SDC, 4-layer PPA, LEC
│   └── dv-and-formal.md              # Adaptive DV (Verilator/cocotb/Icarus) & formal
├── templates/
│   ├── prim_cells.sv                 # Silicon-safe primitives & ASSERT* macros
│   ├── qor_budget.yaml               # Structural QoR budget template
│   ├── eda_tools.example.yaml        # External commercial EDA configuration schema
│   ├── syn_dc.tcl                    # Synopsys Design Compiler baseline script
│   └── lec_fm.tcl                    # Synopsys Formality LEC baseline script
├── examples/tiny_alu_pipe/           # Self-contained 2-stage pipelined RTL demo
├── tests/                            # Unit & integration test suite
├── requirements.txt
└── LICENSE
```

### Running Tests

```bash
python3 -B -m unittest discover -s tests -v
```

Tests verify toolchain probing degradation, `flist_parser` (`-f` vs. `-F` and lazy `-y`), latch separation, dead-logic/pruned-FF guards, `sv2v` package lowering (when `sv2v` is installed), PyYAML-free execution when `--budget` is unused, `tcsh`/`csh` remote login shell wrapping, markdown link integrity, and private-leak hygiene.

---

## License & Trademark Notice

- Licensed under the [MIT License](LICENSE).
- **Trademark Notice**: Synopsys, Design Compiler, PrimeTime, Formality, VCS, SpyGlass, Cadence, Xcelium, and Siemens/Mentor tools are trademarks of their respective owners. This repository contains no proprietary EDA binaries, license keys, foundry PDKs, or vendor reference methodology scripts.
