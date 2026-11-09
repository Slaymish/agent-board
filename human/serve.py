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
<link rel="manifest" href="/manifest.webmanifest">
<link rel="apple-touch-icon" href="/icon-180.png">
<meta name="apple-mobile-web-app-status-bar-style" content="default">
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
  /* No presence indicator: a green dot beside a name reads as "they are here, why
     haven't they answered". Connection trouble shows in the status line instead. */
  main { flex: 1; padding: 4px 18px 24px; }
  /* One column, not two sides. There is no left and right, because a reply is not
     what this is for: a run of notes from one person is normal and does not stack up
     against anything. The author's name appears once per run per day; the time sits
     in the margin where it can be read or ignored. */
  .note { display: grid; grid-template-columns: 44px 1fr; gap: 14px; padding-top: 9px; }
  .note.first { margin-top: 16px; }
  .note .when {
    font-family: var(--mono); font-size: 10px; color: var(--quiet);
    text-align: right; padding-top: 6px; font-variant-numeric: tabular-nums;
  }
  .note .name {
    font-family: var(--sans); font-size: 11px; letter-spacing: .13em;
    text-transform: lowercase; margin: 0 0 2px;
  }
  .note.mine .name { color: var(--mine); }
  .note.theirs .name { color: var(--theirs); }
  .day {
    font-family: var(--mono); font-size: 11px; letter-spacing: .08em;
    text-transform: uppercase; color: var(--quiet);
    margin: 26px 0 4px; padding-bottom: 6px; border-bottom: 1px solid var(--rule);
  }
  .day:first-child { margin-top: 8px; }
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

