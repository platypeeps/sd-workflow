"""A grep of the governed tree for the retired framework's name returns nothing
outside three named sets (sd:10, criterion 18): the exemptions of decision
note 1942 (`EXEMPT`), the lines another lane's pull request held when the
sweep ran (`HELD`, empty since the 31(b1) reword), and the lines a test must
quote to name the residue commands (`ALLOWED_IF_PRESENT`).

The three literals are the criterion's own: the framework's name with its
capital, its dot-directory, and its task script. The grep is case-sensitive
for the same reason: an identifier that carries the name in lower case is not
a claim that the framework is still here, and the criterion does not count it.

`EXEMPT` holds the lines decision note 1942 on sd:10 named, and nothing
else: the upstream pull-request guard in `AGENTS.md`, exempt while sd:241,
sd:242 and sd:244 are open, and the `.trellis` residue row of `sd-status` with
the test that exercises it, exempt until the gating fleet check passes and the
`RESIDUE` detectors are cut. Each entry is `(path, line text)`, the whole line
stripped of its indentation, so an edit above it does not move the exemption
off the line it names and an edit to the line itself -- a second literal
appended to an exempt one, say -- is not covered by it. A row that matches
no line, or more than one, fails `test_every_row_still_names_exactly_one_line`,
so neither set can outlive the lines it names or quietly widen.

`HELD` is a second, separate set: lines the sweep could not reach because
another lane's open pull request held the file. They are not the criterion's
exemptions and the comment on the set says which pull request held them.
The criterion is closed when `HELD` is empty, which it has been since the
sd:10 31(b1) lane reworded both lines; `HELD_BOUND` keeps the two rows as
the frozen ceiling, so `test_the_held_set_is_named_and_shrinking` fails a
row added back.

`ALLOWED_IF_PRESENT` is a third set, for lines another open pull request
adds: a test that names the residue commands must quote them, so #995's
`tests/test_archive_untouched.py` carries the `.trellis` removal string and a
comment naming the framework. Those rows are exemptions of the same kind as
the `sd-status` row, but they are matched by file and content only and may
match zero lines, so this test is green whether #995 merges before this
branch or after it. `test_every_allowed_row_names_at_most_one_line` keeps the
set from widening: a row that matches two lines fails.
"""

# This module reads the whole checkout, so no changed-files fast path may
# narrow it away. `.github/scripts/select-tests.py` greps for the line below.
# select-tests: always-run

from __future__ import annotations

import pathlib
import subprocess
import sys
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

SELF = "tests/test_no_trellis_residue.py"

#: What runs or governs, as `prd.md` criterion 4 of sd:10 defines it, read off
#: the index by `tests/governed.py` rather than declared a second time here.
from tests.governed import GOVERNED  # noqa: E402

#: Criterion 18's three literals, dots escaped, as one alternation.
PATTERN = r"Trellis|\.trellis|task\.py"

#: The exempt lines of decision note 1942, each as its path and its whole
#: text, stripped.
EXEMPT = frozenset({
    # The upstream pull-request guard, `AGENTS.md:7-10,31` at 2eafa78b.
    ("AGENTS.md",
     "- Do not create pull requests in the upstream `Trellis` repository without"),
    ("AGENTS.md",
     "`sd-workflow` work uncovers a `Trellis`-owned change, document the"),
    ("AGENTS.md",
     "finding and provide a paste-ready handoff instead of opening a `Trellis` PR."),
    ("AGENTS.md",
     "This scope exception does not authorize unrelated work or remove specific "
     "approval requirements for destructive actions or upstream Trellis PRs."),
    # The residue row, `bin/sd-status:1091-1093` at 2eafa78b.
    ("bin/sd-status", '".trellis",'),
    ("bin/sd-status", '"the Trellis state directory the pack replaced with docs/work",'),
    ("bin/sd-status", '"git rm -r --cached --ignore-unmatch .trellis && rm -rf .trellis",'),
    # Its test, `tests/test_sd_status.py:1115-1120` at 2eafa78b.
    ("tests/test_sd_status.py", '(self.repo / ".trellis").mkdir()'),
    ("tests/test_sd_status.py",
     '(self.repo / ".trellis" / "state.json").write_text("{}", encoding="utf-8")'),
    ("tests/test_sd_status.py", 'self.assertIn("rm -rf .trellis", found["trellis"]["remove"])'),
})

