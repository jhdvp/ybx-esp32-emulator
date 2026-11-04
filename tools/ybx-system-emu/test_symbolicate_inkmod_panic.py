"""Offline tests for evidence-safe inkMOD panic symbolication."""
import hashlib
import importlib.util
import tempfile
import unittest
from pathlib import Path

MODULE = Path(__file__).with_name("symbolicate_inkmod_panic.py")
SPEC = importlib.util.spec_from_file_location("inkmod_symbols", MODULE)
assert SPEC and SPEC.loader
MOD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MOD)


def sample(sha="98ffc195c", core=1, pc="0x420453a8", fault="0x000060f0"):
    return (
        "ESP-ROM:esp32s3-20210327\n"
        f"Guru Meditation Error: Core  {core} panic'ed (LoadProhibited). Exception was unhandled.\n"
        f"PC      : {pc}  PS : 0x00060d30\n"
        f"EXCVADDR: {fault}\n"
        "Backtrace: 0x420453a5:0x3fced930 0x42046545:0x3fced970\n"
        f"ELF file SHA256: {sha}\n"
        "Rebooting...\n"
        "Guru Meditation Error: Core  1 panic'ed (Unhandled debug exception).\n"
    )


class SymbolicationTest(unittest.TestCase):
    def test_parses_first_cause_not_followup_break(self):
        p = MOD.first_load_prohibited(sample())
        self.assertEqual(p["core"], 1)
        self.assertEqual(p["exception"], "LoadProhibited")
        self.assertEqual(p["pc"], "0x420453a8")
        self.assertEqual(p["excvaddr"], "0x000060f0")
        self.assertEqual(p["backtrace"], ["0x420453a5", "0x42046545"])
        self.assertEqual(p["elf_sha256_prefix"], "98ffc195c")

    def test_skips_other_panic_before_first_load_prohibited(self):
        log = "Guru Meditation Error: Core  0 panic'ed (IllegalInstruction).\n" + sample()
        self.assertEqual(MOD.first_load_prohibited(log)["core"], 1)

    def test_does_not_invent_panic(self):
        self.assertIsNone(MOD.first_load_prohibited("ESP-ROM booted normally"))

    def test_matching_hash_allows_symbolication(self):
        with tempfile.TemporaryDirectory() as directory:
            elf = Path(directory) / "firmware.elf"
            elf.write_bytes(b"isolated synthetic ELF")
            prefix = hashlib.sha256(elf.read_bytes()).hexdigest()[:9]
            report, code = MOD.build_report(sample(sha=prefix), elf)
            self.assertEqual(code, 0)
            self.assertTrue(report["elf_identity_verified"])

    def test_wrong_elf_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            elf = Path(directory) / "firmware.elf"
            elf.write_bytes(b"wrong build")
            report, code = MOD.build_report(sample(), elf)
            self.assertEqual(code, 2)
            self.assertFalse(report["elf_identity_verified"])
            self.assertNotIn("symbolication", report)

    def test_missing_hash_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            elf = Path(directory) / "firmware.elf"
            elf.write_bytes(b"some build")
            log = sample().replace("ELF file SHA256: 98ffc195c", "")
            report, code = MOD.build_report(log, elf)
            self.assertEqual(code, 2)
            self.assertIn("refused", report["error"])

    def test_clean_boot_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            elf = Path(directory) / "firmware.elf"
            elf.write_bytes(b"fixture")
            report, code = MOD.build_report("no panic", elf)
            self.assertEqual(code, 1)
            self.assertIn("error", report)


if __name__ == "__main__":
    unittest.main()
