# 0064 — batch 3: browser bridge hardening

Lane: `browser-hardening` (worktree `ybx-emu-batch3-20261031/browser-hardening`,
branch `agents/ybx-emu-b3-browser-hardening-20261031`, base `72f4c0a`).

Changed files (all in this lane's ownership):

- `tools/ybx-system-emu/serve_x4pro_browser.py`
- `tools/ybx-system-emu/test_serve_x4pro_browser.py`
- `tools/ybx-system-emu/start_x4pro_browser.sh`

The shared `README.md`, `QEMU_BRINGUP.md`, `experiments/README.md`, the QEMU
patch chain and the live Docker container were not touched. Nothing here was
run against the running emulator; all verification is host-side with a fake
guest. No measurements, SHAs or counter values in this report were invented —
live counter values are explicitly marked unproven.

## New / changed HTTP contract

### `GET /api/telemetry` (new, read-only)

Returns the QOM counters the patched emulator (chain 0042+0043+0037+0051,
binary `f07744d9…`) already exposes, plus the last server-computed frame SHA:

```json
{
  "ybx-epd-busy-rise-edges": {"available": true, "value": 17,
    "source": "qom:/machine/soc/ybx-epd-busy-rise-edges"},
  "ybx-epd-busy-fall-edges": {"available": true, "value": 12, "source": "..."},
  "ybx-epd-busy-high-ms":    {"available": true, "value": 350, "source": "..."},
  "ybx-epd-frames":          {"available": true, "value": 9,  "source": "..."},
  "frame-sha256": {"available": false, "value": null,
    "error": "no rendered frame has been hashed yet"}
}
```

Values in the example above are the shapes proven by the fake-guest tests, not
live readings — this lane had no access to the running guest.

- Each key is `{"available": bool, "value": int|string|null, "source"|"error": str}`.
- A counter the running QEMU binary does not expose (pre-0037 build) comes back
  `{"available": false, "value": null, "error": "<QMP error text>"}` — never a
  fabricated `0` (`serve_x4pro_browser.py:328-361`).
- A dead QEMU process or broken QMP socket yields `503`
  `{"error": "...", "type": "guest-missing"}` instead of blank counters
  (`serve_x4pro_browser.py:334-336,345-348`).
- The endpoint is read-only: every QMP command it can issue is `qom-get`
  against `/machine/soc`; it never writes `ybx-touch-*`, never reads
  `visible-hex` (no frame copy) and cannot change guest state
  (test: `test_serve_x4pro_browser.py:257-263`).
- `frame-sha256` is the SHA-256 of the last accepted SSD1677 visible-RAM frame
  already computed by `/api/state`; before the first hashed frame it is
  reported unavailable.

### `GET /screen.png` (validators added)

- `ETag`: content-derived `"f<frame>-<sha256[:16]>"`; `'"blank"'` before the
  first rendered frame and after a reset
  (`serve_x4pro_browser.py:319,242,294,447`).
- `Last-Modified`: time the server rendered that PNG.
- `Cache-Control: private, max-age=0, must-revalidate` (was `no-store` for
  this path).
- Conditional requests: `If-None-Match` (strong, weak `W/`, comma lists, `*`,
  `serve_x4pro_browser.py:51-64`) takes precedence over `If-Modified-Since`
  (1-second resolution, `serve_x4pro_browser.py:67-77`). A match returns
  `304` with only the validators + `Cache-Control` — no body, no
  `Content-Length`, no `Content-Type` (`serve_x4pro_browser.py:492-512`).
- Query strings (`?frame=N`) are still ignored server-side; the page JS now
  drops the `Date.now()` cache-buster because ETag+304 keep frame URLs fresh.

### `GET /api/state` (additive only)

Existing keys `frames`, `nonwhite`, `ready`, `sha256`, `factory_guest`,
`physical_device` are unchanged; a new `"monotonic_ms"` (int,
`time.monotonic()`-based server clock, for client-side ordering) is added
(`serve_x4pro_browser.py:322`). The frame number remains `frames`.

### `POST /api/tap`, `POST /api/reboot` (typed errors, bounded queue)

All error replies keep the `error` string field and now may add `type`:

| situation | status | type | extra header |
|---|---|---|---|
| malformed request (content type, size, JSON, coordinates, duration) | 400 | `bad-request` | — |
| guest still processing another touch (2 s lock wait expired) | 503 | `guest-busy` | `Retry-After: 1` |
| more than `tap_queue_depth=4` taps already waiting | 429 | `guest-busy` | `Retry-After: 1` |
| QEMU process gone / QMP transport dead | 503 | `guest-missing` | — |
| unclassified guest failure (transport blip mid-touch) | 503 | (no `type` field) | — |

Classes: `serve_x4pro_browser.py:26-48` (`BadRequestError(ValueError)`,
`GuestError(RuntimeError)`, `GuestBusyError`, `TapQueueFullError`,
`GuestMissingError`). Subclassing the old `ValueError`/`RuntimeError` keeps
every pre-existing `except` and the page JS working unchanged.

Concurrency: `tap()`/`reboot()` take a `BoundedSemaphore(4)` slot
(`serve_x4pro_browser.py:216,232,363-380`); the 5th queued client is refused
with 429 before touching QMP, waiting clients are serialized exactly as
before (one GT911 press+release at a time), and every failure path releases
both lock and slot (`serve_x4pro_browser.py:363-382`).

Reboot ordering is preserved and re-proven: `ybx-touch-state/x/y` are all
written `0` before `system_reset` (`serve_x4pro_browser.py:427`, test
`test_serve_x4pro_browser.py:76-89`); after a successful reset the PNG
validators rotate back to `"blank"` (`serve_x4pro_browser.py:445-448`).

### Launcher (`start_x4pro_browser.sh`)

- Strict bind predicate (`start_x4pro_browser.sh:17`): IPv4 only; `0.0.0.0`
  (unspecified), link-local `169.254/16`, public ranges and anything
  non-numeric (e.g. `localhost`, IPv6) are rejected with exit 2 *before* any
  file or Docker operation. Loopback and RFC1918/private ranges pass.
- Loud warning (`start_x4pro_browser.sh:24-34`): any non-`127.0.0.1` bind
  prints a 9-line banner to stderr — NO authentication, NO TLS, anyone who
  can route there can watch the screen, inject taps and reboot the guest —
  with the `ssh -L $PORT:127.0.0.1:$PORT` alternative. The default stays
  loopback and prints nothing extra.

## Tests added (host-only, fake guest, no Docker/network/guest)

`test_serve_x4pro_browser.py` grew from 33 to 62 tests (module also runs the
existing launcher suite):

- `TelemetryTests` (:234): full counters+SHA dict equality, read-only proof,
  missing properties → `available:false`/`value:null` (asserts no zero),
  unhashed frame SHA, terminated guest, transport failure.
- `TapQueueTests` (:296): 5th tap → `TapQueueFullError` (429) with zero QMP
  calls; completed and failed taps return every slot; 3 concurrent taps
  serialize to strict `[1,0,1,0,1,0]` press/release events.
- `ConditionalRequestTests` (:346) and `HttpCachingTests` (:566): validator
  headers on 200, `304` for exact/`*`/weak `If-None-Match`, mismatched ETag
  beats `If-Modified-Since`, ETag-less `If-Modified-Since` fallback (match,
  older date, garbage), second-grained comparison, `/api/state` contract keys
  plus `monotonic_ms`.
- `HttpTelemetryErrorTests` (:620): telemetry passthrough and typed 503,
  guest-busy 503 / queue-full 429 / guest-missing 503 table with
  `Retry-After`, `bad-request` 400, reboot guest-missing, 4 concurrent HTTP
  taps all accepted.
- `LauncherBindGuardTests` (:690): runs the real bash launcher with an empty
  `bin` first on `PATH` and fileless roots, so the script always exits at the
  bind check or the missing-artifact check — the real `docker` CLI is never
  invoked. Covers public reject, link-local reject, loud LAN warning, quiet
  loopback.
- Fakes updated to the new surface (`browser_guest` :22, `FakeGuest` :364);
  the pre-existing reboot-ordering, serialization and 503 tests all kept.

Harness note: the per-test `ThreadingHTTPServer` now runs
`serve_forever(poll_interval=0.02)` (`:414-425`) — the library default 0.5 s
made every `shutdown()` cost half a second and pushed the module past 20 s.

## Commands run (real output)

```
$ cd /Users/yaraslauhancharou/.local/state/xteink-agent-workspaces/ybx-emu-batch3-20261031/browser-hardening \
  && python3 -m unittest discover -s tools/ybx-system-emu -p 'test_*.py' -q
Ran 316 tests in 6.220s

OK (skipped=7)
```

(The 7 skips are pre-existing QEMU/Docker-dependent tests in other modules.)

```
$ python3 -m unittest test_serve_x4pro_browser test_start_x4pro_browser -q
Ran 62 tests in 3.817s   OK

$ python3 -m py_compile serve_x4pro_browser.py   → COMPILE-OK
$ bash -n start_x4pro_browser.sh                → BASH-OK
$ node --check <(embedded PAGE script extract)  → JS-SYNTAX-OK  (3051 bytes)
```

## Orchestrator verification (needs the live guest — not runnable in this lane)

No new probe script was required: the counters themselves are already proven
against the compiled binary by `probe_x4pro_telemetry_qtest.py` (0037/0051).
What is NOT yet measured anywhere is the new bridge surface against the real
container. Run from the Mac:

```
curl -s http://127.0.0.1:8784/api/telemetry | python3 -m json.tool
# expect available:true for all four ybx-epd-* keys on binary f07744d9…
# (chain 0042+0043+0037+0051); a pre-0037 build must show available:false.

ETAG=$(curl -sD- -o /dev/null http://127.0.0.1:8784/screen.png | awk -F': ' '/^ETag/{print $2}' | tr -d '\r')
curl -s -o /dev/null -w '%{http_code}\n' -H "If-None-Match: $ETAG" http://127.0.0.1:8784/screen.png
# expect 304, then tap the page and confirm a new f<frames>-<sha> ETag per frame.
```

Then drive a screen refresh (e.g. tap `(350,760)` = More) and confirm
`ybx-epd-busy-rise-edges`/`fall-edges` increase across the SSD1677 refresh.

## Security notes

- The bridge has **no authentication and no TLS**. It must stay on
  `127.0.0.1`; the launcher enforces a private-bind check and prints the loud
  LAN banner; `serve_x4pro_browser.py:main` additionally warns whenever it is
  started on a non-loopback host (in-container `--host 0.0.0.0` is protected
  only by the `-p $BIND_IP:$PORT` mapping).
- `/api/telemetry` exposes emulator-internal device-model counters and the
  already-public frame SHA; it performs only `qom-get` reads and cannot alter
  guest behaviour.
- The 429 queue bound is a self-protection measure, not an access control:
  an unauthenticated LAN peer could still drive taps serially.

## Risks / what stays unproven

- Live BUSY counter values through the browser bridge: never fetched in this
  lane (Docker is off-limits). The endpoint *shape* is fake-tested only.
- The page JS is syntax-checked and string-pinned by tests, but never
  rendered in a real browser against a live guest.
- `monotonic_ms` is process-relative (not epoch); clients must use it only
  for ordering, which is why it is named monotonic.
- `429`/`Retry-After` and the `type` field are additive; existing clients
  that only read `error` are unaffected, but no external client was tested.
- ETag correctness assumes the single in-process renderer (one guest per
  server, as documented).

## Suggested `experiments/README.md` index entry (orchestrator owns that file)

```
## 0064 — browser bridge hardening: telemetry endpoint, PNG validators, bounded taps

/api/telemetry exposes ybx-epd-busy-{rise,fall}-edges / busy-high-ms / frames
plus the frame SHA — honestly reporting unavailable instead of a fake zero,
read-only (qom-get only). /screen.png gets content-derived ETag +
Last-Modified with 304 handling, /api/state adds monotonic_ms, taps are
serialized behind a 4-deep bounded queue with typed 400/429/503 errors, and
the launcher rejects public/link-local binds and prints a loud no-auth LAN
warning. 316 host tests green; live-container checks left to the orchestrator:
0064-batch3-browser-hardening.md.
```