#: Empty. It held two lines the sweep of 2026-09-16 could not reach because
#: #995 (fix-10-sweep) held both files when it ran, one word each in a list
#: of dot-directories; #995 merged as 486a223b with both words in place, and
#: the sd:10 31(b1) lane reworded both. Neither was a decision-note
#: exemption. The type is kept so a row can be held again, under the bound.
HELD: frozenset[tuple[str, str]] = frozenset()

#: The ceiling on `HELD`, frozen at the two rows of 2026-09-16. `HELD` may
#: lose rows; a row added to it fails `test_the_held_set_is_named_and_shrinking`
#: unless this set is edited too, which is the point.
HELD_BOUND = frozenset({
    ("bin/sd", "`.makemd`, `.trellis`), so the rule is the generalisation rather than the"),
})

#: Lines #995 (fix-10-sweep) adds to `tests/test_archive_untouched.py`: the
#: `FROZEN_DELETION_SITES` row that quotes `sd-status`'s `.trellis` removal
#: command, and the comment above it that names the framework. Exempt on the
#: same ground as the `sd-status` row itself. Present once #995 merged
#: (486a223b), absent before; either is green.
ALLOWED_IF_PRESENT = frozenset({
    ("tests/test_archive_untouched.py",
     "#: `RESIDUE` tuple, telling an operator how to uninstall a Trellis or legacy"),
    ("tests/test_archive_untouched.py",
     '("bin/sd-status", "git rm -r --cached --ignore-unmatch .trellis && rm -rf .trellis"),'),
})


def governed_rows() -> list[tuple[str, int, str]]:
    """Every `(path, line, text)` in the governed tree matching `PATTERN`.

    This file is excluded: it quotes every literal it searches for, and a test
    that matches itself measures nothing.
    """
    result = subprocess.run(
        ["git", "grep", "-nIE", PATTERN, "--", *GOVERNED, f":(exclude){SELF}"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    if result.returncode not in (0, 1):
        raise AssertionError(result.stderr.strip())
    rows = []
    for line in result.stdout.splitlines():
        path, number, text = line.split(":", 2)
        rows.append((path, int(number), text.strip()))
    return rows


class NoResidue(unittest.TestCase):
    def test_no_governed_line_names_the_framework_outside_the_exemptions(self):
        allowed = EXEMPT | HELD | ALLOWED_IF_PRESENT
        residue = [f"{path}:{number}:{text}" for path, number, text in governed_rows()
                   if (path, text) not in allowed]
        self.assertEqual([], residue, f"{len(residue)} lines still name the framework")

    def test_every_row_still_names_exactly_one_line(self):
        rows = governed_rows()
        for where, text in sorted(EXEMPT | HELD):
            with self.subTest(path=where, text=text):
                hits = [number for path, number, found in rows
                        if path == where and found == text]
                self.assertEqual(1, len(hits), f"{where}: {text!r} names lines {hits}")

    def test_every_allowed_row_names_at_most_one_line(self):
        rows = governed_rows()
        for where, text in sorted(ALLOWED_IF_PRESENT):
            with self.subTest(path=where, text=text):
                hits = [number for path, number, found in rows
                        if path == where and found == text]
                self.assertLessEqual(len(hits), 1, f"{where}: {text!r} names lines {hits}")

    def test_the_held_set_is_named_and_shrinking(self):
        """No row past the frozen ceiling, and no row in two sets."""

        self.assertLessEqual(HELD, HELD_BOUND)
        self.assertTrue(EXEMPT.isdisjoint(HELD))
        self.assertTrue(ALLOWED_IF_PRESENT.isdisjoint(EXEMPT | HELD))


if __name__ == "__main__":
    unittest.main()
