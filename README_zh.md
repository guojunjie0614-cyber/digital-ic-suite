# Digital IC Suite

[English](README.md) | [简体中文](README_zh.md)

**Digital IC Suite** 是一套面向 **RTL-to-GDS 数字 IC 前端工程**的 Agent Skill（包含 `SKILL.md` 工程规范、本地校验脚本与硅片级 RTL/Tcl 模板）。

它旨在阻止 AI 写完未经约束的 Verilog 后直接“盲猜综合”，而是强迫每次 RTL 修改先经过**自适应本地内循环**（工具链探测、硬件致命 Lint、Yosys 综合前 QoR 与防“假 PPA 优化”守卫、开源形式化证明），本地结构彻底干净后再切入**配置外置、单次 SSH 原子执行且环境硬隔离的商业 EDA Signoff 外循环**（Design Compiler / PrimeTime / Formality / VCS / SpyGlass）。

---

## 核心能力

- **环境自适应工具链探测（`rtl_check/scripts/probe_toolchain.py`）**
  自动探测本机开源工具（`verilator`、`iverilog`、`yosys`、`sv2v`、`verible-verilog-lint`、`sby`、`yosys-smtbmc`、`z3`、`boolector`、`cocotb`、`surfer`、`gtkwave`）与外部商业 EDA 配置，选出当前环境最快的验证路径，并在可选工具缺失时优雅降级。
- **综合前 QoR 追踪、结构预算与防“假优化”守卫（`rtl_check/scripts/pre_synth_check.py`）**
  - 默认用 Verilator 启用硬件致命规则（`LATCH`、`BLKSEQ`、`COMBDLY`、`MULTIDRIVEN`、`UNOPTFLAT`、`IMPLICIT`），缺失时自动降级为 `iverilog`。
  - 当原生 Yosys `read_verilog -sv` 遇到复杂多 package SystemVerilog 报错时，自动启用 `sv2v` 降阶（`--sv2v auto`）。
  - 自动拦截“偷工减料式假 PPA 胜利”：**顶层输出被优化成常量或悬空未驱动**（`stuck_output_bits`、`undriven_output_bits`）、**触发器被异常剪枝过半**（`pruned_ff_pct`）、**组合逻辑 SCC 环路**（`comb_loops`）以及**非时钟高扇出网络**（`max_fanout`）。
  - 每次运行自动记录 `Prev -> Current` 趋势变化（`qor_summary.json` / `qor_history.jsonl`），并支持通过 [`templates/qor_budget.yaml`](templates/qor_budget.yaml) 对乘加器数量、SRAM 推断、扇出和逻辑深度做硬性门禁。
- **开源形式化证明与可复用基础单元**
  - [`templates/prim_cells.sv`](templates/prim_cells.sv) 提供可直接拷贝使用的基础模块与内联 SVA 宏：`prim_flop_2sync`、`prim_rst_sync`、`prim_icg`、`prim_skid_buf`、`prim_sram_1rw_wrap` 以及 `ASSERT*`。
  - [`references/dv-and-formal.md`](references/dv-and-formal.md) 提供了开箱即用的 `sby`（SymbiYosys）与 `yosys-smtbmc` 单时钟 BMC + k-Induction + 非空洞 `cover()` 验证模板。
- **RTL 可读性是硬性要求**
  - 明确要求 Agent 不许为了快牺牲可读性和注释：每个模块都要有文件头（功能、Latency/II、时钟复位、接口协议、数值格式、参数范围）、按接口分组并逐个注释的端口、分段横幅、标号的流水级，以及每个不直观硬件决策旁的“为什么”注释。
  - 规范与评审清单见 [`references/rtl-readability-and-comments.md`](references/rtl-readability-and-comments.md)，可直接复制的零告警骨架见 [`templates/rtl_module_template.sv`](templates/rtl_module_template.sv)。
