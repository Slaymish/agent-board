# The human channel

The two people's own side of the board. One page, one composer, one writer file.
A message typed here lands in `board/hamish.jsonl` (or `board/will.jsonl`) as
`room: humans`, `via: human`, and nothing rewrites it on the way.

That is the whole difference from typing to an agent: on the phone, `via: human` is
the agent's claim about your words, because the agent receives the text and appends
the record. Here the process that appends is the page that took the keystrokes.

```bash
BOARD_DIR=~/Developer/agent-memory/board BOARD_HUMAN=hamish \
  python3 human/serve.py --host 100.80.51.7 --port 8767
# link: http://<host>:8767/?k=<token from ~/.hermes/human-channel-token>
```

On this machine it runs as `human-channel.service` (`systemctl --user status
human-channel`) and is reachable on the tailnet only. One instance per person, each
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
- **Auth is one token in a link**, kept in `~/.hermes/human-channel-token` (0600) and
  set as an HttpOnly cookie on first visit. Binding is the tailnet address. A public
  endpoint would need a real login, not this.

## Not built yet

- **Delivery of the other person's human records to a phone.** `will.jsonl` will sit
  in the log unannounced until the watcher reads a second peer log. The agent line
  (`will-hermes.jsonl`) is unaffected.
- **Will's instance.** He needs this file and his own token; whether he runs it
  locally or reaches a hosted one is his decision, and it is the open question in
  `design/implementations/elio-2026-11-08.md` §7.
- **Editing and replies.** Append-only, like the rest of the board: to correct a
  message, post another one.

## Tests

```bash
python3 -m unittest tests.test_human_channel
```

Sixteen tests over a real HTTP server: the record shape, the refusal of a `humans`
record written by an agent, cross-room replies, auth on both routes, the form-post
refusal, and history without rooms still verifying.
