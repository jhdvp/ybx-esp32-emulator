#!/usr/bin/env python3
"""Validate opt-in X4 Pro QEMU EPD BUSY and cross-core IPI counters via QTest.

QTest injects MMIO, NOT firmware touches. This is a device-model diagnostic
test. A separate genuine factory guest test must still prove GUI navigation.
"""
import argparse
import json
import shutil
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory

from probe_x4pro_touch_injection_qtest import QTest, qmp_connect
from probe_x4pro_epd_qtest import GPIO, PINS, send

SYSTEM_CPU_INTR_FROM_CPU_0 = 0x600C0030
MATRIX_CPU0_SOURCE79 = 0x600C2000 + 79 * 4


def read_property(qmp, path, prop):
    result = qmp("qom-get", {"path": path, "property": prop})
    if "return" not in result:
        raise AssertionError(f"Missing diagnostic QOM property {path}/{prop}: {result}")
    return result["return"]


def assert_non_decreasing(before, after, key):
    if after[key] < before[key]:
        raise AssertionError(f"Counter {key} went backwards: {before[key]} -> {after[key]}")


def run(qemu, rom):
    with TemporaryDirectory(prefix="ybx-telemetry-qtest-") as work:
        path = Path(work)
        (path / "rom").mkdir()
        shutil.copyfile(rom, path / "rom" / "esp32s3_rev0_rom.bin")
        socket = path / "qmp.sock"
        command = [
            str(qemu), "-machine", "esp32s3,ybx-x4pro-spi2=on,ybx-x4pro-epd=on",
            "-accel", "qtest", "-qtest", "stdio",
            "-qmp", f"unix:{socket},server=on,wait=off",
            "-nographic", "-monitor", "none", "-serial", "null",
            "-L", str(path / "rom"),
        ]
        with (path / "qemu.stderr").open("w") as err:
            process = subprocess.Popen(
                command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=err, text=True, bufsize=1,
            )
            client = None
            try:
                client, _reader, qmp = qmp_connect(socket)
                qt = QTest(process)
                def read():
                    return {
                        "ipi_set": read_property(qmp, "/machine/soc/clock", "ybx-cpu-intr-0-sets"),
                        "ipi_clear": read_property(qmp, "/machine/soc/clock", "ybx-cpu-intr-0-clears"),
                        "map_writes": read_property(qmp, "/machine/soc/intmatrix", "ybx-map-writes"),
                        "cpu0_from_cpu_asserted": read_property(qmp, "/machine/soc/intmatrix",
                                                                 "ybx-cpu0-from-cpu-asserted"),
                        "cpu0_unrouted": read_property(qmp, "/machine/soc/intmatrix",
                                                       "ybx-cpu0-unrouted"),
                        "busy_rise": read_property(qmp, "/machine/soc",
                                                   "ybx-epd-busy-rise-edges"),
                        "busy_fall": read_property(qmp, "/machine/soc",
                                                   "ybx-epd-busy-fall-edges"),
                        "busy_high_ms": read_property(qmp, "/machine/soc",
                                                      "ybx-epd-busy-high-ms"),
                    }
                before = read()
                # CPU interrupt 5 is a valid ESP32-S3 external interrupt.
                qt.wr(MATRIX_CPU0_SOURCE79, 5)
                assert qt.rd(MATRIX_CPU0_SOURCE79) == 5
                qt.wr(SYSTEM_CPU_INTR_FROM_CPU_0, 1)
                qt.wr(SYSTEM_CPU_INTR_FROM_CPU_0, 0)
                routed = read()
                for key in ("ipi_set", "ipi_clear", "map_writes",
                            "cpu0_from_cpu_asserted"):
                    assert routed[key] > before[key], (key, before, routed)
                assert routed["cpu0_unrouted"] == before["cpu0_unrouted"], routed

                # Route to interrupt 16, NOT an external Xtensa S3 CPU interrupt.
                # Unlike a real guest path, this is intentionally invalid.
                qt.wr(MATRIX_CPU0_SOURCE79, 16)
                qt.wr(SYSTEM_CPU_INTR_FROM_CPU_0, 1)
                qt.wr(SYSTEM_CPU_INTR_FROM_CPU_0, 0)
                unrouted = read()
                assert unrouted["cpu0_unrouted"] > routed["cpu0_unrouted"], (routed, unrouted)

                # A power-on activation changes BUSY, but does NOT paint a frame.
                qt.wr(GPIO + 0x08, PINS)
                qt.wr(GPIO + 0x24, PINS)
                qt.wr(GPIO + 0x0c, 1 << 13)
                send(qt, [0x22], dc=False)
                send(qt, [0xc0], dc=True)
                send(qt, [0x20], dc=False)
                active = read()
                assert active["busy_rise"] > unrouted["busy_rise"], active
                assert active["busy_fall"] == unrouted["busy_fall"], active
                assert read_property(qmp, "/machine/soc", "ybx-epd-frames") == 0
                qt.tick(351_000_000)
                completed = read()
                assert completed["busy_fall"] > active["busy_fall"], completed
                assert completed["busy_high_ms"] >= 350, completed
                for key in before:
                    assert_non_decreasing(before, completed, key)
                return {
                    "qtest_ipi_delivered": True,
                    "qtest_ipi_unrouted_detected": True,
                    "qtest_epd_busy_rise_and_fall": True,
                    "qtest_busy_high_ms": completed["busy_high_ms"],
                    "native_guest_navigation_verified": False,
                    "before": before, "after_routed": routed,
                    "after_unrouted": unrouted, "after_busy": completed,
                    "qtest_mmio_commands": qt.commands,
                }
            finally:
                if client:
                    client.close()
                process.terminate()
                try:
                    process.communicate(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qemu-bin", type=Path, required=True)
    parser.add_argument("--rom", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    result = run(args.qemu_bin.resolve(strict=True), args.rom.resolve(strict=True))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
