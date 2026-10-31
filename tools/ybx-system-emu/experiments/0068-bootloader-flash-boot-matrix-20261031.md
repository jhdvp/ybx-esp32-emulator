# 0068 — X4 Pro bootloader/flash boot matrix: the SIGSEGV was a PSRAM bounds bug

Date: 2026-10-31
Lane: batch-4 `bootloader-flash` (Qoder CLI, Qwen3.8-Flash)
Base repo commit: 720496e. QEMU tree: `/Users/yaraslauhancharou/.local/state/ybx-emulator-upstream/qemu-0043-agent-audit-20261030` (HEAD `febae18`, dirty with the accepted ybx patch stack). **Nothing in the shared tree was modified**; all experiments ran in `…/bootloader-flash/.qemu-a6-copy` (a `cp -a` clone) and its `build-ybx`.

## TL;DR

- The crash does **not** follow the bootloader. The "CrossPoint-built" bootloader and the factory bootloader are **byte-identical** (`sha256` of `[0x0,0x8000)` = `6e513914db2824287dfba41d90a6bf2907bae72cdcdd5fe12b97a80bb1341533` on every merged image, ours and vendor). The firmware README's attribution is wrong.
- `expB-crosspoint-bl-factory-app.bin` equals the factory flash image except **78 erased bytes** in the nvs/otadata/phy payload windows. Swapping exactly those 78 bytes turns expB into the factory image (SHA `206ed966…` reproduced) and vice versa. Boot-vs-SIGSEGV follows those 78 bytes.
- The SIGSEGV is a **QEMU host crash**, not a guest exception and not a watchdog reset: `psram_read_data` dereferences `ptr[-1]` because `hw/misc/ssi_psram.c` bounds-checks only the upper side of a **signed, guest-driven** address (`s->addr`, an `int`, reset to `-1`, loaded with up to 4 wire bytes whose top bit can be set).
- Trigger chain: the emulated SPI controller asserts **CS0 and CS1 together** (guest `MISC=0` ⇒ neither CS disabled), so every flash transaction also clocks the octal PSRAM state machine; when otadata is erased the guest issues 32-byte page writes of `0xFF` at flash addr `0xE000` whose byte stream makes the PSRAM decode `OCT_READ_SYNC` (`0x0000`) with address `0xFFFFFFFF` → `s->addr = -1` → `ptr[-1]` → host dies. With factory-prewarmed otadata that byte stream never forms, so the factory image survived — the correlation the previous batch mistook for a bootloader difference.
- Fix: 3-hunk bounds fix in `hw/misc/ssi_psram.c` (`experiments/0068-esp32s3-bootloader-flash-fix.patch`, validated read-only against the pinned checkout with `git apply --check` ⇒ OK). After the fix **9/9 merged candidates run to timeout without crashing**, and expB/crosspoint-2.0.0rc/YBX-*-16m now reach the application layer (Arduino `Preferences` output on UART + real flash writes at 0x9000/0xe000). The live emulator container was not touched.

## 1. Image byte analysis (host, bytes only)

Windows parsed per image: bootloader header @0x0, partition table @0x8000 (stride-32 records, magic `0x50AA`), app header @0x10000, payload windows @0x9000 (nvs), @0xe000 (otadata), @0xf000 (phy).

Region SHA-256 (first 16 hex) across `/Users/yaraslauhancharou/Projects/YBX/artifacts/ybx-esp32-system-emulation-20261029`:

| image | bootloader [0,0x8000) | pt-region [0x8000,0x10000) | nvs@0x9000 | otadata@0xe000 | phy@0xf000 |
|---|---|---|---|---|---|
| `qa-20261029/s3-flash-complete-rom-test.bin` (factory) | `6e513914db282428` | `44af525172d8aa8d` | written (`fe…`) | `01000000…` | `00000000` |
| `firmware/ybx-ui-c663d6a0-on-factory-base.bin` | same | `44af5251…` | written | written | written |
| `controls/expA-factory-bl-crosspoint2-app.bin` | same | `44af5251…` | written | written | written |
| `controls/expB-crosspoint-bl-factory-app.bin` | same | `c90ed59f…` | **erased** | **erased** | **erased** |
| `firmware/crosspoint-2.0.0rc-x4pro-flash-16m.bin` | same | `c90ed59f…` | erased | erased | erased |
| `firmware/crosspoint-1.6.5-x4pro-flash-16m.bin` | same | `c90ed59f…` | erased | erased | erased |
| `firmware/inkmod-v1.1.10-x4pro-flash-16m.bin` | same | `c90ed59f…` | erased | erased | erased |
| `controls/YBX-UI/SETTINGS-…-factory-16m.bin` | same | `cef8ba71…` | erased | written (`01000000…`) | written |
| `firmware/YBX-UI-UNRELEASED-x4pro-c663d6a0.bin` | magic `0xE9` @0 but app-shaped | **magic `0x2974`** ✗ | — | — | — |

