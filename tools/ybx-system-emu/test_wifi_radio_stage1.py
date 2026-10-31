#!/usr/bin/env python3
"""Host tests for the Stage-1 Wi-Fi radio register stub (experiment 0071).

No Docker, no QEMU binary, no guest: the tests parse the tables out of
experiments/0071-s3-wifi-radio-stage1.patch and replay the probe's qtest
sequence (probe_x4pro_wifi_radio_qtest.STUB_OPS) against a pure-Python
reference model of the device. If the shipped patch and the pinned probe
expectations ever disagree, these tests fail. The structural checks pin
the honesty boundaries: the property defaults off, the TRNG word is never
claimed, and nothing in the device can wire an interrupt or a netdev.
"""
from __future__ import annotations

import re
import subprocess
import unittest
from pathlib import Path

from probe_x4pro_wifi_radio_qtest import STUB_OPS, classify_stub

HERE = Path(__file__).resolve().parent
PATCH = HERE / "experiments" / "0071-s3-wifi-radio-stage1.patch"
PINNED_QEMU = Path("/Users/yaraslauhancharou/.local/state/ybx-emulator-"
                   "upstream/qemu-0043-agent-audit-20261030")

RNG_WORD = 0x6003507C  # include/hw/misc/esp32s3_rng.h:26 (misc.esp32s3.rng)

PATCH_TEXT = PATCH.read_text()


def added_lines(path: str) -> str:
    """Concatenated '+' lines of one file section of the patch."""
    for section in PATCH_TEXT.split("diff --git a/")[1:]:
        header = section.splitlines()[0]
        if header.startswith(f"{path} b/{path}"):
            return "\n".join(line[1:] for line in section.splitlines()
                             if line.startswith("+")
                             and not line.startswith("+++"))
    raise AssertionError(f"no patch section for {path}")


STUB_C = added_lines("hw/misc/esp32s3_wifi_stub.c")
STUB_H = added_lines("include/hw/misc/esp32s3_wifi_stub.h")
SOC_C = added_lines("hw/xtensa/esp32s3.c")
MESON = added_lines("hw/misc/meson.build")

WINDOWS = tuple((name, int(base, 16), int(size, 16)) for name, base, size
                in re.findall(r'\{\s*"(\w+)",\s*0x([0-9a-fA-F]+),\s*'
                              r'0x([0-9a-fA-F]+)\s*\},', STUB_C))
FORCED_ZERO = tuple((int(a, 16), int(b, 16)) for a, b in
                    re.findall(r'\{\s*0x([0-9a-fA-F]+),\s*'
                               r'0x([0-9a-fA-F]+)\s*\},',
                               re.search(r'forced_zero\[\] = \{(.*?)\};',
                                         STUB_C, re.S).group(1)))
STATS_BASE = int(re.search(r'STATS_BASE (0x[0-9a-fA-F]+)',
                           STUB_H).group(1), 16)
STATS_SIZE = int(re.search(r'STATS_SIZE (0x[0-9a-fA-F]+)',
                           STUB_H).group(1), 16)
STATS_MAGIC = int(re.search(r'STATS_MAGIC (0x[0-9a-fA-F]+)',
                            STUB_H).group(1), 16)
WINDOW_COUNT = int(re.search(r'WINDOW_COUNT (\d+)', STUB_H).group(1))


