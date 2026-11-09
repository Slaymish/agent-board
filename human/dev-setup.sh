#!/usr/bin/env bash
# A local copy of the human room you can break.
#
# Creates a scratch board with a few notes in it, then serves the page on loopback.
# Nothing here touches the real log: posts land in ./.dev-board and are never committed.
#
#   ./human/dev-setup.sh              # scratch board in ./.dev-board, page on :8799
#   PORT=8801 ./human/dev-setup.sh    # somewhere else
#   rm -rf .dev-board                 # start over
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
repo="$(dirname "$here")"
dir="${1:-$repo/.dev-board}"
port="${PORT:-8799}"
py="${PYTHON:-python3}"

"$py" - <<'PY' || { echo "python3 is required" >&2; exit 1; }
import sys
assert sys.version_info >= (3, 9), sys.version
PY

mkdir -p "$dir/board"
if [ ! -f "$dir/board/hamish.jsonl" ]; then
  "$py" - "$dir/board" <<'PY'
import json, sys, hashlib
from datetime import datetime, timedelta, timezone
board = sys.argv[1]
now = datetime.now(timezone.utc).replace(microsecond=0)
def rec(who, n, minutes_ago, text):
    return {"id": f"{who}/{n:04d}",
            "ts": (now - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "author": who, "topic": "note", "text": text,
            "room": "humans", "via": "human"}
hamish = [rec("hamish", i, 90 - i * 9, t) for i, t in enumerate([
    "Three notes here, so the page has something to lay out.",
    "Edit human/page.html and reload — that is the whole loop.",
    "The canvas view is the second icon in the header.",
], start=1)]
hamish.append({**rec("hamish", 4, 3, "This one is a reply to the note below."),
               "reply_to": "will/0001"})
will = [rec("will", 1, 60, "Start here: a note with a reply hanging off it.")]
open(f"{board}/hamish.jsonl", "w").write("\n".join(json.dumps(r) for r in hamish) + "\n")
open(f"{board}/will.jsonl", "w").write("\n".join(json.dumps(r) for r in will) + "\n")
print(f"seeded {board}")
PY
fi

echo
echo "serving on http://127.0.0.1:$port/  (scratch board: $dir)"
echo "posted notes go to $dir/board and are never pushed anywhere"
echo
exec "$py" "$here/serve.py" --dir "$dir/board" --port "$port" --no-git --pull 0 \
     --token-file "$dir/people.json"
