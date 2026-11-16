#!/usr/bin/env python3
"""Load external commercial-EDA configuration and emit safe shell bindings.

Configuration sources (merged):
  1. Main file: --config, $EDA_TOOLS_CONFIG, ./.eda_tools.yaml, or
     ~/.config/digital-ic-suite/tools.yaml (first one that exists wins).
  2. Drop-in directory next to the main file (tools.yaml -> tools.d/*.yaml).
     Each drop-in holds one onboarded server and is written by eda_setup.py,
     so hand-written main files are never rewritten.

A target may carry its own `profiles` and `tools` mappings. They override the
global sections of the same name for that target only, which lets two servers
use different install paths and license servers.
"""

from __future__ import annotations

import argparse
import base64
import os
import re
import shlex
import shutil
import sys
from pathlib import Path

ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
TARGET_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
DEFAULT_CONFIG = Path.home() / ".config" / "digital-ic-suite" / "tools.yaml"
TEMPLATE_HINT = (
    "Onboard a server with: python3 eda_runner/scripts/eda_setup.py init --host <user@host> "
    "(or copy templates/eda_tools.example.yaml to ~/.config/digital-ic-suite/tools.yaml)"
)


class ConfigError(ValueError):
    pass


def dropin_dir(main: Path) -> Path:
    """tools.yaml -> tools.d ; .eda_tools.yaml -> .eda_tools.d"""
    return main.with_suffix(".d")


def config_exists(main: Path) -> bool:
    drop = dropin_dir(main)
    return main.is_file() or (drop.is_dir() and any(drop.glob("*.yaml")))


def resolve_config_path(explicit: Path | None = None) -> Path:
    candidates: list[Path] = []
    if explicit is not None:
        candidates.append(explicit.expanduser())
    elif env_cfg := os.environ.get("EDA_TOOLS_CONFIG"):
        candidates.append(Path(env_cfg).expanduser())
    else:
        candidates.extend([Path.cwd() / ".eda_tools.yaml", DEFAULT_CONFIG])
    for candidate in candidates:
        if config_exists(candidate):
            return candidate.resolve()
    searched = ", ".join(str(p) for p in candidates)
    raise ConfigError(f"no EDA tools config found (checked: {searched}). {TEMPLATE_HINT}")


def require_yaml():
    try:
        import yaml
        return yaml
    except ImportError as exc:
        raise ConfigError(
            "PyYAML is required for commercial EDA configuration; install with: pip install pyyaml"
        ) from exc


def onboarding_marker(main: Path | None = None) -> Path:
    """File recording that the user chose local-only use (so first-run onboarding stops asking)."""
    if main is None:
        env_cfg = os.environ.get("EDA_TOOLS_CONFIG")
        main = Path(env_cfg).expanduser() if env_cfg else DEFAULT_CONFIG
    return main.parent / "onboarding.yaml"


def onboarding_status(main: Path | None = None) -> dict:
    """First-run state: configured | local-only | pending | error.

    pending means no EDA server is saved and the user never declined: the agent
    must ask for the server address before starting work.
    """
    try:
        path = resolve_config_path(main)
    except ConfigError:
        path = None
    if path is not None:
        try:
            data = load(path, allow_empty=True)
        except ConfigError as exc:
            return {"status": "error", "config_path": str(path), "error": str(exc)}
        if data["targets"]:
            return {"status": "configured", "config_path": str(path), "targets": sorted(data["targets"])}
    marker = onboarding_marker(main)
    if marker.is_file():
        return {"status": "local-only", "marker": str(marker)}
    return {"status": "pending", "marker": str(marker)}


def wrap_remote_bash(script: str) -> str:
    """Wrap a bash script so remote tcsh/csh/zsh/dash login shells execute cleanly while preserving stdin."""
    b64 = base64.b64encode(script.encode("utf-8")).decode("ascii")
    return f"bash -c 'eval \"$(printf %s {b64} | (base64 -d 2>/dev/null || base64 -D))\"'"


def _read_yaml(path: Path):
    yaml = require_yaml()
    try:
        return yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"cannot load {path}: {exc}") from exc


