# 0073 — firmware boot matrix: which images actually run, measured by a tool

Date: 2026-10-31. Scope: the standalone YBX ESP32 emulator devtools
(`tools/ybx-system-emu/`), read-only against the firmware folder.

The hand-written table in `firmware/README.txt` is a human note about a handful
of images, and it had already drifted (the same two file names appeared in two
contradicting buckets). This lane adds a runner that produces that table from
measurements instead of memory: `tools/ybx-system-emu/boot_matrix.py`.

## What the tool does

- Discovers candidates in a folder (`--dir`, default the folder of the
  factory flash; `--recursive` also walks subfolders, `--all-files` drops the
  `.bin`/`.img` filter): `boot_matrix.py:127`, flag at `boot_matrix.py:554`.
- Decides the image kind **from the bytes**, with the same rules the bridge
  uses (`serve_x4pro_browser.py:117`): ESP flash magic `0xE9` at 0x0, partition
  table `0xAA50` at 0x8000, application magic `0xE9` at 0x10000 → `flash`; MBR /
  FAT signature `0x55AA` at 0x1FE → `sd`; anything else → `unknown` and
  **refused before any process is started**: `boot_matrix.py:91`, `146`, `226`.
- Boots every `flash` candidate in **its own throwaway container**
  (`docker run --rm --platform linux/arm64 ybx-qemu-builder:20261029`), one
  process per image, all host mounts read-only, no `--name`, no `-p`, no
  `exec`, no `stop` — so the live bridge instance
  (`ybx-x4pro-browser-8784`) is never touched: `boot_matrix.py:485`, `508`.
- Inside the container the guest runs the patched
  `qemu-system-xtensa` with `esp32s3,ybx-x4pro-*=on`, `-m 8M`,
  `-global ssi_psram.is_octal=on`, `-drive if=mtd` (flash) and
  `-drive if=sd` (pristine SD image). Frames counter and the visible bitplane
  are read over QMP on a container-local unix socket (`ybx-epd-frames`,
  `ybx-epd-visible-hex`); the SHA-256 is taken over the raw 480×800 mono
  bitplane (48 000 bytes), the same landscape orientation and the same hashing
  the bridge uses, so the numbers are comparable with older experiments:
  `boot_matrix.py:314`, `346`, `448`.
- Writes machine-readable JSON (`--json-out`, schema
  `ybx-firmware-boot-matrix/1`, 15 keys per row, `boot_matrix.py:165`, `259`)
  and a compact table (`boot_matrix.py:272`).

## The verdict taxonomy

`boot_matrix.py:45-49`, decided by one pure function `classify_outcome`
(`boot_matrix.py:146`):

| verdict | meaning |
|---|---|
| `booted and painted` | QEMU stayed up and the epd model reported at least one frame |
| `booted, painted nothing` | QEMU stayed up for the whole window and produced 0 frames |
| `QEMU died` | the QEMU process exited before the window ended; `qemu_exit_status` says how |
| `refused before boot` | the bytes are not an ESP flash image; no process was started |
| `runner error (not a guest verdict)` | the tool itself failed (Docker missing, no JSON, socket refused while QEMU is alive). Never presented as a statement about the guest |

Unmeasured cells are `-` in the table and `null` in JSON — a hash is only
written when a frame actually arrived.

## Commands run for real

The sweep itself (from this worktree, against the read-only firmware folder;
every default is what the tool uses: 120 s window per image, 5 s linger, one
container per image, all host mounts `:ro`, the image copied into container
tmpfs, RAM-only QMP socket):

```sh
cd /Users/yaraslauhancharou/.local/state/xteink-agent-workspaces/ybx-emu-batch4-20261031/boot-matrix
python3 tools/ybx-system-emu/boot_matrix.py \
  --recursive \
  --json-out /tmp/ybx-boot-matrix-0073-v3.json
```

The same command, before the crash-attribution fix described below, produced
`/tmp/ybx-boot-matrix-0073.json` (v1, 5 guest verdicts mislabelled
`runner error`) and `/tmp/ybx-boot-matrix-0073-v2.json` (v2, 3 mislabelled).
Those two files are kept as evidence for "Fixes found while measuring" #1; the
authoritative measurement is v3, and the table in this report is v3, byte for
byte from its JSON. The options recorded inside v3 are
`{"recursive": true, "all_files": false, "run_seconds": 120.0, "linger_seconds": 5.0, "container_image": "ybx-qemu-builder:20261029"}`.

