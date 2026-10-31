"""Host-only guardrails for the firmware boot matrix.

No QEMU, no Docker, no guest: every test here feeds the tool synthetic bytes
and injected booters, so the classification, the verdict taxonomy, the JSON
shape and the refusal path are pinned without starting a single process.
"""
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import boot_matrix as bm

FLASH_LEN = 0x10010
FRAME_BYTES = bm.FRAME_BYTES


def make_flash(path, boot_magic=0xE9, part_magic=b"\xaa\x50",
               app_magic=0xE9, length=FLASH_LEN):
    """A file shaped like a merged ESP32-S3 image, padded to ``length``."""
    buf = bytearray(b"\x00" * length)
    buf[0] = boot_magic
    buf[0x8000:0x8002] = part_magic
    buf[0x10000] = app_magic
    Path(path).write_bytes(bytes(buf))
    return Path(path)


def make_sd(path, signature=b"\x55\xaa", length=0x200):
    buf = bytearray(b"\x00" * length)
    if length >= 0x200:
        buf[0x1FE:0x200] = signature
    Path(path).write_bytes(bytes(buf))
    return Path(path)


class MatrixCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ybx-boot-matrix-test-")
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)


class ClassificationTests(MatrixCase):
    def test_merged_flash_shape_is_flash(self):
        self.assertEqual(bm.image_kind(make_flash(self.root / "a.bin")),
                         "flash")

    def test_mbr_signature_is_sd(self):
        self.assertEqual(bm.image_kind(make_sd(self.root / "card.img")), "sd")

    def test_a_flash_shape_without_the_partition_table_is_not_flash(self):
        path = make_flash(self.root / "bare.bin", part_magic=b"\x00\x00")
        self.assertEqual(bm.image_kind(path), "unknown")

    def test_a_flash_shape_without_the_app_magic_is_not_flash(self):
        path = make_flash(self.root / "noapp.bin", app_magic=0x00)
        self.assertEqual(bm.image_kind(path), "unknown")

    def test_truncated_short_and_missing_files_are_unknown(self):
        whole = make_flash(self.root / "whole.bin")
        self.assertEqual(bm.image_kind(whole), "flash")
        # Same magic bytes, but QEMU could never map an app at 0x10000.
        truncated = self.root / "half.bin"
        truncated.write_bytes(whole.read_bytes()[:0x1000])
        self.assertEqual(bm.image_kind(truncated), "unknown")
        make_sd(self.root / "tiny.img", length=0x1FF)
        self.assertEqual(bm.image_kind(self.root / "tiny.img"), "unknown")
        self.assertEqual(bm.image_kind(self.root / "absent.bin"), "unknown")

    def test_readme_bytes_are_unknown_not_a_refused_image_type(self):
        path = self.root / "notes.txt"
        path.write_text("hand written table\n", encoding="utf-8")
        self.assertEqual(bm.image_kind(path), "unknown")


class CandidateScanTests(MatrixCase):
    def test_only_image_suffixes_are_considered_by_default(self):
        make_flash(self.root / "app.bin")
        make_sd(self.root / "card.img")
        (self.root / "README.txt").write_text("x", encoding="utf-8")
        (self.root / "SHA256SUMS.txt").write_text("x", encoding="utf-8")
        names = [p.name for p in bm.candidate_images(self.root)]
        self.assertEqual(names, ["app.bin", "card.img"])

    def test_all_files_includes_the_documents(self):
        make_flash(self.root / "app.bin")
        (self.root / "README.txt").write_text("x", encoding="utf-8")
        names = [p.name for p in
                 bm.candidate_images(self.root, include_all=True)]
        self.assertEqual(names, ["README.txt", "app.bin"])

    def test_recursive_walks_into_control_subfolders(self):
        controls = self.root / "controls"
        controls.mkdir()
        make_flash(self.root / "app.bin")
        make_flash(controls / "control.bin")
        self.assertEqual([p.name for p in bm.candidate_images(self.root)],
                         ["app.bin"])
        self.assertEqual([str(p.relative_to(self.root)) for p in
                          bm.candidate_images(self.root, recursive=True)],
                         ["app.bin", "controls/control.bin"])

    def test_directories_are_never_candidates(self):
        (self.root / "dir.bin").mkdir()
        self.assertEqual(bm.candidate_images(self.root), [])


