"""Regression tests for the real browser launcher, using a fake Docker CLI.

No Docker container or QEMU process is created by this test.
"""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


HERE = Path(__file__).resolve().parent
LAUNCHER = HERE / "start_x4pro_browser.sh"
FAKE_DOCKER = """#!/usr/bin/env python3
import os
import sys
from pathlib import Path

status = Path(os.environ["FAKE_DOCKER_STATUS"])
log = Path(os.environ["FAKE_DOCKER_LOG"])
args = sys.argv[1:]
with log.open("a") as f:
    f.write(" ".join(args) + "\\n")
mode = status.read_text()
if args[:2] == ["container", "inspect"]:
    sys.exit(1 if mode == "missing" else 0)
if args and args[0] == "inspect":
    print("true" if mode == "running" else "false")
elif args and args[0] == "ps":
    print("Up")
elif args and args[0] == "rm":
    status.write_text("missing")
elif args and args[0] == "run":
    status.write_text("running")
    print("test-container-id")
else:
    sys.exit(3)
"""


class BrowserLauncherTest(unittest.TestCase):
    def run_launch(self, initial_status, bind="127.0.0.1", power="0"):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bin_dir = root / "bin"
            bin_dir.mkdir()
            docker = bin_dir / "docker"
            docker.write_text(FAKE_DOCKER)
            docker.chmod(0o755)

            qemu = root / "qemu" / "build-ybx" / "qemu-system-xtensa"
            qemu.parent.mkdir(parents=True)
            qemu.touch()
            qa_dir = root / "qa"
            qa_dir.mkdir()
            for name in (
                "s3-full-rom-generator-test.bin",
                "s3-flash-complete-rom-test.bin",
                "ybx-test-sd-fat16-mbr-pristine.img",
            ):
                (qa_dir / name).touch()

            status = root / "status"
            status.write_text(initial_status)
            log = root / "docker.log"
            env = {
                **os.environ,
                "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
                "FAKE_DOCKER_STATUS": str(status),
                "FAKE_DOCKER_LOG": str(log),
                "YBX_QEMU_ROOT": str(qemu.parent.parent),
                "YBX_QA_DIR": str(qa_dir),
                "YBX_BROWSER_PORT": "8797",
                "YBX_BROWSER_BIND_ADDRESS": bind,
                "YBX_BROWSER_POWER_BUTTON": power,
            }
            result = subprocess.run(
                ["bash", str(LAUNCHER)],
                env=env, text=True, capture_output=True, timeout=8,
            )
            return result, status.read_text(), log.read_text() if log.exists() else ""


    def test_power_flag_is_opt_in_and_absent_by_default(self):
        result, _, log = self.run_launch("missing")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("--enable-power-button", log)

    def test_power_flag_injects_only_into_experimental_container(self):
        result, _, log = self.run_launch("missing", power="1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--enable-power-button", log)
        self.assertIn("-p 127.0.0.1:8797:8797", log)

    def test_power_refuses_unprotected_lan_without_separate_consent(self):
        result, status, log = self.run_launch("missing", bind="192.168.10.239", power="1")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(status, "missing")
        self.assertEqual(log, "")

    def test_power_refuses_existing_stable_port_8784(self):
        # The safety check runs before Docker even for an existing container.
        with tempfile.TemporaryDirectory() as directory:
            # Test the static guard alongside dynamic tests; PORT is set to
            # 8797 in run_launch, so ensure the hard refusal exists.
            text = LAUNCHER.read_text()
            self.assertIn('"$POWER_FLAG" == "1" && "$PORT" == "8784"',text)
            self.assertIn('Refusing experimental Power on stable browser port 8784',text)

    def test_malformed_power_flag_fails_before_docker(self):
        result, status, log = self.run_launch("missing", power="yes")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(status, "missing")
        self.assertEqual(log, "")

    def test_missing_container_starts(self):
        result, status, log = self.run_launch("missing")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(status, "running")
        self.assertIn("run --rm -d", log)
        self.assertNotIn("rm ybx-x4pro-browser-8797", log)

    def test_stopped_container_is_removed_and_restarted(self):
        result, status, log = self.run_launch("stopped")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(status, "running")
        self.assertIn("Removing stopped container", result.stdout)
        self.assertLess(log.index("rm ybx-x4pro-browser-8797"),
                        log.index("run --rm -d"))

    def test_running_container_not_replaced(self):
        result, status, log = self.run_launch("running")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(status, "running")
        self.assertIn("Existing running container", result.stdout)
        self.assertNotIn("run --rm -d", log)
        self.assertNotIn("rm ybx-x4pro-browser-8797", log)

    def test_refuses_public_bind_without_running_docker(self):
        result, status, log = self.run_launch("missing", "0.0.0.0")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(status, "missing")
        self.assertEqual(log, "")


if __name__ == "__main__":
    unittest.main()
