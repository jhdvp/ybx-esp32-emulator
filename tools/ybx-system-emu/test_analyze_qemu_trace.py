"""Regression tests for real-app segment correlation in QEMU traces."""
from pathlib import Path
import struct
from tempfile import TemporaryDirectory
import unittest

from analyze_qemu_trace import analyze, app_irom_ranges


class TraceEvidenceTests(unittest.TestCase):
    def factory(self, directory):
        image = bytearray(bytes((255,)) * 0x10200)
        image[0x10000] = 0xe9
        image[0x10001] = 2
        struct.pack_into("<II", image, 0x10018, 0x42001000, 0x30)
        struct.pack_into("<II", image, 0x10018 + 8 + 0x30,
                         0x40374000, 0x20)
        path = Path(directory) / "factory.bin"
        path.write_bytes(image)
        return path

    def test_real_app_region_and_traced_instructions(self):
        with TemporaryDirectory() as temp:
            ranges = app_irom_ranges(self.factory(temp))
            self.assertEqual(ranges, [(0x42001000, 0x42001030)])
            trace = Path(temp) / "trace"
            trace.write_text(
                "0x40050f3f: j 0x40050f3f\n"
                "0x403cda8e: s32i.n\n"
                "0x42001002: entry\n"
                "0x42001015: retw.n\n"
                "0x42001040: invalid other segment\n")
            evidence = analyze(trace, ranges)
            self.assertTrue(evidence["app_irom_reached"])
            self.assertEqual(evidence["app_irom_instruction_records"], 2)
            self.assertEqual(evidence["app_irom_min_pc"], "0x42001002")
            self.assertEqual(evidence["app_irom_max_pc"], "0x42001015")
            self.assertTrue(evidence["prior_rom_fatal_loop_seen"])
            self.assertTrue(evidence["rom_to_iram_seen"])
            self.assertFalse(evidence["application_ready_verified"])

    def test_no_app_segment_pc_does_not_claim_execution(self):
        with TemporaryDirectory() as temp:
            trace = Path(temp) / "trace"
            trace.write_text("0x42008000: nop\n0x403c1234: nop\n")
            result = analyze(trace, [(0x42001000, 0x42001030)])
            self.assertFalse(result["app_irom_reached"])
            self.assertIsNone(result["app_irom_first_pc"])

    def test_truncated_segments_are_rejected(self):
        with TemporaryDirectory() as temp:
            factory = self.factory(temp)
            factory.write_bytes(factory.read_bytes()[:0x10022])
            with self.assertRaisesRegex(ValueError, "truncated ESP32 segment"):
                app_irom_ranges(factory)

    def test_no_application_image_is_rejected(self):
        with TemporaryDirectory() as temp:
            factory = self.factory(temp)
            data = bytearray(factory.read_bytes())
            data[0x10000] = 0
            factory.write_bytes(data)
            with self.assertRaisesRegex(ValueError, "factory image"):
                app_irom_ranges(factory)


if __name__ == "__main__":
    unittest.main()
