#!/usr/bin/env python3
"""The two repos stay two repos.

`Slaymish/agent-board` is public and is the surface; `willhadfield/agent-memory` is private and is
the log. The distinction is not tidiness — a log file in the public repo publishes a private
conversation — so it is asserted here rather than described in a README and hoped for.

  python3 tests/test_repo_boundaries.py
"""
import pathlib
import re
import sys

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parent


def test_no_log_files_in_the_public_repo():
    """A *.jsonl here is a private conversation copied into a public repository."""
    stray = [str(p.relative_to(REPO)) for p in REPO.rglob("*.jsonl")
             if ".git" not in p.parts and "node_modules" not in p.parts]
    assert not stray, (
        "log file(s) committed to the public repo: %s — the logs live in "
        "willhadfield/agent-memory/board and nowhere else" % ", ".join(stray))


def test_the_pane_reads_the_other_repo():
    """The pane's default must point at the log repo, never at this one."""
    src = (REPO / "plugin" / "dashboard" / "plugin_api.py").read_text()
    m = re.search(r"BOARD_DIR\s*=\s*os\.path\.expanduser\(.*?or\s*\"([^\"]+)\"", src)
    assert m, "could not find the pane's default log directory"
    default = m.group(1)
    assert "agent-memory" in default, (
        "the pane defaults to %r — it must read the log repo, not this one" % default)
    assert "agent-board" not in default, (
        "the pane defaults into the surface repo (%r)" % default)


def test_the_two_clis_declare_their_protocol():
    """Same filename, two protocols. Each file must say which one it speaks."""
    v1 = (REPO / "board.py").read_text()[:4000].lower()
    v2 = (REPO / "board_log.py").read_text()[:4000].lower()
    assert "http" in v1 or "v1" in v1, "board.py no longer identifies as the v1 HTTP client"
    assert "jsonl" in v2 or "append-only" in v2, "board_log.py no longer identifies as the v2 log CLI"


def test_documents_live_where_they_belong():
    assert (REPO / "workbench" / "README.md").exists(), "the workbench document belongs in this repo"
    assert not (REPO / "spec").exists(), (
        "spec/ belongs to the log repo (willhadfield/agent-memory); a copy here will drift")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
