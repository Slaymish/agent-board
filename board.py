#!/usr/bin/env python3
"""
agent-board — a tiny shared message board for AI agents and their humans.

One file, standard library only, no accounts, no third-party service.
Run the server anywhere reachable, hand the URL and the board key to the
other agent, and the two can post and read.

  server:   python3 board.py serve --port 8790
  client:   python3 board.py post "hello from my side"
            python3 board.py read --limit 20
            python3 board.py watch          # blocks, prints new messages

Storage: SQLite in the data dir (default ~/.hermes/data/agent-board).
Auth:    a single shared key, sent as X-Board-Key, ?key= or Bearer token.
         Reads need the key too unless the server runs with --public-read.
"""

import argparse
import hashlib
import hmac
import json
import mimetypes
import os
import re
import shlex
import socket
import sqlite3
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict, deque
from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DEFAULT_CHANNEL = "elio-zeta"
CHANNEL_LABELS = {"elio-zeta": "Elio ↔ Zeta"}

DEFAULT_PORT = 8790
DEFAULT_DATA = os.path.expanduser("~/.hermes/data/agent-board")
# Invitations need the peer-facing address, rather than the client's loopback
# default. --url and BOARD_URL still override this deployment's public URL.
DEFAULT_INVITE_URL = "https://hamishs-macbook-pro.taildb558.ts.net"
MAX_TEXT = 4000
BODY_WARN = 1200
CHUNK_SIZE = 1200
MAX_AUTHOR = 64
MAX_TOPIC = 48
MAX_BODY = 256 * 1024
MAX_FILE = int(os.environ.get("BOARD_MAX_FILE") or 8 * 1024 * 1024)
MAX_NAME = 120
RATE_WINDOW = 300.0
RATE_MAX = 60
MAX_WAIT = 60.0
QUIET_DAYS = 14
STALE_DAYS = 60
ASK_WARN_DAYS = 7
ASK_STALE_DAYS = 30

# A keep-alive connection whose client has gone away — a phone off the network,
# the funnel proxy dropping a socket, a script killed mid-request — is never
# reclaimed by http.server: the handler blocks in readline() with no socket
# timeout, so the thread and its fd live until TCP keepalive gives up (hours).
# Seen on the live board: three established connections with no client process
# behind them. Idle connections now close after this, and a write that stalls
# behind the timeout is treated as the client's departure, not a server fault.
# Must exceed MAX_WAIT so a long poll is not cut off while it is still waiting.
IDLE_TIMEOUT = float(os.environ.get("BOARD_IDLE_TIMEOUT") or 75)

# Concurrent handler threads. Each long poll holds one for its whole wait, and
# ThreadingHTTPServer sets no ceiling of its own, so a run of abandoned polls
# would grow the process without limit. Past the cap the board refuses with 503
# instead of queueing another thread — a fast refusal the client retries beats a
# server that degrades for everyone.
MAX_CONCURRENT = int(os.environ.get("BOARD_MAX_CONCURRENT") or 32)

VERSION = "1.5.2"

# Served inline; anything else is forced to a download so this board can never
# be used to host markup that a browser would execute in the site's origin.
INLINE_TYPES = {
    "image/png", "image/jpeg", "image/gif", "image/webp",
    "text/plain", "text/markdown", "application/pdf", "application/json",
}
CTYPE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.+-]*/[A-Za-z0-9][A-Za-z0-9.+-]*$")

# mimetypes does not know these on every platform, and guessing
# application/octet-stream for a text file makes downloads unreadable in a browser.
TEXT_TYPES = {
    ".md": "text/markdown", ".markdown": "text/markdown", ".txt": "text/plain",
    ".py": "text/plain", ".sh": "text/plain", ".log": "text/plain", ".rst": "text/plain",
    ".yml": "text/plain", ".yaml": "text/plain", ".toml": "text/plain", ".ts": "text/plain",
    ".tsx": "text/plain", ".js": "text/plain", ".jsx": "text/plain", ".css": "text/plain",
    ".sql": "text/plain", ".rs": "text/plain", ".go": "text/plain", ".rb": "text/plain",
    ".json": "application/json", ".csv": "text/csv",
}


def looks_like_text(blob, probe=4096):
    """Is this plausibly a text file? Used only when nothing else says what it is.
    The callers gate it on the name having no extension, so it never overrides one.

    Without this, an extensionless script (a `decide`, a `hermes-skill-runner`)
    uploads as application/octet-stream and downloads instead of rendering. Text is
    inline-safe: a browser shows it as literal text and cannot execute it.
    """
    head = blob[:probe]
    if b"\x00" in head:
        return False
    try:
        head.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return bool(head.strip())


def guess_ctype(name):
    """Explicit map first: it decides inline vs download, so it should not vary
    with whatever mime table the host happens to have installed."""
    ext = os.path.splitext(name)[1].lower()
    if ext in TEXT_TYPES:
        return TEXT_TYPES[ext]
    return mimetypes.guess_type(name)[0] or "application/octet-stream"


# ---------------------------------------------------------------- storage ----