- **首次连接 EDA 服务器的初始化向导（`eda_runner/scripts/eda_setup.py`）**
  - 只需把 SSH 地址告诉 Agent 一次：向导会检查免密登录、从远端登录环境读出 License 变量、自动找到已安装的 DC/PT/FM/LC/VCS/Verdi/SpyGlass/Xcelium/Genus/Innovus/Questa，并保存为本机私有的单服务器配置 `~/.config/digital-ic-suite/tools.d/<名字>.yaml`（权限 600）。
  - 从不询问或保存密码，也不会改写你手写的 `tools.yaml`。
- **解耦的商业 EDA 远端执行器（`eda_runner/`）**
  - 所有服务器 SSH 别名、License 地址与工具安装路径全部外置到 `~/.config/digital-ic-suite/tools.yaml` 或项目 `./.eda_tools.yaml`（模板见 [`templates/eda_tools.example.yaml`](templates/eda_tools.example.yaml)），绝不进入仓库。
  - 远端命令统一通过 `bash -c` 安全包装传输，完美兼容 IC 服务器常见的 `tcsh`/`csh`/`dash` 默认登录 Shell，同时保持 `tar` 同步数据流无损。
  - 提供单次 SSH 预检（`eda_preflight.py`，含 FlexLM TCP 端口与 License 文件可达性检查）、单命令执行（`eda_exec.sh`）以及原子化的 `init` / `sync` / `run` / `fetch` 作业流（`eda_job.sh`），遇环境或 License 阻塞以 `rc=20` 硬隔离退出。
  - 附带 Synopsys DC 基线综合脚本（[`templates/syn_dc.tcl`](templates/syn_dc.tcl)）和 Formality LEC 等价性检查脚本（[`templates/lec_fm.tcl`](templates/lec_fm.tcl)）。

---

## 安装指南

### 安装为 Agent Skill

```bash
npx skills add guojunjie0614/digital-ic-suite
```

也可以直接克隆并软链接到你使用的编程 Agent 技能目录：

```bash
git clone https://github.com/guojunjie0614/digital-ic-suite.git
mkdir -p ~/.claude/skills ~/.codex/skills ~/.gemini/config/skills ~/.agents/skills

# 按实际使用的客户端选择对应命令：
ln -s "$(pwd)/digital-ic-suite" ~/.claude/skills/digital-ic-suite
ln -s "$(pwd)/digital-ic-suite" ~/.codex/skills/digital-ic-suite
ln -s "$(pwd)/digital-ic-suite" ~/.gemini/config/skills/digital-ic-suite
ln -s "$(pwd)/digital-ic-suite" ~/.agents/skills/digital-ic-suite
```

### Python 依赖与本地开源 EDA 工具链

- **Python 依赖**：不启用 `--budget` 与 `eda_runner/` 时，本地基础 Lint 与 QoR 检查**仅依赖 Python 3 标准库即可运行**。如需使用 YAML 预算或远端商业 EDA 执行器，安装 `PyYAML`：

  ```bash
  pip install -r requirements.txt
  ```

