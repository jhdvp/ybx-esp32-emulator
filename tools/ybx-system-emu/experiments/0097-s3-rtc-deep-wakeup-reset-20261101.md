# 0097 — ESP32-S3 deep sleep wake triggers a digital reset, not a returning IRQ

Date: 2026-11-01. Worktree: `feat/rtc-deep-wake-20261101`. **Experimental: not deployed to the browser at port 8784, and not appended to the audited 26-patch QEMU chain.**

## 1. Root cause of the 0096 `abort()`

The inkMOD 1.1.10 UART output in `0096` was not an arbitrary crash:

```
E (1487) sleep: Deep sleep request is rejected
abort() was called at PC 0x40377508 on core 1
```

The ESP32-S3 ESP-IDF `components/esp_hw_support/sleep_modes.c` on the host contains:

- `deep_sleep_start()`: on `ESP_ERR_SLEEP_REJECT`, logs `Deep sleep request is rejected`, unwinds stalled CPU, and returns an error.
- `esp_deep_sleep_start()` (annotated `noreturn`): invokes `deep_sleep_start()` and then **deliberately** calls `abort()` if execution ever returns. Thus `SLP_REJECT` cannot be used as a workaround for an otherwise accepted deep sleep.
- An accepted deep sleep does **not return to the instruction after `SLEEP_EN`**. A real deep sleep wake restores the digital subsystem via reset. The RTC domain and its reset cause survive this digital reset.

The first 0096 QMP hook `ybx-rtc-inject-wakeup` raises bit 0 of `RTC_CNTL_INT_RAW`. It is useful for investigating a light-sleep-style handshake, but not a faithful *deep sleep* wake. The first 0096 hook for `SLP_REJECT` correctly exposes an abort in the guest.

## 2. New opt-in experimental QEMU patch

[`0097-s3-rtc-explicit-deep-wakeup-dig-reset.patch`](0097-s3-rtc-explicit-deep-wakeup-dig-reset.patch) applies **after** [`0096`](0096-s3-rtc-sleep-registers-explicit-wakeup.patch). The 0097 patch modifies only the RTC device, its state and the ESP32-S3 SoC QOM aliases. It has no Wi-Fi, EPD, GT911, or flash contents.

- Adds write-one QMP property `/machine/soc:ybx-rtc-inject-deep-wakeup` as a **manual host input**. A write is ignored if `RTC_CNTL_STATE0.SLEEP_EN` has not been requested. This is not real EXT1/timer/GPIO event detection.
- On a qualifying request: clears the sleep request and relevant pending RTC raw wake/reject bits, records `ESP32_DEEPSLEEP_RESET` for both CPU cores, increments a read-only `ybx-rtc-deep-wake-resets` counter and pulses the existing `rtc_cntl.dig_reset_req` line. Existing Espressif QEMU SoC logic then requests digital reset of CPUs and peripherals while retaining RTC state.
- Does **not** return into the guest's suspended call stack; the ROM executes again with `rst:0x5 (DSLEEP)`.
- The previously introduced `ybx-rtc-inject-wakeup`/`...reject` hooks remain independent for negative tests. Normal operation without any QMP injection remains unchanged.

### QEMU identity and scope

| Binary | SHA-256 |
|---|---|
| Audited main 8784 | `f65db0a7e6940a6c08ea2ffe4e8653227e8fbea70b25f4c695fc7a2de6a68236` |
| 0096 RTC registers, no deep reset hook | `f363cc21a74aad7167c18a4d634780a0c21eb2a306ecfa59260b725a08b483eb` |
| 0097 explicit deep reset | `61c051998073fbde67f5147dc8a9496f19eea77482644ce800fe1ed53024cf3b` |

Copy-on-write source: `~/.local/state/ybx-emulator-upstream/qemu-deep-wake-20261101`, built by an isolated Docker QEMU builder with `--network none`. The `0097` patch passed `patch --dry-run -p1` against the 0096 source. None of these QEMU copies replaces the running port 8784.

## 3. Real QTest/QMP acceptance

`probe_x4pro_rtc_deep_wake_qtest.py` executes against actual compiled QEMU with `-accel qtest`. Result: **PASS**, 10 QTest MMIO operations; JSON at `/tmp/ybx-rtc-0097/deepwake-qtest.json`.

