import importlib.util
from pathlib import Path
import tempfile
import sys
import unittest

spec=importlib.util.spec_from_file_location("epd",Path(__file__).with_name("epd_protocol.py"))
epd=importlib.util.module_from_spec(spec)
sys.modules[spec.name]=epd
spec.loader.exec_module(epd)

def issue(panel, cmd, payload=b""):
    panel.command_byte(cmd)
    panel.data_bytes(payload)

def setup_ssd(panel):
    w,h=panel.config.width,panel.config.height
    issue(panel,0x11,b"\x01")
    issue(panel,0x44,(0).to_bytes(2,"little")+(w-1).to_bytes(2,"little"))
    issue(panel,0x45,(h-1).to_bytes(2,"little")+(0).to_bytes(2,"little"))
    issue(panel,0x4e,(0).to_bytes(2,"little"))
    issue(panel,0x4f,(h-1).to_bytes(2,"little"))

class EpdTests(unittest.TestCase):
    def test_x4_real_controller_geometry_and_refresh(self):
        c=epd.EpdPanel(epd.CONFIGS["x4"])
        setup_ssd(c)
        payload=bytes([0xff]*100+[0]*100+[0xff]*100*478)
        issue(c,0x24,payload)
        self.assertEqual(c.refreshes,0)
        self.assertEqual(c.pixel(0,1),1)   # not visible until activation
        issue(c,0x22,b"\xf7")
        issue(c,0x20)
        self.assertEqual(c.refreshes,1)
        self.assertEqual(c._update_sequence,0xf7)
        self.assertEqual(c.pixel(0,0),1)
        self.assertEqual(c.pixel(0,1),0)
        self.assertEqual(c.pixel(799,479),1)

    def test_x4_partial_window_no_global_overwrite(self):
        c=epd.EpdPanel(epd.CONFIGS["x4"])
        setup_ssd(c)
        issue(c,0x24,b"\x00"*c.plane_len)
        issue(c,0x20)
        issue(c,0x44,(16).to_bytes(2,"little")+(31).to_bytes(2,"little"))
        issue(c,0x45,(4).to_bytes(2,"little")+(3).to_bytes(2,"little"))
        issue(c,0x4e,(16).to_bytes(2,"little"))
        issue(c,0x4f,(4).to_bytes(2,"little"))
        issue(c,0x24,b"\xff"*4)
        issue(c,0x20)
        # Panel physical gate 4 maps to visible source row height-1-4.
        self.assertEqual(c.pixel(16,475),1)
        self.assertEqual(c.pixel(31,476),1)
        self.assertEqual(c.pixel(15,475),0)
        self.assertEqual(c.pixel(32,475),0)

    def test_partial_out_of_bounds_and_invalid_window(self):
        c=epd.EpdPanel(epd.CONFIGS["x4"])
        setup_ssd(c)
        with self.assertRaisesRegex(ValueError,"beyond selected"):
            issue(c,0x24,b"\xff"*(c.plane_len+100))
        c=epd.EpdPanel(epd.CONFIGS["x4"])
        issue(c,0x44,(0).to_bytes(2,"little")+(1000).to_bytes(2,"little"))
        with self.assertRaisesRegex(ValueError,"range invalid"):
            c.flush()

    def test_uc8253_x3_ram_then_refresh(self):
        c=epd.EpdPanel(epd.CONFIGS["x3"])
        self.assertEqual((c.config.width,c.config.height),(792,528))
        b=bytearray(b"\xff"*c.plane_len)
        b[0]=0x7f
        issue(c,0x13,b)
        self.assertEqual(c.pixel(0,0),1)
        issue(c,0x12)
        self.assertEqual(c.refreshes,1)
        self.assertEqual(c.pixel(0,527),0)
        self.assertEqual(c.pixel(1,527),1)
        self.assertEqual(c.pixel(0,0),1)

    def test_uc8179_and_uc8279_new_plane(self):
        for kind in ("uc8179","uc8279"):
            c=epd.EpdPanel(epd.PanelConfig(kind,800,480))
            issue(c,0x10,b"\x00"*c.plane_len)
            issue(c,0x13,b"\xff"*c.plane_len)
            issue(c,0x12)
            self.assertEqual(c.pixel(0,0),1)

    def test_trace_replay_and_truncation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/"trace.yepd"
            entries=[(0x13,b"\xff"*epd.CONFIGS["x3"].plane_size),(0x12,b"")]
            epd.write_trace(path,entries)
            self.assertEqual(list(epd.read_trace(path)),entries)
            c=epd.replay(epd.EpdPanel(epd.CONFIGS["x3"]),epd.read_trace(path))
            self.assertEqual(c.refreshes,1)
            self.assertEqual(c.pixel(10,10),1)
            path.write_bytes(path.read_bytes()[:-1])
            with self.assertRaisesRegex(ValueError,"truncated"):
                list(epd.read_trace(path))

    def test_pbm_black_white_and_guard(self):
        with tempfile.TemporaryDirectory() as tmp:
            c=epd.EpdPanel(epd.PanelConfig("ssd1677",8,2))
            setup_ssd(c)
            issue(c,0x24,bytes([0xfe,0xff]))
            issue(c,0x20)
            path=Path(tmp)/"f.pbm"
            c.save_pbm(path)
            self.assertEqual(path.read_bytes(),b"P4\n8 2\n"+bytes([0x01,0x00]))
            with self.assertRaisesRegex(ValueError,"pixel out of bounds"):
                c.pixel(8,0)

if __name__=="__main__":
    unittest.main()
