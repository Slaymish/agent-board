"""The human channel: its own writer file, its own room, and its own auth.

These tests hold the parts that would fail silently in production: a record that
lands in the wrong file, a room label an agent could have written, a page that
serves the thread to anyone who finds the port. Every one of them runs against a
real HTTP server on a loopback ephemeral port rather than a mocked handler.
"""
from __future__ import annotations

import json
import subprocess
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

    def test_a_reply_records_what_it_answers_and_still_verifies(self):
        code, out = self.post({"text": "answering that", "reply_to": "will/0001"})
        self.assertEqual(code, 200, out)
        self.assertEqual(out["record"]["reply_to"], "will/0001")
        import subprocess
        done = subprocess.run([sys.executable, str(HERE.parent / "board_log.py"),
                               "--dir", str(self.board), "verify"],
                              capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stdout)

    def test_a_reply_to_a_note_that_does_not_exist_is_refused(self):
        code, out = self.post({"text": "to nobody", "reply_to": "will/0999"})
        self.assertEqual(code, 400)
        self.assertIn("no note", out["error"])
        self.assertFalse((self.board / "hamish.jsonl").exists())

    def test_a_reply_cannot_cross_out_of_the_humans_room(self):
        code, out = self.post({"text": "to the agent line", "reply_to": "will-hermes/0001"})
        self.assertEqual(code, 400)
        self.assertIn("not in this room", out["error"])
        self.assertFalse((self.board / "hamish.jsonl").exists())

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

    def test_the_work_view_skips_delivery_receipts(self):
        with (self.board / "will-hermes.jsonl").open("a") as fh:
            fh.write(json.dumps({
                "id": "will-hermes/0002", "ts": "2026-11-08T11:00:00Z",
                "author": "will-hermes", "topic": "receipt",
                "text": "delivered will/0001"}) + "\n")
        code, body = self.get("/api/work")
        ids = [r["id"] for r in json.loads(body)["records"]]
        self.assertNotIn("will-hermes/0002", ids, "a receipt is bookkeeping, not work")

    def test_the_work_view_needs_the_link(self):
        code, _ = self.get("/api/work", auth=False)
        self.assertEqual(code, 401)

    def test_the_work_view_shows_agent_records_and_not_the_room(self):
        code, body = self.get("/api/work")
        self.assertEqual(code, 200)
        ids = [r["id"] for r in json.loads(body)["records"]]
        self.assertIn("will-hermes/0001", ids, "the agents' side should be visible")
        self.assertNotIn("will/0001", ids, "the humans room is not 'work'")


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


class SaveAndPublish(unittest.TestCase):
    """The note and its publication are two facts, and the writer is told both.

    Appending is fsynced and local; the commit and push are how the other person gets it.
    Until 9 Nov 2026 a commit failure raised out of the handler, so a note that was safely
    on disk answered as a 500 with no record of what happened.
    """

    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.board = Path(self.tmp.name) / "board"
        self.board.mkdir()
        seed(self.board)
        self.orig_git = board_log.git
        self.orig = board_log.BOARD, board_log.REPO

    def tearDown(self) -> None:
        board_log.git = self.orig_git
        board_log.BOARD, board_log.REPO = self.orig
        self.tmp.cleanup()

    def test_fsync_failure_does_not_claim_saved_or_publish(self):
        from unittest.mock import patch
        app = serve.App(self.board, {"hamish": TOKEN}, git=False)
        with patch.object(serve.os, "fsync", side_effect=OSError("disk failure")), \
                patch.object(app, "publish") as publish:
            with self.assertRaises(OSError):
                app.post("hamish", "uncertain append")
            publish.assert_not_called()

    def test_a_lost_response_does_not_become_two_notes(self):
        app = serve.App(self.board, {"hamish": TOKEN, "will": WILL_TOKEN},
                        git=False, pull_every=0)
        first = app.post("hamish", "once", client_id="sub-1")
        again = app.post("hamish", "once", client_id="sub-1")
        self.assertEqual(first["record"]["id"], again["record"]["id"])
        self.assertTrue(again.get("repeated"))
        lines = (self.board / "hamish.jsonl").read_text().splitlines()
        self.assertEqual(len(lines), 1, "the retry appended a second record")