Host tests (no Docker, no network, no guest). `test_boot_matrix.py` alone is
61 tests and runs in well under a second; the command below is the whole
devtools folder, so its wall time also covers other lanes' files:

```sh
cd /Users/yaraslauhancharou/.local/state/xteink-agent-workspaces/ybx-emu-batch4-20261031/boot-matrix
python3 -m unittest discover -s tools/ybx-system-emu -p 'test_*.py' -q
```

```text
Ran 564 tests in 23.308s
OK (skipped=11)
```

A single image can be booted on its own, which is how an operator re-checks one
row — for example when the matrix reports `runner error` and that has to be told
apart from a guest verdict. `--boot-one` (plus `--flash/--rom/--sd-image/--qemu-bin`,
`boot_matrix.py:570-575`) is the **guest role**: it runs QEMU directly and is
hidden from `--help` because it is meant to run inside the container — the
patched `qemu-system-xtensa` is a linux/arm64 binary. From the host it is
invoked as the matrix does it (`boot_matrix.py:485`):

```sh
docker run --rm --platform linux/arm64 \
  -v <qemu-root>:/qemu:ro -v <qa-dir>:/work:ro -v <firmware-dir>:/fw:ro \
  -v <tools-dir>:/tests:ro -w /tmp ybx-qemu-builder:20261029 \
  python3 /tests/boot_matrix.py --boot-one \
    --flash /fw/<image.bin> --rom /work/s3-full-rom-generator-test.bin \
    --sd-image /work/ybx-test-sd-fat16-mbr-pristine.img \
    --qemu-bin /qemu/build-ybx/qemu-system-xtensa
```

It prints one JSON row, starts one QEMU, and writes nothing outside `/tmp` in
that throwaway container: every host mount is `:ro`, there is no `--name`, no
`-p`, no shared build directory write.

## Measured matrix

The tool's own stdout for the v3 sweep (names truncated by the table renderer
to 44 columns; the full detail table below is from the same JSON):

```
IMAGE                                         KIND     VERDICT                   FRAMES  FIRST FRAME SHA256     SEC  HOME
--------------------------------------------  -------  ------------------------  ------  ----------------  ------  ----
YBX-UI-UNRELEASED-x4pro-c663d6a0.bin          unknown  refused before boot            -  -                      -     -
YBX-SETTINGS-UNRELEASED-x4pro-092c2be5-fa...  flash    booted and painted             4  66957094884e162b      22   yes
YBX-SETTINGS-UNRELEASED-x4pro-092c2be5-fa...  flash    QEMU died                      -  -                      4     -
YBX-UI-UNRELEASED-x4pro-c663d6a0-factory-...  flash    booted and painted             4  66957094884e162b      18   yes
YBX-UI-UNRELEASED-x4pro-c663d6a0-factory.bin  flash    QEMU died                      -  -                      5     -
crosspoint-1.6.5-x4pro-flash.bin              flash    QEMU died                      -  -                      7     -
crosspoint-2.0.0rc-x4pro-flash.bin            flash    QEMU died                      -  -                      4     -
expA-factory-bl-crosspoint2-app.bin           flash    booted and painted             3  4329fd0528d9a496      18    no
expB-crosspoint-bl-factory-app.bin            flash    QEMU died                      0  -                      8     -
inkmod-v1.1.10-x4pro-flash.bin                flash    QEMU died                      -  -                      4     -
crosspoint-1.6.5-x4pro-flash-16m.bin          flash    QEMU died                      0  -                      9     -
crosspoint-2.0.0rc-x4pro-flash-16m.bin        flash    QEMU died                      0  -                      9     -
inkmod-v1.1.10-x4pro-flash-16m.bin            flash    QEMU died                      0  -                      8     -
s3-flash-complete-rom-test.bin                flash    booted and painted             4  66957094884e162b      16   yes
ybx-settings-092c2be5-on-factory-base.bin     flash    booted and painted             4  66957094884e162b      17   yes
ybx-ui-c663d6a0-on-factory-base.bin           flash    booted and painted             4  66957094884e162b      17   yes

16 candidates: 6 painted, 0 painted nothing, 9 QEMU died, 1 refused, 0 runner error
```

