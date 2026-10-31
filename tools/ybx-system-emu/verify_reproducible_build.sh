#!/bin/sh
# Repeatable end-to-end verification of the reproducible YBX QEMU build.
#
# Pipeline this script drives (see experiments/0072 for the executed proof):
#   audit   (default, seconds, host-only): the checked-in patch chain, the
#           composition manifest and the pinned qemu-system-xtensa SHA.
#   compose (--compose, host-only, minutes): rebuild the 23-patch tree from
#           the bare base commit and fail-closed on any source drift.
#   build   (--build, OPT-IN, long): compile qemu-system-xtensa in the pinned
#           container via build_reproducible_qemu.sh --build --verify-binary.
#   binary  (automatic once a binary is resolved): compare against the pinned
#           SHA f07744d9… — byte-identical or a named diff, never a guess.
#   boot    (--boot, OPT-IN, QEMU-level probe for the orchestrator): run the
#           factory flash inside the builder container via
#           probe_x4pro_spi2_guest.py --epd and compare the captured native
#           panel frame against the accepted Home frame SHA e79c2ad5…
#
# Nothing here touches the live browser emulator container; the boot stage
# always spawns its own throwaway QEMU process on temp media copies.
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
CHECKER="$SCRIPT_DIR/check_qemu_patch_composition.py"
BUILDER="$SCRIPT_DIR/build_reproducible_qemu.sh"
IMAGE_DEFAULT="ybx-qemu-builder:20261029"
PLATFORM_DEFAULT="linux/arm64"
# Test/CI hook: override the container runner (same convention as the builder).
DOCKER_BIN=${YBX_DOCKER_BIN:-docker}
# Accepted Home frame (raw 800x480 1bpp visible SSD1677 RAM, guest-written;
# QEMU_BRINGUP.md "Real firmware and genuine framebuffer acceptance").
HOME_FRAME_SHA256="e79c2ad5f2b60b16fe3f20625742d5f1343f5785552d1b32c11f33c42a874672"
QA_DIR="$HOME/Projects/YBX/artifacts/ybx-esp32-system-emulation-20261029/qa-20261029"

usage() {
  cat >&2 <<EOF
Usage: $0 [--audit] [--compose] [--build] [--boot] [--print-plan]
          [--qemu-src DIR] [--qemu-bin PATH] [--work-dir DIR] [--jobs N]
          [--image TAG] [--platform REF]
          [--flash PATH] [--rom PATH] [--sd-image PATH] [--boot-seconds N]
  --audit           run the fast chain/manifest audit (always implied)
  --compose         host-only stage-1 composition + manifest drift check
  --build           opt-in container build via build_reproducible_qemu.sh
                    (implies --compose; logs to <work-dir>/verify-build.log)
  --boot            opt-in QEMU-level boot probe: factory flash in a throwaway
                    container QEMU, compare Home frame SHA; needs a binary
                    (--qemu-bin or a --build result) and pinned factory media
  --print-plan      print the pinned facts and stage commands, run nothing
  --qemu-src DIR    Git object source for compose/build (default:
                    \$HOME/.local/state/ybx-emulator-upstream/qemu)
  --qemu-bin PATH   existing qemu-system-xtensa to hash-compare (and to boot)
  --work-dir DIR    output root (default: fresh mktemp dir, kept on exit;
                    with --build/--boot it must live under a Docker-fileshared
                    path, e.g. \$HOME/.local/state/…)
  --jobs N          ninja -j for --build (default: 2)
  --image TAG       builder image (default: $IMAGE_DEFAULT)
  --platform REF    container platform (default: $PLATFORM_DEFAULT)
  --flash/--rom/--sd-image PATH  guest media for --boot (default: pinned QA
                    copies; every one is SHA-verified before the container
                    starts; the probe itself works on temp copies)
  --boot-seconds N  bounded guest run for --boot (default: 33, the duration
                    the accepted Home frame was measured at)
EOF
}

die() { echo "verify_reproducible_build: $*" >&2; exit 1; }

[ -f "$CHECKER" ] || die "checker missing: $CHECKER"
[ -f "$BUILDER" ] || die "builder missing: $BUILDER"
command -v python3 >/dev/null 2>&1 || die "python3 not found"

