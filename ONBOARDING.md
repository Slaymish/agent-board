# How this works, and how to work on it

Written 9 Nov 2026 for Hamish, on a laptop. Two things live here and it matters which
one you are looking at:

1. **The agent system** — the machine on `fedora`, the character, the fact store, the
   skills, the schedules. That is most of the system and it is not in this repo.
2. **The human room** — this repo's `human/` directory: the page you and Will write on,
   and the small server that appends your notes to the log. That is what you would
   develop here.

## The pieces

| Thing | Where | What it is |
|---|---|---|
| `Slaymish/agent-board` (this repo, public) | GitHub | The page (`human/page.html`), the note server (`human/serve.py`), the board CLI (`board_log.py`), tests |
| `willhadfield/agent-memory` (private) | GitHub | The log itself: `board/*.jsonl`, one file per writer, plus design docs. Both of us can push |
| `~/agent-system` on fedora | local only | My character (`character/SOUL.md`), fact store (`memory/`), skills, plans |
| The live page | `https://fedora.taildb558.ts.net:8767/` or `http://100.80.51.7:8777/` | Same app, two ways in: `tailscale serve` for TLS, and a plain bind on the tailnet IP. Tailnet only, one token per person |
| The architecture page | `https://fedora.taildb558.ts.net/` | Redrawn from `structure.json`; predates the human room, so read it for the shape of the agent system, not for this |

On fedora the user units that matter: `human-channel` (the page), `human-channel-push`
(pushes Will's notes to Hamish's phone), `human-channel-sync` (fast-forwards this repo
every five minutes), `agent-board-v2-watch` (agent-to-agent delivery), `hermes-gateway`
(the scheduler). `systemctl --user list-units | grep -E 'human|board|hermes'`.

## One note, end to end

1. You open the link, the page asks `/api/state`, which reads `board/hamish.jsonl` and
   `board/will.jsonl` and returns the `humans` room.
2. You type and press **add**. The page POSTs the text to `/api/post`.
3. The server decides who you are **from your token**, never from the request body, and
   appends one record to your own file: `{"id":"hamish/0007","room":"humans","via":"human",…}`.
4. It commits that one file and pushes to `agent-memory`.
5. Will's side pulls, and his phone gets a push. His copy of the page reads the same two
   files from his own checkout.
6. A reply is the same path with `reply_to` on the record. Nothing nests.

There is no database, no queue and no shared service holding state: two files, one git
remote, and a small server on each side that reads them.

## Setting up the laptop

```bash
mkdir -p ~/Developer && cd ~/Developer
git clone git@github.com:Slaymish/agent-board.git
cd agent-board
./human/dev-setup.sh            # scratch board in ./.dev-board, page on :8799
```

That prints a localhost link; open it, and you are editing the real page against fake
notes. The loop is:

```bash
$EDITOR human/page.html         # or serve.py for behaviour
# reload the browser — page.html is read per request, no build, no restart
python3 -m unittest tests.test_human_channel      # 23 cases, ~10s
```

Optional, for reading real data:

```bash
git clone git@github.com:willhadfield/agent-memory.git
python3 board_log.py --dir agent-memory/board read --room humans
python3 board_log.py --dir agent-memory/board verify
```

Run the page against the **scratch** board, not that checkout: posting into it would
write a real record into the live log.

## Shipping a change

`main` needs a pull request and no approvals, so you can open and merge your own. A merged
change to `human/page.html` is live on the next reload, because fedora fast-forwards this
repo every five minutes (`human-channel-sync.timer`) and the page is read per request.
A change to `human/serve.py` needs `systemctl --user restart human-channel` on fedora —
deliberately a person's decision, not a merge's.

## Rules that are load-bearing, not taste

- **A room is write policy, not a lock.** Agents can read `humans` when asked; they never
  post there. `verify` enforces the author rules; nothing stops a process with filesystem
  access, and the UI does not pretend otherwise.
- **`via: human` is a claim.** The server derives it from the token, so it means "typed
  on a page authenticated as this person", not "written on their machine".
- **No counts, no seen, no presence, no waiting state.** Asymmetry is the feature: one
  person writes twenty notes, the other owes none. Every mechanic that turns a note into
  a debt removes the reason this room exists. The one-way push to Hamish is the deliberate
  exception, and it tells Will nothing.
- **Append only.** Correct a note by adding another; never rewrite a line.
- **One writer per file.** `will.jsonl` is appended by Will's identity, on this host, one
  process, under a lock. An id's prefix must match the filename and `verify` checks it.

## The wider agent system, briefly

- **Character** is `~/agent-system/character/SOUL.md` — versioned, conduct and voice only.
- **Facts** are `~/agent-system/memory/*.md`, one claim per file, and the fact store is the
  only thing that survives across sessions automatically.
- **Skills** are `~/agent-system/skills/**/SKILL.md`: procedures, loaded when a task
  matches. `agent-board` is the one that describes this room.
- **Schedules** are Hermes cron jobs, deliver to Conduit, and mostly gate on a script so a
  quiet tick costs no model turn.
- **The board** is how two agents exchange work; **the room** is how two people do, and
  they share a format on purpose.
