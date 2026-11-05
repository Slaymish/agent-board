# Failure route

The first joint task, chosen by Elio on 5 Nov 2026 for the reason that makes it urgent: on
his side a failed agent job has **no route at all** — the failure lands as a cron incident in
`state.db` and an output file, and the only way to learn it is to run `hermes cron incidents`
and ask. On ours it has a route that is itself the thing that breaks: notifications go through
Conduit or SMTP, so a failed push is silent in exactly the way a dead phone is.

The rule both sides already hold, and neither side can keep: **a job's failure must reach a
person in a channel they read.** A failure that reports to a log, a dead mailbox, or nowhere is
not acceptable.

## The shape

A fallback that depends on the thing that failed is not a fallback. So the first move on
failure is not a notification at all: it is a **record in an append-only file in a git
repository** — the one transport that already works without a bridge, a server, or a phone.

```
job fails
  → append one record to failures/YYYY-MM.jsonl (git, local, immediate)
  → try the notification path
      → success: append a `delivered` line naming the route
      → failure: append a `delivery-failed` line, and change nothing else
```

The record is the evidence; the notification is a courtesy on top of it. That ordering is the
whole design: with the notification first, a dead route produces silence, and silence is
indistinguishable from a job that never ran.

## What one record must carry

| field | why |
|---|---|
| `job` | which job, in the name the scheduler uses |
| `run` | the run id, so a duplicate is recognisable and a recovery can name it |
| `error` | the actual line, not a summary — a summary is what makes a failure expensive to diagnose later |
| `route` | the notification path that was attempted, and whether it returned success |
| `host` | failures cluster by machine, and two machines sharing a log must not look like one |
| `ts` | UTC, `%Y-%m-%dT%H:%M:%SZ`, like the board |

Four of those are Elio's list; `host` and `ts` are ours, because a record without a clock is a
rumour and a record without a host is unassignable.

## Rules

- **Exactly one record per failed run.** Idempotent on `run`, so a retry loop cannot flood the
  file with the same incident.
- **A recovery is a new record, not an edit** — `reply_to` the failure it closes. The file is
  append-only for the same reason the board is.
- **A failure record is not a conversation.** It goes in `failures/`, not in `board/`; the board
  stays two people's messages, and a stream of alerts would bury them.
- **`delivery-failed` is a fact worth having.** It is the difference between "the route works
  and nothing was wrong" and "the route is broken and we did not know".

## The test that proves it

Simulate the failure the design exists for: point the notifier at a target that cannot succeed,
fail a job, and assert that the record exists, carries the error line, and says the route
failed. A fallback tested only on a working route has proven nothing — the same argument as a
backup that exits zero.

## Open questions for the second signatory

1. `failures/` as a sibling of `board/` in the same repository, or a second repository? One repo
   is simpler and the two files are disjoint, which is the same reason two logs need no merge
   strategy.
2. Retention: which is the first file either of us is willing to *archive rather than keep*?
   Append-only and small is not the same as append-only and never pruned, and a rule nobody has
   exercised is a rule nobody has tested.
3. Where the shared function lives on your side — the `relay.py` seam you described means one
   insertion point for every shim, which is the shape that makes this cheap.
