# 0048 — Wi-Fi selection state machine vs. an emulated board with no radio (batch 2)

Host-only analysis. No guest QEMU run, no Docker run, no change to firmware, to
the patched QEMU tree, or to the live browser emulator on this Mac.

## Question

`Join a Network` on the genuine factory X4 Pro image inside QEMU has been
described as a stall. Three different things get conflated in that wording:

1. the ESP32-S3 802.11 stack has no model in open QEMU;
2. the activity's own timers and abort paths;
3. the device-level reboot the File Transfer flow performs on exit.

This report separates them, and adds a host test that keeps them separated
mechanically.

## Evidence base

| Item | Path | Note |
| --- | --- | --- |
| Factory state machine | `src/activities/network/WifiSelectionActivity.{h,cpp}` | byte-identical to the factory-matching tree `fw-092c2be5-symbols-20261029` (`diff -u` empty for `WifiSelectionActivity.cpp/.h` and `CrossPointWebServerActivity.cpp`), so the analysis applies to the shipped ELF `206ed966…9523` |
| Parent flow | `src/activities/network/CrossPointWebServerActivity.{h,cpp}` | same check |
| Reboot primitives | `src/main.cpp:167-200` | `silentRestartTo()` → `ESP.restart()` at `:178` |
| Prior guest frame | `…/qa-20261029/diagnostics-20261030/x4pro-network-stall-before-restart.png` + `.json` (`sha256 f1ab03ed2ecb52d9…`, frames 66, nonwhite 6374) | the factory guest renders `File Transfer` with all four rows (Join a Network, Calibre Wireless, Create Hotspot, USB Drive) |
| QEMU Wi-Fi status | `tools/ybx-system-emu/QEMU_BRINGUP.md:21,29,91,123`, `experiments/0047-six-qwen38-agent-integration.md` ("Still incomplete") | open QEMU has no Wi-Fi model; nothing here claims otherwise |

## State machine as shipped

`WifiSelectionState` (`WifiSelectionActivity.h:27-38`) is driven entirely from
`loop()` (`cpp:593-824`) plus `checkConnectionStatus()` (`cpp:497-591`).

| State | Who leaves it | Timer |
| --- | --- | --- |
| `SCANNING` | `processWifiScanResults()` when `WiFi.scanComplete()` stops returning `WIFI_SCAN_RUNNING` (`cpp:202-220`); Back → `onComplete(false)` (`cpp:596-600`); Confirm only when auto-connecting (`cpp:601-605`) | **none** |
| `AUTO_CONNECTING` | hard failure (`cpp:564-572`) or `AUTO_CONNECTION_TIMEOUT_MS = 7000` (`cpp:578-586`, `h:103`) → `handleAutoConnectFailure()` | 7 s |
| `CONNECTING` | hard failure (`cpp:564-575`) or `CONNECTION_TIMEOUT_MS = 15000` (`cpp:578-589`, `h:102`) → `CONNECTION_FAILED` | 15 s |
| `CONNECTION_FAILED` | Back **or** Confirm (`cpp:734-750`): saved/auto credential → `FORGET_PROMPT`, otherwise `NETWORK_LIST` | user |
| `FORGET_PROMPT` | Confirm/Back/tap → `startWifiScan()` (`cpp:685-723`, `cpp:74-88`) | user |
| `SAVE_PROMPT` | only reachable after `WL_CONNECTED` (`cpp:550-553`) | user |
| `PASSWORD_ENTRY`, `HIDDEN_SSID_ENTRY` | keyboard result (`cpp:346-388`); `render()` suppresses itself in these two states (`cpp:843-845`) | user |
| `CONNECTED` | defensive only — `checkConnectionStatus()` completes without parking here (`cpp:554-560`, `cpp:727-731`) | — |

Blocking work per loop pass is bounded: `delay(100)` in `startWifiScan()`
(`cpp:196`) and in `attemptConnection()` (`cpp:471`). Everything else is
non-blocking polling of `WiFi.scanComplete()` / `WiFi.status()`.

## Recovery from `Join a Network` with no radio

What the firmware does depends on how `esp_wifi` answers when no 802.11 hardware
responds. The probe exercises each possibility instead of asserting which one
QEMU produces:

* **Scan never completes** (`WIFI_SCAN_RUNNING` forever) → the activity stays in
  `SCANNING` indefinitely. This is the only unbounded state: there is no scan
  timer anywhere in the file. The loop still runs and the screen still repaints,
  so it is *not* a task hang or a panic; it is a screen with no exit condition.
  Recovery exists and is manual: Back cancels (`cpp:596-600`), and while
  auto-connecting the header draws `Cancel` / `Show networks` hints
  (`cpp:1052-1055`). Confirm during auto-connect does **not** reach the list —
  it only sets `manualNetworkListRequested`, which is consumed by a scan result
  that never arrives (`cpp:265-267`, `cpp:601-605`).
* **Scan reports `WIFI_SCAN_FAILED`** → `NETWORK_LIST` with zero real networks
  and just the synthetic `Add hidden network…` row (`cpp:210-220`,
  `cpp:275-284`). On a touch board that row and a `Retry` button are drawn
  instead of the "Press OK" hint (`cpp:924-941`).
* **Scan returns 0 / returns networks** → `NETWORK_LIST`; selecting a row goes to
  the keyboard (encrypted, no saved credential) or straight to
  `attemptConnection()` (open, or saved credential).
