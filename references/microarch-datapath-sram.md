# Microarchitecture, Fixed-Point Datapaths, and Storage Contracts

Decide hardware topology, cycle schedule, numeric precision, and memory mapping
before writing RTL.

---

## 1. Microarchitecture & Cycle Scheduling

Compare candidate architectures whenever an area, throughput (`II`), latency, or
memory-port constraint creates a real trade-off (e.g., full spatial unrolling at
`II = 1` vs. time-multiplexed operator reuse at `II = K`).

### 1.1 Define Timing Precisely
- **Latency**: Exact cycle count from input handshake (`in_valid && in_ready`) to
  corresponding output assertion (`out_valid`), stating behavior under downstream stall.
- **Initiation Interval (`II`)**: Minimum cycles between two consecutive accepted
  inputs (`II = 1` = full streaming; `II = 4` = one input every 4 cycles).

### 1.2 Schedule Resource Sharing Explicitly
Whenever operators or SRAM ports are time-shared (`reuse_factor > 1`), multi-cycle
memories exist, or `II < Latency`, write out a cycle-by-stage table for **two
consecutive overlapping transactions ($T_0, T_1$)** before coding RTL:
- Verify no two active transactions claim the same multiplier, adder, writeback bus,
  or SRAM port in the same cycle.
- Verify stall/backpressure freezes or drains stages without overwriting in-flight data.

---

## 2. Fixed-Point & Numeric Datapath Specification

SystemVerilog expression sizing is **not** a numeric specification. Lock every
external and internal arithmetic stage explicitly:

1. **Format notation**: Total width $W$, signedness (`S`/`U`), integer bits $I$, and
   fractional bits $F$ (e.g., signed `Q7.8` = 16 bits total: 1 sign, 7 integer, 8 frac).
2. **Full-precision growth before quantization**:
   - $W_a$-bit $\times$ $W_b$-bit multiply -> exact $(W_a + W_b)$-bit product (`F_prod = F_a + F_b`).
   - Sum of $N$ terms -> grow accumulator by $\lceil \log_2 N \rceil$ guard bits so intermediate sums cannot wrap before final saturation!
3. **Explicit Rounding & Tie Rule**:
   - Truncation (`floor` toward $-\infty$), round-half-up (`+ 0.5 LSB` before arithmetic shift),
     symmetric round-half-away-from-zero, or convergent round-half-to-even (`RN`).
   - Beware negative two's-complement right shift (`>>>`): `-1 >>> 1 == -1` (floors toward $-\infty$, NOT toward zero!). Test negative half-way ties (`-0.5`, `-1.5`) explicitly against an independent Python/SV integer reference model.
4. **Order of Rounding vs. Saturation**:
   - Always round **first** at extended guard width, **then** saturate to target rails
     ($[-2^{W-1}, +2^{W-1}-1]$ for signed; $[0, 2^W-1]$ for unsigned) — rounding at exact
     target width overflows `MAX_POS` into `MIN_NEG` before saturation sees it!

---

## 3. Logical Memory vs. Physical Foundry SRAM

Separate **logical storage requirements** (width, depth, throughput, collision rules)
from **physical implementation** (banking, word-width packing, foundry macro wrapper):

- **Foundry SRAM Wrapper Discipline**:
  - Never instantiate raw vendor macro names directly inside datapath modules; wrap
    each macro shape inside one clean project SRAM wrapper (`sram_1rw_256x32_wrap`).
  - Simulation uses the behavioral/Verilog model with matching 1-cycle (or pipelined)
    read latency and X-injection on illegal read/write collisions; DC/PT link the
    matching `.db` timing/power view; Formality black-boxes the wrapper identically on
    both reference and implementation sides.
  - If a single port cannot meet `II = 1` read+write bandwidth, solve it in
    microarchitecture (2-bank even/odd interleaving, wide-word 2x fetch, or true 1R1W
    macro) — never fake dual-port access on a single-port SRAM in RTL.

---

## 4. Machine-Checkable QoR Budgets (`pre_synth_check.py --budget`)

When enforcing explicit hardware budgets across iterations, pass
`--budget qor_budget.yaml` (template: `templates/qor_budget.yaml`).

### Supported `resources` Rules
Map arbitrary label names to operator constraints:
- `operation` (required): `multiply`, `add_sub`, `compare`, `shift`, `divide_mod`, or `mux`
- `expected`, `min`, `max` (at least one non-negative int)
- `a_width`, `b_width`, `y_width` (optional exact bit-width filter)
- `match` (optional glob selector on `id`, `path`, `name`, `module`, `instance`, `cell_type`; `$paramod` names are automatically demangled to the clean SV module name)
- `allow_overlap` (bool, default `false`: prevents two rules accidentally claiming the same operator unless both set `true`)

### Supported `storage` Rules
Apply to inferred Yosys memories (`$mem` / `$mem_v2`):
- `implementation`: `memory`, `ram`, `sram`, or `regfile` (requires >= 1 inferred memory object, catching arrays that accidentally dissolved into flops!)
- `width`, `depth`, `banks`, `read_ports`, `write_ports`, `read_latency` (`0` async, `1` sync), `bits`, `match`, `allow_overlap`

### Supported `limits` Budgets
- `latch_bits_max` & `comb_loops_max`: default to **`0`** whenever `--budget` is used (catch accidental latches & SCC loops immediately)
- `stuck_output_bits_max`, `undriven_output_bits_max`, `max_fanout_max`
- `ff_bits_max`, `mux_data_bits_max`, `gate_depth_max` (post-`techmap` gate levels), `word_depth_max` (operator hops), `memory_bits_min/max`, `memory_objects_min/max`
- `regression_pct_max`: map metric names (`ff_bits`, `gate_depth`, `mux_data_bits`, `multiply`, etc.) to max allowed `%` growth vs. `--baseline`
