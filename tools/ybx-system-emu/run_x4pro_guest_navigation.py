#!/usr/bin/env python3
"""Multi-step original-firmware GUI navigation acceptance for the X4 Pro guest.

Drives one synthetic GT911 contact per navigation step through the patched
QEMU's QMP sensor properties and judges each step against the actual SSD1677
RAM read back over QMP: per-step frame SHAs, per-step portrait PNGs, guest I2C
consumption counters, and measured image predicates for the screen reached.

This launches its OWN QEMU process in a private temporary directory. It never
connects to the shared browser server, its /api/tap endpoint, or its Docker
containers, because those are a single long-lived emulator other work depends
on. Expect roughly boot-seconds plus a few seconds per step of wall time.
"""
import argparse
import hashlib
import json
import shutil
import subprocess
import time
from pathlib import Path
from tempfile import TemporaryDirectory

from probe_x4pro_guest_touch_ui import (FACTORY_0025_FLASH_SHA256, get,
                                        qemu_command, sensor, touch_metrics)
from probe_x4pro_touch_injection_qtest import qmp_connect
from epd_frame_io import save_mono_png, ssd1677_landscape_to_portrait
from probe_x4pro_epd_qtest import SIZE
import x4pro_guest_navigation as nav


def capture(qmp, label, out: Path, png_stem: str):
    """Read the real panel: counters, SHAs, portrait pixels, screen identity."""
    state = {key: get(qmp, key) for key in
             ("frames", "nonwhite", "ram-writes", "commands", "busy")}
    r = qmp("qom-get", {"path": "/machine/soc", "property": "ybx-epd-visible-hex"})
    if "return" not in r:
        raise AssertionError(r)
    landscape = bytes.fromhex(r["return"])
    if len(landscape) != SIZE:
        raise AssertionError(f"visible frame is {len(landscape)} bytes, expected {SIZE}")
    portrait = ssd1677_landscape_to_portrait(landscape)
    png = out.with_name(f"{png_stem}-{label}.png")
    save_mono_png(png, portrait, nav.PORTRAIT_WIDTH, nav.PORTRAIT_HEIGHT)
    state.update({
        "label": label,
        "frame_sha256": hashlib.sha256(landscape).hexdigest(),
        "portrait_sha256": hashlib.sha256(portrait).hexdigest(),
        "png": str(png),
        "screen_state": nav.screen_state(portrait),
        "more_dialog_visible": nav.more_dialog_visible(portrait),
        "home_screen_visible": nav.home_screen_visible(portrait),
        "ybx_settings_list_visible": nav.ybx_settings_list_visible(portrait),
        "system_settings_list_visible": nav.system_settings_list_visible(portrait),
    })
    return state


def inject(qmp, raw_xy):
    x, y = raw_xy
    return {"set-x": sensor(qmp, "x", x), "set-y": sensor(qmp, "y", y),
            "press": sensor(qmp, "state", 1)}


def release(qmp):
    return {"release": sensor(qmp, "state", 0)}


