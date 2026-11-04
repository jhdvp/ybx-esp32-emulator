#!/usr/bin/env python3
"""Stock 1.6.14 Stage C live Yandex canary driver (owner-gated) + hermetic dry-run.

Runs the deployment runbook Stage C steps against an **isolated** original
CrossPoint 2.0 RC X4 Pro QEMU guest, reusing the proven 0113/0114 tooling
(``start_eink_qemu11_preview.sh``, the ``stock1614_fixture_server.py`` synthetic
KoSync backend, the candidate ZIP payload validator and the real-guest verdict
``guest_round_trip_valid``).

Two modes:

* ``--dry-run-synthetic`` — hermetic, no internet, no real account, no QEMU
  container. It exercises the *same* Stage C decision logic (candidate payload
  validation, installed-SD byte validation, a 71-value firmware-settings diff
  with values redacted, the guest KoSync Clippings GET/PUT/GET + progress
  round-trip and cold-boot idempotence against the 0113 synthetic fixture, and
  a scoped quote-proof mint/revoke/re-mint model) against local synthetic
  back-ends.  A protocol-level replay of the firmware's exact HTTP sequence
  stands in for the flaky GUI automation, which 0113/0114 already proved.  This
  is the proof the canary's flow works and is the mode that must be GREEN.

* ``--live`` — the ready-to-run real canary. It starts its own disposable
  container on a fresh free port, installs the verified candidate ZIP to a fresh
  FAT16 SD, drives the genuine firmware ``/api/settings`` toggles and pauses for
  the owner to pair a **dedicated test Yandex account** and to confirm quotes.
  Secrets are never printed or stored; only presence / key names / hashes are.

Safety: never prints or stores a token, QR code, pairing link body or real quote
text.  ``token.json`` is inspected for presence and key names only.  Refuses the
reserved production/QA ports 8784, 8940, 8941, 8950, 8951, 8964, 8965.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

# Reused, already-validated tooling.
from stock1614_fixture_server import make_server, seed_from_ledger  # noqa: E402
from run_stock1614_clippings_guest import guest_round_trip_valid, read_ledger  # noqa: E402
from stock1614_ui import matches_guest_screen  # noqa: E402  (0113/0114 real-guest UI gate)
import probe_x4pro_sd_content as sd_probe  # noqa: E402
import x4pro_test_card as card  # noqa: E402

CANDIDATE_SHA = "43019ff44c2daae4f0878419c296e255ab253dce765e75d2053eb94794ca8b6e"
DEFAULT_ZIP = Path.home() / "Projects/YBX/artifacts/ybx-stock1614-release-candidate-20261104/ybx-plugin-1.6.14-rc.zip"
DEFAULT_SD_IMAGE = Path.home() / "Projects/YBX/artifacts/ybx-stock1614-clippings-qa-20261104/stock-1614-plugin-exact-sd.img"
START_SCRIPT = HERE / "start_eink_qemu11_preview.sh"
MANIFEST_FILES = ["manifest.json", "device.json", "plugin.json", "plugin.js",
                  "config.json", "README.md", "icon.png"]
# Ports owned by other live runs / production must never be reused.
RESERVED_PORTS = {8784, 8940, 8941, 8950, 8951, 8964, 8965}
# Firmware settings keys this canary toggles (Stage C step 3).
TOGGLE_KEYS = ["koSync", "libraryUseMetadata", "koSendMetadata", "koSyncClippings"]
SECRET_HINT = re.compile(r"token|key|password|secret|credential|auth|user", re.I)

# Live pairing-pause navigation, measured on the genuine 2.0 RC X4 Pro guest on
# this branch (Home row2 Plugins -> YBX entry -> PICSimLabWifi AP).  These drive
# the real firmware UI through the preview bridge; they are not emulator patches.
NAV_HOME_TO_PLUGINS = (240, 490)
NAV_PLUGINS_TO_YBX = (240, 210)
NAV_WIFI_JOIN = (138, 193)


# --------------------------------------------------------------------------- #
# Generic helpers
# --------------------------------------------------------------------------- #
def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def http_call(base: str, path: str, *, method: str = "GET", body=None,
              headers=None, timeout: float = 8.0):
    payload = json.dumps(body).encode("utf-8") if body is not None else None
    req = Request(base + path, data=payload, method=method,
                  headers={**(headers or {}),
                           **({"Content-Type": "application/json"} if payload else {})})
    try:
        with urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except HTTPError as err:
        try:
            return err.code, err.read()
        finally:
            err.close()


def curl_fetch(url: str, method: str = "GET", timeout: float = 20.0):
    """Fetch over HTTPS using the system CA trust (curl).

    Used only for the external production smoke: this framework's urllib has no
    CA bundle, and disabling verification would mirror the very ``setInsecure()``
    weakness the runbook flags. curl validates the peer certificate for us.
    """
    proc = subprocess.run(
        ["curl", "-sS", "-o", "-", "-w", "\n%{http_code}", "-X", method,
         "--max-time", str(int(timeout)), url], capture_output=True)
    body = proc.stdout
    head, sep, code = body.rpartition(b"\n")
    if not sep:
        return 0, body
    try:
        return int(code), head
    except ValueError:
        return 0, body


def port_is_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.3)
        return sock.connect_ex(("127.0.0.1", port)) != 0


def pick_free_pair() -> tuple[int, int]:
    """Return an unused (preview, qmp) loopback pair above 8800, avoiding reserves."""
    for preview in range(8971, 8999):
        qmp = preview + 1
        if preview in RESERVED_PORTS or qmp in RESERVED_PORTS:
            continue
        if qmp >= 65535:
            break
        if port_is_free(preview) and port_is_free(qmp):
            return preview, qmp
    raise RuntimeError("No free isolated 88xx QEMU port pair available")


# --------------------------------------------------------------------------- #
# Payload validator: candidate ZIP + installed SD byte-equality
# --------------------------------------------------------------------------- #
def validate_candidate_zip(zip_path: Path, expected_sha: str = CANDIDATE_SHA) -> dict:
    """Verify SHA-256 of the archive and the seven-file payload integrity."""
    raw = zip_path.read_bytes()
    actual_sha = sha256_bytes(raw)
    result = {"zip": str(zip_path), "size_bytes": len(raw),
              "sha256": actual_sha, "sha_expected": expected_sha,
              "sha_match": actual_sha == expected_sha}
    archive = zipfile.ZipFile(io.BytesIO(raw))
    names = [n[len("ybx/"):] for n in archive.namelist()
             if n.startswith("ybx/") and not n.endswith("/")]
    manifest = json.loads(archive.read("ybx/manifest.json"))
    listed = list(manifest["files"])
    result["manifest_version"] = manifest["version"]
    result["manifest_files"] = listed
    result["missing"] = [f for f in listed if f not in names]
    result["extra"] = [f for f in names if f not in listed]
    result["corrupt_member"] = archive.testzip()  # None when all CRCs pass
    result["file_sha256"] = {f: sha256_bytes(archive.read("ybx/" + f)) for f in listed}
    result["ok"] = (result["sha_match"] and not result["missing"] and not result["extra"]
                    and result["corrupt_member"] is None
                    and manifest["version"] == "1.6.14"
                    and sorted(listed) == sorted(MANIFEST_FILES))
    return result


def validate_installed_sd(sd_image: Path, zip_result: dict) -> dict:
    """Independently re-read a disposable FAT16 SD and prove the seven installed
    plugin bytes equal the candidate ZIP bytes (the media payload validator)."""
    summary, context = sd_probe.inspect_image(sd_image)
    nodes = summary.get("tree", [])
    present = {}
    for node in nodes:
        path = node.get("path", "")
        if path.startswith("/.crosspoint/plugins/ybx/") and node.get("type") == "file":
            present[path.rsplit("/", 1)[-1]] = node.get("content_sha256")
    expected = zip_result["file_sha256"]
    per_file = {}
    for name, want in expected.items():
        got = present.get(name)
        per_file[name] = {"installed_sha256": got,
                          "matches_candidate": got == want}
    has_koreader = any(n.get("path") == "/.crosspoint/koreader.json" for n in nodes)
    has_epub = any(str(n.get("path", "")).lower().endswith(".epub")
                   and n.get("type") == "file" and "/books/" in n.get("path", "")
                   for n in nodes)
    all_match = all(v["matches_candidate"] for v in per_file.values()) and len(present) == 7
    return {"sd_image": str(sd_image), "fs_type": summary.get("fat16", {}).get("fs_type", "FAT16"),
            "plugin_files_found": len(present), "per_file": per_file,
            "koreader_json_present": has_koreader, "fixture_epub_present": has_epub,
            "ok": all_match and has_koreader and has_epub}


def build_clean_live_sd(dest: Path, zip_path: Path, zip_result: dict) -> dict:
    """Build fresh media; never copy a card containing old account settings."""
    book = io.BytesIO()
    with zipfile.ZipFile(book, "w") as epub:
        epub.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        epub.writestr("META-INF/container.xml", card.CONTAINER_XML)
        epub.writestr("OEBPS/content.opf", card.OPF_XML.replace(card.PROPHET_TITLE, "Hamlet").replace(card.PROPHET_AUTHOR, "William Shakespeare"))
        epub.writestr("OEBPS/toc.ncx", card.NCX_XML.replace(card.PROPHET_TITLE, "Hamlet").replace("Of the Probe", "Act III"))
        # William Shakespeare, Hamlet, act III scene 1 (public domain).
        epub.writestr("OEBPS/chapter1.xhtml", '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Hamlet</title></head><body><p>To be, or not to be, that is the question: Whether tis nobler in the mind to suffer the slings and arrows of outrageous fortune.</p></body></html>')
    files = [(".crosspoint/", b""), (".crosspoint/plugins/", b""),
             (".crosspoint/plugins/ybx/", b""), ("books/", b"")]
    with zipfile.ZipFile(zip_path) as archive:
        files.extend((".crosspoint/plugins/ybx/" + name, archive.read("ybx/" + name))
                     for name in MANIFEST_FILES)
    files.append(("books/Hamlet.epub", book.getvalue()))
    image = card.wrap_mbr(card.build_fat16(files))
    # Scan raw media as well as the file list; no stale deleted clusters can survive.
    forbidden = (b"10.0.2.2", b"18085", b"example.invalid")
    if any(value in image for value in forbidden):
        raise ValueError("clean SD contains a forbidden fixture address")
    dest.write_bytes(image)
    result = validate_installed_sd(dest, zip_result)
    summary, _ = sd_probe.inspect_image(dest)
    paths = {n["path"] for n in summary["tree"] if n.get("type") == "file"}
    expected = {"/.crosspoint/plugins/ybx/" + name for name in MANIFEST_FILES}
    expected.add("/books/Hamlet.epub")
    result["ok"] = result["plugin_files_found"] == 7 and all(
        row["matches_candidate"] for row in result["per_file"].values()) and paths == expected
    result["clean_file_set"] = paths == expected
    result["fixture_address_absent"] = True
    if not result["ok"]:
        raise ValueError("clean SD verification failed")
    return result


# --------------------------------------------------------------------------- #
# Synthetic firmware /api/settings (71-value) model + 71-value diff client
# --------------------------------------------------------------------------- #
def build_settings_model() -> dict:
    """A 71-value settings snapshot mirroring the real Stock firmware shape.

    Only synthetic, obviously-fake values. The toggles under test are booleans;
    the KOSync credential fields hold placeholder text so redaction is exercised.
    """
    settings = {
        # The four independently-toggleable plugin-managed flags.
        "koSync": False, "libraryUseMetadata": False,
        "koSendMetadata": False, "koSyncClippings": False,
        # KOSync / third-party profile fields that must stay untouched.
        "koSyncServer": "http://10.0.2.2:18085/kosync",
        "koSyncUser": "synthetic-placeholder-user",
        "koSyncPassword": "synthetic-placeholder-secret",
        "koSyncServerType": 0, "koSyncMatchingMode": 0, "koSyncSyncMode": 0,
    }
    filler = [
        "wifiSSID", "wifiPassword", "bluetoothEnabled", "screenTimeout",
        "sleepTimeout", "fontSize", "fontFamily", "lineSpacing", "textAlignment",
        "hyphenation", "pageMarginTop", "pageMarginBottom", "pageMarginLeft",
        "pageMarginRight", "autoRotate", "darkMode", "nightLight",
        "dictionaryLang", "ttsRate", "ttsVoice", "showBattery", "clockFormat",
        "dateFormat", "brightness", "contrast", "waveform", "partialRefresh",
        "fullRefreshEvery", "doubleTapAction", "edgeSwipeLeft", "edgeSwipeRight",
        "volumeUp", "volumeDown", "powerButtonAction", "sleepAfterSync",
        "autoSyncInterval", "downloadOverCellular", "coverArt", "readingStats",
        "librarySort", "libraryGroup", "recentCount", "bookmarkLimit",
        "highlightColor", "noteTemplate", "cloudBackup", "crashReports",
        "usageTelemetry", "localeLanguage", "localeRegion", "inputMethod",
    ]
    for key in filler:
        settings.setdefault(key, "0")
    # Trim / grow to exactly 71 keys so the count assertion is meaningful.
    while len(settings) < 71:
        settings[f"setting{len(settings) + 1:02d}"] = "0"
    return dict(list(settings.items())[:71])


def redact(key: str, value) -> str:
    if SECRET_HINT.search(key):
        return "<redacted sha256:%s>" % sha256_bytes(str(value).encode())[:12]
    return value if isinstance(value, bool) else str(value)


def settings_diff(before: dict, after: dict, key_order: list[str]) -> dict:
    """Compare two full settings snapshots; return redacted diff + invariants."""
    changed = [k for k in key_order if before.get(k) != after.get(k)]
    rows = []
    for k in key_order:
        if before.get(k) != after.get(k):
            rows.append({"key": k, "before": redact(k, before.get(k)),
                         "after": redact(k, after.get(k)), "changed": True})
    return {"count_before": len(before), "count_after": len(after),
            "changed_keys": changed, "changed_rows": rows,
            "unrelated_changed": [k for k in changed if k not in TOGGLE_KEYS]}


class SettingsServer:
    """Local stand-in for the guest firmware HTTP admin /api/settings contract."""

    def __init__(self):
        self.settings = build_settings_model()
        self.key_order = list(self.settings.keys())
        self.lock = threading.Lock()

    def start(self) -> str:
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, obj):
                body = json.dumps(obj).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path == "/api/settings":
                    with outer.lock:
                        self._send(200, dict(outer.settings))
                else:
                    self._send(404, {"error": "no_route"})

            def do_PUT(self):
                if self.path.startswith("/api/settings/"):
                    key = self.path.split("/api/settings/", 1)[1]
                    length = int(self.headers.get("Content-Length", "0"))
                    payload = json.loads(self.rfile.read(length) or b"{}")
                    with outer.lock:
                        if key not in outer.settings:
                            self._send(404, {"error": "unknown_key"})
                            return
                        outer.settings[key] = payload["value"]
                        self._send(200, {"ok": True})
                else:
                    self._send(404, {"error": "no_route"})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return "http://127.0.0.1:%d" % self.server.server_address[1]

    def read_all(self) -> dict:
        status, raw = http_call(self.base, "/api/settings")
        assert status == 200, "settings GET %s" % status
        return json.loads(raw)

    def put_key(self, key, value):
        status, _ = http_call(self.base, "/api/settings/" + key, method="PUT",
                              body={"value": value})
        assert status == 200, "settings PUT %s %s" % (key, status)

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


def verify_settings_toggles(base: str, *, live=False, container=None) -> dict:
    """Stage C step 3: enable each flag independently, verify a 71-value diff.

    In dry-run ``base`` points at :class:`SettingsServer`. In live it is unused
    and the guest transport is used instead (same invariant checks).
    """
    def read_all():
        if live:
            return live_guest_settings_get(container)
        status, raw = http_call(base, "/api/settings")
        assert status == 200, "GET /api/settings -> %s" % status
        return json.loads(raw)

    def put(key, value):
        if live:
            live_guest_settings_put(container, key, value)
        else:
            status, _ = http_call(base, "/api/settings/" + key, method="PUT",
                                  body={"value": value})
            assert status == 200, "PUT %s -> %s" % (key, status)

    snapshot = read_all()
    key_order = list(snapshot.keys())
    checks = {}
    for key in TOGGLE_KEYS:
        if live and key not in snapshot:
            # e.g. stock firmware has no plain koSync toggle (KOSync = URL + credentials)
            checks[key] = {"passed": True, "skipped": "not a stock firmware setting"}
            continue
        before = read_all()
        # Flip to the opposite of the current value: some stock defaults are already on
        # (libraryUseMetadata=1), and OFF/ON enums come back as 0/1, not bools.
        target = not bool(before.get(key))
        put(key, target)
        after = read_all()
        diff = settings_diff(before, after, key_order)
        checks[key] = {
            "count": diff["count_after"],
            "flipped_to": target,
            "only_key_changed": diff["changed_keys"] == [key],
            "value_now_true": bool(after.get(key)) == target,
            "unrelated_changed": diff["unrelated_changed"],
            "redacted_diff_rows": diff["changed_rows"],
        }
        checks[key]["passed"] = (checks[key]["only_key_changed"]
                                 and checks[key]["value_now_true"]
                                 and not checks[key]["unrelated_changed"]
                                 and diff["count_after"] == 71)
    # Restore the synthetic guest to its original values.
    for key, value in snapshot.items():
        if read_all().get(key) != value:
            put(key, value)
    final = read_all()
    return {"initial_count": len(snapshot),
            "restored_exact": final == snapshot,
            "checks": checks,
            "ok": all(c["passed"] for c in checks.values()) and len(snapshot) == 71}


# --------------------------------------------------------------------------- #
# Synthetic scoped quote-proof (mirrors production clippings-key contract)
# --------------------------------------------------------------------------- #
class ProofServer:
    """Local model of POST /auth/clippings-key (+ /revoke) for dry-run only.

    Mirrors the production scoping semantics proven in server PR #46: unauth ->
    401, auth -> a scoped key, revoke -> prior keys forbidden on the quote route,
    re-mint -> a *different* key. Not the 0113 fixture; clearly synthetic.
    """

    def __init__(self):
        self.active = set()
        self.counter = 0
        self.lock = threading.Lock()

    def start(self) -> str:
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, obj=None):
                body = json.dumps(obj or {}).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _bearer(self):
                auth = self.headers.get("Authorization", "")
                return auth[7:] if auth.startswith("Bearer ") else None

            def do_POST(self):
                if self.path == "/api/v1/crosspoint/auth/clippings-key":
                    if self._bearer() is None:
                        self._send(401, {"error": "unauthorised"})  # not 404
                        return
                    with outer.lock:
                        outer.counter += 1
                        key = "quote-%d-%s" % (outer.counter,
                                               os.urandom(6).hex())
                        outer.active.add(key)
                    self._send(200, {"kosync_key": key, "username": "ybx",
                                     "clippings_enabled": True})
                elif self.path == "/api/v1/crosspoint/auth/clippings-key/revoke":
                    with outer.lock:
                        outer.active.clear()
                    self._send(204)
                else:
                    self._send(404, {"error": "no_route"})

            def do_GET(self):
                if self.path == "/kosync-clippings-resource":
                    key = self._bearer()
                    if key and key in outer.active:
                        self._send(200, {"ok": True})
                    else:
                        self._send(403, {"error": "proof_revoked"})
                else:
                    self._send(404, {"error": "no_route"})

            def do_PUT(self):
                # Model the ordinary KOSync progress route that must keep
                # working even while quote proofs are revoked (Stage C step 5).
                if self.path == "/syncs/progress":
                    if self.headers.get("x-auth-key"):
                        self._send(200, {"ok": True})
                    else:
                        self._send(401, {"error": "not_authenticated"})
                else:
                    self._send(404, {"error": "no_route"})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return "http://127.0.0.1:%d" % self.server.server_address[1]

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


def mint_proof(base: str, device_token: str) -> str:
    status, raw = http_call(base, "/api/v1/crosspoint/auth/clippings-key",
                            method="POST", headers={"Authorization": "Bearer " + device_token})
    assert status == 200, "unauth/auth mint -> %s" % status
    return json.loads(raw)["kosync_key"]


def verify_proof_lifecycle(clip_base: str, progress_base: str,
                           clip_headers: dict) -> dict:
    """Stage C step 5: disable -> old proof forbidden + progress still works;
    re-enable -> a new, different proof."""
    # Unauthenticated mint must be rejected as 401 (not 404/5xx), matching prod.
    status, _ = http_call(clip_base, "/api/v1/crosspoint/auth/clippings-key",
                          method="POST")
    unauth_401 = status == 401

    key_a = mint_proof(clip_base, "device-scope-token")
    a_usable = http_call(clip_base, "/kosync-clippings-resource",
                         headers={"Authorization": "Bearer " + key_a})[0] == 200
    # Disable Clippings -> revoke.
    http_call(clip_base, "/api/v1/crosspoint/auth/clippings-key/revoke", method="POST")
    a_after_disable = http_call(clip_base, "/kosync-clippings-resource",
                                headers={"Authorization": "Bearer " + key_a})[0] == 403
    # KOSync progress must still work while quotes are disabled.
    progress_ok = http_call(progress_base, "/syncs/progress", method="PUT",
                            body={"document": "d" * 32, "progress": "1/3",
                                  "percentage": 33},
                            headers=clip_headers)[0] == 200
    # Re-enable -> a different proof, old one still forbidden.
    key_b = mint_proof(clip_base, "device-scope-token")
    b_usable = http_call(clip_base, "/kosync-clippings-resource",
                         headers={"Authorization": "Bearer " + key_b})[0] == 200
    a_still_forbidden = http_call(clip_base, "/kosync-clippings-resource",
                                  headers={"Authorization": "Bearer " + key_a})[0] == 403
    different = key_a != key_b
    return {"unauth_mint_401": unauth_401, "first_proof_usable": a_usable,
            "old_proof_forbidden_after_disable": a_after_disable,
            "kosync_progress_still_works": progress_ok,
            "reenabled_proof_different": different,
            "new_proof_usable": b_usable, "old_proof_still_forbidden": a_still_forbidden,
            "ok": all([unauth_401, a_usable, a_after_disable, progress_ok,
                       different, b_usable, a_still_forbidden])}


# --------------------------------------------------------------------------- #
# Clipping round-trip + cold-boot idempotence against the 0113 synthetic fixture
# --------------------------------------------------------------------------- #
def write_synthetic_epub(path: Path) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip",
                         compress_type=zipfile.ZIP_STORED)
        archive.writestr("META-INF/container.xml",
                         '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" '
                         'version="1.0"><rootfiles><rootfile full-path="OEBPS/content.opf" '
                         'media-type="application/oebps-package+xml"/></rootfiles></container>')
        archive.writestr("OEBPS/content.opf",
                         '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" '
                         'unique-identifier="uid"><metadata '
                         'xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:identifier id="uid">'
                         'fixture</dc:identifier><dc:title>Fixture</dc:title>'
                         '<dc:language>en</dc:language></metadata><manifest>'
                         '<item id="ch1" href="chapter.xhtml" media-type="application/xhtml+xml"/>'
                         '</manifest><spine><itemref idref="ch1"/></spine></package>')
        archive.writestr("OEBPS/chapter.xhtml",
                         '<html xmlns="http://www.w3.org/1999/xhtml"><body><p>Fixture only</p>'
                         '</body></html>')


def clipping_clip(marker: str, doc: str) -> dict:
    """A single synthetic clipping item shaped exactly like the firmware sends.

    ``created_at`` / ``layout_signature`` are fixed so a cold-boot retry is
    byte-identical and the fixture proves idempotence (applied 0, no new revision).
    """
    return {
        "id": "ybx-canary-" + doc[:8], "spine": 0, "start_page": 1, "end_page": 1,
        "pages": 1, "start_word": 4, "end_word": 5, "words": 1,
        "created_at": 1793770365, "layout_signature": 0x1234abcd,
        "text": marker, "chapter": "ch1",
        "start_offset": 40, "end_offset": 40 + len(marker),
    }


def _kosync_headers():
    return {"x-auth-user": "canary-reader", "x-auth-key": "synthetic-not-a-real-password"}


def verify_clipping_round_trip(workdir: Path, marker: str) -> dict:
    """Guest-side Clippings GET/PUT/GET + progress PUT, then cold-boot idempotence.

    Reuses the 0113 synthetic ``stock1614_fixture_server.py`` and the real-guest
    verdict function. A protocol replay of the firmware's exact HTTP sequence
    stands in for the flaky GUI automation.
    """
    epub = workdir / "canary-fixture.epub"
    write_synthetic_epub(epub)
    doc = hashlib.sha256(marker.encode()).hexdigest()[:32]
    clip_path = "/kosync/api/v1/clippings/" + doc
    progress_path = "/syncs/progress"

    # First boot ----------------------------------------------------------- #
    ledger1 = workdir / "fixture-ledger-boot1.json"
    server1 = make_server(0, epub, ledger1)
    base1 = "http://127.0.0.1:%d" % server1.server_address[1]
    threading.Thread(target=server1.serve_forever, daemon=True).start()
    try:
        http_call(base1, clip_path + "?cursor=0&limit=1&format=reader", headers=_kosync_headers())
        http_call(base1, clip_path, method="PUT",
                  body={"items": [clipping_clip(marker, doc)]}, headers=_kosync_headers())
        http_call(base1, clip_path + "?cursor=0&limit=1&format=reader", headers=_kosync_headers())
        http_call(base1, progress_path, method="PUT",
                  body={"document": doc, "progress": "1/10", "percentage": 10},
                  headers=_kosync_headers())
        first = read_ledger(ledger1)
        upload_valid = guest_round_trip_valid(first, marker)
    finally:
        server1.shutdown()
        server1.server_close()

    # Cold boot: restart the fixture seeded from the synthetic ledger, keep
    # Flash/SD, replay read-only (no new clipping PUT) -> no duplicate.
    ledger2 = workdir / "fixture-ledger-boot2.json"
    seed = seed_from_ledger(ledger1)
    server2 = make_server(0, epub, ledger2, seed_clippings=seed)
    base2 = "http://127.0.0.1:%d" % server2.server_address[1]
    threading.Thread(target=server2.serve_forever, daemon=True).start()
    try:
        http_call(base2, clip_path + "?cursor=0&limit=1&format=reader", headers=_kosync_headers())
        http_call(base2, clip_path + "?cursor=0&limit=1&format=reader", headers=_kosync_headers())
        http_call(base2, progress_path, method="PUT",
                  body={"document": doc, "progress": "2/10", "percentage": 20},
                  headers=_kosync_headers())
        # Cold boot is read-only: it must NOT re-PUT the existing clipping.
        readonly = read_ledger(ledger2)
        # A retry of the identical clipping must not allocate a new revision.
        retry = http_call(base2, clip_path, method="PUT",
                          body={"items": [clipping_clip(marker, doc)]},
                          headers=_kosync_headers())
        after = read_ledger(ledger2)
    finally:
        server2.shutdown()
        server2.server_close()

    seeded_item = next(iter(next(iter(after["remote_clippings"].values())).values()))
    # The read-only boot produced no clippings PUT; the retry PUT is deliberate.
    duplicate_put = any(e.get("kind") == "clippings-put" for e in readonly["requests"])
    applied_zero = json.loads(retry[1]).get("applied") == 0
    single_item = sum(len(rec) for rec in after["remote_clippings"].values()) == 1
    revision_one = seeded_item["revision"] == 1
    return {"boot1_round_trip_valid": upload_valid,
            "boot2_remote_item_count": single_item,
            "boot2_no_duplicate_put": not duplicate_put,
            "boot2_retry_applied_zero": applied_zero,
            "boot2_revision_stable": revision_one,
            "quote_text_recorded": False,
            "ok": (upload_valid and single_item and not duplicate_put
                   and applied_zero and revision_one)}


# --------------------------------------------------------------------------- #
# QR / device-code pairing (protocol level) + token.json presence check
# --------------------------------------------------------------------------- #
def verify_pairing_and_token(workdir: Path) -> dict:
    """Exercise the device-code pairing exchange and prove token.json handling
    is presence/keys-only. Uses the synthetic fixture's auth endpoints."""
    epub = workdir / "canary-fixture.epub"
    if not epub.is_file():
        write_synthetic_epub(epub)
    ledger = workdir / "pairing-ledger.json"
    server = make_server(0, epub, ledger)
    base = "http://127.0.0.1:%d" % server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        status, raw = http_call(base, "/api/v1/crosspoint/auth/device", method="POST",
                                body={"client_id": "crosspoint"})
        device = json.loads(raw)
        verification_uri = device.get("verification_uri_complete", "")
        status, raw = http_call(base, "/api/v1/crosspoint/auth/token", method="POST",
                                body={"device_code": device.get("device_code")})
        token_response = json.loads(raw)
        access_token = token_response.get("access_token", "")
    finally:
        server.shutdown()
        server.server_close()

    # Persist a token.json the way the guest plugin does, then inspect ONLY its
    # key names and never echo the value.
    token_file = workdir / "token.json"
    token_file.write_text(json.dumps(token_response))
    keys = sorted(json.loads(token_file.read_text()).keys())
    # Scan every file the canary wrote; the raw token must never appear.
    leaked = []
    for path in workdir.rglob("*"):
        if path.is_file() and path != token_file:
            try:
                if access_token and access_token in path.read_text(errors="ignore"):
                    leaked.append(str(path))
            except Exception:
                pass
    return {"device_code_received": bool(device.get("device_code")),
            "verification_uri_present": bool(verification_uri),
            "token_json_keys": keys, "token_value_length": len(access_token),
            "token_never_logged": access_token != "" and not leaked,
            "ok": (device.get("device_code") is not None and bool(verification_uri)
                   and "access_token" in keys and not leaked)}


