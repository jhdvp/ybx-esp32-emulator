"""Unit tests for the Wi-Fi radio capability inventory (no QEMU, no ELF)."""
import unittest

from wifi_radio_inventory import (FACTORY_BASELINE, classify_window,
                                  group_targets, key_register_counts,
                                  parse_device_help, parse_l32r_targets,
                                  parse_mtree, parse_nm, radio_device_names,
                                  subsystem_counts, verify_baseline,
                                  window_for, windows)

MTREE = """address-space: memory
  0000000000000000-ffffffffffffffff (prio 0, i/o): system
    0000000000000000-00000000fffffffe (prio 0, i/o): cpu0-mem
    0000000060000000-00000000600d0fff (prio 0, i/o): esp32s3.iomem
    000000006003507c-000000006003507f (prio 0, i/o): misc.esp32s3.rng
    0000000060026000-00000000600263ff (prio 0, ram): esp32s3.apbctrl
    00000000600c2000-00000000600c2fff (prio 0, i/o): misc.esp32s3.intmatrix
    0000000042000000-0000000043ffffff (prio 0, i/o): alias esp32s3.icache @esp32s3.dcache 0000000000000000-0000000001ffffff
"""

OBJDUMP = """
422a1dec <hal_mac_init>:
422a1dfb:	ef9291        	l32r	a9, 4229dc44 <hal_random+0x10> (60033ca0 <UART2+0x5ca0>)
422a1e09:	09b8      	l32i.n	a11, a9, 0
42262304 <hal_mac_set_bssid>:
42262310:	123456        	l32r	a3, 422623ff <x+1> (60033004 <UART2+0x4>)
421ce8ec <esp_phy_efuse_get_chip_ver_pkg>:
421ce900:	abcdef        	l32r	a2, 421ce9ff (600c2000)
421ce910:	123456        	mov	a0, a0
"""

NM = """42246adc T esp_wifi_ap_get_prof_password_internal
422a1dec T hal_mac_init
3fca86b5 b _esp_wifi_started
4223970c T coex_init
421ce900 T esp_phy_efuse_get_chip_ver_pkg
4224e56c T esp_wifi_get_config
00000000 U esp_wifi_init
4224e720 T lwip_socket
"""


class MtreeParsing(unittest.TestCase):
    def test_alias_regions_are_dropped(self):
        names = {region.name for region in parse_mtree(MTREE)}
        self.assertNotIn("esp32s3.icache", names)
        self.assertIn("esp32s3.iomem", names)

    def test_address_space_roots_are_never_window_owners(self):
        mac = next(w for w in windows() if w.name == "WIFI_MAC")
        row = classify_window(mac, parse_mtree(MTREE))
        self.assertEqual("zero_fill_catchall", row["status"])
        self.assertEqual([], row["owners"])

    def test_wdev_page_is_partially_modelled_by_rng(self):
        wdev = next(w for w in windows() if w.name == "WDEV")
        row = classify_window(wdev, parse_mtree(MTREE))
        self.assertEqual("partially_modelled", row["status"])
        self.assertEqual(["misc.esp32s3.rng"], row["owners"])

    def test_syscon_ram_blob_is_not_a_register_file(self):
        syscon = next(w for w in windows() if w.name == "SYSCON")
        self.assertEqual("ram_blob",
                         classify_window(syscon, parse_mtree(MTREE))["status"])

    def test_intmatrix_is_modelled(self):
        matrix = next(w for w in windows() if w.name == "INTMATRIX")
        self.assertEqual("modelled",
                         classify_window(matrix, parse_mtree(MTREE))["status"])


