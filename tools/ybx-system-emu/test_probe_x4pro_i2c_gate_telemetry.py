"""Host-only contract checks for optional 0098 GT911/I2C diagnostics.

The actual gate/IRQ behavior is separately checked by the real QTest probe,
not inferred from mocks below.
"""
import unittest
from pathlib import Path
from probe_x4pro_inkmod_trace import (
    I2C_DEBUG_PROPS, GT911_DEBUG_PROPS,
    snapshot_i2c_debug, snapshot_gt911_debug,
)

HERE = Path(__file__).resolve().parent / 'experiments'


class ReadOnlyTelemetryContracts(unittest.TestCase):
    def test_i2c_counters_are_fetched_from_two_distinct_controllers(self):
        calls = []
        def qmp(command, args):
            self.assertEqual(command, 'qom-get')
            calls.append((args['path'], args['property']))
            return {'return': 17 if 'i2c0' in args['path'] else 23}
        snapshot = snapshot_i2c_debug(qmp)
        self.assertEqual(set(snapshot), {'i2c0', 'i2c1'})
        self.assertTrue(all(value == 17 for value in snapshot['i2c0'].values()))
        self.assertTrue(all(value == 23 for value in snapshot['i2c1'].values()))
        self.assertEqual(len(calls), 2 * len(I2C_DEBUG_PROPS))
        self.assertEqual(calls[0], ('/machine/soc/i2c0', 'ybx-trans-starts'))
        self.assertEqual(calls[-1], ('/machine/soc/i2c1', 'ybx-last-int-ena'))

    def test_gt911_readiness_is_observed_not_set(self):
        calls = []
        def qmp(command, args):
            calls.append((command, args['path'], args['property']))
            return {'return': args['property'] in (
                'ybx-gt911-ready', 'ybx-gt911-power-present')}
        state = snapshot_gt911_debug(qmp)
        self.assertTrue(state['ready'])
        self.assertTrue(state['power-present'])
        self.assertFalse(state['reset-low-seen'])
        self.assertEqual(len(calls), len(GT911_DEBUG_PROPS))
        self.assertTrue(all(c[0] == 'qom-get' and c[1] == '/machine/soc'
                            for c in calls))

    def test_qmp_failures_are_not_converted_to_fake_success(self):
        with self.assertRaisesRegex(RuntimeError, 'missing QOM property'):
            snapshot_i2c_debug(lambda command, args: {'error': 'missing'})

    def test_functional_patch_and_optional_telemetry_are_separate(self):
        fix = (HERE / '0098-gt911-ignore-short-reset-pulse.patch').read_text()
        telemetry = (HERE / '0098-i2c-gt911-readonly-qom-telemetry.patch').read_text()
        self.assertEqual(fix.count('--- a/'), 1)
        self.assertIn('ready_after_ns', fix)
        self.assertIn('data_ready', fix)
        self.assertIn('gt911_update_irq', fix)
        self.assertNotIn('ybx-trans-starts', fix)
        self.assertIn('ybx-trans-starts', telemetry)
        self.assertIn('ybx-gt911-ready', telemetry)
        self.assertNotIn('ybx-rtc-inject-deep-wakeup', fix)
        self.assertNotIn('esp32s3_wifi_stub.c', fix)


if __name__ == '__main__':
    unittest.main()