Partition table *entries* are identical in all merged shapes (nvs@0x9000/0x5000, otadata@0xE000/0x2000, app0@0x10000/0x640000, app1@0x650000, spiffs@0xC90000, coredump@0xFF0000); the `pt-region` hash differences above are **payload bytes only, not table records**. Boot header fields identical everywhere: magic `0xE9`, 3 segments, `flash_config=0x02`, `spi_pin=0x4F`, entry `0x403c8898`, chip_id `0x0009` (ESP32-S3 in the IDF image header space).

Factory-vs-expB full-image diff: **78 bytes**, in exactly these ranges `[start,end)`:
`0x9000,0x9001 · 0x9004,0x9009 · 0x901c,0x9021 · 0x9040,0x9043 · 0x9044,0x9059 · 0x9060,0x9063 · 0x9064,0x9080 · 0xe000,0xe004 · 0xe01c,0xe020 · 0xf000,0xf004` — on the expB side every one of them is `0xFF`.

Swap experiment (scratch images in `.repro-a6/`, not deliverables):
- `ab1-expB-fact78.bin` = expB with the 78 bytes restored from factory → **SHA = `206ed966…` = factory image itself**.
- `ab2-factory-blank78.bin` = factory with the 78 bytes erased → **SHA = `e0d6ac3f…` = expB itself**.

**Answer to mission question 2 up front: the CrossPoint-built bootloader expects nothing extra from the flash that the emulator fails to provide — because there is no separate CrossPoint bootloader. All candidate images ship the same 0.4.1-era vendor second-stage bootloader; what differed was the factory data payload in the nvs/otadata windows.**

## 2. Reproduction (isolated QEMU copy)

```
cp -a /Users/yaraslauhancharou/.local/state/ybx-emulator-upstream/qemu-0043-agent-audit-20261030 \
      $W/.qemu-a6-copy          # 608 MB, $W = this worktree
```
Guest command replicates the bridge exactly (`tools/ybx-system-emu/serve_x4pro_browser.py:485-500`, ROM per `start_x4pro_browser.sh:82-86` = `qa-20261029/s3-full-rom-generator-test.bin`, SHA `5b950be9…` verified; flash/SD copied to a writable dir first because QEMU opens mtd/sd drives RW):

```
docker run --rm --platform linux/arm64 \
  -v $W/.qemu-a6-copy:/qemu:ro -v $QA:/work:ro -v $FW:/fw:ro -v $W/.repro-a6:/out \
  --entrypoint /bin/bash ybx-qemu-builder:20261029 -c '… timeout 30 \
  /qemu/build-ybx/qemu-system-xtensa -machine esp32s3,ybx-x4pro-i2c=on,ybx-x4pro-gt911=on,\
ybx-x4pro-sd-power=on,ybx-x4pro-spi2=on,ybx-x4pro-epd=on -m 8M -global ssi_psram.is_octal=on \
  -nographic -monitor none -serial file:… -serial null -serial null -serial null -L /out/rom \
  -drive file=…,if=mtd,format=raw -drive file=…,if=sd,format=raw'
```

Results on the **unfixed** binary (pristine, via `:ro` mount of the shared tree for the A/B pair):

| image | exit | UART |
|---|---|---|
| factory `s3-flash-complete-rom-test.bin` | 124 (alive at timeout) | ends `entry 0x403c8898` |
| expB (cp-bl + factory app) | **139 SIGSEGV** | same ROM lines, dies right after bootloader entry |
| expA (factory-bl + cp2 app) | 124 | ends `entry 0x403c8898` |
| `ab1-expB-fact78.bin` (= factory SHA) | 124 | ends `entry …` |
| `ab2-factory-blank78.bin` (= expB SHA) | **139 SIGSEGV** | dies same place |