class VerdictTests(unittest.TestCase):
    def test_four_guest_verdicts_plus_the_runner_error(self):
        cases = [
            # kind, frames, exit status, error, expected outcome
            ("flash", 3, None, None, bm.BOOTED_PAINTED),
            ("flash", 0, None, None, bm.BOOTED_NO_FRAME),
            ("flash", None, None, None, bm.BOOTED_NO_FRAME),
            ("flash", 0, -11, None, bm.QEMU_DIED),
            ("flash", None, 1, None, bm.QEMU_DIED),
            ("flash", 5, -6, None, bm.BOOTED_PAINTED),
            ("sd", 3, None, None, bm.REFUSED),
            ("unknown", None, None, None, bm.REFUSED),
            ("flash", None, None, "NameError: boom", bm.RUNNER_ERROR),
            ("sd", None, None, "docker is not available", bm.REFUSED),
        ]
        for kind, frames, status, error, expected in cases:
            with self.subTest(kind=kind, frames=frames, status=status):
                self.assertEqual(
                    bm.classify_outcome(kind, frames, status, error),
                    expected)

    def test_a_refused_image_is_never_a_runner_error(self):
        # The refusal is decided by the bytes; a broken runner cannot turn a
        # non-flash file into a statement about a guest.
        self.assertEqual(bm.classify_outcome("sd", None, None, "traceback"),
                         bm.REFUSED)

    def test_frames_must_be_a_positive_integer_to_count_as_paint(self):
        for frames in ("3", 0, -1, None, 0.0):
            with self.subTest(frames=frames):
                self.assertEqual(bm.classify_outcome("flash", frames, None),
                                 bm.BOOTED_NO_FRAME)


class RowShapeTests(unittest.TestCase):
    def observed(self, **kw):
        base = {"frames": None, "first_frame_sha256": None,
                "last_frame_sha256": None, "first_frame_seconds": None,
                "wall_seconds": None, "qemu_exit_status": None,
                "qemu_log_tail": None, "error": None}
        base.update(kw)
        return base

    def test_row_has_exactly_the_documented_keys(self):
        row = bm.merge_observation(self.observed(frames=4), "u.bin",
                                   Path("/fw/u.bin"), "flash", "d" * 64, 100)
        self.assertEqual(set(row), {
            "name", "path", "size_bytes", "sha256", "kind", "outcome",
            "frames", "first_frame_sha256", "first_frame_seconds",
            "last_frame_sha256", "reached_known_home_frame", "wall_seconds",
            "qemu_exit_status", "qemu_log_tail", "error"})
        self.assertEqual(row["outcome"], bm.BOOTED_PAINTED)
        self.assertEqual(row["path"], "/fw/u.bin")

    def test_a_refused_row_carries_no_frame_evidence(self):
        row = bm.merge_observation(
            self.observed(qemu_exit_status=None, wall_seconds=None),
            "card.img", Path("/fw/card.img"), "sd", "a" * 64, 512)
        self.assertEqual(row["outcome"], bm.REFUSED)
        for field in ("frames", "first_frame_sha256", "first_frame_seconds",
                      "last_frame_sha256", "reached_known_home_frame",
                      "wall_seconds"):
            self.assertIsNone(row[field], field)

    def test_unpainted_guests_keep_hashes_null(self):
        row = bm.merge_observation(
            self.observed(frames=0, first_frame_sha256="ff" * 32,
                          qemu_exit_status=-11, wall_seconds=2.0),
            "dead.bin", Path("/fw/dead.bin"), "flash", "b" * 64, 10)
        self.assertEqual(row["outcome"], bm.QEMU_DIED)
        self.assertEqual(row["frames"], 0)
        self.assertIsNone(row["first_frame_sha256"])
        self.assertIsNone(row["reached_known_home_frame"])

    def test_home_frame_flag_compares_the_last_frame(self):
        row = bm.merge_observation(
            self.observed(frames=9, last_frame_sha256=bm.KNOWN_HOME_FRAME_SHA256,
                          first_frame_sha256="0" * 64, wall_seconds=20.0),
            "f.bin", Path("/fw/f.bin"), "flash", "c" * 64, 10)
        self.assertTrue(row["reached_known_home_frame"])

        other = bm.merge_observation(
            self.observed(frames=9, last_frame_sha256="1" * 64,
                          first_frame_sha256="0" * 64, wall_seconds=20.0),
            "g.bin", Path("/fw/g.bin"), "flash", "c" * 64, 10)
        self.assertFalse(other["reached_known_home_frame"])

    def test_a_runner_error_is_not_reported_as_a_guest_verdict(self):
        row = bm.merge_observation(
            self.observed(error="docker is not available on this host",
                          wall_seconds=0.0),
            "r.bin", Path("/fw/r.bin"), "flash", None, None)
        self.assertEqual(row["outcome"], bm.RUNNER_ERROR)
        self.assertEqual(row["error"], "docker is not available on this host")
        self.assertIsNone(row["frames"])


