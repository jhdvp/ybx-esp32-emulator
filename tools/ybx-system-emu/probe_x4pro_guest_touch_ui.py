#!/usr/bin/env python3
"""Live original X4 Pro guest GT911 contact -> actual SSD1677 framebuffer.

No QTest CPU, no UI substitutions. Drive synthetic GT911 coordinate/state
through existing QMP sensor properties, read actual SSD1677 RAM via QMP.
Optional --rtos-diagnostics collects factory SHA-gated FreeRTOS evidence
around the native GT911 input without replacing framebuffer acceptance.
"""
import argparse
import hashlib
import json
import re
import shutil
import subprocess
import time
from pathlib import Path
from tempfile import TemporaryDirectory

from probe_x4pro_touch_injection_qtest import qmp_connect
from epd_frame_io import save_mono_png, ssd1677_landscape_to_portrait
from probe_x4pro_epd_qtest import SIZE
import freertos_guest_probe as rtos


def sensor(qmp, prop, value):
    r=qmp("qom-set",{"path":"/machine/soc","property":"ybx-touch-"+prop,
                     "value":value})
    if "return" not in r:
        return {"ok":False,"error":r.get("error",r)}
    return {"ok":True}


def get(qmp, prop):
    r=qmp("qom-get",{"path":"/machine/soc","property":"ybx-epd-"+prop})
    if "return" not in r:
        raise AssertionError((prop,r))
    return r["return"]


def cpu_sample(qmp):
    fast=qmp("query-cpus-fast")
    hmp=qmp("human-monitor-command",{"command-line":"info registers -a"})
    raw=hmp.get("return",hmp)
    pcs=[]
    if isinstance(raw,str):
        for part in re.split(r"\bCPU#(?=\d)",raw)[1:]:
            index=part.splitlines()[0].strip()
            regs={}
            for field in ("PC","CCOUNT","INTERRUPT","INTENABLE",
                          "CCOMPARE0","CCOMPARE1","CCOMPARE2",
                          "EPC1","EXCCAUSE","PS"):
                match=re.search(r"\b"+field+r"=([0-9a-fA-F]+)",part)
                if match: regs[field]="0x"+match.group(1)
            pcs.append({"cpu":int(index),**regs})
    gpio=qmp("qom-get",{"path":"/machine/soc/gpio",
                        "property":"ybx-gpio-irq-level"})
    gpio_status={}
    for key,addr in (("status-lo",0x60004044),("status-hi",0x60004050),
                     ("pin6-config",0x6000408c),("input-lo",0x6000403c),
                     ("enable-lo",0x60004020)):
        r=qmp("human-monitor-command",
              {"command-line":f"xp /1wx 0x{addr:x}"})
        gpio_status[key]=r.get("return",r)
    timers={}
    for n in range(3):
        timers[str(n)]={}
        for kind in ("fires","reprograms"):
            r=qmp("qom-get",{"path":"/machine/soc/systimer",
                         "property":f"ybx-comparator-{n}-{kind}"})
            timers[str(n)][kind]=r.get("return",r)
    return {
      "systimer_comparators":timers,
      "qmp_cpu_count":len(fast.get("return",[])),
      "registers_all":pcs,
      "hmp_error":raw if not isinstance(raw,str) else None,
      "gpio_irq_level":gpio.get("return",gpio),
      "gpio_status_mmio":gpio_status,
    }


def x4pro_raw_to_portrait(raw_x:int,raw_y:int)->tuple[int,int]:
    """FreeInk X4 Pro: swapXY, flipY, then GfxRenderer::Portrait.

    The two rotations cancel in logical portrait coordinates.
    This mapping is backed by BoardConfig.h X4Pro and GfxRenderer.cpp.
    """
    if not (0<=raw_x<=479 and 0<=raw_y<=799):
        raise ValueError("raw GT911 X4 Pro coordinate outside sensor bounds")
    native_x=raw_y
    native_y=479-raw_x
    return 479-native_y,native_x


# Matching X4 Pro factory flash; addresses below are not generic.
FACTORY_0025_FLASH_SHA256 = (
    "206ed9661bd71477d1a54397c5a2cfe490179a3f59ab094f1e7421745c609523"
)

X4PRO_MACHINE = ("esp32s3,ybx-x4pro-i2c=on,ybx-x4pro-gt911=on,"
                 "ybx-x4pro-sd-power=on,ybx-x4pro-spi2=on,ybx-x4pro-epd=on")


