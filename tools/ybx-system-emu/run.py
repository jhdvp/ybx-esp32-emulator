#!/usr/bin/env python3
"""Boot REAL merged ESP32 flash images inside Espressif ESP-EMU.

Scope: CPU, ROM, bootloader, application startup. This is NOT a full
YBX hardware emulation environment: the current release has no Xteink e-ink
controller model and lacks the X4 Pro SDMMC card model.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any

CHIPS = {"c3": "esp32c3", "s3": "esp32s3"}
CHIP_MODELS = {"c3": "Xteink X3 / X4", "s3": "Xteink X4 Pro"}
DEFAULT_IMAGE_GLOB = {
    "c3": "YBX-SETTINGS-UNRELEASED-default-*-factory.bin",
    "s3": "YBX-SETTINGS-UNRELEASED-x4pro-*-factory.bin",
}
# These are register-file stubs, not driver-compatible physical device models.
I2C_STUBS = {
    "c3": [
        "0:0x55",  # X3 fuel gauge BQ27220
        "0:0x68",  # DS3231 RTC
        "0:0x6b",  # QMI8658 IMU
    ],
    "s3": [
        "0:0x5d,width=2,reg=0x8140=0x39",  # GT911 touch, basic register reply only
        "0:0x14,width=2",                   # GT911 alternate address
        "0:0x51",                           # PCF8563 RTC
        "0:0x63,reg=0x04=0x64",             # CW2017 fuel-gauge stub
    ],
}

def classify_log(log: str, chip: str, code: int, i2c: bool) -> dict[str, Any]:
    """Conservative evidence model; emulator timeout is never test PASS."""
    markers = {
        "rom_boot_seen": "ESP-ROM:" in log,
        "flash_boot_seen": "SPI_FAST_FLASH_BOOT" in log and "entry 0x" in log,
        "arduino_component_seen": (
            "[Preferences.cpp:" in log or "[Wire.cpp:" in log or
            "sdmmc_periph:" in log or "Arduino" in log
        ),
        "i2c_driver_errors": "ESP_ERR_INVALID_STATE" in log and "i2c" in log.lower(),
        "sdmmc_driver_error": "sdmmc_host_reset returned 0x107" in log,
        "panic_detected": bool(re.search(r"Guru Meditation Error|assert failed:|abort\(\)", log)),
        "emulator_timeout": "Timeout reached after" in log,
    }
    # Explicit UNKNOWN for all downstream subsystems until their own
    # end-to-end workload and virtual device assertions pass.
    return {
        "chip": chip,
        "emulator_exit_code": code,
        "i2c_register_stubs": i2c,
        "rom": "observed" if markers["rom_boot_seen"] else "not_observed",
        "spi_flash_boot": "observed" if markers["flash_boot_seen"] else "not_observed",
        "application_code": "observed" if markers["arduino_component_seen"] else "not_observed",
        "i2c": "driver_errors" if markers["i2c_driver_errors"] else
               ("register_stub_no_error_observed" if i2c else "unknown"),
        "sdmmc": "driver_error" if markers["sdmmc_driver_error"] else "unverified",
        "epaper": "not_emulated",
        "sd_card_io": "not_verified",
        "wifi_station": "not_verified",
        "tls_handshake": "not_verified",
        "markers": markers,
    }

def validate_image(path: Path) -> str:
    path = path.expanduser().resolve(strict=True)
    size = path.stat().st_size
    if not 0x10000 < size <= 16*1024*1024:
        raise ValueError(f"{path}: expected full merged flash image, got {size} bytes")
    with path.open("rb") as src:
        if src.read(1) != b"\xe9":
            raise ValueError(f"{path}: missing bootloader image header at offset 0")
        src.seek(0x8000)
        if src.read(2) != b"\xaa\x50":
            raise ValueError(f"{path}: missing partition table at 0x8000")
        src.seek(0x10000)
        if src.read(1) != b"\xe9":
            raise ValueError(f"{path}: missing application image header at 0x10000")
    with path.open("rb") as src:
        return hashlib.file_digest(src, "sha256").hexdigest()

def resolved_image(chip: str, explicit: Path | None, directory: Path | None) -> Path:
    if explicit is not None:
        return explicit.expanduser().resolve(strict=True)
    if directory is None:
        raise ValueError(f"{chip}: supply --{chip}-image or --images-dir")
    found = sorted(directory.expanduser().glob(DEFAULT_IMAGE_GLOB[chip]))
    if len(found) != 1:
        raise ValueError(f"{chip}: expected exactly one {DEFAULT_IMAGE_GLOB[chip]} in {directory}, found {len(found)}")
    return found[0].resolve()

def run_one(chip: str, image: Path, emulator: Path, seconds: int,
            i2c: bool, network: bool, output_dir: Path) -> dict[str, Any]:
    digest=validate_image(image)
    cmd = [str(emulator), "--chip", CHIPS[chip],
           "--firmware", str(image),
           "--net", "user" if network else "user,restrict=yes",
           "--timeout", f"{seconds}s"]
    if i2c:
        for stub in I2C_STUBS[chip]:
            cmd.extend(["--i2c-dev", stub])
    try:
        # No --save-state; never alter the firmware image.
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace",
                              timeout=seconds+20, stdin=subprocess.DEVNULL)
        output = proc.stdout+"\n"+proc.stderr
        exit_code=proc.returncode
    except subprocess.TimeoutExpired as exc:
        output = (exc.stdout or b"").decode("utf-8","replace") + "\n" + (
            (exc.stderr or b"").decode("utf-8","replace"))
        output += "\nHOST WATCHDOG: child timeout\n"
        exit_code=124
    status=classify_log(output, chip, exit_code, i2c)
    log_name = f"esp32-{chip}-{'i2c-stubs' if i2c else 'baseline'}.log"
    (output_dir/log_name).write_text(output)
    status.update({
        "image": str(image), "image_sha256": digest,
        "image_size": image.stat().st_size,
        "network_mode": "user NAT (explicitly permitted)" if network else
                        "user NAT restricted (DNS only; no outbound TCP/UDP)",
        "seconds_requested": seconds,
        "log": log_name,
        "command": cmd,
    })
    return status

def main() -> int:
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--chip", choices=["c3","s3","both"],default="both")
    ap.add_argument("--c3-image",type=Path)
    ap.add_argument("--s3-image",type=Path)
    ap.add_argument("--images-dir",type=Path)
    ap.add_argument("--esp-emu",type=Path,default=Path.home()/".local/bin/esp-emu")
    ap.add_argument("--duration",type=int,default=8)
    ap.add_argument("--i2c-stubs",action="store_true",
                    help="inject register-file stubs, never claim hardware fidelity")
    ap.add_argument("--allow-network",action="store_true",
                    help="OPT-IN: permit real outbound TCP/UDP from guest firmware")
    ap.add_argument("--out",type=Path,default=Path("/tmp/ybx-esp32-emu-report"))
    args=ap.parse_args()
    if not 2 <= args.duration <= 60:
        ap.error("--duration must be between 2 and 60 seconds")
    exe=args.esp_emu.expanduser().resolve(strict=True)
    args.out.mkdir(parents=True,exist_ok=True)
    chips=list(CHIPS) if args.chip=="both" else [args.chip]
    report={"generated_at":datetime.now(timezone.utc).isoformat(),
            "engine":"Espressif esp-emu (real CPU / full merged flash)",
            "mode":"REAL firmware execution, not framebuffer host renderer",
            "results": [],
            "peripheral_gates": {
                "spi_flash":"ROM/bootloader boot markers",
                "epaper_spi":"BLOCKED: no SSD1677/UC8253/UC8179/UC8279 external display model",
                "sd_spi_c3":"BLOCKED: no verified virtual SD-card block device",
                "sdmmc_s3":"BLOCKED: X4 Pro requires 1-bit SDMMC model",
                "touch_gt911":"STUB ONLY: register-file I2C is not functional touch model",
                "wifi":"UNTESTED: requires provisioning credentials/test AP and firmware continuing",
                "tls":"UNTESTED: requires WiFi, mock backend, CA/time, handshake assertions",
            }}
    for chip in chips:
        image=resolved_image(chip,getattr(args,f"{chip}_image"),args.images_dir)
        print(f"[{CHIP_MODELS[chip]}] real ROM + bootloader + app image: {image.name}",flush=True)
        entry=run_one(chip,image,exe,args.duration,args.i2c_stubs,args.allow_network,args.out)
        report["results"].append(entry)
        print(f"  ROM={entry['rom']}; Flash={entry['spi_flash_boot']}; "+
              f"App={entry['application_code']}; I2C={entry['i2c']}; SDMMC={entry['sdmmc']}",flush=True)
    target=args.out/"report.json"
    target.write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n")
    print(f"Report: {target}")
    # Exit 0 means only that the boot-stage evidence gates succeeded.
    # It is NOT an end-to-end/peripheral acceptance result.
    return 0 if all(row["rom"]=="observed" and row["spi_flash_boot"]=="observed"
                    and row["application_code"]=="observed" and
                    not row["markers"]["panic_detected"] and
                    row["emulator_exit_code"]==0
                    for row in report["results"]) else 1

if __name__=="__main__":
    try:
        sys.exit(main())
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        print(f"ERROR: {exc}",file=sys.stderr)
        sys.exit(2)
