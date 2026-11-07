# 0113 — Stock 2.0 RC: original QEMU guest clipping + KoSync twice

2026-11-04. Original unchanged CrossPoint X4 Pro release `2.0.0-rc+1070912`, official pinned eink-qemu 0.9.2 / QEMU 11.1.2. **No custom firmware**, no production host `8784`, no physical reader, no real Yandex account. Only synthetic public EPUB and local fixture.

## Isolated reproduction

- Fresh Docker container: `ybx-eink-qemu11-8940`, preview `127.0.0.1:8940`, QMP `127.0.0.1:8941`, new media under `~/Projects/YBX/artifacts/ybx-stock1614-clippings-qa-20261104/preview-stock-new-8940`.
- Original test SD **already had** valid synthetic `/.crosspoint/koreader.json`: `http://10.0.2.2:18085/kosync`, enabled `syncClippings`, binary-matching config. Re-checking the FAT16 media disproved the earlier hypothesis of missing KOSync credentials.
- QEMU's `10.0.2.2` is the QEMU user-NAT gateway **inside its Docker container**, not the Mac host. With a fixture only on macOS, guest reported network connection resets and `Could Not Sync Clippings`. Put `stock1614_fixture_server.py` and synthetic EPUB in the QEMU container (`/out`) and start its HTTP fixture on `0.0.0.0:18085`. Its `/__status` replied 200 from inside the container.
- When QMP `system_reset` reset the firmware, the second Wi-Fi picker intermittently ignored **all** accepted GT911 clicks (including Back); issue #20 remains relevant. A **cold Docker restart of only container 8940** cleared the emulator's touch state while keeping guest media. A cold reboot does not constitute a change to the firmware.

## Genuine guest evidence (no mocked HTTP caller)

The original firmware's UI opened the fixture EPUB, selected and saved the word `different`, then showed `View Clippings`. After a cold reboot, original Stock emitted to its same-container mock KOSync server:

| First successful firmware run | Count | Result |
| --- | ---: | --- |
| Clippings GET | 2 | HTTP 200 |
| Clippings PUT | 1 | HTTP 200 |
| Progress PUT | 1 | HTTP 200 |
| Remote live clipping | 1 | revision 1, `different` |

Source: `preview-stock-new-8940/fixture-ledger-after-coldboot.json` and `guest-e2e-coldboot-container-local/verified-result.json` inside the local QA artifacts directory.

Second cold boot: seed the server from the preceding **synthetic** quote ledger with `--seed-ledger` and preserve Flash/SD. The guest reopened its last EPUB and emitted 2 × Clippings GET 200 plus Progress PUT 200, **no duplicate Clippings PUT**. The fixture still had precisely one remote item, revision 1. Source: `fixture-ledger-second-boot.json` and original guest UI screenshots.

## E2E test driver bugs fixed, not firmware

1. `View Clippings` can move down one 72px row when a quote is created. The old hardcoded y=516 produced a false negative on a real screen with the label at y=588. Added a second strict glyph anchor and a genuine test frame.
2. A Home screen containing a recent-book cover does not have the original `No open book` anchor. Added an independent `home_recent` anchor for resumed EPUBs.
3. Leaving the Wi-Fi picker to `Connecting…` does **not** prove a successful network exchange. Added screenshots/anchors for `Connecting` and `Sync failed → Could Not Sync Clippings` and required **actual guest-originated** Clippings/KoSync HTTP traffic, with explicit failure evidence if absent.
4. Added negative regression tests for neither connecting nor Sync-failed being treated as successful.

## Remaining

The two real-guest loops above prove firmware Clippings and KoSync compatibility, including persistence/idempotence, with an isolated synthetic server. They do **not** prove genuine Yandex cloud roundtrip, actual Stock 1.6.14 QR authorization on this new guest, or TLS peer verification on the original firmware (`setInsecure()` is inherited). Server PR #46 has separate FastAPI-to-mock-Yandex bidirectional regression; plugin PR #17 has three web controls and scoped quote-key revocation.

No production or Stable deployment. GT911/QMP warm-reset touch reliability still needs emulator work, but the isolated cold-restart workaround sufficed for actual firmware HTTP evidence.