class RealGitRecovery(HumanChannel):
    def setUp(self):
        super().setUp()
        self.repo = self.board.parent
        self.remote = self.repo / "remote.git"
        self.run_git("init", "-q")
        self.run_git("config", "user.email", "test@example.invalid")
        self.run_git("config", "user.name", "Test")
        self.run_git("add", "board")
        self.run_git("commit", "-qm", "seed")
        subprocess.run(["git", "init", "--bare", "-q", str(self.remote)], check=True)
        self.run_git("remote", "add", "origin", str(self.remote))
        self.run_git("push", "-qu", "origin", "HEAD")
        self.app.git = True

    def run_git(self, *args):
        return subprocess.run(["git", "-C", str(self.repo), *args], check=True,
                              capture_output=True, text=True).stdout

    def test_commit_hook_failure_http_then_background_recovery(self):
        hook = self.repo / ".git/hooks/pre-commit"
        hook.write_text("#!/bin/sh\nexit 1\n")
        hook.chmod(0o755)
        unrelated = self.repo / "unrelated"
        unrelated.write_text("leave staged")
        self.run_git("add", "unrelated")
        code, out = self.post({"text": "durable", "client_id": "lost"})
        self.assertEqual(code, 200)
        self.assertTrue(out["saved"])
        self.assertFalse(out["published"])
        hook.unlink()
        self.app.publish_every = .05
        stop = threading.Event()
        worker = threading.Thread(target=self.app.publish_forever, args=(stop,))
        worker.start()
        import time
        try:
            deadline = time.monotonic() + 4
            remote_text = ""
            while time.monotonic() < deadline:
                result = subprocess.run(["git", "--git-dir", str(self.remote), "show",
                                         "HEAD:board/hamish.jsonl"], capture_output=True, text=True)
                remote_text = result.stdout
                if "durable" in remote_text:
                    break
                time.sleep(.05)
            self.assertIn("durable", remote_text)
            self.assertIn("unrelated", self.run_git("diff", "--cached", "--name-only"))
        finally:
            stop.set()
            worker.join(2)
        self.assertFalse(worker.is_alive())

    def test_commit_failure_reaches_actual_git_path(self):
        from unittest.mock import patch
        actual = self.app.git_run
        reached = []
        def failing(*args, **kwargs):
            if args[0] == "commit":
                reached.append(args)
                raise subprocess.CalledProcessError(1, args)
            return actual(*args, **kwargs)
        with patch.object(self.app, "git_run", side_effect=failing):
            out = self.app.post("hamish", "saved despite commit failure")
        self.assertTrue(reached)
        self.assertTrue(out["saved"])
        self.assertFalse(out["published"])

    def advance_remote(self, filename):
        clone = self.repo / "second"
        with (self.repo / ".git/info/exclude").open("a") as stream:
            stream.write("second/\nremote.git/\n")
        subprocess.run(["git", "clone", "-q", str(self.remote), str(clone)], check=True)
        def git(*args):
            return subprocess.run(["git", "-C", str(clone), *args], check=True,
                                  capture_output=True, text=True)
        git("config", "user.email", "second@example.invalid")
        git("config", "user.name", "Second")
        with (clone / "board" / filename).open("a") as stream:
            author = Path(filename).stem
            stream.write(json.dumps({"id": author + "/0002", "author": author,
                                     "text": "remote advancement", "ts": "2026-11-08T12:00:00Z",
                                     "room": "agents" if author.endswith("hermes") else "humans",
                                     "via": "agent" if author.endswith("hermes") else "human"}) + "\n")
        git("add", "board/" + filename)
        git("commit", "-qm", "remote advancement")
        git("push", "-q")

    def pending_post(self):
        hook = self.remote / "hooks/pre-receive"
        hook.write_text("#!/bin/sh\nexit 1\n")
        hook.chmod(0o755)
        self.assertFalse(self.app.post("hamish", "pending human")["published"])
        hook.unlink()

    def test_disjoint_remote_advance_rebases_pending_human_commit(self):
        self.pending_post()
        self.advance_remote("will-hermes.jsonl")
        self.assertTrue(self.app.publish())
        body = subprocess.run(["git", "--git-dir", str(self.remote), "show",
                               "HEAD:board/hamish.jsonl"], check=True,
                              capture_output=True, text=True).stdout
        self.assertIn("pending human", body)

    def test_same_file_remote_advance_stays_pending_unchanged(self):
        self.pending_post()
        before = self.run_git("rev-parse", "HEAD")
        body = (self.board / "hamish.jsonl").read_bytes()
        self.advance_remote("hamish.jsonl")
        self.assertFalse(self.app.publish())
        self.assertEqual(before, self.run_git("rev-parse", "HEAD"))
        self.assertEqual(body, (self.board / "hamish.jsonl").read_bytes())
        self.assertEqual("", self.run_git("status", "--porcelain", "--untracked-files=no"))
        self.assertFalse((self.repo / ".git/rebase-merge").exists())

    def test_pull_replacement_and_append_share_repository_lock(self):
        from unittest.mock import patch
        import time
        self.advance_remote("will.jsonl")
        self.app.pull_every = 1
        entered = threading.Event()
        release = threading.Event()
        actual = self.app.git_run
        def held(*args, **kwargs):
            if args[0] == "pull":
                entered.set()
                self.assertTrue(release.wait(3))
            return actual(*args, **kwargs)
        results = []
        with patch.object(self.app, "git_run", side_effect=held):
            puller = threading.Thread(target=self.app.pull)
            poster = threading.Thread(target=lambda: results.append(self.app.post("will", "after replacement")))
            puller.start()
            try:
                self.assertTrue(entered.wait(2))
                poster.start()
                time.sleep(.15)
                self.assertTrue(poster.is_alive())
                self.assertNotIn("after replacement", (self.board / "will.jsonl").read_text())
            finally:
                release.set()
                puller.join(4)
                if poster.ident: poster.join(4)
        self.assertEqual(len(results), 1)
        self.assertIn("after replacement", (self.board / "will.jsonl").read_text())

    def test_remote_rejection_and_unavailability_recover(self):
        hook = self.remote / "hooks/pre-receive"
        hook.write_text("#!/bin/sh\nexit 1\n"); hook.chmod(0o755)
        self.assertFalse(self.app.post("hamish", "rejected")["published"])
        hook.unlink()
        self.run_git("remote", "set-url", "origin", str(self.repo / "missing"))
        self.assertFalse(self.app.publish())
        self.run_git("remote", "set-url", "origin", str(self.remote))
        self.assertTrue(self.app.publish())

    def test_restart_author_scope_and_concurrent_duplicates(self):
        first = self.app.post("hamish", "once", client_id="same")
        app = serve.App(self.board, {"hamish": TOKEN, "will": WILL_TOKEN}, git=False)
        self.assertEqual(first["record"], app.post("hamish", "once", client_id="same")["record"])
        other = app.post("will", "other", client_id="same")
        self.assertEqual(other["record"]["author"], "will")
        with self.assertRaises(ValueError):
            app.post("hamish", "edited", client_id="same")
        second_app = serve.App(self.board, {"hamish": TOKEN}, git=False)
        threads = [threading.Thread(target=(app if n % 2 else second_app).post, args=("hamish", "parallel"),
                                    kwargs={"client_id": "parallel"}) for n in range(8)]
        for t in threads: t.start()
        for t in threads: t.join()
        rows = board_log.records(self.board / "hamish.jsonl")
        self.assertEqual(sum(r.get("client_id") == "parallel" for r in rows), 1)

    def test_exact_old_full_reference_and_auth(self):
        with (self.board / "will-hermes.jsonl").open("a") as stream:
            for n in range(2, 32):
                stream.write(json.dumps({"id": f"will-hermes/{n:04d}", "author": "will-hermes",
                    "room": "agents", "text": "x" * 1800, "ts": "2026-11-08T11:00:00Z"}) + "\n")
        self.assertEqual(self.get("/api/record?id=will-hermes/0002", auth=False)[0], 401)
        code, body = self.get("/api/record?id=will-hermes/0002")
        self.assertEqual(code, 200)
        self.assertEqual(len(json.loads(body)["record"]["text"]), 1800)
        self.assertNotIn("will-hermes/0002", [r["id"] for r in self.app.work()])
        self.assertEqual(self.get("/api/record?id=missing/0001")[0], 404)
        self.assertEqual(json.loads(self.get("/api/record?id=will/0001")[1])["record"]["room"], "humans")


