#!/usr/bin/env python3
"""Preflight diagnostics for a configured commercial EDA tool."""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
from eda_env import (  # noqa: E402
    ConfigError,
    check_transport_executable,
    environment,
    license_shell_expr,
    load,
    resolve_config_path,
    shell_bundle,
    target,
    tool,
    wrap_remote_bash,
)


def run_remote(argv: list[str], remote_cmd: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [*argv, wrap_remote_bash(remote_cmd)],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )


def quick_rules(cfg: dict, profile: dict, bundle: dict[str, str]) -> list[tuple[str, str]]:
    exact, prepend = environment(profile, cfg)
    q = shlex.quote
    rules = [
        ("run_root", f"mkdir -p {q(bundle['REMOTE_RUN_ROOT'])} && test -w {q(bundle['REMOTE_RUN_ROOT'])}"),
        ("binary", 'test -x "$EDA_TOOL_BIN"'),
        ("tool_root", 'test -d "$EDA_TOOL_ROOT"'),
        ("home", f'test "$HOME" = {q(bundle["REMOTE_HOME"])}'),
    ]
    for name, value in exact.items():
        rules.append((f"env:{name}", f'test "${{{name}:-}}" = {q(value)}'))
    for name, paths in prepend.items():
        for p in paths:
            rules.append((f"path:{p}", f"test -e {q(p)}"))
    for child in cfg.get("child_executables", []):
        rules.append((f"child:{child}", f"test -x {q(child)}"))
    lic = license_shell_expr(profile)
    if lic is not None:
        rules.append(lic)
    return rules


def run_quick(argv: list[str], base: str, rules: list[tuple[str, str]]) -> tuple[list[dict], bool]:
    cmds = [base]
    for name, expr in rules:
        cmds.append(f"if {expr}; then echo 'EDA_PRE|{name}|pass'; else echo 'EDA_PRE|{name}|fail'; fi")
    proc = run_remote(argv, "; ".join(cmds))
    rows = []
    for line in proc.stdout.splitlines():
        parts = line.split("|", 2)
        if len(parts) == 3 and parts[0] == "EDA_PRE":
            rows.append({"check": parts[1], "status": parts[2]})
    if proc.returncode != 0:
        rows.append({
            "check": "connection",
            "status": "fail",
            "returncode": proc.returncode,
            "detail": proc.stdout[-1000:],
        })
    ok = proc.returncode == 0 and bool(rows) and all(r["status"] == "pass" for r in rows)
    return rows, ok


def run_levels(argv: list[str], base: str, cfg: dict, level: str, ready: bool) -> tuple[list[dict], str]:
    extra: list[dict] = []
    status = "pass" if ready else "fail"
    if status == "pass" and level in {"probe", "smoke"}:
        proc = run_remote(argv, f"{base}; {cfg['probe']}")
        passed = proc.returncode == 0
        extra.append({
            "check": "probe",
            "status": "pass" if passed else "fail",
            "returncode": proc.returncode,
            "output_tail": proc.stdout[-1000:],
        })
        status = "pass" if passed else "fail"
    if status == "pass" and level == "smoke":
        if not cfg.get("smoke"):
            extra.append({"check": "smoke", "status": "skip", "detail": "no smoke test configured"})
        else:
            proc = run_remote(argv, f"{base}; {cfg['smoke']}")
            passed = proc.returncode == 0
            extra.append({
                "check": "smoke",
                "status": "pass" if passed else "fail",
                "returncode": proc.returncode,
                "output_tail": proc.stdout[-1000:],
            })
            status = "pass" if passed else "fail"
    return extra, status


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify commercial EDA tool readiness.")
    parser.add_argument("tool")
    parser.add_argument("--config", type=Path, help="explicit tools.yaml path")
    parser.add_argument("--target", help="named target from tools.yaml")
    parser.add_argument("--level", choices=("quick", "probe", "smoke"), default="quick")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--brief", action="store_true")
    args = parser.parse_args()

    try:
        cfg_path = resolve_config_path(args.config)
        data = load(cfg_path)
        tname, _, argv = target(data, args.target)
        check_transport_executable(argv)
        tcfg, profile = tool(data, args.tool)
        bundle = shell_bundle(data, args.tool, tname)
    except ConfigError as exc:
        print(f"EDA PREFLIGHT CONFIG ERROR: {exc}", file=sys.stderr)
        return 2

    checks, ready = run_quick(argv, bundle["BASE_REMOTE"], quick_rules(tcfg, profile, bundle))
    extra, status = run_levels(argv, bundle["BASE_REMOTE"], tcfg, args.level, ready)
    report = {
        "tool": args.tool,
        "target": tname,
        "level": args.level,
        "status": status,
        "checks": checks + extra,
    }
    if args.json:
        print(json.dumps(report, indent=2))
    elif args.brief and status == "pass":
        print(f"EDA preflight {args.tool}@{tname}: PASS ({args.level})")
    else:
        print(f"EDA preflight: {args.tool}@{tname} [{args.level}] -> {status.upper()}")
        for row in report["checks"]:
            print(f"  {row['status'].upper():4} {row['check']}")
            if row.get("status") == "fail" and row.get("detail"):
                print(f"       {row['detail'].strip()}")
    return 0 if status == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