def read_dropin(path: Path) -> tuple[str, dict]:
    data = _read_yaml(path)
    if not isinstance(data, dict) or not isinstance(data.get("target"), dict):
        raise ConfigError(f"{path} must contain 'name' and a 'target' mapping")
    name = str(data.get("name") or path.stem)
    if not TARGET_NAME.fullmatch(name):
        raise ConfigError(f"{path}: invalid target name {name!r}")
    return name, data["target"]


def load(path: Path, *, allow_empty: bool = False) -> dict:
    """Load the main file plus drop-ins and validate every target and tool."""
    data = _read_yaml(path) if path.is_file() else {}
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must contain a YAML mapping")
    data = dict(data)
    for section in ("targets", "profiles", "tools"):
        value = data.get(section)
        if value is None:
            data[section] = {}
        elif not isinstance(value, dict):
            raise ConfigError(f"{path} section '{section}' must be a mapping")
        else:
            data[section] = dict(value)

    sources = {str(name): str(path) for name in data["targets"]}
    drop = dropin_dir(path)
    for dropin in sorted(drop.glob("*.yaml")) if drop.is_dir() else []:
        name, tcfg = read_dropin(dropin)
        if name in data["targets"]:
            raise ConfigError(f"target {name!r} in {dropin} duplicates the one in {sources[name]}")
        data["targets"][name] = tcfg
        sources[name] = str(dropin)
    data["_sources"] = sources

    if not data["targets"]:
        if allow_empty:
            return data
        raise ConfigError(f"{path}: no targets configured. {TEMPLATE_HINT}")
    for tname in data["targets"]:
        target(data, str(tname))
        tools, _ = effective_sections(data, str(tname))
        for tool_name in tools:
            tool(data, str(tool_name), str(tname))
    return data


def target(data: dict, name: str | None = None) -> tuple[str, dict, list[str]]:
    targets = data["targets"]
    chosen = name or os.environ.get("EDA_TARGET") or data.get("default_target")
    if not chosen and len(targets) == 1:
        chosen = next(iter(targets))
    if not chosen or chosen not in targets:
        available = ", ".join(sorted(map(str, targets)))
        raise ConfigError(f"unknown target {chosen!r} (available: {available})")
    cfg = targets[chosen]
    if not isinstance(cfg, dict):
        raise ConfigError(f"targets.{chosen} must be a mapping")
    for field in ("transport", "remote_home", "run_root"):
        if cfg.get(field) in (None, ""):
            raise ConfigError(f"missing targets.{chosen}.{field}")
    transport = str(cfg["transport"])
    if transport == "ssh":
        host = str(cfg.get("host") or "").strip()
        if not host:
            raise ConfigError(f"targets.{chosen}.host is required for ssh transport")
        ssh_args = cfg.get("ssh_args", ["-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10"])
        if not isinstance(ssh_args, list):
            raise ConfigError(f"targets.{chosen}.ssh_args must be a list")
        argv = ["ssh", *[str(a) for a in ssh_args], host]
    elif transport == "command":
        raw_cmd = cfg.get("command")
        if not isinstance(raw_cmd, list) or not raw_cmd:
            raise ConfigError(f"targets.{chosen}.command must be a non-empty list")
        first = str(raw_cmd[0])
        if "/" in first or first.startswith("~"):
            first = str(Path(first).expanduser())
        argv = [first, *[str(a) for a in raw_cmd[1:]]]
    else:
        raise ConfigError(f"targets.{chosen}.transport must be 'ssh' or 'command'")
    return str(chosen), cfg, argv


def effective_sections(data: dict, target_name: str) -> tuple[dict, dict]:
    """Return (tools, profiles) for a target: global entries overlaid by target-local ones."""
    tcfg = data["targets"][target_name]
    merged = []
    for section in ("tools", "profiles"):
        local = tcfg.get(section) or {}
        if not isinstance(local, dict):
            raise ConfigError(f"targets.{target_name}.{section} must be a mapping")
        merged.append({**data[section], **local})
    return merged[0], merged[1]


def check_transport_executable(argv: list[str]) -> None:
    exe = argv[0]
    if "/" in exe:
        if not (Path(exe).is_file() and os.access(exe, os.X_OK)):
            raise ConfigError(f"transport helper is missing or not executable: {exe}")
    elif shutil.which(exe) is None:
        raise ConfigError(f"transport executable not found in PATH: {exe}")


