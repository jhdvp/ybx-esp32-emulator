#!/usr/bin/env python3
"""Orchestrator-run qtest probe for the 0061 SSD1677 waveform/sleep patch.

NOT run inside the authoring lane: this needs the compiled QEMU rebuilt with
experiments/0061-ssd1677-waveform-sleep.patch; host-side proof of the same
semantics lives in test_x4pro_epd_waveform.py (pure Python mirror).

Two launches of the patched binary:
  * "default" — no -global overrides: the panel must behave exactly like the
    accepted build (350 ms window for power and waveform, RESET pulse keeps
    BUSY low) while the new deep-sleep gate rejects post-sleep painting.
  * "windows" — structured-form globals driver=ybx.ssd1677,property=
    busy-power-ms,value=40 / busy-waveform-ms=350 / busy-on-reset-ms=10, so
    power cost, pixel waveform and post-reset BUSY become separately
    measurable through the busy-*-high-ms aliases.

Machine contract (unchanged by the patch): esp32s3,ybx-x4pro-spi2=on,
ybx-x4pro-epd=on drives the panel through the genuine GP-SPI2 MMIO path with
no flash attached; GPIO13/18/14/6 = CS/DC/RESET/BUSY, panel wiring as in
hw/xtensa/esp32s3.c (ssi_create_peripheral + "ybx.epd.*" aliases).
FreeInk sequences mirrored: Ssd1677Driver.cpp:614-626 powerOffController,
:956-966 deepSleep (0x22 0x03, 0x20, then 0x10 + mode 0x03), EpdBus.cpp:221-226
hardware reset pulse, :381-390 initController order.
"""
from __future__ import annotations

import argparse
import json
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from tempfile import TemporaryDirectory

SPI = 0x60024000
GPIO = 0x60004000
R = {
    "OUT_W1TS": 0x08,
    "OUT_W1TC": 0x0C,
    "ENABLE_W1TS": 0x24,
    "IN": 0x3C,
}
CS, DC, RST, BUSY = 13, 18, 14, 6
BOARD_OUT = (1 << CS) | (1 << DC) | (1 << RST)
MS = 1_000_000

CTRL2_POWER_ON = 0xC0
CTRL2_POWER_OFF = 0x03
CTRL2_WAVEFORM = 0xF7
CMD_DEEP_SLEEP = 0x10
CMD_SW_RESET = 0x12
CMD_MASTER_ACTIVATION = 0x20
CMD_WRITE_RAM = 0x24

WINDOW_POWER_MS = 40    # -global values for the "windows" phase
WINDOW_WAVEFORM_MS = 350
WINDOW_RESET_MS = 10


def deep_sleep_sequence():
    """Ssd1677Driver::deepSleep(): powerOffController, then 0x10 + mode byte.

    Returns (dc_high, bytes) transaction steps, matching bus.cmd/bus.data calls
    at Ssd1677Driver.cpp:618-622 and :965-966 (mode 0x03).
    """
    return [
        (False, [0x22]), (True, [CTRL2_POWER_OFF]), (False, [CMD_MASTER_ACTIVATION]),
        (False, [CMD_DEEP_SLEEP]), (True, [0x03]),
    ]


def paint_sequence(n_data_bytes=4):
    """CTRL2 waveform, RAM write, MASTER_ACTIVATION — the post-sleep paint."""
    return [
        (False, [0x22]), (True, [CTRL2_WAVEFORM]),
        (False, [CMD_WRITE_RAM]), (True, [0x00] * n_data_bytes),
        (False, [CMD_MASTER_ACTIVATION]),
    ]


def byte_count(seq):
    return sum(len(b) for _, b in seq)


class QTest:
    def __init__(self, proc):
        self.proc = proc
        self.commands = 0

    def raw(self, command):
        self.proc.stdin.write(command + "\n")
        self.proc.stdin.flush()
        value = self.proc.stdout.readline().strip()
        self.commands += 1
        if not value.startswith("OK"):
            raise AssertionError(f"QTest {command!r}: {value}")
        parts = value.split()
        return int(parts[1], 0) if len(parts) > 1 else None

    def wr(self, addr, value):
        self.raw(f"writel 0x{addr:x} 0x{value:x}")

    def tick(self, nanoseconds):
        self.raw(f"clock_step {nanoseconds}")


