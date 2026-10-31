# 0063 — batch 3: reproducible QEMU compose/build pipeline (host audited, long build opt-in)

Date: 2026-10-31. Branch: `agents/ybx-emu-b3-repro-build-20261031` (base
`72f4c0a`, supersedes the batch-2 `0048` auditor work). Scope: host-only
auditing plus one real dry run through the new script. No Docker build, no
container start/stop, no browser/QMP interaction, no flash writes, no device
access, no live-emulator mutation. The read-only inputs
(`~/.local/state/ybx-emulator-upstream/qemu-0043-agent-audit-20261030`,
`.../qemu-rominit-20261029`, `.../qemu`) were fetched from as Git object
sources only; none was written, patched, or rebuilt.

## Mission

Make the patched QEMU reproducible from a clean checkout:

1. `build_reproducible_qemu.sh` — pinned base `febae182e132e4055529be423a818225ebddaa3a`
   → fresh throwaway tree → audited chain (`patches/0001/0003/0005` +
   `experiments/0006..0024,0042` + telemetry `0043,0037,0051`) → source SHA
   manifest → opt-in container build in the pinned image
   `ybx-qemu-builder:20261029` with the QEMU_BRINGUP.md configure flags.
2. Checker extended so 0037/0043/0051 are part of the audited set (0043 is no
   longer "unvalidated") and the shipped binary SHA
   `f07744d900624ce1c34a7a62dc514092a2cc4b86c2666db90c23db42cdb42630`
   is **compared**, not merely recorded.
3. Host tests for chain definition, forbidden patches, manifest determinism,
   reverse checks, fail-closed behaviours, and a dry run that signs without
   building.
4. This report.

## Deliverable 1 — checker (`check_qemu_patch_composition.py`, 895 lines)

New/changed constants and code paths (file:line):

- `TELEMETRY_CHAIN:64` — `0043 → 0037 → 0051`, appended in apply order.
- `X4PRO_FULL_CHAIN:69 = X4PRO_0042_CHAIN + TELEMETRY_CHAIN` (23 patches).
- `AUDITED_CHAINS:70` — both `0042`-only and full chain are audited; 0042 must
  stay the last hardware patch of the first layer (`CHAIN_TAIL:71`), 0051 ends
  the full chain.
- `IPI_COUNTER_MARKER:80 = "ybx_from_cpu_asserted"`, `BUSY_EDGE_MARKER:81 =
  "busy_rise_edges"` — source registers for 0043 and 0037/0051.
- `EXPECTED_BINARY_SHA256:87` — the pinned qemu-system-xtensa build of the
  0042+0043+0037+0051 tree (verified against the real audit build; see
  "Real runs").
- `normalize_patch:167` — now also strips prose preambles (0043 begins with
  commentary; the payload starts at its first `diff --git`); a file without
  any diff payload raises instead of silently composing garbage.
- `check_sources:357` — post-composition markers are **chain-gated**
  (`busy_expected:365`, `ipi_expected`): the panel `busy_rise_edges` check
  applies only when 0037/0051 are in the chain, the intc
  `ybx_from_cpu_asserted` check only when 0043 is. `chain` is a **mandatory**
  positional — the first draft defaulted `chain=()` and the real `--chain
  0042` dry run caught both failure modes (full run losing telemetry markers
  silently, 0042 run demanding them wrongly). Regression tests:
  `test_marker_checks_follow_chain_membership` (including the
  `assertRaises(TypeError)` for the omitted-chain case).