def qemu_command(qemu_bin, work, socket, bios, gdb_port=0, gdb_start_paused=False):
    """Original-factory guest argv. `work` must hold factory-flash.bin, sd.img
    and a rom/ directory with esp32s3_rev0_rom.bin."""
    cmd=[str(qemu_bin),"-machine",X4PRO_MACHINE,
         "-m","8M","-global","ssi_psram.is_octal=on",
         "-nographic","-monitor","none",
         "-serial","null","-serial","null","-serial","null",
         "-serial","stdio",
         "-qmp",f"unix:{socket},server=on,wait=off",
         "-L",str(bios),
         "-drive",f"file={Path(work)/'factory-flash.bin'},if=mtd,format=raw",
         "-drive",f"file={Path(work)/'sd.img'},if=sd,format=raw"]
    if gdb_port:
        if not 1024<=gdb_port<=65535:
            raise ValueError("GDB port must be 1024..65535")
        cmd+=["-gdb",f"tcp:0.0.0.0:{gdb_port}"]
    if gdb_start_paused:
        if not gdb_port:raise ValueError("--gdb-start-paused requires --gdb-port")
        cmd+=["-S"]
    return cmd


def read_guest_epd_semaphore(qmp):
    """Read-only runtime EpdBus ISR semaphore and GPIO6 regs (exact ELF)."""
    import re
    def read32(address):
        r=qmp("human-monitor-command",
              {"command-line":f"xp /1wx 0x{address:08x}"})
        value=r.get("return",r)
        if not isinstance(value,str):
            return None
        match=re.search(r":\s*0x([0-9a-fA-F]+)",value)
        return int(match.group(1),16) if match else None
    result={
        "sem_handle_ptr":read32(0x3fca1da8),
        "gpio6_pin_cfg":read32(0x6000408c),
        "gpio6_input":read32(0x6000403c),
        "gpio_status":read32(0x60004044),
        "sem_pointer_symbol":"freeink::s_epdRefreshDone",
    }
    for prop in ("ybx-gpio6-edges","ybx-gpio-irq-acks","ybx-gpio-irq-level"):
        r=qmp("qom-get",{"path":"/machine/soc/gpio","property":prop})
        result[prop]=r.get("return")
    # Only claim the GPIO6 route if actual guest panel and both
    # IRQ edge/status ACK counters have advanced.
    result["isr_delivery_verified"]=bool(
        isinstance(result["ybx-gpio6-edges"],int)
        and isinstance(result["ybx-gpio-irq-acks"],int)
        and result["ybx-gpio6-edges"]>=2
        and result["ybx-gpio-irq-acks"]>=2)
    return result


def touch_metrics(qmp):
    metrics={}
    for prop in ("status-reads","point-reads","status-clears","injected-frames"):
        r=qmp("qom-get",{"path":"/machine/soc","property":"ybx-touch-"+prop})
        assert "return" in r, (prop,r)
        metrics[prop]=r["return"]
    return metrics


def sample(qmp, name, out):
    state={key:get(qmp,key) for key in
           ("frames","nonwhite","ram-writes","commands","busy","last-command")
           if key != "last-command"} # last-command not exposed through SoC alias
    if state["frames"]:
        r=qmp("qom-get",{"path":"/machine/soc",
                         "property":"ybx-epd-visible-hex"})
        if "return" not in r:
            raise AssertionError(r)
        frame=bytes.fromhex(r["return"])
        assert len(frame)==SIZE
        state["frame_sha256"]=hashlib.sha256(frame).hexdigest()
        if state["nonwhite"]>0:
            png=out.with_name(out.stem+"-"+name+".png")
            save_mono_png(png,ssd1677_landscape_to_portrait(frame),480,800)
            state["png"]=str(png)
    return state


def assess_touch_to_ui(before,after,before_touch,after_touch,injections):
    """Require mature home UI, guest I2C press AND release, and new EPD pixels.

    Do not accept splash-to-home progression or QMP-only injection as proof.
    """
    return bool(
        before.get("frames",0)>=2 and before.get("nonwhite",0)>=4000
        and before.get("frame_sha256")
        and after.get("frame_sha256")
        and before["frame_sha256"]!=after["frame_sha256"]
        and after["frames"]>before["frames"]
        and after_touch["point-reads"]>before_touch["point-reads"]
        and after_touch["status-clears"]>=before_touch["status-clears"]+2
        and all(item.get("ok") for item in injections.values())
    )


def assess_navigation_claim(touch_gate, wake):
    """Panel changes are mandatory, and measurable blocked tasks veto a claim."""
    if not touch_gate:
        return False
    if wake.get("available") and not wake.get("render_task_woken"):
        return False
    return True


def rtos_snapshot(qmp, enabled, task_name):
    if not enabled:
        return {"available": False,
                "reason": "not requested; use --rtos-diagnostics"}
    return rtos.snapshot(qmp, task_name)


