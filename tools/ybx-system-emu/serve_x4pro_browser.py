#!/usr/bin/env python3
"""Browser-driven REAL original X4 Pro factory guest + opt-in QEMU SSD1677.

No firmware writes, invented pixels or direct semaphore injection.
Docker port MUST bind localhost on the host unless separately protected.
"""
import argparse
import email.utils
import hashlib
import json
import os
import shutil
import stat
import subprocess
import threading
import time
from datetime import timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

from epd_frame_io import save_mono_png, ssd1677_landscape_to_portrait
from probe_x4pro_touch_injection_qtest import qmp_connect

W, H = 480, 800

# SHA-256 values verified against the operator's read-only factory media on
# 2026-10-31 (see experiments/0066-batch3-rom-selection.md). Anything else is
# a custom ROM of unknown provenance and is reported as such.
KNOWN_FACTORY_IMAGES = {
    # qa-20261029/s3-full-rom-generator-test.bin (the bridge --rom, -L blob)
    "5b950be93a30610322a3b46049e4095cdda04f33f09fa13a9a9a6167b5994bd9":
        "factory-rom",
    # qa-20261029/s3-flash-complete-rom-test.bin (the factory flash image)
    "206ed9661bd71477d1a54397c5a2cfe490179a3f59ab094f1e7421745c609523":
        "factory-flash",
}
PROVENANCE_LABELS = {
    "factory-rom": "Factory ROM image (SHA-256 matches the known factory -rom blob)",
    "factory-flash": "Factory flash image (SHA-256 matches the known factory flash)",
    "custom": "Custom ROM of unknown provenance (SHA-256 matches no known factory image)",
    "unhashed": "Provenance unverified (file too large to hash)",
}


class BadRequestError(ValueError):
    """Client sent something the bridge cannot act on. Maps to HTTP 400."""
    kind = "bad-request"
    http_status = 400


class RomNotFoundError(BadRequestError):
    """A well-formed name that is not a file in --rom-dir. Maps to HTTP 404."""
    kind = "rom-not-found"
    http_status = 404


class GuestError(RuntimeError):
    """QEMU guest interaction failed in a way the client can classify."""
    kind = "guest-error"
    http_status = 503
    retry_after = None


class GuestBusyError(GuestError):
    kind = "guest-busy"
    retry_after = "1"


class TapQueueFullError(GuestBusyError):
    http_status = 429


class GuestMissingError(GuestError):
    kind = "guest-missing"


def etag_matches(header_value, etag):
    """RFC 9110 If-None-Match comparison: '*', lists, weak (W/) prefixes."""
    if header_value is None:
        return False
    header_value = header_value.strip()
    if header_value == "*":
        return True

    def strip_weak(value):
        value = value.strip()
        return value[2:] if value.startswith("W/") else value

    target = strip_weak(etag)
    return any(strip_weak(item) == target for item in header_value.split(","))


def not_modified_since(header_value, mtime):
    """RFC 9110 If-Modified-Since comparison at one-second resolution."""
    try:
        parsed = email.utils.parsedate_to_datetime(header_value)
    except (TypeError, ValueError):
        return False
    if parsed is None:
        return False
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(mtime) <= int(parsed.timestamp())


def guest_ready_for_touch(frames):
    """Allow input once the guest has completed its initial panel refreshes.

    Pixel density is NOT a readiness signal: real menus and sleep/settings
    screens can contain fewer than 4000 nonwhite bytes. Never disable touch
    after navigating to such a screen.
    """
    return frames >= 2


def image_kind(path):
    """Classify a candidate image by its own bytes, not by its name.

    "flash" - an ESP flash image: bootloader magic 0xE9 at 0x0, partition
    table 0xAA50 at 0x8000, application magic 0xE9 at 0x10000.
    "sd"    - a FAT/SD image: MBR signature 0x55AA at 0x1FE.
    "unknown" - anything else; such a file is never booted.
    """
    try:
        with open(path, "rb") as handle:
            head = handle.read(0x10010)
    except OSError:
        return "unknown"
    if len(head) < 0x200:
        return "unknown"
    if (len(head) >= 0x10010 and head[0] == 0xE9
            and head[0x8000:0x8002] == b"\xaa\x50" and head[0x10000] == 0xE9):
        return "flash"
    if head[0x1FE:0x200] == b"\x55\xaa":
        return "sd"
    return "unknown"


