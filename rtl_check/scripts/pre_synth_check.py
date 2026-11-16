#!/usr/bin/env python3
"""Adaptive pre-synthesis RTL lint, elaboration, QoR extraction, dead-logic guard, and trend tracker."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
sys.dont_write_bytecode = True

from flist_parser import Filelist, parse_filelist, resolve_library_files  # noqa: E402
from netlist_qor import analyze_mapped_netlist, summarize_netlist  # noqa: E402
from qor_budget import check_budget, compare_baseline, load_yaml  # noqa: E402

FORMAT_VERSION = 3
IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*")
PARAM_VALUE = re.compile(r"[A-Za-z0-9_.$'+:/?\-]+")
LTP_RE = re.compile(r"Longest topological path .*?\(length=(\d+)\)")
SCC_RE = re.compile(r"Found (\d+) SCCs\.")

FATAL_VERILATOR_WARNINGS = (
    "-Werror-LATCH",
    "-Werror-BLKSEQ",
    "-Werror-COMBDLY",
    "-Werror-MULTIDRIVEN",
    "-Werror-UNOPTFLAT",
    "-Werror-IMPLICIT",
)

VERIBLE_RELAXED_RULES = (
    "-line-length,-no-trailing-spaces,-explicit-parameter-storage-type,-parameter-name-style"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run adaptive local RTL lint + pre-synthesis QoR check with live trend delta."
    )
    parser.add_argument("--top", required=True, help="top module name")
    parser.add_argument("--filelist", "-f", required=True, help="RTL filelist (.f)")
    parser.add_argument("--out-dir", default="build/rtl_check", help="artifact/history directory")
    parser.add_argument("--param", "-G", action="append", default=[], metavar="NAME=VALUE")
    parser.add_argument("--budget", help="optional qor_budget.yaml for structural & QoR limits")
    parser.add_argument("--baseline", help="explicit previous qor_summary.json (defaults to previous run in --out-dir)")
    parser.add_argument("--no-auto-baseline", action="store_true", help="disable auto-diff against previous run in --out-dir")
    parser.add_argument("--strict-lint", action="store_true", help="treat all Verilator lint warnings as fatal")
    parser.add_argument("--skip-lint", action="store_true", help="skip RTL lint")
    parser.add_argument("--sv2v", choices=("auto", "on", "off"), default="auto",
                        help="sv2v bridge mode for complex SV (default: auto fallback if installed)")
    parser.add_argument("--style", choices=("auto", "on", "off"), default="auto",
                        help="verible-verilog-lint advisory style pass (default: auto if installed)")
    parser.add_argument("--json", action="store_true", help="emit JSON summary to stdout instead of table")
    return parser.parse_args()


def parse_params(values: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in values:
        if "=" not in item:
            raise SystemExit(f"Invalid --param {item!r}; expected NAME=VALUE")
        name, value = (part.strip() for part in item.split("=", 1))
        if not IDENTIFIER.fullmatch(name) or not PARAM_VALUE.fullmatch(value):
            raise SystemExit(f"Invalid --param {item!r}")
        result[name] = value
    return result


def run_cmd(cmd: list[str], log: Path, *, cwd: Path | None = None) -> int:
    proc = subprocess.run(cmd, cwd=cwd, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    log.write_text(proc.stdout)
    return proc.returncode


def tool_version(cmd: list[str]) -> str | None:
    try:
        proc = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    lines = proc.stdout.strip().splitlines()
    return lines[0] if lines else None


def digest(value) -> str:
    if isinstance(value, Path):
        payload = value.read_bytes()
    else:
        payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str).encode()
    return hashlib.sha256(payload).hexdigest()


def relative(path: Path, base: Path) -> str:
    try:
        return str(path.resolve().relative_to(base.resolve()))
    except ValueError:
        return str(path.resolve())


def unique_paths(paths: list[Path]) -> list[Path]:
    return list(dict.fromkeys(p.resolve() for p in paths))


def parse_depfiles(directory: Path, project: Path, executable: str, explicit: list[Path]) -> list[Path]:
    depfiles = sorted(directory.glob("*.d"))
    found: list[Path] = []
    for depfile in depfiles:
        first = depfile.read_text(errors="replace").split("\n\n", 1)[0].replace("\\\n", " ")
        rhs = first.split(" : ", 1)[1] if " : " in first else first.split(":", 1)[1] if ":" in first else ""
        for tok in shlex.split(rhs):
            p = Path(tok)
            found.append((p if p.is_absolute() else project / p).resolve())
    tool_path = Path(executable).resolve()
    return [
        p for p in unique_paths(explicit + found)
        if p != tool_path and p.name != "verilator_bin" and "/share/verilator/" not in str(p) and p.is_file()
    ]


def run_lint(inputs: Filelist, top: str, params: dict[str, str], out: Path,
             project: Path, skip: bool, strict: bool) -> tuple[dict, list[Path]]:
    fallback_sources = resolve_library_files(inputs.files, inputs.library_dirs, inputs.libexts)
    if skip:
        return {"tool": "none", "status": "skipped", "warnings": 0}, fallback_sources

    verilator = shutil.which("verilator")
    if verilator:
        deps = out / "deps"
        shutil.rmtree(deps, ignore_errors=True)
        deps.mkdir(parents=True)
        cmd = [verilator, "--lint-only", "--timing", "--MMD", "--MP", "--Mdir", str(deps), "-Wall"]
        if not strict:
            cmd.append("-Wno-fatal")
            cmd.extend(FATAL_VERILATOR_WARNINGS)
        cmd += ["--top-module", top]
        cmd += [f"-I{p}" for p in inputs.include_dirs]
        for libdir in inputs.library_dirs:
            cmd += ["-y", str(libdir)]
        if inputs.library_dirs and inputs.libexts:
            cmd.append("+libext+" + "+".join(inputs.libexts))
        cmd += [f"-D{d}" for d in inputs.defines]
        cmd += [f"-G{k}={v}" for k, v in params.items()]
        cmd += [str(p) for p in inputs.files]

        log_path = out / "verilator.log"
        rc = run_cmd(cmd, log_path, cwd=project)
        log_lines = log_path.read_text(errors="replace").splitlines() if log_path.exists() else []
        warn_count = sum(1 for line in log_lines if "%Warning" in line)
        status = "pass" if rc == 0 else "fail"
        sources = parse_depfiles(deps, project, verilator, fallback_sources) if rc == 0 else fallback_sources
        return {
            "tool": "verilator",
            "status": status,
            "returncode": rc,
            "warnings": warn_count,
            "version": tool_version([verilator, "--version"]),
        }, sources

    iverilog = shutil.which("iverilog")
    if iverilog:
        cmd = [iverilog, "-g2012", "-Wall", "-tnull", "-s", top]
        cmd += [f"-I{p}" for p in inputs.include_dirs]
        for libdir in inputs.library_dirs:
            cmd += ["-y", str(libdir)]
        cmd += [f"-D{d}" for d in inputs.defines]
        cmd += [f"-P{top}.{k}={v}" for k, v in params.items()]
        cmd += [str(p) for p in fallback_sources]
        log_path = out / "iverilog_lint.log"
        rc = run_cmd(cmd, log_path, cwd=project)
        lines = log_path.read_text(errors="replace").splitlines() if log_path.exists() else []
        warn_count = sum(1 for ln in lines if "warning:" in ln.lower())
        return {
            "tool": "iverilog",
            "status": "pass" if rc == 0 else "fail",
            "returncode": rc,
            "warnings": warn_count,
            "version": tool_version([iverilog, "-V"]),
        }, fallback_sources

    return {"tool": "none", "status": "missing", "warnings": 0}, fallback_sources


def run_verible_style(sources: list[Path], out: Path, project: Path, mode: str) -> dict:
    if mode == "off":
        return {"status": "skipped", "findings": 0}
    verible = shutil.which("verible-verilog-lint")
    if not verible:
        return {"status": "missing" if mode == "on" else "skipped", "findings": 0}
    sv_files = [str(p) for p in sources if p.suffix.lower() in {".v", ".sv"}]
    if not sv_files:
        return {"status": "skipped", "findings": 0}

    log_path = out / "verible.log"
    cmd = [verible, f"--rules={VERIBLE_RELAXED_RULES}", *sv_files]
    proc = subprocess.run(cmd, cwd=project, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if proc.returncode == 2:
        proc = subprocess.run([verible, *sv_files], cwd=project, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    log_path.write_text(proc.stdout)
    findings = [ln.strip() for ln in proc.stdout.splitlines() if ln.strip() and ":" in ln]
    return {
        "status": "advisory",
        "findings": len(findings),
        "sample": findings[:5],
        "version": tool_version([verible, "--version"]),
    }


def _ys_quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _run_sv2v(sv2v_exe: str, inputs: Filelist, sources: list[Path], out: Path, project: Path) -> tuple[bool, Path]:
    lowered = out / "sv2v_lowered.v"
    cmd = [sv2v_exe]
    cmd += [f"-I{p}" for p in inputs.include_dirs]
    cmd += [f"-D{d}" for d in inputs.defines]
    cmd += [str(p) for p in sources]
    proc = subprocess.run(cmd, cwd=project, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        (out / "sv2v.log").write_text(proc.stderr + "\n" + proc.stdout)
        return False, lowered
    lowered.write_text(proc.stdout)
    return True, lowered


def _execute_yosys(yosys: str, read_cmd: list[str], hier_cmd: list[str], top: str,
                   params: dict[str, str], out: Path, project: Path,
                   log_path: Path, apply_params: bool) -> tuple[int, dict | None]:
    script = out / "pre_synth.ys"
    pre_netlist = out / "pre_opt_netlist.json"
    netlist = out / "elaborated.json"
    mapped_netlist = out / "mapped_flat.json"
    word_ltp = out / "word_ltp.rpt"
    gate_ltp = out / "gate_ltp.rpt"
    scc_rpt = out / "scc_loops.rpt"

    lines = [" ".join(read_cmd)]
    if apply_params:
        lines.extend(f"chparam -set {k} {v} {top}" for k, v in params.items())
    lines.extend([
        " ".join(hier_cmd),
        "proc",
        f"write_json {_ys_quote(str(pre_netlist))}",
        "opt; memory -nomap; opt; check",
        f"write_json {_ys_quote(str(netlist))}",
        "flatten; opt",
        f"tee -o {_ys_quote(str(scc_rpt))} scc",
        f"tee -o {_ys_quote(str(word_ltp))} ltp -noff",
        "techmap; opt -fast",
        f"write_json {_ys_quote(str(mapped_netlist))}",
        f"tee -o {_ys_quote(str(gate_ltp))} ltp -noff",
    ])
    script.write_text("\n".join(lines) + "\n")

    rc = run_cmd([yosys, "-s", str(script)], log_path, cwd=project)
    if rc != 0 or not netlist.is_file():
        return rc, None

    qor = summarize_netlist(netlist, top)
    pre_ff_bits = qor["ff_bits"]
    if pre_netlist.is_file():
        try:
            pre_ff_bits = summarize_netlist(pre_netlist, top)["ff_bits"]
        except (OSError, ValueError, KeyError, TypeError):
            pass

    flat_info = {"stuck_output_bits": 0, "undriven_output_bits": 0, "stuck_outputs": [],
                 "max_fanout": 0, "fanout_top": []}
    if mapped_netlist.is_file():
        flat_info = analyze_mapped_netlist(mapped_netlist, top)

    w_matches = LTP_RE.findall(word_ltp.read_text(errors="replace")) if word_ltp.exists() else []
    g_matches = LTP_RE.findall(gate_ltp.read_text(errors="replace")) if gate_ltp.exists() else []
    scc_matches = SCC_RE.findall(scc_rpt.read_text(errors="replace")) if scc_rpt.exists() else []

    pruned_ff_bits = max(0, pre_ff_bits - qor["ff_bits"])
    pruned_ff_pct = (pruned_ff_bits / pre_ff_bits * 100.0) if pre_ff_bits > 0 else 0.0

    qor.update({
        "word_depth": max((int(x) for x in w_matches), default=0),
        "gate_depth": max((int(x) for x in g_matches), default=0),
        "comb_loops": sum(int(x) for x in scc_matches),
        "pre_opt_ff_bits": pre_ff_bits,
        "pruned_ff_bits": pruned_ff_bits,
        "pruned_ff_pct": round(pruned_ff_pct, 1),
        **flat_info,
    })
    qor["logic_depth"] = qor["word_depth"]
    return rc, qor


def run_yosys(inputs: Filelist, resolved_sources: list[Path], top: str, params: dict[str, str],
              out: Path, project: Path, sv2v_mode: str) -> tuple[dict, dict | None]:
    yosys = shutil.which("yosys")
    if not yosys:
        return {"status": "missing", "mode": "none"}, None

    sv_sources = [p for p in resolved_sources if p.suffix.lower() in {".v", ".sv"}] or inputs.files
    sv2v_exe = shutil.which("sv2v")
    ys_ver = tool_version([yosys, "-V"])

    def build_native_cmds() -> tuple[list[str], list[str]]:
        rcmd = ["read_verilog", "-sv"]
        rcmd += ["-I" + _ys_quote(str(p)) for p in inputs.include_dirs]
        rcmd += ["-D" + _ys_quote(d) for d in inputs.defines]
        rcmd += [_ys_quote(str(p)) for p in sv_sources]
        hcmd = [f"hierarchy -check -top {top}"]
        for libdir in inputs.library_dirs:
            hcmd.append(f"-libdir {_ys_quote(str(libdir))}")
        return rcmd, hcmd

    def run_with_sv2v(reason: str) -> tuple[dict, dict | None]:
        assert sv2v_exe is not None
        ok, lowered = _run_sv2v(sv2v_exe, inputs, sv_sources, out, project)
        if not ok:
            return {"status": "fail", "mode": reason, "returncode": 1, "version": ys_ver}, None
        rcmd = ["read_verilog", _ys_quote(str(lowered))]
        hcmd = [f"hierarchy -check -top {top}"]
        try:
            rc, qor = _execute_yosys(yosys, rcmd, hcmd, top, params, out, project,
                                     out / "yosys.log", apply_params=True)
            return {"status": "pass" if rc == 0 else "fail", "mode": reason, "returncode": rc, "version": ys_ver}, qor
        except (OSError, ValueError, KeyError, TypeError) as exc:
            return {"status": "fail", "mode": reason, "returncode": 1, "analysis_error": str(exc), "version": ys_ver}, None

    if sv2v_mode == "on":
        if not sv2v_exe:
            return {"status": "fail", "mode": "sv2v-missing", "returncode": 1,
                    "analysis_error": "--sv2v=on requested but sv2v is not installed", "version": ys_ver}, None
        return run_with_sv2v("sv2v")

    rcmd, hcmd = build_native_cmds()
    try:
        rc, qor = _execute_yosys(yosys, rcmd, hcmd, top, params, out, project,
                                 out / "yosys.log", apply_params=True)
        if rc == 0 and qor is not None:
            return {"status": "pass", "mode": "native", "returncode": 0, "version": ys_ver}, qor
    except (OSError, ValueError, KeyError, TypeError) as exc:
        rc = 1
        err_msg = str(exc)
    else:
        err_msg = ""

    if sv2v_mode == "auto" and sv2v_exe:
        (out / "yosys_native_fail.log").write_text((out / "yosys.log").read_text(errors="replace")
                                                  if (out / "yosys.log").exists() else err_msg)
        return run_with_sv2v("sv2v-fallback")

    res = {"status": "fail", "mode": "native", "returncode": rc, "version": ys_ver}
    yosys_error = first_yosys_error(out / "yosys.log")
    if yosys_error:
        res["error"] = yosys_error
    if err_msg:
        res["analysis_error"] = err_msg
    elif sv2v_mode == "auto" and not sv2v_exe and is_parse_error(yosys_error):
        res["hint"] = "native Yosys SV parse failed; installing sv2v enables automatic SV lowering fallback"
    return res, None


def first_yosys_error(log_path: Path) -> str | None:
    """Return the first Yosys ERROR line (plus its continuation) so failures explain themselves."""
    if not log_path.is_file():
        return None
    lines = log_path.read_text(errors="replace").splitlines()
    for idx, line in enumerate(lines):
        if "ERROR:" in line:
            extra = [ln.strip() for ln in lines[idx + 1: idx + 3] if ln.strip() and not ln.startswith("End of script")]
            return " ".join([line.strip(), *extra])[:400]
    return None


def is_parse_error(message: str | None) -> bool:
    text = (message or "").lower()
    return any(key in text for key in ("syntax error", "parse", "unexpected", "not supported"))


def config_diff_notes(prev_summary: dict, cur_config: dict) -> list[str]:
    old_cfg = (prev_summary.get("build") or {}).get("config") or {}
    notes: list[str] = []
    for key in ("top", "parameters", "defines"):
        if key in old_cfg and old_cfg.get(key) != cur_config.get(key):
            notes.append(f"{key}: {old_cfg.get(key)} -> {cur_config.get(key)}")
    return notes


def format_delta(row: dict) -> str:
    delta = row.get("delta")
    if delta is None:
        return "n/a"
    if delta == 0:
        return "0 (0.0%)"
    sign = "+" if delta > 0 else ""
    pct = row.get("delta_pct")
    pct_str = "new" if pct is None else f"{sign}{pct:.1f}%"
    return f"{sign}{delta} ({pct_str})"


def print_human_report(summary: dict, out_dir: Path) -> None:
    top = summary["top"]
    gate_status = summary["gate"]["status"].upper()
    l_info = summary["tools"]["lint"]
    y_info = summary["tools"]["yosys"]
    s_info = summary["tools"].get("verible") or {}
    lint_label = l_info.get("tool", "lint").capitalize()
    style_str = (
        f" | Style (Verible): {s_info.get('findings', 0)} notes"
        if s_info.get("status") == "advisory"
        else ""
    )
    print(f"=== Pre-Synthesis RTL Check: {top} [{gate_status}] ===")
    print(
        f"Lint ({lint_label}): {l_info['status'].upper()}"
        + (f" ({l_info.get('warnings', 0)} warnings)" if l_info.get("warnings") is not None else "")
        + f" | Elab/Synth (Yosys/{y_info.get('mode', 'native')}): {y_info['status'].upper()}"
        + style_str
    )

    qor = summary.get("qor")
    trend = summary.get("baseline")
    if qor is None:
        for reason in summary["gate"]["failures"]:
            print(f"  FAIL: {reason}")
        if y_info.get("error"):
            print(f"  YOSYS ({y_info.get('version') or 'unknown version'}): {y_info['error']}")
        if y_info.get("hint"):
            print(f"  HINT: {y_info['hint']}")
        print(f"Logs: {out_dir}")
        return

    if qor.get("latch_bits", 0) > 0:
        latch_paths = ", ".join(item["path"] for item in qor.get("latch_inventory", [])[:5])
        print(f"  WARNING: {qor['latch_bits']} latch bits inferred! ({latch_paths})")
    if qor.get("comb_loops", 0) > 0:
        print(f"  WARNING: {qor['comb_loops']} combinational loop(s) (SCC) detected!")
    if qor.get("stuck_output_bits", 0) > 0 or qor.get("undriven_output_bits", 0) > 0:
        ports = ", ".join(f"{item['port']}={item['sample']}" for item in qor.get("stuck_outputs", [])[:5])
        print(
            f"  CRITICAL (Dead-Logic Guard): {qor.get('stuck_output_bits', 0)} constant-tied / "
            f"{qor.get('undriven_output_bits', 0)} undriven top output bit(s)! ({ports})"
        )
    if qor.get("pre_opt_ff_bits", 0) >= 8 and qor.get("pruned_ff_pct", 0.0) >= 50.0:
        print(
            f"  CRITICAL (Pruned-FF Guard): {qor['pruned_ff_bits']}/{qor['pre_opt_ff_bits']} FF bits "
            f"({qor['pruned_ff_pct']}%) folded away during opt — check enable/valid polarity!"
        )

    labels = [
        ("ff_bits", "FF bits"),
        ("latch_bits", "Latch bits"),
        ("comb_loops", "Comb loops (SCC)"),
        ("stuck_output_bits", "Stuck output bits"),
        ("max_fanout", "Max non-clk fanout"),
        ("memory_bits", "Memory bits"),
        ("memory_objects", "Memory banks/objs"),
        ("gate_depth", "Gate depth (techmap)"),
        ("word_depth", "Word depth (RTL ops)"),
        ("mux_data_bits", "Mux data bits"),
        ("multiply", "Op: multiply"),
        ("add_sub", "Op: add/sub"),
        ("compare", "Op: compare"),
        ("shift", "Op: shift"),
        ("divide_mod", "Op: div/mod"),
    ]

    if trend and trend.get("metrics"):
        by_name = {r["metric"]: r for r in trend["metrics"]}
        if trend.get("config_notes"):
            print("  Note (config changed vs baseline): " + "; ".join(trend["config_notes"]))
        print(f"\n  {'Metric':<22} {'Prev':>10} {'Current':>10} {'Delta':>18}")
        print(f"  {'-'*22} {'-'*10} {'-'*10} {'-'*18}")
        for key, title in labels:
            r = by_name.get(key)
            if not r:
                continue
            cur = r["current"]
            prev = r["baseline"]
            if key in {"multiply", "compare", "shift", "divide_mod", "memory_bits",
                       "memory_objects", "latch_bits", "comb_loops", "stuck_output_bits"} and not cur and not prev:
                continue
            prev_s = "-" if prev is None else str(prev)
            cur_s = "-" if cur is None else str(cur)
            print(f"  {title:<22} {prev_s:>10} {cur_s:>10} {format_delta(r):>18}")
    else:
        ops = qor.get("operator_groups", {})
        print(f"\n  {'Metric':<22} {'Value':>10}")
        print(f"  {'-'*22} {'-'*10}")
        for key, title in labels:
            val = qor.get(key, ops.get(key, 0))
            if key in {"multiply", "compare", "shift", "divide_mod", "memory_bits",
                       "memory_objects", "latch_bits", "comb_loops", "stuck_output_bits"} and not val:
                continue
            print(f"  {title:<22} {str(val):>10}")

    if qor.get("fanout_top") and qor.get("max_fanout", 0) >= 32:
        top_nets = ", ".join(f"{item['net']}({item['fanout']})" for item in qor["fanout_top"][:3])
        print(f"\nTop Fanout Nets: {top_nets}")

    if summary.get("budget"):
        b_res = summary["budget"]
        print(f"\nQoR Budget Check: {b_res['status'].upper()} ({len(b_res['checks'])} rules)")
        for fail in b_res.get("failures", []):
            print(f"  BUDGET FAIL: {fail['name']} (actual={fail.get('actual')}, {fail.get('rule')}={fail.get('limit')} {fail.get('reason', '')})")

    if summary["warnings"]:
        print(f"\nTop Warnings ({len(summary['warnings'])}):")
        for w in summary["warnings"][:6]:
            print(f"  - {w}")

    if s_info.get("sample"):
        print(f"\nStyle Advisories (Verible, {s_info['findings']} total):")
        for sline in s_info["sample"][:4]:
            print(f"  - {sline}")

    print(f"\nArtifacts: {out_dir / 'qor_summary.json'} | History: {out_dir / 'qor_history.jsonl'}")


def append_history(out_dir: Path, summary: dict) -> None:
    qor = summary.get("qor")
    if not qor:
        return
    ops = qor.get("operator_groups") or {}
    entry = {
        "timestamp": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "top": summary["top"],
        "source_key": summary["build"]["source_key"][:12],
        "parameters": summary["build"]["config"]["parameters"],
        "lint_warnings": summary["tools"]["lint"].get("warnings", 0),
        "ff_bits": qor["ff_bits"],
        "latch_bits": qor["latch_bits"],
        "comb_loops": qor.get("comb_loops", 0),
        "stuck_output_bits": qor.get("stuck_output_bits", 0),
        "max_fanout": qor.get("max_fanout", 0),
        "memory_bits": qor["memory_bits"],
        "gate_depth": qor["gate_depth"],
        "word_depth": qor["word_depth"],
        "mux_data_bits": qor["mux_data_bits"],
        "multiply": ops.get("multiply", 0),
        "add_sub": ops.get("add_sub", 0),
    }
    with (out_dir / "qor_history.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")


def main() -> int:
    args = parse_args()
    if not IDENTIFIER.fullmatch(args.top):
        raise SystemExit(f"Invalid --top {args.top!r}")

    filelist_path = Path(args.filelist).expanduser().resolve()
    project = filelist_path.parent
    out = Path(args.out_dir).expanduser().resolve()
    out.mkdir(parents=True, exist_ok=True)

    summary_file = out / "qor_summary.json"
    prev_summary: dict | None = None
    if args.baseline:
        bpath = Path(args.baseline).expanduser().resolve()
        if not bpath.is_file():
            raise SystemExit(f"Missing baseline file: {bpath}")
        prev_summary = json.loads(bpath.read_text())
    elif not args.no_auto_baseline and summary_file.is_file():
        try:
            prev_summary = json.loads(summary_file.read_text())
        except (OSError, json.JSONDecodeError):
            prev_summary = None

    inputs = parse_filelist(filelist_path)
    params = parse_params(args.param)
    budget = load_yaml(Path(args.budget).expanduser().resolve()) if args.budget else None

    lint_res, sources = run_lint(inputs, args.top, params, out, project, args.skip_lint, args.strict_lint)
    verible_res = run_verible_style(sources, out, project, args.style)
    yosys_res, qor = run_yosys(inputs, sources, args.top, params, out, project, args.sv2v)

    source_rows = [{"path": relative(p, project), "digest": digest(p)} for p in sources if p.is_file()]
    cur_config = {
        "top": args.top,
        "parameters": params,
        "defines": inputs.defines,
        "sources": [relative(p, project) for p in inputs.files],
        "include_dirs": [relative(p, project) for p in inputs.include_dirs],
        "library_dirs": [relative(p, project) for p in inputs.library_dirs],
    }

    tools_block = {
        "lint": lint_res,
        "verilator": lint_res if lint_res.get("tool") == "verilator" else {"status": "missing", "warnings": 0},
        "verible": verible_res,
        "yosys": yosys_res,
    }

    summary: dict = {
        "format": FORMAT_VERSION,
        "top": args.top,
        "tools": tools_block,
        "qor": qor,
        "build": {
            "source_key": digest(source_rows),
            "config": cur_config,
            "source_files": source_rows,
        },
        "budget": check_budget(budget, qor) if (budget is not None and qor is not None) else None,
        "baseline": None,
    }

    if prev_summary and (isinstance(prev_summary.get("qor"), dict) or isinstance(prev_summary.get("hardware"), dict)) and qor is not None:
        trend = compare_baseline(prev_summary, qor, budget)
        trend["config_notes"] = config_diff_notes(prev_summary, cur_config)
        summary["baseline"] = trend

    warnings: list[str] = []
    for log_name in ("verilator.log", "iverilog_lint.log", "yosys.log"):
        lp = out / log_name
        if lp.is_file():
            for line in lp.read_text(errors="replace").splitlines():
                if "warning" in line.lower():
                    warnings.append(f"{log_name}: {line.strip()}")
    summary["warnings"] = warnings[:80]

    failures: list[str] = []
    if lint_res["status"] == "fail":
        failures.append(f"{lint_res.get('tool', 'lint')} lint/elaboration failed")
    if yosys_res["status"] == "fail":
        failures.append("yosys elaboration/synthesis failed")
    if yosys_res["status"] == "missing":
        failures.append("yosys not installed in PATH")
    if summary.get("budget") and summary["budget"]["status"] == "fail":
        failures.append("QoR budget violated")
    if summary.get("baseline") and summary["baseline"]["status"] == "fail":
        failures.append("baseline regression limit exceeded")

    summary["gate"] = {"status": "fail" if failures else "pass", "failures": failures}
    summary_file.write_text(json.dumps(summary, indent=2) + "\n")
    append_history(out, summary)

    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        print_human_report(summary, out)

    if yosys_res["status"] == "missing":
        return 2
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
