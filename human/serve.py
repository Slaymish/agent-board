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
every ``--publish`` seconds until one succeeds, so a note cannot sit silently in
a local commit waiting for unrelated traffic. A retry from the page carries the same
``client_id``, so a lost response cannot turn one note into two.
"""
from __future__ import annotations

import argparse
import fcntl
from contextlib import contextmanager, nullcontext
import hmac
import json
import os
import secrets
import subprocess
import sys
import tempfile
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

# The review and the reports are this person's own material, not the shared room's.
FACT_REVIEW = Path.home() / ".hermes" / "scripts" / "facts-review.py"
# The generated half: questions written from his own recent conversations. It imports
# dream.py for the model call, and dream.py needs PyYAML, which this service's interpreter
# does not have. Same trap the facts page hit, so the interpreter is named rather than
# inherited: /usr/bin/python3 is the one with yaml on this host.
REVIEW_QUESTIONS = Path.home() / ".hermes" / "scripts" / "review-questions.py"
REPORT_DIRS = [
    Path.home() / "agent-system" / "plans" / "reviews",
    Path.home() / ".hermes" / "data" / "lottery-scanner",
    Path.home() / ".hermes" / "data" / "journal-clearance",
    Path.home() / ".hermes" / "data" / "recs",
]
REPORT_MAX = 200_000

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


def _stamp(epoch: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(epoch))


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
        board_log.set_dir(board)
        self.repo = board_log.git_root(board)

    def git_run(self, *args, check=True):
        return subprocess.run(["git", "-C", str(self.repo), *args],
                              capture_output=True, text=True, check=check, timeout=15,
                              env={**os.environ, "GIT_TERMINAL_PROMPT": "0",
                                   "GIT_EDITOR": ":", "GIT_SEQUENCE_EDITOR": ":"})

    @contextmanager
    def repository_lock(self):
        common = self.git_run("rev-parse", "--git-common-dir").stdout.strip()
        path = (self.repo / common).resolve() / "human-channel.lock"
        with path.open("a") as lock:
            deadline = time.monotonic() + 5
            while True:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("repository is busy")
                    time.sleep(.05)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

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

    def work(self, limit: int = 24) -> list[dict]:
        """The agents' side of the same log, read-only.

        This is what lets a note in the room be followed to the work it caused. It is a
        reader, not a second client: nothing here writes, and nothing here counts,
        acknowledges or flags for attention. The room stays the room.
        """
        rows = [r for r in board_log.all_records()
                if (r.get("room") or "shared") in ("agents", "shared")
                and str(r.get("author")) not in board_log.HUMAN_AUTHORS
                # Delivery receipts are bookkeeping, not work: a list of "delivered NN"
                # lines tells a person nothing and buries the notes that do.
                and str(r.get("topic") or "") != "receipt"
                and not str(r.get("text") or "").startswith("delivered ")]
        rows.sort(key=lambda r: (r.get("ts", ""), r.get("id", "")))
        out = []
        for r in rows[-limit:]:
            text = str(r.get("text") or "")
            out.append({"id": r.get("id"), "ts": r.get("ts"), "author": r.get("author"),
                        "topic": r.get("topic"), "reply_to": r.get("reply_to"),
                        "room": r.get("room") or "shared", "text": text})
        return out

    def resolve(self, ident: str) -> dict | None:
        for record in self.humans() + self.work(limit=10**9):
            if record.get("id") == ident:
                return record
        return None

    def search(self, query: str, limit: int = 40) -> list[dict]:
        """Both rooms, one query. An id matches exactly, so a reference can be pasted in.

        Read-only and unordered by popularity: a search is a way to find a thing you
        already know exists, not a feed.
        """
        term = (query or "").strip().lower()
        if len(term) < 2:
            return []
        hits = []
        for r in board_log.all_records():
            if str(r.get("topic") or "") == "receipt":
                continue
            author = str(r.get("author"))
            room = str(r.get("room") or "shared")
            if author in board_log.HUMAN_AUTHORS:
                if room != "humans":
                    continue
            elif room not in ("agents", "shared"):
                continue
            hay = " ".join(str(r.get(k) or "") for k in ("id", "text", "topic")).lower()
            if term not in hay:
                continue
            text = str(r.get("text") or "")
            hits.append({"id": r.get("id"), "ts": r.get("ts"), "author": author,
                         "topic": r.get("topic"), "room": room,
                         "text": text if len(text) <= 400 else text[:399] + "…"})
        hits.sort(key=lambda r: (r.get("ts", ""), r.get("id", "")), reverse=True)
        return hits[:limit]

    def outcomes(self, force: bool = False) -> dict:
        """The three-routine outcome read model, regenerated when it is old.

        The generator is read-only over cron's own records and the routines' artefacts;
        this method only decides when to run it and returns what it wrote.
        """
        path = Path.home() / ".hermes" / "data" / "outcomes.json"
        stale = True
        try:
            stale = (time.time() - path.stat().st_mtime) > 600
        except OSError:
            pass
        if force or stale:
            try:
                subprocess.run([sys.executable, str(Path.home() / ".hermes" / "scripts"
                                                  / "outcomes.py")],
                               capture_output=True, text=True, timeout=30)
            except (OSError, subprocess.SubprocessError):
                pass
        try:
            return json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return {"generated_at": None, "routines": [],
                    "error": "no outcome snapshot yet"}

    def review_pick(self, limit: int = 5) -> dict:
        """The review queue, from the script that already owns the selection rules.

        This is a form over an existing decision procedure, not a second one: which
        claims are worth a person's attention is `facts-review.py`'s job.
        """
        done = subprocess.run([sys.executable, str(FACT_REVIEW), "pick", "--limit", str(limit)],
                              capture_output=True, text=True, timeout=60)
        if done.returncode != 0:
            raise RuntimeError((done.stderr or done.stdout).strip()[:400] or "review unavailable")
        return json.loads(done.stdout)

    def review_apply(self, answers: list[dict]) -> dict:
        """Apply one batch, bound to the revision each question was asked against."""
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump({"answers": answers}, fh)
            path = fh.name
        try:
            done = subprocess.run([sys.executable, str(FACT_REVIEW), "apply", "--answers", path],
                                  capture_output=True, text=True, timeout=120)
            if done.returncode != 0:
                raise RuntimeError((done.stderr or done.stdout).strip()[:400] or "apply failed")
            return json.loads(done.stdout)
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass


    def _review_python(self) -> str:
        candidate = Path("/usr/bin/python3")
        return str(candidate) if candidate.exists() else sys.executable

    def review_generated(self) -> dict:
        """The current generated batch. Reading it must never generate one."""
        done = subprocess.run([self._review_python(), str(REVIEW_QUESTIONS), "latest"],
                              capture_output=True, text=True, timeout=30)
        if done.returncode != 0:
            raise RuntimeError((done.stderr or done.stdout).strip()[:400] or "questions unavailable")
        return json.loads(done.stdout or "{}")

    def review_generate(self) -> dict:
        """One model call over the last few days of conversation. Slow by nature."""
        done = subprocess.run([self._review_python(), str(REVIEW_QUESTIONS), "generate"],
                              capture_output=True, text=True, timeout=280)
        if done.returncode != 0:
            raise RuntimeError((done.stderr or done.stdout).strip()[:400] or "could not generate")
        batch = json.loads(done.stdout or "{}")
        return {"count": len(batch.get("questions") or []),
                "batch_id": batch.get("batch_id"), "note": batch.get("note")}

    def review_generated_answer(self, batch: str, qid: str, value: str,
                                text: str = "") -> dict:
        """Apply one tap. Exit 4 is a refusal with a reason, not a broken server."""
        args = [self._review_python(), str(REVIEW_QUESTIONS), "answer", "--id", qid,
                "--value", value]
        if batch:
            args += ["--batch", batch]
        if text:
            args += ["--text", text[:600]]
        done = subprocess.run(args, capture_output=True, text=True, timeout=60)
        try:
            outcome = json.loads(done.stdout or "{}")
        except json.JSONDecodeError as exc:
            raise RuntimeError((done.stderr or done.stdout).strip()[:400] or str(exc)) from exc
        if outcome.get("result") != "answered":
            raise ValueError(outcome.get("why") or "that answer did not apply")
        return outcome

    def reports(self) -> list[dict]:
        out = []
        for directory in REPORT_DIRS:
            if not directory.is_dir():
                continue
            for path in directory.glob("*.md"):
                try:
                    st = path.stat()
                except OSError:
                    continue
                title = path.stem.replace("-", " ").replace("_", " ")
                try:
                    for line in path.read_text(errors="replace").splitlines()[:12]:
                        if line.startswith("# "):
                            title = line[2:].strip()
                            break
                except OSError:
                    pass
                out.append({"name": f"{directory.name}/{path.name}", "title": title,
                            "mtime": _stamp(st.st_mtime), "bytes": st.st_size})
        out.sort(key=lambda r: r["mtime"], reverse=True)
        return out[:60]

    def report_text(self, name: str) -> str | None:
        wanted = str(name or "")
        for directory in REPORT_DIRS:
            if not wanted.startswith(directory.name + "/"):
                continue
            path = (directory / wanted.split("/", 1)[1]).resolve()
            if path.parent != directory.resolve() or path.suffix != ".md":
                return None
            try:
                if path.stat().st_size > REPORT_MAX:
                    return None
                return path.read_text(errors="replace")
            except OSError:
                return None
        return None

    def pull(self) -> None:
        if self.git and self.pull_every:
            try:
                with self.repository_lock():
                    self.git_run("pull", "--ff-only", check=False)
            except (OSError, subprocess.SubprocessError):
                pass

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
        if client_id and len(client_id) > 128:
            raise ValueError("client_id too long")
        repeated = False
        with self.repository_lock() if self.git else nullcontext(), self.write_lock:
            path = self.board / f"{human}.jsonl"
            with path.open("a+") as stream:
                fcntl.flock(stream, fcntl.LOCK_EX)
                stream.seek(0)
                rows = [json.loads(line) for line in stream if line.strip()]
                rec = next((r for r in rows if client_id and r.get("client_id") == client_id
                            and r.get("author") == human), None)
                if rec is not None:
                    if (rec.get("text"), rec.get("topic"), rec.get("reply_to")) != (text, topic, reply_to):
                        raise ValueError("client_id already used for a different payload")
                    os.fsync(stream.fileno())
                    repeated = True
                else:
                    number = max([int(r["id"].split("/")[-1]) for r in rows] or [0]) + 1
                    rec = {"id": f"{human}/{number:04d}", "ts": _now(), "author": human,
                           "topic": topic, "text": text, "room": ROOM, "via": "human"}
                    if reply_to:
                        rec["reply_to"] = reply_to
                    if client_id:
                        rec["client_id"] = client_id
                    stream.write(json.dumps(rec, ensure_ascii=False) + "\n")
                    stream.flush()
                    os.fsync(stream.fileno())
        published = self.publish()
        return {"record": rec, "saved": True, "published": published, "pushed": published,
                "repeated": repeated}

    def publish(self) -> bool:
        """Retry both uncommitted human logs and pending commits, under one repo lock."""
        if not self.git:
            return False
        try:
            with self.repository_lock():
                paths = [str((self.board / f"{human}.jsonl").relative_to(self.repo))
                         for human in set(self.tokens.values())
                         if (self.board / f"{human}.jsonl").exists()]
                if paths:
                    changed = self.git_run("status", "--porcelain", "--", *paths).stdout
                    if changed:
                        self.git_run("add", "--", *paths)
                        self.git_run("commit", "--only", "-q", "-m", "board: human notes", "--", *paths)
                # Never stash another writer's work or attempt conflict repairs.
                if self.git_run("push", check=False).returncode == 0:
                    return True
                if not self.reconcile_remote(paths):
                    return False
                return self.git_run("push", check=False).returncode == 0
        except (OSError, subprocess.SubprocessError, ValueError):
            return False

    def reconcile_remote(self, human_paths: list[str]) -> bool:
        """Rebase only clean, disjoint human commits; leave other cases pending."""
        if self.git_run("status", "--porcelain").stdout:
            return False
        for marker in ("rebase-merge", "rebase-apply", "MERGE_HEAD", "CHERRY_PICK_HEAD"):
            location = self.git_run("rev-parse", "--git-path", marker).stdout.strip()
            if (self.repo / location).exists():
                return False
        self.git_run("fetch")
        upstream = self.git_run("rev-parse", "--abbrev-ref", "@{upstream}").stdout.strip()
        base = self.git_run("merge-base", "HEAD", upstream).stdout.strip()
        own = set(filter(None, self.git_run("log", "--format=", "--no-renames", "--name-only",
                                    f"{base}..HEAD").stdout.splitlines()))
        remote = set(filter(None, self.git_run("log", "--format=", "--no-renames", "--name-only",
                                       f"{base}..{upstream}").stdout.splitlines()))
        if not own.issubset(set(human_paths)) or own & remote:
            return False
        return self.git_run("rebase", "--no-autostash", "--no-update-refs", upstream,
                            check=False).returncode == 0

    def publish_forever(self, stop: threading.Event) -> None:
        while not stop.wait(max(.1, self.publish_every)):
            try:
                self.publish()
            except Exception as exc:
                print(f"publisher retry failed: {exc}", file=sys.stderr)

    def pull_forever(self, stop: threading.Event) -> None:
        while not stop.wait(max(.1, self.pull_every)):
            try:
                self.pull()
            except Exception as exc:
                print(f"pull retry failed: {exc}", file=sys.stderr)

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
                                 "host": me == app.local_human,
                                 "messages": rows, "server_time": _now()})
                return
            if url.path == "/api/review":
                who = app.who(self._token())
                if who is None:
                    self._json(401, {"ok": False, "error": "needs the channel link"})
                    return
                if who != app.local_human:
                    # A review is about whose memory it is. The other person's link
                    # must not read, answer or even see this person's facts.
                    self._json(403, {"ok": False, "error": "this review belongs to the host"})
                    return
                if self.command == "POST":
                    self._json(405, {"ok": False, "error": "post answers to the same path"})
                    return
                limit = int((parse_qs(url.query).get("limit") or ["5"])[0] or 5)
                try:
                    self._json(200, {"ok": True, **app.review_pick(max(1, min(limit, 20)))})
                except (RuntimeError, ValueError) as e:
                    self._json(503, {"ok": False, "error": str(e)})
                return
            if url.path == "/api/review/generated":
                who = app.who(self._token())
                if who is None:
                    self._json(401, {"ok": False, "error": "needs the channel link"})
                    return
                if who != app.local_human:
                    self._json(403, {"ok": False, "error": "this review belongs to the host"})
                    return
                try:
                    self._json(200, {"ok": True, "batch": app.review_generated()})
                except (RuntimeError, ValueError) as e:
                    self._json(503, {"ok": False, "error": str(e)})
                return
            if url.path == "/api/outcomes":
                who = app.who(self._token())
                if who is None:
                    self._json(401, {"ok": False, "error": "needs the channel link"})
                    return
                if who != app.local_human:
                    self._json(403, {"ok": False, "error": "outcomes belong to the host"})
                    return
                self._json(200, {"ok": True, **app.outcomes(
                    force=(parse_qs(url.query).get("fresh") or [""])[0] == "1")})
                return
            if url.path == "/api/reports":
                who = app.who(self._token())
                if who is None:
                    self._json(401, {"ok": False, "error": "needs the channel link"})
                    return
                if who != app.local_human:
                    self._json(403, {"ok": False, "error": "reports belong to the host"})
                    return
                self._json(200, {"ok": True, "reports": app.reports()})
                return
            if url.path == "/api/report":
                who = app.who(self._token())
                if who is None:
                    self._json(401, {"ok": False, "error": "needs the channel link"})
                    return
                if who != app.local_human:
                    self._json(403, {"ok": False, "error": "reports belong to the host"})
                    return
                name = (parse_qs(url.query).get("name") or [""])[0]
                text = app.report_text(name)
                self._json(200 if text is not None else 404,
                           {"ok": text is not None, "name": name, "text": text})
                return
            if url.path == "/api/record":
                if not self._authed():
                    self._json(401, {"ok": False, "error": "needs the channel link"})
                    return
                ident = (parse_qs(url.query).get("id") or [""])[0]
                record = app.resolve(ident)
                self._json(200 if record else 404,
                           {"ok": bool(record), "record": record,
                            "error": None if record else "record unavailable"})
                return
            if url.path == "/api/search":
                if not self._authed():
                    self._json(401, {"ok": False, "error": "needs the channel link"})
                    return
                q = (parse_qs(url.query).get("q") or [""])[0]
                self._json(200, {"ok": True, "records": app.search(q)})
                return
            if url.path == "/api/work":
                if not self._authed():
                    self._json(401, {"ok": False, "error": "needs the channel link"})
                    return
                self._json(200, {"ok": True, "records": app.work()})
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
            if url.path == "/api/review":
                who = app.who(self._token())
                if who is None:
                    self._json(401, {"ok": False, "error": "needs the channel link"})
                    return
                if who != app.local_human:
                    self._json(403, {"ok": False, "error": "this review belongs to the host"})
                    return
                if "application/json" not in (self.headers.get("content-type") or ""):
                    self._json(415, {"ok": False, "error": "send JSON"})
                    return
                length = int(self.headers.get("content-length") or 0)
                if length <= 0 or length > 64_000:
                    self._json(413, {"ok": False, "error": "answers missing or too large"})
                    return
                try:
                    body = json.loads(self.rfile.read(length) or b"{}")
                    answers = body.get("answers") or []
                    if not isinstance(answers, list):
                        raise ValueError("answers must be a list")
                except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as e:
                    self._json(400, {"ok": False, "error": str(e) or "body is not JSON"})
                    return
                try:
                    self._json(200, {"ok": True, **app.review_apply(answers)})
                except RuntimeError as e:
                    self._json(503, {"ok": False, "error": str(e)})
                return
            if url.path == "/api/review/generated":
                who = app.who(self._token())
                if who is None:
                    self._json(401, {"ok": False, "error": "needs the channel link"})
                    return
                if who != app.local_human:
                    self._json(403, {"ok": False, "error": "this review belongs to the host"})
                    return
                if "application/json" not in (self.headers.get("content-type") or ""):
                    self._json(415, {"ok": False, "error": "send JSON"})
                    return
                length = int(self.headers.get("content-length") or 0)
                if length <= 0 or length > 8_000:
                    self._json(413, {"ok": False, "error": "body missing or too large"})
                    return
                try:
                    body = json.loads(self.rfile.read(length) or b"{}")
                except (json.JSONDecodeError, UnicodeDecodeError) as e:
                    self._json(400, {"ok": False, "error": str(e) or "body is not JSON"})
                    return
                if body.get("generate"):
                    try:
                        self._json(200, {"ok": True, **app.review_generate()})
                    except (RuntimeError, ValueError) as e:
                        self._json(503, {"ok": False, "error": str(e)})
                    return
                qid = str(body.get("id") or "").strip()
                value = str(body.get("value") or "").strip()
                batch = str(body.get("batch") or "").strip()
                text = str(body.get("text") or "").strip()[:600]
                if not qid or not value or len(value) > 32:
                    self._json(400, {"ok": False, "error": "id and value are required"})
                    return
                try:
                    outcome = app.review_generated_answer(batch, qid, value, text)
                except ValueError as e:
                    # A refused answer is not a broken server: say why in the writer's words.
                    self._json(409, {"ok": False, "error": str(e)})
                    return
                except RuntimeError as e:
                    self._json(503, {"ok": False, "error": str(e)})
                    return
                self._json(200, {"ok": True, "verdict": outcome.get("verdict")})
                return
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
                client_id = str(body.get("client_id") or "") or None
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
            except (OSError, subprocess.SubprocessError) as e:
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
