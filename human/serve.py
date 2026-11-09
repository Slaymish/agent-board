#!/usr/bin/env python3
"""The human channel — the two people's own side of the board.

One person types here and the words land in their own writer file as
``via: human`` in the ``humans`` room. Nothing rewrites them on the way, which is
the one thing a message relayed through an agent cannot offer: on the phone,
``via: human`` is the agent's claim about your words. Here the client that appends
the record is the hand that typed it.

Run it on the machine that owns the writer file, one instance per person::

    BOARD_DIR=~/Developer/agent-memory/board BOARD_HUMAN=hamish \\
        python3 human/serve.py --port 8767

The other person runs the same file against their own checkout. Rooms are write
policy on one shared log, not an access boundary: an agent may read ``humans`` when
its human asks, and never posts into it.

Reading is the local checkout, refreshed by a pull every ``--pull`` seconds (0 turns
it off). Posting appends under a file lock, then commits and pushes only this
person's file. No dependencies: the standard library and ``board_log.py`` next to it.
"""
from __future__ import annotations

import argparse
import hmac
import json
import os
import secrets
import subprocess
import sys
import threading
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import board_log  # noqa: E402  (the one write path; do not duplicate id/locking logic)

ROOM = "humans"
COOKIE = "hc"
MAX_TEXT = board_log.MAX_TEXT

