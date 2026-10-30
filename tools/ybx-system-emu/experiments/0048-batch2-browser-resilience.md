# 0048 — Batch 2: browser and host safety hardening (serve_x4pro_browser.py)

Agent: Qwen3.8 batch 2, browser-resilience lane. Host-only change in this
worktree; the active Mac emulator on :8784, the patched QEMU tree, the
developer repository and the factory ELF were not touched. No firmware,
no Wi-Fi claims, no fabricated frames.

## Audit findings (pre-change line numbers of serve_x4pro_browser.py)

1. Simultaneous taps were rejected, not serialized. `tap()` and `reboot()`
   used `tap_lock.acquire(blocking=False)` (old lines 209, 239). A 150 ms
   GT911 press holds the lock through `time.sleep` (old line 224), so a
   second quick tap or a tap-then-restart returned
   "Previous touch still in progress" with HTTP 400.
2. Uncaught handler crashes → silent no-response failures.
   `do_POST` line 293 `emu.reboot()` caught only `RuntimeError`, and line
   304 `int(self.headers.get("Content-Length","0"))` sat inside a `try`
   that caught `(ValueError, RuntimeError)`. A non-numeric Content-Length
   raises `ValueError` — but a transport fault (`BrokenPipeError`,
   `ConnectionResetError`, i.e. `OSError`) from the half-closed QMP socket
   raised by `probe_x4pro_touch_injection_qtest.qmp_connect`'s `execute`
   (line 42: `raise RuntimeError("QMP closed")` covers EOF only; `client.sendall`
   raises `OSError`) escaped the handler. `BaseHTTPRequestHandler` then
   closes the connection without any JSON answer; the browser only sees
   "Touch failed: Failed to fetch".
3. No recovery once the guest process died. `state()` (old line 190) and
   `reboot()` only reported "QEMU process terminated" forever; the sole
   fix was restarting the Docker container. The Wi-Fi wedge recovery path
   from commit 99243efb (`/api/reboot` → QMP `system_reset`) cannot work
   when the QMP transport itself is dead, which is exactly what a crashed
   guest produces.
4. Request bounds were mostly good (160-byte cap, `check_touch` integer
   type checks reject booleans/floats/strings, panel range, 60..500 ms),
   but a rejected oversized body is never read. That is safe only because
   the handler runs HTTP/1.0 (`BaseHTTPRequestHandler.protocol_version`
   default), so the socket closes after the 400 and the unread bytes can
   never desynchronize a reused connection. Kept as is; noted as a
   constraint for any future `protocol_version = "HTTP/1.1"` change.
5. LAN binding: `start_x4pro_browser.sh` already refuses non-private or
   invalid IPv4 (`ipaddress` check, lines 16–20), defaults to 127.0.0.1,
   and the Docker `-p "$BIND_IP:$PORT:$PORT"` mapping is the real guard
   while the in-container server binds 0.0.0.0. Remaining hole: running
   `serve_x4pro_browser.py --host 0.0.0.0` directly on the Mac exposes an
   unauthenticated control endpoint with no warning.
6. iOS click coordinates: `ev.clientX` vs `getBoundingClientRect()` share
   the viewport coordinate space, so the mapping is correct; the only real
   hazard is a zero-size rect (display:none race), producing `NaN`
   coordinates that become `null` in the JSON body. Server already rejects
   that with 400; a client-side guard avoids the pointless round trip.
7. Boot readiness: `guest_ready_for_touch(frames >= 2)` is unchanged and
   remains pixel-density independent (comment at old lines 24–29). After
   reboot the server invalidated only `self.frame` (old line 246); `sha`
   and `png` stayed from the pre-reboot boot until the first new frame.

## Changes implemented (tools/ybx-system-emu/serve_x4pro_browser.py)

- `_begin_action()` (line 241) replaces both non-blocking acquires with
  `tap_lock.acquire(timeout=FactoryX4Pro.tap_wait_seconds)` (2.0 s class
  default). Two quick taps now serialize and both reach GT911; a stuck
  action is still refused with "Guest busy with another touch; retry
  shortly" after the bound.
