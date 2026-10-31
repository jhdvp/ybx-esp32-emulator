#!/usr/bin/env python3
"""Multi-step original-firmware GUI navigation acceptance for the X4 Pro guest.

Two channels, one acceptance plan (`x4pro_guest_navigation.py`):

1. Native (default): launches its OWN patched QEMU in a private temporary
   directory and drives synthetic GT911 contacts through QMP sensor properties,
   judging each step against the actual SSD1677 RAM read back over QMP, with
   guest I2C consumption counters. Expect roughly boot-seconds plus a few
   seconds per step of wall time. It never connects to the shared browser
   server, its /api/tap endpoint, or its Docker containers, because those are
   a single long-lived emulator other work depends on.

2. Browser (`--base-url`): drives a `serve_x4pro_browser.py`-compatible
   instance over HTTP (/api/state, /screen.png, /api/tap). There is no default
   URL: without an explicit --base-url the browser mode refuses to run, so it
   can never fall onto the orchestrator's live container by accident. That
   channel reports no GT911 I2C counters, so its steps are recorded with
   `full_acceptance: false`; every frame/SHA/PNG comes from the instance and a
   missing readout fails loudly instead of falling back to a recorded
   expectation.

Both channels write per-step PNGs, per-step frame SHAs and a machine-readable
JSON verdict, and both stop claiming success on any step whose panel image did
not actually change.
"""
import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
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
    steps = nav.ordered_acceptance_plan(
        a.sequence, require_accepted_shas=getattr(a, "require_accepted_shas", False))
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
    verdict = nav.assess_sequence(results, steps)
    report = {
        "channel": "native_qmp",
        "factory_flash_sha256": flash_sha,
        "factory_build_sha_gate_matched": flash_sha == FACTORY_0025_FLASH_SHA256,
        "qemu_sha256": hashlib.sha256(a.qemu_bin.read_bytes()).hexdigest(),
        "sequence": a.sequence,
        "configuration": {
            "boot_seconds": a.boot_seconds, "press_seconds": a.press_seconds,
            "after_seconds": a.after_seconds, "settle_seconds": a.settle_seconds,
            "steps": [{"name": s["name"], "target": s["target"],
                       "raw_gt911_xy": list(nav.TARGETS[s["target"]]["raw_xy"]),
                       "expected_screen_after": s["expect_after"],
                       "expected_sha256": s["expected_sha256"],
                       "requires_accepted_sha": s["requires_accepted_sha"]}
                      for s in steps],
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


STATE_KEYS = ("frames", "nonwhite", "ready", "sha256")


class BrowserError(RuntimeError):
    pass


class HttpTransport:
    """Minimal urllib transport; the only place the browser channel touches a socket."""

    def __init__(self, base_url: str, timeout: float):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _open(self, request):
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                status = getattr(response, "status", 200)
                body = response.read()
        except urllib.error.HTTPError as exc:
            status, body = exc.code, exc.read()
        except urllib.error.URLError as exc:
            raise BrowserError(f"instance unreachable: {exc.reason}") from exc
        return status, body

    def get_json(self, path: str) -> dict:
        status, body = self._open(urllib.request.Request(self.base_url + path, method="GET"))
        if status != 200:
            raise BrowserError(f"GET {path} -> HTTP {status}: {body[:200]!r}")
        try:
            return json.loads(body)
        except ValueError as exc:
            raise BrowserError(f"GET {path} returned non-JSON: {body[:200]!r}") from exc

    def get_bytes(self, path: str) -> bytes:
        status, body = self._open(urllib.request.Request(self.base_url + path, method="GET"))
        if status != 200:
            raise BrowserError(f"GET {path} -> HTTP {status}")
        return body

    def post_json(self, path: str, payload: dict) -> tuple[int, dict]:
        data = json.dumps(payload).encode()
        if len(data) > 160:
            raise BrowserError("tap payload exceeds the server's 160-byte limit")
        request = urllib.request.Request(
            self.base_url + path, data=data, method="POST",
            headers={"Content-Type": "application/json"})
        status, body = self._open(request)
        try:
            parsed = json.loads(body)
        except ValueError:
            parsed = {"raw": body[:200]}
        return status, parsed


def validate_base_url(base_url) -> str:
    if not isinstance(base_url, str) or not base_url.strip():
        raise ValueError("--base-url must be an explicit non-empty http(s) URL; "
                         "this tool never guesses a running instance")
    if not re.match(r"https?://[^\s/]+", base_url):
        raise ValueError(f"--base-url {base_url!r} is not an http(s) URL")
    return base_url


def browser_state(transport) -> dict:
    """Read the instance's report of the actual panel; never fabricate it."""
    state = transport.get_json("/api/state")
    for key in STATE_KEYS:
        if key not in state:
            raise BrowserError(f"/api/state response is missing {key!r}: {state!r}")
    return state


def browser_capture(transport, out: Path, png_stem: str, label: str) -> dict:
    """Snapshot: counters, landscape SHA, real portrait pixels, screen identity."""
    state = browser_state(transport)
    png_bytes = transport.get_bytes("/screen.png")
    if not png_bytes:
        raise BrowserError("/screen.png returned an empty body; no frame to judge")
    width, height, packed = nav.portrait_from_mono_png(png_bytes)
    if (width, height) != (nav.PORTRAIT_WIDTH, nav.PORTRAIT_HEIGHT):
        raise BrowserError(f"/screen.png is {width}x{height}, "
                           f"expected {nav.PORTRAIT_WIDTH}x{nav.PORTRAIT_HEIGHT}")
    png = out.with_name(f"{png_stem}-{label}.png")
    png.write_bytes(png_bytes)
    return {
        "label": label,
        "frames": state["frames"],
        "nonwhite": state["nonwhite"],
        "ready": bool(state["ready"]),
        "frame_sha256": state["sha256"],
        "portrait_sha256": hashlib.sha256(packed).hexdigest(),
        "png": str(png),
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "screen_state": nav.screen_state(packed),
        "more_dialog_visible": nav.more_dialog_visible(packed),
        "home_screen_visible": nav.home_screen_visible(packed),
        "ybx_settings_list_visible": nav.ybx_settings_list_visible(packed),
        "system_settings_list_visible": nav.system_settings_list_visible(packed),
    }


def browser_tap(transport, x: int, y: int, duration_ms: int) -> dict:
    """One synthetic GT911 contact through /api/tap; the server validates bounds."""
    status, body = transport.post_json("/api/tap", {"x": int(x), "y": int(y),
                                                    "duration_ms": int(duration_ms)})
    return {"http_status": status, "accepted": status == 200 and body.get("accepted") is True,
            "response": body}


def run_browser_plan(transport, plan, out: Path, png_stem: str, *,
                     sleep=time.sleep, settle_seconds: float = 12.0,
                     poll_seconds: float = 0.4, tap_duration_ms: int = 150) -> dict:
    """Execute the ordered plan over the browser channel; stop at the first failed step."""
    results = []
    stopped_at = None
    for index, step in enumerate(plan, start=1):
        previous = browser_capture(transport, out, png_stem, f"step{index}before")
        if not previous["ready"]:
            results.append({"step": step["name"], "target": step["target"],
                            "raw_xy": step["raw_xy"], "passed": False,
                            "failure_codes": ["guest_not_ready"],
                            "screen_before": previous["screen_state"],
                            "screen_after": previous["screen_state"],
                            "frame_sha256_before": previous["frame_sha256"],
                            "frame_sha256_after": previous["frame_sha256"]})
            stopped_at = step["name"]
            break
        contact = browser_tap(transport, step["raw_xy"][0], step["raw_xy"][1],
                              tap_duration_ms)
        after = browser_capture(transport, out, png_stem, f"step{index}settling")
        deadline = time.monotonic() + settle_seconds
        while (after["frames"] <= previous["frames"]
               or after["frame_sha256"] == previous["frame_sha256"]):
            if time.monotonic() >= deadline:
                break
            sleep(poll_seconds)
            after = browser_capture(transport, out, png_stem, f"step{index}settling")
        sleep(1.0)
        final = browser_capture(transport, out, png_stem, f"step{index}after")
        result = nav.assess_step(step, previous, final, {}, {}, {"api-tap": contact})
        result["tap_http"] = {"status": contact["http_status"],
                             "accepted": contact["accepted"]}
        results.append(result)
        if not result["passed"]:
            stopped_at = step["name"]
            break
    return {"steps": results,
            "verdict": nav.assess_sequence(results, plan),
            "order_check": nav.validate_step_order(plan, results),
            "stopped_at_step": stopped_at}


def run_browser(a, parser, *, transport_factory=HttpTransport, sleep=time.sleep):
    """Browser-channel entry: explicit URL only, per-step PNGs + JSON verdict."""
    try:
        base_url = validate_base_url(a.base_url)
    except ValueError as exc:
        print(f"refusing to run: {exc}", file=sys.stderr)
        return 2
    host = re.sub(r"^https?://", "", base_url).split("/", 1)[0].split(":", 1)[0]
    if host in ("127.0.0.1", "localhost", "::1") and not a.allow_loopback:
        print("refusing to run: a loopback instance needs --allow-loopback as a "
              "deliberate second choice", file=sys.stderr)
        return 2
    if a.out is None:
        parser.error("the browser channel requires --out")
    try:
        plan = nav.ordered_acceptance_plan(a.sequence,
                                           require_accepted_shas=a.require_accepted_shas)
    except ValueError as exc:
        print(f"invalid acceptance plan: {exc}", file=sys.stderr)
        return 2
    a.out.parent.mkdir(parents=True, exist_ok=True)
    transport = transport_factory(base_url, a.timeout_seconds)
    try:
        outcome = run_browser_plan(transport, plan, a.out, a.out.stem,
                                   sleep=sleep, settle_seconds=a.settle_seconds,
                                   poll_seconds=a.poll_seconds,
                                   tap_duration_ms=a.tap_duration_ms)
    except BrowserError as exc:
        report = {"channel": "browser_http", "base_url": base_url,
                  "sequence": a.sequence, "error": str(exc),
                  "steps": [], "navigation_sequence_verified": False,
                  "strict_order_ok": False,
                  "guest_gt911_i2c_consumption_observed": False,
                  "full_acceptance": False}
        a.out.write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(report, indent=2), flush=True)
        print(f"instance error: {exc}", file=sys.stderr)
        return 3
    report = {
        "channel": "browser_http",
        "base_url": base_url,
        "sequence": a.sequence,
        "require_accepted_shas": a.require_accepted_shas,
        "configuration": {
            "settle_seconds": a.settle_seconds, "poll_seconds": a.poll_seconds,
            "tap_duration_ms": a.tap_duration_ms,
            "steps": [{"index": s["index"], "name": s["name"], "target": s["target"],
                       "raw_gt911_xy": s["raw_xy"],
                       "expected_screen_before": s["expect_before"],
                       "expected_screen_after": s["expect_after"],
                       "expected_sha256": s["expected_sha256"],
                       "requires_accepted_sha": s["requires_accepted_sha"]}
                      for s in plan],
        },
        "target_evidence": {name: target["evidence"] for name, target in nav.TARGETS.items()},
        "accepted_screen_shas": dict(nav.ACCEPTED_SCREEN_SHAS),
        "steps": outcome["steps"],
        "stopped_at_step": outcome["stopped_at_step"],
        "order_check": outcome["order_check"],
        **outcome["verdict"],
        "guest_gt911_i2c_consumption_observed": False,
        "full_acceptance": False,
        "physical_touch_verified": False,
        "guest_wifi_emulated": False,
        "native_qmp_launch_used": False,
        "shared_browser_server_used": True,
    }
    a.out.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2), flush=True)
    verified = bool(outcome["verdict"]["navigation_sequence_verified"])
    return 0 if verified and outcome["stopped_at_step"] is None else 1


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--base-url", default=None,
                   help="switch to the browser channel: http(s) URL of a "
                        "serve_x4pro_browser.py instance. There is no default; "
                        "browser mode refuses to run without an explicit URL.")
    for key in ("qemu-bin", "rom", "flash", "sd-image", "out"):
        p.add_argument("--" + key, type=Path, required=False)
    p.add_argument("--sequence", default=nav.DEFAULT_SEQUENCE,
                   choices=sorted(nav.SEQUENCES))
    p.add_argument("--require-accepted-shas", action="store_true",
                   help="hard-gate each step on the accepted SSD1677 SHA of "
                        "the expected screen (default: record the gate outcome "
                        "but do not fail on a legitimate repaint)")
    p.add_argument("--boot-seconds", type=float, default=33.0,
                   help="verified 0042 native run reached home at 33s")
    p.add_argument("--press-seconds", type=float, default=0.15)
    p.add_argument("--after-seconds", type=float, default=2.0,
                   help="verified 0042 native repaint delay after release")
    p.add_argument("--settle-seconds", type=float, default=8.0,
                   help="bounded wait for the panel refresh count to advance")
    p.add_argument("--poll-seconds", type=float, default=0.25)
    p.add_argument("--home-timeout-seconds", type=float, default=30.0)
    p.add_argument("--timeout-seconds", type=float, default=10.0,
                   help="browser channel: per-request HTTP timeout")
    p.add_argument("--tap-duration-ms", type=int, default=150,
                   help="browser channel: /api/tap hold time (server allows 60..500)")
    p.add_argument("--allow-loopback", action="store_true",
                   help="browser channel: permit a 127.0.0.1/localhost instance "
                        "(only your own; a deliberate second choice)")
    p.add_argument("--allow-non-home-start", dest="require_home",
                   action="store_false", default=True,
                   help="start steps even when the guest never reached home")
    a = p.parse_args()
    if a.base_url is not None:
        raise SystemExit(run_browser(a, p))
    missing = [flag for flag in ("qemu_bin", "rom", "flash", "sd_image", "out")
               if getattr(a, flag) is None]
    if missing:
        p.error("native channel requires --" + " --".join(
            m.replace("_", "-") for m in missing)
            + " (or pass --base-url to use the browser channel)")
    report = run(a)
    raise SystemExit(0 if report["navigation_sequence_verified"] else 1)


if __name__ == "__main__":
    main()
