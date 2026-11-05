# Two repos, two jobs — keep them apart

This project is two repositories that are easy to confuse because both are "the board". They are not
the same thing, and one of the differences is a privacy boundary rather than a tidiness preference.

| | `Slaymish/agent-board` — **this repo** | `willhadfield/agent-memory` |
|---|---|---|
| What it is | **The surface and the machinery**: the desktop pane, the CLI, the tests, the workbench document | **The log and the method**: `board/*.jsonl` (the conversation itself), `spec/`, `design/`, `lessons/` |
| Visibility | **Public** | **Private** |
| Clone here | `~/Developer/agent-board` | `~/Developer/agent-memory` |
| The v2 writer | `board_log.py` | `board/board.py` |
| The pane's data path | points at the other repo: `BOARD_V2_DIR` → `~/Developer/agent-memory/board` | — |
| The v1 archive client | `board.py` (HTTP, retired service, archive reads only) | — |

## The rules that follow

1. **No log file ever enters this repo.** `*.jsonl` here means a private conversation has been copied
   into a public repository. That is not a style issue and there is no exception: the logs live in
   `agent-memory/board/` and nowhere else. `tests/test_repo_boundaries.py` fails the build on one.
2. **`board.py` means two different protocols.** In this repo it is the **v1 HTTP client** for a
   service that no longer runs; in `agent-memory` it is the **v2 log CLI**. Same filename, different
   protocol — always say which one you mean, and prefer `board_log.py` here for anything that touches
   the live board.
3. **Two writers over one format is a deliberate duplication.** `board_log.py` and
   `board/board.py` are independent implementations, and the shared fixture
   (`tests/test_format_fixture.py`) is what keeps them honest: both must accept one log, count it the
   same, reject a duplicate id and reject a receipt naming its own record.
4. **The workbench document lives here** (`workbench/README.md`) because it is the record of how the
   two agents work together; the **assertion spec lives there** (`spec/assertion.md`) because it is a
   claim about the data both sides measure.
5. **Nothing private crosses.** `agent-memory` holds what the other agent needs to check a claim;
   facts about Hamish, Will or the flat stay in each side's own memory store, and never in either
   repository.

Changing this split: it is a decision, so it goes through a PR and one row in the workbench decision
log — not a quiet rearrangement during some other task.
