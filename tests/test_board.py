#!/usr/bin/env python3
"""
Tests for agent-board. Standard library only; starts a real server on a
throwaway port and data directory, then drives it over real HTTP.

    python3 tests/test_board.py
"""

import hashlib
import contextlib
import http.client
import importlib.util
import io
import json
import os
import shutil
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
        status, _ = self.call("POST", "/api/messages", key=KEY,
                              body={"author": "x", "text": "a" * (board.MAX_BODY + 10)})
        self.assertEqual(status, 413)

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
        self.identity_key = self.keys.add("will-hermes", "Will")
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
                                         "BOARD_KEY": KEY}, clear=False):
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
                             ["identity", "acked_id", "updated_epoch"])
            self.assertEqual([r["name"] for r in db.execute("PRAGMA table_info(asks)")],
                             ["id", "msg_id", "from_identity", "to_identity", "text",
                              "opened_epoch", "closed_epoch", "closed_by"])
        self.assertEqual(self.call("GET", "/api/search?q=first", key=KEY)[1]["count"], 1)

    def test_identity_and_legacy_auth(self):
        for key, identity, unrestricted in ((KEY, None, True), (self.identity_key, "will-hermes", False)):
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
