#!/bin/sh
# Reproducible QEMU build pipeline for the YBX X4 Pro emulator.
#
#   Stage 1 (host-only, fast): from the pinned Espressif QEMU base commit,
#     compose a FRESH throwaway tree with the audited chain
#     (patches/*.patch + experiments 0006..0024/0042, then telemetry
#     experiments 0043, 0037, 0051), fail-closed on any drift, and write the
#     source SHA-256 manifest.
#   Stage 2 (opt-in, long): build qemu-system-xtensa inside the pinned image
#     ybx-qemu-builder:20261029 with the QEMU_BRINGUP.md configure flags.
#
# Without --build the script never invokes Docker; the default dry run prints
# the exact orchestrator command. Never rebuilds a shared/live QEMU build
# directory: the build tree is always the freshly composed temp copy.
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
CHECKER="$SCRIPT_DIR/check_qemu_patch_composition.py"
IMAGE_DEFAULT="ybx-qemu-builder:20261029"
PLATFORM_DEFAULT="linux/arm64"
# QEMU_BRINGUP.md documents --enable-gcrypt --disable-nettle as essential: the
# automatic crypto option preferred GNUTLS and dropped the esp32c3/s3 aes QOM
# devices, which breaks startup. Keep this string in sync with the bringup doc.
CONFIGURE_FLAGS_DEFAULT="--target-list=xtensa-softmmu,riscv32-softmmu --enable-gcrypt --disable-nettle --disable-docs --disable-gtk --disable-sdl --disable-vnc --disable-curses --disable-tools --disable-werror"
# Test/CI hook only: override the container runner (default: docker).
DOCKER_BIN=${YBX_DOCKER_BIN:-docker}

usage() {
  cat >&2 <<EOF
Usage: $0 [--build] [--qemu-src DIR] [--work-dir DIR] [--image TAG]
          [--platform REF] [--jobs N] [--configure-flags STR] [--verify-binary]
          [--chain full|0042] [--fetch-depth N] [--no-keep]
          [--print-build-command]
  --build              compile qemu-system-xtensa via $DOCKER_BIN (default: no)
  --qemu-src DIR       Git object source for the pinned base commit
                       (default: \$HOME/.local/state/ybx-emulator-upstream/qemu)
  --work-dir DIR       output root (default: fresh mktemp dir, kept on exit)
  --image TAG          builder image (default: $IMAGE_DEFAULT)
  --platform REF       container platform (default: $PLATFORM_DEFAULT)
  --jobs N             ninja -j (default: 3)
  --configure-flags S  override the documented configure flags (avoid)
  --verify-binary      after --build, compare the produced qemu-system-xtensa
                       against the pinned chain SHA (fail-closed)
  --chain SPEC         audited chain selection passed to the checker
                       (default: full = audited 32-patch chain ending in 0101)
  --skip-manifest-compare  compose without diffing against the checked-in
                       manifest. Required for --chain 0042 (that manifest
                       signs the FULL chain); such a run is a shorter audit,
                       NOT a factory reproduction
  --fetch-depth N      shallow-fetch depth for the base commit (default: 1)
  --no-keep            delete the composed tree after stage 1 (manifest kept
                       next to the work dir)
  --print-build-command  print the stage-2 container command and exit (never
                       composes or builds; use for review)
  --chain-file FILE    test hook: patch names (one per line) replacing the
                       audited chain definition; used with --patch-root
  --patch-root DIR     test hook: directory holding the patch files
  --base SHA           test hook: base commit override (default: pinned)
EOF
}

die() { echo "build_reproducible_qemu: $*" >&2; exit 1; }

