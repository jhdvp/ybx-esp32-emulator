#!/usr/bin/env python3
"""Fail-closed, read-only 0101 research QEMU source composition verifier.

Recomposes the audited X4 Pro patch chain from pinned upstream Git objects,
including the 0096..0101 RTC/Power suffix in the audited full chain.
Compares all modified/added source bytes to the known experimental QEMU tree,
never writes to that tree or the live browser. Does NOT rebuild the binary.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory


EXPERIMENTAL_SUFFIX = (
    '0096-s3-rtc-sleep-registers-explicit-wakeup.patch',
    '0097-s3-rtc-explicit-deep-wakeup-dig-reset.patch',
    '0098-gt911-ignore-short-reset-pulse.patch',
    '0098-i2c-gt911-readonly-qom-telemetry.patch',
    '0100-rtc-ext1-gpio-level-wakeup.patch',
    '0101-x4pro-power-button-gpio3-rtc3-reset-retention.patch',
)
ACCEPTED_0101_BINARY_SHA256 = (
    '7b729fba1ceefdc67a55dc9ddd6d359699e16174a52d741ea243849c35e2accc'
)


def run_checked(cmd: list[str]) -> str:
    proc = subprocess.run(cmd, text=True, capture_output=True, check=False)
    if proc.returncode:
        raise RuntimeError(f"Failed ({proc.returncode}): {cmd!r}\n"
                           f"stdout={proc.stdout[-1200:]}\nstderr={proc.stderr[-1200:]}")
    return proc.stdout


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as source:
        for part in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(part)
    return digest.hexdigest()


def changed_source_paths(root: Path) -> list[str]:
    changed = run_checked(['git', '-C', str(root), 'diff', '--name-only'])
    new = run_checked(['git', '-C', str(root), 'ls-files', '--others',
                       '--exclude-standard', '--', 'hw', 'include'])
    paths = sorted(set(changed.splitlines() + new.splitlines()))
    return [p for p in paths if p.startswith(('hw/', 'include/'))]


def verify(*, upstream: Path, research: Path, tools_dir: Path, out: Path) -> dict:
    checker = tools_dir / 'check_qemu_patch_composition.py'
    manifest = tools_dir / 'patches/composition-manifest.sha256'
    patch_dir = tools_dir / 'experiments'
    binary = research / 'build-ybx/qemu-system-xtensa'
    for required in (checker, manifest, binary, *(
        patch_dir / name for name in EXPERIMENTAL_SUFFIX)):
        if not required.is_file():
            raise FileNotFoundError(required)

    # Validate binary of the accepted experimental run, not a claim about a
    # clean-room *binary rebuild*, which requires a separate build acceptance.
    binary_sha = sha256(binary)
    if binary_sha != ACCEPTED_0101_BINARY_SHA256:
        raise RuntimeError(f"Experimental binary differs from accepted 0101: "
                           f"{binary_sha} != {ACCEPTED_0101_BINARY_SHA256}")

    with TemporaryDirectory(prefix='ybx-0101-compose-') as tmp:
        scratch = Path(tmp) / 'composed'
        run_checked([sys.executable, str(checker), 'compose', '--chain', 'full',
                     '--qemu-src', str(upstream), '--out', str(scratch)])
        if (scratch / 'composition-manifest.sha256').read_bytes() != manifest.read_bytes():
            raise RuntimeError('Accepted base patch manifest differs')
        checkout = scratch / 'qemu-compose'
        # 0105: the audited full chain now already includes RTC/GT911/Power.
        # Verify complete ordered membership; never apply the suffix twice.
        from check_qemu_patch_composition import parse_manifest
        signed_chain = parse_manifest(manifest)['chain']
        suffix = ['experiments/' + x for x in EXPERIMENTAL_SUFFIX]
        if signed_chain[-len(suffix):] != suffix:
            raise RuntimeError('Audited full chain lacks ordered Power suffix')
        patches = [{'name': name, 'sha256': sha256(patch_dir / name)}
                   for name in EXPERIMENTAL_SUFFIX]

        paths = changed_source_paths(research)
        if not paths:
            raise RuntimeError('Research QEMU has no modified source paths')
        mismatch = []
        for relative in paths:
            a, b = checkout / relative, research / relative
            if not a.is_file() or not b.is_file() or sha256(a) != sha256(b):
                mismatch.append(relative)
        # Also catch code added by the clean composition but missing from
        # the research QEMU; do not test only one direction.
        composed_paths = changed_source_paths(checkout)
        missing = sorted(set(composed_paths) - set(paths))
        mismatch.extend(missing)
        if mismatch:
            raise RuntimeError('Source composition mismatch: ' + ', '.join(sorted(set(mismatch))))

    report = {'verdict': 'PASS', 'upstream_base':
              'febae182e132e4055529be423a818225ebddaa3a',
              'audited_base_manifest_sha256': sha256(manifest),
              'experimental_patches': patches,
              'source_files_verified': len(paths),
              'source_mismatches': [],
              'accepted_experimental_binary_sha256': binary_sha,
              'binary_rebuilt_in_clean_room': False,
              'research_tree_modified': False,
              'live_browser_modified': False}
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2) + '\n')
    return report


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--upstream', type=Path, required=True,
                    help='original Git object source for pinned upstream QEMU')
    ap.add_argument('--research', type=Path, required=True,
                    help='accepted experimental QEMU source tree, read-only')
    ap.add_argument('--out', type=Path, required=True)
    args = ap.parse_args()
    data = verify(upstream=args.upstream, research=args.research,
                  tools_dir=Path(__file__).resolve().parent, out=args.out)
    print(json.dumps(data, indent=2))
