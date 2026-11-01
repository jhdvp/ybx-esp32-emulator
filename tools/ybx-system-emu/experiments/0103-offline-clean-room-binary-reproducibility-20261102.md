# 0103 — Clean-room offline QEMU binary reproducibility and original guest acceptance

Local host date: 2026-11-02 (Europe/Minsk). Experimental X4 Pro QEMU branch was **not promoted** to the stable 8784 browser; the experimental 8794 instance was also left running and untouched.

## Result: byte-for-byte reproducible `qemu-system-xtensa`

From a **new empty source/build checkout** under
`~/.local/state/xteink-agent-workspaces/ybx-0102-clean-room/qemu-compose/`, I rebuilt `qemu-system-xtensa` using only the existing pinned Espressif QEMU Git object database, the audited main patch manifest, six additional experimental patch files `0096–0101`, three pinned Git subprojects, and the existing `ybx-qemu-builder:20261029` Linux arm64 Docker image with `--network none`.

All **1,544 Ninja steps completed successfully**; the fresh binary SHA-256 is **identical** to the previously tested opt-in virtual-Power QEMU:

```
clean room: 7b729fba1ceefdc67a55dc9ddd6d359699e16174a52d741ea243849c35e2accc
original:   7b729fba1ceefdc67a55dc9ddd6d359699e16174a52d741ea243849c35e2accc
```

This upgrades 0102's **31-file source composition equivalence** into **actual binary reproducibility**. No network access was used for the successful build.

## Source and dependency pinning

- Pinned QEMU Git base commit: `febae182e132e4055529be423a818225ebddaa3a`.
- `check_qemu_patch_composition.py compose --chain full` checked the audited 26-patch source composition against its retained manifest and reported zero failures.
- Applied the additional research patches in strict order: `0096-s3-rtc-sleep-registers-explicit-wakeup.patch`, `0097-s3-rtc-explicit-deep-wakeup-dig-reset.patch`, `0098-gt911-ignore-short-reset-pulse.patch`, `0098-i2c-gt911-readonly-qom-telemetry.patch`, `0100-rtc-ext1-gpio-level-wakeup.patch`, `0101-x4pro-power-button-gpio3-rtc3-reset-retention.patch`.
- Exact external dependencies, verified against both the upstream QEMU `.wrap` revision and the cached Git repo HEAD, then copied into the fresh checkout:
  - `keycodemapdb` — `f5772a62ec52591ff6870b7e8ef32482371f22c6`
  - `berkeley-softfloat-3` — `b64af41c3276f97f0e181920400ee056b9c88037`
  - `berkeley-testfloat-3` — `e7af9751d9f9fd3b47911f51a5cfd08af256a9ab`
- Build flags exactly matched the accepted audited Linux arm64 build: `--target-list=xtensa-softmmu,riscv32-softmmu --enable-gcrypt --disable-nettle --disable-docs --disable-gtk --disable-sdl --disable-vnc --disable-curses --disable-tools --disable-werror`; Ninja `-j3`.

The first two offline configure attempts failed before compiling because fresh Meson clones tried fetching `keycodemapdb` and then `berkeley-softfloat-3` from GitLab. The failures were not QEMU source or compiler errors. After verifying/copying all three pinned offline subprojects, a full **new build** completed without network. `build_x4pro_power_clean_room.sh` retains this deterministic procedure with fail-closed dependency SHA and final binary SHA checks; the exact manual procedure was tested, while the newly extracted shell recipe has been syntax/contract tested but not independently rebuilt for a second time.

## Acceptance on the **new** binary

The following QTests were re-run against the clean-room `qemu-system-xtensa` in offline Docker (all exit 0):

| Real QEMU QTest | Outcome |
|---|---|
| X4 Pro virtual Power GPIO3 → RTC EXT1 and reset retention | **PASS**, 25 QTest MMIO operations |
| ESP32-S3 RTC EXT1 selected GPIO and wake-condition gates | **PASS**, 30 QTest MMIO operations |
| GT911 short RESET glitch / readiness | **PASS**, 135 QTest operations |
| GT911 touch injection / guest I²C | **PASS**, no regression |

Original unmodified inkMOD 1.1.10 native guest:

| Case | Native outcome |
|---|---|
| Before virtual Power press | EXT1 configured to GPIO3/ALL_LOW, **0 wakes, 0 EPD frames**, 0 panics and no I²C errors |
| On virtual Power pressed LOW | **1 RTC EXT1 wake**, `EXT1_STATUS=0x8`, reset reason `0x3145` (both CPUs DSLEEP=5), ROM `rst:0x5 (DSLEEP)`, **5 EPD frames**, **0 I²C invalid-state errors**, 0 panics |

Original Factory X4 Pro firmware: **4 frames**, 82 SSD1677 commands, unchanged visible home-frame SHA `e79c2ad5f2b60b16fe3f20625742d5f1343f5785552d1b32c11f33c42a874672`, no panic/I²C invalid-state errors.

Original CrossPoint 2.0.0rc firmware: **3 frames**, 65 SSD1677 commands, unchanged home-frame SHA `78e743450b8f67ced89eb59d86b7c9da61eb306b4c3e92c31f50b345ad99c296`, no panic/I²C invalid-state errors.

Evidence retained on developer Mac at `/tmp/ybx-0103-clean-room-validation/` and `/tmp/ybx-0102-clean-room-build-3.log`. The firmware/ROM/SD inputs were supplied read-only to an offline container and copied to disposable guest images by the existing probes. No proprietary firmware or QEMU binaries are included in Git.

## Automation and remaining release gates

`build_x4pro_power_clean_room.sh --work-dir EMPTY_DIRECTORY --build` is a dedicated offline, **explicit opt-in** build recipe. It refuses an existing output tree, checks all dependency commits against upstream wrap revisions, verifies each ordered experimental patch and fails if the resulting binary SHA differs. It is not an update/deployment command and never edits a running QEMU or browser.

Host regression suite including four new offline-build safety tests: **947 tests total (937 passed, 10 skipped), exit 0**.

The following have **not** been verified and must remain open in issue #8 before replacing production `8784`: complete long-running SD/FAT write integrity after repeated sleep/wake, all Factory and CrossPoint navigation sequences on the newly built QEMU, RTC analog mux/EXT0/timer and sleep-domain power behavior, browser security outside trusted localhost, and extended GUI stability. Do not treat successful Home frame or virtual Power as physical button validation.

The user's uncommitted `0095` main-worktree report was left untouched throughout. 8784/8794 were not stopped, rebound or rebuilt.
