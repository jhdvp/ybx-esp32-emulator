#!/usr/bin/env python3
"""Expose the guest firmware web UI (File Transfer mode, guest :18080 inside the container)
on a host loopback port, by relaying each request through `docker exec`.

    python3 stock1614-guest-web-proxy.py --container ybx-eink-qemu11-8971 --port 8974
    open http://127.0.0.1:8974/

Loopback only; for isolated canary containers only.
"""
import argparse
import base64
import json
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

RELAY = r'''
import base64,json,sys,urllib.request,urllib.error
r=json.loads(sys.stdin.read())
req=urllib.request.Request('http://127.0.0.1:18080'+r['path'],data=base64.b64decode(r['body']) if r['body'] else None,method=r['method'],headers=r['headers'])
try:
    resp=urllib.request.urlopen(req,timeout=60); code=resp.status; hdrs=dict(resp.headers); body=resp.read()
except urllib.error.HTTPError as e:
    code=e.code; hdrs=dict(e.headers); body=e.read()
print(json.dumps({'code':code,'headers':hdrs,'body':base64.b64encode(body).decode()}))
'''
PASS_HEADERS = ("content-type", "accept", "x-requested-with")
DROP_RESPONSE = ("transfer-encoding", "connection", "content-length", "content-encoding")


def make_handler(container):
    class Handler(BaseHTTPRequestHandler):
        def relay(self):
            size = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(size) if size else b""
            payload = json.dumps({
                "path": self.path, "method": self.command,
                "headers": {k: v for k, v in self.headers.items() if k.lower() in PASS_HEADERS},
                "body": base64.b64encode(body).decode()})
            try:
                out = subprocess.run(["docker", "exec", "-i", container, "python3", "-c", RELAY],
                                     input=payload, capture_output=True, text=True, timeout=90)
                r = json.loads(out.stdout)
            except Exception as e:  # guest web server down or not in File Transfer
                msg = ("guest web UI unreachable (%s). On the guest: File Transfer -> Join a Network."
                       % type(e).__name__).encode()
                self.send_response(502); self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(msg))); self.end_headers(); self.wfile.write(msg)
                return
            data = base64.b64decode(r["body"])
            self.send_response(r["code"])
            for k, v in r["headers"].items():
                if k.lower() not in DROP_RESPONSE:
                    self.send_header(k, v)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        do_GET = do_POST = do_PUT = do_DELETE = relay

        def log_message(self, fmt, *args):
            print("%s %s" % (self.command, self.path.split("?")[0]), flush=True)
    return Handler


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--container", required=True)
    ap.add_argument("--port", type=int, default=8974)
    a = ap.parse_args()
    if a.container in ("ybx-eink-qemu11-8950", "ybx-eink-qemu11-8964"):
        raise SystemExit("that container belongs to another run")
    print("guest web UI: http://127.0.0.1:%d/" % a.port, flush=True)
    ThreadingHTTPServer(("127.0.0.1", a.port), make_handler(a.container)).serve_forever()


if __name__ == "__main__":
    main()
