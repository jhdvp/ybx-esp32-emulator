#!/usr/bin/env python3
"""Trace where the inkMOD v1.1.10 X4 Pro image stops under the YBX QEMU.

Boots one flash image on the full X4 Pro machine, samples both guest CPUs
through QMP ``human-monitor-command``/``info registers``, reads the SSD1677
and RTC_CNTL register blocks, hashes the raw flash before and after, and
classifies the observed stop. Runs inside the offline QEMU builder image next
to the other ``probe_x4pro_*`` scripts; it never talks to the shared browser
emulator and needs no network.

Observation this probe reproduces (live tree, QEMU binary f65db0a7):
inkMOD 1.1.10 reaches ``rtc_sleep_start`` about 4.8 s after boot, CPU1 spins
at 0x4037cb2c polling RTC_CNTL_INT_RAW (0x60008044) for
SLP_WAKEUP/SLP_REJECT, CPU0 is parked in
``esp_ipc_isr_waiting_for_finish_cmd`` (0x4037a4a8) because the IPC ISR
finish command is never published, and the SSD1677 has seen zero commands.
The named wait ranges are signatures of that exact image; other firmware is
reported as ``running_or_unknown`` unless its PCs match.

Usage (inside ybx-qemu-builder:20261029, tree build dir mounted at /qemu):

    python3 probe_x4pro_inkmod_trace.py \
        --qemu /qemu/build-ybx/qemu-system-xtensa \
        --rom /qemu/build-ybx/esp32s3_rev0_rom.bin \
        --flash /fw/inkmod-v1.1.10-x4pro-flash-16m.bin \
        --sd /work/ybx-test-sd-fat16-mbr-pristine.img \
        --label inkmod-1.1.10 --profile inkmod --seconds 60 \
        --exec-trace --out /runs/inkmod-60s
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from probe_x4pro_touch_injection_qtest import qmp_connect

MACHINE = ("esp32s3,ybx-x4pro-i2c=on,ybx-x4pro-gt911=on,"
           "ybx-x4pro-sd-power=on,ybx-x4pro-spi2=on,ybx-x4pro-epd=on")
RTC_BASE = 0x60008000
RTC_NAMES = {
    0x18: "state0",
    0x1c: "timer1",
    0x20: "timer2",
    0x3c: "wakeup_state",
    0x44: "int_raw",
    0x4c: "int_clr",
}
RTC_WORDS = (0x04, 0x08, 0x18, 0x1c, 0x20, 0x3c, 0x44, 0x4c)
EPD_PROPS = (
    "frames", "commands", "ram-writes", "cmd24", "cmd26", "update-ctrl2",
    "busy-rise-edges", "busy-fall-edges", "busy-high-ms",
    "nonwhite", "nonwhite-ram", "nonwhite-ram-writes",
    "deep-sleep", "sleep-entries", "sleep-ignored-bytes",
)
ROM_PC_RANGE = (0x40000000, 0x40080000)
CLOCK_PROPS = (
    "ybx-cpu-intr-0-sets", "ybx-cpu-intr-1-sets",
    "ybx-cpu-intr-2-sets", "ybx-cpu-intr-3-sets",
)
INTMATRIX_PROPS = (
    "ybx-map-writes", "ybx-last-map-addr", "ybx-last-map-value",
    "ybx-cpu0-from-cpu-asserted", "ybx-cpu0-from-cpu-int",
    "ybx-cpu0-from-cpu-default-route",
    "ybx-cpu1-from-cpu-asserted", "ybx-cpu1-from-cpu-int",
    "ybx-cpu1-from-cpu-default-route",
)
INKMOD_FLAGS_ADDR = 0x3fca2908
INKMOD_FLAGS_WORDS = 3
INKMOD_CONFIG_ADDR = 0x3fc99e60
INKMOD_CONFIG_WORDS = 32
# s_config.wakeup_triggers at INKMOD_CONFIG_ADDR + 0x40; 0x2 is EXT1.
INKMOD_CONFIG_WAKEUP_WORD = 16
INKMOD_WAIT_RANGES = {
    "rtc_sleep_start": (0x4037cab8, 0x4037cb60),
    "ipc_isr_wait": (0x4037a4a8, 0x4037a4c8),
}
EXEC_SIGNATURES = {
    "deep_sleep_wrapper": 0x40377428,
    "deep_branch_body": 0x4037742e,
    "light_branch_body": 0x40377448,
    "rtc_sleep_start": 0x4037cab8,
    "rtc_int_raw_poll": 0x4037cb2c,
    "ipc_isr_wait": 0x4037a4a8,
    "esp_sleep_start": 0x4214d240,
}
EXEC_DFILTER = ",".join(f"0x{addr:x}+0x{size:x}" for addr, size in (
    (0x40377428, 0x40), (0x4037a4a8, 0x30), (0x4037cab8, 0xa8),
    (0x4214d240, 0x180),
))
UART_MARKERS_APP = (
    "cpu_start:", "esp_psram:", "Preferences.cpp", "sdmmc_periph:",
    "inkMOD version", "CrossPoint", "Arduino",
)
UART_MARKERS_PANIC = (
    "Guru Meditation Error", "panic'ed", "abort()", "assert failed",
    "Backtrace:",
)
REGISTER_RE = re.compile(r"\b(PC|PS|EXCCAUSE|A\d\d|AR\d\d)=([0-9a-fA-F]+)")
XP_LINE_RE = re.compile(r"^\s*[0-9a-fA-F]+:\s+(.*)$")
EXEC_PC_RE = re.compile(r"\[[0-9a-fA-F]+/([0-9a-fA-F]{16})/")
LOG_CAP = 64 * 1024


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_registers(text: str) -> dict[str, int]:
    """Parse the HMP ``info registers`` dump into {NAME: int}."""
    return {match.group(1): int(match.group(2), 16)
            for match in REGISTER_RE.finditer(text)}


def parse_xp_words(text: str) -> list[int]:
    """Parse HMP ``xp /Nwx`` output (``addr: 0x.. 0x..``) into words."""
    words: list[int] = []
    for line in text.splitlines():
        match = XP_LINE_RE.match(line)
        if not match:
            continue
        words.extend(int(token, 16) for token in match.group(1).split()
                     if token.startswith("0x"))
    return words


def visible_frame_sha(hex_string: str) -> tuple[str, int]:
    data = bytes.fromhex(hex_string.strip())
    return hashlib.sha256(data).hexdigest(), len(data)


def changed_blocks(path_a: Path, path_b: Path, block: int = 4096) -> list[int]:
    """Return byte offsets of differing blocks; both files must match in size."""
    offsets: list[int] = []
    offset = 0
    with path_a.open("rb") as file_a, path_b.open("rb") as file_b:
        while True:
            chunk_a = file_a.read(block)
            chunk_b = file_b.read(block)
            if not chunk_a and not chunk_b:
                return offsets
            if len(chunk_a) != len(chunk_b):
                raise ValueError("flash images differ in size")
            if chunk_a != chunk_b:
                offsets.append(offset)
            offset += block


def classify_pc(pc: int | None, ranges=None) -> str | None:
    if pc is None:
        return None
    for name, (start, end) in (ranges or INKMOD_WAIT_RANGES).items():
        if start <= pc < end:
            return name
    return None


def _tail_is(timeline: list[dict], key: str, tag: str,
             minimum: int = 3) -> bool:
    if len(timeline) < minimum:
        return False
    tail = timeline[-max(minimum, len(timeline) // 4):]
    return all(classify_pc(sample.get(key)) == tag for sample in tail)


def _wedge_since(timeline: list[dict]) -> float | None:
    since = None
    for sample in reversed(timeline):
        if (classify_pc(sample.get("cpu0_pc")) == "ipc_isr_wait" and
                classify_pc(sample.get("cpu1_pc")) == "rtc_sleep_start"):
            since = sample.get("t")
        else:
            break
    return since


def classify_outcome(timeline: list[dict], epd: dict, rtc: dict,
                     flags: dict, uart: dict | None = None) -> dict:
    """Name the guest stop from sampled PCs, EPD counters and RTC_INT_RAW."""
    int_raw = rtc.get(RTC_BASE + 0x44)
    finish_cmd = flags.get("ipc_finish_cmd")
    result = {
        "rtc_int_raw": int_raw,
        "ipc_finish_cmd": finish_cmd,
        "samples": len(timeline),
        "cpu0_tail": classify_pc(timeline[-1].get("cpu0_pc")) if timeline else None,
        "cpu1_tail": classify_pc(timeline[-1].get("cpu1_pc")) if timeline else None,
    }
    if not timeline:
        return {**result, "verdict": "no_samples",
                "reason": "QMP produced no CPU samples"}
    uart = uart or {}
    if uart.get("panic"):
        cpu0_pc = timeline[-1].get("cpu0_pc")
        in_rom = cpu0_pc is not None and ROM_PC_RANGE[0] <= cpu0_pc < ROM_PC_RANGE[1]
        verdict = "rom_panic" if in_rom else "guest_panic"
        return {**result, "verdict": verdict,
                "reason": uart.get("panic_text", "UART reports a panic")}
    frames = int(epd.get("frames", 0) or 0)
    if frames > 0:
        return {**result, "verdict": "painted",
                "reason": f"SSD1677 produced {frames} frame(s)"}
    cpu0_wait = _tail_is(timeline, "cpu0_pc", "ipc_isr_wait")
    cpu1_wait = _tail_is(timeline, "cpu1_pc", "rtc_sleep_start")
    surface = sum(int(epd.get(key, 0) or 0)
                  for key in ("commands", "ram-writes", "data-bytes"))
    if cpu0_wait and cpu1_wait:
        since = _wedge_since(timeline)
        if int_raw == 0:
            verdict = "deep_sleep_poll_stalled"
            reason = ("CPU1 polls RTC_CNTL_INT_RAW for SLP_WAKEUP/SLP_REJECT "
                      "while it reads 0; CPU0 waits for the IPC ISR finish "
                      "command; the SSD1677 has seen no traffic")
        else:
            verdict = "deep_sleep_poll_active"
            reason = "CPU1 waits in rtc_sleep_start with INT_RAW nonzero"
        return {**result, "verdict": verdict, "reason": reason,
                "wedge_since_s": since}
    if cpu1_wait:
        return {**result, "verdict": "rtc_sleep_poll_only",
                "reason": "CPU1 waits in rtc_sleep_start; CPU0 is elsewhere"}
    if cpu0_wait:
        return {**result, "verdict": "ipc_isr_stall_only",
                "reason": "CPU0 waits for the IPC ISR finish command"}
    if surface > 0:
        return {**result, "verdict": "epd_active_no_frame",
                "reason": f"SSD1677 saw {surface} command/write events "
                          "but produced no frame"}
    return {**result, "verdict": "running_or_unknown",
            "reason": "no known wait signature and no EPD frame"}


def summarize_uart(data: bytes) -> dict:
    text = data.decode("utf-8", "replace")
    panic_line = next((line.strip() for line in text.splitlines()
                       if any(marker in line for marker in UART_MARKERS_PANIC)),
                      None)
    return {
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "rom_seen": "ESP-ROM:" in text,
        "bootloader_seen": any(marker in text for marker in (
            "SPI_FAST_FLASH_BOOT", "SPI_FLASH_BOOT", "entry 0x")),
        "app_markers": [marker for marker in UART_MARKERS_APP
                        if marker in text],
        "panic": panic_line is not None,
        "panic_text": panic_line,
    }


def summarize_exec_log(path: Path) -> dict:
    hits = {name: 0 for name in EXEC_SIGNATURES}
    if not path.is_file():
        return {"enabled": True, "exists": False, "hits": hits}
    text = path.read_text("utf-8", "replace")
    for line in text.splitlines():
        match = EXEC_PC_RE.search(line)
        if not match:
            continue
        address = int(match.group(1), 16)
        for name, signature in EXEC_SIGNATURES.items():
            if address == signature:
                hits[name] += 1
    return {"enabled": True, "exists": True,
            "bytes": len(text.encode()), "hits": hits,
            "note": "TB starts only; chaining can hide repeated loop hits"}


def hmp(qmp, line: str, cpu: int | None = None) -> str:
    arguments = {"command-line": line}
    if cpu is not None:
        arguments["cpu-index"] = cpu
    reply = qmp("human-monitor-command", arguments)
    if "return" not in reply:
        raise RuntimeError(f"HMP {line!r} failed: {reply}")
    return reply["return"]


def qom_get(qmp, path: str, prop: str):
    reply = qmp("qom-get", {"path": path, "property": prop})
    if "return" not in reply:
        raise RuntimeError(f"missing QOM property {path}/{prop}: {reply}")
    return reply["return"]


def read_words(qmp, address: int, count: int) -> list[int]:
    words = parse_xp_words(hmp(qmp, f"xp /{count}wx 0x{address:x}"))
    if len(words) < count:
        raise RuntimeError(f"xp at 0x{address:x} returned {len(words)} words")
    return words[:count]


def sample_cpu(qmp, cpu: int) -> dict:
    registers = parse_registers(hmp(qmp, "info registers", cpu=cpu))
    if "PC" not in registers:
        raise RuntimeError(f"CPU{cpu} registers have no PC")
    return {"pc": registers["PC"],
            "ps": registers.get("PS"),
            "exccause": registers.get("EXCCAUSE"),
            "a0": registers.get("A00"),
            "a1": registers.get("A01"),
            "a2": registers.get("A02"),
            "a10": registers.get("A10")}


def qemu_command(args, work: Path, rom_dir: Path, sock: Path) -> list[str]:
    command = [
        str(args.qemu), "-machine", args.machine,
        "-m", "8M", "-global", "ssi_psram.is_octal=on", "-smp", "2",
        "-nographic", "-monitor", "none",
        "-serial", f"file:{work / 'uart0.log'}",
        "-serial", "null", "-serial", "null", "-serial", "null",
        "-qmp", f"unix:{sock},server=on,wait=off",
        "-L", str(rom_dir),
        "-drive", f"file={work / 'flash.bin'},if=mtd,format=raw",
    ]
    if args.sd is not None:
        command += ["-drive", f"file={work / 'sd.img'},if=sd,format=raw"]
    if args.exec_trace:
        command += ["-D", str(work / "exec.log"),
                    "-d", "exec", "-dfilter", EXEC_DFILTER]
    return command


def collect_static(qmp, profile: str, stack_words: int = 0) -> dict:
    data: dict = {}
    data["final_registers"] = {
        f"cpu{cpu}": parse_registers(hmp(qmp, "info registers", cpu=cpu))
        for cpu in (0, 1)
    }
    data["stack"] = {}
    for cpu in (0, 1):
        a1 = data["final_registers"][f"cpu{cpu}"].get("A01")
        if stack_words <= 0 or a1 is None:
            continue
        try:
            data["stack"][f"cpu{cpu}"] = {
                "a1": f"0x{a1:08x}",
                "words": read_words(qmp, a1, stack_words),
            }
        except RuntimeError as error:
            data["stack"][f"cpu{cpu}"] = {"a1": f"0x{a1:08x}",
                                          "error": str(error)}
    rtc = {}
    words = read_words(qmp, RTC_BASE, 0x50 // 4)
    for offset in RTC_WORDS:
        rtc[f"0x{RTC_BASE + offset:08x}"] = words[offset // 4]
    data["rtc_cntl"] = rtc
    data["rtc_cntl_names"] = {f"0x{RTC_BASE + offset:08x}": name
                              for offset, name in RTC_NAMES.items()}
    data["epd"] = {prop: qom_get(qmp, "/machine/soc", f"ybx-epd-{prop}")
                   for prop in EPD_PROPS}
    try:
        frame_hex = qom_get(qmp, "/machine/soc", "ybx-epd-visible-hex")
        frame_sha, frame_bytes = visible_frame_sha(frame_hex)
        data["frame"] = {"sha256": frame_sha, "bytes": frame_bytes}
    except (RuntimeError, ValueError) as error:
        data["frame"] = {"error": str(error)}
    data["clock"] = {prop: qom_get(qmp, "/machine/soc/clock", prop)
                     for prop in CLOCK_PROPS}
    data["intmatrix"] = {prop: qom_get(qmp, "/machine/soc/intmatrix", prop)
                         for prop in INTMATRIX_PROPS}
    if profile == "inkmod":
        flags = read_words(qmp, INKMOD_FLAGS_ADDR, INKMOD_FLAGS_WORDS)
        data["inkmod_flags"] = {
            "address": f"0x{INKMOD_FLAGS_ADDR:08x}",
            "words": flags,
            "ipc_finish_cmd": flags[1],
        }
        config = read_words(qmp, INKMOD_CONFIG_ADDR, INKMOD_CONFIG_WORDS)
        data["inkmod_config"] = {
            "address": f"0x{INKMOD_CONFIG_ADDR:08x}",
            "wakeup_triggers": config[INKMOD_CONFIG_WAKEUP_WORD],
        }
    return data


def probe(args) -> dict:
    work = Path(args.out)
    work.mkdir(parents=True, exist_ok=True)
    (work / "rom").mkdir(exist_ok=True)
    shutil.copyfile(args.rom, work / "rom" / "esp32s3_rev0_rom.bin")
    shutil.copyfile(args.flash, work / "flash.bin")
    if args.sd is not None:
        shutil.copyfile(args.sd, work / "sd.img")
    flash_before = work / "flash.bin"
    flash_sha_before = sha256_file(flash_before)
    sock = work / "qmp.sock"
    if sock.exists():
        sock.unlink()
    command = qemu_command(args, work, work / "rom", sock)
    started = time.monotonic()
    with (work / "qemu.stdout").open("wb") as stdout, \
            (work / "qemu.stderr").open("wb") as stderr:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL,
                                   stdout=stdout, stderr=stderr)
    client = reader = qmp = None
    error = None
    timeline: list[dict] = []
    static: dict = {}
    rtc_injection = None
    try:
        client, reader, qmp = qmp_connect(sock)
        period = max(0.05, args.sample_ms / 1000.0)
        next_sample = 0.0
        while time.monotonic() - started < args.seconds:
            elapsed = time.monotonic() - started
            if elapsed < next_sample:
                time.sleep(next_sample - elapsed)
            elapsed = time.monotonic() - started
            # Explicit test input only. Never advertise this as simulated EXT1
            # hardware, and do not synthesize a wakeup without --inject-rtc.
            if args.inject_rtc and rtc_injection is None:
                requests = qom_get(qmp, "/machine/soc", "ybx-rtc-sleep-requests")
                if requests >= 1:
                    result = qmp("qom-set", {"path": "/machine/soc",
                                             "property": "ybx-rtc-inject-" + args.inject_rtc,
                                             "value": True})
                    if "return" not in result:
                        raise RuntimeError(f"RTC injection rejected: {result}")
                    rtc_injection = {"event": args.inject_rtc,
                                     "at_seconds": round(elapsed, 3),
                                     "sleep_requests_before": requests}
            cpu0 = sample_cpu(qmp, 0)
            cpu1 = sample_cpu(qmp, 1)
            timeline.append({
                "t": round(elapsed, 3),
                "cpu0_pc": cpu0["pc"], "cpu1_pc": cpu1["pc"],
                "cpu0_exccause": cpu0["exccause"],
                "cpu1_exccause": cpu1["exccause"],
                "epd_frames": qom_get(qmp, "/machine/soc", "ybx-epd-frames"),
                "epd_commands": qom_get(qmp, "/machine/soc", "ybx-epd-commands"),
            })
            next_sample += period
        static = collect_static(qmp, args.profile, args.stack_words)
    except Exception as failure:  # keep the captured UART/stderr for the report
        error = f"{type(failure).__name__}: {failure}"
    finally:
        if qmp is not None:
            try:
                qmp("quit")
            except Exception:
                pass
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        if client is not None:
            client.close()
    uart_raw = (work / "uart0.log").read_bytes() \
        if (work / "uart0.log").is_file() else b""
    stderr_raw = (work / "qemu.stderr").read_bytes() \
        if (work / "qemu.stderr").is_file() else b""
    flash_sha_after = sha256_file(flash_before)
    if args.sd is not None:
        sd_sha_before = sha256_file(args.sd)
        sd_sha_after = sha256_file(work / "sd.img")
    else:
        sd_sha_before = sd_sha_after = None
    if flash_sha_before == flash_sha_after:
        flash_blocks: list[int] = []
    else:
        flash_blocks = changed_blocks(args.flash, flash_before,
                                      block=4096)
    rtc = {}
    for offset in RTC_WORDS:
        key = f"0x{RTC_BASE + offset:08x}"
        if key in static.get("rtc_cntl", {}):
            rtc[RTC_BASE + offset] = static["rtc_cntl"][key]
    flags = static.get("inkmod_flags", {})
    epd = static.get("epd", {})
    report = {
        "probe": "probe_x4pro_inkmod_trace",
        "label": args.label,
        "profile": args.profile,
        "seconds": args.seconds,
        "sample_ms": args.sample_ms,
        "machine": args.machine,
        "qemu": {
            "binary": str(args.qemu),
            "sha256": sha256_file(args.qemu),
        },
        "rom": {
            "input": str(args.rom),
            "sha256": sha256_file(args.rom),
        },
        "flash": {
            "input": str(args.flash),
            "input_sha256": sha256_file(args.flash),
            "run_copy_sha256_before": flash_sha_before,
            "run_copy_sha256_after": flash_sha_after,
            "changed_blocks_4k": flash_blocks,
            "changed_bytes": len(flash_blocks) * 4096,
        },
        "sd": {"input": str(args.sd) if args.sd is not None else None,
               "input_sha256": sd_sha_before,
               "run_copy_sha256_after": sd_sha_after},
        "uart": {**summarize_uart(uart_raw),
                 "text": uart_raw[:LOG_CAP].decode("utf-8", "replace"),
                 "truncated": len(uart_raw) > LOG_CAP},
        "qemu_stderr": stderr_raw[:LOG_CAP].decode("utf-8", "replace"),
        "timeline": timeline,
    }
    report.update(static)
    report["rtc_injection"] = rtc_injection
    if args.exec_trace:
        report["exec_trace"] = summarize_exec_log(work / "exec.log")
    report["verdict"] = classify_outcome(timeline, epd, rtc, flags,
                                         report["uart"])
    if error is not None:
        report["error"] = error
    (work / "report.json").write_text(json.dumps(report, indent=1))
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--qemu", type=Path, required=True)
    parser.add_argument("--rom", type=Path, required=True)
    parser.add_argument("--flash", type=Path, required=True)
    parser.add_argument("--sd", type=Path)
    parser.add_argument("--label", required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--seconds", type=float, default=60.0)
    parser.add_argument("--sample-ms", type=int, default=250)
    parser.add_argument("--profile", choices=("generic", "inkmod"),
                        default="generic")
    parser.add_argument("--machine", default=MACHINE)
    parser.add_argument("--stack-words", type=int, default=64,
                        help="words dumped at each CPU A1 after sampling")
    parser.add_argument("--inject-rtc", choices=("wakeup", "reject"),
                        help="EXPLICIT test-only QMP RTC event after first sleep request; "
                             "not a simulated physical wake source")
    parser.add_argument("--exec-trace", action="store_true",
                        help="private-tree -d exec restricted to inkMOD ranges")
    args = parser.parse_args()
    for path in (args.qemu, args.rom, args.flash):
        if not path.is_file():
            parser.error(f"missing input: {path}")
    if args.sd is not None and not args.sd.is_file():
        parser.error(f"missing input: {args.sd}")
    report = probe(args)
    verdict = report["verdict"]
    print(f"{args.label}: {verdict['verdict']} - {verdict['reason']}")
    for cpu in (0, 1):
        last = report["timeline"][-1] if report["timeline"] else {}
        pc = last.get(f"cpu{cpu}_pc")
        print(f"  cpu{cpu} PC=" + (f"0x{pc:08x}" if pc is not None else "n/a"))
    epd = report.get("epd", {})
    print(f"  epd frames={epd.get('frames')} commands={epd.get('commands')} "
          f"ram-writes={epd.get('ram-writes')}")
    flash = report["flash"]
    print(f"  flash writes: {len(flash['changed_blocks_4k'])} block(s), "
          f"sha {'unchanged' if not flash['changed_blocks_4k'] else 'changed'}")
    print(f"  uart: {report['uart']['bytes']} byte(s), "
          f"panic={report['uart']['panic']}")
    frame = report.get("frame", {})
    if "sha256" in frame:
        print(f"  visible buffer sha256={frame['sha256']} "
              f"nonwhite={epd.get('nonwhite')}")
    if "error" in report:
        print(f"  probe error: {report['error']}", file=sys.stderr)
    print(f"wrote {Path(args.out) / 'report.json'}")
    return 0 if report["timeline"] else 2


if __name__ == "__main__":
    sys.exit(main())
