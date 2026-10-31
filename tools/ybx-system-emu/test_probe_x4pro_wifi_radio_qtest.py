"""Unit tests for the Wi-Fi radio qtest/mtree probe logic (no Docker, no QEMU)."""
import unittest

from probe_x4pro_wifi_radio_qtest import (READS, STUB_OPS, classify_reads,
                                          classify_stub, parse_reads,
                                          qtest_protocol, stub_protocol,
                                          verdict, verdict_stub)
from wifi_radio_inventory import parse_mtree

ZERO = "\n".join("OK 0x0000000000000000" for _ in READS) + "\n"
MEASURED = [0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x7c4ef7aa, 0x0, 0x0, 0x0, 0x0, 0x0,
            0x96042000, 0xa000, 0x6]

MTREE = """    0000000060000000-00000000600d0fff (prio 0, i/o): esp32s3.iomem
    000000006003507c-000000006003507f (prio 0, i/o): misc.esp32s3.rng
    0000000060026000-00000000600263ff (prio 0, ram): esp32s3.apbctrl
    00000000600cd000-00000000600cd053 (prio 0, i/o): open_eth.regs
    00000000600cd400-00000000600cd7ff (prio 0, i/o): open_eth.desc
"""


class Protocol(unittest.TestCase):
    def test_reads_cover_every_radio_window_and_a_control(self):
        addresses = {address for _, address, _ in READS}
        self.assertIn(0x60033000, addresses)   # WIFI_MAC base used by factory
        self.assertIn(0x6001d054, addresses)   # BB
        self.assertIn(0x6002607c, addresses)   # apbctrl date control
        self.assertEqual("readl 0x60033000\nreadl 0x60033080\n",
                         qtest_protocol()[:34])

    def test_parse_reads_ignores_monitor_echo(self):
        stdout = ("QEMU 9.2.2 monitor\n(qemu) OK 0x0000000000000000\n"
                  "OK 0x0000000000000006\n")
        self.assertEqual([0x0, 0x6], parse_reads(stdout))

    def test_short_response_is_a_failure_not_a_pass(self):
        with self.assertRaisesRegex(RuntimeError, "expected"):
            classify_reads(parse_reads("OK 0x0\n"))

    def test_silent_zero_reads_match_the_unmodelled_expectation(self):
        rows = classify_reads(parse_reads(ZERO))
        self.assertFalse(all(row["ok"] for row in rows))
        self.assertEqual([False], [row["ok"] for row in rows
                                   if row["label"] == "apbctrl_date_control"])

    def test_measured_values_pass_only_for_modelled_labels(self):
        stdout = "".join(f"OK 0x{value:016x}\n" for value in MEASURED)
        self.assertTrue(all(row["ok"] for row in classify_reads(parse_reads(stdout))))


class Verdict(unittest.TestCase):
    def setUp(self):
        self.rows = classify_reads(MEASURED)
        self.regions = parse_mtree(MTREE)

    def test_radio_windows_reported_unmodelled(self):
        result = verdict(self.rows, self.regions, ["open_eth", "vmxnet3"])
        self.assertTrue(result["radio_register_windows_unmodelled"])
        self.assertEqual("zero_fill_catchall",
                         result["radio_window_statuses"]["WIFI_MAC"])
        self.assertEqual("ram_blob",
                         result["radio_window_statuses"]["SYSCON"])

    def test_never_claims_radio_or_connectivity(self):
        result = verdict(self.rows, self.regions, ["open_eth"])
        self.assertFalse(result["wifi_radio_emulated"])
        self.assertFalse(result["network_connectivity_verified"])

    def test_emac_region_is_not_counted_as_radio(self):
        result = verdict(self.rows, self.regions, ["open_eth", "vmxnet3"])
        self.assertEqual([], result["wifi_device_types_in_qemu"])
        self.assertIn("open_eth", result["ethernet_device_types_in_qemu"])

    def test_a_modelled_mac_window_breaks_the_verdict(self):
        mtree = MTREE + ("    0000000060033000-0000000060033fff "
                         "(prio 0, i/o): esp32s3.wifi_mac\n")
        result = verdict(self.rows, parse_mtree(mtree), ["open_eth"])
        self.assertFalse(result["radio_register_windows_unmodelled"])