class SummaryTests(unittest.TestCase):
    def rows(self):
        return [
            {"outcome": bm.BOOTED_PAINTED},
            {"outcome": bm.BOOTED_PAINTED},
            {"outcome": bm.BOOTED_NO_FRAME},
            {"outcome": bm.QEMU_DIED},
            {"outcome": bm.REFUSED},
            {"outcome": bm.RUNNER_ERROR},
        ]

    def test_counts_are_exact(self):
        self.assertEqual(bm.summarise(self.rows()), {
            "candidates": 6, "booted_and_painted": 2,
            "booted_painted_nothing": 1, "qemu_died": 1, "refused": 1,
            "runner_error": 1})

    def test_empty_matrix_has_no_candidates(self):
        summary = bm.summarise([])
        self.assertEqual(summary["candidates"], 0)
        self.assertEqual(sum(v for k, v in summary.items() if
                              k != "candidates"), 0)

    def test_provenance_marks_the_factory_reference_only(self):
        digest = "2" * 64
        rows = [{"sha256": digest}, {"sha256": "3" * 64}, {"sha256": None}]
        bm.mark_provenance(rows, {digest})
        self.assertEqual([r["is_factory_reference"] for r in rows],
                         [True, False, False])


class TableTests(unittest.TestCase):
    def paint(self, name="ui.bin", **kw):
        row = {"name": name, "kind": "flash", "outcome": bm.BOOTED_PAINTED,
               "frames": 4, "first_frame_sha256": "abcdef0123456789" * 4,
               "reached_known_home_frame": False, "wall_seconds": 14.0}
        row.update(kw)
        return row

    def refused(self):
        return {"name": "card.img", "kind": "sd", "outcome": bm.REFUSED,
                "frames": None, "first_frame_sha256": None,
                "reached_known_home_frame": None, "wall_seconds": None}

    def test_header_and_rows_are_rendered(self):
        table = bm.render_table([self.paint(), self.refused()])
        lines = table.splitlines()
        self.assertIn("IMAGE", lines[0])
        self.assertIn("VERDICT", lines[0])
        self.assertIn("HOME", lines[0])
        body = "\n".join(lines)
        self.assertIn("ui.bin", body)
        self.assertIn(bm.BOOTED_PAINTED, body)
        self.assertIn("abcdef0123456789", body)

    def test_unmeasured_cells_are_dashes_never_zero(self):
        table = bm.render_table([self.refused()])
        line = next(t for t in table.splitlines() if "card.img" in t)
        self.assertIn("sd", line)
        self.assertNotIn(" 0 ", line)
        self.assertTrue(line.rstrip().endswith("-"))

    def test_home_column_maps_the_tri_state(self):
        table = bm.render_table([
            self.paint("yes.bin", last=None,
                       reached_known_home_frame=True),
            self.paint("no.bin", reached_known_home_frame=False),
            self.refused()])
        for name, flag in (("yes.bin", "yes"), ("no.bin", "no")):
            line = next(t for t in table.splitlines() if name in t)
            self.assertTrue(line.rstrip().endswith(flag), line)

    def test_long_names_are_truncated_not_wrapped(self):
        row = self.paint("x" * 60)
        table = bm.render_table([row])
        line = table.splitlines()[2]
        self.assertEqual(len(line.split("  ")[0].rstrip()), 44)
        self.assertTrue(line.startswith("x" * 41 + "..."))

    def test_summary_line_adds_up(self):
        table = bm.render_table([self.paint(), self.refused()])
        self.assertIn("2 candidates: 1 painted, 0 painted nothing, "
                      "0 QEMU died, 1 refused, 0 runner error", table)