def tool(data: dict, name: str, target_name: str | None = None) -> tuple[dict, dict]:
    tname = target(data, target_name)[0]
    tools, profiles = effective_sections(data, tname)
    cfg = tools.get(name)
    if not isinstance(cfg, dict):
        available = ", ".join(sorted(map(str, tools)))
        raise ConfigError(f"unknown EDA tool {name!r} on target {tname!r} (available: {available})")
    for field in ("profile", "root", "bin", "probe"):
        if cfg.get(field) in (None, ""):
            raise ConfigError(f"missing tools.{name}.{field} (target {tname!r})")
    profile = profiles.get(str(cfg["profile"]))
    if not isinstance(profile, dict):
        raise ConfigError(f"tools.{name}.profile {cfg['profile']!r} is undefined (target {tname!r})")
    environment(profile, cfg)
    children = cfg.get("child_executables", [])
    if not isinstance(children, list) or any(not str(item or "").strip() for item in children):
        raise ConfigError(f"tools.{name}.child_executables must be a path list")
    return cfg, profile


def environment(profile: dict, cfg: dict) -> tuple[dict[str, str], dict[str, list[str]]]:
    exact: dict[str, str] = {}
    prepend: dict[str, list[str]] = {}
    for block in (profile, cfg):
        values = block.get("expected_env") or {}
        paths = block.get("path_prepend") or {}
        if not isinstance(values, dict) or not isinstance(paths, dict):
            raise ConfigError("expected_env and path_prepend must be mappings")
        for name, value in values.items():
            if not ENV_NAME.fullmatch(str(name)) or value in (None, ""):
                raise ConfigError(f"invalid environment entry {name!r}")
            exact[str(name)] = str(value)
        for name, entries in paths.items():
            if not ENV_NAME.fullmatch(str(name)) or not isinstance(entries, list) or not entries:
                raise ConfigError(f"invalid path_prepend entry {name!r}")
            prepend[str(name)] = [str(item) for item in entries]
    return exact, prepend


def license_shell_expr(profile: dict) -> tuple[str, str] | None:
    """Return (label, bash_boolean_expr) for license reachability, or None."""
    rule = profile.get("license")
    if rule is None:
        return None
    if not isinstance(rule, dict):
        raise ConfigError("profile.license must be a mapping")
    exact, _ = environment(profile, {})
    var = str(rule.get("variable") or "")
    value = exact.get(var)
    if not value:
        raise ConfigError(f"license variable {var!r} missing from expected_env")
    kind = rule.get("kind")
    if kind == "file_env":
        checks = [f"test -r {shlex.quote(part)}" for part in value.split(":") if part]
    elif kind == "flexlm_env":
        checks = []
        for part in (p.strip() for p in value.split(":") if p.strip()):
            m = re.fullmatch(r"(\d*)@(.+)", part)
            if m:
                port = int(m.group(1)) if m.group(1) else 27000
                host = m.group(2)
                tcp_cmd = f"</dev/tcp/{host}/{port}"
                checks.append(
                    f"(if command -v timeout >/dev/null 2>&1; then timeout 4 bash -c {shlex.quote(tcp_cmd)}; "
                    f"elif command -v gtimeout >/dev/null 2>&1; then gtimeout 4 bash -c {shlex.quote(tcp_cmd)}; "
                    f"else python3 -c 'import socket,sys; s=socket.create_connection((sys.argv[1],int(sys.argv[2])),4); s.close()' {shlex.quote(host)} {port}; fi) >/dev/null 2>&1"
                )
            else:
                checks.append(f"test -r {shlex.quote(part)}")
    else:
        raise ConfigError("license kind must be 'file_env' or 'flexlm_env'")
    if not checks:
        raise ConfigError(f"empty license specification in {var}")
    expr = "{ " + " || ".join(checks) + "; }"
    return f"license:{var}", expr


