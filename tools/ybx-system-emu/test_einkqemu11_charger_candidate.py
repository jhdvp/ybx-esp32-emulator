"""Strict opt-in SHA gates for native QEMU11 GPIO21 browser candidate."""
import unittest
import os
import subprocess
from pathlib import Path

from serve_einkqemu11_browser import (
    KNOWN_BINARY_SHA, KNOWN_CHARGER_CANDIDATE_SHAS,
    verify_qemu_binary_sha, page_for_engine,
)


class EngineGates(unittest.TestCase):
    def test_official_still_allowed_by_default(self):
        self.assertEqual(verify_qemu_binary_sha(KNOWN_BINARY_SHA, False),
                         "upstream-official")

    def test_approved_charger_builds_allowed_explicitly(self):
        self.assertEqual(len(KNOWN_CHARGER_CANDIDATE_SHAS), 2)
        for sha in KNOWN_CHARGER_CANDIDATE_SHAS:
            with self.subTest(sha=sha):
                self.assertEqual(len(sha), 64)
                self.assertEqual(verify_qemu_binary_sha(sha, True),
                                 "x4pro-gpio21-charger-candidate")

    def test_candidate_rejected_without_opt_in(self):
        for sha in KNOWN_CHARGER_CANDIDATE_SHAS:
            with self.assertRaises(RuntimeError):
                verify_qemu_binary_sha(sha, False)

    def test_official_rejected_when_candidate_specifically_requested(self):
        with self.assertRaises(RuntimeError):
            verify_qemu_binary_sha(KNOWN_BINARY_SHA, True)

    def test_candidate_html_is_explicitly_experimental(self):
        self.assertIn("GPIO21 patch", page_for_engine(True))
        self.assertIn("EXPERIMENTAL", page_for_engine(True))
        self.assertNotIn("GPIO21 patch", page_for_engine(False))

    def test_launcher_rejects_production_port_before_any_disk_access(self):
        script = Path(__file__).with_name("start_einkqemu11_charger_candidate.sh")
        env = dict(os.environ, YBX_EINK11_CANDIDATE_PORT="8784")
        result = subprocess.run(["bash", str(script), "--check"],
                                capture_output=True, text=True, env=env, timeout=6)
        self.assertEqual(result.returncode, 2)
        self.assertIn("8784", result.stderr)

    def test_launcher_is_loopback_only(self):
        shell = Path(__file__).with_name("start_einkqemu11_charger_candidate.sh").read_text()
        self.assertIn("127.0.0.1:$PORT:$PORT", shell)
        self.assertIn("--charger-candidate", shell)
        self.assertIn("--network bridge", shell)
        self.assertIn(":ro", shell)
        self.assertIn("/persist:rw", shell)

    def test_unknown_binary_never_allowed(self):
        for candidate in (False, True):
            with self.assertRaises(RuntimeError):
                verify_qemu_binary_sha("0" * 64, candidate)


if __name__ == "__main__":
    unittest.main()
