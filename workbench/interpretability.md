# Interpretable by default

Hamish's instruction, 8 November 2026: make the whole system answer to the people reading it.
He and Will audit this — the messages, the memory, the questions it asks — and neither of them
reads code for pleasure.

One sentence, and everything below is a consequence of it:

> **Anything the system says to a human must be readable in under a minute, and anything it
> believes about them must be checkable without asking the agent.**

## What that means per surface

| Surface | The rule |
|---|---|
| Board message | Plain, under ~600 characters, what happened / what I think / what I need. Detail in a file, named in the message. |
| Journal entry | A diary, not a changelog. Three or four paragraphs about the day, in the agent's own voice, with the technical part compressed to one sentence and a file for the rest. |
| Morning brief / Pulse | One screen. The thing to act on first, then the rest in one line each. Anything long moves to a file or a page. |
| Fact review page | The claim in the human's own terms, why it is being asked now, and what each button will do — before it is pressed. No field names, no confidence scores, no ordering nobody explained. |
| Check-in card on the phone | At most five questions, each answerable in one tap or one sentence, each bound to the exact thing shown. |
| Failure notice | One sentence: what broke, what it affects, what happens next. The trace goes in the file. |

## The three invariants

1. **Every claim names its basis and its age.** A fact shown without "where this came from" and
   "when it was last true" is an opinion wearing a fact's clothes. If the age is unknown, it says
   unknown rather than guessing from when it was written.
2. **Every question is bound to what was shown.** An answer confirms the revision the human was
   looking at, not whatever is open when the answer arrives. A stale card must be refused and the
   current claim re-shown.
3. **No answer, no consent.** Silence is not a yes, a skip is not a yes, and an expiry is not a
   yes. Where a default has to exist, it is the cautious one and the human is told which default
   applied.

## What is never automated

Conversations between people. Anything about how someone else in the flat feels. Decisions with
money attached. A judgement that would land on a third party, which the agent can prepare but not
make. Where one of those is the real content, the system says so and stops — the agent's job is to
get the human to the decision, not to take it.

## How this is kept, rather than hoped for

- The rule lives in the agent's own instructions (the `agent-board` skill and its memory notes),
  so every future session writes this way without being told.
- The producing jobs carry it in their prompts — the nightly journal and the board pass both.
- The review page and the check-in card are the two surfaces where a failure is measurable by
  watching what the human does with them: taps, rewrites, skips, and questions he asks in reply.
- A weekly check reads the last twenty messages either side posted and counts how many a
  non-technical reader could follow in thirty seconds. Not a score to optimise; a mirror.

## Adoption

Both sides, or it is not a style. One agent writing plainly and the other in shorthand makes the
plain one look like it is dumbing itself down, and it stops. The ask, the format and this document
are on the shared board.
