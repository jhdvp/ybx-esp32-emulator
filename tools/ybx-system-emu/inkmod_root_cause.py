#!/usr/bin/env python3
"""Turn the inkMOD v1.1.10 stop signature into a testable root-cause statement.

Two host-only halves, both runnable without Docker or QEMU:

``analyze`` reads the shipped inkMOD flash image and the live QEMU tree
(read-only) and answers, from bytes and source, what the emulator owes the
guest on the observed path:

* which loaded image segment each tracer PC belongs to, so the
  ``rtc_sleep_start`` / ``ipc_isr_wait`` signature is attributed to the image
  instead of taken on faith;
* which RTC_CNTL offsets the S3 model defines, services on reads, services on
  writes, and leaves silently at zero;
* whether the RTC interrupt output is connected to anything at all;
* whether the cross-core FROM_CPU chain exists end to end, which is what
  demotes "the emulator lost the IPI" from a candidate cause to a runtime
  check against counters that already ship.

``variant`` builds the falsification experiment for the top-ranked hypothesis.
CPU1 spins on ``RTC_CNTL_INT_RAW`` bits SLP_WAKEUP(0)/SLP_REJECT(1) and the
model returns 0 for that offset forever, so the loop can only end if the value
the guest loads is nonzero. Instead of patching QEMU (forbidden) the tool
rewrites one literal-pool word in a *copy* of the flash image so the poll reads
an RTC offset the model does service, and repairs both integrity guards that
stand between that edit and the guest's sleep path: the XOR checksum byte and
the appended SHA-256. Which offset to redirect to is measured by
``inkmod_forced_wake.py probe``, not assumed from the register list. If the
wedge dissolves, the missing RTC sleep-wake status is the binding constraint; if
both cores stay parked where they were, the sleep poll is not what holds the
boot and the IPC/IPI side moves to the top of the list.

Executed on 2026-11-01 in a throwaway ``ybx-qemu-builder:20261029`` container:
redirecting the poll to RTC_CNTL_TIME0 took inkMOD from "0 panel commands,
423016 poll visits" to "50 commands, 2 frames, 192000 RAM writes", so H1 is
CONFIRMED. See ``experiments/0093-inkmod-stop-analysis-20261101.md``; guest
numbers in that report all come from the ``report.json`` files the tracer wrote.

Usage:

    python3 inkmod_root_cause.py analyze \
        --flash .../firmware/inkmod-v1.1.10-x4pro-flash-16m.bin \
        --tree ~/.local/state/ybx-emulator-upstream/qemu-wf-radio-20261031 \
        --out /tmp/inkmod-cause.json

    python3 inkmod_root_cause.py variant --flash .../inkmod-...bin \
        --out-dir /tmp/inkmod-variant --literal-vaddr 0x40374b7c

    python3 inkmod_forced_wake.py experiment --qemu ... --rom ... \
        --flash .../inkmod-...bin --out /tmp/ybx-0093/five
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

DEFAULT_TREE = Path(
    "/Users/yaraslauhancharou/.local/state/ybx-emulator-upstream/"
    "qemu-wf-radio-20261031")
DEFAULT_FLASH = Path(
    "/Users/yaraslauhancharou/Projects/YBX/artifacts/"
    "ybx-esp32-system-emulation-20261029/firmware/"
    "inkmod-v1.1.10-x4pro-flash-16m.bin")

APP_OFFSET = 0x10000
IMAGE_MAGIC = 0xE9
HEADER_BYTES = 24
SEGMENT_HEADER_BYTES = 8
HASH_BYTES = 32

RTC_BASE = 0x60008000
# Offset 0x44 is what the tracer reports CPU1 polling. The substitute is the
# offset the guest was measured reading as nonzero (RTC_CNTL_TIME0, 0x60008010
# = 501501 in the 2026-11-01 probe pass); RESET_STATE and the scratch registers
# are kept in WAKE_SUBSTITUTES as fallbacks. The int-group names come from the
# sibling ESP32-C3 RTC header in the same tree
# (include/hw/misc/esp32c3_rtc_cntl.h:157-199: INT_ENA 0x40, INT_RAW 0x44,
# INT_ST 0x48, INT_CLR 0x4C, SLP_WAKEUP bit 0, SLP_REJECT bit 1); the ESP32-S3
# RTC register list itself is not on this disk (no IDF checkout, no ELF, no
# S3 soc header), so offsets the tracer samples but the tree never names stay
# "tracer-probed".
RTC_INT_GROUP = (0x40, 0x44, 0x48, 0x4C)
RTC_TOUCHED = {
    0x00: "OPTIONS0 (sleep/power-down config the guest writes)",
    0x38: "RESET_STATE (modelled; reset cause fields)",
    0x40: "INT_ENA (named from esp32c3_rtc_cntl.h)",
    0x44: "INT_RAW (CPU1's poll target, SLP_WAKEUP bit 0 / SLP_REJECT bit 1)",
    0x48: "INT_ST (named from esp32c3_rtc_cntl.h)",
    0x4C: "INT_CLR (named from esp32c3_rtc_cntl.h)",
    0x04: "tracer-probed, unnamed in the tree",
    0x08: "tracer-probed, unnamed in the tree",
    0x10: "TIME0 (named in esp32s3_rtc_cntl.h, low half of the RTC counter)",
    0x14: "TIME1 (named in esp32s3_rtc_cntl.h, high half of the RTC counter)",
    0x18: "tracer-probed, unnamed in the tree",
    0x1C: "tracer-probed, unnamed in the tree",
    0x20: "tracer-probed, unnamed in the tree",
    0x3C: "tracer-probed, unnamed in the tree",
    0x50: "STORE0 (named in esp32s3_rtc_cntl.h, scratch register)",
    0x54: "STORE1 (named in esp32s3_rtc_cntl.h, scratch register)",
}
FORCED_WAKE_OFFSET = 0x10
# RTC offsets worth substituting for INT_RAW, in the order the experiment
# should pick them: a free-running counter satisfies any wake mask, a register
# that merely reads nonzero satisfies the low bits IDF masks SLP_WAKEUP/SLP_REJECT
# into. Which of them the model actually returns nonzero for is measured by
# inkmod_forced_wake.py probe, not assumed here.
WAKE_SUBSTITUTES = (
    (0x10, "TIME0 (modelled, free-running counter)"),
    (0x14, "TIME1 (modelled, free-running counter)"),
    (0x38, "RESET_STATE (modelled, reset cause fields)"),
    (0x50, "STORE0 (modelled, scratch register)"),
)

# PCs reported by probe_x4pro_inkmod_trace.py for the inkMOD 1.1.10 boot. The
# names are the previous lane's; attributing them to a loaded segment is what
# this module measures.
SIGNATURE_PCS = {
    "deep_sleep_wrapper": 0x40377428,
    "rtc_int_raw_poll": 0x4037CB2C,
    "rtc_sleep_start_range_start": 0x4037CAB8,
    "ipc_isr_wait": 0x4037A4A8,
    "esp_sleep_start": 0x4214D240,
    "inkmod_config_wakeup_word": 0x3FC99E60 + 0x40,
    "inkmod_ipc_finish_cmd": 0x3FCA2908 + 0x04,
}

L32R_MIN_BYTES = -32768
L32R_MAX_BYTES = 32764

RTC_HEADER = Path("include/hw/misc/esp32s3_rtc_cntl.h")
RTC_SOURCE = Path("hw/misc/esp32s3_rtc_cntl.c")
C3_RTC_HEADER = Path("include/hw/misc/esp32c3_rtc_cntl.h")
SOC_SOURCE = Path("hw/xtensa/esp32s3.c")
CLOCK_SOURCE = Path("hw/xtensa/esp32s3_clk.c")
INTC_SOURCE = Path("hw/xtensa/esp32s3_intc.c")
PIC_SOURCE = Path("hw/xtensa/pic_cpu.c")
REG_HEADER = Path("include/hw/misc/esp32s3_reg.h")

CONTAINER = "ybx-qemu-builder:20261029"
DEFAULT_SD = Path(
    "/Users/yaraslauhancharou/Projects/YBX/artifacts/"
    "ybx-esp32-system-emulation-20261029/qa-20261029/"
    "ybx-test-sd-fat16-mbr-pristine.img")
# The generated complete ROM, not the vendor 384 KiB image that ships in the
# build dir: measured 2026-11-01, with build-ybx/esp32s3_rev0_rom.bin
# (5107c580…) every one of factory reference, inkMOD and the forced-wake copy
# dies in ROM with an identical 824-byte LoadProhibited panic (PC 0x400494ff,
# EXCVADDR 0x0000001b), so no app code runs and the sleep poll is never
# reached. boot_matrix.py:62 and panel_matrix.py:600 use this same file.
DEFAULT_ROM = Path(
    "/Users/yaraslauhancharou/Projects/YBX/artifacts/"
    "ybx-esp32-system-emulation-20261029/roms/"
    "s3-full-rom-generator-test.bin")
TRACER = Path(__file__).resolve().parent / "probe_x4pro_inkmod_trace.py"

SEGMENT_REGIONS = (
    (0x3C000000, 0x3D000000, "external flash DROM (MMU mapped)"),
    (0x3FC88000, 0x3FD00000, "internal SRAM data (dram0)"),
    (0x40370000, 0x403E0000, "internal SRAM instruction (iram0)"),
    (0x42000000, 0x43000000, "external flash IROM (MMU mapped)"),
    (0x50000000, 0x50004000, "RTC slow memory"),
    (0x60000000, 0x60100000, "APB peripheral register block"),
)

REG32_RE = re.compile(r"REG32\(\s*(\w+)\s*,\s*(0x[0-9a-fA-F]+)\s*\)")
CASE_RE = re.compile(r"case\s+(A_\w+)\s*:")
FUNC_RE = re.compile(r"static\s+(?:uint64_t|void)\s+(\w+)\s*\(")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def region_of(vaddr: int) -> str | None:
    for low, high, name in SEGMENT_REGIONS:
        if low <= vaddr < high:
            return name
    return None


def esp_app_segments(data: bytes, offset: int = APP_OFFSET) -> dict:
    """Walk the ESP app image at ``offset`` into a vaddr -> file offset map.

    The header is magic(1) segment_count(1) flash_mode(1) flash_size_freq(1)
    entry(4) wp(1) spi_pin(3) chip_id(2) min_rev(2) alignment(2) hash(32 for v2);
    IDF >= 4.5 always writes the 24-byte extended header, and the checksum byte
    that follows the last segment is not part of the segment data, so the walk
    is validated by checking that it lands exactly on a 16-byte boundary minus
    that byte.
    """
    if len(data) < offset + HEADER_BYTES + SEGMENT_HEADER_BYTES:
        raise ValueError("image too small to contain an app header")
    if data[offset] != IMAGE_MAGIC:
        raise ValueError(f"no ESP image magic at 0x{offset:x} "
                         f"(got 0x{data[offset]:02x})")
    segment_count = data[offset + 1]
    entry = int.from_bytes(data[offset + 4:offset + 8], "little")
    chip_id = int.from_bytes(data[offset + 0xC:offset + 0xE], "little")
    if not 0 < segment_count <= 64:
        raise ValueError(f"implausible segment count {segment_count}")
    segments: list[dict] = []
    cursor = offset + HEADER_BYTES
    for index in range(segment_count):
        if cursor + SEGMENT_HEADER_BYTES > len(data):
            raise ValueError("segment header runs past the end of the image")
        vaddr = int.from_bytes(data[cursor:cursor + 4], "little")
        length = int.from_bytes(data[cursor + 4:cursor + 8], "little")
        cursor += SEGMENT_HEADER_BYTES
        if length > len(data) or cursor + length > len(data):
            raise ValueError(f"segment {index} length runs past the image end")
        segments.append({"index": index, "vaddr": vaddr, "length": length,
                         "file_offset": cursor,
                         "covers": [vaddr, vaddr + length],
                         "region": region_of(vaddr)})
        cursor += length
    return {
        "app_offset": offset,
        "magic": IMAGE_MAGIC,
        "segment_count": segment_count,
        "entry_addr": entry,
        "chip_id": chip_id,
        "segments": segments,
        "walk_end": cursor,
        "padding_to_checksum": (cursor + 1 + 15) // 16 * 16 - cursor,
    }


def vaddr_to_file_offset(segments: dict, vaddr: int) -> dict | None:
    for segment in segments["segments"]:
        low, high = segment["covers"]
        if low <= vaddr < high:
            return {"index": segment["index"], "region": segment["region"],
                    "file_offset": segment["file_offset"] + (vaddr - low),
                    "segment_base": f"0x{low:08x}"}
    return None


def attribute_signature(image: dict, pcs=None) -> dict:
    """Map every reported signature address onto a loaded segment."""
    out: dict = {}
    for name, vaddr in (pcs or SIGNATURE_PCS).items():
        hit = vaddr_to_file_offset(image, vaddr)
        out[name] = {"vaddr": f"0x{vaddr:08x}",
                     "attributed": hit is not None,
                     **({"file_offset": f"0x{hit['file_offset']:x}",
                         "segment": hit["index"],
                         "region": hit["region"]} if hit else {})}
    attributed = sum(1 for value in out.values() if value["attributed"])
    return {"addresses": out, "attributed": attributed,
            "absent_from_image": sorted(name for name, value in out.items()
                                        if not value["attributed"]),
            "total": len(out), "image_entry": f"0x{image['entry_addr']:08x}"}


def find_literals(image: dict, data: bytes, words) -> list[dict]:
    """4-byte-aligned occurrences of ``words`` inside loaded segments."""
    found: list[dict] = []
    for segment in image["segments"]:
        low, length, file_offset = (segment["vaddr"], segment["length"],
                                    segment["file_offset"])
        for step in range(0, max(length - 3, 0), 4):
            word = int.from_bytes(data[file_offset + step:
                                       file_offset + step + 4], "little")
            if word in words:
                found.append({"vaddr": low + step, "word": word,
                              "file_offset": file_offset + step,
                              "segment": segment["index"],
                              "region": segment["region"]})
    return found


def l32r_reach(pc: int, literal_vaddr: int) -> dict:
    """Can an l32r at ``pc`` load the pool word at ``literal_vaddr``?

    Xtensa l32r adds a sign-extended 16-bit offset to ``PC & 3``-aligned base,
    so the pool entry must sit in [-32768, 32764] bytes from the instruction.
    """
    delta = literal_vaddr - (pc & ~3)
    return {"pc": f"0x{pc:08x}", "literal": f"0x{literal_vaddr:08x}",
            "delta_bytes": delta, "within_range": L32R_MIN_BYTES <= delta <= L32R_MAX_BYTES}


def _switch_cases(text: str, function: str) -> set[str]:
    """Case labels inside one function body (brace-free, per line)."""
    match = re.search(rf"static\s+\w+\s+{function}\s*\([^)]*\)\s*\{{", text)
    if not match:
        raise ValueError(f"function {function} not found")
    depth = 0
    start = match.end() - 1
    for index in range(start, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                body = text[start:index]
                return set(CASE_RE.findall(body))
    raise ValueError(f"unbalanced braces in {function}")


def rtc_model_coverage(tree: Path) -> dict:
    """What the shipped S3 RTC_CNTL model really services, read from source."""
    header = (tree / RTC_HEADER).read_text("utf-8", "replace")
    source = (tree / RTC_SOURCE).read_text("utf-8", "replace")
    offsets = {name: int(value, 16) for name, value in REG32_RE.findall(header)
               if name.startswith("RTC_CNTL_")}
    read_cases = _switch_cases(source, "esp32s3_rtc_cntl_read")
    write_cases = _switch_cases(source, "esp32s3_rtc_cntl_write")
    defined = {offset: sorted(name for name, value in offsets.items()
                             if value == offset)
               for offset in sorted(set(offsets.values()))}
    rows = []
    for offset in sorted(RTC_TOUCHED):
        names = defined.get(offset, [])
        handled_read = any(f"A_{name}" in read_cases for name in names)
        handled_write = any(f"A_{name}" in write_cases for name in names)
        rows.append({
            "address": f"0x{RTC_BASE + offset:08x}",
            "offset": f"0x{offset:02x}",
            "meaning": RTC_TOUCHED[offset],
            "defined_in_model_header": bool(names),
            "symbols": names,
            "served_on_read": handled_read,
            "served_on_write": handled_write,
            "guest_sees": "stored value" if handled_read else "hardwired 0",
        })
    return {
        "tree": str(tree),
        "header_registers": {f"0x{value:02x}": names
                             for value, names in sorted(defined.items())},
        "read_cases": sorted(read_cases),
        "write_cases": sorted(write_cases),
        "touched": rows,
        "missing_int_group": [row["address"] for row in rows
                              if int(row["offset"], 16) in RTC_INT_GROUP
                              and not row["served_on_read"]],
    }


def grep_lines(path: Path, pattern: str) -> list[str]:
    if not path.is_file():
        return []
    needle = re.compile(pattern)
    hits = []
    for number, line in enumerate(path.read_text("utf-8", "replace").splitlines(), 1):
        if needle.search(line):
            hits.append(f"{path.name}:{number}: {line.strip()}")
    return hits


def irq_wiring(tree: Path) -> dict:
    """Is anything at all able to raise an RTC interrupt into a CPU?"""
    soc = tree / SOC_SOURCE
    hw_dir = tree / "hw"
    users = []
    if hw_dir.is_dir():
        for path in sorted(hw_dir.rglob("*.c")):
            users += grep_lines(path, r"ETS_RTC_CORE_INTR_SOURCE")
    rtc_source_users = grep_lines(tree / RTC_SOURCE, r"qemu_set_irq|qemu_irq_raise|s->irq")
    return {
        "rtc_cntl_irq_initialised": bool(grep_lines(tree / RTC_SOURCE,
                                                    r"sysbus_init_irq\(sbd, &s->irq\)")),
        "rtc_cntl_irq_connected_in_soc": grep_lines(soc, r"SYS_BUS_DEVICE\(&s->rtc_cntl\)"),
        "rtc_core_source_users": users,
        "rtc_source_can_raise_irq": bool([line for line in rtc_source_users
                                         if "qemu_set_irq(s->irq" in line
                                         or "qemu_irq_raise(s->irq" in line]),
        "wakeup_token_in_rtc_model": grep_lines(tree / RTC_SOURCE, r"(?i)wake"),
        "wakeup_token_in_soc": grep_lines(soc, r"(?i)ext_wakeup|wakeup"),
        "sleep_field_in_rtc_header": grep_lines(tree / RTC_HEADER,
                                                r"(?i)SLP|SLEEP|BOOT_MODE"),
    }


def ipi_chain_presence(tree: Path) -> dict:
    """Does the cross-core FROM_CPU path exist end to end in this tree?"""
    clock = tree / CLOCK_SOURCE
    matrix = tree / INTC_SOURCE
    pic = tree / PIC_SOURCE
    soc = tree / SOC_SOURCE
    return {
        "system_from_cpu_registers_modelled": bool(grep_lines(
            clock, r"A_SYSTEM_CPU_INTR_FROM_CPU_0")) and bool(grep_lines(
                clock, r"qemu_set_irq\(s->irqs\[index\], 1\)")),
        "from_cpu_level_cleared_on_zero_write": bool(grep_lines(
            clock, r"qemu_set_irq\(s->irqs\[index\], 0\)")),
        "clock_lines_wired_to_matrix_inputs": bool(grep_lines(
            soc, r"ETS_FROM_CPU_INTR0_SOURCE")),
        "matrix_routes_and_counts": bool(grep_lines(
            matrix, r"ybx_from_cpu_asserted")) and bool(grep_lines(
                matrix, r"qemu_set_irq\(s->outputs\[i\]\[int_index\], level\)")),
        "matrix_reset_default_is_unrouted": bool(grep_lines(
            matrix, r"INTMATRIX_UNINT_VALUE\s+6")),
        "cpu_level_entry_in_pic": bool(grep_lines(pic, r"env->sregs\[INTSET\]"))
        and bool(grep_lines(pic, r"cpu_interrupt\(cs, CPU_INTERRUPT_HARD\)")),
        "counters_exported_for_runtime_check": [
            "ybx-cpu-intr-{0..3}-sets", "ybx-cpu-intr-{0..3}-clears",
            "ybx-cpu-intr-levels", "ybx-map-writes", "ybx-last-map-addr",
            "ybx-last-map-value", "ybx-cpu{0,1}-asserted",
            "ybx-cpu{0,1}-unrouted", "ybx-cpu{0,1}-from-cpu-asserted",
            "ybx-cpu{0,1}-from-cpu-default-route", "ybx-cpu{0,1}-from-cpu-int",
        ],
    }


def rank_hypotheses(coverage: dict, wiring: dict, ipi: dict,
                    attribution: dict | None = None) -> list[dict]:
    """Ranked causes, each with the measurement that would refute it."""
    int_raw_missing = f"0x{RTC_BASE + 0x44:08x}" in coverage["missing_int_group"]
    rows = []
    rows.append({
        "rank": 1,
        "id": "H1-rtc-sleep-status-hardwired-zero",
        "claim": ("CPU1's spin loop loads RTC_CNTL_INT_RAW (0x60008044), an "
                  "offset the shipped S3 model neither defines nor services, so "
                  "the read returns 0 forever and SLP_WAKEUP/SLP_REJECT can "
                  "never appear; the loop has no timeout and no other exit."),
        "status": "SUPPORTED_BY_SOURCE" if int_raw_missing else "REFUTED_BY_SOURCE",
        "basis": [f"hw/misc/esp32s3_rtc_cntl.c read cases: {coverage['read_cases']}"],
        "falsifying_measurement": (
            "Run the forced-wake variant built by `variant` (poll literal "
            "redirected to 0x60008038, which the model services and which reads "
            "with bit 0 set) under probe_x4pro_inkmod_trace.py. REFUTED if the "
            "timeline still ends with cpu1_pc inside 0x4037cab8..0x4037cb60 and "
            "cpu0_pc inside 0x4037a4a8..0x4037a4c8, and ybx-epd-commands stays "
            "0: that would mean the sleep poll is not what holds the boot."),
        "needs_qemu": True,
    })
    rows.append({
        "rank": 2,
        "id": "H2-no-wake-source-and-no-rtc-irq-line",
        "claim": ("Even a correct INT_RAW register could not be raised: the RTC "
                  "device's irq output is initialised but never connected, and "
                  "ETS_RTC_CORE_INTR_SOURCE is not an input of the interrupt "
                  "matrix anywhere in hw/, so the sleep-wake interrupt has no "
                  "path to either core."),
        "status": ("SUPPORTED_BY_SOURCE"
                   if not wiring["rtc_cntl_irq_connected_in_soc"]
                   and not wiring["rtc_core_source_users"]
                   else "REFUTED_BY_SOURCE"),
        "basis": [
            f"rtc_cntl sysbus_init_irq present: {wiring['rtc_cntl_irq_initialised']}",
            f"SoC connections of the rtc_cntl irq: {len(wiring['rtc_cntl_irq_connected_in_soc'])}",
            f"hw/ users of ETS_RTC_CORE_INTR_SOURCE: {len(wiring['rtc_core_source_users'])}",
            f"sleep/wake fields in the S3 RTC header: {len(wiring['sleep_field_in_rtc_header'])}",
        ],
        "falsifying_measurement": (
            "`grep -rn ETS_RTC_CORE_INTR_SOURCE <tree>/hw` returns at least one "
            "wired input, or `qom-list /machine/soc/rtc_cntl` gains an interrupt "
            "property. Also refuted if a guest run shows CPU1 leaving "
            "rtc_sleep_start without any forced value, i.e. something else "
            "already drives the wake path."),
        "needs_qemu": False,
    })
    rows.append({
        "rank": 3,
        "id": "H3-lost-cross-core-ipi",
        "claim": ("CPU0 waits in esp_ipc_isr_waiting_for_finish_cmd because the "
                  "cross-core IPI that should make the other core publish the "
                  "finish command was never delivered by the emulator."),
        "status": ("DEMOTE_TO_RUNTIME_CHECK" if all(v for k, v in ipi.items()
                                                   if k != "counters_exported_for_runtime_check")
                   else "SUPPORTED_BY_SOURCE"),
        "basis": [f"ipi chain stages present: "
                  f"{sorted(k for k, v in ipi.items() if v and k != 'counters_exported_for_runtime_check')}"],
        "falsifying_measurement": (
            "Read the counters that already ship, on the wedged guest: "
            "qom-get /machine/soc/clock ybx-cpu-intr-2-sets and -3-sets (IPC "
            "sources), qom-get /machine/soc/intmatrix "
            "ybx-cpu{0,1}-from-cpu-asserted and ybx-cpu{0,1}-from-cpu-default-"
            "route, and xp /4wx 0x600C0030 for the latched levels. REFUTED if "
            "from-cpu-asserted > 0 and default-route == 0 for the target core "
            "(the matrix delivered); SUPPORTED if the sets counters are 0 (the "
            "guest never requested the IPI, so CPU0's wait is downstream of "
            "H1) or if default-route > 0 (routing table still at the reset "
            "value 6, which is not a valid dc232b external interrupt)."),
        "needs_qemu": True,
    })
    rows.append({
        "rank": 4,
        "id": "H4-intmatrix-addressing-off-by-one-block",
        "claim": ("The model treats the whole 0x60008000-sized intmatrix region "
                  "as a flat per-source map array starting at 0x600C2000, so any "
                  "guest write to a routing register outside that assumed layout "
                  "(including an interrupt-ack write) silently rewrites a routing "
                  "entry instead of acking, corrupting the table as a side "
                  "effect."),
        "status": "UNTESTED_GEOMETRY",
        "basis": [
            "hw/xtensa/esp32s3_intc.c: get_map_entry uses addr/4 with no base offset",
            "0037 reported the geometry as matching IDF "
            "(core0 source79 at +0x13C, core1 at +0x93C); that claim was checked "
            "against IDF headers inside a container and is not reproducible from "
            "this disk",
        ],
        "falsifying_measurement": (
            "During a wedged run read qom-get /machine/soc/intmatrix "
            "ybx-last-map-addr and ybx-map-writes, then compare against the "
            "esp_rom_route_intr_matrix calls the boot makes: REFUTED if every "
            "recorded map address is of the form 0x13C + 4*k style the guest "
            "intends (0x13C..0x14C and 0x93C..0x94C for FROM_CPU) and "
            "ybx-map-writes stops growing after routing setup; SUPPORTED if map "
            "writes keep arriving during the wedge (an ack path landing in the "
            "map array) or if the addresses cluster 0x100 away from the FROM_CPU "
            "window."),
        "needs_qemu": True,
    })
    rows.append({
        "rank": 5,
        "id": "H5-panel-driver-never-ran",
        "claim": ("The SSD1677 saw zero commands because the paint code never "
                  "executed, i.e. the panel model is at fault rather than the "
                  "sleep handshake."),
        "status": "SUBORDINATE",
        "basis": [
            "attribution of the reported PCs to loaded segments: "
            f"{attribution['attributed'] if attribution else 'not measured'}"
            f"/{attribution['total'] if attribution else 'not measured'}",
        ],
        "falsifying_measurement": (
            "The H1 forced-wake run: if the panel then receives commands "
            "(ybx-epd-commands > 0) this hypothesis is refuted as a cause; if "
            "the guest resumes, paints nothing and issues zero commands, the "
            "panel path needs its own trace. Rejected as the top cause already "
            "by the ordering: commands are only produced by code the wedge "
            "prevents from running."),
        "needs_qemu": True,
    })
    rows.append({
        "rank": 6,
        "id": "H6-wake-triggers-configured-empty",
        "claim": ("inkMOD armed no wakeup source at all (s_config.wakeup_triggers "
                  "== 0), so on real hardware the sleep would be armed but never "
                  "waken, and the emulator is faithfully showing a firmware bug "
                  "rather than a model gap."),
        "status": "PENDING_RUNTIME",
        "basis": ["probe_x4pro_inkmod_trace.py already samples "
                  "INKMOD_CONFIG_ADDR + 0x40 (wakeup_triggers, 0x2 == EXT1)"],
        "falsifying_measurement": (
            "Read the recorded inkmod_config.wakeup_triggers from the tracer "
            "report.json: REFUTED if nonzero (the guest armed a source the model "
            "cannot honour); SUPPORTED if 0 while the poll loop is entered, which "
            "would mean real hardware would also sit here — though it would "
            "still get SLP_REJECT, so a nonzero reject cause is required to "
            "settle it."),
        "needs_qemu": True,
    })
    rows.sort(key=lambda row: row["rank"])
    return rows


def variant_plan(image: dict, literals: list[dict], poll_pc: int) -> dict:
    """Pick the literal-pool word the forced-wake experiment should rewrite."""
    int_raw = [item for item in literals if item["word"] == RTC_BASE + 0x44]
    out = []
    for item in int_raw:
        reach = l32r_reach(poll_pc, item["vaddr"])
        out.append({
            "literal_vaddr": f"0x{item['vaddr']:08x}",
            "file_offset": f"0x{item['file_offset']:x}",
            "segment": item["segment"],
            "region": item["region"],
            "reaches_poll_pc": reach["within_range"],
            "delta_bytes": reach["delta_bytes"],
            "replacement_word": f"0x{RTC_BASE + FORCED_WAKE_OFFSET:08x}",
        })
    return {"poll_pc": f"0x{poll_pc:08x}", "candidates": out,
            "unique": len(out) == 1}


def checksum_position(segments: dict) -> int:
    """File offset of the ESP app image's simple checksum byte.

    The IDF bootloader XORs the image and compares against the last byte of the
    16-byte block that holds the end of the segment data. Measured on the inkMOD
    image (walk_end 0x449128 -> byte 0x33 at 0x44912f): after changing one byte
    0x44->0x38 the guest reported "Calculated 0x4f read 0x33", and
    0x33 ^ 0x44 ^ 0x38 == 0x4f, which pins both the position and the XOR model.
    """
    end = segments["walk_end"]
    return ((end + 1 + 15) // 16) * 16 - 1


def app_hash_position(segments: dict) -> int:
    """File offset of the appended app-image hash digest."""
    return checksum_position(segments) + 1


def verify_app_hash(data: bytes, app_offset: int = APP_OFFSET,
                    segments: dict | None = None) -> dict:
    """Recompute the app image hash and compare it to the appended digest.

    The rule is measured, not borrowed from IDF headers: SHA-256 over the
    contiguous bytes [app_offset, hash_position), header checksum byte included,
    stopping where the digest itself starts. Verified by reproducing the stored
    digest of the untouched inkMOD image byte for byte.
    """
    image = segments or esp_app_segments(data, app_offset)
    position = app_hash_position(image)
    stored = data[position:position + HASH_BYTES]
    computed = hashlib.sha256(data[image["app_offset"]:position]).digest()
    return {
        "hash_position": f"0x{position:x}",
        "stored": stored.hex(),
        "computed": computed.hex(),
        "present": len(stored) == HASH_BYTES
                   and set(stored) not in ({0x00}, {0xFF}),
        "match": stored == computed,
    }


def build_variant(flash: Path, out_dir: Path, literal_vaddr: int,
                  replacement_word: int, app_offset: int = APP_OFFSET) -> dict:
    """Write a copy of the image with one literal word replaced.

    Two independent integrity guards sit between a byte edit and the guest's
    sleep path, and both have to be repaired or the run measures nothing:

    * the simple XOR checksum byte (measured 2026-11-01: an unrepaired copy dies
      with "esp_image: Checksum failed. Calculated 0x4f read 0x33" and boot-loops);
    * the appended SHA-256 (measured the same day: with the checksum repaired the
      same copy died with "Image hash failed - image is corrupt").
    """
    original = flash.read_bytes()
    data = bytearray(original)
    image = esp_app_segments(bytes(data), app_offset)
    hit = vaddr_to_file_offset(image, literal_vaddr)
    if hit is None:
        raise ValueError(f"0x{literal_vaddr:08x} is not inside a loaded segment")
    before = int.from_bytes(data[hit["file_offset"]:hit["file_offset"] + 4], "little")
    if before != RTC_BASE + 0x44:
        raise ValueError(f"literal at 0x{literal_vaddr:08x} is 0x{before:08x}, "
                         f"not RTC_CNTL_INT_RAW 0x{RTC_BASE + 0x44:08x}")
    cksum = checksum_position(image)
    if cksum >= len(data):
        raise ValueError("checksum byte falls outside the image")
    delta = 0
    for index in range(4):
        delta ^= ((before >> (8 * index)) & 0xFF) ^ \
                 ((replacement_word >> (8 * index)) & 0xFF)
    stored = data[cksum]
    data[hit["file_offset"]:hit["file_offset"] + 4] = (
        replacement_word).to_bytes(4, "little")
    data[cksum] = stored ^ delta
    checked = verify_app_hash(bytes(original), app_offset, image)
    app_hash = {"state": "absent", "position": checked["hash_position"]}
    if checked["present"]:
        if not checked["match"]:
            raise ValueError(
                f"app hash at {checked['hash_position']} does not match the "
                "recomputed digest; the region rule does not hold for this image")
        position = app_hash_position(image)
        digest = hashlib.sha256(bytes(data[image["app_offset"]:position])).digest()
        data[position:position + HASH_BYTES] = digest
        app_hash = {"state": "repaired", "position": checked["hash_position"],
                    "before": checked["stored"], "after": digest.hex()}
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / "inkmod-forced-wake-x4pro-flash-16m.bin"
    target.write_bytes(bytes(data))
    changed = [number for number, old in enumerate(original)
               if old != data[number]]
    return {
        "literal_vaddr": f"0x{literal_vaddr:08x}",
        "file_offset": f"0x{hit['file_offset']:x}",
        "word_before": f"0x{before:08x}",
        "word_after": f"0x{replacement_word:08x}",
        "checksum_position": f"0x{cksum:x}",
        "checksum_before": f"0x{stored:02x}",
        "checksum_after": f"0x{data[cksum]:02x}",
        "app_hash": app_hash,
        "changed_offsets": [f"0x{number:x}" for number in changed],
        "bytes_changed": len(changed),
        "input_sha256": sha256_file(flash),
        "variant_path": str(target),
        "variant_sha256": sha256_file(target),
    }


def runbook(flash: Path, variant: dict, qemu_root: Path = DEFAULT_TREE,
            sd: Path = DEFAULT_SD, rom: Path = DEFAULT_ROM) -> str:
    """The container commands that would execute the experiment.

    Only /work is writable; the shared build tree, the firmware folder, the SD
    image, the ROM and this repo's tools directory all come in read-only, and
    the emulator binary is never rebuilt. The factory reference runs first
    because it is the control on the invocation itself: if it does not reach
    the panel, nothing measured downstream about inkMOD means anything.
    """
    work = Path(variant["variant_path"]).parent
    return f"""# Throwaway container (--rm), shared tree mounted :ro, no network, the live
