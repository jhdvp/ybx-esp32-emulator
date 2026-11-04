# 0114: Stock 1.6.14 actual YBX EPUB — page turning on original CrossPoint

Date: 2026-11-04. **Only isolated QEMU 11.1.2**, original immutable X4 Pro firmware 2.0.0 RC, real YBX account paired from browser. The EPUB was fetched into **isolated** FAT16 guest media, not committed to Git. No firmware modifications, no staging or production server changes.

## What was verified

- Original YBX Stock 1.6.14 plugin displayed real QR, paired, populated eight real library roots, and downloaded a real 905,311-byte EPUB with 55 ZIP members and valid CRC. The original reader opened it to its cover, and after a cold isolated-container reboot showed it in Recently Read.
- Previously the browser/QMP GT911 mouse "/api/tap" sometimes returned `accepted:true` without a visible page advance. This was an **emulator input problem**. Inspecting the upstream QEMU 11.1.2 QOM exposed `/machine/x4pro-keys` with actual `up`, `down`, `power` GPIO properties.
- The **real firmware itself** rendered the EPUB text and advanced through different pages on a native physical `down=true` / `down=false` key pulse of 50ms. No new firmware was built.
- Live checks: initial 1 page-down succeeded; next **4 down + 1 up** were 5/5 visual E-Ink screen changes; a final **3 down/up/down** checked using new strict driver were 3/3 visual frame changes. Firmware UART contained no `multi_heap_free`, corrupt heap or panic signals in that run.
- Evidence kept **locally only** in `~/Projects/YBX/artifacts/ybx-stock1614-clippings-qa-20261104/preview-stock-plugin-8950/physical-page-turn-results.json`, `native-page-turns-verification.json` and screenshots. No private book pages/tokens/QR codes in this PR.
- Added `StockClippingsGuest.physical_page_key` (native QOM hardware GPIO) with mandatory key release in `finally`, strict key/duration allowlist, and `verify_physical_page_turns()` (poll original screen to prove change), plus regression tests.

## Caveat

This verifies that **the tested actual EPUB renders and pages normally** on unmodified Stock in QEMU and that the previously observed touch ambiguity is not a reader crash. It does not prove every possible EPUB is safe. It also does not prove live Yandex quote synchronization: server-side changes are still in the separate [PR #46](https://github.com/jhdvp/xteink-yandex-books/pull/46) and need an opt-in live acceptance test.