def sha256_file(path, max_bytes):
    """Streaming SHA-256 of a candidate ROM file.

    Returns None when the file is larger than max_bytes (even if it only
    grows over the limit mid-read); OSError propagates to the caller, which
    decides how to report an unreadable file. Never returns a fabricated hash.
    """
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        remaining = max_bytes + 1
        while remaining > 0:
            chunk = handle.read(min(1024 * 1024, remaining))
            if not chunk:
                return digest.hexdigest()
            digest.update(chunk)
            remaining -= len(chunk)
        return None


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport"
content="width=device-width,initial-scale=1,user-scalable=no">
<title>YBX X4 Pro — QEMU Emulator</title>
<style>
:root{font-family:system-ui,-apple-system,sans-serif;background:#eeede7;color:#171715}
*{box-sizing:border-box}
body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center}
main{width:min(100%,900px);padding:20px;display:grid;grid-template-columns:minmax(240px,480px) minmax(210px,1fr);gap:26px;align-items:start}
.device{background:#222221;border-radius:17px;padding:18px;box-shadow:0 18px 36px #2222;position:relative}
.device img{display:block;aspect-ratio:3/5;width:100%;background:#fff;image-rendering:pixelated;touch-action:manipulation;cursor:pointer;user-select:none;-webkit-user-select:none;border-radius:3px}
h1{font-size:22px;margin:0 0 8px;letter-spacing:-.04em}
p{line-height:1.5;font-size:14px;color:#504f49}
strong{color:#161615}
.status{display:inline-block;font-size:13px;border:1px solid #a8a79e;padding:7px 10px;border-radius:5px;margin:16px 0 10px}
code{font-size:12px;word-break:break-word}
button{background:#242421;color:#fff;border:0;border-radius:6px;padding:11px 13px;font-size:13px;cursor:pointer}
button:disabled{opacity:.45;cursor:default}
#power{touch-action:none;user-select:none;-webkit-user-select:none;width:100%;
background:#f9f8f2;color:#171715;border:1px solid #85847c;font-weight:700}
#power.is-held{background:#b5b1a6;transform:translateY(1px)}
#powerbox small{margin:4px 0 0}
h2{font-size:15px;margin:20px 0 6px}
select{width:100%;padding:9px;border-radius:6px;border:1px solid #a8a79e;font-size:13px;background:#fff;color:#171715}
.romerror{color:#8c2f2f;font-size:13px}
#uart{max-height:180px;overflow:auto;background:#111;color:#ddd;padding:8px;
      font:12px/1.35 ui-monospace,Menlo,monospace;white-space:pre-wrap}
small{color:#66665e;display:block;margin-top:11px;line-height:1.6}
@media(max-width:680px){body{align-items:start}main{display:flex;flex-direction:column;align-items:center;padding:12px;gap:16px}.device{padding:12px;width:min(100%,480px)}.details{width:min(100%,480px)}}
</style></head><body><main>
<div class="device"><img id="screen" alt="Real SSD1677 framebuffer, YBX X4 Pro"
draggable="false" /></div>
<div class="details"><h1>YBX X4 Pro <small>QEMU ESP32-S3 · active ROM shown below</small></h1>
<p>The display is the <strong>real guest-written SSD1677 480×800 RAM</strong>,
not an HTML mockup. Tap directly on the reader screen to send a
150 ms GT911 touch to the virtual device.</p>
<div class="status" id="status">Connecting to QEMU…</div>
<p id="info">The original firmware initializes the panel before the home screen appears.</p>
<p id="metrics"><small>BUSY telemetry: loading…</small></p>
<button id="refresh">Refresh display</button>
<button id="reboot">Restart virtual reader</button>
<div id="powerbox" hidden>
<h2>Virtual hardware key</h2>
<button id="power" type="button" aria-label="Hold virtual Power key">Hold Power</button>
<small>Experimental X4 Pro GPIO3 / RTC EXT1. Hold the virtual key;
release it to finish. Safety limit: 1.2 seconds per press. This is not
a physical reader and cannot emulate battery or real sleep current.</small>
</div>
<h2>Guest ROM</h2>
<p><select id="romlist" aria-label="Candidate ROM files in --rom-dir">
<option value="">Loading ROM list…</option></select></p>
<p><button id="loadrom" disabled>Load ROM</button></p>
<p id="romactive"><small>Active ROM: loading…</small></p>
<p id="romerror" class="romerror" hidden></p>
<h2>Guest firmware</h2>
<p><select id="fwlist" aria-label="Candidate firmware images in --firmware-dir">
<option value="">Loading firmware list…</option></select></p>
<p><button id="loadfw" disabled>Load firmware</button></p>
<p id="fwactive"><small>Active firmware: loading…</small></p>
<p id="fwerror" class="romerror" hidden></p>
<h2>Guest console</h2>
<p id="uartinfo"><small>UART0 tail: loading…</small></p>
<pre id="uart">(no guest output yet)</pre>
<p><strong>Wi-Fi radio is not emulated.</strong> Join a Network,
Calibre Wireless and Create Hotspot cannot connect from this factory guest.
Attempting network setup may stop its interface responding; use
<strong>Restart virtual reader</strong> to reboot the virtual device.
If the emulator process itself stopped, the same button starts a fresh guest.</p>
<small>Local developer preview. Some features (Wi-Fi, hardware timing,
real E-Ink waveform and other panel variants) remain unimplemented.
One virtual device per server. No physical reader is attached. The ROM
folder is read-only for this server; loading a ROM restarts the guest and
the page only claims what the frame counter actually shows.</small>
</div></main>
<script>
(function(){
const img=document.getElementById('screen'),status=document.getElementById('status');
const info=document.getElementById('info'),metrics=document.getElementById('metrics');
const refresh=document.getElementById('refresh'),reboot=document.getElementById('reboot');
const powerBox=document.getElementById('powerbox'),power=document.getElementById('power');
const romList=document.getElementById('romlist'),loadRom=document.getElementById('loadrom');
const romActive=document.getElementById('romactive'),romError=document.getElementById('romerror');
const fwList=document.getElementById('fwlist'),loadFw=document.getElementById('loadfw');
const fwActive=document.getElementById('fwactive'),fwError=document.getElementById('fwerror');
const uartBox=document.getElementById('uart'),uartInfo=document.getElementById('uartinfo');
let ready=false,lastFrame=-1,busy=false,ticks=0,romLoaded=false,romBusy=false;
let fwLoaded=false,fwBusy=false;
let powerAvailable=false,powerDown=false,powerChain=Promise.resolve();
function releasePower(){
 if(!powerDown)return;
 powerDown=false;power.classList.remove('is-held');
 queuePower(false);
}
function queuePower(pressed){
 // Keep release behind press even when quick taps overlap async HTTP.
 // The server also auto-releases independently of browser state.
 powerChain=powerChain.catch(()=>{}).then(async()=>{
   const response=await fetch('/api/power',{method:'POST',
     headers:{'Content-Type':'application/json'},
     body:JSON.stringify({pressed:pressed}),keepalive:!pressed});
   const result=await response.json();
   if(!response.ok)throw new Error(result.error||'Virtual Power request failed');
   return result;
 });
 powerChain.catch(e=>{status.textContent='Virtual Power failed: '+e.message;
                     powerDown=false;power.classList.remove('is-held');});
 return powerChain;
}
function pressPower(ev){
 if(!powerAvailable||powerDown)return;
 if(ev&&ev.type==='pointerdown'&&ev.button!==0)return;
 if(ev)ev.preventDefault();
 powerDown=true;power.classList.add('is-held');
 if(ev&&ev.pointerId!==undefined){
   try{power.setPointerCapture(ev.pointerId)}catch(e){}
 }
 status.textContent='Virtual POWER → GPIO3 LOW (RTC EXT1 only if guest armed)';
 queuePower(true);
}
power.addEventListener('pointerdown',pressPower);
for(const name of ['pointerup','pointercancel','lostpointercapture']){
 power.addEventListener(name,releasePower);
}
power.addEventListener('keydown',ev=>{
 if((ev.key===' '||ev.key==='Enter')&&!ev.repeat)pressPower(ev);
});
power.addEventListener('keyup',ev=>{
 if(ev.key===' '||ev.key==='Enter')releasePower();
});
window.addEventListener('blur',releasePower);
document.addEventListener('visibilitychange',()=>{
 if(document.hidden)releasePower();
});
window.addEventListener('pagehide',()=>{
 if(powerDown){
   releasePower();
   // Best effort on a terminating page; the server-side watchdog is
   // authoritative even when neither this nor fetch gets delivered.
   if(navigator.sendBeacon){
     navigator.sendBeacon('/api/power',new Blob(
       [JSON.stringify({pressed:false})],{type:'application/json'}));
   }
 }
});
async function poll(){
 try{
   const response=await fetch('/api/state',{cache:'no-store'});
   const s=await response.json();
   if(!response.ok)throw new Error(s.error||'QEMU read failed');
   ready=Boolean(s.ready);
   powerAvailable=Boolean(s.power&&s.power.available);
   powerBox.hidden=!powerAvailable;
   power.disabled=!powerAvailable;
   if(ready){status.textContent='Ready — tap the display';}
   else if(s.boot_failed){
     status.textContent='The guest did not boot from this ROM (0 frames after '
       +(s.boot_wait_seconds||0)+' s). Check the image for this chip, or switch '
       'back to the factory ROM.';
   }
   else{status.textContent='Starting guest from the active ROM…';}
   info.textContent='Display updates: '+s.frames+' · Nonwhite bytes: '+s.nonwhite+
     (s.sha256?' · Frame SHA-256: '+s.sha256.slice(0,12)+'…':'');
   if(s.frames!==lastFrame){
     lastFrame=s.frames;img.src='/screen.png?frame='+s.frames;
   }
   if(s.rom)showRomActive(s.rom);
   if(s.firmware)showFwActive(s.firmware);
   if(++ticks%3===0)showTelemetry();
 }catch(e){ready=false;status.textContent='Emulator unavailable: '+e.message}
}
function showRomActive(rom){
 const sha=rom.sha256?rom.sha256.slice(0,16)+'…':'not hashed';
 romActive.textContent='Active ROM: '+rom.name+' · '+sha+' · '
   +(rom.provenance_note||rom.provenance||'unknown');
}
async function loadRomList(){
 try{
   const response=await fetch('/api/roms',{cache:'no-store'});
   const r=await response.json();
   if(!response.ok)throw new Error(r.error||'ROM list read failed');
   romList.innerHTML='';romList.disabled=false;
   r.roms.forEach(function(item){
     const o=document.createElement('option');
     o.value=item.name;
     o.textContent=item.name+' · '+(item.size/1048576).toFixed(1)+' MiB'
       +(item.sha256?'':' · too large to hash')+(item.active?' · active':'');
     if(item.active)o.selected=true;
     romList.appendChild(o);
   });
   if(!r.roms.length){
     const o=document.createElement('option');
     o.value='';o.textContent='No ROM files in the ROM folder';
     romList.appendChild(o);
   }
   if(r.truncated)romList.title='List capped at '+r.limit+' entries';
   romLoaded=true;
   loadRom.disabled=romBusy||!romList.value;
 }catch(e){
   // The page must stay usable when /api/roms is unavailable.
   romLoaded=false;romList.innerHTML='';
   const o=document.createElement('option');
   o.value='';o.textContent='ROM list unavailable';
   romList.appendChild(o);
   romList.disabled=true;loadRom.disabled=true;
 }
}
function showFwActive(fw){
 const sha=fw.sha256?fw.sha256.slice(0,16)+'…':'not hashed';
 fwActive.textContent='Active firmware: '+fw.name+' · '+sha+' · '
   +(fw.provenance_note||fw.provenance||'unknown');
}
async function loadFwList(){
 try{
   const response=await fetch('/api/firmware',{cache:'no-store'});
   const r=await response.json();
   if(!response.ok)throw new Error(r.error||'Firmware list read failed');
   fwList.innerHTML='';fwList.disabled=false;
   r.firmwares.forEach(function(item){
     const o=document.createElement('option');
     o.value=item.name;
     o.textContent=item.name+' · '+(item.size/1048576).toFixed(1)+' MiB'
       +(item.kind&&item.kind!=='flash'?' · '+item.kind:' · flash')
       +(item.sha256?'':' · too large to hash')+(item.active?' · active':'');
     if(item.active)o.selected=true;
     fwList.appendChild(o);
   });
   if(!r.firmwares.length){
     const o=document.createElement('option');
     o.value='';o.textContent='No firmware images in the firmware folder';
     fwList.appendChild(o);
   }
   if(r.truncated)fwList.title='List capped at '+r.limit+' entries';
   fwLoaded=true;
   loadFw.disabled=fwBusy||!fwList.value;
 }catch(e){
   // The page must stay usable when /api/firmware is unavailable.
   fwLoaded=false;fwList.innerHTML='';
   const o=document.createElement('option');
   o.value='';o.textContent='Firmware list unavailable';
   fwList.appendChild(o);
   fwList.disabled=true;loadFw.disabled=true;
 }
}
const METRIC_LABELS={'ybx-epd-busy-rise-edges':'BUSY up','ybx-epd-busy-fall-edges':'BUSY down',
 'ybx-epd-busy-high-ms':'BUSY ms','ybx-epd-frames':'frames','frame-sha256':'frame'};
async function showTelemetry(){
 try{
   const response=await fetch('/api/telemetry',{cache:'no-store'});
   const t=await response.json();
   if(!response.ok)throw new Error(t.error||'telemetry read failed');
   const parts=[];
   for(const key in METRIC_LABELS){
     const c=t[key];
     parts.push(METRIC_LABELS[key]+': '+((c&&c.available)?c.value:'n/a'));
   }
   metrics.textContent=parts.join(' · ');
 }catch(e){metrics.textContent='BUSY telemetry: unavailable ('+e.message+')'}
}
img.addEventListener('click',async function(ev){
 if(!ready||busy)return;
 const rect=img.getBoundingClientRect();
 if(!rect.width||!rect.height)return;
 const x=Math.max(0,Math.min(479,Math.floor((ev.clientX-rect.left)/rect.width*480)));
 const y=Math.max(0,Math.min(799,Math.floor((ev.clientY-rect.top)/rect.height*800)));
 busy=true; status.textContent='Touch '+x+', '+y+' → GT911';
 try{
  const response=await fetch('/api/tap',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({x:x,y:y,duration_ms:150})});
  const payload=await response.json();
  if(!response.ok)throw new Error(payload.error||'Touch failed');
 }catch(e){status.textContent='Touch failed: '+e.message}
 finally{busy=false;poll()}
});
refresh.addEventListener('click',poll);
reboot.addEventListener('click',async function(){
 if(busy || !confirm('Restart the virtual X4 Pro? Unsaved guest state may be lost.'))return;
 releasePower();await powerChain.catch(()=>{});
 busy=true;reboot.disabled=true;status.textContent='Restarting virtual device…';
 try{
   const response=await fetch('/api/reboot',{method:'POST'});
   const result=await response.json();
   if(!response.ok)throw new Error(result.error||'QEMU reset failed');
   lastFrame=-1;ready=false;img.removeAttribute('src');
 }catch(e){status.textContent='Restart failed: '+e.message}
 finally{busy=false;reboot.disabled=false;poll()}
});
loadRom.addEventListener('click',async function(){
 if(romBusy||!romList.value)return;
 releasePower();await powerChain.catch(()=>{});
 romBusy=true;loadRom.disabled=true;romError.hidden=true;
 status.textContent='Loading ROM '+romList.value+'…';
 try{
   const response=await fetch('/api/rom',{method:'POST',
     headers:{'Content-Type':'application/json'},
     body:JSON.stringify({name:romList.value})});
   const result=await response.json();
   if(!response.ok)throw new Error(result.error||'ROM switch failed');
   lastFrame=-1;ready=false;img.removeAttribute('src');
   await loadRomList();
   showRomActive({name:result.name,sha256:result.sha256,
                  provenance_note:result.provenance_note});
   status.textContent='ROM '+result.name+' is now active; the guest restarts below';
 }catch(e){romError.textContent='Load ROM failed: '+e.message;romError.hidden=false}
 finally{romBusy=false;loadRom.disabled=!romLoaded||!romList.value;poll()}
});
loadFw.addEventListener('click',async function(){
 if(fwBusy||!fwList.value)return;
 releasePower();await powerChain.catch(()=>{});
 fwBusy=true;loadFw.disabled=true;fwError.hidden=true;
 status.textContent='Loading firmware '+fwList.value+'…';
 try{
   const response=await fetch('/api/firmware',{method:'POST',
     headers:{'Content-Type':'application/json'},
     body:JSON.stringify({name:fwList.value})});
   const result=await response.json();
   if(!response.ok)throw new Error(result.error||'Firmware switch failed');
   lastFrame=-1;ready=false;img.removeAttribute('src');
   await loadFwList();
   showFwActive({name:result.name,sha256:result.sha256,
                 provenance_note:result.provenance_note});
   status.textContent='Firmware '+result.name+' is now active; the guest restarts below';
 }catch(e){fwError.textContent='Load firmware failed: '+e.message;fwError.hidden=false}
 finally{fwBusy=false;loadFw.disabled=!fwLoaded||!fwList.value;poll()}
});
async function pollUart(){
 try{
   const response=await fetch('/api/uart?tail=4096',{cache:'no-store'});
   const u=await response.json();
   if(!response.ok)throw new Error(u.error||'UART read failed');
   if(!u.available){uartInfo.textContent='UART0 tail: '+(u.error||'empty');uartBox.textContent='';return}
   uartInfo.textContent='UART0 tail: '+u.bytes+' bytes total, showing the last '+u.limit;
   uartBox.textContent=u.tail;
   uartBox.scrollTop=uartBox.scrollHeight;
 }catch(e){uartInfo.textContent='UART0 tail: unavailable ('+e.message+')'}
}
poll();loadRomList();loadFwList();pollUart();
setInterval(poll,1000);setInterval(pollUart,3000);
})();
</script></body></html>"""


def check_touch(data):
    """Validate actual X4 Pro 480x800 raw GT911 coordinates."""
    if not isinstance(data, dict):
        raise BadRequestError("Touch payload must be a JSON object")
    x, y = data.get("x"), data.get("y")
    ms = data.get("duration_ms", 150)
    if type(x) is not int or type(y) is not int:
        raise BadRequestError("x and y must be integers")
    if not (0 <= x < W and 0 <= y < H):
        raise BadRequestError("Touch outside X4 Pro panel")
    if type(ms) is not int or not (60 <= ms <= 500):
        raise BadRequestError("duration_ms must be 60..500")
    return x, y, ms


def prepare_guest_media(flash_source: Path, sd_source: Path,
                        workdir: Path, persistent_dir: Path | None):
    """Select guest R/W images without ever changing the immutable templates.

    In persistent mode, existing images are *resumed* without replacement. A
    half-populated or wrong-size media directory must fail closed: silently
    reinitializing either file would destroy book progress and erase flash.
    """
    media_root = persistent_dir if persistent_dir is not None else workdir
    flash = media_root / "flash.bin"
    sd = media_root / "sd.img"
    if flash.is_symlink() or sd.is_symlink():
        raise RuntimeError("persistent guest disk symlinks are forbidden")
    existing = (flash.exists(), sd.exists())
    if persistent_dir is not None and any(existing) and not all(existing):
        raise RuntimeError("persistent media is incomplete: flash.bin and "
                           "sd.img must both exist, or neither")
    if persistent_dir is None or not any(existing):
        if persistent_dir is not None and any(existing):
            raise AssertionError("unreachable partial media")
        shutil.copyfile(flash_source, flash)
        shutil.copyfile(sd_source, sd)
    for target, source in ((flash, flash_source), (sd, sd_source)):
        if not target.is_file() or target.is_symlink():
            raise RuntimeError(f"guest media must be a regular file: {target}")
        if target.stat().st_size != source.stat().st_size:
            raise RuntimeError(f"guest media size mismatch: {target}; "
                               "refusing to discard or resize saved data")
    return flash, sd


class FactoryX4Pro:
    tap_wait_seconds = 2.0
    tap_queue_depth = 4
    BOOT_FAIL_AFTER_SECONDS = 45
    QOM_PATH = "/machine/soc"
    TELEMETRY_COUNTERS = ("busy-rise-edges", "busy-fall-edges",
                          "busy-high-ms", "frames")
    ROM_LIST_LIMIT = 64
    ROM_MAX_HASH_BYTES = 64 * 1024 * 1024
    ROM_FILENAME_IN_WORKDIR = "esp32s3_rev0_rom.bin"
    FW_LIST_LIMIT = 64
    FW_MAX_HASH_BYTES = 64 * 1024 * 1024
    FW_FLASH_FILENAME_IN_WORKDIR = "flash.bin"
    UART_TAIL_MAX = 16384
    POWER_MAX_HOLD_SECONDS = 1.2
    POWER_QOM_PROPERTY = "ybx-x4pro-power-button-pressed"
    FW_SD_FILENAME_IN_WORKDIR = "sd.img"

    def __init__(self, args):
        self.temp = TemporaryDirectory(prefix="ybx-x4pro-browser-")
        self.workdir = Path(self.temp.name)
        t = self.workdir
        (t / "rom").mkdir()
        shutil.copyfile(args.rom, t / "rom" / self.ROM_FILENAME_IN_WORKDIR)
        self.persistent_media_dir = getattr(args, "persistent_media_dir", None)
        self.flash_path, self.sd_path = prepare_guest_media(
            args.flash, args.sd_image, t, self.persistent_media_dir)
        # Snapshot provenance is separate from the ORIGINAL firmware template.
        # Runtime writes can change these hashes after this initial capture.
        with self.flash_path.open("rb") as source:
            self.initial_guest_flash_sha = hashlib.file_digest(source, "sha256").hexdigest()
        with self.sd_path.open("rb") as source:
            self.initial_guest_sd_sha = hashlib.file_digest(source, "sha256").hexdigest()
        self.rom_dir = Path(getattr(args, "rom_dir", None)
                            or Path(args.rom).parent)
        self.rom_name = Path(args.rom).name
        self.rom_sha = sha256_file(Path(args.rom), self.ROM_MAX_HASH_BYTES)
        self.firmware_dir = Path(getattr(args, "firmware_dir", None)
                                 or Path(args.flash).parent)
        self.firmware_name = Path(args.flash).name
        self.firmware_path = Path(args.flash)
        self.firmware_sha = sha256_file(Path(args.flash),
                                        self.FW_MAX_HASH_BYTES)
        self._restore_firmware_selection()
        self.qemu_bin = args.qemu_bin
        self.power_enabled = bool(getattr(args, "enable_power_button", False))
        self.power_pressed = False
        self.power_generation = 0
        self.power_timer = None
        self.lock = threading.RLock()
        self.tap_lock = threading.Lock()
        self.tap_slots = threading.BoundedSemaphore(self.tap_queue_depth)
        self.process = None
        self.client = None
        self.boot_started_at = time.monotonic()
        self.frame = -1
        self.nonwhite = 0
        self.sha = ""
        self.png_file = t / "frame.png"
        save_mono_png(self.png_file, bytes([255]) * (W*H//8), W, H)
        self.blank_png = self.png_file.read_bytes()
        self.png = self.blank_png
        self.png_etag = '"blank"'
        self.png_mtime = time.time()
        self.qemu_log = (t/"qemu.log").open("w")
        try:
            self._launch()
        except Exception:
            self._stop_process()
            self.qemu_log.close()
            self.temp.cleanup()
            raise

    def _launch(self):
        t = self.workdir
        sock = t / "qmp.sock"
        if sock.exists():
            sock.unlink()  # stale socket left by a terminated guest
        self.process = subprocess.Popen([
            str(self.qemu_bin), "-machine",
            "esp32s3,ybx-x4pro-i2c=on,ybx-x4pro-gt911=on,"
            "ybx-x4pro-sd-power=on,ybx-x4pro-spi2=on,"
            "ybx-x4pro-epd=on",
            "-m","8M", "-global","ssi_psram.is_octal=on",
            "-nographic","-monitor","none",
            "-serial",f"file:{t/'uart0.log'}",
            "-serial","null","-serial","null","-serial","null",
            "-qmp",f"unix:{t/'qmp.sock'},server=on,wait=off",
            "-L",str(t/"rom"),
            "-drive",f"file={self.flash_path},if=mtd,format=raw",
            "-drive",f"file={self.sd_path},if=sd,format=raw"
        ], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
           stderr=self.qemu_log)
        try:
            self.client, self.reader, self.qmp = qmp_connect(sock)
            if self.power_enabled:
                probe = self.qmp("qom-get", {"path": self.QOM_PATH,
                                             "property": self.POWER_QOM_PROPERTY})
                if "return" not in probe or not isinstance(probe["return"], bool):
                    raise RuntimeError("Experimental QEMU lacks virtual X4 Pro "
                                       "Power GPIO3; refusing --enable-power-button")
        except Exception:
            self._stop_process()
            raise
        self.boot_started_at = time.monotonic()

    def _stop_process(self):
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try: self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()

    def _reset_frame_cache(self):
        self.frame = -1
        self.sha = ""
        self.boot_started_at = time.monotonic()
        self.png = self.blank_png
        self.png_etag = '"blank"'
        self.png_mtime = time.time()

    def _relaunch(self):
        """Replace a dead guest process inside the same server session."""
        self._reset_power_state_locked()
        self._stop_process()
        self.client.close()
        self._launch()
        self._reset_frame_cache()

    def power_status(self):
        return {"available": bool(getattr(self, "power_enabled", False)),
                "pressed": bool(getattr(self, "power_pressed", False)),
                "max_hold_ms": int(self.POWER_MAX_HOLD_SECONDS * 1000),
                "virtual_only": True}

    def _reset_power_state_locked(self):
        """Forget a button session only when terminating/replacing QEMU."""
        self.power_generation = getattr(self, "power_generation", 0) + 1
        timer = getattr(self, "power_timer", None)
        self.power_timer = None
        self.power_pressed = False
        if timer is not None:
            timer.cancel()

    def _set_power_locked(self, pressed):
        response = self.qmp("qom-set", {"path": self.QOM_PATH,
                                        "property": self.POWER_QOM_PROPERTY,
                                        "value": pressed})
        if "return" not in response:
            raise RuntimeError("QEMU virtual Power GPIO3 rejected input: "
                               + str(response.get("error", response)))

    def _release_power_locked(self):
        if not getattr(self, "power_pressed", False):
            return
        self._reset_power_state_locked()
        if self.process is None or self.process.poll() is not None:
            return  # no process left holding a GPIO level
        try:
            self._set_power_locked(False)
        except (OSError, RuntimeError, ValueError):
            # An unanswered QMP release is not proof of a released button.
            # Kill the possibly held guest and relaunch with clean GPIO inputs.
            self._relaunch()
            raise GuestMissingError("Virtual Power release failed; "
                                    "QEMU guest was restarted for safety")

    def _power_watchdog(self, generation):
        with self.lock:
            if generation != self.power_generation or not self.power_pressed:
                return
            try:
                self._release_power_locked()
            except (GuestError, OSError, RuntimeError):
                # _release_power_locked already attempted a hard recovery.
                pass

    def power_update(self, pressed):
        """Opt-in button input; server watchdog bounds every held session.

        Browser network/disconnect/visibility events are advisory only: no
        HTTP client can hold POWER indefinitely by dropping the release POST.
        A repeated press never renews the original 1.2-second deadline.
        """
        if not isinstance(pressed, bool):
            raise BadRequestError("pressed must be a boolean")
        with self.lock:
            if not self.power_enabled:
                raise BadRequestError("Virtual Power is disabled on this server")
            if self.process is None or self.process.poll() is not None:
                self._reset_power_state_locked()
                raise GuestMissingError("QEMU process is not running")
            if not pressed:
                self._release_power_locked()
            elif not self.power_pressed:
                try:
                    self._set_power_locked(True)
                except (OSError, RuntimeError, ValueError) as exc:
                    # Error while pressing is ambiguous; discard the guest
                    # before it can remain pressed indefinitely.
                    self._relaunch()
                    raise GuestMissingError("Virtual Power press failed; "
                                            "QEMU guest restarted") from exc
                self.power_pressed = True
                self.power_generation += 1
                timer = threading.Timer(self.POWER_MAX_HOLD_SECONDS,
                                        self._power_watchdog,
                                        args=(self.power_generation,))
                timer.daemon = True
                self.power_timer = timer
                timer.start()
            return self.power_status()

    def _get(self, property):
        result = self.qmp("qom-get",{"path":self.QOM_PATH,
                                      "property":"ybx-epd-"+property})
        if "return" not in result:
            raise RuntimeError(str(result.get("error",result)))
        return result["return"]

    def state(self):
        with self.lock:
            if self.process.poll() is not None:
                raise GuestMissingError("QEMU process terminated; "
                                        "use Restart virtual reader")
            frames = self._get("frames")
            self.nonwhite = self._get("nonwhite")
            if frames != self.frame and frames:
                raw = bytes.fromhex(self._get("visible-hex"))
                if len(raw) != W*H//8:
                    raise RuntimeError("SSD1677 frame has wrong size")
                portrait = ssd1677_landscape_to_portrait(raw)
                save_mono_png(self.png_file, portrait, W, H)
                self.png = self.png_file.read_bytes()
                self.sha = hashlib.sha256(raw).hexdigest()
                self.png_etag = f'"f{frames}-{self.sha[:16]}"'
                self.png_mtime = time.time()
            self.frame = frames
            waited = int(time.monotonic() - self.boot_started_at)
            return {"frames":frames,"monotonic_ms":int(time.monotonic()*1000),
                    "nonwhite":self.nonwhite,
                    "ready":guest_ready_for_touch(frames),
                    "guest_booted":bool(frames > 0),
                    "boot_wait_seconds":waited,
                    "boot_failed":bool(frames == 0
                                       and waited >= self.BOOT_FAIL_AFTER_SECONDS),
                    "sha256":self.sha,
                    "factory_guest":self.provenance_of(self.firmware_sha)
                                      == "factory-flash",
                    "firmware":self.firmware_info(),
                    "rom":self.rom_info(),
                    "physical_device":False,
                    "media": {"persistent": bool(getattr(self, "persistent_media_dir", None)),
                              "initial_guest_flash_sha256": getattr(self, "initial_guest_flash_sha", None),
                              "initial_guest_sd_sha256": getattr(self, "initial_guest_sd_sha", None)},
                    "power":self.power_status()}

    def telemetry(self):
        """Read-only EPD BUSY/frame counters from QOM; never writes anything.

        A property the running QEMU binary does not expose is reported as
        unavailable with the QMP error text — never as a fabricated zero.
        """
        with self.lock:
            if self.process.poll() is not None:
                raise GuestMissingError("QEMU process terminated; "
                                        "use Restart virtual reader")
            counters = {}
            for short in self.TELEMETRY_COUNTERS:
                name = "ybx-epd-" + short
                try:
                    value = self._get(short)
                except (OSError, ValueError) as exc:
                    raise GuestMissingError(
                        f"QMP transport failed while reading {name}: {exc}"
                    ) from exc
                except RuntimeError as exc:
                    counters[name] = {"available": False, "value": None,
                                      "error": str(exc)}
                    continue
                counters[name] = {"available": True, "value": value,
                                  "source": f"qom:{self.QOM_PATH}/{name}"}
            if self.sha:
                counters["frame-sha256"] = {
                    "available": True, "value": self.sha,
                    "source": "server:sha256(ssd1677-visible-ram)"}
            else:
                counters["frame-sha256"] = {
                    "available": False, "value": None,
                    "error": "no rendered frame has been hashed yet"}
            return counters

    @staticmethod
    def provenance_of(sha):
        if sha is None:
            return "unhashed"
        return KNOWN_FACTORY_IMAGES.get(sha, "custom")

    def rom_info(self):
        provenance = self.provenance_of(self.rom_sha)
        return {"name": self.rom_name,
                "sha256": self.rom_sha,
                "provenance": provenance,
                "provenance_note": PROVENANCE_LABELS[provenance],
                "known_factory_image": provenance.startswith("factory-")}

    def roms(self):
        """List candidate ROM files in rom_dir; the server never writes there.

        Regular files only (symlinks are skipped, never followed), sorted by
        name, capped at ROM_LIST_LIMIT entries and ROM_MAX_HASH_BYTES per
        hashed file. An unhashed file is reported with sha256=null and an
        error note instead of a fabricated hash.
        """
        candidates = []
        with os.scandir(self.rom_dir) as entries:
            for entry in entries:
                try:
                    if entry.is_file(follow_symlinks=False):
                        candidates.append(entry)
                except OSError:
                    continue  # dangling directory entry; not a usable file
        candidates.sort(key=lambda entry: entry.name)
        truncated = len(candidates) > self.ROM_LIST_LIMIT
        roms = []
        for entry in candidates[:self.ROM_LIST_LIMIT]:
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            item = {"name": entry.name, "size": info.st_size,
                    "sha256": None,
                    "active": entry.name == self.rom_name}
            try:
                item["sha256"] = sha256_file(Path(entry.path),
                                             self.ROM_MAX_HASH_BYTES)
            except OSError:
                item["error"] = "file could not be read for hashing"
            if item["sha256"] is None and "error" not in item:
                item["error"] = (f"larger than {self.ROM_MAX_HASH_BYTES} "
                                 "bytes; not hashed")
            provenance = self.provenance_of(item["sha256"])
            item["provenance"] = provenance
            item["provenance_note"] = PROVENANCE_LABELS[provenance]
            roms.append(item)
        return {"roms": roms, "active": self.rom_name,
                "truncated": truncated,
                "limit": self.ROM_LIST_LIMIT,
                "max_hash_bytes": self.ROM_MAX_HASH_BYTES}

    def _resolve_rom_target(self, name):
        """Map a client-supplied name to a regular file inside rom_dir only."""
        if (not isinstance(name, str) or not name or name in (".", "..")
                or "\x00" in name or "/" in name or "\\" in name
                or os.sep in name or (os.altsep and os.altsep in name)):
            raise BadRequestError("ROM name must be a plain file name "
                                  "inside --rom-dir")
        candidate = self.rom_dir / name
        try:
            info = os.lstat(candidate)
        except OSError:
            raise RomNotFoundError(f"Unknown ROM: {name}") from None
        if not stat.S_ISREG(info.st_mode):
            # Any symlink is rejected, so none can point outside rom_dir.
            raise BadRequestError(f"{name} is not a regular file in --rom-dir")
        resolved = candidate.resolve()
        if resolved.parent != self.rom_dir.resolve():
            raise BadRequestError(f"{name} resolves outside --rom-dir")
        return resolved

    def _clear_touch_state_quietly(self):
        """Best-effort GT911 zeroing before the guest process is replaced.

        The fresh process starts with cleared synthetic touch state anyway; a
        dead QMP transport here must not abort an already validated swap.
        """
        if self.process.poll() is not None:
            return
        try:
            for prop in ("state", "x", "y"):
                self._set_touch(prop, 0)
        except (OSError, RuntimeError, ValueError):
            pass

    def switch_rom(self, name):
        """Load the named ROM and restart the guest like /api/reboot does.

        Success means the new QEMU process came up and accepted QMP; it is
        NOT a claim that the guest booted or painted a frame. /api/state
        frames remain the only boot truth.
        """
        target = self._resolve_rom_target(name)
        try:
            size = target.stat().st_size
        except OSError:
            raise RomNotFoundError(f"ROM vanished while loading: {name}") from None
        if size > self.ROM_MAX_HASH_BYTES:
            raise BadRequestError(f"ROM {name} is larger than "
                                  f"{self.ROM_MAX_HASH_BYTES} bytes; refusing "
                                  "to load an unverified image")
        try:
            sha = sha256_file(target, self.ROM_MAX_HASH_BYTES)
        except OSError:
            raise RomNotFoundError(f"ROM vanished while loading: {name}") from None
        if sha is None:
            raise BadRequestError(f"ROM {name} grew past the hash cap while "
                                  "loading; retry")
        self._begin_action()
        try:
            with self.lock:
                self._release_power_locked()
                self._clear_touch_state_quietly()
                self._stop_process()
                if self.client is not None:
                    self.client.close()
                staged = (self.workdir / "rom"
                          / (self.ROM_FILENAME_IN_WORKDIR + ".new"))
                shutil.copyfile(target, staged)
                os.replace(staged,
                           self.workdir / "rom" / self.ROM_FILENAME_IN_WORKDIR)
                self.rom_name = name
                self.rom_sha = sha
                self._reset_frame_cache()
                self._launch()
        finally:
            self._end_action()
        provenance = self.provenance_of(sha)
        return {"name": name, "sha256": sha, "provenance": provenance,
                "provenance_note": PROVENANCE_LABELS[provenance]}

    # --- firmware picker ----------------------------------------------------
    def _restore_firmware_selection(self):
        """Restore a firmware picker label only for a persistent guest flash.

        The template/source hash and on-disk guest flash can legitimately
        differ as NVS/reading progress is written by the virtual ESP32.
        """
        root = getattr(self, "persistent_media_dir", None)
        if root is None:
            return
        selection = root / "firmware-selection.json"
        if not selection.is_file():
            return
        metadata = json.loads(selection.read_text())
        name, sha = metadata.get("name"), metadata.get("sha256")
        if (not isinstance(name, str) or not name or Path(name).name != name
                or not isinstance(sha, str) or len(sha) != 64
                or any(c not in "0123456789abcdef" for c in sha)):
            raise RuntimeError("invalid persisted firmware metadata")
        self.firmware_name = name
        self.firmware_sha = sha
        self.firmware_path = self.firmware_dir / name

    def _save_firmware_selection(self):
        root = getattr(self, "persistent_media_dir", None)
        if root is None:
            return
        dest = root / "firmware-selection.json"
        staged = root / "firmware-selection.json.new"
        staged.write_text(json.dumps({"name": self.firmware_name,
                                      "sha256": self.firmware_sha}) + "\n")
        os.replace(staged, dest)

    def firmware_info(self):
        provenance = self.provenance_of(self.firmware_sha)
        return {"name": self.firmware_name,
                "sha256": self.firmware_sha,
                "provenance": provenance,
                "provenance_note": PROVENANCE_LABELS[provenance],
                "known_factory_image": provenance == "factory-flash"}

    def uart_tail(self, limit=None):
        """Read the tail of the guest's UART0 log (read-only, bounded).

        QEMU writes the first serial port to workdir/uart0.log. A guest that
        fails to boot prints there before it ever paints, so this is the only
        evidence some images produce.
        """
        try:
            wanted = int(limit) if limit is not None else self.UART_TAIL_MAX
        except (TypeError, ValueError):
            raise BadRequestError("tail must be an integer byte count")
        if wanted <= 0:
            raise BadRequestError("tail must be positive")
        wanted = min(wanted, self.UART_TAIL_MAX)
        path = self.workdir / "uart0.log"
        try:
            size = path.stat().st_size
        except OSError:
            return {"available": False, "bytes": 0, "tail": "",
                    "error": "the guest has not printed anything yet"}
        with path.open("rb") as handle:
            start = max(0, size - wanted)
            handle.seek(start)
            data = handle.read()
        return {"available": True, "bytes": size, "since_byte": start,
                "limit": wanted, "tail": data.decode("utf-8", "replace")}

    def firmwares(self):
        """List candidate firmware images in firmware_dir (read-only).

        Regular files only, symlinks are skipped and never followed, sorted by
        name, capped in count and in hashed size. Each entry carries the image
        kind: "flash" for a valid ESP flash image, "sd" for a FAT/SD image and
        "unknown" otherwise. A file that cannot be hashed is reported with
        sha256=null and an error, never with a fabricated hash.
        """
        candidates = []
        with os.scandir(self.firmware_dir) as entries:
            for entry in entries:
                try:
                    if not entry.is_file(follow_symlinks=False):
                        continue
                except OSError:
                    continue
                if Path(entry.name).suffix.lower() not in (".bin", ".img"):
                    continue  # only image files are offered as candidates
                candidates.append(entry)
        candidates.sort(key=lambda entry: entry.name)
        truncated = len(candidates) > self.FW_LIST_LIMIT
        items = []
        for entry in candidates[:self.FW_LIST_LIMIT]:
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            path = Path(entry.path)
            item = {"name": entry.name, "size": info.st_size,
                    "sha256": None,
                    "kind": image_kind(path),
                    "active": entry.name == self.firmware_name}
            try:
                item["sha256"] = sha256_file(path, self.FW_MAX_HASH_BYTES)
            except OSError:
                item["error"] = "file could not be read for hashing"
            if item["sha256"] is None and "error" not in item:
                item["error"] = (f"larger than {self.FW_MAX_HASH_BYTES} "
                                 "bytes; not hashed")
            provenance = self.provenance_of(item["sha256"])
            item["provenance"] = provenance
            item["provenance_note"] = PROVENANCE_LABELS[provenance]
            items.append(item)
        if not any(item["name"] == self.firmware_name for item in items):
            # The running image may live outside the folder; still show it.
            try:
                size = self.firmware_path.stat().st_size
            except OSError:
                size = None
            provenance = self.provenance_of(self.firmware_sha)
            items.insert(0, {"name": self.firmware_name, "size": size,
                             "sha256": self.firmware_sha,
                             "kind": image_kind(self.firmware_path),
                             "active": True, "in_folder": False,
                             "provenance": provenance,
                             "provenance_note": PROVENANCE_LABELS[provenance]})
        return {"firmwares": items, "active": self.firmware_name,
                "truncated": truncated,
                "limit": self.FW_LIST_LIMIT,
                "max_hash_bytes": self.FW_MAX_HASH_BYTES}

    def _resolve_firmware_target(self, name):
        """Map a client-supplied name to a regular file inside firmware_dir."""
        if (not isinstance(name, str) or not name or name in (".", "..")
                or "\x00" in name or "/" in name or "\\" in name
                or os.sep in name or (os.altsep and os.altsep in name)):
            raise BadRequestError("Firmware name must be a plain file name "
                                  "inside --firmware-dir")
        candidate = self.firmware_dir / name
        try:
            info = os.lstat(candidate)
        except OSError:
            raise RomNotFoundError(f"Unknown firmware: {name}") from None
        if not stat.S_ISREG(info.st_mode):
            raise BadRequestError(f"{name} is not a regular file in "
                                  "--firmware-dir")
        resolved = candidate.resolve()
        if resolved.parent != self.firmware_dir.resolve():
            raise BadRequestError(f"{name} resolves outside --firmware-dir")
        return resolved

    def switch_firmware(self, name):
        """Boot the named firmware image and restart the guest.

        Only a valid ESP flash image is accepted; anything else is refused
        before the guest is touched. Acceptance means the new process came up
        and accepted QMP - it is not a claim that the guest booted, which the
        frame counter in /api/state decides.
        """
        target = self._resolve_firmware_target(name)
        kind = image_kind(target)
        if kind != "flash":
            raise BadRequestError(
                f"{name} is not an ESP flash image (kind={kind}); refusing to "
                "load it")
        try:
            size = target.stat().st_size
        except OSError:
            raise RomNotFoundError(f"Firmware vanished while loading: {name}") \
                from None
        if size > self.FW_MAX_HASH_BYTES:
            raise BadRequestError(f"Firmware {name} is larger than "
                                  f"{self.FW_MAX_HASH_BYTES} bytes; refusing "
                                  "to load an unverified image")
        try:
            sha = sha256_file(target, self.FW_MAX_HASH_BYTES)
        except OSError:
            raise RomNotFoundError(f"Firmware vanished while loading: {name}") \
                from None
        if sha is None:
            raise BadRequestError(f"Firmware {name} grew past the hash cap "
                                  "while loading; retry")
        self._begin_action()
        try:
            with self.lock:
                self._release_power_locked()
                self._clear_touch_state_quietly()
                self._stop_process()
                if self.client is not None:
                    self.client.close()
                # Stage and replace on the SAME filesystem (atomic rename).
                # On a persistent volume do not swap a temporary symlink and
                # accidentally detach the live QEMU disk from host storage.
                flash_path = getattr(self, "flash_path",
                                     self.workdir / self.FW_FLASH_FILENAME_IN_WORKDIR)
                staged = flash_path.with_suffix(flash_path.suffix + ".new")
                shutil.copyfile(target, staged)
                os.replace(staged, flash_path)
                paired = self._paired_sd_image(target)
                if paired is not None:
                    sd_path = getattr(self, "sd_path",
                                      self.workdir / self.FW_SD_FILENAME_IN_WORKDIR)
                    staged_sd = sd_path.with_suffix(sd_path.suffix + ".new")
                    shutil.copyfile(paired, staged_sd)
                    os.replace(staged_sd, sd_path)
                self.firmware_name = name
                self.firmware_sha = sha
                self._reset_frame_cache()
                self._launch()
                self._save_firmware_selection()
        finally:
            self._end_action()
        provenance = self.provenance_of(sha)
        return {"name": name, "sha256": sha, "kind": kind,
                "provenance": provenance,
                "provenance_note": PROVENANCE_LABELS[provenance]}

    def _paired_sd_image(self, flash_path):
        """SD image with the same stem in firmware_dir, if there is one."""
        candidate = flash_path.with_suffix(".img")
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            return None
        return None

    def _begin_action(self):
        """Serialize touches and resets behind a bounded queue.

        At most tap_queue_depth clients may wait for the guest at once; the
        rest are refused immediately instead of piling up threads. A short
        wait lets fast repeated taps queue instead of being rejected.
        """
        if not self.tap_slots.acquire(blocking=False):
            raise TapQueueFullError(
                f"Tap queue is full ({self.tap_queue_depth} waiting); "
                "retry shortly")
        try:
            if not self.tap_lock.acquire(timeout=self.tap_wait_seconds):
                raise GuestBusyError("Guest busy with another touch; "
                                     "retry shortly")
        except BaseException:
            self.tap_slots.release()
            raise

    def _end_action(self):
        self.tap_lock.release()
        self.tap_slots.release()

    def tap(self, x, y, duration_ms):
        self._begin_action()
        pressed=False
        try:
            if not self.state()["ready"]:
                raise GuestBusyError("Guest display has not completed its "
                                     "initial refreshes")
            with self.lock:
                for prop, value in (("x",x),("y",y),("state",1)):
                    self._set_touch(prop, value)
                    if prop=="state":
                        pressed=True
            time.sleep(duration_ms/1000)
        finally:
            try:
                if pressed:
                    with self.lock:
                        self._set_touch("state", 0)
            finally:
                self._end_action()

    def _set_touch(self, prop, value):
        result = self.qmp("qom-set", {"path": "/machine/soc",
                                      "property": "ybx-touch-" + prop,
                                      "value": value})
        if "return" not in result:
            raise RuntimeError(str(result))

    def reboot(self):
        """Clear GT911 session before a QEMU reset; relaunch on dead QMP.

        A rejected touch-property update is a device error, not necessarily a
        disconnected transport. Do not reset on that error silently.
        """
        self._begin_action()
        try:
            with self.lock:
                self._release_power_locked()
                if self.process.poll() is not None:
                    self._relaunch()
                    return
                try:
                    for prop in ("state", "x", "y"):
                        self._set_touch(prop, 0)
                except OSError:
                    self._relaunch()
                    return
                except RuntimeError as exc:
                    message = str(exc)
                    if "QMP closed" in message:
                        self._relaunch()
                        return
                    if "virtual GT911 must be powered" in message:
                        # The guest parked the touch rail, so the synthetic
                        # session cannot be cleared. Replacing the process is
                        # the same clean state without pretending the device
                        # accepted input.
                        self._relaunch()
                        return
                    raise
                try:
                    result = self.qmp("system_reset")
                except (OSError, RuntimeError, ValueError):
                    self._relaunch()
                    return
                if "return" not in result:
                    raise RuntimeError(str(result.get("error", result)))
                self._reset_frame_cache()
        finally:
            self._end_action()

    def close(self):
        with self.lock:
            self._reset_power_state_locked()
        if self.client is not None:
            self.client.close()
        self._stop_process()
        self.qemu_log.close()
        self.temp.cleanup()


def make_handler(emu):
    class Handler(BaseHTTPRequestHandler):
        def reply(self, code, body, kind, cache=False, extra=None):
            self.send_response(code)
            if kind:
                self.send_header("Content-Type",kind)
            if body is not None:
                self.send_header("Content-Length",str(len(body)))
            self.send_header("Cache-Control",
                             "private, max-age=0, must-revalidate" if cache
                             else "no-store")
            self.send_header("X-Content-Type-Options","nosniff")
            for name,value in (extra or {}).items():
                self.send_header(name,value)
            self.end_headers()
            if body is not None:
                self.wfile.write(body)

        def response_json(self, code, payload, extra=None):
            self.reply(code,json.dumps(payload).encode(),
                       "application/json; charset=utf-8",extra=extra)

        def guest_failure(self, exc):
            code = getattr(exc,"http_status",503)
            payload = {"error":str(exc),
                       "type":getattr(exc,"kind","guest-error")}
            extra = None
            retry = getattr(exc,"retry_after",None)
            if retry:
                extra = {"Retry-After":retry}
            self.response_json(code,payload,extra=extra)

        def serve_screen(self):
            emu.state()
            etag = getattr(emu,"png_etag",None)
            mtime = getattr(emu,"png_mtime",None)
            validators = {}
            if etag:
                validators["ETag"] = etag
            if mtime:
                validators["Last-Modified"] = email.utils.formatdate(mtime,usegmt=True)
            if_none_match = self.headers.get("If-None-Match")
            if_modified = self.headers.get("If-Modified-Since")
            fresh = False
            if etag and if_none_match is not None:
                fresh = etag_matches(if_none_match,etag)
            elif not etag and mtime and if_modified is not None:
                fresh = not_modified_since(if_modified,mtime)
            if fresh:
                self.reply(304,None,None,cache=True,extra=validators)
                return
            self.reply(200,emu.png,"image/png",cache=True,extra=validators)

        def do_GET(self):
            try:
                path=self.path.split("?",1)[0]
                if path == "/":
                    self.reply(200,PAGE.encode(),"text/html; charset=utf-8")
                elif path == "/api/state":
                    self.response_json(200,emu.state())
                elif path == "/api/telemetry":
                    self.response_json(200,emu.telemetry())
                elif path == "/api/roms":
                    self.response_json(200,emu.roms())
                elif path == "/api/firmware":
                    self.response_json(200,emu.firmwares())
                elif path == "/api/uart":
                    tail = None
                    if "?" in self.path:
                        query = self.path.split("?", 1)[1]
                        for part in query.split("&"):
                            if part.startswith("tail="):
                                tail = part.split("=", 1)[1]
                    try:
                        self.response_json(200, emu.uart_tail(tail))
                    except BadRequestError as exc:
                        self.response_json(exc.http_status,
                                           {"error": str(exc),
                                            "type": exc.kind})
                elif path == "/screen.png":
                    self.serve_screen()
                else:
                    self.response_json(404,{"error":"Not found"})
            except GuestError as exc:
                self.guest_failure(exc)
            except Exception as exc:
                self.response_json(503,{"error":str(exc)})

        def handle_rom_switch(self):
            try:
                try:
                    if self.headers.get("Content-Type","").split(";",1)[0]!="application/json":
                        raise BadRequestError("Use application/json")
                    try:
                        size=int(self.headers.get("Content-Length","0"))
                    except ValueError:
                        raise BadRequestError("Invalid Content-Length")
                    if not 0 < size <= 256:
                        raise BadRequestError("Invalid payload size")
                    payload=json.loads(self.rfile.read(size))
                except ValueError as exc:
                    raise BadRequestError(str(exc)) from exc
                if not isinstance(payload,dict):
                    raise BadRequestError("ROM payload must be a JSON object")
                name=payload.get("name")
                if not isinstance(name,str):
                    raise BadRequestError("name must be a string")
                result=emu.switch_rom(name)
            except BadRequestError as exc:
                self.response_json(exc.http_status,
                                   {"error":str(exc),"type":exc.kind})
                return
            except GuestError as exc:
                self.guest_failure(exc)
                return
            except (RuntimeError,OSError) as exc:
                self.response_json(503,{"error":str(exc)})
                return
            self.response_json(200,{"accepted":True,"action":"rom-switch",
                                    **result})

        def handle_firmware_switch(self):
            try:
                try:
                    if self.headers.get("Content-Type","").split(";",1)[0]!="application/json":
                        raise BadRequestError("Use application/json")
                    try:
                        size=int(self.headers.get("Content-Length","0"))
                    except ValueError:
                        raise BadRequestError("Invalid Content-Length")
                    if not 0 < size <= 256:
                        raise BadRequestError("Invalid payload size")
                    payload=json.loads(self.rfile.read(size))
                except ValueError as exc:
                    raise BadRequestError(str(exc)) from exc
                if not isinstance(payload,dict):
                    raise BadRequestError("Firmware payload must be a JSON object")
                name=payload.get("name")
                if not isinstance(name,str):
                    raise BadRequestError("name must be a string")
                result=emu.switch_firmware(name)
            except BadRequestError as exc:
                self.response_json(exc.http_status,
                                   {"error":str(exc),"type":exc.kind})
                return
            except GuestError as exc:
                self.guest_failure(exc)
                return
            except (RuntimeError,OSError) as exc:
                self.response_json(503,{"error":str(exc)})
                return
            self.response_json(200,{"accepted":True,"action":"firmware-switch",
                                    **result})

        def handle_power(self):
            try:
                if self.headers.get("Content-Type", "").split(";", 1)[0] != "application/json":
                    raise BadRequestError("Use application/json")
                try:
                    size = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    raise BadRequestError("Invalid Content-Length") from None
                if not 0 < size <= 64:
                    raise BadRequestError("Invalid Power payload size")
                try:
                    payload = json.loads(self.rfile.read(size))
                except (ValueError, UnicodeDecodeError):
                    raise BadRequestError("Invalid Power JSON") from None
                if not isinstance(payload, dict) or set(payload) != {"pressed"} \
                        or type(payload["pressed"]) is not bool:
                    raise BadRequestError("Power payload must be {pressed: boolean}")
                result = emu.power_update(payload["pressed"])
            except BadRequestError as exc:
                self.response_json(400, {"type": "bad-request", "error": str(exc)})
                return
            except GuestError as exc:
                self.guest_failure(exc)
                return
            except (RuntimeError, OSError, ValueError) as exc:
                self.response_json(503, {"type": "guest-error", "error": str(exc)})
                return
            self.response_json(200, {"accepted": True, "action": "virtual-power", **result})

        def do_POST(self):
            if self.path == "/api/power":
                self.handle_power()
                return
            if self.path=="/api/reboot":
                try:
                    emu.reboot()
                    self.response_json(200,{"accepted":True,"action":"QEMU system_reset"})
                except GuestError as exc:
                    self.guest_failure(exc)
                except (ValueError,RuntimeError,OSError) as exc:
                    self.response_json(503,{"error":str(exc)})
                return
            if self.path=="/api/rom":
                self.handle_rom_switch()
                return
            if self.path=="/api/firmware":
                self.handle_firmware_switch()
                return
            if self.path!="/api/tap":
                self.response_json(404,{"error":"Not found"})
                return
            try:
                if self.headers.get("Content-Type","").split(";",1)[0]!="application/json":
                    raise BadRequestError("Use application/json")
                try:
                    size=int(self.headers.get("Content-Length","0"))
                except ValueError:
                    raise BadRequestError("Invalid Content-Length")
                if not 0 < size <= 160:
                    raise BadRequestError("Invalid payload size")
                try:
                    payload=json.loads(self.rfile.read(size))
                except ValueError:
                    raise BadRequestError("Invalid JSON payload")
                x,y,ms=check_touch(payload)
            except ValueError as exc:
                self.response_json(400,{"error":str(exc),"type":"bad-request"})
                return
            try:
                emu.tap(x,y,ms)
            except GuestError as exc:
                self.guest_failure(exc)
                return
            except (RuntimeError,OSError) as exc:
                self.response_json(503,{"error":str(exc)})
                return
            self.response_json(200,{"accepted":True,"x":x,"y":y,
                                    "duration_ms":ms,"frame_not_yet_advanced":True})

        def log_message(self, fmt, *args):
            pass
    return Handler


def main():
    p=argparse.ArgumentParser(description=__doc__)
    for flag in ("qemu-bin","rom","flash","sd-image"):
        p.add_argument("--"+flag,type=Path,required=True)
    p.add_argument("--rom-dir",type=Path,
                   help="read-only folder of candidate ROM files; "
                        "defaults to the directory of --rom")
    p.add_argument("--firmware-dir",type=Path,
                   help="read-only folder of candidate firmware images; "
                        "defaults to the directory of --flash")
    p.add_argument("--persistent-media-dir", type=Path,
                   help="opt-in R/W guest flash.bin and sd.img directory on "
                        "a separately mounted persistent volume; refuses partial "
                        "or wrong-sized saved media")
    p.add_argument("--host",default="127.0.0.1")
    p.add_argument("--port",type=int,default=8784)
    p.add_argument("--enable-power-button",action="store_true",
                   help="experimental X4 Pro Power GPIO3 input; requires "
                        "patched QEMU and private/trusted localhost")
    args=p.parse_args()
    for attr in ("qemu_bin","rom","flash","sd_image"):
        setattr(args,attr,getattr(args,attr).resolve(strict=True))
    rom_dir=(args.rom_dir or args.rom.parent).resolve()
    if not rom_dir.is_dir():
        p.error(f"--rom-dir {rom_dir} is not an existing directory")
    args.rom_dir=rom_dir
    if args.persistent_media_dir is not None:
        args.persistent_media_dir = args.persistent_media_dir.resolve(strict=True)
        if not args.persistent_media_dir.is_dir():
            p.error("--persistent-media-dir must be an existing directory")
    firmware_dir=(args.firmware_dir or args.flash.parent).resolve()
    if not firmware_dir.is_dir():
        p.error(f"--firmware-dir {firmware_dir} is not an existing directory")
    args.firmware_dir=firmware_dir
    emu=FactoryX4Pro(args)
    try:
        server=ThreadingHTTPServer((args.host,args.port),make_handler(emu))
        if args.host not in ("127.0.0.1","::1","localhost"):
            print("WARNING: this HTTP endpoint has NO authentication; only "
                  "launch it behind start_x4pro_browser.sh (private bind "
                  "check) or another protected network.",flush=True)
        print("YBX X4 Pro original factory QEMU browser listening on "
              f"http://{args.host}:{args.port}",flush=True)
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        emu.close()

if __name__=="__main__": main()