- Guest relaunch recovery: `__init__` split into reusable `_launch()`
  (line 171; unlinks a stale `qmp.sock` first), `_stop_process()` and
  `_relaunch()` (line 204). `reboot()` (line 275) now: relaunches the
  process if `poll()` shows it terminated; on `OSError/RuntimeError/
  ValueError` from the QMP transport during `system_reset`, relaunches
  instead of failing forever; a QMP *error reply* still raises and keeps
  the frame cache (old 99243efb semantics). Success now also clears
  `sha` and resets `png` to the blank boot frame, so no stale pre-reboot
  image or SHA is served while the new boot produces its first frame.
  `state()`'s terminated-process message now names the recovery button.
- `do_POST` hardening: non-numeric `Content-Length` → 400 "Invalid
  Content-Length"; malformed JSON → 400 "Invalid JSON payload"; reboot
  catches `(ValueError, RuntimeError, OSError)` → 503 JSON; tap
  validation (400) is separated from guest faults
  (`RuntimeError`/`OSError` → 503 JSON) so a broken QMP link answers JSON
  instead of crashing the handler thread with an unhandled `OSError`.
- Page/client: `if(!rect.width||!rect.height)return;` guard (line 95) and
  honest copy that Restart also starts a fresh guest if the emulator
  process stopped (no Wi-Fi promise — radio remains unemulated).
- `main()` prints an explicit no-authentication warning when bound to a
  non-loopback address directly (line 387); the shell entrypoint remains
  the sanctioned path.

## Host tests (test_serve_x4pro_browser.py, 9 → 29, no QEMU launched)

- Unit: simultaneous taps serialize as `[1,0,1,0]` state edges via two
  barrier-started threads; busy tap times out (0.05 s) without stealing
  the held lock; QMP death mid-touch still releases the lock; reboot of a
  terminated guest relaunches and skips `system_reset`; broken QMP socket
  (`BrokenPipeError`) and closed QMP stream (`RuntimeError("QMP closed")`)
  both trigger relaunch; failed relaunch releases the lock and leaves the
  dead `Popen` object so a second Recovery press retries; QMP error reply
  keeps `frame=42` (preserves 99243efb test intent); terminated `state()`
  message references "Restart virtual reader"; PAGE guards/wording.
- HTTP contract over a real loopback `ThreadingHTTPServer` with an
  in-process fake guest (never the active emulator): valid tap reaches
  the guest; non-numeric/missing Content-Length, oversize, wrong type,
  bad JSON and out-of-panel coordinates all get JSON 400 and the server
  stays alive; guest faults map to 503 JSON on `/api/tap`, `/api/reboot`,
  `/api/state` and `/screen.png`; `/api/shell` → 404.

Results: `python3 -m unittest test_serve_x4pro_browser` → 29 OK (~6 s);
full lane suite `python3 -m unittest discover -p 'test_*.py'` in
`tools/ybx-system-emu` → 183 OK. `git diff --check` clean.

## Residual risks

- Relaunch runs while `self.lock` is held, so `/api/state` polls block
  for the QEMU start + `qmp_connect` handshake (seconds). Acceptable for
  a single-developer tool; it self-heals once the new guest answers.
- Flash/SD images live in the session temp directory, so a relaunched
  guest boots against whatever the crashed guest last wrote — a fresh
  process, not a factory-fresh image. True image re-provisioning still
  means restarting the container; kept out of scope deliberately.
- `ThreadingHTTPServer` has no per-connection bound (unchanged from
  54243f5a); acceptable for localhost/trusted-LAN developer use.
- Recovery via relaunch has only mock tests on this host; a real
  kill-QEMU-then-press-Restart run on the active QA container is still
  the human's hardware-equivalent verification before merge.
- ESP32-S3 Wi-Fi remains unemulated; nothing here fakes network success.
