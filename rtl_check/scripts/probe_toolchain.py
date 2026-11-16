#!/usr/bin/env python3
"""Auto-probe locally installed and remotely configured ASIC/EDA toolchains."""

from __future__ import annotations

import argparse
import importlib.util
import json
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

sys.dont_write_bytecode = True

SCRIPT_DIR = Path(__file__).resolve().parent
SUITE_ROOT = SCRIPT_DIR.parent.parent
EDA_RUNNER_SCRIPTS = SUITE_ROOT / "eda_runner" / "scripts"


def _probe_version(exe: str, args: list[str]) -> str | None:
    try:
        proc = subprocess.run(
            [exe, *args],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    for line in proc.stdout.splitlines():
        clean = line.strip()
        if clean:
            return clean
    return None


def _find_cli(name: str, ver_args: list[str] | None = None) -> dict:
    path = shutil.which(name)
    if not path:
        return {"available": False, "path": None, "version": None}
    version = _probe_version(path, ver_args) if ver_args is not None else None
    return {"available": True, "path": path, "version": version}


def _find_py_pkg(name: str) -> dict:
    spec = importlib.util.find_spec(name)
    if spec is None:
        return {"available": False, "version": None}
    version = None
    try:
        mod = __import__(name)
        version = getattr(mod, "__version__", None)
    except Exception:
        version = None
    return {"available": True, "version": str(version) if version else None}


def _probe_remote_config() -> dict:
    if not EDA_RUNNER_SCRIPTS.is_dir():
        return {"configured": False, "config_path": None, "default_target": None, "targets": [], "tools": []}
    sys.path.insert(0, str(EDA_RUNNER_SCRIPTS))
    try:
        import eda_env  # type: ignore

        cfg_path = eda_env.resolve_config_path()
        data = eda_env.load(cfg_path)
        try:
            default_target = eda_env.target(data)[0]
            tools, _ = eda_env.effective_sections(data, default_target)
        except eda_env.ConfigError:
            default_target, tools = None, data.get("tools", {})
        return {
            "configured": True,
            "config_path": str(cfg_path),
            "default_target": default_target,
            "targets": sorted(map(str, data.get("targets", {}))),
            "tools": sorted(map(str, tools)),
        }
    except Exception as exc:
        return {
            "configured": False,
            "config_path": None,
            "default_target": None,
            "targets": [],
            "tools": [],
            "note": str(exc),
        }


def _probe_onboarding() -> dict:
    """First-run state from eda_env: configured | local-only | pending | error."""
    if not EDA_RUNNER_SCRIPTS.is_dir():
        return {"status": "unknown"}
    sys.path.insert(0, str(EDA_RUNNER_SCRIPTS))
    try:
        import eda_env  # type: ignore

        return eda_env.onboarding_status()
    except Exception as exc:  # never let the probe crash on config problems
        return {"status": "error", "error": str(exc)}


def recommend_strategy(local: dict, py_pkgs: dict, remote: dict) -> dict[str, str]:
    has = lambda k: bool(local.get(k, {}).get("available"))
    has_py = lambda k: bool(py_pkgs.get(k, {}).get("available"))
    remote_tools = set(remote.get("tools") or [])

    if has("verilator") and has("verible-verilog-lint"):
        lint = "verilator (fatal hardware rules) + verible-verilog-lint (style)"
    elif has("verilator"):
        lint = "verilator (fatal hardware rules)"
    elif has("iverilog"):
        lint = "iverilog -g2012 -tnull (fallback syntax lint)"
    elif remote_tools & {"syn-spyglass", "syn-vcs", "cad-irun", "cad-ncvlog"}:
        lint = "commercial lint/compile via eda_runner"
    else:
        lint = "none available locally"

    cocotb_tag = " + cocotb (Python fixed-point golden)" if has_py("cocotb") else ""
    if has("verilator"):
        sim = f"verilator --binary{cocotb_tag}"
    elif has("iverilog") and has("vvp"):
        sim = f"iverilog + vvp{cocotb_tag}"
    elif remote_tools & {"syn-vcs", "cad-irun", "mentor-vsim"}:
        sim = "commercial simulator via eda_runner"
    else:
        sim = "none available locally"

    if has("yosys") and has("sv2v"):
        pre_synth = "pre_synth_check.py (yosys + sv2v fallback for complex SV)"
    elif has("yosys"):
        pre_synth = "pre_synth_check.py (yosys native -sv)"
    else:
        pre_synth = "unavailable (yosys not installed; lint-only fallback)"

    solvers = [s for s in ("z3", "boolector", "yices-smt2") if has(s)]
    if has("sby") and solvers:
        formal = f"sby + {solvers[0]} (single-file BMC/prove/cover + VCD counterexamples)"
    elif has("yosys") and has("yosys-smtbmc") and solvers:
        formal = f"yosys + yosys-smtbmc + {solvers[0]} (direct fallback)"
    else:
        formal = "unavailable locally (use SVA assertions in simulation)"

    if has("surfer"):
        wave = "surfer"
    elif has("gtkwave"):
        wave = "gtkwave"
    else:
        wave = "headless (text/assertion logs first)"

    return {
        "lint": lint,
        "simulation": sim,
        "pre_synth_check": pre_synth,
        "formal": formal,
        "waveform": wave,
        "commercial_eda": ", ".join(sorted(remote_tools)) if remote_tools else "not configured",
    }


def collect_inventory() -> dict:
    local = {
        "verilator": _find_cli("verilator", ["--version"]),
        "iverilog": _find_cli("iverilog", ["-V"]),
        "vvp": _find_cli("vvp", ["-V"]),
        "yosys": _find_cli("yosys", ["-V"]),
        "yosys-smtbmc": _find_cli("yosys-smtbmc", ["--help"]),
        "sby": _find_cli("sby", ["--version"]),
        "z3": _find_cli("z3", ["--version"]),
        "boolector": _find_cli("boolector", ["--version"]),
        "yices-smt2": _find_cli("yices-smt2", ["--version"]),
        "sv2v": _find_cli("sv2v", ["--version"]),
        "verible-verilog-lint": _find_cli("verible-verilog-lint", ["--version"]),
        "surfer": _find_cli("surfer", ["--version"]),
        "gtkwave": _find_cli("gtkwave", ["--version"]),
    }
    py_pkgs = {
        "cocotb": _find_py_pkg("cocotb"),
        "yaml": _find_py_pkg("yaml"),
    }
    remote = _probe_remote_config()
    return {
        "onboarding": _probe_onboarding(),
        "local_tools": local,
        "python_packages": py_pkgs,
        "commercial_eda": remote,
        "recommended_strategy": recommend_strategy(local, py_pkgs, remote),
    }


def print_onboarding(state: dict) -> None:
    status = state.get("status")
    setup = shlex.quote(str(EDA_RUNNER_SCRIPTS / "eda_setup.py"))
    if status == "pending":
        print("!! FIRST RUN: no EDA server is saved yet and the user has not declined one.")
        print("!! AGENT ACTION REQUIRED before starting the task: ask the user for the EDA server")
        print("!!   - SSH address (user@host or ~/.ssh/config alias), port if not 22, jump host if any")
        print("!!   - a short name for the server; optional license server (port@host)")
        print("!! Then run:")
        print(f"!!   python3 {setup} init --target <name> --host <user@host> [--port N] [--jump user@bastion] --default")
        print("!! If the user has no server or wants local tools only, record it so later sessions do not ask:")
        print(f"!!   python3 {setup} skip --reason \"<why>\"")
        print("!! Never ask for or store passwords; key-based SSH only.")
        print()
    elif status == "error":
        print(f"!! EDA config error ({state.get('config_path', '?')}): {state.get('error')}")
        print()


def print_human(inv: dict) -> None:
    print("=== Digital IC Suite: Toolchain Inventory ===")
    print_onboarding(inv.get("onboarding") or {})
    print(f"Onboarding     : {(inv.get('onboarding') or {}).get('status', 'unknown')}")
    avail = [name for name, info in inv["local_tools"].items() if info["available"]]
    missing = [name for name, info in inv["local_tools"].items() if not info["available"]]
    py_avail = [
        f"{name}" + (f" ({info['version']})" if info["version"] else "")
        for name, info in inv["python_packages"].items()
        if info["available"]
    ]
    print(f"Local binaries : {', '.join(avail) if avail else '(none)'}")
    print(f"Not installed  : {', '.join(missing) if missing else '(none)'}")
    print(f"Python packages: {', '.join(py_avail) if py_avail else '(none)'}")
    remote = inv["commercial_eda"]
    if remote["configured"]:
        print(
            f"Commercial EDA : {remote['config_path']} "
            f"(default={remote['default_target']}, targets={', '.join(remote['targets'])}, "
            f"tools={', '.join(remote['tools'])})"
        )
    else:
        print("Commercial EDA : not configured "
              f"(onboard a server: python3 {shlex.quote(str(EDA_RUNNER_SCRIPTS / 'eda_setup.py'))} init --host <user@host>)")
    print("\nRecommended Execution Paths:")
    for k, v in inv["recommended_strategy"].items():
        print(f"  - {k:<17}: {v}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Probe available local and commercial EDA toolchains.")
    parser.add_argument("--json", action="store_true", help="emit JSON inventory")
    args = parser.parse_args()
    inv = collect_inventory()
    if args.json:
        print(json.dumps(inv, indent=2))
    else:
        print_human(inv)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
