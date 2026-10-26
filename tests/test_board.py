#!/usr/bin/env python3
"""
Tests for agent-board. Standard library only; starts a real server on a
throwaway port and data directory, then drives it over real HTTP.

    python3 tests/test_board.py
"""

import hashlib
import ast
import contextlib
import http.client
import importlib.util
import io
import json
import os
import shutil
import socket
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
from http.server import ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("board", os.path.join(HERE, "..", "board.py"))
board = importlib.util.module_from_spec(spec)
spec.loader.exec_module(board)

KEY = "test_key_123"


class BoardTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="agent-board-test-")
        cls.store = board.Board(os.path.join(cls.tmp, "board.db"))
        handler = board.make_handler(cls.store, KEY, "test-board")
        cls.httpd = board.make_server(("127.0.0.1", 0), handler)
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    # -- helpers ---------------------------------------------------------
    def conn(self):
        return http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)

    def call(self, method, path, key=None, body=None):
        c = self.conn()
        headers = {}
        if key:
            headers["X-Board-Key"] = key
        if body is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(body)
        c.request(method, path, body=body, headers=headers)
        r = c.getresponse()
        raw = r.read().decode()
        c.close()
        try:
            return r.status, json.loads(raw)
        except json.JSONDecodeError:
            return r.status, raw

    # -- tests -----------------------------------------------------------
    def test_health_needs_no_key(self):
        status, data = self.call("GET", "/api/health")
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])

    def test_reads_require_the_key(self):
        for key in (None, "wrong"):
            status, _ = self.call("GET", "/api/messages", key=key)
            self.assertEqual(status, 401, "key=%r should be rejected" % key)

    def test_writes_require_the_key(self):
        for key in (None, "wrong"):
            status, _ = self.call("POST", "/api/messages", key=key,
                                  body={"author": "x", "text": "y"})
            self.assertEqual(status, 401, "key=%r should be rejected" % key)

    def test_post_and_read_round_trip(self):
        status, data = self.call("POST", "/api/messages", key=KEY,
                                 body={"author": "tester", "topic": "t", "text": "hello"})
        self.assertEqual(status, 201)
        mid = data["id"]
        status, data = self.call("GET", "/api/messages?since=%d" % (mid - 1), key=KEY)
        self.assertEqual(status, 200)
        self.assertEqual(data["messages"][0]["text"], "hello")

    def test_rejects_empty_and_malformed(self):
        status, _ = self.call("POST", "/api/messages", key=KEY, body={"author": "x", "text": "  "})
        self.assertEqual(status, 400)
        c = self.conn()
        c.request("POST", "/api/messages", body="not json",
                  headers={"Content-Type": "application/json", "X-Board-Key": KEY})
        self.assertEqual(c.getresponse().status, 400)
        c.close()

    def test_rejects_overlong_text_and_huge_body(self):
        status, _ = self.call("POST", "/api/messages", key=KEY,
                              body={"author": "x", "text": "a" * (board.MAX_TEXT + 1)})
        self.assertEqual(status, 413)
        # The server rejects an oversized Content-Length before reading bytes.
        # Sending the body races that deliberate close and can reset sendall()
        # before http.client gets to read the 413 response.
        c = self.conn()
        try:
            c.request("POST", "/api/messages", headers={
                "X-Board-Key": KEY, "Content-Length": str(board.MAX_BODY + 10)})
            response = c.getresponse()
            self.assertEqual(response.status, 413)
            self.assertEqual(response.getheader("Connection"), "close")
            response.read()
        finally:
            c.close()

    def test_rejected_post_does_not_corrupt_the_next_request(self):
        """A 401 must not leave the body in the socket: connection reuse would
        read those bytes as the next request and fail it with a spurious 400."""
        c = self.conn()
        for key, want in ((("bad"), 401), (KEY, 201), (None, 401), (KEY, 201)):
            headers = {"Content-Type": "application/json"}
            if key:
                headers["X-Board-Key"] = key
            c.request("POST", "/api/messages", body=json.dumps({"author": "t", "text": "x"}),
                      headers=headers)
            r = c.getresponse()
            got = r.status
            r.read()          # must drain, or http.client refuses the next request
            self.assertEqual(got, want, "expected %s, got %s after a rejection" % (want, got))
        c.request("GET", "/api/messages?limit=1", headers={"X-Board-Key": KEY})
        r = c.getresponse()
        self.assertEqual(r.status, 200)
        r.read()
        c.close()

    def test_long_poll_wakes_on_post(self):
        since = self.call("GET", "/api/health")[1]["latest_id"]
        result = {}

        def waiter():
            status, data = self.call("GET", "/api/messages?since=%d&wait=20" % since, key=KEY)
            result["status"] = status
            result["ids"] = [m["id"] for m in data["messages"]]

        t = threading.Thread(target=waiter)
        t.start()
        time.sleep(0.5)
        mid = self.call("POST", "/api/messages", key=KEY,
                        body={"author": "waker", "text": "wake up"})[1]["id"]
        t.join(timeout=15)
        self.assertFalse(t.is_alive(), "long poll did not return after a post")
        self.assertEqual(result["ids"], [mid])

    def test_long_poll_times_out_empty(self):
        since = self.call("GET", "/api/health")[1]["latest_id"]
        started = time.time()
        status, data = self.call("GET", "/api/messages?since=%d&wait=2" % since, key=KEY)
        self.assertEqual(status, 200)
        self.assertEqual(data["messages"], [])
        self.assertGreaterEqual(time.time() - started, 1.5)

    def test_filters_and_markdown(self):
        self.call("POST", "/api/messages", key=KEY,
                  body={"author": "filterer", "topic": "zzz", "text": "needle"})
        status, data = self.call("GET", "/api/messages?topic=zzz", key=KEY)
        self.assertTrue(data["messages"])
        status, data = self.call("GET", "/api/messages?author=filterer", key=KEY)
        self.assertTrue(data["messages"])
        status, data = self.call("GET", "/api/messages?search=needle", key=KEY)
        self.assertTrue(data["messages"])
        c = self.conn()
        c.request("GET", "/api/messages.md?limit=1", headers={"X-Board-Key": KEY})
        r = c.getresponse()
        self.assertEqual(r.status, 200)
        self.assertIn("agent-board transcript", r.read().decode())
        c.close()

    def test_unknown_path_is_404(self):
        self.assertEqual(self.call("GET", "/nope", key=KEY)[0], 404)
        self.assertEqual(self.call("POST", "/nope", key=KEY, body={"text": "x"})[0], 404)

    # -- files -----------------------------------------------------------

    def upload(self, name, blob, ctype="text/plain", key=KEY):
        c = self.conn()
        headers = {"Content-Type": ctype} if ctype else {}
        if key:
            headers["X-Board-Key"] = key
        c.request("POST", "/api/files?name=" + board.urllib.parse.quote(name),
                  body=blob, headers=headers)
        r = c.getresponse()
        raw = r.read()
        c.close()
        try:
            return r.status, json.loads(raw)
        except ValueError:
            return r.status, raw.decode("utf-8", "replace")

    def raw_get(self, path, key=KEY):
        c = self.conn()
        headers = {"X-Board-Key": key} if key else {}
        c.request("GET", path, headers=headers)
        r = c.getresponse()
        blob = r.read()
        c.close()
        return r.status, blob, dict(r.getheaders())

    def test_upload_requires_the_key(self):
        for key in (None, "wrong"):
            status, _ = self.upload("x.txt", b"secret", key=key)
            self.assertEqual(status, 401, "upload with key=%r should be rejected" % key)

    def test_upload_download_round_trip(self):
        blob = b"decide | typesafe/jev | 412ms\n  route  choice  billing  p=0.93\n"
        status, rec = self.upload("transcript.txt", blob)
        self.assertEqual(status, 201)
        self.assertEqual(rec["file"]["size"], len(blob))
        self.assertEqual(rec["file"]["sha256"], hashlib.sha256(blob).hexdigest())
        status, got, headers = self.raw_get("/api/files/%d" % rec["file"]["id"])
        self.assertEqual(status, 200)
        self.assertEqual(got, blob)
        self.assertEqual(headers.get("X-File-Sha256"), rec["file"]["sha256"])
        self.assertEqual(headers.get("X-Content-Type-Options"), "nosniff")
        self.assertIn("inline", headers.get("Content-Disposition"))

    def test_download_requires_the_key(self):
        _, rec = self.upload("private.txt", b"private")
        self.assertEqual(self.raw_get("/api/files/%d" % rec["file"]["id"], key=None)[0], 401)

    def test_hostile_names_cannot_escape_the_store(self):
        _, rec = self.upload("../../../../etc/passwd", b"root:x:0:0")
        self.assertEqual(rec["file"]["name"], "passwd")
        path = board.Board.__dict__["get_file"](self.store, rec["file"]["id"])["path"]
        files_dir = os.path.realpath(self.store.files_dir)
        self.assertTrue(os.path.realpath(path).startswith(files_dir + os.sep), path)
        self.assertNotIn("passwd", os.path.basename(path), "the client name must not name the blob")
        self.assertEqual(self.raw_get("/api/files/%d" % rec["file"]["id"])[1], b"root:x:0:0")

    def test_html_is_never_served_inline(self):
        status, rec = self.upload("evil.html", b"<script>alert(1)</script>", ctype="text/html")
        self.assertEqual(status, 201)
        _, _, headers = self.raw_get("/api/files/%d" % rec["file"]["id"])
        self.assertIn("attachment", headers.get("Content-Disposition"))
        self.assertEqual(headers.get("Content-Security-Policy"), "default-src 'none'; sandbox")

    def test_identical_bytes_are_stored_once(self):
        blob = b"same bytes on both sides" * 10
        _, first = self.upload("a.txt", blob)
        _, second = self.upload("b.txt", blob)
        self.assertNotEqual(first["file"]["id"], second["file"]["id"])
        p1 = board.Board.__dict__["get_file"](self.store, first["file"]["id"])["path"]
        p2 = board.Board.__dict__["get_file"](self.store, second["file"]["id"])["path"]
        self.assertEqual(p1, p2)
        self.assertTrue(os.path.exists(p1))
        stats = self.store.file_stats()
        self.assertLess(stats["disk_bytes"], stats["bytes"], "dedupe should show on disk")

    def test_undeclared_types_are_guessed_from_the_name(self):
        """What the CLI does: no Content-Type, so the extension decides — and the
        extension is what decides inline vs download, not something host-specific."""
        for name, want in (("notes.md", "text/markdown"), ("run.py", "text/plain"),
                           ("data.json", "application/json"), ("notes.txt", "text/plain"),
                           ("blob.bin", "application/octet-stream")):
            _, rec = self.upload(name, b"x", ctype=None)
            self.assertEqual(rec["file"]["ctype"], want, name)

    def test_an_extensionless_script_is_text_not_a_blob(self):
        # Sharing a script called `decide` should not upload as an opaque binary.
        _, rec = self.upload("decide", b"#!/usr/bin/env python3\nprint('hi')\n", ctype=None)
        self.assertEqual(rec["file"]["ctype"], "text/plain")
        _, rec = self.upload("blob", b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR", ctype=None)
        self.assertEqual(rec["file"]["ctype"], "application/octet-stream")

    def test_a_declared_type_wins_over_the_extension(self):
        _, rec = self.upload("notes.md", b"plain", ctype="text/plain")
        self.assertEqual(rec["file"]["ctype"], "text/plain")
        _, rec = self.upload("data.dat", b"{}", ctype="application/octet-stream")
        self.assertEqual(rec["file"]["ctype"], "application/octet-stream")

    def test_oversized_upload_is_rejected(self):
        old = board.MAX_FILE
        board.MAX_FILE = 512
        try:
            status, _ = self.upload("big.bin", b"x" * 1024)
            self.assertEqual(status, 413)
        finally:
            board.MAX_FILE = old

    def test_missing_file_id_is_404_and_non_integer_is_400(self):
        self.assertEqual(self.raw_get("/api/files/999999")[0], 404)
        self.assertEqual(self.raw_get("/api/files/nope")[0], 400)

    def test_health_reports_file_storage(self):
        status, data = self.call("GET", "/api/health")
        self.assertEqual(status, 200)
        self.assertIn("files", data)
        self.assertIn("max_file", data)
        self.assertEqual(data["version"], board.VERSION)

    # -- attachments -----------------------------------------------------

    def test_message_can_carry_attachments(self):
        _, rec = self.upload("spec.md", b"# the spec\nsteps")
        fid = rec["file"]["id"]
        status, data = self.call("POST", "/api/messages", key=KEY,
                                 body={"author": "poster", "topic": "files",
                                       "text": "see attached", "files": [fid]})
        self.assertEqual(status, 201)
        self.assertEqual([f["id"] for f in data["message"]["files"]], [fid])
        mid = data["id"]
        status, data = self.call("GET", "/api/messages?since=%d" % (mid - 1), key=KEY)
        msg = [m for m in data["messages"] if m["id"] == mid][0]
        self.assertEqual(msg["files"][0]["name"], "spec.md")
        c = self.conn()
        c.request("GET", "/api/messages.md?since=%d" % (mid - 1), headers={"X-Board-Key": KEY})
        md = c.getresponse().read().decode()
        c.close()
        self.assertIn("/api/files/%d" % fid, md)

    def test_attachment_only_message_is_allowed(self):
        _, rec = self.upload("only.txt", b"no words")
        status, _ = self.call("POST", "/api/messages", key=KEY,
                              body={"author": "poster", "files": [rec["file"]["id"]]})
        self.assertEqual(status, 201)

    def test_unknown_attachment_id_is_rejected(self):
        status, data = self.call("POST", "/api/messages", key=KEY,
                                 body={"author": "poster", "text": "x", "files": [999999]})
        self.assertEqual(status, 400)
        self.assertIn("999999", data["error"])

    def test_files_endpoint_lists_uploads(self):
        self.upload("listed.txt", b"here")
        status, data = self.call("GET", "/api/files", key=KEY)
        self.assertEqual(status, 200)
        self.assertTrue(any(f["name"] == "listed.txt" for f in data["files"]))

    # -- growing up: search, paging, digest, prune -----------------------

    def test_ranked_search_finds_and_snippets(self):
        self.call("POST", "/api/messages", key=KEY,
                  body={"author": "searcher", "text": "the zebra migration crosses the river"})
        status, data = self.call("GET", "/api/search?q=zebra", key=KEY)
        self.assertEqual(status, 200)
        self.assertTrue(data["results"])
        self.assertIn("zebra", data["results"][0]["snippet"])
        self.assertIn("«", data["results"][0]["snippet"])
        self.assertIn("rank", data["results"][0])

    def test_search_ignores_fts_grammar(self):
        """A quote or paren in the query must not become an FTS5 syntax error."""
        for hostile in ('"', "a AND", "((", "*", "zebra OR", "a:b", "-zebra"):
            status, _ = self.call("GET", "/api/search?q=" + board.urllib.parse.quote(hostile),
                                  key=KEY)
            self.assertEqual(status, 200, "query %r broke search" % hostile)
        self.assertEqual(self.call("GET", "/api/search?q=", key=KEY)[0], 400)

    def test_search_is_ranked_not_just_filtered(self):
        for i in range(5):
            self.call("POST", "/api/messages", key=KEY,
                      body={"author": "noise", "text": "unrelated filler %d" % i})
        self.call("POST", "/api/messages", key=KEY,
                  body={"author": "signal", "text": "aardvark aardvark aardvark aardvark"})
        status, data = self.call("GET", "/api/search?q=aardvark", key=KEY)
        self.assertEqual(data["results"][0]["author"], "signal")

    def test_backwards_paging_reports_has_more(self):
        for i in range(6):
            self.call("POST", "/api/messages", key=KEY,
                      body={"author": "pager", "topic": "paging", "text": "page %d" % i})
        # forwards: the oldest page, with a cursor to carry on from
        status, data = self.call("GET", "/api/messages?topic=paging&limit=4", key=KEY)
        self.assertEqual(status, 200)
        self.assertEqual(len(data["messages"]), 4)
        self.assertTrue(data["has_more"])
        ids = [m["id"] for m in data["messages"]]
        self.assertEqual(ids, sorted(ids), "a forward page reads oldest-first")
        status, nxt = self.call("GET", "/api/messages?topic=paging&since=%d"
                                % data["messages"][-1]["id"], key=KEY)
        self.assertEqual(len(nxt["messages"]), 2)
        self.assertFalse(nxt["has_more"])
        # backwards: newest page first, then the history behind it
        latest = nxt["latest_id"]
        status, back = self.call("GET", "/api/messages?topic=paging&limit=4&before=%d"
                                 % (latest + 1), key=KEY)
        self.assertEqual(status, 200)
        self.assertEqual(len(back["messages"]), 4)
        self.assertTrue(back["has_more"])
        self.assertEqual(back["messages"][-1]["id"], latest, "backwards pages end at the newest")
        status, older = self.call("GET", "/api/messages?topic=paging&limit=4&before=%d"
                                  % back["messages"][0]["id"], key=KEY)
        self.assertEqual(len(older["messages"]), 2)
        self.assertFalse(older["has_more"])
        self.assertTrue(all(m["id"] < back["messages"][0]["id"] for m in older["messages"]))

    def test_digest_respects_its_character_budget(self):
        for i in range(12):
            self.call("POST", "/api/messages", key=KEY,
                      body={"author": "digester", "topic": "digest", "text": "x" * 900})
        status, data = self.call("GET", "/api/digest?topic=digest&max_chars=1200", key=KEY)
        self.assertEqual(status, 200)
        self.assertLessEqual(len(data["text"]), 1200)
        self.assertTrue(data["truncated"])
        self.assertGreater(data["shown"], 0)
        self.assertLess(data["shown"], data["entries"])
        self.assertGreater(data["next_since"], 0)
        # a digest must be materially cheaper than the transcript it summarises
        status, full = self.call("GET", "/api/messages?topic=digest&limit=1000", key=KEY)
        self.assertLess(len(data["text"]), sum(len(m["text"]) for m in full["messages"]) / 5)

    def test_digest_names_attachments(self):
        _, rec = self.upload("attached.txt", b"body")
        self.call("POST", "/api/messages", key=KEY,
                  body={"author": "a", "topic": "digest2", "text": "see file",
                        "files": [rec["file"]["id"]]})
        status, data = self.call("GET", "/api/digest?topic=digest2", key=KEY)
        self.assertIn("attached.txt", data["text"])
        self.assertIn("#%d" % rec["file"]["id"], data["text"])

    def test_prune_reports_before_it_deletes(self):
        self.call("POST", "/api/messages", key=KEY, body={"author": "p", "topic": "prune",
                                                          "text": "old"})
        plan = self.store.prune(-1)          # everything is older than a negative day count
        self.assertFalse(plan["applied"], "a dry run must not delete")
        self.assertGreater(plan["messages"], 0)
        self.assertTrue(self.store.fetch(0, 100, "prune"), "a dry run must keep the messages")

    def test_prune_applies_archives_and_gcs_files(self):
        _, rec = self.upload("doomed.txt", b"to be pruned")
        fid = rec["file"]["id"]
        self.call("POST", "/api/messages", key=KEY,
                  body={"author": "p", "topic": "prunable", "text": "old", "files": [fid]})
        blob_path = self.store.get_file(fid)["path"]
        self.assertTrue(os.path.exists(blob_path))
        archive = os.path.join(self.tmp, "archive-test.jsonl")
        plan = self.store.prune(-1, apply=True, archive_path=archive)
        self.assertTrue(plan["applied"])
        self.assertTrue(os.path.exists(archive))
        with open(archive) as fh:
            rows = [json.loads(line) for line in fh if line.strip()]
        self.assertTrue(any(r.get("topic") == "prunable" for r in rows))
        self.assertEqual(self.store.fetch(0, 100, "prunable"), [])
        self.assertIsNone(self.store.get_file(fid))
        self.assertFalse(os.path.exists(blob_path), "the pruned blob should be freed from disk")

    def test_reindex_is_idempotent(self):
        before = self.call("GET", "/api/search?q=zebra", key=KEY)[1]["count"]
        self.store.reindex()
        self.store.reindex()
        self.assertEqual(self.call("GET", "/api/search?q=zebra", key=KEY)[1]["count"], before)

    def test_delete_from_messages_removes_it_from_search(self):
        self.call("POST", "/api/messages", key=KEY,
                  body={"author": "gone", "text": "hippopotamus unique token"})
        self.assertTrue(self.call("GET", "/api/search?q=hippopotamus", key=KEY)[1]["results"])
        with self.store._db() as db:
            db.execute("DELETE FROM messages WHERE text LIKE '%hippopotamus%'")
        self.assertEqual(self.call("GET", "/api/search?q=hippopotamus", key=KEY)[1]["results"], [])


class PhaseOneTest(unittest.TestCase):
    """Each case has an isolated database, key ring and real HTTP server."""

    conn = BoardTest.conn
    call = BoardTest.call
    upload = BoardTest.upload
    raw_get = BoardTest.raw_get

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="agent-board-v12-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.store = board.Board(os.path.join(self.tmp, "board.db"))
        self.keys = board.Keys(KEY, self.tmp)
        self.identity_key = self.keys.add("will-hermes", "Will", ["elio-zeta"])
        self.start_server()

    def start_server(self, legacy=KEY):
        self.httpd = board.make_server(("127.0.0.1", 0), board.make_handler(self.store, legacy, "v12"))
        self.port = self.httpd.server_address[1]
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def post(self, text="hello", key=KEY, **fields):
        status, data = self.call("POST", "/api/messages", key=key, body=dict(text=text, **fields))
        self.assertEqual(status, 201, data)
        return data["message"]

    def cli(self, *argv):
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, {"BOARD_DATA": self.tmp, "BOARD_AUTHOR": "",
                                         "BOARD_KEY": KEY, "BOARD_QUIET": "1"}, clear=False):
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                result = board.main(list(argv))
        return result, stdout.getvalue(), stderr.getvalue()

    def remote_cli(self, *argv):
        return self.cli(*(list(argv) + ["--url", "http://127.0.0.1:%d" % self.port]))

    def test_v11_migration_preserves_rows_and_ids(self):
        path = os.path.join(self.tmp, "v11.db")
        with sqlite3.connect(path) as db:
            db.executescript("""
                CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT,
                    epoch REAL NOT NULL, author TEXT NOT NULL,
                    topic TEXT NOT NULL DEFAULT '', text TEXT NOT NULL);
                CREATE TABLE files (id INTEGER PRIMARY KEY AUTOINCREMENT,
                    epoch REAL NOT NULL, name TEXT NOT NULL,
                    ctype TEXT NOT NULL DEFAULT 'application/octet-stream',
                    size INTEGER NOT NULL, sha256 TEXT NOT NULL,
                    uploader TEXT NOT NULL DEFAULT '', path TEXT NOT NULL);
                CREATE TABLE message_files (message_id INTEGER NOT NULL,
                    file_id INTEGER NOT NULL, PRIMARY KEY (message_id, file_id));
                CREATE INDEX idx_messages_topic ON messages(topic, id);
                CREATE INDEX idx_messages_author ON messages(author, id);
                CREATE INDEX idx_messages_epoch ON messages(epoch);
                CREATE INDEX idx_files_sha ON files(sha256);
                CREATE VIRTUAL TABLE messages_fts USING fts5(text,
                    content='messages', content_rowid='id', tokenize='porter unicode61');
                CREATE TRIGGER messages_ai AFTER INSERT ON messages BEGIN
                    INSERT INTO messages_fts(rowid, text) VALUES (new.id, new.text); END;
                CREATE TRIGGER messages_ad AFTER DELETE ON messages BEGIN
                    INSERT INTO messages_fts(messages_fts, rowid, text)
                    VALUES ('delete', old.id, old.text); END;
                CREATE TRIGGER messages_au AFTER UPDATE ON messages BEGIN
                    INSERT INTO messages_fts(messages_fts, rowid, text)
                    VALUES ('delete', old.id, old.text);
                    INSERT INTO messages_fts(rowid, text) VALUES (new.id, new.text); END;
                INSERT INTO messages(epoch,author,topic,text) VALUES(1,'will-hermes','old','first');
                INSERT INTO messages(epoch,author,topic,text) VALUES(2,'hamish-hermes','','second');
            """)
            before = db.execute("SELECT * FROM messages ORDER BY id").fetchall()
        self.store = board.Board(path)
        self.store = board.Board(path)  # reopening must be a no-op
        self.start_server()
        rows = self.call("GET", "/api/messages", key=KEY)[1]["messages"]
        self.assertEqual([(r["id"], r["epoch"], r["author"], r["topic"], r["text"])
                          for r in rows], before)
        for row in rows:
            for field in ("reply_to", "root_id", "amends", "amended_by"):
                self.assertIsNone(row[field])
        self.assertEqual(self.post()["id"], 3)
        with self.store._db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM chunk_fts").fetchone()[0], 0)
            tables = {r["name"] for r in db.execute("SELECT name FROM sqlite_master")}
            self.assertTrue({"acks", "asks", "chunk_fts", "idx_messages_root", "idx_asks_open"} <= tables)
            self.assertEqual([r["name"] for r in db.execute("PRAGMA table_info(acks)")],
                             ["identity", "channel", "acked_id", "updated_epoch"])
            self.assertEqual([r["name"] for r in db.execute("PRAGMA table_info(asks)")],
                             ["id", "msg_id", "from_identity", "to_identity", "text",
                              "opened_epoch", "closed_epoch", "closed_by"])
        self.assertEqual(self.call("GET", "/api/search?q=first", key=KEY)[1]["count"], 1)

    def test_identity_and_legacy_auth(self):
        for key, identity, unrestricted in ((KEY, "legacy", True), (self.identity_key, "will-hermes", False)):
            self.assertEqual(self.call("GET", "/api/identity", key=key),
                             (200, {"identity": identity, "unrestricted": unrestricted}))
        for key in (None, "unknown", "non-ascii-🔑"):
            # Header values must be latin-1; query auth accepts Unicode.
            path = "/api/identity?key=" + board.urllib.parse.quote(key or "")
            self.assertEqual(self.call("GET", path)[0], 401)
        self.assertEqual(self.post(author="anyone")["author"], "anyone")
        self.assertEqual(self.post(**{"from": "old-alias"})["author"], "old-alias")
        self.assertEqual(self.post()["author"], "anonymous")

    def test_identity_author_restriction(self):
        self.assertEqual(self.post(key=self.identity_key)["author"], "will-hermes")
        self.assertEqual(self.post(key=self.identity_key, author="will-hermes")["author"], "will-hermes")
        for author in ("hamish-hermes", "", None, "will-hermes " + "x" * 64):
            self.assertEqual(self.call("POST", "/api/messages", key=self.identity_key,
                                      body={"text": "no", "author": author}),
                             (403, {"error": "this key may only post as will-hermes"}))
        self.assertEqual(self.post(key=self.identity_key, **{"from": "spoof"})["author"], "will-hermes")

    def test_identity_key_headers_and_file_round_trip(self):
        for headers in ({"Authorization": "Bearer " + self.identity_key}, {"X-Board-Key": self.identity_key}):
            c = self.conn()
            c.request("GET", "/api/identity", headers=headers)
            r = c.getresponse()
            self.assertEqual(r.status, 200)
            self.assertEqual(json.loads(r.read())["identity"], "will-hermes")
            c.close()
        status, rec = self.upload("peer.txt", b"peer", key=self.identity_key)
        self.assertEqual(status, 201)
        fid = rec["file"]["id"]
        msg = self.post(key=self.identity_key, files=[fid])
        self.assertEqual(msg["files"][0]["id"], fid)
        self.assertEqual(self.raw_get("/api/files/%d" % fid, self.identity_key)[1], b"peer")

    def test_key_cli_add_list_revoke_and_legacy_rotation(self):
        board.write_key(KEY, self.tmp)
        status, output, _ = self.cli("key", "add", "--identity", "hamish-hermes", "--label", "Hamish")
        self.assertEqual(status, 0)
        secret = output.strip()
        self.assertEqual(self.call("GET", "/api/identity", key=secret)[1]["identity"], "hamish-hermes")
        path = os.path.join(self.tmp, "keys.json")
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        for flag in ("list", "--list"):
            status, output, _ = self.cli("key", flag)
            self.assertEqual(status, 0)
            for identity in ("legacy", "will-hermes", "hamish-hermes", "Hamish"):
                self.assertIn(identity, output)
            for key in (KEY, secret, self.identity_key):
                self.assertNotIn(key, output)
        self.assertEqual(self.cli("key")[1], KEY + "\n")
        self.assertEqual(self.cli("key", "revoke", "--identity", "hamish-hermes")[0], 0)
        self.assertEqual(self.call("GET", "/api/identity", key=secret)[0], 401)
        self.assertEqual(self.call("POST", "/api/messages", key=secret, body={"text": "no"})[0], 401)
        self.assertEqual(self.call("GET", "/api/identity", key=KEY)[0], 200)
        self.assertEqual(self.cli("key", "--new")[0], 0)
        self.assertNotEqual(board.read_key(self.tmp), KEY)
        self.assertEqual(len(self.keys.entries()), 1)

    def test_keyring_only_and_no_key_legacy_modes(self):
        self.start_server(legacy="")
        self.assertEqual(self.call("POST", "/api/messages", body={"text": "no"})[0], 401)
        self.assertEqual(self.post(key=self.identity_key)["author"], "will-hermes")
        self.keys.revoke("will-hermes")
        self.assertEqual(self.post(key=None)["author"], "anonymous")
        self.assertEqual(self.call("GET", "/api/messages")[0], 401)
        self.store.public_read = True
        self.assertEqual(self.call("GET", "/api/messages")[0], 200)
        self.assertEqual(self.call("GET", "/api/identity")[0], 401)

    def test_post_cli_defaults_to_identity_and_preserves_overrides(self):
        for key, extra, expected in ((self.identity_key, [], "will-hermes"),
                                     (KEY, [], "hamish-hermes"),
                                     (KEY, ["--author", "custom"], "custom")):
            result, _, error = self.remote_cli("post", "cli", "--key", key, *extra)
            self.assertEqual(result, 0, error)
            self.assertEqual(self.call("GET", "/api/messages?before=999999&limit=1", key=KEY)[1]
                             ["messages"][0]["author"], expected)

    def test_reply_roots_and_read_paths(self):
        root = self.post("root", topic="meta")
        reply = self.post("capybara reply", reply_to=root["id"])
        nested = self.post("nested", reply_to=reply["id"])
        self.assertIsNone(root["root_id"])
        self.assertEqual(reply["root_id"], root["id"])
        self.assertEqual(nested["root_id"], root["id"])
        self.assertEqual(nested["reply_to"], reply["id"])
        for path, field in (("/api/messages?search=capybara", "messages"),
                            ("/api/messages.json?author=anonymous", "messages"),
                            ("/api/search?q=capybara", "results")):
            rows = self.call("GET", path, key=KEY)[1][field]
            found = next(m for m in rows if m["id"] == reply["id"])
            self.assertEqual({field: found[field] for field in ("reply_to", "root_id", "amends", "amended_by")},
                             dict(reply_to=root["id"], root_id=root["id"], amends=None, amended_by=None))

    def test_invalid_thread_references_and_types_are_rejected(self):
        for field in ("reply_to", "amends"):
            for value in (999999, 0, -1, "1", 1.0, True, [], {}):
                status, _ = self.call("POST", "/api/messages", key=KEY,
                                      body={"text": "invalid", field: value})
                self.assertEqual(status, 400, (field, value))
        self.assertEqual(self.call("GET", "/api/messages?thread=bad", key=KEY)[0], 400)
        self.assertEqual(self.call("GET", "/api/messages/threads?limit=bad", key=KEY)[0], 400)
        self.assertEqual(self.store.count(), 0)

    def test_amendments_chain_and_inherit_thread_and_topic(self):
        root = self.post("original", topic="meta")
        reply = self.post("reply", topic="reply-topic", reply_to=root["id"])
        first = self.post("correction", amends=reply["id"])
        second = self.post("extension", amends=reply["id"], topic="override")
        third = self.post("clear topic", amends=second["id"], topic="")
        self.assertEqual(first["topic"], "reply-topic")
        self.assertEqual(second["topic"], "override")
        self.assertEqual(third["topic"], "")
        self.assertEqual([m["amends"] for m in (first, second, third)],
                         [reply["id"], first["id"], second["id"]])
        self.assertTrue(all(m["root_id"] == root["id"] for m in (first, second, third)))
        rows = self.call("GET", "/api/messages", key=KEY)[1]["messages"]
        self.assertEqual(rows[1]["text"], "reply")
        self.assertEqual(rows[1]["amended_by"], first["id"])
        self.assertEqual(rows[2]["amended_by"], second["id"])
        self.assertEqual(rows[3]["amended_by"], third["id"])
        self.assertIsNone(rows[4]["amended_by"])
        fourth = self.post("implicit topic", amends=reply["id"])
        self.assertEqual(fourth["amends"], third["id"])
        self.assertEqual(fourth["topic"], "")

    def test_reply_and_amendment_together_use_amended_thread(self):
        root = self.post("root", topic="original")
        other = self.post("different thread")
        amendment = self.post("correction replying elsewhere", reply_to=other["id"], amends=root["id"])
        self.assertEqual(amendment["reply_to"], other["id"])
        self.assertEqual(amendment["root_id"], root["id"])
        self.assertEqual(amendment["topic"], "original")

    def test_long_poll_reports_more_within_thread(self):
        root = self.post("root")
        for _ in range(3):
            self.post("answer", reply_to=root["id"])
        page = self.call("GET", "/api/messages?thread=%d&since=%d&wait=1&limit=2" %
                         (root["id"], root["id"]), key=KEY)[1]
        self.assertEqual(len(page["messages"]), 2)
        self.assertTrue(page["has_more"])

    def test_root_amendment_stays_in_original_thread(self):
        root = self.post("original", topic="root")
        amendment = self.post("new", amends=root["id"])
        second = self.post("newer", amends=root["id"])
        self.assertEqual(amendment["root_id"], root["id"])
        self.assertEqual(second["root_id"], root["id"])
        self.assertEqual(self.call("GET", "/api/messages/threads", key=KEY)[1]["threads"][0]["messages"], 3)

    def test_simultaneous_amendments_do_not_fork(self):
        root = self.post("original")
        results = []
        def amend():
            results.append(self.call("POST", "/api/messages", key=KEY,
                                     body={"text": "update", "amends": root["id"]}))
        threads = [threading.Thread(target=amend) for _ in range(5)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
            self.assertFalse(thread.is_alive())
        self.assertEqual([status for status, _ in results], [201] * 5)
        rows = self.call("GET", "/api/messages", key=KEY)[1]["messages"]
        self.assertEqual([m["amends"] for m in rows[1:]], [m["id"] for m in rows[:-1]])
        self.assertEqual([m["amended_by"] for m in rows[:-1]], [m["id"] for m in rows[1:]])

    def test_thread_filters_and_pagination(self):
        root = self.post("root")
        reply = self.post("needle", reply_to=root["id"], topic="t", author="a")
        other = self.post("unrelated")
        last = self.post("needle nested", reply_to=reply["id"], topic="t", author="a")
        path = "/api/messages?thread=%d" % root["id"]
        first = self.call("GET", path + "&limit=2", key=KEY)[1]
        self.assertEqual([m["id"] for m in first["messages"]], [root["id"], reply["id"]])
        self.assertTrue(first["has_more"])
        page = self.call("GET", path + "&since=%d&limit=2" % reply["id"], key=KEY)[1]
        self.assertEqual([m["id"] for m in page["messages"]], [last["id"]])
        self.assertFalse(page["has_more"])
        back = self.call("GET", path + "&before=999999&limit=2", key=KEY)[1]
        self.assertEqual([m["id"] for m in back["messages"]], [reply["id"], last["id"]])
        self.assertTrue(back["has_more"])
        filtered = self.call("GET", path + "&topic=t&author=a&search=needle", key=KEY)[1]
        self.assertEqual([m["id"] for m in filtered["messages"]], [reply["id"], last["id"]])
        nested = self.call("GET", "/api/messages?thread=%d" % last["id"], key=KEY)[1]
        self.assertEqual(nested["count"], 3)
        roots = self.call("GET", "/api/messages?unthreaded=1", key=KEY)[1]
        self.assertEqual([m["id"] for m in roots["messages"]], [root["id"], other["id"]])
        self.assertEqual(self.call("GET", "/api/messages?thread=999999", key=KEY)[1]["count"], 0)

    def test_thread_long_poll_ignores_other_threads(self):
        root = self.post("root")
        result = []
        waiter = threading.Thread(target=lambda: result.append(self.call(
            "GET", "/api/messages?thread=%d&since=%d&wait=3" % (root["id"], root["id"]), key=KEY)))
        waiter.start()
        time.sleep(0.1)
        self.post("unrelated")
        reply = self.post("reply", reply_to=root["id"])
        waiter.join(5)
        self.assertFalse(waiter.is_alive())
        self.assertEqual([m["id"] for m in result[0][1]["messages"]], [reply["id"]])

    def test_thread_navigation_summary_and_cursor(self):
        root = self.post("root text", topic="subject", author="a")
        other = self.post("x" * 90 + "\nsecond line", author="b")
        reply = self.post("reply", reply_to=root["id"], author="c")
        page = self.call("GET", "/api/messages/threads?limit=1", key=KEY)[1]
        self.assertTrue(page["has_more"])
        self.assertEqual(page["threads"], [{"root_id": root["id"], "subject": "subject",
            "messages": 2, "last_id": reply["id"], "last_ts": reply["ts"],
            "authors": ["a", "c"], "open_asks": 0}])
        page = self.call("GET", "/api/messages/threads?limit=1&before=%d" % reply["id"], key=KEY)[1]
        self.assertFalse(page["has_more"])
        self.assertEqual(page["threads"][0]["root_id"], other["id"])
        self.assertEqual(page["threads"][0]["subject"], "x" * 80)

    def test_markdown_and_digest_structure_markers(self):
        root = self.post("original")
        reply = self.post("answer", reply_to=root["id"])
        amendment = self.post("correction", amends=reply["id"])
        for path in ("/api/messages.md", "/api/digest"):
            status, data = self.call("GET", path, key=KEY)
            self.assertEqual(status, 200)
            text = data if isinstance(data, str) else data["text"]
            self.assertIn("↳#%d" % root["id"], text)
            self.assertIn("amend #%d" % reply["id"], text)
            self.assertIn("amended by #%d" % amendment["id"], text)

    def test_cli_reply_amend_thread_and_unthreaded(self):
        root = self.post("original", topic="meta")
        result, output, error = self.remote_cli("post", "answer", "--reply-to", str(root["id"]), "--topic", "meta")
        self.assertEqual(result, 0, error)
        reply_id = int(output.strip().split("#")[-1])
        note = os.path.join(self.tmp, "note.md")
        with open(note, "w") as fh:
            fh.write("corrected from file\n")
        result, output, error = self.remote_cli("amend", str(reply_id), "--text-file", note)
        self.assertEqual(result, 0, error)
        amendment_id = int(output.strip().split("#")[-1])
        result, output, error = self.remote_cli("read", "--thread", str(root["id"]), "--limit", "1")
        self.assertEqual(result, 0, error)
        self.assertIn("\n  #%d " % reply_id, output)
        self.assertIn("\n    (amended by #%d)\n" % amendment_id, output)
        self.assertIn("corrected from file", output)
        result, output, _ = self.remote_cli("read", "--unthreaded", "--limit", "20")
        self.assertEqual(result, 0)
        self.assertIn("#%d " % root["id"], output)
        self.assertNotIn("#%d " % reply_id, output)
        result, output, _ = self.remote_cli("threads", "--limit", "20")
        self.assertEqual(result, 0)
        self.assertIn("meta  (3 messages)", output)
        self.assertIn("0 open asks", output)

    def test_thread_cli_depth_is_capped(self):
        root = self.post("root")
        parent = root
        for _ in range(10):
            parent = self.post("deep", reply_to=parent["id"])
        result, output, _ = self.remote_cli("read", "--thread", str(root["id"]))
        self.assertEqual(result, 0)
        header = next(line for line in output.splitlines() if line.lstrip().startswith("#%d " % parent["id"]))
        self.assertEqual(len(header) - len(header.lstrip()), 16)

    def test_thread_cli_json_and_markdown_include_all_pages(self):
        root = self.post("root")
        reply = self.post("answer", reply_to=root["id"])
        result, output, _ = self.remote_cli("read", "--thread", str(root["id"]), "--limit", "1", "--json")
        self.assertEqual(result, 0)
        data = json.loads(output)
        self.assertEqual(data["count"], 2)
        self.assertFalse(data["has_more"])
        self.assertEqual([m["id"] for m in data["messages"]], [root["id"], reply["id"]])
        result, output, _ = self.remote_cli("read", "--thread", str(root["id"]), "--limit", "1", "--md")
        self.assertEqual(result, 0)
        self.assertIn("## #%d" % root["id"], output)
        self.assertIn("## #%d" % reply["id"], output)
        self.assertEqual(output.count("# agent-board transcript"), 1)

    def test_plain_read_output_is_unchanged(self):
        root = self.post("original\nsecond", author="a", topic="meta")
        amendment = self.post("correction", author="b", amends=root["id"])
        expected = "".join("#%d %s %s [%s]\n%s\n" % (m["id"], m["author"], m["ts"], m["topic"],
                           "".join("    " + line + "\n" for line in m["text"].splitlines()))
                           for m in (root, amendment))
        result, output, _ = self.remote_cli("read")
        self.assertEqual(result, 0)
        self.assertEqual(output, expected)

    def test_web_thread_controls_are_served(self):
        status, html = self.call("GET", "/")
        self.assertEqual(status, 200)
        for text in ('id="back-live"', 'id="reply-chip"', 'id="clear-reply"',
                     '↳ #', 'amended → #', 'href="#thread/', 'reply_to: replyTo',
                     "mode !== 'live'", 'pollController.abort()', "addEventListener('hashchange', route)"):
            self.assertIn(text, html)


class PhaseTwoTest(unittest.TestCase):
    """Receipt and ask behavior over real HTTP, always on temporary boards."""

    conn = BoardTest.conn
    call = BoardTest.call
    raw_get = BoardTest.raw_get
    start_server = PhaseOneTest.start_server
    post = PhaseOneTest.post
    cli = PhaseOneTest.cli
    remote_cli = PhaseOneTest.remote_cli

    def setUp(self):
        PhaseOneTest.setUp(self)
        self.hamish_key = self.keys.add("hamish-hermes", "Hamish", ["*"])

    def receipt(self, mid, key=None):
        status, data = self.call("POST", "/api/ack", key=key or self.identity_key, body={"id": mid})
        self.assertEqual(status, 200, data)
        return data

    def delivery(self, key=None, peer=None):
        path = "/api/delivery" + ("?peer=" + peer if peer else "")
        status, data = self.call("GET", path, key=key or self.identity_key)
        self.assertEqual(status, 200, data)
        return data

    def age_message(self, mid, seconds):
        with self.store.cond, self.store._db() as db:
            db.execute("UPDATE messages SET epoch = ? WHERE id = ?", (time.time() - seconds, mid))

    def test_ack_requires_identity_and_is_monotonic(self):
        self.assertEqual(self.call("POST", "/api/ack", key=KEY, body={"id": 1}),
                         (400, {"error": "acks need an identity key"}))
        self.assertEqual(self.call("POST", "/api/ack", body={"id": 1})[0], 401)
        mid = self.post(author="hamish-hermes")["id"]
        ack = self.receipt(mid)
        self.assertEqual(ack["identity"], "will-hermes")
        self.assertIn("ts", ack)
        self.assertEqual(self.delivery()["me"]["state"], "ok")
        self.assertEqual(self.receipt(0)["acked_id"], mid)
        self.assertEqual(self.receipt(mid)["acked_id"], mid)

    def test_delivery_unknown_fresh_and_behind(self):
        self.assertEqual(self.delivery()["me"]["state"], "unknown")
        self.receipt(0)
        # A very old ack alone cannot make a new pending message late.
        with self.store.cond, self.store._db() as db:
            db.execute("UPDATE acks SET updated_epoch = ?", (time.time() - 10000,))
        incoming = self.post(author="hamish-hermes")["id"]
        own = self.post(key=self.identity_key)["id"]
        me = self.delivery()["me"]
        self.assertEqual(me["state"], "ok")
        self.assertEqual(me["pending"], [incoming])
        self.assertEqual(me["pending_count"], 1)
        self.assertEqual(me["latest_from_peer"], incoming)
        self.assertNotIn(own, me["pending"])
        self.age_message(incoming, 650)
        me = self.delivery()["me"]
        self.assertEqual(me["state"], "behind")
        self.assertGreater(me["age_seconds"], 600)
        self.assertEqual(self.delivery()["grace_seconds"], 600)
        with mock.patch.dict(os.environ, {"BOARD_ACK_GRACE": "900"}):
            self.assertEqual(self.delivery()["me"]["state"], "ok")
        self.receipt(incoming)
        me = self.delivery()["me"]
        self.assertEqual((me["pending"], me["pending_count"], me["age_seconds"], me["state"]), ([], 0, 0, "ok"))

    def test_delivery_pending_cap_peer_filter_and_one_sided_receipts(self):
        self.receipt(0)
        # Populate through storage to keep this case independent of the IP rate limit.
        ids = [self.store.post("hamish-hermes", "incoming")["id"] for _ in range(55)]
        self.age_message(ids[0], 700)
        data = self.delivery()
        self.assertEqual(data["me"]["pending"], ids[:50])
        self.assertEqual(data["me"]["pending_count"], 55)
        self.assertEqual(data["me"]["latest_from_peer"], ids[-1])
        self.assertEqual(data["peers"][0]["state"], "unknown")
        # The peer's missing receipt remains unknown even after hours, never behind.
        self.assertEqual(self.delivery(self.hamish_key, "will-hermes")["peers"][0]["state"], "behind")
        self.assertEqual(self.delivery(self.hamish_key, "nobody")["peers"], [])
        self.assertEqual(self.call("GET", "/api/delivery")[0], 401)

    def test_delivery_grace_boundary_uses_pending_message_age(self):
        mid = self.post(author="hamish-hermes")["id"]
        self.receipt(0)
        with self.store.cond, self.store._db() as db:
            db.execute("UPDATE messages SET epoch = 1000 WHERE id = ?", (mid,))
        with mock.patch.object(board.time, "time", return_value=1600):
            me = self.delivery()["me"]
            self.assertEqual((me["age_seconds"], me["state"]), (600, "ok"))
        # Even a fresh receipt of an older id does not reset an existing gap.
        self.receipt(0)
        with mock.patch.object(board.time, "time", return_value=1601):
            me = self.delivery()["me"]
            self.assertEqual((me["age_seconds"], me["state"]), (601, "behind"))

    def test_health_delivery_is_public_and_contains_no_message_text(self):
        secret = "private message body must never be in public health"
        mid = self.post(secret, author="hamish-hermes")["id"]
        self.receipt(0)
        self.age_message(mid, 700)
        status, health = self.call("GET", "/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(health["delivery"], {"hamish-hermes": "unknown", "will-hermes": "behind"})
        self.assertNotIn(secret, json.dumps(health))
        self.keys._write([])
        self.assertEqual(self.call("GET", "/api/health")[1]["delivery"], {})

    def test_ack_validation_and_delivery_cli_exit_codes(self):
        for value in (True, "1", 1.5, None, -1):
            self.assertEqual(self.call("POST", "/api/ack", key=self.identity_key, body={"id": value})[0], 400)
        self.assertEqual(self.call("POST", "/api/ack", key=self.identity_key,
                                   body={"id": 0, "channel": 5})[0], 200)
        code, out, _ = self.remote_cli("delivery", "--key", self.identity_key)
        self.assertEqual(code, 2)
        # Only hamish-hermes is unknown here: will-hermes acked whole-board at 0 a
        # moment ago (the non-string channel above), and that receipt covers every
        # room with nothing pending behind it.
        self.assertIn("unknown: hamish-hermes", out)
        self.assertNotIn("unknown: hamish-hermes, will-hermes", out)
        self.assertEqual(self.remote_cli("ack", "--id", "0", "--channel", "elio-zeta", "--key", self.identity_key)[0], 0)
        self.receipt(0)
        self.receipt(0, self.hamish_key)
        self.store.ack("hamish-hermes", 0, "elio-zeta")
        self.assertEqual(self.remote_cli("delivery", "--key", self.identity_key)[0], 0)
        mid = self.post(author="hamish-hermes")["id"]
        self.age_message(mid, 700)
        code, out, err = self.remote_cli("delivery", "--json", "--key", self.identity_key)
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)["me"]["state"], "behind")
        self.assertIn("will-hermes", err)
        self.receipt(mid)
        self.store.ack("will-hermes", mid, "elio-zeta")
        self.keys.revoke("hamish-hermes")
        code, out, _ = self.remote_cli("delivery", "--key", self.identity_key)
        self.assertEqual(code, 0)
        self.assertIn("will-hermes", out)
        self.assertNotIn("hamish-hermes", out)

    def test_ask_lifecycle_and_double_close_rolls_back_message(self):
        msg = self.post("Can you check?", key=self.hamish_key, ask_to="will-hermes")
        aid = msg["ask_id"]
        self.assertIsInstance(aid, int)
        self.assertIsNone(msg["answers_ask"])
        status, data = self.call("GET", "/api/asks?state=open", key=KEY)
        self.assertEqual(status, 200)
        ask = data["asks"][0]
        self.assertEqual((ask["msg_id"], ask["from"], ask["to"], ask["thread_id"]),
                         (msg["id"], "hamish-hermes", "will-hermes", msg["id"]))
        self.assertEqual(ask["state"], "open")
        self.assertIsNone(ask["closed_ts"])
        self.assertIsNone(ask["closed_by"])
        self.assertGreaterEqual(ask["age_seconds"], 0)
        answer = self.post("Checked", key=self.identity_key, answers=aid, reply_to=msg["id"])
        self.assertEqual(answer["answers_ask"], aid)
        self.assertEqual(self.call("GET", "/api/asks", key=KEY)[1]["count"], 0)
        closed = self.call("GET", "/api/asks?state=closed", key=KEY)[1]["asks"][0]
        self.assertEqual((closed["state"], closed["closed_by"]), ("closed", answer["id"]))
        self.assertIsNotNone(closed["closed_ts"])
        count = self.store.count()
        self.assertEqual(self.call("POST", "/api/messages", key=KEY, body={"text": "again", "answers": aid}),
                         (409, {"error": "ask %d is already closed by #%d" % (aid, answer["id"])}))
        self.assertEqual(self.store.count(), count)
        fetched = self.call("GET", "/api/messages", key=KEY)[1]["messages"]
        self.assertEqual(fetched[0]["ask_state"], "closed")
        self.assertEqual(fetched[1]["answers_ask"], aid)

    def test_close_ask_with_existing_older_message_and_errors(self):
        older = self.post("Already answered")["id"]
        opened = self.post("Question", ask_to="will-hermes")
        path = "/api/asks/%d/close" % opened["ask_id"]
        for mid in (99999, True, "1", None):
            self.assertEqual(self.call("POST", path, key=KEY, body={"msg_id": mid})[0], 400)
        self.assertEqual(self.call("POST", "/api/asks/99999/close", key=KEY, body={"msg_id": older})[0], 400)
        self.assertEqual(self.call("POST", path, body={"msg_id": older})[0], 401)
        count = self.store.count()
        status, data = self.call("POST", path, key=KEY, body={"msg_id": older})
        self.assertEqual(status, 200)
        self.assertEqual(data["ask"]["closed_by"], older)
        self.assertEqual(self.store.count(), count)
        self.assertEqual(self.call("POST", path, key=KEY, body={"msg_id": older}),
                         (409, {"error": "ask %d is already closed by #%d" % (opened["ask_id"], older)}))

    def test_ask_validation_and_legacy_empty_ring(self):
        for target in ("unknown", "", "  ", 42, None):
            self.assertEqual(self.call("POST", "/api/messages", key=KEY,
                                       body={"text": "Q", "ask_to": target})[0], 400)
        for aid in (999, True, "1", None):
            self.assertEqual(self.call("POST", "/api/messages", key=KEY,
                                       body={"text": "A", "answers": aid})[0], 400)
        self.assertEqual(self.store.count(), 0)
        self.keys._write([])
        msg = self.post("Legacy question", author="legacy-author", ask_to="new-peer")
        ask = self.call("GET", "/api/asks", key=KEY)[1]["asks"][0]
        self.assertEqual((ask["from"], ask["to"], ask["id"]), ("legacy-author", "new-peer", msg["ask_id"]))
        self.assertEqual(self.call("GET", "/api/asks")[0], 401)

    def test_ask_filters_order_limit_and_thread_counts(self):
        root = self.post("root", ask_to="will-hermes")
        reply = self.post("reply", key=self.identity_key, reply_to=root["id"], ask_to="hamish-hermes")
        other = self.post("other", ask_to="will-hermes")
        def counts():
            return {t["root_id"]: t["open_asks"] for t in
                    self.call("GET", "/api/messages/threads", key=KEY)[1]["threads"]}
        self.assertEqual(counts(), {root["id"]: 2, other["id"]: 1})
        both = self.post("answer and question", answers=root["ask_id"], ask_to="will-hermes", reply_to=root["id"])
        self.assertEqual(both["answers_ask"], root["ask_id"])
        self.assertIsNotNone(both["ask_id"])
        self.assertEqual(counts(), {root["id"]: 2, other["id"]: 1})
        data = self.call("GET", "/api/asks?limit=1", key=KEY)[1]
        self.assertTrue(data["has_more"])
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["asks"][0]["id"], reply["ask_id"])
        data = self.call("GET", "/api/asks?to=hamish-hermes&from=will-hermes", key=KEY)[1]
        self.assertEqual([a["id"] for a in data["asks"]], [reply["ask_id"]])
        self.assertEqual(data["asks"][0]["thread_id"], root["id"])
        all_asks = self.call("GET", "/api/asks?state=all", key=KEY)[1]["asks"]
        self.assertEqual([a["state"] for a in all_asks], ["open", "open", "open", "closed"])
        for query in ("state=bad", "limit=oops"):
            self.assertEqual(self.call("GET", "/api/asks?" + query, key=KEY)[0], 400)

    def test_ask_markers_in_digest_markdown_and_search(self):
        msg = self.post("Question zebrafish", ask_to="will-hermes")
        aid = msg["ask_id"]
        digest = self.call("GET", "/api/digest", key=KEY)[1]["text"]
        self.assertTrue(digest.startswith("! #"))
        self.assertIn("[ask→will-hermes]", digest)
        md = self.raw_get("/api/messages.md")[1].decode()
        self.assertIn("! ##", md)
        self.assertIn("[ask→will-hermes]", md)
        result = self.call("GET", "/api/search?q=zebrafish", key=KEY)[1]["results"][0]
        self.assertEqual((result["ask_id"], result["ask_state"]), (aid, "open"))
        self.post("Answer", answers=aid)
        digest = self.call("GET", "/api/digest", key=KEY)[1]["text"]
        self.assertFalse(digest.startswith("!"))
        self.assertIn("[answers #%d]" % aid, digest)
        self.assertIn("[answers #%d]" % aid, self.raw_get("/api/messages.md")[1].decode())

    def test_concurrent_ask_closures_have_one_winner(self):
        older = self.post("existing answer")["id"]
        aid = self.post("question", ask_to="will-hermes")["ask_id"]
        gate = threading.Barrier(3)
        results = []
        def close(path, payload):
            gate.wait()
            results.append(self.call("POST", path, key=KEY, body=payload))
        workers = [threading.Thread(target=close, args=("/api/asks/%d/close" % aid, {"msg_id": older})),
                   threading.Thread(target=close, args=("/api/messages", {"text": "new answer", "answers": aid}))]
        for worker in workers:
            worker.start()
        gate.wait()
        for worker in workers:
            worker.join(10)
            self.assertFalse(worker.is_alive())
        self.assertEqual(len(results), 2)
        self.assertEqual(sum(status == 409 for status, _ in results), 1)
        winner = next((status, data) for status, data in results if status != 409)
        closed_by = older if winner[0] == 200 else winner[1]["id"]
        ask = self.call("GET", "/api/asks?state=closed", key=KEY)[1]["asks"][0]
        self.assertEqual(ask["closed_by"], closed_by)
        self.assertEqual(self.store.count(), 2 if winner[0] == 200 else 3)

    def test_ask_open_answer_cli_lifecycle_and_exit_codes(self):
        note = os.path.join(self.tmp, "question.txt")
        with open(note, "w", encoding="utf-8") as fh:
            fh.write("Can you check this?")
        self.assertEqual(self.remote_cli("open")[0], 1)
        code, out, error = self.remote_cli("ask", "--to", "will-hermes", "--text-file", note, "--topic", "work")
        self.assertEqual(code, 0, error)
        self.assertIn("posted #1, ask 1 opened for will-hermes", out)
        code, out, _ = self.remote_cli("open", "--to", "will-hermes", "--from", "hamish-hermes", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["asks"][0]["id"], 1)
        older = self.post("existing answer")["id"]
        self.assertEqual(self.remote_cli("answer", "1", "--msg", str(older))[0], 0)
        self.assertEqual(self.remote_cli("open")[0], 1)
        self.assertEqual(self.remote_cli("open", "--all")[0], 0)
        self.assertEqual(self.remote_cli("answer", "1", "--msg", str(older))[0], 1)
        code, out, error = self.remote_cli("post", "next question", "--ask-to", "will-hermes")
        self.assertEqual(code, 0, error)
        self.assertIn("ask 2 opened", out)
        code, _, error = self.remote_cli("answer", "2", "--text-file", note, "--reply-to", "3")
        self.assertEqual(code, 0, error)
        self.assertEqual(self.remote_cli("open")[0], 1)
        self.assertEqual(self.remote_cli("post", "question", "--ask-to", "will-hermes")[0], 0)
        self.assertEqual(self.remote_cli("post", "answer and next", "--answers", "3", "--ask-to", "will-hermes")[0], 0)
        self.assertEqual(self.remote_cli("open")[0], 0)

    def test_web_delivery_and_open_ask_controls_are_served(self):
        status, html = self.call("GET", "/")
        self.assertEqual(status, 200)
        for text in ('id="delivery"', "delivery ok", "behind by ", "?→", "open asks", "ask_state === 'open'"):
            self.assertIn(text, html)
        self.assertEqual(html.count("fetch('/api/delivery'"), 1)


