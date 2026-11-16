#!/usr/bin/env python3
"""First-run onboarding for a commercial EDA server.

Takes an SSH address (from flags, or interactive prompts on a terminal),
verifies non-interactive key-based login, reads the remote login environment
(license variables, PATH), searches standard install roots for Synopsys,
Cadence, and Siemens tools, and saves everything as one private drop-in file:

    ~/.config/digital-ic-suite/tools.d/<target>.yaml   (mode 600)

Guarantees:
  * Passwords are never requested or stored. The server must accept key-based
    login (`ssh -o BatchMode=yes <host> true`); for password-only servers the
    user runs `ssh-copy-id <host>` once.
  * A hand-written main tools.yaml is never rewritten. Only its top-level
    `default_target:` line changes when this target becomes the default, and a
    timestamped backup is written first.
  * Infrastructure failures (SSH unreachable, auth refused) exit with rc=20.

Examples:
  eda_setup.py init --host user@eda-server --default
  eda_setup.py init --target lab --host eda-alias --port 2222 --jump user@bastion
  eda_setup.py init --target lab --host eda-alias --license synopsys=27000@lic-host --force
  eda_setup.py list
  eda_setup.py remove --target lab
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path, PurePosixPath

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
from eda_env import (  # noqa: E402
    DEFAULT_CONFIG,
    TARGET_NAME,
    ConfigError,
    check_transport_executable,
    config_exists,
    dropin_dir,
    load,
    require_yaml,
    target as resolve_target,
    wrap_remote_bash,
)

RC_OK, RC_ERROR, RC_BLOCKED = 0, 1, 20
SCRIPTS_DIR = Path(__file__).resolve().parent

# tool name -> (executable, vendor profile, probe command run on the server)
CATALOG: dict[str, tuple[str, str, str]] = {
    "syn-dc": ("dc_shell", "synopsys", 'timeout 30 "$EDA_TOOL_BIN" -64bit -x "quit" </dev/null'),
    "syn-pt": ("pt_shell", "synopsys", 'timeout 30 "$EDA_TOOL_BIN" -x "quit" </dev/null'),
    "syn-fm": ("fm_shell", "synopsys", 'timeout 30 "$EDA_TOOL_BIN" -64bit -x "quit" </dev/null'),
    "syn-lc": ("lc_shell", "synopsys", 'timeout 30 "$EDA_TOOL_BIN" -x "quit" </dev/null'),
    "syn-vcs": ("vcs", "synopsys", 'timeout 30 "$EDA_TOOL_BIN" -ID'),
    "syn-verdi": ("verdi", "synopsys", 'test -x "$EDA_TOOL_BIN"'),
    "syn-spyglass": ("spyglass", "synopsys", 'timeout 30 "$EDA_TOOL_BIN" -version'),
    "cad-xrun": ("xrun", "cadence", 'timeout 30 "$EDA_TOOL_BIN" -version'),
    "cad-irun": ("irun", "cadence", 'timeout 30 "$EDA_TOOL_BIN" -version'),
    "cad-genus": ("genus", "cadence", 'timeout 60 "$EDA_TOOL_BIN" -version'),
    "cad-innovus": ("innovus", "cadence", 'test -x "$EDA_TOOL_BIN"'),
    "mentor-vsim": ("vsim", "mentor", 'timeout 30 "$EDA_TOOL_BIN" -version'),
}
TOOL_HOME_VARS = {"syn-vcs": "VCS_HOME", "syn-verdi": "VERDI_HOME", "syn-spyglass": "SPYGLASS_HOME"}
VENDOR_LICENSE_VARS = {
    "synopsys": ("SNPSLMD_LICENSE_FILE", "LM_LICENSE_FILE"),
    "cadence": ("CDS_LIC_FILE", "LM_LICENSE_FILE"),
    "mentor": ("MGLS_LICENSE_FILE", "LM_LICENSE_FILE"),
}
EXTRA_ENV_VARS = ("SYNOPSYS", "VCS_HOME", "VERDI_HOME", "SPYGLASS_HOME", "CDS_INST_DIR")
DEFAULT_SEARCH_ROOTS = (
    "/opt", "/eda", "/tools", "/cad", "/apps", "/software",
    "/usr/local", "/usr/synopsys", "/usr/cadence", "/usr/eda",
)
HOST_RE = re.compile(r"[A-Za-z0-9_.@:\[\]%-]+")


# ----------------------------------------------------------------------------
# Remote probing
# ----------------------------------------------------------------------------
def run_remote(argv: list[str], script: str, timeout: int) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            [*argv, wrap_remote_bash(script)], text=True, capture_output=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return 124, "", f"remote command timed out after {timeout}s"
    except OSError as exc:
        return 127, "", str(exc)
    return proc.returncode, proc.stdout, proc.stderr


def discovery_script(roots: list[str]) -> str:
    q = shlex.quote
    binaries = sorted({spec[0] for spec in CATALOG.values()})
    env_vars = sorted({v for vs in VENDOR_LICENSE_VARS.values() for v in vs} | set(EXTRA_ENV_VARS))
    names = " -o ".join(f"-name {q(b)}" for b in binaries)
    return "\n".join([
        'printf "META|HOME|%s\\n" "$HOME"',
        'printf "META|HOST|%s\\n" "$(hostname 2>/dev/null || uname -n)"',
        'printf "META|SHELL|%s\\n" "${SHELL:-}"',
        f'for v in {" ".join(env_vars)}; do',
        '  val="$(printenv "$v" 2>/dev/null || true)"',
        '  if [ -n "$val" ]; then printf "ENV|%s|%s\\n" "$v" "$val"; fi',
        'done',
        f'for b in {" ".join(binaries)}; do',
        '  p="$(command -v "$b" 2>/dev/null || true)"',
        '  case "$p" in /*) if [ -x "$p" ]; then printf "BIN|%s|%s\\n" "$b" "$p"; fi ;; esac',
        'done',
        'if command -v timeout >/dev/null 2>&1; then TO="timeout 120"; else TO=""; fi',
        f'for r in {" ".join(q(r) for r in roots)}; do',
        '  [ -d "$r" ] || continue',
        f'  $TO find "$r" -maxdepth 8 \\( -name doc -o -name docs -o -name examples -o -name .snapshot \\) -prune '
        f'-o \\( -type f -o -type l \\) \\( {names} \\) -print 2>/dev/null | head -n 500 | sed "s/^/FOUND|/"',
        'done',
        'printf "END|ok\\n"',
    ])


def parse_discovery(text: str) -> dict:
    info: dict = {"meta": {}, "env": {}, "path_bins": {}, "found": [], "complete": False}
    for line in text.splitlines():
        parts = line.split("|", 2)
        kind = parts[0]
        if kind == "META" and len(parts) == 3:
            info["meta"][parts[1]] = parts[2]
        elif kind == "ENV" and len(parts) == 3:
            info["env"][parts[1]] = parts[2]
        elif kind == "BIN" and len(parts) == 3:
            info["path_bins"][parts[1]] = parts[2]
        elif kind == "FOUND" and len(parts) >= 2:
            info["found"].append(line.split("|", 1)[1])
        elif kind == "END":
            info["complete"] = True
    return info


def _natural(text: str) -> list:
    return [int(tok) if tok.isdigit() else tok for tok in re.split(r"(\d+)", text)]


def choose_binary(binary: str, info: dict) -> tuple[str | None, list[str]]:
    """Prefer the binary already on the login PATH, else the shallowest, newest install."""
    candidates = {
        p for p in info["found"]
        if PurePosixPath(p).name == binary and PurePosixPath(p).parent.name == "bin"
    }
    path_hit = info["path_bins"].get(binary)
    ranked = sorted(candidates, key=_natural, reverse=True)
    ranked.sort(key=lambda p: len(PurePosixPath(p).parts))
    if path_hit:
        chosen = path_hit
    elif ranked:
        chosen = ranked[0]
    else:
        return None, []
    return chosen, [p for p in ranked if p != chosen]


def tool_root(bin_path: str) -> str:
    """.../syn/V-2023.12/bin/dc_shell -> .../syn/V-2023.12 ; .../XCELIUM/tools.lnx86/bin/xrun -> .../XCELIUM"""
    root = PurePosixPath(bin_path).parent.parent
    if root.name.startswith("tools") and len(root.parts) > 2:
        root = root.parent
    return str(root)


def build_tool(name: str, bin_path: str) -> dict:
    _, vendor, probe = CATALOG[name]
    root = tool_root(bin_path)
    cfg: dict = {
        "profile": vendor,
        "root": root,
        "bin": bin_path,
        "probe": probe,
        "path_prepend": {"PATH": [str(PurePosixPath(bin_path).parent)]},
    }
    if home_var := TOOL_HOME_VARS.get(name):
        cfg["expected_env"] = {home_var: root}
    return cfg


def build_profiles(vendors: set[str], env: dict, overrides: dict) -> tuple[dict, dict]:
    profiles: dict = {}
    notes: dict = {}
    for vendor in sorted(vendors):
        var = value = source = None
        if vendor in overrides:
            var, value, source = VENDOR_LICENSE_VARS[vendor][0], overrides[vendor], "--license"
        else:
            for cand in VENDOR_LICENSE_VARS[vendor]:
                if env.get(cand):
                    var, value, source = cand, env[cand], "remote login environment"
                    break
        if value:
            kind = "flexlm_env" if "@" in value else "file_env"
            profiles[vendor] = {"expected_env": {var: value}, "license": {"kind": kind, "variable": var}}
            notes[vendor] = {"variable": var, "value": value, "source": source}
        else:
            profiles[vendor] = {}
            notes[vendor] = None
    return profiles, notes


# ----------------------------------------------------------------------------
# Config file helpers
# ----------------------------------------------------------------------------
def main_config_path(args: argparse.Namespace) -> Path:
    if getattr(args, "config", None):
        return Path(args.config).expanduser()
    if getattr(args, "project", False):
        return Path.cwd() / ".eda_tools.yaml"
    if env_cfg := os.environ.get("EDA_TOOLS_CONFIG"):
        return Path(env_cfg).expanduser()
    return DEFAULT_CONFIG


def write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


def set_default_target(main_path: Path, name: str, stamp: str) -> Path | None:
    """Point default_target at `name`, editing only that line. Returns the backup path, if any."""
    if not main_path.is_file():
        write_private(main_path, (
            "# digital-ic-suite commercial EDA configuration.\n"
            "# Servers onboarded by eda_setup.py live in the sibling *.d/ directory.\n"
            f"schema_version: 3\ndefault_target: {name}\n"
        ))
        return None
    text = main_path.read_text()
    backup = main_path.with_name(f"{main_path.name}.bak-{stamp}")
    write_private(backup, text)
    if re.search(r"(?m)^default_target:.*$", text):
        text = re.sub(r"(?m)^default_target:.*$", f"default_target: {name}", text, count=1)
    else:
        text = text.rstrip("\n") + f"\ndefault_target: {name}\n"
    write_private(main_path, text)
    return backup


def load_existing(main_path: Path) -> dict:
    if not config_exists(main_path):
        return {"targets": {}, "_sources": {}}
    return load(main_path, allow_empty=True)


# ----------------------------------------------------------------------------
# Commands
# ----------------------------------------------------------------------------
def prompt_missing(args: argparse.Namespace) -> None:
    if args.host or args.command:
        return
    if not sys.stdin.isatty():
        raise ConfigError("missing --host (or --command); pass flags or run in a terminal for prompts")
    print("Digital IC Suite - commercial EDA server onboarding")
    print("Key-based SSH only; passwords are never asked for or stored.\n")
    args.host = input("SSH address (user@host or ~/.ssh/config alias): ").strip()
    port = input("SSH port [22]: ").strip()
    args.port = int(port) if port else None
    args.jump = input("Jump host, e.g. user@bastion (blank if none): ").strip() or None
    if not args.target:
        args.target = input(f"Short name for this server [{derive_name(args.host)}]: ").strip() or None


def derive_name(host: str | None) -> str:
    base = (host or "eda").split("@")[-1].split(".")[0].strip("[]")
    base = re.sub(r"[^A-Za-z0-9._-]", "-", base).strip("-.") or "eda"
    return base if TARGET_NAME.fullmatch(base) else "eda"


def build_transport(args: argparse.Namespace) -> dict:
    if args.command:
        return {"transport": "command", "command": shlex.split(args.command)}
    host = args.host.strip()
    if args.user and "@" not in host:
        host = f"{args.user}@{host}"
    if host.startswith("-") or not HOST_RE.fullmatch(host):
        raise ConfigError(f"invalid SSH address {host!r}")
    ssh_args = ["-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]
    if args.port:
        ssh_args += ["-p", str(args.port)]
    if args.jump:
        if args.jump.startswith("-") or not HOST_RE.fullmatch(args.jump.replace(",", "")):
            raise ConfigError(f"invalid jump host {args.jump!r}")
        ssh_args += ["-J", args.jump]
    if args.identity_file:
        ssh_args += ["-i", str(Path(args.identity_file).expanduser()), "-o", "IdentitiesOnly=yes"]
    return {"transport": "ssh", "host": host, "ssh_args": ssh_args}


def parse_pairs(items: list[str], flag: str, allowed: set[str] | None = None) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in items or []:
        key, sep, value = item.partition("=")
        key, value = key.strip(), value.strip()
        if not sep or not key or not value:
            raise ConfigError(f"{flag} expects KEY=VALUE, got {item!r}")
        if allowed is not None and key not in allowed:
            raise ConfigError(f"{flag}: unknown key {key!r} (choose from: {', '.join(sorted(allowed))})")
        out[key] = value
    return out


def blocked(message: str, detail: str, host_hint: str) -> int:
    print(f"ENVIRONMENT BLOCKED: {message}", file=sys.stderr)
    if detail.strip():
        print("  remote said: " + " | ".join(detail.strip().splitlines()[-3:]), file=sys.stderr)
    print(
        "  - Check key login works without a prompt: ssh -o BatchMode=yes " + host_hint + " true\n"
        "  - Password-only server: run `ssh-copy-id " + host_hint + "` once yourself "
        "(this tool never stores passwords).\n"
        "  - Behind a bastion: pass --jump user@bastion, or define a Host alias in ~/.ssh/config "
        "and pass --host <alias>.",
        file=sys.stderr,
    )
    return RC_BLOCKED


def cmd_init(args: argparse.Namespace) -> int:
    prompt_missing(args)
    name = args.target or derive_name(args.host or "eda")
    if not TARGET_NAME.fullmatch(name):
        raise ConfigError(f"invalid --target {name!r}; use letters, digits, '.', '_' or '-'")
    licenses = parse_pairs(args.license, "--license", set(VENDOR_LICENSE_VARS))
    tool_overrides = parse_pairs(args.tool, "--tool", set(CATALOG))

    main_path = main_config_path(args)
    dropin = dropin_dir(main_path) / f"{name}.yaml"
    existing = load_existing(main_path)
    source = existing["_sources"].get(name)
    if source and Path(source) != dropin:
        raise ConfigError(f"target {name!r} is already defined by hand in {source}; pick another --target")
    if source and not args.force and not args.dry_run:
        raise ConfigError(f"target {name!r} already onboarded ({dropin}); pass --force to redo it")

    transport = build_transport(args)
    _, _, argv = resolve_target({"targets": {name: {**transport, "remote_home": "-", "run_root": "-"}}}, name)
    check_transport_executable(argv)
    host_hint = transport.get("host") or shlex.join(transport["command"])

    # 1) Connectivity: one quick round trip, also reports the remote $HOME.
    rc, out, err = run_remote(argv, 'printf "META|HOME|%s\\n" "$HOME"', timeout=45)
    meta = parse_discovery(out)["meta"]
    if rc != 0 or not meta.get("HOME"):
        return blocked(f"cannot log in to {host_hint!r} non-interactively (rc={rc})", err or out, host_hint)

    # 2) Discovery: login environment + install-root search, one more round trip.
    info = {"meta": dict(meta), "env": {}, "path_bins": {}, "found": [], "complete": True}
    if not args.no_discover:
        roots = list(args.search_root or DEFAULT_SEARCH_ROOTS) + list(args.add_search_root or [])
        rc, out, err = run_remote(argv, discovery_script(roots), timeout=args.discover_timeout)
        if rc != 0 and "END|ok" not in out:
            return blocked(f"discovery on {host_hint!r} failed (rc={rc})", err or out, host_hint)
        info = parse_discovery(out)
        info["meta"].setdefault("HOME", meta["HOME"])

    remote_home = args.remote_home or info["meta"]["HOME"]
    run_root = args.run_root or f"{remote_home.rstrip('/')}/eda-runs"

    tools: dict = {}
    alternatives: dict = {}
    for tool_name, (binary, _, _) in CATALOG.items():
        if tool_name in tool_overrides:
            tools[tool_name] = build_tool(tool_name, tool_overrides[tool_name])
            continue
        chosen, others = choose_binary(binary, info)
        if chosen:
            tools[tool_name] = build_tool(tool_name, chosen)
            if others:
                alternatives[tool_name] = others[:10]
    vendors = {tools[t]["profile"] for t in tools} | set(licenses)
    profiles, license_notes = build_profiles(vendors, info["env"], licenses)

    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    doc = {
        "schema_version": 3,
        "name": name,
        "target": {
            **transport,
            "remote_home": remote_home,
            "run_root": run_root,
            "locale": args.locale,
            "profiles": profiles,
            "tools": tools,
        },
        "discovery": {
            "date": stamp,
            "remote_host": info["meta"].get("HOST"),
            "remote_login_shell": info["meta"].get("SHELL"),
            "search_complete": bool(info.get("complete")),
            "alternatives": alternatives,
            "not_found": [t for t in CATALOG if t not in tools],
        },
    }
    yaml = require_yaml()
    text = (
        f"# Onboarded by digital-ic-suite eda_setup.py on {stamp}.\n"
        "# Safe to hand-edit. Delete this file (or run `eda_setup.py remove`) to forget the server.\n"
        + yaml.safe_dump(doc, sort_keys=False, default_flow_style=False)
    )

    make_default = args.default or not existing.get("default_target")
    summary = {
        "status": "dry-run" if args.dry_run else "pass",
        "target": name,
        "config": str(dropin),
        "default": make_default,
        "login": host_hint,
        "remote_home": remote_home,
        "run_root": run_root,
        "remote_host": info["meta"].get("HOST"),
        "licenses": license_notes,
        "tools": {t: cfg["bin"] for t, cfg in tools.items()},
        "alternatives": alternatives,
        "not_found": doc["discovery"]["not_found"],
    }
    if args.dry_run:
        print(json.dumps(summary, indent=2) if args.json else text)
        return RC_OK

    # 3) Persist atomically, then validate the merged config; roll back on failure.
    previous = dropin.read_text() if dropin.is_file() else None
    write_private(dropin, text)
    os.chmod(dropin.parent, 0o700)
    backup = None
    main_before = main_path.read_text() if main_path.is_file() else None
    try:
        if make_default:
            backup = set_default_target(main_path, name, stamp)
        resolve_target(load(main_path), name)
    except ConfigError:
        if previous is None:
            dropin.unlink(missing_ok=True)
        else:
            write_private(dropin, previous)
        if main_before is not None:
            write_private(main_path, main_before)
        elif make_default:
            main_path.unlink(missing_ok=True)
        raise

    summary["main_config_backup"] = str(backup) if backup else None
    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        print_summary(summary, main_path)
    return RC_OK


def print_summary(s: dict, main_path: Path) -> None:
    print(f"EDA SETUP PASS: target '{s['target']}' saved to {s['config']} (mode 600)")
    print(f"  login      : {s['login']} (key-based, BatchMode)")
    print(f"  remote     : host={s['remote_host'] or '?'}  HOME={s['remote_home']}")
    print(f"  run_root   : {s['run_root']}")
    print(f"  default    : {'yes' if s['default'] else 'no (use --target ' + s['target'] + ')'}")
    if s.get("main_config_backup"):
        print(f"  backup     : {s['main_config_backup']} (only default_target changed)")
    print("  licenses   :")
    if not s["licenses"]:
        print("    (no vendor tools found)")
    for vendor, note in s["licenses"].items():
        if note:
            print(f"    {vendor:<9} {note['variable']}={note['value']}  [{note['source']}]")
        else:
            print(f"    {vendor:<9} NOT FOUND -> rerun with --license {vendor}=<port@host or file> --force")
    print("  tools      :")
    if not s["tools"]:
        print("    none found -> add --add-search-root <dir> or --tool <name>=<path/bin/exe>, then --force")
    for tool_name, bin_path in s["tools"].items():
        extra = s["alternatives"].get(tool_name)
        more = f"  (+{len(extra)} other install(s); pin with --tool {tool_name}=<path>)" if extra else ""
        print(f"    {tool_name:<13} {bin_path}{more}")
    if s["not_found"]:
        print(f"  not found  : {', '.join(s['not_found'])}")
    if (Path.cwd() / ".eda_tools.yaml").is_file() and main_path.resolve() != (Path.cwd() / ".eda_tools.yaml").resolve():
        print("  note       : ./.eda_tools.yaml in this directory takes precedence over this config")
    if s["tools"]:
        first = next(iter(s["tools"]))
        print(f"Next: python3 {SCRIPTS_DIR / 'eda_preflight.py'} {first} --target {s['target']} --level probe")


def cmd_list(args: argparse.Namespace) -> int:
    main_path = main_config_path(args)
    data = load_existing(main_path)
    if not data["targets"]:
        print(f"No EDA servers configured ({main_path}). Onboard one with: eda_setup.py init --host <user@host>")
        return RC_OK
    default = data.get("default_target")
    for name, tcfg in data["targets"].items():
        where = tcfg.get("host") or " ".join(map(str, tcfg.get("command") or []))
        mark = "*" if name == default else " "
        count = len(tcfg.get("tools") or data.get("tools") or {})
        print(f"{mark} {name:<14} {tcfg.get('transport'):<8} {where:<32} tools={count:<3} {data['_sources'][name]}")
    return RC_OK


def cmd_remove(args: argparse.Namespace) -> int:
    main_path = main_config_path(args)
    dropin = dropin_dir(main_path) / f"{args.target}.yaml"
    if not dropin.is_file():
        data = load_existing(main_path)
        if args.target in data["targets"]:
            raise ConfigError(f"{args.target!r} is defined by hand in {data['_sources'][args.target]}; edit that file")
        raise ConfigError(f"no onboarded target {args.target!r} ({dropin})")
    dropin.unlink()
    print(f"Removed {dropin}")
    data = load_existing(main_path)
    if data.get("default_target") == args.target:
        print(f"Warning: default_target in {main_path} still names {args.target!r}; "
              "onboard another server with --default or edit that line.")
    return RC_OK


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", help="main config path (default: $EDA_TOOLS_CONFIG or ~/.config/digital-ic-suite/tools.yaml)")
    common.add_argument("--project", action="store_true", help="use ./.eda_tools.yaml (+ ./.eda_tools.d/) instead")

    parser = argparse.ArgumentParser(description="Onboard and manage commercial EDA servers.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    init = sub.add_parser("init", parents=[common], help="connect a server, discover tools, save config")
    init.add_argument("--target", help="short name for this server (default: derived from host)")
    init.add_argument("--host", help="SSH address: user@host, host, or ~/.ssh/config alias")
    init.add_argument("--user", help="SSH user when --host has no user@ part")
    init.add_argument("--port", type=int, help="SSH port (default 22)")
    init.add_argument("--jump", help="bastion/jump host(s) for ssh -J, e.g. user@bastion")
    init.add_argument("--identity-file", help="private key path for ssh -i (the key itself is never copied)")
    init.add_argument("--command", help="custom connector instead of ssh, e.g. '/path/connect.sh --cmd'")
    init.add_argument("--remote-home", help="override remote $HOME (default: detected)")
    init.add_argument("--run-root", help="remote job directory root (default: $HOME/eda-runs)")
    init.add_argument("--locale", default="en_US.UTF-8")
    init.add_argument("--license", action="append", metavar="VENDOR=VALUE",
                      help="license spec per vendor: synopsys|cadence|mentor = port@host[:...] or file path")
    init.add_argument("--tool", action="append", metavar="NAME=PATH",
                      help=f"pin a tool binary, NAME in: {', '.join(CATALOG)}")
    init.add_argument("--search-root", action="append", help="replace the default install roots to search")
    init.add_argument("--add-search-root", action="append", help="search this root in addition to the defaults")
    init.add_argument("--discover-timeout", type=int, default=300)
    init.add_argument("--no-discover", action="store_true", help="only save the connection, skip tool search")
    init.add_argument("--default", action="store_true", help="make this the default target")
    init.add_argument("--force", action="store_true", help="redo an already onboarded target")
    init.add_argument("--dry-run", action="store_true", help="print the config instead of saving it")
    init.add_argument("--json", action="store_true", help="machine-readable summary")
    init.set_defaults(func=cmd_init)

    lst = sub.add_parser("list", parents=[common], help="list configured servers")
    lst.set_defaults(func=cmd_list)

    rm = sub.add_parser("remove", parents=[common], help="forget an onboarded server")
    rm.add_argument("--target", required=True)
    rm.set_defaults(func=cmd_remove)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        return args.func(args)
    except ConfigError as exc:
        print(f"EDA SETUP ERROR: {exc}", file=sys.stderr)
        return RC_ERROR
    except (KeyboardInterrupt, EOFError):
        print("\nEDA SETUP ABORTED", file=sys.stderr)
        return RC_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