class RadioStubModel:
    """Reference model of hw/misc/esp32s3_wifi_stub.c, tables from the patch."""

    def __init__(self) -> None:
        self.storage = [dict() for _ in WINDOWS]
        self.reads = 0
        self.writes = 0
        self.last_read = 0
        self.last_write = 0

    def locate(self, address: int, size: int = 4):
        for index, (_, base, extent) in enumerate(WINDOWS):
            if base <= address and address + size <= base + extent:
                return index, address - base
        return None, None

    def forced_zero(self, address: int, size: int) -> bool:
        return any(address >= start and address + size <= end
                   for start, end in FORCED_ZERO)

    def stats(self, address: int):
        if STATS_BASE <= address < STATS_BASE + STATS_SIZE:
            return address - STATS_BASE
        return None

    def read(self, address: int, size: int = 4):
        offset = self.stats(address)
        if offset is not None and size == 4 and address % 4 == 0:
            # host-side counters are not guest traffic: not counted
            return {0x00: STATS_MAGIC, 0x04: WINDOW_COUNT,
                    0x08: self.reads & 0xffffffff,
                    0x0c: self.reads >> 32,
                    0x10: self.writes & 0xffffffff,
                    0x14: self.writes >> 32,
                    0x18: self.last_read,
                    0x1c: self.last_write}.get(offset, 0)
        index, addr = self.locate(address, size)
        if index is None:
            return None  # unclaimed: the esp32s3.iomem catch-all answers 0
        self.reads += 1
        self.last_read = address
        if self.forced_zero(address, size):
            return 0
        word = self.storage[index].get(addr // 4, 0)
        return (word >> ((addr % 4) * 8)) & ((1 << (size * 8)) - 1)

    def write(self, address: int, value: int, size: int = 4) -> None:
        if self.stats(address) is not None:
            return  # statistics block is read-only and not counted
        index, addr = self.locate(address, size)
        if index is None:
            return
        self.writes += 1
        self.last_write = address
        if self.forced_zero(address, size):
            return  # a clear of an event register that can never latch
        mask = ((1 << (size * 8)) - 1) << ((addr % 4) * 8)
        word = self.storage[index].get(addr // 4, 0)
        self.storage[index][addr // 4] = \
            (word & ~mask) | ((value << ((addr % 4) * 8)) & mask)


class PatchShape(unittest.TestCase):
    def test_six_windows_cover_the_pinned_factory_clusters(self):
        self.assertEqual(len(WINDOWS), 6)
        self.assertEqual(WINDOW_COUNT, 6)
        from wifi_radio_inventory import FACTORY_BASELINE
        for name, stub_name in (("WIFI_MAC", "wlan_mac"),
                                ("MODEM_COEX", "modem_coex"),
                                ("WIFI_TX_CRYPTO", "tx_crypto"),
                                ("FE", "fe")):
            low, high = (int(x, 16) for x in FACTORY_BASELINE["windows"][name]
                         ["observed_range"].split("-"))
            base, size = next((b, s) for n, b, s in WINDOWS
                              if n == stub_name)
            self.assertLessEqual(base, low, name)
            self.assertLess(high, base + size, name)

    def test_wdev_observed_literals_are_claimed_except_the_rng_word(self):
        from wifi_radio_inventory import FACTORY_BASELINE
        low, high = (int(x, 16) for x in FACTORY_BASELINE["windows"]["WDEV"]
                     ["observed_range"].split("-"))
        model = RadioStubModel()
        for address in range(low, high + 1, 4):
            if address == RNG_WORD:
                continue
            self.assertIsNotNone(model.locate(address)[0], hex(address))
        self.assertEqual(
            [b + s for n, b, s in WINDOWS if n == "wdev_low"], [RNG_WORD])
        self.assertEqual(
            [b for n, b, s in WINDOWS if n == "wdev_high"], [RNG_WORD + 4])

    def test_rng_window_is_untouched_by_the_stub(self):
        model = RadioStubModel()
        self.assertEqual(model.locate(RNG_WORD)[0], None)
        self.assertIsNone(model.read(RNG_WORD))
        model.write(RNG_WORD, 0xdeadbeef)
        self.assertEqual(model.reads, 0)
        self.assertEqual(model.writes, 0)
        for _, base, size in WINDOWS:
            self.assertFalse(base <= RNG_WORD < base + size)

    def test_stats_block_sits_beyond_every_measured_factory_access(self):
        self.assertGreater(STATS_BASE, 0x6003511c)  # max measured WDEV literal
        self.assertEqual(STATS_MAGIC, 0x59425230)

    def test_machine_property_defaults_off_and_guards_everything(self):
        self.assertRegex(
            re.sub(r"\s+", " ", SOC_C),
            r'DEFINE_PROP_BOOL\("ybx-x4pro-wifi-stub", '
            r'Esp32s3SocState, ybx_x4pro_wifi_stub, false\)')
        self.assertIn("if (ss->ybx_x4pro_wifi_stub) {", SOC_C)
        self.assertEqual(SOC_C.count('"wifi_stub", &ss->wifi_stub'), 1,
                         "child object must be initialised only inside the "
                         "guarded block (unrealised children assert in 9.2)")
        guard = SOC_C.index("if (ss->ybx_x4pro_wifi_stub) {")
        init = SOC_C.index('"wifi_stub", &ss->wifi_stub')
        realize = SOC_C.index("qdev_realize(DEVICE(&ss->wifi_stub)")
        self.assertLess(guard, init)
        self.assertLess(init, realize)

    def test_device_is_registered_but_not_user_creatable(self):
        self.assertIn("'esp32s3_wifi_stub.c',", MESON)
        self.assertIn("dc->user_creatable = false;", STUB_C)
        self.assertIn('TYPE_ESP32S3_WIFI_STUB "misc.esp32s3.wifi_stub"',
                      STUB_H)


class StubSemantics(unittest.TestCase):
    def test_probe_sequence_replays_exactly_on_the_reference_model(self):
        model = RadioStubModel()
        values = []
        for kind, address, data, _, _ in STUB_OPS:
            if kind == "write":
                model.write(address, data)
            else:
                values.append(model.read(address))
        rows = classify_stub(values)
        self.assertTrue(all(row["ok"] for row in rows), rows)

    def test_write_ack_filtering_keeps_event_registers_empty(self):
        model = RadioStubModel()
        for start, end in FORCED_ZERO:
            for address in range(start, end, 4):
                model.write(address, 0xffffffff)
                self.assertEqual(model.read(address), 0, hex(address))
        self.assertEqual(len(FORCED_ZERO), 4)

    def test_key_and_control_registers_read_back(self):
        model = RadioStubModel()
        model.write(0x60034400, 0x5a5a5a5a)   # hal_crypto_set_key_entry
        self.assertEqual(model.read(0x60034400), 0x5a5a5a5a)
        model.write(0x60033000, 0x00000001)   # MAC ctrl (force_txrx_off poll)
        self.assertEqual(model.read(0x60033000), 1)
        model.write(0x6000607c, 0xcafe0000)   # fe PA switch
        self.assertEqual(model.read(0x6000607c), 0xcafe0000)

    def test_tsf_is_frozen_across_get_and_set_words(self):
        model = RadioStubModel()
        for address in (0x6003500c, 0x60035010, 0x60035014, 0x6003501c):
            model.write(address, 0xdeadbeef)
        for address in range(0x6003500c, 0x60035020, 4):
            self.assertEqual(model.read(address), 0, hex(address))

    def test_counters_track_guest_traffic_not_stats_reads(self):
        model = RadioStubModel()
        model.write(0x60035f08, 0xffffffff)   # stats write ignored entirely
        model.write(0x60034400, 7)
        self.assertEqual(model.read(0x60035f10), 1)   # write count
        self.assertEqual(model.read(0x60034400), 7)
        self.assertEqual(model.read(0x60035f08), 1)   # stats reads uncounted
        self.assertEqual(model.read(0x60035f18), 0x60034400)
        self.assertEqual(model.read(0x60035f1c), 0x60034400)

    def test_sub_word_lanes_behave_like_a_register_file(self):
        model = RadioStubModel()
        model.write(0x60033002, 0x1234, size=2)
        self.assertEqual(model.read(0x60033002, size=2), 0x1234)
        self.assertEqual(model.read(0x60033000), 0x1234 << 16)
        self.assertEqual(model.read(0x60033003, size=1), 0x12)

    def test_nothing_can_claim_connectivity(self):
        for banned in ("sysbus_init_irq", "qemu_irq", "qdev_prop_set_netdev",
                       "qemu_find_netdev", "qemu_new_net_client",
                       "qemu_send_packet"):
            self.assertNotIn(banned, STUB_C)
        for line in SOC_C.splitlines():
            if "wifi_stub" in line:
                self.assertNotIn("irq", line)


@unittest.skipUnless(PINNED_QEMU.is_dir(), "pinned QEMU checkout not present")
class PatchApplies(unittest.TestCase):
    def test_applies_cleanly_to_the_pinned_checkout_read_only(self):
        result = subprocess.run(
            ["git", "-C", str(PINNED_QEMU), "apply", "--check", str(PATCH)],
            capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
