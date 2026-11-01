#!/usr/bin/env python3
"""Force the guest's RTC wake bit observable, without touching the emulator.

H1 of experiment 0093 says inkMOD never paints because CPU1 waits in
``rtc_sleep_wait_ready`` for a bit in RTC_CNTL_INT_RAW that the emulator has no
code path to ever set. The falsifying measurement is to make that bit readable:
redirect the poll's literal-pool word at one of the RTC offsets the model *does*
service, then watch whether inkMOD walks on to the panel.

Two subcommands, because the substitution target has to be measured before it
can be picked:

    probe        read the modelled RTC offsets from a running guest, twice
    experiment   build the image variant, run it through the tracer, compare
                 against the pristine control

``experiment`` shells out to probe_x4pro_inkmod_trace.py rather than duplicating
its sampling; that module is never modified here.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))

import inkmod_root_cause as irc  # noqa: E402
from probe_x4pro_inkmod_trace import (  # noqa: E402
    INKMOD_WAIT_RANGES, MACHINE, RTC_BASE, classify_pc, qmp_connect,
    qemu_command, read_words, sample_cpu, sha256_file,
)

TRACER = Path(__file__).resolve().parent / "probe_x4pro_inkmod_trace.py"
# IDF masks (SLP_REJECT | wake_trigger) into the raw register; the sibling
# ESP32-C3 header in the same tree names SLP_WAKEUP bit 0 and SLP_REJECT bit 1.
WAKE_BITS = 0b11


def read_poll_literal(qmp, address: int):
    """The literal word the poll loads its register address from.

    Read through the system address space, so an unmapped IRAM alias is reported
    rather than fatal: the RTC table is what the substitution decision needs.
    """
    try:
        return f"0x{read_words(qmp, address, 1)[0]:08x}"
    except Exception as failure:
        return f"unreadable: {type(failure).__name__}"


def probe_rtc(qmp, offsets) -> list[dict]:
    first = read_words(qmp, RTC_BASE, max(offsets) // 4 + 1)
    return [{"offset": f"0x{offset:02x}",
             "address": f"0x{RTC_BASE + offset:08x}",
             "value": first[offset // 4]} for offset in offsets]


def probe(args) -> dict:
    work = Path(args.out)
    work.mkdir(parents=True, exist_ok=True)
    (work / "rom").mkdir(exist_ok=True)
    shutil.copyfile(args.rom, work / "rom" / "esp32s3_rev0_rom.bin")
    shutil.copyfile(args.flash, work / "flash.bin")
    if args.sd is not None:
        shutil.copyfile(args.sd, work / "sd.img")
    sock = work / "qmp.sock"
    if sock.exists():
        sock.unlink()
    command = qemu_command(args, work, work / "rom", sock)
    process = subprocess.Popen(command, stdin=subprocess.DEVNULL,
                               stdout=(work / "qemu.stdout").open("wb"),
                               stderr=(work / "qemu.stderr").open("wb"))
    client = qmp = reader = None
    error = None
    samples = []
    registers = {}
    try:
        client, reader, qmp = qmp_connect(sock)
        time.sleep(args.settle)
        for round_number in range(args.rounds):
            if round_number:
                time.sleep(args.round_seconds)
            samples.append({"round": round_number,
                            "rtc": probe_rtc(qmp, args.offsets),
                            "poll_literal": read_poll_literal(qmp, args.literal_paddr)})
        for cpu in (0, 1):
            snapshot = sample_cpu(qmp, cpu)
            registers[f"cpu{cpu}"] = {**snapshot,
                                       "signature": classify_pc(snapshot["pc"],
                                                               INKMOD_WAIT_RANGES)}
    except Exception as failure:
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
    report = {
        "probe": "inkmod_forced_wake/probe",
        "flash_sha256": sha256_file(args.flash),
        "rom_sha256": sha256_file(args.rom),
        "settle_seconds": args.settle,
        "rounds": args.rounds,
        "samples": samples,
        "registers": registers,
        "candidates": candidate_table(samples),
    }
    if error is not None:
        report["error"] = error
    (work / "probe.json").write_text(json.dumps(report, indent=1))
    return report


def candidate_table(samples: list[dict]) -> list[dict]:
    """Annotate each substitute offset with what the guest actually reads there."""
    names = dict(irc.WAKE_SUBSTITUTES)
    table = []
    for offset, name in irc.WAKE_SUBSTITUTES:
        values = [next(word["value"] for word in round_sample["rtc"]
                       if word["offset"] == f"0x{offset:02x}")
                  for round_sample in samples]
        if not values:
            table.append({"offset": f"0x{offset:02x}",
                          "address": f"0x{RTC_BASE + offset:08x}",
                          "description": name, "values": [],
                          "changing": False, "wake_bits_set": False,
                          "nonzero": False, "sampled": False})
            continue
        table.append({
            "offset": f"0x{offset:02x}",
            "address": f"0x{RTC_BASE + offset:08x}",
            "description": name,
            "values": values,
            "sampled": True,
            "changing": len(set(values)) > 1,
            "wake_bits_set": bool(values[0] & WAKE_BITS),
            "nonzero": bool(values[0]),
        })
    return table


def choose_substitute(table: list[dict]) -> int | None:
    """Pick the offset whose observed reads can satisfy the wake mask.

    A changing value wins outright: the poll spins, so it will catch a set bit on
    some iteration whatever the mask. Otherwise a value with SLP_WAKEUP/SLP_REJECT
    already set, then any nonzero value, then nothing (the experiment is inert).
    """
    for key in ("changing", "wake_bits_set", "nonzero"):
        for row in table:
            if row[key]:
                return int(row["offset"], 16)
    return None


def tracer_run(args, flash: Path, label: str, out: Path) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    command = [sys.executable, str(TRACER),
               "--qemu", str(args.qemu), "--rom", str(args.rom),
               "--flash", str(flash), "--label", label,
               "--profile", "inkmod", "--seconds", str(args.seconds),
               "--sample-ms", str(args.sample_ms), "--exec-trace",
               "--machine", args.machine, "--out", str(out)]
    if args.sd is not None:
        command += ["--sd", str(args.sd)]
    completed = subprocess.run(command, capture_output=True, text=True)
    report = out / "report.json"
    if not report.is_file():
        raise RuntimeError(f"{label} produced no report.json "
                           f"(exit {completed.returncode}): {completed.stderr[-400:]}")
    data = json.loads(report.read_text())
    data["tracer_exit"] = completed.returncode
    return data


def verdict(control: dict, forced: dict, substitute: dict | None) -> dict:
    if substitute is None:
        return {"h1": "NOT MEASURED",
                "reason": "no modelled RTC offset reads nonzero, so the poll "
                          "cannot be redirected without patching the emulator"}
    control_poll = control.get("exec_trace", {}).get("hits", {}).get("rtc_int_raw_poll")
    forced_poll = forced.get("exec_trace", {}).get("hits", {}).get("rtc_int_raw_poll")
    commands = forced.get("epd", {}).get("commands", 0)
    frames = forced.get("epd", {}).get("frames", 0)
    moved = (forced["verdict"]["verdict"] != control["verdict"]["verdict"]
             or forced_poll is None or forced_poll < control_poll)
    return {
        "h1": "CONFIRMED" if commands or frames else (
            "SUPPORTED" if moved else "REFUTED"),
        "control_verdict": control["verdict"]["verdict"],
        "forced_verdict": forced["verdict"]["verdict"],
        "control_poll_hits": control_poll,
        "forced_poll_hits": forced_poll,
        "forced_epd_commands": commands,
        "forced_epd_frames": frames,
        "forced_cpu1_tail": forced["verdict"].get("cpu1_tail"),
        "substitute": substitute,
        "note": "CONFIRMED requires the panel to see traffic; SUPPORTED means the "
                "poll released but the paint path is still not reached, which "
                "moves the gate downstream; REFUTED means the redirect changed "
                "nothing",
    }


def experiment(args) -> dict:
    work = Path(args.out)
    work.mkdir(parents=True, exist_ok=True)
    probe_report = json.loads(Path(args.probe).read_text()) \
        if args.probe else probe(args)
    table = probe_report["candidates"]
    result = {"probe": probe_report}
    if probe_report.get("error") or not probe_report.get("samples"):
        result["conclusion"] = {
            "h1": "NOT MEASURED",
            "reason": "the RTC probe pass did not complete, so no substitution "
                      "target could be measured",
            "probe_error": probe_report.get("error"),
        }
        (work / "experiment.json").write_text(json.dumps(result, indent=1))
        return result
    offset = choose_substitute(table)
    chosen = next((row for row in table
                   if row["offset"] == f"0x{offset:02x}"), None) if offset else None
    if args.flash_variant:
        variant = Path(args.flash_variant)
        chosen = chosen or {"offset": "supplied", "description":
                            "image passed with --flash-variant, offset not probed"}
    elif offset is None:
        variant = None
    else:
        variant = irc.build_variant(
            args.flash, work, args.literal_vaddr, RTC_BASE + offset)
        result["variant"] = variant
        variant = Path(variant["variant_path"])
    result["substitute"] = chosen
    if variant is not None:
        control = json.loads(Path(args.control).read_text()) \
            if args.control else tracer_run(args, args.flash, "inkmod-pristine",
                                            work / "inkmod-pristine")
        forced = tracer_run(args, variant, "inkmod-forced-wake",
                            work / "inkmod-forced-wake")
        result["control"] = {"verdict": control["verdict"],
                             "exec_trace": control.get("exec_trace"),
                             "epd": control.get("epd")}
        result["forced"] = {"verdict": forced["verdict"],
                            "exec_trace": forced.get("exec_trace"),
                            "epd": forced.get("epd"),
                            "uart": forced["uart"]["text"][:4000]}
        result["conclusion"] = verdict(control, forced, chosen)
    else:
        result["conclusion"] = verdict({}, {}, None)
    (work / "experiment.json").write_text(json.dumps(result, indent=1))
    return result


def args_for(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=("probe", "experiment"))
    parser.add_argument("--qemu", type=Path, required=True)
    parser.add_argument("--rom", type=Path, required=True)
    parser.add_argument("--flash", type=Path, required=True)
    parser.add_argument("--sd", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--machine", default=MACHINE)
    parser.add_argument("--settle", type=float, default=20.0,
                        help="seconds to let the guest boot before probing")
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--round-seconds", type=float, default=2.0)
    parser.add_argument("--offsets", type=lambda text: [int(word, 16) for word in text.split(",")],
                        default=[0x00, 0x0c, 0x10, 0x14, 0x38, 0x44, 0x50, 0x54])
    parser.add_argument("--literal-vaddr", type=lambda text: int(text, 16),
                        default=0x40374B7C)
    parser.add_argument("--literal-paddr", type=lambda text: int(text, 16),
                        default=None,
                        help="guest physical alias of the literal, for read-back")
    parser.add_argument("--seconds", type=float, default=60.0)
    parser.add_argument("--sample-ms", type=int, default=250)
    parser.add_argument("--probe", type=Path,
                        help="reuse this probe.json instead of running a probe pass")
    parser.add_argument("--control", type=Path,
                        help="reuse this pristine report.json as the control")
    parser.add_argument("--flash-variant", type=Path,
                        help="use this already-built image instead of building one")
    args = parser.parse_args(argv)
    if args.literal_paddr is None:
        args.literal_paddr = args.literal_vaddr
    args.exec_trace = True
    return args


def main(argv=None) -> int:
    args = args_for(argv)
    report = probe(args) if args.command == "probe" else experiment(args)
    print(json.dumps(report, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
