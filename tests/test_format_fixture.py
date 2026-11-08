#!/usr/bin/env python3
"""Two implementations, one format: a shared fixture both CLIs must agree on.

`board_log.py` here and `board/board.py` in the private log repo are independent
readings of the same JSONL protocol. The risk of two implementations is not that they
disagree loudly, it is that they disagree quietly — a record committed by one and
rejected by the other. This fixture is one small log and two assertions: both accept a
clean log and count the same records, both reject a duplicate id.

  python3 tests/test_format_fixture.py
  PEER_BOARD=/path/to/board.py python3 tests/test_format_fixture.py

The peer CLI is optional; without it the fixture still runs against this one.
"""
import json
import os
import pathlib
import subprocess
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
CLI = HERE.parent / "board_log.py"
PEER = os.environ.get("PEER_BOARD", "")

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
    print("ok", name)


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


def peer_supports_dir(peer):
    """A conformance comparison needs both CLIs to be told the same thing.

    The first run of this fixture failed on exactly that: the peer's `board.py`
    took the directory positionally-ish and read `--dir <path>` as the subcommand,
    because the flag was still on an unmerged branch. Skipping with the reason is
    better than a red test that is describing a version difference.
    """
    try:
        out = subprocess.run([sys.executable, str(peer), "--help"],
                             capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return False
    return "--dir" in (out.stdout + out.stderr)


if __name__ == "__main__":
    check(CLI, "board_log.py")
    if not PEER or not pathlib.Path(PEER).exists():
        print("skip peer: set PEER_BOARD to the other repo's board.py to compare")
    elif not peer_supports_dir(PEER):
        print("skip peer: %s does not take --dir yet (the flag is on a branch)" % PEER)
    else:
        check(PEER, pathlib.Path(PEER).name + " (peer)")
