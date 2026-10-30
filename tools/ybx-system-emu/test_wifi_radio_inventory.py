"""Unit tests for the Wi-Fi radio capability inventory (no QEMU, no ELF)."""
import unittest

from wifi_radio_inventory import (classify_window, group_targets,
                                  parse_device_help, parse_l32r_targets,
                                  parse_mtree, parse_nm, radio_device_names,
                                  subsystem_counts, window_for, windows)

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


if __name__ == "__main__":
    unittest.main()
