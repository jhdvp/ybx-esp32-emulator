"""Synthetic portable checks for C3 rev3 ROM reset-copy source repair."""
from pathlib import Path
from tempfile import TemporaryDirectory
import struct
import unittest

from extract_rom_elf import ROM_BASE, ROM_SIZE
from extract_c3_rom_with_sections import (
    DRAM_BASE, DRAM_SIZE, LEGACY_POINTER, LEGACY_SOURCE, patch_sources,
)


class C3ResetCopyTests(unittest.TestCase):
    def samples(self):
        rom = bytearray(bytes((255,)) * ROM_SIZE)
        init = bytearray(DRAM_SIZE)
        # Required 5 distinct initialized copy descriptors.
        for j in range(5):
            dst = DRAM_BASE + 0x20 + j * 4
            src = ROM_BASE + 0x59000 + j * 4
            struct.pack_into("<IIII", rom, 0x59200 + j * 16,
                             dst, dst + 4, src, 0)
            struct.pack_into("<I", init, dst - DRAM_BASE, j + 10)
        # Other descriptors have zero lengths and must be ignored.
        for j in range(5, 32):
            struct.pack_into("<IIII", rom, 0x59200 + j * 16,
                             DRAM_BASE + 0x100, DRAM_BASE + 0x100,
                             ROM_BASE + 0x59400, 0)
        struct.pack_into("<IIII", rom, 0x59200,
                         LEGACY_POINTER, LEGACY_POINTER + 4,
                         LEGACY_SOURCE, 0)
        struct.pack_into("<I", init, LEGACY_POINTER - DRAM_BASE, 0x3FCDF5C0)
        return rom, init

    def test_expected_pointer_copied_to_rom_source(self):
        rom, init = self.samples()
        self.assertEqual(patch_sources(rom, init), 5)
        self.assertEqual(struct.unpack_from("<I", rom, LEGACY_SOURCE-ROM_BASE)[0],
                         0x3FCDF5C0)
        self.assertEqual(struct.unpack_from("<I", rom, 0x59004)[0], 11)

    def test_bad_pointer_rejected(self):
        rom, init = self.samples()
        struct.pack_into("<I", init, LEGACY_POINTER-DRAM_BASE, 0)
        with self.assertRaisesRegex(ValueError, "legacy pointer"):
            patch_sources(rom, init)

    def test_invalid_rom_copy_address_rejected(self):
        rom, init = self.samples()
        struct.pack_into("<I", rom, 0x59200 + 8, ROM_BASE + ROM_SIZE - 1)
        with self.assertRaisesRegex(ValueError, "outside expected range"):
            patch_sources(rom, init)

    def test_invalid_descriptor_flags_rejected(self):
        rom, init = self.samples()
        struct.pack_into("<I", rom, 0x59200 + 12, 1)
        with self.assertRaisesRegex(ValueError, "invalid C3 reset-copy"):
            patch_sources(rom, init)

    def test_wrong_initializer_length_rejected(self):
        rom, init = self.samples()
        with self.assertRaisesRegex(ValueError, "invalid ROM or DRAM"):
            patch_sources(rom, init[:-1])


if __name__ == "__main__":
    unittest.main()
