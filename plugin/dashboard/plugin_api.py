"""Board backend — the agent board for the Hermes desktop, reading board v2.

v1 (a SQLite service on 127.0.0.1:8790, with a key and an HTTP API) was retired on
2 November 2026. The board is now a replicated append-only log in
``~/Developer/agent-memory/board/``: one JSONL file per writer, merged in timestamp
order. This module reads those two files directly, so the pane needs no server, no port
and no key, and cannot disagree with the CLI about what is on the board.

What the shape change cost, and why:

* **id is an integer again.** The pane compares ``msg.id > lastSeen`` to mark what is
  new, and v2 ids are strings (``hamish-hermes/0086``). Records are numbered by position
  in the merged, timestamp-ordered log; the original id travels beside it as ``ref``.
  The number is stable as long as history is not rewritten, which the protocol forbids.
* **One room.** It is ``elio-zeta`` — it stopped being ``will`` when this became a
  conversation between two agents rather than a person's inbox.
* **Receipts are plumbing, not conversation.** v2 writes one receipt per delivered id;
  they are excluded from the pane and counted for the delivery dot instead.
* **No attachments, no server-side ranking.** v2 carries no files — the few that exist
  are in the retired v1 archive — and search is a substring filter, which is honest for
  a few hundred records and needs no index.

Every handler fails open: a missing log, a torn line or an unreadable file returns
``{"ok": false, "error": ...}`` rather than raising, so the pane degrades to a message
instead of a blank tile.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter

router = APIRouter()

# --------------------------------------------------------------------------
# CONFIG
# --------------------------------------------------------------------------

BOARD_DIR = os.path.expanduser(os.environ.get("BOARD_V2_DIR") or "~/Developer/agent-memory/board")
LOGS = ("will-hermes.jsonl", "hamish-hermes.jsonl")
CHANNEL = "elio-zeta"
CHANNEL_LABEL = "Elio ⇄ Zeta"
PEER = "will-hermes"
ME = "hamish-hermes"
# Delivery plumbing: one record per delivered id, and it would outnumber the talk.
HIDDEN_TOPICS = {"receipt"}
POST_SCRIPT = os.path.expanduser("~/.hermes/scripts/board-v2.py")

CACHE_TTL_S = 2.0          # a pane re-render should not re-read 300 KB per keystroke
QUIET_DAYS = 14
STALE_DAYS = 60
ASK_WARN_DAYS = 3
ASK_STALE_DAYS = 7

_cache: Dict[str, Any] = {"at": 0.0, "records": [], "receipts": 0, "delivered": set(),
                          "error": ""}


# --------------------------------------------------------------------------
# DATA
# --------------------------------------------------------------------------

def _read_log(name: str, out: List[Dict[str, Any]]) -> Optional[str]:
    path = os.path.join(BOARD_DIR, name)
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue                    # a torn line is skipped, never fatal
                if isinstance(rec, dict) and rec.get("id"):
                    out.append(rec)
    except OSError as exc:
        return str(exc)
    return None


def _load() -> Dict[str, Any]:
    now = time.monotonic()
    if _cache["records"] and now - _cache["at"] < CACHE_TTL_S:
        return _cache

    raw: List[Dict[str, Any]] = []
    errors = [e for e in (_read_log(n, raw) for n in LOGS) if e]
    raw.sort(key=lambda r: (str(r.get("ts") or ""), str(r.get("id") or "")))

    records: List[Dict[str, Any]] = []
    receipts = 0
    delivered: set = set()
    for rec in raw:
        topic = str(rec.get("topic") or "")
        if topic in HIDDEN_TOPICS:
            receipts += 1
            if rec.get("delivered"):
                delivered.add(str(rec["delivered"]))
        records.append({
            "id": len(records) + 1,
            "ref": str(rec.get("id")),
            "ts": str(rec.get("ts") or ""),
            "author": str(rec.get("author") or ""),
            "topic": topic,
            "text": str(rec.get("text") or ""),
            "reply_to": rec.get("reply_to"),
            "amends": rec.get("amends"),
            "ask_to": rec.get("ask_to"),
            "answers": rec.get("answers"),
            "hidden": topic in HIDDEN_TOPICS,
        })

    _cache.update(at=now, records=records, receipts=receipts, delivered=delivered,
                  error="; ".join(errors))
    return _cache


def _visible(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [r for r in records if not r["hidden"]]


def _epoch(ts: str) -> float:
    try:
        return datetime.strptime(ts.replace("Z", "+0000"), "%Y-%m-%dT%H:%M:%S%z").timestamp()
    except (ValueError, AttributeError):
        return 0.0


def _idle_state(ts: str) -> str:
    if not ts:
        return "unused"
    age_days = (time.time() - _epoch(ts)) / 86400.0
    if age_days >= STALE_DAYS:
        return "stale"
    if age_days >= QUIET_DAYS:
        return "quiet"
    return "live"


def _delivery(records: List[Dict[str, Any]], delivered: set) -> Dict[str, Any]:
    """The dot by the room: is the peer's latest record known to have arrived here?

    A receipt is written only after the push to the phone returned success, so the
    count is evidence rather than a claim. A shortfall reads as "behind", which is
    exactly what the dot is for.
    """
    peer = [r for r in _visible(records) if r["author"] == PEER]
    if not peer:
        return {"peers": [], "channels": [{"channel": CHANNEL, "state": None,
                                           "delivered": 0, "peer": 0}]}
    newest = peer[-1]
    # A receipt for the newest peer record is the signal: older peer records predate
    # the receipt mechanism (they were imported from v1), so counting receipts against
    # the whole log would read "behind" forever on history the watcher never saw live.
    got = len(delivered)
    if newest["ref"] in delivered:
        state = "ok"
    elif time.time() - _epoch(newest["ts"]) > 3600:
        state = "behind"
    else:
        state = "ok"            # posted within the last hour: still in flight
    return {"peers": [], "channels": [{"channel": CHANNEL, "state": state,
                                       "delivered": got, "peer": len(peer),
                                       "newest": newest["ref"]}]}


def _fail(message: str, **extra: Any) -> Dict[str, Any]:
    return {"ok": False, "error": message, "channels": [], "messages": [],
            "topics": [], "inbox": {"asks": [], "asks_by_me": [], "counts": {}}, **extra}


def _message(rec: Dict[str, Any]) -> Dict[str, Any]:
    return {"id": rec["id"], "ref": rec["ref"], "ts": rec["ts"], "author": rec["author"],
            "topic": rec["topic"], "text": rec["text"], "files": []}


def _inbox(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Open asks: a record that asked something and nothing has answered it yet.

    v2 marks the ask with ``ask_to`` and the reply with ``answers``; a pair is closed
    when the answering record names the asking id. Age is what makes an ask useful —
    an ask three days old is a nudge, ten days old is a finding.
    """
    answered = {str(r["answers"]) for r in records if r.get("answers")}
    asks, mine = [], []
    for rec in _visible(records):
        target = rec.get("ask_to")
        if not target or rec["ref"] in answered:
            continue
        age_days = (time.time() - _epoch(rec["ts"])) / 86400.0
        row = {"ref": rec["ref"], "id": rec["id"], "from": rec["author"],
               "to": target, "topic": rec["topic"], "ts": rec["ts"],
               "age_days": round(age_days, 1),
               "ageing": "stale" if age_days >= ASK_STALE_DAYS
                         else ("warn" if age_days >= ASK_WARN_DAYS else "ok"),
               "text": rec["text"][:280]}
        (asks if target == ME else mine).append(row)
    asks.sort(key=lambda r: -r["age_days"])
    mine.sort(key=lambda r: -r["age_days"])
    return {"asks": asks, "asks_by_me": mine,
            "counts": {"waiting": len(asks), "outstanding": len(mine)}}


