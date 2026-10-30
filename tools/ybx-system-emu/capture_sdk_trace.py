#!/usr/bin/env python3
"""Generate an SSD1677 command/data trace by executing the REAL SDK C++ driver.

No firmware, SDK or native emulator files are modified. Build/test all copied
source under an isolated temporary directory and export only a trace.
"""
from pathlib import Path
import shutil
import subprocess
import tempfile
import argparse
import sys

HERE=Path(__file__).resolve().parent
SDK=HERE.parents[1]/"freeink-sdk/libs/display/FreeInkDisplay"

def capture(out:Path,controller:str="ssd1677") -> None:
    with tempfile.TemporaryDirectory(prefix="ybx-real-epd-") as work:
        root=Path(work)
        for folder in ("src/driver","src/lut","src/bus","include"):
            (root/folder).mkdir(parents=True,exist_ok=True)
        sources={
            "src/driver/Ssd1677Driver.cpp",
            "src/driver/Ssd1677Driver.h",
            "src/driver/Uc8253X3Driver.cpp",
            "src/driver/Uc8253X3Driver.h",
            "src/driver/PanelDriver.h",
            "src/lut/Ssd1677Luts.h",
            "src/lut/Uc8253X3Luts.h",
            "src/lut/UltraChipDirectGrayLuts.h",
            "include/GrayscaleCapabilities.h",
        }
        for relative in sources:
            shutil.copy2(SDK/relative,root/relative)
        for name in ("Arduino.h","BoardConfig.h","SPI.h","esp_heap_caps.h","sdkconfig.h"):
            shutil.copy2(SDK/"test/host/pro_stubs"/name,root/name)
        stub=root/"src/bus/EpdBus.h"
        shutil.copy2(SDK/"test/host/pro_stubs/EpdBus.h",stub)
        stubtxt=stub.read_text()
        needle="  void fillPlane(uint8_t c, uint8_t v, uint16_t h, uint16_t wb) {"
        assert stubtxt.count(needle)==1
        stubtxt=stubtxt.replace(needle,"""  // Model the real EpdBus::sendPlaneFlipped bus stream, not a
  // normal-order byte copy (X3 has reversed panel gate ordering).
  void sendPlaneFlipped(uint8_t c,const uint8_t* p,uint16_t h,uint16_t wb) {
    cmd(c);
    auto txn=beginTxn();
    for(int y=int(h)-1;y>=0;y--)
      txn.writeBytes(p+size_t(y)*wb,wb);
  }
  """+needle)
        stub.write_text(stubtxt)
        exe=root/"export"
        command=["clang++","-std=c++17","-O1","-Wall","-Wextra","-Wno-unused-parameter",
                 "-DARDUINO=1","-DBOARD_HAS_PSRAM=1","-DFREEINK_DEVICE_METALIO_EINK4=1",
                 "-I"+str(root),"-I"+str(root/"include"),
                 str(HERE/"export_epd_trace.cpp"),
                 str(root/"src/driver/Ssd1677Driver.cpp"),
                 str(root/"src/driver/Uc8253X3Driver.cpp"),
                 "-o",str(exe)]
        subprocess.run(command,check=True,cwd=root)
        subprocess.run([str(exe),str(out.resolve()),controller],check=True,cwd=root)
    print(f"FreeInk {controller} real driver trace: {out} ({out.stat().st_size} bytes)")

if __name__=="__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("--out",type=Path,required=True)
    parser.add_argument("--controller",choices=["ssd1677","uc8253"],default="ssd1677")
    args=parser.parse_args()
    args.out.parent.mkdir(parents=True,exist_ok=True)
    capture(args.out,args.controller)