# browser container is never referenced. Deterministic: the guest boots with
# no GDB and no injected interrupts; the only difference between the two inkMOD
# runs is the flash image.
# NOTE --platform linux/arm64: the daemon rejects "linux-arm64" with
# "error parsing specified platform: unknown operating system or architecture".
docker run --rm --platform linux/arm64 --network none \\
  -v {qemu_root}:/qemu:ro \\
  -v {TRACER.parent}:/tools:ro \\
  -v {flash.parent}:/fw:ro \\
  -v {sd.parent}:/sd:ro \\
  -v {rom.parent}:/roms:ro \\
  -v {work}:/work \\
  {CONTAINER} bash -lc '
    for f in factory-control inkmod-pristine inkmod-forced-wake; do
      mkdir -p /work/runs/$f
    done
    python3 /tools/probe_x4pro_inkmod_trace.py \\
      --qemu /qemu/build-ybx/qemu-system-xtensa \\
      --rom /roms/{rom.name} \\
      --flash /fw/s3-flash-complete-rom-test.bin \\
      --sd /sd/{sd.name} \\
      --label factory-control --profile generic --seconds 45 \\
      --out /work/runs/factory-control
    python3 /tools/probe_x4pro_inkmod_trace.py \\
      --qemu /qemu/build-ybx/qemu-system-xtensa \\
      --rom /roms/{rom.name} \\
      --flash /fw/{flash.name} \\
      --sd /sd/{sd.name} \\
      --label inkmod-pristine --profile inkmod --seconds 60 --exec-trace \\
      --out /work/runs/inkmod-pristine
    python3 /tools/probe_x4pro_inkmod_trace.py \\
      --qemu /qemu/build-ybx/qemu-system-xtensa \\
      --rom /roms/{rom.name} \\
      --flash /work/{Path(variant["variant_path"]).name} \\
      --sd /sd/{sd.name} \\
      --label inkmod-forced-wake --profile inkmod --seconds 60 --exec-trace \\
      --out /work/runs/inkmod-forced-wake'

