#!/usr/bin/env python3
"""ESP32-S3 Wi-Fi radio emulation capability inventory (host-only).

Answers two questions with repository evidence and nothing else:
  1. which modem MMIO windows the factory image actually drives, and
  2. whether an Espressif QEMU machine models those windows at all.

It never reports connectivity. A green result here means "the gap is measured",
not "Wi-Fi works".
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import re
import shlex
import subprocess
import sys
from pathlib import Path

# QEMU's catch-all for every unmodelled S3 peripheral: reads return 0 and the
# warning that would say so is compiled out (ESP32S3_IO_WARNING 0).
# hw/xtensa/esp32s3.c:118, 532-549, 750-753 (0x60000000 + 0xd1000).
CATCHALL = "esp32s3.iomem"

# IDF public bases: soc/esp32s3/register/soc/reg_base.h lines in the comment.
# The 0x60030000-0x60036000 modem cluster has no public base define; those
# ranges are recovered from the factory image's own l32r literal pools and from
# include/hw/misc/esp32s3_rng.h:26 (ESP32S3_RNG_BASE 0x6003507C /* DR_REG_WDEV_BASE + 0x07c */).
MODEM_WINDOWS = (
    ("FE2", 0x60005000, 0x60006000, "reg_base.h:11 DR_REG_FE2_BASE"),
    ("FE", 0x60006000, 0x60007000, "reg_base.h:12 DR_REG_FE_BASE"),
    ("RTCCNTL", 0x60008000, 0x60008400, "reg_base.h:14 DR_REG_RTCCNTL_BASE"),
    ("RTC_I2C", 0x60008C00, 0x60009000, "reg_base.h:17 DR_REG_RTC_I2C_BASE"),
    ("BT_BB", 0x60011000, 0x60012000, "reg_base.h:23 DR_REG_BT_BASE"),
    ("NRX", 0x6001CC00, 0x6001D000, "reg_base.h:31 DR_REG_NRX_BASE"),
    ("BB", 0x6001D000, 0x6001E000, "reg_base.h:32 DR_REG_BB_BASE"),
    ("SYSCON", 0x60026000, 0x60026400, "reg_base.h:40; syscon_reg.h:129 WIFI_BB_CFG"),
    ("MODEM_COEX", 0x60031000, 0x60032000, "factory coex_pti_v2, btdm_sleep_clock_sync"),
    ("WIFI_MAC", 0x60033000, 0x60034000, "factory hal_mac_set_bssid, mac_txrx_init"),
    ("WIFI_TX_CRYPTO", 0x60034000, 0x60035000, "factory hal_crypto_set_key_entry, mac_tx_set_htsig"),
    ("WDEV", 0x60035000, 0x60036000, "factory tsf_hal_*, esp32s3_rng.h:26 RNG in WDEV"),
    ("PMS", 0x600C1000, 0x600C2000, "reg_base.h:60 DR_REG_SENSITIVE_BASE"),
    ("INTMATRIX", 0x600C2000, 0x600C3000, "reg_base.h:61 DR_REG_INTERRUPT_BASE"),
    ("EMAC", 0x600CD000, 0x600CE000, "esp32s3_reg.h:77; no MAC driver in the image"),
)

# Symbol-class counts, used to prove which radio stack layers are linked into
# the shipped image (and therefore which ones the guest will exercise).
SUBSYSTEMS = {
    "esp_wifi_api": r"esp_wifi_",
    "wifi_core_hal": r"(hal_mac_|hal_crypto_|mac_tx|mac_rx|mac_bb|tsf_hal_|wDev|wdev_)",
    "phy": r"(esp_phy_|register_chipv7_phy|phy_|ant_|spur_coef|rf_set_)",
    "coexistence": r"coex",
    "network_stack": r"(lwip_|esp_netif|tcpip_|dns_)",
    "tls": r"mbedtls_",
    "wired_eth": r"(esp_eth_|eth_mac|emac_|_emac)",
}

@dataclass(frozen=True)
class Window:
    name: str
    start: int
    end: int
    evidence: str


@dataclass(frozen=True)
class Region:
    start: int
    end: int
    kind: str
    prio: int
    name: str


def windows() -> tuple:
    return tuple(Window(name, start, end, evidence)
                 for name, start, end, evidence in MODEM_WINDOWS)


def window_for(address: int) -> str | None:
    for window in windows():
        if window.start <= address < window.end:
            return window.name
    return None


MTREE_LINE = re.compile(
    r"^\s+(?P<start>[0-9a-f]{16})-(?P<end>[0-9a-f]{16})"
    r" \(prio (?P<prio>-?\d+), (?P<kind>[a-z/]+)\): (?P<name>.*?)\s*$")


def parse_mtree(text: str) -> list:
    """Return the flat region list of an `info mtree` dump.

    Aliases are skipped: `info mtree` marks them with an "alias" name prefix and
    they relocate a region to another base, which would be reported as false
    coverage of a peripheral window.
    """
    regions = []
    for line in text.splitlines():
        match = MTREE_LINE.match(line)
        if not match:
            continue
        name = match.group("name")
        if name.startswith(("alias ", "container ")):
            continue
        regions.append(Region(int(match.group("start"), 16),
                              int(match.group("end"), 16),
                              match.group("kind"),
                              int(match.group("prio")),
                              name))
    return regions


def regions_covering(regions: list, start: int, end: int) -> list:
    """Regions overlapping [start, end), ignoring whole-address-space maps.

    QEMU prints the `system` root plus one `<cpu>-mem` root per core; they span
    the whole 32-bit space and would otherwise look like coverage everywhere.
    """
    return [region for region in regions
            if region.start < end and start < region.end
            and region.end - region.start < (1 << 24)]


def covered_bytes(regions: list, start: int, end: int) -> int:
    total = 0
    for region in regions:
        low, high = max(region.start, start), min(region.end + 1, end)
        if low < high:
            total += high - low
    return total


def classify_window(window, regions: list) -> dict:
    """modelled / partial / ram_blob / zero_fill_catchall / unassigned."""
    hits = regions_covering(regions, window.start, window.end)
    owners = [r for r in hits if r.name != CATCHALL]
    span = window.end - window.start
    if not owners:
        status = "zero_fill_catchall" if hits else "unassigned"
    elif covered_bytes(owners, window.start, window.end) < span:
        status = "partially_modelled"
    elif all(r.kind == "ram" for r in owners):
        # Plain RAM where the silicon has a register file: every read returns
        # whatever a previous write left, so reset values are all zero.
        status = "ram_blob"
    else:
        status = "modelled"
    return {
        "window": window.name,
        "range": f"0x{window.start:08x}-0x{window.end - 1:08x}",
        "status": status,
        "owners": sorted({r.name for r in owners}),
        "owner_ranges": [f"0x{r.start:08x}-0x{r.end:08x} {r.kind} {r.name}"
                         for r in owners],
        "evidence": window.evidence,
    }


NM_LINE = re.compile(r"^[0-9a-f]{8} ([A-Za-z]) (\S+)")


def parse_nm(text: str) -> list:
    """Return (type, symbol) pairs; skips undefined (U) entries."""
    symbols = []
    for line in text.splitlines():
        match = NM_LINE.match(line)
        if match and match.group(1) != "U":
            symbols.append((match.group(1), match.group(2)))
    return symbols


def subsystem_counts(symbols: list) -> dict:
    counts = {}
    for label, pattern in SUBSYSTEMS.items():
        needle = re.compile(pattern, re.IGNORECASE)
        counts[label] = sum(1 for _, name in symbols if needle.search(name))
    counts["radio_stack_total"] = sum(counts[key] for key in
                                      ("esp_wifi_api", "wifi_core_hal", "phy",
                                       "coexistence"))
    return counts


L32R_LINE = re.compile(
    r"^\s*[0-9a-f]+:\s+[0-9a-f]+\s+l32r\s+\w+,\s*[0-9a-f]+(?: <[^>]*>)?"
    r"\s+\((?P<target>[0-9a-f]{8})(?: <[^>]*>)?\)")
FUNCTION_LINE = re.compile(r"^[0-9a-f]+ <(?P<name>[^>]+)>:")

# ---------------------------------------------------------------------------
# Pinned factory baseline (0060). Every number below was produced by running
# this module against the factory X4 Pro ELF
# (sha256 7e040053768db2d6559305dd26433b0ee8264de143202d3c72ace582e7ba98e5,
# /Users/yaraslauhancharou/.local/state/xteink-agent-workspaces/
# fw-092c2be5-symbols-20261029/.pio/build/x4pro/firmware.elf) with
# xtensa-esp32s3-elf-nm/-objdump from PlatformIO
# toolchain-xtensa-esp-elf on 2026-10-31. "accesses" counts literal-pool
# l32r operands whose target falls in the window (compile-time evidence that
# the function dereferences that MMIO); "reason" names why the driver touches
# the window, taken from the disassembling function names, not from guessing.
FACTORY_BASELINE = {
    "elf_sha256": ("7e040053768db2d6559305dd26433b0ee8264de143202d3c72ace5"
                   "82e7ba98e5"),
    "measured": "2026-10-31",
    "symbols": {"total_defined": 25846, "esp_wifi_api": 123,
                "wifi_core_hal": 132, "phy": 269, "coexistence": 292,
                "network_stack": 367, "tls": 312, "wired_eth": 1},
    "total_l32r_modem_accesses": 692,
    "windows": {
        "WIFI_MAC": {
            "accesses": 154, "distinct_addresses": 90,
            "observed_range": "0x60033000-0x60033d14",
            "reason": "MAC init/deinit, BSSID, RX ring setup, TX-queue state, "
                      "interrupt get/clear, EDCA/PTI — every esp_wifi_start/"
                      "stop and queue operation programs these registers",
            "examples": ["hal_mac_init", "hal_mac_set_bssid", "mac_txrx_init",
                         "hal_mac_interrupt_clr_event"],
        },
        "WDEV": {
            "accesses": 125, "distinct_addresses": 32,
            "observed_range": "0x60035000-0x6003511c",
            "reason": "TSF get/set, four hardware timers, TBTT and light "
                      "sleep, RX-beacon PTI, power-interrupt get/clear and "
                      "wdev status word 0x60035000 (hottest single register, "
                      "43 literal sites from cnx_bss_alloc/clear_bss_queue)",
            "examples": ["hal_get_tsf_time", "tsf_hal_set_timer_enable",
                         "hal_mac_tsf_reset", "hal_pwr_interrupt_clr_event"],
        },
        "FE": {
            "accesses": 125, "distinct_addresses": 62,
            "observed_range": "0x60006000-0x600061e4",
            "reason": "RF front-end: PA/antenna switching per TX/RX turn and "
                      "TX digital gains programmed by the PHY init blob",
            "examples": ["ant_wifitx_cfg", "ant_wifirx_cfg",
                         "ram_set_pbus_reg", "force_txrx_off"],
        },
        "RTCCNTL": {
            "accesses": 118, "distinct_addresses": 43,
            "observed_range": "0x60008000-0x600081fc",
            "reason": "SoC clock tree and slow-clock calibration the modem "
                      "sleep timing is derived from",
            "examples": ["esp_clk_init", "esp_clk_slowclk_cal_get"],
        },
        "BT_BB": {
            "accesses": 62, "distinct_addresses": 41,
            "observed_range": "0x60011008-0x600118b0",
            "reason": "coexistence peer baseband parameters (detect, gain, "
                      "filter) programmed during bt/coex init",
            "examples": ["bt_bb_corr_set", "bt_bb_detect_set"],
        },
        "MODEM_COEX": {
            "accesses": 35, "distinct_addresses": 16,
            "observed_range": "0x60031000-0x60031530",
            "reason": "BT/Wi-Fi arbitration: PTI priority table, scheduler "
                      "reset and sleep-clock sync touched per coex slot",
            "examples": ["coex_pti_v2", "btdm_sleep_clock_sync"],
        },
        "WIFI_TX_CRYPTO": {
            "accesses": 34, "distinct_addresses": 20,
            "observed_range": "0x600342f8-0x60034424",
            "reason": "hardware crypto key table 0x60034400-0x60034424 "
                      "(set/get/clear key entry) plus TX PLCP/HT-SIG vectors",
            "examples": ["hal_crypto_set_key_entry",
                         "hal_crypto_get_key_entry",
                         "hal_crypto_clr_key_entry", "mac_tx_set_htsig"],
        },
        "SYSCON": {
            "accesses": 25, "distinct_addresses": 11,
            "observed_range": "0x60026000-0x600260c0",
            "reason": "wifi module clock-enable/reset gating around PHY use",
            "examples": ["phy_module_enable", "btbb_wifi_bb_cfg2"],
        },
        "NRX": {
            "accesses": 10, "distinct_addresses": 7,
            "observed_range": "0x6001cc0c-0x6001cd0c",
            "reason": "receiver calibration in the PHY bring-up blob",
            "examples": ["register_chipv7_phy", "phy_freq_correct"],
        },
        "BB": {
            "accesses": 3, "distinct_addresses": 3,
            "observed_range": "0x6001d018-0x6001d054",
            "reason": "baseband spur coefficients and frequency correction",
            "examples": ["spur_coef_cfg_new", "phy_freq_correct"],
        },
        "FE2": {
            "accesses": 1, "distinct_addresses": 1,
            "observed_range": "0x600050f0-0x600050f0",
            "reason": "single FE2 power-up touch from rtc_sleep_pu",
            "examples": ["rtc_sleep_pu"],
        },
    },
    # Register-level pins that the 0060 stub design is justified by.
    "key_registers": {
        "0x60034400": {"accesses": 3,
                       "functions": ["hal_crypto_clr_key_entry",
                                     "hal_crypto_get_key_entry",
                                     "hal_crypto_set_key_entry"],
                       "meaning": "crypto key-entry CSR: write key, read "
                                  "back, clear"},
        "0x60033c3c": {"accesses": 1,
                       "functions": ["hal_mac_interrupt_get_event"],
                       "meaning": "MAC interrupt event get (hardware-set)"},
        "0x60033c40": {"accesses": 3,
                       "functions": ["hal_deinit", "hal_init",
                                     "hal_mac_interrupt_clr_event"],
                       "meaning": "MAC interrupt event clear (W1C)"},
        "0x60035000": {"accesses": 43,
                       "functions": ["clear_bss_queue", "cnx_bss_alloc"],
                       "meaning": "hottest wdev status/queue word"},
        "0x6003500c": {"accesses": 7,
                       "functions": ["hal_get_sta_tsf", "hal_get_tsf_time"],
                       "meaning": "TSF timer low word"},
        "0x6003507c": {"accesses": 3,
                       "functions": ["esp_random"],
                       "meaning": "TRNG — the only WDEV word QEMU models"},
        "0x60033ca8": {"accesses": 2,
                       "functions": ["hal_mac_get_txq_state"],
                       "meaning": "TX-queue state, drain wait reads this"},
        "0x60035118": {"accesses": 1,
                       "functions": ["hal_pwr_interrupt_get_event"],
                       "meaning": "modem-sleep power interrupt get"},
    },
}


def verify_baseline(report: dict) -> list:
    """Compare a freshly generated inventory report against FACTORY_BASELINE.

    Returns a list of human-readable mismatch strings; empty means the
    pinned factory numbers still reproduce from the image."""
    problems = []
    for key, want in FACTORY_BASELINE["symbols"].items():
        got = report.get("symbols", {}).get(key)
        if got != want:
            problems.append(f"symbols.{key}: expected {want}, got {got}")
    access = report.get("firmware_modem_access", {})
    seen = sum(row["accesses"] for row in access.values())
    if seen != FACTORY_BASELINE["total_l32r_modem_accesses"]:
        problems.append(f"total l32r accesses: expected "
                        f"{FACTORY_BASELINE['total_l32r_modem_accesses']}, "
                        f"got {seen}")
    for name, want in FACTORY_BASELINE["windows"].items():
        got = access.get(name)
        if got is None:
            problems.append(f"{name}: missing from report")
            continue
        for field in ("accesses", "distinct_addresses", "observed_range"):
            if got[field] != want[field]:
                problems.append(f"{name}.{field}: expected {want[field]!r}, "
                                f"got {got[field]!r}")
    keys = report.get("key_register_counts", {})
    for addr, want in FACTORY_BASELINE["key_registers"].items():
        got = keys.get(addr)
        if got is None:
            problems.append(f"key register {addr}: missing from report")
        elif got["accesses"] != want["accesses"]:
            problems.append(f"key register {addr}: expected "
                            f"{want['accesses']} accesses, got "
                            f"{got['accesses']}")
    return problems



def parse_l32r_targets(text: str) -> list:
    """Literal-pool targets of l32r loads, attributed to their function.

    Xtensa reads MMIO bases through a 32-bit literal next to the code, so an
    l32r operand is a hardware address the function really accesses. This avoids
    the false positives of scanning raw instruction words.
    """
    targets = []
    current = None
    for line in text.splitlines():
        function = FUNCTION_LINE.match(line)
        if function:
            current = function.group("name")
            continue
        match = L32R_LINE.match(line)
        if match:
            window = window_for(int(match.group("target"), 16))
            if window:
                targets.append((window, current or "?",
                                int(match.group("target"), 16)))
    return targets


def group_targets(targets: list) -> dict:
    grouped: dict[str, dict] = {}
    for window, function, address in targets:
        entry = grouped.setdefault(window, {"accesses": 0, "functions": set(),
                                            "addresses": set()})
        entry["accesses"] += 1
        entry["functions"].add(function)
        entry["addresses"].add(address)
    return {window: {"accesses": data["accesses"],
                     "functions": sorted(data["functions"])[:12],
                     "distinct_addresses": len(data["addresses"]),
                     "observed_range": "0x%08x-0x%08x" % (
                         min(data["addresses"]), max(data["addresses"]))}
            for window, data in grouped.items()}


def key_register_counts(targets: list) -> dict:
    """Per-address access counts for the pinned registers in the baseline."""
    pinned = {int(addr, 16) for addr in FACTORY_BASELINE["key_registers"]}
    counts: dict[int, dict] = {}
    for _, function, address in targets:
        if address in pinned:
            entry = counts.setdefault(address, {"accesses": 0,
                                                "functions": set()})
            entry["accesses"] += 1
            entry["functions"].add(function)
    return {"0x%08x" % address: {"accesses": data["accesses"],
                                 "functions": sorted(data["functions"])}
            for address, data in sorted(counts.items())}


DEVICE_HELP_LINE = re.compile(r'^name "(?P<name>[^"]+)"', re.MULTILINE)


def parse_device_help(text: str) -> list:
    """Compiled-in device type names, for the absent-radio argument."""
    return sorted({m.group("name") for m in DEVICE_HELP_LINE.finditer(text)})


def radio_device_names(names: list) -> dict:
    radio = [name for name in names
             if re.search(r"wifi|wlan|80211|802\.11|phy|mac_(tx|rx)|rt2|ath", name)]
    ethernet = [name for name in names if re.search(r"eth|fec|gmac|lan9|e1000|cdns", name)]
    return {"radio_devices": radio, "ethernet_devices": ethernet}


def run(command: list, timeout: int = 120) -> str:
    result = subprocess.run(command, capture_output=True, text=True,
                            timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError(f"{shlex.join(command)} exit {result.returncode}: "
                           f"{result.stderr[-400:]}")
    return result.stdout


def build_report(nm_text: str, objdump_text: str, mtree_text: str | None,
                 device_help_text: str | None) -> dict:
    symbols = parse_nm(nm_text)
    targets = parse_l32r_targets(objdump_text)
    regions = parse_mtree(mtree_text) if mtree_text else []
    report = {
        "kind": "wifi-radio-capability-inventory",
        "symbols": {"total_defined": len(symbols),
                    **subsystem_counts(symbols)},
        "firmware_modem_access": group_targets(targets),
        "key_register_counts": key_register_counts(targets),
        "qemu_window_coverage": [classify_window(w, regions) for w in windows()]
                                 if regions else [],
        "qemu_device_types": None,
        "not_claimed": [
            "wifi_associated", "ip_assigned", "tls_handshake", "scan_results",
            "any_network_traffic_delivered"],
    }
    if device_help_text is not None:
        names = parse_device_help(device_help_text)
        report["qemu_device_types"] = {"total": len(names),
                                       **radio_device_names(names)}
    report["baseline_mismatches"] = verify_baseline(report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--elf", type=Path, required=True,
                        help="factory firmware ELF (read-only)")
    parser.add_argument("--nm", default="xtensa-esp32s3-elf-nm")
    parser.add_argument("--objdump", default="xtensa-esp32s3-elf-objdump")
    parser.add_argument("--sections", default=".iram0.text,.flash.text",
                        help="comma separated executable sections to disassemble")
    parser.add_argument("--mtree", type=Path,
                        help="captured `info mtree` text for the S3 machine")
    parser.add_argument("--device-help", type=Path,
                        help="captured `qemu-system-xtensa -device help` text")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    elf = args.elf.resolve(strict=True)
    nm_text = run([args.nm, str(elf)])
    objdump_text = "".join(run([args.objdump, "-d", "--section", section, str(elf)])
                           for section in args.sections.split(","))
    mtree_text = args.mtree.read_text() if args.mtree else None
    device_help = (args.device_help.read_text()
                   if args.device_help else None)
    report = build_report(nm_text, objdump_text, mtree_text, device_help)
    report["inputs"] = {"elf": str(elf)}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