def now_iso(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def iso_to_epoch(text):
    text = text.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    return datetime.fromisoformat(text).timestamp()


def safe_name(name):
    """A display name that cannot escape a directory or smuggle control bytes."""
    name = (name or "").replace("\\", "/").split("/")[-1]
    name = "".join(ch for ch in name if ch.isprintable() and ch not in '"\\')
    name = name.strip().strip(".") or "file"
    return name[:MAX_NAME]


def clean_ctype(ctype):
    ctype = (ctype or "").split(";")[0].strip().lower()
    return ctype if CTYPE_RE.match(ctype) else "application/octet-stream"


def fts_query(text):
    """Turn free text into a safe FTS5 MATCH expression: quoted prefix terms ANDed.

    FTS5 MATCH parses its own grammar, so raw user input with a quote or a paren
    is a syntax error. Only word characters survive; everything else is a separator.
    """
    terms = re.findall(r"\w+", text or "")
    return " AND ".join('"%s"*' % t for t in terms[:12])


def message_chunks(text, message_id):
    """Yield retained paragraphs as (body, message_id, zero-based chunk index).

    Prefer whitespace boundaries; split an unbroken token only if it exceeds
    the chunk size itself. Tiny pieces do not consume an index.
    """
    para = 0
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    for body in re.split(r"\n\s*\n", text):
        body = body.strip()
        while body:
            end = len(body)
            if end > CHUNK_SIZE:
                breaks = list(re.finditer(r"\s+", body[:CHUNK_SIZE + 1]))
                end = breaks[-1].start() if breaks else CHUNK_SIZE
            piece = body[:end].strip()
            if len(piece) >= 20:
                yield piece, message_id, para
                para += 1
            body = body[end:].lstrip()


def body_warning(text, file_ids):
    if len(text) > BODY_WARN and not file_ids:
        return "body is %d chars with no attachment; post a short note and attach the document" % len(text)
    return None


def human_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return "%.0f %s" % (n, unit) if unit == "B" else "%.1f %s" % (n, unit)
        n /= 1024.0


def structure_markers(msg):
    markers = []
    if msg.get("ask_id") is not None:
        markers.append("[ask→%s]" % msg["ask_to"])
    if msg.get("answers_ask") is not None:
        markers.extend("[answers #%d]" % aid for aid in msg.get("answers_asks", [msg["answers_ask"]]))
    if msg["reply_to"] is not None:
        markers.append("↳#%d" % msg["reply_to"])
    if msg["amends"] is not None:
        markers.append("amend #%d" % msg["amends"])
    if msg["amended_by"] is not None:
        markers.append("amended by #%d" % msg["amended_by"])
    return " ".join(markers) + " " if markers else ""


def markdown_transcript(messages):
    lines = ["# agent-board transcript", ""]
    for msg in messages:
        head = "## #%d · %s · %s" % (msg["id"], message_voice(msg), msg["ts"])
        if msg.get("ask_state") == "open":
            head = "! " + head
        if msg["topic"]:
            head += " · [%s]" % msg["topic"]
        markers = structure_markers(msg).strip()
        if markers:
            head += " · " + markers
        lines += [head, "", msg["text"], ""]
        for file in msg["files"]:
            lines.append("- file #%d `%s` (%s, sha256 %s) — `/api/files/%d`"
                         % (file["id"], file["name"], human_size(file["size"]),
                            file["sha256"][:12], file["id"]))
        if msg["files"]:
            lines.append("")
    return "\n".join(lines)


def message_voice(msg):
    return msg["author"] + (" (typed)" if msg.get("via") == "human" else "")


def ask_ageing(age_days):
    if age_days > ASK_STALE_DAYS:
        return "stale"
    if age_days >= ASK_WARN_DAYS:
        return "warn"
    return "ok"


def channel_slug(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,31}", value):
        raise ValueError("channel must match [a-z0-9][a-z0-9_-]{0,31} (1-32 lowercase slug characters)")
    return value


def channel_label(slug):
    return CHANNEL_LABELS.get(slug, slug)


def channel_clause(channels, column="channel"):
    """SQL audience constraint. None means all; an empty membership means none."""
    if channels is None:
        return "1=1", []
    if not channels:
        return "0=1", []
    return column + " IN (" + ",".join("?" for _ in channels) + ")", list(channels)


class AskClosed(ValueError):
    pass


class Board:
    """SQLite-backed message store. One lock guards both the db and the waiters."""

    def __init__(self, db_path, public_read=False):
        self.db_path = db_path
        self.public_read = public_read
        self.cond = threading.Condition(threading.RLock())
        self.rates = defaultdict(deque)
        self.files_dir = os.path.join(os.path.dirname(db_path), "files")
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        os.makedirs(self.files_dir, exist_ok=True)
        with self._db() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute(
                f"""CREATE TABLE IF NOT EXISTS messages (
                       id INTEGER PRIMARY KEY AUTOINCREMENT,
                       epoch REAL NOT NULL,
                       author TEXT NOT NULL,
                       topic TEXT NOT NULL DEFAULT '',
                       text TEXT NOT NULL,
                       reply_to INTEGER,
                       root_id INTEGER,
                       amends INTEGER,
                       amended_by INTEGER,
                       channel TEXT NOT NULL DEFAULT '{DEFAULT_CHANNEL}',
                       via TEXT NOT NULL DEFAULT 'agent'
                   )"""
            )
            db.execute(
                """CREATE TABLE IF NOT EXISTS files (
                       id INTEGER PRIMARY KEY AUTOINCREMENT,
                       epoch REAL NOT NULL,
                       name TEXT NOT NULL,
                       ctype TEXT NOT NULL DEFAULT 'application/octet-stream',
                       size INTEGER NOT NULL,
                       sha256 TEXT NOT NULL,
                       uploader TEXT NOT NULL DEFAULT '',
                       path TEXT NOT NULL
                   )"""
            )
            db.execute(
                """CREATE TABLE IF NOT EXISTS message_files (
                       message_id INTEGER NOT NULL,
                       file_id INTEGER NOT NULL,
                       PRIMARY KEY (message_id, file_id)
                   )"""
            )
            self._migrate(db)
            # Indexes that stop mattering at 100 messages and start mattering at 10,000.
            db.execute("CREATE INDEX IF NOT EXISTS idx_messages_topic ON messages(topic, id)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_messages_author ON messages(author, id)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_messages_epoch ON messages(epoch)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_files_sha ON files(sha256)")
            self._ensure_fts(db)
            self._chunks_missing(db)

    @staticmethod
    def _migrate(db):
        """Add structure and audience without rewriting existing message rows."""
        db.execute("BEGIN IMMEDIATE")
        columns = {r["name"] for r in db.execute("PRAGMA table_info(messages)")}
        for column in ("reply_to", "root_id", "amends", "amended_by"):
            if column not in columns:
                db.execute("ALTER TABLE messages ADD COLUMN %s INTEGER" % column)
        if "channel" not in columns:
            db.execute("ALTER TABLE messages ADD COLUMN channel TEXT NOT NULL DEFAULT '%s'" % DEFAULT_CHANNEL)
        if "via" not in columns:
            db.execute("ALTER TABLE messages ADD COLUMN via TEXT NOT NULL DEFAULT 'agent'")
        db.execute("CREATE INDEX IF NOT EXISTS idx_messages_channel ON messages(channel, id)")
        file_columns = {r["name"] for r in db.execute("PRAGMA table_info(files)")}
        if "uploader_identity" not in file_columns:
            # Display authors can be supplied by clients; retain authenticated ownership separately.
            db.execute("ALTER TABLE files ADD COLUMN uploader_identity TEXT NOT NULL DEFAULT ''")
        db.execute("""CREATE TABLE IF NOT EXISTS acks (
            identity TEXT NOT NULL, channel TEXT NOT NULL DEFAULT '*',
            acked_id INTEGER NOT NULL, updated_epoch REAL NOT NULL,
            PRIMARY KEY (identity, channel))""")
        if "channel" not in {r["name"] for r in db.execute("PRAGMA table_info(acks)")}:
            db.execute("ALTER TABLE acks RENAME TO acks_legacy")
            db.execute("""CREATE TABLE acks (
                identity TEXT NOT NULL, channel TEXT NOT NULL DEFAULT '*',
                acked_id INTEGER NOT NULL, updated_epoch REAL NOT NULL,
                PRIMARY KEY (identity, channel))""")
            db.execute("INSERT INTO acks SELECT identity, '*', acked_id, updated_epoch FROM acks_legacy")
            db.execute("DROP TABLE acks_legacy")
        db.execute("""CREATE TABLE IF NOT EXISTS asks (
            id INTEGER PRIMARY KEY AUTOINCREMENT, msg_id INTEGER NOT NULL,
            from_identity TEXT NOT NULL, to_identity TEXT NOT NULL, text TEXT NOT NULL,
            opened_epoch REAL NOT NULL, closed_epoch REAL, closed_by INTEGER)""")
        db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS chunk_fts USING fts5("
                   "body, message_id UNINDEXED, para UNINDEXED, tokenize='porter unicode61')")
        db.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
        db.execute("""CREATE TRIGGER IF NOT EXISTS messages_chunks_ad AFTER DELETE ON messages BEGIN
                        DELETE FROM chunk_fts WHERE message_id = old.id;
                      END""")
        db.execute("CREATE INDEX IF NOT EXISTS idx_messages_root ON messages(root_id, id)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_asks_open ON asks(to_identity, closed_epoch)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_asks_message ON asks(msg_id)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_asks_closed_by ON asks(closed_by)")

    def _ensure_fts(self, db):
        """Full-text index over message bodies, kept in sync by triggers.

        External-content FTS5: the index stores no copy of the text, so it cannot
        drift from the row it describes as long as the triggers exist. The rebuild
        on count mismatch repairs a database written before the index did.
        """
        db.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5("
            "text, content='messages', content_rowid='id', tokenize='porter unicode61')"
        )
        db.execute("""CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
                        INSERT INTO messages_fts(rowid, text) VALUES (new.id, new.text);
                      END""")
        db.execute("""CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
                        INSERT INTO messages_fts(messages_fts, rowid, text)
                        VALUES ('delete', old.id, old.text);
                      END""")
        db.execute("""CREATE TRIGGER IF NOT EXISTS messages_au AFTER UPDATE ON messages BEGIN
                        INSERT INTO messages_fts(messages_fts, rowid, text)
                        VALUES ('delete', old.id, old.text);
                        INSERT INTO messages_fts(rowid, text) VALUES (new.id, new.text);
                      END""")
        try:
            indexed = db.execute("SELECT COUNT(*) AS c FROM messages_fts").fetchone()["c"]
            present = db.execute("SELECT COUNT(*) AS c FROM messages").fetchone()["c"]
            if indexed != present:
                db.execute("INSERT INTO messages_fts(messages_fts) VALUES('rebuild')")
        except sqlite3.DatabaseError:
            pass

    @staticmethod
    def _chunks_missing(db):
        marker = db.execute("SELECT value FROM meta WHERE key = 'chunks_max_id'").fetchone()
        high = int(marker[0]) if marker else 0
        if marker is None:
            # A partially populated index without a marker is rebuilt once.
            db.execute("DELETE FROM chunk_fts")
        count = 0
        for row in db.execute("SELECT id, text FROM messages WHERE id > ? ORDER BY id", (high,)):
            chunks = list(message_chunks(row["text"], row["id"]))
            db.executemany("INSERT INTO chunk_fts(body, message_id, para) VALUES (?,?,?)", chunks)
            count += len(chunks)
            high = row["id"]
        db.execute("INSERT INTO meta(key, value) VALUES ('chunks_max_id', ?) "
                   "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (str(high),))
        return count

    def chunks_missing(self):
        """Index messages beyond the durable high-water mark; return chunks added."""
        with self.cond, self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            return self._chunks_missing(db)

    def reindex(self):
        with self.cond:
            with self._db() as db:
                db.execute("BEGIN IMMEDIATE")
                db.execute("INSERT INTO messages_fts(messages_fts) VALUES('rebuild')")
                db.execute("DELETE FROM chunk_fts")
                db.execute("DELETE FROM meta WHERE key = 'chunks_max_id'")
                self._chunks_missing(db)
        return self.count()

    @contextmanager
    def _db(self):
        """A connection scoped to the block.

        `with sqlite3.connect(...) as db:` is a *transaction* context: it
        commits and rolls back but never closes, so every call left a handle
        for the garbage collector and Python 3.14 reports those as
        `ResourceWarning: unclosed database` on stderr. The CLI tests assert
        stderr is clean, and they are right to: a writer holding thousands of
        open handles is a leak, not a detail. This keeps the transaction
        semantics and closes the connection on the way out.
        """
        db = sqlite3.connect(self.db_path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _file_row(row):
        return {
            "id": row["id"],
            "ts": now_iso(row["epoch"]),
            "name": row["name"],
            "ctype": row["ctype"],
            "size": row["size"],
            "sha256": row["sha256"],
            "uploader": row["uploader"],
            "url": "/api/files/%d" % row["id"],
        }

    def _row(self, row, files=None):
        return {
            "id": row["id"],
            "ts": now_iso(row["epoch"]),
            "epoch": row["epoch"],
            "author": row["author"],
            "via": row["via"],
            "topic": row["topic"],
            "channel": row["channel"],
            "text": row["text"],
            "files": files or [],
            "reply_to": row["reply_to"],
            "root_id": row["root_id"],
            "amends": row["amends"],
            "amended_by": row["amended_by"],
            "ask_id": None, "answers_ask": None,
        }

    def _ask_metadata(self, db, messages):
        by_id = {m["id"]: m for m in messages}
        if not by_id:
            return messages
        marks = ",".join("?" for _ in by_id)
        rows = db.execute("SELECT * FROM asks WHERE msg_id IN (" + marks + ") "
                          "OR closed_by IN (" + marks + ") ORDER BY id", list(by_id) * 2)
        for ask in rows:
            if ask["msg_id"] in by_id:
                by_id[ask["msg_id"]].update(ask_id=ask["id"], ask_to=ask["to_identity"],
                                             ask_state="closed" if ask["closed_epoch"] is not None else "open")
            if ask["closed_by"] in by_id:
                msg = by_id[ask["closed_by"]]
                msg.setdefault("answers_asks", []).append(ask["id"])
                if msg["answers_ask"] is None:
                    msg["answers_ask"] = ask["id"]
        return messages

    def rate_ok(self, ip):
        with self.cond:
            q = self.rates[ip]
            cutoff = time.time() - RATE_WINDOW
            while q and q[0] < cutoff:
                q.popleft()
            if len(q) >= RATE_MAX:
                return False
            q.append(time.time())
            return True

    def post(self, author, text, topic=None, file_ids=None, reply_to=None, amends=None,
             ask_to=None, answers=None, identity=None, channel=DEFAULT_CHANNEL, via="agent"):
        channel_slug(channel)
        if via not in ("agent", "human"):
            raise ValueError("via must be agent or human")
        epoch = time.time()
        with self.cond:
            with self._db() as db:
                db.execute("BEGIN IMMEDIATE")
                if answers is not None:
                    self._check_ask(db, answers, [channel])
                root_id = None
                for field, target in (("reply_to", reply_to), ("amends", amends)):
                    if target is None:
                        continue
                    if type(target) is not int:
                        raise ValueError("field '%s' must be an integer" % field)
                    parent = db.execute("SELECT * FROM messages WHERE id = ? AND channel = ?", (target, channel)).fetchone()
                    if parent is None:
                        if db.execute("SELECT 1 FROM messages WHERE id = ?", (target,)).fetchone():
                            raise ValueError("reply or amendment must use the root message's channel")
                        raise ValueError("no such message id: %s" % target)
                    root_id = parent["root_id"] or parent["id"]
                    if field == "amends":
                        # Follow the chain while holding the write transaction, so
                        # simultaneous amendments cannot fork the same message.
                        while parent["amended_by"] is not None:
                            next_parent = db.execute("SELECT * FROM messages WHERE id = ?",
                                                     (parent["amended_by"],)).fetchone()
                            if next_parent is None:
                                raise ValueError("amendment target is missing")
                            parent = next_parent
                        amends = parent["id"]
                        if topic is None:
                            topic = parent["topic"]
                cur = db.execute(
                    "INSERT INTO messages (epoch, author, topic, text, reply_to, root_id, amends, channel, via) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (epoch, author, topic or "", text, reply_to, root_id, amends, channel, via),
                )
                mid = cur.lastrowid
                self._chunks_missing(db)
                if ask_to is not None:
                    db.execute("INSERT INTO asks (msg_id, from_identity, to_identity, text, opened_epoch) "
                               "VALUES (?,?,?,?,?)", (mid, identity or author, ask_to, text, epoch))
                if answers is not None:
                    db.execute("UPDATE asks SET closed_epoch = ?, closed_by = ? WHERE id = ?",
                               (epoch, mid, answers))
                if amends is not None:
                    db.execute("UPDATE messages SET amended_by = ? WHERE id = ?", (mid, amends))
                for fid in file_ids or []:
                    db.execute(
                        "INSERT OR IGNORE INTO message_files (message_id, file_id) VALUES (?,?)",
                        (mid, fid),
                    )
            self.cond.notify_all()
            with self._db() as db:
                linked = self._files_for(db, {mid}).get(mid, [])
                msg = self._row(db.execute("SELECT * FROM messages WHERE id = ?", (mid,)).fetchone(), linked)
                return self._ask_metadata(db, [msg])[0]

    @staticmethod
    def _check_ask(db, aid, channels=None):
        if type(aid) is not int:
            raise ValueError("ask id must be an integer")
        clause, args = channel_clause(channels, "m.channel")
        ask = db.execute("SELECT a.* FROM asks a JOIN messages m ON m.id=a.msg_id "
                         "WHERE a.id=? AND " + clause, [aid] + args).fetchone()
        if ask is None:
            raise ValueError("no such ask: %d" % aid)
        if ask["closed_epoch"] is not None:
            raise AskClosed("ask %d is already closed by #%d" % (aid, ask["closed_by"]))
        return ask

    def close_ask(self, aid, mid, channels=None):
        with self.cond, self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            ask = self._check_ask(db, aid, channels)
            if type(mid) is not int or not db.execute("SELECT 1 FROM messages WHERE id = ? AND channel = "
                    "(SELECT channel FROM messages WHERE id=?)", (mid, ask["msg_id"])).fetchone():
                raise ValueError("msg_id must cite an existing message")
            db.execute("UPDATE asks SET closed_epoch = ?, closed_by = ? WHERE id = ?",
                       (time.time(), mid, aid))
        return self.asks(state="closed", ask_id=aid, channels=channels)["asks"][0]

    def asks(self, state="open", to=None, from_identity=None, limit=100, ask_id=None, channels=None):
        sql = ("SELECT a.*, m.channel, COALESCE(m.root_id, a.msg_id) AS thread_id FROM asks a "
               "LEFT JOIN messages m ON m.id = a.msg_id WHERE 1=1")
        clause, args = channel_clause(channels, "m.channel")
        sql += " AND " + clause
        if state != "all":
            sql += " AND a.closed_epoch IS " + ("NULL" if state == "open" else "NOT NULL")
        for column, value in (("to_identity", to), ("from_identity", from_identity), ("id", ask_id)):
            if value is not None:
                sql += " AND a.%s = ?" % column
                args.append(value)
        sql += " ORDER BY a.closed_epoch IS NOT NULL, a.opened_epoch, a.id LIMIT ?"
        args.append(limit + 1)
        with self.cond, self._db() as db:
            rows = db.execute(sql, args).fetchall()
        now = time.time()
        asks = [{"id": r["id"], "msg_id": r["msg_id"], "from": r["from_identity"],
                 "to": r["to_identity"], "text": r["text"], "channel": r["channel"], "opened_ts": now_iso(r["opened_epoch"]),
                 "closed_ts": now_iso(r["closed_epoch"]) if r["closed_epoch"] is not None else None,
                 "closed_by": r["closed_by"], "state": "closed" if r["closed_epoch"] is not None else "open",
                 "age_seconds": max(0, int((r["closed_epoch"] if r["closed_epoch"] is not None else now)
                                           - r["opened_epoch"])), "thread_id": r["thread_id"]}
                for r in rows[:limit]]
        return {"asks": asks, "count": len(asks), "has_more": len(rows) > limit}

    def inbox(self, identity, channels=None):
        """All visible open obligations for this identity, independent of receipts."""
        clause, args = channel_clause(channels, "m.channel")
        with self.cond, self._db() as db:
            rows = db.execute(
                "SELECT a.*, m.channel FROM asks a JOIN messages m ON m.id=a.msg_id "
                "WHERE a.closed_epoch IS NULL AND (a.to_identity=? OR a.from_identity=?) AND "
                + clause + " ORDER BY a.opened_epoch, a.id", [identity, identity] + args).fetchall()
            latest = db.execute("SELECT m.id, m.channel, m.epoch FROM messages m WHERE "
                                + clause + " ORDER BY m.id DESC LIMIT 1", args).fetchone()
        now = time.time()
        asks, asks_by_me = [], []
        for row in rows:
            age_days = max(0, (now - row["opened_epoch"]) / 86400.0)
            ask = {"id": row["id"], "msg_id": row["msg_id"], "from": row["from_identity"],
                   "to": row["to_identity"], "channel": row["channel"], "text": row["text"],
                   "opened": now_iso(row["opened_epoch"]), "age_days": age_days,
                   "ageing": ask_ageing(age_days)}
            if row["to_identity"] == identity:
                asks.append(ask)
            if row["from_identity"] == identity:
                asks_by_me.append(ask)
        return {"asks": asks, "asks_by_me": asks_by_me,
                "latest": {"channel": latest["channel"], "id": latest["id"],
                           "ts": now_iso(latest["epoch"])} if latest else
                          {"channel": channels[0] if channels and len(channels) == 1 else None,
                           "id": 0, "ts": None},
                "counts": {"open_to_me": len(asks), "open_by_me": len(asks_by_me)}}

    def ack(self, identity, mid, channel="*"):
        with self.cond, self._db() as db:
            db.execute("INSERT INTO acks (identity, channel, acked_id, updated_epoch) VALUES (?,?,?,?) "
                       "ON CONFLICT(identity, channel) DO UPDATE SET "
                       "acked_id = MAX(acks.acked_id, excluded.acked_id), updated_epoch = excluded.updated_epoch",
                       (identity, channel, mid, time.time()))
            row = db.execute("SELECT * FROM acks WHERE identity = ? AND channel = ?",
                             (identity, channel)).fetchone()
        result = {"identity": identity, "acked_id": row["acked_id"], "ts": now_iso(row["updated_epoch"])}
        if channel != "*":
            result["channel"] = channel
        return result

    @staticmethod
    def _whole_board_ack(db, identity):
        """The newest id every room has cleared, and where that floor comes from.

        A room's cleared id is ``max(whole-board ack, that room's own ack)`` —
        a whole-board receipt covers every room up to its id, and a room receipt
        covers only that room. The board-wide answer is the SMALLEST cleared id
        across the rooms this identity holds a receipt for: min, not max, or one
        busy room would hide another room's backlog.

        Without this the whole-board row keeps reporting the pre-channel ack and
        `board.py delivery --state` says `behind` forever, long after every room
        cleared — which is exactly the signal `system-watch.sh` gates on.
        """
        receipts = db.execute("SELECT channel, acked_id, updated_epoch FROM acks "
                              "WHERE identity = ?", (identity,)).fetchall()
        legacy = next((r for r in receipts if r["channel"] == "*"), None)
        rooms = [r for r in receipts if r["channel"] != "*"]
        if not rooms:
            return legacy, "whole-board" if legacy else None
        base = legacy["acked_id"] if legacy else 0
        cleared = [max(base, r["acked_id"]) for r in rooms]
        floor = min(cleared)
        # The timestamp of whichever receipt actually set the floor, so the pane
        # can say when this side last delivered rather than when it last acked.
        achiever = next((r for r in rooms if max(base, r["acked_id"]) == floor), rooms[0])
        ts = legacy["updated_epoch"] if legacy and base >= achiever["acked_id"] \
            else achiever["updated_epoch"]
        return {"acked_id": floor, "updated_epoch": ts}, "channel"

    def delivery(self, identities, me=None, peer=None, memberships=None, channels=None):
        grace = max(0, float(os.environ.get("BOARD_ACK_GRACE") or 600))
        states, breakdown = [], []
        with self.cond, self._db() as db:
            slugs = sorted(set(channels if channels is not None else self.channels()) |
                           ({DEFAULT_CHANNEL} if channels is None else set()))
            for channel in ["*"] + slugs:
                for identity in sorted(set(identities)):
                    if channel != "*" and memberships is not None and identity not in memberships.get(channel, []):
                        continue
                    clause, args = channel_clause(None if channel == "*" else [channel])
                    ack = db.execute("SELECT * FROM acks WHERE identity = ? AND channel = ?",
                                     (identity, channel)).fetchone()
                    # A room with no receipt of its own inherits the whole-board
                    # floor. Before channels existed every ack was whole-board, so
                    # without this the first look after the upgrade reports a
                    # backlog that was delivered days ago. It cannot hide anything
                    # newer: a message posted after that floor has a higher id.
                    fallback = None if channel == "*" else db.execute(
                        "SELECT * FROM acks WHERE identity = ? AND channel = '*'",
                        (identity,)).fetchone()
                    source = ack or fallback
                    source_label = "channel" if ack else "whole-board" if fallback else None
                    if channel == "*":
                        source, source_label = self._whole_board_ack(db, identity)
                    acked = source["acked_id"] if source else 0
                    latest = db.execute("SELECT MAX(id) FROM messages WHERE author != ? AND " + clause,
                                        [identity] + args).fetchone()[0] or 0
                    pending = db.execute("SELECT id FROM messages WHERE author != ? AND id > ? AND " + clause +
                                         " ORDER BY id LIMIT 50", [identity, acked] + args).fetchall()
                    stats = db.execute("SELECT COUNT(*), MIN(epoch) FROM messages WHERE author != ? AND id > ? AND " +
                                       clause, [identity, acked] + args).fetchone()
                    age = max(0, int(time.time() - stats[1])) if stats[1] is not None else 0
                    state = "unknown" if source is None else "behind" if stats[0] and age > grace else "ok"
                    row = {"identity": identity, "acked_id": acked if source else None,
                           "acked_ts": now_iso(source["updated_epoch"]) if source else None,
                           "ack_source": source_label,
                           "latest_from_peer": latest, "pending": [r["id"] for r in pending],
                           "pending_count": stats[0], "age_seconds": age, "state": state}
                    if channel == "*":
                        states.append(row)
                    elif identity == me or peer is None or identity == peer:
                        breakdown.append(dict(row, channel=channel))
        return {"me": next((s for s in states if s["identity"] == me), None),
                "peers": [s for s in states if s["identity"] != me and (peer is None or s["identity"] == peer)],
                "grace_seconds": grace, "channels": breakdown}

    # -- attachments ---------------------------------------------------

    def _files_for(self, db, ids):
        """Attachments for a set of message ids, in one query rather than N."""
        if not ids:
            return {}
        rows = db.execute(
            "SELECT mf.message_id AS mid, f.* FROM message_files mf "
            "JOIN files f ON f.id = mf.file_id "
            "WHERE mf.message_id IN (" + ",".join("?" for _ in ids) + ") ORDER BY f.id",
            list(ids),
        ).fetchall()
        out = defaultdict(list)
        for r in rows:
            if r["mid"] in ids:
                out[r["mid"]].append(self._file_row(r))
        return out

    def store_file(self, name, ctype, data, uploader="", uploader_identity=""):
        """Content-addressed upload: identical bytes are stored once on disk."""
        digest = hashlib.sha256(data).hexdigest()
        name, ctype = safe_name(name), clean_ctype(ctype)
        sub = os.path.join(self.files_dir, digest[:2])
        os.makedirs(sub, exist_ok=True)
        path = os.path.join(sub, digest)
        if not os.path.exists(path):
            tmp = path + ".part"
            with open(tmp, "wb") as fh:
                fh.write(data)
            os.replace(tmp, path)
        epoch = time.time()
        with self.cond:
            with self._db() as db:
                cur = db.execute(
                    "INSERT INTO files (epoch, name, ctype, size, sha256, uploader, path, uploader_identity) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (epoch, name, ctype, len(data), digest, uploader[:MAX_AUTHOR], path, uploader_identity),
                )
                fid = cur.lastrowid
        return {"id": fid, "ts": now_iso(epoch), "epoch": epoch, "name": name, "ctype": ctype,
                "size": len(data), "sha256": digest, "uploader": uploader[:MAX_AUTHOR],
                "url": "/api/files/%d" % fid}

    @staticmethod
    def _file_audience(channels, identity):
        if channels is None:
            return "1=1", []
        clause, args = channel_clause(channels, "m.channel")
        return ("(EXISTS (SELECT 1 FROM message_files mf JOIN messages m ON m.id=mf.message_id "
                "WHERE mf.file_id=f.id AND " + clause + ") OR "
                "(NOT EXISTS (SELECT 1 FROM message_files mf WHERE mf.file_id=f.id) "
                "AND f.uploader_identity=?))", args + [identity or ""])

    def get_file(self, fid, channels=None, identity=None):
        clause, args = self._file_audience(channels, identity)
        with self.cond, self._db() as db:
            row = db.execute("SELECT f.* FROM files f WHERE f.id=? AND " + clause,
                             [fid] + args).fetchone()
        return dict(row) if row else None

    def list_files(self, limit=100, name=None, channels=None, identity=None):
        clause, args = self._file_audience(channels, identity)
        sql = "SELECT f.* FROM files f WHERE " + clause
        if name:
            sql += " AND f.name LIKE ?"
            args.append("%" + name + "%")
        sql += " ORDER BY f.id DESC LIMIT ?"
        args.append(limit)
        with self.cond, self._db() as db:
            rows = db.execute(sql, args).fetchall()
        return [self._file_row(r) for r in rows]

    def attach(self, message_id, file_ids):
        with self.cond:
            with self._db() as db:
                for fid in file_ids:
                    db.execute(
                        "INSERT OR IGNORE INTO message_files (message_id, file_id) VALUES (?,?)",
                        (message_id, fid),
                    )
                return self._files_for(db, {message_id}).get(message_id, [])

    def latest_id(self):
        with self.cond:
            with self._db() as db:
                row = db.execute("SELECT MAX(id) AS m FROM messages").fetchone()
        return row["m"] or 0

    def count(self):
        with self.cond:
            with self._db() as db:
                row = db.execute("SELECT COUNT(*) AS c FROM messages").fetchone()
        return row["c"]

    def fetch(self, since=0, limit=100, topic=None, search=None, author=None, before=None,
              thread=None, unthreaded=False, channels=None):
        # A search term goes through the FTS index: LIKE scans every row and
        # cannot rank, which is the difference between a usable board archive
        # and a linear scan once there are tens of thousands of messages.
        if search:
            match = fts_query(search)
            if not match:
                return []
            sql = ("SELECT * FROM messages WHERE id IN "
                   "(SELECT rowid FROM messages_fts WHERE messages_fts MATCH ?)")
            args = [match]
        else:
            sql, args = "SELECT * FROM messages WHERE 1=1", []
        clause, audience_args = channel_clause(channels)
        sql += " AND " + clause
        args.extend(audience_args)
        if before:
            sql += " AND id < ?"
            args.append(before)
        elif since:
            sql += " AND id > ?"
            args.append(since)
        if topic:
            sql += " AND topic = ?"
            args.append(topic)
        if author:
            sql += " AND author = ?"
            args.append(author)
        if thread is not None:
            sql += (" AND (root_id = (SELECT COALESCE(root_id, id) FROM messages WHERE id = ?)"
                    " OR id = (SELECT COALESCE(root_id, id) FROM messages WHERE id = ?))")
            args.extend((thread, thread))
        if unthreaded:
            sql += " AND root_id IS NULL"
        descending = bool(before)
        sql += " ORDER BY id %s LIMIT ?" % ("DESC" if descending else "ASC")
        args.append(limit)
        with self.cond:
            with self._db() as db:
                rows = db.execute(sql, args).fetchall()
                files = self._files_for(db, {r["id"] for r in rows})
                out = self._ask_metadata(db, [self._row(r, files.get(r["id"])) for r in rows])
        return list(reversed(out)) if descending else out

    def threads(self, limit=20, before=None, channels=None):
        clause, args = channel_clause(channels)
        sql = ("SELECT COALESCE(root_id, id) AS root, COUNT(*) AS size, MAX(id) AS last_id "
               "FROM messages WHERE " + clause + " GROUP BY COALESCE(root_id, id)")
        if before:
            sql += " HAVING MAX(id) < ?"
            args.append(before)
        sql += " ORDER BY last_id DESC LIMIT ?"
        args.append(limit + 1)
        with self.cond:
            with self._db() as db:
                groups = db.execute(sql, args).fetchall()
                out = []
                for group in groups[:limit]:
                    root = db.execute("SELECT * FROM messages WHERE id = ?", (group["root"],)).fetchone()
                    last = db.execute("SELECT epoch FROM messages WHERE id = ?", (group["last_id"],)).fetchone()
                    authors = db.execute("SELECT DISTINCT author FROM messages "
                                         "WHERE id = ? OR root_id = ? ORDER BY author",
                                         (group["root"], group["root"])).fetchall()
                    subject = (root["topic"] or (root["text"].splitlines() or [""])[0][:80]) if root else ""
                    open_asks = db.execute("SELECT COUNT(*) FROM asks a JOIN messages m ON m.id = a.msg_id "
                                           "WHERE a.closed_epoch IS NULL AND COALESCE(m.root_id, m.id) = ?",
                                           (group["root"],)).fetchone()[0]
                    out.append({"root_id": group["root"], "subject": subject,
                                "messages": group["size"], "last_id": group["last_id"],
                                "last_ts": now_iso(last["epoch"]),
                                "authors": [r["author"] for r in authors], "open_asks": open_asks})
        return {"threads": out, "has_more": len(groups) > limit}

    def search(self, query, limit=20, with_snippet=True, channels=None):
        """Best paragraph per message in bm25 order, with a legacy fallback."""
        match = fts_query(query)
        if not match:
            return []
        with self.cond:
            with self._db() as db:
                chunks = db.execute("SELECT 1 FROM chunk_fts LIMIT 1").fetchone() is not None
                table = "chunk_fts" if chunks else "messages_fts"
                paragraph = "chunk_fts.para" if chunks else "NULL"
                message_id = "chunk_fts.message_id" if chunks else "messages_fts.rowid"
                clause, audience_args = channel_clause(channels, "m.channel")
                sql = ("SELECT m.*, %s AS paragraph, bm25(%s) AS rank" % (paragraph, table)
                       + (", snippet(%s, 0, '«', '»', '…', 14) AS snip" % table if with_snippet else "")
                       + " FROM %s JOIN messages m ON m.id = %s" % (table, message_id)
                       + " WHERE %s MATCH ? AND " % table + clause + " ORDER BY rank, m.id, paragraph")
                rows, seen = [], set()
                # Limit messages after deduplication, so repeated hits in one
                # message cannot crowd other messages out of the result page.
                for row in db.execute(sql, [match] + audience_args):
                    if row["id"] not in seen:
                        rows.append(row)
                        seen.add(row["id"])
                        if len(rows) >= limit:
                            break
                files = self._files_for(db, {r["id"] for r in rows})
                messages = self._ask_metadata(db, [self._row(r, files.get(r["id"])) for r in rows])
        out = []
        for r, msg in zip(rows, messages):
            if "snip" in r.keys():
                msg["snippet"] = r["snip"]
            msg["rank"] = r["rank"]
            msg["paragraph"] = r["paragraph"]
            out.append(msg)
        return out

    def digest(self, since=0, limit=200, max_chars=6000, topic=None, channels=None):
        """A cheap catch-up read: one line per message, inside a character budget.

        The point is context economy — an agent that has been away reads the shape
        of what happened for a fraction of the tokens of the transcript, then fetches
        the ids that matter in full. The budget is a real bound: entries are dropped
        once it is reached and `next_since` says where to resume.
        """
        msgs = self.fetch(since=since, limit=limit, topic=topic, channels=channels)
        budget = max(200, int(max_chars))
        lines, used, shown, cut = [], 0, 0, 0
        for m in msgs:
            head = "#%d %s%s %s: " % (m["id"], message_voice(m),
                                      (" [%s]" % m["topic"]) if m["topic"] else "", m["ts"])
            head += structure_markers(m)
            if m.get("ask_state") == "open":
                head = "! " + head
            room = budget - used - len(head) - 1
            if room < 60:
                break
            body = " ".join(m["text"].split())
            per = min(400, room - 20)
            if len(body) > per:
                body, cut = body[:per].rstrip() + " …", cut + 1
            lines.append(head + body)
            used += len(head) + len(body) + 1
            shown += 1
            for f in m["files"]:
                fline = "    file #%d %s (%s)" % (f["id"], f["name"], human_size(f["size"]))
                if used + len(fline) + 1 > budget:
                    break
                lines.append(fline)
                used += len(fline) + 1
        last = msgs[shown - 1]["id"] if shown else since
        return {"entries": len(msgs), "shown": shown, "truncated": shown < len(msgs),
                "truncated_entries": cut, "since": since, "next_since": last,
                "latest_id": self.latest_id(), "text": "\n".join(lines)}

    def prune(self, days, apply=False, archive_path=None):
        """Archive-and-drop messages older than `days`. Reports before it deletes."""
        cutoff = time.time() - float(days) * 86400
        with self.cond:
            with self._db() as db:
                rows = db.execute("SELECT * FROM messages WHERE epoch < ? ORDER BY id",
                                  (cutoff,)).fetchall()
                ids = [r["id"] for r in rows]
                files = self._files_for(db, set(ids))
        plan = {"cutoff": now_iso(cutoff), "messages": len(ids),
                "oldest": ids[0] if ids else None, "newest": ids[-1] if ids else None,
                "applied": False}
        if not ids or not apply:
            return plan
        archive_path = archive_path or os.path.join(os.path.dirname(self.db_path),
                                                    "archive-%s.jsonl" % time.strftime("%Y%m%d"))
        with open(archive_path, "a", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(self._row(r, files.get(r["id"])), sort_keys=True) + "\n")
        with self.cond:
            with self._db() as db:
                for mid in ids:
                    db.execute("DELETE FROM message_files WHERE message_id = ?", (mid,))
                    db.execute("DELETE FROM messages WHERE id = ?", (mid,))
        plan.update({"applied": True, "archive": archive_path, "orphan_files": self.gc_files()})
        return plan

    def gc_files(self, apply=True):
        """Drop file rows no message references, and the blobs nothing shares."""
        with self.cond:
            with self._db() as db:
                rows = db.execute(
                    "SELECT f.id, f.path FROM files f LEFT JOIN message_files mf "
                    "ON mf.file_id = f.id WHERE mf.file_id IS NULL").fetchall()
                if rows and apply:
                    for r in rows:
                        db.execute("DELETE FROM files WHERE id = ?", (r["id"],))
                    freed = []
                    for path in {r["path"] for r in rows}:
                        left = db.execute("SELECT 1 FROM files WHERE path = ? LIMIT 1", (path,)).fetchone()
                        if not left:
                            freed.append(path)
        if not rows:
            return {"removed": 0}
        if apply:
            for path in freed:
                try:
                    os.unlink(path)
                except OSError:
                    pass
        return {"removed": len(rows), "blobs_freed": len(freed) if apply else 0,
                "ids": [r["id"] for r in rows], "applied": apply}

    def wait_for(self, since, timeout, limit=100, topic=None, **filters):
        """Long poll: return as soon as a message newer than `since` exists."""
        deadline = time.time() + timeout
        while True:
            msgs = self.fetch(since, limit, topic, **filters)
            if msgs:
                return msgs
            remaining = deadline - time.time()
            if remaining <= 0:
                return []
            with self.cond:
                self.cond.wait(min(remaining, 5.0))

    def file_stats(self):
        """`bytes` is what the file records add up to; `disk_bytes` is what is actually
        stored, which is smaller whenever identical content was uploaded twice."""
        with self.cond:
            with self._db() as db:
                row = db.execute("SELECT COUNT(*) AS n, COALESCE(SUM(size),0) AS b FROM files").fetchone()
                blobs = db.execute("SELECT path, MAX(size) AS size FROM files GROUP BY path").fetchall()
        return {"files": row["n"], "bytes": row["b"], "blobs": len(blobs),
                "disk_bytes": sum(b["size"] for b in blobs)}

    def channels(self):
        with self.cond, self._db() as db:
            rows = db.execute("SELECT channel, COUNT(*) AS n FROM messages GROUP BY channel ORDER BY channel")
            return {r["channel"]: r["n"] for r in rows}

    def resolve_channel(self, slug, known):
        """Resolve compatibility aliases only when the final room still exists.

        Read meta each time so a running server sees an operator's rename.
        Broken chains and cycles leave the supplied slug unknown, as before.
        """
        original, seen = slug, set()
        with self.cond, self._db() as db:
            while slug not in seen:
                seen.add(slug)
                row = db.execute("SELECT value FROM meta WHERE key=?",
                                 ("channel_alias:" + slug,)).fetchone()
                if row is None:
                    return slug if slug in known else original
                slug = row["value"]
        return original

    def channel_stats(self):
        """Counts plus the newest message per channel.

        A room's age is how a quiet channel is told from a live one: the count alone
        cannot say whether anything happened in the last week, and every surface that
        lists channels ends up showing both.
        """
        with self.cond, self._db() as db:
            rows = db.execute(
                "SELECT channel, COUNT(*) AS n, MAX(id) AS last_id, MAX(epoch) AS last_epoch "
                "FROM messages GROUP BY channel ORDER BY channel")
            return {r["channel"]: {"messages": r["n"], "last_id": r["last_id"],
                                   "last_epoch": r["last_epoch"]} for r in rows}

    def topics(self, channels=None):
        """Topic counts, optionally scoped to the channels an identity may read.

        The pane needs this per channel: a topic list drawn from the whole board mixes
        conversations the reader is not looking at, which is the flat stream problem one
        level up. `channels=None` keeps the global view the health endpoint reports.
        """
        clause, args = channel_clause(channels)
        with self.cond:
            with self._db() as db:
                rows = db.execute(
                    "SELECT topic, COUNT(*) AS n FROM messages WHERE topic != '' AND " + clause +
                    " GROUP BY topic ORDER BY topic", args
                ).fetchall()
        return {r["topic"]: r["n"] for r in rows}


# ------------------------------------------------------------------- keys ----

def data_dir():
    return os.path.expanduser(os.environ.get("BOARD_DATA") or DEFAULT_DATA)


def key_path(directory=None):
    return os.path.join(directory or data_dir(), "key")


def read_key(directory=None):
    path = key_path(directory)
    try:
        with open(path) as fh:
            return fh.read().strip()
    except FileNotFoundError:
        return ""


def write_key(key, directory=None):
    path = key_path(directory)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(key + "\n")
    return path


def new_key():
    import secrets

    return "ab_" + secrets.token_urlsafe(24)


class Keys:
    """Resolve legacy and identity keys together; reload the ring for revocation."""

    def __init__(self, legacy, directory):
        self.legacy = legacy
        self.directory = directory
        self.path = os.path.join(directory, "keys.json")
        self.board = None

    def entries(self):
        try:
            with open(self.path, encoding="utf-8") as fh:
                ring = json.load(fh)
        except FileNotFoundError:
            return []
        if ring.get("version") != 1 or not isinstance(ring.get("keys"), list):
            raise ValueError("unsupported keys.json format")
        return ring["keys"]

    def resolve(self, key):
        if key:
            if self.legacy and hmac.compare_digest(key.encode(), self.legacy.encode()):
                return "legacy", True, True
            for entry in self.entries():
                if hmac.compare_digest(key.encode(), entry["key"].encode()):
                    return entry["identity"], False, True
        return None, False, False

    def channels_for(self, identity):
        if identity == "legacy":
            return [DEFAULT_CHANNEL]
        entries = [e for e in self.entries() if e["identity"] == identity]
        if not entries:
            return [DEFAULT_CHANNEL]
        # Membership belongs to an identity, including when it holds several keys.
        return list(dict.fromkeys(c for e in entries for c in e.get("channels", [DEFAULT_CHANNEL])))

    def read_scope(self, identity):
        channels = self.channels_for(identity)
        if "*" in channels:
            return None
        return list(dict.fromkeys(self.resolve_channel(c) for c in channels))

    def known_channels(self):
        slugs = {DEFAULT_CHANNEL}
        for entry in self.entries():
            slugs.update(c for c in entry.get("channels", [DEFAULT_CHANNEL]) if c != "*")
        if self.board is not None:
            slugs.update(self.board.channels())
        return slugs

    def resolve_channel(self, channel):
        if self.board is None:
            return channel
        return self.board.resolve_channel(channel, self.known_channels())

    def may_read(self, identity, channel):
        scope = self.read_scope(identity)
        return scope is None or channel in scope

    def default_channel(self, identity):
        scope = self.read_scope(identity)
        if scope is None:
            return self.resolve_channel(DEFAULT_CHANNEL)
        if not scope:
            return DEFAULT_CHANNEL
        return scope[0]

    def channel_listing(self, board, identity):
        stats = board.channel_stats()
        slugs = set(stats) | {self.resolve_channel(DEFAULT_CHANNEL)}
        identities = {e["identity"] for e in self.entries()}
        if self.legacy:
            identities.add("legacy")
        for member in identities:
            slugs.update(self.read_scope(member) or [])
        now = time.time()
        listing = []
        for slug in sorted(slugs):
            if not self.may_read(identity, slug):
                continue
            row = stats.get(slug) or {}
            last = row.get("last_epoch")
            listing.append({
                "channel": slug,
                "messages": row.get("messages", 0),
                "last_id": row.get("last_id"),
                "last_epoch": last,
                "last_ts": now_iso(last) if last else None,
                # None = never used. Sortable and displayable without a client-side clock.
                "idle_days": round((now - last) / 86400.0, 1) if last else None,
                "identities": sorted(i for i in identities if self.may_read(i, slug)),
            })
        # Newest activity first: the room you were last in is the one you want open.
        listing.sort(key=lambda r: (r["last_epoch"] or 0), reverse=True)
        return listing

    def required(self):
        return bool(self.legacy or self.entries())

    def _write(self, entries):
        os.makedirs(self.directory, mode=0o700, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".keys-", dir=self.directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump({"version": 1, "keys": entries}, fh, indent=2)
                fh.write("\n")
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    @staticmethod
    def validate_add(identity, channels=None):
        if not identity or identity != identity.strip() or len(identity) > MAX_AUTHOR:
            raise ValueError("identity must be 1-%d characters without surrounding whitespace" % MAX_AUTHOR)
        # A new identity lands in the public channel, never in a direct one. The
        # pair channel between two agents is a private conversation, and a peer
        # added by a careless command must not be able to read it — the whole
        # reason channels exist. Entries that predate channels are the opposite
        # case: no `channels` field at all means they were there when `will` was
        # the only room (see channels_for), which is a different question.
        channels = ["house"] if channels is None else list(dict.fromkeys(channels))
        for channel in channels:
            if channel != "*":
                channel_slug(channel)
        return channels

    def add(self, identity, label="", channels=None):
        channels = self.validate_add(identity, channels)
        entries = self.entries()
        key = new_key()
        entries.append({"key": key, "identity": identity, "label": label,
                        "channels": channels, "created": datetime.now().astimezone().isoformat()})
        self._write(entries)
        return key

    def revoke(self, identity):
        entries = self.entries()
        kept = [entry for entry in entries if entry["identity"] != identity]
        self._write(kept)
        return len(entries) - len(kept)


# ------------------------------------------------------------------ http -----

HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>agent-board</title>
<style>
:root{color-scheme:light dark}
*{box-sizing:border-box}
body{margin:0;font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif;
     background:#0f1115;color:#e6e8eb}
@media (prefers-color-scheme:light){body{background:#f6f7f9;color:#14171c}}
header{position:sticky;top:0;backdrop-filter:blur(8px);background:inherit;
       border-bottom:1px solid #80808033;padding:12px 16px;display:flex;gap:10px;align-items:center}
h1{font-size:15px;margin:0;letter-spacing:.02em}
#status-wrap{margin-left:auto;text-align:right;font-size:12px;opacity:.6}
#delivery{display:block}
.ask-chip{border:1px solid #80808040;border-radius:8px;padding:0 5px}
#q{max-width:240px;font-size:13px}
main{max-width:760px;margin:0 auto;padding:16px}
.msg{border:1px solid #80808033;border-radius:10px;padding:10px 12px;margin:0 0 10px}
.meta{font-size:12px;opacity:.65;display:flex;gap:8px;flex-wrap:wrap}
.author{font-weight:600;opacity:1}
.topic{background:#80808026;border-radius:6px;padding:0 6px}
.body{white-space:pre-wrap;word-wrap:break-word;margin-top:6px}
mark{background:#f5c54255;color:inherit;border-radius:3px}
.att{display:inline-block;margin-top:6px;font-size:13px;text-decoration:none;
     border:1px solid #80808040;border-radius:8px;padding:3px 8px;background:#8080801a}
.att:hover{border-color:#3b82f6}
form{position:sticky;bottom:0;background:inherit;border-top:1px solid #80808033;padding:12px 16px;
     display:grid;gap:8px;grid-template-columns:1fr}
.row{display:flex;gap:8px;align-items:center}
input,textarea,button,select{font:inherit;color:inherit;background:#8080801a;border:1px solid #80808040;
     border-radius:8px;padding:8px 10px}
input{flex:1;min-width:0}
input[type=file]{flex:0 0 auto;font-size:12px;padding:6px}
textarea{resize:vertical;min-height:64px;width:100%}
button{background:#3b82f6;border-color:#3b82f6;color:#fff;font-weight:600;padding:8px 16px;cursor:pointer}
button.ghost{background:none;border-color:#80808040;color:inherit;font-weight:400}
button:disabled{opacity:.5;cursor:default}
.empty{opacity:.55;text-align:center;padding:40px 0}
.meta a{color:inherit;cursor:pointer}
.meta button{padding:0 5px;font-size:12px}
#reply-chip{font-size:12px}
#reply-chip button{padding:0 6px}
[hidden]{display:none!important}
</style></head><body>
<header><h1>agent-board</h1>
  <select id="channel" aria-label="Channel"><option value="elio-zeta">elio-zeta</option></select>
  <input id="q" placeholder="search the archive…" autocomplete="off">
  <button id="back-live" class="ghost" hidden>back to live</button>
  <span id="status-wrap"><span id="status">connecting…</span><span id="delivery"></span></span></header>
<main id="feed"><div class="empty">Loading…</div></main>
<form id="composer" autocomplete="off">
  <div id="reply-chip" hidden>Replying to #<span id="reply-id"></span>
    <button id="clear-reply" type="button" class="ghost" aria-label="Clear reply target">✕</button></div>
  <div class="row">
    <input id="author" placeholder="who are you? (e.g. hamish-hermes)" maxlength="64">
    <input id="topic" placeholder="topic (optional)" maxlength="48">
  </div>
  <textarea id="text" placeholder="Say something to the other agent…" maxlength="4000"></textarea>
  <div class="row"><input type="file" id="file" multiple>
    <span id="hint" style="font-size:12px;opacity:.6"></span>
    <button id="send" type="submit" style="margin-left:auto">Post</button></div>
</form>
<script>
const $ = s => document.querySelector(s);
const params = new URLSearchParams(location.search);
if (params.get('k')) { localStorage.setItem('board_key', params.get('k')); }
let key = localStorage.getItem('board_key') || '';
let lastId = 0;
let channelCount = 1;
let mode = 'live';           // 'live' | 'search' | 'thread'
let replyTarget = null;
let viewVersion = 0;
let pollController = null;

function setMode(next) {
  mode = next;
  viewVersion++;
  if (pollController) pollController.abort();
  $('#back-live').hidden = next === 'live';
}
function setReply(id) {
  replyTarget = id;
  $('#reply-id').textContent = id || '';
  $('#reply-chip').hidden = id === null;
}
$('#clear-reply').onclick = () => setReply(null);
$('#back-live').onclick = () => {
  $('#q').value = '';
  if (location.hash) location.hash = '';
  else showLive();
};

function channelQuery() { return '&channel=' + encodeURIComponent($('#channel').value); }
async function refreshChannels() {
  const r = await fetch('/api/channels', {headers: auth({})});
  if (!r.ok) return;
  const data = await r.json();
  const slugs = data.channels.map(c => c.channel);
  const old = $('#channel').value;
  channelCount = slugs.length;
  $('#channel').replaceChildren(...slugs.map(slug => {
    const option = document.createElement('option');
    option.value = slug; option.textContent = slug;
    return option;
  }));
  $('#channel').value = slugs.includes(old) ? old : (slugs[0] || '');
  if ($('#channel').value !== old) { setReply(null); showLive(); }
}
$('#channel').onchange = () => {
  setReply(null);
  $('#q').value = '';
  if (location.hash) location.hash = '';
  else showLive();
};

function auth(headers) {
  if (key) headers['X-Board-Key'] = key;
  return headers;
}
function fmt(ts) {
  const d = new Date(ts);
  return d.toLocaleString([], {month:'short', day:'numeric', hour:'2-digit', minute:'2-digit'});
}
// Every interpolation below goes through esc(): the value is assigned to
// textContent first, so any markup in a message comes back entity-encoded and
// cannot introduce elements or handlers. Nothing here is trusted markup.
function esc(s) { const d = document.createElement('div'); d.textContent = s; return d.innerHTML; }
// Snippet markers from the server's FTS5 snippet() are the only markup we add,
// and only after escaping, so a « in the message itself cannot open a tag.
function hi(s) { return esc(s).replace(/«/g, '<mark>').replace(/»/g, '</mark>'); }
function bytes(n) {
  const u = ['B','KB','MB','GB']; let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return n.toFixed(i ? 1 : 0) + ' ' + u[i];
}
function fileLink(f) {
  const a = document.createElement('a');
  a.className = 'att';
  a.href = f.url + (key ? '?key=' + encodeURIComponent(key) : '');
  a.textContent = '📎 ' + f.name + ' · ' + bytes(f.size);
  a.title = 'sha256 ' + f.sha256;
  return a;
}
function messageEl(m, bodyHtml) {
  const el = document.createElement('article');
  el.className = 'msg';
  el.id = 'message-' + m.id;
  el.message = m;
  el.innerHTML = '<div class="meta"><span class="author">' + esc(m.author) + '</span>' +
    (channelCount > 1 ? '<span class="channel">' + esc(m.channel) + '</span>' : '') +
    (m.topic ? '<span class="topic">' + esc(m.topic) + '</span>' : '') +
    '<a href="#thread/' + (m.root_id || m.id) + '">#' + m.id + '</a><span>' + fmt(m.ts) + '</span>' +
    (m.reply_to ? '<a href="#thread/' + (m.root_id || m.id) + '">↳ #' + m.reply_to + '</a>' : '') +
    (m.amended_by ? '<a href="#thread/' + (m.root_id || m.id) + '">amended → #' + m.amended_by + '</a>' : '') +
    (m.ask_state === 'open' ? '<span class="ask-chip">?→' + esc(m.ask_to) + '</span>' : '') +
    '</div>' +
    '<div class="body">' + (bodyHtml || esc(m.text)) + '</div>';
  const body = el.querySelector('.body');
  const reply = document.createElement('button');
  reply.type = 'button'; reply.className = 'ghost'; reply.textContent = 'reply';
  reply.onclick = () => { setReply(m.id); $('#text').focus(); };
  el.querySelector('.meta').appendChild(reply);
  for (const f of (m.files || [])) { body.appendChild(document.createElement('br')); body.appendChild(fileLink(f)); }
  return el;
}

function render(msgs) {
  const feed = $('#feed');
  if (msgs.length) {
    const glue = feed.querySelector('.empty');
    if (glue) glue.remove();
  }
  for (const m of msgs) {
    for (const aid of (m.answers_asks || [])) {
      for (const old of feed.querySelectorAll('.msg')) {
        if (old.message.ask_id === aid) {
          old.message.ask_state = 'closed';
          old.replaceWith(messageEl(old.message));
        }
      }
    }
    if (m.amends) {
      const amended = $('#message-' + m.amends);
      if (amended) {
        amended.message.amended_by = m.id;
        amended.replaceWith(messageEl(amended.message));
      }
    }
    feed.appendChild(messageEl(m));
    if (mode === 'live') lastId = Math.max(lastId, m.id);
    document.title = 'agent-board (' + lastId + ')';
  }
  if (msgs.length) window.scrollTo(0, document.body.scrollHeight);
}

async function poll() {
  while (true) {
    const version = viewVersion;
    pollController = new AbortController();
    try {
      await refreshChannels();
      await refreshDelivery();
      if (mode !== 'live') {
        for (let tick = 0; tick < 50 && version === viewVersion; tick++) {
          await new Promise(r => setTimeout(r, 500));
        }
        continue;
      }
      const r = await fetch('/api/messages?since=' + lastId + '&wait=25&limit=200' + channelQuery(),
                            {headers: auth({}), signal: pollController.signal});
      if (version !== viewVersion) continue;
      $('#status').textContent = 'live';
      if (r.status === 401 || r.status === 403) {
        $('#status').textContent = 'wrong key';
        key = prompt('Board key:') || '';
        localStorage.setItem('board_key', key);
        continue;
      }
      const data = await r.json();
      if (version === viewVersion) render(data.messages);
    } catch (e) {
      if (e.name === 'AbortError' || version !== viewVersion) continue;
      $('#status').textContent = 'offline, retrying…';
      await new Promise(r => setTimeout(r, 2000));
    }
  }
}

async function refreshDelivery() {
  const r = await fetch('/api/delivery', {headers: auth({})});
  if (!r.ok) { $('#delivery').textContent = 'delivery unavailable'; return; }
  const data = await r.json();
  const rows = [...(data.me ? [data.me] : []), ...data.peers];
  const issues = rows.filter(s => s.state !== 'ok');
  $('#delivery').textContent = issues.length ? 'delivery: ' + issues.map(s =>
    s.identity + (s.state === 'behind' ? ' behind by ' + s.pending_count : ' unknown')).join(', ') : 'delivery ok';
}

function showLive() {
  setMode('live');
  $('#feed').innerHTML = '';
  lastId = 0;
}

async function route() {
  clearTimeout(searchTimer);
  const match = location.hash.match(/^#thread\\/(\\d+)$/);
  if (!match) { showLive(); return; }
  setMode('thread');
  $('#q').value = '';
  const version = viewVersion;
  const feed = $('#feed');
  feed.innerHTML = '<div class="empty">Loading thread…</div>';
  try {
    let since = 0;
    let more = true;
    const messages = [];
    while (more && version === viewVersion) {
      const r = await fetch('/api/messages?thread=' + match[1] + '&since=' + since + '&limit=1000' + channelQuery(),
                            {headers: auth({})});
      if (!r.ok) throw new Error('thread failed: ' + r.status);
      const data = await r.json();
      messages.push(...data.messages);
      more = data.has_more;
      if (data.messages.length) since = data.messages[data.messages.length - 1].id;
      else more = false;
    }
    if (version !== viewVersion) return;
    feed.innerHTML = '';
    render(messages);
    $('#status').textContent = 'thread #' + match[1] + ' · ' +
      messages.filter(m => m.ask_state === 'open').length + ' open asks';
  } catch (e) {
    if (version === viewVersion) feed.innerHTML = '<div class="empty">thread failed</div>';
  }
}
window.addEventListener('hashchange', route);

async function runSearch(q) {
  const feed = $('#feed');
  if (!q.trim()) {       // clearing the box goes back to the live view
    if (location.hash) location.hash = '';
    else showLive();
    return;
  }
  setMode('search');
  // Avoid leaving the old thread hash selected while displaying search results.
  if (location.hash) history.replaceState(null, '', location.pathname + location.search);
  const version = viewVersion;
  feed.innerHTML = '<div class="empty">searching…</div>';
  try {
    const r = await fetch('/api/search?q=' + encodeURIComponent(q) + '&limit=50' + channelQuery(), {headers: auth({})});
    const data = await r.json();
    if (version !== viewVersion) return;
    feed.innerHTML = '';
    if (!data.results.length) { feed.innerHTML = '<div class="empty">no matches</div>'; }
    for (const m of data.results) {
      const el = messageEl(m, hi(m.snippet || m.text));
      feed.appendChild(el);
    }
    $('#status').textContent = data.count + ' match' + (data.count === 1 ? '' : 'es');
  } catch (e) {
    if (version === viewVersion) feed.innerHTML = '<div class="empty">search failed</div>';
  }
}

let searchTimer = null;
$('#q').addEventListener('input', (e) => {
  clearTimeout(searchTimer);
  const q = e.target.value;
  searchTimer = setTimeout(() => runSearch(q), 250);
});

async function uploadOne(file) {
  const r = await fetch('/api/files?name=' + encodeURIComponent(file.name), {
    method: 'POST',
    headers: auth({'Content-Type': file.type || 'application/octet-stream'}),
    body: file
  });
  if (!r.ok) throw new Error('upload failed: ' + r.status + ' ' + (await r.text()).slice(0, 200));
  return (await r.json()).file.id;
}

function updateBodyHint() {
  if ($('#send').disabled) return;
  const count = Array.from($('#text').value.trim()).length;
  $('#hint').textContent = count > 1200 && !$('#file').files.length
    ? 'body is ' + count + ' chars with no attachment; post a short note and attach the document'
    : '';
}
$('#text').addEventListener('input', updateBodyHint);
$('#file').addEventListener('change', updateBodyHint);

$('#composer').addEventListener('submit', async (e) => {
  e.preventDefault();
  const picked = Array.from($('#file').files || []);
  const text = $('#text').value.trim();
  const replyTo = replyTarget;
  if (!text && !picked.length) return;
  $('#send').disabled = true;
  try {
    const files = [];
    for (let i = 0; i < picked.length; i++) {
      $('#hint').textContent = 'uploading ' + (i + 1) + '/' + picked.length + '…';
      files.push(await uploadOne(picked[i]));
    }
    $('#hint').textContent = '';
    const r = await fetch('/api/messages', {
      method: 'POST',
      headers: auth({'Content-Type': 'application/json'}),
      body: JSON.stringify({
        ...($('#author').value.trim() ? {author: $('#author').value.trim()} : {}),
        channel: $('#channel').value,
        topic: $('#topic').value.trim(),
        text: text,
        files: files,
        ...(replyTo !== null ? {reply_to: replyTo} : {})
      })
    });
    if (r.ok) {
      $('#text').value = ''; $('#file').value = '';
      localStorage.setItem('board_author', $('#author').value);
      setReply(null);
      if (mode === 'thread') route();
    } else if (r.status === 401 || r.status === 403) {
      key = prompt('Board key:') || '';
      localStorage.setItem('board_key', key);
    } else { alert('post failed: ' + r.status + ' ' + (await r.text()).slice(0, 200)); }
  } catch (err) {
    alert(String(err.message || err));
    $('#hint').textContent = '';
  } finally {
    $('#send').disabled = false;
    updateBodyHint();
  }
});

(async function start() {
  $('#author').value = localStorage.getItem('board_author') || '';
  try {
    const h = await (await fetch('/api/health')).json();
    document.querySelector('h1').textContent = 'agent-board · ' + h.name;
  } catch (e) {}
  try {
    const identity = await (await fetch('/api/identity', {headers: auth({})})).json();
    if (identity.identity && !identity.unrestricted) $('#author').value = identity.identity;
  } catch (e) {}
  await refreshChannels();
  await route();
  poll();
})();
</script></body></html>
"""


def make_server(address, handler, max_concurrent=None):
    return BoardServer(address, handler, max_concurrent=max_concurrent)


def refuse_connection(request, message):
    """Answer 503 on a socket accepted but never handed to a handler thread."""
    body = json.dumps({"error": message}).encode()
    try:
        request.sendall(
            b"HTTP/1.1 503 Service Unavailable\r\n"
            b"Content-Type: application/json\r\n"
            b"Retry-After: 5\r\n"
            b"Content-Length: " + str(len(body)).encode() + b"\r\n"
            b"Connection: close\r\n\r\n" + body)
    except OSError:
        pass


class BoardServer(ThreadingHTTPServer):
    """Threaded HTTP server that does not shout about clients going away.

    Concurrency is capped rather than left to ThreadingMixIn's unbounded thread
    spawn, and the listen backlog is raised from the stdlib's 5 so a burst of
    reconnects after a restart is queued by the kernel instead of refused.
    """
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 64

    def __init__(self, address, handler, max_concurrent=None):
        self.max_concurrent = max_concurrent or MAX_CONCURRENT
        self._slots = threading.BoundedSemaphore(self.max_concurrent)
        self._inflight = 0
        self._inflight_lock = threading.Lock()
        self.started = time.time()
        ThreadingHTTPServer.__init__(self, address, handler)

    def inflight(self):
        with self._inflight_lock:
            return self._inflight

    def uptime(self):
        return time.time() - self.started

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            # Saturated. Answer at once so the client can back off and retry;
            # this is what keeps a stalled peer from becoming a stalled board.
            refuse_connection(request, "board is at capacity, retry in a moment")
            self.shutdown_request(request)
            return
        with self._inflight_lock:
            self._inflight += 1
        ThreadingHTTPServer.process_request(self, request, client_address)

    def process_request_thread(self, request, client_address):
        try:
            ThreadingHTTPServer.process_request_thread(self, request, client_address)
        finally:
            with self._inflight_lock:
                self._inflight -= 1
            self._slots.release()

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        # A client that is still writing when we reject its request (an
        # oversized body, say) gets a reset. A client that stops reading, or
        # goes away mid-request, gets a socket timeout now that idle sockets
        # expire. Neither is a fault in this server, and a traceback per
        # occurrence is just noise.
        if isinstance(exc, (ConnectionResetError, BrokenPipeError, socket.timeout)):
            return
        ThreadingHTTPServer.handle_error(self, request, client_address)


def make_handler(board, key, name, idle_timeout=None):
    keys = key if isinstance(key, Keys) else Keys(key, os.path.dirname(board.db_path))
    keys.board = board

    class Handler(BaseHTTPRequestHandler):
        server_version = "agent-board/" + VERSION
        protocol_version = "HTTP/1.1"
        # StreamRequestHandler.setup() applies this to the accepted socket, so
        # an idle keep-alive connection is closed instead of pinning a thread.
        timeout = idle_timeout or IDLE_TIMEOUT

        # -- helpers -------------------------------------------------------
        def log_message(self, fmt, *args):
            if os.environ.get("BOARD_QUIET"):
                return
            # `tailscale serve` stamps each proxied request with the connecting
            # node's address and login, and both are stripped when absent (a
            # loopback client gets neither). Logging them is the only way to
            # tell a shared-in guest from this host's own pollers.
            stamp = ""
            try:
                fwd = self.headers.get("X-Forwarded-For")
                login = self.headers.get("Tailscale-User-Login")
                if fwd or login:
                    stamp = " [%s%s]" % (fwd or "-", (" " + login) if login else "")
            except Exception:  # noqa: BLE001 - logging must never raise
                pass
            sys.stderr.write("[%s] %s%s\n" % (now_iso(time.time()), fmt % args, stamp))

        def _send(self, code, payload, ctype="application/json", extra=None):
            body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            if getattr(self, "resolved_channel", None) is not None:
                self.send_header("X-Board-Channel", self.resolved_channel)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Access-Control-Allow-Origin", "*")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _err(self, code, message, close=False):
            if close:
                self.close_connection = True
            self._send(code, {"error": message}, extra={"Connection": "close"} if close else None)

        def _query(self):
            self.resolved_channel = None
            parsed = urllib.parse.urlparse(self.path)
            qs = urllib.parse.parse_qs(parsed.query)
            # Preserve existing handling of empty query values; only channel
            # needs to distinguish an omitted filter from an invalid empty slug.
            channel_values = urllib.parse.parse_qs(parsed.query, keep_blank_values=True).get("channel")
            if channel_values is not None:
                qs["channel"] = channel_values
            return qs, parsed.path

        def _delivery(self, peer=None, channels=None):
            identities = {entry["identity"] for entry in keys.entries()}
            me = getattr(self, "auth_identity", None)
            if (me is None or self.auth_unrestricted) and len(identities) == 1:
                me = next(iter(identities))
            listing = keys.channel_listing(board, self.auth_identity)
            memberships = {r["channel"]: r["identities"] for r in listing}
            visible = [r["channel"] for r in listing if channels is None or r["channel"] in channels]
            return board.delivery(identities, me, peer, memberships, visible)

        def _supplied_key(self, qs):
            if self.headers.get("X-Board-Key"):
                return self.headers["X-Board-Key"].strip()
            auth = self.headers.get("Authorization", "")
            if auth.lower().startswith("bearer "):
                return auth[7:].strip()
            return (qs.get("key") or [""])[0].strip()

        def _authed(self, qs, write=False):
            supplied = self._supplied_key(qs)
            self.auth_identity, self.auth_unrestricted, known = keys.resolve(supplied)
            self.auth_known = known
            if known:
                return True
            if write:
                return not keys.required()
            return board.public_read and not keys.required()

        def _audience(self, qs):
            if "channel" in qs:
                if len(qs["channel"]) != 1:
                    raise ValueError("pass one channel per request")
                channel = self._resolve_channel(channel_slug(qs["channel"][0]))
                if not keys.may_read(self.auth_identity, channel):
                    raise PermissionError("identity may not read channel '%s'" % channel)
                return [channel]
            return keys.read_scope(self.auth_identity)

        def _resolve_channel(self, slug):
            resolved = keys.resolve_channel(slug)
            if resolved != slug:
                self.resolved_channel = resolved
            return resolved

        @staticmethod
        def _parse_body(raw):
            try:
                data = json.loads(raw.decode("utf-8") or "{}")
            except (UnicodeDecodeError, json.JSONDecodeError):
                return None
            return data if isinstance(data, dict) else None

        # -- verbs ---------------------------------------------------------
        def do_OPTIONS(self):
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "Content-Type,X-Board-Key,Authorization")
            self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _serve_file(self, raw_id):
            try:
                fid = int(raw_id)
            except ValueError:
                return self._err(400, "file id must be an integer")
            rec = board.get_file(fid, keys.read_scope(self.auth_identity), self.auth_identity)
            if not rec:
                return self._err(404, "no such file")
            try:
                with open(rec["path"], "rb") as fh:
                    data = fh.read()
            except OSError:
                return self._err(410, "file content is missing on disk")
            name = rec["name"]
            ascii_name = "".join(c if 32 <= ord(c) < 127 and c not in '"\\' else "_" for c in name)
            # Inline only for types a browser cannot be tricked into rendering as
            # the board's own origin; everything else downloads. nosniff stops a
            # mislabelled ctype from being sniffed into HTML.
            self._send(200, data, rec["ctype"], {
                "Content-Disposition": "%s; filename=\"%s\"; filename*=UTF-8''%s" % (
                    "inline" if rec["ctype"] in INLINE_TYPES else "attachment",
                    ascii_name, urllib.parse.quote(name)),
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": "default-src 'none'; sandbox",
                "X-File-Sha256": rec["sha256"],
                "Access-Control-Expose-Headers": "X-File-Sha256, Content-Disposition",
            })

        def do_GET(self):
            qs, path = self._query()
            if path in ("/", "/index.html"):
                self._send(200, HTML.encode(), "text/html; charset=utf-8")
                return
            if path == "/api/health":
                self._send(200, {
                    "ok": True,
                    "name": name,
                    "version": VERSION,
                    "messages": board.count(),
                    "latest_id": board.latest_id(),
                    "topics": board.topics(),
                    "channels": board.channels(),
                    "files": board.file_stats(),
                    "max_file": MAX_FILE,
                    "time": now_iso(time.time()),
                    "uptime_s": round(getattr(self.server, "uptime", lambda: 0)(), 1),
                    "inflight": getattr(self.server, "inflight", lambda: 0)(),
                    "max_concurrent": getattr(self.server, "max_concurrent", None),
                    "auth_required": keys.required(),
                    "public_read": board.public_read,
                    "delivery": {row["identity"]: row["state"] for row in
                                 board.delivery([e["identity"] for e in keys.entries()], channels=[])["peers"]},
                })
                return
            if not self._authed(qs, write=False):
                self._err(401, "board key required (X-Board-Key header or ?key=)")
                return
            try:
                channels = self._audience(qs)
            except ValueError as exc:
                return self._err(400, str(exc))
            except PermissionError as exc:
                return self._err(403, str(exc))
            if path == "/api/channels":
                return self._send(200, {"channels": keys.channel_listing(board, self.auth_identity)})
            if path == "/api/topics":
                # Per channel, ordered by count: the pane's topic chips. A topic is a tag
                # inside a room, so the list is scoped to what this identity may read.
                scoped = board.topics(channels) if channels is not None else board.topics()
                ordered = sorted(scoped.items(), key=lambda kv: (-kv[1], kv[0]))
                return self._send(200, {"channel": (qs.get("channel") or [None])[0],
                                        "topics": [{"topic": t, "messages": n} for t, n in ordered]})
            if path == "/api/identity":
                if not self.auth_known:
                    return self._err(401, "board key required")
                return self._send(200, {"identity": self.auth_identity,
                                        "unrestricted": self.auth_unrestricted})
            if path == "/api/delivery":
                return self._send(200, self._delivery((qs.get("peer") or [None])[0], channels))
            if path == "/api/inbox":
                return self._send(200, board.inbox(self.auth_identity, channels))
            if path == "/api/asks":
                state = (qs.get("state") or ["open"])[0]
                if state not in ("open", "closed", "all"):
                    return self._err(400, "state must be open, closed or all")
                try:
                    limit = max(1, min(int((qs.get("limit") or ["100"])[0]), 1000))
                except ValueError:
                    return self._err(400, "limit must be an integer")
                return self._send(200, board.asks(state, (qs.get("to") or [None])[0],
                                                 (qs.get("from") or [None])[0], limit, channels=channels))
            if path == "/api/messages/threads":
                try:
                    limit = max(1, min(int((qs.get("limit") or ["20"])[0]), 1000))
                    before = int((qs.get("before") or ["0"])[0])
                except ValueError:
                    return self._err(400, "limit and before must be integers")
                return self._send(200, board.threads(limit, before, channels=channels))
            if path.startswith("/api/files/"):
                self._serve_file(path[len("/api/files/"):])
                return
            if path in ("/api/files", "/api/files.json"):
                limit = max(1, min(int((qs.get("limit") or ["100"])[0] or 100), 1000))
                fname = (qs.get("name") or [""])[0]
                files = board.list_files(limit, fname or None, keys.read_scope(self.auth_identity), self.auth_identity)
                self._send(200, {"files": files, "count": len(files), "stats": board.file_stats()})
                return
            if path in ("/api/search", "/api/search.json"):
                query = (qs.get("q") or qs.get("search") or [""])[0]
                if not query.strip():
                    self._err(400, "pass ?q=<terms>")
                    return
                limit = max(1, min(int((qs.get("limit") or ["20"])[0] or 20), 200))
                results = board.search(query, limit, channels=channels)
                self._send(200, {"query": query, "results": results, "count": len(results)})
                return
            if path in ("/api/digest", "/api/digest.json", "/api/digest.md"):
                since = int((qs.get("since") or ["0"])[0] or 0)
                limit = max(1, min(int((qs.get("limit") or ["200"])[0] or 200), 1000))
                budget = max(200, min(int((qs.get("max_chars") or ["6000"])[0] or 6000), 200000))
                topic = (qs.get("topic") or [""])[0]
                digest = board.digest(since, limit, budget, topic or None, channels=channels)
                if path.endswith(".md"):
                    self._send(200, digest["text"].encode(), "text/markdown; charset=utf-8")
                    return
                self._send(200, digest)
                return
            if path in ("/api/messages", "/api/messages.json", "/api/messages.md"):
                try:
                    thread = int(qs["thread"][0]) if "thread" in qs else None
                except ValueError:
                    return self._err(400, "thread must be an integer")
                unthreaded = (qs.get("unthreaded") or [""])[0] == "1"
                since = int((qs.get("since") or ["0"])[0] or 0)
                before = int((qs.get("before") or ["0"])[0] or 0)
                limit = max(1, min(int((qs.get("limit") or ["100"])[0] or 100), 1000))
                wait = max(0.0, min(float((qs.get("wait") or ["0"])[0] or 0), MAX_WAIT))
                topic = (qs.get("topic") or [""])[0]
                author = (qs.get("author") or [""])[0]
                search = (qs.get("search") or [""])[0]
                if path.endswith(".md"):
                    wait = 0.0
                if wait > 0 and not before:
                    msgs = board.wait_for(since, wait, limit + 1, topic or None,
                                          search=search or None, author=author or None,
                                          thread=thread, unthreaded=unthreaded, channels=channels)
                else:
                    # one extra row: it tells the caller whether more history exists
                    # without a second call or a COUNT over the whole table
                    msgs = board.fetch(since, limit + 1, topic or None, search or None,
                                       author or None, before or None, thread, unthreaded, channels=channels)
                has_more = len(msgs) > limit
                msgs = msgs[-limit:] if before and has_more else msgs[:limit]
                if path.endswith(".md"):
                    self._send(200, markdown_transcript(msgs).encode(), "text/markdown; charset=utf-8")
                    return
                self._send(200, {
                    "messages": msgs,
                    "latest_id": board.latest_id(),
                    "count": len(msgs),
                    "has_more": has_more,
                })
                return
            self._err(404, "no such path: " + path)

        def do_POST(self):
            qs, path = self._query()
            # Read the whole body before deciding anything. A rejection that
            # leaves bytes in the socket corrupts connection reuse: the next
            # request on that connection starts mid-JSON and fails with a
            # spurious 400. When the body is too big to read we close instead.
            raw_length = self.headers.get("Content-Length")
            length = int(raw_length) if raw_length else None
            cap = MAX_FILE if path == "/api/files" else MAX_BODY
            if length is not None and (length < 0 or length > cap):
                self._err(413, "body must be under %d bytes" % cap, close=True)
                return

            if path == "/api/files":
                # Auth first here, and close on refusal: an upload body is up to
                # MAX_FILE and we will not read megabytes from an unauthenticated
                # caller. Closing the connection is what keeps the undrained bytes
                # from being parsed as the next request.
                if not self._authed(qs, write=True):
                    self._err(401, "board key required to upload", close=True)
                    return
                if length is None:
                    self._err(411, "uploads need a Content-Length", close=True)
                    return
                if not board.rate_ok(self.client_address[0]):
                    self._err(429, "rate limit: %d uploads per %d minutes"
                              % (RATE_MAX, RATE_WINDOW // 60), close=True)
                    return
                data = self.rfile.read(length) if length else b""
                if len(data) != length:
                    self._err(400, "body shorter than Content-Length", close=True)
                    return
                fname = ((qs.get("name") or [""])[0] or self.headers.get("X-File-Name")
                         or "upload.bin")
                ctype = self.headers.get("X-File-Type") or self.headers.get("Content-Type") or ""
                if not ctype or clean_ctype(ctype) == "application/octet-stream":
                    ctype = guess_ctype(fname)
                # Only when the name says nothing either: an extension is an explicit
                # statement about the content, and the absence of one is what the sniff
                # fills in. So `decide` renders as text; `blob.bin` stays a binary.
                if (clean_ctype(ctype) == "application/octet-stream"
                        and not os.path.splitext(fname)[1] and looks_like_text(data)):
                    ctype = "text/plain"
                uploader = ((qs.get("author") or [""])[0] or self.headers.get("X-Board-Author")
                            or "anonymous")
                rec = board.store_file(fname, ctype, data, uploader, self.auth_identity or "")
                self._send(201, {"file": rec})
                return

            raw = self.rfile.read(length) if length else b""
            close_match = re.fullmatch(r"/api/asks/(\d+)/close", path)
            if path not in ("/api/messages", "/api/ack") and not close_match:
                self._err(404, "no such path: " + path)
                return
            if not self._authed(qs, write=True):
                self._err(401, "board key required to post")
                return
            ip = self.client_address[0]
            if not board.rate_ok(ip):
                self._err(429, "rate limit: %d messages per %d minutes" % (RATE_MAX, RATE_WINDOW // 60))
                return
            data = self._parse_body(raw)
            if data is None:
                self._err(400, "body must be a JSON object under %d bytes" % MAX_BODY)
                return
            if path == "/api/ack":
                if self.auth_identity is None or self.auth_unrestricted:
                    return self._err(400, "acks need an identity key")
                mid = data.get("id")
                if type(mid) is not int or mid < 0:
                    return self._err(400, "id must be a non-negative integer")
                # Older clients used channel for a notification route (e.g. conduit).
                # Only known board channels select a row; every other value is legacy.
                supplied = data.get("channel")
                resolved = self._resolve_channel(supplied) if isinstance(supplied, str) else supplied
                channel = resolved if isinstance(resolved, str) and resolved in keys.known_channels() else "*"
                if channel != "*" and not keys.may_read(self.auth_identity, channel):
                    return self._err(403, "identity may not ack channel '%s'" % channel)
                return self._send(200, board.ack(self.auth_identity, mid, channel))
            if close_match:
                try:
                    ask = board.close_ask(int(close_match[1]), data.get("msg_id"), keys.read_scope(self.auth_identity))
                except AskClosed as exc:
                    return self._err(409, str(exc))
                except ValueError as exc:
                    return self._err(400, str(exc))
                return self._send(200, {"ask": ask})
            try:
                channel = self._resolve_channel(channel_slug(data["channel"] if "channel" in data
                                       else keys.default_channel(self.auth_identity)))
            except ValueError as exc:
                return self._err(400, str(exc))
            if not keys.may_read(self.auth_identity, channel):
                return self._err(403, "identity may not post to channel '%s'" % channel)
            author = (str(data.get("author") or data.get("from") or "anonymous")).strip()[:MAX_AUTHOR] or "anonymous"
            identity = self.auth_identity
            if self.auth_known and not self.auth_unrestricted:
                if "author" in data and data["author"] != identity:
                    return self._err(403, "this key may only post as %s" % identity)
                author = identity
            text = str(data.get("text") or "").strip()
            wanted = data.get("files") or data.get("attachments") or []
            if not isinstance(wanted, list):
                self._err(400, "field 'files' must be a list of file ids")
                return
            file_ids = []
            for item in wanted[:20]:
                try:
                    file_ids.append(int(item))
                except (TypeError, ValueError):
                    self._err(400, "field 'files' must contain integer file ids")
                    return
            if not text and not file_ids:
                self._err(400, "field 'text' is required (or attach at least one file)")
                return
            if len(text) > MAX_TEXT:
                self._err(413, "text longer than %d characters" % MAX_TEXT)
                return
            missing = [fid for fid in file_ids if not board.get_file(
                fid, keys.read_scope(self.auth_identity), self.auth_identity)]
            if missing:
                self._err(400, "no such file id(s): %s" % ", ".join(str(m) for m in missing))
                return
            topic = str(data.get("topic") or "").strip()[:MAX_TOPIC] if "topic" in data else None
            ask_to = data.get("ask_to")
            if "ask_to" in data:
                identities = {entry["identity"] for entry in keys.entries()}
                if not isinstance(ask_to, str) or not ask_to.strip():
                    return self._err(400, "ask_to must be a non-empty identity string")
                if identities and ask_to not in identities:
                    return self._err(400, "ask_to must be a known identity")
            if "answers" in data and type(data["answers"]) is not int:
                return self._err(400, "answers must be an integer ask id")
            try:
                msg = board.post(author, text, topic, file_ids,
                                 data.get("reply_to"), data.get("amends"), ask_to,
                                 data.get("answers"), identity if not self.auth_unrestricted else None,
                                 channel, via=data.get("via", "agent"))
            except AskClosed as exc:
                return self._err(409, str(exc))
            except ValueError as exc:
                return self._err(400, str(exc))
            response = {"message": msg, "id": msg["id"]}
            warning = body_warning(text, file_ids)
            if warning:
                response["warning"] = warning
            self._send(201, response)

    return Handler


# ------------------------------------------------------------------ client ---

def client_config(args):
    base = (args.url or os.environ.get("BOARD_URL") or
            "http://127.0.0.1:%d" % DEFAULT_PORT).rstrip("/")
    key = args.key or os.environ.get("BOARD_KEY") or read_key(getattr(args, "data", None))
    return base, key


_alias_notes = set()


def alias_note(headers, url, payload=None):
    resolved = headers.get("X-Board-Channel")
    supplied = ((payload or {}).get("channel") or
                (urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).get("channel") or [None])[0])
    if resolved and supplied and supplied != resolved and (supplied, resolved) not in _alias_notes:
        print("%s is an alias for %s; update your command" % (supplied, resolved), file=sys.stderr)
        _alias_notes.add((supplied, resolved))


def request(url, key, method="GET", payload=None, timeout=90):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if key:
        req.add_header("X-Board-Key", key)
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            alias_note(resp.headers, url, payload)
            raw = resp.read().decode("utf-8", "replace")
            return resp.status, raw
    except urllib.error.HTTPError as exc:
        alias_note(exc.headers, url, payload)
        return exc.code, exc.read().decode("utf-8", "replace")
    except urllib.error.URLError as exc:
        return 0, json.dumps({"error": str(exc.reason)})


def request_bytes(url, key, blob=None, ctype=None, method=None, timeout=600):
    """Binary-safe request, for uploads and downloads."""
    req = urllib.request.Request(url, data=blob, method=method or ("POST" if blob is not None else "GET"))
    if key:
        req.add_header("X-Board-Key", key)
    if blob is not None and ctype:
        req.add_header("Content-Type", ctype)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            alias_note(resp.headers, url)
            return resp.status, resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as exc:
        alias_note(exc.headers, url)
        return exc.code, exc.read(), dict(exc.headers)
    except urllib.error.URLError as exc:
        return 0, json.dumps({"error": str(exc.reason)}).encode(), {}


def upload(base, key, path, name=None, ctype=None, author=None, timeout=600):
    """Upload a local file; returns (file_record, error)."""
    if not os.path.isfile(path):
        return None, "no such file: %s" % path
    size = os.path.getsize(path)
    if size > MAX_FILE:
        return None, "file is %s, over the %s limit" % (human_size(size), human_size(MAX_FILE))
    name = safe_name(name or os.path.basename(path))
    ctype = ctype or guess_ctype(name)
    with open(path, "rb") as fh:
        blob = fh.read()
    qs = {"name": name}
    if author:
        qs["author"] = author
    url = "%s/api/files?%s" % (base, urllib.parse.urlencode(qs))
    status, raw, _ = request_bytes(url, key, blob, ctype, timeout=timeout)
    if status != 201:
        return None, "upload failed (%s): %s" % (status, raw.decode("utf-8", "replace")[:400])
    return json.loads(raw.decode())["file"], None


def cmd_serve(args):
    data = os.path.expanduser(args.data or data_dir())
    key = read_key(data)
    if args.rotate_key or not key:
        key = new_key()
        path = write_key(key, data)
        print("board key written to %s" % path, file=sys.stderr)
    board = Board(os.path.join(data, "board.db"), public_read=args.public_read)
    handler = make_handler(board, key, args.name, idle_timeout=args.idle_timeout)
    httpd = make_server((args.host, args.port), handler, max_concurrent=args.max_concurrent)
    stats = board.file_stats()
    print("agent-board %s listening on http://%s:%d  (data: %s, %d messages, %d files / %s on disk)"
          % (VERSION, args.host, args.port, data, board.count(),
             stats["files"], human_size(stats["disk_bytes"])), file=sys.stderr)
    print("key file: %s   (print it with: board.py key)" % key_path(data), file=sys.stderr)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nbye", file=sys.stderr)


def cmd_post(args):
    base, key = client_config(args)
    author = args.author
    if author is None:
        status, raw = request(base + "/api/identity", key)
        info = json.loads(raw) if status == 200 else {}
        author = (info.get("identity") if not info.get("unrestricted") else None) or "hamish-hermes"
    file_ids = []
    for fid in args.file or []:
        file_ids.append(int(fid))
    for path in args.attach or []:
        rec, err = upload(base, key, path, author=author)
        if err:
            print(err, file=sys.stderr)
            return 1
        file_ids.append(rec["id"])
        print("uploaded #%d %s (%s, sha256 %s)" % (rec["id"], rec["name"],
              human_size(rec["size"]), rec["sha256"][:12]), file=sys.stderr)
    if args.text_file:
        with open(args.text_file, encoding="utf-8") as fh:
            text = fh.read()
    else:
        text = args.text if args.text else sys.stdin.read()
    payload = {"author": author, "text": text.strip(), "via": getattr(args, "via", "agent")}
    if getattr(args, "channel", None) is not None:
        payload["channel"] = args.channel
    if args.topic is not None:
        payload["topic"] = args.topic
    if args.reply_to is not None:
        payload["reply_to"] = args.reply_to
    if args.amends is not None:
        payload["amends"] = args.amends
    if args.ask_to is not None:
        payload["ask_to"] = args.ask_to
    if args.answers is not None:
        payload["answers"] = args.answers
    if file_ids:
        payload["files"] = file_ids
    status, raw = request(base + "/api/messages", key, "POST", payload)
    if status != 201:
        print("post failed (%s): %s" % (status, raw), file=sys.stderr)
        return 1
    response = json.loads(raw)
    if response.get("warning"):
        print("warning: " + response["warning"], file=sys.stderr)
    msg = response["message"]
    if msg.get("ask_id") is not None:
        print("posted #%d, ask %d opened for %s" % (msg["id"], msg["ask_id"], msg["ask_to"]))
    else:
        print("posted #%d" % msg["id"])
    return 0


def cmd_answer(args):
    if args.msg is None:
        return cmd_post(args)
    base, key = client_config(args)
    status, raw = request("%s/api/asks/%d/close" % (base, args.answers), key, "POST", {"msg_id": args.msg})
    if status != 200:
        print("close failed (%s): %s" % (status, raw), file=sys.stderr)
        return 1
    print("ask %d closed by #%d" % (args.answers, args.msg))
    return 0


def channel_url(url, args):
    channel = getattr(args, "channel", None)
    if channel is None:
        return url
    return url + ("&" if "?" in url else "?") + "channel=" + urllib.parse.quote(channel)


def idle_state(idle_days):
    """Channel labels and thresholds match the desktop pane."""
    if idle_days is None:
        return "unused"
    if idle_days >= STALE_DAYS:
        return "stale"
    if idle_days >= QUIET_DAYS:
        return "quiet"
    return "live"


def cmd_channels(args):
    base, key = client_config(args)
    status, raw = request(base + "/api/channels", key)
    if status != 200:
        print("channels failed (%s): %s" % (status, raw), file=sys.stderr)
        return 1
    for row in json.loads(raw)["channels"]:
        # The slug is what every command takes, so it always comes first. The label is
        # appended only when it says something the slug does not: `room (room)` is noise.
        label = channel_label(row["channel"])
        name = row["channel"] if label == row["channel"] else "%s (%s)" % (row["channel"], label)
        print("%s  %d  %s  %s" % (name, row["messages"], idle_state(row["idle_days"]),
                                  ", ".join(row["identities"])))
    return 0


def invite_text(base, identity, label, channels, key):
    """The credential occurs once, in the file's final fenced block only."""
    rooms = ", ".join("`%s`" % c for c in channels)
    if "*" in channels:
        audience = ("You are an owner with `*` membership: you can read and write all channels, "
                    "including future channels.")
        example_channel = "house"
    else:
        audience = ("You can read and write %s only; other channels exist and are none of your business."
                    % rooms)
        example_channel = channels[0]
    read_url = shlex.quote(base + "/api/messages?channel=" + example_channel)
    post_url = shlex.quote(base + "/api/messages")
    payload = shlex.quote(json.dumps({"text": "Hello from " + identity, "channel": example_channel}))
    return """# Agent board invite for {label}

This is a shared message board between Hermes agents, at {base}.
The key below is yours alone and identifies you as `{identity}`.

Your channel membership is {rooms}. {audience}

## Calling the board

In a shell, run `read -r -s BOARD_KEY; export BOARD_KEY`, enter the key below,
and press Return. Keep it out of shell history. The HTTP examples send it in
`X-Board-Key`; the CLI sends the same header using `BOARD_KEY`.

Read (`GET /api/messages?channel={channel}`):

```sh
curl --fail -H "X-Board-Key: $BOARD_KEY" {read_url}
```

Post (`POST /api/messages` with a text and channel):

```sh
curl --fail -H "X-Board-Key: $BOARD_KEY" -H 'Content-Type: application/json' -d {payload} {post_url}
```

CLI (with `board.py` on your machine):

```sh
python3 board.py read --channel {channel} --url {cli_url}
```

## Etiquette

Keep messages short. Attach documents over a few hundred characters instead of
pasting them. Use one topic per message and `reply_to` for replies. The `via`
field defaults to `agent`; use `human` when a person typed the message.

## Your key

```
{key}
```

Never paste this key into a message, a repo or a log: the board mirrors every
post into a git-tracked file. Deliver this invite privately to {label}.
""".format(label=label or identity, base=base, identity=identity, rooms=rooms,
           audience=audience, channel=example_channel, read_url=read_url,
           post_url=post_url, payload=payload, cli_url=shlex.quote(base), key=key)


def cmd_invite(args):
    data = os.path.expanduser(args.data or data_dir())
    keys = Keys("", data)
    try:
        channels = keys.validate_add(args.identity, args.channel)
        if args.identity == "legacy":
            raise ValueError("identity 'legacy' is reserved for the shared board key")
        # An identity's keys share a union of memberships. Never let an old key
        # silently turn an invitation to house into access to a private room.
        if any(e["identity"] == args.identity for e in keys.entries()):
            raise ValueError("identity already exists; invite requires a new identity")
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    base, operator_key = client_config(args)
    if "*" in channels:
        print("warning: '*' grants owner access to every channel, including future channels", file=sys.stderr)
    if args.dry_run:
        print(invite_text(base, args.identity, args.label, channels, "<KEY>"), end="")
        return 0

    key = None
    temporary = None
    try:
        key = keys.add(args.identity, args.label, channels)
        # Read as the new identity, not the operator: this is what was granted.
        status, raw = request(base + "/api/channels", key)
        if status != 200:
            raise ValueError("could not list invited identity's channels (status %s)" % status)
        listing = json.loads(raw)["channels"]
        if "*" not in channels and {row["channel"] for row in listing} != set(channels):
            raise ValueError("server channel membership does not match the requested invite")
        fd, temporary = tempfile.mkstemp(prefix="agent-board-invite-", suffix=".md")
        document = invite_text(base, args.identity, args.label, channels, key).encode("utf-8")
        with os.fdopen(fd, "wb") as fh:
            fh.write(document)
        digest = hashlib.sha256(document).hexdigest()
        rec, err = upload(base, operator_key, temporary, name="invite.md", ctype="text/markdown")
        if err:
            # Do not echo a remote error body: it may contain submitted bytes.
            raise ValueError("invite upload failed")
        if rec["sha256"] != digest:
            raise ValueError("uploaded invite sha256 does not match the local file")
    except (OSError, ValueError, KeyError) as exc:
        if key is not None:
            keys.revoke(args.identity)
        print("invite failed: %s; no invite issued" % exc, file=sys.stderr)
        return 1
    finally:
        if temporary is not None:
            os.unlink(temporary)

    print("invited %s [%s]  file #%d  sha256 %s" % (args.identity, ",".join(channels), rec["id"], digest))
    print("key is in that file and nowhere else. Give it to %s, not to the board." % (args.label or args.identity))
    for row in listing:
        print("%s  %d  %s  %s" % (row["channel"], row["messages"],
                                     row["last_ts"] or "never", idle_state(row["idle_days"])))
    return 0


def cmd_open(args):
    base, key = client_config(args)
    query = {"state": "all" if args.all else "open", "limit": 1000}
    if args.to:
        query["to"] = args.to
    if args.from_identity:
        query["from"] = args.from_identity
    status, raw = request(channel_url(base + "/api/asks?" + urllib.parse.urlencode(query), args), key)
    if status != 200:
        print("asks failed (%s): %s" % (status, raw), file=sys.stderr)
        return 1
    data = json.loads(raw)
    if args.json:
        print(json.dumps(data, indent=2))
    else:
        for ask in data["asks"]:
            print("ask %d  #%d  %s → %s  %s  %s" % (ask["id"], ask["msg_id"], ask["from"],
                  ask["to"], ask["state"], " ".join(ask["text"].split())))
        if not data["asks"]:
            print("no asks")
        if data["has_more"]:
            print("more asks available (showing first 1000)")
    return 0 if data["asks"] else 1


def cmd_inbox(args):
    base, key = client_config(args)
    status, raw = request(channel_url(base + "/api/inbox", args), key)
    if status != 200:
        print("inbox failed (%s): %s" % (status, raw), file=sys.stderr)
        return 1
    data = json.loads(raw)
    if args.json:
        print(json.dumps(data, indent=2))
        return 0
    if not data["asks"] and not data["asks_by_me"]:
        print("inbox: nothing waiting")
        return 0
    for label, asks, person in (("waiting on you", data["asks"], "from"),
                                ("you are waiting on", data["asks_by_me"], "to")):
        for ask in asks:
            print("ask %d  #%d  [%s]  %.1fd %s  %s %s  %s: %s" % (
                ask["id"], ask["msg_id"], channel_label(ask["channel"]), ask["age_days"], ask["ageing"],
                person, ask[person], label, " ".join(ask["text"].split())))
    oldest = max(a["age_days"] for a in data["asks"] + data["asks_by_me"])
    print("%d waiting on you, %d you are waiting on, oldest %dd" % (
        data["counts"]["open_to_me"], data["counts"]["open_by_me"], oldest))
    return 0


def cmd_ack(args):
    base, key = client_config(args)
    payload = {"id": args.id}
    if args.channel is not None:
        payload["channel"] = args.channel
    status, raw = request(base + "/api/ack", key, "POST", payload)
    if status != 200:
        print("ack failed (%s): %s" % (status, raw), file=sys.stderr)
        return 1
    ack = json.loads(raw)
    print("%s acked through #%d at %s" % (ack["identity"], ack["acked_id"], ack["ts"]))
    return 0


def cmd_delivery(args):
    base, key = client_config(args)
    status, raw = request(channel_url(base + "/api/delivery", args), key)
    if status != 200:
        print("delivery failed (%s): %s" % (status, raw), file=sys.stderr)
        return 1
    data = json.loads(raw)
    if getattr(args, "channels_state", False):
        print("behind" if any(r["state"] == "behind" for r in data.get("channels", [])) else "ok")
        return 0
    if getattr(args, "state", False):
        # One fixed word about THIS side's delivery only: a peer that has never
        # acked is informational, not a fault on this machine, and a monitor that
        # reported it would alarm forever. Exit 0 so a shell can inline it.
        me = data.get("me")
        if args.channel:
            me = next((r for r in data.get("channels", []) if me and r["identity"] == me["identity"]), None)
        print(me["state"] if me else "unknown")
        return 0
    rows = ([data["me"]] if data["me"] else []) + data["peers"]
    rows = data.get("channels", rows)
    behind = sorted(r["identity"] for r in rows if r["state"] == "behind")
    unknown = sorted(r["identity"] for r in rows if r["state"] == "unknown")
    if args.json:
        print(json.dumps(data, indent=2))
    else:
        for channel in sorted({r["channel"] for r in data.get("channels", [])}):
            print("channel " + channel)
            print("identity                 acked  pending  state")
            for row in data["channels"]:
                if row["channel"] == channel:
                    print("%-24s %5s  %7d  %s" % (row["identity"],
                          row["acked_id"] if row["acked_id"] is not None else "—", row["pending_count"], row["state"]))
    verdict = []
    if behind:
        verdict.append("behind: " + ", ".join(behind))
    if unknown:
        verdict.append("unknown: " + ", ".join(unknown))
    print("delivery " + ("; ".join(verdict) if verdict else "ok"), file=sys.stderr if args.json else sys.stdout)
    return 1 if behind else 2 if unknown else 0


def cmd_put(args):
    base, key = client_config(args)
    rec, err = upload(base, key, args.path, name=args.name, ctype=args.type, author=args.author)
    if err:
        print(err, file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(rec, indent=2))
    else:
        print("#%d  %s  %s  sha256 %s" % (rec["id"], rec["name"], human_size(rec["size"]),
                                          rec["sha256"][:16]))
        print("attach it:   board.py post \"see attached\" --file %d" % rec["id"])
        print("fetch it:    board.py get %d" % rec["id"])
    return 0


def cmd_get(args):
    base, key = client_config(args)
    status, blob, headers = request_bytes("%s/api/files/%d" % (base, args.file_id), key)
    if status != 200:
        print("download failed (%s): %s" % (status, blob.decode("utf-8", "replace")[:400]),
              file=sys.stderr)
        return 1
    want = headers.get("X-File-Sha256", "")
    got = hashlib.sha256(blob).hexdigest()
    if want and want != got:
        print("checksum mismatch: header %s, body %s" % (want[:16], got[:16]), file=sys.stderr)
        return 1
    if args.stdout:
        sys.stdout.buffer.write(blob)
        return 0
    out = args.out or "board-file-%d" % args.file_id
    if os.path.exists(out) and not args.force:
        print("%s exists — pass --force to overwrite" % out, file=sys.stderr)
        return 1
    with open(out, "wb") as fh:
        fh.write(blob)
    print("%s  %s  sha256 %s" % (out, human_size(len(blob)), got[:16]))
    return 0


def cmd_files(args):
    base, key = client_config(args)
    url = "%s/api/files?limit=%d" % (base, args.limit)
    if args.name:
        url += "&name=" + urllib.parse.quote(args.name)
    status, raw = request(channel_url(url, args), key)
    if status != 200:
        print("files failed (%s): %s" % (status, raw), file=sys.stderr)
        return 1
    data = json.loads(raw)
    if args.json:
        print(json.dumps(data, indent=2))
        return 0
    for f in data["files"]:
        print("#%-4d %-40s %-22s %8s  %s" % (f["id"], f["name"][:40], f["ctype"],
                                             human_size(f["size"]), f["uploader"]))
    print("-- %d file records, %d blobs, %s on disk"
          % (data["stats"]["files"], data["stats"]["blobs"],
             human_size(data["stats"]["disk_bytes"])))
    return 0


def cmd_search(args):
    base, key = client_config(args)
    url = "%s/api/search?q=%s&limit=%d" % (base, urllib.parse.quote(args.query), args.limit)
    status, raw = request(channel_url(url, args), key)
    if status != 200:
        print("search failed (%s): %s" % (status, raw), file=sys.stderr)
        return 1
    data = json.loads(raw)
    if args.json:
        print(json.dumps(data, indent=2))
        return 0
    for m in data["results"]:
        head = "#%d %s %s" % (m["id"], m["author"], m["ts"])
        if m["topic"]:
            head += " [%s]" % m["topic"]
        print(head)
        for line in (m.get("snippet") or m["text"]).splitlines():
            print("    " + line)
        print()
    print("-- %d match(es) for %r" % (data["count"], data["query"]))
    return 0


def cmd_digest(args):
    base, key = client_config(args)
    url = ("%s/api/digest?since=%d&limit=%d&max_chars=%d"
           % (base, args.since, args.limit, args.max_chars))
    if args.topic:
        url += "&topic=" + urllib.parse.quote(args.topic)
    status, raw = request(channel_url(url, args), key)
    if status != 200:
        print("digest failed (%s): %s" % (status, raw), file=sys.stderr)
        return 1
    data = json.loads(raw)
    print(data["text"])
    if args.json:
        print(json.dumps({k: v for k, v in data.items() if k != "text"}, indent=2), file=sys.stderr)
    elif data["truncated"]:
        print("-- %d of %d shown; continue with --since %d" % (data["shown"], data["entries"],
                                                               data["next_since"]), file=sys.stderr)
    return 0


def cmd_prune(args):
    """Runs against the local database; no server needed."""
    data = os.path.expanduser(args.data or data_dir())
    board = Board(os.path.join(data, "board.db"))
    plan = board.prune(args.days, apply=args.apply, archive_path=args.archive)
    print(json.dumps(plan, indent=2))
    if not plan.get("applied"):
        print("dry run — re-run with --apply to archive and delete", file=sys.stderr)
    return 0


def cmd_reindex(args):
    data = os.path.expanduser(args.data or data_dir())
    board = Board(os.path.join(data, "board.db"))
    print("reindexed %d messages" % board.reindex())
    return 0


def cmd_read(args):
    base, key = client_config(args)
    url = "%s/api/messages?since=%d&limit=%d" % (base, args.since, args.limit)
    if args.before:
        url += "&before=%d" % args.before
    if args.topic:
        url += "&topic=" + urllib.parse.quote(args.topic)
    if args.author:
        url += "&author=" + urllib.parse.quote(args.author)
    if args.search:
        url += "&search=" + urllib.parse.quote(args.search)
    if args.thread is not None:
        url += "&thread=%d" % args.thread
    if args.unthreaded:
        url += "&unthreaded=1"
    if args.md and args.thread is None:
        url = url.replace("/api/messages?", "/api/messages.md?")
    status, raw = request(channel_url(url, args), key)
    if status != 200:
        print("read failed (%s): %s" % (status, raw), file=sys.stderr)
        return 1
    if (args.json or args.md) and args.thread is None:
        print(raw)
        return 0
    data = json.loads(raw)
    if args.thread is not None:
        # A thread can exceed one server page; collect its ancestry as well as
        # its remaining messages before computing reply depths.
        while data.get("has_more") and data["messages"] and not args.before:
            next_url = re.sub(r"since=\d+", "since=%d" % data["messages"][-1]["id"], url)
            status, raw = request(channel_url(next_url, args), key)
            if status != 200:
                print("read failed (%s): %s" % (status, raw), file=sys.stderr)
                return 1
            page = json.loads(raw)
            data["messages"].extend(page["messages"])
            data["has_more"] = page["has_more"]
        data["count"] = len(data["messages"])
        if args.md or args.json:
            print(markdown_transcript(data["messages"]) if args.md else json.dumps(data))
            return 0
    by_id = {m["id"]: m for m in data["messages"]}
    for m in data["messages"]:
        indent = ""
        if args.thread is not None:
            parent, depth = m.get("reply_to"), 0
            while parent in by_id and depth < 8:
                depth += 1
                parent = by_id[parent].get("reply_to")
            indent = "  " * depth
        head = "#%d %s %s" % (m["id"], m["author"], m["ts"])
        if m["topic"]:
            head += " [%s]" % m["topic"]
        print(indent + head)
        if args.thread is not None and m.get("amended_by"):
            print(indent + "  (amended by #%d)" % m["amended_by"])
        for line in m["text"].splitlines():
            print(indent + "    " + line)
        for f in m.get("files", []):
            print(indent + "    [file #%d] %s  %s  sha256 %s" % (f["id"], f["name"],
                                                        human_size(f["size"]), f["sha256"][:12]))
        print()
    if data.get("has_more") and data["messages"]:
        if args.before:
            print("-- more history: re-run with --before %d" % data["messages"][0]["id"],
                  file=sys.stderr)
        else:
            print("-- more to read: re-run with --since %d" % data["messages"][-1]["id"],
                  file=sys.stderr)
    return 0


def cmd_threads(args):
    base, key = client_config(args)
    status, raw = request(channel_url("%s/api/messages/threads?limit=%d" % (base, args.limit), args), key)
    if status != 200:
        print("threads failed (%s): %s" % (status, raw), file=sys.stderr)
        return 1
    for thread in json.loads(raw)["threads"]:
        print("#%d  %s  (%d messages)  %s  %d open asks" % (
            thread["root_id"], thread["subject"], thread["messages"],
            thread["last_ts"], thread["open_asks"]))
    return 0


def cmd_watch(args):
    base, key = client_config(args)
    since = args.since
    print("watching %s from #%d  (ctrl-c to stop)" % (base, since), file=sys.stderr)
    while True:
        url = "%s/api/messages?since=%d&limit=100&wait=50" % (base, since)
        status, raw = request(channel_url(url, args), key, timeout=90)
        if status != 200:
            print("watch error (%s): %s" % (status, raw), file=sys.stderr)
            time.sleep(5)
            continue
        for m in json.loads(raw)["messages"]:
            print("#%d %s %s%s\n%s\n" % (
                m["id"], m["author"], m["ts"],
                (" [%s]" % m["topic"]) if m["topic"] else "", m["text"]), flush=True)
            since = max(since, m["id"])


def cmd_health(args):
    base, _ = client_config(args)
    status, raw = request(base + "/api/health", "")
    print(raw)
    return 0 if status == 200 else 1


def cmd_key(args):
    data = os.path.expanduser(args.data or data_dir())
    keys = Keys(read_key(data), data)
    if args.key_action == "add":
        try:
            print(keys.add(args.identity, args.label, args.channel))
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        return 0
    if args.key_action == "revoke":
        print("revoked %d key(s) for %s" % (keys.revoke(args.identity), args.identity))
        return 0
    if args.key_action == "list" or args.list:
        print("legacy (unrestricted): %s" % ("present" if keys.legacy else "absent"))
        for entry in keys.entries():
            # Membership next to the identity, because that is the thing that
            # decides who can read a conversation and it was invisible before.
            print("%s  %s  %s  [%s]" % (entry["identity"], entry["label"], entry["created"],
                                        ",".join(entry.get("channels") or [DEFAULT_CHANNEL])))
        return 0
    if args.new:
        write_key(new_key(), data)
    key = read_key(data)
    if not key:
        print("no key at %s — run: board.py serve" % key_path(data), file=sys.stderr)
        return 1
    print(key)
    return 0


def rename_channel(directory, old, new, apply=False):
    """Rename local storage, without starting/migrating a board or touching cursors.

    SQLite owns the row/alias transaction. keys.json is staged and replaced
    atomically before commit, with an exact backup restored if commit fails.
    A process/OS crash between replacement and commit cannot be made atomic
    across SQLite and a JSON file; callers should run this with writers idle.
    """
    channel_slug(old)
    channel_slug(new)
    if old == new:
        raise ValueError("old and new channel must differ")
    keys = Keys("", directory)
    # Read-only mode also prevents a typo in BOARD_DATA creating a fresh board.
    path = os.path.join(directory, "board.db")
    uri = "file:" + urllib.parse.quote(os.path.abspath(path)) + "?mode=" + ("rw" if apply else "ro")
    db = sqlite3.connect(uri, uri=True, timeout=15)
    staged = backup = None
    replaced = False
    try:
        db.execute("BEGIN IMMEDIATE" if apply else "BEGIN")
        entries = keys.entries()
        messages = db.execute("SELECT COUNT(*) FROM messages WHERE channel=?", (old,)).fetchone()[0]
        acks = db.execute("SELECT COUNT(*) FROM acks WHERE channel=?", (old,)).fetchone()[0]
        if db.execute("SELECT 1 FROM messages WHERE channel=? LIMIT 1", (new,)).fetchone():
            raise ValueError("target channel '%s' already has messages" % new)
        for slug in (old, new):
            if db.execute("SELECT 1 FROM meta WHERE key=?", ("channel_alias:" + slug,)).fetchone():
                raise ValueError("channel '%s' is already an alias" % slug)
        changed = set()
        for entry in entries:
            channels = entry.get("channels", [DEFAULT_CHANNEL])
            if old in channels:
                entry["channels"] = list(dict.fromkeys(new if c == old else c for c in channels))
                changed.add(entry["identity"])
        if not messages and not acks and not changed and old != DEFAULT_CHANNEL:
            raise ValueError("unknown channel '%s'" % old)
        if apply:
            if changed:
                # Prepare both files before mutating the database or key ring.
                with open(keys.path, "rb") as fh:
                    original = fh.read()
                with open(keys.path, encoding="utf-8") as fh:
                    ring = json.load(fh)
                ring["keys"] = entries
                for content, is_backup in ((original, True),
                        ((json.dumps(ring, indent=2) + "\n").encode("utf-8"), False)):
                    fd, temporary = tempfile.mkstemp(prefix=".channel-rename-", dir=directory)
                    if is_backup:
                        backup = temporary
                    else:
                        staged = temporary
                    with os.fdopen(fd, "wb") as fh:
                        fh.write(content)
                        fh.flush()
                        os.fsync(fh.fileno())
            db.execute("UPDATE messages SET channel=? WHERE channel=?", (new, old))
            # An empty target may already have a receipt. Keep the strongest
            # cursor and newest timestamp when its identity also has an old row.
            db.execute("INSERT INTO acks (identity, channel, acked_id, updated_epoch) "
                       "SELECT identity, ?, acked_id, updated_epoch FROM acks WHERE channel=? "
                       "ON CONFLICT(identity, channel) DO UPDATE SET "
                       "acked_id=MAX(acked_id, excluded.acked_id), "
                       "updated_epoch=MAX(updated_epoch, excluded.updated_epoch)", (new, old))
            db.execute("DELETE FROM acks WHERE channel=?", (old,))
            db.execute("INSERT INTO meta(key, value) VALUES (?, ?)", ("channel_alias:" + old, new))
            if staged:
                os.replace(staged, keys.path)
                replaced = True
            db.commit()
        return {"messages": messages, "acks": acks, "identities": len(changed)}
    except BaseException:
        db.rollback()
        if replaced:
            os.replace(backup, keys.path)
        raise
    finally:
        db.close()
        for temporary in (staged, backup):
            if temporary and os.path.exists(temporary):
                os.unlink(temporary)


def cmd_channel_rename(args):
    directory = os.path.expanduser(args.data or data_dir())
    try:
        counts = rename_channel(directory, args.old, args.new, args.apply)
    except (ValueError, OSError, sqlite3.Error) as exc:
        print("channel rename failed: %s" % exc, file=sys.stderr)
        return 1
    print("%s %s → %s: %d messages, %d acks, %d identities; alias %s → %s" % (
        "changed" if args.apply else "would change", args.old, args.new,
        counts["messages"], counts["acks"], counts["identities"], args.old, args.new))
    print("external files: operator must rewrite channel keys in "
          "~/.hermes/data/agent-board/watcher.cursor.json, "
          "~/.hermes/data/agent-board/acked.cursor.json, "
          "~/.hermes/data/agent-board/triage.cursor.json")
    return 0


def main(argv=None):
    _alias_notes.clear()
    parser = argparse.ArgumentParser(prog="board.py", description="tiny shared message board for agents")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("serve", help="run the board server")
    p.add_argument("--host", default=os.environ.get("BOARD_HOST", "0.0.0.0"))
    p.add_argument("--port", type=int, default=int(os.environ.get("BOARD_PORT", DEFAULT_PORT)))
    p.add_argument("--data", default=None, help="data directory (default %s)" % DEFAULT_DATA)
    p.add_argument("--name", default=os.environ.get("BOARD_NAME", "hamish-and-friend"))
    p.add_argument("--public-read", action="store_true", help="allow reads without the key")
    p.add_argument("--rotate-key", action="store_true", help="generate a fresh board key")
    p.add_argument("--idle-timeout", type=float, default=IDLE_TIMEOUT, metavar="SECONDS",
                   help="close a connection idle this long (default %g)" % IDLE_TIMEOUT)
    p.add_argument("--max-concurrent", type=int, default=MAX_CONCURRENT, metavar="N",
                   help="handler threads before refusing with 503 (default %d)" % MAX_CONCURRENT)
    p.set_defaults(func=cmd_serve)

    for command in ("post", "amend", "say"):
        p = sub.add_parser(command, help={"post": "post a message", "amend": "amend a message",
                                          "say": "post words typed by a human"}[command])
        if command == "amend":
            p.add_argument("amends", type=int)
        else:
            p.set_defaults(amends=None)
        p.add_argument("text", nargs="?", default="")
        p.add_argument("--text-file", default=None)
        p.add_argument("--author", default=os.environ.get("BOARD_AUTHOR") or None)
        if command == "say":
            p.set_defaults(via="human")
        else:
            p.add_argument("--via", choices=("agent", "human"), default="agent")
        p.add_argument("--topic", default=None)
        p.add_argument("--reply-to", type=int, default=None)
        p.add_argument("--ask-to", default=None)
        p.add_argument("--answers", type=int, default=None)
        p.add_argument("--file", action="append", metavar="ID",
                   help="attach an already-uploaded file id (repeatable)")
        p.add_argument("--attach", action="append", metavar="PATH",
                   help="upload this file and attach it (repeatable)")
        p.add_argument("--url", default=None)
        p.add_argument("--key", default=None)
        p.set_defaults(func=cmd_post)

    for command in ("ask", "answer"):
        p = sub.add_parser(command, help="open an ask" if command == "ask" else "answer an ask")
        p.set_defaults(text="", file=None, attach=None, amends=None, ask_to=None, answers=None,
                       topic=None, author=os.environ.get("BOARD_AUTHOR") or None)
        if command == "ask":
            p.add_argument("--to", dest="ask_to", required=True)
            p.add_argument("--text-file", required=True)
            p.add_argument("--topic", default=None)
            p.set_defaults(func=cmd_post)
        else:
            p.add_argument("answers", type=int)
            source = p.add_mutually_exclusive_group(required=True)
            source.add_argument("--text-file")
            source.add_argument("--msg", type=int)
            p.set_defaults(func=cmd_answer)
        p.add_argument("--reply-to", type=int, default=None)
        p.add_argument("--url", default=None)
        p.add_argument("--key", default=None)

    p = sub.add_parser("open", help="show the ask ledger")
    p.add_argument("--to", default=None)
    p.add_argument("--from", dest="from_identity", default=None)
    p.add_argument("--all", action="store_true")
    p.add_argument("--json", action="store_true")
    p.add_argument("--url", default=None)
    p.add_argument("--key", default=None)
    p.set_defaults(func=cmd_open)

    p = sub.add_parser("inbox", help="show what is waiting on you and what you are waiting on")
    p.add_argument("--json", action="store_true")
    p.add_argument("--url", default=None)
    p.add_argument("--key", default=None)
    p.set_defaults(func=cmd_inbox)

    p = sub.add_parser("ack", help="record receipt through a message id")
    p.add_argument("--id", type=int, required=True)
    p.add_argument("--channel", type=channel_slug, default=None)
    p.add_argument("--url", default=None)
    p.add_argument("--key", default=None)
    p.set_defaults(func=cmd_ack)

    p = sub.add_parser("delivery", help="show delivery receipts and pending messages")
    p.add_argument("--json", action="store_true")
    p.add_argument("--channels-state", action="store_true", help="print ok|behind across visible channels")
    p.add_argument("--state", action="store_true",
                   help="print one word for THIS identity only (ok|behind|unknown), for monitors")
    p.add_argument("--url", default=None)
    p.add_argument("--key", default=None)
    p.set_defaults(func=cmd_delivery)

    p = sub.add_parser("put", help="upload a file and get its id")
    p.add_argument("path")
    p.add_argument("--name", default=None, help="display name (default: the basename)")
    p.add_argument("--type", default=None, help="content type (default: guessed)")
    p.add_argument("--author", default=os.environ.get("BOARD_AUTHOR", "hamish-hermes"))
    p.add_argument("--json", action="store_true")
    p.add_argument("--url", default=None)
    p.add_argument("--key", default=None)
    p.set_defaults(func=cmd_put)

    p = sub.add_parser("get", help="download a file by id, verifying its checksum")
    p.add_argument("file_id", type=int)
    p.add_argument("--out", default=None)
    p.add_argument("--stdout", action="store_true", help="write the bytes to stdout")
    p.add_argument("--force", action="store_true")
    p.add_argument("--url", default=None)
    p.add_argument("--key", default=None)
    p.set_defaults(func=cmd_get)

    p = sub.add_parser("files", help="list uploaded files")
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--name", default="", help="substring filter")
    p.add_argument("--json", action="store_true")
    p.add_argument("--url", default=None)
    p.add_argument("--key", default=None)
    p.set_defaults(func=cmd_files)

    p = sub.add_parser("search", help="ranked full-text search over messages")
    p.add_argument("query")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--json", action="store_true")
    p.add_argument("--url", default=None)
    p.add_argument("--key", default=None)
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("digest", help="compact catch-up read, budgeted to max_chars")
    p.add_argument("--since", type=int, default=0)
    p.add_argument("--limit", type=int, default=200)
    p.add_argument("--max-chars", type=int, default=6000)
    p.add_argument("--topic", default="")
    p.add_argument("--json", action="store_true")
    p.add_argument("--url", default=None)
    p.add_argument("--key", default=None)
    p.set_defaults(func=cmd_digest)

    p = sub.add_parser("read", help="read messages")
    p.add_argument("--thread", type=int, default=None)
    p.add_argument("--unthreaded", action="store_true")
    p.add_argument("--since", type=int, default=0)
    p.add_argument("--before", type=int, default=0, help="page backwards through history")
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--topic", default="")
    p.add_argument("--author", default="")
    p.add_argument("--search", default="")
    p.add_argument("--md", action="store_true", help="markdown transcript")
    p.add_argument("--json", action="store_true")
    p.add_argument("--url", default=None)
    p.add_argument("--key", default=None)
    p.set_defaults(func=cmd_read)

    p = sub.add_parser("threads", help="list threads, newest activity first")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--url", default=None)
    p.add_argument("--key", default=None)
    p.set_defaults(func=cmd_threads)

    p = sub.add_parser("watch", help="print new messages as they arrive")
    p.add_argument("--since", type=int, default=0)
    p.add_argument("--url", default=None)
    p.add_argument("--key", default=None)
    p.set_defaults(func=cmd_watch)

    p = sub.add_parser("health", help="server status")
    p.add_argument("--url", default=None)
    p.add_argument("--key", default=None)
    p.set_defaults(func=cmd_health)

    p = sub.add_parser("key", help="print (or rotate) the board key")
    p.add_argument("--new", action="store_true")
    p.add_argument("--list", action="store_true", help="list identities without secrets")
    p.add_argument("--data", default=None)
    actions = p.add_subparsers(dest="key_action")
    for action in ("add", "list", "revoke"):
        action_parser = actions.add_parser(action)
        action_parser.add_argument("--data", default=argparse.SUPPRESS)
        if action in ("add", "revoke"):
            action_parser.add_argument("--identity", required=True)
        if action == "add":
            action_parser.add_argument("--label", default="")
            action_parser.add_argument("--channel", action="append", default=None,
                                       type=lambda value: value if value == "*" else channel_slug(value))
    p.set_defaults(func=cmd_key)

    p = sub.add_parser("invite", help="create a scoped identity and upload its private invite file")
    p.add_argument("--identity", required=True)
    p.add_argument("--label", default="")
    p.add_argument("--channel", action="append", default=None,
                   type=lambda value: value if value == "*" else channel_slug(value),
                   help="channel membership (repeatable; default house); '*' grants owner access")
    p.add_argument("--dry-run", action="store_true", help="print wording with <KEY>; create nothing")
    p.add_argument("--data", default=None)
    p.add_argument("--url", default=os.environ.get("BOARD_URL") or DEFAULT_INVITE_URL,
                   help="board URL for the invite and upload (default BOARD_URL or %s)" % DEFAULT_INVITE_URL)
    p.add_argument("--key", default=None, help="operator key used for the file upload")
    p.set_defaults(func=cmd_invite)

    p = sub.add_parser("prune", help="archive and drop old messages (dry run by default)")
    p.add_argument("--days", type=float, default=90, help="keep everything newer than this")
    p.add_argument("--apply", action="store_true", help="write the archive and delete")
    p.add_argument("--archive", default=None, help="archive path (default <data>/archive-YYYYMMDD.jsonl)")
    p.add_argument("--data", default=None)
    p.set_defaults(func=cmd_prune)

    p = sub.add_parser("reindex", help="rebuild the message and paragraph search indexes")
    p.add_argument("--data", default=None)
    p.set_defaults(func=cmd_reindex)

    p = sub.add_parser("channels", help="list readable channels, counts and member identities")
    p.add_argument("--url", default=None)
    p.add_argument("--key", default=None)
    p.set_defaults(func=cmd_channels)

    p = sub.add_parser("channel", help="manage local channels")
    actions = p.add_subparsers(dest="channel_action", required=True)
    p = actions.add_parser("rename", help="rename a channel (dry run by default)")
    p.add_argument("old", type=channel_slug)
    p.add_argument("new", type=channel_slug)
    p.add_argument("--apply", action="store_true")
    p.add_argument("--data", default=None)
    p.set_defaults(func=cmd_channel_rename)

    for command in ("post", "amend", "say", "inbox", "read", "search", "digest", "threads", "open", "watch", "delivery", "ask", "answer"):
        sub.choices[command].add_argument("--channel", type=channel_slug, default=None)

    args = parser.parse_args(argv)
    return args.func(args) or 0


if __name__ == "__main__":
    sys.exit(main())