The A/B pair is the decisive control: **same binary, same machine flags; only 78 payload bytes decide host survival.**

## 3. Where the crash happens: QEMU host crash, `psram_read_data`

gdb batch session (derived image `ybx-qemu-builder-gdb:20261031` = builder + apt gdb; my copy only):

```
Thread 3 received signal SIGSEGV, Segmentation fault.
#0  psram_quad_read (s=<optimized out>) at ../hw/misc/ssi_psram.c:154   # inlined psram_read_data
#1  psram_transfer (dev=…, value=…) at ../hw/misc/ssi_psram.c:440
#2  ssi_transfer (bus=…, val=255) at ../hw/ssi/ssi.c:165
#3  esp32s3_spi_txrx_buffer (s=…, tx_bytes=32, rx_bytes=0) at ../hw/ssi/esp32s3_spi.c:148
#4  esp32s3_spi_perform_transaction (…) at ../hw/ssi/esp32s3_spi.c:196
#5  esp32s3_spi_special_command (…) at ../hw/ssi/esp32s3_spi.c:368     # CMD_PP branch :312-325
#6  esp32s3_spi_write (…) at ../hw/ssi/esp32s3_spi.c:387
… #16-#19 TCG store helper, guest MMIO write to 0x60002000 (SPI1 CMD reg)
pc  psram_transfer+512; x1=0x800000 (size_bytes), x3=0xffff6c200000 (psram ptr), x4=0xffffffffffffffff (destination = -1)
```
At the fault, the SPI state showed `misc=0` (both `CS0_DIS` and `CS1_DIS` cleared — `include/hw/ssi/esp32s3_spi.h:148-149`), `mem_addr=0xE000` (otadata!), a 32-byte MOSI user write. So the guest was initializing the erased otadata through flash CS0 while `esp32s3_spi_cs_set` (`hw/ssi/esp32s3_spi.c:161-177`, called at `:192` and `:197-198`) also asserted CS1 — where the octal PSRAM hangs off SPI1 (`hw/xtensa/esp32s3.c:307-318`, `cs=1`). The `0xFF` data bytes were clocked into the PSRAM state machine.

## 4. Root cause in `hw/misc/ssi_psram.c` (upstream code, unmodified in this tree)

- `SsiPsramState.addr` and `byte_count` are **signed `int`** (`include/hw/misc/ssi_psram.h:28-29`); after every CS deselect `psram_cs` resets `command=-1, addr=-1` (`hw/misc/ssi_psram.c:448-463`).
- The octal command table treats `0x0000` as the valid `OCT_READ_SYNC` command (`:41`), so stray `0x00` wire bytes from a flash transaction arm a read; the following four `0xFF` address bytes accumulate (`:330-344`) into `addr = 0xFFFFFFFF` = **-1 as `int`** (`:332-344`).
- `psram_read_data`/`psram_write_data` (`:133-141`, `:148-157`) compute `off_t destination = s->addr + offset` and check **only** `destination < size_bytes`. `-1 < 0x800000` passes ⇒ `ptr[-1]` ⇒ the host SIGSEGV at `:154` (registers above confirm `destination=-1`).
- The same signed value is fed to `memory_region_set_dirty(&s->data_mr, s->addr, s->byte_count)` at `:453-456`, an out-of-range dirty-bitmap write (`system/memory.c:2274-2281` does not bounds-check).

Why the factory images never hit it: their otadata already contains `0x01000000…`, the guest performs no erased-page writes at `0xE000`, and no `0x00…0xFF` byte stream forms an OCT_READ_SYNC-with-negative-address. It is a **content-dependent host bounds bug**, not a flash-size/eFuse/secure-boot/format mismatch — the images do not "need a real flash size the emulator cannot present"; 16 MiB, layout, mode and image format were all identical and acceptable.

## 5. Fix and verification matrix