def run(a):
    factory_build=(hashlib.sha256(a.flash.read_bytes()).hexdigest() ==
                   rtos.FACTORY_092C2BE5_FLASH_SHA256)
    if getattr(a, "rtos_diagnostics", False) and not factory_build:
        raise ValueError("--rtos-diagnostics requires matching original factory flash")
    with TemporaryDirectory(prefix="ybx-gt911-guest-ui-") as tmp:
        t=Path(tmp)
        bios=t/"rom";bios.mkdir()
        shutil.copyfile(a.rom,bios/"esp32s3_rev0_rom.bin")
        shutil.copyfile(a.flash,t/"factory-flash.bin")
        shutil.copyfile(a.sd_image,t/"sd.img")
        socket=t/"qmp.sock"
        cmd=qemu_command(a.qemu_bin,t,socket,bios,gdb_port=a.gdb_port,
                         gdb_start_paused=a.gdb_start_paused)
        with (t/"out.log").open("w") as stdout,(t/"err.log").open("w") as stderr:
            process=subprocess.Popen(cmd,stdin=subprocess.DEVNULL,stdout=stdout,
                                     stderr=stderr)
            client=None
            try:
                client,reader,qmp=qmp_connect(socket)
                time.sleep(a.boot_seconds)
                before=sample(qmp,"before",a.out)
                before_touch=touch_metrics(qmp)
                epd_irq_before=(read_guest_epd_semaphore(qmp)
                                if a.epd_sem_diagnostics else {})
                cpu_before=cpu_sample(qmp)
                rtos_before=rtos_snapshot(qmp,getattr(a,"rtos_diagnostics",False),
                                          getattr(a,"render_task_name","ActivityManagerRender"))
                if a.debug_hold_seconds:
                    print("GDB_READY_FOR_ATTACH",flush=True)
                    time.sleep(a.debug_hold_seconds)
                # A splash-to-home transition after injection is NOT an input
                # response. Require finished home (>=2 SSD1677 frames).
                baseline_home_ready=(before["frames"]>=2
                                     and before["nonwhite"]>=4000)
                injections={}
                # X4 Pro touch swap/flip plus renderer portrait cancel:
                # raw (350,760) targets More, (130,760) targets Library.
                injections["set-x"]=sensor(qmp,"x",a.raw_x)
                injections["set-y"]=sensor(qmp,"y",a.raw_y)
                injections["press"]=sensor(qmp,"state",1)
                time.sleep(a.press_seconds)
                during=sample(qmp,"during",a.out)
                during_touch=touch_metrics(qmp)
                cpu_during=cpu_sample(qmp)
                injections["release"]=sensor(qmp,"state",0)
                time.sleep(a.after_seconds)
                after=sample(qmp,"after",a.out)
                after_touch=touch_metrics(qmp)
                epd_irq_after=(read_guest_epd_semaphore(qmp)
                               if a.epd_sem_diagnostics else {})
                cpu_after=cpu_sample(qmp)
                rtos_after=rtos_snapshot(qmp,getattr(a,"rtos_diagnostics",False),
                                         getattr(a,"render_task_name","ActivityManagerRender"))
                late=None
                rtos_late=None
                if getattr(a,"rtos_late_seconds",0):
                    time.sleep(a.rtos_late_seconds)
                    late=sample(qmp,"late",a.out)
                    rtos_late=rtos_snapshot(qmp,getattr(a,"rtos_diagnostics",False),
                                            getattr(a,"render_task_name","ActivityManagerRender"))
                # A point can be injected while firmware unavailable; compare
                # the actual SSD1677 image and refresh counts, NOT QMP success.
            finally:
                if client:client.close()
                process.terminate()
                try:process.communicate(timeout=3)
                except subprocess.TimeoutExpired:process.kill();process.communicate()
        guest_uart=(t/"out.log").read_text(errors="replace")
        host_error=(t/"err.log").read_text(errors="replace")
    changed=(before.get("frame_sha256") and after.get("frame_sha256") and
             before["frame_sha256"]!=after["frame_sha256"])
    wake=rtos.assess_render_wake(rtos_before,rtos_after)
    touch_gate=assess_touch_to_ui(before,after,before_touch,after_touch,injections)
    wake_late=(rtos.assess_render_wake(rtos_before,rtos_late)
               if rtos_late is not None else None)
    late_gate=(assess_touch_to_ui(before,late,before_touch,after_touch,injections)
               if late is not None else False)
    result={
        "factory_flash_sha256":hashlib.sha256(a.flash.read_bytes()).hexdigest(),
        "qemu_sha256":hashlib.sha256(a.qemu_bin.read_bytes()).hexdigest(),
        "configuration":{
            "boot_seconds":a.boot_seconds,"press_seconds":a.press_seconds,
            "after_seconds":a.after_seconds,"raw_gt911_xy":[a.raw_x,a.raw_y],
            "nominal_ui_target":(
                "More button" if (a.raw_x,a.raw_y)==(350,760)
                else "Library button" if (a.raw_x,a.raw_y)==(130,760)
                else "custom"),
            "expected_portrait_xy":x4pro_raw_to_portrait(a.raw_x,a.raw_y)},
        "before":before,"injection_results":injections,
        "during":during,"after":after,"late":late,
        "rtos_before":rtos_before,
        "rtos_after":rtos_after,
        "rtos_late":rtos_late,
        "rtos_render_wake_assessment":wake,
        "rtos_render_wake_assessment_late":wake_late,
        "factory_build_sha_gate_matched":factory_build,
        "home_ready_before_injection":baseline_home_ready,
        "cpu_before":cpu_before,"cpu_during":cpu_during,"cpu_after":cpu_after,
        "epd_irq_before":epd_irq_before,
        "epd_irq_after":epd_irq_after,
        "factory_epd_semaphore_matches_blocked_queue":bool(
            a.epd_sem_diagnostics and
            epd_irq_after.get("sem_handle_ptr")==0x3fcec294),
        "touch_metrics_before":before_touch,
        "touch_metrics_during":during_touch,
        "touch_metrics_after":after_touch,
        "guest_gt911_point_read_after_injection":
            after_touch["point-reads"]>before_touch["point-reads"],
        "guest_gt911_cleared_after_injection":
            after_touch["status-clears"]>=before_touch["status-clears"]+2,
        "gt911_injection_qmp_accepted":all(i["ok"] for i in injections.values()),
        "guest_epd_frame_sha_changed_after_contact":bool(changed),
        "guest_epd_frame_count_increased":after["frames"]>before["frames"],
        "true_guest_touch_to_ui_verified":touch_gate,
        "guest_render_task_woken_after_contact":bool(wake["render_task_woken"]),
        "guest_ui_navigation_claim_allowed":assess_navigation_claim(touch_gate,wake),
        "guest_ui_navigation_claim_allowed_late":(
            assess_navigation_claim(late_gate,wake_late)
            if wake_late is not None else None),
        "kernel_counters_are_not_screen_proof":True,
        "guest_rom_uart": "ESP-ROM:" in guest_uart,
        "guest_app_uart": "Preferences.cpp" in guest_uart,
        "uart_tail":guest_uart[-900:],
        "host_stderr_tail":host_error[-1100:],
        "physical_touch_verified":False
    }
    a.out.parent.mkdir(parents=True,exist_ok=True)
    a.out.write_text(json.dumps(result,indent=2)+"\n")
    print(json.dumps(result,indent=2),flush=True)
    return result