AUDIT=1 COMPOSE=0 BUILD=0 BOOT=0 PRINT_CMD=0 JOBS=2
IMAGE=$IMAGE_DEFAULT PLATFORM=$PLATFORM_DEFAULT
QEMU_SRC="$HOME/.local/state/ybx-emulator-upstream/qemu"
QEMU_BIN=""
WORK_DIR=""
FLASH="$QA_DIR/s3-flash-complete-rom-test.bin"
ROM="$QA_DIR/s3-full-rom-generator-test.bin"
SD_IMAGE="$QA_DIR/ybx-test-sd-fat16-mbr-pristine.img"
BOOT_SECONDS=33

while [ $# -gt 0 ]; do
  case "$1" in
    --audit) AUDIT=1 ;;
    --compose) COMPOSE=1 ;;
    --build) BUILD=1; COMPOSE=1 ;;
    --boot) BOOT=1 ;;
    --print-plan) PRINT_CMD=1 ;;
    --jobs) [ $# -ge 2 ] || die "--jobs needs a value"; JOBS=$2; shift ;;
    --image) [ $# -ge 2 ] || die "--image needs a value"; IMAGE=$2; shift ;;
    --platform) [ $# -ge 2 ] || die "--platform needs a value"; PLATFORM=$2; shift ;;
    --qemu-src) [ $# -ge 2 ] || die "--qemu-src needs a value"; QEMU_SRC=$2; shift ;;
    --qemu-bin) [ $# -ge 2 ] || die "--qemu-bin needs a value"; QEMU_BIN=$2; shift ;;
    --work-dir) [ $# -ge 2 ] || die "--work-dir needs a value"; WORK_DIR=$2; shift ;;
    --flash) [ $# -ge 2 ] || die "--flash needs a value"; FLASH=$2; shift ;;
    --rom) [ $# -ge 2 ] || die "--rom needs a value"; ROM=$2; shift ;;
    --sd-image) [ $# -ge 2 ] || die "--sd-image needs a value"; SD_IMAGE=$2; shift ;;
    --boot-seconds) [ $# -ge 2 ] || die "--boot-seconds needs a value"; BOOT_SECONDS=$2; shift ;;
    -h|--help) usage; exit 0 ;;
    *) usage; die "unknown option: $1" ;;
  esac
  shift
done

printf '%s\n' "$JOBS" | grep -q '^[1-9][0-9]*$' || die "--jobs must be a number"
printf '%s\n' "$BOOT_SECONDS" | grep -q '^[1-9][0-9]*$' \
  || die "--boot-seconds must be a number"

EXPECTED_BIN=$(python3 "$CHECKER" expected-binary-sha) \
  || die "cannot read the pinned binary SHA from the checker"

if [ "$PRINT_CMD" -eq 1 ]; then
  echo "pinned expected binary sha : $EXPECTED_BIN"
  echo "pinned accepted home frame : $HOME_FRAME_SHA256"
  echo "builder image              : $IMAGE ($PLATFORM)"
  echo "stage audit   : python3 \"$CHECKER\" verify --skip-compose"
  echo "stage compose : sh \"$BUILDER\" --qemu-src \"$QEMU_SRC\" --work-dir <WORK_DIR>"
  echo "stage build   : sh \"$BUILDER\" --build --verify-binary --jobs $JOBS --qemu-src \"$QEMU_SRC\" --work-dir <WORK_DIR>"
  echo "stage binary  : python3 \"$CHECKER\" binary-sha --qemu-bin <resolved qemu-system-xtensa>"
  echo "stage boot    : $DOCKER_BIN run --rm --platform $PLATFORM -v \"$SCRIPT_DIR\":/tests:ro -v <BINDIR>:/qemubin:ro -v <flash>:<flashmount>:ro -v <rom>:<rommount>:ro -v <sd>:<sdmount>:ro -v <WORK_DIR>:/out -w /tests $IMAGE python3 /tests/probe_x4pro_spi2_guest.py --qemu-bin /qemubin/qemu-system-xtensa --rom <rommount> --flash <flashmount> --sd-image <sdmount> --epd --seconds $BOOT_SECONDS --out /out/boot-frame.json"
  echo "stage boot    : then compare epd_ram_state.visible-frame-sha256 against $HOME_FRAME_SHA256"
  exit 0
fi

