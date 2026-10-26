"""Board backend — the agent board for the Hermes desktop.

Mounted at ``/api/plugins/board/`` by the dashboard plugin system. The desktop half
(``desktop/plugin.js``) reaches it through ``ctx.rest`` and never sees a key: this
module reads the identity key from the board's data directory and talks to the board
over its own HTTP API, so the pane and the CLI see exactly the same thing.

Every handler fails open. A board that is asleep, a missing key file or a timeout
returns ``{"ok": false, "error": ...}`` instead of raising, so the pane degrades to a
message rather than a blank tile.

Routes
    GET  /state?channel=<slug>&since=<id>&limit=<n>   channels + messages + delivery
    POST /post  {"text", "channel", "reply_to"?}      post and return the new message
    GET  /health                                      board reachability, for the chip

Nothing here acks: a receipt means "delivered to the human" and the watcher owns that
claim. Reading a message on screen is not evidence the push landed, and a pane that
acked on view would hide exactly the failure the receipts exist to expose.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional, Tuple

from fastapi import APIRouter

router = APIRouter()

# --------------------------------------------------------------------------
# CONFIG
# --------------------------------------------------------------------------

# The board runs on this machine under launchd. BOARD_URL lets the pane point at a
# scratch server while the board is being changed.
BASE_URL = os.environ.get("BOARD_URL", "http://127.0.0.1:8790").rstrip("/")
DATA_DIR = os.path.expanduser(os.environ.get("BOARD_DATA") or "~/.hermes/data/agent-board")

# The identity key, so posts are attributed to hamish-hermes and the shared legacy
# key (scoped to `will`, and on its way out) is not needed at all. Fall back to it
# only if the identity key file is missing.
KEY_FILES = ("hermes.key", "key")

# A board call is a local HTTP request to a stdlib server: 5 s is generous. The read
# that includes a long poll never happens here, so a slow answer means a wedged board
# and the pane should say so rather than hang.
TIMEOUT_S = 5.0

# Idle thresholds, in days, as the board defines them: a channel with no traffic for
# a fortnight is quiet, for two months it is stale. Quiet is cosmetic; stale is what
# stops a dead room from being the pane's default.
QUIET_DAYS = 14
STALE_DAYS = 60


# --------------------------------------------------------------------------
# HELPERS
# --------------------------------------------------------------------------

def _key() -> Optional[str]:
    for name in KEY_FILES:
        path = os.path.join(DATA_DIR, name)
        try:
            with open(path, encoding="utf-8") as fh:
                value = fh.read().strip()
        except OSError:
            continue
        if value:
            return value
    return None


def _call(path: str, key: str, payload: Optional[Dict[str, Any]] = None,
          timeout: float = TIMEOUT_S) -> Tuple[int, Any]:
    """One board call. Returns (status, parsed-json-or-raw). Raises OSError/ValueError."""
    url = BASE_URL + path
    data = None
    headers = {"X-Board-Key": key}
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers,
                                 method="POST" if payload is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return resp.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw)
        except ValueError:
            return exc.code, {"error": raw.decode("utf-8", "replace")[:200]}


def _fail(message: str, **extra) -> Dict[str, Any]:
    return {"ok": False, "error": message, "channels": [], "messages": [], **extra}


def _idle_state(idle_days: Optional[float]) -> str:
    if idle_days is None:
        return "unused"
    if idle_days >= STALE_DAYS:
        return "stale"
    if idle_days >= QUIET_DAYS:
        return "quiet"
    return "live"


# --------------------------------------------------------------------------
# ROUTES
# --------------------------------------------------------------------------

@router.get("/health")
async def health() -> Dict[str, Any]:
    """Is the board answering, and with what version. Never raises."""
    try:
        status, body = _call("/api/health", _key() or "", timeout=3.0)
    except (OSError, ValueError) as exc:
        return {"ok": False, "error": "board unreachable: %s" % exc.__class__.__name__}
    if status != 200:
        return {"ok": False, "error": "board answered %s" % status}
    return {"ok": True, "version": body.get("version"), "messages": body.get("messages"),
            "channels": body.get("channels", {}), "delivery": body.get("delivery", {})}


@router.get("/state")
async def state(channel: Optional[str] = None, since: int = 0, limit: int = 40,
                topic: Optional[str] = None) -> Dict[str, Any]:
    """One screenful: the channels, the selected channel's messages, delivery state."""
    key = _key()
    if not key:
        return _fail("no board key at %s" % os.path.join(DATA_DIR, KEY_FILES[0]))
    query: Dict[str, Any] = {"limit": max(1, min(int(limit), 200))}
    if channel:
        query["channel"] = channel
    if since:
        query["since"] = int(since)
    try:
        status, listing = _call("/api/channels", key)
        if status != 200:
            return _fail("channels refused (%s): %s" % (status, listing.get("error", "")))
        channels = listing.get("channels", [])
        for entry in channels:
            entry["state"] = _idle_state(entry.get("idle_days"))
        # Newest activity first, with an unused room kept but last: a room nobody has
        # ever spoken in must not be the default just because it sorts first.
        channels.sort(key=lambda c: (c.get("last_epoch") or 0), reverse=True)

        selected = channel or (channels[0]["channel"] if channels else None)
        messages: list = []
        if selected:
            row = next((c for c in channels if c["channel"] == selected), {})
            # Read BACKWARDS from the newest id. `GET /api/messages` with no `since`
            # starts at the oldest page (ORDER BY id ASC), so asking for the latest
            # screenful that way returns the beginning of the archive and the pane
            # would look frozen a month in the past. Same trap that pinned the triage
            # gate to id 34 for four days.
            params = dict(query, channel=selected)
            # A topic is a tag inside the room: filtering here is what stops one long
            # conversation looking like the whole board.
            if topic:
                params["topic"] = topic
            if not since and row.get("last_id"):
                params["before"] = int(row["last_id"]) + 1
            status, body = _call("/api/messages?" + urllib.parse.urlencode(params), key)
            if status != 200:
                return _fail("messages refused (%s): %s" % (status, body.get("error", "")),
                             channels=channels)
            messages = body.get("messages", [])
            if params.get("before"):
                messages.reverse()      # oldest→newest for display

        # Topic index for this channel: the chips that stop the pane reading as one
        # endless thread. Failure here costs navigation, not the view.
        topics: list = []
        try:
            tpath = "/api/topics" + (("?channel=" + urllib.parse.quote(selected)) if selected else "")
            tstatus, tbody = _call(tpath, key)
            if tstatus == 200:
                topics = tbody.get("topics", [])
        except (OSError, ValueError):
            topics = []

        delivery: Dict[str, Any] = {}
        try:
            _, dbody = _call("/api/delivery", key)
            delivery = {"peers": dbody.get("peers", []), "channels": dbody.get("channels", [])}
        except (OSError, ValueError):
            delivery = {}      # the pane is still useful without it

        # The inbox is what the human opens the pane for: what is waiting on him. A
        # board that cannot answer that is a log, and a log is what the agent reads.
        inbox: Dict[str, Any] = {"asks": [], "asks_by_me": [], "counts": {}}
        try:
            path = "/api/inbox" + (("?channel=" + urllib.parse.quote(selected)) if selected else "")
            status, ibody = _call(path, key)
            if status == 200:
                inbox = ibody
        except (OSError, ValueError):
            inbox = {"asks": [], "asks_by_me": [], "counts": {}, "error": "inbox unavailable"}

        return {"ok": True, "channels": channels, "channel": selected,
                "messages": messages, "delivery": delivery, "inbox": inbox,
                "topics": topics, "topic": topic,
                "quiet_days": QUIET_DAYS, "stale_days": STALE_DAYS}
    except (OSError, ValueError) as exc:
        return _fail("board unreachable: %s" % exc.__class__.__name__)


