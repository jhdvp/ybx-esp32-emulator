# 0066 — batch 3: operator ROM selection for the browser bridge

Lane: `rom-selection` (worktree
`~/.local/state/xteink-agent-workspaces/ybx-emu-batch3-20261031/rom-selection`,
branch `agents/ybx-emu-b3-rom-selection-20261031`, base `59bb244`).

Changed files (all in this lane's ownership):

- `tools/ybx-system-emu/serve_x4pro_browser.py`
- `tools/ybx-system-emu/test_serve_x4pro_browser.py`
- `tools/ybx-system-emu/start_x4pro_browser.sh`
- this report

`README.md`, `QEMU_BRINGUP.md`, `experiments/README.md`, other lanes' files and
the live Docker container `ybx-x4pro-browser-8784` were not touched. Nothing
was run against the running emulator; every verification below is host-side
with a fake guest or an in-process HTTP harness. No frame, SHA or boot result
in this report was invented — live boot behaviour of a swapped ROM is
explicitly marked unproven.

Owner decision honoured: the operator loads his OWN ROM via a folder of ROM
files on the Mac plus a picker in the page; the server only READS that folder.

## What the operator does on the Mac

1. ROM candidates live in `~/Projects/YBX/artifacts/ybx-esp32-system-emulation-20261029/roms/`
   (default; override with `YBX_ROM_DIR=/some/dir`). To add a ROM:
   `cp my-image.bin ~/Projects/YBX/artifacts/ybx-esp32-system-emulation-20261029/roms/`.
   New files appear in the picker on page reload — the folder is bind-mounted,
   so no container rebuild is needed once the container was created by the
   updated launcher.
2. Start/recreate the bridge with `bash tools/ybx-system-emu/start_x4pro_browser.sh`
   (the currently running 8784 container predates the `/roms` mount; the
   orchestrator must recreate it from this launcher for the picker to see files).
3. In the page: "Guest ROM" section → pick a file in the dropdown → press
   "Load ROM". The button is disabled while the switch is in flight; failures
   appear on a red "Load ROM failed: …" line and the previous ROM stays active.

## UI: what is shown

- The dropdown lists `name · size MiB` (+ ` · too large to hash` when the
  file exceeds the hash cap, + ` · active` for the running one).
- Below it: `Active ROM: <name> · <sha256 first 16 chars>… · <provenance note>`
  refreshed every second from `GET /api/state` (serve_x4pro_browser.py:450,
  PAGE JS `showRomActive` at :212-:216).
- The old static claim "Original factory firmware" was removed from the header
  so a custom ROM is never presented as the factory image; the page never
  claims boot — "Ready" only appears when the guest's own frame counter says so.
- When `/api/roms` fails, the dropdown shows "ROM list unavailable" and the
  rest of the page keeps working (:240-247).

## HTTP contract (serve_x4pro_browser.py)

- `--rom-dir` argument, default = directory of `--rom` (:877-878, :885-889;
  `FactoryX4Pro.__init__` :346-349). The server never writes to it; the QEMU
  `-L` blob is always the copy in the private temp workdir
  (`rom/esp32s3_rev0_rom.bin`, :341-:345, :610-:614).
- `GET /api/roms` (:780-781 → `FactoryX4Pro.roms()` :502-544):
  `{"roms":[{"name","size","sha256","active","provenance","provenance_note"}…],
  "active","truncated","limit":64,"max_hash_bytes":67108864}`.
  Regular files only (`entry.is_file(follow_symlinks=False)`), symlinks and
  directories are never listed or followed; sorted by name; capped at 64
  entries; files > 64 MiB are listed with `"sha256": null` and an error note —
  never with a fabricated hash (:519-:537).
- `POST /api/rom` with `{"name": "<file>"}` (:834-836 → `handle_rom_switch`
  :791-826 → `switch_rom` :580-:623):
  - name validation in `_resolve_rom_target` :546-563: non-string/empty/`.`/`..`/
    any path separator/NUL → 400 `bad-request`; `os.lstat` first, so ANY
    symlink (including one pointing outside) → 400 "not a regular file";
    missing file → 404 `rom-not-found` (:52-:55); post-resolve parent check must
    equal the resolved `--rom-dir` as a second fence.
  - > 64 MiB → 400 "refusing to load an unverified image" (:592-:595).
  - on success: GT911 state cleared like `/api/reboot` (:566-:578), QEMU process
    stopped, chosen file staged + `os.replace`d onto the workdir ROM blob,
    frame cache + ETag reset via `_reset_frame_cache` (:407-:412), guest
    relaunched under the tap-queue action lock. Response:
    `{"accepted":true,"action":"rom-switch","name","sha256","provenance",
    "provenance_note"}`. If the relaunch fails, the error propagates as a typed
    503 and `/api/state` then answers `guest-missing` — a failed boot is never
    reported as a success.
- `GET /api/state` keeps every previous field and adds
  `"rom":{"name","sha256","provenance","provenance_note","known_factory_image"}`
  (:450, `rom_info` :494-:500).

## Provenance honesty

`KNOWN_FACTORY_IMAGES` (:30-:38) pins two SHA-256 values. Both were verified
on this Mac during this lane, not copied blindly:

```
$ shasum -a 256 .../qa-20261029/s3-full-rom-generator-test.bin \
          .../qa-20261029/s3-flash-complete-rom-test.bin \
          .../qa-20261029/ybx-test-sd-fat16-mbr-pristine.img
5b950be93a30610322a3b46049e4095cdda04f33f09fa13a9a9a6167b5994bd9  s3-full-rom-generator-test.bin
206ed9661bd71477d1a54397c5a2cfe490179a3f59ab094f1e7421745c609523  s3-flash-complete-rom-test.bin
6c4c1bf4e6102e5963944dcf26572ab695c2259270e3aa9d0d0667787d8a7468  ybx-test-sd-fat16-mbr-pristine.img
```

Note the discrepancy found while writing this lane: the mission's "factory ROM
SHA" `206ed966…` is actually the factory **flash** image (`--flash`), while the
factory **`--rom`** blob the picker swaps is `5b950be9…`. Both are pinned with
distinct labels (`factory-flash` / `factory-rom`); any other hash is reported
as `custom` = "Custom ROM of unknown provenance (SHA-256 matches no known
factory image)". `provenance_of` :489-:492; `test_factory_media_on_this_mac_match_the_pinned_constants`
(test_serve_x4pro_browser.py:966-976) re-hashes the real read-only media on every run
(≈60 ms for 16 MiB) so a rotated constant fails loudly instead of silently.

