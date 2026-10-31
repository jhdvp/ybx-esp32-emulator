"""Host-only tests for build_reproducible_qemu.sh.

Stage-1 runs against a synthetic mini-chain (fake git repo + tiny patches)
through the script's --chain-file/--patch-root hooks, so the suite stays fast
and never needs the multi-hundred-MB QEMU object source. The real 23-patch
composition is exercised by the opt-in test below when
YBX_RUN_REAL_COMPOSE=1 (documented timings in experiments/0063; the audited
manifest already pins that composition). Stage-2 is driven with a stub
container runner via YBX_DOCKER_BIN; the real long build is the orchestrator's
job.
"""
import hashlib
import os
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "build_reproducible_qemu.sh"
QEMU_SRC = Path.home() / ".local/state/ybx-emulator-upstream/qemu"

sys.path.insert(0, str(HERE))
import check_qemu_patch_composition as chk  # noqa: E402
from test_check_qemu_patch_composition import (  # noqa: E402
    make_fake_repo, patch_text, patch_text_tail, run_git)


def run_script(args, env=None, timeout=120):
    environ = dict(os.environ)
    if env:
        environ.update(env)
    return subprocess.run(["sh", str(SCRIPT), *args], capture_output=True,
                          text=True, timeout=timeout, env=environ)


def parse_sections(path: Path) -> dict:
    sections: dict = {"base": [], "chain": [], "patches": [], "sources": []}
    current = None
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("["):
            current = line.strip("[]")
            continue
        sections[current].append(line)
    return sections


def stub_docker(tmp: Path):
    """Fake container runner: logs args, fabricates the built binary."""
    log = tmp / "docker.log"
    stub = tmp / "docker"
    stub.write_text(
        "#!/bin/sh\n"
        f"echo \"ARGS $*\" >> '{log}'\n"
        "if [ \"$1\" = run ]; then\n"
        "  for arg in \"$@\"; do\n"
        "    case \"$arg\" in *:/src) SRC=\"${arg%%:/src}\" ;;\n"
        "  esac\n"
        "  done\n"
        "  mkdir -p \"$SRC/build-ybx\"\n"
        "  printf 'stub-qemu-system-xtensa\\n' "
        "> \"$SRC/build-ybx/qemu-system-xtensa\"\n"
        "fi\n"
        "exit 0\n")
    stub.chmod(0o755)
    return stub, log


class SandboxMixin:
    """Synthetic tools dir: checker copy + three tiny patches + fake repo."""

    @classmethod
    def build_sandbox(cls):
        cls.src = make_fake_repo(cls.root)
        cls.base = run_git(cls.src, "rev-parse", "HEAD").strip()
        tools = cls.root / "tools"
        (tools / "patches").mkdir(parents=True)
        (tools / "experiments").mkdir()
        a = patch_text("line2\n", "A\n")
        b = patch_text_tail("line4\n", "D\n")
        n = ("diff --git a/extra.c b/extra.c\n--- /dev/null\n+++ b/extra.c\n"
             "@@ -0,0 +1,1 @@\n+hello\n")
        (tools / "patches" / "a.patch").write_text(a)
        (tools / "patches" / "b.patch").write_text(b)
        (tools / "experiments" / "n.patch").write_text(n)
        cls.chain_file = tools / "chain.txt"
        cls.chain_file.write_text("patches/a.patch\npatches/b.patch\n"
                                  "experiments/n.patch\n")
        cls.tools = tools