class FirmwareEvidence(unittest.TestCase):
    def test_l32r_targets_are_attributed_and_bounded(self):
        targets = parse_l32r_targets(OBJDUMP)
        self.assertEqual(
            [("WIFI_MAC", "hal_mac_init", 0x60033ca0),
             ("WIFI_MAC", "hal_mac_set_bssid", 0x60033004),
             ("INTMATRIX", "esp_phy_efuse_get_chip_ver_pkg", 0x600c2000)],
            targets)

    def test_instruction_bytes_are_not_treated_as_addresses(self):
        self.assertEqual([], parse_l32r_targets(
            "422a1dfb:\tef9291 l32i.n\tnop\n"))

    def test_grouping_counts_functions_and_addresses(self):
        grouped = group_targets(parse_l32r_targets(OBJDUMP))
        self.assertEqual(2, grouped["WIFI_MAC"]["accesses"])
        self.assertEqual(2, grouped["WIFI_MAC"]["distinct_addresses"])
        self.assertIn("hal_mac_init", grouped["WIFI_MAC"]["functions"])

    def test_window_lookup_matches_observed_bases(self):
        self.assertEqual("WIFI_MAC", window_for(0x60033080))
        self.assertEqual("WDEV", window_for(0x6003507C))
        self.assertEqual("NRX", window_for(0x6001CCD4))
        self.assertIsNone(window_for(0x60004000))

    def test_subsystems_separate_radio_from_stack_and_wired_eth(self):
        counts = subsystem_counts(parse_nm(NM))
        self.assertGreaterEqual(counts["esp_wifi_api"], 3)
        self.assertGreaterEqual(counts["coexistence"], 1)
        self.assertGreaterEqual(counts["phy"], 1)
        self.assertGreaterEqual(counts["network_stack"], 1)
        self.assertEqual(0, counts["wired_eth"])


class DeviceInventory(unittest.TestCase):
    def test_device_help_names(self):
        names = parse_device_help('name "xtensa.esp32s3", bus sysfbus\n'
                                  'name "open_eth", bus cssb\n'
                                  'name "vmxnet3", bus PCI, desc "x"\n')
        self.assertEqual(["open_eth", "vmxnet3", "xtensa.esp32s3"], names)

    def test_no_radio_device_but_ethernet_adapters_exist(self):
        found = radio_device_names(["vmxnet3", "open_eth", "xtensa.esp32s3",
                                    "esp32s3.rng"])
        self.assertEqual([], found["radio_devices"])
        self.assertIn("open_eth", found["ethernet_devices"])


# Excerpts from
# xtensa-esp32s3-elf-objdump -d --section .flash.text --section .iram0.text
# of the factory X4 Pro ELF (sha256 7e040053...). Addresses, encodings and
# symbol decorations are verbatim; inter-column whitespace was normalised to
# spaces. They pin that the shipped image really dereferences these literal
# addresses.
FACTORY_EXCERPTS = """
4037e1a8 <hal_mac_interrupt_get_event>:
4037e1a8:	004136        	entry	a1, 32
4037e1ab:	fffe21        	l32r	a2, 4037e1a4 <wdev_dump_rx_linked_list+0x78> (60033c3c <UART2+0x5c3c>)
4037e1bc <hal_mac_interrupt_clr_event>:
4037e1bc:	004136        	entry	a1, 32
4037e1bf:	fffe81        	l32r	a8, 4037e1b8 <hal_mac_interrupt_get_event+0x10> (60033c40 <UART2+0x5c40>)
4037e1d8 <hal_get_tsf_time>:
4037e1d8:	004136        	entry	a1, 32
4037e1db:	fffc81        	l32r	a8, 4037e1cc <hal_mac_interrupt_clr_event+0x10> (6003500c <UART2+0x700c>)
4037e1de:	fffc91        	l32r	a9, 4037e1d0 <hal_mac_interrupt_clr_event+0x14> (6003501c <UART2+0x701c>)
42262290 <hal_deinit>:
422622d8:	ffec81        	l32r	a8, 42262288 <mac_last_rxbuf_init+0xf8> (60033c40 <UART2+0x5c40>)
"""