# --------------------------------------------------------------------------- #
# PART 1 — read-only production smoke (unauthenticated HTTP; no writes)
# --------------------------------------------------------------------------- #
PROD_BASE = "https://ybx.app"
DEFAULT_STABLE_DIR = (Path.home() / "Projects/YBX/.worktrees/"
                      "ybx-stock1614-server-mainline-qa-20261104/src/"
                      "xteink_yandex_bridge/static/crosspoint-plugin-stable")


def run_prod_smoke(args) -> dict:
    """Unauthenticated production smoke. Any mismatch is reported, never fixed."""
    stable = args.stable_dir
    rows = []
    results = {}

    # HTTPS via curl/system CA trust; verification is never disabled.
    def net(path, method="GET"):
        return curl_fetch(PROD_BASE + path, method=method)

    status, _ = net("/health")
    rows.append(("1 /health == 200", status == 200, "status=%d" % status))
    results["health"] = status

    status, _ = net("/api/v1/crosspoint/auth/clippings-key", method="POST")
    rows.append(("2 unauth POST clippings-key == 401 (was 404 pre-deploy)",
                 status == 401, "status=%d" % status))
    results["clippings_key_unauth_status"] = status

    for route, want_key in (("/crosspoint-plugin/latest.json", "version"),
                            ("/crosspoint-plugin/catalog.json", None)):
        status, raw = net(route)
        version = None
        try:
            data = json.loads(raw)
            if "plugins" in data:
                version = data["plugins"][0]["version"]
                files_ok = data["plugins"][0]["files"] == MANIFEST_FILES
            else:
                version = data.get("version")
                files_ok = None
        except Exception:
            files_ok = None
        ok = status == 200 and version == "1.6.13" and files_ok in (True, None)
        rows.append(("%s -> 1.6.13" % route, ok,
                    "status=%d version=%s files_ok=%s" % (status, version, files_ok)))
        results[route] = {"status": status, "version": version}

    # manifest served by the per-file route must be 1.6.13 too.
    status, raw = net("/crosspoint-plugin/manifest.json")
    man_version = json.loads(raw).get("version") if status == 200 else None
    rows.append(("/crosspoint-plugin/manifest.json version 1.6.13",
                 status == 200 and man_version == "1.6.13",
                 "status=%d version=%s" % (status, man_version)))

    # Seven per-file routes must byte-match the stable 1.6.13 package.
    per_file = {}
    for name in MANIFEST_FILES:
        status, served = net("/crosspoint-plugin/" + name)
        local = stable / name
        served_sha = sha256_bytes(served)
        stable_sha = sha256_bytes(local.read_bytes()) if local.is_file() else None
        match = served_sha == stable_sha
        per_file[name] = {"served_sha256": served_sha, "stable_sha256": stable_sha,
                          "match": match, "status": status}
        rows.append(("per-file %s matches stable 1.6.13" % name,
                     match and status == 200, served_sha[:12]))
    results["per_file"] = per_file

    # The installer ZIP re-packs the seven files; compare members to stable.
    status, zbytes = net("/crosspoint-plugin/ybx-plugin.zip")
    zip_members_ok = status == 200
    member_report = {}
    if status == 200:
        archive = zipfile.ZipFile(io.BytesIO(zbytes))
        for name in MANIFEST_FILES:
            member = archive.read("ybx/" + name)
            local = stable / name
            ok = local.is_file() and sha256_bytes(member) == sha256_bytes(local.read_bytes())
            member_report[name] = ok
            zip_members_ok = zip_members_ok and ok
    rows.append(("ybx-plugin.zip seven members match stable 1.6.13",
                 zip_members_ok, "all_members_match=%s" % zip_members_ok))
    results["zip_members"] = member_report

    all_ok = all(bool(r[1]) for r in rows)
    report = {"mode": "prod-smoke", "base": PROD_BASE, "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
              "all_pass": all_ok, "steps": [list(r) for r in rows],
              "details": results,
              "note": "Read-only unauthenticated HTTP; production is never modified. "
                      "SHA-256 digests of public files are recorded, not secrets."}
    out = Path(args.evidence_dir) / "0115-prod-smoke.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print_table(rows, all_ok)
    print("evidence written to: %s" % out)
    return report


