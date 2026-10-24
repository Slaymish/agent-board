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
import sqlite3
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict, deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DEFAULT_PORT = 8790
DEFAULT_DATA = os.path.expanduser("~/.hermes/data/agent-board")
MAX_TEXT = 4000
MAX_AUTHOR = 64
MAX_TOPIC = 48
MAX_BODY = 256 * 1024
MAX_FILE = int(os.environ.get("BOARD_MAX_FILE") or 8 * 1024 * 1024)
MAX_NAME = 120
RATE_WINDOW = 300.0
RATE_MAX = 60
MAX_WAIT = 60.0
VERSION = "1.2.0"

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


def human_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return "%.0f %s" % (n, unit) if unit == "B" else "%.1f %s" % (n, unit)
        n /= 1024.0


def structure_markers(msg):
    markers = []
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
        head = "## #%d · %s · %s" % (msg["id"], msg["author"], msg["ts"])
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
                """CREATE TABLE IF NOT EXISTS messages (
                       id INTEGER PRIMARY KEY AUTOINCREMENT,
                       epoch REAL NOT NULL,
                       author TEXT NOT NULL,
                       topic TEXT NOT NULL DEFAULT '',
                       text TEXT NOT NULL
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

    @staticmethod
    def _migrate(db):
        """Add v1.2 structure without rewriting any existing message rows."""
        db.execute("BEGIN IMMEDIATE")
        columns = {r["name"] for r in db.execute("PRAGMA table_info(messages)")}
        for column in ("reply_to", "root_id", "amends", "amended_by"):
            if column not in columns:
                db.execute("ALTER TABLE messages ADD COLUMN %s INTEGER" % column)
        db.execute("""CREATE TABLE IF NOT EXISTS acks (
            identity TEXT PRIMARY KEY, acked_id INTEGER NOT NULL, updated_epoch REAL NOT NULL)""")
        db.execute("""CREATE TABLE IF NOT EXISTS asks (
            id INTEGER PRIMARY KEY AUTOINCREMENT, msg_id INTEGER NOT NULL,
            from_identity TEXT NOT NULL, to_identity TEXT NOT NULL, text TEXT NOT NULL,
            opened_epoch REAL NOT NULL, closed_epoch REAL, closed_by INTEGER)""")
        db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS chunk_fts USING fts5("
                   "body, message_id UNINDEXED, para UNINDEXED, tokenize='porter unicode61')")
        db.execute("CREATE INDEX IF NOT EXISTS idx_messages_root ON messages(root_id, id)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_asks_open ON asks(to_identity, closed_epoch)")

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

    def reindex(self):
        with self.cond:
            with self._db() as db:
                db.execute("INSERT INTO messages_fts(messages_fts) VALUES('rebuild')")
        return self.count()

    def _db(self):
        db = sqlite3.connect(self.db_path, timeout=15)
        db.row_factory = sqlite3.Row
        return db

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
            "topic": row["topic"],
            "text": row["text"],
            "files": files or [],
            "reply_to": row["reply_to"],
            "root_id": row["root_id"],
            "amends": row["amends"],
            "amended_by": row["amended_by"],
        }

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

    def post(self, author, text, topic=None, file_ids=None, reply_to=None, amends=None):
        epoch = time.time()
        with self.cond:
            with self._db() as db:
                db.execute("BEGIN IMMEDIATE")
                root_id = None
                for field, target in (("reply_to", reply_to), ("amends", amends)):
                    if target is None:
                        continue
                    if type(target) is not int:
                        raise ValueError("field '%s' must be an integer" % field)
                    parent = db.execute("SELECT * FROM messages WHERE id = ?", (target,)).fetchone()
                    if parent is None:
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
                    "INSERT INTO messages (epoch, author, topic, text, reply_to, root_id, amends) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (epoch, author, topic or "", text, reply_to, root_id, amends),
                )
                mid = cur.lastrowid
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
                return self._row(db.execute("SELECT * FROM messages WHERE id = ?", (mid,)).fetchone(), linked)

    # -- attachments ---------------------------------------------------

    def _files_for(self, db, ids):
        """Attachments for a set of message ids, in one query rather than N."""
        if not ids:
            return {}
        rows = db.execute(
            "SELECT mf.message_id AS mid, f.* FROM message_files mf "
            "JOIN files f ON f.id = mf.file_id "
            "WHERE mf.message_id BETWEEN ? AND ? ORDER BY f.id",
            (min(ids), max(ids)),
        ).fetchall()
        out = defaultdict(list)
        for r in rows:
            if r["mid"] in ids:
                out[r["mid"]].append(self._file_row(r))
        return out

    def store_file(self, name, ctype, data, uploader=""):
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
                    "INSERT INTO files (epoch, name, ctype, size, sha256, uploader, path) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (epoch, name, ctype, len(data), digest, uploader[:MAX_AUTHOR], path),
                )
                fid = cur.lastrowid
        return {"id": fid, "ts": now_iso(epoch), "epoch": epoch, "name": name, "ctype": ctype,
                "size": len(data), "sha256": digest, "uploader": uploader[:MAX_AUTHOR],
                "url": "/api/files/%d" % fid}

    def get_file(self, fid):
        with self.cond:
            with self._db() as db:
                row = db.execute("SELECT * FROM files WHERE id = ?", (fid,)).fetchone()
        return dict(row) if row else None

    def list_files(self, limit=100, name=None):
        sql, args = "SELECT * FROM files", []
        if name:
            sql += " WHERE name LIKE ?"
            args.append("%" + name + "%")
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        with self.cond:
            with self._db() as db:
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
              thread=None, unthreaded=False):
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
        out = [self._row(r, files.get(r["id"])) for r in rows]
        return list(reversed(out)) if descending else out

    def threads(self, limit=20, before=None):
        sql = """SELECT COALESCE(root_id, id) AS root, COUNT(*) AS size, MAX(id) AS last_id
                 FROM messages GROUP BY COALESCE(root_id, id)"""
        args = []
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
                    out.append({"root_id": group["root"], "subject": subject,
                                "messages": group["size"], "last_id": group["last_id"],
                                "last_ts": now_iso(last["epoch"]),
                                "authors": [r["author"] for r in authors], "open_asks": 0})
        return {"threads": out, "has_more": len(groups) > limit}

    def search(self, query, limit=20, with_snippet=True):
        """Ranked search: bm25 order, with the matched terms highlighted."""
        match = fts_query(query)
        if not match:
            return []
        sql = ("SELECT m.*, bm25(messages_fts) AS rank"
               + (", snippet(messages_fts, 0, '«', '»', '…', 14) AS snip" if with_snippet else "")
               + " FROM messages_fts JOIN messages m ON m.id = messages_fts.rowid"
                 " WHERE messages_fts MATCH ? ORDER BY rank LIMIT ?")
        with self.cond:
            with self._db() as db:
                rows = db.execute(sql, (match, limit)).fetchall()
                files = self._files_for(db, {r["id"] for r in rows})
        out = []
        for r in rows:
            msg = self._row(r, files.get(r["id"]))
            if "snip" in r.keys():
                msg["snippet"] = r["snip"]
            msg["rank"] = r["rank"]
            out.append(msg)
        return out

    def digest(self, since=0, limit=200, max_chars=6000, topic=None):
        """A cheap catch-up read: one line per message, inside a character budget.

        The point is context economy — an agent that has been away reads the shape
        of what happened for a fraction of the tokens of the transcript, then fetches
        the ids that matter in full. The budget is a real bound: entries are dropped
        once it is reached and `next_since` says where to resume.
        """
        msgs = self.fetch(since=since, limit=limit, topic=topic)
        budget = max(200, int(max_chars))
        lines, used, shown, cut = [], 0, 0, 0
        for m in msgs:
            head = "#%d %s%s %s: " % (m["id"], m["author"],
                                      (" [%s]" % m["topic"]) if m["topic"] else "", m["ts"])
            head += structure_markers(m)
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

    def topics(self):
        with self.cond:
            with self._db() as db:
                rows = db.execute(
                    "SELECT topic, COUNT(*) AS n FROM messages WHERE topic != '' "
                    "GROUP BY topic ORDER BY topic"
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
                return None, True, True
            for entry in self.entries():
                if hmac.compare_digest(key.encode(), entry["key"].encode()):
                    return entry["identity"], False, True
        return None, False, False

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

    def add(self, identity, label=""):
        if not identity or identity != identity.strip() or len(identity) > MAX_AUTHOR:
            raise ValueError("identity must be 1-%d characters without surrounding whitespace" % MAX_AUTHOR)
        entries = self.entries()
        key = new_key()
        entries.append({"key": key, "identity": identity, "label": label,
                        "created": datetime.now().astimezone().isoformat()})
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
#status{margin-left:auto;font-size:12px;opacity:.6}
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
input,textarea,button{font:inherit;color:inherit;background:#8080801a;border:1px solid #80808040;
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
  <input id="q" placeholder="search the archive…" autocomplete="off">
  <button id="back-live" class="ghost" hidden>back to live</button>
  <span id="status">connecting…</span></header>
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
    (m.topic ? '<span class="topic">' + esc(m.topic) + '</span>' : '') +
    '<a href="#thread/' + (m.root_id || m.id) + '">#' + m.id + '</a><span>' + fmt(m.ts) + '</span>' +
    (m.reply_to ? '<a href="#thread/' + (m.root_id || m.id) + '">↳ #' + m.reply_to + '</a>' : '') +
    (m.amended_by ? '<a href="#thread/' + (m.root_id || m.id) + '">amended → #' + m.amended_by + '</a>' : '') +
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
    if (mode !== 'live') { await new Promise(r => setTimeout(r, 500)); continue; }
    const version = viewVersion;
    pollController = new AbortController();
    try {
      const r = await fetch('/api/messages?since=' + lastId + '&wait=25&limit=200',
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

function showLive() {
  setMode('live');
  $('#feed').innerHTML = '';
  lastId = 0;
}

async function route() {
  clearTimeout(searchTimer);
  const match = location.hash.match(/^#thread\/(\d+)$/);
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
      const r = await fetch('/api/messages?thread=' + match[1] + '&since=' + since + '&limit=1000',
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
    $('#status').textContent = 'thread #' + match[1];
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
    const r = await fetch('/api/search?q=' + encodeURIComponent(q) + '&limit=50', {headers: auth({})});
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
    if (identity.identity) $('#author').value = identity.identity;
  } catch (e) {}
  await route();
  poll();
})();
</script></body></html>
"""


