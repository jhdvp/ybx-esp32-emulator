"""Host-only state-machine probe for the factory Wi-Fi selection flow.

Run with: python3 tools/ybx-system-emu/test_wifi_selection_state_machine.py

No guest, no QEMU, no Docker, no firmware edit. This test slices the *production*
transition logic out of src/activities/network/WifiSelectionActivity.cpp, compiles
those definitions verbatim against the unmodified production header, and drives
them with a virtual clock and a stubbed radio. That answers, with the shipped
code rather than a re-implementation:

  * which states are bounded by a timer and which are not;
  * whether "no 802.11 hardware" shows up as a hang, a timeout, or a live loop;
  * how the user gets out of "Join a Network" when the radio never answers.

HARDWARE BOUNDARY (stubbed, see CORE_H): the Arduino WiFi client, the SD-backed
WifiCredentialStore, FreeInk rendering/UiAppHost routing, ButtonNavigator,
MappedInputManager, the activity scheduler, millis()/delay(), NTP/plugin side
effects. Everything that decides a WifiSelectionState transition is real.
"""

from pathlib import Path
import os
import re
import subprocess
import tempfile
import unittest

# This source-extraction test is an OPTIONAL cross-repository contract check.
# Never vendor proprietary/factory images or claim firmware behavior when no
# matching source tree is available. Standalone host tests still run.
ROOT = Path(os.environ.get(
    "YBX_FIRMWARE_ROOT",
    str(Path(__file__).resolve().parents[3] / "CrossPoint-firmware"),
)).expanduser().resolve()
SOURCE = ROOT / "src/activities/network/WifiSelectionActivity.cpp"
HEADER = ROOT / "src/activities/network/WifiSelectionActivity.h"

# Member definitions compiled verbatim from the production source.
EXTRACTED = [
    "WifiSelectionActivity::WifiSelectionActivity",
    "WifiSelectionActivity::onEnter",
    "WifiSelectionActivity::onExit",
    "WifiSelectionActivity::onRowEvent",
    "WifiSelectionActivity::onScanEvent",
    "WifiSelectionActivity::onPromptEvent",
    "WifiSelectionActivity::startWifiScan",
    "WifiSelectionActivity::processWifiScanResults",
    "WifiSelectionActivity::appendHiddenNetworkEntry",
    "WifiSelectionActivity::rebuildNetworkRowItems",
    "WifiSelectionActivity::selectNetwork",
    "WifiSelectionActivity::promptPasswordEntry",
    "WifiSelectionActivity::promptHiddenSsid",
    "WifiSelectionActivity::hasAttemptedAutoSsid",
    "WifiSelectionActivity::tryAutoConnectCredential",
    "WifiSelectionActivity::tryNextSavedNetworkFromScan",
    "WifiSelectionActivity::handleAutoConnectFailure",
    "WifiSelectionActivity::showNetworkListFromAutoConnect",
    "WifiSelectionActivity::attemptConnection",
    "WifiSelectionActivity::checkConnectionStatus",
    "WifiSelectionActivity::loop",
    "WifiSelectionActivity::getSignalStrengthIndicator",
    "WifiSelectionActivity::onComplete",
]

END_PATTERNS = {
    "WifiSelectionActivity::WifiSelectionActivity": r"\{\}\n",
}

# Drawing is out of scope; these two still need a definition (listScreen's
# address is taken by onEnter, render() is virtual).
STUBBED_MEMBERS = ["render", "listScreen"]


def extract_functions(source: str) -> str:
    """Slice each requested definition from its signature to its closing brace.

    Every member definition in this file closes with `}` in column 0, so the
    first such line after the signature ends the function. A missing or
    renamed production function fails the extraction instead of passing
    vacuously.
    """
    chunks = []
    # The anonymous namespace holds the ACTION_* ids referenced by onEnter().
    anon = re.search(r"^namespace \{\n.*?^\}  // namespace\n", source, re.DOTALL | re.MULTILINE)
    assert anon, "anonymous namespace with the ACTION_* ids is not where the probe expects"
    chunks.append(anon.group(0))

    for name in EXTRACTED:
        match = re.search(r"^[\w:<>,\* ]*?\b" + re.escape(name) + r"\s*\(", source, re.MULTILINE)
        assert match, f"production definition not found: {name}"
        body = source[match.start():]
        # Every member definition closes with `}` in column 0 except the
        # constructor, which ends in an empty body after its initializer list.
        end = re.search(END_PATTERNS.get(name, r"^\}\n"), body, re.MULTILINE)
        assert end, f"definition does not close where the probe expects: {name}"
        chunks.append(body[: end.end()])
    return "\n".join(chunks)


