# 0072 — batch 4: clean-room QEMU build executed end to end (compose → container build → binary SHA → factory-flash boot)

Date: 2026-10-31. Branch: `agents/ybx-emu-b4-repro-build-e2e-20261031`
(worktree `repro-build-e2e`, base `720496e`). Mission: the reproducible-build
pipeline was proven in *composition* (batch-3, experiments/0063) but the
container *build* stage had never run end to end. This lane executed it.

## Scope and safety

- The shared QEMU build directory, the patched checkout
  `~/.local/state/ybx-emulator-upstream/qemu-0043-agent-audit-20261030` and the
  live container `ybx-x4pro-browser-8784` were never written to, restarted, or
  sent any HTTP request. The shared object source
  `~/.local/state/ybx-emulator-upstream/qemu` was not even read through Git:
  a `cp -a` copy in the lane's own scratch dir was used as `--qemu-src`.
- No flash/ROM/SD media was modified; the boot probe works on throwaway
  container-internal `/tmp` copies (probe_x4pro_spi2_guest.py:30-37).
- Every claim below is either backed by a recorded command output in
  `0072-build-e2e-buildlog.txt` (committed next to this report; the repo's
  `tools/ybx-system-emu/.gitignore` excludes `*.log`, so the artifact ships
  with a `.txt` extension) or explicitly marked
  NOT MEASURED.

## Files delivered

- `tools/ybx-system-emu/verify_reproducible_build.sh` (new, 285 lines, POSIX sh): one
  orchestrator command for the repeatable check — `audit` (fast, host-only:
  chain definitions + patch signatures + manifest via
  `check_qemu_patch_composition.py verify --skip-compose`, script lines
  132-141), `binary` (SHA-256 compare against the pinned accepted build,
  lines 170-197), `compose`/`build` (opt-in, delegate to
  `build_reproducible_qemu.sh`, lines 142-169; `--build` always passes
  `--build --verify-binary`, line 156), and `boot` (opt-in QEMU-level probe,
  lines 198-284: fail-closed media pinning against the checker constants
  `FACTORY_FLASH_SHA256` / `FACTORY_ARTIFACTS`, then a throwaway
  `docker run … python3 /tests/probe_x4pro_spi2_guest.py … --epd --seconds 33`,
  then compares `epd_ram_state.visible-frame-sha256` against the accepted
  Home frame `e79c2ad5…` pinned at line 31 — a mismatch is reported with both
  digests, never smoothed over).
- `tools/ybx-system-emu/test_verify_reproducible_build.py` (new, 12 tests):
  help/plan, fast-audit skip lines, missing binary, wrong binary (both SHAs
  named), boot without binary, unpinned media rejected **before** docker is
  invoked, probe exit≠0 fail-closed, zero-frame fail-closed, and a fixture
  probe report with a different panel hash proving the script prints
  `stage=boot state=home-frame-differs` and exits non-zero. Container runner
  stubbed through `YBX_DOCKER_BIN` (same hook convention as
  build_reproducible_qemu.sh:26). No test compiles, runs real Docker or boots
  a guest.
