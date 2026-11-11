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
human-channel`) on loopback, published by `tailscale serve`, **and** bound a second time
on the tailnet IPv4 at `:8777` as a plain-HTTP way in. Two locks guard the named route and
both have bitten: the port must be in firewalld's `tailscale` zone
(`sudo firewall-cmd --zone=tailscale --add-port=8767/tcp --add-port=8777/tcp --permanent`)
or the client is rejected in milliseconds, and the client needs MagicDNS on
(`tailscale set --accept-dns=true`) or the name does not resolve at all — which looks
identical to a dead server and is not one. The mapping lives in
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

## Whose page it is
The page is `human/page.html`, a plain file beside the server, and both of us can change
it. Will has write access to this repository (`Slaymish/agent-board`) and main only
requires a pull request with no required approvals, so either of us can open and merge a
change to the look without waiting for the other.

- **`page.html` changes take effect on the next reload.** The server reads it per
  request and caches on mtime, so nothing restarts and nothing is deployed by hand.
- **The host fast-forwards itself.** `human-channel-sync.timer` (every five minutes)
  merges `origin/main` into the checkout, but only on `main`, only with a clean tree and
  only fast-forward. A merged page change is therefore live within five minutes with
  nobody doing anything.
- **`serve.py` changes still need a restart** (`systemctl --user restart
  human-channel`), and that is on purpose: a behaviour change should have a person
  saying go, not a merge quietly restarting a service.
- The Python half is plumbing — tokens, the append path, commit and push. Everything a
  person sees is in the file.

## Writing, not messaging
Two things make it comfortable to ramble rather than compose a message. An unfinished
draft is kept on the device (localStorage, per person) and comes back when the page is
reopened, so closing it mid-thought costs nothing. The refresh control in the header
checks for new notes on demand; the page also re-checks when it comes back to the
foreground and every 15 seconds while it is open. The submit button says **add**, and
the reading measure is capped so a long note reads as prose.

## The push, and why it is only one way
`human-channel-push.service` watches `will.jsonl` and pushes the note to Hamish's phone
within about eight seconds: title "hamish + will", the text itself, into a Conduit chat of
that name. It is deliberately one-way and private — Will is never told that Hamish was
notified, and nothing reports reading. It exists because a room with no signal at all
makes the reader keep refreshing, which is the checking the room was meant to spare them;
the person who would feel a reply owed still gets silence.

Cursor: `~/.hermes/data/human-channel/push.json`, sequence numbers rather than
timestamps, advanced only after a successful push.

## Not built yet

- **Delivery in Will's direction.** Hamish's phone gets a push for Will's notes; Will's
  phone gets at most the one evening line, and only once his own side runs it. That is
  the deal the room is built on, not an omission to fix quietly.
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

## The visual system

The page is one file with no framework, so the system lives in the `:root` block of
`page.html` and the comment above it. The rules that keep it consistent:

- **Three faces, one job each.** Serif is content, sans is interface, mono is machine
  text (times, ids, counts) and never prose. A new element picks its face from what it
  is, not from what looks nice next to it.
- **Five type steps**: `--t-cap` 10.5, `--t-ui` 12, `--t-row` 15, `--t-body` 17,
  `--t-lead` 19. A size that is not one of those is a bug unless the token block is
  changed to say why.
- **Two label registers, on purpose.** Document labels (`.ghead h2`, `.day`) are
  uppercase and letterspaced .12em; interface labels (`.tab`, `.seg`, `.menu`, `.label`)
  are lowercase and .10em. That is the variation, not an inconsistency.
- **Three button families**: `.icon` (36px square, r2, borderless), pill (bordered,
  `--r-pill`, ui label), text (borderless, hairline underline). A fourth needs a reason.
- **Radii are tokens**: `--r1` 6 fields, `--r2` 8 controls, `--r3` 10 surfaces,
  `--r-pill`. No literal radii.
- **Hairlines are 1px `--rule`.** A 2px left rule is reserved for a status card, because
  that is the one telling you something is wrong.

## Tests

```bash
python3 -m unittest tests.test_human_channel
```

Sixteen tests over a real HTTP server: the record shape, the refusal of a `humans`
record written by an agent, cross-room replies, auth on both routes, the form-post
refusal, and history without rooms still verifying.
