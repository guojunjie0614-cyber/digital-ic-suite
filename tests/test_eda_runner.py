#!/usr/bin/env python3
"""Tests for decoupled eda_runner and repository desensitization."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "eda_runner" / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.dont_write_bytecode = True

import eda_env  # noqa: E402


def make_fixture(root: Path, remote_shell: str = "bash") -> tuple[Path, Path, Path]:
    remote = root / "remote_runs"
    home = root / "home"
    home.mkdir()
    call_log = root / "transport_calls.log"
    helper = root / "mock_connect.sh"
    helper.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        f"echo CALL >> {call_log}\n"
        '[[ "$1" == "--cmd" ]]\n'
        f'exec {remote_shell} -c "$2"\n'
    )
    helper.chmod(0o755)
    lic_file = root / "lic.dat"
    lic_file.write_text("SERVER stub ANY 27000\n")

    cfg = root / "tools.yaml"
    cfg.write_text(yaml.safe_dump({
        "schema_version": 3,
        "default_target": "helper_box",
        "targets": {
            "helper_box": {
                "transport": "command",
                "command": [str(helper), "--cmd"],
                "remote_home": str(home),
                "run_root": str(remote),
                "locale": "C.UTF-8",
            },
            "ssh_box": {
                "transport": "ssh",
                "host": "example-eda-host",
                "remote_home": "/home/user",
                "run_root": "/home/user/eda-runs",
            },
        },
        "profiles": {
            "local_prof": {
                "expected_env": {"TEST_LIC_FILE": str(lic_file)},
                "license": {"kind": "file_env", "variable": "TEST_LIC_FILE"},
            },
        },
        "tools": {
            "mock-dc": {
                "profile": "local_prof",
                "root": "/bin",
                "bin": "/bin/bash",
                "probe": "true",
            },
        },
    }, sort_keys=False))
    return cfg, remote, call_log


class EdaRunnerAndHygieneTests(unittest.TestCase):
    def test_skill_repo_contains_zero_private_leaks_and_valid_links(self) -> None:
        patterns = [
            re.compile(
                r"\b(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}"
                r"|192\.168\.\d{1,3}\.\d{1,3}"
                r"|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})\b"
            ),
            re.compile(r"/(?:Users|home)/(?!designer\b|user\b|runner\b)[A-Za-z0-9._-]+"),
            re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"),
        ]
        if extra := os.environ.get("DIGITAL_IC_EXTRA_FORBIDDEN_REGEX"):
            patterns.append(re.compile(extra))

        link_re = re.compile(r"\[[^\]]*\]\(([^)#]+)")
        for path in ROOT.rglob("*"):
            if not path.is_file() or "__pycache__" in path.parts or ".git" in path.parts:
                continue
            text = path.read_text(errors="ignore")
            for pattern in patterns:
                match = pattern.search(text)
                self.assertIsNone(
                    match,
                    f"Private/hardcoded string {match and match.group(0)!r} leaked in {path.relative_to(ROOT)}",
                )
            if path.suffix == ".md":
                for target in link_re.findall(text):
                    if not target.startswith(("http://", "https://", "/")):
                        self.assertTrue(
                            (path.parent / target).resolve().exists(),
                            f"Broken markdown link in {path.relative_to(ROOT)}: {target}",
                        )

    def test_example_template_loads_cleanly(self) -> None:
        data = eda_env.load(ROOT / "templates" / "eda_tools.example.yaml")
        self.assertIn("syn-dc", data["tools"])
        self.assertEqual(data["default_target"], "eda-main")

    def test_flexlm_colon_failover_expression(self) -> None:
        prof = {
            "expected_env": {"SNPSLMD_LICENSE_FILE": "27000@lic1:27000@lic2:/opt/lic.dat"},
            "license": {"kind": "flexlm_env", "variable": "SNPSLMD_LICENSE_FILE"},
        }
        label, expr = eda_env.license_shell_expr(prof)  # type: ignore[misc]
        self.assertEqual(label, "license:SNPSLMD_LICENSE_FILE")
        self.assertIn("/dev/tcp/lic1/27000", expr)
        self.assertIn("/dev/tcp/lic2/27000", expr)
        self.assertIn("test -r /opt/lic.dat", expr)

    def test_single_ssh_roundtrip_and_atomic_job_flow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg, remote, call_log = make_fixture(root)
            env = {**os.environ, "EDA_TOOLS_CONFIG": str(cfg)}

            pre = subprocess.run(
                [sys.executable, str(SCRIPTS / "eda_preflight.py"), "mock-dc", "--json"],
                text=True, capture_output=True, env=env,
            )
            self.assertEqual(pre.returncode, 0, pre.stdout + pre.stderr)
            self.assertEqual(json.loads(pre.stdout)["status"], "pass")

            call_log.write_text("")
            tool_run = subprocess.run(
                ["bash", str(SCRIPTS / "eda_exec.sh"), "mock-dc", "--cmd", "echo INLINE_OK"],
                text=True, capture_output=True, env=env,
            )
            self.assertEqual(tool_run.returncode, 0, tool_run.stdout + tool_run.stderr)
            self.assertIn("INLINE_OK", tool_run.stdout)
            self.assertEqual(len(call_log.read_text().splitlines()), 1)

            job_sh = SCRIPTS / "eda_job.sh"
            def run_job(*args: str) -> subprocess.CompletedProcess:
                return subprocess.run(["bash", str(job_sh), *args], text=True, capture_output=True, env=env)

            self.assertEqual(run_job("init", "proj1").returncode, 0)
            self.assertNotEqual(run_job("init", "proj1").returncode, 0)

            src = root / "rtl"
            src.mkdir()
            (src / "old.sv").write_text("// stale\n")
            self.assertEqual(run_job("sync", "proj1", str(src), "src").returncode, 0)
            (src / "old.sv").unlink()
            (src / "clean.sv").write_text("module clean; endmodule\n")
            self.assertEqual(run_job("sync", "proj1", str(src), "src").returncode, 0)
            self.assertFalse((remote / "proj1/src/rtl/old.sv").exists())

            r = run_job("run", "proj1", "mock-dc", "--cmd", 'echo "WNS=0.05" > "$EDA_JOB_DIR/reports/qor.rpt"')
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

            dl = root / "fetched"
            self.assertEqual(run_job("fetch", "proj1", str(dl), "reports").returncode, 0)
            self.assertEqual((dl / "qor.rpt").read_text().strip(), "WNS=0.05")

            call_log.write_text("")
            bad = run_job("init", "../escape")
            self.assertNotEqual(bad.returncode, 0)
            self.assertEqual(call_log.read_text(), "")

    def test_tcsh_remote_login_shell_compatibility(self) -> None:
        csh_bin = shutil.which("tcsh") or shutil.which("csh")
        if not csh_bin:
            self.skipTest("tcsh/csh required for remote login shell compatibility test")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg, remote, _ = make_fixture(root, remote_shell=csh_bin)
            env = {**os.environ, "EDA_TOOLS_CONFIG": str(cfg)}
            pre = subprocess.run(
                [sys.executable, str(SCRIPTS / "eda_preflight.py"), "mock-dc", "--json"],
                text=True, capture_output=True, env=env,
            )
            self.assertEqual(pre.returncode, 0, pre.stdout + pre.stderr)
            self.assertEqual(json.loads(pre.stdout)["status"], "pass")

            job_sh = SCRIPTS / "eda_job.sh"
            def run_job(*args: str) -> subprocess.CompletedProcess:
                return subprocess.run(["bash", str(job_sh), *args], text=True, capture_output=True, env=env)

            self.assertEqual(run_job("init", "csh_proj").returncode, 0)
            src = root / "rtl"
            src.mkdir()
            (src / "top.sv").write_text("module top; endmodule\n")
            self.assertEqual(run_job("sync", "csh_proj", str(src), "src").returncode, 0)
            self.assertTrue((remote / "csh_proj/src/rtl/top.sv").is_file())
            r = run_job("run", "csh_proj", "mock-dc", "--cmd", 'echo "TCSH_OK" > "$EDA_JOB_DIR/reports/qor.rpt"')
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            dl = root / "fetched"
            self.assertEqual(run_job("fetch", "csh_proj", str(dl), "reports").returncode, 0)
            self.assertEqual((dl / "qor.rpt").read_text().strip(), "TCSH_OK")


if __name__ == "__main__":
    unittest.main()