- `tools/ybx-system-emu/experiments/0072-build-e2e-buildlog.txt`: full build
  log artifact of the clean-room run (2240 lines, 148 KB; byte-identical
  copy of the run's `$SCRATCH/build-e2e.log`).

## Deliverable 1 — the executed end-to-end build

Commands actually run (from this lane's worktree):

```sh
SCRATCH=~/.local/state/xteink-agent-workspaces/ybx-emu-batch4-20261031/scratch-repro-build-0072
cp -a ~/.local/state/ybx-emulator-upstream/qemu "$SCRATCH/qemu-objsrc"   # 31.3 s, shared tree untouched
mkdir -p "$SCRATCH/run1"
sh tools/ybx-system-emu/build_reproducible_qemu.sh \
  --build --verify-binary --jobs 2 \
  --qemu-src "$SCRATCH/qemu-objsrc" --work-dir "$SCRATCH/run1" \
  > "$SCRATCH/build-e2e.log" 2>&1
```

Stage results (verbatim anchors in `0072-build-e2e-buildlog.txt`):

- Stage 1 compose: `"ok": true`, all 23 chain patches report
  `"state": "applied"`, base `febae182e132e4055529be423a818225ebddaa3a`
  (check_qemu_patch_composition.py:26). The composed manifest
  `$SCRATCH/run1/compose/composition-manifest.sha256` is **byte-identical**
  to the checked-in `patches/composition-manifest.sha256` (both
  SHA-256 `47e13c748f22c5b0244fcd7743886ccbdb72884fc70846871430a793090d0fe5`,
  `cmp` exit 0) — so the *source* of the clean-room tree is identical to the
  audited source, not merely equivalent.
- Stage 2 build: container `ybx-qemu-builder:20261029`
  (linux/arm64; Docker 28.3.2 on macOS 26.0), configure flags per
  QEMU_BRINGUP.md (build_reproducible_qemu.sh:24), `ninja -j2
  qemu-system-xtensa`. Wall time **35 min 06 s** end to end
  (`E2E_BUILD_START=2026-10-31T10:16:38Z` → `E2E_BUILD_END=…T10:51:44Z`,
  log lines 1 and 2240; stage-1 compose, configure and the full
  1543-target ninja graph are inside that window — per-stage seconds were
  not separately timed). `ninja` finished with
  `[1543/1543] Linking target qemu-system-xtensa` (log line 2231).
  Configure toolchain recorded from the container:
  `gcc (Ubuntu 13.3.0-6ubuntu2~24.04.1) 13.3.0`, `GNU ld 2.42`,
  `ninja 1.11.1`, `Python 3.12.3`, `uname -m = aarch64`.
- Binary verify: `--verify-binary` ran
  `check_qemu_patch_composition.py binary-sha` (lines 744-755), which
  compares against `EXPECTED_BINARY_SHA256` (lines 87-88). Log lines
  2232-2240: binary path, JSON `"ok": true` with
  `actual == expected == f07744d9…`, `E2E_BUILD_EXIT=0`.

## Deliverable 2 — verdict against the accepted binary

- Clean-room binary SHA-256:
  `f07744d900624ce1c34a7a62dc514092a2cc4b86c2666db90c23db42cdb42630`
- Accepted pinned binary SHA-256: `f07744d900624ce1c34a7a62dc514092a2cc4b86c2666db90c23db42cdb42630`
- Accepted binary re-hashed in place before this run
  (`~/.local/state/ybx-emulator-upstream/qemu-0043-agent-audit-20261030/build-ybx/qemu-system-xtensa`,
  66 142 888 bytes): `f07744d9…42630` — the pin is live, not stale.
- Verdict: **BYTE-IDENTICAL.** The clean-room container build of the freshly
  composed tree, done from a copied object source on 2026-10-31, produced the
  exact accepted 2026-10-30 binary. Nothing differs; no re-pin is needed.

## Deliverable 3 — boot probe: does the factory flash still paint the Home frame

**YES, measured — not asserted.** The same lane drove the new
`verify_reproducible_build.sh --boot` stage against the clean-room binary
(throwaway `docker run --rm` on the pinned builder image; the probe copies
ROM/flash/SD into container `/tmp` and speaks QMP; the live browser container
was not touched):

```
$ sh tools/ybx-system-emu/verify_reproducible_build.sh --boot \
    --qemu-bin <scratch>/run1/compose/qemu-compose/build-ybx/qemu-system-xtensa \
    --work-dir <scratch>/run1
=== stage audit: chain definitions + patch signatures + manifest ===
stage=audit state=pass
stage=binary state=byte-identical expected=f07744d9…42630
=== stage boot: factory flash on a throwaway container QEMU ===
guest frames=4 nonwhite=4743 ram-writes=384000
frame sha256  : e79c2ad5f2b60b16fe3f20625742d5f1343f5785552d1b32c11f33c42a874672
stage=boot state=home-frame-match
verify_reproducible_build: all requested stages passed
(real wall time of that command: 37.0 s, 33 s of it the bounded guest run)
```

From `<scratch>/run1/boot-frame.json` (local artifact, 1.8 KB):
`rom_boot_seen: true` (UART `ESP-ROM:esp32s3-20210327 … SPI_FLASH_BOOT`),
`factory_flash_sha256: 206ed966…9523` inside the report,
`qemu_binary_sha256: f07744d9…42630` hashed **inside** the container,
`portrait_sha256: 9dc4e636e646629c81a91b9c97b8456931e247f10c5ed69a451c4fd720715485`
(transposed 480x800 view); `boot-frame.bmp`/`.png` kept beside it locally.
`nonwhite=4743` matches the historical accepted measurement
(QEMU_BRINGUP.md:1106).

The accepted facts used as pins (independently re-measured on this host
2026-10-31):

```
206ed9661bd71477d1a54397c5a2cfe490179a3f59ab094f1e7421745c609523  s3-flash-complete-rom-test.bin
5b950be93a30610322a3b46049e4095cdda04f33f09fa13a9a9a6167b5994bd9  s3-full-rom-generator-test.bin
```

Home frame pin source: `e79c2ad5…` recorded at
QEMU_BRINGUP.md:1111 and reproduced bit-identically at QEMU_BRINGUP.md:1169;
mirrored as `ACCEPTED_SCREEN_SHAS["home"]` in x4pro_guest_navigation.py:184-185.

## Honest limits of the comparison

- **What byte-identity would/would not prove.** It proves the *whole
  pipeline* — pinned base commit, 23-patch composition, configure flags,
  pinned builder image, container mount path — yields the accepted binary
  without touching the original build tree. It does **not** prove
  cross-toolchain reproducibility: a different gcc/binutils/libgcrypt,
  another image revision, or a changed `/src` mount path may legitimately
  produce different bytes. This is one comparison pair (this run vs the
  2026-10-30 accepted build), on one host (same Docker Desktop 28.3.2 VM, so
  the same guest kernel `uname` strings QEMU records in config-host).
- **Non-determinism vectors considered:**
  - The version/configure strings derive from the pinned base commit and the
    fixed relative configure invocation (build_reproducible_qemu.sh:172-176),
    so they matched across the two runs — observed, not assumed.
  - No `--enable-debug`, so no `-g` debug sections carry absolute paths; the
    compose tree is always bind-mounted at `/src`, making even the recorded
    `SRC_PATH` identical run to run.
  - The container image tag `ybx-qemu-builder:20261029` is mutable; a
    re-tagged image with different gcc/binutils/gcrypt would legitimately
    change the binary. This lane ran against image ID `223366d59dd6`
    (`docker images` output recorded at run time; experiments/0063 named the
    tag but never pinned the image ID — flag that for the next re-pin).
  - Host side: the composed tree lives on macOS bind-mounted into the
    aarch64 VM; file mtimes/ordering do not feed the binary — ninja output
    order is deterministic for a fixed graph.
- **Boot timing.** The probe gives the guest a bounded 33 s of wall time
  under TCG; a slower host or a contended VM (two other `ninja` lanes share
  this Docker VM) can cut the run short before the Home repaint. A
  `no-frame` result therefore means "did not paint within the window", not
  "cannot paint" — the script never infers more than it measures.
- **Not measured by this lane:** live browser container state, ROM/firmware
  picker behaviour, any guest beyond the Home page, SD FAT file creation,
  Wi-Fi/TLS. (Long-standing gates in QEMU_BRINGUP.md remain unchanged.)

## Host test evidence

```
$ cd tools/ybx-system-emu && python3 -m unittest test_verify_reproducible_build -v
… (12 tests: PlanAndUsage x3, FastAudit x1, BinaryCompare x3, BootStage x5)
Ran 12 tests in 8.854s
OK

$ python3 -m unittest discover -s tools/ybx-system-emu -p 'test_*.py' -q
Ran 515 tests in 30.871s
OK (skipped=11)
```

The five BootStageTests run against the *real* accepted binary and the *real*
pinned QA media (read-only inputs, both present on this host) so the SHA
gate, the media-pinning gate and the frame-compare logic are exercised for
real; only the container itself is stubbed via `YBX_DOCKER_BIN`. The fixture
probe reports inside tests carry deliberately wrong frame hashes — they are
test scaffolding for the mismatch branch, and the tests assert the script
names both the pinned and the measured value and exits non-zero. The
positive `home-frame-match` line above comes only from the real container
run in Deliverable 3.

## Orchestrator follow-ups

1. Optional repeat (all stages, ~1 h):
   `sh tools/ybx-system-emu/verify_reproducible_build.sh --build --boot --jobs 2 --qemu-src <copy> --work-dir <Docker-fileshared dir>`
2. Clean scratch dirs after merge:
   `~/.local/state/xteink-agent-workspaces/ybx-emu-batch4-20261031/scratch-repro-build-0072` (~2.4 GB: 1.1 GB object copy + composed tree + build-ybx).