if [ -z "$WORK_DIR" ]; then
  WORK_DIR=$(mktemp -d "${TMPDIR:-/tmp}/ybx-verify-repro-XXXXXX")
else
  [ -d "$WORK_DIR" ] || die "work dir does not exist: $WORK_DIR"
fi

STAGE_FAIL=0

# --- stage audit (fast, host-only) -------------------------------------------
echo "=== stage audit: chain definitions + patch signatures + manifest ==="
if python3 "$CHECKER" verify --skip-compose >/dev/null; then
  echo "stage=audit state=pass"
else
  echo "stage=audit state=fail" >&2
  STAGE_FAIL=1
  die "audit failed; refuse to continue"
fi

# --- stage compose (host-only) -----------------------------------------------
BUILT_BIN="$WORK_DIR/compose/qemu-compose/build-ybx/qemu-system-xtensa"
if [ "$COMPOSE" -eq 1 ]; then
  echo "=== stage compose: rebuild the audited chain from the bare base ==="
  sh "$BUILDER" --qemu-src "$QEMU_SRC" --work-dir "$WORK_DIR" \
    || { echo "stage=compose state=fail" >&2; die "compose failed"; }
  echo "stage=compose state=pass"
else
  echo "stage=compose state=skip (pass --compose or --build)"
fi

# --- stage build (opt-in, long) ------------------------------------------------
if [ "$BUILD" -eq 1 ]; then
  echo "=== stage build: container build, log $WORK_DIR/verify-build.log ==="
  if sh "$BUILDER" --build --verify-binary --jobs "$JOBS" \
       --image "$IMAGE" --platform "$PLATFORM" \
       --qemu-src "$QEMU_SRC" --work-dir "$WORK_DIR" \
       >"$WORK_DIR/verify-build.log" 2>&1; then
    echo "stage=build state=pass log=$WORK_DIR/verify-build.log"
  else
    tail -n 20 "$WORK_DIR/verify-build.log" >&2 || true
    echo "stage=build state=fail log=$WORK_DIR/verify-build.log" >&2
    die "container build failed (see log tail above)"
  fi
else
  echo "stage=build state=skip (pass --build to compile)"
fi

# --- stage binary: compare against the pinned accepted binary -----------------
if [ "$BUILD" -eq 1 ]; then
  RESOLVED_BIN="$BUILT_BIN"
elif [ -n "$QEMU_BIN" ]; then
  [ -f "$QEMU_BIN" ] || { echo "stage=binary state=missing path=$QEMU_BIN" >&2; die "binary does not exist: $QEMU_BIN"; }
  RESOLVED_BIN="$QEMU_BIN"
else
  RESOLVED_BIN=""
fi
if [ -n "$RESOLVED_BIN" ]; then
  echo "=== stage binary: SHA-256 compare against $EXPECTED_BIN ==="
  ACTUAL_BIN=$(python3 - "$RESOLVED_BIN" <<'PYEOF'
import hashlib, sys
print(hashlib.file_digest(open(sys.argv[1], "rb"), "sha256").hexdigest())
PYEOF
)
  echo "binary        : $RESOLVED_BIN"
  echo "actual sha256 : $ACTUAL_BIN"
  if python3 "$CHECKER" binary-sha --qemu-bin "$RESOLVED_BIN" >/dev/null; then
    echo "stage=binary state=byte-identical expected=$EXPECTED_BIN"
  else
    echo "stage=binary state=differs expected=$EXPECTED_BIN actual=$ACTUAL_BIN" >&2
    STAGE_FAIL=1
  fi
else
  echo "stage=binary state=skip (no --qemu-bin and no --build result)"
fi

# --- stage boot (opt-in, QEMU-level probe) -------------------------------------
if [ "$BOOT" -eq 1 ]; then
  echo "=== stage boot: factory flash on a throwaway container QEMU ==="
  [ -n "$RESOLVED_BIN" ] \
    || { echo "stage=boot state=skip reason=no-binary" >&2; die "--boot needs --qemu-bin or --build"; }
  for media in "$FLASH" "$ROM" "$SD_IMAGE"; do
    [ -f "$media" ] || { echo "stage=boot state=fail reason=missing-media path=$media" >&2; die "guest media missing: $media"; }
  done
  # Fail-closed media pinning: refuse to boot anything that is not the audited
  # factory set. The expected digests come from the checker constants.
  python3 - "$CHECKER" "$FLASH" "$ROM" "$SD_IMAGE" <<'PYEOF' || die "factory media are not the pinned QA set; boot stage is fail-closed"