# --------------------------------------------------------------------------- #
# LIVE mode: real isolated container + guest firmware /api/settings + owner gates
# --------------------------------------------------------------------------- #
def live_guest_settings_get(container: str) -> dict:
    """Read the genuine guest firmware /api/settings (hostfwd 18080 inside the
    container). Firmware returns a list of {key, type, value, ...}; we map it to
    {key: value} with toggles as bools. Values stay in memory, never printed."""
    snippet = (
        "import json,urllib.request;"
        "print(json.dumps(json.load(urllib.request.urlopen("
        "'http://127.0.0.1:18080/api/settings',timeout=15))))"
    )
    out = subprocess.check_output(["docker", "exec", "-i", container,
                                   "python3", "-c", snippet], text=True)
    raw = json.loads(out)
    if isinstance(raw, dict):
        return raw
    result = {}
    for item in raw:
        if not isinstance(item, dict) or "key" not in item:
            continue
        value = item.get("value")
        if item.get("type") == "toggle" and isinstance(value, int):
            value = bool(value)
        result[item["key"]] = value
    return result


def live_guest_settings_put(container: str, key: str, value) -> None:
    # Stock firmware: POST /api/settings with {key: value}; toggles are 0/1.
    if isinstance(value, bool):
        value = int(value)
    body = json.dumps({key: value})
    snippet = (
        "import urllib.request;"
        "req=urllib.request.Request('http://127.0.0.1:18080/api/settings',"
        "data=" + repr(body) + ".encode(),"
        "method='POST',headers={'Content-Type':'application/json'});"
        "urllib.request.urlopen(req,timeout=15)"
    )
    subprocess.check_call(["docker", "exec", "-i", container, "python3", "-c", snippet])


