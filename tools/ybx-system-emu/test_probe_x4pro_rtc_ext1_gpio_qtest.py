"""Host contracts for opt-in 0100 EXT1 wake register and level model.

Real IO acceptance is provided by probe_x4pro_rtc_ext1_gpio_qtest.py.
"""
import unittest
from pathlib import Path
from probe_x4pro_rtc_ext1_gpio_qtest import (
    RTC, STATE0, WAKEUP, RESET_CAUSE, EXT1_CONF, EXT1_SEL, EXT1_STATUS,
    SLEEP, EXT1_WAKE, ANY_HIGH, CLEAR_STATUS, DEEPSLEEP_RESET,
)

PATCH = Path(__file__).parent / 'experiments/0100-rtc-ext1-gpio-level-wakeup.patch'


class Ext1SourceContracts(unittest.TestCase):
    def test_s3_register_offsets_and_reset_causes(self):
        self.assertEqual(RTC, 0x60008000)
        self.assertEqual((STATE0, WAKEUP, RESET_CAUSE), (0x18, 0x3c, 0x38))
        self.assertEqual((EXT1_CONF, EXT1_SEL, EXT1_STATUS), (0x64, 0xe0, 0xe4))
        self.assertEqual((SLEEP, EXT1_WAKE, ANY_HIGH, CLEAR_STATUS),
                         (1<<31, 1<<16, 1<<31, 1<<22))
        self.assertEqual(DEEPSLEEP_RESET, 5)

    def test_level_must_be_guest_enabled_and_selected(self):
        content=PATCH.read_text()
        self.assertIn('s->wakeup_state_reg & RTC_CNTL_EXT1_WAKEUP_EN',content)
        self.assertIn('s->state0_reg & R_RTC_CNTL_STATE0_SLEEP_EN_MASK',content)
        self.assertIn('s->ext1_sel & RTC_CNTL_EXT1_PAD_MASK',content)
        self.assertIn('!selected || !valid',content)
        self.assertIn('valid != selected || highs != 0',content)
        self.assertIn('s->ext1_status = active & selected;',content)

    def test_gpio_level_only_not_direct_wakeup_and_negative_gates(self):
        content=PATCH.read_text()
        self.assertIn('ybx.rtc-ext1-pad',content)
        self.assertIn('ybx-rtc-ext1-test-level',content)
        self.assertIn('ESP32_DEEPSLEEP_RESET',content)
        self.assertIn('RTC_CNTL_EXT1_STATUS_CLEAR',content)
        self.assertIn('EXT1 RTC pad must be in range 0..21',content)
        self.assertNotIn('esp32s3_wifi_stub.c',content)

    def test_patch_is_isolated_and_not_a_released_core_change(self):
        content=PATCH.read_text()
        self.assertEqual(content.count('--- a/'),3)
        self.assertEqual(content.count('+++ b/'),3)
        self.assertNotIn('ybx_x4pro_i2c.c',content)
        self.assertNotIn('ybx_ssd1677.c',content)


if __name__=='__main__':
    unittest.main()
