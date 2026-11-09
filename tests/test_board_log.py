"""Tests for board_log.py — the v2 append-only log CLI.

Two properties here were each bought with a real failure:

1. A cursor file that does not exist means "this reader has never looked", not
   "everything is new". A renamed machine produced a cursor filename that had never
   existed, and the first watch run announced the other side's whole archive — 81
   phone notifications and 81 delivery receipts in one tick, none of them retractable.

2. There is no default author. An author baked into a shared tool writes one party's
   log on the other party's machine, which is the one mistake this design exists to
   prevent; it already happened once, during the v1 import.

    python3 tests/test_board_log.py
"""

import contextlib
import importlib.util
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "board_log.py"
assert SCRIPT.is_file(), SCRIPT


def load(logs: Path | None = None):
    """Import board_log.py fresh, optionally pointed at a log directory."""
    spec = importlib.util.spec_from_file_location("board_log_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["board_log_under_test"] = mod
    spec.loader.exec_module(mod)
    if logs is not None:
        mod.set_dir(logs)
    # No git in a unit test: the temp directory is not a repository.
    mod.__dict__["sync"] = lambda push=True: None
    return mod


def write_records(path: Path, author: str, start: int, count: int) -> None:
    rows = [{"id": f"{author}/{n:04d}", "ts": "2026-11-01T00:00:00Z", "author": author,
             "topic": "t", "text": f"record {n}"}
            for n in range(start, start + count)]
    with path.open("a") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")


def watch(mod, author="me"):
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        mod.cmd_watch(Namespace(author=author, notify="none", no_receipt=False))
    return out.getvalue()


class Directory(unittest.TestCase):
    def test_set_dir_repoints_the_logs(self):
        with tempfile.TemporaryDirectory() as tmp:
            logs = Path(tmp) / "logs"
            logs.mkdir()
            mod = load(logs)
            self.assertEqual(mod.BOARD, logs.resolve())
            self.assertEqual(mod.own_file("a-b").parent, logs.resolve())

    def test_dir_is_accepted_before_or_after_the_subcommand(self):
        mod = load()
        argv = ["board_log.py", "--dir", "/tmp/x", "status"]
        self.assertEqual(mod._pop_dir(argv), "/tmp/x")
        self.assertEqual(argv, ["board_log.py", "status"])
        argv = ["board_log.py", "status", "--dir=/tmp/y"]
        self.assertEqual(mod._pop_dir(argv), "/tmp/y")
        self.assertEqual(argv, ["board_log.py", "status"])


class AuthorIsNeverDefaulted(unittest.TestCase):
    def test_no_author_and_no_pin_is_an_error(self):
        mod = load()
        old = os.environ.pop("BOARD_AUTHOR", None)
        try:
            with self.assertRaises(SystemExit) as raised:
                mod._guard_author(Namespace(author=None))
            self.assertIn("no author", str(raised.exception))
        finally:
            if old is not None:
                os.environ["BOARD_AUTHOR"] = old

    def test_pin_refuses_a_foreign_write_unless_asked(self):
        mod = load()
        old = os.environ.get("BOARD_AUTHOR")
        os.environ["BOARD_AUTHOR"] = "mine"
        try:
            with self.assertRaises(SystemExit):
                mod._guard_author(Namespace(author="theirs", allow_foreign_author=False))
            mod._guard_author(Namespace(author="theirs", allow_foreign_author=True))
        finally:
            if old is None:
                os.environ.pop("BOARD_AUTHOR", None)
            else:
                os.environ["BOARD_AUTHOR"] = old


class ColdCursor(unittest.TestCase):
    def test_a_cold_cursor_primes_silently_and_writes_no_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            logs = Path(tmp)
            mod = load(logs)
            write_records(logs / "peer.jsonl", "peer", 1, 5)
            out = watch(mod)
            self.assertIn("primed cold cursor", out)
            self.assertNotIn("new: peer/", out)
            self.assertFalse((logs / "me.jsonl").exists(),
                             "a cold prime must not append receipts to own log")

    def test_a_warm_cursor_announces_only_what_arrived_since(self):
        with tempfile.TemporaryDirectory() as tmp:
            logs = Path(tmp)
            mod = load(logs)
            write_records(logs / "peer.jsonl", "peer", 1, 3)
            watch(mod)                                     # primes at peer/0003
            write_records(logs / "peer.jsonl", "peer", 4, 1)
            out = watch(mod)
            self.assertIn("new: peer/0004", out)
            self.assertNotIn("peer/0001", out)

    def test_the_cursor_is_keyed_by_hostname(self):
        mod = load()
        self.assertIn(socket.gethostname(), mod._cursor_path("peer").name)


class Records(unittest.TestCase):
    def test_ids_are_per_writer_and_contiguous(self):
        with tempfile.TemporaryDirectory() as tmp:
            mod = load(Path(tmp))
            for _ in range(2):
                mod._append_record("w", {"topic": "t", "text": "x", "ts": mod.now_ts()})
            rows = [json.loads(line) for line in
                    (Path(tmp) / "w.jsonl").read_text().splitlines() if line.strip()]
            self.assertEqual([r["id"] for r in rows], ["w/0001", "w/0002"])


class Verify(unittest.TestCase):
    """The CLI contract: a bad log exits non-zero and names the problem."""

    def run_cli(self, logs: Path):
        return subprocess.run([sys.executable, str(SCRIPT), "--dir", str(logs), "verify"],
                              capture_output=True, text=True)

    def test_a_clean_log_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            logs = Path(tmp)
            write_records(logs / "me.jsonl", "me", 1, 2)
            write_records(logs / "peer.jsonl", "peer", 1, 2)
            done = self.run_cli(logs)
            self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
            self.assertIn("ok", done.stdout)

    def test_a_duplicate_id_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            logs = Path(tmp)
            write_records(logs / "me.jsonl", "me", 1, 1)
            write_records(logs / "me.jsonl", "me", 1, 1)
            done = self.run_cli(logs)
            self.assertEqual(done.returncode, 1)
            self.assertIn("duplicate id", done.stdout)

    def test_a_receipt_for_our_own_record_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            logs = Path(tmp)
            (logs / "me.jsonl").write_text(json.dumps(
                {"id": "me/0001", "ts": "2026-11-01T00:00:00Z", "author": "me",
                 "topic": "receipt", "text": "delivered peer/0001", "delivered": "me/0001"}
            ) + "\n")
            (logs / "peer.jsonl").write_text(json.dumps(
                {"id": "peer/0001", "ts": "2026-11-01T00:00:00Z", "author": "peer",
                 "topic": "t", "text": "x"}) + "\n")
            done = self.run_cli(logs)
            self.assertEqual(done.returncode, 1)
            self.assertIn("receipts its own record", done.stdout)


class AnswersBindsSeveralAsks(unittest.TestCase):
    """A record that settles four asks is one record, not four.

    The field took a single id, so a reply that named several asks in prose closed none
    of them in the ledger: both readers saw them open and the ask went stale. Two ids
    must verify, and one bad id among good ones must still fail.
    """

    def post(self, logs: Path, answers):
        mod = load(logs)
        # No repository in a unit test: the temp directory is not one.
        mod.__dict__["git"] = lambda *a, **k: None
        ns = Namespace(topic="meta", text=["settled"], author="me", reply_to=None,
                       amends=None, ask_to=None, answers=answers,
                       no_push=True, allow_foreign_author=False)
        with contextlib.redirect_stdout(io.StringIO()):
            mod.cmd_post(ns)
        return json.loads((logs / "me.jsonl").read_text().splitlines()[-1])

    def verify(self, logs: Path):
        return subprocess.run([sys.executable, str(SCRIPT), "--dir", str(logs), "verify"],
                              capture_output=True, text=True)

    def test_two_ids_are_stored_as_a_list_and_verify(self):
        with tempfile.TemporaryDirectory() as tmp:
            logs = Path(tmp)
            write_records(logs / "peer.jsonl", "peer", 1, 2)
            rec = self.post(logs, "peer/0001,peer/0002")
            self.assertEqual(rec["answers"], ["peer/0001", "peer/0002"])
            done = self.verify(logs)
            self.assertEqual(done.returncode, 0, done.stdout + done.stderr)

    def test_repeated_flags_are_the_same_as_a_comma_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            logs = Path(tmp)
            write_records(logs / "peer.jsonl", "peer", 1, 2)
            rec = self.post(logs, ["peer/0001", "peer/0002"])
            self.assertEqual(rec["answers"], ["peer/0001", "peer/0002"])

    def test_one_id_keeps_the_string_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            logs = Path(tmp)
            write_records(logs / "peer.jsonl", "peer", 1, 1)
            rec = self.post(logs, "peer/0001")
            self.assertEqual(rec["answers"], "peer/0001")

    def test_an_unknown_id_among_good_ones_still_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            logs = Path(tmp)
            write_records(logs / "peer.jsonl", "peer", 1, 1)
            self.post(logs, "peer/0001,peer/0999")
            done = self.verify(logs)
            self.assertEqual(done.returncode, 1)
            self.assertIn("peer/0999 does not exist", done.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
