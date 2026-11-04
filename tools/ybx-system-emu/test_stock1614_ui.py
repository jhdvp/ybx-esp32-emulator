"""Regression assertions against screenshots taken from real Stock QEMU E-Ink."""
from pathlib import Path
import unittest
from stock1614_ui import ANCHORS, matches_guest_screen

SAMPLES = Path(__file__).resolve().parent / 'testdata' / 'stock1614_ui'


class RealGuestUIAnchorsTests(unittest.TestCase):
    def frame(self, name):
        return (SAMPLES / (name + '.png')).read_bytes()

    def test_all_positive_real_guest_screen_anchors(self):
        for name in ANCHORS:
            with self.subTest(name=name):
                self.assertTrue(matches_guest_screen(self.frame(name), name))

    def test_recent_book_home_is_not_a_reader_or_empty_home(self):
        self.assertTrue(matches_guest_screen(self.frame('home_recent'), 'home_recent'))
        self.assertFalse(matches_guest_screen(self.frame('home_recent'), 'reader'))
        self.assertFalse(matches_guest_screen(self.frame('home'), 'home_recent'))

    def test_menu_not_falsely_detected_on_book_text_and_selection(self):
        for name in ('reader', 'clip_action', 'home', 'wifi', 'sd_root'):
            with self.subTest(frame=name):
                self.assertFalse(matches_guest_screen(self.frame(name), 'more'))

    def test_view_clippings_proves_saved_item_not_just_open_more(self):
        self.assertFalse(matches_guest_screen(self.frame('more'), 'created'))
        self.assertTrue(matches_guest_screen(self.frame('created'), 'created'))
        self.assertFalse(matches_guest_screen(self.frame('sync'), 'created'))

    def test_clipping_created_label_can_shift_down_one_real_stock_row(self):
        self.assertTrue(matches_guest_screen(self.frame('created-shifted'), 'created'))
        for other in ('more', 'sync', 'reader', 'clip_action'):
            with self.subTest(other=other):
                self.assertFalse(matches_guest_screen(self.frame(other), 'created'))

    def test_sync_not_implicitly_on_first_more_page(self):
        self.assertFalse(matches_guest_screen(self.frame('more'), 'sync'))
        self.assertTrue(matches_guest_screen(self.frame('sync'), 'sync'))

    def test_non_wifi_screen_cannot_trigger_wifi_tap(self):
        self.assertFalse(matches_guest_screen(self.frame('reader'), 'wifi'))
        self.assertFalse(matches_guest_screen(self.frame('more'), 'wifi'))

    def test_connecting_and_real_sync_failed_are_not_success(self):
        self.assertTrue(matches_guest_screen(self.frame('wifi_connecting'), 'wifi_connecting'))
        self.assertTrue(matches_guest_screen(self.frame('sync_failed'), 'sync_failed'))
        self.assertFalse(matches_guest_screen(self.frame('wifi_connecting'), 'sync_failed'))
        self.assertFalse(matches_guest_screen(self.frame('sync_failed'), 'wifi_connecting'))
        self.assertFalse(matches_guest_screen(self.frame('reader'), 'sync_failed'))

    def test_invalid_anchor_fails_closed(self):
        with self.assertRaises(ValueError):
            matches_guest_screen(self.frame('reader'), 'unrecognized')


if __name__ == '__main__':
    unittest.main()