# The page is deliberately one file with no assets and no framework: it has to open on
# a phone over the tailnet, and a board that needs a build has a second way to be down.
PAGE = """<!doctype html>
<html lang="en-NZ">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="hamish + will">
<meta name="theme-color" content="#111111" media="(prefers-color-scheme: dark)">
<meta name="theme-color" content="#f6f3ec" media="(prefers-color-scheme: light)">
<title>hamish + will</title>
<style>
  :root {
    --ink: #171614; --quiet: #6d675c; --paper: #f6f3ec; --rule: #ddd6c7;
    --mine: #1f4d3a; --theirs: #6b3f2a; --warn: #8a2d22;
    --serif: "Iowan Old Style", "Palatino Linotype", Palatino, Charter, Georgia, serif;
    --sans: ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif;
    --mono: ui-monospace, "SF Mono", Menlo, Consolas, monospace;
  }
  @media (prefers-color-scheme: dark) {
    :root { --ink: #ece7dc; --quiet: #98917f; --paper: #141312; --rule: #2e2b27;
            --mine: #8bc7a6; --theirs: #d9a47f; --warn: #e08a7c; }
  }
  * { box-sizing: border-box; }
  html, body { margin: 0; background: var(--paper); color: var(--ink); }
  body {
    font-family: var(--serif); font-size: 17px; line-height: 1.5;
    min-height: 100dvh; display: flex; flex-direction: column;
    -webkit-text-size-adjust: 100%;
  }
  header {
    position: sticky; top: 0; z-index: 2; background: var(--paper);
    border-bottom: 1px solid var(--rule);
    padding: max(12px, env(safe-area-inset-top)) 18px 10px;
    display: flex; align-items: baseline; gap: 10px;
  }
  h1 {
    font-family: var(--sans); font-size: 13px; letter-spacing: .14em;
    text-transform: lowercase; font-weight: 600; margin: 0;
  }
  .who { font-family: var(--mono); font-size: 11px; color: var(--quiet); margin-left: auto;
         letter-spacing: .02em; }
  .dot { width: 7px; height: 7px; border-radius: 50%; background: var(--quiet);
         align-self: center; flex: none; }
  .dot.live { background: var(--mine); }
  .dot.down { background: var(--warn); }
  main { flex: 1; padding: 8px 18px 20px; }
  .msg { padding: 14px 0 0; display: grid; grid-template-columns: 3px 1fr; gap: 12px; }
  .msg + .msg { border-top: 1px solid var(--rule); margin-top: 14px; }
  .msg.same { border-top: 0; margin-top: 0; padding-top: 8px; }
  .day {
    font-family: var(--mono); font-size: 11px; letter-spacing: .08em;
    text-transform: uppercase; color: var(--quiet);
    margin: 22px 0 4px; padding-bottom: 6px; border-bottom: 1px solid var(--rule);
  }
  .day:first-child { margin-top: 8px; }
  .msg .bar { background: var(--theirs); border-radius: 2px; }
  .msg.me .bar { background: var(--mine); }
  .meta {
    font-family: var(--sans); font-size: 11px; letter-spacing: .1em;
    text-transform: lowercase; color: var(--quiet);
    display: flex; gap: 8px; align-items: baseline;
  }
  .meta time { font-family: var(--mono); letter-spacing: 0; }
  .text { white-space: pre-wrap; overflow-wrap: anywhere; margin-top: 3px; }
  .text a { color: inherit; text-decoration-thickness: 1px; text-underline-offset: 2px; }
  #empty { color: var(--quiet); font-size: 15px; padding: 28px 0; }
  form {
    position: sticky; bottom: 0; background: var(--paper);
    border-top: 1px solid var(--rule);
    padding: 10px 14px calc(10px + env(safe-area-inset-bottom));
    display: flex; gap: 10px; align-items: flex-end;
  }
  textarea {
    flex: 1; resize: none; min-height: 44px; max-height: 40dvh;
    font: inherit; color: inherit; background: transparent;
    border: 1px solid var(--rule); border-radius: 8px;
    padding: 10px 12px; line-height: 1.4;
  }
  textarea:focus { outline: 2px solid var(--mine); outline-offset: -1px; }
  button {
    font-family: var(--sans); font-size: 12px; letter-spacing: .12em;
    text-transform: lowercase; font-weight: 600;
    border: 1px solid var(--ink); background: var(--ink); color: var(--paper);
    border-radius: 8px; padding: 0 16px; height: 44px; cursor: pointer;
  }
  button[disabled] { opacity: .45; cursor: default; }
  #status { font-family: var(--mono); font-size: 11px; color: var(--quiet);
            padding: 0 18px; min-height: 16px; }
  #status.bad { color: var(--warn); }
</style>
</head>
<body>
<header>
  <span class="dot" id="dot"></span>
  <h1 id="title">hamish + will</h1>
  <span class="who" id="who">humans</span>
</header>
<main id="thread"><p id="empty">Nothing here yet.</p></main>
<div id="status"></div>
<form id="composer">
  <textarea id="text" rows="1" maxlength="4000" placeholder="write to will"
            autocomplete="off" enterkeyhint="enter" aria-label="message"></textarea>
  <button id="send" type="submit">send</button>
</form>
<script>
const state = { me: "?", peer: "?", messages: [], sending: false };
const $ = (id) => document.getElementById(id);

function stamp(ts) {
  const d = new Date(ts);
  if (isNaN(d)) return ts;
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function dayLabel(ts) {
  const d = new Date(ts);
  if (isNaN(d)) return "";
  const now = new Date();
  const day = (x) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
  const diff = Math.round((day(now) - day(d)) / 86400000);
  if (diff === 0) return "today";
  if (diff === 1) return "yesterday";
  return d.toLocaleDateString([], { weekday: "long", day: "numeric", month: "long" });
}

function dayOf(ts) {
  const d = new Date(ts);
  return isNaN(d) ? String(ts) : d.toDateString();
}

function linkify(text) {
  const parts = text.split(/(https?:\\/\\/[^\\s<]+)/g);
  return parts.map((p, i) => {
    if (i % 2 === 0) return document.createTextNode(p);
    const a = document.createElement("a");
    a.href = p; a.rel = "noreferrer noopener"; a.textContent = p;
    return a;
  });
}

function line(r, showMeta) {
  const el = document.createElement("article");
  el.className = "msg" + (r.author === state.me ? " me" : "") + (showMeta ? "" : " same");
  el.dataset.id = r.id;
  const bar = document.createElement("div"); bar.className = "bar";
  const body = document.createElement("div");
  if (showMeta) {
    const meta = document.createElement("div"); meta.className = "meta";
    const who = document.createElement("span"); who.textContent = r.author === state.me ? "you" : r.author;
    const time = document.createElement("time"); time.textContent = stamp(r.ts);
    meta.append(who, time);
    body.append(meta);
  }
  const text = document.createElement("div"); text.className = "text";
  text.append(...linkify(r.text || ""));
  body.append(text);
  el.append(bar, body);
  return el;
}

function render() {
  const thread = $("thread");
  const atBottom = window.innerHeight + window.scrollY >= document.body.scrollHeight - 80;
  thread.textContent = "";
  if (!state.messages.length) {
    const p = document.createElement("p"); p.id = "empty"; p.textContent = "Nothing here yet.";
    thread.append(p); return;
  }
  let lastDay = "", prev = null;
  for (const r of state.messages) {
    const day = dayOf(r.ts);
    if (day !== lastDay) {
      const sep = document.createElement("div");
      sep.className = "day"; sep.textContent = dayLabel(r.ts);
      thread.append(sep);
      lastDay = day; prev = null;
    }
    const close = prev && prev.author === r.author &&
      (new Date(r.ts) - new Date(prev.ts)) < 10 * 60000;
    thread.append(line(r, !close));
    prev = r;
  }
  window.scrollTo({ top: document.body.scrollHeight, behavior: "instant" });
}

function status(msg, bad) {
  const el = $("status"); el.textContent = msg || ""; el.classList.toggle("bad", !!bad);
}

async function load(first) {
  try {
    const res = await fetch("/api/state", { headers: { "accept": "application/json" } });
    if (!res.ok) throw new Error("HTTP " + res.status);
    const data = await res.json();
    state.me = data.me; state.peer = data.peer;
    $("title").textContent = [data.me, data.peer].sort().join(" + ");
    $("who").textContent = "humans";
    $("text").placeholder = "write to " + data.peer;
    state.messages = data.messages;
    render();
    $("dot").className = "dot live";
    status("");
  } catch (e) {
    $("dot").className = "dot down";
    status("not reaching the board: " + e.message, true);
  }
}

async function send(text) {
  state.sending = true;
  $("send").disabled = true;
  status("sending…");
  try {
    const res = await fetch("/api/post", {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ text })
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok || !data.ok) throw new Error(data.error || ("HTTP " + res.status));
    state.messages.push(data.record);
    render();
    status(data.pushed === false ? "saved here; will not see it until the push succeeds" : "",
           data.pushed === false);
    return true;
  } catch (e) {
    status("not sent: " + e.message, true);
    return false;
  } finally {
    state.sending = false;
    $("send").disabled = false;
  }
}

$("composer").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const box = $("text");
  const text = box.value.trim();
  if (!text || state.sending) return;
  const ok = await send(text);
  if (ok) { box.value = ""; box.style.height = "auto"; box.focus(); }
});
$("text").addEventListener("keydown", (ev) => {
  if (ev.key === "Enter" && (ev.metaKey || ev.ctrlKey)) { ev.preventDefault(); $("composer").requestSubmit(); }
});
$("text").addEventListener("input", (ev) => {
  const box = ev.target; box.style.height = "auto";
  box.style.height = Math.min(box.scrollHeight, window.innerHeight * 0.4) + "px";
});
document.addEventListener("visibilitychange", () => { if (!document.hidden) load(false); });
setInterval(() => { if (!document.hidden) load(false); }, 15000);
load(true);
</script>
</body>
</html>
"""