def main():
    p=argparse.ArgumentParser(description=__doc__)
    for key in ("qemu-bin","rom","flash","sd-image","out"):
        p.add_argument("--"+key,type=Path,required=True)
    p.add_argument("--boot-seconds",type=float,default=37)
    p.add_argument("--press-seconds",type=float,default=0.8)
    p.add_argument("--after-seconds",type=float,default=14)
    p.add_argument("--gdb-port",type=int,default=0,
                   help="test-only GDB stub port; publish localhost via Docker -p")
    p.add_argument("--gdb-start-paused",action="store_true",
                   help="start guest halted, debugger must resume CPU (test only)")
    p.add_argument("--debug-hold-seconds",type=float,default=0,
                   help="guest continued before injection for external debugger attach")
    p.add_argument("--epd-sem-diagnostics",action="store_true",
                   help="read-only EpdBus ISR semaphore & GPIO6, factory 092c2be5")
    p.add_argument("--rtos-diagnostics",action="store_true",
                   help="read-only FreeRTOS TCB/BUSY-semaphore decode, exact factory SHA")
    p.add_argument("--render-task-name",default="ActivityManagerRender")
    p.add_argument("--rtos-late-seconds",type=float,default=0)
    p.add_argument("--raw-x",type=int,default=130)
    p.add_argument("--raw-y",type=int,default=760)
    a=p.parse_args()
    if a.epd_sem_diagnostics:
        sha=hashlib.sha256(a.flash.read_bytes()).hexdigest()
        if sha!=FACTORY_0025_FLASH_SHA256:
            p.error("--epd-sem-diagnostics requires matching original factory flash")
    run(a)


if __name__=="__main__":main()
