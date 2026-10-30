"""Validate DROM mirror reconstruction without shipping Espressif ROM bytes."""
from pathlib import Path
from tempfile import TemporaryDirectory
import struct
import unittest

from extract_rom_elf import ROM_SIZE
from extract_rom_interface import overlay_drom_interface


def create_rom_elf(path: Path, *, corrupt_pointer=False, missing=False):
    blob = bytearray(0x300)
    blob[:6] = b"\x7fELF\x01\x01"
    struct.pack_into("<I", blob, 32, 0x80)
    struct.pack_into("<HHH", blob, 46, 40, 3, 2)
    names = b"\x00.rodata.interface\x00.shstrtab\x00"
    blob[0x170:0x170+len(names)] = names
    struct.pack_into("<IIIIIIIIII", blob, 0x80 + 2*40,
                     19, 3, 0, 0, 0x170, len(names), 0, 0, 1, 0)
    section_name = 0 if missing else 1
    struct.pack_into("<IIIIIIIIII", blob, 0x80 + 40,
                     section_name, 1, 1, 0x3FF1FFF0, 0x200,
                     16, 0, 0, 4, 0)
    struct.pack_into("<I", blob, 0x20c,
                     0 if corrupt_pointer else 0x3FF1AE90)
    path.write_bytes(blob)


class DromInterfaceTests(unittest.TestCase):
    def test_overlay_restores_layout_pointer(self):
        with TemporaryDirectory() as tmp:
            elf = Path(tmp)/"rom.elf"
            create_rom_elf(elf)
            rom = bytearray(b"\xff"*ROM_SIZE)
            self.assertEqual(overlay_drom_interface(rom, elf), 16)
            self.assertEqual(struct.unpack_from("<I",rom,0x5fffc)[0],
                             0x3FF1AE90)

    def test_bad_layout_pointer_is_rejected(self):
        with TemporaryDirectory() as tmp:
            elf=Path(tmp)/"rom.elf"
            create_rom_elf(elf,corrupt_pointer=True)
            with self.assertRaisesRegex(ValueError,"layout pointer"):
                overlay_drom_interface(bytearray(ROM_SIZE),elf)

    def test_missing_interface_is_rejected(self):
        with TemporaryDirectory() as tmp:
            elf=Path(tmp)/"rom.elf"
            create_rom_elf(elf,missing=True)
            with self.assertRaisesRegex(ValueError,"missing .rodata.interface"):
                overlay_drom_interface(bytearray(ROM_SIZE),elf)

    def test_unexpected_rom_length_rejected(self):
        with TemporaryDirectory() as tmp:
            elf=Path(tmp)/"rom.elf"
            create_rom_elf(elf)
            with self.assertRaisesRegex(ValueError,"unexpected ESP ROM"):
                overlay_drom_interface(bytearray(128),elf)


if __name__ == "__main__":
    unittest.main()
