"""The v2 Help page (sd:10917).

What this slice promises: `/api/help` lists every phrase skills/sd-next/SKILL.md names, the run list's rows with
the budget each reference file states; the help.md sections carry live values read on each request from their
sources, so a changed `sd.machine_cap` shows on the next read; a value that does not read says why and is named
under `problems`; help.md uses only tokens a source reads and links only dashboard views; the rail links Help.
"""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path
from unittest.mock import patch

from sd_dashboard import help_screen, v2
from sd_db import create_item
from support import NOW, ScreenCase
from test_v2_registry import GROUPS, Registers


def text(document) -> str:
    """Every word the page shows: the phrase rows and each section's parts."""
    rows = " ".join(" ".join(row.values()) for row in document["phrases"])
    parts = [part.get("text") or part.get("code") or part.get("value") or part.get("unknown") or ""
             for section in document["sections"] for block in section["blocks"] for part in block["parts"]]
    return rows + " " + " ".join(parts)


def live(document, key):
    return next(part for section in document["sections"] for block in section["blocks"]
                for part in block["parts"] if part.get("live") == key)


STUBS = {name: (lambda name=name: f"stub {name}") for name in help_screen.LIVE}


class ThePhrases(ScreenCase):
    def setUp(self):
        super().setUp()
        stubs = patch.dict(help_screen.LIVE, STUBS)
        stubs.start()
        self.addCleanup(stubs.stop)

    def test_the_page_lists_every_phrase_the_skill_names(self):
        skill = help_screen.SKILL.read_text(encoding="utf-8")
        runs = re.findall(r'"([^"]+)"', "\n".join(help_screen._runs_block(skill)))
        named = set(help_screen.described()) | set(runs)
        self.assertGreaterEqual(named, {"triage", "park review", "health check", "check open PRs", "what next"})
        shown = text(help_screen.document(self.connection, now=NOW))
        self.assertEqual(sorted(phrase for phrase in named if f'"{phrase}"' not in shown and phrase not in shown), [])

    def test_each_run_row_carries_the_budget_its_reference_states(self):
        rows = {row["phrase"]: row for row in help_screen.phrases()}
        self.assertEqual(set(rows) >= {"triage", "park review", "health check", "open threads", "collect followups",
                                       "check open PRs"}, True, sorted(rows))
        for row in rows.values():
            reference = help_screen._reference(row["reference"], help_screen.SKILL, help_screen.ROOT)
            self.assertIn(f"Budget: {row['budget']}", reference.read_text(encoding="utf-8"), row)
        self.assertEqual(rows["open threads"]["does"], rows["collect followups"]["does"])
        self.assertIn("queues nothing", rows["check open PRs"]["mode"])

    def test_a_new_run_in_the_skill_shows_without_a_page_edit(self):
        root = Path(self.tmp.name) / "pack"
        (root / "skills" / "sd-next" / "references").mkdir(parents=True)
        (root / "skills" / "sd-next" / "references" / "sweep.md").write_text("# Sweep\n\nBudget: about 5 minutes.\n")
        skill = root / "skills" / "sd-next" / "SKILL.md"
        skill.write_text('---\nname: x\n---\n\n## Operator runs\n\n- `references/sweep.md`: "sweep" (the new run).\n\n## Never\n')
        self.assertEqual(help_screen.phrases(skill, root), [{"phrase": "sweep", "does": "the new run", "budget": "about 5 minutes",
                                                             "mode": "acts: fixes, files, rules, queues",
                                                             "reference": "references/sweep.md"}])

    def test_a_reference_outside_the_pack_is_refused(self):
        root = Path(self.tmp.name) / "pack"
        (root / "skills" / "sd-next").mkdir(parents=True)
        skill = root / "skills" / "sd-next" / "SKILL.md"
        skill.write_text('## Operator runs\n\n- `../../../outside.md`: "leak" (out).\n')
        with self.assertRaisesRegex(ValueError, "outside the pack"):
            help_screen.phrases(skill, root)


