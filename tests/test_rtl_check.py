#!/usr/bin/env python3
"""Behavioral tests for adaptive toolchain probe, pre-synthesis QoR check, dead-logic guards, and degradation."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
RTL_CHECK_DIR = ROOT / "rtl_check" / "scripts"
CHECK_CLI = RTL_CHECK_DIR / "pre_synth_check.py"
PRIM_CELLS_SV = ROOT / "templates" / "prim_cells.sv"
sys.path.insert(0, str(RTL_CHECK_DIR))
sys.dont_write_bytecode = True

import flist_parser  # noqa: E402
import probe_toolchain  # noqa: E402
import qor_budget  # noqa: E402


@unittest.skipUnless(shutil.which("verilator") and shutil.which("yosys"), "Verilator and Yosys required")
class RtlCheckIntegrationTests(unittest.TestCase):
    def test_lazy_libdir_skips_unused_broken_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lib = root / "lib"
            lib.mkdir()
            (lib / "used_mac.sv").write_text(
                "module used_mac #(parameter W=8) (input logic [W-1:0] a, b, output logic [2*W-1:0] p);\n"
                "  assign p = a * b;\n"
                "endmodule\n"
            )
            (lib / "unused_broken.v").write_text("module unused_broken( THIS WOULD FAIL PARSE ;\n")
            (root / "top.sv").write_text(
                "module top(input logic [15:0] a, b, output logic [31:0] p);\n"
                "  used_mac #(.W(16)) u_mac (.a(a), .b(b), .p(p));\n"
                "endmodule\n"
            )
            (root / "rtl.f").write_text("-y lib\n+libext+.sv+.v\ntop.sv\n")
            budget = root / "budget.yaml"
            budget.write_text(
                "design: top\n"
                "resources:\n"
                "  mac_mul:\n"
                "    operation: multiply\n"
                "    expected: 1\n"
                "    a_width: 16\n"
                "    match:\n"
                "      module: used_mac\n"
            )
            proc = subprocess.run(
                [sys.executable, str(CHECK_CLI), "--top", "top", "-f", str(root / "rtl.f"),
                 "--budget", str(budget), "--out-dir", str(root / "out")],
                text=True, capture_output=True,
            )
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            summary = json.loads((root / "out" / "qor_summary.json").read_text())
            self.assertEqual(summary["qor"]["operator_inventory"][0]["module"], "used_mac")

    def test_latch_separation_and_iterative_optimization_trend(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out = root / "rtl_check"
            rtl = root / "sum8.sv"
            flist = root / "rtl.f"
            flist.write_text("sum8.sv\n")
            budget = root / "budget.yaml"
            budget.write_text("design: sum8\nlimits:\n  latch_bits_max: 0\n")

            rtl.write_text(
                "module sum8(\n"
                "  input  logic        clk, rst_n, en,\n"
                "  input  logic [7:0]  a0, a1, a2, a3, a4, a5, a6, a7,\n"
                "  output logic [10:0] y_q,\n"
                "  output logic [7:0]  dbg_lat\n"
                ");\n"
                "  logic [10:0] acc;\n"
                "  always_comb begin\n"
                "    acc = ((((((({3'b0,a0}+{3'b0,a1})+{3'b0,a2})+{3'b0,a3})+{3'b0,a4})+{3'b0,a5})+{3'b0,a6})+{3'b0,a7});\n"
                "  end\n"
                "  always_latch if (en) dbg_lat = a0;\n"
                "  always_ff @(posedge clk or negedge rst_n) begin\n"
                "    if (!rst_n) y_q <= '0;\n"
                "    else        y_q <= acc;\n"
                "  end\n"
                "endmodule\n"
            )
            r1 = subprocess.run(
                [sys.executable, str(CHECK_CLI), "--top", "sum8", "-f", str(flist),
                 "--budget", str(budget), "--out-dir", str(out)],
                text=True, capture_output=True,
            )
            self.assertEqual(r1.returncode, 1, r1.stdout)
            s1 = json.loads((out / "qor_summary.json").read_text())
            self.assertEqual(s1["qor"]["latch_bits"], 8)
            self.assertEqual(s1["qor"]["ff_bits"], 11)

            rtl.write_text(
                "module sum8(\n"
                "  input  logic        clk, rst_n, en,\n"
                "  input  logic [7:0]  a0, a1, a2, a3, a4, a5, a6, a7,\n"
                "  output logic [10:0] y_q,\n"
                "  output logic [7:0]  dbg_lat\n"
                ");\n"
                "  wire [8:0]  s0 = {1'b0,a0}+{1'b0,a1}, s1 = {1'b0,a2}+{1'b0,a3};\n"
                "  wire [8:0]  s2 = {1'b0,a4}+{1'b0,a5}, s3 = {1'b0,a6}+{1'b0,a7};\n"
                "  wire [9:0]  p0 = {1'b0,s0}+{1'b0,s1}, p1 = {1'b0,s2}+{1'b0,s3};\n"
                "  wire [10:0] acc = {1'b0,p0}+{1'b0,p1};\n"
                "  assign dbg_lat = en ? a0 : 8'h00;\n"
                "  always_ff @(posedge clk) if (en) y_q <= acc;\n"
                "endmodule\n"
            )
            r2 = subprocess.run(
                [sys.executable, str(CHECK_CLI), "--top", "sum8", "-f", str(flist),
                 "--budget", str(budget), "--out-dir", str(out)],
                text=True, capture_output=True,
            )
            self.assertEqual(r2.returncode, 0, r2.stdout + r2.stderr)
            self.assertIn("Prev", r2.stdout)
            self.assertIn("Delta", r2.stdout)
            s2 = json.loads((out / "qor_summary.json").read_text())
            self.assertEqual(s2["qor"]["latch_bits"], 0)
            self.assertLess(s2["qor"]["word_depth"], s1["qor"]["word_depth"])
            self.assertLess(s2["qor"]["gate_depth"], s1["qor"]["gate_depth"])
            self.assertEqual(len((out / "qor_history.jsonl").read_text().strip().splitlines()), 2)

    def test_parameter_sweep_computes_trend_without_blocking(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out = root / "sweep"
            (root / "vec.sv").write_text(
                "module vec #(parameter LANES=4) (\n"
                "  input logic [LANES*8-1:0] a, b,\n"
                "  output logic [LANES*16-1:0] y\n"
                ");\n"
                "  genvar i;\n"
                "  generate for (i=0; i<LANES; i=i+1) begin : gen_lane\n"
                "    assign y[i*16 +: 16] = a[i*8 +: 8] * b[i*8 +: 8];\n"
                "  end endgenerate\n"
                "endmodule\n"
            )
            (root / "rtl.f").write_text("vec.sv\n")
            subprocess.run(
                [sys.executable, str(CHECK_CLI), "--top", "vec", "-f", str(root / "rtl.f"),
                 "-G", "LANES=4", "--out-dir", str(out)],
                check=True, capture_output=True,
            )
            r2 = subprocess.run(
                [sys.executable, str(CHECK_CLI), "--top", "vec", "-f", str(root / "rtl.f"),
                 "-G", "LANES=2", "--out-dir", str(out)],
                text=True, capture_output=True,
            )
            self.assertEqual(r2.returncode, 0, r2.stdout)
            self.assertIn("config changed vs baseline", r2.stdout)
            summary = json.loads((out / "qor_summary.json").read_text())
            mul_row = next(m for m in summary["baseline"]["metrics"] if m["metric"] == "multiply")
            self.assertEqual((mul_row["baseline"], mul_row["current"], mul_row["delta"]), (4, 2, -2))

    def test_dead_logic_stuck_output_and_pruned_ff_guard(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "dead.sv").write_text(
                "module dead(input logic clk, input logic [7:0] d_i, output logic [7:0] q_o);\n"
                "  logic [7:0] stage_q;\n"
                "  always_ff @(posedge clk) begin\n"
                "    stage_q <= d_i;\n"
                "    q_o <= 1'b0 ? stage_q : 8'h00;\n"
                "  end\n"
                "endmodule\n"
            )
            (root / "rtl.f").write_text("dead.sv\n")
            (root / "budget.yaml").write_text("design: dead\nlimits:\n  stuck_output_bits_max: 0\n")
            r = subprocess.run(
                [sys.executable, str(CHECK_CLI), "--top", "dead", "-f", str(root / "rtl.f"),
                 "--budget", str(root / "budget.yaml"), "--out-dir", str(root / "out")],
                text=True, capture_output=True,
            )
            self.assertEqual(r.returncode, 1, r.stdout)
            self.assertIn("Dead-Logic Guard", r.stdout)
            self.assertIn("Pruned-FF Guard", r.stdout)
            summary = json.loads((root / "out" / "qor_summary.json").read_text())
            self.assertEqual(summary["qor"]["stuck_output_bits"], 8)
            self.assertEqual(summary["qor"]["pre_opt_ff_bits"], 16)
            self.assertEqual(summary["qor"]["ff_bits"], 0)

    def test_fatal_blkseq_and_high_fanout_detection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "bad_blk.sv").write_text(
                "module bad_blk(input logic clk, d_i, output logic q_o);\n"
                "  always_ff @(posedge clk) q_o = d_i;\n"
                "endmodule\n"
            )
            (root / "rtl.f").write_text("bad_blk.sv\n")
            r_bad = subprocess.run(
                [sys.executable, str(CHECK_CLI), "--top", "bad_blk", "-f", str(root / "rtl.f"),
                 "--out-dir", str(root / "out1")],
                text=True, capture_output=True,
            )
            self.assertNotEqual(r_bad.returncode, 0)

            (root / "fan.sv").write_text(
                "module fan(input logic clk, en_i, input logic [39:0] d_i, output logic [39:0] q_o);\n"
                "  always_ff @(posedge clk) if (en_i) q_o <= d_i;\n"
                "endmodule\n"
            )
            (root / "rtl2.f").write_text("fan.sv\n")
            r_fan = subprocess.run(
                [sys.executable, str(CHECK_CLI), "--top", "fan", "-f", str(root / "rtl2.f"),
                 "--out-dir", str(root / "out2")],
                text=True, capture_output=True,
            )
            self.assertEqual(r_fan.returncode, 0, r_fan.stdout + r_fan.stderr)
            summary = json.loads((root / "out2" / "qor_summary.json").read_text())
            self.assertGreaterEqual(summary["qor"]["max_fanout"], 40)

    def test_graceful_degradation_when_optional_tools_absent(self) -> None:
        yosys_path = shutil.which("yosys")
        assert yosys_path is not None
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            wrapper = fake_bin / "yosys"
            wrapper.write_text(f'#!/bin/sh\nexec "{yosys_path}" "$@"\n')
            wrapper.chmod(0o755)
            (root / "simple.sv").write_text(
                "module simple(input logic clk, a_i, output logic y_o);\n"
                "  always_ff @(posedge clk) y_o <= a_i;\n"
                "endmodule\n"
            )
            (root / "rtl.f").write_text("simple.sv\n")
            # PATH holds only the yosys wrapper: distro packages put verilator and
            # iverilog in /usr/bin, so adding system dirs would defeat the test.
            env = {**os.environ, "PATH": str(fake_bin)}
            r = subprocess.run(
                [sys.executable, str(CHECK_CLI), "--top", "simple", "-f", str(root / "rtl.f"),
                 "--out-dir", str(root / "out")],
                text=True, capture_output=True, env=env,
            )
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            summary = json.loads((root / "out" / "qor_summary.json").read_text())
            self.assertEqual(summary["tools"]["lint"]["status"], "missing")
            self.assertEqual(summary["tools"]["verible"]["status"], "skipped")
            self.assertEqual(summary["qor"]["ff_bits"], 1)

    @unittest.skipUnless(shutil.which("sv2v"), "sv2v required for complex-SV fallback test")
    def test_sv2v_auto_fallback_for_complex_package_sv(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pkg.sv").write_text(
                "package my_pkg;\n"
                "  typedef struct packed { logic [7:0] data; logic valid; } pkt_t;\n"
                "endpackage\n"
            )
            (root / "pkg_top.sv").write_text(
                "module pkg_top import my_pkg::*; (\n"
                "  input  logic clk,\n"
                "  input  pkt_t in_i,\n"
                "  output pkt_t out_o\n"
                ");\n"
                "  always_ff @(posedge clk) out_o <= in_i;\n"
                "endmodule\n"
            )
            (root / "rtl.f").write_text("pkg.sv\npkg_top.sv\n")
            r = subprocess.run(
                [sys.executable, str(CHECK_CLI), "--top", "pkg_top", "-f", str(root / "rtl.f"),
                 "--out-dir", str(root / "out")],
                text=True, capture_output=True,
            )
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            summary = json.loads((root / "out" / "qor_summary.json").read_text())
            self.assertEqual(summary["qor"]["ff_bits"], 9)

    def test_prim_cells_compile_cleanly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "rtl.f").write_text(f'"{PRIM_CELLS_SV}"\n')
            for mod in ("prim_flop_2sync", "prim_rst_sync", "prim_skid_buf", "prim_sram_1rw_wrap"):
                r = subprocess.run(
                    [sys.executable, str(CHECK_CLI), "--top", mod, "-f", str(root / "rtl.f"),
                     "--out-dir", str(root / f"out_{mod}")],
                    text=True, capture_output=True,
                )
                self.assertEqual(r.returncode, 0, f"{mod} failed:\n{r.stdout}\n{r.stderr}")
                summary = json.loads((root / f"out_{mod}" / "qor_summary.json").read_text())
                self.assertEqual(summary["qor"]["latch_bits"], 0)
            r_icg = subprocess.run(
                ["verilator", "--lint-only", "-Wall", "-Wno-LATCH", "--top-module", "prim_icg", str(PRIM_CELLS_SV)],
                text=True, capture_output=True,
            )
            self.assertEqual(r_icg.returncode, 0, r_icg.stdout + r_icg.stderr)

    def test_readability_template_and_example_are_lint_clean(self) -> None:
        """The RTL style references must themselves pass -Wall lint and the QoR gate."""
        cases = (
            ("rtl_module_template", ROOT / "templates" / "rtl_module_template.sv"),
            ("tiny_alu_pipe", ROOT / "examples" / "tiny_alu_pipe" / "tiny_alu_pipe.sv"),
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for top, src in cases:
                lint = subprocess.run(
                    ["verilator", "--lint-only", "-Wall", "-Wno-DECLFILENAME", "--top-module", top,
                     str(PRIM_CELLS_SV), str(src)],
                    text=True, capture_output=True,
                )
                self.assertEqual(lint.returncode, 0, f"{top}:\n{lint.stdout}{lint.stderr}")
                flist = root / f"{top}.f"
                flist.write_text(f'"{PRIM_CELLS_SV}"\n"{src}"\n')
                r = subprocess.run(
                    [sys.executable, str(CHECK_CLI), "--top", top, "-f", str(flist),
                     "--out-dir", str(root / f"out_{top}"), "--style", "off"],
                    text=True, capture_output=True,
                )
                self.assertEqual(r.returncode, 0, f"{top}:\n{r.stdout}{r.stderr}")
                qor = json.loads((root / f"out_{top}" / "qor_summary.json").read_text())["qor"]
                self.assertEqual(qor["latch_bits"], 0)
                self.assertEqual(qor["stuck_output_bits"], 0)


class UnitSafetyTests(unittest.TestCase):
    def test_probe_toolchain_degrades_cleanly_with_zero_binaries(self) -> None:
        inv = probe_toolchain.collect_inventory()
        self.assertIn("recommended_strategy", inv)
        with mock.patch("shutil.which", return_value=None):
            empty_inv = probe_toolchain.collect_inventory()
            self.assertEqual(empty_inv["recommended_strategy"]["formal"], "unavailable locally (use SVA assertions in simulation)")
            self.assertIn("unavailable", empty_inv["recommended_strategy"]["pre_synth_check"])

    def test_filelist_f_vs_F_bases(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sub = root / "sub"
            sub.mkdir()
            (root / "m.sv").write_text("module m_root; endmodule\n")
            (sub / "m.sv").write_text("module m_sub; endmodule\n")
            (sub / "nested.f").write_text("m.sv\n")
            (root / "low.f").write_text("-f sub/nested.f\n")
            (root / "up.f").write_text("-F sub/nested.f\n")
            self.assertEqual(flist_parser.parse_filelist(root / "low.f").files, [(root / "m.sv").resolve()])
            self.assertEqual(flist_parser.parse_filelist(root / "up.f").files, [(sub / "m.sv").resolve()])

    def test_exclusive_resource_claim_catches_accidental_overlap(self) -> None:
        qor = {"operator_inventory": [{"id": "top/m0", "operation": "multiply", "a_width": 8}]}
        res = qor_budget.check_budget(
            {"resources": {
                "r1": {"operation": "multiply", "expected": 1},
                "r2": {"operation": "multiply", "expected": 1},
            }},
            qor,
        )
        self.assertEqual(res["status"], "fail")

    def test_pre_synth_check_runs_without_pyyaml_when_no_budget(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "m.sv").write_text(
                "module m(input logic clk, a_i, output logic y_o);\n"
                "  always_ff @(posedge clk) y_o <= a_i;\n"
                "endmodule\n"
            )
            (root / "rtl.f").write_text("m.sv\n")
            run_check = "0"
            if shutil.which("yosys"):
                run_check = "1"
            code = (
                "import sys\n"
                "sys.modules['yaml'] = None\n"
                f"sys.path.insert(0, {str(RTL_CHECK_DIR)!r})\n"
                "import pre_synth_check, qor_budget\n"
                "cmp_res = qor_budget.compare_baseline({'qor': {'ff_bits': 1}}, {'ff_bits': 1})\n"
                "assert cmp_res['status'] == 'pass'\n"
                f"if {run_check!r} == '1':\n"
                f"    sys.argv = ['pre_synth_check.py', '--top', 'm', '-f', {str(root / 'rtl.f')!r}, '--out-dir', {str(root / 'out')!r}]\n"
                "    rc = pre_synth_check.main()\n"
                "    assert rc == 0, rc\n"
            )
            proc = subprocess.run([sys.executable, "-c", code], text=True, capture_output=True)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)


if __name__ == "__main__":
    unittest.main()
