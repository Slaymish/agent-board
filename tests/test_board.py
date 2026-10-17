#!/usr/bin/env python3
"""
Tests for agent-board. Standard library only; starts a real server on a
throwaway port and data directory, then drives it over real HTTP.

    python3 tests/test_board.py
"""

import hashlib
import http.client
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