# --------------------------------------------------------------------------
# The stubbed hardware boundary.
# --------------------------------------------------------------------------
CORE_H = r"""
#pragma once
#include <algorithm>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <functional>
#include <memory>
#include <optional>
#include <string>
#include <variant>
#include <vector>

// ---- virtual clock -------------------------------------------------------
static unsigned long g_nowMs = 1000;  // ESP32 millis() is never 0 this early
static unsigned long g_maxBlockingMs = 0;  // longest single blocking delay seen
static unsigned long millis() { return g_nowMs; }
static void delay(unsigned long ms) {
  g_maxBlockingMs = std::max(g_maxBlockingMs, ms);
  g_nowMs += ms;
}
static void yield() {}

// ---- logging / ESP singleton --------------------------------------------
#define LOG_DBG(tag, ...) \
  do {                    \
  } while (0)
#define LOG_ERR(tag, ...) \
  do {                    \
  } while (0)
struct EspStub {
  int getFreeHeap() { return 300000; }
  void restart() { printf("EVENT restart\n"); }
};
static EspStub ESP;

// ---- tr(): keep the translation id visible in the trace -----------------
// STR_* are deliberately NOT defined, so tr(STR_FOO) stringifies to "STR_FOO"
// and the probe can assert which message the state machine selected.
#define tr(id) (#id)

// ---- esp_mac -------------------------------------------------------------
typedef int esp_err_t;
#define ESP_OK 0
#define ESP_ERR 1
#define ESP_MAC_WIFI_STA 1
static esp_err_t g_macReadResult = ESP_OK;
static esp_err_t esp_read_mac(uint8_t* mac, int) {
  for (int i = 0; i < 6; i++) mac[i] = static_cast<uint8_t>(0xAA + i);
  return g_macReadResult;
}

// ---- Arduino WiFi client: the piece QEMU does not model ------------------
typedef uint8_t wl_status_t;
#define WL_IDLE_STATUS 0
#define WL_NO_SSID_AVAIL 1
#define WL_CONNECTED 3
#define WL_CONNECT_FAILED 4
#define WL_DISCONNECTED 6
#define WIFI_SCAN_RUNNING (-1)
#define WIFI_SCAN_FAILED (-2)
#define WIFI_STA 1
#define WIFI_AP 2
#define WIFI_OFF 0
#define WIFI_MODE_NULL 0
#define WIFI_AUTH_OPEN 0
#define WIFI_ALL_CHANNEL_SCAN 0
#define WIFI_CONNECT_AP_BY_SIGNAL 0

struct IPAddress {
  uint8_t b[4] = {0, 0, 0, 0};
  uint8_t operator[](int i) const { return b[i]; }
};

// ScanBehaviour/LinkBehaviour enumerate how the esp_wifi driver can answer when
// no 802.11 radio responds to its requests. Which one the real driver picks
// under QEMU is NOT claimed here; every branch is exercised instead.
enum class ScanBehaviour { NEVER_COMPLETES, FAILS, RETURNS };
enum class LinkBehaviour { NEVER_CONNECTS, CONNECTS, FAILS, NO_SSID };

struct FakeAp {
  std::string ssid;
  int32_t rssi = -60;
  bool encrypted = true;
};

struct RadioState {
  ScanBehaviour scan = ScanBehaviour::RETURNS;
  LinkBehaviour link = LinkBehaviour::NEVER_CONNECTS;
  int scanTicksToComplete = 1;  // scanComplete() reports RUNNING this many times
  std::vector<FakeAp> aps;
  unsigned long connectedAfterMs = 0;  // only for LinkBehaviour::CONNECTS
  // observable side effects
  uint8_t mode = WIFI_OFF;
  bool connected = false;
  bool linkAttemptActive = false;
  int modeCalls = 0;
  int disconnects = 0;
  int scansStarted = 0;
  int scanDeletes = 0;
  std::vector<std::string> attempts;  // "ssid|password" in attempt order
};
static RadioState g_radio;

static unsigned long g_attemptStartMs = 0;

static wl_status_t fake_status() {
  if (!g_radio.linkAttemptActive) return g_radio.connected ? WL_CONNECTED : WL_DISCONNECTED;
  switch (g_radio.link) {
    case LinkBehaviour::NEVER_CONNECTS: return WL_DISCONNECTED;
    case LinkBehaviour::FAILS: return WL_CONNECT_FAILED;
    case LinkBehaviour::NO_SSID: return WL_NO_SSID_AVAIL;
    case LinkBehaviour::CONNECTS:
      if (g_nowMs - g_attemptStartMs >= g_radio.connectedAfterMs) return WL_CONNECTED;
      return WL_DISCONNECTED;
  }
  return WL_DISCONNECTED;
}
struct WiFiStub {
  void mode(uint8_t m) {
    g_radio.modeCalls++;
    g_radio.mode = (m == WIFI_OFF) ? WIFI_MODE_NULL : m;
  }
  uint8_t getMode() { return g_radio.mode; }
  void persistent(bool) {}
  void setScanMethod(int) {}
  void setSortMethod(int) {}
  void setHostname(const char*) {}
  void disconnect(bool = false, bool = false) {
    g_radio.disconnects++;
    g_radio.connected = false;
    g_radio.linkAttemptActive = false;
  }
  void softAPdisconnect(bool) { g_radio.disconnects++; }
  int scanNetworks(bool = false) {
    g_radio.scansStarted++;
    return 0;
  }
  int16_t scanComplete() {
    if (g_radio.scan == ScanBehaviour::NEVER_COMPLETES) return WIFI_SCAN_RUNNING;
    if (g_radio.scan == ScanBehaviour::FAILS) return WIFI_SCAN_FAILED;
    if (g_radio.scanTicksToComplete > 0) {
      g_radio.scanTicksToComplete--;
      return WIFI_SCAN_RUNNING;
    }
    return static_cast<int16_t>(g_radio.aps.size());
  }
  void scanDelete() { g_radio.scanDeletes++; }
  std::string SSID(int i) { return g_radio.aps[static_cast<size_t>(i)].ssid; }
  int32_t RSSI(int i) { return g_radio.aps[static_cast<size_t>(i)].rssi; }
  uint8_t encryptionType(int i) { return g_radio.aps[static_cast<size_t>(i)].encrypted ? 1 : WIFI_AUTH_OPEN; }
  int channel() { return 1; }
  IPAddress localIP() {
    IPAddress ip;
    ip.b[0] = 192;
    ip.b[1] = 168;
    ip.b[2] = 1;
    ip.b[3] = 42;
    return ip;
  }
  bool BSSID(uint8_t* bssid) {
    for (int i = 0; i < 6; i++) bssid[i] = 0;
    return true;
  }
  bool begin(const char* ssid) { return begin(ssid, ""); }
  bool begin(const char* ssid, const char* password) {
    g_radio.attempts.push_back(std::string(ssid) + "|" + (password ? password : ""));
    g_attemptStartMs = g_nowMs;
    g_radio.linkAttemptActive = true;
    return true;
  }
  wl_status_t status() {
    const wl_status_t s = fake_status();
    g_radio.connected = (s == WL_CONNECTED);
    return s;
  }
};
static WiFiStub WiFi;

// ---- credential store (SD card + hardware-key obfuscation in production) --
struct WifiCredential {
  std::string ssid;
  std::string password;
};
struct FakeCredentialStore {
  std::vector<WifiCredential> creds;
  std::string lastConnected;
  std::vector<std::string> writes;
  void loadFromFile() {}
  size_t getCredentialCount() const { return creds.size(); }
  std::string getLastConnectedSsid() const { return lastConnected; }
  void setLastConnectedSsid(const std::string& ssid) { lastConnected = ssid; }
  bool hasSavedCredential(const std::string& ssid) const {
    for (const auto& c : creds)
      if (c.ssid == ssid) return true;
    return false;
  }
  std::optional<WifiCredential> findCredential(const std::string& ssid) const {
    for (const auto& c : creds)
      if (c.ssid == ssid) return c;
    return std::nullopt;
  }
  bool addCredential(const std::string& ssid, const std::string& password) {
    writes.push_back("add:" + ssid);
    creds.push_back({ssid, password});
    return true;
  }
  bool removeCredential(const std::string& ssid) {
    writes.push_back("forget:" + ssid);
    creds.erase(std::remove_if(creds.begin(), creds.end(),
                               [&](const WifiCredential& c) { return c.ssid == ssid; }),
                creds.end());
    return true;
  }
};
static FakeCredentialStore g_store;
#define WIFI_STORE g_store

// ---- side effects on a successful connect --------------------------------
struct HalClockStub {
  bool isAvailable() { return false; }
  bool syncFromNTP() { return false; }
};
static HalClockStub halClock;
struct SettingsStub {
  int clockHasBeenSynced = 0;
  void saveToFile() {}
};
static SettingsStub SETTINGS;
namespace trustedtime {
inline void startSync() {}
}  // namespace trustedtime
namespace pluginevents {
template <typename Renderer>
inline void drain(Renderer*) {}
inline void refreshSubscriptions() {}
}  // namespace pluginevents

// ---- rendering -----------------------------------------------------------
struct GfxRenderer {
  int getScreenWidth() { return 480; }
  int getScreenHeight() { return 800; }
  int getLineHeight(int) { return 20; }
  std::string truncatedText(int, const char*, int) { return std::string(); }
  void clearScreen() {}
  void displayBuffer() {}
  void drawCenteredText(int, int, const char*, bool = true) {}
};
struct RenderLock {
  explicit RenderLock(class Activity&) {}
};

// ---- FreeInk UI surface used by the state machine -----------------------
namespace freeink {
namespace ui {
using ActionId = uint16_t;
struct ActionEvent {
  ActionId action = 0;
  int16_t value = -1;
  bool longPress = false;
  explicit operator bool() const { return action != 0; }
};
struct InputSnapshot {};
struct ListItem {
  const char* label = "";
  const char* value = "";
  int16_t actionValue = 0;
};
// Stand-in for the SDK viewport helper. The Wi-Fi state machine only reads
// visibleRows and asks the view to follow/scroll; list maths is not under test.
struct ListNav {
  int top = 0;
  int selected = 0;
  int visibleRows = 6;
  void reset() {
    top = 0;
    selected = 0;
  }
  bool scrollBy(int, int) { return false; }
  void follow(int) {}
};
}  // namespace ui
}  // namespace freeink

// ---- input manager -------------------------------------------------------
class MappedInputManager {
 public:
  // Same leading order as src/MappedInputManager.h:17 so the spec's button
  // indices mean what the firmware sees.
  enum class Button { Back, Confirm, Left, Right, Up, Down, Power, PageBack, PageForward, Home };
  enum class SwipeDir { None, Left, Right, Up, Down };

  uint32_t pressed = 0;
  uint32_t released = 0;
  uint32_t held = 0;
  bool touch = false;

  static uint32_t mask(Button b) { return 1u << static_cast<unsigned>(b); }
  bool wasPressed(Button b) const { return pressed & mask(b); }
  bool wasReleased(Button b) const { return released & mask(b); }
  bool isPressed(Button b) const { return held & mask(b); }
  bool wasHomeGesture() const { return false; }
  bool hasTouch() const { return touch; }
  SwipeDir wasSwipe(int* = nullptr, int* = nullptr) const { return SwipeDir::None; }
  void update(bool = false) {}
};

// ---- activity plumbing ---------------------------------------------------
// Payload structs mirror src/activities/ActivityResult.h:10-18.
struct WifiResult {
  bool connected = false;
  std::string ssid;
  std::string ip;
};
struct KeyboardResult {
  std::string text;
};
struct ActivityResult {
  bool isCancelled = false;
  std::variant<WifiResult, KeyboardResult> data = WifiResult{};
};
using ActivityResultHandler = std::function<void(const ActivityResult&)>;

class Activity {
 public:
  std::string name;
  GfxRenderer& renderer;
  MappedInputManager& mappedInput;
  ActivityResultHandler resultHandler;
  ActivityResult result;
  int updateRequests = 0;

  explicit Activity(std::string name, GfxRenderer& renderer, MappedInputManager& mappedInput)
      : name(std::move(name)), renderer(renderer), mappedInput(mappedInput) {}
  virtual ~Activity() = default;
  virtual void onEnter() {}
  virtual void onExit() {}
  virtual void loop() {}
  virtual void render(RenderLock&&) {}
  virtual bool skipLoopDelay() { return false; }
  virtual bool preventAutoSleep() { return false; }
  virtual void requestUpdate(bool = false) { updateRequests++; }
  virtual void requestUpdateAndWait() { updateRequests++; }
  void startActivityForResult(std::unique_ptr<Activity>&& activity, ActivityResultHandler handler);
  void setResult(ActivityResult&& r) { result = std::move(r); }
  void finish();
  static void onGoHome() { printf("EVENT goHome\n"); }
  static bool finished;
  static Activity* launched;
};

enum class InputType { Text, Number };
class KeyboardEntryActivity final : public Activity {
 public:
  KeyboardEntryActivity(GfxRenderer& r, MappedInputManager& m, const char*, std::string, int, InputType)
      : Activity("Keyboard", r, m) {}
};

// ---- UiAppHost / ButtonNavigator ----------------------------------------
class UiAppHost {
 public:
  struct Screen {
    freeink::ui::ListNav nav;
  };
  using UiScreen = Screen;
  struct App {
    int clears = 0;
    bool invalidatedFlag = false;
    void clearTapFlash() { clears++; }
    bool invalidated() const { return invalidatedFlag; }
    template <typename... Args>
    void on(Args...) {}
    template <typename... Args>
    void setScreen(Args...) {}
  };
  explicit UiAppHost(const GfxRenderer&) {}
  void resetUi() {}
  void renderUi() {}
  struct TouchRoute {
    freeink::ui::ActionEvent event{};
    bool routed = false;
    explicit operator bool() const { return static_cast<bool>(event); }
  };
  TouchRoute routeTouch(const MappedInputManager&, bool = false, bool = false) { return {}; }
  App app;
};

class ButtonNavigator {
 public:
  template <typename Fn>
  void onNext(Fn&&) {}
  template <typename Fn>
  void onPrevious(Fn&&) {}
  static int nextIndex(int i, int n) { return n == 0 ? 0 : (i + 1) % n; }
  static int previousIndex(int i, int n) { return n == 0 ? 0 : (i + n - 1) % n; }
};
"""

