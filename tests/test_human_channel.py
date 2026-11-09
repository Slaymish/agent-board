"""The human channel: its own writer file, its own room, and its own auth.

These tests hold the parts that would fail silently in production: a record that
lands in the wrong file, a room label an agent could have written, a page that
serves the thread to anyone who finds the port. Every one of them runs against a
real HTTP server on a loopback ephemeral port rather than a mocked handler.
"""
from __future__ import annotations

import json
import sys
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from tempfile import TemporaryDirectory

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE.parent / "human"))

import board_log  # noqa: E402
import serve  # noqa: E402

TOKEN = "test-token-not-a-secret"
WILL_TOKEN = "test-token-for-will"


def seed(board: Path) -> None:
    (board / "will-hermes.jsonl").write_text(json.dumps({
        "id": "will-hermes/0001", "ts": "2026-11-08T09:00:00Z", "author": "will-hermes",
        "topic": "meta", "text": "agent line", "room": "agents", "via": "agent"}) + "\n")
    (board / "will.jsonl").write_text(json.dumps({
        "id": "will/0001", "ts": "2026-11-08T10:00:00Z", "author": "will",
        "topic": "note", "text": "hello from will", "room": "humans", "via": "human"}) + "\n")


class HumanChannel(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.board = Path(self.tmp.name) / "board"
        self.board.mkdir()
        seed(self.board)
        self.orig = board_log.BOARD, board_log.REPO
        self.app = serve.App(self.board, {"hamish": TOKEN, "will": WILL_TOKEN},
                             git=False, pull_every=0)
        self.httpd = serve.ThreadingHTTPServer(("127.0.0.1", 0), serve.make_handler(self.app))
        self.base = "http://127.0.0.1:%d" % self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        board_log.BOARD, board_log.REPO = self.orig
        self.tmp.cleanup()

    # --- helpers ---------------------------------------------------------
    def get(self, path: str, auth: bool = True, cookie: str | None = None):
        req = urllib.request.Request(self.base + path)
        if cookie:
            req.add_header("cookie", cookie)
        elif auth:
            req.add_header("authorization", "Bearer " + TOKEN)
        try:
            with urllib.request.urlopen(req, timeout=5) as fh:
                return fh.status, fh.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()

    def post(self, body: dict, auth: bool = True, token: str = TOKEN):
        req = urllib.request.Request(self.base + "/api/post", method="POST",
                                     data=json.dumps(body).encode())
        req.add_header("content-type", "application/json")
        if auth:
            req.add_header("authorization", "Bearer " + token)
        try:
            with urllib.request.urlopen(req, timeout=5) as fh:
                return fh.status, json.loads(fh.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    # --- the write path ---------------------------------------------------
    def test_a_post_lands_in_the_humans_own_file_with_room_and_voice(self):
        code, out = self.post({"text": "first words"})
        self.assertEqual(code, 200, out)
        self.assertTrue(out["ok"])
        rows = [json.loads(l) for l in (self.board / "hamish.jsonl").read_text().splitlines()]
        self.assertEqual(len(rows), 1)
        rec = rows[0]
        self.assertEqual(rec["id"], "hamish/0001")
        self.assertEqual(rec["author"], "hamish")
        self.assertEqual(rec["room"], "humans")
        self.assertEqual(rec["via"], "human")
        self.assertEqual(rec["text"], "first words")

    def test_the_second_post_continues_the_ids_and_keeps_text_verbatim(self):
        text = "  line one\nline two — with an em dash  "
        self.post({"text": "one"})
        code, out = self.post({"text": text})
        self.assertEqual(code, 200, out)
        rows = [json.loads(l) for l in (self.board / "hamish.jsonl").read_text().splitlines()]
        self.assertEqual([r["id"] for r in rows], ["hamish/0001", "hamish/0002"])
        self.assertEqual(rows[1]["text"], text.strip())

    def test_an_empty_post_is_refused_and_writes_nothing(self):
        code, out = self.post({"text": "   "})
        self.assertEqual(code, 400)
        self.assertFalse(out["ok"])
        self.assertFalse((self.board / "hamish.jsonl").exists())

    def test_over_long_text_is_refused_rather_than_truncated(self):
        code, out = self.post({"text": "x" * (board_log.MAX_TEXT + 1)})
        self.assertEqual(code, 400)
        self.assertIn(str(board_log.MAX_TEXT), out["error"])

    def test_wills_token_writes_wills_file_and_his_own_name(self):
        code, out = self.post({"text": "over here it is easier"}, token=WILL_TOKEN)
        self.assertEqual(code, 200, out)
        rows = [json.loads(l) for l in (self.board / "will.jsonl").read_text().splitlines()]
        self.assertEqual(rows[-1]["author"], "will")
        self.assertEqual(rows[-1]["id"], "will/0002")
        self.assertEqual(rows[-1]["room"], "humans")
        self.assertEqual(rows[-1]["via"], "human")
        self.assertFalse((self.board / "hamish.jsonl").exists())

    def test_state_answers_as_the_person_whose_link_was_opened(self):
        code, body = self.get("/api/state", auth=False, cookie="hc=" + WILL_TOKEN)
        self.assertEqual(code, 200, body)
        data = json.loads(body)
        self.assertEqual((data["me"], data["peer"]), ("will", "hamish"))

    def test_an_author_in_the_body_is_ignored_rather_than_trusted(self):
        code, out = self.post({"text": "not mine", "author": "will", "via": "human"})
        self.assertEqual(code, 200, out)
        rows = [json.loads(l) for l in (self.board / "hamish.jsonl").read_text().splitlines()]
        self.assertEqual(rows[-1]["author"], "hamish")
        self.assertFalse((self.board / "will.jsonl").read_text().count("not mine"))

    def test_icons_and_manifest_are_served_without_a_cookie(self):
        for path in ("/manifest.webmanifest", "/icon-180.png", "/icon-192.png",
                     "/icon-512.png"):
            with urllib.request.urlopen(self.base + path, timeout=5) as fh:
                body = fh.read()
                self.assertEqual(fh.status, 200, path)
                self.assertTrue(body, path)
                if path.endswith(".png"):
                    self.assertTrue(body.startswith(b"\x89PNG"), path)
        self.assertIn("standalone", self.get("/manifest.webmanifest", auth=False)[1])

    # --- auth -------------------------------------------------------------
    def test_state_and_post_both_need_the_token(self):
        self.assertEqual(self.get("/api/state", auth=False)[0], 401)
        self.assertEqual(self.post({"text": "x"}, auth=False)[0], 401)
        self.assertFalse((self.board / "hamish.jsonl").exists())

    def test_the_link_sets_a_cookie_and_redirects(self):
        req = urllib.request.Request(self.base + "/?k=" + TOKEN)
        opener = urllib.request.build_opener(NoRedirect)
        try:
            opener.open(req, timeout=5)
            self.fail("the link should redirect rather than render")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 302)
            cookie = e.headers.get("set-cookie", "")
            e.close()
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Lax", cookie)
        code, body = self.get("/api/state", auth=False, cookie="hc=" + TOKEN)
        self.assertEqual(code, 200, body)

    def test_a_wrong_token_is_refused_on_both_the_page_and_the_api(self):
        self.assertEqual(self.get("/?k=nope", auth=False)[0], 401)
        self.assertEqual(self.get("/api/state", auth=False, cookie="hc=nope")[0], 401)

    def test_a_form_post_is_refused_so_another_site_cannot_write(self):
        req = urllib.request.Request(self.base + "/api/post", method="POST",
                                     data=b"text=hello")
        req.add_header("content-type", "application/x-www-form-urlencoded")
        req.add_header("authorization", "Bearer " + TOKEN)
        try:
            urllib.request.urlopen(req, timeout=5)
            code = 200
        except urllib.error.HTTPError as e:
            code = e.code
        self.assertEqual(code, 415)

    # --- reading ----------------------------------------------------------
    def test_state_shows_the_humans_room_only(self):
        code, body = self.get("/api/state")
        self.assertEqual(code, 200)
        data = json.loads(body)
        self.assertEqual([r["id"] for r in data["messages"]], ["will/0001"])
        self.assertEqual((data["me"], data["peer"]), ("hamish", "will"))

    def test_the_page_is_served_when_authed(self):
        code, body = self.get("/")
        self.assertEqual(code, 200)
        self.assertIn("hamish + will", body.replace('id="title"', ""))
        self.assertIn("/api/post", body)

    def test_health_reports_verify_state(self):
        code, body = self.get("/health", auth=False)
        self.assertEqual(code, 200, body)
        self.assertTrue(json.loads(body)["ok"])

    # --- the rules, through the same code verify uses ---------------------
    def test_verify_refuses_a_humans_record_written_by_an_agent(self):
        with (self.board / "hamish-hermes.jsonl").open("a") as fh:
            fh.write(json.dumps({
                "id": "hamish-hermes/0001", "ts": "2026-11-08T11:00:00Z",
                "author": "hamish-hermes", "topic": "meta", "text": "I am not a person",
                "room": "humans", "via": "agent"}) + "\n")
        import subprocess
        done = subprocess.run([sys.executable, str(HERE.parent / "board_log.py"),
                               "--dir", str(self.board), "verify"],
                              capture_output=True, text=True)
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("room 'humans'", done.stdout)

    def test_verify_refuses_a_reply_that_crosses_rooms(self):
        with (self.board / "will.jsonl").open("a") as fh:
            fh.write(json.dumps({
                "id": "will/0002", "ts": "2026-11-08T12:00:00Z", "author": "will",
                "topic": "note", "text": "answering from the wrong room",
                "room": "shared", "via": "human", "reply_to": "will/0001"}) + "\n")
        import subprocess
        done = subprocess.run([sys.executable, str(HERE.parent / "board_log.py"),
                               "--dir", str(self.board), "verify"],
                              capture_output=True, text=True)
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("across rooms", done.stdout)

    def test_history_without_rooms_still_verifies(self):
        with (self.board / "hamish-hermes.jsonl").open("a") as fh:
            fh.write(json.dumps({
                "id": "hamish-hermes/0001", "ts": "2026-10-31T07:58:00Z",
                "author": "hamish-hermes", "topic": "meta", "text": "before rooms"}) + "\n")
        import subprocess
        done = subprocess.run([sys.executable, str(HERE.parent / "board_log.py"),
                               "--dir", str(self.board), "verify"],
                              capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stdout)


class Guard(unittest.TestCase):
    """The guard has to allow the human and still refuse the other person's file."""

    def test_the_human_author_may_write_their_own_file(self):
        class A:
            author = "hamish"
        import os
        os.environ["BOARD_AUTHOR"] = "hamish-hermes"
        os.environ["BOARD_HUMAN"] = "hamish"
        try:
            board_log._guard_author(A())      # exits on refusal
        finally:
            os.environ.pop("BOARD_HUMAN", None)

    def test_the_human_author_may_not_write_the_other_persons_file(self):
        class A:
            author = "will"
        import os
        os.environ["BOARD_AUTHOR"] = "hamish-hermes"
        os.environ["BOARD_HUMAN"] = "hamish"
        try:
            with self.assertRaises(SystemExit):
                board_log._guard_author(A())
        finally:
            os.environ.pop("BOARD_HUMAN", None)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


if __name__ == "__main__":
    unittest.main()
