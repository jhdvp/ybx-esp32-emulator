"""Regression assertions against screenshots taken from real Stock QEMU E-Ink."""
from pathlib import Path
import hashlib
import subprocess
import sys
import tempfile
import unittest
from stock1614_ui import ANCHORS, _luma_pixels, matches_guest_screen
from epd_frame_io import save_mono_png

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


class StdlibOnlyMatcherTests(unittest.TestCase):
    """The host CI runner installs no packages, so the matcher stays stdlib-only."""

    def frame(self, name):
        return (SAMPLES / (name + '.png')).read_bytes()

    def test_matcher_needs_no_third_party_image_library(self):
        program = (
            "import sys\n"
            "class Block:\n"
            "    def find_spec(self, name, path=None, target=None):\n"
            "        if name == 'PIL' or name.startswith('PIL.'):\n"
            "            raise ModuleNotFoundError(\"No module named 'PIL'\")\n"
            "        return None\n"
            "sys.meta_path.insert(0, Block())\n"
            "from pathlib import Path\n"
            "from stock1614_ui import matches_guest_screen\n"
            "recent = Path('testdata/stock1614_ui/home_recent.png').read_bytes()\n"
            "reader = Path('testdata/stock1614_ui/reader.png').read_bytes()\n"
            "if not matches_guest_screen(recent, 'home_recent'):\n"
            "    raise SystemExit('home_recent anchor did not match')\n"
            "if matches_guest_screen(reader, 'more'):\n"
            "    raise SystemExit('reader frame falsely matched the more anchor')\n"
            "if 'PIL' in sys.modules:\n"
            "    raise SystemExit('PIL was imported')\n"
        )
        result = subprocess.run([sys.executable, '-c', program],
                                cwd=Path(__file__).resolve().parent,
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_decoded_luma_is_pinned(self):
        width, height, luma = _luma_pixels(self.frame('home'))
        self.assertEqual((width, height), (480, 800))
        self.assertEqual(len(luma), 480 * 800)
        self.assertEqual(hashlib.sha256(luma).hexdigest(),
                         '289e651cd6ad1d91cb066934902ad8b07f7850c3b8e476ee89b2ef5f37c0ea1f')

    def test_images_that_cannot_be_decoded_fail_closed(self):
        with self.assertRaises(ValueError):
            matches_guest_screen(b'not a PNG', 'home')
        with tempfile.TemporaryDirectory() as tmp:
            mono = Path(tmp) / 'mono.png'
            save_mono_png(mono, bytes(16 * 16 // 8), 16, 16)
            with self.assertRaises(ValueError):
                matches_guest_screen(mono.read_bytes(), 'home')


if __name__ == '__main__':
    unittest.main()
