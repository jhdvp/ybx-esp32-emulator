# 0051 — BUSY telemetry reset-ordering fix: reproduced, fixed, rebuilt, verified

Date: 2026-10-30. Lanes: batch-2 epd-lifecycle finding, live QEMU rebuild, factory guest acceptance.

Scope: the read-only BUSY edge/duration counters added by experiment 0037. The
defect affects *measurement only*: panel rendering, GT911, GPIO6 IRQ routing and
the factory UI were not changed. No firmware or media file was modified.

## 1. Defect reproduced against the compiled instrumented binary

QEMU binary before the fix (SHA-256
`35957474ea7cfcbc1e41e8ec283ec8b730cd9c1f0f9c7d1ebd5f9d7928b619cd`),
`-accel qtest`, real ESP32-S3 ROM, X4 Pro machine with `ybx-x4pro-epd=on`:

1. Advance the virtual clock 500 ms.
2. Send `0x22, 0xC0, 0x20` — `0x20` raises BUSY for the 350 ms window.
3. Advance 120 ms, then `system_reset` via QMP.

Result (`probe` output, virtual clock = 620 ms):

```
after_rise:        rise=1 fall=0 high_ms=0
busy_active:       rise=1 fall=0 high_ms=0
after_system_reset: rise=0 fall=1 high_ms=620
```

Exactly the predicted defect: the reset-driven release is counted as a fall
whose rise was erased, and `busy-high-ms` becomes `now - 0`, i.e. the whole VM
uptime. Any lane using these counters across `/api/reboot` would report a
fabricated multi-second/hours-long BUSY window.

## 2. Fix

`0051-busy-telemetry-reset-order.patch` (applies on top of 0037):

- move `busy_rise_edges/busy_fall_edges/busy_high_ms/busy_started_ms` clearing
  in `epd_reset()` to **after** `timer_del()` + `epd_set_busy(s, false)`;
- correct the 0037 comment in `epd_set_busy()`: GPIO6 is not pulled up on X4 Pro
  (`esp32s3.c` board pullups = bits 0|3|7), and `qemu_set_irq()` is already
  called unconditionally.

`apply_x4pro_telemetry_patches.sh` now applies 0043, 0037 and 0051 in that
order, with the same fail-closed `--check` / reverse-apply acceptance.

Incremental `ninja -j3 qemu-system-xtensa` inside `ybx-qemu-builder:20261029`
passed. Rebuilt binary SHA-256:
`f07744d900624ce1c34a7a62dc514092a2cc4b86c2666db90c23db42cdb42630`.

## 3. Verification after the fix

Same probe on the rebuilt binary:

```
after_system_reset: rise=0 fall=0 high_ms=0
```

`probe_x4pro_telemetry_qtest.py` was extended with this reset case (it now also
asserts IPI delivery, unrouted detection and the normal rise/fall/350 ms
window). It passes against the rebuilt binary:
`qtest_busy_reset_counters_cleared = true`, `after_reset` BUSY fields all zero.

Host suite: **311 tests, OK, 6 skipped** (run with the CrossPoint firmware
checkout present for the Wi-Fi state-machine tests).

## 4. Factory firmware acceptance on the rebuilt kernel

The main browser instance was restarted on the rebuilt binary with the genuine
factory flash and SD image. Authentic GT911 taps through `/api/tap`:

| Step | Frame | SSD1677 SHA-256 |
|:--|:--|:--|
| Home | 3 | `e79c2ad5f2b60b16fe3f20625742d5f1343f5785552d1b32c11f33c42a874672` |
| More popup | 5 | `8b81cec46b2dd5690cc2748d80d01a7ee86ca7ffbb2d57c7a92bcb68354222cf` |
| YBX Settings | 7 | `5d19930cf416a60c755aec341aaa2749d994998d241def31cbc36a2a9b6b9ba2` |
| System settings | 8 | `a8c871b0ea932bd80018b9d8a2bc1efcbf5562ebe173fff3d7732f5377fe0585` |
| Back → YBX Settings | 9 | `5d19930cf416a60c755aec341aaa2749d994998d241def31cbc36a2a9b6b9ba2` |

The state endpoint reported `factory_guest=true`, `physical_device=false`. The
back step restored the exact earlier Settings image, so the fix did not disturb
render or input behaviour.

## 5. Limits

- QTest drives MMIO directly; it is not a CPU-driven guest path. The factory
  acceptance above is the guest-level check.
- The counters are diagnostics. This does not claim physical E-Ink waveform
  timing, deep-sleep fidelity, or Wi-Fi support.
- The old binary is kept as `build-ybx/qemu-system-xtensa.pre-busyfix` for
  rollback; the pre-fix browser instance on port 8785 was stopped as superseded.