function line(r, first) {
  const mine = r.author === state.me;
  const el = document.createElement("article");
  el.className = "note" + (mine ? " mine" : " theirs") + (first ? " first" : "");
  el.dataset.id = r.id;
  const when = document.createElement("time"); when.className = "when";
  when.textContent = stamp(r.ts); when.title = new Date(r.ts).toString();
  const body = document.createElement("div");
  if (first) {
    const name = document.createElement("div"); name.className = "name";
    name.textContent = r.author;
    body.append(name);
  }
  const text = document.createElement("div"); text.className = "text";
  text.append(...linkify(r.text || ""));
  body.append(text);
  el.append(when, body);
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
    // A run is one person on one day. Twenty notes and one reply is the normal shape,
    // so nothing here groups, counts or orders them into an exchange.
    const first = !prev || prev.author !== r.author;
    thread.append(line(r, first));
    prev = r;
  }
  if (atBottom) window.scrollTo({ top: document.body.scrollHeight, behavior: "instant" });
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
    status("");
  } catch (e) {
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

    def __init__(self, board: Path, tokens: dict[str, str], *, local_human: str = "hamish",
                 git: bool = True, pull_every: int = 45) -> None:
        self.board = board
        self.tokens = {t: h for h, t in tokens.items() if t}
        self.local_human = local_human
        self.git = git
        self.pull_every = pull_every
        self.write_lock = threading.Lock()
        self.pulling = threading.Lock()
        board_log.set_dir(board)

    def who(self, token: str) -> str | None:
        """The person this token belongs to, or None.

        The author of a post is derived here and never taken from the request body:
        a page that let the client name the author would make `via: human` mean
        nothing at all, which is the whole thing it is meant to establish.
        """
        for known, human in self.tokens.items():
            if token and hmac.compare_digest(token, known):
                return human
        return None

    def peer(self, human: str | None = None) -> str:
        human = human or self.local_human
        return "will" if human == "hamish" else "hamish"

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

    def post(self, human: str, text: str, topic: str = "note") -> dict:
        text = text.strip()
        if not text:
            raise ValueError("empty message")
        if len(text) > MAX_TEXT:
            raise ValueError(f"text is {len(text)} chars; the limit is {MAX_TEXT}")
        problem = board_log.room_voice_problem(human, ROOM, "human")
        if problem:                      # configuration error, not the writer's fault
            raise RuntimeError(problem)
        with self.write_lock:
            rec = board_log._append_record(human, {
                "ts": _now(), "author": human, "topic": topic, "text": text,
                "room": ROOM, "via": "human",
            })
            pushed = None
            if self.git:
                path = board_log.own_file(human)
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

        def _token(self) -> str:
            given = self._cookie_token()
            if not given:
                auth = self.headers.get("authorization", "")
                if auth.lower().startswith("bearer "):
                    given = auth[7:].strip()
            return given

        def _authed(self) -> bool:
            return app.who(self._token()) is not None

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
            if url.path in ("/manifest.webmanifest", "/icon-180.png", "/icon-192.png",
                            "/icon-512.png"):
                # Icons and the manifest carry nothing secret, and the browser fetches
                # them before the cookie exists on a fresh install.
                name = url.path.lstrip("/")
                path = HERE / "static" / name
                if not path.exists():
                    self._json(404, {"ok": False, "error": "no such asset"})
                    return
                ctype = ("application/manifest+json" if name.endswith(".webmanifest")
                         else "image/png")
                self._send(200, path.read_bytes(), ctype,
                           {"cache-control": "public, max-age=86400"})
                return
            if url.path == "/health":
                ok, detail = app.verify()
                self._json(200 if ok else 500,
                           {"ok": ok, "local_human": app.local_human, "room": ROOM,
                            "people": sorted(set(app.tokens.values())),
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
                me = app.who(self._token())
                self._json(200, {"ok": True, "me": me, "peer": app.peer(me),
                                 "messages": rows, "server_time": _now()})
                return
            if url.path in ("/", "/index.html"):
                q = parse_qs(url.query)
                key = (q.get("k") or [""])[0]
                if key and app.who(key) is not None:
                    cookie = (f"{COOKIE}={key}; Path=/; HttpOnly; SameSite=Lax; "
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
            human = app.who(self._token())
            if human is None:
                self._json(401, {"ok": False, "error": "needs the channel link"})
                return
            try:
                out = app.post(human, text, topic)
            except ValueError as e:
                self._json(400, {"ok": False, "error": str(e)})
                return
            except RuntimeError as e:
                self._json(500, {"ok": False, "error": str(e)})
                return
            self._json(200, {"ok": True, **out})

    return Handler


def people_from(path: Path, local_human: str) -> dict[str, str]:
    """human -> token, one per person, created once.

    A single token was enough when the page was one person's viewer. Two tokens are
    what make the author derivable: the server decides who is writing from the token
    that opened the page, so nobody has to pick a name and no request can claim one.
    The old single-token file, if present, becomes the local person's token so a link
    already on a phone keeps working.
    """
    if path.exists():
        try:
            data = json.loads(path.read_text())
            if isinstance(data, dict) and data:
                return {str(k): str(v) for k, v in data.items() if v}
        except json.JSONDecodeError:
            pass
    legacy = path.parent / "human-channel-token"
    people: dict[str, str] = {}
    if legacy.exists():
        tok = legacy.read_text().strip()
        if tok:
            people[local_human] = tok
    for human in board_log.HUMAN_AUTHORS:
        people.setdefault(human, secrets.token_urlsafe(24))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(people, indent=1) + "\n")
    path.chmod(0o600)
    return people


def main() -> None:
    p = argparse.ArgumentParser(description="The human channel: your own writer file, from a browser")
    p.add_argument("--dir", default=os.environ.get("BOARD_DIR")
                   or str(Path.home() / "Developer/agent-memory/board"))
    p.add_argument("--author", default=os.environ.get("BOARD_HUMAN") or "hamish",
                   help="the person on this machine (hamish or will)")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8767)
    p.add_argument("--token-file", default=str(Path.home() / ".hermes/human-channels.json"),
                   help="human -> token; created on first start")
    p.add_argument("--no-git", action="store_true", help="write the file only; no commit or push")
    p.add_argument("--pull", type=int, default=45, metavar="SECONDS",
                   help="how often to pull the other side's records (0 = never)")
    a = p.parse_args()

    board = Path(a.dir).expanduser().resolve()
    if not board.is_dir():
        sys.exit(f"no board directory at {board}")
    if board_log.is_human_author(a.author) is False:
        sys.exit(f"--author must be one of {', '.join(board_log.HUMAN_AUTHORS)}")
    people = people_from(Path(a.token_file).expanduser(), a.author)
    app = App(board, people, local_human=a.author, git=not a.no_git, pull_every=a.pull)
    stop = threading.Event()
    if not a.no_git and a.pull:
        threading.Thread(target=app.pull_forever, args=(stop,), daemon=True).start()

    httpd = ThreadingHTTPServer((a.host, a.port), make_handler(app))
    print(f"human channel: {board} on http://{a.host}:{a.port}/")
    for human, token in people.items():
        print(f"  {human}: /?k={token}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        httpd.server_close()


if __name__ == "__main__":
    main()