def qmp_connect(path):
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    deadline = time.monotonic() + 5
    while True:
        try:
            client.connect(str(path))
            break
        except (ConnectionRefusedError, FileNotFoundError):
            if time.monotonic() > deadline:
                raise
            time.sleep(0.02)
    reader = client.makefile("r", encoding="utf-8", newline="\n")
    assert "QMP" in json.loads(reader.readline())

    def execute(cmd, args=None):
        payload = {"execute": cmd}
        if args is not None:
            payload["arguments"] = args
        client.sendall((json.dumps(payload) + "\n").encode())
        while True:
            line = reader.readline()
            if not line:
                raise RuntimeError("QMP closed")
            data = json.loads(line)
            if "event" in data:
                continue
            return data

    assert "return" in execute("qmp_capabilities")
    return execute


class Epd:
    def __init__(self, qt, qmp):
        self.qt = qt
        self.qmp = qmp

    def prop(self, name):
        res = self.qmp("qom-get", {"path": "/machine/soc",
                                   "property": "ybx-epd-" + name})
        if "return" not in res:
            raise KeyError(f"{name}: {res}")
        return res["return"]

    def busy(self):
        return self.prop("busy")

    def frames(self):
        return self.prop("frames")

    def send(self, buf, dc):
        self.qt.wr(GPIO + (R["OUT_W1TS"] if dc else R["OUT_W1TC"]), 1 << DC)
        self.qt.wr(SPI + 0x10, 1 << 27)
        self.qt.wr(SPI + 0x1C, len(buf) * 8 - 1)
        for i in range(0, len(buf), 4):
            word = int.from_bytes(bytes(buf[i:i + 4]).ljust(4, b"\0"), "little")
            self.qt.wr(SPI + 0x98 + i, word)
        self.qt.wr(SPI + 0x00, 1 << 24)

    def send_seq(self, seq):
        for dc, buf in seq:
            self.send(buf, dc)

    def activation(self, ctrl2):
        self.send([0x22], dc=False)
        self.send([ctrl2], dc=True)
        self.send([CMD_MASTER_ACTIVATION], dc=False)

    def reset_pulse(self, low_ms=10, high_settle_ms=0):
        """EpdBus::reset() :221-229 restricted to the LOW->HIGH transition."""
        self.qt.wr(GPIO + R["OUT_W1TC"], 1 << RST)
        self.qt.tick(low_ms * MS)
        self.qt.wr(GPIO + R["OUT_W1TS"], 1 << RST)
        if high_settle_ms:
            self.qt.tick(high_settle_ms * MS)

    def release_ms(self, limit_ms):
        """Virtual ms until BUSY reads low, sampled every 1 ms, or None."""
        for step in range(1, limit_ms + 1):
            self.qt.tick(MS)
            if self.busy() == 0:
                return step
        return None


def global_window_args():
    """-global argv fragment for the "windows" phase.

    The short "-global driver.prop=value" form parses the driver with
    "%63[^.=]" (system/qdev-monitor.c:1188): it truncates "ybx.ssd1677" to
    "ybx" and QEMU then only warns "global ybx.ssd1677... has invalid class
    name" on stderr while the property silently stays at its default.  Only
    the structured driver=/property=/value= form reaches a dotted type name;
    verified against the rebuilt binary in 0069.
    """
    return [
        "-global", f"driver=ybx.ssd1677,property=busy-power-ms,value={WINDOW_POWER_MS}",
        "-global", f"driver=ybx.ssd1677,property=busy-waveform-ms,value={WINDOW_WAVEFORM_MS}",
        "-global", f"driver=ybx.ssd1677,property=busy-on-reset-ms,value={WINDOW_RESET_MS}",
    ]


