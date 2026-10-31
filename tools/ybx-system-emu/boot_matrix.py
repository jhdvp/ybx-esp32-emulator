#!/usr/bin/env python3
"""Boot every candidate firmware image in its own QEMU and report the truth.

The hand-written table in the firmware folder README is replaced by a
measurement: this tool classifies each candidate by its bytes, boots every
real ESP flash image in a SEPARATE, throwaway QEMU instance, and records the
EPD frame counter, the SHA-256 of the first painted frame, the wall time and
the QEMU exit status. Anything that cannot be measured is reported as such,
never as a fabricated result.

It never touches a running emulator: no ``docker exec``, no ``docker stop``,
no HTTP call to the bridge, no shared workdir. Each boot is a
``docker run --rm`` container (no name, no port) that mounts the QEMU build,
the QA media and the candidate folder READ-ONLY, copies the images into its
own tmpfs and boots from the copies - so a guest that writes to its flash or
SD card still cannot corrupt the operator's media.

Two roles, one file:

* host role (default)  - scan, classify, dispatch, aggregate, print:
    python3 boot_matrix.py                       # default firmware folder
    python3 boot_matrix.py --dir PATH --json-out out.json
* guest role (inside the container, driven by the host role) - boot one image:
    python3 boot_matrix.py --boot-one --flash /fw/image.bin ...

Exit codes: 0 when every candidate produced a verdict, 1 when the folder is
unusable, 2 when at least one candidate ended in a runner error (the verdict
for those images is simply unknown).
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone

# --- outcomes -------------------------------------------------------------
BOOTED_PAINTED = "booted and painted"
BOOTED_NO_FRAME = "booted, painted nothing"
QEMU_DIED = "QEMU died"
REFUSED = "refused before boot"
RUNNER_ERROR = "runner error (not a guest verdict)"

OUTCOMES = (BOOTED_PAINTED, BOOTED_NO_FRAME, QEMU_DIED, REFUSED, RUNNER_ERROR)

# --- defaults taken from the live bridge and its start script -------------
DEFAULT_FIRMWARE_DIR = Path(
    "/Users/yaraslauhancharou/Projects/YBX/artifacts/"
    "ybx-esp32-system-emulation-20261029/firmware")
DEFAULT_QA_DIR = DEFAULT_FIRMWARE_DIR.parent / "qa-20261029"
DEFAULT_QEMU_ROOT = Path(
    "/Users/yaraslauhancharou/.local/state/ybx-emulator-upstream/"
    "qemu-0043-agent-audit-20261030")
DEFAULT_CONTAINER_IMAGE = "ybx-qemu-builder:20261029"
DEFAULT_ROM_NAME = "s3-full-rom-generator-test.bin"
DEFAULT_SD_NAME = "ybx-test-sd-fat16-mbr-pristine.img"
CONTAINER_QEMU = "/qemu/build-ybx/qemu-system-xtensa"
CONTAINER_TESTS = "/tests"
CONTAINER_FW = "/fw"
CONTAINER_WORK = "/work"

# The SSD1677 panel is 480x800 monochrome, one bit per pixel; the frame SHA
# covers the raw landscape bitplane exactly as the bridge computes it
# (serve_x4pro_browser.py:546-552).
FRAME_WIDTH, FRAME_HEIGHT = 480, 800
FRAME_BYTES = FRAME_WIDTH * FRAME_HEIGHT // 8

# The Home-screen frame the operator accepted on 2026-10-30 (see
# experiments/0066 and the live bridge state). Recorded as a comparison only:
# a different frame is a fact, not a failure.
KNOWN_HOME_FRAME_SHA256 = (
    "e79c2ad5f2b60b16fe3f20625742d5f1343f5785552d1b32c11f33c42a874672")

MACHINE_ARGS = ("esp32s3,ybx-x4pro-i2c=on,ybx-x4pro-gt911=on,"
                "ybx-x4pro-sd-power=on,ybx-x4pro-spi2=on,ybx-x4pro-epd=on")
ROM_FILENAME_IN_WORKDIR = "esp32s3_rev0_rom.bin"
QOM_PATH = "/machine/soc"
POLL_SECONDS = 0.5
LOG_TAIL_CHARS = 400
QEMU_EXIT_GRACE_SECONDS = 5.0


# --- classification -------------------------------------------------------
def image_kind(path):
    """Classify a candidate by its own bytes, mirroring the bridge rules.

    Same three rules as ``serve_x4pro_browser.image_kind`` (lines 117-137):
    ESP flash image = bootloader magic 0xE9 at 0x0, partition table 0xAA50 at
    0x8000, application magic 0xE9 at 0x10000; FAT/SD image = MBR signature
    0x55AA at 0x1FE; anything else (including unreadable files) is "unknown"
    and is never booted.
    """
    try:
        with open(path, "rb") as handle:
            head = handle.read(0x10010)
    except OSError:
        return "unknown"
    if len(head) < 0x200:
        return "unknown"
    if (len(head) >= 0x10010 and head[0] == 0xE9
            and head[0x8000:0x8002] == b"\xaa\x50" and head[0x10000] == 0xE9):
        return "flash"
    if head[0x1FE:0x200] == b"\x55\xaa":
        return "sd"
    return "unknown"


def sha256_file(path):
    """Streaming SHA-256, or None when the file cannot be read."""
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()


def candidate_images(directory, recursive=False, include_all=False):
    """Candidate image files in ``directory``, sorted by name.

    By default only ``.bin`` and ``.img`` files are considered, so the folder
    README and checksum list do not show up as refused images.
    """
    directory = Path(directory)
    paths = (directory.rglob("*") if recursive else directory.glob("*"))
    found = []
    for path in sorted(paths, key=lambda p: str(p)):
        if not path.is_file():
            continue
        if not include_all and path.suffix.lower() not in (".bin", ".img"):
            continue
        found.append(path)
    return found


# --- the verdict ----------------------------------------------------------
def classify_outcome(kind, frames, exit_status, error=None):
    """Map one observation onto an honest outcome label.

    ``frames`` is the EPD frame counter last read from QOM (None when QEMU
    never answered), ``exit_status`` is the QEMU wait status as reported by
    the OS (negative for signals, None when the process was still alive and
    the runner terminated it deliberately), and ``error`` carries a runner
    failure (docker missing, no JSON, crashed driver) - which is explicitly
    NOT a statement about the guest.
    """
    if kind != "flash":
        return REFUSED
    if error:
        return RUNNER_ERROR
    if isinstance(frames, int) and frames > 0:
        return BOOTED_PAINTED
    return BOOTED_NO_FRAME if exit_status is None else QEMU_DIED


def merge_observation(observed, name, path, kind, digest, size):
    """Build the JSON row for one candidate from a guest observation."""
    frames = observed.get("frames")
    exit_status = observed.get("qemu_exit_status")
    error = observed.get("error")
    outcome = classify_outcome(kind, frames, exit_status, error)
    painted = outcome == BOOTED_PAINTED
    return {
        "name": name,
        "path": str(path),
        "size_bytes": size,
        "sha256": digest,
        "kind": kind,
        "outcome": outcome,
        # A refused or failed image has no frame evidence at all; the fields
        # stay null instead of inventing a zero or a hash.
        "frames": frames if outcome in (BOOTED_PAINTED, BOOTED_NO_FRAME,
                                         QEMU_DIED) else None,
        "first_frame_sha256": (observed.get("first_frame_sha256")
                               if painted else None),
        "first_frame_seconds": (observed.get("first_frame_seconds")
                                 if painted else None),
        "last_frame_sha256": (observed.get("last_frame_sha256")
                              if painted else None),
        "reached_known_home_frame": (
            observed.get("last_frame_sha256") == KNOWN_HOME_FRAME_SHA256
            if painted else None),
        "wall_seconds": observed.get("wall_seconds"),
        "qemu_exit_status": exit_status,
        "qemu_log_tail": observed.get("qemu_log_tail"),
        "error": error,
    }


def summarise(rows):
    counts = {outcome: 0 for outcome in OUTCOMES}
    for row in rows:
        counts[row["outcome"]] = counts.get(row["outcome"], 0) + 1
    return {"candidates": len(rows),
            "booted_and_painted": counts[BOOTED_PAINTED],
            "booted_painted_nothing": counts[BOOTED_NO_FRAME],
            "qemu_died": counts[QEMU_DIED],
            "refused": counts[REFUSED],
            "runner_error": counts[RUNNER_ERROR]}


def factory_sha256s(qa_dir):
    """SHA-256 of the factory flash image, when the QA media is readable."""
    path = Path(qa_dir) / "s3-flash-complete-rom-test.bin"
    digest = sha256_file(path) if path.is_file() else None
    return {digest} if digest else set()


def mark_provenance(rows, factory_digests):
    for row in rows:
        row["is_factory_reference"] = bool(
            row["sha256"] and row["sha256"] in factory_digests)
    return rows


# --- aggregation ----------------------------------------------------------
def run_matrix(directory, *, recursive=False, include_all=False, boot=None,
               run_seconds=120.0, linger_seconds=5.0, log=print):
    """Classify every candidate and boot each flash image; return the rows.

    ``boot`` is injected by the callers (and by the host tests) so the scan
    and the verdict logic are testable without QEMU. It receives the image
    path and must return the guest observation dict.
    """
    directory = Path(directory)
    if not directory.is_dir():
        raise NotADirectoryError(f"not a folder: {directory}")
    boot = boot or (lambda image, **kw: boot_one_via_container(
        image, root=directory, **kw))
    rows = []
    for path in candidate_images(directory, recursive, include_all):
        kind = image_kind(path)
        size = path.stat().st_size if path.is_file() else None
        digest = sha256_file(path)
        if kind != "flash":
            # The refusal path is decided by the bytes alone: no process is
            # ever started for an image that is not a flash image.
            observed = {"frames": None, "qemu_exit_status": None,
                        "wall_seconds": None, "error": None}
            log(f"  refusing {path.name} (kind={kind})")
        else:
            log(f"  booting {path.name} ...")
            observed = boot(path, run_seconds=run_seconds,
                            linger_seconds=linger_seconds)
        rows.append(merge_observation(observed, path.name, path, kind,
                                      digest, size))
    return rows


def build_report(rows, directory, options, factory_digests):
    return {
        "schema": "ybx-firmware-boot-matrix/1",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "firmware_dir": str(directory),
        "options": options,
        "factory_flash_sha256": sorted(factory_digests),
        "summary": summarise(rows),
        "results": rows,
    }


# --- rendering ------------------------------------------------------------
def render_table(rows):
    """A compact human table. Unmeasured cells are '-' or 'n/a', never 0."""
    header = ("IMAGE", "KIND", "VERDICT", "FRAMES", "FIRST FRAME SHA256",
              "SEC", "HOME")
    fmt = f"{{:<{44}}}  {{:<7}}  {{:<24}}  {{:>6}}  {{:<16}}  {{:>6}}  {{:>4}}"
    lines = [fmt.format(*header),
             fmt.format("-" * 44, "-" * 7, "-" * 24, "-" * 6, "-" * 16,
                        "-" * 6, "-" * 4)]
    for row in rows:
        name = row["name"]
        if len(name) > 44:
            name = name[:41] + "..."
        sha = row["first_frame_sha256"] or "-"
        if sha != "-":
            sha = sha[:16]
        frames = row["frames"] if isinstance(row["frames"], int) else "-"
        seconds = (f"{row['wall_seconds']:.0f}"
                   if isinstance(row["wall_seconds"], (int, float)) else "-")
        home = {True: "yes", False: "no", None: "-"}[
            row.get("reached_known_home_frame")]
        lines.append(fmt.format(name, row["kind"], row["outcome"], frames,
                                sha, seconds, home))
    counts = summarise(rows)
    lines.append("")
    lines.append(
        f"{counts['candidates']} candidates: "
        f"{counts['booted_and_painted']} painted, "
        f"{counts['booted_painted_nothing']} painted nothing, "
        f"{counts['qemu_died']} QEMU died, "
        f"{counts['refused']} refused, "
        f"{counts['runner_error']} runner error")
    return "\n".join(lines)


# --- guest role: boot ONE image in this (throwaway) QEMU ------------------
def qom_frames(execute, prop):
    result = execute("qom-get", {"path": QOM_PATH, "property": prop})
    if "return" not in result:
        raise RuntimeError(str(result.get("error", result)))
    return result["return"]


def read_frame_sha(execute):
    """SHA-256 of the visible bitplane, or None when QEMU cannot supply it.

    The hash covers the raw 480x800 landscape bytes, the same input the bridge
    hashes (serve_x4pro_browser.py:546-552), so the two are comparable.
    """
    try:
        raw = bytes.fromhex(qom_frames(execute, "ybx-epd-visible-hex"))
    except (OSError, RuntimeError, ValueError):
        return None
    if len(raw) != FRAME_BYTES:
        return None
    return hashlib.sha256(raw).hexdigest()


def process_exited(process, grace_seconds=QEMU_EXIT_GRACE_SECONDS):
    """True if the child is gone, waiting briefly if it still looks alive.

    A guest that dies on its own closes its QMP socket a moment before the
    process disappears, so a socket error alone does not say which happened.
    A child that is still running after the grace period is the runner's
    problem, not the guest's verdict.
    """
    if process.poll() is not None:
        return True
    try:
        process.wait(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        return False
    return True


def boot_local(flash, rom, sd_image, qemu_bin, run_seconds, linger_seconds,
               workdir=None):
    """Boot one flash image here and observe it; returns the raw observation.

    The image files are copied into a private folder first: the guest may
    write to its flash or SD card, and the operator's media stays untouched.
    """
    observation = {"frames": None, "first_frame_sha256": None,
                   "last_frame_sha256": None,
                   "first_frame_seconds": None, "wall_seconds": None,
                   "qemu_exit_status": None, "qemu_log_tail": None,
                   "error": None}
    temp = tempfile.TemporaryDirectory(prefix="ybx-boot-matrix-", dir=workdir)
    root = Path(temp.name)
    process = None
    sock = None
    log_handle = None
    started = time.monotonic()
    try:
        (root / "rom").mkdir()
        shutil.copyfile(rom, root / "rom" / ROM_FILENAME_IN_WORKDIR)
        shutil.copyfile(flash, root / "flash.bin")
        shutil.copyfile(sd_image, root / "sd.img")
        sock_path = root / "qmp.sock"
        log_handle = (root / "qemu.log").open("w+")
        process = subprocess.Popen([
            str(qemu_bin), "-machine", MACHINE_ARGS,
            "-m", "8M", "-global", "ssi_psram.is_octal=on",
            "-nographic", "-monitor", "none",
            "-serial", "null", "-serial", "null",
            "-serial", "null", "-serial", "null",
            "-qmp", f"unix:{sock_path},server=on,wait=off",
            "-L", str(root / "rom"),
            "-drive", f"file={root / 'flash.bin'},if=mtd,format=raw",
            "-drive", f"file={root / 'sd.img'},if=sd,format=raw",
        ], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=log_handle)
        try:
            sock, reader, execute = qmp_connect_unix(sock_path)
        except OSError as exc:
            if not process_exited(process, QEMU_EXIT_GRACE_SECONDS):
                raise exc  # QEMU is alive and untalkable: that is a runner fault
            # The guest closes its QMP socket a moment before the process
            # disappears. Waiting for that moment keeps the verdict with the
            # guest (the exit status below) instead of blaming the runner.
            execute = None
        deadline = started + run_seconds
        first_seen = None
        while execute is not None and time.monotonic() < deadline:
            if process.poll() is not None:
                break
            try:
                frames = qom_frames(execute, "ybx-epd-frames")
            except (OSError, RuntimeError, ValueError):
                break  # QEMU vanished mid-poll; the exit status tells how
            observation["frames"] = frames
            if frames:
                try:
                    sha = read_frame_sha(execute)
                except (OSError, RuntimeError, ValueError):
                    break  # QEMU vanished with a frame already counted
                if sha:
                    if observation["first_frame_sha256"] is None:
                        first_seen = time.monotonic()
                        observation["first_frame_seconds"] = round(
                            first_seen - started, 1)
                        observation["first_frame_sha256"] = sha
                    observation["last_frame_sha256"] = sha
            if first_seen and time.monotonic() - first_seen >= linger_seconds:
                break
            time.sleep(POLL_SECONDS)
        observation["wall_seconds"] = round(time.monotonic() - started, 1)
        if process.poll() is not None:
            observation["qemu_exit_status"] = process.returncode
        else:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
    except Exception as exc:  # reported verbatim; never hidden as a verdict
        observation["error"] = f"{type(exc).__name__}: {exc}"
        observation["wall_seconds"] = round(time.monotonic() - started, 1)
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait()
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        if log_handle is not None:
            log_handle.seek(0)
            tail = log_handle.read()[-LOG_TAIL_CHARS:]
            log_handle.close()
            observation["qemu_log_tail"] = tail or None
        temp.cleanup()
    return observation


def qmp_connect_unix(sock_path, deadline_seconds=20.0):
    """Connect to the container-local QMP unix socket and say hello."""
    deadline = time.monotonic() + deadline_seconds
    while True:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            client.connect(str(sock_path))
            break
        except OSError:
            client.close()
            if time.monotonic() > deadline:
                raise
            time.sleep(0.1)
    reader = client.makefile("r", encoding="utf-8", newline="\n")
    greeting = json.loads(reader.readline())
    if "QMP" not in greeting:
        raise RuntimeError(f"not a QMP greeting: {greeting}")

    def execute(command, arguments=None):
        payload = {"execute": command}
        if arguments is not None:
            payload["arguments"] = arguments
        client.sendall((json.dumps(payload) + "\n").encode())
        while True:
            line = reader.readline()
            if not line:
                raise RuntimeError("QMP closed")
            data = json.loads(line)
            if "event" in data:
                continue
            return data
    if "return" not in execute("qmp_capabilities"):
        raise RuntimeError("QMP capabilities negotiation failed")
    return client, reader, execute


# --- host role: dispatch ONE boot into its own container ------------------
def container_command(tools_dir, fw_dir, qa_dir, qemu_root, image,
                      container_image=DEFAULT_CONTAINER_IMAGE):
    """docker run for exactly one throwaway guest; all mounts are :ro."""
    guest_image = Path(image)
    try:
        rel = guest_image.relative_to(Path(fw_dir))
    except ValueError:
        rel = Path(guest_image.name)
    return [
        "docker", "run", "--rm", "--platform", "linux/arm64",
        "-v", f"{qemu_root}:/qemu:ro",
        "-v", f"{qa_dir}:/work:ro",
        "-v", f"{fw_dir}:/fw:ro",
        "-v", f"{tools_dir}:/tests:ro",
        "-w", "/tmp", container_image,
        "python3", f"{CONTAINER_TESTS}/{Path(__file__).name}", "--boot-one",
        "--flash", str(PurePosixPath(CONTAINER_FW, *rel.parts)),
        "--rom", f"{CONTAINER_WORK}/{DEFAULT_ROM_NAME}",
        "--sd-image", f"{CONTAINER_WORK}/{DEFAULT_SD_NAME}",
        "--qemu-bin", CONTAINER_QEMU,
    ]


def boot_one_via_container(image, *, run_seconds, linger_seconds, root=None,
                           tools_dir=None, fw_dir=None, qa_dir=None,
                           qemu_root=None,
                           container_image=DEFAULT_CONTAINER_IMAGE):
    """Boot one candidate in a dedicated container and read back its JSON."""
    image = Path(image)
    tools_dir = Path(tools_dir or Path(__file__).resolve().parent)
    fw_dir = Path(fw_dir or root or image.parent)
    cmd = container_command(tools_dir, fw_dir, qa_dir or DEFAULT_QA_DIR,
                            qemu_root or DEFAULT_QEMU_ROOT, image,
                            container_image) + [
        "--run-seconds", str(run_seconds),
        "--linger-seconds", str(linger_seconds)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              errors="replace",
                              timeout=run_seconds + 120,
                              stdin=subprocess.DEVNULL)
    except FileNotFoundError:
        return {"frames": None, "qemu_exit_status": None, "wall_seconds": 0.0,
                "error": "docker is not available on this host"}
    except subprocess.TimeoutExpired:
        return {"frames": None, "qemu_exit_status": None,
                "wall_seconds": run_seconds + 120,
                "error": "the boot container did not finish in time"}
    payload = None
    for line in reversed(proc.stdout.strip().splitlines()):
        try:
            candidate = json.loads(line)
        except ValueError:
            continue
        if isinstance(candidate, dict) and "frames" in candidate:
            payload = candidate
            break
    if payload is None:
        return {"frames": None, "qemu_exit_status": proc.returncode,
                "wall_seconds": None,
                "error": ("no JSON from the boot container "
                          f"(docker exit {proc.returncode}): "
                          f"{proc.stderr.strip()[-LOG_TAIL_CHARS:]}")}
    return payload


# --- CLI ------------------------------------------------------------------
def parser():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--dir", dest="firmware_dir", type=Path,
                   default=DEFAULT_FIRMWARE_DIR,
                   help="folder of candidate images (default: the firmware folder)")
    p.add_argument("--recursive", action="store_true",
                   help="also walk subfolders such as controls/")
    p.add_argument("--all-files", action="store_true",
                   help="classify every file, not only .bin and .img")
    p.add_argument("--run-seconds", type=float, default=120.0,
                   help="watch each guest for this long (default 120)")
    p.add_argument("--linger-seconds", type=float, default=5.0,
                   help="keep watching this long after the first frame")
    p.add_argument("--json-out", type=Path,
                   help="write the machine-readable report here")
    p.add_argument("--container-image", default=DEFAULT_CONTAINER_IMAGE)
    p.add_argument("--qa-dir", type=Path, default=DEFAULT_QA_DIR)
    p.add_argument("--qemu-root", type=Path, default=DEFAULT_QEMU_ROOT)
    p.add_argument("--boot-one", action="store_true",
                   help=argparse.SUPPRESS)
    p.add_argument("--flash", type=Path, help=argparse.SUPPRESS)
    p.add_argument("--rom", type=Path, help=argparse.SUPPRESS)
    p.add_argument("--sd-image", type=Path, help=argparse.SUPPRESS)
    p.add_argument("--qemu-bin", type=Path, help=argparse.SUPPRESS)
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    if args.boot_one:
        missing = [f"--{name.replace('_', '-')}"
                   for name in ("flash", "rom", "sd_image", "qemu_bin")
                   if getattr(args, name) is None]
        if missing:
            print(f"--boot-one needs {', '.join(missing)}", file=sys.stderr)
            return 2
        observation = boot_local(args.flash, args.rom, args.sd_image,
                                 args.qemu_bin, args.run_seconds,
                                 args.linger_seconds)
        print(json.dumps(observation))
        return 0 if observation.get("error") is None else 1
    if not args.firmware_dir.is_dir():
        print(f"No such firmware folder: {args.firmware_dir}", file=sys.stderr)
        return 1
    options = {"recursive": args.recursive, "all_files": args.all_files,
               "run_seconds": args.run_seconds,
               "linger_seconds": args.linger_seconds,
               "container_image": args.container_image}
    rows = run_matrix(args.firmware_dir, recursive=args.recursive,
                      include_all=args.all_files,
                      run_seconds=args.run_seconds,
                      linger_seconds=args.linger_seconds,
                      log=lambda text: print(text, file=sys.stderr, flush=True))
    factory_digests = factory_sha256s(args.qa_dir)
    mark_provenance(rows, factory_digests)
    report = build_report(rows, args.firmware_dir, options, factory_digests)
    if args.json_out:
        args.json_out.write_text(json.dumps(report, indent=2) + "\n",
                                 encoding="utf-8")
    print(render_table(rows))
    return 2 if report["summary"]["runner_error"] else 0


if __name__ == "__main__":
    sys.exit(main())
