"""Unit tests for the read-only QEMU guest HTTP diagnostics."""
import importlib.util
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "tools/ybx-system-emu/probe_ybx_guest_http.py"
spec = importlib.util.spec_from_file_location("ybx_guest_http_probe", SCRIPT)
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


class ProbeTests(TestCase):
    @patch.object(probe, "guest_probe", side_effect=[{"status": 200}, {"status": 200}])
    @patch.object(probe, "bridge_probe", return_value={
        "qmp_running": True, "screen_dimensions": [480, 800]})
    def test_ready_requires_both_firmware_endpoints(self, bridge, guest):
        result = probe.run(8820, "ybx-eink-qemu11-8820", 18080)
        self.assertTrue(result["ok"])
        self.assertEqual(guest.call_count, 2)

    @patch.object(probe, "guest_probe", return_value={"error_type": "ConnectionResetError"})
    @patch.object(probe, "bridge_probe", return_value={
        "qmp_running": True, "screen_dimensions": [480, 800]})
    def test_qmp_running_cannot_mask_broken_guest_network(self, bridge, guest):
        result = probe.run(8820, "ybx-eink-qemu11-8820", 18080)
        self.assertFalse(result["ok"])
        self.assertEqual(result["guest_plugin_js"]["error_type"], "not_checked")
        self.assertEqual(guest.call_count, 1)

    @patch.object(probe, "guest_probe")
    @patch.object(probe, "bridge_probe", return_value={"qmp_running": False})
    def test_unbooted_guest_is_not_probed(self, bridge, guest):
        result = probe.run(8820, "ybx-eink-qemu11-8820", 18080)
        self.assertFalse(result["ok"])
        guest.assert_not_called()
