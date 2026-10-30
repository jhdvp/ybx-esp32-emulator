#!/usr/bin/env python3
"""Restore ESP32-C3/S3 DROM interface payload omitted from ROM PT_LOAD images.

Only extracts writable PROGBITS from the matching *local* Espressif ROM ELF.
The DROM window aliases the final 128 KiB of instruction ROM in QEMU.
"""
from pathlib import Path
import struct

from extract_rom_elf import ROM_SIZE

DROM_BASE = 0x3FF00000
DROM_SIZE = 0x20000
DROM_ALIAS_OFFSET = 0x40000
LAYOUT_POINTER_ADDR = 0x3FF1FFFC


def overlay_drom_interface(rom: bytearray, elf: Path) -> int:
    if len(rom) != ROM_SIZE:
        raise ValueError("unexpected ESP ROM length")
    data = elf.read_bytes()
    if len(data) < 52 or data[:6] != b"\x7fELF\x01\x01":
        raise ValueError("expected little-endian ELF32")
    shoff = struct.unpack_from("<I", data, 32)[0]
    ent, n, names_i = struct.unpack_from("<HHH", data, 46)
    if (ent != 40 or not 1 <= n <= 256 or names_i >= n or
            shoff + n * ent > len(data)):
        raise ValueError("invalid ELF section table")
    names = struct.unpack_from("<IIIIIIIIII", data, shoff + names_i * ent)
    if names[4] > len(data) or names[5] > len(data) - names[4]:
        raise ValueError("invalid ELF section names")
    strings = data[names[4]:names[4] + names[5]]
    written = 0
    for i in range(n):
        name_id, kind, flags, addr, src, size, *_ = struct.unpack_from(
            "<IIIIIIIIII", data, shoff + i * ent)
        if name_id >= len(strings):
            raise ValueError("invalid ELF section name offset")
        end = strings.find(b"\x00", name_id)
        if end < 0:
            raise ValueError("unterminated ELF section name")
        if strings[name_id:end] != b".rodata.interface":
            continue
        if (written or kind != 1 or not flags & 1 or
                addr < DROM_BASE or addr >= DROM_BASE + DROM_SIZE or
                size != DROM_BASE + DROM_SIZE - addr or
                src > len(data) or size > len(data) - src):
            raise ValueError("invalid DROM interface section")
        offset = DROM_ALIAS_OFFSET + addr - DROM_BASE
        rom[offset:offset + size] = data[src:src + size]
        written = size
    if not written:
        raise ValueError("missing .rodata.interface from ROM ELF")
    ptr_offset = DROM_ALIAS_OFFSET + LAYOUT_POINTER_ADDR - DROM_BASE
    pointer = struct.unpack_from("<I", rom, ptr_offset)[0]
    if not DROM_BASE <= pointer < DROM_BASE + DROM_SIZE:
        raise ValueError("ROM layout pointer invalid after DROM reconstruction")
    return written