- `acceptance_guardrails:427` — live-tree checks now use the FULL chain's
  allowed-file set, additionally demand `busy_rise_edges` in
  `hw/display/ybx_ssd1677.c`, and **compare** `--qemu-bin` against
  `EXPECTED_BINARY_SHA256:481` with a fail-closed message ("qemu binary is
  not the pinned full-chain build …"), not just record it.
- `pick_chain:504`, `reverse_undo_check:512`, `compose_and_sign:532` — the
  compose stage used by the builder: audited chains are fail-fast pre-checked
  against the checked-in patch signatures (`stage: patch-signatures`),
  composed from the bare base, signed to a fresh manifest, and diffed against
  the checked-in manifest (chain/base/sources drift, stale entries);
  non-audited chains prove determinism by double composition and report
  `reverse_undo` informationally. Report carries `chain_audited:586`.
- CLI: `compose` subcommand (`--chain full|0042`, `--skip-manifest-compare`,
  test hooks `--chain-file/--patch-root/--base/--fetch-depth/--out`),
  `expected-binary-sha`, and `binary-sha:705` which exits non-zero on
  mismatch. `record`/`verify` gained `--chain` and `--fetch-depth` and now
  refuse a stale checked-in chain (`checked-in manifest chain …`).
- `UNVALIDATED` is gone: 0043/0037/0051 are signed members of the audited
  full chain; 0020–0022 remain quarantined, 0002/0004 rejected, and
  `verify_chain_definitions:334` fails closed on any of them in a chain.

## Deliverable 2 — builder (`build_reproducible_qemu.sh`, 190 lines, POSIX sh)

- Stage 1 (default, host-only, seconds): delegates to
  `"$CHECKER" compose --out … --chain "$CHAIN" --fetch-depth "$FETCH_DEPTH"`
  (`:136-142`); refuses half-composed work dirs and only reuses a composition
  carrying a `.stage1-ok` sentinel **written after stage 1 exited 0** and
  recording the chain spec (`:126-130`, `:152`) — reuse for a different
  `--chain` fails closed ("prior composition was signed for chain …").
- `--skip-manifest-compare` pass-through (`:82`, `:138`) exists for the
  separate 0042-only audit; usage text (`:44-49`) states it is **not** a
  factory reproduction. The default full-chain run always compares against
  the checked-in manifest.
- Without `--build`: prints `=== dry run: no container build was invoked …`
  plus the exact orchestrator command (`:157-160`). Docker is only looked up
  and invoked under `--build` (`:168-175`), always against the freshly
  composed temp tree — never a shared/live build directory.
- Stage 2 recipe is pinned verbatim from QEMU_BRINGUP.md
  (`CONFIGURE_FLAGS_DEFAULT:24` including `--enable-gcrypt --disable-nettle`,
  image `ybx-qemu-builder:20261029:19`, `ninja -j3 qemu-system-xtensa`).
  `--print-build-command` shows it without composing.
- `--verify-binary` (`:178-180`) hands the produced binary to the checker's
  `binary-sha` and dies on mismatch; without it the SHA is printed as
  "recorded only".
- Trap found the hard way: macOS `/bin/sh` is bash 3.2, which cannot parse a
  nested quoted `$(cat …)` inside a double-quoted string; the reuse check now
  reads the sentinel into `PRIOR_CHAIN` first.

## Manifest

`patches/composition-manifest.sha256` regenerated by `record` (full chain):
base `febae182…`, `[chain]` 23 entries ending
`0043 → 0037 → 0051`, 28 signed patch files (chain + quarantined + rejected,
so tampering with excluded patches is also detected), 25 signed sources.

## Tests (host-only, no Docker/network/guest)

`tools/ybx-system-emu/test_check_qemu_patch_composition.py` — 36 tests (4.525 s standalone):
previous 18 plus full-chain definition/ordering, telemetry audited-set
membership, prose-preamble strip and no-payload raise, `EXPECTED_BINARY_SHA256`
literal pin, chain-gated `check_sources` regression (incl. mandatory-`chain`
TypeError), `compose_and_sign` suite (deterministic dry-run manifest,
already-applied detection, missing patch, wrong base, dirty target, conflicting
orphan, audited drift vs checked-in manifest, tampered patch bytes rejected
pre-compose), guardrails **compare** the pinned binary SHA (mismatch fails; an
overridden constant matches).

`tools/ybx-system-emu/test_build_reproducible_qemu.py` — 17 tests (4.470 s standalone): synthetic
mini-chain through the script's test hooks (dry-run manifest content +
cross-run byte determinism + reverse-order undo), fail-closed wrong base /
missing patch / forbidden quarantined name / object source / unknown flag /
reuse without sentinel; `--skip-manifest-compare` acceptance+forwarding is asserted inside the missing-patch fail-closed test (stage 1 still runs, `"stage": "chain"` still reached), default chain
is the full 23-patch audited set (empty `--patch-root` names every required
file, incl. 0043/0051, before any fetch), pinned recipe strings, stub
`YBX_DOCKER_BIN` stage-2 tests (pinned args logged; fabricated binary fails
`--verify-binary`; without it SHA recorded only; reuse prints "reusing prior
composition"), and the opt-in `YBX_RUN_REAL_COMPOSE=1` real full-chain dry run
compared byte-for-byte to the checked-in manifest.

Mandated gate:

```
cd ~/.local/state/xteink-agent-workspaces/ybx-emu-batch3-20261031-2/repro-build \
  && python3 -m unittest discover -s tools/ybx-system-emu -p 'test_*.py' -q
Ran 323 tests in 18.357s
OK (skipped=8)
```

(The 8 skips are pre-existing optional integrations plus the opt-in real
compose.) Opt-in evidence pass:

```
YBX_RUN_REAL_COMPOSE=1 python3 -m unittest test_build_reproducible_qemu.RealChainComposeTests test_check_qemu_patch_composition -q
Ran 37 tests in 10.393s
OK
```

## Real read-only runs (this session)

1. `sh build_reproducible_qemu.sh --chain 0042 --skip-manifest-compare
   --work-dir /tmp/x4pro-check --qemu-src ~/.local/state/ybx-emulator-upstream/qemu`
   → stage 1 `failures: []`, composed base `febae182e132…`, 21 sources signed,
   4.6 s. (This run is what exposed the marker-gating bug; see checker notes.)
2. `sh build_reproducible_qemu.sh --work-dir /tmp/x4pro-full` (default = full
   audited chain, manifest comparison ON, no Docker) → stage 1
   `failures: []`, dry-run banner + printed orchestrator command, 4.7 s, and
   `FULL_MANIFEST_IDENTICAL True` — the produced manifest is byte-equal to
   `patches/composition-manifest.sha256`.
3. `check_qemu_patch_composition.py verify --live-qemu-root
   ~/.local/state/ybx-emulator-upstream/qemu-0043-agent-audit-20261030
   --fetch-depth 1` → `ok=true`, `failures=[]`, `deterministic_recompose=true`
   across the 25-file live status signature (live tree == fresh composition),
   chain 23 entries ending in the telemetry trio, `patches_signed=28`
   (~10 s).
4. `binary-sha` against the real audit build
   `qemu-0043-agent-audit-20261030/build-ybx/qemu-system-xtensa` →
   `ok=true` (`actual == expected == f07744d9…42630`); against the older
   0042-only build `qemu-rominit-20261029/...` (`4106cff2…`) → `ok=false`,
   **exit 1**. So the pin discriminates the telemetry layer.
5. `guardrails --qemu-root <audit tree> --flash <factory bin> --rom
   <s3 rom> --qemu-bin <audit build> --require-artifacts` → `ok=true`,
   `failures=[]`, all 5 pinned artifacts `state=ok`, `bin == expected ==
   f07744d90062…` compared (not recorded).

## What is now reproducible

- Clone-and-apply determinism: from bare `febae182`, the 23-patch audited
  chain composes byte-identically (double composition + cross-run manifest
  equality + live-tree equivalence, runs 2–3).
- Provenance: patch bytes, 25 source files, base commit, factory flash/ROM/SD
  media, and the shipped `qemu-system-xtensa` binary are signed/compared
  fail-closed by the checker; the builder refuses to compose or reuse
  anything that drifts from the checked-in manifest (full chain).

## What remains unproven (no fabrication)

- **The long container build.** I did not run `--build`; whether a fresh
  compose of `febae182` inside `ybx-qemu-builder:20261029` yields a binary
  with SHA `f07744d9…` is asserted only against the orchestrator's existing
  audit build (run 4). Only the human/orchestrator run proves toolchain
  determinism end to end.
- Acceptance frame SHAs (`8b81cec4…` etc.) remain guest-run measurements tied
  to the pinned binary; nothing here re-measures them and nothing here
  touched the live emulator (container `ybx-x4pro-browser-8784` untouched).
- Reverse-order linear undo is undefined for the real chain by design (0042
  rewrites 0023/0024 context; 0051 rewrites 0037 hunks); the composed trees
  prove determinism instead. The audit report keeps `reverse_undo`
  informationally for custom chains.
- Wi-Fi/ESP32-S3 radio: still not emulated; no association/IP/DHCP/TLS claims.

## Orchestrator commands

```sh
# host-only, seconds — fresh full-chain compose + manifest drift check:
~/.local/state/xteink-agent-workspaces/ybx-emu-batch3-20261031-2/repro-build/tools/ybx-system-emu/build_reproducible_qemu.sh

# full pipeline (long; composes if needed, then builds and compares the
# binary against the pinned f07744d9… SHA; never touches shared builds):
…/build_reproducible_qemu.sh --build --verify-binary --work-dir <same-dir-as-dry-run>

# independent audits (read-only):
python3 tools/ybx-system-emu/check_qemu_patch_composition.py verify \
  --live-qemu-root ~/.local/state/ybx-emulator-upstream/qemu-0043-agent-audit-20261030 --fetch-depth 1
python3 tools/ybx-system-emu/check_qemu_patch_composition.py binary-sha \
  --qemu-bin ~/.local/state/ybx-emulator-upstream/qemu-0043-agent-audit-20261030/build-ybx/qemu-system-xtensa
```

## Risks / limits

- The builder's stage-1 trust boundary is the checker; both live in this repo
  and are signed by review, not by code execution policy. A malicious
  `--chain-file` hook is test-only and never touches the audited comparison.
- `--skip-manifest-compare` weakens a run by design; the usage text and the
  0042-only flow document that such a run is not a factory reproduction.
- Pinned artifact paths are this developer's layout; elsewhere guardrails
  reports `missing` states (fail-closed only with `--require-artifacts`).
- `binary-sha` compares one file; it cannot detect a swapped build directory
  *after* verification — the `--build --verify-binary` flow verifies the
  binary it just produced in the composed temp tree, which is the intended
  acceptance moment.

## Files changed (batch 3)

- `tools/ybx-system-emu/check_qemu_patch_composition.py` — telemetry layer
  audited, binary SHA compared, compose_and_sign/compose CLI, chain-gated
  markers (mandatory `chain`).
- `tools/ybx-system-emu/build_reproducible_qemu.sh` — new two-stage builder.
- `tools/ybx-system-emu/test_check_qemu_patch_composition.py` — 39 tests.
- `tools/ybx-system-emu/test_build_reproducible_qemu.py` — 18 tests + opt-in
  real compose.
- `tools/ybx-system-emu/patches/composition-manifest.sha256` — regenerated
  (full chain).
- `tools/ybx-system-emu/experiments/0063-batch3-repro-build.md` — this report.
- (Root `README.md` deliberately untouched: the standing "README не правь" rule
  from the orchestrator overrides the earlier generic Russian-section request.)
