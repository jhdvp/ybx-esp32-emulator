# Validated QEMU telemetry integration — 2026-10-30

Scope: diagnostics only. Neither ESP32-S3 firmware nor the original QEMU
binary currently serving port 8784 has been modified.

## Isolated build with both first-batch candidate patches

The QEMU source and build were copied from
`qemu-rominit-20261029` to a separate isolated directory
`~/.local/state/ybx-emulator-upstream/qemu-0043-agent-audit-20261030`.
Agent experiment IPI counter patch 0043 and EPD BUSY waveform/edge telemetry
patch 0037 passed `git apply --check` and were applied **only** in that copy.
The Docker offline incremental `ninja -j2 qemu-system-xtensa` build passed.

Test binary SHA256: `35957474ea7cfcbc1e41e8ec283ec8b730cd9c1f0f9c7d1ebd5f9d7928b619cd`.
Original currently-active binary SHA256:
`4106cff2795eaaaab79d0cf074aba0fa4acbc98e9ea520e93f5721f2aa07e7be`.

## QTest verification

- Existing GPIO6 PCPU_INT W1TC-ack and SSD1677 tests PASS:
  `s3-batch2-0043-telemetry-qtest.json`.
- New direct QMP/QTest `probe_x4pro_telemetry_qtest.py` PASS:
  `s3-batch2-0043-telemetry-counters-qtest.json`.
- FROM_CPU source79 mapped to Xtensa CPU interrupt **5**:
  clock set 0→1, clear 0→1; matrix writes 0→1;
  FROM_CPU asserted 0→1 and **unrouted remains 0**.
- Intentionally mapping to CPU interrupt **16**, which is not an external
  interrupt in the S3 core, increments the **unrouted** counter. This corrects
  a subtle interpretive mistake in the agent's prose: `from-cpu-asserted`
  counts source assertions, **including unrouted**, so do not interpret it
  alone as proof of CPU delivery.
- SSD1677 power-on (not a pixel refresh) produces BUSY rise 0→1,
  later fall 0→1 and `busy-high-ms=350` with the default model. It does
  **not** increment rendered frames.
- These are synthetic QTest MMIO actions, NOT firmware GUI acceptance.

## Original, unmodified factory guest acceptance

The isolated instrumented binary also passed the native 33-second boot
and 150ms More touch with 2-second follow-up:
`s3-batch2-0043-native-more-2s.json`, exact factory flash SHA256
`206ed9661bd71477d1a54397c5a2cfe490179a3f59ab094f1e7421745c609523`.
Native panel frames 4→5, SHA
`e79c2ad5f2b60b16fe3f20625742d5f1343f5785552d1b32c11f33c42a874672` →
`8b81cec46b2dd5690cc2748d80d01a7ee86ca7ffbb2d57c7a92bcb68354222cf`,
true factory GT911 point read/ACK, and
`true_guest_touch_to_ui_verified=true`.

## Decision

**ACCEPT** both agent proposals as optional instrumentation for further
ESP32-S3 / SSD1677 forensics. This does not prove or create Wi-Fi support,
full touch navigation, physical E-Ink waveform accuracy or lifecycle fidelity.
Keep the production-running port 8784 on its existing binary until a
controlled, reversible switch is appropriate.

`apply_x4pro_telemetry_patches.sh --check|--apply QEMU_CHECKOUT` provides
a fail-closed route to reproduce the same opt-in source changes on a checkout
that already contains the tested 0042 GPIO PCPU_INT fix. Applying source
patches alone does not change the running QEMU binary.

Additional regression: `python3 -m unittest discover -s tools/ybx-system-emu -p 'test_*.py'`.

## Separate browser integration

Two instances are deliberately independent:
- Stable baseline: `192.168.10.239:8784`, original QEMU binary.
- Telemetry preview: `192.168.10.239:8785`, isolated rebuilt binary.

Via 8785 HTTP `/api/tap`, actual native GT911 touches
(350,760) then (240,453) generated
Home → More → real **YBX Settings** screen. Panel updates:
frame 4 SHA `e79c2ad5f2b6...` → frame 5 SHA `8b81cec46b2d...`
→ frame 7 SHA `5d19930cf416a60c755aec341aaa2749d994998d241def31cbc36a2a9b6b9ba2`.
The last image visibly contains the factory YBX Settings UI:
My Account, Sources, Synchronization, Appearance, Language, Sleep Screen,
Check For Updates, Support and System settings.
Evidence: `s3-batch2-0043-browser-home-more-settings.png` and its
`s3-batch2-0043-browser-home-more-settings-state.json` in the local QA
directory, NOT newly invented web UI pixels. The browser uses real
SSD1677 RAM and actual firmware UI.

The second LAN endpoint is for local development, currently unauthenticated;
neither endpoint should be exposed to the internet.

### Further real guest Settings navigation

A third authentic GT911 tap (210,679) advanced the instrumented guest
from YBX Settings (frame 7, SHA 5d19930cf416a60c755aec341aaa2749d994998d241def31cbc36a2a9b6b9ba2) into the factory
System settings page (frame 8, SHA a8c871b0ea932bd80018b9d8a2bc1efcbf5562ebe173fff3d7732f5377fe0585). The rendered page
contains System, Display, Reader, Controls, Library, Connectivity.
A native Back touch at (35,61) advanced to frame 9 and
**restored the exact earlier YBX Settings SHA** (5d19930cf416a60c755aec341aaa2749d994998d241def31cbc36a2a9b6b9ba2).
Local evidence: `s3-batch2-0043-browser-multistep.json`,
`s3-batch2-0043-browser-system-settings.png` and
`s3-batch2-0043-browser-back-from-system-settings.png`.
These results only apply to the isolated instrumented 8785 guest.