def run_phase(qemu, rom, phase, checks):
    """One QEMU launch; returns the checks dict. Raises on missing 0061 props."""
    globals_ = [] if phase == "default" else global_window_args()
    expected_window = 350 if phase == "default" else WINDOW_POWER_MS
    out = checks

    def note(name, value, expected=None):
        ok = value == expected if expected is not None else bool(value)
        out[name] = {"value": value, "expected": expected, "pass": ok}
        return ok

    with TemporaryDirectory(prefix=f"ybx-epd-0061-{phase}-") as td:
        temp = Path(td)
        bios = temp / "rom"
        bios.mkdir()
        shutil.copyfile(rom, bios / "esp32s3_rev0_rom.bin")
        sock = temp / "qmp.sock"
        cmd = [str(qemu), "-machine",
               "esp32s3,ybx-x4pro-spi2=on,ybx-x4pro-epd=on,ybx-x4pro-gt911=off",
               "-accel", "qtest", "-qtest", "stdio",
               "-qmp", f"unix:{sock},server=on,wait=off",
               "-nographic", "-monitor", "none", "-serial", "null",
               "-L", str(bios)] + globals_
        with (temp / "qemu.stderr").open("w") as err:
            proc = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                                    stdout=subprocess.PIPE, stderr=err,
                                    text=True, bufsize=1)
            qt = None
            try:
                qmp = qmp_connect(sock)
                qt = QTest(proc)
                epd = Epd(qt, qmp)
                qt.wr(GPIO + R["ENABLE_W1TS"], BOARD_OUT)
                qt.wr(GPIO + R["OUT_W1TS"], BOARD_OUT)
                qt.wr(GPIO + R["OUT_W1TC"], 1 << CS)
                for name in ("deep-sleep", "sleep-entries", "sleep-ignored-bytes",
                             "busy-power-high-ms", "busy-waveform-high-ms",
                             "busy-reset-high-ms", "busy-on-reset-ms"):
                    epd.prop(name)  # raises KeyError on a pre-0061 build

                if phase == "windows":
                    # The opt-in windows must be visible on the device itself:
                    # a truncated "-global ybx.ssd1677.prop=" never errors,
                    # it only warns on stderr, so verify through the aliases.
                    note("power_window_applied", epd.prop("busy-power-ms"),
                         WINDOW_POWER_MS)
                    note("waveform_window_applied",
                         epd.prop("busy-waveform-ms"), WINDOW_WAVEFORM_MS)
                    note("reset_window_applied", epd.prop("busy-on-reset-ms"),
                         WINDOW_RESET_MS)
                    # Measured at startup (0069): the GPIO model delivers the
                    # RESET line low on connect, so the board-init OUT write
                    # above is a real release-reassertion edge; with
                    # busy-on-reset-ms>0 the panel answers it with trailing
                    # BUSY before the first activation.  The pending window is
                    # merged into step 1's activation by the last-writer
                    # attribution rule.
                    note("startup_release_trails_busy", epd.busy(), 1)
                    note("startup_sleep_idle", epd.prop("deep-sleep"), 0)
                else:
                    note("idle", (epd.busy(), epd.prop("deep-sleep")), (0, 0))

                # 1) A power-only activation must release after busy-power-ms
                #    (350 in the default phase) and count only into
                #    busy-power-high-ms — the attribution 0037 could not give.
                epd.activation(CTRL2_POWER_ON)
                note("power_asserts_busy", epd.busy(), 1)
                rel = epd.release_ms(expected_window + 100)
                note(f"power_release_ms_{phase}", rel, expected_window)
                note("power_attributed", epd.prop("busy-power-high-ms"),
                     expected_window)
                note("waveform_still_zero", epd.prop("busy-waveform-high-ms"), 0)

                # 2) A real paint lands in the waveform category.
                frames = epd.frames()
                epd.send_seq(paint_sequence())
                note("paint_frames", epd.frames(), frames + 1)
                rel = epd.release_ms(WINDOW_WAVEFORM_MS + 100)
                note(f"waveform_release_ms_{phase}", rel, 350)
                note("waveform_attributed", epd.prop("busy-waveform-high-ms"), 350)

                # 3) Deep sleep (0x10 + mode) parks the controller: the power-off
                #    0x20 completes normally, then every byte is rejected.
                sleep_seq = deep_sleep_sequence()
                pre_ignore = epd.prop("sleep-ignored-bytes")
                epd.send_seq(sleep_seq[:-1])   # up to the 0x10 command byte
                epd.send_seq(sleep_seq[-1:])   # mode byte arrives while asleep
                note("deep_sleep_flag", epd.prop("deep-sleep"), 1)
                note("sleep_entries", epd.prop("sleep-entries"), 1)
                frames = epd.frames()
                rises = epd.prop("busy-rise-edges")
                paint = paint_sequence()
                epd.send_seq(paint)
                note("post_sleep_frames_unchanged", epd.frames(), frames)
                note("post_sleep_no_busy_edge", epd.prop("busy-rise-edges"), rises)
                note("post_sleep_bytes_rejected",
                     epd.prop("sleep-ignored-bytes") >= pre_ignore + byte_count(paint),
                     True)

                # 4) SW_RESET does not wake the model — only a hardware RESET does.
                epd.send([CMD_SW_RESET], dc=False)
                note("sw_reset_keeps_sleep", epd.prop("deep-sleep"), 1)

                # 5) Hardware RESET wakes; in the "windows" phase the panel then
                #    trails BUSY (busy-on-reset-ms), in "default" it stays quiet.
                epd.reset_pulse(low_ms=10)
                note("wake_clears_sleep", epd.prop("deep-sleep"), 0)
                if phase == "windows":
                    note("reset_trails_busy", epd.busy(), 1)
                    rel = epd.release_ms(WINDOW_RESET_MS + 50)
                    note("reset_busy_release_ms", rel, WINDOW_RESET_MS)
                    note("reset_attributed", epd.prop("busy-reset-high-ms"),
                         WINDOW_RESET_MS)
                else:
                    note("default_reset_still_quiet", epd.busy(), 0)
                frames = epd.frames()
                epd.send_seq(paint_sequence())
                note("wake_paint_frames", epd.frames(), frames + 1)
                epd.release_ms(500)

                # 6) Category counters must partition the pooled 0037 counter.
                pooled = epd.prop("busy-high-ms")
                parts = (epd.prop("busy-power-high-ms")
                         + epd.prop("busy-waveform-high-ms")
                         + epd.prop("busy-reset-high-ms"))
                note("categories_partition_pooled", parts <= pooled, True)
                note("edges_balanced", epd.prop("busy-rise-edges"),
                     epd.prop("busy-fall-edges"))
            finally:
                if qt:
                    out["qtest_commands"] = qt.commands
                proc.stdin.close()
                proc.terminate()
                proc.wait(timeout=15)
                out.setdefault("qemu_stderr_tail",
                               (temp / "qemu.stderr").read_text()[-600:])
    return out


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--qemu-bin", "--qemu", dest="qemu", type=Path, required=True)
    p.add_argument("--rom", type=Path, required=True)
    p.add_argument("--report", type=Path,
                   default=Path(__file__).resolve().parent /
                   "experiments/0069-epd-waveform-qtest-report.json")
    a = p.parse_args()
    result = {"qemu_binary": str(a.qemu),
              "machine": "esp32s3,ybx-x4pro-spi2=on,ybx-x4pro-epd=on",
              "phases": {}}
    failed = []
    for phase in ("default", "windows"):
        checks = {}
        try:
            run_phase(a.qemu, a.rom, phase, checks)
        except Exception as exc:
            # KeyError: pre-0061 binary lacks the new aliases.  OSError:
            # QEMU exits at startup when -global names a property the
            # shipped device does not have (busy-on-reset-ms), so the QMP
            # socket resets before the handshake finishes.  The per-phase
            # check dict plus qemu_stderr_tail stay in the report, so a real
            # model bug is still diagnosable even though it lands here.
            checks["0061_aliases"] = {"value": f"absent ({exc})",
                                      "expected": "present", "pass": False}
        result["phases"][phase] = checks
        failed += [f"{phase}/{k}" for k, v in checks.items()
                   if isinstance(v, dict) and not v["pass"]]
    result["failed"] = failed
    a.report.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({"failed": failed,
                      "checks": {ph: {k: v["pass"] for k, v in c.items()
                                      if isinstance(v, dict)}
                                 for ph, c in result["phases"].items()}},
                     indent=2))
    sys.exit(0 if not failed else 1)


if __name__ == "__main__":
    main()