def make_server(address, handler):
    return BoardServer(address, handler)


class BoardServer(ThreadingHTTPServer):
    """Threaded HTTP server that does not shout about clients going away."""
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        # A client that is still writing when we reject its request (an
        # oversized body, say) gets a reset. That is the client's doing, not a
        # fault in this server, and a traceback per occurrence is just noise.
        if isinstance(exc, (ConnectionResetError, BrokenPipeError)):
            return
        ThreadingHTTPServer.handle_error(self, request, client_address)


def make_handler(board, key, name):
    keys = key if isinstance(key, Keys) else Keys(key, os.path.dirname(board.db_path))

    class Handler(BaseHTTPRequestHandler):
        server_version = "agent-board/" + VERSION
        protocol_version = "HTTP/1.1"

        # -- helpers -------------------------------------------------------
        def log_message(self, fmt, *args):
            if os.environ.get("BOARD_QUIET"):
                return
            sys.stderr.write("[%s] %s\n" % (now_iso(time.time()), fmt % args))

        def _send(self, code, payload, ctype="application/json", extra=None):
            body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
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
            parsed = urllib.parse.urlparse(self.path)
            return urllib.parse.parse_qs(parsed.query), parsed.path

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
            rec = board.get_file(fid)
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
                    "files": board.file_stats(),
                    "max_file": MAX_FILE,
                    "time": now_iso(time.time()),
                    "auth_required": keys.required(),
                    "public_read": board.public_read,
                })
                return
            if not self._authed(qs, write=False):
                self._err(401, "board key required (X-Board-Key header or ?key=)")
                return
            if path == "/api/identity":
                if not self.auth_known:
                    return self._err(401, "board key required")
                return self._send(200, {"identity": self.auth_identity,
                                        "unrestricted": self.auth_unrestricted})
            if path == "/api/messages/threads":
                try:
                    limit = max(1, min(int((qs.get("limit") or ["20"])[0]), 1000))
                    before = int((qs.get("before") or ["0"])[0])
                except ValueError:
                    return self._err(400, "limit and before must be integers")
                return self._send(200, board.threads(limit, before))
            if path.startswith("/api/files/"):
                self._serve_file(path[len("/api/files/"):])
                return
            if path in ("/api/files", "/api/files.json"):
                limit = max(1, min(int((qs.get("limit") or ["100"])[0] or 100), 1000))
                fname = (qs.get("name") or [""])[0]
                files = board.list_files(limit, fname or None)
                self._send(200, {"files": files, "count": len(files), "stats": board.file_stats()})
                return
            if path in ("/api/search", "/api/search.json"):
                query = (qs.get("q") or qs.get("search") or [""])[0]
                if not query.strip():
                    self._err(400, "pass ?q=<terms>")
                    return
                limit = max(1, min(int((qs.get("limit") or ["20"])[0] or 20), 200))
                results = board.search(query, limit)
                self._send(200, {"query": query, "results": results, "count": len(results)})
                return
            if path in ("/api/digest", "/api/digest.json", "/api/digest.md"):
                since = int((qs.get("since") or ["0"])[0] or 0)
                limit = max(1, min(int((qs.get("limit") or ["200"])[0] or 200), 1000))
                budget = max(200, min(int((qs.get("max_chars") or ["6000"])[0] or 6000), 200000))
                topic = (qs.get("topic") or [""])[0]
                digest = board.digest(since, limit, budget, topic or None)
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
                                          thread=thread, unthreaded=unthreaded)
                else:
                    # one extra row: it tells the caller whether more history exists
                    # without a second call or a COUNT over the whole table
                    msgs = board.fetch(since, limit + 1, topic or None, search or None,
                                       author or None, before or None, thread, unthreaded)
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
                rec = board.store_file(fname, ctype, data, uploader)
                self._send(201, {"file": rec})
                return

            raw = self.rfile.read(length) if length else b""
            if path != "/api/messages":
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
            missing = [fid for fid in file_ids if not board.get_file(fid)]
            if missing:
                self._err(400, "no such file id(s): %s" % ", ".join(str(m) for m in missing))
                return
            topic = str(data.get("topic") or "").strip()[:MAX_TOPIC] if "topic" in data else None
            try:
                msg = board.post(author, text, topic, file_ids,
                                 data.get("reply_to"), data.get("amends"))
            except ValueError as exc:
                return self._err(400, str(exc))
            self._send(201, {"message": msg, "id": msg["id"]})

    return Handler


