#!/usr/bin/env python3
"""Drive the *real* Stock 2.0 X4 Pro in isolated QEMU through Clippings E2E.

Uses the genuine QEMU guest, GT911 touch and virtual SD, never a DOM/model
simulation.  Requires an already-started disposable QEMU guest and fixture
server, with the public test EPUB in /books/ybx and a configured synthetic
KoSync account.  The script deliberately cannot target a real IP or port 8784.

Run only on a fresh, backed-up/synthetic test image. This script is not
appropriate for a user's actual reader or real account credentials.
"""
from __future__ import annotations

import argparse
import hashlib
from io import BytesIO
import json
from pathlib import Path
import time
from urllib.request import Request, urlopen

from eink_qemu11_preview_bridge import QMP

BOOK_COORDS = (137, 185)
OPEN_LIBRARY_COORDS = (135, 420)
LIBRARY_BOOK_COORDS = (145, 188)
MODAL_OPEN_COORDS = (235, 350)
READER_TOOLBAR_COORDS = (240, 395)
MORE_COORDS = (380, 751)
CREATE_CLIP_COORDS = (137, 458)
WORD_DIFFERENT_COORDS = (240, 232)
CLIP_CONFIRM_COORDS = (285, 170)
PAGE_MORE_COORDS = (452, 604)
SYNC_PROGRESS_COORDS = (139, 605)
WIFI_FIXTURE_COORDS = (138, 193)


