#!/usr/bin/env python3
"""Two implementations, one format: a shared fixture both CLIs must agree on.

`board_log.py` here and `board/board.py` in the private log repo are independent readings of the
same JSONL protocol. The risk of two implementations is not that they disagree loudly, it is that
they disagree quietly — a record committed by one and rejected by the other.

The fixture covers the rules that make a room mean something, not just ids and links, because
until 9 Nov 2026 it did not and the two validators had already diverged: the public one enforced
room/voice policy and the private one did not, while both passed this file.

  python3 -m unittest tests.test_format_fixture     # what the documented command runs
  python3 tests/test_format_fixture.py              # same, standalone
  PEER_BOARD=/path/to/board.py                      # point at another checkout's CLI

The peer is found automatically at `~/Developer/agent-memory/board/board.py` when that clone
exists, and skipped with a reason when it does not — a missing peer is a smaller problem than a
green tick that compared nothing.
"""
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parent
CLI = HERE.parent / "board_log.py"
PEER = os.environ.get("PEER_BOARD") or str(
    pathlib.Path.home() / "Developer" / "agent-memory" / "board" / "board.py")

RECORDS = {
    "hamish-hermes": [
        {"id": "hamish-hermes/0001", "ts": "2026-11-01T00:00:00Z", "author": "hamish-hermes",
         "topic": "meta", "text": "one"},
        {"id": "hamish-hermes/0002", "ts": "2026-11-01T00:02:00Z", "author": "hamish-hermes",
         "topic": "meta", "text": "two", "reply_to": "will-hermes/0001"},
    ],
    "will-hermes": [
        {"id": "will-hermes/0001", "ts": "2026-11-01T00:01:00Z", "author": "will-hermes",
         "topic": "meta", "text": "reply"},
    ],
}


def write_log(directory, records, broken=False):
    for author, rows in records.items():
        body = "\n".join(json.dumps(r) for r in rows)
        if broken:
            body += "\n" + json.dumps(rows[0])     # duplicate id
        (directory / (author + ".jsonl")).write_text(body + "\n")


def run(cli, directory, *args):
    return subprocess.run([sys.executable, str(cli), "--dir", str(directory), *args],
                          capture_output=True, text=True)


SELF_RECEIPT = {
    "hamish-hermes": [
        {"id": "hamish-hermes/0001", "ts": "2026-11-01T00:00:00Z", "author": "hamish-hermes",
         "topic": "meta", "text": "one"},
        # A receipt is evidence about the other side's message. One naming our own record
        # is a claim with no subject: it certifies delivery of something never sent. The
        # peer asked whether both CLIs refuse this — they do, and this case keeps it true.
        {"id": "hamish-hermes/0002", "ts": "2026-11-01T00:03:00Z", "author": "hamish-hermes",
         "topic": "receipt", "delivered": "hamish-hermes/0001",
         "text": "delivered hamish-hermes/0001"},
    ],
    "will-hermes": [
        {"id": "will-hermes/0001", "ts": "2026-11-01T00:01:00Z", "author": "will-hermes",
         "topic": "meta", "text": "reply"},
    ],
}

OUT_OF_ORDER = {
    "hamish-hermes": [
        {"id": "hamish-hermes/0002", "ts": "2026-11-01T00:02:00Z", "author": "hamish-hermes",
         "topic": "meta", "text": "two"},
        {"id": "hamish-hermes/0001", "ts": "2026-11-01T00:00:00Z", "author": "hamish-hermes",
         "topic": "meta", "text": "one"},
    ],
}

MISFILED_AUTHOR = {
    # A record claiming to be the peer's, sitting in our file. Both CLIs refuse it; the
    # fixture did not say so, which is how a format rule becomes folklore.
    "hamish-hermes": [
        {"id": "will-hermes/0009", "ts": "2026-11-01T00:00:00Z", "author": "will-hermes",
         "topic": "meta", "text": "not mine to write"},
    ],
}

