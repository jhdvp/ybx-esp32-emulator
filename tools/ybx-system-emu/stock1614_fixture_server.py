#!/usr/bin/env python3
"""Local-only fake library for genuine Stock CrossPoint firmware in QEMU.

Accessible only as 10.0.2.2 via QEMU slirp from the disposable guest.
No production credentials, no actual YBX account or book rights involved.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
from threading import Lock
from urllib.parse import parse_qs, urlsplit

TOK = "ybx-qemu-fixture-token-ONLY-NOT-REAL"
BOOKS = [
    {"id": "upload:fixture-1", "title": "Fixture EPUB One",
     "author": "E2E Author", "source": "uploaded", "kind": "to_read"},
    *[
        {"id": f"yb-{i:02}", "title": f"Fixture Yandex Book {i:02}",
         "author": "E2E Author", "source": "yandex", "kind": ["reading","to_read","done"][i % 3]}
        for i in range(1, 13)
    ],
    *[
        {"id": f"wb--{i:02}", "title": f"Fixture WB Book {i:02}",
         "author": "E2E Author", "source": "wb", "kind": "to_read"}
        for i in range(1, 4)
    ],
    *[
        {"id": f"litres--{i:02}", "title": f"Fixture LitRes Book {i:02}",
         "author": "E2E Author", "source": "litres", "kind": "done"}
        for i in range(1, 4)
    ],
]
SOURCES={"yandex":"yandex","wb":"wb","litres":"litres"}


def make_server(port:int, epub:Path, evidence:Path):
    events=[]
    accounts={"device_codes":0,"polls":0,"auth_success":0}
    data={"books": [], "last_clippings": [], "last_progress": None}
    lock=Lock()
    evidence.parent.mkdir(parents=True,exist_ok=True)

    def record(method,path,status,kind=""):
        # No raw auth headers, data or user secrets in the log.
        with lock:
            events.append({"method":method,"path":path,"status":status,"kind":kind})
            # Atomically publish each ledger snapshot. A CI test or a GUI
            # reader must never observe a truncated JSON file mid-write.
            snapshot = json.dumps({
                "requests":events,"accounts":accounts,
                "uploads":data["books"],
                "last_progress":data["last_progress"],
                "last_clippings":data["last_clippings"]
            },ensure_ascii=False,indent=2)
            staged = evidence.with_name(evidence.name + ".tmp")
            staged.write_text(snapshot, encoding="utf-8")
            staged.replace(evidence)

    class Handler(BaseHTTPRequestHandler):
        protocol_version="HTTP/1.1"

        def log_message(self,*args):
            pass

        def send(self,status:int,body:bytes,content_type:str,kind=""):
            self.send_response(status)
            self.send_header("Content-Type",content_type)
            self.send_header("Content-Length",str(len(body)))
            self.send_header("Cache-Control","no-store")
            self.end_headers()
            # Complete the evidence record before unblocking the client:
            # otherwise a fast HTTP client can race the file-backed log.
            record(self.command,urlsplit(self.path).path,status,kind)
            self.wfile.write(body)

        def reply(self,status,thing,kind=""):
            self.send(status,json.dumps(thing,ensure_ascii=False).encode("utf-8"),"application/json",kind)

        def body(self):
            n=int(self.headers.get("Content-Length","0"))
            if n>8192: raise ValueError("excessive body")
            return self.rfile.read(n) if n else b""

        def authorised(self):
            return self.headers.get("Authorization")==f"Bearer {TOK}"

        def guard(self):
            if not self.authorised():
                self.reply(401,{"error":"invalid_token"},"rejected")
                return False
            return True

        def do_GET(self):
            url=urlsplit(self.path)
            path=url.path
            query=parse_qs(url.query)
            if path=="/__status":
                self.reply(200,{"request_count":len(events),"accounts":accounts,
                                "recent":events[-40:]}, "status")
                return
            if path=="/health":
                self.reply(200,{"status":"ok","fixture":True},"health")
                return
            if path.endswith("/users/auth") or path.endswith("/syncs/progress"):
                # Separate KoSync headers from CrossPoint Bearer in real API.
                if not self.headers.get("x-auth-user") or not self.headers.get("x-auth-key"):
                    self.reply(401,{"error":"not_authenticated"},"kosync-denied")
                else:
                    self.reply(200,{"document":"","progress":"","percentage":0,"device":""},"kosync")
                return
            if "/api/v1/clippings/" in path:
                if not self.headers.get("x-auth-user") or not self.headers.get("x-auth-key"):
                    self.reply(401,{"error":"not_authenticated"},"clippings-denied")
                else:
                    self.reply(200,{"sync_version":2,"items":data["last_clippings"],
                                    "cursor":len(data["last_clippings"]),"more":False},"clippings")
                return
            if path.startswith("/api/v1/crosspoint/books/") and path.endswith("/download"):
                if not self.guard(): return
                id=path.removeprefix("/api/v1/crosspoint/books/").removesuffix("/download")
                if not any(x["id"]==id for x in BOOKS):
                    self.reply(404,{"error":"unknown_book"},"download-denied");return
                blob=epub.read_bytes()
                self.send(200,blob,"application/epub+zip","epub")
                return
            if path.startswith("/api/v1/crosspoint/catalog") or path.startswith("/api/v1/crosspoint/wb/catalog") or path.startswith("/api/v1/crosspoint/litres/catalog"):
                if not self.guard(): return
                kind=query.get("kind",["all"])[0]
                shelf=query.get("shelf",["all"])[0]
                needle=query.get("q",[""])[0].casefold()
                try: page,max_items=int(query.get("page",[1])[0]),int(query.get("limit",[11])[0])
                except ValueError: self.reply(400,{"error":"bad_page"});return
                if "/wb/" in path: candidates=[b for b in BOOKS if b["source"]=="wb"]
                elif "/litres/" in path: candidates=[b for b in BOOKS if b["source"]=="litres"]
                elif path.endswith("/catalog"): candidates=[b for b in BOOKS if b["source"]=="yandex"]
                else:
                    candidates=[b for b in BOOKS if (kind=="all" or
                        (kind=="uploaded" and b["source"]=="uploaded") or kind==b["kind"])]
                if shelf not in ("all","reading","done","to_read"):
                    candidates=[]
                elif shelf!="all": candidates=[b for b in candidates if b["kind"]==shelf]
                if needle: candidates=[b for b in candidates if needle in (b["title"]+" "+b["author"]).casefold()]
                start=(page-1)*(max_items-1)
                selected=candidates[start:start+max_items]
                origin=f"http://10.0.2.2:{port}"
                rows=[{"id":b["id"],"title":b["title"],"author":b["author"],
                       "description":"OFFLINE TEST FIXTURE – no real books",
                       "download_url":f"{origin}/api/v1/crosspoint/books/{b['id']}/download"} for b in selected]
                self.reply(200,{"items":rows},"catalog")
                return
            self.reply(404,{"error":"no_route"},"missing")

        def do_POST(self):
            path=urlsplit(self.path).path
            if path=="/api/v1/crosspoint/auth/device":
                self.body()
                with lock:accounts["device_codes"]+=1
                self.reply(200,{"device_code":"OFFLINE-FIXTURE-CODE",
                                "user_code":"DEMO-1234",
                                "verification_uri":"https://example.invalid/fixture",
                                "verification_uri_complete":"https://example.invalid/fixture?code=DEMO",
                                "interval":3,"expires_in":86400},"device-code")
                return
            if path=="/api/v1/crosspoint/auth/token":
                self.body()
                with lock: accounts["polls"]+=1;accounts["auth_success"]+=1
                self.reply(200,{"access_token":TOK,"token_type":"Bearer",
                                "expires_in":86400},"device-token")
                return
            if path=="/api/v1/crosspoint/events":
                if not self.guard(): return
                blob=self.body()
                try:event=json.loads(blob)
                except (ValueError,UnicodeDecodeError): self.reply(400,{"error":"bad_event"});return
                with lock:data["books"].append({"event":event.get("event"),"book_id":event.get("book_id")})
                self.reply(200,{"ok":True},"event")
                return
            self.reply(404,{"error":"no_route"},"missing")

        def do_PUT(self):
            path=urlsplit(self.path).path
            if path.endswith("/syncs/progress"):
                if not self.headers.get("x-auth-key"):self.reply(401,{"error":"not_authenticated"});return
                try:body=json.loads(self.body())
                except(ValueError,UnicodeDecodeError):self.reply(400,{"error":"bad_progress"});return
                with lock:data["last_progress"]={k:body.get(k) for k in ("document","progress","percentage")}
                self.reply(200,{"ok":True},"progress-put")
                return
            if "/api/v1/clippings/" in path:
                if not self.headers.get("x-auth-key"): self.reply(401,{"error":"not_authenticated"});return
                try:body=json.loads(self.body())
                except(ValueError,UnicodeDecodeError):self.reply(400,{"error":"bad_clipping"});return
                with lock:data["last_clippings"]=body.get("items",[])
                self.reply(200,{"sync_version":2,"accepted":len(data["last_clippings"])},"clippings-put")
                return
            self.reply(404,{"error":"no_route"},"missing")

    return ThreadingHTTPServer(("0.0.0.0",port),Handler)


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--port",type=int,default=18085)
    ap.add_argument("--epub",type=Path,default=Path("/out/mock.epub"))
    ap.add_argument("--evidence",type=Path,default=Path("/out/stock1614-fixture-requests.json"))
    args=ap.parse_args()
    assert args.epub.is_file()
    srv=make_server(args.port,args.epub,args.evidence)
    print(f"QEMU fake YBX fixture listening on 0.0.0.0:{args.port}",flush=True)
    srv.serve_forever()


if __name__=="__main__":
    main()
