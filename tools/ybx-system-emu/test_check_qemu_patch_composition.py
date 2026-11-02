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


def patch_text_tail(old: str, new: str) -> str:
    """Independent hunk on the fourth line (no context overlap with patch_text)."""
    old, new = old.strip(), new.strip()
    return ("diff --git a/device.c b/device.c\n"
            "--- a/device.c\n+++ b/device.c\n"
            "@@ -3,2 +3,2 @@\n"
            f" line3\n-{old}\n+{new}\n")


def write_patch(workdir: Path, name: str, text: str) -> str:
    """Place a patch under a temp tools dir; returns manifest-style name."""
    patches = workdir / "patches"
    patches.mkdir(parents=True, exist_ok=True)
    (patches / name).write_text(text)
    return f"patches/{name}"


class HelperTests(unittest.TestCase):
    def test_real_chain_definitions_are_self_consistent(self):
        self.assertEqual(chk.verify_chain_definitions(chk.X4PRO_0042_CHAIN), [])
        self.assertEqual(chk.verify_chain_definitions(chk.X4PRO_FULL_CHAIN), [])
        self.assertTrue(set(chk.QUARANTINED).isdisjoint(chk.X4PRO_FULL_CHAIN))
        self.assertTrue(set(chk.REJECTED).isdisjoint(chk.X4PRO_FULL_CHAIN))

    def test_full_chain_is_0042_prefix_plus_ordered_telemetry(self):
        self.assertEqual(chk.X4PRO_FULL_CHAIN,
                         chk.X4PRO_0042_CHAIN + chk.TELEMETRY_CHAIN
                         + chk.PSRAM_CHAIN + chk.POST_PSRAM_CHAIN
                         + chk.RTC_POWER_CHAIN)
        self.assertEqual(chk.TELEMETRY_CHAIN[0],
                         "experiments/0043-s3-optin-crosscore-ipi-counters.patch")
        self.assertEqual(chk.TELEMETRY_CHAIN[1],
                         "experiments/0037-x4pro-epd-busy-window-edge-telemetry.patch")
        # 0051 rewrites 0037 hunks, so it must stay last inside the telemetry
        # layer; the PSRAM bounds fix (0068) follows it.
        self.assertEqual(chk.TELEMETRY_CHAIN[-1],
                         "experiments/0051-busy-telemetry-reset-order.patch")
        self.assertEqual(chk.PSRAM_CHAIN,
                         ("experiments/0068-esp32s3-bootloader-flash-fix.patch",))
        self.assertEqual(chk.POST_PSRAM_CHAIN,
                         ("experiments/0069-ssd1677-waveform-sleep-v2.patch",
                          "experiments/0071-s3-wifi-radio-stage1.patch"))
        self.assertEqual(chk.X4PRO_FULL_CHAIN[-1],
                         "experiments/0101-x4pro-power-button-gpio3-rtc3-reset-retention.patch")
        self.assertEqual(len(chk.X4PRO_FULL_CHAIN), 32)
        self.assertIn("hw/xtensa/esp32s3.c",
                      chk.chain_touched_files(chk.RTC_POWER_CHAIN))
        self.assertIn("hw/gpio/esp32_gpio.c",
                      chk.chain_touched_files(chk.RTC_POWER_CHAIN))

    def test_0043_telemetry_is_audited_not_excluded(self):
        name = "experiments/0043-s3-optin-crosscore-ipi-counters.patch"
        self.assertIn(name, chk.X4PRO_FULL_CHAIN)
        self.assertEqual(chk.pick_chain("full"), chk.X4PRO_FULL_CHAIN)
        self.assertEqual(chk.pick_chain("0042"), chk.X4PRO_0042_CHAIN)

    def test_expected_binary_sha_is_pinned_literal(self):
        self.assertEqual(
            chk.EXPECTED_BINARY_SHA256,
            "7b729fba1ceefdc67a55dc9ddd6d359699e16174a52d741ea243849c35e2accc")
        self.assertRegex(chk.EXPECTED_BINARY_SHA256, r"^[0-9a-f]{64}$")

    def test_telemetry_patch_in_0042_chain_fails_closed(self):
        poisoned = chk.X4PRO_0042_CHAIN + chk.TELEMETRY_CHAIN[:1]
        self.assertTrue(any("audited" in f for f in
                            chk.verify_chain_definitions(poisoned)))

    def test_real_patches_pass_signature_after_roundtrip(self):
        with TemporaryDirectory() as tmp:
            manifest_path = Path(tmp) / "m.txt"
            hashes = {name: chk.file_sha256(chk.HERE / name)
                      for name in chk.X4PRO_FULL_CHAIN}
            chk.write_manifest(manifest_path, hashes, {},
                               chk.X4PRO_FULL_CHAIN)
            manifest = chk.parse_manifest(manifest_path)
            self.assertEqual(chk.verify_patch_files(manifest,
                                                    chk.X4PRO_FULL_CHAIN), [])
            self.assertEqual(tuple(manifest["chain"]), chk.X4PRO_FULL_CHAIN)

    def test_manifest_base_override_roundtrips(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "m.txt"
            chk.write_manifest(path, {}, {}, chk.X4PRO_FULL_CHAIN,
                               base="a" * 40)
            self.assertEqual(chk.parse_manifest(path)["base_commit"], "a" * 40)

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

    def test_normalize_strips_prose_preamble(self):
        with TemporaryDirectory() as tmp:
            here = chk.HERE
            tools = Path(tmp)
            (tools / "experiments").mkdir()
            (tools / "experiments" / "p.patch").write_text(
                "Some prose header\nwith lines\n\ndiff --git a/f.c b/f.c\n"
                "--- a/f.c\n+++ b/f.c\n@@ -1,1 +1,1 @@\n-x\n+y\n")
            chk.HERE = tools
            try:
                text = chk.normalize_patch("experiments/p.patch")
            finally:
                chk.HERE = here
            self.assertTrue(text.startswith("diff --git a/f.c b/f.c"))

    def test_normalize_without_diff_payload_raises(self):
        with TemporaryDirectory() as tmp:
            tools = Path(tmp)
            (tools / "e").mkdir()
            (tools / "e" / "bad.patch").write_text("prose only\n")
            with self.assertRaises(RuntimeError):
                chk.normalize_patch("e/bad.patch", tools)

    def test_real_0043_patch_normalizes_to_git_payload(self):
        text = chk.normalize_patch(
            "experiments/0043-s3-optin-crosscore-ipi-counters.patch")
        self.assertTrue(text.startswith("diff --git a/hw/xtensa/esp32s3_clk.c"))

    def test_added_files_detect_both_header_styles(self):
        added = chk.chain_added_files(chk.X4PRO_0042_CHAIN)
        self.assertIn("hw/i2c/ybx_x4pro_i2c.c", added)
        self.assertIn("hw/display/ybx_ssd1677.c", added)


class ComposeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = TemporaryDirectory(prefix="ybx-compose-test-")
        cls.root = Path(cls.tmp.name)
        cls.src = make_fake_repo(cls.root)
        cls.base = run_git(cls.src, "rev-parse", "HEAD").strip()
        cls.here = chk.HERE
        chk.HERE = cls.root

    @classmethod
    def tearDownClass(cls):
        chk.HERE = cls.here
        cls.tmp.cleanup()

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


class ComposeAndSignTests(unittest.TestCase):
    """compose_and_sign: the host-only pre-build stage used by the builder."""

    @classmethod
    def setUpClass(cls):
        # One shared synthetic sandbox for the whole class: individual tests
        # never mutate the base repo or the a/b/n patches, they only add new
        # files. Keeps the host suite under its time budget.
        cls.tmp = TemporaryDirectory(prefix="ybx-compose-sign-")
        cls.root = Path(cls.tmp.name)
        cls.src = make_fake_repo(cls.root)
        cls.base = run_git(cls.src, "rev-parse", "HEAD").strip()
        cls.here = chk.HERE
        chk.HERE = cls.root
        cls.a = write_patch(cls.root, "a.patch", patch_text("line2\n", "A\n"))
        cls.b = write_patch(cls.root, "b.patch",
                            patch_text_tail("line4\n", "D\n"))
        n = ("diff --git a/extra.c b/extra.c\n--- /dev/null\n+++ b/extra.c\n"
             "@@ -0,0 +1,1 @@\n+hello\n")
        (cls.root / "patches" / "n.patch").write_text(n)
        cls.n = "patches/n.patch"

    @classmethod
    def tearDownClass(cls):
        chk.HERE = cls.here
        cls.tmp.cleanup()

    def _sign(self, out, chain, **kw):
        kw.setdefault("base", self.base)
        return chk.compose_and_sign(self.src, out, chain, **kw)

    def test_dry_run_writes_deterministic_manifest_without_building(self):
        report = self._sign(self.root / "o1", [self.a, self.b, self.n])
        self.assertTrue(report["ok"], report.get("failures"))
        self.assertTrue(Path(report["manifest"]).is_file())
        self.assertTrue(report["deterministic_recompose"])
        # Idempotent reverse check: conflict-free chains undo fully.
        self.assertTrue(report["reverse_undo"]["clean"])
        manifest = Path(report["manifest"]).read_text()
        self.assertIn("[sources]", manifest)
        self.assertIn("extra.c", manifest)
        # cross-run byte equality is itself the determinism proof, so the
        # second sign skips its internal double-compose
        other = self._sign(self.root / "o2", [self.a, self.b, self.n],
                           determinism_check=False)
        self.assertEqual(Path(other["manifest"]).read_text(), manifest)

    def test_already_applied_patch_is_detected(self):
        dup = write_patch(self.root, "dup.patch",
                          (self.root / "patches" / "a.patch").read_text())
        report = self._sign(self.root / "o3", [self.a, dup],
                            determinism_check=False)
        self.assertTrue(report["ok"], report.get("failures"))
        self.assertEqual([r["state"] for r in report["patches"]],
                         ["applied", "already-applied"])

    def test_marker_checks_follow_chain_membership(self):
        """0042-only trees must not be held to telemetry markers, but a chain
        containing 0037/0051 must enforce busy_rise_edges (regression: the
        first chain-gating draft defaulted chain=() and would have silenced
        the full audited run)."""
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            panel = repo / "hw" / "display"
            panel.mkdir(parents=True)
            (panel / "ybx_ssd1677.c").write_text("no telemetry here\n")
            allowed = {"hw/display/ybx_ssd1677.c"}
            fails = chk.check_sources(repo, allowed, set(),
                                      ("experiments/0042-s3-epd.patch",))
            self.assertEqual(fails, [])
            fails = chk.check_sources(repo, allowed, set(),
                                      ("experiments/0037-x4pro-epd.patch",))
            self.assertTrue(any("busy_rise_edges" in f for f in fails))
        with self.assertRaises(TypeError):
            chk.check_sources(repo, allowed, set())

    def test_missing_patch_file_fails_closed(self):
        report = self._sign(self.root / "o4", [self.a, "patches/absent.patch"])
        self.assertFalse(report["ok"])
        self.assertEqual(report["stage"], "chain")
        self.assertTrue(any("absent" in f for f in report["failures"]))

    def test_wrong_base_commit_fails_closed(self):
        with self.assertRaises(RuntimeError):
            self._sign(self.root / "o5", [self.a], base="0" * 40)

    def test_dirty_target_refuses_reuse(self):
        out = self.root / "o6"
        out.mkdir(parents=True)
        (out / "composition-manifest.sha256").write_text("stale\n")
        with self.assertRaises(RuntimeError):
            self._sign(out, [self.a])

    def test_conflicting_chain_fails_closed(self):
        orphan = write_patch(self.root, "orphan.patch",
                             patch_text("A\n", "Z\n"))
        with self.assertRaises(RuntimeError):
            self._sign(self.root / "o7", [orphan])

    def test_audited_chain_drifts_are_caught(self):
        """Simulate an audited chain + checked-in manifest comparison."""
        chain = (self.a, self.b)
        old_manifest = chk.MANIFEST
        old_audited = chk.AUDITED_CHAINS
        self.addCleanup(setattr, chk, "MANIFEST", old_manifest)
        self.addCleanup(setattr, chk, "AUDITED_CHAINS", old_audited)
        chk.AUDITED_CHAINS = (chain,)
        chk.MANIFEST = self.root / "checked-in.sha256"
        good = self._sign(self.root / "o8", chain, compare_manifest=False)
        self.assertTrue(good["ok"], good.get("failures"))
        # The audited path signs the real tree once; reuse its manifest as the
        # checked-in reference, then tamper a source hash.
        import shutil
        shutil.copyfile(good["manifest"], chk.MANIFEST)
        text = chk.MANIFEST.read_text()
        src_line = [ln for ln in text.splitlines()
                    if ln.endswith("  device.c")][0]
        tampered = text.replace(src_line, "f" * 64 + "  device.c")
        chk.MANIFEST.write_text(tampered)
        bad = self._sign(self.root / "o9", chain)
        self.assertFalse(bad["ok"])
        self.assertTrue(any("differ from checked-in" in f
                            for f in bad["failures"]))

    def test_tampered_patch_bytes_reject_audited_compose(self):
        chain = (self.a, self.b)
        old_manifest = chk.MANIFEST
        old_audited = chk.AUDITED_CHAINS
        self.addCleanup(setattr, chk, "MANIFEST", old_manifest)
        self.addCleanup(setattr, chk, "AUDITED_CHAINS", old_audited)
        chk.AUDITED_CHAINS = (chain,)
        chk.MANIFEST = self.root / "checked-in.sha256"
        hashes = {name: "0" * 64 for name in chain}
        chk.write_manifest(chk.MANIFEST, hashes, {}, chain, base=self.base)
        report = self._sign(self.root / "o10", chain)
        self.assertFalse(report["ok"])
        self.assertTrue(any("differs from manifest" in f
                            for f in report["failures"]))


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

    def test_guardrails_compare_pinned_binary_sha(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = make_fake_repo(root)
            flash = self._image(root)
            rom = root / "rom.bin"
            rom.write_bytes(b"\x00" * 16)
            qbin = root / "qemu-system-xtensa"
            qbin.write_bytes(b"not the pinned build\n")
            report = chk.acceptance_guardrails(src, flash, rom, qbin)
            joined = " ".join(report["failures"])
            self.assertIn("pinned full-chain build", joined)
            digest = chk.file_sha256(qbin)
            self.assertIn(digest, report["provenance"]["qemu_bin_sha256"])
            old = chk.EXPECTED_BINARY_SHA256
            chk.EXPECTED_BINARY_SHA256 = digest
            self.addCleanup(setattr, chk, "EXPECTED_BINARY_SHA256", old)
            report = chk.acceptance_guardrails(src, flash, rom, qbin)
            self.assertFalse(
                [f for f in report["failures"] if "pinned full-chain" in f])

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
