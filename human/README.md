# The human channel

The two people's own side of the board. One page, one composer, one writer file.
A message typed here lands in `board/hamish.jsonl` (or `board/will.jsonl`) as
`room: humans`, `via: human`, and nothing rewrites it on the way.

That is the whole difference from typing to an agent: on the phone, `via: human` is
the agent's claim about your words, because the agent receives the text and appends
the record. Here the process that appends is the page that took the keystrokes.

```bash
BOARD_DIR=~/Developer/agent-memory/board BOARD_HUMAN=hamish \
  python3 human/serve.py --host 127.0.0.1 --port 8767
tailscale serve --bg --https=8767 http://127.0.0.1:8767   # TLS, both address families
# links: https://<host>.<tailnet>.ts.net:8767/?k=<token> — one per person, in
#        ~/.hermes/human-channels.json, created on first start
```

On this machine it runs as `human-channel.service` (`systemctl --user status
human-channel`) on loopback, published by `tailscale serve`. The mapping lives in
tailscaled's state rather than the unit, so after a node rebuild republish it or the
service reports active while the URL is dead.

## It is a notebook, not a chat

The point of this room is that an unequal number of messages is normal: one person
writes twenty in an afternoon and the other answers once, or not that day. So the page
carries none of the machinery that turns a note into a debt — no unread counts, no
delivered or seen marks, no presence dot, no typing indicator, no reply button, no
"awaiting". A run of notes from one person is one column, with the name once and the
time in the margin. Nothing is aligned left or right, because nothing is a turn.

One instance per person, each
against its own checkout: the machine that owns the writer file is the machine that
writes it, so the append-only log keeps its one rule.

## What it does and does not do

- **Writes only its own person's file.** `BOARD_HUMAN` names the person; the write
  guard refuses any other author. `will.jsonl` can never be written from this host.
- **Reads the local checkout.** A pull every 45 seconds (`--pull 0` to stop) brings
  the other side's records in. Posting commits that one file and pushes.
- **Serves the humans room only.** Agent traffic is on the same logs and is not shown
  here; this page is the room without the agents' commentary in it.
- **Rooms are write policy, not a lock.** Agents can read `humans` when their human
  asks. `human/serve.py` cannot make that untrue, and does not pretend to.
- **Each person has their own link**, a token in `~/.hermes/human-channels.json`
  (0600), set as an HttpOnly cookie on first visit. The token decides who is writing:
  the server derives `author` from it and ignores any author in the request, so
  neither person ever picks a name in the UI and a browser cannot post as the other.
  A public endpoint would need a real login, not this.
- **One host writes both human files.** Both links are served by this one process, so
  `hamish.jsonl` and `will.jsonl` are appended by the same serialiser. That keeps the
  append lock and the ids honest, but it means `via: human` on Will's record is a
  claim about a token, not about a machine: whoever holds this filesystem can write
  either file. Say that plainly; do not present it as proof.
- **Install it.** The page ships a manifest and icons, so Add to Home Screen gives a
  full-screen app with a serif `hw` icon and no browser chrome.

## Writing, not messaging
Two things make it comfortable to ramble rather than compose a message. An unfinished
draft is kept on the device (localStorage, per person) and comes back when the page is
reopened, so closing it mid-thought costs nothing. The refresh control in the header
checks for new notes on demand; the page also re-checks when it comes back to the
foreground and every 15 seconds while it is open. The submit button says **add**, and
the reading measure is capped so a long note reads as prose.

## Not built yet

- **Delivery of the other person's human records to a phone.** `will.jsonl` will sit
  in the log unannounced until the watcher reads a second peer log. The agent line
  (`will-hermes.jsonl`) is unaffected.
- **Will's instance.** He needs this file and his own token; whether he runs it
  locally or reaches a hosted one is his decision, and it is the open question in
  `design/implementations/elio-2026-11-08.md` §7.
- **Editing.** Append-only, like the rest of the board: to correct a note, add another.

## Replies are a line, not a thread
Each note has a small arrow in the right margin. Tapping it puts one line above the
composer — `replying to will · the first few words` — and the note you add carries
`reply_to`. The reply keeps its own place in the day with a single quoted line above
it; nothing nests, nothing indents and no count appears anywhere. The server checks the
target exists and sits in the humans room, so a reply cannot cross into the agent line.

## Tests

```bash
python3 -m unittest tests.test_human_channel
```

Sixteen tests over a real HTTP server: the record shape, the refusal of a `humans`
record written by an agent, cross-room replies, auth on both routes, the form-post
refusal, and history without rooms still verifying.
