#!/usr/bin/env python3
"""Offline, fail-closed audit of the opt-in QEMU patch composition for X4 Pro.

Reproduces the verified 0042 patch chain from the bare Espressif QEMU base
commit inside a throwaway clone (the live checkout is only ever read), checks
patch apply idempotence, records source signatures, verifies factory flash
provenance hashes, and provides pre-spawn guardrails for native guest
acceptance tests. Never rebuilds QEMU, never attaches to a running emulator.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import socket
from pathlib import Path
from tempfile import TemporaryDirectory

HERE = Path(__file__).resolve().parent

BASE_COMMIT = "febae182e132e4055529be423a818225ebddaa3a"
BUILD_DIR = "build-ybx"
MANIFEST = HERE / "patches" / "composition-manifest.sha256"

X4PRO_0042_CHAIN = (
    "patches/0001-esp32c3-gdma-interrupt-layout.patch",
    "patches/0003-usb-serial-jtag-tx-ready.patch",
    "patches/0005-usb-serial-jtag-output.patch",
    "experiments/0006-s3-adc-oneshot-handshake.patch",
    "experiments/0007-c3-s3-i2c-master.patch",
    "experiments/0008-c3-adc-oneshot-handshake.patch",
    "experiments/0009-s3-optin-bm8563-cw2017.patch",
    "experiments/0010-s3-optin-gpio-output-registers.patch",
    "experiments/0011-s3-optin-gt911-power-gating.patch",
    "experiments/0012-s3-gt911-touch-injection-gpio-feedback.patch",
    "experiments/0013-x4pro-gt911-master-rail-gpio1.patch",
    "experiments/0014-x4pro-sdmmc-board-power-gate.patch",
    "experiments/0015-s3-optin-gpspi2-transaction-scaffold.patch",
    "experiments/0016-s3-x4pro-ssd1677-panel-gpio-frame.patch",
    "experiments/0017-ssd1677-guest-ram-telemetry.patch",
    "experiments/0018-ssd1677-10bit-pixel-ram-refresh-gating.patch",
    "experiments/0019-s3-gt911-guest-i2c-readback-counters.patch",
    "experiments/0023-x4pro-standalone-idle-button-pullups.patch",
    "experiments/0024-s3-systimer-readonly-comparator-counters.patch",
    "experiments/0042-s3-epd-busy-gpio-pcpu-irq.patch",
)
QUARANTINED = frozenset({
    "experiments/0020-s3-gpio-input-status-irq-matrix.patch",
    "experiments/0021-x4pro-digital-button-pullups.patch",
    "experiments/0022-s3-gpio-level-irq-deassert.patch",
})
REJECTED = frozenset({
    "experiments/0002-s3-rom-dram-experiment.patch",
    "experiments/0004-s3-rom-initializer-trial.patch",
})
UNVALIDATED = frozenset({
    "experiments/0043-s3-optin-crosscore-ipi-counters.patch",
})
MODEL_TEST_0043 = HERE / "experiments" / "0043-agent-epd-busy-model-test.c"

# 0042 must expose GPIO_PCPU_INT: ESP-IDF S3 gpio_ll_get_intr_status() reads
# 0x6000405c; patches that only expose GPIO_STATUS cause the CPU1 IRQ storm.
PCPU_INT_MARKER = "GPIO_PCPU_INT"
X4PRO_MACHINE = "ybx-x4pro-epd"
FACTORY_FLASH_SHA256 = (
    "206ed9661bd71477d1a54397c5a2cfe490179a3f59ab094f1e7421745c609523"
)
# Earlier 092c2be5 packaging, superseded; names may still say "-factory.bin".
KNOWN_SUPERSEDED_FLASH = {
    "268c62e01ada2958fe2df0122a73dc1c037d646207805a6d14a5a6d146434b25":
        "092c2be5 settings/ packaging from 29 Oct, not the audited QA flash",
}
BROWSER_SERVER_PORT = 8784

# Exact media used by the accepted 0042 guest runs, pinned by SHA-256.
FACTORY_ARTIFACTS = {
    "flash_x4pro_0042_qa": (
        "~/Projects/YBX/artifacts/ybx-esp32-system-emulation-20261029/"
        "qa-20261029/s3-flash-complete-rom-test.bin",
        FACTORY_FLASH_SHA256),
    "rom_s3_drom_complete": (
        "~/Projects/YBX/artifacts/ybx-esp32-system-emulation-20261029/"
        "qa-20261029/s3-full-rom-generator-test.bin",
        "5b950be93a30610322a3b46049e4095cdda04f33f09fa13a9a9a6167b5994bd9"),
    "rom_s3_first_corrected": (
        "~/Projects/YBX/artifacts/ybx-esp32-system-emulation-20261029/"
        "qa-20261029/esp32s3-rom-complete.bin",
        "7c3220323773980fe73d929af322bb66b06432feb959ca620c2c5429b8e15f31"),
    "sd_test_fat16_superfloppy": (
        "~/Projects/YBX/artifacts/ybx-esp32-system-emulation-20261029/"
        "qa-20261029/ybx-test-sd-fat16.img",
        "af721e7fca28c19c05099f8a0049cb40a2d5ac6e9ccde50b2e09c1575d100ed3"),
    "sd_test_mbr_pristine": (
        "~/Projects/YBX/artifacts/ybx-esp32-system-emulation-20261029/"
        "qa-20261029/ybx-test-sd-fat16-mbr-pristine.img",
        "6c4c1bf4e6102e5963944dcf26572ab695c2259270e3aa9d0d0667787d8a7468"),
}


def file_sha256(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def git(repo: Path, *args: str, timeout: int = 180) -> str:
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                          text=True, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} in {repo} exit={proc.returncode}: "
            f"{proc.stderr.strip()[:300]}")
    return proc.stdout


def patch_paths(names) -> list[Path]:
    return [HERE / name for name in names]


def chain_touched_files(names) -> set[str]:
    """Paths the chain rewrites or adds; the audit allowlist for tree status."""
    touched: set[str] = set()
    pattern = re.compile(r"^diff --git a/(\S+) b/(\S+)$", re.MULTILINE)
    for path in patch_paths(names):
        for match in pattern.finditer(path.read_text(errors="replace")):
            touched.add(match.group(2))
    return touched


def chain_added_files(names) -> set[str]:
    """New files created by the chain, incl. legacy `--- /dev/null` headers."""
    added: set[str] = set()
    new_mode = re.compile(
        r"^diff --git a/\S+ b/(\S+)\n(?:index [0-9a-f. ]+\n)?new file mode",
        re.MULTILINE)
    legacy = re.compile(r"^--- /dev/null\n\+\+\+ b/(\S+)$", re.MULTILINE)
    for path in patch_paths(names):
        text = path.read_text(errors="replace")
        added.update(new_mode.findall(text))
        added.update(legacy.findall(text))
    return added


def normalize_patch(name: str) -> str:
    """Rewrite legacy `--- /dev/null` new-file blocks into git format.

    Several experiment patches were hand-stripped and lack `new file mode`
    index lines, which `git apply` misreads as deleting /dev/null. The disk
    bytes stay authoritative; only the composed temp copy is rewritten.
    Fail-closed: a `--- /dev/null` block whose first hunk is not `@@ -0,0`
    is left untouched and the later git apply rejects it.
    """
    lines = (HERE / name).read_text().splitlines()
    out: list[str] = []
    for i, line in enumerate(lines):
        if (line == "--- /dev/null" and out
                and out[-1].startswith("diff --git ")
                and i + 1 < len(lines) and lines[i + 1].startswith("+++ b/")):
            j = len(out) - 1
            if not any(ln.startswith("new file mode") for ln in out[j:]):
                k = i + 2
                while (k < len(lines) and not lines[k].startswith("@@")
                       and (lines[k].startswith("index ")
                            or lines[k].startswith("+++ "))):
                    k += 1
                if k < len(lines) and lines[k].startswith("@@ -0,0"):
                    out.insert(j + 1, "new file mode 100644")
        out.append(line)
    return "\n".join(out) + "\n"


def parse_manifest(path: Path) -> dict:
    manifest: dict = {"patches": {}, "sources": {}, "base_commit": "",
                      "chain": []}
    section = None
    for lineno, raw in enumerate(path.read_text().splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("["):
            section = line.strip("[]")
            continue
        if section == "base":
            if line.startswith("commit="):
                manifest["base_commit"] = line.split("=", 1)[1].strip()
            continue
        if section == "chain":
            manifest["chain"].append(line)
            continue
        parts = line.split()
        if len(parts) != 2 or not re.fullmatch(r"[0-9a-f]{64}", parts[0]):
            raise ValueError(f"{path}:{lineno}: malformed manifest line")
        (manifest["patches"] if section == "patches"
         else manifest["sources"])[parts[1]] = parts[0]
    return manifest


def write_manifest(path: Path, patch_hashes: dict, source_hashes: dict,
                   chain) -> None:
    lines = ["# QEMU patch composition manifest — regenerate with:",
             "#   python3 check_qemu_patch_composition.py record",
             "# Fail-closed: any unlisted or rehashed patch file rejects audit.",
             f"[base]\ncommit={BASE_COMMIT}",
             "[chain]"]
    lines += list(chain)
    lines.append("[patches]")
    lines += [f"{digest}  {name}"
              for name, digest in sorted(patch_hashes.items())]
    lines.append("[sources]")
    lines += [f"{digest}  {name}"
              for name, digest in sorted(source_hashes.items())]
    path.write_text("\n".join(lines) + "\n")


def status_signature(repo: Path, allowed: set[str]) -> dict[str, str]:
    """Tracked modifications + chain-added untracked files, filtered."""
    out = git(repo, "status", "--porcelain=v1", "-uno")
    entries = {}
    for line in out.splitlines():
        marker, rel = line[:2], line[3:].strip()
        if rel in allowed and marker != "??":
            entries[rel] = "M"
    out = git(repo, "status", "--porcelain=v1", "-uall", "--", *sorted(allowed))
    for line in out.splitlines():
        if line.startswith("?? ") and line[3:].strip() in allowed:
            entries[line[3:].strip()] = "?"
    return entries


def normalized_patch_files(chain, workdir: Path) -> dict[str, Path]:
    """Write git-applicable copies of each patch into `workdir/normalized`."""
    norm = workdir / "normalized"
    norm.mkdir(parents=True, exist_ok=True)
    out = {}
    for name in chain:
        path = norm / Path(name).name
        path.write_text(normalize_patch(name))
        out[name] = path
    return out


def compose_chain(qemu_src: Path, workdir: Path, chain, base=BASE_COMMIT,
                  fetch_depth: int | None = None):
    """Rebuild the patched tree in `workdir` from the bare base commit."""
    repo = workdir / "qemu-compose"
    if repo.exists():
        raise RuntimeError(f"compose target must be fresh: {repo} exists")
    repo.mkdir(parents=True)
    git(repo, "init", "-q")
    remote = qemu_src.resolve()
    if not (remote / ".git").exists() and not (remote / "objects").exists():
        raise RuntimeError(f"not a Git repository: {remote}")
    git(repo, "remote", "add", "origin", str(remote))
    git(repo, "fetch", "-q", "--no-tags",
        *(["--depth", str(fetch_depth)] if fetch_depth else []),
        "origin", base, timeout=900)
    git(repo, "cat-file", "-e", f"{base}^{{commit}}")
    git(repo, "checkout", "-q", "--detach", base)
    if git(repo, "rev-parse", "HEAD").strip() != base:
        raise RuntimeError(f"base commit {base} not reproduced")
    patch_files = normalized_patch_files(chain, workdir)
    results = []
    for name in chain:
        patch = patch_files[name]
        already = subprocess.run(
            ["git", "-C", str(repo), "apply", "--reverse", "--check",
             str(patch)], capture_output=True, text=True).returncode == 0
        if already:
            results.append({"patch": name, "state": "already-applied"})
            continue
        proc = subprocess.run(
            ["git", "-C", str(repo), "apply", "--whitespace=nowarn",
             str(patch)], capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(f"{name} does not apply on this composition: "
                               f"{proc.stderr.strip()[:400]}")
        results.append({"patch": name, "state": "applied"})
    return repo, results


def verify_patch_files(manifest: dict, chain) -> list[str]:
    failures = []
    for name in chain:
        path = HERE / name
        if not path.is_file():
            failures.append(f"missing patch file: {name}")
            continue
        recorded = manifest["patches"].get(name)
        if recorded is None:
            failures.append(f"patch not in manifest: {name}")
        elif recorded != file_sha256(path):
            failures.append(f"patch SHA-256 differs from manifest: {name}")
    for name in manifest["patches"]:
        if not (HERE / name).is_file():
            failures.append(f"manifest lists absent file: {name}")
    return failures


def verify_chain_definitions(chain) -> list[str]:
    failures = []
    if tuple(chain) != X4PRO_0042_CHAIN:
        failures.append("manifest chain differs from audited X4PRO_0042_CHAIN")
    overlap = set(chain) & (QUARANTINED | REJECTED | UNVALIDATED)
    if overlap:
        failures.append(f"forbidden patches in accepted chain: {sorted(overlap)}")
    if not chain or chain[-1] != "experiments/0042-s3-epd-busy-gpio-pcpu-irq.patch":
        failures.append("chain must end with 0042 (GPIO6 PCPU_INT fix)")
    for name in chain:
        if not (HERE / name).is_file():
            failures.append(f"chain patch missing on disk: {name}")
    return failures


def check_sources(repo: Path, allowed: set[str], required: set[str]) -> list[str]:
    failures = []
    gpio = repo / "hw/gpio/esp32_gpio.c"
    if not gpio.is_file():
        return ["hw/gpio/esp32_gpio.c missing after composition"]
    if PCPU_INT_MARKER not in gpio.read_text(errors="replace"):
        failures.append(f"0042 marker {PCPU_INT_MARKER} absent in composed "
                        "hw/gpio/esp32_gpio.c")
    s3 = repo / "hw/xtensa/esp32s3.c"
    if s3.is_file() and X4PRO_MACHINE not in s3.read_text(errors="replace"):
        failures.append(f"opt-in machine property {X4PRO_MACHINE} absent in "
                        "hw/xtensa/esp32s3.c")
    missing = [rel for rel in required if not (repo / rel).is_file()]
    if missing:
        failures.append(f"chain-added sources missing: {missing}")
    return failures


def probe_port(host: str, port: int, timeout: float = 0.35) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def validate_flash(path: Path) -> tuple[str | None, dict]:
    data = path.read_bytes()
    info = {"flash_size": len(data)}
    if len(data) < 0x10001 or data[0] != 0xE9:
        return f"{path}: not an ESP image (magic at offset 0)", info
    if data[0x8000:0x8002] != b"\xaa\x50":
        return f"{path}: no partition table at 0x8000", info
    if data[0x10000] != 0xE9:
        return f"{path}: no application image at 0x10000", info
    return None, info


def acceptance_guardrails(qemu_root: Path, flash: Path, rom: Path,
                          qemu_bin: Path | None) -> dict:
    """Pre-spawn fail-closed checks for guest acceptance runs; read-only."""
    report: dict = {"failures": [], "shared_server": {}, "provenance": {}}
    failures = report["failures"]
    if not (qemu_root / ".git").exists():
        return {"failures": [f"{qemu_root} is not a Git checkout"],
                "shared_server": {}, "provenance": {}}
    head = git(qemu_root, "rev-parse", "HEAD").strip()
    if head != BASE_COMMIT:
        failures.append(f"qemu-root HEAD {head} != audited base {BASE_COMMIT}")
    allowed = chain_touched_files(X4PRO_0042_CHAIN)
    live = status_signature(qemu_root, allowed)
    report["provenance"]["live_touched"] = live
    missing = sorted(allowed - set(live))
    if missing:
        failures.append(f"chain files not modified/added in live tree: {missing}")
    gpio = qemu_root / "hw/gpio/esp32_gpio.c"
    if gpio.is_file() and PCPU_INT_MARKER not in gpio.read_text(errors="replace"):
        failures.append("live tree lacks 0042 PCPU_INT; build would stall "
                        "on s_epdRefreshDone for 30 s")
    for path, label in ((flash, "flash"), (rom, "rom")):
        if not path.is_file():
            failures.append(f"{label} missing: {path}")
            continue
        digest = file_sha256(path)
        report["provenance"][f"{label}_sha256"] = digest
        report["provenance"][f"{label}_path"] = str(path)
    if flash.is_file():
        error, extra = validate_flash(flash)
        report["provenance"].update(extra)
        if error:
            failures.append(error)
        else:
            digest = report["provenance"]["flash_sha256"]
            if digest != FACTORY_FLASH_SHA256:
                why = KNOWN_SUPERSEDED_FLASH.get(digest)
                failures.append(
                    "flash is not the audited factory build "
                    f"{FACTORY_FLASH_SHA256[:12]}…; got {digest[:12]}…"
                    + (f" ({why})" if why else ""))
    if qemu_bin is not None:
        if not qemu_bin.is_file():
            failures.append(f"qemu binary missing: {qemu_bin}")
        else:
            report["provenance"]["qemu_bin_sha256"] = file_sha256(qemu_bin)
            report["provenance"]["qemu_bin_mtime_ns"] = \
                qemu_bin.stat().st_mtime_ns
    server_live = probe_port("127.0.0.1", BROWSER_SERVER_PORT)
    proc = subprocess.run(["pgrep", "-fl", "qemu-system-xtensa"],
                          capture_output=True, text=True)
    report["shared_server"] = {
        "browser_port_open": server_live,
        "xtensa_processes_running": bool(proc.stdout.strip()),
        "note": ("the shared developer browser server owns the active guest; "
                 "any added acceptance run must use its own temporary QEMU "
                 "process and temp copies of flash/ROM/SD"),
    }
    if server_live and qemu_bin is None:
        failures.append("shared browser server is live and no --qemu-bin was "
                        "provided to bind the evidence")
    return report


def verify_artifacts(required: bool = False) -> dict:
    """Check the pinned factory media exists with the recorded SHA-256."""
    out: dict = {"artifacts": {}, "failures": []}
    for name, (raw_path, digest) in FACTORY_ARTIFACTS.items():
        path = Path(raw_path).expanduser()
        if not path.is_file():
            entry = {"state": "missing", "path": str(path), "expected": digest}
            if required:
                out["failures"].append(f"{name}: missing {path}")
        elif file_sha256(path) != digest:
            entry = {"state": "mismatch", "path": str(path), "expected": digest,
                     "actual": file_sha256(path)}
            out["failures"].append(f"{name}: SHA-256 differs from provenance")
        else:
            entry = {"state": "ok", "path": str(path)}
        out["artifacts"][name] = entry
    return out


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_record = sub.add_parser("record", help="compose offline and rewrite manifest")
    p_record.add_argument("--qemu-src", type=Path, required=True,
                          help="existing QEMU checkout used only as Git object source")
    p_record.add_argument("--bootstrap", action="store_true",
                          help="create the manifest even if none exists yet")
    p_record.add_argument("--keep", type=Path, default=None)

    p_verify = sub.add_parser("verify",
                              help="manifest + offline compose + live tree equivalence")
    p_verify.add_argument("--live-qemu-root", type=Path, default=None,
                          help="read-only comparison against a patched checkout")
    p_verify.add_argument("--skip-compose", action="store_true",
                          help="signature/definition checks only (fast, no clone)")
    p_verify.add_argument("--keep", type=Path, default=None)

    p_guard = sub.add_parser("guardrails", help="pre-spawn acceptance checks")
    p_guard.add_argument("--qemu-root", type=Path, required=True)
    p_guard.add_argument("--flash", type=Path, required=True)
    p_guard.add_argument("--rom", type=Path, required=True)
    p_guard.add_argument("--qemu-bin", type=Path, default=None)
    p_guard.add_argument("--require-artifacts", action="store_true",
                         help="fail unless every pinned factory media is present")

    sub.add_parser("sha256sums", help="print the provenance manifest block")

    p_model = sub.add_parser("model-test",
                             help="compile+run the host-only 0043 EPD busy model test")
    p_model.add_argument("--compiler", default="cc")

    args = parser.parse_args(argv)

    if args.command == "model-test":
        with TemporaryDirectory(prefix="ybx-0043-") as tmp:
            binary = Path(tmp) / "epd_busy_test"
            proc = subprocess.run(
                [args.compiler, "-O1", "-Wall", "-Wextra", "-o", str(binary),
                 str(MODEL_TEST_0043)], capture_output=True, text=True)
            if proc.returncode != 0:
                print(json.dumps({"ok": False,
                                  "stage": "compile",
                                  "stderr": proc.stderr[-2000:]}, indent=2))
                return 1
            run = subprocess.run([str(binary)], capture_output=True, text=True,
                                 timeout=30)
        ok = run.returncode == 0 and "PASS:" in run.stdout
        print(json.dumps({"ok": ok, "exit": run.returncode,
                          "stdout": run.stdout, "stderr": run.stderr[-1500:]},
                         indent=2))
        return 0 if ok else 1

    if args.command == "sha256sums":
        for name, (raw_path, digest) in FACTORY_ARTIFACTS.items():
            print(f"{digest}  {raw_path}  # {name}")
        return 0

    if args.command == "guardrails":
        report = acceptance_guardrails(args.qemu_root, args.flash, args.rom,
                                      args.qemu_bin)
        if args.require_artifacts:
            artifacts = verify_artifacts(required=True)
            report["artifacts"] = artifacts["artifacts"]
            report["failures"] += artifacts["failures"]
        report["ok"] = not report["failures"]
        print(json.dumps(report, indent=2))
        return 0 if report["ok"] else 1

    if not MANIFEST.is_file():
        if args.command == "record" and args.bootstrap:
            manifest = {"patches": {}, "sources": {}, "base_commit": "",
                        "chain": []}
        else:
            print(json.dumps({"ok": False,
                              "failures": [f"missing manifest: {MANIFEST}"]},
                             indent=2))
            return 1
    else:
        manifest = parse_manifest(MANIFEST)

    if args.command == "record":
        chain = X4PRO_0042_CHAIN
        workdir = args.keep or Path(TemporaryDirectory(
            prefix="ybx-qemu-compose-").name)
        repo, results = compose_chain(args.qemu_src, workdir, chain)
        allowed = chain_touched_files(chain)
        added = chain_added_files(chain)
        failures = check_sources(repo, allowed, added)
        if failures:
            print(json.dumps({"ok": False, "stage": "compose",
                              "failures": failures, "workdir": str(workdir)},
                             indent=2))
            return 1
        live_sig = status_signature(repo, allowed)
        source_hashes = {rel: file_sha256(repo / rel)
                         for rel in sorted(allowed) if rel in live_sig}
        patch_hashes = {name: file_sha256(HERE / name)
                        for name in sorted(set(chain) | QUARANTINED | REJECTED
                                           | UNVALIDATED
                                           | set(manifest["patches"]))
                        if (HERE / name).is_file()}
        write_manifest(MANIFEST, patch_hashes, source_hashes, chain)
        print(json.dumps({"ok": True, "recorded_sources": len(source_hashes),
                          "patches_signed": len(patch_hashes),
                          "compose": results, "workdir": str(workdir)}, indent=2))
        return 0

    # verify
    chain = X4PRO_0042_CHAIN
    failures = verify_chain_definitions(chain)
    failures += verify_patch_files(manifest, chain)
    report: dict = {"base_commit": BASE_COMMIT,
                    "manifest_base": manifest["base_commit"],
                    "chain": list(chain), "patches_signed":
                        len(manifest["patches"]), "compose": None,
                    "live": None}
    if manifest["base_commit"] != BASE_COMMIT:
        failures.append("manifest base commit differs from audited BASE_COMMIT")
    if not failures and not args.skip_compose:
        workdir = args.keep or Path(TemporaryDirectory(
            prefix="ybx-qemu-compose-").name)
        src_for_clone = (args.live_qemu_root if args.live_qemu_root
                         else Path.home() / ".local/state/ybx-emulator-upstream/qemu")
        repo, results = compose_chain(src_for_clone, workdir, chain)
        allowed = chain_touched_files(chain)
        added = chain_added_files(chain)
        compose_fail = check_sources(repo, allowed, added)
        sig = status_signature(repo, allowed)
        source_mismatch = [rel for rel, digest in manifest["sources"].items()
                           if rel not in sig or file_sha256(repo / rel) != digest]
        unrecorded = [rel for rel in sig if rel not in manifest["sources"]]
        compose_fail += [f"composed source differs from manifest: {r}"
                         for r in source_mismatch]
        compose_fail += [f"composed source not signed in manifest: {r}"
                         for r in unrecorded]
        # Composition determinism: a second independent compose must produce
        # byte-identical chain files. Linear per-patch undo is NOT guaranteed
        # (0042 intentionally rewrites 0023/0024 context), so reproducibility
        # is proven by equality of two full compositions instead.
        repo2, _ = compose_chain(src_for_clone, workdir / "repeat", chain)
        diff_files = [rel for rel in sig
                      if file_sha256(repo / rel) != file_sha256(repo2 / rel)]
        reversible = not diff_files
        if not reversible:
            compose_fail.append(
                f"second composition differs at: {diff_files}")
        report["compose"] = {"workdir": str(workdir), "patches": results,
                             "status": sig,
                             "deterministic_recompose": reversible}
        failures += compose_fail
        if args.live_qemu_root:
            live = status_signature(args.live_qemu_root, allowed)
            report["live"] = live
            if live != sig:
                only_live = {k: v for k, v in live.items() if k not in sig}
                only_composed = {k: v for k, v in sig.items() if k not in live}
                failures.append(
                    "live tree differs from reproducible composition; "
                    f"live-only={sorted(only_live)} "
                    f"composed-only={sorted(only_composed)}")
            else:
                diff_src = [rel for rel in sig
                            if file_sha256(repo / rel) !=
                            file_sha256(args.live_qemu_root / rel)]
                if diff_src:
                    failures.append(f"live file contents differ: {diff_src}")
    report["failures"] = failures
    report["ok"] = not failures
    print(json.dumps(report, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