@router.get("/search")
async def search(q: str = "", channel: Optional[str] = None, limit: int = 30) -> Dict[str, Any]:
    """Ranked full-text search. The agent has `board.py search`; this is the same index."""
    key = _key()
    if not key or not q.strip():
        return {"ok": False, "error": "nothing to search for", "results": []}
    query: Dict[str, Any] = {"q": q.strip(), "limit": max(1, min(int(limit), 100))}
    if channel:
        query["channel"] = channel
    try:
        status, body = _call("/api/search?" + urllib.parse.urlencode(query), key)
    except (OSError, ValueError) as exc:
        return {"ok": False, "error": "board unreachable: %s" % exc.__class__.__name__, "results": []}
    if status != 200:
        return {"ok": False, "error": body.get("error") or ("board answered %s" % status),
                "results": []}
    return {"ok": True, "query": q.strip(), "count": body.get("count", 0),
            "results": body.get("results", [])}


@router.get("/file/{fid}")
async def file_preview(fid: int) -> Dict[str, Any]:
    """One attachment: metadata always, the text itself when it is text and small.

    A pane that can read an attached note without leaving the app is the difference
    between "see #50" and actually reading it; a binary is described instead, because
    rendering a download inside a chat surface is not this plugin's job.
    """
    key = _key()
    if not key:
        return {"ok": False, "error": "no board key"}
    url = BASE_URL + "/api/files/%d" % fid
    request = urllib.request.Request(url, headers={"X-Board-Key": key})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as resp:
            blob = resp.read()
            name = resp.headers.get("X-File-Name") or ("file-%d" % fid)
            ctype = (resp.headers.get("Content-Type") or "").split(";")[0]
            sha = resp.headers.get("X-File-Sha256") or ""
    except urllib.error.HTTPError as exc:
        return {"ok": False, "error": "board answered %s" % exc.code}
    except (OSError, ValueError) as exc:
        return {"ok": False, "error": "board unreachable: %s" % exc.__class__.__name__}

    text_like = ctype.startswith("text/") or ctype in ("application/json", "application/xml")
    body: Dict[str, Any] = {"ok": True, "id": fid, "name": name, "ctype": ctype,
                            "size": len(blob), "sha256": sha, "text": None}
    if text_like and len(blob) <= 40_000:
        body["text"] = blob.decode("utf-8", "replace")
    elif text_like:
        body["note"] = "too large to show here (%d bytes) — use board.py get %d" % (len(blob), fid)
    else:
        body["note"] = "%s · %d bytes — use board.py get %d" % (ctype or "binary", len(blob), fid)
    return body


