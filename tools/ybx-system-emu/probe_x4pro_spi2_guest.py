#!/usr/bin/env python3
"""Observe real factory S3 app SPI2 PIO counters, NOT EPD pixels or UI readiness.

Runs inside offline Docker with a copied SD test disk, real CPU (no qtest
accelerator). No production firmware, media, or device hardware is modified.
"""
import argparse
import hashlib
import json
import re
import shutil
from pathlib import Path
import subprocess
import time
from tempfile import TemporaryDirectory
from probe_x4pro_touch_injection_qtest import qmp_connect

def main():
    p=argparse.ArgumentParser(description=__doc__)
    for key in ("qemu-bin","rom","flash","sd-image","out"):
        p.add_argument("--"+key,type=Path,required=True)
    p.add_argument("--seconds",type=int,default=9)
    p.add_argument("--trace-sd",action="store_true",
                   help="trace actual SDMMC commands and QEMU SD card sector reads")
    p.add_argument("--original-fat",type=Path,
                   help="optional unchanged superfloppy FAT16 baseline for same-run SD proof")
    p.add_argument("--epd",action="store_true",
                   help="attach experimental SSD1677 panel and collect true SPI RAM frame")
    a=p.parse_args()
    with TemporaryDirectory(prefix="ybx-spi2-factory-") as temp:
        root=Path(temp)
        bios=root/"bios";bios.mkdir()
        shutil.copyfile(a.rom,bios/"esp32s3_rev0_rom.bin")
        shutil.copyfile(a.sd_image,root/"test-sd.img")
        # Never write to the actual factory flash file; QEMU VM gets
        # an isolated copy of the image as the test's MTD.
        shutil.copyfile(a.flash,root/"factory-flash.bin")
        socket=root/"qmp.sock"
        machine="esp32s3,ybx-x4pro-i2c=on,ybx-x4pro-gt911=on," \
                "ybx-x4pro-sd-power=on,ybx-x4pro-spi2=on"
        if a.epd:
            machine+=",ybx-x4pro-epd=on"
        cmd=[
            str(a.qemu_bin),
            "-machine",machine,
            "-m","8M","-global","ssi_psram.is_octal=on",
            "-nographic","-monitor","none",
            "-serial","null","-serial","null","-serial","null","-serial","stdio",
            "-qmp",f"unix:{socket},server=on,wait=off",
            "-L",str(bios),
            "-drive",f"file={root/'factory-flash.bin'},if=mtd,format=raw",
            "-drive",f"file={root/'test-sd.img'},if=sd,format=raw",
        ]
        sd_trace=root/"sd-commands.log"
        if a.trace_sd:
            events=root/"sd-trace-events"
            events.write_text("sdbus_command\nsdcard_read_block\nsdcard_write_block\n")
            cmd+=["-trace",f"events={events},file={sd_trace}"]
        with (root/"qemu.out").open("w") as stdout, (root/"qemu.err").open("w") as stderr:
            process=subprocess.Popen(cmd,stdin=subprocess.DEVNULL,
                                     stdout=stdout,stderr=stderr)
            client=None
            try:
                client,reader,qmp=qmp_connect(socket)
                time.sleep(a.seconds)
                result={}
                for property in ("bytes-sent","transactions","last-byte"):
                    value=qmp("qom-get",{"path":"/machine/soc/spi2",
                                         "property":property})
                    if "return" not in value:raise AssertionError(value)
                    result[property]=value["return"]
                epd={}
                if a.epd:
                    for prop in ("frames","commands","ram-writes",
                                  "busy","nonwhite","nonwhite-ram","nonwhite-ram-writes",
                                 "cmd24","cmd26","update-ctrl2"):
                        value=qmp("qom-get",{"path":"/machine/soc",
                                             "property":"ybx-epd-"+prop})
                        if "return" not in value:raise AssertionError(value)
                        epd[prop]=value["return"]
                    if epd["frames"] > 0:
                        raw=qmp("qom-get",{"path":"/machine/soc",
                                            "property":"ybx-epd-visible-hex"})
                        if "return" not in raw:raise AssertionError(raw)
                        from probe_x4pro_epd_qtest import save_bmp, SIZE
                        frame=bytes.fromhex(raw["return"])
                        assert len(frame)==SIZE
                        epd["visible-frame-sha256"]=hashlib.sha256(frame).hexdigest()
                        save_bmp(a.out.with_suffix(".bmp"),frame)
                        epd["raw_panel_bmp"]=str(a.out.with_suffix(".bmp"))
                        if epd.get("nonwhite",0)>0 and epd.get("ram-writes",0)>0:
                            from epd_frame_io import (
                                ssd1677_landscape_to_portrait, save_mono_png)
                            portrait=ssd1677_landscape_to_portrait(frame)
                            output_png=a.out.with_suffix(".png")
                            save_mono_png(output_png,portrait,480,800)
                            epd["portrait_png"]=str(output_png)
                            epd["portrait_sha256"]=hashlib.sha256(portrait).hexdigest()
            finally:
                if client:client.close()
                process.terminate()
                try:process.communicate(timeout=3)
                except subprocess.TimeoutExpired:process.kill();process.communicate()
        uart=(root/"qemu.out").read_text(errors="replace")
        err=(root/"qemu.err").read_text(errors="replace")
        sd_trace_counts={"commands":0,"sector_reads":0,"sector_writes":0}
        if a.trace_sd and sd_trace.is_file():
            trace=sd_trace.read_text(errors="replace")
            sd_trace_counts["commands"]=len(re.findall(
                r"(?m)^sdbus_command ",trace))
            sd_trace_counts["sector_reads"]=len(re.findall(
                r"(?m)^sdcard_read_block ",trace))
            sd_trace_counts["sector_writes"]=len(re.findall(
                r"(?m)^sdcard_write_block ",trace))
            shutil.copyfile(sd_trace,a.out.with_suffix(".sdtrace.log"))
        sd_fat={}
        if a.original_fat:
            try:
                from analyze_guest_fat_image import analyze
                sd_fat=analyze(a.original_fat,root/"test-sd.img",
                               a.out.with_name(a.out.stem+"-sd-screenshot.bmp"))
            except (AssertionError,OSError,ValueError) as exc:
                sd_fat={"guest_sd_fat_file_creation_verified":False,
                        "guest_sd_error":str(exc)}
    report={
        "qemu_binary_sha256":hashlib.sha256(a.qemu_bin.read_bytes()).hexdigest(),
        "factory_flash_sha256":hashlib.sha256(a.flash.read_bytes()).hexdigest(),
        "executed_seconds":a.seconds,
        "spi2_pio_counters":result,
        "epd_model_attached":a.epd,
        "epd_ram_state":epd,
        "guest_fat_same_run":sd_fat,
        "guest_sdmmc_trace":sd_trace_counts,
        "guest_simultaneous_sdmmc_reads_and_epd_pixels_verified":bool(
            a.trace_sd and sd_trace_counts["sector_reads"]>0 and
            a.epd and epd.get("frames",0)>0 and epd.get("nonwhite",0)>0),
        "guest_simultaneous_sd_and_epd_verified":bool(
            a.epd and epd.get("frames",0)>0 and
            epd.get("nonwhite",0)>0 and epd.get("portrait_png") and
            sd_fat.get("guest_sd_fat_file_creation_verified",False)
            and sd_fat.get("guest_written_fat_structure_verified",False)),
        "guest_epd_refresh_observed":bool(a.epd and epd.get("frames",0)>0),
        "guest_epd_ram_write_observed":bool(a.epd and epd.get("ram-writes",0)>0),
        "guest_nonblank_epd_frame_verified":bool(
            a.epd and epd.get("frames",0)>0 and
            epd.get("ram-writes",0)>0 and epd.get("nonwhite",0)>0
            and epd.get("portrait_png")),
        "factory_application_uart_seen":"Preferences.cpp" in uart,
        "rom_boot_seen":"ESP-ROM:" in uart,
        "qemu_host_error_excerpt":err[-1500:],
        "qemu_uart_excerpt":uart[-1000:],
        "guest_spi2_pio_activity_detected":result["transactions"]>0,
        "dma_gdma_emulated":False,
        "epd_controller_verified":False,
        "physical_screen_pixels_verified":False
    }
    a.out.parent.mkdir(parents=True,exist_ok=True)
    a.out.write_text(json.dumps(report,indent=2)+"\n")
    print(json.dumps(report,indent=2))

if __name__=="__main__": main()