def read_ledger(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def guest_round_trip_valid(ledger: dict, quote: str,
                           *, required_progress_puts: int = 1,
                           require_clipping_put: bool = True) -> bool:
    """True only when *guest-originated HTTP* and retained data prove E2E.

    Fake server unit probes do not satisfy this: use a fresh evidence ledger
    containing only guest traffic for the current run.
    """
    events = ledger.get("requests")
    if not isinstance(events, list):
        return False
    clips = [e for e in events if e.get("kind", "").startswith("clippings")
             and "/api/v1/clippings/" in e.get("path", "")]
    good_gets = [e for e in clips if e.get("kind") == "clippings"
                 and e.get("method") == "GET" and e.get("status") == 200]
    good_puts = [e for e in clips if e.get("kind") == "clippings-put"
                 and e.get("method") == "PUT" and e.get("status") == 200]
    if not good_gets or (require_clipping_put and not good_puts):
        return False
    if any(e.get("status") != 200 for e in clips):
        return False
    # A complete guest transaction must have GET probe before PUT,
    # then a GET after upload (the firmware downloads remote deltas).
    if require_clipping_put:
        put_at = next(i for i,e in enumerate(events) if e is good_puts[0])
        if not any(e is g for e in events[:put_at] for g in good_gets):
            return False
        if not any(e is g for e in events[put_at+1:] for g in good_gets):
            return False
    progress_puts = [e for e in events if e.get("kind") == "progress-put"
                     and e.get("method") == "PUT" and e.get("status") == 200]
    if len(progress_puts) < required_progress_puts:
        return False
    if len(good_gets) < 2 * required_progress_puts:
        return False
    histories = ledger.get("remote_clippings") or {}
    if not isinstance(histories, dict):
        return False
    matches = [row for document, rows in histories.items()
               if isinstance(rows, dict)
               for row in rows.values()
               if isinstance(row, dict) and row.get("text") == quote
               and row.get("deleted") == 0 and type(row.get("revision")) is int
               and row["revision"] >= 1]
    return len(matches) == 1


class StockClippingsGuest:
    def __init__(self, *, preview_port: int, qmp_port: int,
                 evidence: Path, out: Path, quote: str = "different",
                 tap_delay: float = 2.0):
        if not (8800 <= preview_port < 65535 and
                8800 <= qmp_port < 65535 and
                preview_port != qmp_port and
                8784 not in (preview_port, qmp_port)):
            raise ValueError("Only a distinct isolated QEMU 88xx port pair is allowed")
        if not (quote and all(0x20 <= ord(ch) <= 0x7E for ch in quote)):
            raise ValueError("Only synthetic ASCII test quotes are allowed")
        self.preview = f"http://127.0.0.1:{preview_port}"
        self.qmp = QMP("127.0.0.1", qmp_port)
        self.evidence = evidence
        self.out = out
        self.quote = quote
        self.delay = tap_delay

    def get(self, path: str) -> bytes:
        with urlopen(self.preview + path, timeout=12) as response:
            if response.status != 200:
                raise RuntimeError(f"Preview {path}: {response.status}")
            return response.read()

    def assert_isolated_qemu(self) -> None:
        state = json.loads(self.get("/api/state"))
        if (state.get("firmware_model") !=
                "original X4 Pro, external 11.1.2 QEMU" or
                not state.get("experimental", False)):
            raise RuntimeError("Refusing non-QEMU or non-pinned Stock firmware")
        self.out.mkdir(parents=True, exist_ok=False)
        if any(e.get("kind") not in ("health", "status")
               for e in read_ledger(self.evidence).get("requests", [])):
            raise RuntimeError("Require a FRESH synthetic fixture ledger, not prior guest traffic")

    def tap(self, where: tuple[int, int], name: str) -> None:
        x, y = where
        req = Request(
            self.preview + "/api/tap",
            data=json.dumps({"x": x, "y": y, "duration_ms": 80}).encode(),
            method="POST", headers={"Content-Type": "application/json"},
        )
        with urlopen(req, timeout=14) as response:
            answer = json.load(response)
        if answer.get("accepted") is not True:
            raise RuntimeError(f"QMP touch rejected at {name}: {answer}")
        time.sleep(self.delay)

    def capture(self, name: str) -> str:
        frame = self.get("/screen.png")
        if not frame.startswith(b"\x89PNG\r\n\x1a\n"):
            raise RuntimeError("Guest E-Ink screenshot is not a PNG")
        (self.out / f"{name}.png").write_bytes(frame)
        return hashlib.sha256(frame).hexdigest()

    def dark_ratio(self, box: tuple[int, int, int, int]) -> float:
        """Read *actual guest e-paper*; Pillow is required for live runs only.

        A hit target is not tappable while it is still drawing.  Waiting on
        observable pixels avoids the former false-positive QMP touch path.
        """
        from PIL import Image  # optional runtime dependency; stdlib tests work
        image = Image.open(BytesIO(self.get("/screen.png"))).convert("L")
        cropped = image.crop(box)
        pixels = cropped.get_flattened_data() if hasattr(
            cropped, "get_flattened_data") else cropped.getdata()
        return sum(value < 155 for value in pixels) / (cropped.width * cropped.height)

    def wait_pixels(self, box: tuple[int, int, int, int], above: float,
                    name: str, timeout_s: float = 18.0,
                    *, reverse: bool = False) -> None:
        end = time.monotonic() + timeout_s
        while time.monotonic() < end:
            ratio = self.dark_ratio(box)
            if (ratio < above if reverse else ratio >= above):
                return
            time.sleep(0.8)
        raise RuntimeError(f"Guest E-Ink never displayed {name}, last ratio {ratio:.3f}")

    def wait_for(self, predicate, name: str, timeout_s: float = 50) -> dict:
        until = time.monotonic() + timeout_s
        last = {}
        while time.monotonic() < until:
            last = read_ledger(self.evidence)
            if predicate(last):
                return last
            time.sleep(0.45)
        raise RuntimeError(f"Guest did not complete {name}; recent synthetic events: "
                           + repr(last.get("requests", [])[-8:]))

    def open_reader_from_home(self, *, has_recent_book: bool = False) -> None:
        self.capture("14-reboot-home" if has_recent_book else "01-home")
        if has_recent_book:
            self.tap(BOOK_COORDS, "resume recent book")
        else:
            self.tap(OPEN_LIBRARY_COORDS, "highlight library")
            self.tap(OPEN_LIBRARY_COORDS, "open library")
            self.tap(LIBRARY_BOOK_COORDS, "open fixture EPUB details")
            self.tap(MODAL_OPEN_COORDS, "open the EPUB reader")
        self.wait_pixels((40, 320, 430, 500), 0.075, "real EPUB reader", timeout_s=25)
        # The EPD framebuffer can already show text while the reader's
        # asynchronous render task still owns the touch-ready gate.
        time.sleep(5)
        self.capture("15-reboot-reader" if has_recent_book else "02-reader")

    def open_more(self) -> None:
        # QEMU's accelerated EPD flush can skip a frame if two taps arrive
        # before the overlay registers its new touch targets.  Inspect actual
        # glass state instead of assuming accepted QMP input == UI action.
        if self.dark_ratio((27, 438, 230, 473)) > 0.14:
            return
        # QMP "screendump" can interfere with touch event dispatch while a
        # full refresh is pending. A contiguous 3-tap sequence is the
        # procedure actually verified on the 2.0 RC guest: content,
        # toolbar More, menu More. Avoid intervening screenshots.
        for attempt in range(3):
            self.tap(READER_TOOLBAR_COORDS, f"reader toolbar attempt {attempt + 1}")
            self.tap(MORE_COORDS, f"More toolbar attempt {attempt + 1}")
            self.tap(MORE_COORDS, f"More panel attempt {attempt + 1}")
            if self.dark_ratio((27, 438, 230, 473)) > 0.14:
                return
        raise RuntimeError("E-Ink More panel never appeared after three validated sequences")

    def create_clipping(self) -> None:
        self.open_more()
        for attempt in range(3):
            self.tap(CREATE_CLIP_COORDS, f"Create Clipping {attempt + 1}")
            # Dithered selection on actual EPUB word, not just a closed menu.
            if self.dark_ratio((170, 216, 289, 251)) >= 0.32:
                break
            self.open_more()
        self.wait_pixels((170, 216, 289, 251), 0.32, "clipping selection")
        self.capture("03-word-picker")
        self.tap(WORD_DIFFERENT_COORDS, "select synthetic word")
        # In the action popup the light Clip button covers x237..340.
        self.wait_pixels((237, 155, 340, 187), 0.17, "Clip action popup",
                         reverse=True)
        self.capture("04-quote-selected")
        self.tap(CLIP_CONFIRM_COORDS, "Clip")
        self.wait_pixels((170, 216, 289, 251), 0.30, "saved quote",
                         reverse=True)
        self.capture("05-clipping-saved")
        time.sleep(3)

    def sync_clippings(self, *, retry: bool = False) -> None:
        self.open_more()
        self.tap(PAGE_MORE_COORDS, "More next page 1")
        self.tap(PAGE_MORE_COORDS, "More next page 2")
        self.capture("10-more-with-sync" if retry else "06-more-with-sync")
        self.tap(SYNC_PROGRESS_COORDS, "Sync Progress")
        # Wait for the *completed real WiFi scan*. Earlier UI automation
        # tapped the blank scanning page before PICSimLabWifi appeared.
        self.wait_pixels((26, 168, 453, 219), 0.22,
                         "PICSimLabWifi AP visible", timeout_s=30)
        self.capture("11-wifi" if retry else "07-wifi")
        self.tap(WIFI_FIXTURE_COORDS, "Connect PICSimLabWifi")
        self.capture("12-sync-start" if retry else "08-sync-start")

    def run(self, *, reboot_and_retrieve: bool = True) -> dict:
        self.assert_isolated_qemu()
        self.open_reader_from_home()
        self.create_clipping()
        self.sync_clippings()
        first = self.wait_for(
            lambda d: guest_round_trip_valid(d, self.quote),
            "first firmware upload and remote download",
        )
        self.capture("09-first-sync-complete")
        before_count = sum(e.get("kind") == "clippings-put"
                           for e in first["requests"])
        initial_revisions = {
            document: {name: item["revision"] for name, item in records.items()}
            for document, records in first["remote_clippings"].items()
        }
        if reboot_and_retrieve:
            # The ledger is published before the HTTP response reaches the
            # guest; let the reader finish its own SD writes before reset.
            time.sleep(3.0)
            self.qmp.call("system_reset")
            time.sleep(9.0)  # wait for actual boot+SDMMC and Home EPD refresh
            self.open_reader_from_home(has_recent_book=True)
            self.sync_clippings(retry=True)
            last = self.wait_for(
                lambda d: guest_round_trip_valid(
                    d, self.quote, required_progress_puts=2),
                "second synchronization after guest reboot",
            )
            after_count = sum(e.get("kind") == "clippings-put"
                              for e in last["requests"])
            after_revisions = {
                document: {name: item["revision"] for name, item in records.items()}
                for document, records in last["remote_clippings"].items()
            }
            if after_count != before_count or after_revisions != initial_revisions:
                raise RuntimeError("Rebooted guest duplicated or changed stored clipping")
            self.capture("13-reboot-retrieved")
        else:
            last = first
        events = last["requests"]
        summary = {
            "status": "verified_original_stock_qemu_guest",
            "quote": self.quote,
            "fixture_only": True,
            "reboot_and_retrieve": reboot_and_retrieve,
            "clippings_puts": sum(e.get("kind") == "clippings-put" for e in events),
            "clippings_gets": sum(e.get("kind") == "clippings" for e in events),
            "progress_puts": sum(e.get("kind") == "progress-put" for e in events),
            "remote_revisions": {
                doc: [row["revision"] for row in records.values()]
                for doc, records in last["remote_clippings"].items()
            },
        }
        (self.out / "verified-result.json").write_text(
            json.dumps(summary, indent=2, ensure_ascii=False) + "\n")
        return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preview-port", required=True, type=int)
    parser.add_argument("--qmp-port", required=True, type=int)
    parser.add_argument("--evidence", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--quote", default="different")
    parser.add_argument("--delay", type=float, default=2.0)
    parser.add_argument("--no-reboot-retrieve", action="store_true")
    args = parser.parse_args()
    if not 0.6 <= args.delay <= 8:
        parser.error("QEMU touch refresh wait must be 0.6..8 seconds")
    result = StockClippingsGuest(
        preview_port=args.preview_port, qmp_port=args.qmp_port,
        evidence=args.evidence, out=args.out,
        quote=args.quote, tap_delay=args.delay,
    ).run(reboot_and_retrieve=not args.no_reboot_retrieve)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
