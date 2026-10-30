"""Strict GT911 -> guest firmware -> SSD1677 acceptance criteria."""
import argparse
import hashlib
import tempfile
import unittest
from pathlib import Path
from probe_x4pro_guest_touch_ui import (
    assess_navigation_claim, assess_touch_to_ui, rtos_snapshot, run,
    x4pro_raw_to_portrait, read_guest_epd_semaphore)

class GT911GuestAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.home={"frames":2,"nonwhite":4743,"frame_sha256":"home"}
        self.next={"frames":3,"nonwhite":5321,"frame_sha256":"menu"}
        self.before={"point-reads":0,"status-clears":0}
        self.after={"point-reads":1,"status-clears":2}
        self.inject={"set-x":{"ok":True},"set-y":{"ok":True},
                     "press":{"ok":True},"release":{"ok":True}}
    def accept(self):
        return assess_touch_to_ui(self.home,self.next,
            self.before,self.after,self.inject)
    def test_all_gates_confirmed(self):
        self.assertTrue(self.accept())
    def test_splash_to_home_false_positive_rejected(self):
        self.home["frames"]=1
        self.home["nonwhite"]=1622
        self.assertFalse(self.accept())
    def test_qmp_success_without_guest_i2c_rejected(self):
        self.after["point-reads"]=0
        self.after["status-clears"]=0
        self.assertFalse(self.accept())
    def test_status_without_press_read_rejected(self):
        self.after["point-reads"]=0
        self.assertFalse(self.accept())
    def test_missing_release_ack_rejected(self):
        self.after["status-clears"]=1
        self.assertFalse(self.accept())
    def test_same_pixels_or_no_new_refresh_rejected(self):
        self.next["frame_sha256"]="home"
        self.assertFalse(self.accept())
        self.next["frame_sha256"]="menu"
        self.next["frames"]=2
        self.assertFalse(self.accept())
    def test_failed_qmp_injection_rejected(self):
        self.inject["release"]={"ok":False}
        self.assertFalse(self.accept())


class NavigationClaimGateTests(unittest.TestCase):
    """Kernel counters never prove a screen change, and pixels alone are not
    enough once the render task is measurable and still blocked."""

    def setUp(self):
        self.touch_passes = dict(
            before={"frames": 2, "nonwhite": 4743, "frame_sha256": "home"},
            after={"frames": 3, "nonwhite": 5321, "frame_sha256": "menu"},
            before_touch={"point-reads": 0, "status-clears": 0},
            after_touch={"point-reads": 1, "status-clears": 2},
            inject={"press": {"ok": True}, "release": {"ok": True}})
        self.woken = {"available": True, "render_task_woken": True}
        self.still_blocked = {"available": True, "render_task_woken": False}

    def gate(self):
        return assess_touch_to_ui(self.touch_passes["before"],
                                  self.touch_passes["after"],
                                  self.touch_passes["before_touch"],
                                  self.touch_passes["after_touch"],
                                  self.touch_passes["inject"])

    def test_pixels_plus_kernel_wake_allows_the_claim(self):
        self.assertTrue(self.gate())
        self.assertTrue(assess_navigation_claim(self.gate(), self.woken))

    def test_measurable_block_refutes_the_claim(self):
        self.assertFalse(assess_navigation_claim(self.gate(),
                                                self.still_blocked))

    def test_no_touch_pixels_never_allows_the_claim(self):
        self.assertFalse(assess_navigation_claim(False, self.woken))

    def test_unmeasurable_kernel_state_does_not_invent_a_refutation(self):
        unavailable = {"available": False, "render_task_woken": False,
                       "reason": "not requested"}
        self.assertTrue(assess_navigation_claim(self.gate(), unavailable))

    def test_snapshot_without_diagnostics_is_marked_not_requested(self):
        view = rtos_snapshot(None, False, "ActivityManagerRender")
        self.assertFalse(view["available"])
        self.assertIn("--rtos-diagnostics", view["reason"])

    def test_fixed_addresses_refuse_a_flash_that_is_not_the_factory_build(self):
        """The RAM layout is build-specific, so the SHA gate must stop early."""
        with tempfile.TemporaryDirectory() as tmp:
            flash = Path(tmp)/"other-build.bin"
            flash.write_bytes(b"not the 092c2be5 factory image")
            self.assertNotEqual(
                hashlib.sha256(flash.read_bytes()).hexdigest(),
                "206ed9661bd71477d1a54397c5a2cfe490179a3f59ab094f1e7421745c609523")
            args = argparse.Namespace(
                flash=flash, qemu_bin=Path(tmp)/"absent-qemu", rom=Path(tmp),
                sd_image=Path(tmp)/"sd.img", out=Path(tmp)/"report.json",
                rtos_diagnostics=True, render_task_name="ActivityManagerRender",
                rtos_late_seconds=0, gdb_port=0, debug_hold_seconds=0,
                boot_seconds=0, press_seconds=0, after_seconds=0,
                raw_x=350, raw_y=760)
            with self.assertRaises(ValueError):
                run(args)   # must fail before starting any emulator process


class GT911LogicalCoordinatesTests(unittest.TestCase):
    def test_x4pro_orientation_maps_raw_touch_to_logical_portrait(self):
        self.assertEqual(x4pro_raw_to_portrait(130,760),(130,760))
        self.assertEqual(x4pro_raw_to_portrait(350,760),(350,760))
        self.assertEqual(x4pro_raw_to_portrait(0,0),(0,0))
        self.assertEqual(x4pro_raw_to_portrait(479,799),(479,799))
    def test_reject_bad_touch_coordinates(self):
        with self.assertRaises(ValueError):
            x4pro_raw_to_portrait(480,750)


class FactoryEpdBusySemaphoreTests(unittest.TestCase):
    def test_qmp_read_only_factory_semaphore_and_pcpu_gpios(self):
        seen=[]
        values={0x3fca1da8:0x3fcec294, 0x6000408c:0x2180,
                0x6000403c:0x40, 0x60004044:0}
        def qmp(command,args):
            if command=="qom-get":
                return {"return":3 if args["property"]!="ybx-gpio-irq-level" else 0}
            self.assertEqual(command,"human-monitor-command")
            self.assertTrue(args["command-line"].startswith("xp /1wx 0x"))
            addr=int(args["command-line"].split()[-1],16)
            seen.append(addr)
            return {"return":f"0x{addr:08x}: 0x{values[addr]:08x}\n"}
        result=read_guest_epd_semaphore(qmp)
        self.assertEqual(result["sem_handle_ptr"],0x3fcec294)
        self.assertEqual(result["gpio6_pin_cfg"],0x2180)
        self.assertEqual(len(seen),4)
        self.assertTrue(result["isr_delivery_verified"])

    def test_unparseable_qmp_never_fabricates_semaphore(self):
        def qmp(command,args):
            return {"error":{"class":"GenericError","desc":"not available"}}
        state=read_guest_epd_semaphore(qmp)
        self.assertIsNone(state["sem_handle_ptr"])
        self.assertFalse(state["isr_delivery_verified"])
