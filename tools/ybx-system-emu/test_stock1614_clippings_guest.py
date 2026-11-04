"""Authoritative guest E2E verdict and safety gate unit tests."""
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parent))
from run_stock1614_clippings_guest import StockClippingsGuest, guest_round_trip_valid


def fixture_events():
    root="/kosync/api/v1/clippings/"+"a"*32
    return {
        "requests":[
            {"method":"GET","path":root,"status":200,"kind":"clippings"},
            {"method":"PUT","path":root,"status":200,"kind":"clippings-put"},
            {"method":"GET","path":root,"status":200,"kind":"clippings",
             "query":{"cursor":"0","limit":"1","format":"reader"}},
            {"method":"GET","path":root,"status":200,"kind":"clippings",
             "query":{"cursor":"1","limit":"1","format":"reader"}},
            {"method":"GET","path":"/kosync/syncs/progress/"+"a"*32,
             "status":404,"kind":"missing"},
            {"method":"PUT","path":"/kosync/syncs/progress",
             "status":200,"kind":"progress-put"},
        ],
        "remote_clippings":{"a"*32: {
            "one":{"text":"different","deleted":0,"revision":1}
        }},
    }


class StockGuestVerdictTests(unittest.TestCase):
    def test_real_guest_get_put_get_and_progress_is_verifiable(self):
        self.assertTrue(guest_round_trip_valid(fixture_events(),"different"))

    def test_fake_server_only_without_guest_get_put_get_is_not_verified(self):
        data=fixture_events()
        data["requests"]=[data["requests"][1],data["requests"][-1]]
        self.assertFalse(guest_round_trip_valid(data,"different"))

    def test_no_progress_upload_cannot_claim_success(self):
        data=fixture_events()
        data["requests"]=data["requests"][:-1]
        self.assertFalse(guest_round_trip_valid(data,"different"))

    def test_no_remote_quote_cannot_claim_success(self):
        data=fixture_events()
        data["remote_clippings"]={}
        self.assertFalse(guest_round_trip_valid(data,"different"))

    def test_duplicate_clipping_revision_rejected(self):
        data=fixture_events()
        data["remote_clippings"]["a"*32]["two"]={
            "text":"different","revision":2,"deleted":0}
        self.assertFalse(guest_round_trip_valid(data,"different"))

    def test_repeated_second_sync_requires_two_complete_read_cycles(self):
        data=fixture_events()
        self.assertFalse(guest_round_trip_valid(
            data,"different",required_progress_puts=2))
        data["requests"] += [
            {"method":"GET","path":"/kosync/api/v1/clippings/"+"a"*32,
             "status":200,"kind":"clippings"},
            {"method":"GET","path":"/kosync/api/v1/clippings/"+"a"*32,
             "status":200,"kind":"clippings"},
            {"method":"PUT","path":"/kosync/syncs/progress",
             "status":200,"kind":"progress-put"},
        ]
        self.assertTrue(guest_round_trip_valid(
            data,"different",required_progress_puts=2))

    def test_wifi_stall_is_not_reported_as_success(self):
        with tempfile.TemporaryDirectory() as work:
            p = Path(work)
            guest = StockClippingsGuest(
                preview_port=8896, qmp_port=8897,
                evidence=p / 'ledger.json', out=p / 'screens', tap_delay=0.7)
            guest.out.mkdir()
            stage = {'current': 'sync'}
            def tap(where, label):
                if label == 'Sync Progress':
                    stage['current'] = 'wifi'
            guest.tap = tap
            guest.open_more = lambda: None
            guest.screen_is = lambda name: name == stage['current']
            guest.capture = lambda name: 'test-sha'
            guest.get = lambda path: b'{"radio_connected":null}'
            with self.assertRaisesRegex(RuntimeError, 'did not leave Wi-Fi picker'):
                guest.sync_clippings()
            import json
            verdict = json.loads((guest.out / 'blocked-result.json').read_text())
            self.assertEqual(verdict['status'], 'blocked_guest_wifi_input')
            self.assertEqual(verdict['stage'], 'PICSimLabWifi picker')

    def test_native_x4pro_hardware_page_buttons_use_both_gpio_edges(self):
        with tempfile.TemporaryDirectory() as work:
            guest = StockClippingsGuest(
                preview_port=8950, qmp_port=8951, evidence=Path(work)/'fixture.json',
                out=Path(work)/'screens')
            calls = []
            guest.qmp.call = lambda cmd, args: calls.append((cmd, dict(args)))
            guest.physical_page_key('down', press_s=.02)
            self.assertEqual(calls, [
                ('qom-set', {'path':'/machine/x4pro-keys', 'property':'down', 'value':True}),
                ('qom-set', {'path':'/machine/x4pro-keys', 'property':'down', 'value':False}),
            ])
            for bad_key in ('power', 'home', 'left', '', 'UP'):
                with self.assertRaises(ValueError):
                    guest.physical_page_key(bad_key)
            for bad_duration in (0, .001, .2):
                with self.assertRaises(ValueError):
                    guest.physical_page_key('up', press_s=bad_duration)
            self.assertEqual(len(calls), 2)

    def test_native_x4pro_release_is_guaranteed_after_wait_error(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as work:
            guest = StockClippingsGuest(
                preview_port=8950, qmp_port=8951, evidence=Path(work)/'fixture.json',
                out=Path(work)/'screens')
            calls = []
            guest.qmp.call = lambda cmd, args: calls.append(args['value'])
            with patch('run_stock1614_clippings_guest.time.sleep',
                       side_effect=RuntimeError('interrupted')):
                with self.assertRaisesRegex(RuntimeError, 'interrupted'):
                    guest.physical_page_key('up')
            self.assertEqual(calls, [True, False])

    def test_verified_page_turn_sequence_cannot_pass_on_same_frame(self):
        with tempfile.TemporaryDirectory() as work:
            guest = StockClippingsGuest(
                preview_port=8950, qmp_port=8951, evidence=Path(work)/'fixture.json',
                out=Path(work)/'screens')
            frames = iter([b'PNG-same']*50)
            guest.get = lambda url: next(frames)
            guest.physical_page_key = lambda key: None
            from unittest.mock import patch
            with patch('run_stock1614_clippings_guest.time.sleep', return_value=None), \
                 patch('run_stock1614_clippings_guest.time.monotonic', side_effect=[0, 10]):
                with self.assertRaisesRegex(RuntimeError, 'ignored physical down'):
                    guest.verify_physical_page_turns(('down',))

    def test_sync_failed_ui_does_not_count_as_success(self):
        with tempfile.TemporaryDirectory() as work:
            p = Path(work)
            guest = StockClippingsGuest(
                preview_port=8940, qmp_port=8941, evidence=p/'ledger.json',
                out=p/'screens', tap_delay=0.7)
            guest.out.mkdir()
            guest.screen_is = lambda name: name == 'sync_failed'
            guest.capture = lambda name: 'fake-screen-hash'
            guest.get = lambda path: b'{"ready":true,"radio_connected":null}'
            with self.assertRaisesRegex(RuntimeError, 'Original Stock Sync failed'):
                guest._wait_for_network_exchange(timeout_s=1.0)
            import json
            report = json.loads((guest.out/'blocked-result.json').read_text())
            self.assertEqual(report['status'], 'guest_sync_failed')
            self.assertEqual(report['fixture_events'], 0)

    def test_wifi_connecting_without_guest_http_is_not_e2e(self):
        with tempfile.TemporaryDirectory() as work:
            p = Path(work)
            guest = StockClippingsGuest(
                preview_port=8940, qmp_port=8941, evidence=p/'ledger.json',
                out=p/'screens', tap_delay=0.7)
            guest.out.mkdir()
            guest.screen_is = lambda name: name == 'wifi_connecting'
            guest.capture = lambda name: 'fake-screen-hash'
            guest.get = lambda path: b'{"ready":true,"radio_connected":null}'
            with self.assertRaisesRegex(RuntimeError, 'no KOSync or Clippings HTTP traffic'):
                guest._wait_for_network_exchange(timeout_s=0.001)
            import json
            report = json.loads((guest.out/'blocked-result.json').read_text())
            self.assertEqual(report['status'], 'blocked_guest_no_sync_traffic')
            self.assertEqual(report['stage'], 'Stock Wi-Fi connecting')

    def test_refuses_production_and_non_qemu_ports(self):
        with tempfile.TemporaryDirectory() as work:
            p=Path(work)
            for prev,qmp in ((8784,8811),(8810,8810),(80,8811),(8810,8784)):
                with self.subTest(ports=(prev,qmp)), self.assertRaises(ValueError):
                    StockClippingsGuest(
                        preview_port=prev,qmp_port=qmp,
                        evidence=p/"evidence.json",out=p/"screens")
            guest=StockClippingsGuest(
                preview_port=8896,qmp_port=8897,
                evidence=p/"evidence.json",out=p/"screens")
            self.assertEqual(guest.preview,"http://127.0.0.1:8896")


if __name__=="__main__":
    unittest.main()