## Launcher (start_x4pro_browser.sh)

- :13 `ROM_DIR="${YBX_ROM_DIR:-${QA_ROOT%/*}/roms}"` — a folder next to the QA
  media, overridable.
- :44-:48 creates the folder if absent and seeds one copy of the factory ROM
  into it (only if the file name does not exist yet) so "switch back" is always
  selectable. The *server* never writes there; only the launcher prepares it.
- :68 `-v "$ROM_DIR:/roms:ro"` (read-only mount) and :73 `--rom-dir /roms`;
  the factory `--rom /work/s3-full-rom-generator-test.bin` stays the default
  (:72). The private-IP bind predicate (:17-:22) and the loud no-auth warning
  (:23-:34) are unchanged and still covered by `LauncherBindGuardTests`.
- :79 prints the ROM folder path with the "read-only for the bridge" note.

## Commands run and real output

Verification (mandated):

```
$ cd ~/.local/state/xteink-agent-workspaces/ybx-emu-batch3-20261031/rom-selection \
  && python3 -m unittest discover -s tools/ybx-system-emu -p 'test_*.py' -q
Ran 438 tests in 17.216s

OK (skipped=11)
```

Baseline before this lane's changes was `Ran 415 tests … OK (skipped=11)`; the
delta is 23 new ROM-selection tests in `test_serve_x4pro_browser.py`
(`rom_guest` :52, `make_rom_file` :68, `RomListingTests` :768,
`RomSwitchTests` :824, `RomProvenanceTests` :953, `HttpRomContractTests` :989).
Module alone: `Ran 81 tests in 1.484s OK` (fast, no Docker, no network, no guest).

Additional host-side checks (real outputs, abridged where noted):

- `bash -n start_x4pro_browser.sh` → `bash syntax OK`.
- The embedded page JS passes `node --check` → exit 0.
- `python3 serve_x4pro_browser.py --help` shows `[--rom-dir ROM_DIR]` with the
  "defaults to the directory of --rom" help text.
- `--rom-dir /tmp/.../nope` → `error: --rom-dir /private/var/…/nope is not an
  existing directory` (exit 2, argparse).
- In-process HTTP harness (fake guest, no QEMU) exercising the new endpoints:

```
GET /api/roms -> 200 [('custom-boot.bin', False, 'custom'), ('s3-full-rom-generator-test.bin', True, 'custom')] truncated False
POST .. -> (400, {'error': 'ROM name must be a plain file name inside --rom-dir', 'type': 'bad-request'})
POST symlink -> (400, {'error': 'escape.bin is not a regular file in --rom-dir', 'type': 'bad-request'})
POST unknown -> (404, {'error': 'Unknown ROM: ghost.bin', 'type': 'rom-not-found'})
POST custom-boot.bin -> 200 custom-boot.bin 550ecde8e9f2431ff5928c94f5611b97f71c499c4656261f6449305596e53aeb custom
work rom now: b'CUSTOM-BOOT-IMAGE'          # the -L blob was actually replaced
frame cache reset: -1 '' "blank"            # same session reset as /api/reboot
GET /api/state rom -> {"name": "custom-boot.bin", "sha256": "550ecde8e9f2431ff5928c94f5611b97f71c499c4656261f6449305596e53aeb",
  "provenance": "custom", "provenance_note": "Custom ROM of unknown provenance (SHA-256 matches no known factory image)",
  "known_factory_image": false}
GET /api/state keys -> ['factory_guest', 'frames', 'monotonic_ms', 'nonwhite',
                        'physical_device', 'ready', 'rom', 'sha256']
POST back -> 200 custom 260817762428e176
```

In that demo the seeded `s3-full-rom-generator-test.bin` contained the bytes
`MY-OWN-ROM`, not the real factory content, and the bridge still reported it as
`custom` — proof that provenance is hash-driven, never name-driven
(`POST back` line). A file whose SHA really equals a `KNOWN_FACTORY_IMAGES`
value is labelled `factory-rom`/`factory-flash` instead
(`test_known_factory_images_are_recognized_and_others_are_custom`).

Bug found by this demo and fixed before commit: `switch_rom` crashed with
`AttributeError` when the previous guest never established a QMP client;
guarded at serve_x4pro_browser.py:608-:609 and covered by
`test_switch_works_when_no_qmp_client_was_ever_established`
(test_serve_x4pro_browser.py:927-934).

## Test coverage of each new branch

- Listing order/hash/active flag + dir/symlink exclusion:
  `test_sorted_regular_files_with_sizes_hashes_and_active_flag` (:776).
- 64-entry cap + `truncated`: `test_entry_count_is_capped_and_truncation_is_reported` (:798).
- 64 MiB hash cap honest reporting: `test_oversized_file_is_listed_but_never_fabricated_as_hashed`
  (:807), `test_sha256_file_returns_none_over_the_cap` (:816).
- Traversal/absolute/nested/`.`/`..`/backslash/NUL/empty rejection:
  `test_traversal_absolute_and_nested_names_are_rejected` (:870); symlink-outside
  rejection: :882; directory + unknown name typed 4xx: :891; oversized switch
  refused without touching the active blob: :901; failed relaunch leaves no fake
  boot and frees the tap queue: :910-:926.
- Switch resets GT911 (3 zeroed `qom-set` props), stops+relaunches, replaces the
  blob, resets frame/ETag, releases locks/slots: :835-:861.
- `/api/state` reports the active ROM and keeps old fields: :935-:951 (+ HTTP
  contract test strengthened at :642-:653).
- Provenance mapping + real factory media re-hash: RomProvenanceTests :953-:976.
- HTTP layer: list 200, list failure 503 (not an empty list), accepted switch,
  typed 400/404/503 mapping, cached-frame reset through a real socket:
  HttpRomContractTests :989-:1071.
- Page contract (picker endpoints, provenance line, degradation without
  `/api/roms`, textContent-only option labels): :977-:987.

## Risks and what stays unproven

1. **No real boot was performed.** This lane never started QEMU. Whether the
   patched binary actually re-reads `rom/esp32s3_rev0_rom.bin` on relaunch
   (vs caching it) is unproven here; the relaunch path is the same one
   `/api/reboot` uses for a dead guest, which the browser lane has exercised.
2. **Wrong-chip-family ROM honesty:** if a loaded image boots nothing, the
   bridge's only boot claim is the QEMU-sourced frame counter, so the page
   stays at "Starting guest from the active ROM…" forever — which is honest.
   That a C3 image loaded into the S3 machine produces exactly this (rather
   than, say, a QEMU assert that kills the container process) is unproven
   without a live boot; the dead-process path does answer typed
   `guest-missing` 503s.
3. Hashing a folder full of 16 MiB images costs ≈10 ms/image on this Mac;
   the first `/api/roms` under QEMU-user-emulation inside the container may be
   slower (unmeasured).
4. The `/roms` mount exists only for containers created by the updated
   launcher; the live `ybx-x4pro-browser-8784` predates it. Until the
   orchestrator recreates the container, that bridge falls back to listing the
   directory of its `--rom` (the read-only `/work` QA folder, full of logs and
   JSON), which is honest but noisy.
5. The launcher seeds the factory ROM into `ROM_DIR` once by filename; an
   operator who replaces a seeded file with his own content under the SAME
   name gets provenance `custom` (SHA mismatch), never `factory-rom` — the
   hash, not the name, decides.