In that rendering `SEC` is `wall_seconds` rounded (`boot_matrix.py:288`) — the
time the QEMU process lived, not the time to the first frame. Names longer than
44 characters are truncated with `...` (`boot_matrix.py:282`), which is why the
two `-factory-16m` rows look identical here; the rows below are the same JSON
with full names and the fields the table drops (`FIRST` = first frame at,
`WALL` = `wall_seconds`, `EXIT` = `qemu_exit_status`, `SIZE` bytes, `DIR` =
`controls/` or the firmware root, `FACT` = `is_factory_reference`):

| # | image | dir | size | verdict | frames | first frame | FIRST | WALL | EXIT | HOME | FACT |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | `YBX-UI-UNRELEASED-x4pro-c663d6a0.bin` | root | 5 529 984 | refused before boot | – | – | – | – | – | – | no |
| 2 | `YBX-SETTINGS-UNRELEASED-x4pro-092c2be5-factory-16m.bin` | controls | 16 777 216 | booted and painted | 4 | `66957094…` | 16.1 | 21.5 | – | yes | no |
| 3 | `YBX-SETTINGS-UNRELEASED-x4pro-092c2be5-factory.bin` | controls | 5 599 040 | QEMU died | – | – | – | 4.0 | 1 | – | no |
| 4 | `YBX-UI-UNRELEASED-x4pro-c663d6a0-factory-16m.bin` | controls | 16 777 216 | booted and painted | 4 | `66957094…` | 12.6 | 17.9 | – | yes | no |
| 5 | `YBX-UI-UNRELEASED-x4pro-c663d6a0-factory.bin` | controls | 5 595 520 | QEMU died | – | – | – | 4.9 | 1 | – | no |
| 6 | `crosspoint-1.6.5-x4pro-flash.bin` | controls | 5 698 176 | QEMU died | – | – | – | 6.6 | 1 | – | no |
| 7 | `crosspoint-2.0.0rc-x4pro-flash.bin` | controls | 5 419 952 | QEMU died | – | – | – | 3.6 | 1 | – | no |
| 8 | `expA-factory-bl-crosspoint2-app.bin` | controls | 16 777 216 | booted and painted | 3 | `4329fd05…` | 13.0 | 18.2 | – | **no** | no |
| 9 | `expB-crosspoint-bl-factory-app.bin` | controls | 16 777 216 | QEMU died | 0 | – | – | 8.0 | **-11** | – | no |
| 10 | `inkmod-v1.1.10-x4pro-flash.bin` | controls | 4 493 648 | QEMU died | – | – | – | 3.5 | 1 | – | no |
| 11 | `crosspoint-1.6.5-x4pro-flash-16m.bin` | root | 16 777 216 | QEMU died | 0 | – | – | 9.0 | **-11** | – | no |
| 12 | `crosspoint-2.0.0rc-x4pro-flash-16m.bin` | root | 16 777 216 | QEMU died | 0 | – | – | 9.0 | **-11** | – | no |
| 13 | `inkmod-v1.1.10-x4pro-flash-16m.bin` | root | 16 777 216 | QEMU died | 0 | – | – | 7.5 | **-11** | – | no |
| 14 | `s3-flash-complete-rom-test.bin` | root | 16 777 216 | booted and painted | 4 | `66957094…` | 10.7 | 15.9 | – | yes | **yes** |
| 15 | `ybx-settings-092c2be5-on-factory-base.bin` | root | 16 777 216 | booted and painted | 4 | `66957094…` | 11.6 | 16.7 | – | yes | **yes** |
| 16 | `ybx-ui-c663d6a0-on-factory-base.bin` | root | 16 777 216 | booted and painted | 4 | `66957094…` | 12.1 | 17.2 | – | yes | no |

