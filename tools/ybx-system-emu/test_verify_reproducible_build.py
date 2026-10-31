"""Host-only tests for verify_reproducible_build.sh.

Covered: help/plan output, the fast audit path, and every fail-closed branch
(missing binary, wrong binary, boot without a binary, unpinned media, probe
errors, frame mismatch). The container runner is stubbed through the script's
YBX_DOCKER_BIN hook; no test ever compiles QEMU, starts a real container, or
boots a guest. The positive boot verdict can only come from the orchestrator's
real probe run (experiments/0072).
"""
import hashlib
import json
import os
import subprocess
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "verify_reproducible_build.sh"
EXPECTED_BIN_SHA = (
    "7e554ba9eb3e22b46eaf9d7cbcfbb2279d4b5880d1f407314fb0bbe452e13da7")
HOME_FRAME_SHA = (
    "e79c2ad5f2b60b16fe3f20625742d5f1343f5785552d1b32c11f33c42a874672")
AUDITED_BINARY = Path.home() / (
    ".local/state/ybx-emulator-upstream/qemu-psram-fix-20261031/"
    "build-ybx/qemu-system-xtensa")
QA_DIR = Path.home() / (
    "Projects/YBX/artifacts/ybx-esp32-system-emulation-20261029/qa-20261029")


def run_script(args, env=None, timeout=60):
    environ = dict(os.environ)
    if env:
        environ.update(env)
    start = time.monotonic()
    proc = subprocess.run(["sh", str(SCRIPT), *args], capture_output=True,
                          text=True, timeout=timeout, env=environ)
    return proc, time.monotonic() - start


def booted_env_ready() -> bool:
    """The boot tests reuse the REAL accepted binary and pinned QA media only
    as read-only inputs; without them the fail-closed wiring cannot be
    exercised end to end."""
    return (AUDITED_BINARY.is_file()
            and (QA_DIR / "s3-flash-complete-rom-test.bin").is_file()
            and (QA_DIR / "s3-full-rom-generator-test.bin").is_file()
            and (QA_DIR / "ybx-test-sd-fat16-mbr-pristine.img").is_file())


def stub_docker(tmp: Path, payload: dict | None, exit_code: int = 0):
    """Fake container runner: logs args and fabricates the probe report the
    script would parse after a real container run."""
    log = tmp / "docker.log"
    stub = tmp / "docker"
    writes = ""
    if payload is not None:
        writes = (
            "  for arg in \"$@\"; do\n"
            "    case \"$arg\" in *:/out) OUT=\"${arg%%:/out}\" ;;\n"
            "  esac\n"
            "  done\n"
            f"  printf '%s' {json.dumps(payload)!r} > \"$OUT/boot-frame.json\"\n")
    stub.write_text(
        "#!/bin/sh\n"
        f"echo \"ARGS $*\" >> '{log}'\n"
        "if [ \"$1\" = run ]; then\n"
        f"{writes}"
        f"  exit {exit_code}\n"
        "fi\n"
        "exit 0\n")
    stub.chmod(0o755)
    return stub, log