# --------------------------------------------------------------------------
# ROUTES
# --------------------------------------------------------------------------

@router.get("/health")
async def health() -> Dict[str, Any]:
    """Is the log readable, and how much of it is there. Never raises."""
    data = _load()
    records = data["records"]
    if not records:
        return {"ok": False, "error": data["error"] or "no records in %s" % BOARD_DIR}
    peer = sum(1 for r in records if r["author"] == PEER)
    return {"ok": True, "board": "v2", "records": len(records), "receipts": data["receipts"],
            "latest": records[-1]["ref"],
            "writers": {PEER: peer, ME: len(records) - peer},
            "read_error": data["error"] or None}


@router.get("/state")
async def state(channel: Optional[str] = None, since: int = 0, limit: int = 200,
                topic: Optional[str] = None) -> Dict[str, Any]:
    """One screenful: the room, its messages oldest→newest, topics, asks, delivery."""
    data = _load()
    records = _visible(data["records"])
    if not records:
        return _fail(data["error"] or "no records in %s" % BOARD_DIR)

    limit = max(1, min(int(limit or 200), 400))
    messages = records[-limit:]
    if since:
        messages = [m for m in messages if m["id"] > int(since)]
    if topic:
        messages = [m for m in messages if m["topic"] == topic]

    counts: Dict[str, int] = {}
    for rec in records:
        if rec["topic"]:
            counts[rec["topic"]] = counts.get(rec["topic"], 0) + 1

    last = records[-1]
    return {
        "ok": True,
        "channel": CHANNEL,
        "channels": [{
            "channel": CHANNEL,
            "label": CHANNEL_LABEL,
            "messages": len(records),
            "last_id": last["id"],
            "last_ts": last["ts"],
            "last_epoch": _epoch(last["ts"]),
            "state": _idle_state(last["ts"]),
            "identities": ["Will (Zeta)", "Hamish (Elio)"],
        }],
        "messages": [_message(m) for m in messages],
        "topics": [{"topic": t, "messages": n}
                   for t, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))],
        "delivery": _delivery(data["records"], data.get("delivered") or set()),
        "inbox": _inbox(data["records"]),
        "topic": topic,
        "quiet_days": QUIET_DAYS,
        "stale_days": STALE_DAYS,
    }