`experiments/0068-esp32s3-bootloader-flash-fix.patch` (3 hunks, `hw/misc/ssi_psram.c`):
treat the wire address as unsigned (`(uint32_t)s->addr`) in `psram_read_data`/`psram_write_data` so the existing upper-bound check also rejects the bogus values (they become ≥ size_bytes → read 0 / drop the write, matching the existing "above end" behaviour); clamp the deselect-time dirty range to the region. No behaviour change for legal accesses.

Validation (real output):
```
git -C …/qemu-0043-agent-audit-20261030 apply --check …/experiments/0068-esp32s3-bootloader-flash-fix.patch
   → (silent) APPLY_CHECK_OK            # shared tree NOT touched, only --check
ninja -j2 qemu-system-xtensa            # in .qemu-a6-copy/build-ybx → [4/4] Linking target OK
```

Matrix on the **fixed** build (my copy), `timeout 15`/`20`/`45` per row; exit 124 = still running = healthy; UART classes measured with `-serial file:`:

| image | before (unfixed) | after (fixed) | UART after | payload state |
|---|---|---|---|---|
| factory | 124 | 124 | `…entry 0x403c8898` (app runs, silent on UART0; paints per bridge) | prewarmed |
| `ybx-ui-c663d6a0-on-factory-base.bin` | 124 | 124 | silent (app runs) | prewarmed |
| expA (factory-bl + cp2 app) | 124 | 124 | silent | prewarmed |
| expB | **139** | 124 | `[  6827][E][Preferences.cpp:47] begin(): nvs_open failed: NOT_FOUND` | erased-base |
| crosspoint-2.0.0rc-16m | **139** | 124 | same Preferences errors | erased-base |
| crosspoint-1.6.5-16m | **139** | 124 | silent | erased-base |
| inkmod-v1.1.10-16m | **139** | 124 | silent | erased-base |
| YBX-UI-…-c663d6a0-factory-16m | **139** | 124 | Preferences errors | nvs-erased-ota-written |
| YBX-SETTINGS-…-092c2be5-factory-16m | **139** | 124 | Preferences errors | nvs-erased-ota-written |
| `ab2-factory-blank78.bin` | **139** | 124 | Preferences errors | erased-base |

9/9 crash cases no longer crash; five of them now demonstrably run the **application** (Arduino component logs) and the guest performs real flash writes through the m25p80 model — post-run flash files changed at blocks `0x9000, 0x9040, 0xe000` (expB/cp2) vs `0x9000, 0x9080` (factory) — evidence the write path works and the app reached NVS init. None of these runs measured an e-ink frame: frame capture is the bridge's job and the live container was not touched (see §8).

## 6. Host test

`tools/ybx-system-emu/test_x4pro_flash_boot_matrix.py` (stdlib only):
- `TestBootloaderByteIdentity` — pins the single-bootloader fact (region SHA, boot header fields `chip_id=0x0009`/`entry=0x403c8898`, exact 6-entry partition matrix, `app0@0x10000`).
- `TestSeventyEightByteCorrelation` — recomputes the 78-byte diff ranges and the SHA reproduction both ways (this is the test that would have prevented the misattribution).
- `TestClassificationMatrix` — per shipped candidate: classification (`merged` / `rejected:partition-table-magic` for the app-only bin), payload state, documented measured behaviour class; pins that no record uses extended flags bytes `rec[28:32]==0`.
- `TestSyntheticFixtures` — byte-shape rules always runnable (no QA media needed): build/parse/reject synthetic images, bad partition magic, bad app magic, truncated file, payload states.
Artifacts-dependent classes skip with `YBX_QA_ARTIFACTS` override; runtime with all artifacts present: `Ran 14 tests in 0.112s OK`. Full suite gate: `Ran 517 tests in 18.241s OK (skipped=11)`.

## 7. Corrected boot matrix for the firmware README

The current `firmware/README.txt` (read-only for us) should be corrected by its owner when convenient:
- "crashes QEMU (SIGSEGV) … the crash follows the BOOTLOADER" → **wrong**; reclassify as: all 9 merged images are bootable-shape, identical bootloader; with the 0068 patch none crashes. The old crash set was the `nvs/otadata-erased` set.
- `ybx-ui-c663d6a0-on-factory-base.bin` and `ybx-settings-092c2be5-on-factory-base.bin` appear in BOTH "boots and paints" and "hangs before app start" lists — a doc bug.
- "not a flash image" for `YBX-UI-UNRELEASED-x4pro-c663d6a0.bin` stays correct and is now pinned by the test.

