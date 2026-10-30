#!/usr/bin/env python3
"""QEMU ESP32-S3 Wi-Fi radio coverage probe (qtest + mtree, no guest boot).

Measures what the prebuilt patched QEMU answers for the modem register windows
that the factory image drives. Runs the CPU-frozen machine only, inside a
disposable container with no network: this proves a gap, not connectivity.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys

from wifi_radio_inventory import (classify_window, parse_device_help,
                                  parse_mtree, radio_device_names, windows)

# Registers the factory image really accesses (see experiments/0048) plus the
# controls that show a modelled register does answer non-zero.
READS = (
    ("wlan_mac_ctrl_0x60033000", 0x60033000, "zero"),
    ("wlan_mac_rxbuf_ctrl", 0x60033080, "zero"),
    ("wlan_mac_intr_status", 0x60033c34, "zero"),
    ("coex_pti", 0x60031050, "zero"),
    ("crypto_key_entry", 0x60034400, "zero"),
    ("tsf_timer_ctrl", 0x6003505c, "zero"),
    ("wdev_rng", 0x6003507c, "modelled"),
    ("bb_ctrl", 0x6001d054, "zero"),
    ("nrx_power_ctrl", 0x6001ccd4, "zero"),
    ("fe_pa_switch", 0x6000607c, "zero"),
    ("bt_bb_ctrl", 0x60011000, "zero"),
    ("syscon_wifi_bb_cfg", 0x6002600c, "zero"),
    ("apbctrl_date_control", 0x6002607c, "modelled"),
    ("emac_control", 0x600cd000, "modelled"),
    ("intmatrix_ctrl", 0x600c2000, "modelled"),
)

# Windows that must show no device model at all for this report to hold.
RADIO_WINDOWS = ("FE2", "FE", "BT_BB", "NRX", "BB", "MODEM_COEX", "WIFI_MAC",
                 "WIFI_TX_CRYPTO")

QEMU = "/qemu/build-ybx/qemu-system-xtensa"


def qtest_protocol() -> str:
    return "".join(f"readl 0x{address:x}\n" for _, address, _ in READS)


READ_OK = re.compile(r"OK\s+0x([0-9a-f]{16})")


def parse_reads(stdout: str) -> list[int]:
    return [int(match.group(1), 16)
            for match in (READ_OK.search(line) for line in stdout.splitlines())
            if match]


def classify_reads(values: list[int]) -> list[dict]:
    if len(values) != len(READS):
        raise RuntimeError(
            f"expected {len(READS)} reads, got {len(values)}: {values}")
    rows = []
    for (label, address, expectation), value in zip(READS, values):
        modelled = value != 0
        rows.append({"label": label, "address": f"0x{address:08x}",
                     "value": f"0x{value:08x}",
                     "expectation": expectation,
                     "ok": (modelled if expectation == "modelled"
                            else not modelled)})
    return rows


def verdict(read_rows: list[dict], regions: list, device_names: list) -> dict:
    coverage = [classify_window(window, regions) for window in windows()]
    by_name = {row["window"]: row["status"] for row in coverage}
    wdev = next(row for row in coverage if row["window"] == "WDEV")
    devices = radio_device_names(device_names)
    return {
        "radio_register_windows_unmodelled": all(
            by_name[name] == "zero_fill_catchall" for name in RADIO_WINDOWS),
        "radio_window_statuses": by_name,
        "wdev_status": wdev["status"],
        "wdev_only_decoded_regions": wdev["owners"],
        "wifi_device_types_in_qemu": devices["radio_devices"],
        "ethernet_device_types_in_qemu": devices["ethernet_devices"],
        "reads_all_as_expected": all(row["ok"] for row in read_rows),
        "wifi_radio_emulated": False,
        "network_connectivity_verified": False,
    }


def docker(image: str, qemu_root: Path, script: list[str], stdin: str = "",
           timeout: int = 25) -> subprocess.CompletedProcess:
    command = ["docker", "run", "--rm", "-i", "--network", "none",
               "--platform", "linux/arm64",
               "-v", f"{qemu_root}:/qemu:ro", image] + script
    return subprocess.run(command, input=stdin, capture_output=True,
                          text=True, timeout=timeout)


def device_help(image: str, qemu_root: Path) -> str:
    result = docker(image, qemu_root,
                    ["timeout", "-k", "2s", "8s", QEMU, "-device", "help"])
    return result.stdout + result.stderr


def mtree_capture(image: str, qemu_root: Path, rom_dir: str, nic: bool) -> str:
    script = ["timeout", "-k", "2s", "8s", QEMU, "-M", "esp32s3",
              "-L", rom_dir, "-S", "-nographic", "-serial", "null"]
    if nic:
        script += ["-nic", "user,model=open_eth,id=ybxeth0"]
    result = docker(image, qemu_root, script, stdin="info mtree\nquit\n")
    return result.stdout + result.stderr


def qtest_capture(image: str, qemu_root: Path, rom_dir: str) -> str:
    result = docker(
        image, qemu_root,
        ["timeout", "-k", "2s", "5s", QEMU, "-machine", "esp32s3",
         "-L", rom_dir, "-S", "-nographic", "-monitor", "none",
         "-serial", "null", "-qtest", "stdio"], stdin=qtest_protocol())
    return result.stdout


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qemu-root", type=Path, required=True,
                        help="patched QEMU checkout (read-only)")
    parser.add_argument("--docker-image", default="ybx-qemu-builder:20261029")
    parser.add_argument("--rom-dir", default="/qemu/build-ybx",
                        help="container path holding esp32s3_rev0_rom.bin")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    root = args.qemu_root.resolve(strict=True)
    binary = root / "build-ybx" / "qemu-system-xtensa"
    if not binary.is_file():
        raise SystemExit(f"missing prebuilt QEMU: {binary}")
    if not (root / "build-ybx" / "esp32s3_rev0_rom.bin").is_file():
        raise SystemExit("missing esp32s3_rev0_rom.bin next to the QEMU binary")
    rows = classify_reads(parse_reads(
        qtest_capture(args.docker_image, root, args.rom_dir)))
    regions = parse_mtree(mtree_capture(args.docker_image, root,
                                        args.rom_dir, nic=False))
    bridged = mtree_capture(args.docker_image, root, args.rom_dir, nic=True)
    devices = parse_device_help(device_help(args.docker_image, root))
    report = {
        "reads": rows,
        "regions_default": [asdict(region) for region in regions],
        "slirp_bridge_regions": sorted({region.name for region
                                        in parse_mtree(bridged)
                                        if "open_eth" in region.name}),
        "verdict": verdict(rows, regions, devices),
        "qemu_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
        "guest_boot": False,
        "container_network": "none",
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report["verdict"], indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
