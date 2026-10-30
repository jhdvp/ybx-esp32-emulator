"""Portable checks for panic address / factory application mapping."""
import struct
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from analyze_guest_panic import analyze


class GuestPanicEvidenceTests(unittest.TestCase):
    def make_factory(self, root: Path) -> Path:
        data = bytearray(b"\xff" * 0x10200)
        data[0x10000] = 0xe9
        data[0x10001] = 1
        struct.pack_into("<II", data, 0x10018, 0x42000020, 0x100)
        path = root / "factory.bin"
        path.write_bytes(data)
        return path

    def test_app_panic_inside_irom_range(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            image = self.make_factory(root)
            log = root / "uart.log"
            log.write_text(
                "Guru Meditation Error: Core 0 panic'ed (Load access fault)\n"
                "MEPC    : 0x42000080  RA : 0x42000060\n"
                "MTVAL   : 0x00000003\n")
            result = analyze(image, log)
            self.assertTrue(result["guest_panic_detected"])
            self.assertTrue(result["panic_pc_in_factory_app_irom"])
            self.assertEqual(result["guest_fault_addresses"], ["0x00000003"])
            self.assertFalse(result["stable_app_ready"])

    def test_rom_panic_is_not_an_app_panic(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            image = self.make_factory(root)
            log = root / "uart.log"
            log.write_text("Guru Meditation Error\nPC : 0x4004cc4e\n"
                           "EXCVADDR: 0x0000001b\n")
            result = analyze(image, log)
            self.assertFalse(result["panic_pc_in_factory_app_irom"])
            self.assertEqual(result["panic_pc_count"], 1)

    def test_no_panic_not_accepted(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            image = self.make_factory(root)
            log = root / "uart.log"
            log.write_text("ESP-ROM:esp32s3-20210327\nentry 0x403c8898\n")
            result = analyze(image, log)
            self.assertFalse(result["guest_panic_detected"])
            self.assertFalse(result["panic_pc_in_factory_app_irom"])


if __name__ == "__main__":
    unittest.main()
