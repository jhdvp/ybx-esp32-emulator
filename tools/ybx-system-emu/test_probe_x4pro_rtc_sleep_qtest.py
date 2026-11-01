"""Host-only checks for the optional live QTest RTC sleep probe and patch."""
from pathlib import Path
import unittest
from probe_x4pro_rtc_sleep_qtest import RTC, SLEEP, STATE0, WAKEUP, ENA, RAW, ST, CLR, REJECT_CONF

HERE = Path(__file__).resolve().parent
PATCH = HERE / "experiments/0096-s3-rtc-sleep-registers-explicit-wakeup.patch"


class RtcSleepProbeContracts(unittest.TestCase):
    def test_s3_register_offsets_match_idf(self):
        self.assertEqual(RTC, 0x60008000)
        self.assertEqual((STATE0, WAKEUP, ENA, RAW, ST, CLR, REJECT_CONF),
                         (0x18, 0x3c, 0x40, 0x44, 0x48, 0x4c, 0x68))
        self.assertEqual(SLEEP, 0x80000000)

    def test_explicit_wake_only_not_automatic(self):
        code = PATCH.read_text()
        self.assertIn('"ybx-rtc-inject-wakeup"', code)
        self.assertIn('"ybx-rtc-inject-reject"', code)
        self.assertIn('if (!(s->state0_reg & R_RTC_CNTL_STATE0_SLEEP_EN_MASK))', code)
        self.assertIn('rtc_int_raw &= ~((uint32_t)value)', code)
        self.assertIn('s->rtc_int_raw & s->rtc_int_ena', code)
        self.assertIn('ETS_RTC_CORE_INTR_SOURCE', code)

    def test_patch_covers_three_files_and_no_network_model(self):
        code = PATCH.read_text()
        self.assertEqual(code.count('--- a/'), 3)
        self.assertEqual(code.count('+++ b/'), 3)
        self.assertNotIn('esp32s3_wifi_stub.c', code)
        self.assertNotIn('netdev', code)


if __name__ == '__main__':
    unittest.main()