class PageNavigation(unittest.TestCase):
    def test_exact_links_history_focus_scroll_and_refresh(self):
        page = (HERE.parent / "human/page.html").read_text()
        linkify = page[page.index("function linkify("):page.index("function snippet(")]
        navigation = page[page.index("let navDepth ="):page.index('$("composer").addEventListener')]
        load_work = page[page.index("async function loadWork()"):page.index("// Following a reference")]
        harness = r"""
const assert = require('node:assert/strict');
let scrollY = 310;
const scrollTo = (x, y) => { scrollY = y; };
const requestAnimationFrame = fn => fn();
const elements = new Map();
const $ = id => { if (!elements.has(id)) elements.set(id, {}); return elements.get(id); };
let pop;
const window = {addEventListener: (name, fn) => { pop = fn; }};
const document = {
  createTextNode: text => ({textContent: text}),
  createElement: tag => ({tag, style: {}, addEventListener() {}}),
  querySelectorAll: () => rendered.map(r => ({dataset: {id:r.id}, scrollIntoView() {scrollY = 777;}}))
};
const state = {view:'lines', roomView:'lines', messages:[], work:[], focusWork:null};
let rendered = [];
function render() { rendered = state.view === 'work' ? state.work : state.messages; }
function paintViewIcon() {}
function status(message) { throw new Error(message); }
const rows = {
 'will-hermes/0001': {id:'will-hermes/0001', room:'agents', text:'A full record'},
 'will-hermes/0002': {id:'will-hermes/0002', room:'agents', text:'B full record'}
};
async function fetch(url) {
 return {ok:true, json:async () => url.startsWith('/api/record') ?
   {record:rows[decodeURIComponent(url.split('=')[1])]} : {records:[]}};
}
const entries = [{}]; let cursor = 0;
const history = {
 replaceState(value) { entries[cursor] = structuredClone(value); },
 pushState(value) { entries.splice(++cursor); entries[cursor] = structuredClone(value); },
 async back() { if (cursor) await pop({state:entries[--cursor]}); },
 async forward() { await pop({state:entries[++cursor]}); }
};
"""
        checks = r"""
(async () => {
 assert.equal(linkify('will-hermes/10000').filter(x=>x.tag==='button').length, 0);
 for (const bad of ['xwill-hermes/1000', 'will-hermes/1000x', 'will-hermes/1000/extra', 'will-hermes/10000'])
   assert.equal(linkify(bad).filter(x=>x.tag==='button').length, 0, bad);
 assert.equal(linkify('(will-hermes/1000)').filter(x=>x.tag==='button').length, 1);
 assert.equal($('return').hidden, true);
 await openWork('will-hermes/0001');
 assert.equal(entries[cursor].y, 777);
 scrollY = 888;
 await openWork('will-hermes/0002');
 assert.equal(state.focusWork, 'will-hermes/0002');
 await history.back();
 assert.equal(state.focusWork, 'will-hermes/0001');
 assert.equal(state.work.find(r=>r.id===state.focusWork).text, 'A full record');
 assert.equal(scrollY, 888);
 await loadWork(); render();
 assert.equal(state.work.find(r=>r.id===state.focusWork).text, 'A full record');
 await history.back();
 assert.equal(state.view, 'lines'); assert.equal(scrollY, 310);
 assert.equal($('return').hidden, true);
 $('return').onclick(); assert.equal(cursor, 0);
 await history.forward(); assert.equal(state.focusWork, 'will-hermes/0001');
 await history.back();
 await $('rooms').onclick(); scrollY = 456;
 await $('rooms').onclick(); assert.equal(scrollY, 310);
 await $('rooms').onclick(); assert.equal(scrollY, 456);
})().catch(e => { console.error(e); process.exitCode=1; });
"""
        done = subprocess.run(["node"], input=harness + linkify + load_work + navigation + checks,
                              capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn('[hidden] { display: none !important; }', page)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


if __name__ == "__main__":
    unittest.main()
