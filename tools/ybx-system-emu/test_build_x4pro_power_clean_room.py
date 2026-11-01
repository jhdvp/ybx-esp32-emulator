"""Offline clean-room builder safety contracts; never compile in unit tests."""
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).with_name('build_x4pro_power_clean_room.sh')


class OfflineCleanRoomBuildContracts(unittest.TestCase):
    def shell(self, *args):
        return subprocess.run(['sh', str(SCRIPT), *args], text=True,
                              capture_output=True, timeout=5)

    def test_without_explicit_build_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            result = self.shell('--work-dir',temp)
            self.assertEqual(result.returncode,1)
            self.assertIn('use --build',result.stderr)
            self.assertEqual(list(Path(temp).iterdir()),[])

    def test_missing_or_nonempty_workspace_fails_before_docker(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            (root/'compose').mkdir()
            result=self.shell('--work-dir',temp,'--build')
            self.assertEqual(result.returncode,1)
            self.assertIn('refusing to reuse or overwrite',result.stderr)

    def test_help_never_compiles(self):
        result=self.shell('--help')
        self.assertEqual(result.returncode,0)
        self.assertIn('--build',result.stderr)
        self.assertIn('No network access',result.stderr)

    def test_pinned_order_and_dependencies(self):
        text=SCRIPT.read_text()
        patches=[
          '0096-s3-rtc-sleep-registers-explicit-wakeup.patch',
          '0097-s3-rtc-explicit-deep-wakeup-dig-reset.patch',
          '0098-gt911-ignore-short-reset-pulse.patch',
          '0098-i2c-gt911-readonly-qom-telemetry.patch',
          '0100-rtc-ext1-gpio-level-wakeup.patch',
          '0101-x4pro-power-button-gpio3-rtc3-reset-retention.patch',
        ]
        self.assertEqual(sorted(text.index(x) for x in patches),
                         [text.index(x) for x in patches])
        for key in ('f5772a62ec52591ff6870b7e8ef32482371f22c6',
                    'b64af41c3276f97f0e181920400ee056b9c88037',
                    'e7af9751d9f9fd3b47911f51a5cfd08af256a9ab'):
            self.assertIn(key,text)
        self.assertIn('--network none',text)
        self.assertIn('--chain full',text)
        self.assertIn('--disable-werror',text)
        self.assertIn('CLEAN_ROOM_BYTE_IDENTICAL_PASS',text)
        self.assertNotIn('docker restart',text)
        self.assertNotIn('docker stop',text)


if __name__ == '__main__':
    unittest.main()
