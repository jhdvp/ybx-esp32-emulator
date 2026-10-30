"""Regression checks for ESP32-S3 writable ROM initializer extraction."""
from pathlib import Path
from tempfile import TemporaryDirectory
import struct
import unittest

from extract_rom_data import BASE, LEGACY_POINTER, SIZE, extract


def make_elf(path: Path, *, overlap: bool = False, flags: int = 1) -> None:
    data = bytearray(0x400)
    data[:6] = bytes((127, 69, 76, 70, 1, 1))
    struct.pack_into("<I", data, 32, 0x80)
    struct.pack_into("<HH", data, 46, 40, 2)
    pointer = struct.pack("<I", BASE + 0x400)
    data[0x200:0x204] = pointer
    data[0x210:0x214] = b"ABCD"
    struct.pack_into("<IIIIIIIIII", data, 0x80, 0, 1, flags,
                     LEGACY_POINTER, 0x200, 4, 0, 0, 4, 0)
    struct.pack_into("<IIIIIIIIII", data, 0xa8, 0, 1, 1,
                     LEGACY_POINTER if overlap else BASE + 0x100,
                     0x210, 4, 0, 0, 4, 0)
    path.write_bytes(data)


class RomDataTests(unittest.TestCase):
    def test_writable_sections_without_alloc_flag(self):
        with TemporaryDirectory() as temp:
            path = Path(temp) / "rom.elf"
            make_elf(path)
            image = extract(path)
            self.assertEqual(len(image), SIZE)
            self.assertEqual(image[LEGACY_POINTER - BASE:LEGACY_POINTER - BASE + 4],
                             struct.pack("<I", BASE + 0x400))
            self.assertEqual(image[0x100:0x104], b"ABCD")

    def test_missing_writable_section_rejected(self):
        with TemporaryDirectory() as temp:
            path = Path(temp) / "rom.elf"
            make_elf(path, flags=0)
            with self.assertRaisesRegex(ValueError, "initializer"):
                extract(path)

    def test_conflicting_sections_rejected(self):
        with TemporaryDirectory() as temp:
            path = Path(temp) / "rom.elf"
            make_elf(path, overlap=True)
            with self.assertRaisesRegex(ValueError, "overlapping"):
                extract(path)

    def test_invalid_header_rejected(self):
        with TemporaryDirectory() as temp:
            path = Path(temp) / "rom.elf"
            path.write_bytes(bytes(70))
            with self.assertRaisesRegex(ValueError, "ELF32"):
                extract(path)


if __name__ == "__main__":
    unittest.main()