class ReportTests(unittest.TestCase):
    def test_report_is_json_serialisable_with_the_versioned_schema(self):
        rows = [{"name": "a.bin", "outcome": bm.BOOTED_PAINTED}]
        report = bm.build_report(rows, Path("/fw"), {"run_seconds": 120.0},
                                 {"1" * 64})
        self.assertEqual(report["schema"], "ybx-firmware-boot-matrix/1")
        self.assertEqual(report["firmware_dir"], "/fw")
        self.assertEqual(report["factory_flash_sha256"], ["1" * 64])
        self.assertEqual(report["results"], rows)
        self.assertEqual(json.loads(json.dumps(report))["summary"],
                         bm.summarise(rows))
        self.assertRegex(report["generated_at"],
                         r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\+00:00$")


class RunMatrixTests(MatrixCase):
    def populate(self):
        self.flash = make_flash(self.root / "app.bin")
        self.sd = make_sd(self.root / "card.img")
        (self.root / "README.txt").write_text("table", encoding="utf-8")

    def test_refused_images_never_start_a_process(self):
        self.populate()
        calls = []

        def boot(image, **kw):
            calls.append(Path(image))
            return {"frames": 1, "wall_seconds": 1.0}

        rows = bm.run_matrix(self.root, boot=boot)
        self.assertEqual(calls, [self.flash])
        self.assertEqual([r["outcome"] for r in rows],
                         [bm.BOOTED_PAINTED, bm.REFUSED])
        refused = rows[1]
        self.assertEqual(refused["kind"], "sd")
        for field in ("frames", "first_frame_sha256", "last_frame_sha256",
                      "reached_known_home_frame", "wall_seconds", "error"):
            self.assertIsNone(refused[field], field)

    def test_booter_receives_the_timeout_settings(self):
        self.populate()
        seen = {}

        def boot(image, **kw):
            seen.update(kw)
            return {"frames": 0, "qemu_exit_status": None, "wall_seconds": 3.0}

        rows = bm.run_matrix(self.root, boot=boot, run_seconds=7.5,
                             linger_seconds=1.5)
        self.assertEqual(seen, {"run_seconds": 7.5, "linger_seconds": 1.5})
        self.assertEqual(rows[0]["outcome"], bm.BOOTED_NO_FRAME)

    def test_recursive_scan_and_all_files_pass_through(self):
        controls = self.root / "controls"
        controls.mkdir()
        make_flash(controls / "control.bin")
        make_sd(self.root / "card.img")
        (self.root / "README.txt").write_text("x", encoding="utf-8")
        boots = []

        def boot(image, **kw):
            boots.append(Path(image).name)
            return {"frames": 2, "wall_seconds": 1.0}

        flat = bm.run_matrix(self.root, boot=boot)
        nested = bm.run_matrix(self.root, recursive=True, boot=boot)
        everything = bm.run_matrix(self.root, recursive=True,
                                   include_all=True, boot=boot)
        self.assertEqual([r["name"] for r in flat], ["card.img"])
        self.assertEqual([r["name"] for r in nested],
                         ["card.img", "control.bin"])
        self.assertEqual([r["name"] for r in everything],
                         ["README.txt", "card.img", "control.bin"])
        self.assertEqual(boots, ["control.bin"] * 2)

    def test_size_and_digest_are_taken_from_the_file(self):
        path = make_flash(self.root / "app.bin")
        rows = bm.run_matrix(self.root, boot=lambda image, **kw: {
            "frames": 1, "wall_seconds": 0.5})
        self.assertEqual(rows[0]["size_bytes"], path.stat().st_size)
        self.assertEqual(rows[0]["sha256"], bm.sha256_file(path))
        self.assertEqual(len(rows[0]["sha256"]), 64)

    def test_a_missing_folder_raises_instead_of_returning_nothing(self):
        with self.assertRaises(NotADirectoryError):
            bm.run_matrix(self.root / "nope")

    def test_log_reports_both_booting_and_refusing(self):
        self.populate()
        lines = []
        bm.run_matrix(self.root, boot=lambda image, **kw: {
            "frames": 1, "wall_seconds": 0.5}, log=lines.append)
        self.assertIn("booting app.bin", "\n".join(lines))
        self.assertIn("refusing card.img", "\n".join(lines))


class ContainerCommandTests(unittest.TestCase):
    def command(self, image="/fw/sub/app.bin", fw="/fw"):
        return bm.container_command("/tests", fw, "/work", "/qemu", image)

    def test_the_container_is_unnamed_short_lived_and_reads_only(self):
        cmd = self.command("/fw/app.bin")
        joined = " ".join(cmd)
        self.assertEqual(cmd[:5], ["docker", "run", "--rm", "--platform",
                                   "linux/arm64"])
        self.assertNotIn("--name", joined)
        self.assertNotIn("exec", cmd)
        self.assertNotIn("stop", cmd)
        self.assertNotIn("-p", cmd)
        for mount in ("-v /qemu:/qemu:ro", "-v /work:/work:ro",
                      "-v /fw:/fw:ro", "-v /tests:/tests:ro"):
            self.assertIn(mount, joined)

    def test_it_boots_from_the_in_container_paths(self):
        cmd = self.command("/fw/app.bin")
        self.assertIn("--flash", cmd)
        self.assertEqual(cmd[cmd.index("--flash") + 1], "/fw/app.bin")
        self.assertEqual(cmd[cmd.index("--rom") + 1],
                         "/work/" + bm.DEFAULT_ROM_NAME)
        self.assertEqual(cmd[cmd.index("--sd-image") + 1],
                         "/work/" + bm.DEFAULT_SD_NAME)
        self.assertEqual(cmd[cmd.index("--qemu-bin") + 1], bm.CONTAINER_QEMU)
        self.assertEqual(cmd[cmd.index("--boot-one") - 1],
                         "/tests/boot_matrix.py")

    def test_subfolder_candidates_keep_their_relative_path(self):
        cmd = self.command("/fw/controls/control.bin", fw="/fw")
        self.assertEqual(cmd[cmd.index("--flash") + 1],
                         "/fw/controls/control.bin")

    def test_an_image_outside_the_scanned_folder_falls_back_to_its_name(self):
        cmd = self.command("/elsewhere/loose.bin", fw="/fw")
        self.assertEqual(cmd[cmd.index("--flash") + 1], "/fw/loose.bin")

    def test_guest_flag_arguments_are_appended_by_the_caller(self):
        self.assertNotIn("--run-seconds", self.command("/fw/app.bin"))


class ContainerDispatchTests(unittest.TestCase):
    def payload(self, frames=3):
        return json.dumps({"frames": frames, "first_frame_sha256": "a" * 64,
                           "last_frame_sha256": "b" * 64,
                           "first_frame_seconds": 8.0, "wall_seconds": 12.0,
                           "qemu_exit_status": None, "qemu_log_tail": "",
                           "error": None})

    def call(self, stdout="", returncode=0, stderr="", error=None):
        def fake_run(cmd, **kw):
            if error is not None:
                raise error
            return subprocess.CompletedProcess(cmd, returncode, stdout, stderr)
        with patch.object(bm.subprocess, "run", fake_run):
            return bm.boot_one_via_container(Path("/fw/app.bin"),
                                             run_seconds=30.0,
                                             linger_seconds=5.0)

    def test_the_last_json_line_is_the_observation(self):
        out = "noise\n" + self.payload(4) + "\n"
        self.assertEqual(self.call(stdout=out)["frames"], 4)

    def test_docker_missing_is_a_runner_error_not_a_guest_verdict(self):
        observed = self.call(error=FileNotFoundError())
        self.assertEqual(observed["error"], "docker is not available on this host")
        self.assertIsNone(observed["frames"])

    def test_a_container_that_outlives_its_budget_is_a_runner_error(self):
        observed = self.call(error=subprocess.TimeoutExpired(["docker"], 150))
        self.assertIn("did not finish in time", observed["error"])
        self.assertEqual(observed["wall_seconds"], 150.0)

    def test_a_container_with_no_json_reports_the_exit_status_and_stderr(self):
        observed = self.call(stdout="", returncode=125, stderr="no such image")
        self.assertIn("no JSON from the boot container", observed["error"])
        self.assertIn("docker exit 125", observed["error"])
        self.assertIn("no such image", observed["error"])
        self.assertEqual(observed["qemu_exit_status"], 125)

    def test_a_guest_error_json_is_passed_through_untouched(self):
        bad = json.dumps({"frames": None, "qemu_exit_status": None,
                          "wall_seconds": 0.4,
                          "error": "NameError: name 'shutil' is not defined"})
        observed = self.call(stdout=bad + "\n")
        self.assertEqual(observed["error"],
                         "NameError: name 'shutil' is not defined")
        self.assertEqual(
            bm.classify_outcome("flash", observed["frames"],
                                observed["qemu_exit_status"],
                                observed["error"]),
            bm.RUNNER_ERROR)


class FrameHashTests(unittest.TestCase):
    def execute(self, hex_len):
        def fake(command, arguments=None):
            return {"return": "00" * hex_len}
        return fake

    def test_only_a_full_bitplane_is_hashed(self):
        raw = b"\x11" * FRAME_BYTES
        digest = bm.read_frame_sha(lambda command, arguments=None:
                                   {"return": raw.hex()})
        self.assertEqual(digest, hashlib.sha256(raw).hexdigest())

    def test_a_short_or_missing_bitplane_is_none_not_a_hash(self):
        self.assertIsNone(bm.read_frame_sha(self.execute(FRAME_BYTES - 1)))
        self.assertIsNone(bm.read_frame_sha(self.execute(0)))

    def test_a_qmp_failure_is_none_not_an_exception(self):
        def raiser(command, arguments=None):
            raise RuntimeError("QMP closed")
        self.assertIsNone(bm.read_frame_sha(raiser))


class ProcessGraceTests(unittest.TestCase):
    """The socket goes down before the process does; only the exit status decides."""

    class DeadProcess:
        returncode = 1

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            return self.returncode

        def terminate(self):
            pass

    class LiveProcess:
        def __init__(self):
            self.stopped = False

        def poll(self):
            return None

        def wait(self, timeout=None):
            if self.stopped:
                return -9
            raise subprocess.TimeoutExpired(cmd="qemu", timeout=timeout)

        def terminate(self):
            pass

        def kill(self):
            self.stopped = True

    def test_process_exited_sees_a_child_that_already_died(self):
        process = subprocess.Popen([sys.executable, "-c",
                                    "raise SystemExit(7)"])
        self.assertTrue(bm.process_exited(process, 5.0))
        self.assertEqual(process.returncode, 7)

    def test_process_exited_does_not_wait_out_the_suite_for_a_live_child(self):
        process = subprocess.Popen([sys.executable, "-c",
                                    "import time; time.sleep(30)"])
        try:
            self.assertFalse(bm.process_exited(process, 0.2))
        finally:
            process.kill()
            process.wait()

    def observe(self, process, reset):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            flash = make_flash(root / "app.bin")
            rom = root / "rom.bin"
            rom.write_bytes(b"rom")
            sd = make_sd(root / "sd.img")
            with patch.object(bm.subprocess, "Popen",
                              return_value=process), \
                 patch.object(bm, "qmp_connect_unix",
                              side_effect=reset):
                return bm.boot_local(flash, rom, sd, root / "qemu",
                                     run_seconds=1.0, linger_seconds=0.0,
                                     workdir=tmp)

    def test_reset_after_the_guest_died_is_a_guest_verdict_not_a_fault(self):
        observation = self.observe(self.DeadProcess(),
                                   ConnectionResetError(104, "reset"))
        self.assertIsNone(observation["error"])
        self.assertEqual(observation["qemu_exit_status"], 1)
        self.assertEqual(
            bm.classify_outcome("flash", observation["frames"],
                                observation["qemu_exit_status"],
                                observation["error"]), bm.QEMU_DIED)

    def test_reset_while_the_guest_is_alive_stays_a_runner_fault(self):
        observation = self.observe(self.LiveProcess(),
                                   ConnectionResetError(104, "reset"))
        self.assertTrue(observation["error"].startswith("ConnectionResetError"))
        self.assertIsNone(observation["qemu_exit_status"])
        self.assertEqual(
            bm.classify_outcome("flash", observation["frames"],
                                observation["qemu_exit_status"],
                                observation["error"]), bm.RUNNER_ERROR)


class CliTests(MatrixCase):
    def run_cli(self, *argv):
        from contextlib import redirect_stdout, redirect_stderr
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = bm.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_a_folder_of_non_flash_images_needs_no_docker(self):
        make_sd(self.root / "card.img")
        (self.root / "README.txt").write_text("x", encoding="utf-8")
        code, out, _ = self.run_cli("--dir", str(self.root))
        self.assertEqual(code, 0)
        self.assertIn("1 candidates: 0 painted", out)

    def test_json_out_is_the_machine_readable_report(self):
        make_sd(self.root / "card.img")
        target = self.root / "report.json"
        code, _, _ = self.run_cli("--dir", str(self.root),
                                  "--json-out", str(target))
        self.assertEqual(code, 0)
        report = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(report["schema"], "ybx-firmware-boot-matrix/1")
        self.assertEqual(report["summary"]["refused"], 1)
        self.assertEqual(report["results"][0]["outcome"], bm.REFUSED)
        self.assertEqual(report["options"]["run_seconds"], 120.0)

    def test_a_missing_folder_is_an_error_exit_one(self):
        code, _, err = self.run_cli("--dir", str(self.root / "nope"))
        self.assertEqual(code, 1)
        self.assertIn("No such firmware folder", err)

    def test_a_runner_error_makes_the_exit_code_two(self):
        make_flash(self.root / "app.bin")
        with patch.object(bm, "boot_one_via_container",
                          lambda image, **kw: {
                              "frames": None, "qemu_exit_status": None,
                              "wall_seconds": 0.0,
                              "error": "docker is not available on this host"}):
            code, out, _ = self.run_cli("--dir", str(self.root))
        self.assertEqual(code, 2)
        self.assertIn("1 runner error", out)

    def test_boot_one_without_its_inputs_is_refused(self):
        code, _, err = self.run_cli("--boot-one")
        self.assertEqual(code, 2)
        self.assertIn("--flash", err)

    def test_the_guest_role_prints_one_json_object(self):
        observed = {"frames": 2, "first_frame_sha256": None,
                    "last_frame_sha256": None, "first_frame_seconds": None,
                    "wall_seconds": 1.0, "qemu_exit_status": None,
                    "qemu_log_tail": None, "error": None}
        with patch.object(bm, "boot_local", lambda *a, **kw: observed):
            code, out, _ = self.run_cli("--boot-one", "--flash", "/fw/a.bin",
                                        "--rom", "/work/rom.bin",
                                        "--sd-image", "/work/sd.img",
                                        "--qemu-bin", "/qemu/qemu-system-xtensa")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["frames"], 2)

    def test_the_guest_role_exits_one_when_it_could_not_observe(self):
        with patch.object(bm, "boot_local", lambda *a, **kw: {
                "frames": None, "error": "FileNotFoundError: rom missing"}):
            code, out, _ = self.run_cli("--boot-one", "--flash", "/fw/a.bin",
                                        "--rom", "/work/rom.bin",
                                        "--sd-image", "/work/sd.img",
                                        "--qemu-bin", "/qemu/qemu-system-xtensa")
        self.assertEqual(code, 1)
        self.assertIn("rom missing", json.loads(out)["error"])