def shell_bundle(data: dict, tool_name: str, target_name: str | None = None) -> dict[str, str]:
    tname, tcfg, argv = target(data, target_name)
    cfg, profile = tool(data, tool_name, tname)
    home = os.environ.get("EDA_REMOTE_HOME", str(tcfg["remote_home"]))
    locale = os.environ.get("EDA_REMOTE_LANG", str(tcfg.get("locale") or "en_US.UTF-8"))
    lc_all = os.environ.get("EDA_REMOTE_LC_ALL", locale)
    run_root = os.environ.get("EDA_RUN_ROOT", str(tcfg["run_root"]))
    exact, prepend = environment(profile, cfg)

    exports = {
        "HOME": home,
        "LANG": locale,
        "LC_ALL": lc_all,
        "EDA_TOOL_BIN": str(cfg["bin"]),
        "EDA_TOOL_ROOT": str(cfg["root"]),
        "EDA_RUN_ROOT": run_root,
        **exact,
    }
    parts = [f"export {k}={shlex.quote(v)}" for k, v in exports.items()]
    for k, paths in prepend.items():
        parts.append(f"export {k}={shlex.quote(':'.join(paths))}:${{{k}:-}}")

    preflight_steps = [
        f'mkdir -p {shlex.quote(run_root)} || {{ echo "ENVIRONMENT BLOCKED: cannot create run_root {run_root}" >&2; exit 20; }}',
        f'test -x "$EDA_TOOL_BIN" || {{ echo "ENVIRONMENT BLOCKED: missing executable $EDA_TOOL_BIN" >&2; exit 20; }}',
    ]
    lic = license_shell_expr(profile)
    if lic is not None:
        label, expr = lic
        preflight_steps.append(
            f'{expr} || {{ echo "ENVIRONMENT BLOCKED: {label} unreachable" >&2; exit 20; }}'
        )

    return {
        "TARGET_NAME": tname,
        "TRANSPORT_CMD": shlex.join(argv),
        "TOOL_BIN": str(cfg["bin"]),
        "TOOL_ROOT": str(cfg["root"]),
        "PROBE_CMD": str(cfg["probe"]),
        "REMOTE_HOME": home,
        "REMOTE_RUN_ROOT": run_root,
        "BASE_REMOTE": "; ".join(parts),
        "INLINE_PREFLIGHT": "; ".join(preflight_steps),
    }


def main() -> int:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", type=Path, default=argparse.SUPPRESS, help="explicit tools.yaml path")
    common.add_argument("--target", default=argparse.SUPPRESS, help="named target from tools.yaml or tools.d/")
    parser = argparse.ArgumentParser(description="Query commercial EDA environment configuration.", parents=[common])
    parser.set_defaults(config=None, target=None)
    sub = parser.add_subparsers(dest="command", required=True)
    for cmd_name in ("validate", "list-tools", "list-targets", "target-vars"):
        sub.add_parser(cmd_name, parents=[common])
    svars = sub.add_parser("shell-vars", parents=[common])
    svars.add_argument("tool")
    args = parser.parse_args()

    try:
        cfg_path = resolve_config_path(args.config)
        data = load(cfg_path)
        if args.command == "validate":
            tname, _, argv = target(data, args.target)
            check_transport_executable(argv)
            tools, _ = effective_sections(data, tname)
            print(f"EDA CONFIG PASS: {cfg_path} (target={tname}, tools={len(tools)})")
        elif args.command == "list-tools":
            tools, _ = effective_sections(data, target(data, args.target)[0])
            print("\n".join(tools))
        elif args.command == "list-targets":
            print("\n".join(data["targets"]))
        elif args.command == "target-vars":
            tname, tcfg, argv = target(data, args.target)
            check_transport_executable(argv)
            run_root = os.environ.get("EDA_RUN_ROOT", str(tcfg["run_root"]))
            out = {
                "TARGET_NAME": tname,
                "TRANSPORT_CMD": shlex.join(argv),
                "REMOTE_RUN_ROOT": run_root,
            }
            for k, v in out.items():
                print(f"{k}={shlex.quote(v)}")
        elif args.command == "shell-vars":
            _, _, argv = target(data, args.target)
            check_transport_executable(argv)
            for k, v in shell_bundle(data, args.tool, args.target).items():
                print(f"{k}={shlex.quote(v)}")
    except ConfigError as exc:
        print(f"EDA CONFIG ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