STUB_HEADERS = {
    "activities/Activity.h": '#pragma once\n#include "core.h"\n',
    "components/UiAppHost.h": '#pragma once\n#include "core.h"\n',
    "util/ButtonNavigator.h": '#pragma once\n#include "core.h"\n',
}

# --------------------------------------------------------------------------
# Driver: a minimal ActivityManager stand-in (one active activity, one
# subactivity, 20 ms loop cadence) plus a scenario spec on the command line.
# --------------------------------------------------------------------------
HARNESS_TAIL = r"""
// Drawing members the probe replaces (never reached by the state machine).
void WifiSelectionActivity::render(RenderLock&&) {}
void WifiSelectionActivity::listScreen(UiScreen&, void*) {}

bool Activity::finished = false;
Activity* Activity::launched = nullptr;
static ActivityResultHandler g_pendingHandler;  // the parent's continuation

void Activity::startActivityForResult(std::unique_ptr<Activity>&& activity, ActivityResultHandler handler) {
  launched = activity.release();  // harness owns it; deleted on delivery
  g_pendingHandler = std::move(handler);
  launched->onEnter();
  printf("EVENT subactivity=%s\n", launched->name.c_str());
}

void Activity::finish() {
  finished = true;
  printf("EVENT finish cancelled=%d\n", result.isCancelled ? 1 : 0);
}
"""