class PlanAndUsageTests(unittest.TestCase):
    def test_help_exits_zero(self):
        proc, _ = run_script(["--help"])
        self.assertEqual(proc.returncode, 0)
        self.assertIn("--boot", proc.stderr)

    def test_unknown_flag_fails(self):
        proc, _ = run_script(["--definitely-not-a-flag"])
        self.assertNotEqual(proc.returncode, 0)

    def test_print_plan_pins_accepted_facts_and_runs_nothing(self):
        proc, _ = run_script(["--print-plan"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = proc.stdout
        self.assertIn(EXPECTED_BIN_SHA, out)
        self.assertIn(HOME_FRAME_SHA, out)
        self.assertIn("ybx-qemu-builder:20261029", out)
        self.assertIn("probe_x4pro_spi2_guest.py", out)
        # --print-plan never composes or builds: nothing mentions a real tree
        self.assertNotIn("stage=audit state=pass", out)


class FastAuditTests(unittest.TestCase):
    def test_default_run_is_fast_host_only_and_skips_optins(self):
        with TemporaryDirectory(prefix="ybx-verify-test-") as tmp:
            proc, elapsed = run_script(["--work-dir", tmp])
            self.assertEqual(proc.returncode, 0, proc.stderr[-800:])
            self.assertIn("stage=audit state=pass", proc.stdout)
            self.assertIn("stage=compose state=skip", proc.stdout)
            self.assertIn("stage=build state=skip", proc.stdout)
            self.assertIn("stage=binary state=skip", proc.stdout)
            self.assertIn("stage=boot state=skip", proc.stdout)
            self.assertLess(elapsed, 15.0,
                            f"audit path took {elapsed:.1f}s; must stay fast")


class BinaryCompareTests(unittest.TestCase):
    def test_missing_binary_fails_closed(self):
        with TemporaryDirectory(prefix="ybx-verify-test-") as tmp:
            proc, _ = run_script(["--work-dir", tmp,
                                  "--qemu-bin", str(Path(tmp) / "nope")])
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("stage=binary state=missing",
                          proc.stdout + proc.stderr)

    def test_wrong_binary_fails_closed_and_names_both_shas(self):
        with TemporaryDirectory(prefix="ybx-verify-test-") as tmp:
            stub = Path(tmp) / "qemu-system-xtensa"
            stub.write_bytes(b"not-a-qemu-binary\n")
            proc, _ = run_script(["--work-dir", tmp, "--qemu-bin", str(stub)])
            self.assertNotEqual(proc.returncode, 0)
            out = proc.stdout + proc.stderr
            self.assertIn("stage=binary state=differs", out)
            self.assertIn(f"expected={EXPECTED_BIN_SHA}", out)
            self.assertIn(
                f"actual={hashlib.sha256(b'not-a-qemu-binary\n').hexdigest()}",
                out)

    def test_build_stage_delegates_verified_recipe(self):
        text = SCRIPT.read_text()
        self.assertIn('"$BUILDER" --build --verify-binary --jobs "$JOBS"',
                      text)
        self.assertIn('JOBS=2', text)


@unittest.skipUnless(booted_env_ready(),
                     "needs the accepted audit binary and the pinned QA media "
                     "(read-only) to exercise the boot wiring")
class BootStageTests(unittest.TestCase):
    def test_boot_without_binary_fails_closed(self):
        with TemporaryDirectory(prefix="ybx-verify-test-") as tmp:
            proc, _ = run_script(["--work-dir", tmp, "--boot"])
            self.assertNotEqual(proc.returncode, 0)
            out = proc.stdout + proc.stderr
            self.assertIn("stage=boot state=skip reason=no-binary", out)

    def test_boot_refuses_unpinned_media_before_docker(self):
        with TemporaryDirectory(prefix="ybx-verify-test-") as tmp:
            junk = Path(tmp) / "junk-flash.bin"
            junk.write_bytes(b"definitely-not-the-factory-flash\n")
            stub, log = stub_docker(Path(tmp), payload=None)
            proc, _ = run_script(["--work-dir", tmp, "--boot",
                                  "--qemu-bin", str(AUDITED_BINARY),
                                  "--flash", str(junk)],
                                 env={"YBX_DOCKER_BIN": str(stub)})
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("not the pinned QA set", proc.stdout + proc.stderr)
            self.assertFalse(log.exists(),
                             "unpinned media must reject before docker runs")

    def test_boot_probe_error_is_fail_closed(self):
        with TemporaryDirectory(prefix="ybx-verify-test-") as tmp:
            stub, log = stub_docker(Path(tmp), payload=None, exit_code=1)
            proc, _ = run_script(["--work-dir", tmp, "--boot",
                                  "--qemu-bin", str(AUDITED_BINARY)],
                                 env={"YBX_DOCKER_BIN": str(stub)})
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("stage=boot state=fail reason=probe-error",
                          proc.stdout + proc.stderr)
            self.assertIn("probe_x4pro_spi2_guest.py", log.read_text())

    def test_boot_frame_mismatch_is_reported_not_fabricated(self):
        """Fixture probe output carries a different panel RAM hash; the script
        must exit non-zero and name both the pinned and the measured value."""
        fake_sha = hashlib.sha256(b"not-the-home-frame").hexdigest()
        payload = {"rom_boot_seen": True,
                   "epd_ram_state": {"frames": 2, "nonwhite": 1234,
                                     "ram-writes": 144000,
                                     "visible-frame-sha256": fake_sha}}
        with TemporaryDirectory(prefix="ybx-verify-test-") as tmp:
            stub, log = stub_docker(Path(tmp), payload=payload)
            proc, _ = run_script(["--work-dir", tmp, "--boot",
                                  "--qemu-bin", str(AUDITED_BINARY)],
                                 env={"YBX_DOCKER_BIN": str(stub)})
            self.assertNotEqual(proc.returncode, 0)
            out = proc.stdout + proc.stderr
            self.assertIn("stage=boot state=home-frame-differs", out)
            self.assertIn(f"expected={HOME_FRAME_SHA}", out)
            self.assertIn(fake_sha, out)
            logged = log.read_text()
            self.assertIn("--epd", logged)
            self.assertIn("--seconds 33", logged)
            # the real accepted binary must have passed the SHA stage first
            self.assertIn("stage=binary state=byte-identical", out)

    def test_boot_zero_frames_is_fail_closed(self):
        payload = {"rom_boot_seen": False, "epd_ram_state": {"frames": 0}}
        with TemporaryDirectory(prefix="ybx-verify-test-") as tmp:
            stub, _ = stub_docker(Path(tmp), payload=payload)
            proc, _ = run_script(["--work-dir", tmp, "--boot",
                                  "--qemu-bin", str(AUDITED_BINARY)],
                                 env={"YBX_DOCKER_BIN": str(stub)})
            self.assertNotEqual(proc.returncode, 0)
            out = proc.stdout + proc.stderr
            self.assertIn("stage=boot state=no-frame", out)


if __name__ == "__main__":
    unittest.main()