First-frame SHA `66957094884e162b8dd288eb215b93fd40225b611da18b97a426d8b6c63f12ea`
is identical across all six painted YBX/factory rows; the last frame of rows
2/4/14/15/16 is the accepted Home frame
`e79c2ad5f2b60b16fe3f20625742d5f1343f5785552d1b32c11f33c42a874672`. Row 8 ends
at `78e743450b8f67ced89eb59d86b7c9da61eb306b4c3e92c31f50b345ad99c296` — a
painted screen that is not the known Home. Full digests are in the JSON: rows
2/4 (`52819e5f…` / `edb32bde…`), 8 (`4eeb2a57…`), 9 (`e0d6ac3f…`),
16 (`5c6dae33…`), and rows 14/15 share `206ed9661bd71477d1a54397c5a2cfe490179a3f59ab094f1e7421745c609523`,
the factory flash digest.

Log tails, quoted from the JSON, are what distinguishes the two death classes:

```
# exit status 1, rows 3, 5, 6, 7, 10 (size not supported):
Adding SPI flash device
qemu-system-xtensa: Drive size error: only 2, 4, 8, and 16MB images are supported
qemu-system-xtensa: -drive file=/tmp/ybx-boot-matrix-XXXX/flash.bin,if=mtd,format=raw: \
  machine type does not support if=mtd,bus=0,unit=0

# exit status -11, rows 9, 11, 12, 13 (SIGSEGV, stops at the first line):
Adding SPI flash device

# painted rows 2, 4, 8, 14, 15, 16 (killed by the runner at the end of the window):
Adding SPI flash device
qemu-system-xtensa: terminating on signal 15 from pid 1 (python3)
```

## What the measurement says about `firmware/README.txt`

1. **The same two files sit in two contradicting rows.** `firmware/README.txt`
   lists `ybx-ui-c663d6a0-on-factory-base.bin` and
   `ybx-settings-092c2be5-on-factory-base.bin` under "boots and paints" **and**
   under "hangs before app start". Measured: both paint 4 frames, first frame at
   11.6 s and 12.1 s, and the last frame is the known Home frame. The "hangs"
   row is wrong for both.
2. **`expA-factory-bl-crosspoint2-app.bin` does not hang.** The README says
   "ROM and bootloader load, then no app output, no frames". Measured: it paints
   3 frames (first `4329fd05…`, last `78e74345…`) — the app runs; it simply
   never reaches the known Home screen, which is the only row in the matrix with
   `HOME = no`.
3. **`YBX-UI-UNRELEASED-x4pro-c663d6a0-factory-16m.bin` does not crash QEMU.**
   It is in the README's "crashes QEMU (SIGSEGV)" row. Measured: booted and
   painted 4 frames, Home reached, exit status never observed because the guest
   survived the whole window. This row contradicts the README's own explanation
   ("the crash follows the BOOTLOADER, not the app") — this image carries the
   factory bootloader, so the rule predicts exactly what was measured. The rule
   itself holds up: the four real `-11` crashes
   (`crosspoint-1.6.5-…-16m`, `crosspoint-2.0.0rc-…-16m`, `inkmod-…-16m`,
   `expB-crosspoint-bl-factory-app.bin`) all carry the CrossPoint bootloader,
   and `expB` — the CrossPoint-bootloader control with the *factory* app —
   crashes identically.
4. **Five images are missing from the README table entirely**: the unpadded
   merged builds in `controls/` (`YBX-SETTINGS-…-factory.bin`,
   `YBX-UI-…-factory.bin`, `crosspoint-1.6.5-x4pro-flash.bin`,
   `crosspoint-2.0.0rc-x4pro-flash.bin`, `inkmod-v1.1.10-x4pro-flash.bin`).
   They are neither a hang nor a crash: the emulator's flash model rejects the
   size before the guest starts —
   `qemu-system-xtensa: Drive size error: only 2, 4, 8, and 16MB images are
   supported`, QEMU exit status 1. Their sizes are 4.29–5.43 MiB, so no
   supported flash size is configured. A human reading the README would look
   for a firmware bug that does not exist; the images simply need `--pad` to a
   supported size, like the `-16m` variants next to them.
