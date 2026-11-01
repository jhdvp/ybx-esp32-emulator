# 0104 — Native Factory/CrossPoint/inkMOD acceptance and VFAT regression fix

Date: 2026-11-02. Tested with independently rebuilt QEMU `qemu-system-xtensa` SHA-256 `7b729fba1ceefdc67a55dc9ddd6d359699e16174a52d741ea243849c35e2accc`, using **private** browser bridges `127.0.0.1:8797`, `:8798`, `:8799` and disposable FAT16 cards. Main/shared browser `192.168.10.239:8784` was never touched.

## Native Factory GT911 end-to-end, with guest I²C acknowledgements

`run_x4pro_guest_navigation.py` in its **native** mode launched the original Factory X4 Pro firmware on the new QEMU in a private Linux/arm64 Docker run; sequence `home-more-settings-system-settings-back`, with accepted frame SHA gates: **4/4 navigation steps PASS**. Real GT911 guest `I2C point reads` and `status clears` were observed; `guest_gt911_i2c_consumption_observed=true`, `full_acceptance=true`, no panic. The original Factory home image and visible frame SHA remained unchanged. Evidence: `artifacts/ybx-esp32-system-emulation-20261029/acceptance-0104-20261102/native-factory/factory-native-nav.json`.

## Stock CrossPoint 2.0.0rc menus and FAT16 persistence

The original CrossPoint 2.0.0rc firmware SHA `2be70081d645ab1df57e28319f010557e243ef4e3eb06190731f56f91f981bbe` was exercised on private `:8797`, selected by exact firmware SHA. Every accepted step had a real SSD1677 panel repaint, increasing frame counter and expected SHA, and per-step PNG rehashed against live framebuffer:

- `home-tour`: **6/6**, starting Home, opening Browse, returning, entering Library, returning, opening Settings
- `browse-files-and-back`: **2/2**
- `library-and-back`: **2/2**
- `settings-and-back`: **2/2** when correctly invoked **after** `library-and-back` without a reboot. Two earlier negative tests attempted `--reboot-first` even though this sequence's required precondition is `home-library-selected` SHA `4b3ab700…`; those correctly failed closed with `initial_frame_not_reached_after_reboot` and did not inject any taps. The failure was test orchestration, not a runtime regression.

The CrossPoint browser channel proves SSD1677 navigation, **not guest GT911 I²C consumption**; its JSON correctly says `full_acceptance=false` because that per-step telemetry is unavailable in HTTP mode. The Factory native acceptance above proves physical guest-side I²C consumption in its own distinct scenario, not every CrossPoint browser step.

The guest had created `/.crosspoint/library.idx` on the original pristine FAT16 card (5 changed sectors). The same private guest was **rebooted five times**; all 5 Home frames matched original SHA `78e74345…99c296`; its already-written guest SD image remained **byte-identical** across all five reboots (the pristine input never modified). Two FAT copies remained identical; no files were lost.

## Real blocker discovered in the host-generated test-library SD card

The earlier `x4pro_test_card.py` host-only builder claimed to produce a usable FAT16 test card, but the parser `read_fat16_tree()` simply trusted the LFN records it wrote. Independent **`fsck.fat -n -v`** on the extracted 32/128MB FAT16 partition revealed invalid long-file-name checksums and nonzero *first cluster* bytes in **all** LFN records; the guest could mount and write an empty 512-byte library index but did not find the test books. An invalid card made guest acceptance misleading.

Root cause: `_checksum()` used a 16-bit rotate-right algorithm instead of VFAT's **8-bit** checksum. `_lfn_records()` wrote that checksum into **bytes 26–27** (reserved first-cluster field, which must be zero) instead of the single-byte **offset 13**. It also mismanaged exact multiples of 13 UTF-16 units and surrogate pairs. The card's generated default FAT16 partition was 32MiB, whereas the reference X4 Pro QA card is **128MiB** (partition starts LBA 2048, BPB 131072 sectors, 32695 clusters).

Fixed: checksum 8-bit at offset 13; first-cluster 0, type 0, 13-unit correctly padded and ordered LFN records; use full 128MiB guest card geometry by default. Added tests that independently calculate the VFAT checksum and inspect reserved fields, exact 13-unit names and surrogate pairs, plus a 128MiB capacity check. Independent `fsck.fat -n -v` then exited **0** with no diagnostics, seeing six files / five clusters.

### Actual guest acceptance with corrected FAT16

The corrected card contains original locally authored `notes.txt`, `readme.md`, `Field Notes/chapter.txt`, and a small synthetic original EPUB `The Prophet.epub`. The corrected pristine 128MiB card has SHA `f81d1ef74dd451839ced784b14555957ce19b7b515f2e3864106af2abe993d3f`, produced entirely in a disposable artifacts folder and validated by the independent `fsck.fat` parser.

On private `:8799`, original Factory's Library action detected real content and created a **2170-byte** `/.crosspoint/library.idx` (the previous malformed card produced only a 512-byte empty index). Original CrossPoint 2.0.0rc then displayed a changed Browse Files frame and a real native GT911 tap at `(240,326)` opened the EPUB, producing new SSD1677 frames and the following actual guest SD cache files:

`/.crosspoint/epub_2711421234/book.bin` (138 bytes), `css_rules.cache` (4 B), `html/0.html` (50 B), and the `sections/` and `html/` directories, while retaining `library.idx` (2170 B). No source files were removed/changed; two FAT copies stayed identical and the MBR/BPB did not change.

With the **same persistent guest SD copy**, 3 subsequent QEMU reboots all returned to the expected CrossPoint Home frame (SHA `78e74345…99c296`), and the guest SD image stayed **byte-identical across the three reboots** with cache intact. Final independent **`fsck.fat -n -v` exited 0**, showing 14 files, 14/32695 clusters, no errors and unchanged original-file hashes. Artifacts: `crosspoint-epub-cache-3-reboots.json` and guest on-card FAT report in `/tmp/ybx-0104-epub-after-reboots.json` (private test container).

## Three controlled inkMOD wakes (separate fresh firmware reloads)

Original inkMOD v1.1.10 was reselected on the private :8797 bridge **three separate times**. Each time, the guest was first given six seconds to arm RTC EXT1 sleep (an earlier deliberately too-early test pressed Power *before* RTC was configured and correctly produced no wake). Each cycle verified 0 EPD frames before pressing Power, one ROM `rst:0x5 (DSLEEP)` after, real Home SSD1677 SHA `8955f0f2…e3266`, at least 3 EPD frames, `power.pressed=false` after watchdog release, no UART panic or `ESP_ERR_INVALID_STATE`, and guest SD byte-identical after all cycles. **3/3 PASS**. This proves 3 separate clean-boot Power wakes; it is **not** three consecutive deep-sleep reentries in one running firmware session.

## Scope and limits

No network or Wi-Fi MAC/DHCP/TLS was emulated by these tests. RTC EXT0/timer/analog IO mux, deep sleep power gating and actual physical buttons remain incomplete. Browser HTTP over LAN has no authentication; never claim full hardware behavior or deploy the experimental Power endpoint on 8784 without an explicit security decision. Test captures do not give OS-level 'all SDMMC writes are always durable' guarantees under unclean host power loss. All tests used private firmware/SD copies and did not alter 8784. Stable QEMU promotion requires a separately controlled, rollback-capable rollout and explicit post-cutover health probes.
