# 0098 — inkMOD post-deep-sleep I²C: genuine GT911 reset glitch, fix verified

Date: **2026-11-01**. Branch: `feat/inkmod-i2c-telemetry-20261101`. This is an **isolated** QEMU build, not the deployed 8784 kernel.

## Source of error 259 established by native guest + emulated bus counters

After the `0097` opt-in digital deep-wakeup (`rst:0x5 (DSLEEP)`), the real inkMOD 1.1.10 firmware drew three SSD1677 frames but printed repeated Arduino `esp32-hal-i2c-ng.c::i2cWriteReadNonStop()` / `ESP_ERR_INVALID_STATE` diagnostics. The installed ESP-IDF `esp_driver_i2c/i2c_master.c` returns this error when `s_i2c_send_commands` leaves its status other than `I2C_STATUS_DONE` (several other cases also exist, so that static code alone was inconclusive).

To distinguish peripheral completion vs NACK, I added **read-only** QOM metrics for both I²C controllers (start, NACK, completed, END, IRQ, last address/command/raw/enable) and virtual GT911 power/reset state (ready, rails, pulse lengths, qualified/short reset counts, I²C accepts/NACK). The optional `probe_x4pro_inkmod_trace.py --i2c-telemetry` samples them without affecting the guest. No forged ACK, disabled power gating, bypassed I²C error, or synthetic touch was added.

Actual 0098 control run, **before** changing reset semantics:

- `I2C0`: 447 transactions; **227 NACK**, 220 completes, IRQ raises 447; `I2C1`: 0 transactions. UART reported **227 `ESP_ERR_INVALID_STATE`** occurrences for that run; `last-addr = 0x5D` (GT911).
- Instrumented repeat with GT911 gate telemetry (10 seconds): **113 I²C NACK**, 113 GT911 gate-NACK; **113 UART `ESP_ERR_INVALID_STATE`**. No generic I²C IRQ starvation.
- At t≈2.5s, GT911 had `power-present=true`, `ready=true`, successful valid reset pulse **9653 µs**, and had accepted 163 starts.
- At t≈4.0s after the host-triggered digital reset, its rails were still on, `reset-high=true`, but **`ready=false`**, `ready-timer-armed=false` and it began NACKing. The last reset-low pulse was just **30 µs**, with `short-reset-rises=1`. There were two qualified reset-rises as well, so the failure was *not* absence of an initial power-on reset.

**Root cause in our GT911 model:** `gt911_reset_pin()` immediately set `ready_after_ns=INT64_MAX` and cleared contact state on **every falling RESET edge**, while it only scheduled a new 50 ms ready timer if the subsequent rising edge completed a pulse of at least **2 ms**. The observed 30 µs pulse therefore left the virtual GT911 **permanently unready** after it had previously been ready. The QEMU I²C controller correctly reported the NACK and IDF rejected the transaction. This is a mismatch in the emulator's own reset-gate policy, not a missing I²C IRQ.

## Actual fix — maintain the emulator's existing timing contract

[`0098-gt911-ignore-short-reset-pulse.patch`](0098-gt911-ignore-short-reset-pulse.patch) changes *only* `hw/i2c/ybx_x4pro_i2c.c`:

- When RESET is held low the device remains NACKing, as before, but the previous ready timer/contact state is **not erased on the falling edge**.
- On an adequately qualified low pulse (at least 2 ms), explicitly clear the old contact state and schedule the existing 50 ms startup interval. Full reset and power-off behavior remain unchanged.
- On a shorter invalid pulse, re-assert the prior IRQ/readiness state when RESET is released instead of latching the device permanently offline. A device that was already unready remains unready; no new fake ready event is introduced.

Physical GT911 response to very brief RESET glitches has not been established on a real device. This patch preserves and makes self-consistent the **existing model requirement** (2 ms minimum) and native guest expectations; it is not a claim about a measured GT911 hardware timing limit.

The **separate and optional** [`0098-i2c-gt911-readonly-qom-telemetry.patch`](0098-i2c-gt911-readonly-qom-telemetry.patch) adds observability only. Verified sequential composition of `0098` functional + telemetry patches against the source tree produced **byte-for-byte identical source files** to the experimental QEMU rebuilt and run. These two patches assume the 0096 + 0097 experimental QEMU base for the inkMOD native run. The functional GT911 fix can be independently reviewed for the stable 26-patch QEMU, but has not been deployed to port 8784.

## Measured acceptance after fix

- **Native inkMOD 1.1.10**, opt-in QMP `deep-wakeup`, 12s isolated run: **420 I²C0 starts / 420 completes, 0 NACK**, **0 UART `ESP_ERR_INVALID_STATE`**, 0 panic; GT911 `ready=true`, 612 accepted slave start events, `short-reset-rises=1`, `last-reset-low-us=32`.
- Native E-Ink frame unchanged: **3 frames, 69 commands, 288000 RAM writes**, visible SSD1677 SHA `8955f0f2d64e2139ce2c9f8af5a570ddfe4c3871ba4c89949730c1cd2b8e3266`.
- **Real QTest against compiled experimental QEMU**: `probe_x4pro_gt911_short_reset_qtest.py` **PASS, 135 QTest operations**, verifies power-off/reset-low NACK, valid 3ms RESET requires 50ms startup, subminimum 30µs RESET preserves a previously ready device and an injected contact, and QMP reports qualified vs short pulses. Report `/tmp/ybx-i2c-0098/gt911-short-reset-qtest.json`.
- Pre-existing real `probe_x4pro_touch_injection_qtest.py`: **PASS**; no GT911 touch regression.
- **Factory X4 Pro**: 4 frames, 82 commands, EPD home SHA `e79c2ad5f2b60b16fe3f20625742d5f1343f5785552d1b32c11f33c42a874672`, NACK 0, panic false.
- **CrossPoint 2.0.0rc**: 3 frames, 65 commands, EPD home SHA `78e743450b8f67ced89eb59d86b7c9da61eb306b4c3e92c31f50b345ad99c296`, NACK 0, panic false.

All firmware, ROM, UART and SD runs were offline, with *private writable copies* of the original images. No firmware binary, private UART dump or key is included in Git.

## Limitations and next gates

1. This does **not** implement physical ESP32-S3 RTC GPIO/EXT1/timer wake. The `0097` deep-wakeup must still be triggered by the host via QMP; real independent sleep/wake needs a separate hardware model.
2. inkMOD **does not yet have native navigation/end-to-end function acceptance**. Three painted frames and no I²C errors are boot/UI milestones only.
3. The exact behavior of a real GT911 under a 30µs RESET pulse has not been probed on hardware; the chosen behavior is internally consistent with the emulator's minimum reset pulse model. Hardware integration tests remain an acceptance gate.
4. The running `8784` container is still on the independently audited 26-patch binary `f65db0a7…`. The 0098 patches are research candidates, **not applied to its patch manifest or launcher**.
5. The best next step is to verify GT911 touch and navigation on **native inkMOD** through frame-SHA and genuine guest I²C consumption, then model an actual RTC wake source instead of requiring manual QMP input.