# Decision rule, read off the two inkMOD report.json files:
#   forced-wake verdict != deep_sleep_poll_stalled while the pristine run still
#     stalls at deep_sleep_poll_stalled  ->  H1 CONFIRMED.
#   forced-wake still reports exec signature 0x4037cb2c and the same verdict
#     ->  H1 REFUTED; the stall is downstream (panel BUSY / semaphore / IPC),
#     and H2 plus H6 become the front of the queue.
# If the factory control does not reach the panel, the invocation is wrong:
# check the ROM first, the vendor build-ybx/esp32s3_rev0_rom.bin boots nothing.
"""


def load_tracer_report(path: Path | None) -> dict | None:
    if path is None or not path.is_file():
        return None
    return json.loads(path.read_text())


def compare_against_report(hypotheses: list[dict], report: dict) -> list[dict]:
    """Attach whatever a recorded tracer run says to each hypothesis."""
    if report is None:
        return hypotheses
    verdict = report.get("verdict", {})
    epd = report.get("epd", {})
    intmatrix = report.get("intmatrix", {})
    clock = report.get("clock", {})
    config = report.get("inkmod_config", {})
    observed = {
        "verdict": verdict.get("verdict"),
        "epd_commands": epd.get("commands"),
        "intmatrix": intmatrix,
        "clock": clock,
        "wakeup_triggers": config.get("wakeup_triggers"),
    }
    for row in hypotheses:
        row["recorded_run"] = observed
    return hypotheses


def analyse(flash: Path, tree: Path, poll_pc: int = 0x4037CB2C,
            tracer_report: Path | None = None) -> dict:
    data = flash.read_bytes()
    image = esp_app_segments(data)
    attribution = attribute_signature(image)
    literals = find_literals(image, data,
                             {RTC_BASE + 0x44, RTC_BASE + 0x40,
                              RTC_BASE + 0x48, RTC_BASE + 0x4C})
    coverage = rtc_model_coverage(tree)
    wiring = irq_wiring(tree)
    ipi = ipi_chain_presence(tree)
    hypotheses = rank_hypotheses(coverage, wiring, ipi, attribution)
    hypotheses = compare_against_report(
        hypotheses, load_tracer_report(tracer_report))
    return {
        "tool": "inkmod_root_cause",
        "inputs": {
            "flash": str(flash),
            "flash_sha256": sha256_file(flash),
            "tree": str(tree),
            "tracer_report": str(tracer_report) if tracer_report else None,
        },
        "image": {
            "entry_addr": f"0x{image['entry_addr']:08x}",
            "chip_id": f"0x{image['chip_id']:04x}",
            "segment_count": image["segment_count"],
            "segments": [{**segment, "vaddr": f"0x{segment['vaddr']:08x}"}
                         for segment in image["segments"]],
        },
        "signature_attribution": attribution,
        "rtc_literals": [{**item, "vaddr": f"0x{item['vaddr']:08x}",
                          "word": f"0x{item['word']:08x}"}
                         for item in literals],
        "rtc_model_coverage": coverage,
        "irq_wiring": wiring,
        "ipi_chain": ipi,
        "forced_wake_plan": variant_plan(image, literals, poll_pc),
        "hypotheses": hypotheses,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--flash", type=Path, default=DEFAULT_FLASH)
    common.add_argument("--app-offset", type=lambda value: int(value, 0),
                        default=APP_OFFSET)

    show = sub.add_parser("analyze", parents=[common],
                          help="derive the missing-behaviour table and hypotheses")
    show.add_argument("--tree", type=Path, default=DEFAULT_TREE)
    show.add_argument("--poll-pc", type=lambda value: int(value, 0),
                      default=0x4037CB2C)
    show.add_argument("--tracer-report", type=Path)
    show.add_argument("--out", type=Path)

    make = sub.add_parser("variant", parents=[common],
                          help="write the forced-wake falsification image")
    make.add_argument("--out-dir", type=Path, required=True)
    make.add_argument("--literal-vaddr", type=lambda value: int(value, 0),
                      required=True)
    make.add_argument("--replacement", type=lambda value: int(value, 0),
                      default=RTC_BASE + FORCED_WAKE_OFFSET)
    make.add_argument("--tree", type=Path, default=DEFAULT_TREE)
    make.add_argument("--sd", type=Path, default=DEFAULT_SD)
    make.add_argument("--rom", type=Path, default=DEFAULT_ROM)

    args = parser.parse_args(argv)
    if not args.flash.is_file():
        parser.error(f"missing flash image: {args.flash}")
    if args.command == "analyze":
        if not (args.tree / RTC_SOURCE).is_file():
            parser.error(f"QEMU tree not readable at {args.tree}")
        result = analyse(args.flash, args.tree, args.poll_pc, args.tracer_report)
        text = json.dumps(result, indent=1)
        if args.out is not None:
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(text)
        print(text)
        return 0
    record = build_variant(args.flash, args.out_dir, args.literal_vaddr,
                           args.replacement, args.app_offset)
    print(json.dumps({**record, "runbook": runbook(args.flash, record,
                                                   args.tree, args.sd,
                                                   args.rom)},
                     indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
