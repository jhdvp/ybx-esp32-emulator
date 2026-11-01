"""Host-only correctness guards for reproducible 0101 QEMU source verifier."""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from verify_x4pro_power_source_composition import (
    ACCEPTED_0101_BINARY_SHA256, EXPERIMENTAL_SUFFIX, changed_source_paths,
    sha256,
)


class PowerSourceCompositionTests(unittest.TestCase):
    def test_suffix_is_pinned_and_in_hardware_dependency_order(self):
        self.assertEqual(len(EXPERIMENTAL_SUFFIX), 6)
        self.assertEqual([name[:4] for name in EXPERIMENTAL_SUFFIX],
                         ['0096', '0097', '0098', '0098', '0100', '0101'])
        root=Path(__file__).resolve().parent/'experiments'
        self.assertTrue(all((root/name).is_file() for name in EXPERIMENTAL_SUFFIX))

    def test_accepted_binary_is_full_sha256_not_previous_bad_report(self):
        self.assertEqual(len(ACCEPTED_0101_BINARY_SHA256), 64)
        self.assertNotEqual(ACCEPTED_0101_BINARY_SHA256,
                            '7a3feb4ca30af038bc1b7f63165a45197fa298f26a93a87963b051dc245bfab3')

    def test_changed_sources_ignore_untracked_build_artifacts(self):
        from subprocess import run
        with TemporaryDirectory(prefix='ybx-source-fixture-') as tmp:
            root=Path(tmp)
            run(['git', '-C',tmp,'init','-q'],check=True)
            (root/'hw').mkdir()
            (root/'include').mkdir()
            (root/'build-ybx').mkdir()
            (root/'hw/a.c').write_text('original')
            run(['git','-C',tmp,'add','hw/a.c'],check=True)
            (root/'hw/a.c').write_text('changed')
            (root/'include/b.h').write_text('new')
            (root/'build-ybx/large-log').write_text('unrelated')
            self.assertEqual(changed_source_paths(root),['hw/a.c','include/b.h'])

    def test_sha256_streams_bytes(self):
        with TemporaryDirectory() as tmp:
            file=Path(tmp)/'small.bin'
            file.write_bytes(b'abc')
            self.assertEqual(sha256(file),
                'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad')


if __name__ == '__main__':
    unittest.main()
