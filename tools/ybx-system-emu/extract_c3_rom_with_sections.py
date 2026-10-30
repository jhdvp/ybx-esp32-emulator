#!/usr/bin/env python3
"""Restore ESP32-C3 rev3 ROM reset-copy sources from Espressif's local ROM ELF.

No hardware access or production firmware changes. QEMU test BIOS only.
"""
from pathlib import Path
import argparse
import hashlib
import struct

from extract_rom_elf import extract as extract_irom, ROM_BASE, ROM_SIZE
from extract_rom_data import extract_range
from extract_rom_interface import overlay_drom_interface

DRAM_BASE = 0x3FCCAE00
DRAM_SIZE = 0x15200
LEGACY_POINTER = 0x3FCDFFF0
COPY_BEGIN = 0x40059200
COPY_END = 0x40059400
LEGACY_SOURCE = 0x40059658


def patch_sources(rom: bytearray, initializers: bytes) -> int:
    if len(rom) != ROM_SIZE or len(initializers) != DRAM_SIZE:
        raise ValueError("invalid ROM or DRAM initializer size")
    count = 0
    for addr in range(COPY_BEGIN, COPY_END, 16):
        dst, dst_end, src, flags = struct.unpack_from(
            "<IIII", rom, addr - ROM_BASE)
        if dst_end < dst or flags != 0:
            raise ValueError("invalid C3 reset-copy descriptor")
        length = dst_end - dst
        if not length:
            continue
        if (dst < DRAM_BASE or length > DRAM_SIZE - (dst - DRAM_BASE) or
                src < ROM_BASE or length > ROM_SIZE - (src - ROM_BASE)):
            raise ValueError("C3 reset-copy descriptor outside expected range")
        rom[src - ROM_BASE:src - ROM_BASE + length] = (
            initializers[dst - DRAM_BASE:dst - DRAM_BASE + length])
        count += 1
    pointer = struct.unpack_from("<I", rom, LEGACY_SOURCE - ROM_BASE)[0]
    if count < 5 or pointer != 0x3FCDF5C0:
        raise ValueError("C3 SPI flash legacy pointer missing or wrong ROM rev")
    return count


def extract(elf: Path) -> tuple[bytes, int]:
    rom = bytearray(extract_irom(elf))
    initializers = extract_range(elf, DRAM_BASE, DRAM_SIZE, LEGACY_POINTER)
    count = patch_sources(rom, initializers)
    overlay_drom_interface(rom, elf)
    return bytes(rom), count


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--elf", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    args = p.parse_args()
    rom, count = extract(args.elf)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_bytes(rom)
    print(f"C3 ROM: {len(rom)} bytes, {count} reset-copy sources")
    print(f"SHA256={hashlib.sha256(rom).hexdigest()}")
    print(f"Local-only QEMU BIOS: {args.out}")


if __name__ == "__main__":
    main()
