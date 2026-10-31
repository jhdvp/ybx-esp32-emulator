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

# ---------------------------------------------------------------------------
# 0060: semantics check for the opt-in register stub (patch
# experiments/0060-s3-wifi-radio-model.patch, machine property
# ybx-x4pro-wifi-stub=on). Pure data + parsers below; the QEMU run itself
# needs the rebuilt binary and is left to the orchestrator.
# (kind, address, data-or-None, label, expected)
STUB_OPS = (
    ("read",  0x60035f00, None,        "stats.magic",          0x59425230),
    ("read",  0x60035f04, None,        "stats.window_count",   6),
    ("write", 0x60034400, 0x5a5a5a5a,  "crypto.key_write",     None),
    ("read",  0x60034400, None,        "crypto.key_readback",  0x5a5a5a5a),
    ("read",  0x60033c3c, None,        "mac.intr_get_empty",   0),
    ("write", 0x60033c40, 0xffffffff,  "mac.intr_clr",         None),
    ("read",  0x60033c3c, None,        "mac.intr_still_empty", 0),
    ("read",  0x6003500c, None,        "tsf.frozen_low",       0),
    ("write", 0x60035010, 0xdeadbeef,  "tsf.set_ignored",      None),
    ("read",  0x6003500c, None,        "tsf.still_frozen",     0),
    ("read",  0x60035f08, None,        "stats.read_count",     5),
    ("read",  0x60035f10, None,        "stats.write_count",    3),
    ("read",  0x60035f18, None,        "stats.last_read",      0x6003500c),
    ("read",  0x60035f1c, None,        "stats.last_write",     0x60035010),
)

QEMU = "/qemu/build-ybx/qemu-system-xtensa"


def qtest_protocol() -> str:
    return "".join(f"readl 0x{address:x}\n" for _, address, _ in READS)


def stub_protocol() -> str:
    lines = []
    for kind, address, data, _, _ in STUB_OPS:
        if kind == "write":
            lines.append(f"writel 0x{address:x} 0x{data:x}")
        else:
            lines.append(f"readl 0x{address:x}")
    return "".join(line + "\n" for line in lines)


READ_OK = re.compile(r"OK\s+0x([0-9a-f]{16})")


def parse_reads(stdout: str) -> list[int]:
    return [int(match.group(1), 16)
            for match in (READ_OK.search(line) for line in stdout.splitlines())
            if match]


def classify_stub(values: list[int]) -> list[dict]:
    """Match STUB_OPS reads against expected register-stub semantics."""
    read_ops = [op for op in STUB_OPS if op[0] == "read"]
    if len(values) != len(read_ops):
        raise RuntimeError(f"expected {len(read_ops)} reads, got "
                           f"{len(values)}: {values}")
    rows = []
    for (_, address, _, label, expected), value in zip(read_ops, values):
        rows.append({"label": label, "address": f"0x{address:08x}",
                     "value": f"0x{value:08x}",
                     "expected": f"0x{expected:08x}", "ok": value == expected})
    return rows


def verdict_stub(stub_rows: list[dict], regions: list) -> dict:
    """What the stub changed in the mtree, and whether its semantics held."""
    coverage = {row["window"]: row
                for row in (classify_window(w, regions) for w in windows())}
    owners = {name: coverage[name]["owners"] for name in
              ("WIFI_MAC", "MODEM_COEX", "WIFI_TX_CRYPTO", "FE", "WDEV")}
    stub_regions = sorted({o for names in owners.values() for o in names
                           if "wifi_stub" in o})
    return {
        "stub_semantics_all_ok": all(row["ok"] for row in stub_rows),
        "stub_mapped_regions": stub_regions,
        "radio_window_owners_with_stub": owners,
        "association_claimed": False,
        "frames_delivered": 0,
        "interrupt_wired": False,
    }


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


def mtree_capture(image: str, qemu_root: Path, rom_dir: str, nic: bool,
                  machine: str = "esp32s3") -> str:
    script = ["timeout", "-k", "2s", "8s", QEMU, "-M", machine,
              "-L", rom_dir, "-S", "-nographic", "-serial", "null"]
    if nic:
        script += ["-nic", "user,model=open_eth,id=ybxeth0"]
    result = docker(image, qemu_root, script, stdin="info mtree\nquit\n")
    return result.stdout + result.stderr


def qtest_capture(image: str, qemu_root: Path, rom_dir: str,
                  machine: str = "esp32s3", stdin: str | None = None) -> str:
    result = docker(
        image, qemu_root,
        ["timeout", "-k", "2s", "5s", QEMU, "-machine", machine,
         "-L", rom_dir, "-S", "-nographic", "-monitor", "none",
         "-serial", "null", "-qtest", "stdio"],
        stdin=qtest_protocol() if stdin is None else stdin)
    return result.stdout


def machine_supports_property(image: str, qemu_root: Path, prop: str) -> bool:
    """True when the built QEMU knows `-M esp32s3,<prop>=on`."""
    result = docker(image, qemu_root,
                    ["timeout", "-k", "2s", "5s", QEMU, "-M",
                     f"esp32s3,{prop}=on", "-L", "/qemu/build-ybx", "-S",
                     "-nographic", "-serial", "null"], stdin="quit\n")
    return result.returncode == 0 and prop not in result.stderr


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qemu-root", type=Path, required=True,
                        help="patched QEMU checkout (read-only)")
    parser.add_argument("--docker-image", default="ybx-qemu-builder:20261029")
    parser.add_argument("--rom-dir", default="/qemu/build-ybx",
                        help="container path holding esp32s3_rev0_rom.bin")
    parser.add_argument("--with-stub", action="store_true",
                        help="also exercise the 0060 register stub "
                             "(requires a QEMU built with "
                             "experiments/0060-s3-wifi-radio-model.patch)")
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
        "stub": None,
        "qemu_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
        "guest_boot": False,
        "container_network": "none",
    }
    if args.with_stub:
        machine = "esp32s3,ybx-x4pro-wifi-stub=on"
        if machine_supports_property(args.docker_image, root,
                                    "ybx-x4pro-wifi-stub"):
            stub_rows = classify_stub(parse_reads(
                qtest_capture(args.docker_image, root, args.rom_dir,
                              machine=machine, stdin=stub_protocol())))
            stub_regions = parse_mtree(mtree_capture(
                args.docker_image, root, args.rom_dir, nic=False,
                machine=machine))
            report["stub"] = {
                "machine_property": machine,
                "rows": stub_rows,
                "verdict": verdict_stub(stub_rows, stub_regions),
            }
        else:
            report["stub"] = {"machine_property":
                              "ybx-x4pro-wifi-stub=on",
                              "available": False,
                              "reason": "prebuilt QEMU predates "
                                        "0060-s3-wifi-radio-model.patch"}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report["verdict"], indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