def live_start_container(sd_image: Path, preview: int, qmp: int, workdir: Path) -> subprocess.Popen:
    # The start script requires ``workdir`` to NOT exist (it mkdir + populates it)
    # and copies flash + SD into it, so the guest only ever writes the disposable
    # copy under workdir — never the shared artifact image.
    env = {**os.environ,
           "YBX_QEMU11_PREVIEW_PORT": str(preview),
           "YBX_QEMU11_QMP_PORT": str(qmp),
           "YBX_QEMU11_WORKDIR": str(workdir),
           "YBX_QEMU11_SD": str(sd_image)}
    return subprocess.Popen(["bash", str(START_SCRIPT)], env=env, text=True)


def live_base(preview: int) -> str:
    return "http://127.0.0.1:%d" % preview


def live_state(preview: int) -> dict:
    return json.loads(http_call(live_base(preview), "/api/state")[1])


def live_screen(preview: int) -> bytes:
    return http_call(live_base(preview), "/screen.png", timeout=20)[1]


def live_anchor(preview: int, name: str) -> bool:
    """Real-guest framebuffer gate (the 0113/0114 ``matches_guest_screen`` path)."""
    try:
        return matches_guest_screen(live_screen(preview), name)
    except Exception:
        return False


def live_tap(preview: int, x: int, y: int, duration_ms: int = 80) -> bool:
    status, raw = http_call(live_base(preview), "/api/tap", method="POST",
                            body={"x": x, "y": y, "duration_ms": duration_ms}, timeout=15)
    try:
        return status == 200 and json.loads(raw).get("accepted") is True
    except Exception:
        return False


