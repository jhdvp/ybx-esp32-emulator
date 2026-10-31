#!/usr/bin/env python3
"""Measure the FreeRTOS wake tick of the EPD render task around one GT911 contact.

Answers the question experiment 0037 §4.1 left open — does the shipped guest
wait on the 20 ms poll branch (EpdBus.cpp:445) or the 30 s timeout branch
(EpdBus.cpp:460)? — with a number:

    wakeTick = *(uint32_t *)(xStateListItem + 0x00)  // TCB + 0x04
    remaining = wakeTick - xTickCount                 // 0x3fcabf94

`prvAddCurrentTaskToDelayedList` writes `xTimeToWake = xConstTickCount +
xTicksToWait` into the state list item (tasks.c:6393), so `remaining` is the
timeout still in force. 20 ticks can only come from the first branch, 30000
only from the second. A `portMAX_DELAY` wait goes to xSuspendedTaskList and
carries no tick at all (tasks.c:6381-6386); this script reports that instead
of inventing a number.

Read-only against guest RAM (QMP `xp /Nwx`) plus one real GT911 contact through
the existing QMP sensor properties. It does NOT touch the framebuffer and is
NOT a UI claim: panel correctness stays with probe_x4pro_guest_touch_ui.py.
This script is for the orchestrator, who owns the live emulator — the lane that
wrote it never started a guest.

Run it wherever the guest's QMP socket is reachable. For the Docker emulator
that means inside the container, where /tests is this directory:

    docker cp tools/ybx-system-emu/probe_x4pro_freertos_wake.py \\
      ybx-x4pro-browser-8784:/tests/probe_x4pro_freertos_wake.py
    docker exec ybx-x4pro-browser-8784 sh -c 'cd /tests && python3 \\
      probe_x4pro_freertos_wake.py \\
      --qmp "$(ls -d /tmp/ybx-x4pro-browser-*/qmp.sock | head -1)" \\
      --flash /work/s3-flash-complete-rom-test.bin \\
      --contact more --samples 6 --sample-interval 0.25 \\
      --out /tmp/freertos-wake.json'
    docker cp ybx-x4pro-browser-8784:/tmp/freertos-wake.json .

Adding `--elf /path/to/x4pro/firmware.elf` replaces the recorded statics with
addresses discovered from the ELF symbol table; without it the layout is the
recorded factory 092c2be5 one, and the report says so. The flash SHA gate is
mandatory: a digest mismatch exits 2 rather than decoding another build with
factory offsets.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import socket
import sys
import time
from pathlib import Path

import freertos_guest_probe as rtos

# Raw GT911 coordinates verified against the accepted SSD1677 frames.
CONTACTS = {
    "more": (350, 760),
    "ybx-settings": (240, 453),
    "system-settings": (210, 679),
    "back": (35, 61),
}


def qmp_connect(path=None, host=None, port=None, timeout=10.0):
    """Attach to an already-running QEMU monitor over unix or tcp."""
    deadline = time.monotonic() + timeout
    client = None
    while True:
        if path is not None:
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            client.settimeout(timeout)
            try:
                client.connect(str(path))
                break
            except (ConnectionRefusedError, FileNotFoundError, OSError):
                client.close()
        else:
            client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            client.settimeout(timeout)
            try:
                client.connect((host, port))
                break
            except OSError:
                client.close()
        if time.monotonic() > deadline:
            raise SystemExit(f"cannot reach QMP at {path or (host, port)}")
        time.sleep(0.05)
    reader = client.makefile("r", encoding="utf-8", newline="\n")
    greeting = json.loads(reader.readline())
    if "QMP" not in greeting:
        raise SystemExit(f"not a QMP greeting: {greeting}")

    def execute(command, arguments=None):
        payload = {"execute": command}
        if arguments is not None:
            payload["arguments"] = arguments
        client.sendall((json.dumps(payload) + "\n").encode())
        while True:
            line = reader.readline()
            if not line:
                raise SystemExit("QMP closed the connection")
            data = json.loads(line)
            if "event" in data:
                continue
            return data

    if "return" not in execute("qmp_capabilities"):
        raise SystemExit("qmp_capabilities rejected")
    return client, reader, execute


def sensor(execute, prop, value):
    reply = execute("qom-set", {"path": "/machine/soc",
                                 "property": "ybx-touch-" + prop,
                                 "value": value})
    return {"ok": "return" in reply, "reply": reply}


def wake_of(snap):
    block = (snap or {}).get("render_task_block") or {}
    return block.get("wake") or {}


def tick_rate(samples):
    """Observed xTickCount per wall-clock second, as a tick-rate check.

    The 20/30000 windows are tick counts; classifying them assumes the 1000 Hz
    rate the folded `pdMS_TO_TICKS` immediates imply. Sampling the same guest
    over wall time lets the assumption be checked rather than asserted.
    """
    points = [(s["wall"], s["snapshot"]["x_tick_count"]) for s in samples
              if s["snapshot"].get("x_tick_count") is not None]
    rate = None
    if len(points) >= 2:
        elapsed = points[-1][0] - points[0][0]
        ticks = (points[-1][1] - points[0][1]) % rtos.TICK_SPACE
        if elapsed > 0:
            rate = {"ticks": ticks, "seconds": round(elapsed, 3),
                    "ticks_per_second": round(ticks / elapsed, 1)}
    return {"points": len(points), "measured": rate,
            "assumed_hz": 1000,
            "note": "assumed from folded pdMS_TO_TICKS immediates; "
                    "measured value above corroborates or refutes it"}


def verdict(samples, pair):
    """The strongest statement the collected wake samples support."""
    after = [s for s in samples if s["phase"] != "before"]
    readable = [s for s in after if wake_of(s["snapshot"]).get(
        "wake_ticks_remaining") is not None]
    out = {
        "render_task_reachable": any(
            s["snapshot"].get("available") for s in samples),
        "render_task_woken_first_after": pair.get("render_task_woken"),
        "wake_ticks_remaining_after_max": None,
        "epd_wait_branch": None,
        "epd_wait_candidates": [],
        "answer": "not-measured",
        "reason": "",
    }
    if not any(s["snapshot"].get("available") for s in after):
        out["reason"] = ("render task not reachable in any post-contact sample; "
                        "see task_list_walk and read_errors")
        return out
    if not readable:
        states = {wake_of(s["snapshot"]).get("state_list") for s in after}
        out["post_contact_states"] = sorted(str(state) for state in states)
        out["wake_tick_meanings"] = sorted({
            str(wake_of(s["snapshot"]).get("wake_tick_meaning"))
            for s in after})
        out["reason"] = ("render task was never caught in a delayed wait after "
                        "the contact, so no wake tick exists to classify")
        return out
    best = max(readable,
               key=lambda s: wake_of(s["snapshot"])["wake_ticks_remaining"])
    remaining = wake_of(best["snapshot"])["wake_ticks_remaining"]
    out["wake_ticks_remaining_after_max"] = remaining
    out["epd_wait_branch"] = wake_of(best["snapshot"]).get("epd_wait_branch")
    out["epd_wait_candidates"] = wake_of(best["snapshot"]).get(
        "candidate_branches", [])
    if wake_of(best["snapshot"]).get("distinguishable"):
        out["answer"] = out["epd_wait_branch"]
        out["reason"] = wake_of(best["snapshot"]).get("reason", "")
    else:
        out["answer"] = "ambiguous"
        out["reason"] = (wake_of(best["snapshot"]).get("reason", "") +
                         "; sample earlier or faster after the contact")
    return out


def main():
    parser = argparse.ArgumentParser(
        description="Wake-tick measurement for the EPD render task.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--qmp", type=Path, help="QMP unix socket of a live guest")
    group.add_argument("--qmp-host", help="QMP tcp host (with --qmp-port)")
    parser.add_argument("--qmp-port", type=int, help="QMP tcp port")
    sha = parser.add_mutually_exclusive_group(required=True)
    sha.add_argument("--flash", type=Path,
                     help="guest flash image; hashed and checked against the "
                          "factory SHA")
    sha.add_argument("--flash-sha256", help="digest reported by the emulator")
    parser.add_argument("--elf", type=Path,
                        help="factory firmware.elf; discover the kernel layout "
                             "instead of using the recorded addresses")
    parser.add_argument("--qemu-bin", type=Path,
                        help="hash this emulator binary into the report")
    parser.add_argument("--contact", choices=sorted(CONTACTS), default="more")
    parser.add_argument("--raw-x", type=int, help="override the contact X")
    parser.add_argument("--raw-y", type=int, help="override the contact Y")
    parser.add_argument("--task-name", default="ActivityManagerRender")
    parser.add_argument("--press-seconds", type=float, default=0.4)
    parser.add_argument("--samples", type=int, default=6,
                        help="post-contact snapshots (the first one is the "
                             "decisive one: it sees the wait still running)")
    parser.add_argument("--sample-interval", type=float, default=0.2)
    parser.add_argument("--skip-contact", action="store_true",
                        help="passive before/after pair only, no touch injected")
    parser.add_argument("--out", type=Path, help="write JSON here as well")
    args = parser.parse_args()
    if (args.qmp_host is None) != (args.qmp_port is None):
        parser.error("--qmp-host and --qmp-port belong together")

    digest = (hashlib.sha256(args.flash.read_bytes()).hexdigest()
              if args.flash else args.flash_sha256.lower())
    if digest != rtos.FACTORY_092C2BE5_FLASH_SHA256:
        print(f"Refusing to decode: flash sha256 {digest} is not the factory "
              f"{rtos.FACTORY_092C2BE5_FLASH_SHA256}. Recorded kernel statics "
              "do not apply to another build.", file=sys.stderr)
        return 2

    layout = None
    layout_note = "recorded factory 092c2be5 statics (no --elf given)"
    if args.elf:
        try:
            layout = rtos.KernelLayout.from_elf(args.elf)
            layout_note = f"discovered from {args.elf}"
        except rtos.LayoutError as error:
            print(f"ELF layout discovery failed: {error}", file=sys.stderr)
            return 2

    base = CONTACTS[args.contact]
    raw = (base[0] if args.raw_x is None else args.raw_x,
           base[1] if args.raw_y is None else args.raw_y)
    client, reader, execute = qmp_connect(args.qmp, args.qmp_host, args.qmp_port)
    samples = []
    injections = {}
    try:
        first = rtos.snapshot(execute, args.task_name, layout)
        samples.append({"phase": "before", "wall": time.monotonic(),
                        "snapshot": first})
        if not args.skip_contact:
            injections["set-x"] = sensor(execute, "x", raw[0])
            injections["set-y"] = sensor(execute, "y", raw[1])
            injections["press"] = sensor(execute, "state", 1)
            time.sleep(args.press_seconds)
            injections["release"] = sensor(execute, "state", 0)
        for index in range(args.samples):
            if index:
                time.sleep(args.sample_interval)
            samples.append({"phase": f"after-{index + 1}",
                            "wall": time.monotonic(),
                            "snapshot": rtos.snapshot(execute, args.task_name,
                                                      layout)})
    finally:
        client.close()

    before = samples[0]["snapshot"]
    after = next((s["snapshot"] for s in samples[1:]), None)
    pair = rtos.assess_render_wake(before, after)
    custom = args.raw_x is not None or args.raw_y is not None
    report = {
        "measured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "factory_flash_sha256": digest,
        "factory_build_sha_gate_matched": True,
        "qemu_sha256": (hashlib.sha256(args.qemu_bin.read_bytes()).hexdigest()
                        if args.qemu_bin else None),
        "layout_source": layout_note,
        "layout": (layout or rtos.DEFAULT_LAYOUT).to_dict(),
        "contact": {"raw_gt911_xy": list(raw), "skipped": args.skip_contact,
                    "press_seconds": args.press_seconds,
                    "nominal_target": "custom" if custom else args.contact},
        "injection_results": injections,
        "task_name_needle": args.task_name,
        "samples": [{
            "phase": s["phase"],
            "seconds_from_start": round(s["wall"] - samples[0]["wall"], 3),
            "available": s["snapshot"].get("available"),
            "reason": s["snapshot"].get("reason"),
            "x_tick_count": s["snapshot"].get("x_tick_count"),
            "x_next_task_unblock_time":
                s["snapshot"].get("x_next_task_unblock_time"),
            "task_count": s["snapshot"].get("task_count"),
            "render_task": s["snapshot"].get("render_task"),
            "render_task_block": s["snapshot"].get("render_task_block"),
            "wake": wake_of(s["snapshot"]),
        } for s in samples],
        "task_list_walk": [
            {"phase": s["phase"], "walk": s["snapshot"].get("task_list_walk"),
             "read_errors": s["snapshot"].get("read_errors", [])}
            for s in samples],
        "epd_refresh_done_semaphore_handle":
            before.get("epd_refresh_done_semaphore_handle"),
        "epd_busy_gpio": before.get("epd_busy_gpio"),
        "render_task_woken_after_contact": pair,
        "tick_rate_check": tick_rate(samples),
        "verdict": verdict(samples, pair),
        "not_a_framebuffer_claim": (
            "This measures scheduler state only. It neither proves nor denies "
            "that any pixel changed; panel acceptance stays with "
            "probe_x4pro_guest_touch_ui.py and the recorded frame SHAs."),
    }
    text = json.dumps(report, indent=2, sort_keys=True)
    print(text)
    if args.out:
        args.out.write_text(text + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
