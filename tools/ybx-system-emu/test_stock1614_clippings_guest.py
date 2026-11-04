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
