#!/usr/bin/env python3
"""
Tests for agent-board. Standard library only; starts a real server on a
throwaway port and data directory, then drives it over real HTTP.

    python3 tests/test_board.py
"""

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