- **本地开源仿真/综合/形式化工具（按需安装，脚本会自动识别）**：
  - **macOS (Homebrew)**：
    ```bash
    brew install verilator yosys sv2v z3
    ```
  - **Ubuntu / Debian**：
    ```bash
    sudo apt-get update && sudo apt-get install -y verilator yosys iverilog z3
    ```
  - **一站式全套工具包（含 `sby`、`sv2v`、`verible-verilog-lint`、`surfer`）**：安装 [YosysHQ OSS CAD Suite](https://github.com/YosysHQ/oss-cad-suite-build/releases) 并将 `bin` 加入 `PATH`。

随时运行以下命令查看当前机器可用路径：

```bash
python3 rtl_check/scripts/probe_toolchain.py
```

---

## 快速上手（`examples/tiny_alu_pipe`）

仓库内置了一个带 `valid/ready` 反压的两级流水线乘加示例（[`examples/tiny_alu_pipe/tiny_alu_pipe.sv`](examples/tiny_alu_pipe/tiny_alu_pipe.sv)）以及配套结构预算文件 [`qor_budget.yaml`](examples/tiny_alu_pipe/qor_budget.yaml)。

无需配置商业服务器，克隆后即可直接运行本地检查与预算门禁：

```bash
python3 rtl_check/scripts/pre_synth_check.py \
  --top tiny_alu_pipe \
  -f examples/tiny_alu_pipe/rtl.f \
  --budget examples/tiny_alu_pipe/qor_budget.yaml \
  --out-dir /tmp/tiny_alu_check
```

终端输出示例（Verilator + Yosys）：

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

修改 RTL 后再次执行同一命令，会自动对比同 `--out-dir` 下的历史基线并打印 `Prev -> Current -> Delta` 增量变化表。

---

## 商业 EDA 签核配置（`eda_runner/`）

### 第一次使用：让 Agent 帮你接入服务器

直接告诉 Agent 服务器的 SSH 地址（例如“连 `designer@eda-server`，端口 2222，经跳板机 `designer@bastion`”），它会替你运行初始化向导；也可以自己运行：

```bash
python3 eda_runner/scripts/eda_setup.py init --target lab \
  --host designer@eda-server --port 2222 --jump designer@bastion --default
```

向导会依次：

1. 检查 `ssh -o BatchMode=yes` 能否免密登录。必须使用密钥登录；只支持密码的服务器请先自己运行一次 `ssh-copy-id <host>`。连不上时以 `rc=20` 退出并提示怎么修。
2. 从远端登录环境读取 License 变量（`SNPSLMD_LICENSE_FILE`、`CDS_LIC_FILE`、`MGLS_LICENSE_FILE`、`LM_LICENSE_FILE`）和 `PATH`。
3. 在常见安装目录（`/opt`、`/eda`、`/tools`、`/cad` 等，可用 `--add-search-root` 追加）里查找工具，优先用你 `PATH` 里的那份，否则选最新版本。
4. 保存为 `~/.config/digital-ic-suite/tools.d/lab.yaml`（权限 600），并打印汇总：哪些工具找到了、缺哪个 License、哪些工具有多个版本。需要指定时用 `--license synopsys=27000@lic-host` 或 `--tool syn-dc=/path/bin/dc_shell` 加 `--force` 重跑。

用 `eda_setup.py list` 查看、`eda_setup.py remove --target lab` 删除已接入的服务器。每台服务器的工具路径和 License 各自独立，多台服务器可以并存。

### 手动配置

也可以手写配置：

1. 将 [`templates/eda_tools.example.yaml`](templates/eda_tools.example.yaml) 复制到仓库之外的配置目录：

   ```bash
   mkdir -p ~/.config/digital-ic-suite
   cp templates/eda_tools.example.yaml ~/.config/digital-ic-suite/tools.yaml
   ```

2. 在 `~/.config/digital-ic-suite/tools.yaml`（或项目根目录 `.eda_tools.yaml`）中填入你的 SSH 主机别名（或跳板机 `command` 包装脚本）、License 服务器（如 `SNPSLMD_LICENSE_FILE`）与工具安装路径（仓库 `.gitignore` 已默认屏蔽 `.eda_tools.yaml`、`tools.yaml` 和 `tools.d/`）。

### 运行作业

1. 单次 SSH 预检连接、环境变量、License 连通性与工具探针：

   ```bash
   python3 eda_runner/scripts/eda_preflight.py syn-dc --level probe
   ```

2. 配合 [`templates/syn_dc.tcl`](templates/syn_dc.tcl) 或 [`templates/lec_fm.tcl`](templates/lec_fm.tcl) 运行独立原子作业：

   ```bash
   bash eda_runner/scripts/eda_job.sh init my_block --reuse
   bash eda_runner/scripts/eda_job.sh sync my_block ./rtl src
   bash eda_runner/scripts/eda_job.sh run my_block syn-dc \
     --cmd 'cd "$EDA_JOB_DIR/src/rtl" && "$EDA_TOOL_BIN" -64bit -f run.tcl'
   bash eda_runner/scripts/eda_job.sh fetch my_block ./results reports
   ```

---

## 仓库结构与测试

```text
digital-ic-suite/
├── SKILL.md                          # Skill 主规范与执行闭环入口
├── agents/openai.yaml                # Codex 等客户端 UI 元数据
├── rtl_check/scripts/
│   ├── probe_toolchain.py            # 本地与商业 EDA 工具链探测
│   ├── pre_synth_check.py            # 自适应 Lint + Yosys QoR + 死逻辑守卫
│   ├── flist_parser.py               # 支持 -f/-F 与惰性 -y 库的 filelist 解析器
│   ├── netlist_qor.py                # 层级化网表资源、扇出与死逻辑分析
│   └── qor_budget.py                 # 结构预算与历史趋势比对
├── eda_runner/scripts/
│   ├── eda_setup.py                  # 首次接入服务器的初始化向导（tools.d/<名字>.yaml）
│   ├── eda_env.py                    # 配置加载（tools.yaml + tools.d/）与跨 Shell 封装
│   ├── eda_preflight.py              # 单会话 SSH/License/工具可用性预检
│   ├── eda_exec.sh                   # 单会话远端命令执行器
│   └── eda_job.sh                    # 原子化 init/sync/run/fetch 作业管理
├── references/
│   ├── rtl-readability-and-comments.md  # 文件头、注释、排版规则与可读性评审清单
│   ├── sv-coding-guidelines.md       # 可综合 SV 子集与跨工具陷阱手册
│   ├── microarch-datapath-sram.md    # 拍数/II 调度、定点量化、SRAM 与预算说明
│   ├── cdc-sta-lec-signoff.md        # CDC/RDC、SpyGlass 排查、SDC、4 层 PPA 与 LEC
│   └── dv-and-formal.md              # 自适应验证（Verilator/cocotb/Icarus）与形式化
├── templates/
│   ├── rtl_module_template.sv        # 注释完整、零告警的模块骨架
│   ├── prim_cells.sv                 # 硅片级基础模块与 ASSERT* 宏
│   ├── qor_budget.yaml               # 结构 QoR 预算配置模板
│   ├── eda_tools.example.yaml        # 商业 EDA 外部环境配置模板
│   ├── syn_dc.tcl                    # Synopsys Design Compiler 基线脚本
│   └── lec_fm.tcl                    # Synopsys Formality LEC 基线脚本
├── examples/tiny_alu_pipe/           # 开箱即用的两级流水线乘加示例
├── tests/                            # 单元与集成测试套件
├── requirements.txt
└── LICENSE
```

运行完整测试套件：

```bash
python3 -B -m unittest discover -s tests -v
```

测试覆盖：工具缺失降级、`flist_parser`（`-f` 与 `-F` 语义及惰性 `-y`）、Latch 分离、死逻辑/触发器剪枝守卫、`sv2v` 复杂 package 自动降阶（当本机安装 `sv2v` 时）、无 `PyYAML` 零依赖启动、`tcsh`/`csh` 远端登录 Shell 兼容、服务器初始化向导（工具发现、私有文件权限、手写配置不被改动、多服务器互不干扰、连不上时的报错）、可读性模板与示例零告警、Markdown 链接有效性以及敏感信息防泄漏自检。

---

## 开源协议与商标声明

- 本项目基于 [MIT License](LICENSE) 开源。
- **商标声明**：Synopsys、Design Compiler、PrimeTime、Formality、VCS、SpyGlass、Cadence、Xcelium 及 Siemens/Mentor 等均为各自厂商的注册商标。本仓库不包含任何商业 EDA 软件、License 密钥、代工厂 PDK 或厂商受版权保护的参考流程脚本。
