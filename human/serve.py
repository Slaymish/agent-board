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

Saving and publishing are separate facts. The append is the note; the push is how the
other person gets it. A failed commit or push therefore returns ``saved: true,
published: false`` rather than an error, and a publisher loop retries pending commits
every ``--publish-every`` seconds until one succeeds, so a note cannot sit silently in
a local commit waiting for unrelated traffic. A retry from the page carries the same
``client_id``, so a lost response cannot turn one note into two.
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
# The page is a file beside this one, read on each request, so changing how the room
# looks does not mean editing Python, restarting a service or asking the other person to
# deploy: it is a pull request either of us can open, and the change is live on the next
# reload. The server is plumbing; the page belongs to whoever is in the room.
PAGE_PATH = HERE / "page.html"
_page_cache: tuple[float, bytes] = (0.0, b"")


def page_bytes() -> bytes:
    """page.html, re-read whenever its mtime moves (so a pull needs no restart)."""
    global _page_cache
    mtime = PAGE_PATH.stat().st_mtime
    if _page_cache[0] != mtime:
        _page_cache = (mtime, PAGE_PATH.read_bytes())
    return _page_cache[1]


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
                 git: bool = True, pull_every: int = 45, publish_every: int = 30) -> None:
        self.board = board
        self.tokens = {t: h for h, t in tokens.items() if t}
        self.local_human = local_human
        self.git = git
        self.pull_every = pull_every
        self.publish_every = publish_every
        self.write_lock = threading.Lock()
        self.pulling = threading.Lock()
        self.publishing = threading.Lock()
        # client_id -> the record it produced, so a retry after a lost response returns
        # the note instead of appending a second one. Bounded: it is a response cache,
        # not a second log.
        self.recent: dict[str, dict] = {}
        self.recent_lock = threading.Lock()
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

    def stamp(self) -> tuple:
        """Cheap fingerprint of the human logs: the two writer files' size and mtime.

        This exists so a reader does not have to poll to find out whether anything
        happened. It says only "the room changed", never "someone read your note".
        """
        out = []
        for human in board_log.HUMAN_AUTHORS:
            p = self.board / f"{human}.jsonl"
            try:
                st = p.stat()
                out.append((human, st.st_size, st.st_mtime_ns))
            except FileNotFoundError:
                out.append((human, 0, 0))
        return tuple(out)

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

    def post(self, human: str, text: str, topic: str = "note",
             reply_to: str | None = None, client_id: str | None = None) -> dict:
        text = text.strip()
        if not text:
            raise ValueError("empty message")
        if len(text) > MAX_TEXT:
            raise ValueError(f"text is {len(text)} chars; the limit is {MAX_TEXT}")
        problem = board_log.room_voice_problem(human, ROOM, "human")
        if problem:                      # configuration error, not the writer's fault
            raise RuntimeError(problem)
        if reply_to:
            # A reply stays in its parent's room, so the target has to be one of these
            # notes. Checked here rather than trusted from the page: the page can only
            # send an id, but an id is still a claim about the log.
            parent = next((r for r in board_log.all_records()
                           if str(r.get("id")) == str(reply_to)), None)
            if parent is None:
                raise ValueError(f"no note {reply_to} to reply to")
            if (parent.get("room") or "shared") != ROOM:
                raise ValueError(f"{reply_to} is not in this room")
        if client_id:
            with self.recent_lock:
                seen = self.recent.get(str(client_id))
            if seen is not None:
                return {"record": seen, "saved": True, "published": True, "repeated": True}
        with self.write_lock:
            body = {"ts": _now(), "author": human, "topic": topic, "text": text,
                    "room": ROOM, "via": "human"}
            if reply_to:
                body["reply_to"] = str(reply_to)
            rec = board_log._append_record(human, body)
            if client_id:
                with self.recent_lock:
                    if len(self.recent) > 200:
                        self.recent.pop(next(iter(self.recent)))
                    self.recent[str(client_id)] = rec
            published = True
            error = None
            if self.git:
                path = board_log.own_file(human)
                try:
                    board_log.git("add", str(path.relative_to(board_log.REPO)))
                    board_log.git("commit", "-q", "-m", f"board: {rec['id']} {topic}".strip())
                except (subprocess.CalledProcessError, OSError) as e:
                    # The note is on disk and fsynced. A commit failure is a publication
                    # problem, not a reason to tell the writer their words were lost.
                    published, error = False, str(e)
                else:
                    published = self.publish()
        return {"record": rec, "saved": True, "published": published,
                **({"error": error} if error else {})}

    def publish(self) -> bool:
        """Push pending commits. Safe to call from the request, the loop, or both.

        A rejected push is usually a peer that got there first, so one rebase-and-retry
        is the whole policy. Anything past that is left to the loop rather than holding
        the writer's request open.
        """
        if not self.git:
            return True
        if not self.publishing.acquire(blocking=False):
            return False
        try:
            for _ in range(2):
                if board_log.git("push", check=False).returncode == 0:
                    return True
                board_log.git("pull", "--rebase", "--autostash", check=False)
            return False
        finally:
            self.publishing.release()

    def publish_forever(self, stop: threading.Event) -> None:
        while not stop.wait(self.publish_every):
            ahead = board_log.git("rev-list", "--count", "@{u}..HEAD", check=False)
            if ahead.returncode == 0 and ahead.stdout.strip() not in ("", "0"):
                self.publish()

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
            if url.path == "/api/events":
                # Server-sent events, because the alternative is a person refreshing
                # every few minutes to see whether they are being answered.
                if not self._authed():
                    self._json(401, {"ok": False, "error": "needs the channel link"})
                    return
                self.send_response(200)
                self.send_header("content-type", "text/event-stream")
                self.send_header("cache-control", "no-store")
                self.send_header("connection", "keep-alive")
                self.end_headers()
                last = app.stamp()
                deadline = time.time() + 120
                try:
                    while time.time() < deadline:
                        time.sleep(2)
                        now = app.stamp()
                        if now != last:
                            last = now
                            self.wfile.write(b"data: changed\n\n")
                        else:
                            self.wfile.write(b": keep\n\n")
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass
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
                try:
                    body = page_bytes()
                except FileNotFoundError:
                    self._send(500, b"human/page.html is missing from this checkout",
                               "text/plain; charset=utf-8")
                    return
                self._send(200, body, "text/html; charset=utf-8")
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
                reply_to = body.get("reply_to") or None
                client_id = str(body.get("client_id") or "")[:64] or None
            except (json.JSONDecodeError, UnicodeDecodeError):
                self._json(400, {"ok": False, "error": "body is not JSON"})
                return
            human = app.who(self._token())
            if human is None:
                self._json(401, {"ok": False, "error": "needs the channel link"})
                return
            try:
                out = app.post(human, text, topic,
                               str(reply_to) if reply_to else None, client_id)
            except ValueError as e:
                self._json(400, {"ok": False, "error": str(e)})
                return
            except RuntimeError as e:
                self._json(500, {"ok": False, "error": str(e)})
                return
            except subprocess.CalledProcessError as e:
                # Only reachable if a git call outside the guarded block fails. The note
                # is already on disk, so say that rather than a bare 500.
                self._json(200, {"ok": True, "saved": True, "published": False,
                                 "error": str(e)})
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
    p.add_argument("--also-host", default="",
                   help="a second bind address, e.g. the tailnet IPv4, so a client whose "
                        "MagicDNS or TLS is unhappy still has a plain route in")
    p.add_argument("--port", type=int, default=8767)
    p.add_argument("--also-port", type=int, default=0,
                   help="port for --also-host (default: --port + 10)")
    p.add_argument("--token-file", default=str(Path.home() / ".hermes/human-channels.json"),
                   help="human -> token; created on first start")
    p.add_argument("--no-git", action="store_true", help="write the file only; no commit or push")
    p.add_argument("--pull", type=int, default=45, metavar="SECONDS",
                   help="how often to pull the other side's records (0 = never)")
    p.add_argument("--publish", type=int, default=30, metavar="SECONDS",
                   help="how often to retry commits that did not reach the remote (0 = never)")
    a = p.parse_args()
    if not a.also_port:
        a.also_port = a.port + 10

    board = Path(a.dir).expanduser().resolve()
    if not board.is_dir():
        sys.exit(f"no board directory at {board}")
    if board_log.is_human_author(a.author) is False:
        sys.exit(f"--author must be one of {', '.join(board_log.HUMAN_AUTHORS)}")
    people = people_from(Path(a.token_file).expanduser(), a.author)
    app = App(board, people, local_human=a.author, git=not a.no_git, pull_every=a.pull,
              publish_every=a.publish)
    stop = threading.Event()
    if not a.no_git and a.pull:
        threading.Thread(target=app.pull_forever, args=(stop,), daemon=True).start()
    if not a.no_git and a.publish:
        threading.Thread(target=app.publish_forever, args=(stop,), daemon=True).start()

    httpd = ThreadingHTTPServer((a.host, a.port), make_handler(app))
    if a.also_host:
        # A different port: tailscaled already owns this one on the tailnet address for
        # its TLS front, so sharing it would race and answer plain HTTP with a 400.
        try:
            second = ThreadingHTTPServer((a.also_host, a.also_port), make_handler(app))
            threading.Thread(target=second.serve_forever, daemon=True).start()
            print(f"also listening on http://{a.also_host}:{a.also_port}/")
        except OSError as exc:
            print(f"could not bind {a.also_host}:{a.port}: {exc}", file=sys.stderr)
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