BUILD=0 VERIFY_BINARY=0 KEEP=1 JOBS=3 PRINT_CMD=0
SKIP_MANIFEST_COMPARE=0
CHAIN=full FETCH_DEPTH=1 IMAGE=$IMAGE_DEFAULT PLATFORM=$PLATFORM_DEFAULT
CONFIGURE_FLAGS=$CONFIGURE_FLAGS_DEFAULT
WORK_DIR=
CHAIN_FILE=
PATCH_ROOT=
BASE_OVERRIDE=
QEMU_SRC="$HOME/.local/state/ybx-emulator-upstream/qemu"

while [ $# -gt 0 ]; do
  case "$1" in
    --build) BUILD=1 ;;
    --verify-binary) VERIFY_BINARY=1 ;;
    --no-keep) KEEP=0 ;;
    --print-build-command) PRINT_CMD=1 ;;
    --jobs) [ $# -ge 2 ] || die "--jobs needs a value"; JOBS=$2; shift ;;
    --chain) [ $# -ge 2 ] || die "--chain needs a value"; CHAIN=$2; shift ;;
    --skip-manifest-compare) SKIP_MANIFEST_COMPARE=1 ;;
    --chain-file) [ $# -ge 2 ] || die "--chain-file needs a value"; CHAIN_FILE=$2; shift ;;
    --patch-root) [ $# -ge 2 ] || die "--patch-root needs a value"; PATCH_ROOT=$2; shift ;;
    --base) [ $# -ge 2 ] || die "--base needs a value"; BASE_OVERRIDE=$2; shift ;;
    --fetch-depth) [ $# -ge 2 ] || die "--fetch-depth needs a value"; FETCH_DEPTH=$2; shift ;;
    --image) [ $# -ge 2 ] || die "--image needs a value"; IMAGE=$2; shift ;;
    --platform) [ $# -ge 2 ] || die "--platform needs a value"; PLATFORM=$2; shift ;;
    --configure-flags) [ $# -ge 2 ] || die "--configure-flags needs a value"
      CONFIGURE_FLAGS=$2; shift ;;
    --qemu-src) [ $# -ge 2 ] || die "--qemu-src needs a value"; QEMU_SRC=$2; shift ;;
    --work-dir) [ $# -ge 2 ] || die "--work-dir needs a value"; WORK_DIR=$2; shift ;;
    -h|--help) usage; exit 0 ;;
    *) usage; die "unknown option: $1" ;;
  esac
  shift
done

[ -f "$CHECKER" ] || die "checker missing: $CHECKER"
command -v python3 >/dev/null 2>&1 || die "python3 not found"
printf '%s\n' "$JOBS" | grep -q '^[1-9][0-9]*$' || die "--jobs must be a number"

TREE=""
MANIFEST=""

if [ "$PRINT_CMD" -eq 1 ]; then
  echo "$DOCKER_BIN run --rm --platform $PLATFORM -v <COMPOSED_TREE>:/src -w /src $IMAGE sh -lc 'set -eu; mkdir -p build-ybx && cd build-ybx; ../configure $CONFIGURE_FLAGS; ninja -j$JOBS qemu-system-xtensa'"
  exit 0
fi

[ -d "$QEMU_SRC" ] || die "QEMU object source not found: $QEMU_SRC"
if [ -z "$WORK_DIR" ]; then
  WORK_DIR=$(mktemp -d "${TMPDIR:-/tmp}/ybx-qemu-repro-XXXXXX")
else
  [ -d "$WORK_DIR" ] || die "work dir does not exist: $WORK_DIR"
fi
COMPOSE_ROOT="$WORK_DIR/compose"
TREE="$COMPOSE_ROOT/qemu-compose"
MANIFEST="$COMPOSE_ROOT/composition-manifest.sha256"

