# 0114 — Original Stock + exact YBX 1.6.14 bundle: plugin discovery and genuine QR

Date: 2026-11-04. Only an isolated official CrossPoint 2.0 RC X4 Pro guest, no modified firmware or live user device.

## Media and package provenance

- Stock source: `jhdvp/ybx-crosspoint-plugin` branch `feat/stock1614-clippings-button-20261104`, version `1.6.14`, seven exact files listed in `manifest.json`: `manifest.json`, `device.json`, `plugin.json`, `plugin.js`, `config.json`, `README.md`, `icon.png`.
- Created a new FAT16 SD image `stock-1614-plugin-exact-sd.img` from the untouched synthetic `clean-clippings-sd.img`. The original `/.crosspoint/plugins/ybx/` directory was created in this **QA copy**, and all seven bytestrings were read back and verified equal. The production reader's storage was never touched.
- Started an **independent QEMU** `ybx-eink-qemu11-8950` with preview `127.0.0.1:8950`, QMP `127.0.0.1:8951` and separate Flash/SD under `artifacts/ybx-stock1614-clippings-qa-20261104/preview-stock-plugin-8950`. The official QEMU binary and flash hashes passed the pinned SHA checks. Previous QA instance 8940, production 8784 and other services remained untouched.

## Real guest observations

1. The firmware's **Plugins** menu lists `YBX` with the shipped YBX description, after scanning the seven files. It was not merely injected as a fake DOM card.
2. Selecting YBX triggered the stock firmware's own `PICSimLabWifi` network picker. After joining, UART confirmed an actual outbound TLS 1.3 handshake to the public YBX host.
3. The firmware rendered a genuine QR code and `Waiting for authorization…` through its own device-code authentication implementation, not through Python render mocks. No `Failed to fetch` occurred at the code issuance stage.
4. The first code expired before account approval and Stock displayed `Sign-in failed or expired. Try again`. Tapping retry generated a new pending QR. **Cabinet approval, token storage and real catalog from this new guest are still NOT verified.** Do not report this as a completed sign-in.
5. QR screenshots and live pairing codes remain in local QA files only. Never publish them to this repository or user-visible CI logs.

## Catalog compatibility evidence (independent of uncompleted real pairing)

- Exact Stock `device.json` lists eight firmware-compatible root sections: `Все книги`, `Читаю`, `Хочу прочитать`, `Прочитано`, `Загруженные`, `Яндекс Книги`, `WB Книги`, `ЛитРес`.
- Original firmware substitutes `{limit}` with `page_size + 1` to detect a next page; server advances `(page - 1) * (limit - 1)`. This is intentional, *not* a duplicate-page bug.
- Additional `tests/test_stock_catalog_manifest.mjs` in Stock plugin PR #17 validates those eight paths, HTTPS URLs, provider-specific fallbacks, search, QR device code, the 11-record lookahead request and sidecar metadata. Plugin suite **72/72 Node tests**, package validator and **13 Python + 4 subtests** passed.
- Server PR #46 passed **263/263 SQLite and real PostgreSQL 16 tests** in an isolated local Docker QA database, without skipped tests. Local PG was stopped/removed after verification. GitHub runner is currently blocked by the repository's billing/spending-limit state, not failing assertions.

## Release gates and boundaries

- The original Stock firmware/fixture exchanged and persisted a clipping and KoSync progress across two cold boots in 0113, without duplicates. This is separate from the production QR session.
- Real QR approval, the user account's existing catalog and actual Yandex bidirectional quote delivery **are unverified** on exact Stock 1.6.14.
- Server `POST /api/v1/crosspoint/auth/clippings-key` is not in production until PR #46 is deployed; do not claim quotes on real YBX are enabled by Stock plugin alone.
- Original firmware uses TLS `setInsecure()` for Clippings and progress. Plugin/server cannot make the firmware verify peer certificates; keep custom-firmware TLS improvements separate from Stock compatibility.
- Prior E-Ink heap crash is not reproduced with the synthetic EPUB, but this does not prove that particular production EPUB is fixed. QMP GT911 warm-reboot input intermittently freezes; test-only cold restart works.
- Neither PR #17 nor PR #46 has been merged to Stable or deployed to production.