class PinnedFactoryBaseline(unittest.TestCase):
    """0060: numbers measured from the factory image on 2026-10-31.

    Regenerating the inventory against the same ELF must reproduce every
    pinned count; verify_baseline() is what the tool itself runs so a drift
    is loud rather than silent.
    """

    def test_pinned_window_counts(self):
        want = {
            "WIFI_MAC": (154, 90), "WDEV": (125, 32), "FE": (125, 62),
            "RTCCNTL": (118, 43), "BT_BB": (62, 41), "MODEM_COEX": (35, 16),
            "WIFI_TX_CRYPTO": (34, 20), "SYSCON": (25, 11), "NRX": (10, 7),
            "BB": (3, 3), "FE2": (1, 1),
        }
        self.assertEqual(set(want), set(FACTORY_BASELINE["windows"]))
        for name, (accesses, distinct) in want.items():
            row = FACTORY_BASELINE["windows"][name]
            self.assertEqual(accesses, row["accesses"], name)
            self.assertEqual(distinct, row["distinct_addresses"], name)

    def test_total_and_symbol_pins(self):
        self.assertEqual(692, FACTORY_BASELINE["total_l32r_modem_accesses"])
        self.assertEqual(sum(w["accesses"]
                             for w in FACTORY_BASELINE["windows"].values()),
                         FACTORY_BASELINE["total_l32r_modem_accesses"])
        self.assertEqual(25846, FACTORY_BASELINE["symbols"]["total_defined"])
        self.assertEqual(1, FACTORY_BASELINE["symbols"]["wired_eth"])
        self.assertEqual(292, FACTORY_BASELINE["symbols"]["coexistence"])

    def test_every_window_states_a_reason(self):
        for name, row in FACTORY_BASELINE["windows"].items():
            self.assertTrue(row["reason"].strip(), name)
            self.assertTrue(row["examples"], name)

    def test_key_register_pins(self):
        keys = FACTORY_BASELINE["key_registers"]
        self.assertEqual(3, keys["0x60034400"]["accesses"])
        self.assertEqual(43, keys["0x60035000"]["accesses"])
        self.assertEqual(1, keys["0x60033c3c"]["accesses"])
        self.assertEqual(3, keys["0x60033c40"]["accesses"])
        self.assertEqual(["hal_mac_interrupt_get_event"],
                         keys["0x60033c3c"]["functions"])

    def _report_like_factory(self):
        access = {name: {"accesses": row["accesses"],
                         "distinct_addresses": row["distinct_addresses"],
                         "observed_range": row["observed_range"]}
                  for name, row in FACTORY_BASELINE["windows"].items()}
        keys = {addr: {"accesses": row["accesses"]}
                for addr, row in
                FACTORY_BASELINE["key_registers"].items()}
        return {"symbols": dict(FACTORY_BASELINE["symbols"]),
                "firmware_modem_access": access,
                "key_register_counts": keys}

    def test_verify_baseline_accepts_factory_shaped_report(self):
        self.assertEqual([], verify_baseline(self._report_like_factory()))

    def test_verify_baseline_flags_drift(self):
        report = self._report_like_factory()
        report["firmware_modem_access"]["WIFI_MAC"]["accesses"] = 153
        report["key_register_counts"]["0x60034400"]["accesses"] = 2
        problems = verify_baseline(report)
        self.assertTrue(any("WIFI_MAC.accesses" in p for p in problems))
        self.assertTrue(any("0x60034400" in p for p in problems))

    def test_verify_baseline_flags_missing_window(self):
        report = self._report_like_factory()
        del report["firmware_modem_access"]["FE2"]
        self.assertTrue(any("FE2" in p
                            for p in verify_baseline(report)))


class FactoryExcerpts(unittest.TestCase):
    """l32r attribution against verbatim factory disassembly lines."""

    def test_interrupt_get_and_clear_are_attributed(self):
        targets = parse_l32r_targets(FACTORY_EXCERPTS)
        self.assertIn(("WIFI_MAC", "hal_mac_interrupt_get_event", 0x60033c3c),
                      targets)
        self.assertIn(("WIFI_MAC", "hal_mac_interrupt_clr_event", 0x60033c40),
                      targets)
        # the code that clears the MAC event register inside hal_deinit lives
        # at 422622d8; objdump decorates its literal pool with the neighbouring
        # mac_last_rxbuf_init symbol, attribution must follow the enclosing
        # function header, not the pool decoration.
        self.assertIn(("WIFI_MAC", "hal_deinit", 0x60033c40), targets)

    def test_tsf_reads_are_attributed_to_wdev(self):
        targets = parse_l32r_targets(FACTORY_EXCERPTS)
        self.assertIn(("WDEV", "hal_get_tsf_time", 0x6003500c), targets)
        self.assertIn(("WDEV", "hal_get_tsf_time", 0x6003501c), targets)

    def test_key_register_counts_over_excerpt(self):
        counts = key_register_counts(parse_l32r_targets(FACTORY_EXCERPTS))
        self.assertEqual(1, counts["0x60033c3c"]["accesses"])
        self.assertEqual(2, counts["0x60033c40"]["accesses"])
        self.assertEqual(["hal_deinit", "hal_mac_interrupt_clr_event"],
                         counts["0x60033c40"]["functions"])
        self.assertNotIn("0x60034400", counts)  # not inside the excerpt


if __name__ == "__main__":
    unittest.main()
