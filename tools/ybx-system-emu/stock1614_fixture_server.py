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


def reader_clipping(item, revision):
    """Mirror the *production* clippings.reader_item / ClippingSync decoder.

    Do not return the raw PUT body as a GET item: the reader requires a
    monotonic revision plus numeric deleted and bounded integer fields.
    """
    if not isinstance(item, dict):
        raise ValueError("clipping must be an object")
    identifier=item.get("id")
    if not (isinstance(identifier,str) and 1 <= len(identifier) <= 64 and
            all("!" <= c <= "~" for c in identifier)):
        raise ValueError("invalid clipping id")
    deleted=item.get("deleted",0)
    if type(deleted) is not int or deleted not in (0,1):
        raise ValueError("invalid deleted flag")
    row={"id":identifier,"deleted":deleted,"revision":revision}
    if deleted:
        return row
    fields16=("spine","start_page","end_page","pages","start_word","end_word","words")
    fields32=("created_at","layout_signature")
    for field in fields16+fields32:
        value=item.get(field)
        upper=65535 if field in fields16 else 4294967295
        if type(value) is not int or not 0 <= value <= upper:
            raise ValueError("missing or invalid " + field)
        row[field]=value
    paragraph=item.get("para")
    if paragraph is not None:
        if type(paragraph) is not int or not 0 <= paragraph <= 65535:
            raise ValueError("invalid paragraph")
        row["para"]=paragraph
    quote=item.get("text")
    if not (isinstance(quote,str) and 0 < len(quote.encode("utf-8")) <= 4096
            and "\x00" not in quote):
        raise ValueError("invalid text")
    row["text"]=quote
    chapter=item.get("chapter","")
    if not isinstance(chapter,str):
        raise ValueError("invalid chapter")
    row["chapter"]=chapter
    start,end=item.get("start_offset"),item.get("end_offset")
    if (start is None) != (end is None):
        raise ValueError("unpaired offsets")
    if start is not None:
        if not (type(start) is int and type(end) is int and
                0 <= start < end < 4294967295):
            raise ValueError("invalid offsets")
        row["start_offset"],row["end_offset"]=start,end
    return row


def seed_from_ledger(path):
    """Only synthetic E2E data; infer the document from the last PUT route."""
    ledger=json.loads(Path(path).read_text())
    requests=[event for event in ledger.get("requests",[]) if
              event.get("kind")=="clippings-put" and
              event.get("path","").startswith("/kosync/api/v1/clippings/")]
    items=ledger.get("last_clippings",[])
    if not requests or not items:
        raise ValueError("seed ledger lacks a clipping PUT and item")
    document=requests[-1]["path"].rsplit("/",1)[-1]
    if not re.fullmatch(r"[0-9a-f]{32}",document):
        raise ValueError("unrecognized fixture document")
    return {document:items}


def make_server(port:int, epub:Path, evidence:Path, seed_clippings=None):
    events=[]
    accounts={"device_codes":0,"polls":0,"auth_success":0}
    data={"books": [], "last_clippings": [], "last_progress": None}
    # Separate book histories keyed by document hash, with stable revisions.
    # Seeded rows reproduce a remote account surviving a fixture restart.
    histories={}
    for document, items in (seed_clippings or {}).items():
        existing={}
        for item in items:
            row=reader_clipping(item,len(existing)+1)
            existing[row["id"]]=row
        histories[document]=existing
    lock=Lock()
    evidence.parent.mkdir(parents=True,exist_ok=True)

    def record(method,path,status,kind="",safe_query=None):
        # No raw auth headers or secrets in the log. Only log numeric
        # Clippings pagination parameters; never log arbitrary URL queries.
        with lock:
            event={"method":method,"path":path,"status":status,"kind":kind}
            if safe_query: event["query"]=safe_query
            events.append(event)
            # Atomically publish each ledger snapshot. A CI test or a GUI
            # reader must never observe a truncated JSON file mid-write.
            snapshot = json.dumps({
                "requests":events,"accounts":accounts,
                "uploads":data["books"],
                "last_progress":data["last_progress"],
                "last_clippings":data["last_clippings"],
                "remote_clippings":histories
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
            uri=urlsplit(self.path)
            pagination={}
            if "/api/v1/clippings/" in uri.path:
                params=parse_qs(uri.query)
                pagination={key:params[key][0] for key in ("cursor","limit","format")
                            if key in params and len(params[key])==1}
            record(self.command,uri.path,status,kind,pagination)
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
                    document=path.rsplit("/",1)[-1]
                    try:
                        cursor=int(query.get("cursor",[0])[0])
                        limit=int(query.get("limit",[1])[0])
                        if cursor < 0 or limit < 1:
                            raise ValueError()
                    except ValueError:
                        self.reply(400,{"error":"invalid_pagination"},"clippings-invalid")
                        return
                    cap=1 if query.get("format",["reader"])[0]=="reader" else min(100,limit)
                    with lock:
                        candidates=sorted((r.copy() for r in histories.get(document,{}).values()
                                           if r["revision"]>cursor),
                                          key=lambda r:(r["revision"],r["id"]))
                    chosen=candidates[:cap]
                    next_cursor=chosen[-1]["revision"] if chosen else cursor
                    self.reply(200,{"sync_version":2,"items":chosen,
                                    "cursor":next_cursor,"more":len(candidates)>len(chosen)},
                               "clippings")
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
                try:
                    batch=body["items"]
                    if not isinstance(batch,list) or not 1 <= len(batch) <= 1:
                        raise ValueError("one clipping per upload")
                    document=path.rsplit("/",1)[-1]
                    if not re.fullmatch(r"[0-9a-f]{32}",document):
                        raise ValueError("invalid document")
                    with lock:
                        previous=histories.setdefault(document,{})
                        new_revision=max((r["revision"] for r in previous.values()),default=0)+1
                        row=reader_clipping(batch[0],new_revision)
                        prior=previous.get(row["id"])
                        changed=not prior or {k:v for k,v in prior.items() if k!="revision"} != {
                            k:v for k,v in row.items() if k!="revision"}
                        if changed:
                            previous[row["id"]]=row
                        data["last_clippings"]=batch
                        effective_revision=previous[row["id"]]["revision"]
                except (ValueError,KeyError,TypeError):
                    self.reply(400,{"accepted":0,"error":"invalid_clipping"},"clippings-invalid")
                    return
                self.reply(200,{"accepted":1,"applied":int(changed),
                                "revision":effective_revision},"clippings-put")
                return
            self.reply(404,{"error":"no_route"},"missing")

    return ThreadingHTTPServer(("0.0.0.0",port),Handler)


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--port",type=int,default=18085)
    ap.add_argument("--epub",type=Path,default=Path("/out/mock.epub"))
    ap.add_argument("--evidence",type=Path,default=Path("/out/stock1614-fixture-requests.json"))
    ap.add_argument("--seed-ledger",type=Path,
                    help="Optional saved synthetic request ledger from a previous isolated QEMU run")
    args=ap.parse_args()
    assert args.epub.is_file()
    seed=seed_from_ledger(args.seed_ledger) if args.seed_ledger else None
    srv=make_server(args.port,args.epub,args.evidence,seed_clippings=seed)
    print(f"QEMU fake YBX fixture listening on 0.0.0.0:{args.port}",flush=True)
    srv.serve_forever()


if __name__=="__main__":
    main()
