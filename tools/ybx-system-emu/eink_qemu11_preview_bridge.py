#!/usr/bin/env python3
"""Isolated QEMU 11.1.2 X4 Pro preview: QMP screenshot/touch and read-only UART.

This is an EXPERIMENTAL alternative to the stable 8784 browser, NOT a drop-in
replacement for its old ybx-* QOM/SSD1677 API. Loopback-only by design.
No guest network, firmware bytes or hotkeys are modified by this bridge.
"""
import argparse
import hashlib
import json
import re
import socket
import struct
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit
import zlib

PANEL_WIDTH, PANEL_HEIGHT = 480, 800


def require_loopback(host, port):
    if host != '127.0.0.1' or not (8800 <= port <= 65534) or port == 8784:
        raise ValueError('QEMU 11 preview must bind 127.0.0.1 on a separate port >=8800')


def rgba_compatible_png(ppm):
    """Read QEMU P6 screendump and encode PNG using Python stdlib only."""
    match = re.match(rb'^P6\n([0-9]+) ([0-9]+)\n255\n', ppm)
    if not match:
        raise ValueError('unsupported QEMU PPM header')
    width, height = map(int, match.groups())
    if (width, height) != (PANEL_WIDTH, PANEL_HEIGHT):
        raise ValueError(f'unexpected X4 Pro portrait framebuffer: {width}x{height}')
    rgb = ppm[match.end():]
    if len(rgb) != width * height * 3:
        raise ValueError('truncated or oversized PPM framebuffer')
    raw = b''.join(b'\x00' + rgb[i:i + width * 3]
                   for i in range(0, len(rgb), width * 3))
    def chunk(name, data):
        return struct.pack('>I', len(data)) + name + data + \
            struct.pack('>I', zlib.crc32(name + data) & 0xffffffff)
    png = (b'\x89PNG\r\n\x1a\n' +
           chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0)) +
           chunk(b'IDAT', zlib.compress(raw, 7)) + chunk(b'IEND', b''))
    return png, hashlib.sha256(rgb).hexdigest()


class QMP:
    def __init__(self, host='127.0.0.1', port=8811):
        self.host = host
        self.port = port
        self.lock = threading.RLock()

    def call(self, command, args=None):
        with self.lock, socket.create_connection((self.host, self.port), timeout=8) as sock:
            sock.settimeout(8)
            stream = sock.makefile('rb')
            greeting = json.loads(stream.readline())
            if 'QMP' not in greeting:
                raise RuntimeError('wrong QMP endpoint')
            def send(obj):
                sock.sendall(json.dumps(obj).encode() + b'\n')
                for _ in range(30):
                    result = json.loads(stream.readline())
                    if 'event' in result:
                        continue
                    if 'error' in result:
                        raise RuntimeError(str(result['error']))
                    return result.get('return')
                raise RuntimeError('too many unsolicited QMP events')
            send({'execute': 'qmp_capabilities'})
            request = {'execute': command}
            if args is not None:
                request['arguments'] = args
            return send(request)

    def tap(self, x, y, duration_ms):
        if not (0 <= x < PANEL_WIDTH and 0 <= y < PANEL_HEIGHT):
            raise ValueError('touch outside panel')
        if not (60 <= duration_ms <= 450):
            raise ValueError('touch duration outside 60..450ms')
        events = [{'type': 'abs', 'data': {'axis': axis,
                   'value': round(32767 * value / limit)}}
                  for axis, value, limit in [('x', x, PANEL_WIDTH - 1),
                                             ('y', y, PANEL_HEIGHT - 1)]]
        self.call('input-send-event', {'events': events +
                  [{'type': 'btn', 'data': {'button': 'left', 'down': True}}]})
        try:
            time.sleep(duration_ms / 1000)
        finally:
            self.call('input-send-event', {'events': [
                {'type': 'btn', 'data': {'button': 'left', 'down': False}}]})


