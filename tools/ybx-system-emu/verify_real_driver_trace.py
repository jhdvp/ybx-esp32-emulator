#!/usr/bin/env python3
"""Integration verification: real FreeInk C++ bus traces → virtual e-paper.

This deliberately exercises the real SDK driver, not ESP32 machine code.
For board-level CPU-to-SPI tests see unimplemented QEMU glue requirement.
"""
from pathlib import Path
import argparse
import json
from epd_protocol import CONFIGS, EpdPanel, read_trace, replay
from capture_sdk_trace import capture

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--out",type=Path,default=Path("/tmp/ybx-epd-real-driver-qa"))
    args=ap.parse_args()
    out=args.out
    out.mkdir(parents=True,exist_ok=True)
    report=[]
    for model,controller in (("x3","uc8253"),("x4","ssd1677")):
        trace=out/(controller+".yepd")
        capture(trace,controller)
        frames=list(read_trace(trace))
        panel=replay(EpdPanel(CONFIGS[model]),frames)
        panel.save_pbm(out/(model+".pbm"))
        expected=(0,0,0,1,1)
        actual=tuple(panel.pixel(x,y) for x,y in ((0,0),(7,0),(0,1),(1,1),(0,2)))
        assert actual==expected,(model,actual)
        assert panel.refreshes>=1,(model,panel.refreshes)
        # A production RAM update must touch new pixels; a no-op test doesn't
        # demonstrate the raster transport.
        assert any(cmd in (0x24,0x13) and len(data)==panel.plane_len for cmd,data in frames)
        report.append(dict(model=model,controller=controller,
                           width=panel.config.width,height=panel.config.height,
                           events=len(frames),refreshes=panel.refreshes,
                           expected_markers=expected,got_markers=actual,
                           trace_bytes=trace.stat().st_size))
    (out/"report.json").write_text(json.dumps(report,indent=2,ensure_ascii=False)+"\n")
    for row in report:
        print(f"PASS {row['model']} {row['controller']} "
              f"{row['width']}x{row['height']}, "
              f"{row['events']} SDK transactions, "
              f"{row['refreshes']} refreshes")
    print("Real SDK trace integration: 2/2 PASS")

if __name__=="__main__":
    main()
