#!/usr/bin/env python3
"""Host QTest of X4 Pro SSD1677 BUSY semantics and GPIO6 interrupt acknowledgement.

Drives the opt-in panel over the genuine GP-SPI2 MMIO path with no flash
attached, so every assertion is about the device model and the GPIO interrupt
state machine, not about guest firmware. No production firmware, no default
machine change: the machine is only usable with ybx-x4pro-spi2=on,ybx-x4pro-epd=on.

FreeInk contract being verified (fw-092c2be5 symbols checkout):
  EpdBus.cpp:438  attachInterrupt(GPIO6, epdBusyIsr, CHANGE)
  EpdBus.cpp:445  xSemaphoreTake(s_epdRefreshDone, pdMS_TO_TICKS(20))   # assert grace
  EpdBus.cpp:459  while (digitalRead(6) != LOW)  xSemaphoreTake(..., pdMS_TO_TICKS(30000))
So one refresh must produce exactly two GPIO6 CHANGE edges (BUSY active, BUSY
released) and each must be clearable through GPIO_STATUS_W1TC.
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
    "STATUS": 0x44,
    "STATUS_W1TC": 0x4C,
    "PCPU_INT": 0x5C,
    "PIN6": 0x8C,
}
CS, DC, RST, BUSY = 13, 18, 14, 6
BOARD_OUT = (1 << CS) | (1 << DC) | (1 << RST)
INT_TYPE = 7
INT_ENA_CPU0 = 13  # ESP32-S3 pin cfg: int_type[9:7], config[12:11], int_ena[17:13]
EDGE_ANY = 3
LEVEL_HIGH = 5
MS = 1_000_000


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

    def rd(self, addr):
        return self.raw(f"readl 0x{addr:x}")

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
    """Panel + GPIO6 observation helpers."""

    def __init__(self, qt, qmp):
        self.qt = qt
        self.qmp = qmp

    def prop(self, name, epd=True):
        path = "/machine/soc" if epd else "/machine/soc/gpio"
        key = ("ybx-epd-" if epd else "ybx-gpio-") + name
        res = self.qmp("qom-get", {"path": path, "property": key})
        if "return" not in res:
            raise KeyError(f"{key}: {res}")
        return res["return"]

    def busy(self):
        return self.prop("busy")

    def frames(self):
        return self.prop("frames")

    def in_busy(self):
        return (self.qt.rd(GPIO + R["IN"]) >> BUSY) & 1

    def status_busy(self):
        return (self.qt.rd(GPIO + R["STATUS"]) >> BUSY) & 1

    def pcpu_busy(self):
        return (self.qt.rd(GPIO + R["PCPU_INT"]) >> BUSY) & 1

    def irq_level(self):
        return self.prop("irq-level", epd=False)

    def ack(self):
        self.qt.wr(GPIO + R["STATUS_W1TC"], 1 << BUSY)

    def arm(self, pin_cfg):
        self.qt.wr(GPIO + R["PIN6"], pin_cfg)

    def send(self, buf, dc):
        self.qt.wr(GPIO + (R["OUT_W1TS"] if dc else R["OUT_W1TC"]), 1 << DC)
        self.qt.wr(SPI + 0x10, 1 << 27)  # ctrl.update
        self.qt.wr(SPI + 0x1C, len(buf) * 8 - 1)  # mosi_len
        for i in range(0, len(buf), 4):
            word = int.from_bytes(bytes(buf[i:i + 4]).ljust(4, b"\0"), "little")
            self.qt.wr(SPI + 0x98 + i, word)
        self.qt.wr(SPI + 0x00, 1 << 24)  # user -> GP-SPI2 pushes bytes to the panel

    def activation(self, ctrl2):
        self.send([0x22], dc=False)
        self.send([ctrl2], dc=True)
        self.send([0x20], dc=False)


def measure_high_ms(epd, limit_ms=1500):
    """Virtual milliseconds the panel keeps BUSY active, sampled every 5 ms."""
    for step in range(1, limit_ms // 5 + 1):
        epd.qt.tick(5 * MS)
        if epd.busy() == 0:
            return step * 5
    return None


def run(qemu, rom, report, frozen=False):
    out = {"qemu_binary": str(qemu), "machine": "esp32s3,ybx-x4pro-spi2=on,ybx-x4pro-epd=on",
           "vcpu_state": "frozen (-S)" if frozen else
           "running with no flash: only test MMIO reaches SPI2/GPIO"}
    checks = {}

    def note(name, value, expected=None):
        ok = value == expected if expected is not None else bool(value)
        checks[name] = {"value": value, "expected": expected, "pass": ok}
        return ok

    with TemporaryDirectory(prefix="ybx-epd-busy-") as td:
        temp = Path(td)
        bios = temp / "rom"
        bios.mkdir()
        shutil.copyfile(rom, bios / "esp32s3_rev0_rom.bin")
        sock = temp / "qmp.sock"
        cmd = [str(qemu), "-machine",
               "esp32s3,ybx-x4pro-spi2=on,ybx-x4pro-epd=on,ybx-x4pro-gt911=off"]
        if frozen:
            cmd.append("-S")
        cmd += ["-accel", "qtest", "-qtest", "stdio",
               "-qmp", f"unix:{sock},server=on,wait=off",
               "-nographic", "-monitor", "none", "-serial", "null", "-L", str(bios)]
        with (temp / "qemu.stderr").open("w") as err:
            proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=err, text=True, bufsize=1)
            qtest_commands = 0
            qt = None
            try:
                qmp = qmp_connect(sock)
                qt = QTest(proc)
                epd = Epd(qt, qmp)
                # Board lines driven: RESET high, CS low, D/C usable.
                epd.qt.wr(GPIO + R["ENABLE_W1TS"], BOARD_OUT)
                epd.qt.wr(GPIO + R["OUT_W1TS"], BOARD_OUT)
                epd.qt.wr(GPIO + R["OUT_W1TC"], 1 << CS)
                epd.arm(0)
                note("idle_busy_low", epd.busy(), 0)
                note("idle_gpio_in_low", epd.in_busy(), 0)

                # 1) Unarmed BUSY rising edge must leave no stale STATUS bit:
                #    a latched-but-unacked bit is what made 0020/0022 stall CPU1.
                epd.activation(0xF7)
                note("assert_rises_busy", epd.busy(), 1)
                note("assert_rises_guest_visible", epd.in_busy(), 1)
                note("unarmed_edge_no_status_latch", epd.status_busy(), 0)
                note("unarmed_edge_no_machine_irq", epd.irq_level(), 0)
                high = measure_high_ms(epd)
                note("display_refresh_busy_high_ms", high)
                if frozen:
                    # QEMU_CLOCK_VIRTUAL does not advance while the machine is
                    # stopped (-S, or a GDB halt before "continue"), so the panel
                    # release timer cannot fire: BUSY stays asserted and the guest
                    # semaphore wait can only end on its timeout.
                    checks.pop("display_refresh_busy_high_ms", None)
                    checks["frozen_busy_release_deferred"] = {
                        "value": "still high after 1500 ms of clock_step"
                        if high is None else "released",
                        "expected": "still high", "pass": high is None}
                    out["checks"] = checks
                    out["failed"] = sorted(k for k, v in checks.items() if not v["pass"])
                    out["qtest_commands"] = qt.commands
                    report.write_text(json.dumps(out, indent=2) + "\n")
                    print(json.dumps({"frozen_probe": out["failed"] or "OK",
                                      "busy_high_after_1500ms_of_clock_step": high is None}))
                    return 0 if not out["failed"] else 1

                # 2) Armed CHANGE: both edges must be observable and ackable.
                epd.arm((1 << INT_ENA_CPU0) | (EDGE_ANY << INT_TYPE))
                note("armed_idle_no_irq", epd.irq_level(), 0)
                epd.activation(0xF7)
                note("rising_edge_latches_status", epd.status_busy(), 1)
                note("rising_edge_visible_in_pcpu_int", epd.pcpu_busy(), 1)
                note("rising_edge_raises_machine_irq", epd.irq_level(), 1)
                epd.ack()
                note("w1tc_clears_pcpu_int", epd.pcpu_busy(), 0)
                note("w1tc_lower_machine_irq", epd.irq_level(), 0)
                epd.qt.tick(351 * MS)
                note("falling_edge_busy_low", epd.busy(), 0)
                note("falling_edge_latches_status", epd.status_busy(), 1)
                note("falling_edge_raises_machine_irq", epd.irq_level(), 1)
                epd.ack()
                note("completion_ack_clears_irq", epd.irq_level(), 0)

                # 3) Power-only activation (CTRL2 0xC0, no DISPLAY_START bit2).
                #    Must not paint a frame, but the model still holds BUSY.
                frames = epd.frames()
                epd.arm(0)
                epd.activation(0xC0)
                note("poweron_does_not_paint", epd.frames(), frames)
                note("poweron_busy_high", epd.busy(), 1)
                note("poweron_busy_high_ms", measure_high_ms(epd))

                # 4) Level-type (POSLEVEL) is self-reasserting while BUSY is high:
                #    documents why a level-driven guest ISR would storm, and why the
                #    FreeInk CHANGE/edge path is the one that must be emulated.
                epd.arm((1 << INT_ENA_CPU0) | (LEVEL_HIGH << INT_TYPE))
                note("level_idle_no_irq", epd.irq_level(), 0)
                epd.activation(0xC0)
                epd.ack()
                note("level_reasserts_while_busy_high", epd.irq_level(), 1)
                epd.qt.tick(351 * MS)
                epd.ack()
                note("level_clears_after_release", epd.irq_level(), 0)
                epd.arm(0)
                # Optional 0037 telemetry: absent on the pre-patch build.
                try:
                    rise = epd.prop("busy-rise-edges")
                    fall = epd.prop("busy-fall-edges")
                    high_ms = epd.prop("busy-high-ms")
                except KeyError:
                    checks["edge_telemetry"] = {"value": "absent (pre-0037 build)",
                                                 "expected": None, "pass": True}
                else:
                    # Four activations ran above: two DISPLAY_START, two power-only.
                    note("rise_edges_equal_fall_edges", rise == fall, True)
                    note("edge_count_matches_activations", rise, 4)
                    note("busy_high_ms_recorded", high_ms > 0, True)
                    out["busy_high_ms_total"] = high_ms
            finally:
                out["qtest_commands"] = qt.commands if qt else 0
                proc.stdin.close()
                proc.terminate()
                proc.wait(timeout=15)
                out["qemu_stderr_tail"] = (temp / "qemu.stderr").read_text()[-600:]

    out["checks"] = checks
    out["failed"] = sorted(k for k, v in checks.items() if not v["pass"])
    out["bus_semantic_summary"] = {
        "edges_per_refresh": 2,
        "display_start_busy_ms": checks.get("display_refresh_busy_high_ms", {}).get("value"),
        "power_only_busy_ms": checks.get("poweron_busy_high_ms", {}).get("value"),
        "ack_register": "GPIO_STATUS_W1TC 0x4C; guest reads GPIO_PCPU_INT 0x5C",
    }
    report.write_text(json.dumps(out, indent=2) + "\n")
    print(json.dumps({"checks": {k: v["pass"] for k, v in checks.items()},
                      "failed": out["failed"],
                      "busy_ms": out["bus_semantic_summary"]}, indent=2))
    return 0 if not out["failed"] else 1


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--qemu", type=Path, required=True)
    p.add_argument("--rom", type=Path, required=True)
    p.add_argument("--report", type=Path,
                   default=Path(__file__).resolve().parent /
                   "experiments/0037-epd-busy-qtest-report.json")
    p.add_argument("--frozen-probe", action="store_true",
                   help="extra pass with -S: prove the BUSY release timer is virtual-clock bound")
    a = p.parse_args()
    code = run(a.qemu, a.rom, a.report)
    if a.frozen_probe:
        frozen_report = a.report.with_name(a.report.stem + "-frozen.json")
        code = run(a.qemu, a.rom, frozen_report, frozen=True) or code
    sys.exit(code)


if __name__ == "__main__":
    main()
