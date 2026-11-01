# 0099 — inkMOD 1.1.10: native GUI navigation via actual GT911 → SSD1677

Date: 2026-11-01. Scope: **isolated test QEMU only**, not browser 8784. Based on the `0096` RTC-register handshake, `0097` explicit deep-wakeup digital reset and `0098` GT911 short-reset repair. This does **not** imply an autonomous EXT1 physical wake source exists or that the emulator is hardware-complete.

## Live test environment

- QEMU `~/.local/state/ybx-emulator-upstream/qemu-i2c-20261101/build-ybx/qemu-system-xtensa`; SHA-256 `940cf82983969d09f8b1831a2e0926bd09fd1ee46113611d98dc85401c5f483c`.
- Original inkMOD v1.1.10 X4 Pro merged flash, SHA-256 starts `3fb63a3101fd662a39` (verified again against each run input). Separate original ESP32-S3 ROM and pristine SD FAT16 image, copied into each isolated, disposable QEMU process.
- Each test booted the real guest, waited for `RTC_CNTL_STATE0.SLEEP_EN`, **explicitly** injected one `ybx-rtc-inject-deep-wakeup` QMP event, waited for three native SSD1677 frames, then drove `ybx-touch-x/y/state` on the virtual GT911. No firmware patches, mocks, HTML screens, preloaded guest screenshots or synthetic I2C success were used.
- Evidence root on host: `~/Projects/YBX/artifacts/ybx-esp32-system-emulation-20261029/inkmod-native-touch-20261101/{baseline,home-navigation,settings,settings-tabs}/`. Each lane has its original `verdict.json` and frame PNGs. Representative 480x800 real captures are committed to `assets/screenshots/06-inkmod-home.png` through `10-inkmod-reader.png`.

## Confirmed transitions

| Guest input and state | Native I2C touch evidence | SSD1677 frame SHA after | Verdict |
|---|---|---|---|
| Baseline after DSLEEP wake | GT911 ready; no injected tap | `8955f0f2d64e2139ce2c9f8af5a570ddfe4c3871ba4c89949730c1cd2b8e3266` | Real inkMOD Home |
| Home → **Browse Files** `(240,344)` | +1 GT911 `point-reads`, +2 `status-clears` | `e5fdf7b8e394bb23ccc2886611a41d1b71e879949161760bd8327dbb7413707c` | **PASS**: SD card / No files found |
| Home → **Settings** `(240,590)` | +1 point read, +2 clears | `3f9b3bf8fb06988ad82a346c7703bc89f2404c3a2d225d568a1d4f5f61b2685e` | **PASS**: Display/Reader/Controls/System tabs |
| Settings → **Controls** `(351,110)` | +1 point read, +2 clears | `6f6c73c8190073cad9f31229313d175a162320826d2c3627807aa874eade07a1` | **PASS**: touch reader controls, power/home/side buttons |
| Controls → **Reader** `(142,110)` | +1 point read, +2 clears | `9f12ad3a7239587882f44899b8c1a8831275814e172d14b0d0f65f72021f2b50` | **PASS**: font, layout, bionic-reading settings |

The input coordinates have been measured against the actual framebuffer screenshots; do not assume a touch at a screen header always activates the displayed label. All accepted transitions increment the SSD1677 frame counter, have a different visible frame hash, and were consumed by the **guest's I2C reads**, rather than just accepted by QMP.

## Negative steps are NOT counted

- From **Browse Files**, tapping `(35,60)` was consumed by GT911 (+1 point read and +2 status clears) but did **not** change the display. That screen has **no visible Back arrow** in the measured image. Later `(240,590)` is also inert from Browse Files. The first mixed lane is therefore `PARTIAL` under the revised strict per-step classifier, **not a 3/3 successful tour**.
- From the Controls tab, `(245,110)` was consumed by GT911 but did **not** change the screen; that intermediate step must not be described as a successful transition. The settings-tabs lane is `PARTIAL` (3 out of 4 transitions).
- The single-step Settings lane (`Home → Settings`) is independently `PASS`. Baseline with zero taps is always `NOT_VERIFIED`.
- Every recorded run had `uart_panic=false`, zero `ESP_ERR_INVALID_STATE` errors. No fabricated ACK or web-rendered frame was involved.

`probe_inkmod_native_touch.py` is a standalone offline runner with a strict outcome contract: `PASS` only if **all** requested taps result in real guest I2C point consumption/status acknowledgment and changed, advanced visible SSD1677 frames; `PARTIAL` when some steps are real and others inert; `NOT_VERIFIED` when no step qualifies. Six independent host tests enforce no false positives for QMP-only injection, stale or missing SHA, unchanged frames, no guest status clearing, failed QMP press, and mixed/empty sequences. It also declares `physical_touch_verified=false`: this tests a virtual GT911 and a real guest, not an actual reader.

## Limitations and next steps

1. The live `8784` container still runs the audited 26-patch QEMU and does not boot inkMOD past the RTC poll without the experimental work. No kernel swap, production deployment or 26-patch manifest change was made here.
2. The `0097` deep-sleep wake is still manually triggered by an **explicit** QMP input. The next QEMU-level work is to implement ESP32-S3 RTC EXT1 source selection and actual board GPIO level routing, verify that only enabled pins and the right wake level can cause a digital reset, and test RTC wake status/reset reason across a true guest boot. Avoid turning a fixed timer or unconditional sleep request into an artificial wake.
3. Physical side/Home/Back button emulation and navigation have not been accepted for inkMOD. No claims about real Wi-Fi, SD content discovery beyond an empty test card, touch hardware electrical fidelity, or reading books on real hardware are implied.
4. The host is low on disk space (approximately 4.3 GiB free during the run). Use copy-on-write for any additional QEMU checkout, avoid duplicate 128 MB SD images and do not remove other users' build files without inspection.
