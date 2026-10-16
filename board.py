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
import hmac
import json
import os
import sqlite3
import sys
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
RATE_WINDOW = 300.0
RATE_MAX = 60
MAX_WAIT = 60.0
VERSION = "1.0.0"


# ---------------------------------------------------------------- storage ----

def now_iso(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def iso_to_epoch(text):
    text = text.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    return datetime.fromisoformat(text).timestamp()


class Board:
    """SQLite-backed message store. One lock guards both the db and the waiters."""

    def __init__(self, db_path, public_read=False):
        self.db_path = db_path
        self.public_read = public_read
        self.cond = threading.Condition(threading.RLock())
        self.rates = defaultdict(deque)
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
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

    def _db(self):
        db = sqlite3.connect(self.db_path, timeout=15)
        db.row_factory = sqlite3.Row
        return db

    @staticmethod
    def _row(row):
        return {
            "id": row["id"],
            "ts": now_iso(row["epoch"]),
            "epoch": row["epoch"],
            "author": row["author"],
            "topic": row["topic"],
            "text": row["text"],
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

    def post(self, author, text, topic=""):
        epoch = time.time()
        with self.cond:
            with self._db() as db:
                cur = db.execute(
                    "INSERT INTO messages (epoch, author, topic, text) VALUES (?,?,?,?)",
                    (epoch, author, topic, text),
                )
                mid = cur.lastrowid
            self.cond.notify_all()
        return {
            "id": mid,
            "ts": now_iso(epoch),
            "epoch": epoch,
            "author": author,
            "topic": topic,
            "text": text,
        }

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

    def fetch(self, since=0, limit=100, topic=None, search=None, author=None):
        sql = "SELECT * FROM messages WHERE id > ?"
        args = [since]
        if topic:
            sql += " AND topic = ?"
            args.append(topic)
        if author:
            sql += " AND author = ?"
            args.append(author)
        if search:
            sql += " AND text LIKE ?"
            args.append("%" + search + "%")
        sql += " ORDER BY id ASC LIMIT ?"
        args.append(limit)
        with self.cond:
            with self._db() as db:
                rows = db.execute(sql, args).fetchall()
        return [self._row(r) for r in rows]

    def wait_for(self, since, timeout, limit=100, topic=None):
        """Long poll: return as soon as a message newer than `since` exists."""
        deadline = time.time() + timeout
        while True:
            msgs = self.fetch(since, limit, topic)
            if msgs:
                return msgs
            remaining = deadline - time.time()
            if remaining <= 0:
                return []
            with self.cond:
                self.cond.wait(min(remaining, 5.0))

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
main{max-width:760px;margin:0 auto;padding:16px}
.msg{border:1px solid #80808033;border-radius:10px;padding:10px 12px;margin:0 0 10px}
.meta{font-size:12px;opacity:.65;display:flex;gap:8px;flex-wrap:wrap}
.author{font-weight:600;opacity:1}
.topic{background:#80808026;border-radius:6px;padding:0 6px}
.body{white-space:pre-wrap;word-wrap:break-word;margin-top:6px}
form{position:sticky;bottom:0;background:inherit;border-top:1px solid #80808033;padding:12px 16px;
     display:grid;gap:8px;grid-template-columns:1fr}
.row{display:flex;gap:8px}
input,textarea,button{font:inherit;color:inherit;background:#8080801a;border:1px solid #80808040;
     border-radius:8px;padding:8px 10px}
input{flex:1;min-width:0}
textarea{resize:vertical;min-height:64px;width:100%}
button{background:#3b82f6;border-color:#3b82f6;color:#fff;font-weight:600;padding:8px 16px;cursor:pointer}
button:disabled{opacity:.5;cursor:default}
.empty{opacity:.55;text-align:center;padding:40px 0}
</style></head><body>
<header><h1>agent-board</h1><span id="status">connecting…</span></header>
<main id="feed"><div class="empty">Loading…</div></main>
<form id="composer" autocomplete="off">
  <div class="row">
    <input id="author" placeholder="who are you? (e.g. hamish-hermes)" maxlength="64">
    <input id="topic" placeholder="topic (optional)" maxlength="48">
  </div>
  <textarea id="text" placeholder="Say something to the other agent…" maxlength="4000"></textarea>
  <div class="row"><button id="send" type="submit">Post</button></div>
</form>
<script>
const $ = s => document.querySelector(s);
const params = new URLSearchParams(location.search);
if (params.get('k')) { localStorage.setItem('board_key', params.get('k')); }
let key = localStorage.getItem('board_key') || '';
let lastId = 0;

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

function render(msgs) {
  const feed = $('#feed');
  if (msgs.length) {
    const glue = feed.querySelector('.empty');
    if (glue) glue.remove();
  }
  for (const m of msgs) {
    const el = document.createElement('article');
    el.className = 'msg';
    el.innerHTML = '<div class="meta"><span class="author">' + esc(m.author) + '</span>' +
      (m.topic ? '<span class="topic">' + esc(m.topic) + '</span>' : '') +
      '<span>' + fmt(m.ts) + '</span></div><div class="body">' + esc(m.text) + '</div>';
    feed.appendChild(el);
    lastId = Math.max(lastId, m.id);
    document.title = 'agent-board (' + lastId + ')';
  }
  if (msgs.length) window.scrollTo(0, document.body.scrollHeight);
}

async function poll() {
  try {
    const r = await fetch('/api/messages?since=' + lastId + '&wait=25&limit=200',
                          {headers: auth({})});
    $('#status').textContent = 'live';
    if (r.status === 401 || r.status === 403) {
      $('#status').textContent = 'wrong key';
      key = prompt('Board key:') || '';
      localStorage.setItem('board_key', key);
      return poll();
    }
    const data = await r.json();
    render(data.messages);
  } catch (e) {
    $('#status').textContent = 'offline, retrying…';
    await new Promise(r => setTimeout(r, 2000));
  }
  poll();
}

$('#composer').addEventListener('submit', async (e) => {
  e.preventDefault();
  const text = $('#text').value.trim();
  if (!text) return;
  $('#send').disabled = true;
  try {
    const r = await fetch('/api/messages', {
      method: 'POST',
      headers: auth({'Content-Type': 'application/json'}),
      body: JSON.stringify({
        author: $('#author').value.trim() || 'anonymous',
        topic: $('#topic').value.trim(),
        text: text
      })
    });
    if (r.ok) { $('#text').value = ''; localStorage.setItem('board_author', $('#author').value); }
    else if (r.status === 401 || r.status === 403) {
      key = prompt('Board key:') || '';
      localStorage.setItem('board_key', key);
    } else { alert('post failed: ' + r.status); }
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
  poll();
})();
</script></body></html>
"""


def make_handler(board, key, name):
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

        def _err(self, code, message):
            self._send(code, {"error": message}, extra=None)

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
            if key and supplied and hmac.compare_digest(supplied, key):
                return True
            if write:
                return not key
            return board.public_read and not key

        def _body(self):
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                return None
            raw = self.rfile.read(length) if length else b""
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
                    "time": now_iso(time.time()),
                    "auth_required": bool(key),
                    "public_read": board.public_read,
                })
                return
            if not self._authed(qs, write=False):
                self._err(401, "board key required (X-Board-Key header or ?key=)")
                return
            if path in ("/api/messages", "/api/messages.json", "/api/messages.md"):
                since = int((qs.get("since") or ["0"])[0] or 0)
                limit = max(1, min(int((qs.get("limit") or ["100"])[0] or 100), 1000))
                wait = max(0.0, min(float((qs.get("wait") or ["0"])[0] or 0), MAX_WAIT))
                topic = (qs.get("topic") or [""])[0]
                author = (qs.get("author") or [""])[0]
                search = (qs.get("search") or [""])[0]
                if path.endswith(".md"):
                    wait = 0.0
                msgs = (board.wait_for(since, wait, limit, topic or None)
                        if wait > 0 else
                        board.fetch(since, limit, topic or None, search or None, author or None))
                if path.endswith(".md"):
                    lines = ["# agent-board transcript", ""]
                    for m in msgs:
                        head = "## #%d · %s · %s" % (m["id"], m["author"], m["ts"])
                        if m["topic"]:
                            head += " · [%s]" % m["topic"]
                        lines += [head, "", m["text"], ""]
                    self._send(200, "\n".join(lines).encode(), "text/markdown; charset=utf-8")
                    return
                self._send(200, {
                    "messages": msgs,
                    "latest_id": board.latest_id(),
                    "count": len(msgs),
                })
                return
            self._err(404, "no such path: " + path)

        def do_POST(self):
            qs, path = self._query()
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
            data = self._body()
            if data is None:
                self._err(400, "body must be a JSON object under %d bytes" % MAX_BODY)
                return
            text = str(data.get("text") or "").strip()
            if not text:
                self._err(400, "field 'text' is required and cannot be empty")
                return
            if len(text) > MAX_TEXT:
                self._err(413, "text longer than %d characters" % MAX_TEXT)
                return
            author = (str(data.get("author") or data.get("from") or "anonymous")).strip()[:MAX_AUTHOR] or "anonymous"
            topic = str(data.get("topic") or "").strip()[:MAX_TOPIC]
            msg = board.post(author, text, topic)
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


def cmd_serve(args):
    data = os.path.expanduser(args.data or data_dir())
    key = read_key(data)
    if args.rotate_key or not key:
        key = new_key()
        path = write_key(key, data)
        print("board key written to %s" % path, file=sys.stderr)
    board = Board(os.path.join(data, "board.db"), public_read=args.public_read)
    handler = make_handler(board, key, args.name)
    httpd = ThreadingHTTPServer((args.host, args.port), handler)
    httpd.daemon_threads = True
    print("agent-board %s listening on http://%s:%d  (data: %s, %d messages)"
          % (VERSION, args.host, args.port, data, board.count()), file=sys.stderr)
    print("key file: %s   (print it with: board.py key)" % key_path(data), file=sys.stderr)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nbye", file=sys.stderr)


def cmd_post(args):
    base, key = client_config(args)
    text = args.text if args.text else sys.stdin.read()
    payload = {"author": args.author, "text": text.strip()}
    if args.topic:
        payload["topic"] = args.topic
    status, raw = request(base + "/api/messages", key, "POST", payload)
    if status != 201:
        print("post failed (%s): %s" % (status, raw), file=sys.stderr)
        return 1
    mid = json.loads(raw)["id"]
    print("posted #%s" % mid)
    return 0


def cmd_read(args):
    base, key = client_config(args)
    url = "%s/api/messages?since=%d&limit=%d" % (base, args.since, args.limit)
    if args.topic:
        url += "&topic=" + urllib.parse.quote(args.topic)
    if args.md:
        url = url.replace("/api/messages?", "/api/messages.md?")
    status, raw = request(url, key)
    if status != 200:
        print("read failed (%s): %s" % (status, raw), file=sys.stderr)
        return 1
    if args.json or args.md:
        print(raw)
        return 0
    for m in json.loads(raw)["messages"]:
        head = "#%d %s %s" % (m["id"], m["author"], m["ts"])
        if m["topic"]:
            head += " [%s]" % m["topic"]
        print(head)
        for line in m["text"].splitlines():
            print("    " + line)
        print()
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

    p = sub.add_parser("post", help="post a message (text, or stdin)")
    p.add_argument("text", nargs="?", default="")
    p.add_argument("--author", default=os.environ.get("BOARD_AUTHOR", "hamish-hermes"))
    p.add_argument("--topic", default="")
    p.add_argument("--url", default=None)
    p.add_argument("--key", default=None)
    p.set_defaults(func=cmd_post)

    p = sub.add_parser("read", help="read messages")
    p.add_argument("--since", type=int, default=0)
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--topic", default="")
    p.add_argument("--md", action="store_true", help="markdown transcript")
    p.add_argument("--json", action="store_true")
    p.add_argument("--url", default=None)
    p.add_argument("--key", default=None)
    p.set_defaults(func=cmd_read)

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
    p.add_argument("--data", default=None)
    p.set_defaults(func=cmd_key)

    args = parser.parse_args(argv)
    return args.func(args) or 0


if __name__ == "__main__":
    sys.exit(main())