@router.post("/post")
async def post(body: Dict[str, Any]) -> Dict[str, Any]:
    """Post as hamish-hermes. Same validation as the CLI, done by the board itself."""
    key = _key()
    if not key:
        return {"ok": False, "error": "no board key at %s" % os.path.join(DATA_DIR, KEY_FILES[0])}
    text = (body.get("text") or "").strip()
    if not text:
        return {"ok": False, "error": "nothing to post"}
    payload: Dict[str, Any] = {"text": text, "author": "hamish-hermes"}
    if body.get("channel"):
        payload["channel"] = body["channel"]
    if body.get("topic"):
        payload["topic"] = str(body["topic"])[:48]
    # The pane is the human typing, not the agent posting on his behalf; the board records
    # which voice it was so a quote can never be attributed to the wrong one.
    payload["via"] = "human" if body.get("via") == "human" else "agent"
    if body.get("reply_to"):
        payload["reply_to"] = int(body["reply_to"])
    try:
        status, answer = _call("/api/messages", key, payload)
    except (OSError, ValueError) as exc:
        return {"ok": False, "error": "board unreachable: %s" % exc.__class__.__name__}
    if status not in (200, 201):
        return {"ok": False, "error": answer.get("error") or ("board answered %s" % status)}
    # The POST answers with nulls for author/topic/files, which is not a failed write;
    # the pane re-reads the channel rather than trusting the echo.
    return {"ok": True, "message": answer.get("message", {})}
