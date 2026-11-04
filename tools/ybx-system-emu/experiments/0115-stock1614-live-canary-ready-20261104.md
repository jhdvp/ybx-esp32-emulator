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

## Known limits (honest, unfixable client-side)

- **`setInsecure()` TLS:** the original Stock firmware disables peer-certificate verification on Clippings/progress. Neither the plugin nor the server can force the firmware to verify certs; this is a firmware property, kept separate from Stock compatibility. The smoke harness therefore uses curl's system trust rather than papering over it in Python.
- **QMP touch flakiness:** GT911 touch over QMP intermittently freezes on warm reboot; test-only **cold** container restart is the workaround (the `--live` flow already asks the owner to cold-restart for the duplicate check). 0113/0114 already covered real-firmware pixel interaction; the dry-run is **protocol-level replay, not pixels** — it proves the script's logic and gates, not that a specific touch landed.
- **Not a real sign-in:** the dry-run pairing/token step exercises the device-code protocol against the fixture; real QR approval, the account's live catalog, and bidirectional Yandex quote delivery remain **owner-verified on real hardware only**.
- **Plugin-repo QA doc is stale on the endpoint status:** the `jhdvp/ybx-crosspoint-plugin` QA note still says unauthenticated `POST …/auth/clippings-key` returns **404**; it is **401** now that PR #46 is deployed (confirmed in Part 1). That repo was **not** edited here — this note is the correction of record for this branch.
- **No promotion:** PR #17 (1.6.14 RC) and the two-phase `YBX_STOCK_PLUGIN_CHANNEL` stay on `stable`; production still serves 1.6.13. Promotion is a separate, audited owner action, not a consequence of this run.