class PhaseThreeStorageTest(unittest.TestCase):
    """Chunk migration and storage checks do not need a listening socket."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="agent-board-chunks-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    @staticmethod
    def chunks(store):
        with store._db() as db:
            return [tuple(r) for r in db.execute(
                "SELECT body, message_id, para FROM chunk_fts ORDER BY message_id, para")]

    def test_chunk_backfill_runs_once(self):
        for preexisting_chunks in (False, True):
            with self.subTest(preexisting_chunks=preexisting_chunks):
                path = os.path.join(self.tmp, str(preexisting_chunks), "v11.db")
                os.makedirs(os.path.dirname(path))
                with sqlite3.connect(path) as db:
                    db.executescript("""
                        CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT,
                            epoch REAL NOT NULL, author TEXT NOT NULL,
                            topic TEXT NOT NULL DEFAULT '', text TEXT NOT NULL);
                        CREATE VIRTUAL TABLE messages_fts USING fts5(text,
                            content='messages', content_rowid='id', tokenize='porter unicode61');
                    """)
                    db.executemany("INSERT INTO messages(epoch, author, text) VALUES(1, 'old', ?)",
                                   [("Archived narwhal paragraph with evidence.\n\n"
                                     "Another archived paragraph with context.",), ("tiny",), ("",)])
                    db.execute("INSERT INTO messages_fts(messages_fts) VALUES('rebuild')")
                    if preexisting_chunks:
                        db.execute("CREATE VIRTUAL TABLE chunk_fts USING fts5("
                                   "body, message_id UNINDEXED, para UNINDEXED, tokenize='porter unicode61')")
                first = board.Board(path)
                before = self.chunks(first)
                self.assertEqual(len(before), 2)
                self.assertEqual([r[2] for r in before], [0, 1])
                self.assertEqual(first.search("narwhal")[0]["id"], 1)
                with mock.patch.object(board, "message_chunks", wraps=board.message_chunks) as chunker:
                    second = board.Board(path)
                    self.assertEqual(second.chunks_missing(), 0)
                    chunker.assert_not_called()
                self.assertEqual(self.chunks(second), before)
                with second._db() as db:
                    self.assertEqual(db.execute("SELECT value FROM meta WHERE key='chunks_max_id'")
                                     .fetchone()[0], "3")
                new = second.post("new", "New narwhal evidence beyond the archive.")
                self.assertEqual(new["id"], 4)
                self.assertEqual(len(self.chunks(second)), 3)

    def test_reindex_is_idempotent(self):
        store = board.Board(os.path.join(self.tmp, "board.db"))
        msg = store.post("tester", "Opening context for reindex verification.\n\n"
                         "The platypus evidence belongs in the second paragraph.")
        expected = self.chunks(store)
        # Corrupt both indexes so the test proves that both are rebuilt.
        with store._db() as db:
            db.execute("DELETE FROM chunk_fts")
            db.execute("INSERT INTO messages_fts(messages_fts) VALUES('delete-all')")
            db.execute("UPDATE meta SET value='999' WHERE key='chunks_max_id'")
        for _ in range(2):
            self.assertEqual(store.reindex(), 1)
            self.assertEqual(self.chunks(store), expected)
            result = store.search("platypus")
            self.assertEqual([(r["id"], r["paragraph"]) for r in result], [(msg["id"], 1)])
            self.assertIn("«platypus»", result[0]["snippet"])
            self.assertEqual(len(store.fetch(search="platypus")), 1)
            with store._db() as db:
                self.assertEqual(db.execute("SELECT value FROM meta WHERE key='chunks_max_id'")
                                 .fetchone()[0], str(msg["id"]))

    def test_chunk_boundaries_and_short_piece_filter(self):
        first = "a" * 20
        long = " ".join("word%03d" % n for n in range(350))
        chunks = list(board.message_chunks("---\r\n \r\n" + first + "\r\n\r\n" + long, 7))
        self.assertEqual(chunks[0], (first, 7, 0))
        self.assertEqual([c[2] for c in chunks], list(range(len(chunks))))
        self.assertTrue(all(20 <= len(c[0]) <= 1200 and c[1] == 7 for c in chunks))
        self.assertEqual(" ".join(c[0] for c in chunks[1:]), long)
        self.assertEqual([len(c[0]) for c in board.message_chunks("x" * 2500, 8)], [1200, 1200, 100])
        self.assertEqual(list(board.message_chunks("short\n\n---\n\n", 9)), [])

    def test_incremental_chunks_and_deletion(self):
        store = board.Board(os.path.join(self.tmp, "board.db"))
        original = store.post("tester", "Original echidna evidence before the amendment.")
        with mock.patch.object(board, "message_chunks", wraps=board.message_chunks) as chunker:
            amended = store.post("tester", "Corrected echidna evidence in a new message.", amends=original["id"])
            self.assertEqual(chunker.call_count, 1)
            self.assertEqual(chunker.call_args[0][1], amended["id"])
        self.assertEqual(len(self.chunks(store)), 2)
        self.assertEqual({r["id"] for r in store.search("echidna")}, {original["id"], amended["id"]})
        with store._db() as db:
            db.execute("DELETE FROM messages WHERE id = ?", (original["id"],))
        self.assertEqual([c[1] for c in self.chunks(store)], [amended["id"]])


class PhaseThreeTest(unittest.TestCase):
    """Paragraph search and posting hints over HTTP on isolated boards."""

    conn = BoardTest.conn
    call = BoardTest.call
    upload = BoardTest.upload
    start_server = PhaseOneTest.start_server
    setUp = PhaseOneTest.setUp
    post = PhaseOneTest.post
    cli = PhaseOneTest.cli
    remote_cli = PhaseOneTest.remote_cli

    def test_search_finds_paragraph_inside_a_long_message(self):
        body = ("Openingcontext describes the initial background. " * 24 + "\n\n"
                + "A separate paragraph supplies more background. " * 15 + "\n\n"
                + "The iridescentaxolotl sentence records the distinctive middle claim.\n\n")
        body += ("Closing context continues the supporting evidence. " * 60)[:4000 - len(body)]
        self.assertEqual(len(body), 4000)
        msg = self.post(body)
        status, data = self.call("GET", "/api/search?q=iridescentaxolotl", key=KEY)
        self.assertEqual(status, 200)
        self.assertEqual(data["count"], 1)
        result = data["results"][0]
        self.assertEqual(result["id"], msg["id"])
        self.assertGreater(result["paragraph"], 0)
        self.assertIn("«iridescentaxolotl»", result["snippet"])
        self.assertNotIn("Openingcontext", result["snippet"])

    def test_search_falls_back_when_chunks_empty(self):
        msg = self.post("The fallbackpangolin remains searchable in the archive.")
        with self.store._db() as db:
            db.execute("DELETE FROM chunk_fts")
        status, data = self.call("GET", "/api/search?q=fallbackpangolin", key=KEY)
        self.assertEqual(status, 200)
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["results"][0]["id"], msg["id"])
        self.assertIn("«fallbackpangolin»", data["results"][0]["snippet"])
        self.assertIsNone(data["results"][0]["paragraph"])
        self.assertIn("rank", data["results"][0])

    def test_search_deduplicates_before_limit_and_keeps_best_chunk(self):
        first = self.post("quasarchunk " + "background " * 40 + "\n\n"
                          + ("quasarchunk quasarchunk quasarchunk.\n\n" * 30).strip())
        second = self.post("quasarchunk observations from a different message.")
        self.post("quasarchunk evidence from one final message.")
        status, data = self.call("GET", "/api/search?q=quasarchunk&limit=2", key=KEY)
        self.assertEqual(status, 200)
        self.assertEqual(data["count"], 2)
        self.assertEqual([m["id"] for m in data["results"]], [first["id"], second["id"]])
        self.assertEqual(data["results"][0]["paragraph"], 1)
        self.assertEqual(len({m["id"] for m in data["results"]}), 2)
        self.assertEqual([m["rank"] for m in data["results"]], sorted(m["rank"] for m in data["results"]))
        self.assertIn(first["id"], {m["id"] for m in self.store.search("quasarchunk", limit=3)})
        with self.store._db() as db:
            # Check the API's winner against every matching chunk, including ties.
            for msg in data["results"]:
                best = db.execute("SELECT message_id, para, bm25(chunk_fts) AS score FROM chunk_fts "
                                  "WHERE chunk_fts MATCH ? ORDER BY score, message_id, para",
                                  (board.fts_query("quasarchunk"),)).fetchall()
                winner = next(r for r in best if r["message_id"] == msg["id"])
                self.assertEqual((msg["paragraph"], msg["rank"]), (winner["para"], winner["score"]))
        self.assertEqual(self.store.search("quasarchunk", limit=1)[0]["id"], data["results"][0]["id"])

    def test_read_search_keeps_whole_message_filter(self):
        msg = self.post("Firstparagraphterm has its own context.\n\nSecondparagraphterm lives separately.")
        query = "firstparagraphterm%20secondparagraphterm"
        data = self.call("GET", "/api/messages?search=" + query, key=KEY)[1]
        self.assertEqual([m["id"] for m in data["messages"]], [msg["id"]])
        self.assertEqual(self.call("GET", "/api/search?q=" + query, key=KEY)[1]["count"], 0)

    def test_body_warning_only_without_attachment(self):
        fid = self.upload("document.txt", b"attached artifact")[1]["file"]["id"]
        for size, files, warned in ((3204, [], True), (4000, [], True), (3204, [fid], False),
                                    (1200, [], False), (1201, [], True), (20, [], False)):
            with self.subTest(size=size, files=files):
                status, data = self.call("POST", "/api/messages", key=KEY,
                                         body={"text": "a" * size, "files": files})
                self.assertEqual(status, 201)
                self.assertEqual("warning" in data, warned)
                self.assertEqual(data["message"]["text"], "a" * size)
                if warned:
                    self.assertEqual(data["warning"], "body is %d chars with no attachment; "
                                     "post a short note and attach the document" % size)

    def test_post_and_amend_cli_print_body_warning(self):
        text = "word " * 300
        warning = "warning: body is %d chars with no attachment; post a short note and attach the document" % len(text.strip())

        def warnings(stderr):
            # The in-process server logs requests to stderr, so compare the
            # warning lines only — an equality test on the whole stream passes
            # under the sandbox's in-memory transport and fails on real sockets.
            return [line for line in stderr.splitlines() if line.startswith("warning:")]

        code, output, error = self.remote_cli("post", text)
        self.assertEqual(code, 0)
        self.assertEqual(warnings(error), [warning])
        mid = output.strip().split("#")[-1]
        code, _, error = self.remote_cli("amend", mid, text)
        self.assertEqual(code, 0)
        self.assertEqual(warnings(error), [warning])
        fid = self.upload("document.txt", b"attached artifact")[1]["file"]["id"]
        code, _, error = self.remote_cli("post", text, "--file", str(fid))
        self.assertEqual(code, 0)
        self.assertEqual(warnings(error), [])
        self.assertEqual(warnings(self.remote_cli("post", "short note")[2]), [])

    def test_web_body_warning_controls_are_served(self):
        status, html = self.call("GET", "/")
        self.assertEqual(status, 200)
        for text in ("function updateBodyHint()", "count > 1200", "!$('#file').files.length",
                     "Array.from($('#text').value.trim()).length", "post a short note and attach the document",
                     "$('#text').addEventListener('input', updateBodyHint)",
                     "$('#file').addEventListener('change', updateBodyHint)"):
            self.assertIn(text, html)


class ServerResilienceTest(unittest.TestCase):
    """The connection handling that keeps a long-lived server from drifting.

    Both cases were observed on the live board on 26 Oct 2026: established
    connections with no client process behind them, and no ceiling on how many
    handler threads a stalled peer could hold.
    """

    def start(self, **kwargs):
        tmp = tempfile.mkdtemp(prefix="agent-board-resilience-")
        self.addCleanup(shutil.rmtree, tmp, True)
        store = board.Board(os.path.join(tmp, "board.db"))
        handler = board.make_handler(store, KEY, "resilience",
                                     idle_timeout=kwargs.pop("idle_timeout", 1.0))
        httpd = board.make_server(("127.0.0.1", 0), handler, **kwargs)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        return httpd, httpd.server_address[1]

    def test_idle_connection_is_closed_not_held_open(self):
        httpd, port = self.start(idle_timeout=1.0)
        sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        self.addCleanup(sock.close)
        # Say nothing, as a peer does when its network drops mid keep-alive.
        sock.settimeout(6.0)
        started = time.time()
        self.assertEqual(sock.recv(1), b"", "server kept an idle connection open")
        self.assertLess(time.time() - started, 5.0)

    def test_an_idle_connection_stops_holding_a_slot(self):
        httpd, port = self.start(idle_timeout=1.0)
        sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        self.addCleanup(sock.close)
        time.sleep(0.3)
        self.assertEqual(httpd.inflight(), 1)
        time.sleep(1.6)
        self.assertEqual(httpd.inflight(), 0, "an abandoned connection pinned a handler thread")

    def test_over_capacity_is_refused_rather_than_queued(self):
        httpd, port = self.start(idle_timeout=30.0, max_concurrent=1)
        held = socket.create_connection(("127.0.0.1", port), timeout=5)
        self.addCleanup(held.close)
        time.sleep(0.3)
        self.assertEqual(httpd.inflight(), 1)
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request("GET", "/api/health")
        response = conn.getresponse()
        body = json.loads(response.read().decode())
        conn.close()
        self.assertEqual(response.status, 503)
        self.assertEqual(response.getheader("Retry-After"), "5")
        self.assertIn("capacity", body["error"])

    def test_health_reports_uptime_and_capacity(self):
        httpd, port = self.start(idle_timeout=5.0, max_concurrent=7)
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request("GET", "/api/health")
        data = json.loads(conn.getresponse().read().decode())
        conn.close()
        self.assertGreaterEqual(data["uptime_s"], 0)
        self.assertEqual(data["max_concurrent"], 7)
        self.assertEqual(data["inflight"], 1, "the health request itself is in flight")


class ChannelStorageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="agent-board-channel-storage-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_fresh_and_migrated_schema_backfill_and_idempotence(self):
        fresh = board.Board(os.path.join(self.tmp, "fresh.db"))
        path = os.path.join(self.tmp, "old.db")
        with sqlite3.connect(path) as db:
            db.executescript("""
                CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT,
                    epoch REAL NOT NULL, author TEXT NOT NULL,
                    topic TEXT NOT NULL DEFAULT '', text TEXT NOT NULL,
                    reply_to INTEGER, root_id INTEGER, amends INTEGER, amended_by INTEGER);
                INSERT INTO messages(epoch,author,topic,text) VALUES(12,'old','tag','original');
                INSERT INTO messages(epoch,author,text,reply_to,root_id) VALUES(13,'peer','reply',1,1);
                CREATE TABLE row_updates (n INTEGER);
                INSERT INTO row_updates VALUES(0);
                CREATE TRIGGER detect_updates AFTER UPDATE ON messages BEGIN
                    UPDATE row_updates SET n=n+1; END;
            """)
            before = db.execute("SELECT * FROM messages").fetchall()
        migrated = board.Board(path)
        with fresh._db() as db:
            schema = [tuple(r) for r in db.execute("PRAGMA table_info(messages)")]
            fts = [tuple(r) for r in db.execute("PRAGMA table_info(messages_fts)")]
        with migrated._db() as db:
            self.assertEqual([tuple(r) for r in db.execute("PRAGMA table_info(messages)")], schema)
            rows = [tuple(r) for r in db.execute("SELECT * FROM messages ORDER BY id")]
            self.assertEqual([r[:-2] for r in rows], before)
            self.assertEqual([r[-2:] for r in rows], [("elio-zeta", "agent"), ("elio-zeta", "agent")])
            self.assertEqual([tuple(r) for r in db.execute("PRAGMA table_info(messages_fts)")], fts)
            self.assertEqual([r[2] for r in db.execute("PRAGMA index_info(idx_messages_channel)")],
                             ["channel", "id"])
            self.assertEqual(db.execute("SELECT n FROM row_updates").fetchone()[0], 0)
        reopened = board.Board(path)
        with reopened._db() as db:
            self.assertEqual([tuple(r) for r in db.execute("SELECT * FROM messages ORDER BY id")], rows)
            self.assertEqual(db.execute("SELECT n FROM row_updates").fetchone()[0], 0)
        self.assertEqual(reopened.post("new", "next")["id"], 3)

    def test_membership_matrix_and_key_round_trips(self):
        keys = board.Keys(KEY, self.tmp)
        owner = keys.add("hamish-hermes", "Hamish", ["*"])
        peer = keys.add("will-hermes", "Will", ["elio-zeta"])
        old = keys.add("old", "Older ring")
        entries = keys.entries()
        entries[-1].pop("channels")
        keys._write(entries)
        self.assertEqual(keys.resolve(KEY), ("legacy", True, True))
        for key, identity, channels in ((owner, "hamish-hermes", ["*"]),
                                         (peer, "will-hermes", ["elio-zeta"]),
                                         (old, "old", ["elio-zeta"]), (KEY, "legacy", ["elio-zeta"])):
            self.assertEqual(keys.resolve(key)[0], identity)
            self.assertEqual(keys.channels_for(identity), channels)
            self.assertTrue(keys.may_read(identity, "elio-zeta"))
            self.assertEqual(keys.may_read(identity, "future"), identity == "hamish-hermes")
        keys.add("temporary", channels=["another", "elio-zeta"])
        self.assertEqual(keys.revoke("temporary"), 1)
        self.assertEqual(keys.entries(), entries)
        self.assertEqual(keys.default_channel("hamish-hermes"), "elio-zeta")
        self.assertEqual(keys.default_channel("old"), "elio-zeta")

    def test_a_new_identity_lands_in_the_public_channel_not_a_direct_one(self):
        # The whole point of channels is that a peer added later cannot read the
        # conversation that was already happening. A default of `will` would make
        # one careless `key add` leak the direct channel to a flatmate.
        keys = board.Keys(KEY, self.tmp)
        keys.add("hamish-hermes", "Hamish", ["*"])
        keys.add("will-hermes", "Will", ["elio-zeta"])
        flatmate = keys.add("cole-hermes", "Cole")
        self.assertEqual(keys.resolve(flatmate)[0], "cole-hermes")
        self.assertEqual(keys.channels_for("cole-hermes"), ["house"])
        self.assertTrue(keys.may_read("cole-hermes", "house"))
        self.assertFalse(keys.may_read("cole-hermes", "elio-zeta"),
                         "a new identity must not read a direct channel by default")


class ChannelAPITest(unittest.TestCase):
    conn = BoardTest.conn
    call = BoardTest.call
    upload = BoardTest.upload
    raw_get = BoardTest.raw_get
    start_server = PhaseOneTest.start_server
    post = PhaseOneTest.post
    cli = PhaseOneTest.cli
    remote_cli = PhaseOneTest.remote_cli

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="agent-board-channels-api-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.store = board.Board(os.path.join(self.tmp, "board.db"))
        self.keys = board.Keys(KEY, self.tmp)
        self.owner = self.keys.add("hamish-hermes", "Hamish", ["*"])
        self.peer = self.keys.add("will-hermes", "Will", ["elio-zeta"])
        self.multi = self.keys.add("other", "Other", ["private", "elio-zeta"])
        self.start_server()

    def seed(self):
        public = self.post("Visible narwhal evidence in the shared conversation.",
                           key=self.owner, topic="same", ask_to="will-hermes")
        secret = self.post("Hidden narwhal secret-codename inside another audience.",
                           key=self.owner, channel="private", topic="same", ask_to="other")
        return public, secret

    def test_default_write_channels_and_read_union(self):
        self.assertEqual(self.post(key=self.owner)["channel"], "elio-zeta")
        self.assertEqual(self.post(key=self.peer)["channel"], "elio-zeta")
        self.assertEqual(self.post(key=self.multi)["channel"], "private")
        self.assertEqual(self.post(author="any-author")["channel"], "elio-zeta")
        for key, expected in ((self.owner, {"elio-zeta", "private"}), (self.multi, {"elio-zeta", "private"}),
                              (self.peer, {"elio-zeta"}), (KEY, {"elio-zeta"})):
            status, data = self.call("GET", "/api/messages", key)
            self.assertEqual(status, 200)
            self.assertEqual({m["channel"] for m in data["messages"]}, expected)
        entries = self.keys.entries()
        entries[1].pop("channels")
        self.keys._write(entries)
        self.assertEqual(self.call("GET", "/api/messages?channel=private", self.peer)[0], 403)

    def test_forbidden_channels_for_posts_and_all_read_surfaces(self):
        self.seed()
        paths = ("/api/messages", "/api/messages.md", "/api/messages.json", "/api/search?q=narwhal",
                 "/api/search.json?q=narwhal", "/api/digest", "/api/digest.md", "/api/digest.json",
                 "/api/messages/threads", "/api/asks")
        for key in (KEY, self.peer):
            status, data = self.call("POST", "/api/messages", key,
                                     {"text": "forbidden", "channel": "private"})
            self.assertEqual(status, 403)
            self.assertIn("private", data["error"])
            for path in paths:
                with self.subTest(key=key == KEY, path=path):
                    status, data = self.call("GET", path + ("&" if "?" in path else "?") + "channel=private", key)
                    self.assertEqual(status, 403)
                    self.assertIn("private", data["error"])

    def test_empty_query_values_keep_existing_search_alias_behavior(self):
        self.seed()
        status, data = self.call("GET", "/api/search?q=&search=narwhal", self.peer)
        self.assertEqual(status, 200)
        self.assertEqual(data["count"], 1)

    def test_unknown_authorized_channel_returns_empty_results(self):
        self.seed()
        for path, field in (("/api/messages", "messages"), ("/api/search?q=narwhal", "results"),
                            ("/api/messages/threads", "threads"), ("/api/asks", "asks")):
            status, data = self.call("GET", path + ("&" if "?" in path else "?") + "channel=unknown", self.owner)
            self.assertEqual(status, 200)
            self.assertEqual(data[field], [])
        status, data = self.call("GET", "/api/digest?channel=unknown", self.owner)
        self.assertEqual((status, data["entries"], data["text"]), (200, 0, ""))
        self.assertNotIn("secret-codename", self.call("GET", "/api/messages.md?channel=unknown", self.owner)[1])
        # Nonexistent channels still require membership: do not bypass the 403 rule.
        self.assertEqual(self.call("GET", "/api/messages?channel=unknown", self.peer)[0], 403)

    def test_topics_are_counted_per_channel_and_scoped_to_membership(self):
        # The pane's topic chips come from here. A topic list drawn from the whole board
        # would mix rooms the reader is not looking at, which is the flat-stream problem
        # one level up.
        # Explicit channels: the module default is `elio-zeta`, and `will` is only an
        # alias for it now, so the fixture names the rooms it means.
        self.post("narwhal one", key=self.owner, channel="elio-zeta", topic="same")
        self.post("narwhal two", key=self.owner, channel="elio-zeta", topic="same")
        self.post("a different thread", key=self.owner, channel="elio-zeta", topic="other")

        status, data = self.call("GET", "/api/topics?channel=elio-zeta", self.owner)
        self.assertEqual(status, 200)
        counts = {row["topic"]: row["messages"] for row in data["topics"]}
        self.assertEqual(counts, {"same": 2, "other": 1})
        # Highest count first, so the biggest conversation is the first chip.
        self.assertEqual([row["topic"] for row in data["topics"]][0], "same")

        scoped = {row["topic"] for row in self.call("GET", "/api/topics?channel=private", self.owner)[1]["topics"]}
        self.assertNotIn("other", scoped)

        # The peer is not in `private`, so it cannot ask for that room's topics.
        self.assertEqual(self.call("GET", "/api/topics?channel=private", self.peer)[0], 403)
        peer_topics = {row["topic"] for row in self.call("GET", "/api/topics", self.peer)[1]["topics"]}
        self.assertEqual(peer_topics, {"same", "other"})

    def test_search_digest_markdown_threads_and_asks_do_not_leak(self):
        public, secret = self.seed()
        paths = ("/api/messages", "/api/messages.md", "/api/messages?search=narwhal", "/api/search?q=narwhal",
                 "/api/digest", "/api/digest.md", "/api/messages/threads", "/api/asks")
        for path in paths:
            for key in (self.peer, KEY):
                status, data = self.call("GET", path, key)
                self.assertEqual(status, 200)
                rendered = json.dumps(data)
                self.assertNotIn("secret-codename", rendered, path)
                self.assertNotIn('"private"', rendered, path)
        result = self.call("GET", "/api/search?q=narwhal", self.peer)[1]["results"]
        self.assertEqual([m["id"] for m in result], [public["id"]])
        self.assertIn("«narwhal»", result[0]["snippet"])
        self.assertEqual(self.call("GET", "/api/digest", self.peer)[1]["entries"], 1)
        self.assertEqual(self.call("GET", "/api/asks?channel=private", self.owner)[1]["asks"][0]["msg_id"], secret["id"])
        self.assertEqual(self.call("GET", "/api/messages?topic=same&channel=private", self.owner)[1]["messages"][0]["topic"], "same")
        # Exercise the legacy FTS snippet fallback with no paragraph chunks.
        with self.store._db() as db:
            db.execute("DELETE FROM chunk_fts")
        result = self.call("GET", "/api/search?q=narwhal", self.peer)[1]["results"]
        self.assertEqual([m["id"] for m in result], [public["id"]])
        self.assertNotIn("secret-codename", json.dumps(result))

    def test_sql_filters_before_message_conversion_and_pagination(self):
        public, _ = self.seed()
        later = self.post("Second readable narwhal evidence for pagination.", key=self.peer)
        original = self.store._row
        def check_row(row, files=None):
            self.assertEqual(row["channel"], "elio-zeta", "forbidden row fetched before Python conversion")
            return original(row, files)
        with mock.patch.object(self.store, "_row", side_effect=check_row):
            for path in ("/api/messages?limit=1", "/api/messages?before=999&limit=1", "/api/search?q=narwhal&limit=1",
                         "/api/digest", "/api/messages?search=narwhal"):
                self.assertEqual(self.call("GET", path, self.peer)[0], 200)
        data = self.call("GET", "/api/messages?limit=1", self.peer)[1]
        self.assertTrue(data["has_more"])
        self.assertEqual(data["messages"][0]["id"], public["id"])
        data = self.call("GET", "/api/messages?since=%d&limit=1" % public["id"], self.peer)[1]
        self.assertFalse(data["has_more"])
        self.assertEqual(data["messages"][0]["id"], later["id"])

    def test_long_poll_does_not_return_foreign_messages(self):
        self.post("Hidden", key=self.owner, channel="private")
        started = time.monotonic()
        status, data = self.call("GET", "/api/messages?wait=0.15", self.peer)
        self.assertEqual((status, data["messages"]), (200, []))
        self.assertGreaterEqual(time.monotonic() - started, 0.1)

    def test_reply_and_amend_channels_and_whole_thread(self):
        root = self.post("Root", key=self.owner, channel="private", topic="tag")
        child = self.post("Child", key=self.multi, channel="private", reply_to=root["id"])
        for field in ("reply_to", "amends"):
            status, data = self.call("POST", "/api/messages", self.owner,
                                     {"text": "wrong audience", "channel": "elio-zeta", field: child["id"]})
            self.assertEqual(status, 400)
            self.assertIn("channel", data["error"])
        amended = self.post("Amended", key=self.owner, channel="private", amends=root["id"])
        self.assertEqual(amended["topic"], "tag")
        status, data = self.call("GET", "/api/messages?thread=%d" % child["id"], self.multi)
        self.assertEqual(status, 200)
        self.assertEqual([m["id"] for m in data["messages"]], [root["id"], child["id"], amended["id"]])
        self.assertEqual(self.call("GET", "/api/messages?thread=%d" % root["id"], self.peer)[1]["messages"], [])
        code, out, error = self.remote_cli("read", "--thread", str(child["id"]), "--channel", "private",
                                           "--key", self.multi, "--json", "--limit", "1")
        self.assertEqual(code, 0, error)
        self.assertEqual(json.loads(out)["count"], 3)

    def test_files_follow_message_audience_and_authenticated_upload_owner(self):
        _, own = self.upload("own.txt", b"peer upload", key=self.peer)
        own_id = own["file"]["id"]
        _, other = self.upload("other.txt", b"other upload", key=self.multi)
        other_id = other["file"]["id"]
        path = "/api/files/%d" % other_id
        self.assertEqual(self.raw_get(path, self.peer)[0], 404)
        self.assertEqual(self.raw_get(path, self.multi)[0], 200)
        self.assertEqual(self.raw_get(path, self.owner)[0], 200)
        self.assertEqual([f["id"] for f in self.call("GET", "/api/files", self.peer)[1]["files"]], [own_id])
        self.post("Private attachment", key=self.owner, channel="private", files=[own_id, other_id])
        # Even an uploader loses access once all attachments belong to a foreign audience.
        self.assertEqual(self.raw_get("/api/files/%d" % own_id, self.peer)[0], 404)
        self.assertEqual(self.call("GET", "/api/files", self.peer)[1]["files"], [])
        self.assertEqual(self.call("POST", "/api/messages", self.peer,
                                   {"text": "steal attachment", "files": [other_id]})[0], 400)
        self.post("Shared attachment", key=self.owner, files=[other_id])
        self.assertEqual(self.raw_get(path, self.peer)[1], b"other upload")
        self.assertEqual([f["id"] for f in self.call("GET", "/api/files", self.peer)[1]["files"]], [other_id])
        # The old public uploader label must not act as authenticated ownership.
        rec = self.store.store_file("spoof.txt", "text/plain", b"spoof", "will-hermes", "other")
        self.assertEqual(self.raw_get(rec["url"], self.peer)[0], 404)

    def test_asks_cannot_be_closed_from_a_foreign_audience(self):
        _, secret = self.seed()
        visible = self.post("Visible answer", key=self.peer)
        path = "/api/asks/%d/close" % secret["ask_id"]
        self.assertEqual(self.call("POST", path, self.peer, {"msg_id": visible["id"]})[0], 400)
        self.assertEqual(self.call("POST", "/api/messages", self.peer,
                                   {"text": "answer", "answers": secret["ask_id"]})[0], 400)
        self.assertEqual(self.call("POST", path, self.owner, {"msg_id": visible["id"]})[0], 400)
        self.assertEqual(self.call("GET", "/api/asks?channel=private", self.owner)[1]["count"], 1)

    def test_invalid_channel_boundaries(self):
        for slug in ("", "UPPER", "has space", "a/b", "-first", "a" * 33, "*", "é", "a\n"):
            with self.subTest(slug=slug):
                self.store.rates.clear()
                status, data = self.call("POST", "/api/messages", self.owner, {"text": "bad", "channel": slug})
                self.assertEqual(status, 400)
                self.assertIn("channel must match", data["error"])
                status, data = self.call("GET", "/api/messages?channel=" + board.urllib.parse.quote(slug), self.owner)
                self.assertEqual(status, 400)
                self.assertIn("channel must match", data["error"])
                with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                    board.main(["read", "--channel", slug])
                self.assertEqual(error.exception.code, 2)
        for value in (None, 7, [], True):
            self.assertEqual(self.call("POST", "/api/messages", self.owner, {"text": "bad", "channel": value})[0], 400)
        self.assertEqual(self.call("GET", "/api/messages?channel=elio-zeta&channel=private", self.owner)[0], 400)
        self.assertEqual(self.post(key=self.owner, channel="a_0-" + "z" * 28)["channel"], "a_0-" + "z" * 28)

    def test_health_channels_discovery_and_channels_cli_no_secrets(self):
        self.seed()
        status, data = self.call("GET", "/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(data["channels"], {"elio-zeta": 1, "private": 1})
        for field in ("messages", "latest_id", "topics", "files", "delivery", "uptime_s", "inflight", "max_concurrent"):
            self.assertIn(field, data)
        code, out, error = self.remote_cli("channels", "--key", self.owner)
        self.assertEqual(code, 0, error)
        self.assertIn("private  1  live  hamish-hermes, other", out)
        # The slug first, the human label only where it adds something: the listing is
        # what commands are copied from, so a label can never replace the slug.
        self.assertIn("elio-zeta (Elio ↔ Zeta)  1  live  hamish-hermes, legacy, other, will-hermes", out)
        for secret in (KEY, self.owner, self.peer, self.multi):
            self.assertNotIn(secret, out)
        code, out, _ = self.remote_cli("channels", "--key", self.peer)
        self.assertEqual(code, 0)
        self.assertNotIn("private", out)
        # Wildcard membership includes future channels without another key-ring edit.
        self.post(key=self.owner, channel="future")
        self.assertIn("future", self.remote_cli("channels", "--key", self.owner)[1])
        self.assertNotIn("house", self.call("GET", "/api/health")[1]["channels"])

    def test_channels_cli_idle_states_match_desktop_boundaries(self):
        # Read the pane's constants without importing its separate dependencies.
        with open(os.path.join(HERE, "..", "plugin", "dashboard", "plugin_api.py")) as fh:
            tree = ast.parse(fh.read())
        pane = {node.targets[0].id: ast.literal_eval(node.value) for node in tree.body
                if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id in ("QUIET_DAYS", "STALE_DAYS")}
        self.assertEqual((board.QUIET_DAYS, board.STALE_DAYS), (pane["QUIET_DAYS"], pane["STALE_DAYS"]))
        for idle, expected in ((None, "unused"), (0, "live"), (13.9, "live"),
                               (14, "quiet"), (59.9, "quiet"), (60, "stale"), (90, "stale")):
            with self.subTest(idle=idle):
                rows = [{"channel": "room", "messages": 0 if idle is None else 71,
                         "idle_days": idle, "identities": ["hamish-hermes", "legacy", "will-hermes"]}]
                with mock.patch.object(board, "request", return_value=(200, json.dumps({"channels": rows}))), \
                     contextlib.redirect_stdout(io.StringIO()) as out:
                    self.assertEqual(board.main(["channels", "--url", "http://unused", "--key", KEY]), 0)
                self.assertEqual(out.getvalue(), "room  %d  %s  hamish-hermes, legacy, will-hermes\n" %
                                 (rows[0]["messages"], expected))

    def test_cli_channel_flags_text_file_and_key_membership_round_trips(self):
        note = os.path.join(self.tmp, "note.txt")
        with open(note, "w") as fh:
            fh.write("A narwhal note from the unchanged text-file workflow.")
        code, _, error = self.remote_cli("post", "--text-file", note, "--key", self.peer)
        self.assertEqual(code, 0, error)
        self.assertEqual(self.store.fetch()[0]["channel"], "elio-zeta")
        code, _, error = self.remote_cli("post", "--text-file", note, "--channel", "private", "--key", self.multi)
        self.assertEqual(code, 0, error)
        self.assertEqual(self.store.fetch()[1]["channel"], "private")
        self.assertEqual(self.remote_cli("amend", "2", "correction", "--channel", "private", "--key", self.multi)[0], 0)
        for argv in (("read", "--json"), ("search", "narwhal", "--json"), ("digest",), ("threads",), ("open", "--json")):
            code, _, error = self.remote_cli(*argv, "--channel", "private", "--key", self.owner)
            self.assertIn(code, (0, 1), error)  # open returns 1 for an empty ledger
            self.assertNotIn("failed", error)
        # Delivery can select one channel’s receipt state.
        self.assertEqual(self.remote_cli("delivery", "--state", "--channel", "private", "--key", self.owner)[0], 0)
        with mock.patch.object(board, "request", side_effect=KeyboardInterrupt) as request:
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(KeyboardInterrupt):
                board.main(["watch", "--channel", "private", "--key", self.owner, "--url", "http://127.0.0.1:%d" % self.port])
            self.assertIn("channel=private", request.call_args.args[0])
        code, _, error = self.cli("key", "add", "--identity", "new-peer", "--label", "New", "--channel", "elio-zeta", "--channel", "private")
        self.assertEqual(code, 0, error)
        self.assertEqual(self.keys.channels_for("new-peer"), ["elio-zeta", "private"])
        before = self.keys.entries()
        self.assertEqual(self.cli("key", "add", "--identity", "remove-me")[0], 0)
        self.assertEqual(self.cli("key", "revoke", "--identity", "remove-me")[0], 0)
        self.assertEqual(self.keys.entries(), before)

    def test_channel_acks_are_independent_and_legacy_fallback_is_unchanged(self):
        public, secret = self.seed()
        for value in ("conduit", "does-not-exist", "", None, 5, [], {}):
            status, receipt = self.call("POST", "/api/ack", self.owner,
                                        {"id": secret["id"], "channel": value})
            self.assertEqual(status, 200, receipt)
            self.assertNotIn("channel", receipt)
        self.call("POST", "/api/ack", self.owner, {"id": public["id"], "channel": "elio-zeta"})
        data = self.call("GET", "/api/delivery", self.owner)[1]
        mine = {r["channel"]: r for r in data["channels"] if r["identity"] == "hamish-hermes"}
        self.assertEqual(data["me"]["acked_id"], secret["id"])
        self.assertEqual(mine["elio-zeta"]["acked_id"], public["id"])
        # No receipt of its own: the whole-board floor covers it, and it says so
        # rather than reporting a backlog that was delivered before channels existed.
        self.assertEqual(mine["private"]["acked_id"], secret["id"])
        self.assertEqual(mine["private"]["ack_source"], "whole-board")
        self.assertEqual(mine["private"]["pending_count"], 0)
        self.assertEqual(mine["private"]["state"], "ok")
        self.assertEqual(self.call("POST", "/api/ack", self.peer,
                                   {"id": secret["id"], "channel": "private"})[0], 403)
        status, receipt = self.call("POST", "/api/ack", self.owner,
                                    {"id": secret["id"], "channel": "private"})
        self.assertEqual(status, 200)
        self.assertEqual(receipt["channel"], "private")
        self.call("POST", "/api/ack", self.owner, {"id": 0, "channel": "private"})
        data = self.call("GET", "/api/delivery?channel=private", self.owner)[1]
        self.assertEqual({r["channel"] for r in data["channels"]}, {"private"})
        self.assertEqual(next(r for r in data["channels"] if r["identity"] == "hamish-hermes")["acked_id"], secret["id"])
        peer_data = self.call("GET", "/api/delivery", self.peer)[1]
        self.assertEqual({r["channel"] for r in peer_data["channels"]}, {"elio-zeta"})
        self.assertEqual(self.call("GET", "/api/delivery?channel=private", self.peer)[0], 403)

    def test_a_whole_board_ack_covers_rooms_it_predates(self):
        # The shape of the 1.2 -> 1.3 cutover: every receipt in the database is
        # whole-board, no room has one of its own, and the board must not report a
        # backlog that was delivered before rooms existed.
        self.seed()
        self.call("POST", "/api/ack", self.owner, {"id": self.store.latest_id()})
        data = self.call("GET", "/api/delivery", self.owner)[1]
        rows = [r for r in data["channels"] if r["identity"] == "hamish-hermes"]
        self.assertTrue(rows, "no per-channel rows to inspect")
        for row in rows:
            self.assertEqual((row["state"], row["pending_count"], row["ack_source"]),
                             ("ok", 0, "whole-board"), row["channel"])

    def test_delivery_channel_grace_unknown_and_cli_blocks(self):
        self.seed()
        with self.store._db() as db:
            db.execute("UPDATE messages SET epoch=?", (time.time() - 700,))
        self.store.ack("hamish-hermes", 0, "elio-zeta")
        data = self.call("GET", "/api/delivery", self.owner)[1]
        mine = {r["channel"]: r for r in data["channels"] if r["identity"] == "hamish-hermes"}
        # An ack in `will` says nothing about `private`, and there is no
        # whole-board receipt here either, so this room is still unknown: unknown
        # is reserved for "no evidence", and a peer that never acked is never
        # called behind.
        self.assertEqual(mine["private"]["state"], "unknown")
        self.assertIsNone(mine["private"]["ack_source"])
        self.assertEqual(self.remote_cli("delivery", "--channels-state", "--key", self.owner)[1].strip(), "ok")
        self.store.ack("will-hermes", 0, "elio-zeta")
        self.assertEqual(self.remote_cli("delivery", "--channels-state", "--key", self.owner)[1].strip(), "behind")
        code, output, _ = self.remote_cli("delivery", "--key", self.owner)
        self.assertEqual(code, 1)
        self.assertIn("channel elio-zeta", output)
        self.assertIn("channel private", output)
        code, output, _ = self.remote_cli("delivery", "--channel", "private", "--key", self.owner)
        self.assertEqual(code, 2)
        self.assertNotIn("channel elio-zeta", output)
        self.assertEqual(self.remote_cli("delivery", "--state", "--channel", "elio-zeta", "--key", self.peer)[1].strip(), "behind")
        self.store.ack("will-hermes", self.store.latest_id(), "elio-zeta")
        self.assertEqual(self.remote_cli("delivery", "--state", "--channel", "elio-zeta", "--key", self.peer)[1].strip(), "ok")

    def test_web_channel_controls_are_self_contained(self):
        status, html = self.call("GET", "/")
        self.assertEqual(status, 200)
        for text in ('<select id="channel"', "fetch('/api/channels'", "channel: $('#channel').value", "channelCount > 1",
                     "esc(m.channel)", "channelQuery()", "setReply(null)"):
            self.assertIn(text, html)
        self.assertNotIn('<script src=', html)


class HumanVoiceStorageTest(unittest.TestCase):
    def test_existing_channel_database_migrates_via_without_rewriting_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "old.db")
            with sqlite3.connect(path) as db:
                db.executescript("""
                    CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT,
                        epoch REAL NOT NULL, author TEXT NOT NULL,
                        topic TEXT NOT NULL DEFAULT '', text TEXT NOT NULL,
                        reply_to INTEGER, root_id INTEGER, amends INTEGER, amended_by INTEGER,
                        channel TEXT NOT NULL DEFAULT 'elio-zeta');
                    INSERT INTO messages(epoch,author,text,channel)
                        VALUES(123,'hamish-hermes','Original words','private');
                    CREATE TABLE row_updates (n INTEGER);
                    INSERT INTO row_updates VALUES(0);
                    CREATE TRIGGER detect_updates AFTER UPDATE ON messages BEGIN
                        UPDATE row_updates SET n=n+1; END;
                """)
                before = db.execute("SELECT * FROM messages").fetchone()
            store = board.Board(path)
            human = store.post("hamish-hermes", "Actually typed", via="human")
            store = board.Board(path)
            fresh = board.Board(os.path.join(directory, "fresh.db"))
            with store._db() as db, fresh._db() as new_db:
                schema = [tuple(r) for r in db.execute("PRAGMA table_info(messages)")]
                self.assertEqual(schema, [tuple(r) for r in new_db.execute("PRAGMA table_info(messages)")])
                self.assertEqual(schema[-1][1:5], ("via", "TEXT", 1, "'agent'"))
                row = tuple(db.execute("SELECT * FROM messages WHERE id=1").fetchone())
                self.assertEqual(row, before + ("agent",))
                self.assertEqual(db.execute("SELECT n FROM row_updates").fetchone()[0], 0)
                db.execute("INSERT INTO messages(epoch,author,text) VALUES(124,'old-client','no via')")
                self.assertEqual(db.execute("SELECT via FROM messages WHERE id=3").fetchone()[0], "agent")
            self.assertEqual(store.fetch()[1]["via"], "human")
            self.assertEqual(human["id"], 2)


class HumanVoiceInboxTest(unittest.TestCase):
    conn = BoardTest.conn
    call = BoardTest.call
    start_server = PhaseOneTest.start_server
    post = PhaseOneTest.post
    cli = PhaseOneTest.cli
    remote_cli = PhaseOneTest.remote_cli
    setUp = ChannelAPITest.setUp

    def inbox(self, key=None, channel=None):
        path = "/api/inbox" + ("?channel=" + channel if channel else "")
        status, data = self.call("GET", path, key or self.owner)
        self.assertEqual(status, 200, data)
        return data

    def test_human_round_trip_and_default_agent_on_all_read_surfaces(self):
        human = self.post("Human narwhal paragraph with enough words for paragraph search.",
                          key=self.owner, via="human")
        agent = self.post("Agent narwhal paragraph in the same thread.", key=self.owner,
                          reply_to=human["id"])
        self.assertEqual((human["author"], human["via"]), ("hamish-hermes", "human"))
        self.assertEqual(agent["via"], "agent")
        for path, field in (("/api/messages", "messages"),
                            ("/api/messages.json?thread=%d" % human["id"], "messages"),
                            ("/api/search?q=narwhal", "results"),
                            ("/api/messages?search=narwhal", "messages")):
            with self.subTest(path=path):
                status, data = self.call("GET", path, self.owner)
                self.assertEqual(status, 200)
                self.assertEqual({m["id"]: m["via"] for m in data[field]},
                                 {human["id"]: "human", agent["id"]: "agent"})
        for path in ("/api/messages.md", "/api/digest", "/api/digest.json", "/api/digest.md"):
            status, data = self.call("GET", path, self.owner)
            self.assertEqual(status, 200)
            text = data["text"] if isinstance(data, dict) else data
            self.assertIn("hamish-hermes (typed)", text)
            self.assertEqual(text.count("(typed)"), 1)

    def test_invalid_via_is_400_without_posting(self):
        for value in ("robot", "Human", "", None, False, 1, [], {}):
            with self.subTest(via=value):
                status, data = self.call("POST", "/api/messages", self.owner,
                                         {"text": "invalid voice", "via": value})
                self.assertEqual(status, 400, data)
                self.assertIn("via", data["error"])
        self.assertEqual(self.store.count(), 0)

    def test_say_and_post_amend_via_cli(self):
        for argv in (("say", "These are my words"),
                     ("post", "These too", "--via", "human"),
                     ("amend", "1", "Corrected words", "--via", "human"),
                     ("post", "Agent words")):
            code, output, error = self.remote_cli(*argv, "--key", self.owner, "--channel", "elio-zeta")
            self.assertEqual(code, 0, error)
            self.assertIn("posted #", output)
        messages = self.store.fetch()
        self.assertEqual([m["via"] for m in messages], ["human", "human", "human", "agent"])
        self.assertEqual({m["author"] for m in messages}, {"hamish-hermes"})
        self.assertEqual(messages[2]["amends"], 1)

    def test_via_does_not_change_author_or_channel_permissions(self):
        for via in ("human", "agent"):
            self.assertEqual(self.call("POST", "/api/messages", self.peer,
                {"text": "spoof", "author": "hamish-hermes", "via": via})[0], 403)
            self.assertEqual(self.call("POST", "/api/messages", self.peer,
                {"text": "private", "channel": "private", "via": via})[0], 403)

    def test_inbox_shape_order_ageing_and_closed_asks(self):
        now = 1800000000.0
        incoming = []
        for days in (0, 6.999, 7, 30, 30.001):
            msg = self.post("Question aged %s days" % days, key=self.peer, ask_to="hamish-hermes")
            incoming.append((msg, days))
        outgoing = self.post("Please reply", key=self.owner, ask_to="will-hermes")
        closed = self.post("Already answered", key=self.peer, ask_to="hamish-hermes")
        self.store.close_ask(closed["ask_id"], closed["id"])
        with self.store._db() as db:
            for msg, days in incoming + [(outgoing, 6.2)]:
                db.execute("UPDATE asks SET opened_epoch=? WHERE id=?", (now - days * 86400, msg["ask_id"]))
        with mock.patch.object(board.time, "time", return_value=now):
            data = self.inbox(channel="elio-zeta")
        self.assertEqual(set(data), {"asks", "asks_by_me", "latest", "counts"})
        self.assertEqual(data["counts"], {"open_to_me": 5, "open_by_me": 1})
        self.assertEqual([a["msg_id"] for a in data["asks"]], [m["id"] for m, _ in reversed(incoming)])
        self.assertEqual([a["ageing"] for a in data["asks"]], ["stale", "warn", "warn", "ok", "ok"])
        for ask, (msg, days) in zip(data["asks"], reversed(incoming)):
            self.assertEqual(ask["id"], msg["ask_id"])
            self.assertEqual(ask["from"], "will-hermes")
            self.assertEqual(ask["channel"], "elio-zeta")
            self.assertEqual(ask["text"], msg["text"])
            self.assertEqual(ask["opened"], board.now_iso(now - days * 86400))
            self.assertAlmostEqual(ask["age_days"], days)
        self.assertEqual(data["asks_by_me"][0]["to"], "will-hermes")
        self.assertAlmostEqual(data["asks_by_me"][0]["age_days"], 6.2)
        self.assertEqual(data["asks_by_me"][0]["ageing"], "ok")
        self.assertEqual(data["latest"], {"channel": "elio-zeta", "id": closed["id"], "ts": closed["ts"]})

    def test_inbox_omits_unreadable_asks_and_latest_and_filters_channel(self):
        visible = self.post("Visible ask", key=self.owner, ask_to="will-hermes")
        hidden = self.post("Hidden ask", key=self.owner, channel="private", ask_to="will-hermes")
        data = self.inbox(self.peer)
        self.assertEqual([a["msg_id"] for a in data["asks"]], [visible["id"]])
        self.assertEqual(data["latest"]["id"], visible["id"])
        self.assertEqual(data["counts"], {"open_to_me": 1, "open_by_me": 0})
        # Authorship alone must not make an unreadable outgoing ask visible either.
        entries = self.keys.entries()
        entries[0]["channels"] = ["elio-zeta"]
        self.keys._write(entries)
        self.assertEqual([a["msg_id"] for a in self.inbox()["asks_by_me"]], [visible["id"]])
        self.assertNotIn(hidden["text"], json.dumps(data))
        filtered = self.inbox(self.peer, "elio-zeta")
        self.assertEqual([a["msg_id"] for a in filtered["asks"]], [visible["id"]])
        self.assertEqual(filtered["latest"], data["latest"])
        self.assertEqual(self.call("GET", "/api/inbox?channel=private", self.peer)[0], 403)
        self.assertEqual(self.call("GET", "/api/inbox")[0], 401)
        for suffix in ("?channel=", "?channel=BAD", "?channel=elio-zeta&channel=private"):
            self.assertEqual(self.call("GET", "/api/inbox" + suffix, self.owner)[0], 400)

    def test_inbox_has_no_ledger_limit_and_does_not_ack_on_view(self):
        for i in range(105):
            self.store.post("will-hermes", "Question %d" % i, ask_to="hamish-hermes")
        self.assertEqual(self.inbox()["counts"]["open_to_me"], 105)
        with self.store._db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM acks").fetchone()[0], 0)
        self.store.ack("hamish-hermes", 105, "elio-zeta")
        self.assertEqual(self.inbox()["counts"]["open_to_me"], 105)

    def test_empty_inbox_cli_and_json(self):
        code, output, error = self.remote_cli("inbox", "--channel", "elio-zeta", "--key", self.owner)
        self.assertEqual((code, output, error), (0, "inbox: nothing waiting\n", ""))
        code, output, error = self.remote_cli("inbox", "--json", "--key", self.owner)
        self.assertEqual((code, error), (0, ""))
        self.assertEqual(json.loads(output), {"asks": [], "asks_by_me": [],
            "latest": {"channel": None, "id": 0, "ts": None},
            "counts": {"open_to_me": 0, "open_by_me": 0}})

    def test_inbox_cli_lists_both_sides_and_summary(self):
        incoming = self.post("A question for you", key=self.peer, ask_to="hamish-hermes")
        outgoing = self.post("A question for Will", key=self.owner, ask_to="will-hermes")
        with self.store._db() as db:
            db.execute("UPDATE asks SET opened_epoch=? WHERE id=?",
                       (time.time() - 12 * 86400, incoming["ask_id"]))
        code, output, error = self.remote_cli("inbox", "--channel", "elio-zeta", "--key", self.owner)
        self.assertEqual((code, error), (0, ""))
        lines = output.splitlines()
        self.assertEqual(len(lines), 3)
        self.assertIn("[Elio ↔ Zeta]  12.0d warn  from will-hermes", lines[0])
        self.assertIn("to will-hermes", lines[1])
        self.assertEqual(lines[-1], "1 waiting on you, 1 you are waiting on, oldest 12d")
        code, output, error = self.remote_cli("inbox", "--json", "--key", self.peer)
        self.assertEqual((code, error), (0, ""))
        data = json.loads(output)
        self.assertEqual(data["asks"][0]["msg_id"], outgoing["id"])
        self.assertEqual(data["asks_by_me"][0]["msg_id"], incoming["id"])


class ChannelAckMigrationTest(unittest.TestCase):
    def test_channel_grace_boundary_and_whole_board_row_are_independent(self):
        with tempfile.TemporaryDirectory() as directory:
            store = board.Board(os.path.join(directory, "board.db"))
            will = store.post("will-hermes", "delivered", channel="elio-zeta")["id"]
            store.post("house-peer", "still pending", channel="house")
            with store._db() as db:
                db.execute("UPDATE messages SET epoch=1000")
            store.ack("hamish-hermes", 0)
            store.ack("hamish-hermes", will, "elio-zeta")
            store.ack("hamish-hermes", 0, "house")
            with mock.patch.object(board.time, "time", return_value=1600):
                data = store.delivery(["hamish-hermes"], "hamish-hermes")
                self.assertEqual({r["channel"]: r["state"] for r in data["channels"]}, {"elio-zeta": "ok", "house": "ok"})
            with mock.patch.object(board.time, "time", return_value=1601):
                data = store.delivery(["hamish-hermes"], "hamish-hermes")
                self.assertEqual(data["me"]["state"], "behind")
                self.assertEqual({r["channel"]: r["state"] for r in data["channels"]}, {"elio-zeta": "ok", "house": "behind"})
                self.assertEqual({r["channel"]: r["pending_count"] for r in data["channels"]}, {"elio-zeta": 0, "house": 1})

    def test_migration_preserves_legacy_row_timestamp_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "board.db")
            store = board.Board(path)
            with store._db() as db:
                db.execute("DROP TABLE acks")
                db.execute("CREATE TABLE acks (identity TEXT PRIMARY KEY, acked_id INTEGER NOT NULL, updated_epoch REAL NOT NULL)")
                db.execute("INSERT INTO acks VALUES ('will-hermes', 84, 123.5)")
            store = board.Board(path)
            store.ack("will-hermes", 2, "house")
            store = board.Board(path)
            with store._db() as db:
                rows = [tuple(r) for r in db.execute("SELECT identity, channel, acked_id, updated_epoch FROM acks ORDER BY channel")]
            self.assertEqual(rows[0], ("will-hermes", "*", 84, 123.5))
            self.assertEqual(rows[1][:3], ("will-hermes", "house", 2))
            self.assertEqual(len(rows), 2)


class InviteTest(unittest.TestCase):
    call = BoardTest.call
    conn = BoardTest.conn
    raw_get = BoardTest.raw_get
    cli = PhaseOneTest.cli
    remote_cli = PhaseOneTest.remote_cli
    start_server = PhaseOneTest.start_server

    def setUp(self):
        quiet = mock.patch.dict(os.environ, {"BOARD_QUIET": "1"})
        quiet.start()
        self.addCleanup(quiet.stop)
        self.tmp = tempfile.mkdtemp(prefix="agent-board-invite-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.store = board.Board(os.path.join(self.tmp, "board.db"))
        self.keys = board.Keys(KEY, self.tmp)
        self.owner = self.keys.add("hamish-hermes", "Hamish", ["*"])
        self.keys.add("will-hermes", "Will", ["elio-zeta"])
        self.start_server()

    def invite(self, *extra):
        return self.remote_cli("invite", "--identity", "cole-hermes", "--label", "Cole", *extra)

    def entry(self):
        return next(e for e in self.keys.entries() if e["identity"] == "cole-hermes")

    def test_default_upload_is_the_only_delivery_and_matches_local_sha256(self):
        local = {}
        real_upload = board.upload

        def inspect_upload(base, key, path, **kwargs):
            with open(path, "rb") as fh:
                local["blob"] = fh.read()
            local["path"] = path
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
            return real_upload(base, key, path, **kwargs)

        with mock.patch.object(board, "upload", side_effect=inspect_upload), \
             mock.patch.object(board, "request", wraps=board.request) as requests:
            code, out, err = self.invite()
        self.assertEqual((code, err), (0, ""))
        entry = self.entry()
        self.assertEqual(entry["channels"], ["house"])
        self.assertEqual(entry["label"], "Cole")
        self.assertNotIn(entry["key"], out + err)
        self.assertEqual(local["blob"].count(entry["key"].encode()), 1)
        self.assertFalse(os.path.exists(local["path"]))
        files = self.call("GET", "/api/files", KEY)[1]["files"]
        self.assertEqual(len(files), 1)
        rec = files[0]
        self.assertEqual(rec["name"], "invite.md")
        self.assertEqual(rec["ctype"], "text/markdown")
        self.assertEqual(rec["sha256"], hashlib.sha256(local["blob"]).hexdigest())
        self.assertEqual(self.raw_get(rec["url"], KEY)[1], local["blob"])
        self.assertIn("invited cole-hermes [house]  file #%d  sha256 %s\n" %
                      (rec["id"], rec["sha256"]), out)
        self.assertIn("key is in that file and nowhere else. Give it to Cole, not to the board.\n", out)
        self.assertIn("house  0  never  unused\n", out)
        self.assertNotIn("elio-zeta", out)
        self.assertEqual(self.store.count(), 0)
        self.assertTrue(all("/api/messages" not in c.args[0] for c in requests.call_args_list))
        with self.store._db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM message_files").fetchone()[0], 0)

    def test_repeatable_membership_is_exact_and_unknown_rooms_are_listed(self):
        code, out, err = self.invite("--channel", "future-room", "--channel", "house",
                                     "--channel", "future-room")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(self.entry()["channels"], ["future-room", "house"])
        key = self.entry()["key"]
        rows = self.call("GET", "/api/channels", key)[1]["channels"]
        self.assertEqual({r["channel"] for r in rows}, {"house", "future-room"})
        self.assertIn("future-room  0  never  unused\n", out)
        self.assertIn("future-room", self.remote_cli("channels", "--key", key)[1])
        self.assertEqual(self.call("GET", "/api/messages?channel=elio-zeta", key)[0], 403)
        self.assertEqual(self.call("POST", "/api/messages", key,
                                   {"text": "forbidden", "channel": "elio-zeta"})[0], 403)
        self.assertEqual(self.store.count(), 0)

    def test_dry_run_redacts_and_has_no_side_effects_even_with_new_data_directory(self):
        empty_data = os.path.join(self.tmp, "not-created")
        with mock.patch.object(board, "new_key") as generate, \
             mock.patch.object(board, "upload") as upload, \
             mock.patch.object(board, "request") as request, \
             mock.patch.object(board.tempfile, "mkstemp") as temporary:
            code, out, err = self.invite("--dry-run", "--data", empty_data)
        self.assertEqual((code, err), (0, ""))
        self.assertIn("```\n<KEY>\n```", out)
        self.assertNotIn("ab_", out)
        self.assertNotIn("elio-zeta", out)
        self.assertFalse(os.path.exists(empty_data))
        for operation in (generate, upload, request, temporary):
            operation.assert_not_called()
        self.assertEqual(len(self.keys.entries()), 2)
        self.assertEqual(self.store.list_files(), [])
        self.assertEqual(self.store.count(), 0)

    def test_wording_order_examples_and_explicit_privacy(self):
        code, text, err = self.invite("--dry-run")
        self.assertEqual((code, err), (0, ""))
        sections = ["shared message board between Hermes agents", "Your channel membership",
                    "## Calling the board", "## Etiquette", "## Your key"]
        self.assertEqual(sorted(text.index(s) for s in sections), [text.index(s) for s in sections])
        for required in ("http://127.0.0.1:%d" % self.port,
                         "yours alone and identifies you as `cole-hermes`",
                         "read and write `house` only; other channels exist and are none of your business",
                         "GET /api/messages?channel=house", "POST /api/messages",
                         '"text": "Hello from cole-hermes", "channel": "house"',
                         "board.py read --channel house", "X-Board-Key", "BOARD_KEY",
                         "few hundred characters", "one topic per message", "reply_to",
                         "`via`", "`agent`", "`human`", "a message, a repo or a log",
                         "git-tracked file"):
            self.assertIn(required, text)
        self.assertEqual(text.count("```sh\n"), 3)

    def test_default_invite_url_is_public_and_configurable_without_network_io(self):
        with mock.patch.dict(os.environ, {"BOARD_URL": ""}), \
             mock.patch.object(board, "request") as request:
            code, text, err = self.cli("invite", "--identity", "cole-hermes", "--dry-run")
        self.assertEqual((code, err), (0, ""))
        self.assertIn(board.DEFAULT_INVITE_URL, text)
        self.assertNotIn("127.0.0.1", text)
        request.assert_not_called()
        with mock.patch.dict(os.environ, {"BOARD_URL": "https://configured.example"}):
            code, text, err = self.cli("invite", "--identity", "cole-hermes", "--dry-run")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("https://configured.example", text)

    def test_invalid_identity_and_every_channel_rejected_before_key_write(self):
        with open(self.keys.path, "rb") as fh:
            before = fh.read()
        invalid = [("--identity", value) for value in ("", " cole", "cole ", "x" * 65, "legacy")]
        invalid += [("--channel", "house", "--channel", value)
                    for value in ("", "House", "../elio-zeta", "a" * 33, "all*", "a,b")]
        for extra in invalid:
            with self.subTest(extra=extra), mock.patch.object(board.Keys, "add") as add:
                try:
                    code, out, err = self.invite(*extra)
                except SystemExit as exc:
                    code = exc.code
                self.assertNotEqual(code, 0)
                add.assert_not_called()
            with open(self.keys.path, "rb") as fh:
                self.assertEqual(fh.read(), before)
        self.assertEqual(self.store.list_files(), [])

    def test_existing_identity_cannot_inherit_private_membership(self):
        old = self.keys.add("cole-hermes", "Cole", ["elio-zeta"])
        code, out, err = self.invite()
        self.assertEqual(code, 1)
        self.assertIn("identity already exists", err)
        self.assertNotIn(old, out + err)
        self.assertEqual(len(self.keys.entries()), 3)
        self.assertEqual(self.keys.channels_for("cole-hermes"), ["elio-zeta"])
        self.assertEqual(self.store.list_files(), [])

    def test_explicit_wildcard_warns_and_lists_owner_scope(self):
        code, out, err = self.invite("--channel", "*")
        self.assertEqual(code, 0)
        self.assertEqual(self.entry()["channels"], ["*"])
        self.assertEqual(len(err.splitlines()), 1)
        self.assertIn("owner access to every channel", err)
        self.assertIn("elio-zeta  0  never  unused", out)
        rec = self.store.list_files()[0]
        document = self.raw_get(rec["url"], KEY)[1].decode()
        self.assertIn("read and write all channels", document)
        self.assertNotIn("other channels exist and are none of your business", document)

    def test_channel_states_share_the_existing_cli_thresholds(self):
        for slug, days in (("live", 0), ("quiet", 14), ("stale", 60)):
            msg = self.store.post("fixture", "fixture", channel=slug)
            with self.store._db() as db:
                db.execute("UPDATE messages SET epoch=? WHERE id=?",
                           (time.time() - days * 86400, msg["id"]))
        code, out, err = self.invite("--channel", "live", "--channel", "quiet",
                                     "--channel", "stale", "--channel", "unused")
        self.assertEqual((code, err), (0, ""))
        for line in out.splitlines()[2:]:
            slug, count, timestamp, state = line.split()
            self.assertEqual(slug, state)
            self.assertEqual(count, "0" if slug == "unused" else "1")
            self.assertEqual(timestamp == "never", slug == "unused")

    def test_failed_upload_removes_new_key_and_temp_file_without_echoing_secret(self):
        paths = []

        def fail(base, key, path, **kwargs):
            paths.append(path)
            return None, "remote echoed secret: " + self.entry()["key"]

        with mock.patch.object(board, "upload", side_effect=fail):
            code, out, err = self.invite()
        self.assertEqual(code, 1)
        self.assertIn("invite upload failed", err)
        self.assertNotIn("remote echoed", out + err)
        self.assertEqual(len(self.keys.entries()), 2)
        self.assertTrue(paths)
        self.assertFalse(os.path.exists(paths[0]))
        self.assertEqual(self.store.count(), 0)

    def test_checksum_mismatch_is_failure_and_revokes_the_undelivered_key(self):
        real_upload = board.upload

        def corrupt(*args, **kwargs):
            rec, err = real_upload(*args, **kwargs)
            rec["sha256"] = "0" * 64
            return rec, err

        with mock.patch.object(board, "upload", side_effect=corrupt):
            code, out, err = self.invite()
        self.assertEqual(code, 1)
        self.assertIn("sha256 does not match", err)
        self.assertNotIn("invited cole-hermes", out)
        self.assertEqual(len(self.keys.entries()), 2)
        self.assertEqual(self.store.count(), 0)

    def test_failed_or_inexact_channel_discovery_rolls_back_before_upload(self):
        for response in ((401, '{}'),
                         (200, json.dumps({"channels": [{"channel": "elio-zeta"}]}))):
            with self.subTest(response=response), \
                 mock.patch.object(board, "request", return_value=response), \
                 mock.patch.object(board, "upload") as upload, \
                 mock.patch.object(board.tempfile, "mkstemp", wraps=board.tempfile.mkstemp) as temporary:
                code, out, err = self.invite()
            self.assertEqual(code, 1)
            self.assertIn("invite failed", err)
            upload.assert_not_called()
            # Only the key-ring's atomic write and rollback need temporary files.
            self.assertTrue(all(c.kwargs.get("prefix") == ".keys-" for c in temporary.call_args_list))
            self.assertEqual(len(self.keys.entries()), 2)
            self.assertEqual(self.store.list_files(), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
