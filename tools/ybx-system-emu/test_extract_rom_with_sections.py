"""Verify the ESP32-S3 ROM reset-copy-source reconstruction."""
from pathlib import Path
import struct
import unittest

from extract_rom_data import BASE, LEGACY_POINTER, SIZE
from extract_rom_elf import ROM_BASE, ROM_SIZE
from extract_rom_with_sections import LEGACY_SOURCE, patch_reset_copy_sources


class RomCopySourceTests(unittest.TestCase):
    def make_data(self):
        rom = bytearray(ROM_SIZE)
        source = ROM_BASE + 0x1000
        struct.pack_into("<II", rom, 0x428, source, source + 32)
        struct.pack_into("<IIII", rom, source - ROM_BASE,
                         LEGACY_POINTER, LEGACY_POINTER + 4, LEGACY_SOURCE, 0)
        struct.pack_into("<IIII", rom, source - ROM_BASE + 16,
                         BASE + 0x100, BASE + 0x104, ROM_BASE + 0x2000, 0)
        dram = bytearray(SIZE)
        struct.pack_into("<I", dram, LEGACY_POINTER - BASE, BASE + 0x400)
        dram[0x100:0x104] = b"ABCD"
        return rom, dram

    def test_copy_sources_reproduce_rom_reset_initializers(self):
        rom, dram = self.make_data()
        self.assertEqual(patch_reset_copy_sources(rom, dram), 2)
        self.assertEqual(
            rom[LEGACY_SOURCE - ROM_BASE:LEGACY_SOURCE - ROM_BASE + 4],
            struct.pack("<I", BASE + 0x400))
        self.assertEqual(rom[0x2000:0x2004], b"ABCD")

    def test_invalid_copy_table_rejected(self):
        rom, dram = self.make_data()
        struct.pack_into("<I", rom, 0x42c, ROM_BASE + ROM_SIZE + 16)
        with self.assertRaisesRegex(ValueError, "reset-copy table"):
            patch_reset_copy_sources(rom, dram)

    def test_out_of_rom_source_rejected(self):
        rom, dram = self.make_data()
        struct.pack_into("<I", rom, 0x1000 + 8, ROM_BASE + ROM_SIZE - 1)
        with self.assertRaisesRegex(ValueError, "outside approved"):
            patch_reset_copy_sources(rom, dram)

    def test_invalid_legacy_pointer_rejected(self):
        rom, dram = self.make_data()
        struct.pack_into("<I", dram, LEGACY_POINTER - BASE, 0)
        with self.assertRaisesRegex(ValueError, "legacy SPI flash"):
            patch_reset_copy_sources(rom, dram)

    def test_unexpected_image_size_rejected(self):
        _, dram = self.make_data()
        with self.assertRaisesRegex(ValueError, "unexpected instruction"):
            patch_reset_copy_sources(bytearray(40), dram)


if __name__ == "__main__":
    unittest.main()
