# YBX ESP32 emulator — status snapshot, 2026-11-01

This file is the consolidated result of every agent batch and every fix up to
2026-11-01. It is meant to be read instead of the individual experiment
reports; each row links to the evidence.

## The live emulator

| Item | Value |
|---|---|
| Repository | `github.com/jhdvp/ybx-esp32-emulator` (MIT tooling, GPL-2.0 QEMU patches) |
| Live QEMU tree | `~/.local/state/ybx-emulator-upstream/qemu-wf-radio-20261031` |
| Live binary SHA-256 | `f65db0a7e6940a6c08ea2ffe4e8653227e8fbea70b25f4c695fc7a2de6a68236` |
| Patch chain | 20 audited X4 Pro patches + 0042 + 0037 + 0051 + 0043 + **0068** + **0069** + **0071** (26 patches) |
| Rollback trees | `qemu-psram-fix-20261031` (`7e554ba9…`, 0068 only), `qemu-0043-agent-audit-20261030` (`f07744d9…`, pre-0068) |
| Host tests | 832 tests, 10 skipped (with the companion firmware artifacts present) |
| Clean-room build | ✅ verified 2026-10-31: the 26-patch chain built from base `febae18` reproduces the live binary byte-identically (`f65db0a7…`, `rc=0`) |

## What the emulator can do today

| Capability | State |
|---|---|
| Boot the factory YBX image and paint | ✅ verified (Home `e79c2ad5…`) |
| Boot stock CrossPoint 2.0.0rc and 1.6.5 | ✅ verified (Home `78e743450b8f…` / `9efb6cf59096…`) |
| Boot local YBX builds | ✅ verified |
| Boot inkMOD 1.1.10 | ⚠️ starts, paints nothing; stop point traced to the IPC/RTC sleep handshake |
| Touch (GT911), navigation, reboot | ✅ verified end to end with frame SHA gates |
| E-Ink BUSY telemetry in the browser | ✅ `/api/telemetry`, balanced edges, 350 ms windows |
| SSD1677 deep sleep / split windows / BUSY around reset | ✅ modelled (0069), defaults unchanged |
| Guest UART console in the browser | ✅ `/api/uart` + page panel |
| Choose ROM and firmware images from folders | ✅ two in-page pickers, byte-validated, provenance marked |
| Reproducible build | ✅ composition + clean-room container build |
| Wi-Fi / 802.11 | ❌ not emulated. Stage 1 register stub exists, off by default; measured to change nothing at boot |
| Virtual SD card contents | ✅ FAT16 card read back out-of-band; library index and EPUB cache verified |
| Scripted CrossPoint navigation | ✅ measured tap plan + frame-SHA acceptance runner |

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

### Batch 6 — 2026-11-01, in flight
| Lane | Result |
|---|---|
| library-browse2 (re-queued from batch 5) | **0092**: FAT16 test-card builder whose output is verified by parsing it back, plus browse-gate thresholds pinned to the sector counts 0084 measured. Arrived as `pytest`-style functions invisible to the repo's unittest gate — ported to `TestCase`, pristine check made skippable, BPB geometry test added |
| inkmod-cause2 | running — sleep/IPC stop point turned into a ranked, falsifiable hypothesis list |
| wifi-narrow | running — builds 0081's narrowing patch in a private tree, re-runs the boot matrix on both kernels |
| nav-acceptance, panel-support | queued behind the running lanes |

### Batch 5 — twelve lanes (2026-10-31 → 11-01)
| Lane | Result |
|---|---|
| wifi-stub-effect | **0081**: refuted the brief's own claim — the stub absorbs exactly one word (`0x60006090`, PHY/RTC bring-up) and returns the same `0` as the catch-all, so it cannot change the boot outcome. Narrowing patch proposed, **not applied** to the shipped kernel |
| crosspoint-nav + cp-nav-runner | **0082**: six-step CrossPoint 2.0.0rc tap plan gated on the real frame SHA, with an HTTP acceptance runner that requires an explicit `--base-url` |
| panel-matrix | **0083**: every published asset judged by its bytes — the `x3-x4` images are **ESP32-C3** (`chip_id 0x0005`) and cannot boot on our ESP32-S3 machine; the S3 siblings differ only in panel wiring |
| sd-storage | **0084**: reads the virtual card back out-of-band — the factory image writes only `/.crosspoint/library.idx`, CrossPoint really parses an EPUB and caches it (`book.bin`, `sections/0.bin`, `progress.bin`), the pristine image never changes |
| sleep-wake + sleep-acceptance | **0085 / 0091**: the lane's probe had never run and could not (`NameError`, wrong QOM path, a `… or True` verdict), and its "acceptance" suite asserted on its own fixture. Both artefacts **removed**; the same behaviour was then measured live with `probe_x4pro_epd_waveform_qtest.py` against `f65db0a7…` — exit `0`, sleep/wake assertions green, transcript in `0085-sleep-wake-live-20261101.json` |
| inkmod-hang | register-level tracer for the paint-less inkMOD boot: CPU1 polls `RTC_CNTL_INT_RAW` for SLP_WAKEUP/SLP_REJECT while CPU0 is parked in `esp_ipc_isr_waiting_for_finish_cmd`, panel sees zero commands. Probe + 24 tests merged, the lane was killed before writing its report |
| panel-support, inkmod-fix, wifi-stage2 | **no output** — exited `rc=1` (quota/provider), nothing to review |
| library-browse | killed mid-run with a 575-line untested tool; **held out of the repo**, task re-queued |

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
   with `ybx-x4pro-wifi-stub=on` and stays opt-in. 0081 measured that it
   changes nothing observable at boot for CrossPoint or the factory image, so
   the earlier "the stub hangs the guest earlier" note was wrong and is
   retired here. No scan, association, address, DHCP or TLS exists.
2. **inkMOD starts but paints nothing.** 0081 established that the "only ROM
   lines" signature belongs to inkMOD, not to the radio stub, and
   `probe_x4pro_inkmod_trace.py` pinned the stop point: CPU0 waits for a
   cross-core IPC finish command that never arrives while CPU1 polls the RTC
   sleep bits. That is a signature, not a fix — the register model that should
   answer it is not built yet (batch-6 `inkmod-cause2`).
3. **The 0081 narrowing patch is unapplied.** The shipped stub still claims
   4 KiB windows; the tighter measured ranges need their own build, boot
   matrix and acceptance-SHA re-check before they can enter the chain.
4. **Physical E-Ink waveform, sleep/resume and power are not validated** on a
   real device — the model is host-side and QTest-side only (0085 records the
   host-side sleep/wake measurement that exists today).

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
