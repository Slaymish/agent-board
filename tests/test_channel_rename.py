"""v1.5 rename and compatibility tests; every board lives in a temporary directory."""
import contextlib
import http.client
import importlib.util
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest import mock

spec = importlib.util.spec_from_file_location("board_rename", Path(__file__).resolve().parents[1] / "board.py")
board = importlib.util.module_from_spec(spec)
spec.loader.exec_module(board)


class ChannelRenameTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="board-rename-test-")
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        self.env = mock.patch.dict(os.environ, {"BOARD_DATA": self.tmp.name, "BOARD_QUIET": "1"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.store = board.Board(str(self.directory / "board.db"))
        self.keys = board.Keys("legacy-test", self.tmp.name)
        self.owner = self.keys.add("elio", channels=["*"])
        self.peer = self.keys.add("zeta", channels=["will", "house"])
        self.keys.add("zeta", channels=["will"])  # Count identities, not keys.
        self.flatmate = self.keys.add("cole")
        self.first = self.store.post("elio", "A searchable narwhal question for you.",
                                     channel="will", ask_to="zeta")
        self.second = self.store.post("zeta", "A reply.", channel="will", reply_to=self.first["id"])
        self.store.post("cole", "Shared room", channel="house")
        self.store.ack("zeta", self.second["id"], "will")
        self.store.ack("zeta", 1, "*")
        self.store.ack("cole", 0, "house")

    def start_server(self):
        server = board.make_server(("127.0.0.1", 0), board.make_handler(self.store, self.keys, "test"))
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()

    def call(self, method, path, key=None, payload=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = {"X-Board-Key": key or self.peer}
        if payload is not None:
            headers["Content-Type"] = "application/json"
        conn.request(method, path, json.dumps(payload) if payload is not None else None, headers)
        response = conn.getresponse()
        status, raw = response.status, response.read().decode()
        conn.close()
        try:
            return status, json.loads(raw)
        except ValueError:
            return status, raw

    def cli(self, *args, remote=False):
        out, err = io.StringIO(), io.StringIO()
        if remote:
            args += ("--url", "http://127.0.0.1:%d" % self.port, "--key", self.peer)
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = board.main(list(args))
        return code, out.getvalue(), err.getvalue()

    def snapshot(self):
        with contextlib.closing(self.store._db()) as db:
            tables = {name: db.execute("SELECT * FROM " + name + " ORDER BY 1, 2").fetchall()
                      for name in ("messages", "acks", "meta")}
            tables = {name: [tuple(row) for row in rows] for name, rows in tables.items()}
        return tables, (self.directory / "keys.json").read_bytes()

    def apply(self):
        return board.rename_channel(self.tmp.name, "will", "elio-zeta", apply=True)

    def test_dry_run_changes_no_rows_or_files_and_reports_external_state(self):
        before = self.snapshot()
        files = {p.name: p.read_bytes() for p in self.directory.iterdir() if p.is_file()}
        code, out, err = self.cli("channel", "rename", "will", "elio-zeta")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("would change will → elio-zeta: 2 messages, 1 acks, 1 identities", out)
        self.assertIn("alias will → elio-zeta", out)
        for name in ("watcher.cursor.json", "acked.cursor.json", "triage.cursor.json"):
            self.assertIn("~/.hermes/data/agent-board/" + name, out.splitlines()[-1])
        self.assertEqual(self.snapshot(), before)
        self.assertEqual({p.name: p.read_bytes() for p in self.directory.iterdir() if p.is_file()}, files)

    def test_apply_moves_rows_memberships_and_alias_and_leaves_external_files(self):
        for name in ("watcher.cursor.json", "acked.cursor.json", "triage.cursor.json"):
            (self.directory / name).write_text('{"will": 2}')
        before, _ = self.snapshot()
        code, out, err = self.cli("channel", "rename", "will", "elio-zeta", "--apply")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("changed will → elio-zeta: 2 messages, 1 acks, 1 identities", out)
        after, _ = self.snapshot()
        channel_index = 9
        for old, new in zip(before["messages"], after["messages"]):
            expected = list(old)
            if expected[channel_index] == "will":
                expected[channel_index] = "elio-zeta"
            self.assertEqual(tuple(expected), new)
        self.assertEqual(after["acks"], [(i, "elio-zeta" if c == "will" else c, mid, ts)
                                        for i, c, mid, ts in before["acks"]])
        self.assertIn(("channel_alias:will", "elio-zeta"), after["meta"])
        self.assertEqual(self.keys.channels_for("zeta"), ["elio-zeta", "house"])
        self.assertEqual(self.keys.channels_for("elio"), ["*"])
        self.assertEqual(self.keys.channels_for("cole"), ["house"])
        self.assertEqual((self.directory / "keys.json").stat().st_mode & 0o777, 0o600)
        for name in ("watcher.cursor.json", "acked.cursor.json", "triage.cursor.json"):
            self.assertEqual((self.directory / name).read_text(), '{"will": 2}')

    def test_failure_replacing_keys_rolls_back_all_database_changes(self):
        before = self.snapshot()
        with mock.patch.object(board.os, "replace", side_effect=OSError("test replace failure")):
            with self.assertRaisesRegex(OSError, "test replace failure"):
                self.apply()
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(list(self.directory.glob(".channel-rename-*")), [])

    def test_database_failure_leaves_keys_and_rows_unchanged(self):
        with contextlib.closing(self.store._db()) as db, db:
            db.execute("CREATE TRIGGER fail_alias BEFORE INSERT ON meta "
                       "WHEN new.key LIKE 'channel_alias:%' BEGIN SELECT RAISE(ABORT, 'test alias failure'); END")
        before = self.snapshot()
        with self.assertRaisesRegex(sqlite3.IntegrityError, "test alias failure"):
            self.apply()
        self.assertEqual(self.snapshot(), before)

    def test_commit_failure_restores_exact_original_keys(self):
        class FailingCommit(sqlite3.Connection):
            def commit(self):
                raise sqlite3.OperationalError("test commit failure")

        connect = sqlite3.connect
        before = self.snapshot()
        def connection(*args, **kwargs):
            return connect(*args, factory=FailingCommit, **kwargs)
        with mock.patch.object(board.sqlite3, "connect", side_effect=connection):
            with self.assertRaisesRegex(sqlite3.OperationalError, "test commit failure"):
                self.apply()
        self.assertEqual(self.snapshot(), before)

    def test_refuses_same_name_and_occupied_target_without_mutating(self):
        self.store.post("elio", "Target is occupied", channel="elio-zeta")
        before = self.snapshot()
        for old, new, message in (("will", "will", "must differ"),
                                  ("will", "elio-zeta", "already has messages")):
            for apply in (False, True):
                with self.assertRaisesRegex(ValueError, message):
                    board.rename_channel(self.tmp.name, old, new, apply=apply)
                self.assertEqual(self.snapshot(), before)

    def test_empty_target_receipt_collision_keeps_strongest_cursor(self):
        self.store.ack("zeta", 9, "elio-zeta")
        self.apply()
        with contextlib.closing(self.store._db()) as db:
            self.assertEqual(db.execute("SELECT acked_id FROM acks WHERE identity='zeta' "
                                        "AND channel='elio-zeta'").fetchone()[0], 9)

    def test_running_server_resolves_read_write_ack_and_response_names(self):
        self.start_server()  # Rename after handler creation: no restart needed.
        self.apply()
        status, data = self.call("GET", "/api/messages?channel=will")
        self.assertEqual(status, 200)
        self.assertEqual({m["channel"] for m in data["messages"]}, {"elio-zeta"})
        status, data = self.call("POST", "/api/messages", payload={"text": "Old peer", "channel": "will"})
        self.assertEqual(status, 201)
        self.assertEqual(data["message"]["channel"], "elio-zeta")
        status, receipt = self.call("POST", "/api/ack", payload={"id": data["id"], "channel": "will"})
        self.assertEqual(status, 200)
        self.assertEqual(receipt["channel"], "elio-zeta")
        for path in ("/api/messages", "/api/inbox", "/api/asks", "/api/messages/threads",
                     "/api/search?q=narwhal", "/api/digest", "/api/delivery"):
            with self.subTest(path=path):
                query = "&" if "?" in path else "?"
                old = self.call("GET", path + query + "channel=will")
                new = self.call("GET", path + query + "channel=elio-zeta")
                self.assertEqual(old[0], 200)
                # Time/age fields can advance between requests.
                self.assertNotIn('"channel": "will"', json.dumps(old[1]))
                self.assertEqual(old[1].keys(), new[1].keys())
        listing = self.call("GET", "/api/channels")[1]["channels"]
        self.assertEqual({r["channel"] for r in listing}, {"elio-zeta", "house"})
        self.assertNotIn("alias_of", json.dumps(listing))
        self.assertNotIn("Elio ↔ Zeta", json.dumps(listing, ensure_ascii=False))
        self.assertEqual(self.call("POST", "/api/messages", payload={"text": "No channel"})[1]
                         ["message"]["channel"], "elio-zeta")

    def test_legacy_key_stays_scoped_and_alias_never_grants_membership(self):
        self.apply()
        self.start_server()
        for suffix in ("", "?channel=will", "?channel=elio-zeta"):
            status, data = self.call("GET", "/api/messages" + suffix, key="legacy-test")
            self.assertEqual(status, 200)
            self.assertEqual({m["channel"] for m in data["messages"]}, {"elio-zeta"})
        self.assertEqual(self.call("GET", "/api/messages?channel=house", key="legacy-test")[0], 403)
        self.assertEqual(self.call("POST", "/api/messages", key="legacy-test",
                                   payload={"author": "anyone", "text": "Compatible"})[0], 201)
        for method, path, payload in (("GET", "/api/messages?channel=will", None),
                ("POST", "/api/messages", {"text": "No access", "channel": "will"}),
                ("POST", "/api/ack", {"id": 1, "channel": "will"})):
            self.assertEqual(self.call(method, path, key=self.flatmate, payload=payload)[0], 403)

    def test_missing_target_alias_is_inert_including_ack_legacy_routing(self):
        self.start_server()
        with contextlib.closing(self.store._db()) as db, db:
            db.execute("INSERT INTO meta VALUES ('channel_alias:retired', 'missing')")
        self.assertEqual(self.call("GET", "/api/messages?channel=retired", key=self.owner),
                         self.call("GET", "/api/messages?channel=missing", key=self.owner))
        self.assertEqual(self.call("GET", "/api/messages?channel=retired")[0], 403)
        self.assertEqual(self.call("POST", "/api/messages", payload={"text": "Denied", "channel": "retired"})[0], 403)
        status, receipt = self.call("POST", "/api/ack", payload={"id": 2, "channel": "retired"})
        self.assertEqual(status, 200)
        self.assertNotIn("channel", receipt)  # Same as an unknown legacy notification route.
        with contextlib.closing(self.store._db()) as db:
            self.assertFalse(db.execute("SELECT 1 FROM messages WHERE channel='missing'").fetchone())

    def test_cli_alias_note_once_and_display_labels_only_for_humans(self):
        self.apply()
        self.start_server()
        note = "will is an alias for elio-zeta; update your command\n"
        for args in (("read",), ("post", "Peer still works"), ("ack", "--id", "2"),
                     ("inbox",), ("search", "narwhal"), ("digest",), ("delivery", "--state")):
            with self.subTest(args=args):
                code, _, err = self.cli(*args, "--channel", "will", remote=True)
                self.assertEqual((code, err), (0, note))
        code, out, err = self.cli("channels", remote=True)
        self.assertEqual((code, err), (0, ""))
        self.assertIn("Elio ↔ Zeta", out)
        self.assertIn("house", out)
        self.assertNotIn("will", out)
        code, out, err = self.cli("inbox", "--channel", "elio-zeta", remote=True)
        self.assertEqual((code, err), (0, ""))
        self.assertIn("[Elio ↔ Zeta]", out)
        code, out, err = self.cli("inbox", "--json", remote=True)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(json.loads(out)["asks"][0]["channel"], "elio-zeta")

    def test_no_alias_keeps_explicit_membership_and_legacy_ack_behaviour(self):
        self.start_server()
        self.assertEqual(self.call("GET", "/api/messages?channel=will")[0], 200)
        code, _, err = self.cli("read", "--channel", "will", remote=True)
        self.assertEqual((code, err), (0, ""))
        status, data = self.call("POST", "/api/messages", payload={"text": "Still will", "channel": "will"})
        self.assertEqual((status, data["message"]["channel"]), (201, "will"))
        for channel in (None, "conduit"):
            status, receipt = self.call("POST", "/api/ack", payload={"id": 2, "channel": channel})
            self.assertEqual(status, 200)
            self.assertNotIn("channel", receipt)
        self.assertEqual(board.DEFAULT_CHANNEL, "elio-zeta")
        self.assertEqual(self.keys.default_channel("legacy"), "elio-zeta")
        self.keys.add("empty", channels=[])
        self.assertEqual(self.keys.default_channel("empty"), "elio-zeta")
        self.assertEqual(self.keys.channels_for("cole"), ["house"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
