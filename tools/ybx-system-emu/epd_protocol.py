"""Protocol model for Xteink e-paper controllers (from real FreeInk drivers).

Input is DC-separated SPI commands/data. This module does not magically
intercept an ESP CPU bus; it is a testable peripheral state machine to attach
to a QEMU SSI/GPIO backend. Only black/white plane and refresh visibility
are modelled. Analog e-paper physics, LUT timing and partial windows are not.
"""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
import struct
from typing import Iterator

MAGIC=b"YEPD1\x00"
MAX_TRACE_PAYLOAD=128*1024

@dataclass(frozen=True)
class PanelConfig:
    kind: str
    width: int
    height: int

    def __post_init__(self):
        if self.kind not in ("ssd1677","uc8253","uc8179","uc8279"):
            raise ValueError("unsupported controller")
        if self.width <= 0 or self.height <= 0 or self.width%8 or self.width*self.height>2_000_000:
            raise ValueError("invalid geometry")

    @property
    def plane_size(self)->int:
        return self.width*self.height//8

CONFIGS={
    "x3":PanelConfig("uc8253",792,528),
    "x4":PanelConfig("ssd1677",800,480),
    "x4pro":PanelConfig("ssd1677",800,480),
}

class EpdPanel:
    """State advances only when CS is active (caller groups command + data).

    SSD1677: BW RAM 0x24 and 0x26, update 0x22/0x20. On this family of
    Xteink boards, the framebuffer stores white=1, black=0 (MSB first).
    UC8253/UC8179/UC8279: DTM2 0x13 holds new plane; 0x12 refresh.

    A refresh latches the active RAM into the visible framebuffer.
    This is not a brightness/ghosting/LUT simulation.
    """
    def __init__(self, config: PanelConfig):
        self.config=config
        self.ram_bw=bytearray(b"\xff"*config.plane_size)
        self.ram_red=bytearray(b"\xff"*config.plane_size)
        self.visible=bytes(self.ram_bw)
        self.command=None
        self.payload=bytearray()
        self.refreshes=0
        self.commands=0
        self.errors=[]
        self.sleeping=False
        self._x_window=(0,config.width//8-1)
        self._y_window=(config.height-1,0)  # SSD panel gate scan is reversed
        self._x_counter=0
        self._y_counter=config.height-1
        self._x_step=1
        self._y_step=-1
        self._transfer_counter=0
        self._update_sequence=None

    @property
    def plane_len(self)->int:
        return self.config.plane_size

    def command_byte(self, command:int):
        if not 0<=command<=255:
            raise ValueError("invalid command")
        if self.command is not None:
            self._finalize()
        self.command=command
        self.payload.clear()
        self._transfer_counter=0
        self.commands+=1
        # A fresh full-frame transfer commonly starts with write RAM command.
        if self.config.kind=="ssd1677":
            if command in (0x24,0x26):
                self._x_counter=self._x_window[0]
                self._y_counter=self._y_window[0]
        if command==0x12 and self.config.kind!="ssd1677":
            self._refresh()
        if command==0x20 and self.config.kind=="ssd1677":
            self._refresh()
        if command==0x07 and self.config.kind!="ssd1677":
            self.sleeping=True

    def data_bytes(self,data:bytes):
        if self.command is None:
            raise ValueError("SPI data with no prior DC=command byte")
        if not isinstance(data,(bytes,bytearray,memoryview)):
            raise TypeError("SPI data must be bytes")
        if self.config.kind=="ssd1677" and self.command in (0x24,0x26):
            self._ram_write_ssd(bytes(data))
        elif self.config.kind!="ssd1677" and self.command in (0x10,0x13):
            self._ram_write_uc(bytes(data))
        else:
            if len(self.payload)+len(data)>MAX_TRACE_PAYLOAD:
                raise ValueError("register payload is unreasonably large")
            self.payload.extend(data)

    def _ram_write_uc(self,data:bytes):
        target=self.ram_red if self.command==0x13 else self.ram_bw
        end=self._transfer_counter+len(data)
        if end>self.plane_len:
            raise ValueError("UC RAM write beyond panel bounds")
        target[self._transfer_counter:end]=data
        self._transfer_counter=end

    def _ram_write_ssd(self,data:bytes):
        target=self.ram_bw if self.command==0x24 else self.ram_red
        start_x,end_x=self._x_window
        start_y,end_y=self._y_window
        row_bytes=self.config.width//8
        for byte in data:
            if not min(start_y,end_y)<=self._y_counter<=max(start_y,end_y):
                raise ValueError("SSD RAM write beyond selected Y window")
            if not min(start_x,end_x)<=self._x_counter<=max(start_x,end_x):
                raise ValueError("SSD RAM write beyond selected X window")
            target[self._y_counter*row_bytes+self._x_counter]=byte
            self._x_counter+=self._x_step
            if self._x_counter==end_x+self._x_step:
                self._x_counter=start_x
                self._y_counter+=self._y_step
        self._transfer_counter+=len(data)

    def _finalize(self):
        c=self.command
        b=bytes(self.payload)
        if self.config.kind=="ssd1677":
            if c==0x11 and b:
                # FreeInk SSD1677 uses 0x01 for X ascending/Y descending,
                # 0x00 for mirrored X descending/Y descending.
                self._x_step=1 if b[0]&1 else -1
                self._y_step=-1
            elif c==0x44 and len(b)>=4:
                # X4 driver supplies *pixel* X addresses (little-endian),
                # not the one-byte 0..99 ranges used by some SSD1677 libraries.
                x0=(b[0]|(b[1]<<8))//8
                x1=(b[2]|(b[3]<<8))//8
                if x0<0 or x1<0 or x0>=self.config.width//8 or x1>=self.config.width//8:
                    raise ValueError("SSD X range invalid")
                self._x_window=(x0,x1)
            elif c==0x45 and len(b)>=4:
                y0=b[0]|(b[1]<<8)
                y1=b[2]|(b[3]<<8)
                if y0>=self.config.height or y1>=self.config.height:
                    raise ValueError("SSD Y range invalid")
                self._y_window=(y0,y1)
                self._y_step=1 if y1>y0 else -1
            elif c==0x4e and len(b)>=2:
                x=(b[0]|(b[1]<<8))//8
                if not min(self._x_window)<=x<=max(self._x_window):
                    raise ValueError("SSD X counter invalid")
                self._x_counter=x

            elif c==0x4f and len(b)>=2:
                y=b[0]|(b[1]<<8)
                if not min(self._y_window)<=y<=max(self._y_window):
                    raise ValueError("SSD Y counter invalid")
                self._y_counter=y
            elif c==0x22 and b:
                self._update_sequence=b[0]
            elif c==0x10 and b:
                self.sleeping=True
            elif c==0x12:
                # software reset; display RAM persists in this simplified model
                self.sleeping=False
        else:
            if c==0x07 and b:
                self.sleeping=True
        self.command=None
        self.payload.clear()

    def _refresh(self):
        # UC full-screen source is DTM2 (new plane 0x13); SSD uses BW 0x24.
        if self.config.kind=="ssd1677":
            # FreeInk writes source row0 at physical gate height-1;
            # export upright panel pixels, not raw reversed gate order.
            rb=self.config.width//8
            self.visible=b"".join(
                self.ram_bw[y*rb:(y+1)*rb] for y in range(self.config.height-1,-1,-1)
            )
        else:
            # FreeInk sends UC8253/UC8179 planes with reversed rows
            # (EpdBus::sendPlaneFlipped), so map gate order back upright.
            rb=self.config.width//8
            self.visible=b"".join(
                self.ram_red[y*rb:(y+1)*rb] for y in range(self.config.height-1,-1,-1)
            )
        self.refreshes+=1
        self.sleeping=False

    def flush(self):
        if self.command is not None:
            self._finalize()

    def pixel(self,x:int,y:int)->int:
        if not 0<=x<self.config.width or not 0<=y<self.config.height:
            raise ValueError("pixel out of bounds")
        pos=y*(self.config.width//8)+x//8
        return 1 if self.visible[pos]&(0x80>>(x&7)) else 0

    def save_pbm(self,path:Path):
        """PBM P4: 1 is black; inverted because SDK RAM uses 1=white."""
        path=Path(path)
        inverted=bytes(b^0xff for b in self.visible)
        path.write_bytes(f"P4\n{self.config.width} {self.config.height}\n".encode()+inverted)


def write_trace(path:Path, items:list[tuple[int,bytes]]):
    """Record {command:uint8, payload_len:uint32_le, payload}; no implicit CS."""
    path=Path(path)
    with path.open("wb") as out:
        out.write(MAGIC)
        for cmd,data in items:
            if not 0<=cmd<=255 or len(data)>MAX_TRACE_PAYLOAD:
                raise ValueError("bad record")
            out.write(struct.pack("<BI",cmd,len(data)))
            out.write(data)


def read_trace(path:Path)->Iterator[tuple[int,bytes]]:
    with Path(path).open("rb") as inp:
        if inp.read(len(MAGIC))!=MAGIC:
            raise ValueError("not a YBX EPD trace")
        while True:
            head=inp.read(5)
            if not head:
                break
            if len(head)!=5:
                raise ValueError("truncated record")
            cmd,size=struct.unpack("<BI",head)
            if size>MAX_TRACE_PAYLOAD:
                raise ValueError("oversized trace record")
            payload=inp.read(size)
            if len(payload)!=size:
                raise ValueError("truncated payload")
            yield cmd,payload


def replay(panel:EpdPanel,records)->EpdPanel:
    for command,data in records:
        panel.command_byte(command)
        panel.data_bytes(data)
    panel.flush()
    return panel
