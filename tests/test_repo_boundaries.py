#!/usr/bin/env python3
"""The two repos stay two repos.

`Slaymish/agent-board` is public and is the surface; `willhadfield/agent-memory` is private and is
the log. The distinction is not tidiness — a log file in the public repo publishes a private
conversation — so it is asserted here rather than described in a README and hoped for.

These checks were module-level functions with a `__main__` runner until 9 Nov 2026, which meant
`python3 -m unittest discover -s tests` collected none of them: the documented command printed OK
while the privacy guard never ran. They are `TestCase` methods now, and `python3
tests/test_repo_boundaries.py` still works for a one-off.

What this file can and cannot prove: a filename rule catches the common accident, not a note pasted
into HTML, a screenshot, or a fixture. "No conversation content in the public repo" is a review
rule with one automated tripwire, not a guarantee.
"""
import pathlib
import re
import unittest

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parent


class RepoBoundaries(unittest.TestCase):
    def test_no_log_files_in_the_public_repo(self):
        """A *.jsonl here is a private conversation copied into a public repository."""
        stray = [str(p.relative_to(REPO)) for p in REPO.rglob("*.jsonl")
                 if ".git" not in p.parts and "node_modules" not in p.parts]
        self.assertFalse(
            stray,
            "log file(s) committed to the public repo: %s — the logs live in "
            "willhadfield/agent-memory/board and nowhere else" % ", ".join(stray))

    def test_the_pane_reads_the_other_repo(self):
        """The pane's default must point at the log repo, never at this one."""
        src = (REPO / "plugin" / "dashboard" / "plugin_api.py").read_text()
        m = re.search(r"BOARD_DIR\s*=\s*os\.path\.expanduser\(.*?or\s*\"([^\"]+)\"", src)
        self.assertTrue(m, "could not find the pane's default log directory")
        default = m.group(1)
        self.assertIn("agent-memory", default,
                      "the pane defaults to %r — it must read the log repo, not this one" % default)
        self.assertNotIn("agent-board", default,
                         "the pane defaults into the surface repo (%r)" % default)

    def test_the_retired_client_has_not_come_back(self):
        """v1 spoke HTTP and was retired on 2 Nov 2026; its two names are the trap.

        Same filename, two protocols, one of them dead. A `board.py` here is either the dead
        client returning or the private repo's CLI copied across — both wrong, and both silent.
        """
        self.assertFalse((REPO / "board.py").exists(),
                         "board.py is the retired v1 HTTP client; v2 lives in board_log.py "
                         "and the private repo's own CLI stays in that repo")
        v2 = (REPO / "board_log.py").read_text()[:4000].lower()
        self.assertTrue("jsonl" in v2 or "append-only" in v2,
                        "board_log.py no longer identifies as the v2 log CLI")

    def test_documents_live_where_they_belong(self):
        self.assertTrue((REPO / "workbench" / "README.md").exists(),
                        "the workbench document belongs in this repo")
        self.assertFalse((REPO / "spec").exists(),
                         "spec/ belongs to the log repo (willhadfield/agent-memory); "
                         "a copy here will drift")


if __name__ == "__main__":
    unittest.main(verbosity=2)
