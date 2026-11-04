#!/usr/bin/env python3
"""Symbolicate ESP32-S3 inkMOD panics only against the matching ELF.

A same-version rebuilt image is NOT a trustworthy map for original firmware.
The ESP-IDF panic's ELF SHA-256 prefix validates the chosen image.
No firmware, QEMU process or image is modified.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

PANIC_RE = re.compile(r"Guru Meditation Error: Core\s+(\d+)\s+panic'ed\s+\(([^)]+)\)")
ELF_RE = re.compile(r"ELF file SHA256:\s*([0-9a-fA-F]{8,64})")
PC_RE = re.compile(r"\bPC\s*:\s*(0x[0-9a-fA-F]+)")
FAULT_RE = re.compile(r"\bEXCVADDR\s*:\s*(0x[0-9a-fA-F]+)")
BACKTRACE_RE = re.compile(r"Backtrace:\s*([^\r\n]+)")


def first_load_prohibited(text: str) -> dict[str, object] | None:
    """Select the first LoadProhibited, never a secondary BREAK exception."""
    matches = list(PANIC_RE.finditer(text))
    for index, match in enumerate(matches):
        if match.group(2) != "LoadProhibited":
            continue
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        reboot = text.find("Rebooting...", match.end(), end)
        if reboot >= 0:
            end = reboot
        block = text[match.start():end]
        pc = PC_RE.search(block)
        fault = FAULT_RE.search(block)
        trace = BACKTRACE_RE.search(block)
        elf = ELF_RE.search(block)
        return {
            "core": int(match.group(1)),
            "exception": "LoadProhibited",
            "pc": pc.group(1).lower() if pc else None,
            "excvaddr": fault.group(1).lower() if fault else None,
            "backtrace": (
                [a.lower() for a in re.findall(r"(0x[0-9a-fA-F]+):0x[0-9a-fA-F]+", trace.group(1))]
                if trace else []
            ),
            "elf_sha256_prefix": elf.group(1).lower() if elf else None,
        }
    return None


def elf_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_report(text: str, elf: Path, addr2line: str | None = None) -> tuple[dict[str, object], int]:
    panic = first_load_prohibited(text)
    if panic is None:
        return {"error": "No LoadProhibited panic was found"}, 1
    sha = elf_sha256(elf)
    marker = panic["elf_sha256_prefix"]
    report: dict[str, object] = {
        **panic,
        "elf_sha256": sha,
        "elf_identity_verified": bool(marker and sha.startswith(str(marker))),
    }
    if not report["elf_identity_verified"]:
        report["error"] = "ELF mismatch or missing panic ELF hash; symbolication refused"
        return report, 2
    addresses = list(dict.fromkeys(
        addr for addr in [panic["pc"], *panic["backtrace"]] if isinstance(addr, str)
    ))
    if addr2line is not None and addresses:
        result = subprocess.run(
            [addr2line, "-pfiaC", "-e", str(elf), *addresses],
            capture_output=True, text=True, check=True, timeout=30,
        )
        report["symbolication"] = result.stdout.strip().splitlines()
    return report, 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--uart", type=Path, required=True)
    parser.add_argument("--elf", type=Path, required=True)
    parser.add_argument("--addr2line", help="Xtensa toolchain addr2line executable")
    args = parser.parse_args(argv)
    try:
        report, status = build_report(args.uart.read_text(errors="replace"), args.elf, args.addr2line)
    except (OSError, subprocess.SubprocessError) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 3
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return status


if __name__ == "__main__":
    sys.exit(main())
