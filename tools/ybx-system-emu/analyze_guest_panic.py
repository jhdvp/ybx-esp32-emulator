#!/usr/bin/env python3
"""Correlate ESP-IDF UART panic addresses with actual factory IROM segments."""
from pathlib import Path
import argparse
import json
import re

from analyze_qemu_trace import app_irom_ranges

PANIC_PC = re.compile(r"(?m)^\s*(?:MEPC|PC)\s*:\s*(0x[0-9a-fA-F]{8})\b")
FAULT_ADDRESS = re.compile(r"\b(?:EXCVADDR|MTVAL)\s*:\s*(0x[0-9a-fA-F]{8})\b")


def analyze(factory: Path, uart_log: Path) -> dict:
    ranges = app_irom_ranges(factory)
    data = uart_log.read_text(encoding="utf-8", errors="replace")
    pcs = [int(addr, 16) for addr in PANIC_PC.findall(data)]
    faults = [int(addr, 16) for addr in FAULT_ADDRESS.findall(data)]
    guest_panic = "Guru Meditation Error" in data
    return {
        "guest_panic_detected": guest_panic,
        "panic_pc_count": len(pcs),
        "panic_pcs": [f"0x{x:08x}" for x in pcs],
        "guest_fault_addresses": [f"0x{x:08x}" for x in faults],
        "panic_pc_in_factory_app_irom": guest_panic and bool(pcs) and all(
            any(start <= pc < end for start, end in ranges) for pc in pcs
        ),
        "app_irom_ranges": [
            {"start": f"0x{start:08x}", "end_exclusive": f"0x{end:08x}"}
            for start, end in ranges
        ],
        "exact_elf_symbols_verified": False,
        "stable_app_ready": False,
    }


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--factory-image", type=Path, required=True)
    p.add_argument("--uart-log", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    args = p.parse_args()
    result = analyze(args.factory_image, args.uart_log)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
