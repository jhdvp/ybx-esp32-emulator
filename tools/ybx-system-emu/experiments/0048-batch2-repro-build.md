# 0048 — offline opt-in patch-composition reproducibility audit (host-only)

Date: 2026-10-30. Scope: X4 Pro factory guest 0042 chain. No live QEMU/Docker run,
no shared-emulator or main-checkout mutation, no firmware change. Reviewers must
re-derive the SHA claims below; every check here is reproducible from this repo.

## Mission

Audit and improve offline opt-in QEMU patch composition/reproducibility for the
original X4 Pro with accepted 0042 and experiment 0043. Deliverables: patch
idempotence, source signatures, build docs, exact factory-flash provenance,
native acceptance-test guardrails, and session reset — as a **safe host checker
plus host-only tests**, prioritising **fail-closed** checks.

## What was already true (repository evidence)

- `apply_qemu_patches.sh:14` installs **only** the accepted CPU-peripheral
  patches `0001/0003/0005`. The 20-patch X4 Pro chain was assembled by hand in
  the isolated `qemu-rominit-20261029` checkout; nothing proved a fresh clone of
  base `febae182e132e4055529be423a818225ebddaa3a` reproduces it, nor that the
  on-disk patch bytes still yield the accepted More-popup frame SHA `8b81cec4…`
  (`QEMU_BRINGUP.md:1380–1397`).
- 0042 is the GPIO6 EPD-BUSY `GPIO_PCPU_INT`/`PCPU_INT1` fix
  (`QEMU_BRINGUP.md:1381–1386`); patches 0020–0022 are **quarantined** and
  0002/0004 **rejected** (`experiments/README.md`); 0043 cross-core IPI counters
  are **unvalidated**.
- The correct factory flash is `s3-flash-complete-rom-test.bin`, SHA-256
  `206ed966…609523`. An earlier `settings/`-packaged 092c2be5 bin
  (`268c62e0…434b25`) is **superseded** and would silently produce different
  guest behaviour.
- `probe_x4pro_guest_touch_ui.py` records `qemu_sha256` but never compares it,
  and never validated flash magic/provenance before spawning.

## Deliverable 1 — `check_qemu_patch_composition.py` (new, 612 lines, host-only)

Constants establish the closed-world model:
- `X4PRO_0042_CHAIN:28` — the 20 patch paths in apply order.
- `QUARANTINED:50`, `REJECTED:55`, `UNVALIDATED:59` — 0020/0021/0022,
  0002/0004, 0043. These are never composed.
- `PCPU_INT_MARKER:66 = "GPIO_PCPU_INT"`, `X4PRO_MACHINE:67 = "ybx-x4pro-epd"`
  (the source registers the property name; `=on` is a CLI flag, not a token in
  `esp32s3.c` — an early false-flag trap that was corrected).
- `FACTORY_FLASH_SHA256:68` and `KNOWN_SUPERSEDED_FLASH:72` (named rejection).
- `FACTORY_ARTIFACTS` (added in `verify_artifacts`) pins flash/ROM/SD media.

Subcommands (all read-only w.r.t. any shared checkout):
- `record --qemu-src <checkout>` → `compose_chain:244` clones the bare base into
  a throwaway temp repo (`repo.mkdir(parents=True)`; refuses a non-fresh target),
  applies `normalize_patch`ed patches, `check_sources:316` asserts PCPU_INT and
  the machine property survive composition and that every chain-added file
  exists, then `write_manifest` signs 21 touched sources + 26 patch files into
  `patches/composition-manifest.sha256`.
- `normalize_patch:146` — several experiment patches were hand-stripped and lost
  the `new file mode` line on `--- /dev/null` blocks, so `git apply` tried to
  delete `/dev/null` (patch 0009). It re-inserts `new file mode 100644` **only**
  when the block is a genuine pure insertion (first hunk `@@ -0,0`); anything
  else is left as-is so the later `git apply` rejects it — fail-closed.
- `verify [--live-qemu-root] [--skip-compose]` — `verify_chain_definitions:301`
  (chain equals the audited tuple, no forbidden patch, 0042 last, all files
  present) + `verify_patch_files:283` (per-patch SHA vs manifest, and every
  manifest entry exists on disk) + compose determinism.
- **Idempotence decision (important).** Linear per-patch `--reverse --check`
  undo is **undefined** here: 0042 intentionally rewrites 0023/0024 context, so
  undoing one patch in isolation fails even on a correct tree; a reverse-order
  chain undo also fails at 0023. Reproducibility is therefore proven by
  **double composition**: `compose_chain` is run twice into independent temp
  repos and the two trees' source SHAs must match
  (`check_qemu_patch_composition.py:569–578`, `deterministic_recompose`).
- `guardrails --qemu-root --flash --rom [--qemu-bin] [--require-artifacts]` →
  `acceptance_guardrails:354` runs pre-spawn fail-closed checks: base-HEAD match,
  PCPU_INT in the live tree, `validate_flash:342` (ESP magic `0xE9`, partition
  `AA50@0x8000`, app `0xE9@0x10000`), exact factory flash SHA with named
  superseded rejection, optional pinned-artifact SHA audit
  (`verify_artifacts:418`), and shared-emulator detection
  (`probe_port:334` on browser port 8784 + `pgrep -fl qemu-system-xtensa`) so a
  new run knows it must spawn its own temp QEMU and media copies rather than
  attach to the active guest.