5. **`ybx-settings-092c2be5-on-factory-base.bin` is not an own build.** The
   README's provenance calls it a local settings build; its SHA-256 is
   `206ed9661bd71477d1a54397c5a2cfe490179a3f59ab094f1e7421745c609523` —
   byte-identical to the factory reference `s3-flash-complete-rom-test.bin`,
   and the tool marks both `is_factory_reference: true`. So two rows of the
   matrix (and two rows of the README) measure the same factory image twice,
   not two builds.
6. **Agreement:** `YBX-UI-UNRELEASED-x4pro-c663d6a0.bin` (5 529 984 bytes) is
   correctly called "not a flash image". The tool's bytes classifier reports
   kind `unknown` and refuses it before starting anything, which matches the
   bridge's own rule and the README's conclusion.

## Fixes found while measuring

1. **A guest crash reported as a runner error.** The first full run labelled
   five images `runner error (ConnectionResetError …)`. The log tail said
   otherwise (`Drive size error: only 2, 4, 8, and 16MB images are supported`,
   exit status 1): a guest that dies closes its QMP socket **before** the
   process disappears, and the old code decided from `process.poll()` at that
   exact instant, which was still `None`. `boot_matrix.py:329` now gives the
   child a short grace period (`QEMU_EXIT_GRACE_SECONDS = 5.0`) before blaming
   the runner, and a frame read that dies mid-poll (`boot_matrix.py:346`) keeps
   the frames already counted instead of raising. Host tests for both sides of
   that decision: `test_boot_matrix.py` `ProcessGraceTests`.
2. `main()` hashed the 16 MiB factory image twice; it now hashes once and
   passes the digests to both the provenance marker and the report
   (`boot_matrix.py:579`).
3. The `--boot-one` error message named flags as `sd_image`; it now prints the
   flags as typed (`--sd-image`).

## Limits and what stays unproven

- One 120 s window per image, one pass, no repetition. A firmware that needs
  longer than the window to reach its first frame would land as
  `booted, painted nothing`. Nothing in this folder is close to that boundary:
  every painted row produced its first frame between 10.7 s and 16.1 s of a
  120 s window.
- `booted and painted` is not "works". The tool observes only the SSD1677 frame
  counter and the visible bitplane. No touch or button input is injected, no
  navigation is attempted, no Wi-Fi, no SD traffic is checked. A painted Home
  screen says the guest booted far enough to draw — nothing more.
- The `HOME` column only compares the **last** frame against one accepted Home
  SHA (`e79c2ad5…`). `no` means "painted something else", not "broken"; the
  tool cannot say what that other screen is.
- `QEMU died` is one bucket for two different events, distinguished only in the
  row data: a crash (`qemu_exit_status: -11`, SIGSEGV, log tail stops at
  "Adding SPI flash device") and a clean startup rejection (`qemu_exit_status:
  1` with the drive-size error in the log). Splitting them into a sixth verdict
  would need the log tail in `classify_outcome`, which is deliberately a pure
  function over counters and exit status today.
- The cause of the SIGSEGVs is not established here — it lives in the patched
  QEMU or in what the CrossPoint bootloader asks the emulated SPI flash to do.
  The matrix only records the exit status and the log tail. This corroborates
  the parallel `bootloader-flash` lane; it does not replace it.
- `boot_local` and `qmp_connect_unix` (the QEMU/QMP path) are **not** covered by
  host tests — they need a guest. Host tests pin the classification, the
  verdict taxonomy, the row/JSON shape, the table, the refusal path, the
  container command shape and the dispatch, and they exercise the crash/runner
  decision through `process_exited` with real children. Anything that goes
  wrong on the QMP path shows up as `runner error` in a run, never as a fake
  guest verdict.
- Every measurement in this report ran on this Mac through
  `ybx-qemu-builder:20261029` (the QEMU binary is linux/arm64). The wall times
  therefore include container start-up and are not comparable with a native
  Linux host.
- The live bridge was not touched: the container for `ybx-x4pro-browser-8784`
  stayed `Up 2 hours` across the whole sweep, and the matrix starts containers
  with no name, no published port and no shared volume writes.
- Nothing here validates the SD card content, the ROM picker, or the
  `/api/firmware` switch path — those belong to the bridge lane.