DENIED = """<!doctype html><meta name="viewport" content="width=device-width, initial-scale=1">
<title>human channel</title>
<body style="font:16px/1.5 ui-sans-serif,system-ui;margin:0;padding:32px">
<p>This page needs its own link. Ask the other person for the one that ends in <code>?k=</code>.</p>
</body></html>"""


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class App:
    """Everything the handler needs, resolved once at start-up."""

    def __init__(self, board: Path, human: str, token: str, *, git: bool = True,
                 pull_every: int = 45) -> None:
        self.board = board
        self.human = human
        self.token = token
        self.git = git
        self.pull_every = pull_every
        self.write_lock = threading.Lock()
        self.pulling = threading.Lock()
        board_log.set_dir(board)

    def peer(self) -> str:
        return "will" if self.human == "hamish" else "hamish"

    def humans(self) -> list[dict]:
        out = [r for r in board_log.all_records()
               if (r.get("room") or "shared") == ROOM]
        out.sort(key=lambda r: (r.get("ts", ""), r.get("id", "")))
        return out

    def pull(self) -> None:
        if not self.git or not self.pull_every:
            return
        if not self.pulling.acquire(blocking=False):
            return
        try:
            board_log.git("pull", "--rebase", "--autostash", check=False)
        finally:
            self.pulling.release()

    def post(self, text: str, topic: str = "note") -> dict:
        text = text.strip()
        if not text:
            raise ValueError("empty message")
        if len(text) > MAX_TEXT:
            raise ValueError(f"text is {len(text)} chars; the limit is {MAX_TEXT}")
        problem = board_log.room_voice_problem(self.human, ROOM, "human")
        if problem:                      # configuration error, not the writer's fault
            raise RuntimeError(problem)
        with self.write_lock:
            rec = board_log._append_record(self.human, {
                "ts": _now(), "author": self.human, "topic": topic, "text": text,
                "room": ROOM, "via": "human",
            })
            pushed = None
            if self.git:
                path = board_log.own_file(self.human)
                board_log.git("add", str(path.relative_to(board_log.REPO)))
                board_log.git("commit", "-q", "-m", f"board: {rec['id']} {topic}".strip())
                pushed = board_log.git("push", check=False).returncode == 0
        return {"record": rec, "pushed": pushed}

    def pull_forever(self, stop: threading.Event) -> None:
        while not stop.wait(self.pull_every):
            self.pull()

    def verify(self) -> tuple[bool, str]:
        done = subprocess.run([sys.executable, str(HERE.parent / "board_log.py"),
                               "--dir", str(self.board), "verify"],
                              capture_output=True, text=True)
        return done.returncode == 0, (done.stdout + done.stderr).strip()


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        server_version = "human-channel/1"

        def log_message(self, format: str, *args) -> None:      # quiet by default
            if os.environ.get("HUMAN_CHANNEL_DEBUG"):
                super().log_message(format, *args)

        # --- helpers ---------------------------------------------------------
        def _cookie_token(self) -> str:
            raw = self.headers.get("cookie")
            if not raw:
                return ""
            jar = SimpleCookie()
            try:
                jar.load(raw)
            except Exception:
                return ""
            morsel = jar.get(COOKIE)
            return morsel.value if morsel else ""

        def _authed(self) -> bool:
            given = self._cookie_token()
            if not given:
                auth = self.headers.get("authorization", "")
                if auth.lower().startswith("bearer "):
                    given = auth[7:].strip()
            return bool(given) and hmac.compare_digest(given, app.token)

        def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
            self.send_response(code)
            self.send_header("content-type", ctype)
            self.send_header("cache-control", "no-store")
            self.send_header("referrer-policy", "no-referrer")
            self.send_header("x-content-type-options", "nosniff")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, code: int, obj: dict) -> None:
            self._send(code, json.dumps(obj, ensure_ascii=False).encode(), "application/json")

        # --- routes ----------------------------------------------------------
        def do_GET(self) -> None:
            url = urlparse(self.path)
            if url.path == "/health":
                ok, detail = app.verify()
                self._json(200 if ok else 500,
                           {"ok": ok, "human": app.human, "room": ROOM,
                            "board": str(app.board), "detail": detail.splitlines()[0]})
                return
            if url.path == "/api/state":
                if not self._authed():
                    self._json(401, {"ok": False, "error": "needs the channel link"})
                    return
                since = (parse_qs(url.query).get("since") or [""])[0]
                rows = app.humans()
                if since:
                    ids = [r.get("id") for r in rows]
                    rows = rows[ids.index(since) + 1:] if since in ids else rows
                self._json(200, {"ok": True, "me": app.human, "peer": app.peer(),
                                 "messages": rows, "server_time": _now()})
                return
            if url.path in ("/", "/index.html"):
                q = parse_qs(url.query)
                key = (q.get("k") or [""])[0]
                if key and hmac.compare_digest(key, app.token):
                    cookie = (f"{COOKIE}={app.token}; Path=/; HttpOnly; SameSite=Lax; "
                              f"Max-Age=31536000")
                    self._send(302, b"", "text/plain", {"location": "/",
                                                        "set-cookie": cookie})
                    return
                if not self._authed():
                    self._send(401, DENIED.encode(), "text/html; charset=utf-8")
                    return
                self._send(200, PAGE.encode(), "text/html; charset=utf-8")
                return
            self._json(404, {"ok": False, "error": "no such path"})

        def do_HEAD(self) -> None:
            self.do_GET()

        def do_POST(self) -> None:
            url = urlparse(self.path)
            if url.path != "/api/post":
                self._json(404, {"ok": False, "error": "no such path"})
                return
            if not self._authed():
                self._json(401, {"ok": False, "error": "needs the channel link"})
                return
            # A JSON content type is required as well as the cookie: a form POST from
            # another site cannot set it without a preflight this server never answers.
            if "application/json" not in (self.headers.get("content-type") or ""):
                self._json(415, {"ok": False, "error": "send JSON"})
                return
            length = int(self.headers.get("content-length") or 0)
            if length <= 0 or length > 16_384:
                self._json(413, {"ok": False, "error": "message body missing or too large"})
                return
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
                text = str(body.get("text") or "")
                topic = str(body.get("topic") or "note")[:40] or "note"
            except (json.JSONDecodeError, UnicodeDecodeError):
                self._json(400, {"ok": False, "error": "body is not JSON"})
                return
            try:
                out = app.post(text, topic)
            except ValueError as e:
                self._json(400, {"ok": False, "error": str(e)})
                return
            except RuntimeError as e:
                self._json(500, {"ok": False, "error": str(e)})
                return
            self._json(200, {"ok": True, **out})

    return Handler