- Without a prior `SLEEP_EN` request, the external deep-wake injection does nothing.
- After an armed request, `INT_RAW` does **not** fake a returning wake or reject IRQ.
- `RTC_CNTL_RESET_STATE == 0x3145`; reset cause fields on both CPUs decode to **5 = ESP32_DEEPSLEEP_RESET**.
- The counter advances exactly once and a second unarmed injection is ignored.
- Firmware ROM reset and guest boot verified separately below.

## 4. Real firmware acceptance

All images are original private local guest images supplied from the existing YBX artifact set; no image is included in Git. Each run uses a private copy of flash/SD and an offline Docker process. `probe_x4pro_inkmod_trace.py --inject-rtc deep-wakeup` sends one explicit QMP deep-wake only after the guest has made its first sleep request. No ELF/map was available for inkMOD; function attribution below is supported by the installed Espressif IDF implementation and UART message, not by inkMOD's exact source-level symbols.

| Guest | QMP event | Outcome |
|---|---|---|
| inkMOD 1.1.10 | none (0096 control) | RTC poll stalled, 0 EPD commands/frames |
| inkMOD 1.1.10 | `wakeup` (0096) | CPU1 leaves the first poll, but 0 EPD frames |
| inkMOD 1.1.10 | `reject` (0096) | 2 frames then `Deep sleep request is rejected` and deliberate abort |
| **inkMOD 1.1.10** | **`deep-wakeup` (0097)** | **ROM reports `rst:0x5 (DSLEEP)`; 3 frames, 69 commands, 288000 RAM writes, 4034 nonwhite bytes; UART contains no panic** |
| Factory X4 Pro | none (0097) | 4 frames, 82 commands; home frame `e79c2ad5f2b60b16fe3f20625742d5f1343f5785552d1b32c11f33c42a874672`, no panic |
| CrossPoint 2.0.0rc | none (0097) | 3 frames, 65 commands; home frame `78e743450b8f67ced89eb59d86b7c9da61eb306b4c3e92c31f50b345ad99c296`, no panic |

This is **meaningful inkMOD progress, not complete compatibility**. After the deep-wake boot, `esp32-hal-i2c-ng.c` repeatedly reports `i2c_master_transmit_receive failed: [259] ESP_ERR_INVALID_STATE`. The same class of failures appeared after the 0096 `SLP_REJECT` abort-triggered reboot. The I²C issue therefore needs a separate diagnosis; it is not proven to originate in the deep-wake hook. No claims of complete functional navigation or normal RTC/EXT1/timer wake cycles can be made yet.

The image reported `flash writes: 1 block` because the *private run copy* changed; the original local ROM, firmware and SD inputs remained read-only and were not modified.

## 5. Reproduce and protect against false positives

The experimental `0097` patch is sequential after `0096`. Build its composed QEMU source in the project's offline builder. Run:

```bash
python3 /tools/probe_x4pro_rtc_deep_wake_qtest.py \
  --qemu-bin /qemu/build-ybx/qemu-system-xtensa \
  --rom /qa/s3-full-rom-generator-test.bin

python3 /tools/probe_x4pro_inkmod_trace.py \
  --qemu /qemu/build-ybx/qemu-system-xtensa \
  --rom /roms/s3-full-rom-generator-test.bin \
  --flash /fw/inkmod-v1.1.10-x4pro-flash-16m.bin \
  --sd /qa/ybx-test-sd-fat16-mbr-pristine.img \
  --label inkmod-deepwake --profile inkmod \
  --inject-rtc deep-wakeup --seconds 27 --out /out/inkmod-deepwake
```

The trace tool now distinguishes `deep_sleep_rejected_abort` from arbitrary `guest_panic`. UART decoding also exposes `deep_sleep_reset_seen`, `deep_sleep_rejected_then_abort` and `i2c_invalid_state_count`. Host regression tests pin both positive and negative cases, avoiding the false conclusion that a frame before a deliberate abort means a successful boot.

## 6. Next acceptance gates

1. Investigate `ESP_ERR_INVALID_STATE` on ESP32-S3's next-boot I²C path. Compare initialized I²C master state and SDA/SCL routing to factory and CrossPoint; do not replace errors with fabricated successful transactions.
2. Model proper sleep entry, RTC EXT1/timer wake source selection, power-off/restart and reset causes independently. The 0097 QMP injection is a diagnostic hook, not physical wake emulation.
3. Verify any proposed promotion on factory and CrossPoint navigation, SD read/write integrity and EPD/GT911 QTest after composition. **Do not automatically enable this experiment in the stable 8784 launcher.**
4. Exclude private binary images, UART dumps containing possible credentials, and QEMU build products from Git. Only the patch, host tests and evidence narrative should be committed.