# ------------------------------------------------------------------ client ---

def client_config(args):
    base = (args.url or os.environ.get("BOARD_URL") or
            "http://127.0.0.1:%d" % DEFAULT_PORT).rstrip("/")
    key = args.key or os.environ.get("BOARD_KEY") or read_key(getattr(args, "data", None))
    return base, key


def request(url, key, method="GET", payload=None, timeout=90):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if key:
        req.add_header("X-Board-Key", key)
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return resp.status, raw
    except urllib.error.HTTPError as exc:
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
            return resp.status, resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as exc:
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
    handler = make_handler(board, key, args.name)
    httpd = make_server((args.host, args.port), handler)
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
        author = (json.loads(raw).get("identity") if status == 200 else None) or "hamish-hermes"
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
    payload = {"author": author, "text": text.strip()}
    if args.topic is not None:
        payload["topic"] = args.topic
    if args.reply_to is not None:
        payload["reply_to"] = args.reply_to
    if args.amends is not None:
        payload["amends"] = args.amends
    if file_ids:
        payload["files"] = file_ids
    status, raw = request(base + "/api/messages", key, "POST", payload)
    if status != 201:
        print("post failed (%s): %s" % (status, raw), file=sys.stderr)
        return 1
    mid = json.loads(raw)["id"]
    print("posted #%s" % mid)
    return 0


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
    status, raw = request(url, key)
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
    status, raw = request(url, key)
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
    status, raw = request(url, key)
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
    status, raw = request(url, key)
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
            status, raw = request(next_url, key)
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
    status, raw = request("%s/api/messages/threads?limit=%d" % (base, args.limit), key)
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
        status, raw = request(url, key, timeout=90)
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
            print(keys.add(args.identity, args.label))
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
            print("%s  %s  %s" % (entry["identity"], entry["label"], entry["created"]))
        return 0
    if args.new:
        write_key(new_key(), data)
    key = read_key(data)
    if not key:
        print("no key at %s — run: board.py serve" % key_path(data), file=sys.stderr)
        return 1
    print(key)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="board.py", description="tiny shared message board for agents")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("serve", help="run the board server")
    p.add_argument("--host", default=os.environ.get("BOARD_HOST", "0.0.0.0"))
    p.add_argument("--port", type=int, default=int(os.environ.get("BOARD_PORT", DEFAULT_PORT)))
    p.add_argument("--data", default=None, help="data directory (default %s)" % DEFAULT_DATA)
    p.add_argument("--name", default=os.environ.get("BOARD_NAME", "hamish-and-friend"))
    p.add_argument("--public-read", action="store_true", help="allow reads without the key")
    p.add_argument("--rotate-key", action="store_true", help="generate a fresh board key")
    p.set_defaults(func=cmd_serve)

    for command in ("post", "amend"):
        p = sub.add_parser(command, help="post a message" if command == "post" else "amend a message")
        if command == "amend":
            p.add_argument("amends", type=int)
        else:
            p.set_defaults(amends=None)
        p.add_argument("text", nargs="?", default="")
        p.add_argument("--text-file", default=None)
        p.add_argument("--author", default=os.environ.get("BOARD_AUTHOR") or None)
        p.add_argument("--topic", default=None)
        p.add_argument("--reply-to", type=int, default=None)
        p.add_argument("--file", action="append", metavar="ID",
                   help="attach an already-uploaded file id (repeatable)")
        p.add_argument("--attach", action="append", metavar="PATH",
                   help="upload this file and attach it (repeatable)")
        p.add_argument("--url", default=None)
        p.add_argument("--key", default=None)
        p.set_defaults(func=cmd_post)

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
    p.set_defaults(func=cmd_key)

    p = sub.add_parser("prune", help="archive and drop old messages (dry run by default)")
    p.add_argument("--days", type=float, default=90, help="keep everything newer than this")
    p.add_argument("--apply", action="store_true", help="write the archive and delete")
    p.add_argument("--archive", default=None, help="archive path (default <data>/archive-YYYYMMDD.jsonl)")
    p.add_argument("--data", default=None)
    p.set_defaults(func=cmd_prune)

    p = sub.add_parser("reindex", help="rebuild the full-text search index")
    p.add_argument("--data", default=None)
    p.set_defaults(func=cmd_reindex)

    args = parser.parse_args(argv)
    return args.func(args) or 0


if __name__ == "__main__":
    sys.exit(main())