INDEX = '''<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>YBX · QEMU 11.1.2 Preview</title>
<style>body{margin:0;background:#e9e9e6;color:#1d1d1d;font:14px system-ui,sans-serif}header{padding:16px 22px;background:#fafaf8;border-bottom:1px solid #ccc;display:flex;gap:16px;justify-content:space-between;align-items:center}h1{font-size:17px;margin:0}small{color:#777}main{display:grid;grid-template-columns:minmax(240px,480px) minmax(240px,420px);gap:24px;justify-content:center;padding:24px}#screen{width:100%;height:auto;image-rendering:auto;box-shadow:0 2px 14px #0002;border:1px solid #bbb;background:#fff;cursor:crosshair;touch-action:manipulation}pre{background:#181818;color:#eee;overflow:auto;white-space:pre-wrap;max-height:65vh;padding:15px;border-radius:6px;font:11px ui-monospace,monospace}section{min-width:0}.notice{padding:12px;background:#fff;border:1px solid #ccc;margin-bottom:12px;line-height:1.55}button{background:#222;color:#fff;padding:7px 14px;border:0;border-radius:5px;cursor:pointer}#info{font-family:ui-monospace,monospace;font-size:12px;line-height:2;overflow-wrap:anywhere}@media(max-width:750px){main{grid-template-columns:minmax(250px,480px);padding:12px;gap:14px}}</style>
<header><div><h1>YBX / QEMU 11.1.2 · X4 Pro</h1><small>eink-qemu 0.9.2 • experimental • original firmware</small></div><button onclick="refresh()">Refresh</button></header>
<main><section><img id="screen" width="480" height="800" alt="X4 Pro native QEMU screen"><div id="info">Connecting to QMP…</div></section><section><div class="notice"><b>Isolated research environment.</b><div>Click the display to send a genuine GT911 touch. The production 8784, its firmware and saved SD are untouched. Native Wi-Fi MAC/NAT is available only inside this experimental QEMU.</div></div><h2>Firmware UART</h2><pre id="uart">Loading…</pre></section></main>
<script>
let busy=false,seq=0;const panel=document.querySelector('#screen');
async function refresh(){if(busy)return;busy=true;try{let [state,log]=await Promise.all([fetch('/api/state'),fetch('/api/uart?tail=5000')]);let s=await state.json(),l=await log.json();if(!state.ok)throw Error(s.error);document.querySelector('#info').textContent=`QMP: ${s.qmp_status} · boot: ${s.home_log_observed?'Home reached':'waiting'} · ${s.frame_sha256?.slice(0,16)||'no frame'}`;document.querySelector('#uart').textContent=l.tail;panel.src='/screen.png?v='+(++seq)}catch(e){document.querySelector('#info').textContent='Error: '+e.message}finally{busy=false}}
panel.addEventListener('click',async e=>{let r=panel.getBoundingClientRect(),x=Math.min(479,Math.max(0,Math.floor((e.clientX-r.left)*480/r.width))),y=Math.min(799,Math.max(0,Math.floor((e.clientY-r.top)*800/r.height)));try{let res=await fetch('/api/tap',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({x,y,duration_ms:170})});if(!res.ok)throw Error(await res.text());setTimeout(refresh,600)}catch(err){document.querySelector('#info').textContent=err.message}});
refresh();setInterval(refresh,3000);
</script></html>'''


class Backend:
    def __init__(self, workdir, qmp):
        self.workdir = Path(workdir)
        self.qmp = qmp
        self.frame_lock = threading.RLock()
        self.frame_sha256 = None

    def screenshot(self):
        with self.frame_lock:
            self.qmp.call('human-monitor-command', {
                'command-line': 'screendump /out/preview-screen.ppm'})
            payload = (self.workdir / 'preview-screen.ppm').read_bytes()
            png, sha = rgba_compatible_png(payload)
            self.frame_sha256 = sha
            return png

    def uart(self, max_bytes=5000):
        tail = (self.workdir / 'uart.log').read_bytes()[-max_bytes:]
        return tail.decode('utf-8', errors='replace')

    def state(self):
        status = self.qmp.call('query-status')
        log = self.uart(30000)
        return {'ready': status['running'], 'qmp_status': status['status'],
                'home_log_observed': ('Entering activity: Home' in log or
                                      '[MEM] Free:' in log),
                'frame_sha256': self.frame_sha256,
                'firmware_model': 'original X4 Pro, external 11.1.2 QEMU',
                'radio_model': 'ESP32-S3 MAC + slirp user NAT (opt-in guest)',
                'radio_connected': None,
                'experimental': True}


def serve(backend, host='127.0.0.1', port=8810):
    require_loopback(host, port)
    class Handler(BaseHTTPRequestHandler):
        def respond(self, status, data, mime='application/json'):
            self.send_response(status)
            self.send_header('Content-Type', mime)
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            try:
                route = urlsplit(self.path)
                if route.path == '/':
                    self.respond(200, INDEX.encode(), 'text/html; charset=utf-8')
                elif route.path == '/screen.png':
                    self.respond(200, backend.screenshot(), 'image/png')
                elif route.path == '/api/state':
                    self.respond(200, json.dumps(backend.state()).encode())
                elif route.path == '/api/uart':
                    self.respond(200, json.dumps({'tail': backend.uart(5000)}).encode())
                else:
                    self.respond(404, b'{"error":"not found"}')
            except Exception as e:
                self.respond(503, json.dumps({'error': str(e)}).encode())

        def do_POST(self):
            if self.path != '/api/tap':
                self.respond(404, b'{"error":"not found"}')
                return
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= 512:
                    raise ValueError('invalid request size')
                data = json.loads(self.rfile.read(size))
                if type(data.get('x')) is not int or type(data.get('y')) is not int:
                    raise ValueError('coordinates must be integers')
                duration = data.get('duration_ms', 170)
                if type(duration) is not int:
                    raise ValueError('duration must be integer')
                backend.qmp.tap(data['x'], data['y'], duration)
                self.respond(200, b'{"accepted":true}')
            except (ValueError, KeyError) as e:
                self.respond(400, json.dumps({'error': str(e)}).encode())
            except Exception as e:
                self.respond(503, json.dumps({'error': str(e)}).encode())

    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f'Native eink-qemu 11.1.2 preview: http://{host}:{port}/', flush=True)
    httpd.serve_forever()


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--workdir', type=Path, required=True)
    p.add_argument('--port', type=int, default=8810)
    p.add_argument('--qmp-port', type=int, default=8811)
    p.add_argument('--bind', default='127.0.0.1')
    args = p.parse_args()
    require_loopback(args.bind, args.port)
    require_loopback(args.bind, args.qmp_port)
    serve(Backend(args.workdir, QMP(port=args.qmp_port)), args.bind, args.port)
