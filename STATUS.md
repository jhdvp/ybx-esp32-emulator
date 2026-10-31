# YBX ESP32 emulator — status snapshot, 2026-10-31

This file is the consolidated result of every agent batch and every fix up to
2026-10-31. It is meant to be read instead of the individual experiment
reports; each row links to the evidence.

## The live emulator

| Item | Value |
|---|---|
| Repository | `github.com/jhdvp/ybx-esp32-emulator` (MIT tooling, GPL-2.0 QEMU patches) |
| Live QEMU tree | `~/.local/state/ybx-emulator-upstream/qemu-wf-radio-20261031` |
| Live binary SHA-256 | `f65db0a7e6940a6c08ea2ffe4e8653227e8fbea70b25f4c695fc7a2de6a68236` |
| Patch chain | 20 audited X4 Pro patches + 0042 + 0037 + 0051 + 0043 + **0068** + **0069** + **0071** (26 patches) |
| Rollback trees | `qemu-psram-fix-20261031` (`7e554ba9…`, 0068 only), `qemu-0043-agent-audit-20261030` (`f07744d9…`, pre-0068) |
| Host tests | 672 tests, 10 skipped |
| Clean-room build | reproduces the live binary byte-identically from base `febae18` |

## What the emulator can do today

| Capability | State |
|---|---|
| Boot the factory YBX image and paint | ✅ verified (Home `e79c2ad5…`) |
| Boot stock CrossPoint 2.0.0rc and 1.6.5 | ✅ verified (Home `78e743450b8f…` / `9efb6cf59096…`) |
| Boot local YBX builds | ✅ verified |
| Boot inkMOD 1.1.10 | ⚠️ starts, paints nothing (UART shows only the ROM) |
| Touch (GT911), navigation, reboot | ✅ verified end to end with frame SHA gates |
| E-Ink BUSY telemetry in the browser | ✅ `/api/telemetry`, balanced edges, 350 ms windows |
| SSD1677 deep sleep / split windows / BUSY around reset | ✅ modelled (0069), defaults unchanged |
| Guest UART console in the browser | ✅ `/api/uart` + page panel |
| Choose ROM and firmware images from folders | ✅ two in-page pickers, byte-validated, provenance marked |
| Reproducible build | ✅ composition + clean-room container build |
| Wi-Fi / 802.11 | ❌ not emulated. Stage 1 register stub exists, off by default |

## Agent batches — what was accepted, what was not

### Batch 1 — six Qwen3.8-Flash agents (0037 / 0043)
Audited in `0052-six-agent-verification-pass-20261030.md`:
acceptance-probe tooling (in use), cross-core IPI forensics (counters,
synthetic only), EPD BUSY analysis (confirmed twice; the 20 ms fallback was
right and the 30 s framing wrong), FreeRTOS TCB mapping (offsets confirmed),
QEMU EPD fix (re-run; its telemetry patch carried the reset-order defect),
render-pipeline (**rejected**, wrong controller family).

### Batch 2 — six agents (0048)
Browser resilience (in use), GUI navigation tooling (in use; its coordinates
were wrong and were corrected), reproducible build (in use), Wi-Fi radio
inventory (documentation), Wi-Fi flow (tests, skip without firmware),
E-Ink lifecycle (**found the BUSY reset-order defect — fixed as 0051**).

### Batch 3 — six lanes + the ROM/firmware pickers
browser-hardening, wifi-radio inventory, epd-waveform proposal, navigation
acceptance runner, reproducible build, FreeRTOS TCB probe (and the
orchestrator's ROM + firmware pickers after one lane died with a provider
error).

### Batch 4 — six lanes
| Lane | Result |
|---|---|
| bootloader-flash | **0068**: the SIGSEGV was a host-side PSRAM bounds bug, not the bootloader — this is what unblocked upstream firmware |
| waveform-apply | **0069**: split BUSY windows, deep-sleep state, BUSY around reset, measured on a rebuilt binary |
| guest-uart-log | **0070**: bounded guest console module |
| wifi-radio-stage1 | **0071**: opt-in radio register stub |
| repro-build-e2e | **0072**: clean-room build executed end to end |
| boot-matrix | **0073**: measured boot table tool |

## Orchestrator fixes on top of the agent work

| Fix | Why |
|---|---|
| 0051 BUSY telemetry reset ordering | a reset during BUSY recorded a phantom fall and the VM uptime |
| Boot verdict (`guest_booted`, `boot_wait_seconds`, `boot_failed`) | a non-booting ROM looked like a slow boot |
| Firmware picker + derived `factory_guest` | the bridge hardcoded `factory_guest: true`, which became a lie with custom builds |
| Byte-based image validation | an app-only image must be refused, not booted |
| Boot-matrix `--qemu-root` forwarding | the tool silently used the old kernel and blamed the images |
| Reboot fallback when the GT911 rail is parked | Restart refused and the page got stuck |
| Chain pinning + manifest (0068/0069/0071) | the audited chain must match the live binary |
| CI fixes (test source path, chain pin) | host tests failed on the runner or tested the wrong tree |
| Patch normalizer accepts plain unified diffs | 0069 ships without a `diff --git` header |

## Honest open items

1. **Wi-Fi is not emulated.** The Stage 1 stub answers its register inventory
   with `ybx-x4pro-wifi-stub=on`, but it does not unblock stock CrossPoint, and
   enabling it changes that image's early boot path; it stays opt-in and needs
   its own investigation. No scan, association, address, DHCP or TLS exists.
2. **inkMOD does not reach its UI.** It prints only the ROM lines and paints
   nothing; the cause is not yet known.
3. **Custom firmware coverage is narrow.** Three upstream builds and two local
   builds were exercised; other panels (X3, X4C, sticky, Picco) are untested.
4. **Physical E-Ink waveform, sleep/resume and power are not validated** on a
   real device — the model is host-side and QTest-side only.

## Reproduce the whole check

```sh
cd ~/Projects/YBX/ybx-esp32-emulator
python3 -m unittest discover -s tools/ybx-system-emu -p 'test_*.py' -q
python3 tools/ybx-system-emu/check_qemu_patch_composition.py verify
python3 tools/ybx-system-emu/boot_matrix.py \
  --qemu-root ~/.local/state/ybx-emulator-upstream/qemu-wf-radio-20261031 \
  --run-seconds 75 --linger-seconds 10
tools/ybx-system-emu/build_reproducible_qemu.sh --build --verify-binary \
  --qemu-src ~/.local/state/ybx-emulator-upstream/qemu-wf-radio-20261031 \
  --work-dir /tmp/ybx-repro-final --jobs 2
```
