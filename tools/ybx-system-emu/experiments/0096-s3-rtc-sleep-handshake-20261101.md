# 0096 — ESP32-S3 RTC sleep handshake: isolated implementation and live acceptance

Date: 2026-11-01. Experimental branch: `feat/rtc-sleep-wake-20261101`. **Not promoted to the running 8784 QEMU.**

## Finding and implementation

The native inkMOD 1.1.10 application polls `RTC_CNTL_INT_RAW_REG` at `0x60008044` after programming sleep. The audited 26-patch QEMU kernel has **no read/write cases** for the ESP32-S3 RTC group `0x18`, `0x3c`, `0x40..0x4c`, and has no RTC source wired to the interrupt matrix. The upstream ESP-IDF **ESP32-S3** register header and `esp_hw_support/port/esp32s3/rtc_sleep.c` (not the C3 equivalents) establish `SLEEP_EN` at bit 31 of `STATE0`, and wakeup/reject raw bits 0/1. This revises the previous 0093 caveat about the absence of S3 headers: the headers are installed in the host's local PlatformIO ESP-IDF package.

Experimental patch: [`0096-s3-rtc-sleep-registers-explicit-wakeup.patch`](0096-s3-rtc-sleep-registers-explicit-wakeup.patch). Three QEMU source files changed: `include/hw/misc/esp32s3_rtc_cntl.h`, `hw/misc/esp32s3_rtc_cntl.c`, and `hw/xtensa/esp32s3.c`.

- `STATE0` and `WAKEUP_STATE` and `SLP_REJECT_CONF` become readable/writable, including `SLEEP_EN` requests.
- `INT_ENA`, `INT_RAW`, `INT_ST` and `INT_CLR` now provide masked-status and write-one-to-clear semantics.
- The RTC IRQ output is wired to `ETS_RTC_CORE_INTR_SOURCE` in the S3 interrupt matrix.
- QOM properties `ybx-rtc-inject-wakeup` and `ybx-rtc-inject-reject` may **explicitly inject** the corresponding RTC event **only after** a guest sleep request. The source of each injection is the test harness, not an emulated GPIO, timer, EXT1 signal, or real hardware.
- Read-only QOM counters `ybx-rtc-sleep-requests`, `ybx-rtc-injected-wakeups`, `ybx-rtc-injected-rejects`, `ybx-rtc-int-raw` make the handshake observable; no automatic wakeup is invented.
- The current QEMU machine leaves the RTC domain intact on QMP `system_reset`, although it resets the CPUs. The QTest explicitly records and checks this observed behavior, rather than asserting a cold RTC reset that the machine does not perform.

`patch --dry-run -p1` of the experiment against the unmodified audited live source checkout passed for all three files. The working binary was built in a **copy-on-write isolated** QEMU directory using the already installed `ybx-qemu-builder:20261029` Docker image, `--network none`; the user's live `/qemu` mount was never changed.

## Binary identity

| Role | SHA-256 |
|---|---|
| Original running 8784 kernel | `f65db0a7e6940a6c08ea2ffe4e8653227e8fbea70b25f4c695fc7a2de6a68236` |
| Isolated 0096 kernel | `f363cc21a74aad7167c18a4d634780a0c21eb2a306ecfa59260b725a08b483eb` |

The experimental kernel lives under `~/.local/state/ybx-emulator-upstream/qemu-rtc-experiment-20261101/build-ybx/`. No prebuilt QEMU binary or proprietary firmware image is committed.

## Actual QTest + QMP acceptance

Run `probe_x4pro_rtc_sleep_qtest.py` with `--qemu-bin` and `--rom` against an isolated QEMU instance (example inside the existing offline Docker environment):

```sh
python3 /tools/probe_x4pro_rtc_sleep_qtest.py \
  --qemu-bin /qemu/build-ybx/qemu-system-xtensa \
  --rom /qa/s3-full-rom-generator-test.bin \
  --out /results/rtc-qtest.json
```

Measured result (2026-11-01): **PASS**, 39 real QTest MMIO operations. `WAKEUP_STATE` and reject-conf readback; no automatic event on unarmed RTC; `INT_RAW=1` for explicit wakeup; `INT_RAW=2` for explicit reject; masked `INT_ST`, W1C clearing, and RTC-domain retention under QMP `system_reset` all matched assertions. This is genuine QEMU/QMP execution, not a Python mirror of the registers. Saved to `/tmp/ybx-rtc-0096/rtc-qtest.json` on the development host. Host-only patch contract tests also pass.

## Native inkMOD 1.1.10 and control firmware

Executed against the compiled **0096 kernel**, the original merged flash images, and the generated full ESP32-S3 ROM; each run had a **private flash/SD copy** and a separate QEMU process. These are live guest measurements, not deductions from static comments.

| Guest and explicit test input | SSD1677 frames / commands / RAM writes | Exit point |
|---|---|---|
| inkMOD 1.1.10 — **no injection** | `0 / 0 / 0` | CPU1 polls `0x60008044==0`, CPU0 waits in IPC |
| inkMOD — **QMP inject wakeup** | `0 / 0 / 0` | CPU1 moves to `0x403774fc`, CPU0 still waiting IPC; no paint |
| inkMOD — **QMP inject reject** | `2 / 50 / 192000` | Guest prints `abort() was called at PC 0x40377508 on core 1`; 3848 nonwhite bytes |
| Factory X4 Pro — **no injection** | `4 / 82 / 384000` | Boot and home frame SHA `e79c2ad5f2b60b16fe3f20625742d5f1343f5785552d1b32c11f33c42a874672`, no panic |
| CrossPoint 2.0.0rc — **no injection** | `3 / 65 / 288000` | Boot and home frame SHA `78e743450b8f67ced89eb59d86b7c9da61eb306b4c3e92c31f50b345ad99c296`, no panic |

`probe_x4pro_inkmod_trace.py` gained an opt-in `--inject-rtc {wakeup,reject}` parameter that emits exactly one QMP event *after* `ybx-rtc-sleep-requests >= 1`. The report records the injected event and guest consequences. Normal tracing with no flag remains unchanged. Private reports are stored under `/tmp/ybx-rtc-0096/{inkmod-control,inkmod-wakeup,inkmod-reject,factory,crosspoint}/report.json`.

**Crucial distinction:** `SLP_REJECT` reproduces the previously observed two painted frames **without tampering with inkMOD's flash bytes**; it does not make inkMOD boot successfully, because the guest then aborts. `SLP_WAKEUP` does *not* produce the same behavior. The RTC failure is real, but not the only gap, and blindly forcing every sleep request to wake would misrepresent real ESP32-S3 behavior.

## Remaining work and release gate

1. EXT1 GPIO wake source, RTC timer and real sleep/deep-sleep transitions are still **not implemented**. The manual QMP event is a diagnostic interface, **not physical wake emulation**. Deep sleep must not simply return into the guest; a genuine reset/wake lifecycle is needed.
2. The inkMOD `abort()` at PC `0x40377508` remains unexplained; trace the exact program path, RTC reset reason and wake cause after `SLP_REJECT` before attempting to make it boot. Do not claim a successful inkMOD boot.
3. The RTC IRQ output is connected by source code and `INT_ST` is verified in QTest, but a guest-written interrupt routing/ack sequence for `ETS_RTC_CORE_INTR_SOURCE` has not yet been measured in live firmware.
4. Do not add this patch to the audited 26-patch release chain or replace the 8784 binary until RTC wake-source fidelity and boot matrix acceptance are complete.
5. No new background monitoring or continuously running QEMU instances are left by these tests; all experimental QEMU instances were disposable Docker processes.
