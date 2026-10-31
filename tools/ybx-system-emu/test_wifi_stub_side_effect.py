#!/usr/bin/env python3
"""Host tests for the 0081 Wi-Fi stub narrowing (experiment 0081).

No Docker, no QEMU binary, no guest. The tests reconstruct the two stub
files as they would look after applying
experiments/0081-wifi-stub-overlap.patch to the live QEMU tree, then pin:

  * the narrowed window table equals the 0060 factory inventory extremes
    (wifi_radio_inventory.FACTORY_BASELINE), widened by nothing but word
    alignment, plus the 0x60006090 word the Stage-1 runtime tap actually
    measured (the only guest touch in a 120 s boot);
  * every qtest address the 0071 probe reads stays inside a region;
  * the TRNG word 0x6003507c and the inventory windows that were never
    stubbed (FE2/BT_BB/NRX/BB/SYSCON/RTCCNTL) stay catch-all;
  * the host stats block becomes a dedicated region and is no longer
    folded into wdev_high, while stats "window_count" keeps reporting
    the six radio windows.

Pins marked PINNED(0081) carry the exact numbers measured in
experiments/0081-wifi-stub-effect-20261031.md.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

from probe_x4pro_wifi_radio_qtest import STUB_OPS
from wifi_radio_inventory import FACTORY_BASELINE

HERE = Path(__file__).resolve().parent
PATCH = HERE / "experiments" / "0081-wifi-stub-overlap.patch"
LIVE_TREE = Path("/Users/yaraslauhancharou/.local/state/ybx-emulator-"
                 "upstream/qemu-wf-radio-20261031")
RNG_WORD = 0x6003507C
TAP_FE_WORD = 0x60006090  # PINNED(0081): the only runtime tap hit
STATS_BASE, STATS_SIZE = 0x60035F00, 0x20


def _reconstruct(relpath: str) -> str:
    """Apply the patch's hunks for one file to the pristine live copy.

    The patch is a plain unified diff; this is a minimal strict applier:
    every context/removed line must match the live tree byte-for-byte
    (drift guard), removed lines are dropped and '+' lines inserted."""
    text = PATCH.read_text()
    lines = (LIVE_TREE / relpath).read_text().split("\n")
    sections = 0
    for chunk in text.split("--- a/")[1:]:
        header, body = chunk.split("\n", 1)
        if header.strip() != relpath:
            continue
        sections += 1
        parts = re.split(r"(?m)^(@@[^\n]*@@.*)$", body)
        offset = 0
        for i in range(1, len(parts), 2):
            start = int(re.match(r"@@ -(\d+)", parts[i]).group(1))
            hunk = parts[i + 1].split("\n")
            while hunk and (hunk[-1] == "" or hunk[-1].startswith("\\")):
                hunk.pop()
            idx = start - 1 + offset
            pos = idx
            for ln in hunk:
                if ln.startswith(" ") or ln.startswith("-"):
                    assert pos < len(lines) and lines[pos] == ln[1:], \
                        f"hunk line drifted in {relpath}: {ln!r}"
                    pos += 1
            region = [ln[1:] for ln in hunk
                      if ln.startswith(" ") or ln.startswith("-")]
            repl = [ln[1:] for ln in hunk
                    if ln.startswith(" ") or ln.startswith("+")]
            lines[idx:idx + len(region)] = repl
            offset += len(repl) - len(region)
    assert sections == 1, f"{relpath}: {sections} patch sections"
    return "\n".join(lines)


STUB_C = _reconstruct("hw/misc/esp32s3_wifi_stub.c")
STUB_H = _reconstruct("include/hw/misc/esp32s3_wifi_stub.h")

WINDOW_RE = re.compile(
    r'\{\s*"(?P<name>[\w]+)",\s*(?:0x(?P<b1>[0-9a-fA-F]+)|'
    r'ESP32S3_WIFI_STUB_STATS_BASE),\s*(?:0x(?P<s1>[0-9a-fA-F]+)|'
    r'ESP32S3_WIFI_STUB_STATS_SIZE)[^}]*\},', re.S)
WINDOWS: list[tuple[str, int, int]] = []
for _m in WINDOW_RE.finditer(STUB_C.split("forced_zero")[0]):
    _base = int(_m.group("b1"), 16) if _m.group("b1") else STATS_BASE
    _size = int(_m.group("s1"), 16) if _m.group("s1") else STATS_SIZE
    WINDOWS.append((_m.group("name"), _base, _size))


def covers(addr: int, size: int = 4) -> list[str]:
    return [n for n, b, s in WINDOWS if b <= addr and addr + size <= b + s]


def inventory_range(key: str) -> tuple[int, int]:
    lo, hi = FACTORY_BASELINE["windows"][key]["observed_range"].split("-")
    return int(lo, 16), int(hi, 16)


class TestNarrowedWindowTable(unittest.TestCase):
    def test_region_shape(self):
        # PINNED(0081): six radio windows + one stats region
        self.assertEqual([n for n, _, _ in WINDOWS],
                         ["modem_coex", "wlan_mac", "tx_crypto", "wdev_low",
                          "wdev_high", "fe", "ybx_stats"])
        self.assertIn("#define ESP32S3_WIFI_STUB_WINDOW_COUNT 6", STUB_H)
        self.assertIn("#define ESP32S3_WIFI_STUB_REGION_COUNT 7", STUB_H)

    def test_stats_is_its_own_region_and_no_longer_inside_wdev_high(self):
        self.assertEqual(WINDOWS[-1], ("ybx_stats", STATS_BASE, STATS_SIZE))
        for name, base, size in WINDOWS[:-1]:
            self.assertFalse(base <= STATS_BASE < base + size,
                             f"{name} still claims the stats block")
        # the probe ABI keeps reporting the radio window count
        self.assertIn("case 0x04: return ESP32S3_WIFI_STUB_WINDOW_COUNT;",
                      STUB_C)

    def test_windows_are_tight_around_pinned_inventory_ranges(self):
        w = dict((n, (b, s)) for n, b, s in WINDOWS)
        for key, name in (("MODEM_COEX", "modem_coex"),
                          ("WIFI_MAC", "wlan_mac"),
                          ("FE", "fe")):
            lo, hi = inventory_range(key)
            base, size = w[name]
            self.assertLessEqual(base, lo, key)
            self.assertGreaterEqual(base + size, hi + 4, key)
            self.assertLess(base + size - (hi + 4), 4, f"{key} tail slack")
            self.assertLess(lo - base, 4, f"{key} head slack")
        lo, hi = inventory_range("WIFI_TX_CRYPTO")
        self.assertEqual(w["tx_crypto"], (lo, hi + 4 - lo))
        # WDEV split stays around the TRNG word
        wlo, wlow = w["wdev_low"]
        whi, whigh = w["wdev_high"]
        lo, hi = inventory_range("WDEV")
        self.assertEqual((wlo, wlow), (lo, RNG_WORD - lo))
        self.assertGreaterEqual(whi, RNG_WORD + 4, "wdev_high must not eat RNG")
        self.assertGreaterEqual(whi + whigh, hi + 4, "WDEV top uncovered")
        self.assertLess(whi + whigh - (hi + 4), 4, "WDEV tail slack")

    def test_measured_tap_word_stays_answered(self):
        self.assertEqual(covers(TAP_FE_WORD), ["fe"])

    def test_probe_addresses_stay_inside_regions(self):
        for kind, addr, _payload, label, _expect in STUB_OPS:
            self.assertTrue(covers(addr),
                            f"{label}: {addr:#010x} fell out of every region")

    def test_rng_word_never_claimed(self):
        self.assertEqual(covers(RNG_WORD), [])


class TestCatchallRemainsForEverythingElse(unittest.TestCase):
    NEVER_STUBBED = ("FE2", "BT_BB", "NRX", "BB", "SYSCON", "RTCCNTL")

    def test_unrelated_inventory_windows_stay_catchall(self):
        for key in self.NEVER_STUBBED:
            lo, _hi = inventory_range(key)
            self.assertEqual(covers(lo), [], key)

    def test_old_wide_claim_areas_fall_back_to_zero_fill(self):
        # PINNED(0081): words inside the old 0071 windows but outside every
        # measured range; only the catch-all may answer them.
        for addr in (0x60031534, 0x60033D18, 0x60034428, 0x60034000,
                     0x60035120, 0x600061E8, 0x60035E00):
            self.assertEqual(covers(addr), [], f"{addr:#010x}")

    def test_forced_zero_ranges_stay_inside_windows(self):
        fz = re.search(r'forced_zero\[\] = \{(.*?)\};', STUB_C, re.S).group(1)
        for start, end in re.findall(r'\{\s*0x([0-9a-fA-F]+),\s*'
                                     r'0x([0-9a-fA-F]+)\s*\},', fz):
            s, e = int(start, 16), int(end, 16)
            for addr in range(s, e, 4):
                self.assertTrue(covers(addr), f"{addr:#010x} outside window")


class TestHonestyBoundariesUnchanged(unittest.TestCase):
    def test_patch_adds_no_irq_netdev_or_default_on(self):
        for line in PATCH.read_text().splitlines():
            if line.startswith("+") and not line.startswith("+++"):
                low = line.lower()
                for banned in ("sysbus_init_irq", "netdev", "qemu_irq",
                               "receive", "deliver", "connect_irq"):
                    self.assertNotIn(banned, low, line)

    def test_property_guard_and_meson_not_touched(self):
        text = PATCH.read_text()
        self.assertNotIn("meson.build", text)
        added_machine = "\n".join(
            l for l in text.split("hw/xtensa/esp32s3.c b/hw/xtensa/"
                                  "esp32s3.c")[1].splitlines()
            if l.startswith("+") and not l.startswith("+++"))
        self.assertIn("ESP32S3_WIFI_STUB_REGION_COUNT", added_machine)
        self.assertNotIn("ybx-x4pro-wifi-stub", added_machine)


if __name__ == "__main__":
    unittest.main()