class StageOneTests(SandboxMixin, unittest.TestCase):
    """Default (no --build) path: fast, host-only, manifest-producing."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = TemporaryDirectory(prefix="ybx-script-s1-")
        cls.root = Path(cls.tmp.name)
        cls.build_sandbox()
        cls.work = cls.root / "w1"
        cls.work.mkdir()
        cls.result = cls._dry_at(cls.work)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    @classmethod
    def _dry_at(cls, work: Path):
        return run_script(["--work-dir", str(work),
                           "--qemu-src", str(cls.src),
                           "--base", cls.base,
                           "--chain-file", str(cls.chain_file),
                           "--patch-root", str(cls.tools)])

    def test_dry_run_succeeds_without_docker(self):
        self.assertEqual(self.result.returncode, 0, self.result.stderr[-800:])
        self.assertIn("dry run: no container build was invoked",
                      self.result.stdout)
        self.assertIn("orchestrator command", self.result.stdout)
        self.assertIn('"state": "applied"', self.result.stdout)

    def test_dry_run_writes_manifest(self):
        manifest = self.work / "compose" / "composition-manifest.sha256"
        self.assertTrue(manifest.is_file())
        sections = parse_sections(manifest)
        self.assertEqual(sections["base"], [f"commit={self.base}"])
        self.assertEqual(sections["chain"],
                         ["patches/a.patch", "patches/b.patch",
                          "experiments/n.patch"])
        self.assertIn("device.c", "\n".join(sections["sources"]))
        self.assertIn("extra.c", "\n".join(sections["sources"]))

    def test_dry_run_manifest_is_deterministic_across_runs(self):
        second = self.root / "d2"
        second.mkdir()
        result = self._dry_at(second)
        self.assertEqual(result.returncode, 0, result.stderr[-800:])
        self.assertEqual(
            (self.work / "compose" / "composition-manifest.sha256")
            .read_bytes(),
            (second / "compose" / "composition-manifest.sha256").read_bytes())

    def test_idempotent_reverse_check_on_composed_tree(self):
        tree = self.work / "compose" / "qemu-compose"
        norm = self.work / "compose" / "normalized"
        # reverse-order linear undo must fully unwind a conflict-free chain
        for name in ("n.patch", "b.patch", "a.patch"):
            proc = subprocess.run(
                ["git", "-C", str(tree), "apply", "--reverse", "--check",
                 str(norm / name)], capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0,
                             f"{name}: {proc.stderr}")

    def test_wrong_base_commit_fails_closed(self):
        work = self.root / "w2"
        work.mkdir()
        result = run_script(["--work-dir", str(work),
                             "--qemu-src", str(self.src),
                             "--base", "0" * 40,
                             "--chain-file", str(self.chain_file),
                             "--patch-root", str(self.tools)])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("stage 1 failed", result.stderr)

    def test_missing_patch_file_fails_closed(self):
        """Also proves --skip-manifest-compare is accepted by the script and
        forwarded to the checker: stage 1 still runs and still fails closed."""
        bad_chain = self.tools / "bad-chain.txt"
        bad_chain.write_text("patches/a.patch\npatches/absent.patch\n")
        work = self.root / "w3"
        work.mkdir()
        result = run_script(["--work-dir", str(work),
                             "--qemu-src", str(self.src),
                             "--base", self.base,
                             "--chain-file", str(bad_chain),
                             "--patch-root", str(self.tools),
                             "--skip-manifest-compare"])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("absent", result.stdout + result.stderr)
        self.assertIn('"stage": "chain"', result.stdout)

    def test_forbidden_patch_in_chain_file_fails_closed(self):
        poisoned = self.tools / "poison.txt"
        poisoned.write_text("patches/a.patch\n"
                            "experiments/0020-s3-gpio-input-status-irq-matrix"
                            ".patch\n")
        # the quarantined name must exist for the forbidden-set check to bite
        (self.tools / "experiments" /
         "0020-s3-gpio-input-status-irq-matrix.patch").write_text(
            patch_text("line2\n", "Q\n"))
        work = self.root / "w4"
        work.mkdir()
        result = run_script(["--work-dir", str(work),
                             "--qemu-src", str(self.src),
                             "--base", self.base,
                             "--chain-file", str(poisoned),
                             "--patch-root", str(self.tools)])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("forbidden", result.stdout + result.stderr)

    def test_reuse_refuses_composition_without_sentinel(self):
        """A stage-1 that died mid-way (e.g. on marker drift) leaves tree +
        manifest but no .stage1-ok; reuse must refuse it."""
        work = self.root / "w7"
        compose = work / "compose"
        (compose / "qemu-compose").mkdir(parents=True)
        (compose / "composition-manifest.sha256").write_text("[chain]\n")
        result = run_script(["--work-dir", str(work),
                             "--qemu-src", str(self.src),
                             "--base", self.base,
                             "--chain-file", str(self.chain_file),
                             "--patch-root", str(self.tools)])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("never completed stage 1", result.stderr)

    def test_missing_object_source_fails_closed(self):
        work = self.root / "w5"
        work.mkdir()
        result = run_script(["--work-dir", str(work),
                             "--qemu-src", str(self.root / "nope")])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("object source not found", result.stderr)

    def test_unknown_flag_fails(self):
        result = run_script(["--definitely-not-a-flag"])
        self.assertNotEqual(result.returncode, 0)


class ChainDefinitionTests(unittest.TestCase):
    """The script's default chain must be the audited full 23-patch chain."""

    def test_default_invocation_targets_full_chain(self):
        text = SCRIPT.read_text()
        self.assertIn("CHAIN=full", text)
        self.assertIn('"$CHECKER" compose', text)
        self.assertIn("check_qemu_patch_composition.py", text)

    def test_default_chain_needs_the_full_patch_set(self):
        """With an empty patch-root the default chain refuses to compose and
        names every required file — proving the default is the full audited
        23-patch chain, 0051 included (checked before any fetch)."""
        with TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / "empty").mkdir()
            (tmp / "w").mkdir()
            src = make_fake_repo(tmp)
            result = run_script(["--work-dir", str(tmp / "w"),
                                 "--qemu-src", str(src),
                                 "--patch-root", str(tmp / "empty")])
            self.assertNotEqual(result.returncode, 0)
            out = result.stdout + result.stderr
            self.assertIn("chain patch missing on disk", out)
            self.assertIn("experiments/0051-busy-telemetry-reset-order.patch",
                          out)
            self.assertIn("experiments/0043-s3-optin-crosscore-ipi-counters"
                          ".patch", out)