def run(a):
    steps = nav.resolve_sequence(a.sequence)
    flash_sha = hashlib.sha256(a.flash.read_bytes()).hexdigest()
    png_stem = a.out.stem
    results = []
    with TemporaryDirectory(prefix="ybx-gt911-nav-") as tmp:
        t = Path(tmp)
        bios = t / "rom"
        bios.mkdir()
        shutil.copyfile(a.rom, bios / "esp32s3_rev0_rom.bin")
        shutil.copyfile(a.flash, t / "factory-flash.bin")
        shutil.copyfile(a.sd_image, t / "sd.img")
        socket = t / "qmp.sock"
        with (t / "out.log").open("w") as stdout, (t / "err.log").open("w") as stderr:
            process = subprocess.Popen(qemu_command(a.qemu_bin, t, socket, bios),
                                       stdin=subprocess.DEVNULL, stdout=stdout,
                                       stderr=stderr)
            client = None
            try:
                client, _, qmp = qmp_connect(socket)
                time.sleep(a.boot_seconds)
                previous = capture(qmp, "boot", a.out, png_stem)
                deadline = time.monotonic() + a.home_timeout_seconds
                while previous["screen_state"] != "home" and time.monotonic() < deadline:
                    time.sleep(a.poll_seconds)
                    previous = capture(qmp, "boot", a.out, png_stem)
                home_ready = previous["screen_state"] == "home"
                boot_frame = previous
                # A guest stuck on a splash or an unsupported Wi-Fi screen is
                # reported with its UART tail rather than raising, so the
                # failing run still produces evidence.
                if home_ready or not a.require_home:
                    for index, step in enumerate(steps, start=1):
                        before_touch = touch_metrics(qmp)
                        injections = inject(qmp, nav.TARGETS[step["target"]]["raw_xy"])
                        time.sleep(a.press_seconds)
                        injections.update(release(qmp))
                        settled = time.monotonic() + a.settle_seconds
                        after = capture(qmp, f"step{index}t", a.out, png_stem)
                        # The panel repaints asynchronously; re-read until the
                        # refresh count moves, never past the settle deadline.
                        while (after["frames"] <= previous["frames"]
                               and time.monotonic() < settled):
                            time.sleep(a.poll_seconds)
                            after = capture(qmp, f"step{index}t", a.out, png_stem)
                        time.sleep(a.after_seconds)
                        after = capture(qmp, f"step{index}a", a.out, png_stem)
                        after_touch = touch_metrics(qmp)
                        results.append(nav.assess_step(step, previous, after,
                                                       before_touch, after_touch,
                                                       injections))
                        previous = after
            finally:
                if client:
                    client.close()
                process.terminate()
                try:
                    process.communicate(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate()
        guest_uart = (t / "out.log").read_text(errors="replace")
        host_error = (t / "err.log").read_text(errors="replace")
    verdict = nav.assess_sequence(results)
    report = {
        "factory_flash_sha256": flash_sha,
        "factory_build_sha_gate_matched": flash_sha == FACTORY_0025_FLASH_SHA256,
        "qemu_sha256": hashlib.sha256(a.qemu_bin.read_bytes()).hexdigest(),
        "sequence": a.sequence,
        "configuration": {
            "boot_seconds": a.boot_seconds, "press_seconds": a.press_seconds,
            "after_seconds": a.after_seconds, "settle_seconds": a.settle_seconds,
            "steps": [{"name": s["name"], "target": s["target"],
                       "raw_gt911_xy": list(nav.TARGETS[s["target"]]["raw_xy"]),
                       "expected_screen_after": s["expect_after"]} for s in steps],
        },
        "target_evidence": {name: target["evidence"]
                            for name, target in nav.TARGETS.items()},
        "boot_frame": boot_frame,
        "home_ready_before_steps": home_ready,
        "steps": results,
        **verdict,
        "failure_diagnostics": nav.failure_diagnostics(results),
        "panel_replay_signals": {
            s["name"]: nav.panel_replay_signal(results, s["replay_of"], s["name"])
            for s in steps if s.get("replay_of")},
        "kernel_counters_are_not_screen_proof": True,
        "guest_wifi_emulated": False,
        "physical_touch_verified": False,
        "shared_browser_server_used": False,
        "uart_tail": guest_uart[-900:],
        "host_stderr_tail": host_error[-1100:],
    }
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2), flush=True)
    return report


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for key in ("qemu-bin", "rom", "flash", "sd-image", "out"):
        p.add_argument("--" + key, type=Path, required=True)
    p.add_argument("--sequence", default="more-to-settings-and-back",
                   choices=sorted(nav.SEQUENCES))
    p.add_argument("--boot-seconds", type=float, default=33.0,
                   help="verified 0042 native run reached home at 33s")
    p.add_argument("--press-seconds", type=float, default=0.15)
    p.add_argument("--after-seconds", type=float, default=2.0,
                   help="verified 0042 native repaint delay after release")
    p.add_argument("--settle-seconds", type=float, default=8.0,
                   help="bounded wait for the panel refresh count to advance")
    p.add_argument("--poll-seconds", type=float, default=0.25)
    p.add_argument("--home-timeout-seconds", type=float, default=30.0)
    p.add_argument("--allow-non-home-start", dest="require_home",
                   action="store_false", default=True,
                   help="start steps even when the guest never reached home")
    a = p.parse_args()
    report = run(a)
    raise SystemExit(0 if report["navigation_sequence_verified"] else 1)


if __name__ == "__main__":
    main()