HARNESS_DRIVER = r"""
namespace driver {

struct Press {
  int tick;
  int button;
};

struct Spec {
  bool autoConnect = true;
  bool touch = false;
  ScanBehaviour scan = ScanBehaviour::RETURNS;
  int scanTicksToComplete = 1;
  LinkBehaviour link = LinkBehaviour::NEVER_CONNECTS;
  int connectedAfterMs = 0;
  std::vector<FakeAp> aps;
  std::vector<WifiCredential> creds;
  std::string lastConnected;
  std::vector<Press> presses;    // one-frame wasPressed edges
  std::vector<Press> holds;      // sustained isPressed state
  std::vector<Press> releases;   // one-frame wasReleased edges
  std::vector<std::string> keyboards = {"text:hunter2"};  // each: text:<t> | cancel | empty
  int keyboardTick = -1;                  // -1 = deliver one tick after launch
  int keyboardCount = 0;
  int rowTick = -1;
  int rowIndex = 0;
  int promptTick = -1;
  int promptValue = 0;
  int retryTick = -1;
};

static std::vector<std::string> splitAll(const std::string& text, char sep) {
  std::vector<std::string> out;
  size_t start = 0;
  while (true) {
    const size_t next = text.find(sep, start);
    out.push_back(text.substr(start, next == std::string::npos ? next : next - start));
    if (next == std::string::npos) break;
    start = next + 1;
  }
  return out;
}

static std::vector<std::string> splitList(const std::string& text, char sep) {
  std::vector<std::string> out;
  for (const auto& part : splitAll(text, sep))
    if (!part.empty()) out.push_back(part);
  return out;
}

static bool has(const std::string& arg, const std::string& key) { return arg.rfind(key, 0) == 0; }

static Spec parseArg(const std::string& arg, Spec spec) {
  if (arg == "noautoconnect") spec.autoConnect = false;
  else if (arg == "touch") spec.touch = true;
  else if (has(arg, "scan=never")) spec.scan = ScanBehaviour::NEVER_COMPLETES;
  else if (has(arg, "scan=fail")) spec.scan = ScanBehaviour::FAILS;
  else if (has(arg, "scanticks=")) spec.scanTicksToComplete = std::atoi(arg.c_str() + 10);
  else if (has(arg, "link=idle")) spec.link = LinkBehaviour::NEVER_CONNECTS;
  else if (has(arg, "link=fail")) spec.link = LinkBehaviour::FAILS;
  else if (has(arg, "link=nossid")) spec.link = LinkBehaviour::NO_SSID;
  else if (has(arg, "link=connect@")) {
    spec.link = LinkBehaviour::CONNECTS;
    spec.connectedAfterMs = std::atoi(arg.c_str() + 14);
  } else if (has(arg, "aps=")) {
    spec.aps.clear();
    for (const auto& ap : splitList(arg.substr(4), ',')) {
      FakeAp entry;
      const std::vector<std::string> parts = splitList(ap, ':');
      entry.ssid = parts.empty() ? "" : parts[0];
      if (parts.size() > 1) entry.rssi = std::atoi(parts[1].c_str());
      // "ssid:rssi:open" marks an unencrypted AP; "ssid" or "ssid:rssi" is encrypted.
      entry.encrypted = std::find(parts.begin(), parts.end(), "open") == parts.end();
      spec.aps.push_back(entry);
    }
  } else if (has(arg, "creds=")) {
    spec.creds.clear();
    for (const auto& cred : splitList(arg.substr(6), ',')) {
      const size_t colon = cred.find(':');
      spec.creds.push_back({cred.substr(0, colon), colon == std::string::npos ? "" : cred.substr(colon + 1)});
    }
  } else if (has(arg, "last=")) spec.lastConnected = arg.substr(5);
  else if (has(arg, "press=")) {
    for (const auto& p : splitList(arg.substr(6), ',')) {
      const size_t colon = p.find(':');
      spec.presses.push_back({std::atoi(p.c_str()), std::atoi(p.c_str() + colon + 1)});
    }
  } else if (has(arg, "hold=")) {
    for (const auto& p : splitList(arg.substr(5), ',')) {
      const size_t colon = p.find(':');
      spec.holds.push_back({std::atoi(p.c_str()), std::atoi(p.c_str() + colon + 1)});
    }
  } else if (has(arg, "release=")) {
    for (const auto& p : splitList(arg.substr(8), ',')) {
      const size_t colon = p.find(':');
      spec.releases.push_back({std::atoi(p.c_str()), std::atoi(p.c_str() + colon + 1)});
    }
  } else if (has(arg, "kbd=")) spec.keyboards = {arg.substr(4)};
  else if (has(arg, "kbds=")) spec.keyboards = splitList(arg.substr(5), ',');
  else if (has(arg, "kbdtick=")) spec.keyboardTick = std::atoi(arg.substr(8).c_str());
  else if (has(arg, "row=")) {
    const std::vector<std::string> parts = splitList(arg.substr(4), ':');
    spec.rowTick = std::atoi(parts[0].c_str());
    spec.rowIndex = parts.size() > 1 ? std::atoi(parts[1].c_str()) : 0;
  } else if (has(arg, "prompt=")) {
    const std::vector<std::string> parts = splitList(arg.substr(7), ':');
    spec.promptTick = std::atoi(parts[0].c_str());
    spec.promptValue = parts.size() > 1 ? std::atoi(parts[1].c_str()) : 0;
  } else if (has(arg, "retry=")) spec.retryTick = std::atoi(arg.substr(6).c_str());
  return spec;
}

}  // namespace driver

int main(int argc, char** argv) {
  if (argc < 3) {
    fprintf(stderr, "usage: harness <ticks> <tickms> [spec...]\n");
    return 2;
  }
  const int totalTicks = std::atoi(argv[1]);
  const int tickMs = std::atoi(argv[2]);
  driver::Spec spec;
  for (int i = 3; i < argc; i++) spec = driver::parseArg(argv[i], spec);

  GfxRenderer renderer;
  MappedInputManager input;
  input.touch = spec.touch;
  g_radio = RadioState{};
  g_radio.scan = spec.scan;
  g_radio.scanTicksToComplete = spec.scanTicksToComplete;
  g_radio.link = spec.link;
  g_radio.connectedAfterMs = static_cast<unsigned long>(spec.connectedAfterMs);
  g_radio.aps = spec.aps;
  g_store = FakeCredentialStore{};
  g_store.creds = spec.creds;
  g_store.lastConnected = spec.lastConnected;
  g_nowMs = 1000;
  g_maxBlockingMs = 0;
  Activity::finished = false;
  Activity::launched = nullptr;

  WifiSelectionActivity activity(renderer, input, spec.autoConnect);
  activity.onEnter();
  int subLaunchedAt = -1;

  for (int tick = 0; tick < totalTicks; tick++) {
    input.pressed = input.released = input.held = 0;
    for (const auto& p : spec.presses)
      if (p.tick == tick) input.pressed |= 1u << p.button;
    for (const auto& p : spec.holds)
      if (p.tick >= tick) input.held |= 1u << p.button;
    for (const auto& p : spec.releases)
      if (p.tick == tick) input.released |= 1u << p.button;

    // Touch taps: dispatched straight to the action handlers the FreeInkApp
    // routing calls on the device (same functions, same ActionEvent).
    if (spec.rowTick == tick) {
      freeink::ui::ActionEvent event;
      event.action = 1;  // ACTION_ROW
      event.value = static_cast<int16_t>(spec.rowIndex);
      WifiSelectionActivity::onRowEvent(event, &activity);
    }
    if (spec.retryTick == tick) {
      freeink::ui::ActionEvent event;
      event.action = 2;  // ACTION_SCAN
      WifiSelectionActivity::onScanEvent(event, &activity);
    }
    if (spec.promptTick == tick) {
      freeink::ui::ActionEvent event;
      event.action = 3;  // ACTION_PROMPT
      event.value = static_cast<int16_t>(spec.promptValue);
      WifiSelectionActivity::onPromptEvent(event, &activity);
    }

    // The subactivity owns input while it is up; deliver its result once.
    if (Activity::launched != nullptr) {
      if (subLaunchedAt < 0) subLaunchedAt = tick;
      const int deliverAt = spec.keyboardTick >= 0 ? spec.keyboardTick : subLaunchedAt + 1;
      if (tick >= deliverAt) {
        ActivityResult result;
        const std::string& entry =
            spec.keyboards[std::min<size_t>(spec.keyboardCount, spec.keyboards.size() - 1)];
        spec.keyboardCount++;
        if (entry == "cancel") {
          result.isCancelled = true;
          result.data = KeyboardResult{};
        } else if (entry == "empty") {
          result.data = KeyboardResult{};
        } else {
          KeyboardResult text;
          text.text = entry.rfind("text:", 0) == 0 ? entry.substr(5) : entry;
          result.data = text;
        }
        g_pendingHandler(result);
        g_pendingHandler = nullptr;
        delete Activity::launched;
        Activity::launched = nullptr;
        subLaunchedAt = -1;
      }
    }

    if (Activity::finished) break;
    if (Activity::launched == nullptr) activity.loop();
    g_nowMs += static_cast<unsigned long>(tickMs);

    printf("TICK %d t=%lu state=%s real=%zu total=%zu auto=%d err=%s connected=%d finished=%d "
           "maxdelay=%lu scans=%d disconnects=%d\n",
           tick, g_nowMs, stateName(activity.state), activity.realNetworkCount, activity.networks.size(),
           activity.autoConnecting ? 1 : 0, activity.connectionError.c_str(),
           fake_status() == WL_CONNECTED ? 1 : 0, Activity::finished ? 1 : 0, g_maxBlockingMs, g_radio.scansStarted,
           g_radio.disconnects);
  }
  for (const auto& attempt : g_radio.attempts) printf("ATTEMPT %s\n", attempt.c_str());
  for (const auto& write : g_store.writes) printf("STORE %s\n", write.c_str());
  std::string connectedSsid;
  std::string connectedIp;
  if (const auto* wifi = std::get_if<WifiResult>(&activity.result.data)) {
    connectedSsid = wifi->ssid;
    connectedIp = wifi->ip;
  }
  printf("FINAL state=%s connected=%d finished=%d cancelled=%d ssid=%s ip=%s mode=%d err=%s scans=%d\n",
         stateName(activity.state), fake_status() == WL_CONNECTED ? 1 : 0, Activity::finished ? 1 : 0,
         activity.result.isCancelled ? 1 : 0, connectedSsid.c_str(), connectedIp.c_str(),
         static_cast<int>(g_radio.mode), activity.connectionError.c_str(), g_radio.scansStarted);
  activity.onExit();
  return 0;
}
"""

