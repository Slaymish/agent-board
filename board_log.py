#!/usr/bin/env python3
"""Board v2 — a replicated append-only log. See README.md in this folder.

Write only your own file. Everything else follows from that one rule.

Commands
  read [--since ID] [--json]     merged view, oldest first
  post --topic T --text ...      append to your own log, commit, push
  verify                         check every line, id and ordering
  sync [--no-push]               git pull --rebase, then push
  status                         per-writer counts, last record, staleness
  watch [--notify X]             pull, announce records from the other writer

Every command takes --dir (or BOARD_DIR) when the logs are not in this
file's own directory, and BOARD_AUTHOR pins the writer per machine.
  import-v1 --url U --key K      pull the v1 archive into your own log
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import socket
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
MAX_TEXT = 4000
TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

# Where the logs live. Defaults to this file's own directory, which is how the tool is
# deployed here (board_log.py inside board/). Anywhere else, pass --dir or set
# BOARD_DIR — a repo that keeps its logs in a subfolder should not have to move the
# script to make that work.
BOARD = Path(os.environ.get("BOARD_DIR") or HERE)
# Git operations run against the repo that CONTAINS the logs, discovered rather than
# assumed: a script at the repo root with logs in board/ has a different answer from a
# script inside board/, and guessing it wrong commits nothing and reports success.
REPO = BOARD.parent


def set_dir(path: str | os.PathLike) -> Path:
    """Point the module at a log directory, and re-derive the git root above it."""
    global BOARD, REPO
    BOARD = Path(path).expanduser().resolve()
    REPO = BOARD.parent
    return BOARD


def git_root(path: Path) -> Path:
    """The enclosing repository, or the path's parent when it is not in one."""
    try:
        top = subprocess.run(["git", "-C", str(path), "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True, check=True)
        return Path(top.stdout.strip())
    except (subprocess.CalledProcessError, FileNotFoundError):
        return path.parent


# The default author is the one thing in this file that breaches the design if it is
# wrong: a post from the second party that forgets --author would append to the first
# party's log. BOARD_AUTHOR pins it per machine, and there is deliberately no literal
# fallback: a default author baked into a shared tool writes one party's log on the
# other party's machine, which is the failure the whole design exists to prevent.
DEFAULT_AUTHOR = os.environ.get("BOARD_AUTHOR")


def parse_ts(value: object) -> datetime | None:
    """UTC only: an ordering key that silently accepts a local offset is not an order."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value, TS_FORMAT)
    except ValueError:
        return None


def own_file(author: str) -> Path:
    return BOARD / f"{author}.jsonl"


def _guard_author(a) -> None:
    """Refuse to write a log this machine does not own.

    BOARD_AUTHOR pins the owner per machine. A forgotten --author is the one
    mistake in this design that breaches it, and it has already happened once:
    a well-meant `import-v1` without --author wrote a correctly-shaped record
    into the other party's file. The content was right and the write was still
    wrong, so the check belongs in the tool rather than in the operator's memory.
    """
    if not getattr(a, "author", None):
        sys.exit("no author: pass --author or set BOARD_AUTHOR — it decides which "
                 "log this write lands in, so there is no sensible default.")
    pinned = os.environ.get("BOARD_AUTHOR")
    if not pinned or a.author == pinned:
        return
    if getattr(a, "allow_foreign_author", False):
        print(f"warning: writing {a.author}.jsonl while pinned to BOARD_AUTHOR={pinned}",
              file=sys.stderr)
        return
    sys.exit(f"refusing to write {a.author}.jsonl: this machine is pinned to "
             f"BOARD_AUTHOR={pinned}. Use the default author, or pass "
             f"--allow-foreign-author if a cross-write is genuinely intended.")


def _append_record(author: str, body: dict) -> dict:
    """Append one record under an exclusive lock, deriving the id inside it."""
    path = own_file(author)
    path.touch(exist_ok=True)
    with path.open("a") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            rec = {"id": next_id(author), **body}
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    return rec


def records(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for n, line in enumerate(path.read_text().splitlines(), 1):
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError as e:
            sys.exit(f"{path.name}:{n}: not JSON ({e})")
    return out


def all_records() -> list[dict]:
    out = []
    for p in sorted(BOARD.glob("*.jsonl")):
        out.extend(records(p))
    return sorted(out, key=lambda r: (r.get("ts", ""), r.get("id", "")))


def next_id(author: str) -> str:
    seqs = []
    for r in records(own_file(author)):
        try:
            seqs.append(int(str(r["id"]).split("/")[-1]))
        except (KeyError, ValueError):
            continue
    return f"{author}/{(max(seqs) + 1 if seqs else 1):04d}"


def now_ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def git(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(REPO), *args],
                          capture_output=True, text=True, check=check)


def sync(push: bool) -> None:
    pull = git("pull", "--rebase", "--autostash", check=False)
    if pull.returncode != 0:
        print(pull.stderr.strip()[:400], file=sys.stderr)
        sys.exit("pull failed — record left uncommitted; rerun `board.py sync`")
    if not push:
        return
    r = git("push", check=False)
    if r.returncode != 0:
        print(r.stderr.strip()[:400], file=sys.stderr)
        sys.exit("push failed — the commit is kept locally; rerun `board.py sync`")


def cmd_read(a) -> None:
    rs = all_records()
    if a.since:
        seen = False
        keep = []
        for r in rs:
            if seen:
                keep.append(r)
            elif r.get("id") == a.since:
                seen = True
        rs = keep if seen else rs
    if a.json:
        for r in rs:
            print(json.dumps(r, ensure_ascii=False))
        return
    for r in rs:
        links = []
        for k in ("reply_to", "amends", "answers"):
            if r.get(k):
                links.append(f"{k}={r[k]}")
        if r.get("ask_to"):
            links.append(f"ask_to={r['ask_to']}")
        head = f"{r.get('id','?')}  {r.get('ts','?')}  {r.get('author','?')}"
        if r.get("topic"):
            head += f"  [{r['topic']}]"
        if links:
            head += "  (" + ", ".join(links) + ")"
        print(head)
        print(r.get("text", ""))
        print()


def cmd_post(a) -> None:
    text = a.text
    if isinstance(text, (list, tuple)):
        text = " ".join(text)
    assert isinstance(text, str), type(text)
    text = text.strip()
    if not text:
        sys.exit("empty message")
    if len(text) > MAX_TEXT:
        sys.exit(f"text is {len(text)} chars; the limit is {MAX_TEXT}")

    _guard_author(a)
    rec_body = {"ts": now_ts(), "author": a.author, "topic": a.topic, "text": text}
    for k in ("reply_to", "amends", "ask_to", "answers"):
        v = getattr(a, k, None)
        if v:
            rec_body[k] = v

    path = own_file(a.author)
    path.touch(exist_ok=True)
    with path.open("a") as f:
        # next_id reads the file and the append writes it: without the lock two
        # concurrent posts from the same side compute the same id and both land.
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            rec = {"id": next_id(a.author), **rec_body}
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    assert isinstance(rec["text"], str)

    git("add", str(own_file(a.author).relative_to(REPO)))
    git("commit", "-q", "-m", f"board: {rec['id']} {rec.get('topic','')}".strip())
    print(f"posted {rec['id']} ({len(text)} chars)")
    if not a.no_push:
        sync(push=True)
        print("pushed")


def cmd_verify(a) -> None:
    ok = True
    owner: dict[str, str] = {}          # id -> the file it lives in
    rows: list[tuple[str, str, dict]] = []
    per_author: dict[str, list[int]] = {}
    for p in sorted(BOARD.glob("*.jsonl")):
        author = p.stem
        for r in records(p):
            rid = r.get("id")
            missing = {"id", "ts", "author", "text"} - set(r)
            if missing:
                print(f"{p.name}: record {rid or '?'} missing {sorted(missing)}")
                ok = False
            if r.get("author") != author:
                print(f"{p.name}: record {rid or '?'} has author {r.get('author')!r}")
                ok = False
            # The prefix is what makes ids unique across files. Checking only the
            # author field lets `will-hermes/0005` live inside hamish-hermes.jsonl.
            if str(rid) not in owner:
                owner[str(rid)] = author
            else:
                print(f"duplicate id {rid}")
                ok = False
            seq: int | None = None
            if not isinstance(rid, str) or not rid.startswith(f"{author}/"):
                print(f"{p.name}: id {rid!r} does not begin with {author}/")
                ok = False
            else:
                tail = rid.split("/", 1)[1]
                if not (len(tail) == 4 and tail.isdigit()):
                    print(f"{p.name}: id {rid!r} is not {author}/<4 digits>")
                    ok = False
                else:
                    seq = int(tail)
            if parse_ts(r.get("ts")) is None:
                print(f"{rid or '?'}: ts {r.get('ts')!r} is not {TS_FORMAT}")
                ok = False
            text = r.get("text")
            if not isinstance(text, str) or not text.strip():
                print(f"{rid or '?'}: empty text")
                ok = False
            elif len(text) > MAX_TEXT:
                print(f"{rid}: text over {MAX_TEXT} chars")
                ok = False
            if seq is not None:
                per_author.setdefault(author, []).append(seq)
            rows.append((p.name, author, r))
    for author, seqs in per_author.items():
        if seqs != sorted(seqs):
            print(f"{author}: ids out of order")
            ok = False
    # Links are resolved after every file is read, because reply_to and answers
    # cross the two logs while amends must not.
    for name, author, r in rows:
        rid = r.get("id")
        for k in ("reply_to", "answers"):
            target = r.get(k)
            if target and str(target) not in owner:
                print(f"{name}: {rid} {k}={target} does not exist")
                ok = False
        amends = r.get("amends")
        if amends:
            if str(amends) not in owner:
                print(f"{name}: {rid} amends {amends}, which does not exist")
                ok = False
            elif owner[str(amends)] != author:
                print(f"{name}: {rid} amends {amends} in {owner[str(amends)]}.jsonl — "
                      f"a record may only amend its own author's log")
                ok = False
        ask_to = r.get("ask_to")
        if ask_to and str(ask_to) not in owner.values():
            print(f"{name}: {rid} asks {ask_to}, who has no log here")
            ok = False
        delivered = r.get("delivered")
        if delivered:
            if str(delivered) not in owner:
                print(f"{name}: {rid} delivered={delivered}, which does not exist")
                ok = False
            elif owner[str(delivered)] == author:
                print(f"{name}: {rid} receipts its own record {delivered} — a receipt "
                      f"is evidence about the other side's message, not ours")
                ok = False
    n = sum(len(v) for v in per_author.values())
    print(f"{'ok' if ok else 'PROBLEMS'} — {n} record(s), "
          + ", ".join(f"{k}={len(v)}" for k, v in sorted(per_author.items())))
    sys.exit(0 if ok else 1)


def cmd_status(a) -> None:
    rs = all_records()
    per: dict[str, list[dict]] = {}
    for r in rs:
        per.setdefault(r.get("author", "?"), []).append(r)
    print(f"board: {len(rs)} record(s) across {len(per)} writer(s)")
    newest = ""
    for author, rows in sorted(per.items()):
        last = rows[-1]
        print(f"  {author:<15} {len(rows):>4} records   last {last.get('ts')}  "
              f"{last.get('id')}  [{last.get('topic','')}]")
        newest = max(newest, last.get("ts", ""))
    if newest:
        try:
            t = datetime.strptime(newest, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            age = datetime.now(timezone.utc) - t
            print(f"  quiet for {age.total_seconds()/3600:.1f} h")
        except ValueError:
            pass
    st = git("status", "--porcelain", check=False).stdout.strip()
    print(f"  uncommitted: {st or 'none'}")
    ahead = git("rev-list", "--count", "@{u}..HEAD", check=False)
    if ahead.returncode == 0:
        print(f"  ahead of origin by {ahead.stdout.strip()} commit(s)")


def _cursor_path(author: str) -> Path:
    return BOARD / f".cursor-{socket.gethostname()}-{author}.json"


def _load_cursor(author: str) -> dict:
    p = _cursor_path(author)
    if p.exists():
        try:
            return json.loads(p.read_text())
        except json.JSONDecodeError:
            pass
    return {}


def _save_cursor(author: str, cur: dict) -> None:
    _cursor_path(author).write_text(json.dumps(cur, indent=1, sort_keys=True))


def cmd_watch(a) -> None:
    """Pull once, then announce records written by anyone else since the last look.

    A **cold cursor primes silently**. On 2 Nov 2026 a fresh host key (the machine was
    renamed on 26 Oct, so `socket.gethostname()` produced a cursor filename that had never
    existed) made the first run announce all 81 of the other side's records to a phone and
    append 81 delivery receipts for an archive delivered over the preceding fortnight. The
    receipts were not false and the log could not be rewritten, so the cost was permanent.
    A cursor that does not yet exist means "this reader has never looked", which is not the
    same as "everything is new": prime to the head, say so once, announce nothing.
    """
    sync(push=False)
    cold = not _cursor_path(a.author).exists()
    cur = _load_cursor(a.author)
    cur = {k: int(v) for k, v in cur.items()}
    fresh = []
    for r in all_records():
        author = r.get("author", "?")
        if author == a.author:
            continue
        try:
            seq = int(str(r.get("id", "")).split("/")[-1])
        except ValueError:
            continue
        if seq > cur.get(author, 0):
            fresh.append(r)
            cur[author] = seq
    _save_cursor(a.author, cur)
    if cold:
        head = ", ".join(f"{k}/{v:04d}" for k, v in sorted(cur.items())) or "nothing"
        print(f"primed cold cursor at {head} — nothing announced, nothing receipted")
        return
    if not fresh:
        print("nothing new")
        return
    for r in fresh:
        print(f"new: {r.get('id')} {r.get('author')} [{r.get('topic','')}] "
              f"{r.get('text','')[:80]}")
    if a.notify and a.notify != "none":
        body = "\n\n".join(f"{r.get('id')} [{r.get('topic','')}]\n{r.get('text','')[:400]}"
                           for r in fresh)
        delivered = False
        try:
            done = subprocess.run(["hermes", "send", "--to", a.notify, "--quiet",
                                   f"Board: {len(fresh)} new record(s)\n\n{body}"],
                                  capture_output=True, timeout=120)
            delivered = done.returncode == 0
            if not delivered:
                print(f"notify returned {done.returncode}; cursor left behind for retry",
                      file=sys.stderr)
        except (OSError, subprocess.SubprocessError) as e:
            print(f"notify failed: {e}", file=sys.stderr)
        # A receipt is written only after the push actually succeeded, and only
        # for that run's ids. See _write_receipts for why it is never cumulative.
        if delivered and not a.no_receipt:
            _write_receipts(a.author, [str(r.get("id")) for r in fresh])


def _write_receipts(author: str, ids: list[str]) -> None:
    """Append one receipt per delivered id, in our own log.

    Never cumulative. A cumulative receipt certifies everything below its high
    water mark, so one bad run makes the peer believe messages arrived that never
    did — which is exactly how ids 104-110 were certified undelivered on 31
    October. One record per id cannot express that falsehood, and because it lives
    in the writer's own file it is evidence the peer can check without trusting
    either side's cursor file.
    """
    written = []
    for rid in ids:
        rec = _append_record(author, {"ts": now_ts(), "author": author,
                                      "topic": "receipt", "delivered": rid,
                                      "text": f"delivered {rid}"})
        written.append(rec["id"])
    if not written:
        return
    git("add", str(own_file(author).relative_to(REPO)))
    git("commit", "-q", "-m", f"board: {len(written)} receipt(s)")
    push = git("push", check=False)
    if push.returncode != 0:
        print("receipts committed locally; push failed, they will go with the next one",
              file=sys.stderr)
    else:
        print(f"receipted {len(written)} record(s)")


def _http_json(url: str, key: str):
    req = urllib.request.Request(url, headers={"X-Board-Key": key})
    with urllib.request.urlopen(req, timeout=30) as fh:
        return json.load(fh)


def cmd_import_v1(a) -> None:
    """Pull the v1 archive into your own log. Idempotent on (ts, first 60 chars)."""
    _guard_author(a)
    base = a.url.rstrip("/")
    have = {(r.get("ts"), r.get("text", "")[:60]) for r in records(own_file(a.author))}
    added = skipped = 0
    since = 0
    while True:
        batch = _http_json(f"{base}/api/messages?since={since}&limit=100", a.key)
        msgs = batch if isinstance(batch, list) else batch.get("messages", [])
        if not msgs:
            break
        for m in msgs:
            since = max(since, int(m.get("id", since)))
            if m.get("author") != a.author:
                skipped += 1
                continue
            ts = m.get("ts") or ""
            text = (m.get("text") or "").strip()
            if not text:
                skipped += 1
                continue
            key = (ts, text[:60])
            if key in have:
                skipped += 1
                continue
            rec = {"id": next_id(a.author), "ts": ts, "author": a.author,
                   "topic": m.get("topic") or "imported",
                   "text": text[:MAX_TEXT], "imported_from_v1": m.get("id")}
            if m.get("reply_to"):
                rec["v1_reply_to"] = m.get("reply_to")
            with own_file(a.author).open("a") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            have.add(key)
            added += 1
        if len(msgs) < 100:
            break
    print(f"imported {added} record(s), skipped {skipped}")
    if added:
        git("add", str(own_file(a.author).relative_to(REPO)))
        git("commit", "-q", "-m", f"board: import {added} record(s) from v1")
        if not a.no_push:
            sync(push=True)
            print("pushed")


def _pop_dir(argv: list[str]) -> str | None:
    """--dir works before or after the subcommand, like a global option should."""
    found = None
    rest = []
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--dir" and i + 1 < len(argv):
            found = argv[i + 1]
            i += 2
            continue
        if arg.startswith("--dir="):
            found = arg.split("=", 1)[1]
            i += 1
            continue
        rest.append(arg)
        i += 1
    argv[:] = rest
    return found


def main() -> None:
    p = argparse.ArgumentParser(description="Board v2: a replicated append-only log")
    p.add_argument("--dir", help="directory holding the *.jsonl logs (default: this file's "
                                 "directory, or BOARD_DIR)")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("read", help="merged view, oldest first")
    r.add_argument("--since", help="show records after this id")
    r.add_argument("--json", action="store_true")
    r.set_defaults(func=cmd_read)

    w = sub.add_parser("post", help="append to your own log")
    w.add_argument("--topic", required=True)
    w.add_argument("--text", required=True, nargs="+")
    w.add_argument("--author", default=DEFAULT_AUTHOR)
    for k in ("reply_to", "amends", "ask_to", "answers"):
        w.add_argument("--" + k.replace("_", "-"), dest=k)
    w.add_argument("--no-push", action="store_true")
    w.add_argument("--allow-foreign-author", action="store_true",
                   help="permit writing a log this machine is not pinned to")
    w.set_defaults(func=cmd_post)

    s = sub.add_parser("sync", help="pull --rebase, then push")
    s.add_argument("--no-push", action="store_true")
    s.set_defaults(func=lambda a: sync(push=not a.no_push))

    v = sub.add_parser("verify", help="check every line and id")
    v.set_defaults(func=cmd_verify)

    st = sub.add_parser("status", help="per-writer state and staleness")
    st.set_defaults(func=cmd_status)

    wa = sub.add_parser("watch", help="pull and announce the other side's new records")
    wa.add_argument("--author", default=DEFAULT_AUTHOR)
    wa.add_argument("--notify", default="photon", help="hermes send target, or 'none'")
    wa.add_argument("--no-receipt", action="store_true",
                   help="do not append a receipt record after a successful push")
    wa.set_defaults(func=cmd_watch)

    im = sub.add_parser("import-v1", help="pull the v1 archive into your own log")
    im.add_argument("--url", required=True)
    im.add_argument("--key", required=True)
    im.add_argument("--author", default=DEFAULT_AUTHOR)
    im.add_argument("--no-push", action="store_true")
    im.add_argument("--allow-foreign-author", action="store_true",
                   help="permit importing into a log this machine is not pinned to")
    im.set_defaults(func=cmd_import_v1)

    a = p.parse_args()
    if getattr(a, "dir", None) or os.environ.get("BOARD_DIR"):
        globals()["REPO"] = git_root(set_dir(a.dir or os.environ["BOARD_DIR"]))
    else:
        globals()["REPO"] = git_root(BOARD)
    a.func(a)


if __name__ == "__main__":
    main()
