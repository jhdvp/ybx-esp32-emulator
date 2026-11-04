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

    def test_menu_not_falsely_detected_on_book_text_and_selection(self):
        for name in ('reader', 'clip_action', 'home', 'wifi', 'sd_root'):
            with self.subTest(frame=name):
                self.assertFalse(matches_guest_screen(self.frame(name), 'more'))

    def test_view_clippings_proves_saved_item_not_just_open_more(self):
        self.assertFalse(matches_guest_screen(self.frame('more'), 'created'))
        self.assertTrue(matches_guest_screen(self.frame('created'), 'created'))
        self.assertFalse(matches_guest_screen(self.frame('sync'), 'created'))

    def test_sync_not_implicitly_on_first_more_page(self):
        self.assertFalse(matches_guest_screen(self.frame('more'), 'sync'))
        self.assertTrue(matches_guest_screen(self.frame('sync'), 'sync'))

    def test_non_wifi_screen_cannot_trigger_wifi_tap(self):
        self.assertFalse(matches_guest_screen(self.frame('reader'), 'wifi'))
        self.assertFalse(matches_guest_screen(self.frame('more'), 'wifi'))

    def test_invalid_anchor_fails_closed(self):
        with self.assertRaises(ValueError):
            matches_guest_screen(self.frame('reader'), 'unrecognized')


if __name__ == '__main__':
    unittest.main()
