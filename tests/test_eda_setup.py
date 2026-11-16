#!/usr/bin/env python3
"""Tests for eda_setup.py first-run server onboarding."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "eda_runner" / "scripts"
SETUP = SCRIPTS / "eda_setup.py"
sys.path.insert(0, str(SCRIPTS))
sys.dont_write_bytecode = True

import eda_env  # noqa: E402
import eda_setup  # noqa: E402


def fake_server(root: Path, *, fail: bool = False) -> tuple[str, Path, Path]:
    """Build a fake remote: install tree, license file, HOME, and a connector script."""
    eda = root / "eda"
    for rel in (
        "synopsys/syn/T-2022.03/bin/dc_shell",
        "synopsys/syn/V-2023.12/bin/dc_shell",
        "synopsys/syn/V-2023.12/linux64/syn/bin/dc_shell",
        "synopsys/vcs/V-2023.12/bin/vcs",
        "cadence/XCELIUM2309/tools.lnx86/bin/xrun",
        "synopsys/syn/V-2023.12/doc/bin/dc_shell",
    ):
        exe = eda / rel
        exe.parent.mkdir(parents=True, exist_ok=True)
        exe.write_text("#!/bin/sh\nexit 0\n")
        exe.chmod(0o755)
    home = root / "home"
    home.mkdir()
    lic = root / "synopsys.lic"
    lic.write_text("SERVER stub ANY 27000\n")
    helper = root / "connect.sh"
    body = "exit 255\n" if fail else (
        f'export HOME="{home}"\n'
        f'export SNPSLMD_LICENSE_FILE="{lic}"\n'
        '[[ "$1" == "--cmd" ]]\n'
        'exec bash -c "$2"\n'
    )
    helper.write_text("#!/usr/bin/env bash\nset -euo pipefail\n" + body)
    helper.chmod(0o755)
    return f"{helper} --cmd", eda, home


def run_setup(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SETUP), *args], text=True, capture_output=True)


class EdaSetupTests(unittest.TestCase):
    def test_init_discovers_tools_and_writes_private_dropin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            connector, eda, home = fake_server(root)
            main = root / "cfg" / "tools.yaml"
            r = run_setup("init", "--config", str(main), "--target", "lab", "--command", connector,
                          "--search-root", str(eda), "--json")
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            summary = json.loads(r.stdout)

            dropin = root / "cfg" / "tools.d" / "lab.yaml"
            self.assertTrue(dropin.is_file())
            self.assertEqual(stat.S_IMODE(dropin.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(dropin.parent.stat().st_mode), 0o700)
            self.assertTrue(summary["default"])

            data = eda_env.load(main)
            self.assertEqual(eda_env.target(data)[0], "lab")
            tcfg = data["targets"]["lab"]
            self.assertEqual(tcfg["remote_home"], str(home))
            self.assertEqual(tcfg["run_root"], f"{home}/eda-runs")
            tools = tcfg["tools"]
            # Newest shallow install wins; linux64/ and doc/ duplicates are ignored.
            self.assertEqual(tools["syn-dc"]["bin"], str(eda / "synopsys/syn/V-2023.12/bin/dc_shell"))
            self.assertEqual(tools["syn-dc"]["root"], str(eda / "synopsys/syn/V-2023.12"))
            self.assertEqual(tools["syn-vcs"]["expected_env"]["VCS_HOME"], str(eda / "synopsys/vcs/V-2023.12"))
            self.assertEqual(tools["cad-xrun"]["root"], str(eda / "cadence/XCELIUM2309"))
            self.assertIn("syn-dc", summary["alternatives"])
            self.assertEqual(tcfg["profiles"]["synopsys"]["license"]["kind"], "file_env")
            self.assertEqual(tcfg["profiles"]["cadence"], {})
            self.assertIsNone(summary["licenses"]["cadence"])

            pre = subprocess.run(
                [sys.executable, str(SCRIPTS / "eda_preflight.py"), "syn-dc", "--config", str(main),
                 "--target", "lab", "--json"],
                text=True, capture_output=True,
            )
            self.assertEqual(pre.returncode, 0, pre.stdout + pre.stderr)
            self.assertEqual(json.loads(pre.stdout)["status"], "pass")

            listing = run_setup("list", "--config", str(main))
            self.assertIn("* lab", listing.stdout)

    def test_hand_written_main_config_is_preserved_and_targets_are_isolated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            connector, eda, _ = fake_server(root)
            main = root / "tools.yaml"
            original = (
                "# hand-written lab notes: keep me\n"
                "schema_version: 3\n"
                "default_target: bench\n"
                "targets:\n"
                "  bench:\n"
                "    transport: command\n"
                f"    command: [{json.dumps(connector.split()[0])}, \"--cmd\"]\n"
                "    remote_home: /home/user\n"
                "    run_root: /home/user/eda-runs\n"
                "profiles:\n"
                "  synopsys: {}\n"
                "tools:\n"
                "  syn-dc:\n"
                "    profile: synopsys\n"
                "    root: /opt/legacy/syn\n"
                "    bin: /opt/legacy/syn/bin/dc_shell\n"
                "    probe: 'true'\n"
            )
            main.write_text(original)

            r = run_setup("init", "--config", str(main), "--target", "lab", "--command", connector,
                          "--search-root", str(eda), "--json")
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertEqual(main.read_text(), original, "main file must stay untouched without --default")

            data = eda_env.load(main)
            self.assertEqual(eda_env.target(data)[0], "bench")
            self.assertEqual(eda_env.shell_bundle(data, "syn-dc", "bench")["TOOL_BIN"], "/opt/legacy/syn/bin/dc_shell")
            self.assertEqual(eda_env.shell_bundle(data, "syn-dc", "lab")["TOOL_BIN"],
                             str(eda / "synopsys/syn/V-2023.12/bin/dc_shell"))

            r = run_setup("init", "--config", str(main), "--target", "lab", "--command", connector,
                          "--search-root", str(eda), "--default", "--force", "--json")
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            text = main.read_text()
            self.assertIn("# hand-written lab notes: keep me", text)
            self.assertIn("default_target: lab", text)
            self.assertEqual(text.replace("default_target: lab", "default_target: bench"), original)
            self.assertEqual(len(list(root.glob("tools.yaml.bak-*"))), 1)

    def test_refuses_duplicates_and_reports_unreachable_server(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            connector, eda, _ = fake_server(root)
            main = root / "tools.yaml"
            base = ("init", "--config", str(main), "--command", connector, "--search-root", str(eda))
            self.assertEqual(run_setup(*base, "--target", "lab").returncode, 0)
            dup = run_setup(*base, "--target", "lab")
            self.assertEqual(dup.returncode, 1)
            self.assertIn("--force", dup.stderr)

            bad_root = root / "bad"
            bad_root.mkdir()
            bad_connector, _, _ = fake_server(bad_root, fail=True)
            blocked = run_setup("init", "--config", str(main), "--target", "down", "--command", bad_connector)
            self.assertEqual(blocked.returncode, 20, blocked.stdout + blocked.stderr)
            self.assertIn("ENVIRONMENT BLOCKED", blocked.stderr)
            self.assertIn("ssh-copy-id", blocked.stderr)
            self.assertFalse((root / "tools.d" / "down.yaml").exists())

            rm = run_setup("remove", "--config", str(main), "--target", "lab")
            self.assertEqual(rm.returncode, 0, rm.stdout + rm.stderr)
            self.assertFalse((root / "tools.d" / "lab.yaml").exists())

    def test_first_run_asks_until_configured_or_declined(self) -> None:
        probe = ROOT / "rtl_check" / "scripts" / "probe_toolchain.py"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            main = root / "cfg" / "tools.yaml"
            env = {**os.environ, "EDA_TOOLS_CONFIG": str(main)}

            def probe_run(*extra: str) -> subprocess.CompletedProcess:
                return subprocess.run([sys.executable, str(probe), *extra], text=True,
                                      capture_output=True, env=env, cwd=root)

            def status() -> str:
                r = subprocess.run([sys.executable, str(SETUP), "status", "--json"], text=True,
                                   capture_output=True, env=env, cwd=root)
                self.assertEqual(r.returncode, 0, r.stderr)
                return json.loads(r.stdout)["status"]

            # Brand-new machine: the probe must tell the agent to ask for the server first.
            self.assertEqual(status(), "pending")
            first = probe_run()
            self.assertIn("FIRST RUN", first.stdout)
            self.assertIn("AGENT ACTION REQUIRED", first.stdout)
            self.assertIn("Onboarding     : pending", first.stdout)
            self.assertEqual(json.loads(probe_run("--json").stdout)["onboarding"]["status"], "pending")

            # User has no server: recorded once, later sessions stop asking.
            skip = subprocess.run([sys.executable, str(SETUP), "skip", "--reason", "no server yet"],
                                  text=True, capture_output=True, env=env, cwd=root)
            self.assertEqual(skip.returncode, 0, skip.stderr)
            marker = root / "cfg" / "onboarding.yaml"
            self.assertEqual(stat.S_IMODE(marker.stat().st_mode), 0o600)
            self.assertEqual(status(), "local-only")
            self.assertNotIn("FIRST RUN", probe_run().stdout)

            # Connecting a server later switches to configured and clears the marker.
            connector, eda, _ = fake_server(root)
            init = subprocess.run([sys.executable, str(SETUP), "init", "--target", "lab", "--command", connector,
                                   "--search-root", str(eda)], text=True, capture_output=True, env=env, cwd=root)
            self.assertEqual(init.returncode, 0, init.stdout + init.stderr)
            self.assertFalse(marker.exists())
            self.assertEqual(status(), "configured")
            self.assertNotIn("FIRST RUN", probe_run().stdout)

    def test_ssh_transport_flags_and_input_validation(self) -> None:
        parser = eda_setup.build_parser()
        args = parser.parse_args(["init", "--host", "eda-box", "--user", "designer", "--port", "2222",
                                  "--jump", "designer@bastion"])
        transport = eda_setup.build_transport(args)
        self.assertEqual(transport["host"], "designer@eda-box")
        self.assertIn("BatchMode=yes", transport["ssh_args"])
        self.assertEqual(transport["ssh_args"][-4:], ["-p", "2222", "-J", "designer@bastion"])
        self.assertEqual(eda_setup.derive_name("designer@eda-box.example.com"), "eda-box")
        for evil in ("-oProxyCommand=sh", "host; rm -rf /", "a b"):
            args = parser.parse_args(["init", f"--host={evil}"])
            with self.assertRaises(eda_env.ConfigError):
                eda_setup.build_transport(args)


if __name__ == "__main__":
    unittest.main()