class PlanAndUsageTests(unittest.TestCase):
    def test_print_build_command_pinned_recipe(self):
        result = run_script(["--print-build-command"])
        self.assertEqual(result.returncode, 0, result.stderr)
        out = result.stdout
        self.assertIn("ybx-qemu-builder:20261029", out)
        self.assertIn("linux/arm64", out)
        self.assertIn("--enable-gcrypt --disable-nettle", out)
        self.assertIn("ninja -j3 qemu-system-xtensa", out)

    def test_help_exits_zero(self):
        result = run_script(["--help"])
        self.assertEqual(result.returncode, 0)


class StageTwoBuildTests(SandboxMixin, unittest.TestCase):
    """Container stage driven through a stub runner (never real Docker)."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = TemporaryDirectory(prefix="ybx-script-s2-")
        cls.root = Path(cls.tmp.name)
        cls.build_sandbox()
        cls.work = cls.root / "w"
        cls.work.mkdir()
        dry = run_script(["--work-dir", str(cls.work),
                          "--qemu-src", str(cls.src), "--base", cls.base,
                          "--chain-file", str(cls.chain_file),
                          "--patch-root", str(cls.tools)])
        assert dry.returncode == 0, dry.stderr[-800:]
        cls.stub, cls.log = stub_docker(cls.root)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def _build(self, extra):
        return run_script(["--work-dir", str(self.work), "--build", *extra],
                          env={"YBX_DOCKER_BIN": str(self.stub)})

    def test_build_uses_pinned_recipe_and_fails_closed_on_sha(self):
        before = self.log.read_text() if self.log.exists() else ""
        result = self._build(["--verify-binary"])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("reusing prior composition", result.stdout)
        self.assertIn("does not match the pinned chain build", result.stderr)
        logged = self.log.read_text()[len(before):]
        self.assertIn("run --rm --platform linux/arm64", logged)
        self.assertIn("ybx-qemu-builder:20261029", logged)
        self.assertIn("--enable-gcrypt --disable-nettle", logged)
        self.assertIn("ninja -j3 qemu-system-xtensa", logged)
        self.assertIn("--target-list=xtensa-softmmu,riscv32-softmmu", logged)

    def test_build_without_verify_records_binary_sha_only(self):
        result = self._build([])
        self.assertEqual(result.returncode, 0, result.stderr[-800:])
        self.assertIn("recorded only", result.stdout)
        digest = hashlib.sha256(b"stub-qemu-system-xtensa\n").hexdigest()
        self.assertIn(digest, result.stdout)
        self.assertIn("reusing prior composition", result.stdout)
        produced = self.work / "compose" / "composition-manifest.sha256"
        self.assertTrue(produced.is_file())


@unittest.skipUnless(os.environ.get("YBX_RUN_REAL_COMPOSE") == "1"
                     and QEMU_SRC.is_dir(),
                     "set YBX_RUN_REAL_COMPOSE=1 (needs the local QEMU object "
                     "source) to compose the real 23-patch chain")
class RealChainComposeTests(unittest.TestCase):
    """Opt-in: full-chain dry run against the real pinned base commit."""

    def test_real_dry_run_manifest_matches_checked_in(self):
        with TemporaryDirectory() as tmp:
            work = Path(tmp) / "w"
            work.mkdir()
            result = run_script(["--work-dir", str(work)])
            self.assertEqual(result.returncode, 0, result.stderr[-800:])
            produced = work / "compose" / "composition-manifest.sha256"
            self.assertEqual(produced.read_bytes(),
                             (HERE / "patches" /
                              "composition-manifest.sha256").read_bytes())
            sections = parse_sections(produced)
            self.assertEqual(sections["base"], [f"commit={chk.BASE_COMMIT}"])
            self.assertEqual(sections["chain"][-3:], list(chk.TELEMETRY_CHAIN))
            sources = {line.split()[1] for line in sections["sources"]}
            self.assertIn("hw/xtensa/esp32s3_intc.c", sources)
            self.assertIn("hw/display/ybx_ssd1677.c", sources)
            self.assertIn("hw/gpio/esp32_gpio.c", sources)


if __name__ == "__main__":
    unittest.main()