STUB_MTREE = """    0000000060000000-00000000600d0fff (prio 0, i/o): esp32s3.iomem
    0000000060031000-0000000060031fff (prio 0, i/o): esp32s3.wifi_stub.modem_coex
    0000000060033000-0000000060033fff (prio 0, i/o): esp32s3.wifi_stub.wlan_mac
    0000000060034000-0000000060034fff (prio 0, i/o): esp32s3.wifi_stub.tx_crypto
    0000000060035000-000000006003507b (prio 0, i/o): esp32s3.wifi_stub.wdev_low
    000000006003507c-000000006003507f (prio 0, i/o): misc.esp32s3.rng
    0000000060035080-0000000060035fff (prio 0, i/o): esp32s3.wifi_stub.wdev_high
    0000000060006000-0000000060006fff (prio 0, i/o): esp32s3.wifi_stub.fe
"""

# What a correct stub answers to stub_protocol(): the same sequence the probe
# sends (see STUB_OPS), so the parser and classifier are pinned end to end.
STUB_VALUES = [0x59425230, 6, 0x5a5a5a5a, 0, 0, 0, 0, 5, 3, 0x6003500c,
               0x60035010]


class StubProtocol(unittest.TestCase):
    def test_protocol_interleaves_writes_and_reads(self):
        text = stub_protocol()
        self.assertTrue(text.startswith("readl 0x60035f00\n"
                                        "readl 0x60035f04\n"
                                        "writel 0x60034400 0x5a5a5a5a\n"))
        self.assertEqual(len([ln for ln in text.splitlines()
                              if ln.startswith("readl")]),
                         len([op for op in STUB_OPS if op[0] == "read"]))

    def test_parser_ignores_write_acknowledgements(self):
        read_values = iter(STUB_VALUES)
        stdout = "".join(
            f"OK 0x{next(read_values):016x}\n" if op[0] == "read" else "OK\n"
            for op in STUB_OPS)
        self.assertEqual(STUB_VALUES, parse_reads(stdout))

    def test_expected_semantics_all_pass(self):
        rows = classify_stub(STUB_VALUES)
        self.assertTrue(all(row["ok"] for row in rows), rows)
        labels = {row["label"] for row in rows}
        self.assertIn("crypto.key_readback", labels)   # key storage coherent
        self.assertIn("mac.intr_still_empty", labels)  # events stay empty
        self.assertIn("tsf.still_frozen", labels)      # TSF never ticks

    def test_short_reply_is_a_failure(self):
        with self.assertRaisesRegex(RuntimeError, "expected"):
            classify_stub(parse_reads("OK 0x0000000059425230\n"))

    def test_nonzero_event_register_breaks_the_verdict(self):
        broken = list(STUB_VALUES)
        broken[3] = 0x00000008  # MAC int get must be empty
        rows = classify_stub(broken)
        result = verdict_stub(rows, parse_mtree(STUB_MTREE))
        self.assertFalse(result["stub_semantics_all_ok"])

    def test_verdict_maps_stub_regions_and_refuses_fake_claims(self):
        result = verdict_stub(classify_stub(STUB_VALUES),
                              parse_mtree(STUB_MTREE))
        self.assertTrue(result["stub_semantics_all_ok"])
        self.assertIn("esp32s3.wifi_stub.wlan_mac",
                      result["stub_mapped_regions"])
        self.assertIn("esp32s3.wifi_stub.tx_crypto",
                      result["radio_window_owners_with_stub"]["WIFI_TX_CRYPTO"])
        # WDEV keeps the TRNG as a co-owner: the split windows do not
        # shadow it.
        self.assertIn("misc.esp32s3.rng",
                      result["radio_window_owners_with_stub"]["WDEV"])
        self.assertFalse(result["association_claimed"])
        self.assertFalse(result["interrupt_wired"])
        self.assertEqual(0, result["frames_delivered"])


if __name__ == "__main__":
    unittest.main()