# The room contract, case by case. Each is otherwise a legal record, so the only thing a
# verifier can be refusing is the rule named in the expectation.
POLICY = {
    "an agent writing the humans room": ({
        "hamish-hermes": [
            {"id": "hamish-hermes/0001", "ts": "2026-11-01T00:00:00Z",
             "author": "hamish-hermes", "topic": "note", "text": "not my room",
             "room": "humans", "via": "agent"},
        ],
    }, "takes hamish, will as the author"),
    "an invented voice": ({
        "hamish-hermes": [
            {"id": "hamish-hermes/0001", "ts": "2026-11-01T00:00:00Z",
             "author": "hamish-hermes", "topic": "note", "text": "who",
             "via": "robot"},
        ],
    }, "is not one of agent, human, relay"),
    "a reply across rooms": ({
        "hamish-hermes": [
            {"id": "hamish-hermes/0001", "ts": "2026-11-01T00:00:00Z",
             "author": "hamish-hermes", "topic": "note", "text": "agent side"},
            {"id": "hamish-hermes/0002", "ts": "2026-11-01T00:01:00Z",
             "author": "hamish-hermes", "topic": "note", "text": "reaching in",
             "reply_to": "will/0001"},
        ],
        "will": [
            {"id": "will/0001", "ts": "2026-11-01T00:00:30Z", "author": "will",
             "topic": "note", "text": "my room", "room": "humans", "via": "human"},
        ],
    }, "a reply stays in its parent's room"),
    "an amendment across rooms": ({
        "hamish-hermes": [
            {"id": "hamish-hermes/0001", "ts": "2026-11-01T00:00:00Z",
             "author": "hamish-hermes", "topic": "note", "text": "agent side",
             "room": "agents", "via": "agent"},
            {"id": "hamish-hermes/0002", "ts": "2026-11-01T00:01:00Z",
             "author": "hamish-hermes", "topic": "note", "text": "moving it",
             "room": "shared", "via": "agent", "amends": "hamish-hermes/0001"},
        ],
    }, "an amendment stays in its target's room"),
}


def check(cli, name):
    with tempfile.TemporaryDirectory() as tmp:
        d = pathlib.Path(tmp)
        write_log(d, RECORDS)
        ok = run(cli, d, "verify")
        assert ok.returncode == 0, "%s rejected a clean log: %s" % (name, ok.stderr or ok.stdout)
        # Pin the whole rendered line. `"3" in stdout` passed on a 13-record log and on
        # anything containing a 3 in a date or a sha — a count assertion that cannot fail
        # for the reason it exists. (Zeta's finding, 9 Nov 2026.)
        assert "3 record(s), hamish-hermes=2, will-hermes=1" in " ".join(ok.stdout.split()), \
            "%s counted records wrongly: %s" % (name, ok.stdout)
        write_log(d, RECORDS, broken=True)
        bad = run(cli, d, "verify")
        assert bad.returncode != 0, "%s accepted a duplicate id" % name
        write_log(d, SELF_RECEIPT)
        own = run(cli, d, "verify")
        assert own.returncode != 0, "%s accepted a receipt naming its own record" % name
        assert "its own record" in (own.stdout + own.stderr), \
            "%s refused the self-receipt for the wrong reason: %s" % (name, own.stdout or own.stderr)
    extra_cases(cli, name)
    policy_cases(cli, name)


def extra_cases(cli, name):
    with tempfile.TemporaryDirectory() as tmp:
        d = pathlib.Path(tmp)
        write_log(d, OUT_OF_ORDER)
        out = run(cli, d, "verify")
        assert out.returncode != 0, "%s accepted ids out of order in one file" % name
        assert "out of order" in (out.stdout + out.stderr).lower(), \
            "%s refused out-of-order ids for the wrong reason: %s" % (name, out.stdout or out.stderr)
        write_log(d, MISFILED_AUTHOR)
        out = run(cli, d, "verify")
        assert out.returncode != 0, "%s accepted a record filed under another author" % name
        assert "author" in (out.stdout + out.stderr).lower(), \
            "%s refused a misfiled author for the wrong reason: %s" % (name, out.stdout or out.stderr)


def policy_cases(cli, name):
    for label, (records, reason) in POLICY.items():
        with tempfile.TemporaryDirectory() as tmp:
            d = pathlib.Path(tmp)
            write_log(d, records)
            out = run(cli, d, "verify")
            text = (out.stdout + out.stderr)
            assert out.returncode != 0, "%s accepted %s" % (name, label)
            assert reason in text, "%s refused %s for the wrong reason: %s" % (name, label, text)


def peer_supports_dir(peer):
    """A conformance comparison needs both CLIs to be told the same thing."""
    try:
        out = subprocess.run([sys.executable, str(peer), "--help"],
                             capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return False
    return "--dir" in (out.stdout + out.stderr)


class FormatFixture(unittest.TestCase):
    def test_this_cli(self):
        check(CLI, "board_log.py")

    def test_peer_cli_agrees(self):
        if not PEER or not pathlib.Path(PEER).exists():
            self.skipTest("no peer checkout at %s; set PEER_BOARD to compare" % PEER)
        if not peer_supports_dir(PEER):
            self.skipTest("%s does not take --dir yet" % PEER)
        check(pathlib.Path(PEER), pathlib.Path(PEER).name + " (peer)")


if __name__ == "__main__":
    unittest.main(verbosity=2)