if [ -d "$TREE" ] || [ -e "$MANIFEST" ]; then
  # Reuse mode: only a composition whose stage 1 exited 0 for THIS chain spec
  # may be picked up (the sentinel records the spec).
  [ -d "$TREE" ] || die "work dir is half-composed: tree missing under $WORK_DIR"
  [ -f "$MANIFEST" ] || die "work dir is half-composed: manifest missing"
  [ -f "$COMPOSE_ROOT/.stage1-ok" ] \
    || die "refusing to reuse a composition that never completed stage 1; delete $COMPOSE_ROOT or pick a fresh --work-dir"
  PRIOR_CHAIN=$(cat "$COMPOSE_ROOT/.stage1-ok")
  [ "$PRIOR_CHAIN" = "$CHAIN" ] \
    || die "prior composition was signed for chain '$PRIOR_CHAIN', not '$CHAIN'; pick a fresh --work-dir"
  echo "=== stage 1: reusing prior composition in $WORK_DIR ==="
  echo "source tree   : $TREE"
  echo "sha manifest  : $MANIFEST"
else
  echo "=== stage 1: compose audited chain '$CHAIN' from the pinned base ==="
  set -- python3 "$CHECKER" compose --qemu-src "$QEMU_SRC" \
    --out "$COMPOSE_ROOT" --chain "$CHAIN" --fetch-depth "$FETCH_DEPTH"
  [ "$SKIP_MANIFEST_COMPARE" -eq 0 ] || set -- "$@" --skip-manifest-compare
  [ -z "$CHAIN_FILE" ] || set -- "$@" --chain-file "$CHAIN_FILE"
  [ -z "$PATCH_ROOT" ] || set -- "$@" --patch-root "$PATCH_ROOT"
  [ -z "$BASE_OVERRIDE" ] || set -- "$@" --base "$BASE_OVERRIDE"
  "$@" || die "stage 1 failed"
  [ -f "$MANIFEST" ] || die "stage 1 produced no manifest: $MANIFEST"
  BASE=$(python3 - "$MANIFEST" <<'PYEOF'
import re, sys
text = open(sys.argv[1]).read()
m = re.search(r"^commit=([0-9a-f]{40})$", text, re.M)
print(m.group(1) if m else "")
PYEOF
)
  [ -n "$BASE" ] || die "manifest has no base commit line"
  printf '%s\n' "$CHAIN" > "$COMPOSE_ROOT/.stage1-ok"
  echo "composed base : $BASE"
  echo "source tree   : $TREE"
  echo "sha manifest  : $MANIFEST"
fi

if [ "$BUILD" -eq 0 ]; then
  echo "=== dry run: no container build was invoked (pass --build to compile) ==="
  echo "orchestrator command:"
  echo "  $0 --build --verify-binary --work-dir '$WORK_DIR'"
  if [ "$KEEP" -eq 0 ]; then
    rm -rf "$TREE"
    echo "kept manifest only (removed $TREE)"
  fi
  exit 0
fi

echo "=== stage 2: build qemu-system-xtensa in $IMAGE ==="
command -v "$DOCKER_BIN" >/dev/null 2>&1 || die "$DOCKER_BIN not found; --build needs the container runner"
[ -d "$TREE" ] || die "composed tree missing: $TREE"
"$DOCKER_BIN" run --rm --platform "$PLATFORM" -v "$TREE:/src" -w /src "$IMAGE" \
  sh -lc "set -eu
mkdir -p build-ybx && cd build-ybx
../configure $CONFIGURE_FLAGS
ninja -j$JOBS qemu-system-xtensa" || die "stage 2 container build failed"

BIN="$TREE/build-ybx/qemu-system-xtensa"
[ -f "$BIN" ] || die "build finished but no binary at $BIN"
echo "binary        : $BIN"
if [ "$VERIFY_BINARY" -eq 1 ]; then
  python3 "$CHECKER" binary-sha --qemu-bin "$BIN" || die "binary SHA does not match the pinned chain build"
else
  python3 - "$BIN" <<'PYEOF'
import hashlib, sys
print("binary sha256:", hashlib.file_digest(open(sys.argv[1], "rb"),
                                           "sha256").hexdigest())
print("(recorded only; --verify-binary compares against the pinned chain SHA)")
PYEOF
fi
