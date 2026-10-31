# 0074 — separate kernel with 0069 (SSD1677 waveform/sleep) and 0071 (Wi-Fi radio Stage 1)

Date: 2026-10-31. Orchestrator (Qoder Desktop).

## What was built

A private QEMU tree was cloned from the live fixed checkout and both proposals
were applied on top of it:

```
cp -a ~/.local/state/ybx-emulator-upstream/qemu-psram-fix-20261031 \
      ~/.local/state/ybx-emulator-upstream/qemu-wf-radio-20261031
git -C …/qemu-wf-radio-20261031 apply experiments/0069-ssd1677-waveform-sleep-v2.patch
git -C …/qemu-wf-radio-20261031 apply experiments/0071-s3-wifi-radio-stage1.patch
docker run --rm --platform linux/arm64 -v <tree>:/src -w /src/build-ybx \
  ybx-qemu-builder:20261029 ninja -j2 qemu-system-xtensa      # rc=0
```

Chain in the binary: 0042 + 0037 + 0043 + 0051 + **0068** + **0069** + **0071**.
Binary SHA-256: `f65db0a7e6940a6c08ea2ffe4e8653227e8fbea70b25f4c695fc7a2de6a68236`.
The live emulator keeps running `7e554ba9…` (chain without 0069/0071); this tree
is an opt-in experiment, not the default.

## Defaults are unchanged

- 0069 keeps `busy-waveform-ms = 350` and `busy-power-ms = 350` (the previously
  hard-coded value), so a default boot behaves exactly as before.
- 0071 adds `ybx-x4pro-wifi-stub`, a bool that defaults to **false**; the radio
  tap logger `x-radio-tap` also defaults to false.

## Verification on the new binary

| Check | Result |
|---|---|
| factory image boots | Home frame `e79c2ad5…`, frames 4, `factory_guest: true` |
| `/api/telemetry` on the factory image | BUSY rise 4, fall 4 (balanced), high-ms 1408 ≈ 4 × 350 ms — default timing unchanged |
| stock CrossPoint 2.0.0rc boots via the picker | frames 3, frame `78e74345…` |
| `probe_x4pro_epd_waveform_qtest.py` | all checks true: split power/waveform windows, `power_attributed`, `waveform_attributed`, deep-sleep flag and entries, `post_sleep_bytes_rejected`, `post_sleep_no_busy_edge`, `sw_reset_keeps_sleep`, `wake_clears_sleep`, BUSY trailing around reset, `edges_balanced` |
| `probe_x4pro_wifi_radio_qtest.py` (default) | unchanged: radio windows still zero-fill, `stub: null` |
| `probe_x4pro_wifi_radio_qtest.py --with-stub` | stub answers coherently: magic `0x59425230`, window count 6, crypto key read-back `0x5a5a5a5a`, all rows `ok: true` |
| stock CrossPoint with `ybx-x4pro-wifi-stub=on` (direct QEMU, 40 s) | the guest's early path CHANGES: with the stub off it prints two `Preferences.cpp nvs_open failed: NOT_FOUND` lines and hangs; with the stub on it prints only the ROM lines and hangs earlier. Neither paints. |

## Honest conclusion

- 0069 is verified as an SSD1677 model upgrade: it adds the split windows, the
  deep-sleep state and the reset trailing behaviour without changing any
  default boot.
- 0071 is verified as an instrument, not as a fix: the stub answers the pinned
  register inventory, but it does not unblock stock CrossPoint, and enabling it
  changes that image's early boot path. It must stay opt-in, and the side effect
  needs its own investigation before the stub is used for Wi-Fi work.
- The default emulator remains on `7e554ba9…`; this kernel is built and
  verified separately at the owner's request.

## Reproduce

```sh
DST=~/.local/state/ybx-emulator-upstream/qemu-wf-radio-20261031
E=~/Projects/YBX/ybx-esp32-emulator/tools/ybx-system-emu/experiments
git -C $DST apply --check $E/0069-ssd1677-waveform-sleep-v2.patch
git -C $DST apply --check $E/0071-s3-wifi-radio-stage1.patch
docker run --rm --platform linux/arm64 -v $DST:/src -w /src/build-ybx \
  ybx-qemu-builder:20261029 ninja -j2 qemu-system-xtensa
shasum -a 256 $DST/build-ybx/qemu-system-xtensa   # f65db0a7…8236
```

## Switched to the live emulator (owner decision, 2026-10-31)

The main browser instance now runs this kernel: the container reports
`f65db0a7…` inside `/qemu/build-ybx/qemu-system-xtensa`, the factory image
boots to Home `e79c2ad5…` with balanced BUSY counters (rise 4 / fall 4,
high-ms 1402 ≈ 4 × 350 ms), stock CrossPoint 2.0.0rc still boots via the picker
(`78e74345…`), and the factory image comes back cleanly afterwards.
Rollback trees: `qemu-psram-fix-20261031` (`7e554ba9…`, 0068 only) and
`qemu-0043-agent-audit-20261030` (`f07744d9…`, pre-0068).