import sys
sys.path.insert(0, sys.argv[1][: sys.argv[1].rindex("/")])
import check_qemu_patch_composition as chk
from pathlib import Path
expected = {
    "flash": chk.FACTORY_FLASH_SHA256,
    "rom": chk.FACTORY_ARTIFACTS["rom_s3_drom_complete"][1],
    "sd": chk.FACTORY_ARTIFACTS["sd_test_mbr_pristine"][1],
}
given = {"flash": sys.argv[2], "rom": sys.argv[3], "sd": sys.argv[4]}
bad = []
for name, path in given.items():
    digest = chk.file_sha256(Path(path))
    if digest != expected[name]:
        bad.append(f"{name}: {digest[:12]}… != pinned {expected[name][:12]}…")
if bad:
    print("\n".join(bad), file=sys.stderr)
    sys.exit(1)
PYEOF
  BIN_NAME=$(basename "$RESOLVED_BIN")
  BIN_DIR=$(dirname "$RESOLVED_BIN")
  command -v "$DOCKER_BIN" >/dev/null 2>&1 \
    || { echo "stage=boot state=fail reason=no-docker" >&2; die "$DOCKER_BIN not found; --boot needs the container runner"; }
  "$DOCKER_BIN" run --rm --platform "$PLATFORM" \
    -v "$SCRIPT_DIR:/tests:ro" \
    -v "$BIN_DIR:/qemubin:ro" \
    -v "$FLASH:/media/flash.bin:ro" \
    -v "$ROM:/media/rom.bin:ro" \
    -v "$SD_IMAGE:/media/sd.img:ro" \
    -v "$WORK_DIR:/out" \
    -w /tests "$IMAGE" \
    python3 /tests/probe_x4pro_spi2_guest.py \
      --qemu-bin "/qemubin/$BIN_NAME" --rom /media/rom.bin \
      --flash /media/flash.bin --sd-image /media/sd.img \
      --epd --seconds "$BOOT_SECONDS" --out /out/boot-frame.json \
    >"$WORK_DIR/boot-probe.log" 2>&1 \
    || { tail -n 20 "$WORK_DIR/boot-probe.log" >&2 || true; echo "stage=boot state=fail reason=probe-error log=$WORK_DIR/boot-probe.log" >&2; die "boot probe failed"; }
  BOOT_STATUS=0
  python3 - "$WORK_DIR/boot-frame.json" "$HOME_FRAME_SHA256" <<'PYEOF' || BOOT_STATUS=$?
import json, sys
report = json.load(open(sys.argv[1]))
state = report.get("epd_ram_state") or {}
frames = state.get("frames")
sha = state.get("visible-frame-sha256")
if not frames:
    print("stage=boot state=no-frame guest painted 0 frames in the bounded "
          f"run (rom_boot_seen={report.get('rom_boot_seen')})")
    sys.exit(1)
if sha is None:
    print("stage=boot state=no-frame frames painted but panel RAM was blank")
    sys.exit(1)
print(f"guest frames={frames} nonwhite={state.get('nonwhite')} "
      f"ram-writes={state.get('ram-writes')}")
print("frame sha256  :", sha)
if sha == sys.argv[2]:
    print("stage=boot state=home-frame-match")
    sys.exit(0)
print(f"stage=boot state=home-frame-differs expected={sys.argv[2]} actual={sha}")
sys.exit(2)
PYEOF
  case "$BOOT_STATUS" in
    0) : ;;
    2) echo "stage=boot state=home-frame-differs" >&2
       echo "the clean-room binary boots, but NOT with the accepted frame" >&2
       STAGE_FAIL=1 ;;
    *) echo "stage=boot state=fail status=$BOOT_STATUS log=$WORK_DIR/boot-probe.log" >&2
       STAGE_FAIL=1 ;;
  esac
else
  echo "stage=boot state=skip (pass --boot to run the guest probe)"
fi

if [ "$STAGE_FAIL" -ne 0 ]; then
  echo "verify_reproducible_build: completed with failures (see stage= lines)" >&2
  exit 1
fi
echo "verify_reproducible_build: all requested stages passed; work dir kept: $WORK_DIR"
