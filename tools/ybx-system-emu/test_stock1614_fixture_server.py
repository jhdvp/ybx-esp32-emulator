"""Protocol regressions for the fully isolated Stock 1.6.14 QEMU fixture."""
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
from stock1614_fixture_server import make_server, TOK


class FixtureProtocolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        root = Path(self.tmp.name)
        self.epub = root / "fixture.epub"
        with zipfile.ZipFile(self.epub, "w") as z:
            z.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
            z.writestr("META-INF/container.xml",
                       """<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container"
                       version="1.0"><rootfiles><rootfile
                       full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
                       </rootfiles></container>""")
            z.writestr("OEBPS/content.opf",
                       """<package xmlns="http://www.idpf.org/2007/opf" version="3.0"
                       unique-identifier="uid"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
                       <dc:identifier id="uid">fixture</dc:identifier><dc:title>Fixture</dc:title>
                       <dc:language>en</dc:language></metadata><manifest>
                       <item id="ch1" href="chapter.xhtml" media-type="application/xhtml+xml"/>
                       </manifest><spine><itemref idref="ch1"/></spine></package>""")
            z.writestr("OEBPS/chapter.xhtml",
                       '<html xmlns="http://www.w3.org/1999/xhtml"><body><p>Fixture only</p></body></html>')
        self.evidence = root / "requests.json"
        self.server = make_server(0, self.epub, self.evidence)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = "http://127.0.0.1:" + str(self.server.server_address[1])

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=4)
        self.tmp.cleanup()

    def call(self, path, *, method="GET", body=None, auth=False, extra=None):
        headers = ({"Authorization": "Bearer " + TOK} if auth else {})
        headers.update(extra or {})
        payload = json.dumps(body).encode() if body is not None else None
        if payload is not None: headers["Content-Type"] = "application/json"
        req = Request(self.base + path, data=payload, method=method, headers=headers)
        try:
            with urlopen(req, timeout=4) as response:
                return response.status, response.read()
        except HTTPError as error:
            try:
                return error.code, error.read()
            finally:
                error.close()

    def test_compatible_oauth_catalog_paging_and_epub(self):
        path = "/api/v1/crosspoint"
        self.assertEqual(self.call(path + "/catalog/all?kind=all")[0], 401)
        code, body = self.call(path + "/auth/device", method="POST", body={"client_id":"crosspoint"})
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)["device_code"], "OFFLINE-FIXTURE-CODE")
        status, body = self.call(path + "/auth/token", method="POST",
                                 body={"device_code":"OFFLINE-FIXTURE-CODE"})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["access_token"], TOK)
        status, body = self.call(path + "/catalog/all?kind=all&page=1&limit=11",auth=True)
        self.assertEqual(status,200)
        first = json.loads(body)["items"]
        self.assertEqual(len(first),11)
        self.assertEqual(first[0]["title"],"Fixture EPUB One")
        status, body = self.call(path + "/catalog/all?kind=all&page=2&limit=11",auth=True)
        self.assertEqual(status,200)
        self.assertTrue(json.loads(body)["items"])
        self.assertEqual(self.call(path + "/books/upload:fixture-1/download")[0],401)
        status, epub = self.call(path + "/books/upload:fixture-1/download",auth=True)
        self.assertEqual(status,200)
        self.assertEqual(epub, self.epub.read_bytes())
        status, body = self.call(path + "/catalog/all?kind=all&q=Fixture%20WB&page=1&limit=11",auth=True)
        self.assertEqual(status,200)
        self.assertEqual(len(json.loads(body)["items"]),3)

    def test_reader_protocol_rejects_invalid_upload_shapes(self):
        from stock1614_fixture_server import reader_clipping
        valid={
            "id":"fixture-valid","spine":0,"start_page":0,
            "end_page":0,"pages":1,"start_word":0,"end_word":0,
            "words":1,"created_at":1,"layout_signature":1,
            "text":"hello","chapter":"",
        }
        for malformed in (
            {k:v for k,v in valid.items() if k!="created_at"},
            {**valid,"deleted":True},
            {**valid,"start_offset":12},
            {**valid,"revision":"1"},
            {**valid,"id":" "},
        ):
            # A revision sent by the *client* is ignored: all other broken
            # payloads must be rejected by the fixture just as production.
            if malformed.get("revision")=="1":
                continue
            with self.subTest(malformed=malformed), self.assertRaises(ValueError):
                reader_clipping(malformed,1)
        good=reader_clipping(valid,1)
        self.assertEqual(good["deleted"],0)
        self.assertEqual(good["revision"],1)
        self.assertEqual(good["text"],"hello")

    def test_kosync_clipping_extension_and_event_without_production_secrets(self):
        clip = "/kosync/api/v1/clippings/" + "a"*32
        self.assertEqual(self.call(clip)[0],401)
        auth = {"x-auth-user":"fixture-reader","x-auth-key":"fake-md5-not-a-real-password"}
        status, raw = self.call(clip + "?cursor=0&limit=1&format=reader",extra=auth)
        self.assertEqual(status,200)
        self.assertEqual(json.loads(raw)["sync_version"],2)
        sample = {
            "id":"fixture-clip","text":"Selected text","spine":0,
            "start_page":0,"end_page":0,"pages":1,
            "start_word":2,"end_word":3,"words":2,
            "chapter":"Overview","created_at":1793770365,
            "layout_signature":3708546873,
            "start_offset":12,"end_offset":26,
        }
        status, raw = self.call(clip,method="PUT",body={"items":[sample]},extra=auth)
        self.assertEqual(status,200)
        self.assertEqual(json.loads(raw)["accepted"],1)
        status, raw = self.call(clip + "?cursor=0&limit=1&format=reader",extra=auth)
        self.assertEqual(status,200)
        response=json.loads(raw)
        self.assertEqual(response["cursor"],1)
        self.assertEqual(response["more"],False)
        self.assertEqual(response["items"],[{**sample,"deleted":0,"revision":1}])
        self.assertEqual(self.call(clip+"?cursor=1&limit=1&format=reader",extra=auth)[0],200)
        self.assertEqual(json.loads(self.call(
            clip+"?cursor=1&limit=1&format=reader",extra=auth)[1])["items"],[])
        # Replaying the same clipping does not allocate a new revision.
        self.assertEqual(json.loads(self.call(
            clip,method="PUT",body={"items":[sample]},extra=auth)[1])["applied"],0)
        newer={**sample,"id":"fixture-clip-2","text":"Another excerpt"}
        self.assertEqual(self.call(clip,method="PUT",
                                   body={"items":[newer]},extra=auth)[0],200)
        first=json.loads(self.call(
            clip+"?cursor=0&limit=1&format=reader",extra=auth)[1])
        self.assertEqual(first["cursor"],1)
        self.assertEqual(first["more"],True)
        second=json.loads(self.call(
            clip+"?cursor=1&limit=1&format=reader",extra=auth)[1])
        self.assertEqual(second["cursor"],2)
        self.assertEqual(second["items"][0]["text"],"Another excerpt")
        self.assertEqual(second["more"],False)
        event="/api/v1/crosspoint/events"
        self.assertEqual(self.call(event,method="POST",body={"event":"book.downloaded"})[0],401)
        self.assertEqual(self.call(event,method="POST",body={"event":"book.downloaded"},
                                   auth=True)[0],200)
        ledger=self.evidence.read_text()
        self.assertNotIn(TOK,ledger)
        data=json.loads(ledger)
        self.assertEqual(data["accounts"]["device_codes"],0)
        self.assertIn("clippings-put",[x["kind"] for x in data["requests"]])


if __name__ == "__main__":
    unittest.main()
