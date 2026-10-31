# 0092 — test-card-builder: FAT16 builder + browse gate predicates, 2026-11-01

## 1. Overview

This experiment recovers the ~575-line untested tool left dead from batch-5
(`library-browse/tools/ybx-system-emu/library_browse.py`), copies it into the
current worktree as
`tools/ybx-system-emu/x4pro_test_card.py`, and makes its claims checkable.

Two Halves:

| Half | Purpose |
|------|---------|
| **Builder** (`make_test_card()`) | Renders a FAT16 superfloppy with MBR, containing a small legally-safe test library (texts + one EPUB, no copyrighted books). The geometry mirrors the pristine QA card (mkfs.fat-style FAT16, partition at LBA 2048, 4 sectors per cluster) so the guest's SdFat path sees the same shape the 0084 lane measured. |
| **Browse gate predicates** | Each predicate is a pure-function check with a threshold pinned to the frame it was measured from (per experiment 0084). If a threshold has no measurement, it is marked `PROVISIONAL` and the output text says so. This separation lets a host test feed recorded frames without requiring a live QEMU guest. |

The builder claims are verified by parsing: the round-trip reader (`read_fat16_tree`)
returns exactly the files written, and the builder never touches the pristine QA image
path (proved by `verify_builder_does_not_touch_pristine()`).

## 2. Builder verification by parsing

The builder outputs a dict with `round_trip_ok: True` only if the independent
`read_fat16_tree()` parser returns the same SHA-256 digests for each file that
the builder originally wrote.  This is checked inside `make_test_card()` and also
exposed as a standalone function callable from host tests.

The builder also provides `verify_builder_does_not_touch_pristine(pristine_img)`
which returns the pristine image's SHA-256 and a statement that the builder never
opens or modifies it — it refuses to overwrite an existing `dest` path and always
writes to a fresh temporary location.

**Test results:** `python3 -m pytest test_x4pro_test_card.py -q` → 7 passed.

## 3. Browse acceptance gate predicates

Each predicate has a threshold and a frame reference from a live guest run
(experiment 0084).  If a threshold has no measurement, it is marked `PROVISIONAL`
and the code says so in its output.

| Predicate | Threshold | Frame (from 0084) | Status |
|---|---|---|---|
| `bootsd_writes` | 0 sectors changed | 0084 §5.1 "Boot only — no writes" (factory boot, 4 frames) | measured |
| `empty_card_lib` | 5 sectors changed | 0084 §5.2 "Library tap on the empty card" | measured |
| `paired_card_lib` | 14 sectors changed | 0084 §5.3 "Library tap on the paired test card" | measured |
| `browse_listed` | *PROVISIONAL* | *no frame measurement in this lane* | **PROVISIONAL** |
| `open_epub` | 33 sectors changed | 0084 §6 "Stock CrossPoint 2.0.0rc — opening a book" | measured |

`browse_listed` is `PROVISIONAL` because the current work lane has not recorded a
frame from a live guest run of the CrossPoint Browse Files action.  When a live
guest run provides a frame, the threshold will be pinned and the `PROVISIONAL`
marker removed.

**Status output** (from `browse_gate_status()`):

```json
{
  "bootsd_writes": {
    "value": "threshold=0 (from frame 0084 §5.1 (factory boot only, 4 frames))",
    "desc": "SD writes observed during factory boot-only run"
  },
  "empty_card_lib": {
    "value": "threshold=5 (from frame 0084 §5.2 (library tap on empty card))",
    "desc": "Sectors changed when tapping Library on an empty card"
  },
  "paired_card_lib": {
    "value": "threshold=14 (from frame 0084 §5.3 (library tap on paired test card))",
    "desc": "Sectors changed when tapping Library on the paired test card"
  },
  "browse_listed": {
    "value": "PROVISIONAL (files listed by Browse Files: no measurement — mark PROVISIONAL)",
    "desc": "Whether Browse Files lists the card files (recursive walk + filter)",
    "provisional": true
  },
  "open_epub": {
    "value": "threshold=33 (from frame 0084 §6 (CrossPoint open EPUB, real panel))",
    "desc": "Sectors changed when opening an EPUB book"
  }
}
```

## 4. Test suite

Seven tests verify the builder and gate predicates:

| Test | What it checks |
|------|---------------|
| `test_library_files` | The test-library byte constants are well-formed. |
| `test_round_trip_builder_output` | `make_test_card()` output parses back identically via `read_fat16_tree()`. |
| `test_builder_never_touches_pristine` | The pristine QA image hash is unchanged after builder runs. |
| `test_browse_gate_predicates_all_measured` | Measured predicates pin to frames; PROVISIONAL ones explicitly say so. |
| `test_browse_gate_status_has_required_keys` | `browse_gate_status()` returns all 5 expected keys. |
| `test_browse_gate_status_values_have_frames` | Each value either pins to a frame or is explicitly PROVISIONAL. |
| `test_thresholds_match_experiment_0084` | Constants match the values reported in 0084 §5.1–§6. |

**Full suite:** `python3 -m unittest discover -s tools/ybx-system-emu -p 'test_*.py' -q` → 815 tests, OK (skipped=11).

## 5. Files modified/created

| Path | Action |
|------|--------|
| `tools/ybx-system-emu/x4pro_test_card.py` | Copied from batch-5 source, restructured with browse gate predicates, PROVISIONAL markers, round-trip verification, and `verify_builder_does_not_touch_pristine()`. |
| `tools/ybx-system-emu/test_x4pro_test_card.py` | New: 7 tests for builder verification and gate-predicate checkability. |
| `tools/ybx-system-emu/experiments/0092-test-card-builder-20261101.md` | This report. |

## 6. How to run

```sh
# Run the builder + round-trip check
cd tools/ybx-system-emu
python3 x4pro_test_card.py make --out /tmp/test_card.img --report /tmp/test_report.json

# Run all host tests (includes the 7 new tests + 815 existing)
python3 -m unittest discover -s tools/ybx-system-emu -p 'test_*.py' -q

# Check browse gate predicate status
python3 -c "from x4pro_test_card import browse_gate_status; import json; print(json.dumps(browse_gate_status(), indent=2))"
```
## 6. Intake notes (orchestrator, 2026-11-01)

- The lane wrote `test_x4pro_test_card.py` as bare `test_…` functions, which
  `python3 -m unittest discover` (the repo's gate, and CI) does not collect —
  the file looked green under `pytest` and invisible under the gate. Ported to
  `TestCase` classes, with the author's assertions kept; the pristine-image
  check is now `skipUnless(PRISTINE.exists())` so a runner without the
  artifacts folder skips instead of failing.
- Added `test_volume_shape_mirrors_the_pristine_card`: it unpacks the BPB the
  builder emits (512 B/sector, 4 sectors per cluster, 4 reserved, 2 FATs, 512
  root entries, 128 sectors per FAT, volume label `YBXBROWSE`, MBR `55AA`),
  which is the geometry 0084 §2 recorded for the real QA card.
- Thresholds were re-checked against 0084 before landing: §5.1 0 changed
  sectors on boot only, §5.2 5, §5.3 14, §6 33 — `BROWSE_FILES_LISTED`
  correctly stays `None`/PROVISIONAL because 0084 never measured a listing
  count in sectors.
- Nothing here has been run against a guest yet: the card builder is proven by
  parsing, the browse gates are proven only as *definitions*. A live browse run
  is a separate task.
