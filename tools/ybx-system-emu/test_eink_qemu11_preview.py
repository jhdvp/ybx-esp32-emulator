"""Offline safety, input and lossless panel contracts for the 0107 preview."""
import hashlib
import json
from pathlib import Path
import re
import struct
import subprocess
import unittest
from unittest.mock import Mock, patch
import zlib

import eink_qemu11_preview_bridge as bridge
import serve_einkqemu11_browser as native_browser

ROOT = Path(__file__).resolve().parent
LOCK = ROOT / 'third_party/eink-qemu-0.9.2.lock.json'
START = ROOT / 'start_eink_qemu11_preview.sh'


class PreviewV11Tests(unittest.TestCase):
    def test_upstream_release_and_source_pinned(self):
        d = json.loads(LOCK.read_text())
        self.assertEqual(d['qemu_version'], '11.1.2')
        self.assertEqual(d['release'], '0.9.2')
        self.assertEqual(d['source_commit_main_verified_2026_11_02'],
                         'd1b24a5fbc1bbae7ea31075460f63cd4c989c1dc')
        self.assertEqual(d['linux_arm64_archive_sha256'],
                         '6adc72273fd6fea75407ee312af8f21b9c83e67d88720515d56068e71e7f6300')
        self.assertEqual(d['linux_arm64_extracted_binary_sha256'],
                         '8fce2150c091ceb7b644ac7603169620e9fc171ebaa37bed8aece2a457dcbd6a')
        self.assertIn('GPL-2.0', d['license'])

    def test_png_exact_rgb_payload_and_sha(self):
        rgb = bytes([0, 0, 0, 255, 255, 255]) * (bridge.PANEL_WIDTH * bridge.PANEL_HEIGHT // 2)
        raw = f'P6\n{bridge.PANEL_WIDTH} {bridge.PANEL_HEIGHT}\n255\n'.encode() + rgb
        png, sha = bridge.rgba_compatible_png(raw)
        self.assertEqual(sha, hashlib.sha256(rgb).hexdigest())
        self.assertTrue(png.startswith(b'\x89PNG\r\n\x1a\n'))
        length = struct.unpack('>I', png[8:12])[0]
        self.assertEqual(length, 13)
        self.assertEqual(png[12:16], b'IHDR')
        self.assertEqual(struct.unpack('>II', png[16:24]), (480, 800))
        off = 8 + 12 + 13
        self.assertEqual(png[off + 4:off + 8], b'IDAT')
        n = struct.unpack('>I', png[off:off + 4])[0]
        decoded = zlib.decompress(png[off + 8:off + 8 + n])
        self.assertEqual(len(decoded), len(rgb) + 800)
        self.assertEqual(decoded[:7], b'\x00' + rgb[:6])
        self.assertEqual(decoded[-6:], rgb[-6:])

    def test_png_fails_closed_on_truncated_and_wrong_geometry(self):
        prefix = b'P6\n480 800\n255\n'
        for invalid in (b'', b'P5\n480 800\n255\n',
                        b'P6\n480 480\n255\n' + b'\0' * 256,
                        prefix + b'\0' * 18):
            with self.subTest(payload=invalid[:20]):
                with self.assertRaises(ValueError):
                    bridge.rgba_compatible_png(invalid)

    def test_private_bind_and_port_guards(self):
        bridge.require_loopback('127.0.0.1', 8810)
        for host, port in (('0.0.0.0', 8810), ('192.168.10.239', 8810),
                           ('127.0.0.1', 8784), ('127.0.0.1', 8799),
                           ('::1', 8810), ('127.0.0.1', 65535)):
            with self.subTest(host=host, port=port), self.assertRaises(ValueError):
                bridge.require_loopback(host, port)

    def test_touch_coordinates_scaled_and_release_always_sent(self):
        qmp = bridge.QMP()
        qmp.call = Mock(return_value={})
        with patch.object(bridge.time, 'sleep'):
            qmp.tap(240, 400, 170)
        self.assertEqual(qmp.call.call_count, 2)
        commands = [c.args[0] for c in qmp.call.call_args_list]
        self.assertEqual(commands, ['input-send-event', 'input-send-event'])
        events = qmp.call.call_args_list[0].args[1]['events']
        self.assertEqual(len(events), 3)
        self.assertEqual(events[0]['data']['value'], round(32767 * 240 / 479))
        self.assertEqual(events[1]['data']['value'], round(32767 * 400 / 799))
        self.assertTrue(events[2]['data']['down'])
        self.assertFalse(qmp.call.call_args_list[-1].args[1]['events'][0]['data']['down'])

    def test_touch_rejects_out_of_bounds_and_extreme_durations(self):
        qmp = bridge.QMP()
        qmp.call = Mock()
        for x, y, ms in ((480, 0, 170), (-1, 30, 170),
                         (200, 800, 170), (200, 150, 10), (20, 20, 10000)):
            with self.subTest(x=x, y=y, ms=ms), self.assertRaises(ValueError):
                qmp.tap(x, y, ms)
        qmp.call.assert_not_called()

    def test_launcher_is_optin_pinned_and_does_not_touch_8784(self):
        s = START.read_text()
        self.assertIn('linux_arm64_archive_sha256', s)
        self.assertIn('linux_arm64_extracted_binary_sha256', s)
        self.assertIn('source_commit_main_verified_2026_11_02', s)
        self.assertIn('127.0.0.1:$QMP_PORT:4488', s)
        self.assertIn('"$PORT" == "8784"', s)
        self.assertIn('--network bridge', s)
        self.assertIn('model=esp32_wifi', s)
        self.assertIn('[[ "$WORK" = /* && ! -e "$WORK" ]]', s)
        self.assertNotIn('docker stop', s)
        result = subprocess.run(['bash', '-n', str(START)], capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)


    def test_approved_binaries_allow_exact_official_and_gpio21_builds_only(self):
        approved = native_browser.APPROVED_BINARY_SHAS
        self.assertIn(native_browser.KNOWN_BINARY_SHA, approved)
        self.assertIn(native_browser.PATCHED_BINARY_SHA, approved)
        self.assertEqual(len(approved), 2)
        for sha in approved:
            with self.subTest(sha=sha), patch.object(native_browser, 'file_sha', return_value=sha):
                self.assertEqual(native_browser.require_approved_qemu_binary('/private/qemu'), sha)
        with patch.object(native_browser, 'file_sha', return_value='0' * 64):
            with self.assertRaisesRegex(RuntimeError, 'not an audited'):
                native_browser.require_approved_qemu_binary('/private/not-verified')

    def test_patched_browser_launcher_is_loopback_and_optin(self):
        start = ROOT / "start_eink_qemu11_patched_browser.sh"
        script = start.read_text()
        self.assertIn("220f668cc4d0c093fbf292e962cd8c7e92801d75392356f0afc015b661588aef",script)
        self.assertIn("127.0.0.1:$PORT:$PORT", script)
        self.assertIn("--network bridge", script)
        self.assertIn("--reuse", script)
        self.assertIn("Existing container is never replaced", script)
        self.assertIn('PORT="8913"', script)
        self.assertIn("QEMU binary SHA mismatch", script)
        self.assertNotIn('--network host', script)
        result = subprocess.run(["bash", "-n", str(start)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_http_status_never_invents_wifi_connected(self):
        b = bridge.Backend('/tmp/nonexistent', Mock())
        b.qmp.call.return_value = {'running': True, 'status': 'running'}
        b.uart = Mock(return_value='[DBG] [ACT] Entering activity: Home')
        s = b.state()
        self.assertTrue(s['ready'])
        self.assertTrue(s['home_log_observed'])
        self.assertIsNone(s['radio_connected'])
        self.assertTrue(s['experimental'])


if __name__ == '__main__':
    unittest.main()