* **`WiFi.begin()` never reaches `WL_CONNECTED`** → `CONNECTING` times out at
  15 s with `STR_ERROR_CONNECTION_TIMEOUT` (`cpp:578-589`); a driver-reported
  `WL_CONNECT_FAILED` / `WL_NO_SSID_AVAIL` ends it immediately with
  `STR_ERROR_GENERAL_FAILURE` / `STR_ERROR_NETWORK_NOT_FOUND` (`cpp:564-575`).
* **Auto-connect cascade** → bounded by `autoAttemptedSsids`
  (`cpp:390-392`, `cpp:412-424`): each saved SSID is attempted at most once, 7 s
  each, then the flow lands on `NETWORK_LIST`. With two saved credentials the
  probe measures settle at ~14.1 s, not a loop.
* **Nothing reports a connection.** Across every no-radio branch the probe sees
  no `CONNECTED`, no `SAVE_PROMPT`, no credential write, and an empty IP in the
  result handed to the parent (`cpp:1104-1112`).

## The part that is not a UI problem at all

`CrossPointWebServerActivity::onExit()` reboots the chip whenever
`WiFi.getMode() != WIFI_MODE_NULL` (`CrossPointWebServerActivity.cpp:125-133`) —
and `WifiSelectionActivity::startWifiScan()` puts the radio in `WIFI_STA` on the
very first entry (`cpp:194`), before any scan result. So *backing out of
`Join a Network` at all* ends in `ESP.restart()` via `silentRestartTo()`
(`main.cpp:167-180`), after a `Loading…` popup (`main.cpp:176-178`). In an
emulator that reads as "the reader disappeared"; on the device it is the
documented heap-defrag behaviour. The sibling `silentRestartToJoinNetwork()`
(`cpp:144-146`) is deliberately skipped on touch boards (`main.cpp:187-200`, guard at `:193`), so
X4 Pro does **not** reboot on the way *in* — only on the way out.

While the web server runs, the parent also tolerates a dead association for
`WIFI_ABANDON_MS = 5 min` before giving up (`CrossPointWebServerActivity.h:56`,
`.cpp:350-371`), and pumps input inside the `handleClient()` burst
(`.cpp:408-431`) plus an upload cancel check (`.cpp:306-315`), so Back stays
live. None of that is reachable in QEMU without a connection, which is why the
`SCANNING` gap above is the one that matters for the emulator lane.

## Change

`tools/ybx-system-emu/test_wifi_selection_state_machine.py` (new, host-only).

It slices the production definitions out of `WifiSelectionActivity.cpp` verbatim
(ctor, `onEnter`, `onExit`, `loop`, `startWifiScan`, `processWifiScanResults`,
`attemptConnection`, `checkConnectionStatus`, the auto-connect helpers, the
prompts, `onComplete`, the three touch handlers), compiles them against the
**unmodified** production header with a stubbed hardware boundary, and drives
them with a virtual clock and a scripted radio. `render()`/`listScreen()` are the
only production members replaced (drawing is out of scope; `listScreen`'s address
is taken by `onEnter`). A rename or a reshaped function fails the extraction
rather than passing vacuously.

Stubbed: `WiFi` client, `WifiCredentialStore`, `MappedInputManager`,
`UiAppHost`/`FreeInkApp` routing, `ButtonNavigator`, `ListNav`, activity
scheduler, `millis()`/`delay()`, NTP/trusted-time/plugin side effects, `ESP`,
`tr()` (stringified so the probe can assert *which* message was chosen).

Scenarios (24 tests, ~2.3 s): scan-never-completes, scan-failed, empty scan,
Retry tap, manual join with 15 s timeout, `WL_CONNECT_FAILED`,
`WL_NO_SSID_AVAIL`, "no path reaches CONNECTED without the radio" over five
no-radio branches, positive control with `link=connect@3000`, auto-connect
cascade (2 saved SSIDs, bounded), stuck auto-connect with Back/Confirm,
failed-saved-network → forget prompt → rescan, forget, keyboard cancel, hidden
SSID (typed SSID then password, and empty SSID), open network and saved-password
shortcuts, STA-mode-on-entry, plus three source-shape guards (render suppression,
the two timeout constants, the parent's exit-time reboot).

Run:

```
python3 tools/ybx-system-emu/test_wifi_selection_state_machine.py
# Ran 24 tests in 2.305s — OK   (host c++ -std=c++20 -fno-access-control)
```

The trace it prints (`TICK … state=… err=… scans=…`, `ATTEMPT`, `FINAL`) is
machine-readable, so a later lane can render a Wi-Fi capability panel from it
without touching the firmware. A live `/api` capability endpoint was **not**
added: the browser server and its emulator are shared and running, and this lane
must not restart or drive them.

## Risks and limits

* The probe does **not** establish what `esp_wifi` actually returns under the
  patched QEMU. It proves what the firmware does for each answer. Only a guest
  run (human-owned, >20 s, not done here) can say which branch the emulator is
  in; the earlier `x4pro-network-stall-*` capture stops at the mode picker.
* `ListNav`, `UiAppHost` touch routing and `ButtonNavigator` are stubs: row
  scrolling, tap-flash and long-press-to-forget geometry are not covered.
* Subactivity timing is idealised (keyboard result delivered one tick after
  launch); real keyboard latency is not modelled.
* No heap, flash, or display-timing measurement was taken; nothing here says the
  Wi-Fi flow fits in the X3 budget.
* The `SCANNING`-has-no-timer finding is a real firmware gap, but fixing it means
  touching shipped behaviour (a scan deadline in
  `WifiSelectionActivity::processWifiScanResults()`), which this lane was not
  allowed to do. It needs a human decision, and hardware verification before any
  PR.