class DefaultsTests(unittest.TestCase):
    def test_defaults_point_at_the_operator_media_not_a_temporary_guess(self):
        self.assertEqual(bm.DEFAULT_FIRMWARE_DIR.name, "firmware")
        self.assertEqual(bm.DEFAULT_QA_DIR.name, "qa-20261029")
        self.assertEqual(bm.DEFAULT_CONTAINER_IMAGE, "ybx-qemu-builder:20261029")
        self.assertTrue(str(bm.DEFAULT_QEMU_ROOT).endswith(
            "qemu-0043-agent-audit-20261030"))

    def test_the_machine_args_match_the_bridge_launch(self):
        for feature in ("ybx-x4pro-i2c=on", "ybx-x4pro-gt911=on",
                        "ybx-x4pro-sd-power=on", "ybx-x4pro-spi2=on",
                        "ybx-x4pro-epd=on"):
            self.assertIn(feature, bm.MACHINE_ARGS)
        self.assertTrue(bm.MACHINE_ARGS.startswith("esp32s3"))

    def test_the_frame_size_matches_the_ssd1677_panel(self):
        self.assertEqual((bm.FRAME_WIDTH, bm.FRAME_HEIGHT), (480, 800))
        self.assertEqual(FRAME_BYTES, 48000)

    def test_no_third_party_imports(self):
        source = Path(bm.__file__).read_text(encoding="utf-8")
        for banned in ("import requests", "import numpy", "import psutil",
                       "import docker", "import pexpect"):
            self.assertNotIn(banned, source)


if __name__ == "__main__":
    unittest.main()


class QemuRootForwardingTests(unittest.TestCase):
    """The matrix must use the QEMU tree it was given, not the hard default."""

    def test_run_matrix_forwards_qemu_root_to_the_boot_stage(self):
        import tempfile
        from pathlib import Path as _Path
        import boot_matrix as bm
        calls = {}

        def fake_container(image, **kwargs):
            calls.update(kwargs)
            return {"frames": 0, "qemu_exit_status": None,
                    "wall_seconds": 0.0, "error": None}

        original = bm.boot_one_via_container
        bm.boot_one_via_container = fake_container
        try:
            with tempfile.TemporaryDirectory() as td:
                directory = _Path(td)
                data = bytearray(b"\xff" * 0x20000)
                data[0] = 0xE9
                data[0x8000:0x8002] = b"\xaa\x50"
                data[0x10000] = 0xE9
                (directory / "candidate.bin").write_bytes(bytes(data))
                bm.run_matrix(directory, qemu_root=_Path("/qemu/fixed"),
                              run_seconds=1.0, linger_seconds=0.1,
                              log=lambda _text: None)
        finally:
            bm.boot_one_via_container = original
        self.assertEqual(str(calls.get("qemu_root")), "/qemu/fixed")