STATE_NAME_FN = r"""
const char* stateName(WifiSelectionState s) {
  switch (s) {
    case WifiSelectionState::AUTO_CONNECTING: return "AUTO_CONNECTING";
    case WifiSelectionState::SCANNING: return "SCANNING";
    case WifiSelectionState::NETWORK_LIST: return "NETWORK_LIST";
    case WifiSelectionState::HIDDEN_SSID_ENTRY: return "HIDDEN_SSID_ENTRY";
    case WifiSelectionState::PASSWORD_ENTRY: return "PASSWORD_ENTRY";
    case WifiSelectionState::CONNECTING: return "CONNECTING";
    case WifiSelectionState::CONNECTED: return "CONNECTED";
    case WifiSelectionState::SAVE_PROMPT: return "SAVE_PROMPT";
    case WifiSelectionState::CONNECTION_FAILED: return "CONNECTION_FAILED";
    case WifiSelectionState::FORGET_PROMPT: return "FORGET_PROMPT";
  }
  return "?";
}
"""


def build_harness(tmpdir: Path) -> Path:
    inc = tmpdir / "inc"
    for rel, content in STUB_HEADERS.items():
        path = inc / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    (inc / "core.h").write_text(CORE_H, encoding="utf-8")

    source = SOURCE.read_text(encoding="utf-8")
    header = HEADER.read_text(encoding="utf-8")
    for member in STUBBED_MEMBERS:
        assert f"{member}(" in header, f"production header no longer declares {member}()"

    harness = (
        '#include "core.h"\n'
        # Aliases the production .cpp establishes above the extracted bodies.
        "namespace fui = freeink::ui;\n"
        "using std::find_if;\n"
        '#include "WifiSelectionActivity.h"\n'
        + STATE_NAME_FN
        + extract_functions(source)
        + HARNESS_TAIL
        + HARNESS_DRIVER
    )
    (tmpdir / "harness.cpp").write_text(harness, encoding="utf-8")
    binary = tmpdir / "harness"
    result = subprocess.run(
        [
            "c++",
            "-std=c++20",
            "-w",
            "-fno-access-control",  # the driver reads state-machine fields
            f"-I{inc}",
            f"-I{ROOT / 'src/activities/network'}",
            str(tmpdir / "harness.cpp"),
            "-o",
            str(binary),
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"harness failed to compile:\n{result.stderr}\n--- generated source ---\n{harness}"
        )
    return binary


class WifiStateMachineProbe(unittest.TestCase):
    """Each scenario is named after the radio response it simulates."""

    @classmethod
    def setUpClass(cls):
        if not (SOURCE.is_file() and HEADER.is_file()):
            raise unittest.SkipTest(
                "cross-repository Wi-Fi contract requires YBX_FIRMWARE_ROOT "
                "pointing at a compatible firmware checkout"
            )
        cls._tmp = tempfile.TemporaryDirectory()
        cls.binary = build_harness(Path(cls._tmp.name))

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def run_scenario(self, *spec: str, ticks: int = 2000, tick_ms: int = 20) -> dict:
        args = [str(self.binary), str(ticks), str(tick_ms), *spec]
        out = subprocess.run(args, capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stderr)
        parsed = {"ticks": [], "attempts": [], "store": [], "events": [], "final": {}}
        for line in out.stdout.splitlines():
            if line.startswith("TICK "):
                row = {}
                for field in line.split()[1:]:
                    key, _, value = field.partition("=")
                    row[key] = int(value) if key in {"t", "tick", "real", "total", "auto", "connected", "finished",
                                                     "maxdelay", "scans", "disconnects"} else value
                parsed["ticks"].append(row)
            elif line.startswith("ATTEMPT "):
                parsed["attempts"].append(line[len("ATTEMPT "):])
            elif line.startswith("STORE "):
                parsed["store"].append(line[len("STORE "):])
            elif line.startswith("EVENT "):
                parsed["events"].append(line[len("EVENT "):])
            elif line.startswith("FINAL "):
                for field in line.split()[1:]:
                    key, _, value = field.partition("=")
                    parsed["final"][key] = value
        return parsed

    @staticmethod
    def states(result: dict) -> list:
        return [row["state"] for row in result["ticks"]]

    # --- scan phase ---------------------------------------------------------
    def test_scan_that_never_completes_parks_in_scanning_without_a_timer(self):
        """processWifiScanResults() returns early on WIFI_SCAN_RUNNING (cpp:205-208)."""
        result = self.run_scenario("scan=never", "noautoconnect", ticks=4000)  # 80 s of virtual time
        self.assertEqual(set(self.states(result)), {"SCANNING"})
        self.assertEqual(result["final"]["connected"], "0")
        # The activity keeps looping and keeps repainting requests alive: this is
        # a screen with no exit timer, not a wedged task.
        self.assertGreater(len(result["ticks"]), 3000)
        self.assertLessEqual(int(result["ticks"][-1]["maxdelay"]), 100)

    def test_back_escapes_a_stuck_scan(self):
        result = self.run_scenario("scan=never", "noautoconnect", "press=50:0", ticks=200)
        self.assertEqual(result["final"]["finished"], "1")
        self.assertEqual(result["final"]["cancelled"], "1")
        self.assertEqual(result["final"]["connected"], "0")

    def test_scan_failure_still_opens_the_list_with_the_hidden_row(self):
        result = self.run_scenario("scan=fail", "noautoconnect", ticks=50)
        self.assertEqual(result["ticks"][1]["state"], "NETWORK_LIST")
        self.assertEqual(result["ticks"][1]["real"], 0)
        self.assertEqual(result["ticks"][1]["total"], 1)  # synthetic "Add hidden network..."

    def test_empty_scan_opens_the_list(self):
        result = self.run_scenario("aps=", "noautoconnect", "scanticks=3", ticks=50)
        self.assertEqual(result["ticks"][3]["state"], "NETWORK_LIST")
        self.assertEqual(result["ticks"][3]["real"], 0)

    def test_retry_button_rescans_from_the_empty_list(self):
        result = self.run_scenario("aps=", "noautoconnect", "retry=20", ticks=60)
        states = self.states(result)
        self.assertEqual(states[1], "NETWORK_LIST")
        # The Retry tap starts a second scan; it settles back on the list.
        self.assertEqual(result["ticks"][19]["scans"], 1)
        self.assertEqual(result["ticks"][20]["scans"], 2)
        self.assertEqual(states[20], "NETWORK_LIST")

    # --- connect phase ------------------------------------------------------
    def test_manual_join_times_out_after_15s_and_never_reports_connected(self):
        result = self.run_scenario("aps=LabNet:-55", "noautoconnect", "row=5", "kbd=text:secret", "link=idle",
                                   ticks=1200)
        states = self.states(result)
        self.assertIn("CONNECTING", states)
        self.assertNotIn("CONNECTED", states)
        self.assertNotIn("SAVE_PROMPT", states)
        self.assertEqual(result["final"]["state"], "CONNECTION_FAILED")
        self.assertEqual(result["final"]["err"], "STR_ERROR_CONNECTION_TIMEOUT")
        self.assertEqual(result["final"]["connected"], "0")
        first = states.index("CONNECTING")
        failed = states.index("CONNECTION_FAILED")
        elapsed = int(result["ticks"][failed]["t"]) - int(result["ticks"][first]["t"])
        # attemptConnection() spends delay(100) before WiFi.begin(), and every
        # printed timestamp is one loop pass ahead of the sampled clock.
        self.assertGreaterEqual(elapsed, 15000 - 100 - 40)  # CONNECTION_TIMEOUT_MS
        self.assertLess(elapsed, 15000 + 100)  # the next loop pass, not a hang

    def test_driver_reported_link_failure_ends_immediately(self):
        for link, expected in (("fail", "STR_ERROR_GENERAL_FAILURE"), ("nossid", "STR_ERROR_NETWORK_NOT_FOUND")):
            with self.subTest(link=link):
                result = self.run_scenario("aps=LabNet:-55", "noautoconnect", "row=5", "kbd=text:x", f"link={link}",
                                           ticks=200)
                self.assertEqual(result["final"]["err"], expected)
                self.assertEqual(result["final"]["connected"], "0")
                self.assertEqual(result["attempts"], ["LabNet|x"])

    def test_no_path_reaches_connected_without_the_radio_reporting_it(self):
        for spec in ("scan=never", "scan=fail", "link=idle", "link=fail", "link=nossid"):
            with self.subTest(spec=spec):
                result = self.run_scenario(spec, "aps=Home:-60", "creds=Home:pw", ticks=2000)
                states = set(self.states(result))
                self.assertNotIn("CONNECTED", states)
                self.assertNotIn("SAVE_PROMPT", states)
                self.assertEqual(result["final"]["connected"], "0")
                self.assertEqual(result["store"], [])  # no credential write, no last-SSID commit
                self.assertEqual(result["final"]["ip"], "")

    def test_positive_control_the_flow_does_complete_when_the_radio_answers(self):
        """Guards against a probe that only ever sees failures."""
        result = self.run_scenario("aps=Home:-60", "noautoconnect", "row=5", "kbd=text:pw",
                                   "link=connect@3000", "press=250:1", ticks=400)
        self.assertIn("SAVE_PROMPT", self.states(result))
        self.assertEqual(result["store"], ["add:Home"])  # "Yes" on the save prompt
        self.assertEqual(result["final"]["connected"], "1")
        self.assertEqual(result["final"]["ssid"], "Home")
        self.assertEqual(result["final"]["ip"], "192.168.1.42")

    # --- auto-connect -------------------------------------------------------
    def test_auto_connect_cascade_is_bounded_by_attempted_ssids(self):
        result = self.run_scenario("creds=Home:pw,Cafe:pw", "last=Home", "aps=Home:-60,Cafe:-70", "link=idle",
                                   ticks=2000)
        self.assertEqual(result["attempts"], ["Home|pw", "Cafe|pw"])
        self.assertEqual(result["final"]["state"], "NETWORK_LIST")
        self.assertEqual(result["final"]["connected"], "0")
        # AUTO_CONNECTION_TIMEOUT_MS (7 s) per saved network, so ~14 s + scan time.
        settled = self.states(result).index("NETWORK_LIST")
        self.assertLess(int(result["ticks"][settled]["t"]), 1000 + 2 * 7000 + 3000)
        self.assertGreater(int(result["ticks"][settled]["t"]), 1000 + 2 * 7000 - 4000)

    def test_auto_connect_cascade_survives_a_failed_scan(self):
        result = self.run_scenario("creds=Home:pw", "last=Home", "scan=fail", "link=idle", ticks=2000)
        self.assertEqual(result["attempts"], ["Home|pw"])
        self.assertEqual(result["final"]["state"], "NETWORK_LIST")

    def test_auto_connect_with_a_stuck_scan_is_cancellable_but_not_escapable(self):
        """SCANNING honours Back and Confirm (cpp:596-605); neither yields the list."""
        back = self.run_scenario("creds=Home:pw", "last=Home", "scan=never", "link=idle", "press=400:0", ticks=500)
        self.assertEqual(back["final"]["cancelled"], "1")
        show = self.run_scenario("creds=Home:pw", "last=Home", "scan=never", "link=idle", "press=400:1", ticks=500)
        self.assertEqual(show["final"]["state"], "SCANNING")

    # --- recovery screens ---------------------------------------------------
    def test_failed_saved_network_offers_forget_then_rescans(self):
        result = self.run_scenario("aps=Home:-60", "creds=Home:pw", "noautoconnect", "row=5", "press=900:0",
                                   "prompt=950:0", ticks=1100)
        states = self.states(result)
        self.assertIn("CONNECTION_FAILED", states)
        self.assertIn("FORGET_PROMPT", states)
        self.assertEqual(result["store"], [])  # "Cancel" keeps the credential
        self.assertEqual(states[899], "CONNECTION_FAILED")
        self.assertEqual(states[900], "FORGET_PROMPT")  # the Back edge on the failure screen
        self.assertEqual(result["ticks"][950]["scans"], 2)  # "Cancel" restarts the scan
        self.assertEqual(states[-1], "NETWORK_LIST")

    def test_forget_prompt_forgets_when_asked(self):
        result = self.run_scenario("aps=Home:-60", "creds=Home:pw", "noautoconnect", "row=5", "press=900:0",
                                   "prompt=950:1", ticks=1100)
        self.assertEqual(result["store"], ["forget:Home"])

    def test_keyboard_cancel_returns_to_the_list_without_touching_the_radio(self):
        result = self.run_scenario("aps=LabNet:-55", "noautoconnect", "row=5", "kbd=cancel", ticks=100)
        self.assertEqual(result["attempts"], [])
        self.assertEqual(result["final"]["state"], "NETWORK_LIST")

    def test_hidden_network_placeholder_prompts_for_an_ssid_then_a_password(self):
        result = self.run_scenario("aps=LabNet:-55", "noautoconnect", "row=5:1",
                                   "kbds=text:GhostNet,text:pw", "link=idle", ticks=200)
        states = self.states(result)
        self.assertIn("HIDDEN_SSID_ENTRY", states)
        self.assertIn("CONNECTING", states)
        self.assertEqual(result["attempts"], ["GhostNet|pw"])  # typed SSID, then typed password

    def test_hidden_network_empty_ssid_falls_back_to_the_list(self):
        result = self.run_scenario("aps=LabNet:-55", "noautoconnect", "row=5:1", "kbd=empty", ticks=200)
        self.assertEqual(result["attempts"], [])
        self.assertEqual(result["final"]["state"], "NETWORK_LIST")

    def test_open_network_skips_the_keyboard(self):
        result = self.run_scenario("aps=OpenNet:-60:open", "noautoconnect", "row=5", "link=idle", ticks=100)
        self.assertEqual(result["attempts"], ["OpenNet|"])
        self.assertNotIn("EVENT subactivity=Keyboard", result["events"])

    def test_saved_password_skips_the_keyboard(self):
        result = self.run_scenario("aps=Home:-60", "creds=Home:pw", "noautoconnect", "row=5", "link=idle", ticks=100)
        self.assertEqual(result["attempts"], ["Home|pw"])

    def test_entering_the_screen_switches_the_radio_to_station_mode(self):
        """startWifiScan() calls WiFi.mode(WIFI_STA): the reason the parent
        CrossPointWebServerActivity::onExit() (cpp:125-133) reboots the device
        even when the user never got past the scan screen."""
        for spec in ("scan=never", "scan=fail", "aps="):
            with self.subTest(spec=spec):
                result = self.run_scenario(spec, "noautoconnect", ticks=20)
                self.assertEqual(result["final"]["mode"], "1")  # WIFI_STA

    # --- source-shape guards ------------------------------------------------
    def test_render_suppresses_the_keyboard_transition_states(self):
        source = SOURCE.read_text(encoding="utf-8")
        render = source[source.index("void WifiSelectionActivity::render("):]
        guard = render[: render.index("renderer.clearScreen()")]
        self.assertIn("PASSWORD_ENTRY", guard)
        self.assertIn("HIDDEN_SSID_ENTRY", guard)

    def test_timeout_constants_the_recovery_analysis_depends_on(self):
        header = HEADER.read_text(encoding="utf-8")
        self.assertIn("CONNECTION_TIMEOUT_MS = 15000", header)
        self.assertIn("AUTO_CONNECTION_TIMEOUT_MS = 7000", header)
        self.assertNotIn("SCAN_TIMEOUT", header)  # SCANNING has no timer at all

    def test_parent_reboots_the_device_after_any_wifi_use(self):
        """Source-shape guard (not a behavioural probe): Join a Network powers the
        radio on entry and the parent reboots on exit, which is what a guest that
        'vanishes' after a failed Wi-Fi attempt actually is."""
        parent = (ROOT / "src/activities/network/CrossPointWebServerActivity.cpp").read_text()
        exit_body = parent[parent.index("void CrossPointWebServerActivity::onExit()"):]
        exit_body = exit_body[: exit_body.index("\n}\n")]
        self.assertIn("WiFi.getMode() != WIFI_MODE_NULL", exit_body)
        self.assertIn("silentRestart();", exit_body)
        main = (ROOT / "src/main.cpp").read_text()
        restart = main[main.index("static void silentRestartTo("):]
        self.assertIn("ESP.restart();", restart[: restart.index("\n}\n")])
        # The Join-Network defrag reboot is skipped on touch boards (X4 Pro).
        join = main[main.index("void silentRestartToJoinNetwork()"):]
        self.assertIn("BoardConfig::hasTouch()", join[: join.index("\n}\n")])

    def test_scan_and_connect_are_the_only_polling_states(self):
        """checkConnectionStatus() early-returns outside CONNECTING/AUTO_CONNECTING."""
        source = SOURCE.read_text(encoding="utf-8")
        body = source[source.index("void WifiSelectionActivity::checkConnectionStatus()"):]
        body = body[: body.index("\n}\n")]
        self.assertIn("state != WifiSelectionState::CONNECTING", body)
        self.assertIn("state != WifiSelectionState::AUTO_CONNECTING", body)


if __name__ == "__main__":
    unittest.main()