class TheLiveValues(ScreenCase):
    def cap(self, value):
        config = Path(self.tmp.name) / "xdg-config" / "sd-ai-command-pack" / "config.json"
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text(json.dumps({"config": {"sd": {"machine_cap": value}}}))

    def test_a_changed_machine_cap_shows_on_the_next_read(self):
        with patch.dict(help_screen.LIVE, {name: STUBS[name] for name in help_screen.LIVE if name != "machine_cap"}):
            self.cap("3")
            self.assertIn("3 builders at once", live(help_screen.document(self.connection, now=NOW), "live:machine_cap")["value"])
            self.cap("5")
            self.assertIn("5 builders at once", live(help_screen.document(self.connection, now=NOW), "live:machine_cap")["value"])

    def test_the_pause_item_reads_from_the_database(self):
        number = create_item(self.connection, kind="task", title="Resume builds", status="planning")
        with patch.object(help_screen, "HELP", Path(self.tmp.name) / "help.md"), patch.dict(help_screen.LIVE, STUBS):
            help_screen.HELP.write_text(f"## Pause\n\nThe pause: {{item:{number}}}.\n")
            document = help_screen.document(self.connection, now=NOW)
            self.assertEqual(live(document, f"item:{number}")["value"], f"sd:{number} is planning: Resume builds")
            self.assertEqual(document["problems"], [])
            self.connection.execute("UPDATE item SET status = 'done' WHERE id = ?", (number,))
            self.assertEqual(live(help_screen.document(self.connection, now=NOW), f"item:{number}")["value"],
                             f"sd:{number} is done: Resume builds")

    def test_a_source_that_fails_says_why_and_is_named(self):
        def broken():
            raise ValueError("no gate answered")

        with patch.dict(help_screen.LIVE, {**STUBS, "gate_slots": broken}):
            document = help_screen.document(self.connection, now=NOW)
        self.assertEqual(live(document, "live:gate_slots"), {"live": "live:gate_slots", "unknown": "no gate answered"})
        self.assertIn("live:gate_slots did not read: no gate answered", document["problems"])

    def test_help_md_uses_only_tokens_a_source_reads(self):
        seen = []
        help_screen.sections(help_screen.HELP.read_text(encoding="utf-8"),
                             lambda token, name: seen.append((token, name)) or {"live": f"{token}:{name}", "value": "x"})
        self.assertTrue(seen)
        self.assertEqual([pair for pair in seen if pair[0] == "live" and pair[1] not in help_screen.LIVE], [])

    def test_an_unknown_token_is_a_named_problem(self):
        with patch.object(help_screen, "HELP", Path(self.tmp.name) / "help.md"), patch.dict(help_screen.LIVE, STUBS):
            help_screen.HELP.write_text("## A\n\nThe cap is {live:no_such_value}.\n")
            document = help_screen.document(self.connection, now=NOW)
        self.assertIn("live:no_such_value did not read: help.md names live:no_such_value, which no source reads",
                      document["problems"])

    def test_a_link_off_the_server_or_text_before_a_section_is_refused(self):
        for body, why in (("## A\n\n[x](javascript:alert(1))\n", "dashboard view"), ("loose\n## A\n", "before the first")):
            with self.subTest(body=body), self.assertRaisesRegex(ValueError, why):
                help_screen.sections(body, lambda token, name: {})

    def test_markup_and_non_ascii_stay_text(self):
        found = help_screen.sections("## Ü <b>\n\n- one <i>é</i>\n  two `a<b`\n", lambda token, name: {})
        self.assertEqual(found, [{"title": "Ü <b>", "blocks": [
            {"kind": "li", "parts": [{"text": "one <i>é</i> two "}, {"code": "a<b"}]}]}])


class TheRail(unittest.TestCase):
    def test_the_rail_links_help(self):
        self.assertIn("Help", GROUPS)
        self.assertEqual(v2.SECTIONS["Help"], "/help")


class TheRegistration(Registers, unittest.TestCase):
    page, section, route, api = "help", "Help", "/help", ("/api/help",)


if __name__ == "__main__":
    unittest.main()