def token_from(path: Path) -> str:
    if path.exists():
        tok = path.read_text().strip()
        if tok:
            return tok
    path.parent.mkdir(parents=True, exist_ok=True)
    tok = secrets.token_urlsafe(24)
    path.write_text(tok + "\n")
    path.chmod(0o600)
    return tok


def main() -> None:
    p = argparse.ArgumentParser(description="The human channel: your own writer file, from a browser")
    p.add_argument("--dir", default=os.environ.get("BOARD_DIR")
                   or str(Path.home() / "Developer/agent-memory/board"))
    p.add_argument("--author", default=os.environ.get("BOARD_HUMAN") or "hamish",
                   help="the person on this machine (hamish or will)")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8767)
    p.add_argument("--token-file", default=str(Path.home() / ".hermes/human-channel-token"))
    p.add_argument("--no-git", action="store_true", help="write the file only; no commit or push")
    p.add_argument("--pull", type=int, default=45, metavar="SECONDS",
                   help="how often to pull the other side's records (0 = never)")
    a = p.parse_args()

    board = Path(a.dir).expanduser().resolve()
    if not board.is_dir():
        sys.exit(f"no board directory at {board}")
    if board_log.is_human_author(a.author) is False:
        sys.exit(f"--author must be one of {', '.join(board_log.HUMAN_AUTHORS)}")
    token = token_from(Path(a.token_file).expanduser())
    app = App(board, a.author, token, git=not a.no_git, pull_every=a.pull)
    stop = threading.Event()
    if not a.no_git and a.pull:
        threading.Thread(target=app.pull_forever, args=(stop,), daemon=True).start()

    httpd = ThreadingHTTPServer((a.host, a.port), make_handler(app))
    print(f"human channel: {board} as {a.author} on http://{a.host}:{a.port}/")
    print(f"link: http://{a.host}:{a.port}/?k={token}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        httpd.server_close()


if __name__ == "__main__":
    main()