## 8. Orchestrator runbook (apply the fix to the live build)

```
SHARED=~/.local/state/ybx-emulator-upstream/qemu-0043-agent-audit-20261030
git -C $SHARED apply --check <…>/experiments/0068-esp32s3-bootloader-flash-fix.patch && \
git -C $SHARED apply <…>/experiments/0068-esp32s3-bootloader-flash-fix.patch
docker run --rm --platform linux/arm64 -v $SHARED:/src -w /src/build-ybx \
  ybx-qemu-builder:20261029 ninja -j2 qemu-system-xtensa        # incremental, ~2 min in our run
# then restart ybx-x4pro-browser-8784 via start_x4pro_browser.sh and pick expB/cp2 in the firmware picker
```
Re-verify with: `docker run … ybx-qemu-builder:20261029 -c '… -drive file=expB…,if=mtd …; echo exit=$?'` expecting 124 instead of 139 (this lane's copy commands in §2). Acceptance frame SHA `e79c2ad5…` is unchanged for the factory image; a frame from the patched crash-class images was **not** captured (no bridge restart from this lane).

## 9. Risks, limits, open questions

- **Unproven:** frames/paint from the newly-surviving images (expB/cp2/YBX-*-16m) end-to-end in the browser bridge; `guest_booted` semantics there are log/property-based. Expect `nvs_open failed` noise, and NVS-dependent UI state, on the erased-base images until their nvs is initialized (they do initialize it — the guest wrote NVS blocks).
- **cp165 / inkmod / expA remain silent** at UART even after the fix (up to 45 s) — same class as the old "factory bootloader + foreign app stops before app start". This is application-side (IDF/PSRAM/config expectations of those app builds), orthogonal to the host crash; NOT investigated here.
- The CS0+CS1 simultaneous selection (`MISC=0` from the guest during flash ops) is *why* PSRAM saw the garbage stream; we fixed the crash, not that routing question. A follow-up could gate transactions by active CS in `esp32s3_spi_perform_transaction` (real hardware: `MISC` CS bits are usually managed per-operation by IDF). Marked as open, deliberately out of this minimal patch.
- gdb runs happened in an ad-hoc derived docker image (`ybx-qemu-builder:20261029` + apt `gdb`) from this lane's scratch; no shared artifact changed. The `.qemu-a6-copy` and `.repro-a6` scratch directories were deleted after the run (rebuild recipe is §2/§8).
- The `(uint32_t)` cast semantics assume little-endian host addressing, matching the file's own existing assumption (`esp32s3_spi.c:205-207` comment). On any supported QEMU host this holds; upstream-worthy as-is.

## Appendix — commands log (chronological)

1. region/full-image SHA + partition parsing (host python, §1 outputs above).
2. `cp -a` of the QEMU checkout; ROM/flash SHA verification (`5b950be9…`, `206ed966…` — both match the lane brief).
3. bridge-exact `qemu-system-xtensa` runs in docker (factory/expB/expA) — §2 exit table.
4. gdb batch `run; bt 25; info registers` on expB — §3 stack.
5. instrumented build (`PSRAM_WARNING=1`, `SPI1_DEBUG=1`, OOR logging): expB survives (124), `[PSRAM-OOR] cmd=0x0 state=7 addr=-1 count=1 off=0 dest=-1 READ`, `[OCT PSRAM] Unsupported command 0x0000`, `SPI1-TXN … addr=0x809000 …`.
6. A/B swap images built on host; run on **shared** binary `:ro` → 124/139 both directions.
7. minimal fix applied to the copy; `ninja -j2 qemu-system-xtensa` → link OK; 9-image matrix; flash writeback block diff; 45 s long runs for cp165/inkmod/expA.
8. `git diff hw/misc/ssi_psram.c > experiments/0068-esp32s3-bootloader-flash-fix.patch`; `git -C $SHARED apply --check` → OK (shared tree untouched).
9. `python3 -m unittest discover -s tools/ybx-system-emu -p 'test_*.py' -q` → `Ran 517 tests … OK (skipped=11)`.
