"""Host-only tests for the offline QEMU patch composition auditor.

No live QEMU, no shared checkout access, no network: every Git operation
runs inside throwaway temporary repositories.
"""
import hashlib
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import check_qemu_patch_composition as chk


def run_git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def make_fake_repo(workdir: Path) -> Path:
    repo = workdir / "src"
    repo.mkdir(parents=True)
    run_git(repo, "init", "-q", "-b", "main")
    run_git(repo, "config", "user.email", "ybx@example.invalid")
    run_git(repo, "config", "user.name", "YBX Test")
    (repo / "device.c").write_text("line1\nline2\nline3\nline4\n")
    run_git(repo, "add", "device.c")
    run_git(repo, "commit", "-qm", "base")
    return repo


def patch_text(old: str, new: str) -> str:
    old, new = old.strip(), new.strip()
    return ("diff --git a/device.c b/device.c\n"
            "--- a/device.c\n+++ b/device.c\n"
            "@@ -1,3 +1,3 @@\n"
            f" line1\n-{old}\n+{new}\n line3\n")


def write_patch(workdir: Path, name: str, text: str) -> str:
    """Place a patch under a temp tools dir; returns manifest-style name."""
    patches = workdir / "patches"
    patches.mkdir(parents=True, exist_ok=True)
    (patches / name).write_text(text)
    return f"patches/{name}"