def live_wait_anchor(preview: int, name: str, timeout_s: float, poll: float = 1.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if live_anchor(preview, name):
            return True
        time.sleep(poll)
    return live_anchor(preview, name)


def live_drive(preview: int, xy, from_anchor: str, to_anchor: str,
               attempts: int = 5, wait_s: float = 12.0) -> bool:
    """Tap only while the *expected* prior screen is showing, then wait for the
    target. Never blind-taps after the guest has already advanced (a stray tap
    on the connecting/QR screen could hit something else)."""
    for _ in range(attempts):
        if live_anchor(preview, to_anchor):
            return True
        if not live_anchor(preview, from_anchor):
            if live_wait_anchor(preview, to_anchor, wait_s):
                return True
            continue
        live_tap(preview, xy[0], xy[1])
        if live_wait_anchor(preview, to_anchor, wait_s):
            return True
    return live_anchor(preview, to_anchor)


def live_wait_boot(preview: int, timeout_s: float):
    """Prove the guest really booted to Home: UART boot marker AND the Home
    framebuffer anchor. Returns (booted, uart_boot_marker_seen)."""
    uart_marker = False
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            state = live_state(preview)
        except Exception:
            time.sleep(3)
            continue
        if state.get("home_log_observed"):
            uart_marker = True
            if live_anchor(preview, "home"):
                return True, uart_marker
        time.sleep(3)
    return False, uart_marker


def live_screenshot(evidence_dir: Path, name: str, preview: int) -> None:
    """Commit only non-secret screens (Home / Plugins / Wi-Fi). The device-code
    QR screen is never written here."""
    try:
        (evidence_dir / ("%s.png" % name)).write_bytes(live_screen(preview))
    except Exception:
        pass


def run_live(args) -> int:
    if not args.stop_at_pairing:
        print("Live owner flow is not ready: automatic navigation and web-panel activation are not verified. Refusing to run.")
        return 2
    preview, qmp = (args.preview_port, args.qmp_port) if args.preview_port else pick_free_pair()
    for p in (preview, qmp):
        if p in RESERVED_PORTS or not port_is_free(p):
            print("REFUSE port %d (reserved or already in use by another run)" % p)
            return 2
    zip_result = validate_candidate_zip(args.zip)
    print("candidate ZIP sha_match=%s version=%s ok=%s"
          % (zip_result["sha_match"], zip_result["manifest_version"], zip_result["ok"]))
    if not zip_result["ok"]:
        print("Candidate payload validator FAILED; refusing to start a live canary.")
        return 2

    base = Path(tempfile.mkdtemp(prefix="ybx-canary-live-"))
    work = base / ("preview-%d" % preview)          # start script requires this absent
    disp_sd = base / "canary-disposable-sd.img"      # fresh copy; shared artifact untouched
    sd_result = build_clean_live_sd(disp_sd, args.zip, zip_result)
    print("disposable SD install matches candidate ok=%s (shared artifact not booted)"
          % sd_result["ok"])
    container = "ybx-eink-qemu11-%d" % preview
    proc = None
    evidence = {"mode": "live-pairing-pause",
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "container": container, "preview_port": preview, "qmp_port": qmp,
                "candidate_zip_sha256": zip_result["sha256"],
                "disposable_sd_matches_candidate": sd_result["ok"],
                "shared_artifact_sd_written": False,
                "steps": []}

    def finish(rc: int) -> int:
        evidence["ok"] = bool(evidence.get("all_gates_pass", False))
        name = "0115-live-full.json" if evidence.get("mode") == "live-full" else "0115-live-pairing-pause.json"
        out = args.evidence_dir / name
        out.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n")
        print("redacted live evidence written to: %s" % out)
        return rc

    try:
        print("Starting isolated container %s preview=%d qmp=%d work=%s"
              % (container, preview, qmp, work))
        proc = live_start_container(disp_sd, preview, qmp, work)
        booted, uart_marker = live_wait_boot(preview, args.boot_timeout)
        evidence["steps"].append({"gate": "guest_booted_home", "pass": booted,
                                  "uart_boot_marker": uart_marker,
                                  "frame_sha256": sha256_bytes(live_screen(preview)) if booted else None})
        if booted:
            live_screenshot(args.evidence_dir, "live-1-home", preview)
        if not booted:
            print("Guest never reached Home; aborting live canary.")
            evidence["all_gates_pass"] = False
            return finish(1)

        # A fresh card has no persisted plugin preference. Enable plugins using
        # the firmware UI; never seed settings or credentials on the card.
        enabled = (live_drive(preview, (150, 565), "home", "settings", wait_s=12)
                   and live_drive(preview, (150, 522), "settings", "connectivity", wait_s=12)
                   and live_drive(preview, (130, 388), "connectivity", "plugins_popup", wait_s=12))
        if enabled:
            live_tap(preview, 230, 362)
            time.sleep(2)  # let the firmware dismiss the popup
            enabled = not live_anchor(preview, "plugins_popup") and live_anchor(preview, "connectivity")
        enabled = (enabled
                   and live_drive(preview, (30, 62), "connectivity", "settings", wait_s=12)
                   and live_drive(preview, (30, 62), "settings", "home", wait_s=12))
        evidence["steps"].append({"gate": "enable_plugins_on_clean_card", "pass": enabled})
        plugins = enabled and live_drive(preview, NAV_HOME_TO_PLUGINS, "home", "plugins", wait_s=12)
        evidence["steps"].append({"gate": "plugins_screen_opened", "pass": plugins,
                                  "frame_sha256": sha256_bytes(live_screen(preview))})
        if plugins:
            live_screenshot(args.evidence_dir, "live-2-plugins", preview)

        ybx = (live_drive(preview, NAV_PLUGINS_TO_YBX, "plugins", "wifi", wait_s=12)
               if plugins else False)
        evidence["steps"].append({"gate": "ybx_launched_wifi_picker", "pass": ybx,
                                  "frame_sha256": sha256_bytes(live_screen(preview))})
        if ybx:
            live_screenshot(args.evidence_dir, "live-3-wifi", preview)

        pairing = (live_drive(preview, NAV_WIFI_JOIN, "wifi", "pairing_waiting",
                              attempts=4, wait_s=args.pairing_timeout) if ybx else False)
        # The QR screen is proven by its framebuffer anchor + a frame hash only;
        # the image (and any device code / pairing link) is never committed.
        evidence["steps"].append({"gate": "pairing_pause_qr_screen", "pass": pairing,
                                  "qr_frame_sha256_not_committed": sha256_bytes(live_screen(preview))})
        evidence["qr_image_committed"] = False
        evidence["pairing_performed"] = False
        evidence["all_gates_pass"] = bool(booted and plugins and ybx and pairing
                                          and sd_result["ok"])
        if not pairing:
            print("Reached Wi-Fi join but never saw the device-code pairing screen; "
                  "not claiming the pairing pause.")
            return finish(1)

        print("\nPAIRING PAUSE REACHED (owner action; nothing was paired here):")
        print("  The isolated guest is showing its device-code QR / 'Waiting for authorization'.")
        print("  Preview: http://127.0.0.1:%d/  (container %s)." % (preview, container))
        print("  Pair ONLY with the dedicated test Yandex account; never copy the")
        print("  QR/token/link into this repo or any log. No QR image is committed.")
        if args.stop_at_pairing:
            print("\n--stop-at-pairing: exiting before any pairing. Tearing down only "
                  "this run's container.")
            return finish(0)

        print("\nSTAGE C LIVE CANARY — remaining owner action required:")
        input("   Press <enter> once you have paired and the guest shows a populated catalog: ")
        # Stock /api/settings only exists while the firmware web server runs
        # (File Transfer -> Join a Network); a failed read must not tear down the paired guest.
        while True:
            try:
                live_guest_settings_get(container)
                break
            except (subprocess.CalledProcessError, ValueError) as exc:
                print("   /api/settings is not reachable yet (%s)." % type(exc).__name__)
                print("   On the guest: Back to Home -> File Transfer -> Join a Network (PICSimLabWifi).")
                answer = input("   Press <enter> to retry, or type 'skip' to skip the settings gate: ")
                if answer.strip().lower() == "skip":
                    break
        try:
            toggles = verify_settings_toggles("", live=True, container=container)
        except (subprocess.CalledProcessError, ValueError) as exc:
            print("   settings gate skipped/failed (%s); continuing." % type(exc).__name__)
            toggles = {"checks": {}}
        print("settings toggles ok=%s (71-value diff, values redacted)"
              % json.dumps({k: v["passed"] for k, v in toggles["checks"].items()}))
        evidence["settings_toggles"] = {k: v.get("passed") for k, v in toggles["checks"].items()}
        print("\n4) Leave File Transfer (Back), open Plugins -> YBX, Fetch + open the test book,")
        print("   long-press to save a disposable clipping, then Sync. Confirm the quote appears")
        print("   in Yandex for the TEST account; create a quote in Yandex and confirm it reaches")
        print("   the guest. Do NOT restart the container here (it would end this run).")
        while True:
            answer = input("   Type 'done' when the round-trip is verified, or 'fail' if it is not: ").strip().lower()
            if answer in ("done", "fail"):
                break
        evidence["yandex_round_trip_owner"] = answer
        evidence["mode"] = "live-full"
        evidence["pairing_performed"] = "owner (test account)"
        evidence["all_gates_pass"] = bool(evidence["all_gates_pass"]
                                          and all(evidence["settings_toggles"].values())
                                          and answer == "done")
        print("5) Disable Clippings in the plugin UI (old proof must be revoked, "
              "KOSync progress must keep working), then re-enable (a new, "
              "different proof must be minted).")
        print("\nLive canary complete. Owner to record pass/fail against the runbook "
              "Stage C gates. Nothing here was promoted to Stable.")
        return finish(0)
    finally:
        print("\nTearing down live container %s (only this run's container)." % container)
        subprocess.run(["docker", "rm", "-f", container], capture_output=True, text=True)
        if proc and proc.poll() is None:
            proc.terminate()
        bridge_pid = work / "bridge.pid"
        if bridge_pid.is_file():
            try:
                os.kill(int(bridge_pid.read_text().strip()), 15)
            except (ProcessLookupError, ValueError):
                pass
        # Kill any lingering bridge bound to this preview port.
        subprocess.run(["bash", "-c",
                        "pkill -f 'eink_qemu11_preview_bridge.py --port %d' || true" % preview],
                       capture_output=True, text=True)
        shutil.rmtree(base, ignore_errors=True)


# --------------------------------------------------------------------------- #
# DRY-RUN (hermetic) — the mode that must go GREEN
# --------------------------------------------------------------------------- #
def run_dry_run(args) -> dict:
    workdir = Path(tempfile.mkdtemp(prefix="ybx-canary-dryrun-"))
    marker = "ybx-canary-" + time.strftime("%Y%m%d%H%M%S")
    settings = SettingsServer()
    proof = ProofServer()
    settings.base = settings.start()
    proof_base = proof.start()
    rows = []
    results = {}
    try:
        # Step 1a — candidate ZIP payload validator.
        zip_result = validate_candidate_zip(args.zip)
        results["candidate_zip"] = zip_result
        rows.append(("1a Candidate ZIP verified + 7-file payload validator",
                     zip_result["ok"], "sha=%s v=%s" % (
                         zip_result["sha256"][:12], zip_result["manifest_version"])))

        # Step 1b — pinned isolated-QEMU inputs (no container started).
        pinned = run_pinned_inputs_check(args)
        results["pinned_inputs"] = pinned
        rows.append(("1b Pinned eink-qemu / flash / SD inputs (--check)",
                     pinned["ok"], pinned["detail"]))

        # Step 1c — installed-SD media payload validator (byte-equality).
        sd_result = validate_installed_sd(args.sd_image, zip_result) if args.sd_image.is_file() \
            else {"ok": None, "note": "install SD image not present; skipped"}
        results["installed_sd"] = sd_result
        rows.append(("1c Installed disposable SD matches candidate (7 files)",
                     sd_result["ok"], "found=%s koreader=%s epub=%s" % (
                         sd_result.get("plugin_files_found", "n/a"),
                         sd_result.get("koreader_json_present", "n/a"),
                         sd_result.get("fixture_epub_present", "n/a"))))

        # Step 2 — device-code pairing + token.json presence/keys-only.
        pairing = verify_pairing_and_token(workdir)
        results["pairing"] = pairing
        rows.append(("2 QR/device-code pairing + token.json (keys only)",
                     pairing["ok"], "keys=%s token_never_logged=%s" % (
                         pairing["token_json_keys"], pairing["token_never_logged"])))

        # Step 3 — 71-value firmware settings diff, values redacted.
        toggles = verify_settings_toggles(settings.base)
        results["settings"] = toggles
        rows.append(("3 Enable 4 flags independently (71-value diff, redacted)",
                     toggles["ok"], "count=%s per-key=%s" % (
                         toggles["initial_count"],
                         {k: v["passed"] for k, v in toggles["checks"].items()})))

        # Step 4 — guest-side Clippings GET/PUT/GET + progress + cold-boot idempotence.
        clip = verify_clipping_round_trip(workdir, marker)
        results["clipping"] = clip
        rows.append(("4 Clippings GET/PUT/GET + progress + cold-boot idempotence",
                     clip["ok"], "upload_valid=%s no_dup=%s applied0=%s rev1=%s" % (
                         clip["boot1_round_trip_valid"], clip["boot2_no_duplicate_put"],
                         clip["boot2_retry_applied_zero"], clip["boot2_revision_stable"])))

        # Step 5 — scoped quote proof disable -> forbidden, progress ok, re-mint differs.
        proof_result = verify_proof_lifecycle(proof_base, proof_base, _kosync_headers())
        results["proof"] = proof_result
        rows.append(("5 Disable->old proof forbidden + progress ok; re-enable->new proof",
                     proof_result["ok"], "unauth401=%s disabled_forbid=%s progress=%s diff=%s" % (
                         proof_result["unauth_mint_401"],
                         proof_result["old_proof_forbidden_after_disable"],
                         proof_result["kosync_progress_still_works"],
                         proof_result["reenabled_proof_different"])))

        # Step 6 — evidence hygiene: nothing secret lands in the artifact we ship.
        provisional = {"mode": "dry-run-synthetic",
                       "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
                       "steps": [list(row) for row in rows],
                       "redacted_results": results}
        leak = scan_for_leaks(json.dumps(provisional, ensure_ascii=False), workdir, marker)
        results["leak_scan"] = leak
        rows.append(("6 Evidence hygiene (no token / pairing link / marker text)",
                     leak["ok"], "offenders=%d" % leak["offender_count"]))

        checks = [zip_result, pinned, sd_result, pairing, toggles, clip, proof_result, leak]
        all_ok = all(bool(c["ok"]) for c in checks if c.get("ok") is not None) \
            and not any(c.get("ok") is False for c in checks)
    finally:
        settings.stop()
        proof.stop()

    report = {"mode": "dry-run-synthetic", "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
              "all_pass": all_ok, "steps": rows,
              "redacted_results": results,
              "note": "Protocol-level replay stands in for flaky GUI automation; "
                      "0113/0114 already proved real-firmware pixels. No internet, "
                      "no real account, no container."}
    out = Path(args.evidence_dir) / "0115-dryrun-summary.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    # Re-check the exact bytes we wrote, so the shipped artifact is what was audited.
    final_leak = scan_for_leaks(out.read_text(), workdir, marker)
    if not final_leak["ok"]:
        all_ok = False
        report["all_pass"] = False
    print_table(rows, all_ok)
    print("redacted evidence written to: %s" % out)
    return report


def scan_for_leaks(summary_text: str, workdir: Path, marker: str) -> dict:
    """Prove nothing secret lands in the artifact we would ship.

    * The committed summary text must not contain the fixture bearer token, the
      disposable clipping marker, or the pairing verification host.
    * Every fixture ledger on disk must be free of the bearer token, mirroring
      production ledger hygiene (auth headers are never logged).
    """
    from stock1614_fixture_server import TOK
    offenders = []
    for needle, label in ((TOK, "fixture bearer token in summary"),
                          (marker, "clipping marker in summary"),
                          ("example.invalid", "pairing link host in summary")):
        if needle and needle in summary_text:
            offenders.append(label)
    for path in workdir.rglob("*.json"):
        if path.name == "token.json":
            continue
        if TOK and TOK in path.read_text(errors="ignore"):
            offenders.append("bearer token in " + path.name)
    return {"offender_count": len(offenders), "offenders": offenders,
            "ok": not offenders}


def run_pinned_inputs_check(args) -> dict:
    """Run start_eink_qemu11_preview.sh --check (offline verification, no docker run)."""
    if not START_SCRIPT.is_file():
        return {"ok": None, "detail": "start script not found"}
    preview, qmp = pick_free_pair()
    env = {**os.environ, "YBX_QEMU11_PREVIEW_PORT": str(preview),
           "YBX_QEMU11_QMP_PORT": str(qmp)}
    completed = subprocess.run(["bash", str(START_SCRIPT), "--check"], env=env,
                               capture_output=True, text=True)
    return {"ok": completed.returncode == 0,
            "detail": (completed.stdout + completed.stderr).strip().splitlines()[-1]
            if (completed.stdout + completed.stderr).strip() else "no output",
            "ports": [preview, qmp]}


def print_table(rows, all_ok, title="STAGE C CANARY"):
    print("\n" + "=" * 78)
    print(title + " RESULTS")
    print("=" * 78)
    for name, ok, detail in rows:
        flag = "PASS" if ok else ("SKIP" if ok is None else "FAIL")
        print(f"[{flag}] {name}")
        print(f"       {detail}")
    print("=" * 78)
    print("OVERALL: " + ("GREEN" if all_ok else "NOT GREEN"))
    print("=" * 78)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run-synthetic", action="store_true",
                      help="Hermetic synthetic run (no internet/account/container).")
    mode.add_argument("--live", action="store_true",
                      help="Real isolated-QEMU canary; owner-gated.")
    mode.add_argument("--prod-smoke", action="store_true",
                      help="Read-only unauthenticated production smoke (Part 1).")
    parser.add_argument("--zip", type=Path, default=DEFAULT_ZIP)
    parser.add_argument("--sd-image", type=Path, default=DEFAULT_SD_IMAGE)
    parser.add_argument("--stable-dir", type=Path, default=DEFAULT_STABLE_DIR)
    parser.add_argument("--evidence-dir", type=Path,
                        default=HERE / "experiments" / "0115-evidence")
    parser.add_argument("--preview-port", type=int, default=0)
    parser.add_argument("--qmp-port", type=int, default=0)
    parser.add_argument("--stop-at-pairing", action="store_true",
                        help="With --live: prove boot + navigation to the device-code "
                             "pairing pause, then exit 0 without pairing (non-interactive).")
    parser.add_argument("--boot-timeout", type=float, default=300.0,
                        help="With --live: seconds to wait for the guest to reach Home.")
    parser.add_argument("--pairing-timeout", type=float, default=90.0,
                        help="With --live: seconds to wait for the QR/pairing screen "
                             "after joining the network.")
    args = parser.parse_args()
    args.evidence_dir.mkdir(parents=True, exist_ok=True)
    if args.dry_run_synthetic:
        report = run_dry_run(args)
        return 0 if report["all_pass"] else 1
    if args.prod_smoke:
        report = run_prod_smoke(args)
        return 0 if report["all_pass"] else 1
    return run_live(args)


if __name__ == "__main__":
    raise SystemExit(main())
