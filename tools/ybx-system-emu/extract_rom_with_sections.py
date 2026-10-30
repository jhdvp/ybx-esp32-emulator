#!/usr/bin/env python3
"""Reconstruct the ESP32-S3 ROM *copy-source* data from a local ROM ELF.

ESP32-S3's reset vector copies tables of initialized data from ROM to DRAM.
Some Espressif ROM ELFs store the authoritative writable section payloads
outside the physical ROM PT_LOAD segments. Filling DRAM alone is insufficient:
the ROM reset routine overwrites it from the (otherwise empty) ROM sources.
"""
from pathlib import Path
import argparse
import hashlib
import struct

from extract_rom_elf import extract as extract_irom, ROM_BASE, ROM_SIZE
from extract_rom_data import extract as extract_dram, BASE, SIZE
from extract_rom_interface import overlay_drom_interface

COPY_TABLE_START_ADDR = ROM_BASE + 0x428
COPY_TABLE_END_ADDR = ROM_BASE + 0x42c
LEGACY_SOURCE = 0x40057874


def patch_reset_copy_sources(rom: bytearray, initialized_ram: bytes) -> int:
    if len(rom) != ROM_SIZE or len(initialized_ram) != SIZE:
        raise ValueError("unexpected instruction ROM or initializer size")
    start, end = struct.unpack_from("<II", rom, COPY_TABLE_START_ADDR - ROM_BASE)
    if (start < ROM_BASE or end > ROM_BASE + ROM_SIZE or end <= start or
            (end - start) % 16 != 0 or (end - start) // 16 > 128):
        raise ValueError("unexpected ESP32-S3 ROM reset-copy table")
    copied = 0
    for addr in range(start, end, 16):
        dst, dst_end, source, core = struct.unpack_from(
            "<IIII", rom, addr - ROM_BASE)
        if dst_end < dst or core not in (0, 1):
            raise ValueError("invalid ROM copy descriptor")
        length = dst_end - dst
        if length == 0:
            continue
        if (dst < BASE or length > BASE + SIZE - dst or
                source < ROM_BASE or length > ROM_BASE + ROM_SIZE - source):
            raise ValueError("ROM copy descriptor outside approved ranges")
        rom[source - ROM_BASE:source - ROM_BASE + length] = (
            initialized_ram[dst - BASE:dst - BASE + length])
        copied += 1
    pointer = struct.unpack_from("<I", rom, LEGACY_SOURCE - ROM_BASE)[0]
    if copied < 2 or not BASE <= pointer < BASE + SIZE:
        raise ValueError("ROM legacy SPI flash copy-source not initialized")
    return copied


def extract(path: Path) -> tuple[bytes, int]:
    instruction_rom = bytearray(extract_irom(path))
    count = patch_reset_copy_sources(instruction_rom, extract_dram(path))
    overlay_drom_interface(instruction_rom, path)
    return bytes(instruction_rom), count


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--elf", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    data, count = extract(args.elf)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_bytes(data)
    print(f"Populated {count} ROM reset-copy sources; {len(data)} bytes")
    print(f"SHA256={hashlib.sha256(data).hexdigest()}")
    print(f"Local-only test ROM: {args.out}")


if __name__ == "__main__":
    main()
