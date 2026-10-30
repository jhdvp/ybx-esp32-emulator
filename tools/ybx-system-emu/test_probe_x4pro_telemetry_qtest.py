"""Host-only invariants for opt-in QEMU instrumentation."""
import unittest
from probe_x4pro_telemetry_qtest import assert_non_decreasing, read_property

class TelemetryQtestGuards(unittest.TestCase):
    def test_monotonic_counter(self):
        assert_non_decreasing({"irq": 0}, {"irq": 4}, "irq")
        assert_non_decreasing({"irq": 4}, {"irq": 4}, "irq")
        with self.assertRaises(AssertionError):
            assert_non_decreasing({"irq": 4}, {"irq": 2}, "irq")

    def test_missing_qom_property_fails_closed(self):
        def qmp(_cmd, _args):
            return {"error": {"desc": "property absent"}}
        with self.assertRaises(AssertionError):
            read_property(qmp, "/machine/soc", "ybx-epd-busy-rise-edges")

    def test_qom_int_value_not_synthesized(self):
        def qmp(command, args):
            self.assertEqual(command, "qom-get")
            self.assertEqual(args["path"], "/machine/soc")
            return {"return": 13}
        self.assertEqual(read_property(qmp, "/machine/soc", "ybx-epd-busy-rise-edges"), 13)