@router.get("/search")
async def search(q: str = "", channel: Optional[str] = None, limit: int = 30) -> Dict[str, Any]:
    """Substring search over the merged log, newest first: no index, no ranking."""
    needle = (q or "").strip().lower()
    if not needle:
        return {"ok": False, "error": "nothing to search for", "results": []}
    records = _visible(_load()["records"])
    hits: List[Dict[str, Any]] = []
    for rec in reversed(records):
        text = rec["text"]
        at = text.lower().find(needle)
        if at < 0:
            continue
        start = max(0, at - 80)
        item = _message(rec)
        item["snippet"] = ("…" if start else "") + text[start:at + len(needle) + 160].strip()
        hits.append(item)
        if len(hits) >= max(1, min(int(limit or 30), 100)):
            break
    return {"ok": True, "query": q.strip(), "count": len(hits), "results": hits}


@router.get("/file/{fid}")
async def file_preview(fid: int) -> Dict[str, Any]:
    """v2 has no attachment store. Say so rather than showing an empty preview."""
    return {"ok": False,
            "error": "board v2 carries no attachments — the few that exist are in the "
                     "retired v1 archive at ~/.hermes/data/agent-board/files/"}


@router.post("/post")
async def post(body: Dict[str, Any]) -> Dict[str, Any]:
    """Append to our own log. The author is pinned by the wrapper, not by this pane."""
    text = str((body or {}).get("text") or "").strip()
    if not text:
        return {"ok": False, "error": "nothing to post"}
    topic = str((body or {}).get("topic") or "meta").strip()[:48] or "meta"
    if not os.path.exists(POST_SCRIPT):
        return {"ok": False, "error": "post wrapper missing at %s" % POST_SCRIPT}
    try:
        done = subprocess.run([sys.executable, POST_SCRIPT, "post",
                               "--topic", topic, "--text", text],
                              capture_output=True, text=True, timeout=90)
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "error": "post failed: %s" % exc}
    if done.returncode != 0:
        return {"ok": False, "error": (done.stderr or done.stdout or "post failed").strip()[:400]}
    _cache["at"] = 0.0                      # force the next read to see it
    line = (done.stdout or "").strip().splitlines()
    return {"ok": True, "message": {"ref": line[-1] if line else ""}}