class HelperTests(unittest.TestCase):
    def test_real_chain_definitions_are_self_consistent(self):
        self.assertEqual(chk.verify_chain_definitions(chk.X4PRO_0042_CHAIN), [])
        self.assertTrue(set(chk.QUARANTINED).isdisjoint(chk.X4PRO_0042_CHAIN))
        self.assertTrue(set(chk.UNVALIDATED).isdisjoint(chk.X4PRO_0042_CHAIN))

    def test_real_patches_pass_signature_after_roundtrip(self):
        with TemporaryDirectory() as tmp:
            manifest_path = Path(tmp) / "m.txt"
            hashes = {name: chk.file_sha256(chk.HERE / name)
                      for name in chk.X4PRO_0042_CHAIN}
            chk.write_manifest(manifest_path, hashes, {},
                               chk.X4PRO_0042_CHAIN)
            manifest = chk.parse_manifest(manifest_path)
            self.assertEqual(chk.verify_patch_files(manifest,
                                                    chk.X4PRO_0042_CHAIN), [])

    def test_tampered_patch_is_rejected(self):
        with TemporaryDirectory() as tmp:
            manifest_path = Path(tmp) / "m.txt"
            hashes = {name: "0" * 64 for name in chk.X4PRO_0042_CHAIN}
            chk.write_manifest(manifest_path, hashes, {},
                               chk.X4PRO_0042_CHAIN)
            manifest = chk.parse_manifest(manifest_path)
            failures = chk.verify_patch_files(manifest, chk.X4PRO_0042_CHAIN)
            self.assertEqual(len(failures), len(chk.X4PRO_0042_CHAIN))

    def test_quarantined_patch_in_chain_fails_closed(self):
        poisoned = chk.X4PRO_0042_CHAIN + ("experiments/0020-s3-gpio-input-status-irq-matrix.patch",)
        self.assertTrue(chk.verify_chain_definitions(poisoned))

    def test_malformed_manifest_line_rejected(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "m.txt"
            path.write_text("[patches]\nnotahash  patches/a.patch\n")
            with self.assertRaises(ValueError):
                chk.parse_manifest(path)

    def test_normalize_adds_new_file_mode_once(self):
        with TemporaryDirectory() as tmp:
            here = chk.HERE
            tools = Path(tmp)
            (tools / "experiments").mkdir()
            (tools / "experiments" / "p.patch").write_text(
                "diff --git a/new.c b/new.c\n--- /dev/null\n+++ b/new.c\n"
                "@@ -0,0 +1,2 @@\n+x\n+y\n"
                "diff --git a/known.c b/known.c\nnew file mode 100644\n"
                "--- /dev/null\n+++ b/known.c\n@@ -0,0 +1,1 @@\n+z\n")
            chk.HERE = tools
            try:
                text = chk.normalize_patch("experiments/p.patch")
            finally:
                chk.HERE = here
            self.assertEqual(text.count("new file mode 100644"), 2)

    def test_added_files_detect_both_header_styles(self):
        added = chk.chain_added_files(chk.X4PRO_0042_CHAIN)
        self.assertIn("hw/i2c/ybx_x4pro_i2c.c", added)
        self.assertIn("hw/display/ybx_ssd1677.c", added)


class ComposeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory(prefix="ybx-compose-test-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.src = make_fake_repo(self.root)
        self.base = run_git(self.src, "rev-parse", "HEAD").strip()
        self.here = chk.HERE
        chk.HERE = self.root
        self.addCleanup(setattr, chk, "HERE", self.here)

    def _chain(self, *names):
        return list(names)

    def test_compose_applies_and_is_undoable(self):
        a = write_patch(self.root, "a.patch", patch_text("line2\n", "A\n"))
        b = write_patch(self.root, "b.patch", patch_text("A\n", "B\n"))
        workdir = self.root / "w1"
        repo, results = chk.compose_chain(self.src, workdir,
                                          self._chain(a, b), base=self.base)
        self.assertEqual([r["state"] for r in results], ["applied", "applied"])
        self.assertIn("B", (repo / "device.c").read_text())
        # Reproducibility property: an independent second composition from the
        # same bare base must yield byte-identical chain files.
        repo2, _ = chk.compose_chain(self.src, self.root / "w2",
                                     self._chain(a, b), base=self.base)
        self.assertEqual((repo / "device.c").read_text(),
                         (repo2 / "device.c").read_text())

    def test_order_conflict_fails_closed(self):
        b = write_patch(self.root, "b.patch", patch_text("A\n", "B\n"))
        with self.assertRaises(RuntimeError):
            chk.compose_chain(self.src, self.root / "w3", self._chain(b),
                              base=self.base)

    def test_bad_base_commit_fails(self):
        with self.assertRaises(RuntimeError):
            chk.compose_chain(self.src, self.root / "w4", [],
                              base="0" * 40)

    def test_existing_target_refuses_reuse(self):
        (self.root / "w5" / "qemu-compose").mkdir(parents=True)
        with self.assertRaises(RuntimeError):
            chk.compose_chain(self.src, self.root / "w5", [], base=self.base)

    def test_status_signature_tracks_modifications(self):
        a = write_patch(self.root, "a.patch", patch_text("line2\n", "A\n"))
        n = ("diff --git a/extra.c b/extra.c\n--- /dev/null\n+++ b/extra.c\n"
             "@@ -0,0 +1,1 @@\n+hello\n")
        (self.root / "patches").mkdir(parents=True, exist_ok=True)
        (self.root / "patches" / "n.patch").write_text(n)
        names = self._chain(a, "patches/n.patch")
        repo, _ = chk.compose_chain(self.src, self.root / "w6", names,
                                    base=self.base)
        allowed = chk.chain_touched_files(names)
        sig = chk.status_signature(repo, allowed)
        self.assertEqual(sig, {"device.c": "M", "extra.c": "?"})

    def test_verify_patch_files_flags_missing_disk_file(self):
        manifest = {"patches": {"patches/absent.patch": "0" * 64},
                    "sources": {}, "base_commit": self.base, "chain": []}
        failures = chk.verify_patch_files(manifest, [])
        self.assertIn("manifest lists absent file: patches/absent.patch",
                      failures)


class ProvenanceTests(unittest.TestCase):
    def _image(self, root: Path, **flags) -> Path:
        size = flags.get("size", 0x10001)
        data = bytearray(size)
        if flags.get("magic", True):
            data[0] = 0xE9
        if flags.get("parttable", True):
            data[0x8000:0x8002] = b"\xaa\x50"
        if flags.get("app", True) and size > 0x10000:
            data[0x10000] = 0xE9
        path = root / "img.bin"
        path.write_bytes(bytes(data))
        return path

    def test_validate_flash_accepts_esp_image(self):
        with TemporaryDirectory() as tmp:
            path = self._image(Path(tmp))
            error, info = chk.validate_flash(path)
            self.assertIsNone(error)
            self.assertEqual(info["flash_size"], 0x10001)

    def test_validate_flash_rejects_each_header_stage(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            for kwargs in ({"magic": False}, {"parttable": False},
                           {"app": False}, {"size": 1024}):
                path = self._image(root, **kwargs)
                error, _ = chk.validate_flash(path)
                self.assertIsNotNone(error, kwargs)

    def test_guardrails_reject_non_git_root(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = chk.acceptance_guardrails(root / "nope",
                                               root / "f.bin", root / "r.bin",
                                               None)
            self.assertTrue(report["failures"])

    def test_guardrails_reject_wrong_base_and_flash(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = make_fake_repo(root)
            flash = self._image(root)
            rom = root / "rom.bin"
            rom.write_bytes(b"\x00" * 16)
            report = chk.acceptance_guardrails(src, flash, rom, None)
            joined = " ".join(report["failures"])
            self.assertIn("audited base", joined)
            self.assertIn("not the audited factory build", joined)

    def test_superseded_flash_is_named_in_failure(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = bytearray(0x10001)
            data[0] = 0xE9
            data[0x8000:0x8002] = b"\xaa\x50"
            digest = hashlib.sha256(bytes(data)).hexdigest()
            self.assertNotIn(digest, chk.KNOWN_SUPERSEDED_FLASH)
            self.assertIn("268c62e0",
                          next(iter(chk.KNOWN_SUPERSEDED_FLASH)))


if __name__ == "__main__":
    unittest.main()
