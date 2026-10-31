#!/usr/bin/env python3
"""Browser-driven REAL original X4 Pro factory guest + opt-in QEMU SSD1677.

No firmware writes, invented pixels or direct semaphore injection.
Docker port MUST bind localhost on the host unless separately protected.
"""
import argparse
import email.utils
import hashlib
import json
import shutil
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


class BadRequestError(ValueError):
    """Client sent something the bridge cannot act on. Maps to HTTP 400."""
    kind = "bad-request"


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
small{color:#66665e;display:block;margin-top:11px;line-height:1.6}
@media(max-width:680px){body{align-items:start}main{display:flex;flex-direction:column;align-items:center;padding:12px;gap:16px}.device{padding:12px;width:min(100%,480px)}.details{width:min(100%,480px)}}
</style></head><body><main>
<div class="device"><img id="screen" alt="Real SSD1677 framebuffer, YBX X4 Pro"
draggable="false" /></div>
<div class="details"><h1>YBX X4 Pro <small>Original factory firmware · QEMU ESP32-S3</small></h1>
<p>The display is the <strong>real guest-written SSD1677 480×800 RAM</strong>,
not an HTML mockup. Tap directly on the reader screen to send a
150 ms GT911 touch to the virtual device.</p>
<div class="status" id="status">Connecting to QEMU…</div>
<p id="info">The original firmware initializes the panel before the home screen appears.</p>
<p id="metrics"><small>BUSY telemetry: loading…</small></p>
<button id="refresh">Refresh display</button>
<button id="reboot">Restart virtual reader</button>
<p><strong>Wi-Fi radio is not emulated.</strong> Join a Network,
Calibre Wireless and Create Hotspot cannot connect from this factory guest.
Attempting network setup may stop its interface responding; use
<strong>Restart virtual reader</strong> to reboot the virtual device.
If the emulator process itself stopped, the same button starts a fresh guest.</p>
<small>Local developer preview. Some features (Wi-Fi, hardware timing,
real E-Ink waveform and other panel variants) remain unimplemented.
One virtual device per server. No physical reader is attached.</small>
</div></main>
<script>
(function(){
const img=document.getElementById('screen'),status=document.getElementById('status');
const info=document.getElementById('info'),metrics=document.getElementById('metrics');
const refresh=document.getElementById('refresh'),reboot=document.getElementById('reboot');
let ready=false,lastFrame=-1,busy=false,ticks=0;
async function poll(){
 try{
   const response=await fetch('/api/state',{cache:'no-store'});
   const s=await response.json();
   if(!response.ok)throw new Error(s.error||'QEMU read failed');
   ready=Boolean(s.ready);
   status.textContent=ready?'Ready — tap the display':'Booting factory firmware…';
   info.textContent='Display updates: '+s.frames+' · Nonwhite bytes: '+s.nonwhite+
     (s.sha256?' · Frame SHA-256: '+s.sha256.slice(0,12)+'…':'');
   if(s.frames!==lastFrame){
     lastFrame=s.frames;img.src='/screen.png?frame='+s.frames;
   }
   if(++ticks%3===0)showTelemetry();
 }catch(e){ready=false;status.textContent='Emulator unavailable: '+e.message}
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
 busy=true;reboot.disabled=true;status.textContent='Restarting virtual device…';
 try{
   const response=await fetch('/api/reboot',{method:'POST'});
   const result=await response.json();
   if(!response.ok)throw new Error(result.error||'QEMU reset failed');
   lastFrame=-1;ready=false;img.removeAttribute('src');
 }catch(e){status.textContent='Restart failed: '+e.message}
 finally{busy=false;reboot.disabled=false;poll()}
});
poll();setInterval(poll,1000);
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


class FactoryX4Pro:
    tap_wait_seconds = 2.0
    tap_queue_depth = 4
    QOM_PATH = "/machine/soc"
    TELEMETRY_COUNTERS = ("busy-rise-edges", "busy-fall-edges",
                          "busy-high-ms", "frames")

    def __init__(self, args):
        self.temp = TemporaryDirectory(prefix="ybx-x4pro-browser-")
        self.workdir = Path(self.temp.name)
        t = self.workdir
        (t / "rom").mkdir()
        shutil.copyfile(args.rom, t / "rom" / "esp32s3_rev0_rom.bin")
        shutil.copyfile(args.flash, t / "flash.bin")
        shutil.copyfile(args.sd_image, t / "sd.img")
        self.qemu_bin = args.qemu_bin
        self.lock = threading.RLock()
        self.tap_lock = threading.Lock()
        self.tap_slots = threading.BoundedSemaphore(self.tap_queue_depth)
        self.process = None
        self.client = None
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
            "-serial","null","-serial","null","-serial","null","-serial","null",
            "-qmp",f"unix:{t/'qmp.sock'},server=on,wait=off",
            "-L",str(t/"rom"),
            "-drive",f"file={t/'flash.bin'},if=mtd,format=raw",
            "-drive",f"file={t/'sd.img'},if=sd,format=raw"
        ], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
           stderr=self.qemu_log)
        try:
            self.client, self.reader, self.qmp = qmp_connect(sock)
        except Exception:
            self._stop_process()
            raise

    def _stop_process(self):
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try: self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()

    def _relaunch(self):
        """Replace a dead guest process inside the same server session."""
        self._stop_process()
        self.client.close()
        self._launch()
        self.frame = -1
        self.sha = ""
        self.png = self.blank_png
        self.png_etag = '"blank"'
        self.png_mtime = time.time()

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
            return {"frames":frames,"monotonic_ms":int(time.monotonic()*1000),
                    "nonwhite":self.nonwhite,
                    "ready":guest_ready_for_touch(frames),
                    "sha256":self.sha,"factory_guest":True,
                    "physical_device":False}

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
                    if "QMP closed" not in str(exc):
                        raise
                    self._relaunch()
                    return
                try:
                    result = self.qmp("system_reset")
                except (OSError, RuntimeError, ValueError):
                    self._relaunch()
                    return
                if "return" not in result:
                    raise RuntimeError(str(result.get("error", result)))
                self.frame = -1
                self.sha = ""
                self.png = self.blank_png
                self.png_etag = '"blank"'
                self.png_mtime = time.time()
        finally:
            self._end_action()

    def close(self):
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
                elif path == "/screen.png":
                    self.serve_screen()
                else:
                    self.response_json(404,{"error":"Not found"})
            except GuestError as exc:
                self.guest_failure(exc)
            except Exception as exc:
                self.response_json(503,{"error":str(exc)})

        def do_POST(self):
            if self.path=="/api/reboot":
                try:
                    emu.reboot()
                    self.response_json(200,{"accepted":True,"action":"QEMU system_reset"})
                except GuestError as exc:
                    self.guest_failure(exc)
                except (ValueError,RuntimeError,OSError) as exc:
                    self.response_json(503,{"error":str(exc)})
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
    p.add_argument("--host",default="127.0.0.1")
    p.add_argument("--port",type=int,default=8784)
    args=p.parse_args()
    for attr in ("qemu_bin","rom","flash","sd_image"):
        setattr(args,attr,getattr(args,attr).resolve(strict=True))
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
