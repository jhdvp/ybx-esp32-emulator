# 0115 — Stock 1.6.14 Live Canary: ready-for-owner script + read-only production smoke

Date: 2026-11-04. Branch `test/stock1614-live-canary-20261104` (based on emulator PR #19). Nothing here deploys, promotes, logs into, or writes to production. Stage C is scripted and **ready for the owner**; the only parts that require a real Yandex account and the guest screen are left as owner-gated pauses, never automated with credentials.

Script: `tools/ybx-system-emu/stock1614-live-canary.py` (three mutually exclusive modes: `--prod-smoke`, `--dry-run-synthetic`, `--live`).
Evidence (redacted): `tools/ybx-system-emu/experiments/0115-evidence/0115-prod-smoke.json`, `0115-dryrun-summary.json`.

## PART 1 — Read-only production smoke (GREEN, zero mismatches)

Unauthenticated HTTPS only, via `curl` with **system CA trust** (verification is never disabled — the framework Python has no CA bundle, and turning verification off would mirror the very `setInsecure()` weakness the runbook flags). Production is only ever read; any mismatch would be reported, not fixed.

| Gate | Result |
|------|--------|
| `GET /health` | `200` |
| `POST /api/v1/crosspoint/auth/clippings-key` (unauth) | **`401`** (was `404` pre-deploy → PR #46 is deployed) |
| `GET /crosspoint-plugin/latest.json` | `200`, version `1.6.13` |
| `GET /crosspoint-plugin/catalog.json` | `200`, version `1.6.13`, files == the seven-file manifest |
| `GET /crosspoint-plugin/manifest.json` | `200`, version `1.6.13` |
| Seven per-file routes vs stable 1.6.13 package | **byte-identical** |
| `ybx-plugin.zip` seven members vs stable 1.6.13 | **all members match** |

The public device-update channel is still pinned to stable **1.6.13** — the candidate 1.6.14 RC is NOT promoted. Per-file SHA-256 (served == stable, from the server worktree `static/crosspoint-plugin-stable/`):

```
manifest.json  4beb34c26915fd82e959fa040e4f7029688b73b5ea42f25d0b4948642637fa0a
device.json    809eb7f7b39638918617eb5e40853cd7e7fc86686b8abc4b1be8aa693731d126
plugin.json    2b5c41059cf5c0f5320016291bef3f20d9e7b58905ab4e18b6c8021a67e9d857
plugin.js      8c00375ce149ba840582721d8b76cd86fc55830ab64070dc60a058b1fe264713
config.json    c05a70d3b398521053cccd1f2f1034173230f259186e75b4f46bb1192dc936a8
README.md      c886001a4c5f3d48fc4b43dd0e095fe6eac6e75636d6b3cff9b286e3114f41ab
icon.png       d20524eacbb86d275622f1dbf7ac7b3c2d46840cda8cbc6ef2ed3cc4510d1796
```

## PART 2 — How to run the canary

All commands from `tools/ybx-system-emu/`. Candidate ZIP `~/Projects/YBX/artifacts/ybx-stock1614-release-candidate-20261104/ybx-plugin-1.6.14-rc.zip` (SHA-256 `43019ff44c2daae4f0878419c296e255ab253dce765e75d2053eb94794ca8b6e`); disposable install SD `~/Projects/YBX/artifacts/ybx-stock1614-clippings-qa-20261104/stock-1614-plugin-exact-sd.img`; stable comparison dir defaults to the server-mainline-qa worktree. Ports auto-pick a free pair in 8971..8999 and **refuse** the reserved 8784/8940/8941/8950/8951/8964/8965 owned by other runs.

### 2a. Prove the script works — hermetic dry-run (this is the gate; run until GREEN)

```
python3 stock1614-live-canary.py --dry-run-synthetic
```

No internet, no real account, no container. Replays the same six Stage C checks against the 0113 synthetic KoSync/fake-Yandex fixture (`stock1614_fixture_server`) and the real `guest_round_trip_valid` verdict gate. Confirmed **OVERALL: GREEN** (exit 0), repeatable.

### 2b. Re-check production (Part 1, read-only)

```
python3 stock1614-live-canary.py --prod-smoke
```

### 2c. The real Stage C canary — OWNER-GATED

```
python3 stock1614-live-canary.py --live
```

The script: validates the candidate ZIP + installed SD byte-by-byte; starts its **own** isolated container `ybx-eink-qemu11-<port>` on a free port with fresh Flash/SD; then pauses for the owner at two points:
- **Owner acts here (pairing):** open `http://127.0.0.1:<port>/`, File Transfer → Join a Network → PICSimLabWifi, Plugins → YBX, read the QR on the guest screen, and pair it **only** with the dedicated test Yandex account. The token/QR/link is never printed or stored; the script records only `token.json` presence and its key names.
- **Owner acts here (Yandex round-trip):** create a disposable clipping on the test book, Sync, confirm the quote lands in Yandex, create a quote in Yandex, confirm the return journey, then cold-restart **only this container** and confirm no duplicate.

Toggle/re-enable gates (`--live`) run against the genuine guest firmware `/api/settings` over the container hostfwd (`docker exec … http://127.0.0.1:18080`), with a 71-value diff whose values are redacted. On exit the `finally` tears down **only** this run's container and bridge. Nothing is promoted to Stable.

## PART 2 — Dry-run results (GREEN)

| # | Stage C check | Result | Detail |
|---|---------------|--------|--------|
| 1a | Candidate ZIP + 7-file payload validator | PASS | sha `43019ff4…`, v `1.6.14` |
| 1b | Pinned eink-qemu / flash / SD inputs (`--check`) | PASS | official QEMU 11.1.2 / 0.9.2 SHA checks |
| 1c | Installed disposable SD matches candidate | PASS | found=7, koreader.json + fixture EPUB present |
| 2 | Device-code pairing + `token.json` (keys only) | PASS | keys `access_token/expires_in/token_type`, token never logged |
| 3 | Enable 4 flags independently (71-value diff, redacted) | PASS | only the target key flips each time; count stays 71 |
| 4 | Clippings GET/PUT/GET + progress + cold-boot idempotence | PASS | round-trip valid, no duplicate PUT, retry applied=0, revision stable |
| 5 | Disable → old proof forbidden + progress ok; re-enable → new proof | PASS | unauth mint 401, disabled old proof forbidden, KOSync progress still works, re-minted proof differs |
| 6 | Evidence hygiene | PASS | offenders=0 (no fixture token, marker text, or pairing host in the shipped artifact) |

The evidence JSON stores booleans, public SHA-256 digests, key **names**, and value lengths only. `token_never_logged=true`; the leak scan rejects the fixture bearer token, the disposable marker, and the pairing host, and re-scans the exact bytes written before reporting success.

## Live path to pairing pause (real isolated guest, GREEN)

The `--live` path had never been executed. It is now driven **non-interactively** up to the first owner pause — the device-code QR pairing screen — and no further. This proves the script really boots the original CrossPoint 2.0 RC X4 Pro guest and reaches the pairing pause; it does **not** pair, does **not** open the pairing link, and never prints or stores the QR / device code / link.

Exact command (run from `tools/ybx-system-emu/`, stdin closed):

```
python3 stock1614-live-canary.py --live --stop-at-pairing </dev/null
```

It exits `0` the instant the pairing pause is confirmed, then tears down **only its own** container.

### What was proven (all gates PASS, exit 0)

| Gate | How proven (existing 0113/0114 gates) | Result |
|------|----------------------------------------|--------|
| Own container on a free port | `pick_free_pair()` chose preview `8971` / QMP `8972`; reserved `8950/8951/8964/8965` and any busy port are **refused** (verified: `--preview-port 8950` → exit 2) | PASS |
| Fresh disposable SD, shared artifact untouched | the shared `stock-1614-plugin-exact-sd.img` is copied to a temp `canary-disposable-sd.img`, byte-validated against the candidate ZIP (7/7 files), and the guest only ever writes the copy under its temp workdir | PASS |
| Guest booted to Home | UART boot marker (`home_log_observed`, `[MEM] Free:`) **and** the `home` framebuffer anchor from `matches_guest_screen` | PASS |
| Plugins screen opened | tap Home row2 `(240,490)`; new `plugins` text anchor matches (and only that screen) | PASS |
| YBX launched → firmware Wi-Fi picker | tap `(240,210)`; the genuine `wifi` anchor (PICSimLabWifi picker) appears — the same signal 0114 recorded | PASS |
| Wi-Fi joined → device-code QR pause | tap `(138,193)`; `wifi` anchor clears and the new `pairing_waiting` anchor ("Waiting for authorization…") matches | PASS |

Evidence (redacted): `experiments/0115-evidence/0115-live-pairing-pause.json` (booleans + per-stage frame SHA-256 only), plus the secret-free screenshots `live-1-home.png`, `live-2-plugins.png`, `live-3-wifi.png`. The QR/pairing screen itself is **never** written to the repo — it is proven by the `pairing_waiting` anchor plus `qr_frame_sha256_not_committed` (a hash, not the image).

### Tooling added on this branch (no emulator/firmware changes)

- Two **secret-free** real-guest UI anchors in `stock1614_ui.py`: `plugins` (the "Plugins" header word) and `pairing_waiting` (the "Waiting for authorization…" line). Both are static firmware *text* crops captured from the genuine guest; neither contains a QR, device code or pairing link. `test_stock1614_ui.py` proves each matches only its own screen, fails on a blank panel, and never cross-matches. The `pairing_waiting` fixture is the text region composited on a blank panel precisely so the raw QR screen is never stored.
- `run_live` gained `--stop-at-pairing`, `--boot-timeout`, `--pairing-timeout`, a real boot gate (UART **and** framebuffer), the Plugins→YBX→Wi-Fi→QR navigation via the preview bridge `/api/tap` + `/screen.png`, a fresh disposable SD copy, a reserved/busy-port refusal, and teardown that also removes the temp workdir on success **and** failure.

### Honest limits — what this does NOT prove

- **Reaching the QR uses a real device-code request.** As in 0114, launching YBX and joining the network makes the *firmware itself* contact its configured auth host to render a genuine device-code QR. The canary only observes the resulting screen; it does not approve, pair, or open the link. No token/QR/link is captured.
- **Not a completed sign-in.** Catalog load, `token.json` storage, the live account catalog, the bidirectional Yandex quote round-trip, and cold-boot idempotence on the **real** account remain owner-gated (the interactive `--live` path continues past the pause for those). They were not exercised here.
- **TLS `setInsecure()`** on the original firmware is unchanged and out of scope.
- **Navigation reliability** uses GT911 taps through the bridge; on a fresh **cold** container it was deterministic. 0113's intermittent touch freeze is a warm-reboot property and does not apply to this cold-start path.

### Remaining owner steps at the pause

1. Open the preview `http://127.0.0.1:<port>/` (or rerun `--live` without `--stop-at-pairing` to continue past the pause).
2. Scan the on-screen QR with the **dedicated test Yandex account only** and approve; never copy the QR/token/link into this repo or any log.
3. Confirm the populated catalog, then run the Stage C gates the interactive path prompts for: independent `/api/settings` toggles (71-value redacted diff), the disposable clipping + Yandex round-trip, a cold restart of **only this container** for the duplicate check, and Clippings disable→re-enable proof revocation/re-mint.

## Known limits (honest, unfixable client-side)

- **`setInsecure()` TLS:** the original Stock firmware disables peer-certificate verification on Clippings/progress. Neither the plugin nor the server can force the firmware to verify certs; this is a firmware property, kept separate from Stock compatibility. The smoke harness therefore uses curl's system trust rather than papering over it in Python.
- **QMP touch flakiness:** GT911 touch over QMP intermittently freezes on warm reboot; test-only **cold** container restart is the workaround (the `--live` flow already asks the owner to cold-restart for the duplicate check). 0113/0114 already covered real-firmware pixel interaction; the dry-run is **protocol-level replay, not pixels** — it proves the script's logic and gates, not that a specific touch landed.
- **Not a real sign-in:** the dry-run pairing/token step exercises the device-code protocol against the fixture; real QR approval, the account's live catalog, and bidirectional Yandex quote delivery remain **owner-verified on real hardware only**.
- **Plugin-repo QA doc is stale on the endpoint status:** the `jhdvp/ybx-crosspoint-plugin` QA note still says unauthenticated `POST …/auth/clippings-key` returns **404**; it is **401** now that PR #46 is deployed (confirmed in Part 1). That repo was **not** edited here — this note is the correction of record for this branch.
- **No promotion:** PR #17 (1.6.14 RC) and the two-phase `YBX_STOCK_PLUGIN_CHANNEL` stay on `stable`; production still serves 1.6.13. Promotion is a separate, audited owner action, not a consequence of this run.

## Owner-friendly live run v2

Work in progress; **do not use the full owner run yet**. The driver now builds a fresh card containing only the seven candidate plugin files and a public-domain Hamlet excerpt, verifies the file set and scans the complete image for fixture addresses. It refuses the full live run until automated navigation and web-panel activation are implemented and verified. The previous owner instructions above describe v1, not a completed v2.

The intended owner command, once the remaining work is complete, is:

```sh
python3 tools/ybx-system-emu/stock1614-live-canary.py --live
```

Not verified: clean-card guest boot and pairing pause, Home/File Transfer/YBX navigation, unpaired safe failure in the plugin web panel, paired activation, settings verification, opening the book and clipping synchronization. In the current sandbox, binding a loopback socket fails with `PermissionError`, so even the synthetic dry run cannot start its local server; no guest or owner flow was run here. No pairing was performed.

## Owner-friendly live run v2

Work in progress: do not run the owner canary yet. The fresh SD exposes a first-boot condition missed by the old media: Plugins are disabled by default. The driver now enables them through Settings → Connectivity → Plugins and returns using the header back arrow; no settings or credentials are preseeded on the SD. The firmware source maps the header back target to Back (`MappedInputManager::wasBackGesture`). A clean-SD `--live --stop-at-pairing` reached the pairing screen without pairing, and `--dry-run-synthetic` passed. The full live mode remains deliberately blocked.

Reproduction from the repository root:

```sh
python3 tools/ybx-system-emu/stock1614-live-canary.py --live --stop-at-pairing
```

Still unverified and not implemented: automated navigation from YBX to File Transfer and back, the unpaired web-panel failure and paired button activation, verification of the resulting settings, automatic proxy and owner prompts, UART clipping-error reporting, and the private decoded pairing-URL file. Playwright and Chrome are present locally; Python QR decoding and `zbarimg` were not found. No pairing or production writes were performed.