- `sha256sums` prints the provenance block.

## Deliverable 2 — session reset (`serve_x4pro_browser.py`)

`reboot()` previously issued `system_reset` without clearing the synthetic GT911
QMP properties, so a latched press survived reset and the rebooted guest
re-read the previous user's tap as a fresh touch. Extracted `_set_touch:229`
(shared with `tap`) and `reboot:236` now zeroes `state`, `x`, `y` under the
device lock **before** `system_reset`, sets `self.frame = -1`, and on any clear
or reset error leaves the cached frame intact and releases `tap_lock`
(`finally`). This touches the browser bridge's QMP client only when that bridge
owns its own process — the auditor never invokes it against the shared server.

## Tests (host-only, `python3 -m unittest discover -s . -p 'test_*.py'`)

Full suite **182 tests, OK** (3.8 s). New/changed coverage:
- `test_check_qemu_patch_composition.py` (new, 18 tests: Helper 7, Compose 6,
  Provenance 5) — every compose test builds a **throwaway fake git repo**, so no
  real checkout is needed to exercise the logic.
  - Provenance: `test_validate_flash_accepts_esp_image`,
    `test_validate_flash_rejects_each_header_stage` (magic/partition/app each),
    `test_guardrails_reject_non_git_root`,
    `test_guardrails_reject_wrong_base_and_flash`,
    `test_superseded_flash_is_named_in_failure` (failure string names the
    superseded SHA).
- `test_serve_x4pro_browser.py` — `test_reboot_clears_gt911_session_before_real_qmp_reset`
  (3 × `ybx-touch-*` qom-set to 0, `system_reset` last),
  `test_reboot_touch_clear_failure_skips_reset` (`call_count==1`, frame kept),
  `test_reboot_qmp_error_preserves_guest_frame`.

## Validation against the real (read-only) checkout

Run in the isolated worktree against
`~/.local/state/ybx-emulator-upstream/qemu-rominit-20261029` (object source /
comparison only — it was never written, patched, or rebuilt):
- `verify --live-qemu-root …` → `ok=true`, `failures=[]`,
  `deterministic_recompose=true`, and live status signature + per-file SHAs equal
  the throwaway composition (`live == composed`).
- `guardrails … --require-artifacts` → `ok=true`; all 5 pinned media
  `state=ok`; `browser_port_open` / `xtensa_processes_running` reported (they do
  not fail the run, they are surfaced so the caller uses its own instance).
  `qemu_bin_sha256` recorded as `4106cff2795e…` (recorded, not asserted).
- Negative provenance is enforced by unit tests with synthetic images; the real
  superseded `268c62e0…` bin is not present on disk, so the reject is proven
  deterministically rather than against that file.
- `model-test` (0043 EPD-busy host model, `experiments/0043-agent-epd-busy-model-test.c`)
  compiles under `-Wall -Wextra` and prints PASS; MODE_SHIPPED stalls,
  MODE_0042 and MODE_0042_NOPCPU show the PCPU_INT-vs-stall contrast.

## Manifest state

`patches/composition-manifest.sha256` (self SHA prefix `e45109bed8c7…`):
`[base] febae182…`, `[chain]` 20 entries ending in 0042, `[patches]` 26 signed,
`[sources]` 21 signed.

## Risks / limits (not hidden)

- Reproducibility is *clone-and-apply* determinism from base `febae182`; it does
  not by itself prove the composed tree **builds** or that the built QEMU yields
  SHA `8b81cec4…` — that remains live-Docker acceptance work for the human.
- The auditor reads only. If a future patch legitimately changes an accepted
  source, `record` must be re-run and the manifest reviewed; `verify` fails
  closed on any unrecorded or rehashed file.
- Flash/ROM provenance SHAs are environment-specific to this developer's
  `~/Projects/YBX/artifacts/...` layout; `guardrails` reports `missing` (not a
  crash) when run elsewhere, and `sha256sums` exposes the expected values.
- Shared-emulator detection is advisory: it reports the port/pgrep result and
  only *fails* if the browser port is live **and** no `--qemu-bin` was supplied.
- Experiment 0043 is deliberately **not** in the chain (`UNVALIDATED`); its model
  test is host-only. 0020–0022 stay quarantined.
- No Wi-Fi/ESP32-S3 networking is claimed; the guest has no emulated radio.

## Files changed

- `tools/ybx-system-emu/check_qemu_patch_composition.py` — new auditor.
- `tools/ybx-system-emu/test_check_qemu_patch_composition.py` — 18 host tests.
- `tools/ybx-system-emu/patches/composition-manifest.sha256` — generated.
- `tools/ybx-system-emu/serve_x4pro_browser.py` — reboot clears GT911 + `_set_touch`.
- `tools/ybx-system-emu/test_serve_x4pro_browser.py` — reboot tests.
- `tools/ybx-system-emu/QEMU_BRINGUP.md` — new "offline patch-composition audit"
  + session-reset section.
- `tools/ybx-system-emu/experiments/0048-batch2-repro-build.md` — this report.
