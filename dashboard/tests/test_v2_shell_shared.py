"""What every v2 page now takes from the shell (sd:2588, sd:2527, sd:2528, sd:2529, sd:2530).

`shell.js` holds the page's reads and writes (`shell.post`, `shell.getJSON`) and the list grammar (`shell.list`: sort header,
numbered pager, filter chips, and the URL keys they keep). `shell.css` holds the annunciator and ledger rules that were the
same on every page, the list grammar's styles, and the lamp hover: a ring in the lamp's ink token, not a brightness filter.

The JavaScript runs under JavaScriptCore (osascript) on the blocks the tests cut out of `shell.js`, so a test reads the
shipped code, not a copy. Reports wires the list grammar end to end; `test_v2_reports` drives that page.

sd:2680: one annunciator and one ledger layout. shell.css holds the ledger's desktop rules, with the browser's auto table
layout and no colgroup. `markup.cells(spec)` draws every page's lamps; `cell_grammar` is the markup test each page's own
suite runs on what it drew. sd:3588: the lamps sit in the foundation's <annunciator-strip>, and its stylesheet, a pinned
copy (test_v2_foundation), is the only one that styles a lamp, but for the sd:2530 hover ring in shell.css.
"""

from __future__ import annotations

import json
import re
import subprocess
import unittest
from html.parser import HTMLParser

from test_v2_tasks import (
    FETCH_JS,
    LIST_JS,
    MARKUP_JS,
    SETTLE_JS,
    SHELL_JS,
    STAND_IN,
    V2,
)
from test_v2_today import OSASCRIPT

STATIC = V2 / "static"
SHELL_CSS = (STATIC / "shell.css").read_text(encoding="utf-8")
# The foundation's own files (test_v2_foundation pins them) are neither page nor shell code.
FOUNDATION = ("tokens.css", "theme.js", "annunciator-strip.css", "annunciator-strip.js")
PAGE_CSS = {p.name: p.read_text(encoding="utf-8") for p in STATIC.glob("*.css") if p.name not in ("shell.css", *FOUNDATION)}
PAGE_JS = {p.name: p.read_text(encoding="utf-8") for p in STATIC.glob("*.js")
           if p.name not in ("shell.js", "markup.js", "sections.js", "read.js", *FOUNDATION)}
# The ledger rules every page that draws one shares; shell.css holds the one copy (sd:2588, sd:2680).
SHARED = (
    ".ledger { width: 100%; border-collapse: collapse; font-size: var(--text-sm); }",
    ".ledger th { text-align: left; font: 600 var(--text-2xs)/1 var(--font-label); letter-spacing: 0.09em; text-transform: uppercase;"
    " color: var(--color-muted); padding: var(--space-xs); border-bottom: var(--rule-hair) solid var(--color-rule); white-space: nowrap; }",
    ".ledger td { padding: var(--space-2xs) var(--space-xs); height: var(--row); border-bottom: var(--rule-hair) solid var(--color-rule-2); vertical-align: middle; }",
    ".ledger .g { text-align: center; font-family: var(--font-data); }",
    # sd:5463: Activity's title line and filter bar, the ones every table page draws (the bar through shell.list.fbar).
    ".sec-head { display: flex; flex-wrap: wrap; align-items: baseline; justify-content: space-between; gap: var(--space-xs) var(--space-md); margin: 0 0 var(--space-xs); }",
    ".sec-head h2 { margin: 0; font: 600 var(--text-md)/1.2 var(--font-ui); display: flex; align-items: center; gap: var(--space-2xs); }",
    ".fbar { display: flex; flex-wrap: wrap; align-items: center; gap: var(--space-xs); min-width: 0; }",
    ".fgroup { display: flex; flex-wrap: wrap; align-items: center; gap: var(--space-2xs); min-width: 0; }",
    ".fseg { display: inline-flex; gap: 2px; padding: 2px; border: var(--rule-hair) solid var(--color-rule); border-radius: var(--radius-md); background: var(--color-panel); }",
    # sd:5529 (operator, 2026-10-20): Clear all sits right after the filters, "don't have it aligned right, but left".
    ".fsum { display: flex; align-items: center; gap: var(--space-2xs); font: 400 var(--text-xs)/1 var(--font-data); color: var(--color-muted); white-space: nowrap; }",
    # sd:5641: the layout picker Tasks and Writing share, an icon button and its menu of layouts.
    ".viewpick { position: relative; }",
    ".viewmenu { position: absolute; top: calc(100% + var(--space-2xs)); right: 0; width: auto; min-width: 11rem; }",
)
# The lamp selectors only annunciator-strip.css may style, and the ledger selectors only shell.css may. A page keeps what is
# its own: a cell's extra class (`.cell.clock`), what sits inside a cell (`.cell kbd`) and its narrow-screen ledger cards.
LAMP_ONLY = re.compile(r'(?m)^\s*((\.annunciator\s+)?\.cell(\s*[{,]|:hover|:focus-visible|:disabled|\s+\.(lbl|ph|val)\b|\[data-state|\[aria-pressed)'
                       r'|(a|button)\.cell\b|\.annunciator(\s+li)?\s*[{,])')
SHELL_ONLY = re.compile(LAMP_ONLY.pattern + r'|^\s*\.(fbar|fseg|fsum|tok)\b' + r'|^\s*\.ledger(\s+(th|td)\s*[{,]|\s*[{,]|\s+col\b|\s+tbody tr(:hover td|\[aria-selected="true"\] td)?\s*[{,])')
# The pages whose script draws an annunciator; each draws it through markup.cells. Today's lamps are server HTML that
# today.js fills in place, so its test reads today.html against the same grammar.
LAMP_PAGES = ("activity", "briefs", "contributions", "designs", "health", "hoa", "metrics", "reports", "skills")
# One lamp as markup.cells draws it: a link, a button or a div; the page's attributes; then the label, the value and words
# for a screen reader. A page that draws a lamp by hand fails here.
CELL = re.compile(r'<li><(button|a|div) class="cell(?: [\w-]+)?"(?: type="button"| href="[^"]*")?((?: [\w-]+="[^"]*")*)>'
                  r'<span class="lbl">(?:(?!</?li>).)*?</span><span class="val">(?:(?!</?li>).)*</span>(?:<span class="sr">[^<]*</span>)?</\1></li>', re.S)


def wide(css):
    """`css` without its comments and its @media (max-width: …) blocks, where a page lays its ledger out as cards."""
    css, out, i = re.sub(r"/\*.*?\*/", "", css, flags=re.S), [], 0
    for m in re.finditer(r"@media \(max-width:[^{]*\{", css):
        if m.start() < i:
            continue
        depth, j = 1, m.end()
        while depth:
            depth += {"{": 1, "}": -1}.get(css[j], 0)
            j += 1
        out.append(css[i:m.start()])
        i = j
    return "".join(out) + css[i:]


def cell_grammar(case, markup):
    """Fail `case` unless every <li> in `markup` is a lamp markup.cells drew; return each lamp's (tag, data-state)."""
    items = re.findall(r"<li>.*?</li>(?=<li>|$)", markup.strip(), re.S)
    case.assertTrue(items, "no lamp drawn")
    out = []
    for item in items:
        m = CELL.fullmatch(item)
        case.assertIsNotNone(m, f"not a markup.cells lamp: {item[:160]}")
        state = re.search(r' data-state="(\w+)"', m.group(2))
        out.append((m.group(1), state.group(1) if state else None))
    return out


def run_js(body):
    """The list block with markup.js, as the shell has it; body sets R."""
    script = (STAND_IN + MARKUP_JS + "\nconst { html, plural } = window.markup;\n"
              + "const ICON = n => html`<svg class=\"i\" aria-hidden=\"true\"><use href=\"#i-${n}\"/></svg>`;\n"
              + FETCH_JS + LIST_JS + "\nvar R = {};\n(async () => { try {\n" + body
              + "\n} catch (e) { OUT.error = String(e) + ' ' + e.stack; } })();\n"
              + "function run() { OUT.R = R; return JSON.stringify(OUT); }\n")
    result = subprocess.run([OSASCRIPT, "-l", "JavaScript", "-e", script], capture_output=True, text=True, timeout=60, check=False)
    if result.returncode:
        raise AssertionError(result.stderr)
    out = json.loads(result.stdout)
    if out["error"]:
        raise AssertionError(out["error"])
    return out["R"]


class TheFetchHelpers(unittest.TestCase):
    """sd:2588: one post and one getJSON, in the shell; no page keeps its own."""

    def test_no_page_defines_its_own_csrf_post_or_getjson(self):
        for name, js in PAGE_JS.items():
            with self.subTest(name):
                self.assertNotRegex(js, r"(?m)^\s*(const|let|var) csrf\b|^\s*(async )?function (post|getJSON)\b|sd-csrf")

    def test_post_sends_the_token_and_marks_a_409_stale(self):
        out = run_js("""ANSWER = path => path === '/stale' ? [409, { error: 'changed since' }] : path === '/bad' ? [400, {}] : [200, { ok: 1 }];
  R.ok = await post('/fine', { a: 1 });
  R.stale = await post('/stale', {}).catch(e => [e.message, e.stale]);
  R.bad = await post('/bad', {}).catch(e => [e.message, e.stale]);
  R.read = await getJSON('/stale').catch(e => e.message);
  R.posts = OUT.posts;""")
        self.assertEqual(out["ok"], {"ok": 1})
        self.assertEqual(out["stale"], ["changed since", True])
        self.assertEqual(out["bad"], ["HTTP 400", False])
        self.assertEqual(out["read"], "changed since")
        self.assertEqual(out["posts"][0], ["/fine", {"a": 1}, 64])

    def test_a_200_whose_body_cannot_be_read_is_an_unknown_outcome(self):
        # sd:8828: a 200 whose JSON failed to parse resolved as {}, so the
        # shell toasted success and offered Undo. It may or may not have
        # landed: post rejects marked unknown, with no status, as for no answer.
        out = run_js(SETTLE_JS + """ANSWER = () => [200, new SyntaxError('truncated')];
  const c = { label: 'Move', on: 'item', risk: 'undo', undo() {} }, o = { id: 1, label: '#1' }, toasts = [];
  const toast = (m, undo) => toasts.push([m, !!undo]);
  R.err = await post('/x', {}).catch(e => [e.message, !!e.unknown, e.status === undefined]);
  await settleOne(c, o, post('/x', {}), { toast });
  R.bulk = await settleBulk(c, [o], [post('/x', {})], { toast, plural: (n, w) => `${n} ${w}s` });
  R.toasts = toasts;""")
        self.assertEqual(out["err"], [
            "the answer (HTTP 200) could not be read, so whether it changed is not known", True, True])
        self.assertEqual(out["bulk"], {"landed": 0, "stale": 0, "failed": 0, "unknown": 1})
        self.assertEqual([t[1] for t in out["toasts"]], [False, False], "an unknown outcome offers no Undo")


# sd:3520: the shell files every capture. A page hook only adds to it; with none, the dialog still files a row.
CAPTURE = re.search(r"^  // capture:start\n(.*?)^  // capture:end$", SHELL_JS, re.S | re.M)


def run_capture(case, body):
    case.assertIsNotNone(CAPTURE, "shell.js has no capture:start block, so a page without SHELL_CAPTURE files nothing")
    return run_js(CAPTURE.group(1) + body)


class TheShownGuard(unittest.TestCase):
    """sd:5064, lane review on 16cad7b5: an answer for what the page no longer shows changes nothing."""

    def test_a_newer_read_or_another_subject_ends_an_older_reads_check(self):
        out = run_js("""let on = 'a';
  const a1 = stillShown('recs', 'a', () => on); R.first = a1();
  const a2 = stillShown('recs', 'a', () => on); R.older = a1(); R.newer = a2();
  R.away = stillShown('recs', 'b', () => on); R.kept = a2();
  const other = stillShown('poll', 'a', () => on); R.slots = [a2(), other()];
  on = 'b'; R.moved = [a2(), other()];
  on = 'a'; R.back = a2();""")
        self.assertEqual(out, {"first": True, "older": False, "newer": True, "away": None, "kept": True,
                               "slots": [True, True], "moved": [False, False], "back": True})


class TheDefaultCapture(unittest.TestCase):
    """sd:3520: a capture on a page without its own hook files a row, and a toast says Captured only with the new id."""

    def test_a_task_or_followup_posts_to_api_items_and_names_the_new_id(self):
        out = run_capture(self, """ANSWER = (path, body) => [201, { item: { id: 77, title: body.title } }];
  R.task = await fileCapture({ kind: 'task', title: 'Read it' });
  R.followup = await fileCapture({ kind: 'followup', title: 'Then this', item: 12 });
  R.p = await fileCapture({ kind: 'task', title: 'Ship it p1 today' });
  R.posts = OUT.posts;""")
        self.assertEqual(out["task"], "Captured sd:77: Read it")
        self.assertEqual(out["followup"], "Captured sd:77: Then this")
        self.assertEqual(out["posts"], [["/api/items", {"title": "Read it"}, 64],
                                        ["/api/items", {"title": "Then this", "kind": "followup", "followup_of": 12}, 64],
                                        ["/api/items", {"title": "Ship it today", "priority": 1}, 64]])
        self.assertEqual(out["p"], "Captured sd:77: Ship it today")

    def test_a_note_reads_the_items_revision_then_posts_the_note(self):
        out = run_capture(self, """ANSWER = path => path === '/api/items/12/capture-context' ? [200, { item: { id: 12 }, revision: 'r'.repeat(64) }] : [200, {}];
  R.note = await fileCapture({ kind: 'note', title: 'Seen it', item: 12 });
  R.gets = OUT.gets; R.posts = OUT.posts;""")
        self.assertEqual(out["note"], "Note added to sd:12")
        self.assertEqual(out["gets"], ["/api/items/12/capture-context"])
        self.assertEqual(out["posts"], [["/api/items/12/notes", {"body": "Seen it", "kind": "comment", "revision": "r" * 64}, 64]])

    def test_a_refusal_says_not_filed_and_no_answer_says_not_known(self):
        out = run_capture(self, """ANSWER = path => [400, { error: 'A title is required.' }];
  R.refused = await fileCapture({ kind: 'task', title: 'x' }).catch(e => e.message);
  ANSWER = path => { throw new TypeError('Load failed'); };
  R.lost = await fileCapture({ kind: 'task', title: 'x' }).catch(e => e.message);
  ANSWER = path => [201, {}];
  R.empty = await fileCapture({ kind: 'task', title: 'x' }).catch(e => e.message);
  R.noRow = await fileCapture({ kind: 'note', title: 'x', item: null }).catch(e => e.message);""")
        self.assertEqual(out["refused"], "Not filed: A title is required.")
        self.assertEqual(out["lost"], "Not known if it filed: Load failed. Look in Tasks before you capture it again.")
        self.assertEqual(out["empty"], "Not known if it filed: the server named no item. Look in Tasks before you capture it again.")
        self.assertEqual(out["noRow"], "Not filed: select a row that has an item first")

    def test_a_refused_capture_reopens_with_the_kind_priority_and_repo_the_operator_picked(self):
        # Review 2026-10-12 class pass: the reopened dialog kept the text but reset Kind, Priority and Repo to the defaults.
        reopen = re.search(r"if \(m\.startsWith\('Not filed'\)\) openCapture\(o, v(, [^)]*)?\)", SHELL_JS)
        self.assertIsNotNone(reopen)
        self.assertEqual(reopen.group(1), ", picks", "a refusal reopens without the operator's picks")
        self.assertIn("function openCapture(target, text = '', picks = null) {", SHELL_JS)
        self.assertIn("if (picks) { if (!f.querySelector(`[name=\"kind\"][value=\"${picks.kind}\"]`)?.disabled) f.kind.value = picks.kind; f.priority.value = String(picks.priority); }", SHELL_JS)
        self.assertIn("sel.value = captureRepo(list, picks ? picks.repo : hint);", SHELL_JS)
        # A repo the operator picked resolves from the list as any hint does; No repo stays No repo.
        out = run_capture(self, """const LIST = [{ path: '~/repos/one', name: 'one' }];
  R.got = [captureRepo(LIST, '~/repos/one'), captureRepo(LIST, '')];""")
        self.assertEqual(out["got"], ["~/repos/one", ""])

    def test_enter_in_the_capture_box_files_it_and_does_not_cancel(self):
        # Review 2026-10-12 edit-during-refresh check: the form's first button is Cancel, so the browser's implicit submit on
        # Enter closed the box with "no" and dropped the typed text. Enter in a field submits with the Capture button, as the
        # confirm dialog does; the required text still blocks an empty capture.
        box = SHELL_JS[SHELL_JS.index("function openCapture("):SHELL_JS.index("modal(cap, inp);")]
        self.assertLess(box.index('value="no" formnovalidate data-cancel>Cancel'), box.index('value="yes">Capture'))
        enter = ("if (e.key === 'Enter' && !e.isComposing && e.target.tagName === 'INPUT') { e.preventDefault(); "
                 "f.requestSubmit(f.querySelector('[value=\"yes\"]')); }")
        self.assertTrue(enter in box, "Enter in the capture box cancels it and drops the text")

    def test_the_repo_and_priority_go_with_the_capture_and_a_typed_p_wins(self):
        # sd:3521: the box's picks (the page's repo, P3 unless changed) are fields of the new item; p1 to p4 typed in the
        # title still sets the priority.
        out = run_capture(self, """ANSWER = (path, body) => [201, { item: { id: 78, title: body.title } }];
  await fileCapture({ kind: 'task', title: 'Fix it', priority: 3, repo: '~/repos/one' });
  await fileCapture({ kind: 'followup', title: 'Fix it p1', item: 5, priority: 3, repo: '' });
  await fileCapture({ kind: 'note', title: 'Seen', item: 5, priority: 3, repo: '~/repos/one' }).catch(() => null);
  R.posts = OUT.posts.map(p => p[1]);""")
        self.assertEqual(out["posts"][:2], [{"title": "Fix it", "priority": 3, "repo": "~/repos/one"},
                                            {"title": "Fix it", "priority": 1, "kind": "followup", "followup_of": 5}])

    def test_the_page_repo_resolves_by_path_or_name_and_an_unknown_one_is_no_default(self):
        out = run_capture(self, """const LIST = [{ path: '~/repos/one', name: 'one' }, { path: '~/repos/two', name: 'two' }];
  R.byPath = captureRepo(LIST, '~/repos/two'); R.byName = captureRepo(LIST, 'one'); R.byOwner = captureRepo(LIST, 'someone/two');
  R.unknown = captureRepo(LIST, 'three'); R.none = captureRepo(LIST, null); R.empty = captureRepo([], '~/repos/one');""")
        self.assertEqual([out[k] for k in ("byPath", "byName", "byOwner", "unknown", "none", "empty")],
                         ["~/repos/two", "~/repos/one", "~/repos/two", "", "", ""])

    def test_the_command_line_names_the_repo_and_priority_it_files(self):
        out = run_capture(self, """R.lines = [captureCli({ kind: 'task', title: "it's", priority: 3, repo: '~/repos/one' }),
  captureCli({ kind: 'followup', title: 'x', item: 5, priority: 2, repo: '' }),
  captureCli({ kind: 'note', title: 'x', item: 5, priority: 3, repo: '~/repos/one' }),
  captureCli({ kind: 'task', title: 'x', priority: 4, repo: '/tmp/a b' })];""")
        self.assertEqual(out["lines"], ["cd ~/repos/one && sd task add --priority 3 'it'\\''s'",
                                        "sd task add --no-repo --kind followup --priority 2 'x'",
                                        "sd task note 5 'x' --kind comment",
                                        "cd '/tmp/a b' && sd task add --priority 4 'x'"])

    def test_the_box_offers_the_pickers_with_p3_and_the_pages_repo_as_defaults(self):
        self.assertIn("window.PAGE_REPO", SHELL_JS)
        box = re.search(r"function openCapture\(.*?\n  \}\n", SHELL_JS, re.S).group(0)
        self.assertRegex(box, r'name="priority" value="\$\{n\}"\$\{n === 3 \? html` checked` : ..\}')
        self.assertIn('id="cap-repo"', box)
        self.assertIn('aria-keyshortcuts="Alt+R"', box)
        for page in ("management.js", "tasks.js", "today.js"):
            with self.subTest(page):
                self.assertIn("window.PAGE_REPO =", PAGE_JS[page])

    def test_the_dialog_files_through_the_page_hook_or_the_default_and_never_toasts_captured_itself(self):
        close = re.search(r"cap\.onclose = .*?\n    modal\(cap, inp\);", SHELL_JS, re.S)
        self.assertIsNotNone(close)
        self.assertIn("(window.SHELL_CAPTURE || fileCapture)(", close.group(0))
        self.assertNotIn("'Captured'", close.group(0))
        self.assertNotIn("Note added", close.group(0))

    def test_only_tasks_and_review_keep_a_page_hook_and_review_files_tasks_through_the_shell(self):
        # Today's and Briefs' hooks filed the same POST the shell now files; Tasks keeps its own to reread its board.
        # Review (sd:3709, merged from main) keeps one for a note on a question, a comment on its batch; a task or a
        # followup goes through the shell's fileCapture, so the box's priority and repo (sd:3521) reach the item.
        hooks = sorted(name for name, js in PAGE_JS.items() if re.search(r"window\.SHELL_CAPTURE\s*=", js))
        self.assertEqual(hooks, ["review.js", "tasks.js"])
        self.assertTrue("return window.shell.fileCapture({ kind, title, item, priority, repo });" in PAGE_JS["review.js"],
                        "Review's capture drops the priority and repo the operator picked")


class TheChat(unittest.TestCase):
    """sd:5294: the pane posts the question with the page's row and shows the answer, or a failure that says no answer."""

    def test_the_box_opts_out_of_writing_extension_overlays(self):
        # sd:5455: Grammarly and LanguageTool drew badges over the box's border.
        box = re.search(r'<textarea id="chat-input"[^>]*>', SHELL_JS).group(0)
        for attr in ('data-gramm="false"', 'data-gramm_editor="false"', 'data-enable-grammarly="false"', 'data-lt-active="false"'):
            self.assertIn(attr, box)

    def run_chat(self, body):
        block = re.search(r"^  // chat:start\n(.*?)^  // chat:end$", SHELL_JS, re.S | re.M)
        self.assertIsNotNone(block, "shell.js has no chat:start block, so the pane posts nothing")
        return run_js(block.group(1) + body)

    def test_the_question_goes_with_the_row_and_the_answer_comes_back(self):
        out = self.run_chat("""ANSWER = () => [200, { answer: 'Line one\\nLine two', where: '/r' }];
  const row = chatRow({ id: 't12', type: 'item', label: '#12 Plan', item: 12, title: 'not sent' });
  R.answer = await askChat({ scope: 'tasks', page: 'Tasks', selected: row, text: 'What is left?' });
  R.posts = OUT.posts;
  R.rows = [chatRow(null), chatRow({ id: 4, type: 'brief', label: 'x' }), chatRow({ id: 'a', item: 'n/a' })];""")
        self.assertEqual(out["answer"], {"text": "Line one\nLine two", "failed": False})
        self.assertEqual(out["posts"], [["/api/chat", {"scope": "tasks", "page": "Tasks", "text": "What is left?",
                                         "selected": {"id": "t12", "type": "item", "label": "#12 Plan", "item": 12}}, 64]])
        self.assertEqual(out["rows"], [None, {"id": "4", "type": "brief", "label": "x", "item": None},
                                       {"id": "a", "type": "", "label": "", "item": None}])

    def test_a_refusal_shows_the_servers_words_and_no_reply_says_so(self):
        out = self.run_chat("""ANSWER = () => [409, { error: 'A chat answer is still running; ask again when it is shown.' }];
  R.busy = await askChat({ text: 'x' });
  ANSWER = () => [504, { error: 'No answer: claude -p gave no answer in 120 s.' }];
  R.slow = await askChat({ text: 'x' });
  ANSWER = () => { throw new TypeError('Load failed'); };
  R.lost = await askChat({ text: 'x' });
  ANSWER = () => [200, {}];
  R.empty = await askChat({ text: 'x' });""")
        self.assertEqual(out["busy"], {"text": "A chat answer is still running; ask again when it is shown.", "failed": True})
        self.assertEqual(out["slow"], {"text": "No answer: claude -p gave no answer in 120 s.", "failed": True})
        self.assertEqual(out["lost"], {"text": "No answer: the dashboard did not reply (Load failed).", "failed": True})
        self.assertEqual(out["empty"], {"text": "No answer: the server sent none.", "failed": True})

    def test_the_mockup_line_is_gone_from_the_built_dashboard(self):
        self.assertNotIn("No chat backend is connected", SHELL_JS)
        self.assertIn(".msg p.answer, .msg p.failed { white-space: pre-wrap;", SHELL_CSS)


class TheActBar(unittest.TestCase):
    """sd:3464, sd:3444: the Details Act area holds the buttons; the CLI lines and the off and copy-only notes fold away."""

    def test_the_cli_section_is_a_details_element_closed_by_default(self):
        bar = re.search(r"  function bar\(id\) \{.*?\n  \}\n", SHELL_JS, re.S).group(0)
        self.assertIn('<details class="cli-all"><summary>Same thing from the CLI</summary>', bar)
        self.assertNotIn("<details class=\"cli-all\" open", bar)
        self.assertNotIn("<h3>Same thing from the CLI</h3>", bar)
        # Nothing explains a command between the buttons and the fold: the off reasons are inside it.
        self.assertRegex(bar, r'</div>\s*<details class="cli-all">')

    def test_a_command_whose_job_runs_shows_as_a_turning_button_not_an_off_line(self):
        """pack sd:7675: Run now on a running job is off (launchd refuses a second start), so the bar folded it away and
        nothing said the job runs. A command whose `busy(o)` holds shows as its button, disabled and aria-busy, with the
        turning icon and words that say it runs; reduced motion holds the icon still."""
        bar = re.search(r"  function bar\(id\) \{.*?\n  \}\n", SHELL_JS, re.S).group(0)
        R = run_js("""const tipOf = c => c.label, cliOf = c => c.cli(), noCli = () => false,
  executes = () => true, offWhy = (c, o) => { const w = c.when ? c.when(o) : true; return w === true ? '' : w; };
const REGS = [{ id: 'jobs.retry', on: 'job', label: 'Run now', key: 't', cli: () => 'sd jobs retry x', when: o => !o.running || 'the job is already running', busy: o => o.running },
  { id: 'jobs.cancel', on: 'job', label: 'Cancel', key: 'x', cli: () => 'sd jobs cancel x', when: o => o.running || 'not running', primary: o => o.running }];
const OBJ = new Map([['job:a', { type: 'job', running: true }], ['job:b', { type: 'job', running: false }]]), cmdsFor = o => REGS.filter(c => c.on === o.type);
""" + bar + "R.on = String(bar('job:a')); R.idle = String(bar('job:b'));")
        self.assertRegex(R["on"], r'<button class="btn quiet" type="button" data-cmd="jobs.retry"[^>]* disabled aria-busy="true"><svg class="i" aria-hidden="true"><use href="#i-rotate-ccw"/></svg>Running')
        self.assertRegex(R["on"], r'<button class="btn" type="button" data-cmd="jobs.cancel"')
        self.assertNotIn("<b>Run now</b> is off", R["on"])
        self.assertNotIn("aria-busy", R["idle"])
        rules = re.findall(r"([^{}]+)\{([^{}]*)\}", re.sub(r"/\*.*?\*/", "", SHELL_CSS, flags=re.S))
        turn = [sel for sel, body in rules if '[aria-busy="true"]' in sel and "svg" in sel and "animation:" in body and "none" not in body]
        self.assertTrue(turn, "no rule turns a busy button's icon")
        still = re.search(r"@media \(prefers-reduced-motion: reduce\) \{[^@]*?\[aria-busy=\"true\"\][^{]*svg[^{]*\{ animation: none; \}", SHELL_CSS)
        self.assertIsNotNone(still, "reduced motion does not hold the busy icon still")

    def test_a_help_icon_with_a_command_copies_it_when_pressed(self):
        click = re.search(r"document\.addEventListener\('click', e => \{\n(?:    .*\n)?    const b = e\.target\.closest\('\.help'\);.*?\}, true\);", SHELL_JS, re.S)
        self.assertIsNotNone(click)
        self.assertIn("if (b.dataset.copy) copy(b.dataset.copy);", click.group(0))


# How-to and legend words that sd:5733 moved out of view text, captions and table headers into the help line.
HOWTO = re.compile(r"\b(Drag|[Pp]ress \d|Tap|[Cc]lick|or use Move to)\b|Run now starts|lamps per piece")


class TheTableHelp(unittest.TestCase):
    """sd:5733: how to work a table and what its marks mean sit in one help line under it; its header keeps what it is."""

    def test_the_help_line_draws_the_legend_then_the_how_to(self):
        R = run_js("""R.both = String(list.help('Tap a job to see its reports.', [[html`<span class="g-ok">●</span>`, 'ran clean']]));
R.text = String(list.help('Drag a card.')); R.none = String(list.help(''));""")
        self.assertEqual(R["both"], '<p class="list-help"><span class="key"><span class="g-ok">●</span> ran clean</span>'
                                    '<span>Tap a job to see its reports.</span></p>')
        self.assertEqual(R["text"], '<p class="list-help"><span>Drag a card.</span></p>')
        self.assertEqual(R["none"], "")

    def test_no_view_text_caption_or_table_header_holds_how_to_or_legend_words(self):
        heads = []
        for page in V2.glob("*.html"):
            text = page.read_text(encoding="utf-8")
            heads += [(page.name, t) for t in re.findall(r"<caption[^>]*>(.*?)</caption>", text, re.S)]
            heads += [(page.name, t) for t in re.findall(r'<div class="panel-head">\s*<div>(.*?)</div>', text, re.S)]
        for name, js in PAGE_JS.items():
            heads += [(name, t) for t in re.findall(r"const SUB = \{(.*?)\};", js, re.S)]
            heads += [(name, t) for t in re.findall(r"(?:'subhead'|'cad-caption'|sub\.textContent =)[^;]*", js)]
        self.assertTrue(any(name == "tasks.js" for name, _ in heads), "the walk found no Tasks view text")
        for name, text in heads:
            with self.subTest(name, text=text[:60]):
                self.assertNotRegex(text, HOWTO)

    def test_each_moved_text_is_in_its_tables_help_line(self):
        for name, text in (("tasks.js", PAGE_JS["tasks.js"]), ("writing.js", PAGE_JS["writing.js"]),
                           ("reports.html", (V2 / "reports.html").read_text(encoding="utf-8")),
                           ("hoa.html", (V2 / "hoa.html").read_text(encoding="utf-8"))):
            with self.subTest(name):
                self.assertTrue("list.help(" in text or 'class="list-help"' in text)
        self.assertIn(".list-help {", SHELL_CSS)


# The shell's keys, as shell.js has them: the Keys section's keydown handler, after its chord counter.
KEYS_JS = re.search(r"^  let chord = 0;\n(  document\.addEventListener\('keydown', .*?^  \}\);)$", SHELL_JS, re.S | re.M).group(1)
# What the handler reads from the rest of the shell: no overlay open, one selected row (selId), and a record of what ran.
KEYS_STUBS = r"""
const pal = { open: false }, pop = { hidden: true }, menuEl = { hidden: true, contains: () => false }, pane = { hasAttribute: () => false };
const rail = { hasAttribute: () => false }, body = { dataset: {} }, keys = {}, t = { querySelector: () => null }, CHORDS = {};
let chord = 0, pinned = null, tUndo = null, selId = 'row-1';
const ran = n => () => { OUT.ran = (OUT.ran || []).concat(n); };
const openPal = ran('palette'), hideHelp = ran('hideHelp'), closeMenu = ran('closeMenu'), closePane = ran('closePane'), setMenu = () => {};
const shiftClear = () => false, go = ran('go'), step = () => false, flipTime = ran('flipTime'), openMenu = ran('menu'), openCapture = ran('capture');
const openChat = ran('chat'), setRail = ran('rail'), setPane = ran('pane'), openPane = ran('pane'), modal = ran('keys'), wide = () => true;
const commands = { pick: ran('pick') };
document.addEventListener('shell:open', e => { OUT.opened = e.detail; });
// An element of tag with attributes attrs: matches() reads the simple selectors the handler names (tag, .class, [attr], [attr="v"]).
const el = (tag, attrs = {}, cls = []) => ({ tagName: tag.toUpperCase(), closest: () => null,
  matches: list => list.split(',').some(s => { const m = /^([a-z]*)((?:\.[\w-]+)*)((?:\[[^\]]+\])*)$/.exec(s.trim()); if (!m) throw new Error('selector ' + s);
    return (!m[1] || m[1] === tag) && (m[2].match(/[\w-]+/g) || []).every(c => cls.includes(c))
      && (m[3].match(/\[[^\]]+\]/g) || []).every(a => { const [, k, v] = /^\[([\w-]+)(?:="([^"]*)")?\]$/.exec(a); return k in attrs && (v === undefined || attrs[k] === v); }); }) });
const press = (key, target) => { const e = { key, target, prevented: false, preventDefault() { e.prevented = true; } };
  OUT.opened = null; DOC_LISTENERS.keydown.forEach(f => f(e)); return { prevented: e.prevented, opened: OUT.opened }; };
"""


def run_keys(body):
    """The shell's keydown handler over the stand-in document, with KEYS_STUBS; body sets R."""
    script = (STAND_IN + KEYS_STUBS + KEYS_JS + "\nvar R = {};\ntry {\n" + body + "\n} catch (e) { OUT.error = String(e) + ' ' + e.stack; }\n"
              + "function run() { OUT.R = R; return JSON.stringify(OUT); }\n")
    result = subprocess.run([OSASCRIPT, "-l", "JavaScript", "-e", script], capture_output=True, text=True, timeout=60, check=False)
    if result.returncode:
        raise AssertionError(result.stderr)
    out = json.loads(result.stdout)
    if out["error"]:
        raise AssertionError(out["error"])
    return out["R"]


class TheFilterPick(unittest.TestCase):
    """sd:6254: the operator's Repo pick on Tasks did not work. A pick's summary grew with the picked name, so the control
    wrapped to the next line of the filter bar and its open menu left the pointer: the next click landed outside and closed
    it. And Enter on a focused summary opened the selected row, not the menu, on every page with a selected row."""

    def test_a_multi_pick_keeps_its_size_whatever_is_picked(self):
        R = run_js("""const opts = [['system', 'system'], ['sd-workflow', 'sd-workflow'], ['hoa', 'hoa']];
R.none = String(list.pick('repo', 'Repo', opts, new Set(), { multi: true }));
R.one = String(list.pick('repo', 'Repo', opts, new Set(['sd-workflow']), { multi: true }));
R.two = String(list.pick('repo', 'Repo', opts, new Set(['system', 'hoa']), { multi: true }));""")
        summary = {k: re.search(r"<summary[^>]*>(.*?)</summary>", v, re.S).group(1) for k, v in R.items()}
        shown = {k: re.search(r'<span id="f-repo-v">([^<]*)</span>', s).group(1) for k, s in summary.items()}
        self.assertEqual(shown, {"none": "All", "one": "sd-workflow", "two": "2 picked"})
        # Beside the shown label, every summary holds the same labels out of sight: each one it can show, as a select's
        # options size it, so the pill is as wide as its widest label whatever is picked.
        rest = {k: s.replace(f'<span id="f-repo-v">{shown[k]}</span>', "") for k, s in summary.items()}
        self.assertEqual(rest["none"], rest["one"])
        self.assertEqual(rest["none"], rest["two"])
        self.assertEqual(rest["none"], '<span class="fpick-size" aria-hidden="true"><span>All</span><span>3 picked</span>'
                                       '<span>system</span><span>sd-workflow</span><span>hoa</span></span>')
        # They share the shown label's one grid cell and take no space or paint of their own.
        self.assertIn(".fpick > summary { display: grid; }", SHELL_CSS)
        self.assertIn(".fpick > summary > span, .fpick-size > span { grid-area: 1 / 1; overflow: hidden; text-overflow: ellipsis; }", SHELL_CSS)
        self.assertIn(".fpick-size { display: contents; visibility: hidden; }", SHELL_CSS)

    def test_enter_on_a_pick_summary_opens_its_menu_not_the_selected_row(self):
        R = run_keys("""R.summary = press('Enter', el('summary'));
R.row = press('Enter', el('div', { 'data-id': 'row-1' }));
R.button = press('Enter', el('button'));""")
        self.assertEqual(R["row"], {"prevented": True, "opened": "row-1"}, "the stand-in reaches the shell's Enter")
        self.assertEqual(R["button"], {"prevented": False, "opened": None})
        self.assertEqual(R["summary"], {"prevented": False, "opened": None})

    # The shell's open-menu rules, as shell.js has them: toggle, a press outside, focus leaving.
    MENU_JS = re.search(r"^  // A filter dropdown in use stays open across a redraw;.*?\n\n", SHELL_JS, re.S | re.M).group(0)

    def test_a_tap_on_a_box_keeps_the_menu_open_where_the_browser_focuses_an_ancestor(self):
        """sd:6254, the operator's iPad: WebKit does not focus a checkbox on a press. A tap on a box moved focus from the
        summary to <main tabindex="-1">, its focusable ancestor; the focusout rule read that as focus leaving the menu and
        shut it on mousedown, so the click fell on what lay under it and nothing was picked. Chrome focuses the box."""
        R = run_keys(self.MENU_JS + r"""
// A tree: main > details.fpick[open] > summary, input; and a button outside the menu.
const node = (name, parent, extra = {}) => Object.assign({ name, parent,
  contains(n) { for (; n; n = n.parent) if (n === this) return true; return false; },
  closest(sel) { for (let n = this; n; n = n.parent) if (sel.split(', ').includes('details.fpick[open]') && n.name === 'details' && n.open) return n; return null; } }, extra);
const main = node('main', null), menu = node('details', main, { open: true }), summary = node('summary', menu), box = node('input', menu);
const outside = node('button', main);
const blur = (target, relatedTarget) => { menu.open = true; DOC_LISTENERS.focusout.forEach(f => f({ target, relatedTarget })); return menu.open; };
R.toMain = blur(summary, main); R.fromBoxToMain = blur(box, main); R.toBox = blur(summary, box); R.toNothing = blur(summary, null);
R.toOutside = blur(summary, outside);""")
        self.assertTrue(R["toMain"], "a press on a box moved focus to an ancestor and shut the menu")
        self.assertTrue(R["fromBoxToMain"])
        self.assertTrue(R["toBox"])
        self.assertTrue(R["toNothing"])
        self.assertFalse(R["toOutside"], "focus leaving for a control outside still shuts it")


class TheSharedRules(unittest.TestCase):
    """sd:2588: shell.css holds each shared annunciator and ledger rule once; no page repeats one."""

    def test_shell_css_holds_each_shared_rule_and_no_page_repeats_it(self):
        for rule in SHARED:
            self.assertEqual(SHELL_CSS.count(rule), 1, rule)
            for name, css in PAGE_CSS.items():
                with self.subTest(name, rule=rule[:30]):
                    self.assertNotIn(rule, css)

    def test_no_page_restyles_the_lamp_or_the_ledger(self):
        for name, css in PAGE_CSS.items():
            if name == "home.css":  # Home's lamp grid is its own component (home.js), not the annunciator
                continue
            with self.subTest(name):
                self.assertEqual([m.group(0).strip() for m in SHELL_ONLY.finditer(wide(css))], [])

    def test_the_shell_leaves_the_lamp_to_the_foundation_strip(self):
        self.assertEqual([m.group(0).strip() for m in LAMP_ONLY.finditer(wide(SHELL_CSS))], [])
        self.assertTrue(LAMP_ONLY.search(wide((STATIC / "annunciator-strip.css").read_text(encoding="utf-8"))),
                        "the pattern finds the strip's own lamp rules")

    def test_no_rule_lays_a_buttons_icon_label_and_count_on_lines_of_their_own(self):
        """sd:5729: shell.css's .btn is one nowrap flex line; HOA's touch rule made its toolbar buttons inline-grid, so on
        an iPad Acknowledge all stacked its icon, label and count. No rule on a .btn may make it a grid or let it wrap."""
        self.assertRegex(SHELL_CSS, r"\.btn \{[^}]*display: inline-flex;[^}]*white-space: nowrap;")
        for name, css in {**PAGE_CSS, "shell.css": SHELL_CSS}.items():
            found = []
            for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", re.sub(r"/\*.*?\*/", "", css, flags=re.S)):
                # Split the selector list at its top-level commas: a :not(.a, .b) holds commas of its own.
                selectors = [sel.strip() for sel in re.split(r",(?![^(]*\))", m.group(1))]
                if re.search(r"display:\s*(inline-)?grid|white-space:\s*(normal|pre-wrap)", m.group(2)):
                    found += [sel for sel in selectors if re.search(r"\.btn(\.[\w-]+|\[[^\]]*\]|:[\w-]+)*$", sel)]
            with self.subTest(name):
                self.assertEqual(found, [])

    def test_a_due_or_when_cell_keeps_its_date_on_one_line_and_no_page_lets_it_wrap(self):
        """sd:6185: shell.css's one date-cell rule holds every Due and When cell to one line; no page loosens it, even in
        a narrow container, and none sets a width for it."""
        self.assertRegex(SHELL_CSS, r"(?m)^td:is\(\.due, \.when\) \{ white-space: nowrap; \}")
        for name, css in PAGE_CSS.items():
            found = []
            for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", re.sub(r"/\*.*?\*/", "", css, flags=re.S)):
                if re.search(r"white-space:\s*(normal|pre-wrap)|(^|;)\s*(min-|max-)?width:", m.group(2)) and re.search(r"\.due\b", m.group(1)):
                    found.append(m.group(1).strip())
            with self.subTest(name):
                self.assertEqual(found, [])

    def test_one_ledger_layout_auto_with_no_colgroup(self):
        self.assertNotIn("table-layout", SHELL_CSS)
        for name, text in {**PAGE_CSS, **PAGE_JS, **{p.name: p.read_text(encoding="utf-8") for p in V2.glob("*.html")}}.items():
            with self.subTest(name):
                self.assertFalse("table-layout" in text or "<colgroup" in text)


class OneLayoutOnEveryPage(unittest.TestCase):
    """Pack sd:7084: views switch only in the rail; a section head's actions are quiet and one size; filters sit in the
    filter row, not the toolbar."""

    PAGES = {p.name: p.read_text(encoding="utf-8") for p in V2.glob("*.html")}

    def test_the_shell_draws_no_tab_row(self):
        """The rail's sub nav is the one view switch; #viewswitch is only the marker for views with no panel."""
        self.assertNotIn(".viewswitch", SHELL_CSS)
        self.assertNotIn(".viewswitch", SHELL_JS)
        self.assertIn("#viewswitch, .place-switch:empty { display: none; }", SHELL_CSS)
        for name, page in self.PAGES.items():
            with self.subTest(name):
                self.assertNotRegex(page, r'id="viewswitch"[^>]*>[^<]')

    def test_every_section_head_action_is_a_quiet_button_of_one_size(self):
        self.assertRegex(SHELL_CSS, r"\.sec-head \.tools \.btn \{ min-height: 2rem;")
        for name, page in self.PAGES.items():
            for tools in re.findall(r'<div class="sec-head">.*?<div class="tools"[^>]*>(.*?)</div>', page, re.S):
                for cls in re.findall(r'<button class="(btn[^"]*)"', tools):
                    with self.subTest(name, cls=cls):
                        self.assertIn("quiet", cls.split())

    def test_no_toolbar_row_holds_a_segment_group(self):
        """A .vseg or .fseg is a filter: it sits in the filter row (.filters.toolbar-2), never in the toolbar."""
        for name, page in self.PAGES.items():
            for row in re.findall(r'<div class="toolbar"[^>]*>\n(.*?)\n  </div>', page, re.S):
                with self.subTest(name):
                    self.assertNotRegex(row, r'class="(vseg|fseg)"')

    def test_no_section_head_or_tally_holds_a_filter(self):
        """Briefs' Range chips sat in each view's tally, and a place's Issues set-aside group beside its title."""
        class Heads(HTMLParser):
            """The class names of every element inside a .sec-head."""

            def __init__(self):
                super().__init__()
                self.depth, self.seen = 0, []

            def handle_starttag(self, tag, attrs):
                cls = (dict(attrs).get("class") or "").split()
                if self.depth and tag == "div":
                    self.depth += 1
                elif tag == "div" and "sec-head" in cls:
                    self.depth = 1
                if self.depth:
                    self.seen += cls

            def handle_endtag(self, tag):
                if self.depth and tag == "div":
                    self.depth -= 1

        for name, page in self.PAGES.items():
            heads = Heads()
            heads.feed(page)
            with self.subTest(name):
                self.assertFalse({"chips", "vseg", "fseg"} & set(heads.seen))
        places = (STATIC / "places.js").read_text(encoding="utf-8")
        self.assertIn('<div class="filters toolbar-2" id="place-issues-filters">${window.shell.aside.seg(\'issues-hidden\'', places)


class EveryFoldControlIsALink(unittest.TestCase):
    """sd:5878: every expand, collapse, Show all and Show more control has the table bar's link look (shell.js tfolds), the
    one shared class `linkbtn`; Reports' Show all jobs and Notes' Show all were buttons (btn quiet sm)."""

    maxDiff = None
    FOLD = re.compile(r"\b(Show (all|fewer|more|the first|only)|Expand all|Collapse all)\b|data-[\w-]*more|id=\"[\w-]*more\"|data-tfold")

    def test_no_fold_or_show_all_control_carries_btn(self):
        sources = {**PAGE_JS, "shell.js": SHELL_JS, **{p.name: p.read_text(encoding="utf-8") for p in V2.glob("*.html")}}
        found, wrong = [], []
        for name, text in sources.items():
            for m in re.finditer(r"<button\b([^>]*)>(.*?)</button>", text, re.S):
                # A help icon's text names these controls; it is not one.
                if not self.FOLD.search(re.sub(r'data-help="[^"]*"', "", m.group(1)) + m.group(2)[:200]):
                    continue
                classes = (re.search(r'class="([^"]*)"', m.group(1)) or [None, ""])[1].split()
                found.append(name)
                if "btn" in classes or "linkbtn" not in classes:
                    wrong.append(f"{name}: {m.group(0)[:120]}")
        self.assertEqual(wrong, [])
        # The sweep reaches each page that draws one, so a renamed control cannot leave it empty. today-github.js pages its
        # table with the shell's pager since sd:5899, so it draws none. Today's group folds are today.js's
        # since sd:5921 deleted today-status.js.
        self.assertLessEqual({"shell.js", "notes.js", "reports.html", "research.js", "today.js"}, set(found))


class EveryDialogHasAClose(unittest.TestCase):
    """sd:5883: every dialog a page draws carries an X (aria-label Close) in its header, and the X presses the dialog's own
    Cancel ([data-cancel]), so it runs the Cancel path, never a copy of it. Esc stays the browser's."""

    maxDiff = None
    X = re.compile(r'data-dlg-x|\$\{(CLOSE_X|window\.shell\.closeX)\}')
    SOURCES = {**PAGE_JS, "shell.js": SHELL_JS, **{p.name: p.read_text(encoding="utf-8") for p in V2.glob("*.html")}}

    def test_each_dialog_form_carries_the_x_and_one_cancel(self):
        forms, wrong = [], []
        for name, text in self.SOURCES.items():
            for m in re.finditer(r'<form method="dialog".*?</form>', text, re.S):
                forms.append(name)
                cancels = re.findall(r"<button\b[^>]*\bdata-cancel\b[^>]*>", m.group(0))
                if not self.X.search(m.group(0)) or len(cancels) != 1:
                    wrong.append(f"{name}: {m.group(0)[:100]}")
        self.assertEqual(wrong, [])
        # Confirm and capture, Start next, Build (commands.js, Tasks' and Today's, pack sd:7271), Writing's correction, the
        # three Tasks prompts and Quick notes.
        self.assertEqual(sorted(forms), sorted(["shell.js"] * 2 + ["tasks.js", "commands.js", "writing.js", "notes.html"] + ["task-details.js"] * 3))
        # The key sheet is the shell's one dialog without a form; its Close is the Cancel the X presses.
        keys = re.search(r"put\(keys, html`(.*?)`\);", SHELL_JS, re.S).group(1)
        self.assertRegex(keys, self.X)
        self.assertRegex(keys, r'<button[^>]*id="keys-close"[^>]*data-cancel|<button[^>]*data-cancel[^>]*id="keys-close"')

    def test_every_file_that_opens_a_dialog_is_swept(self):
        opens = {name for name, text in self.SOURCES.items() if re.search(r"showModal\(|createElement\('dialog'\)|<dialog\b", text)}
        self.assertEqual(opens, {"shell.js", "tasks.js", "commands.js", "writing.js", "notes.html", "notes.js", "task-details.js"})

    def test_the_x_presses_the_dialogs_cancel_and_closes_nothing_itself(self):
        block = re.search(r"// dlgx:start\n(.*?)// dlgx:end", SHELL_JS, re.S)
        self.assertIsNotNone(block, "shell.js has no dlgx block")
        script = (block.group(1) + """
const OUT = [];
const cancel = { click: () => OUT.push('cancel') };
const dlg = { close: () => OUT.push('close'), querySelector: s => s === '[data-cancel]' ? cancel : null };
const x = { closest: s => s === 'dialog' ? dlg : null };
pressCancel(x); pressCancel({ closest: () => null });
function run() { return JSON.stringify(OUT); }
""")
        out = subprocess.run([OSASCRIPT, "-l", "JavaScript", "-e", script], capture_output=True, text=True, timeout=60, check=False)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(json.loads(out.stdout), ["cancel"])

    def test_the_x_is_an_icon_button_named_close(self):
        self.assertIn("const CLOSE_X = html`<button class=\"icon-btn dlg-x\" type=\"button\" data-dlg-x aria-label=\"Close\">${ICON('x')}</button>`;", SHELL_JS)
        self.assertIn('<button class="icon-btn dlg-x" type="button" data-dlg-x aria-label="Close"><svg class="i" aria-hidden="true"><use href="#i-x"/></svg></button>',
                      (V2 / "notes.html").read_text(encoding="utf-8"))


class TheBulkBar(unittest.TestCase):
    """sd:5904: a bulk button reads its command's label alone; the bar's "<b>N</b> selected" already counts the rows."""

    def test_each_bulk_button_reads_its_command_label_alone(self):
        found = re.search(r"  function renderBulk\(\) \{\n.*?\n  \}\n", SHELL_JS, re.S)
        self.assertIsNotNone(found, "shell.js has no renderBulk")
        out = run_js("""const picked = new Set(['a', 'b']), OBJ = new Map([['a', { type: 't' }], ['b', { type: 't' }]]);
  const REG = [{ id: 't.hide', on: 't', label: 'Hide', bulk: true }], offWhy = () => '';
  const bulk = { hidden: true }, put = (el, m) => { el.html = String(m); };
""" + found.group(0) + "  renderBulk(); R.html = bulk.html;")
        self.assertIn("<b>2</b> selected", out["html"])
        self.assertIn('data-bulk="t.hide">Hide</button>', out["html"])


class TheRowMenu(unittest.TestCase):
    """sd:7206: the ⋯ menu leaves out a command its row already shows as a button; the menu's key still runs it."""

    def run_menu(self, body):
        found = re.search(r"^  // menu:start\n(.*?)^  // menu:end$", SHELL_JS, re.S | re.M)
        self.assertIsNotNone(found, "shell.js has no menu:start ... menu:end block")
        return run_js("""var innerHeight = 900, body = document.body, put = window.markup.put, RAN = [];
  const OBJ = new Map([['r1', { id: 'r1', label: 'Row one' }]]);
  const CMDS = [{ id: 't.open', label: 'Open', icon: 'eye', key: 'o', primary: () => true },
    { id: 't.snooze', label: 'Snooze', icon: 'clock', key: 'z' }, { id: 't.copy', label: 'Copy', key: 'y' }];
  const live = () => CMDS, doesOf = c => `${c.label} it`, shownCli = () => '', run = (c, o) => RAN.push([c.id, o.id]);
  // A row's ⋯ button: its .rowact holds the buttons rowActions drew, each a data-cmd for its object.
  const row = btns => ({ setAttribute() {}, getBoundingClientRect: () => ({ left: 0, right: 0, top: 0, bottom: 0 }),
    closest: s => s === '.rowact' ? { querySelectorAll: q => q === '[data-cmd]' ? btns.map(([cmd, obj]) => ({ dataset: { cmd, obj } })) : [] } : null });
  const items = () => [...menuEl.html.matchAll(/role="menuitem" type="button" data-cmd="([^"]+)"/g)].map(m => m[1]);
""" + found.group(1) + "  menuEl.querySelector = () => ({ focus() {}, click() {} });\n" + body)

    def test_the_menu_leaves_out_what_the_row_shows_and_keeps_the_rest(self):
        out = self.run_menu("""openMenu('r1', row([['t.open', 'r1']])); R.primary = items(); closeMenu();
  openMenu('r1', row([['t.open', 'r1'], ['t.snooze', 'r1']])); R.icons = items(); R.html = menuEl.html;
  menuEl.listeners.keydown[0]({ key: 'z', preventDefault() {}, stopPropagation() {} }); R.ran = RAN;
  openMenu('r1', row([['t.open', 'r2']])); R.other = items(); closeMenu();
  openMenu('r1', null); R.none = items();""")
        self.assertEqual(out["primary"], ["t.snooze", "t.copy"], "the primary button's command stayed in the menu")
        self.assertEqual(out["icons"], ["t.copy"], "an icon button's command stayed in the menu")
        self.assertIn('data-menu-open="r1"><span>Open details</span>', out["html"])
        self.assertIn('data-menu-ask="r1"><span>Ask in chat</span>', out["html"])
        # The key of a command the menu left out still runs it.
        self.assertEqual(out["ran"], [["t.snooze", "r1"]])
        # Only this row's own buttons count: another object's, or no row at all, leaves every command in.
        self.assertEqual(out["other"], ["t.open", "t.snooze", "t.copy"])
        self.assertEqual(out["none"], ["t.open", "t.snooze", "t.copy"])


class TheViewPicker(unittest.TestCase):
    """sd:5641: a page's layout menu (Tasks' List, Board, Matrix; Writing's Board, Editor) is the shell's one viewPick."""

    def test_each_page_with_a_view_menu_wires_it_through_the_shell(self):
        pages = sorted(p.stem for p in V2.glob("*.html") if 'class="viewpick"' in p.read_text(encoding="utf-8"))
        self.assertEqual(pages, ["tasks", "writing"])
        for name in pages:
            with self.subTest(name):
                self.assertIn("window.shell.viewPick(", PAGE_JS[f"{name}.js"])
        self.assertIn("  // viewpick:start\n", SHELL_JS)
        self.assertIn("viewPick,", SHELL_JS)

    def test_no_page_keeps_its_own_menu_keys(self):
        # The menu's roving focus and Esc are the shell's; a page that reads its radio items again has copied them.
        for name, text in PAGE_JS.items():
            with self.subTest(name):
                self.assertNotIn('[role="menuitemradio"]', text)


class TheHallmarkStamp(unittest.TestCase):
    """sd:1333: every v2 stylesheet names the Hallmark run it came from; fonts.css is a font list, not a design."""

    def test_every_stylesheet_but_fonts_carries_a_hallmark_stamp(self):
        for path in sorted(STATIC.glob("*.css")):
            if path.name == "fonts.css":
                continue
            with self.subTest(path.name):
                self.assertRegex(path.read_text(encoding="utf-8"), r"(?m)^(/\*| \*) Hallmark · ")


class TheCells(unittest.TestCase):
    """sd:2680: markup.cells(spec) draws every annunciator's lamps; no page writes a lamp's markup by hand."""

    def test_every_lamp_page_draws_through_cells_and_writes_no_cell_markup(self):
        for page in LAMP_PAGES:
            with self.subTest(page):
                js = PAGE_JS[f"{page}.js"]
                self.assertTrue("cells(" in js and 'class="cell' not in js)

    def test_todays_server_drawn_lamps_follow_the_same_grammar(self):
        today = (V2 / "today.html").read_text(encoding="utf-8")
        lamps = re.search(r'<ul class="annunciator" id="annunciator">(.*?)</ul>', today, re.S).group(1)
        self.assertEqual(cell_grammar(self, re.sub(r">\s+<li>", "><li>", lamps)), [("button", "unknown")] * 6 + [("a", "unknown")] * 2)
        self.assertNotIn('class="cell', PAGE_JS["today.js"])

    def test_a_spec_becomes_a_button_a_link_or_a_div(self):
        out = run_js("""const { cells } = window.markup;
  R.button = cells([{ button: true, state: 'caution', pressed: true, attrs: html` data-area="wt"`, label: 'Worktrees', mark: ICON('folder'),
    val: html`<b>2</b> gone`, sr: 'state caution' }]).text;
  R.link = cells([{ href: '?row=a&b', state: 'ok', label: 'Use', mark: html`<span class="g" aria-hidden="true">●</span>`, val: '1 row', small: 'from claude' }]).text;
  R.div = cells([null, { state: 'unknown', cls: 'clock', label: '<x>', val: 'not read' }, false]).text;
  R.plain = cells([{ button: true, attrs: html` id="plain"`, label: 'Plain', val: 'now' }]).text;
  try { cells([{ attrs: ' onclick="x()"', label: 'a', val: 'b' }]); R.refused = false; } catch (e) { R.refused = e instanceof TypeError; }""")
        self.assertEqual(out["button"], '<li><button class="cell" type="button" data-area="wt" data-state="caution" aria-pressed="true">'
                         '<span class="lbl">Worktrees<svg class="i" aria-hidden="true"><use href="#i-folder"/></svg></span>'
                         '<span class="val"><b>2</b> gone</span><span class="sr">state caution</span></button></li>')
        self.assertEqual(out["link"], '<li><a class="cell" href="?row=a&amp;b" data-state="ok"><span class="lbl">Use<span class="g" aria-hidden="true">●</span></span>'
                         '<span class="val">1 row<small>from claude</small></span></a></li>')
        self.assertEqual(out["div"], '<li><div class="cell clock" data-state="unknown"><span class="lbl">&lt;x&gt;</span><span class="val">not read</span></div></li>')
        self.assertEqual(out["plain"], '<li><button class="cell" type="button" id="plain"><span class="lbl">Plain</span><span class="val">now</span></button></li>')
        self.assertTrue(out["refused"])
        self.assertEqual(cell_grammar(self, out["button"] + out["link"] + out["div"] + out["plain"]),
                         [("button", "caution"), ("a", "ok"), ("div", "unknown"), ("button", None)])


class TheLampHover(unittest.TestCase):
    """sd:2530: a lit lamp keeps its state colour under the pointer. The operator ruled for a ring (2026-10-13); the
    foundation strip draws it since ui-design sd:3743 (sd:4750), so no stylesheet here dims or rings a lamp."""

    STRIP = (STATIC / "annunciator-strip.css").read_text(encoding="utf-8")

    def test_no_state_hover_shifts_brightness(self):
        for name, css in {**PAGE_CSS, "shell.css": SHELL_CSS, "annunciator-strip.css": self.STRIP}.items():
            with self.subTest(name):
                self.assertNotRegex(css, r'data-state="(caution|warning|unknown)"\][^{]*:hover[^{]*\{[^}]*filter:(?!\s*none\b)')

    def test_the_strip_no_longer_dims(self):
        self.assertNotIn("brightness(", self.STRIP)

    def test_the_strip_draws_the_hover_ring_in_the_lamps_ink_token(self):
        for state, ink in (("caution", "--color-caution-lamp-ink"), ("warning", "--color-warning-ink"), ("unknown", "--color-ink-2")):
            self.assertIn(f'.cell[data-state="{state}"]:not([aria-pressed="true"]):is(:hover, .is-hover) '
                          f'{{ box-shadow: inset 0 0 0 1px var({ink}); }}', self.STRIP)

    def test_the_shell_neither_cancels_the_dim_nor_draws_a_ring(self):
        self.assertNotIn("filter:", SHELL_CSS.split("Annunciator and ledger")[1].split("The ledger")[0])
        self.assertNotRegex(SHELL_CSS, r'\.cell\[data-state="(caution|warning|unknown)"\][^{]*\{[^}]*box-shadow')


class TheSortHeader(unittest.TestCase):
    """sd:2527: each column with an order sorts through a button; only the sorted th carries aria-sort."""

    def test_only_the_sorted_th_carries_aria_sort_and_each_ordered_column_has_a_button(self):
        out = run_js("""const cols = [['a', 'Alpha'], ['b', 'Beta'], [null, 'Actions']];
  const st = { sort: 'b', dir: -1, page: 3 };
  R.head = sortHead(cols, st).text;
  R.flip = [sortBy({ target: { closest: () => ({ dataset: { sort: 'b' } }) } }, st), st.sort, st.dir, st.page];
  R.other = (sortBy({ target: { closest: () => ({ dataset: { sort: 'a' } }) } }, st), [st.sort, st.dir]);
  R.none = sortBy({ target: { closest: () => null } }, st);""")
        self.assertEqual(re.findall(r'aria-sort="(\w+)"', out["head"]), ["descending"])
        self.assertRegex(out["head"], r'<th scope="col" class="" aria-sort="descending"><button class="sorter" type="button" data-sort="b"[^>]*>Beta<svg[^>]*><use href="#i-arrow-down"/>')
        self.assertIn('data-sort="a" title="Sort ascending">Alpha<svg class="i" aria-hidden="true"><use href="#i-arrow-up-down"/>', out["head"])
        self.assertIn('<th scope="col" class="">Actions</th>', out["head"])
        self.assertEqual(out["flip"], [True, "b", 1, 1])
        self.assertEqual(out["other"], ["a", 1])
        self.assertFalse(out["none"])


class ThePager(unittest.TestCase):
    """sd:2528: a range line, numbered pages and the sizes 25, 50, 100 and 200."""

    def test_the_range_line_reads_like_51_to_100_of_1284(self):
        out = run_js("""R.mid = rangeText(1284, { page: 2, size: 50 }); R.last = rangeText(1284, { page: 26, size: 50 });
  R.none = rangeText(0, { page: 1, size: 50 }); R.html = pager(1284, { page: 2, size: 50 }, 'reports').text;""")
        self.assertEqual(out["mid"], "51–100 of 1,284")
        self.assertEqual(out["last"], "1,251–1,284 of 1,284")
        self.assertEqual(out["none"], "0 of 0")
        self.assertIn('<span class="range">51–100 of 1,284</span>', out["html"])
        self.assertEqual(re.findall(r'data-size="(\d+)" aria-pressed="(\w+)"', out["html"]),
                         [("25", "false"), ("50", "true"), ("100", "false"), ("200", "false")])
        self.assertEqual(re.findall(r'data-page="(\d+)"[^>]*aria-current="page"', out["html"]), ["2"])

    def test_pages_show_the_ends_and_two_either_side_with_one_gap_for_a_skipped_run(self):
        out = run_js("R.a = pageList(26, 2); R.b = pageList(26, 13); R.c = pageList(3, 1); R.d = pageList(1, 1);")
        self.assertEqual(out["a"], [1, 2, 3, 4, "…", 26])
        self.assertEqual(out["b"], [1, "…", 11, 12, 13, 14, 15, "…", 26])
        self.assertEqual(out["c"], [1, 2, 3])
        self.assertEqual(out["d"], [1])

    def test_a_click_moves_the_page_and_a_size_starts_again_at_page_one(self):
        out = run_js("""const st = { page: 4, size: 50 }, at = (sel, ds) => ({ target: { closest: s => s === sel ? { dataset: ds } : null } });
  R.page = [paging(at('.list-pager [data-page]', { page: '7' }), st), st.page];
  R.size = [paging(at('.list-pager [data-size]', { size: '200' }), st), st.page, st.size];
  const rows = Array.from({ length: 120 }, (_, i) => i), late = { page: 9, size: 50 };
  R.slice = [pageOf(rows, late).length, late.page];""")
        self.assertEqual(out["page"], [True, 7])
        self.assertEqual(out["size"], [True, 1, 200])
        self.assertEqual(out["slice"], [20, 3])


class TheFocusAfterARedraw(unittest.TestCase):
    """sd:2682 review: a redraw replaces the header and the pager, so focus moves to the new copy of the clicked button."""

    def test_focus_goes_to_the_redrawn_copy_of_the_clicked_control(self):
        out = run_js("""const click = ds => {
    const e = { target: { closest: () => ({ dataset: ds }) }, currentTarget: { querySelector: s => ({ focus: () => R.seen.push([s, R.drawn]) }) } };
    keepFocus(e, () => R.drawn++);
  };
  R.seen = []; R.drawn = 0;
  click({ sort: 'mod' }); click({ page: '3' }); click({ size: '100' });
  keepFocus({ target: { closest: () => null }, currentTarget: null }, () => R.drawn++);""")
        self.assertEqual(out["seen"], [['button[data-sort="mod"]', 1], ['button[data-page="3"]', 2], ['button[data-size="100"]', 3]])
        self.assertEqual(out["drawn"], 4)

    def test_every_page_redraws_a_sort_or_pager_click_through_keep_focus(self):
        for name, js in PAGE_JS.items():
            for line in re.findall(r"(?m)^.*\b(?:sortBy|paging)\(e, .*$", js):
                with self.subTest(name, line=line.strip()[:80]):
                    self.assertIn("keepFocus(e, ", line)

    def test_page_size_and_sort_round_trip_through_the_url_and_bad_values_are_ignored(self):
        out = run_js("""const opts = { sorts: ['at', 'what'], size: 50 }, defaults = { sort: 'at', dir: -1, size: 50 };
  const st = listParams(new URLSearchParams('?page=3&size=100&sort=what&dir=desc'), {}, opts);
  R.read = st; R.back = listQuery(new URLSearchParams(), st, defaults).toString();
  R.bad = listParams(new URLSearchParams('?page=1.5&size=30&sort=nope'), { page: 1, sort: 'at', dir: -1 }, opts);
  R.hex = listParams(new URLSearchParams('?page=0x2'), { page: 1 }, opts).page;
  R.plain = listQuery(new URLSearchParams(), { page: 1, size: 50, sort: 'at', dir: -1 }, defaults).toString();""")
        self.assertEqual(out["read"], {"page": 3, "size": 100, "sort": "what", "dir": -1})
        self.assertEqual(out["back"], "page=3&size=100&sort=what&dir=desc")
        self.assertEqual(out["bad"], {"page": 1, "sort": "at", "dir": -1, "size": 50})
        self.assertEqual(out["hex"], 1)
        self.assertEqual(out["plain"], "")


class TheFilterChips(unittest.TestCase):
    """sd:2529: one removable chip per active filter, a count, and one Clear all."""

    def test_chips_name_each_filter_the_count_and_clear_all(self):
        out = run_js("""R.two = chips([{ key: 'job', label: 'Job: nightly' }, { key: 'act', label: 'Needs action' }], 3, 1284).text;
  R.none = chips([], 10, 10).text;""")
        self.assertEqual(re.findall(r'data-unfilter="(\w+)"', out["two"]), ["job", "act"])
        self.assertIn('<span class="n">2 filters · 3 of 1,284</span>', out["two"])
        self.assertEqual(out["two"].count("data-unfilter-all"), 1)
        self.assertIn('aria-label="Remove filter: Job: nightly"', out["two"])
        self.assertEqual(out["none"], "")


TABLES = re.search(r"^  // tables:start\n(.*?)^  // tables:end$", (STATIC / "shell.js").read_text(encoding="utf-8"), re.S | re.M)


def run_tables(body):
    """The table grammar's model functions, as the shell has them; body sets R."""
    if TABLES is None:
        raise AssertionError("shell.js has no tables:start block")
    return run_js(TABLES.group(1) + body)


class TheTableGrammar(unittest.TestCase):
    """sd:5064: every table sorts, filters and folds through one grammar in shell.js."""

    ROWS = """const row = (pos, ...cells) => ({ kind: 'row', cells, pos }), band = (pos, label) => ({ kind: 'group', cells: [label], pos });
  const rows = [band(0, 'Mon'), row(1, '#10', 'job', '3'), row(2, '#9', 'mail', ''), row(3, '#2', 'job', '12'), { kind: 'static', cells: ['2 more'], pos: 4 },
    band(5, 'Tue'), row(6, '#1', 'job', '7'), band(7, 'Wed')];
  const cols = ['Item', 'Kind', 'Min'], ids = list => list.map(r => r.cells[0]);"""

    def test_a_sort_orders_rows_inside_each_band_numbers_as_numbers_and_empty_cells_last(self):
        out = run_tables(self.ROWS + """
  R.page = ids(torder(rows, { sort: null, dir: 1 }, cols));
  R.item = ids(torder(rows, { sort: 'Item', dir: 1 }, cols));
  R.minUp = ids(torder(rows, { sort: 'Min', dir: 1 }, cols));
  R.minDown = ids(torder(rows, { sort: 'Min', dir: -1 }, cols));
  R.lost = ids(torder(rows, { sort: 'Gone', dir: 1 }, cols));
  R.states = ['ok', 'warning', 'unknown', 'caution'].sort((a, b) => tcompare(a, b, 1));""")
        self.assertEqual(out["page"], ["Mon", "#10", "#9", "#2", "2 more", "Tue", "#1", "Wed"])
        self.assertEqual(out["item"], ["Mon", "#2", "#9", "#10", "2 more", "Tue", "#1", "Wed"])
        self.assertEqual(out["minUp"], ["Mon", "#10", "#2", "#9", "2 more", "Tue", "#1", "Wed"])
        self.assertEqual(out["minDown"], ["Mon", "#2", "#10", "#9", "2 more", "Tue", "#1", "Wed"])
        self.assertEqual(out["lost"], out["page"])
        self.assertEqual(out["states"], ["warning", "caution", "unknown", "ok"])

    def test_a_header_click_goes_ascending_descending_then_back_to_the_page_order(self):
        out = run_tables("""const st = { sort: null, dir: 1 }, seen = [];
  for (const l of ['Kind', 'Kind', 'Kind', 'Min', 'Kind']) { tsortBy(st, l); seen.push([st.sort, st.dir]); }
  R.seen = seen;""")
        self.assertEqual(out["seen"], [["Kind", 1], ["Kind", -1], [None, 1], ["Min", 1], ["Kind", 1]])

    def test_a_band_with_no_row_under_it_is_a_note_and_not_a_fold(self):
        out = run_tables(self.ROWS + """
  R.heads = tsegments(rows).filter(s => s.head).map(s => s.head.cells[0]);""")
        self.assertEqual(out["heads"], ["Mon", "Tue"])

    def test_filters_match_every_word_and_each_column_value_and_chips_name_them(self):
        out = run_tables(self.ROWS + """
  const st = { q: 'JOB 1', f: { Kind: 'job', Gone: 'x' } };
  R.shown = ids(rows.filter(r => tshows(r, st, cols)));
  R.chips = tchips(st);
  R.afterOne = tunfilter({ q: 'a', f: { Kind: 'job', Min: '3' } }, 'f:Kind');
  R.afterAll = tunfilter({ q: 'a', f: { Kind: 'job' } }, null);""")
        self.assertEqual(out["shown"], ["Mon", "#10", "#2", "2 more", "Tue", "#1", "Wed"])
        self.assertEqual(out["chips"], [{"key": "q", "label": "Text: JOB 1"}, {"key": "f:Kind", "label": "Kind: job"}, {"key": "f:Gone", "label": "Gone: x"}])
        self.assertEqual(out["afterOne"], {"q": "a", "f": {"Min": "3"}})
        self.assertEqual(out["afterAll"], {"q": "", "f": {}})

    def test_a_column_offers_its_values_when_they_repeat_over_four_rows_or_more(self):
        out = run_tables(self.ROWS + """
  R.few = tfacets(cols, rows.filter(r => r.cells[0] !== '#1'));
  R.facets = tfacets(cols, rows);
  R.skipped = tfacets(cols, rows, [false, true, false]);""")
        self.assertEqual(out["few"], [])
        self.assertEqual(out["facets"], [{"label": "Kind", "values": ["job", "mail"]}])
        self.assertEqual(out["skipped"], [])

    def test_every_table_with_a_row_gets_the_filters_and_its_count_on_one_row(self):
        # sd:5281: the toolbar standard. A column filter from the first data row, not the sixth. sd:5467 (operator,
        # 2026-10-20): a page with its own filter bar gets no column filter either, so one table has one place to filter, and
        # keeps only the folds. sd:5539: the text filter is the Shapeshift bar's, so no table draws a box of its own.
        out = run_tables("""R.one = tbar({ total: 1 }); R.none = tbar({ total: 0 }); R.on = tbar({ total: 0, filterOn: true });
  R.own = tbar({ total: 40, ownFilter: true, bands: 3 });""")
        self.assertEqual(out["one"], {"facet": True, "folds": False})
        self.assertEqual(out["none"], {"facet": False, "folds": False})
        self.assertEqual(out["on"], {"facet": True, "folds": False})
        self.assertEqual(out["own"], {"facet": False, "folds": True})

    def test_the_filter_bar_sits_in_the_tables_toolbar_with_the_count_at_its_right_end(self):
        # sd:5281: a table names its toolbar row (data-toolbar); the bar's parts join that row, the count last.
        js = (STATIC / "shell.js").read_text(encoding="utf-8")
        self.assertIn("const { facet: facetOn, folds: foldsOn } = tbar(", js)
        # The text is the Shapeshift bar's (sd:5539): a table the page filters hears it as shell:filter; the shell filters the rest.
        self.assertIn("const use = { ...st, q: ownFilter ? '' : shiftQ() }, chipped = tchips(shiftIn ? use : { ...use, q: '' }), filterOn = chipped.length > 0;", js)
        self.assertIn("document.addEventListener('shell:filter', () => { if (!tq) tq = requestAnimationFrame(tscan); });", js)
        # A table in a hidden view keeps its parts out of the shared row, and a view change rescans.
        self.assertIn("const away = !!t.dataset.toolbar && !!t.closest('[hidden]');", js)
        self.assertIn("document.addEventListener('shell:view', () => { if (!tq) tq = requestAnimationFrame(tscan); });", js)
        self.assertIn("document.getElementById(t.dataset.toolbar)", js)
        # Health and Management redraw a table on each filter key: the replaced table's bar leaves the shared row.
        self.assertIn("document.querySelectorAll('.tgrid').forEach(bar => { if (!bar._t?.isConnected) bar.remove(); });", js)
        self.assertIn("`${bar._name}: filter by column…`", js)
        self.assertIn(".toolbar > .tgrid, .filters > .tgrid { display: contents; }", SHELL_CSS)

    def test_the_view_tab_carries_the_row_count_and_the_table_bar_none(self):
        # sd:5524 (operator, 2026-10-20): "remove the row count, instead the sub tab view should carry that". The bar drew
        # "15 rows" on the title line; now each rescan gives every view the page does not count the shown rows of its one table.
        js = (STATIC / "shell.js").read_text(encoding="utf-8")
        self.assertNotIn("tcount", js)
        self.assertNotIn(".tcount", SHELL_CSS)
        self.assertIn("t._rows = { shown, total };", js)
        self.assertIn("view.auto(); shiftPlace(); tobs.takeRecords();", js)
        self.assertIn("viewSet(i, ts.length === 1 ? ts[0]._rows.shown : null);", js)
        self.assertIn("if (viewOwned.has(v.id) || (v.api && !viewSeen.has(v.id))) return;", js)
        # A page's own count wins: shell.view.count marks the view owned.
        self.assertIn("function viewCount(id, n) { viewOwned.add(id);", js)
        # The pages that printed a separate count put it on a tab, and the two pages without views have one now.
        for page, call in [("designs.js", "shell.view.count('pages', rows.length);"),
                           ("activity.js", "window.shell.view.count('timeline', shown);"),
                           ("briefs.js", "shell.view.count('list', all.length);"),
                           ("skills.js", "window.shell.view?.count('catalog', DOC ? v.length : null);"),
                           ("management.js", "shell.view.count('repos', rows.length);"),
                           ("documents.js", "window.PAGE_VIEWS = [{ id: 'documents', label: 'Documents' }, { id: 'books', label: 'Books', api: '/api/documents/books' },")]:
            self.assertIn(call, (STATIC / page).read_text(encoding="utf-8"), page)

    def test_a_kept_state_that_is_malformed_falls_back_field_by_field(self):
        out = run_tables("""R.bad = tstate({ sort: 3, dir: 7, q: null, f: ['x'], closed: 'Mon' });
  R.good = tstate({ sort: 'Kind', dir: -1, q: 'a', f: { Kind: 'job', n: 2 }, closed: ['Mon', 4] });
  R.none = tstate(null);""")
        self.assertEqual(out["bad"], {"sort": None, "dir": 1, "f": {}, "closed": []})
        # sd:5539: an older build kept the table's own text (q); it is not read back, so no row hides behind a box that is gone.
        self.assertEqual(out["good"], {"sort": "Kind", "dir": -1, "f": {"Kind": "job"}, "closed": ["Mon"]})
        self.assertEqual(out["none"], out["bad"])

    def test_a_band_with_data_band_keys_its_fold_by_it_so_a_changing_count_keeps_it_shut(self):
        """sd:5484: Documents' repo bands carry a count that moves with every filter; data-band names the fold instead."""
        js = (STATIC / "shell.js").read_text(encoding="utf-8")
        found = re.search(r"\n  (const tlabel = .*?;)\n", js), re.search(r"\n  (function tmodel\(t\) \{.*?\n  \})\n", js, re.S)
        self.assertTrue(all(found), "shell.js has no tlabel or tmodel")
        out = run_js(found[0].group(1) + "\n" + found[1].group(1) + """
  const td = text => ({ colSpan: 7, textContent: text, matches: () => false, dataset: {} });
  const tr = (text, data) => ({ cells: [td(text)], textContent: text, dataset: data, matches: () => false });
  const t = { tBodies: [{ rows: [tr('Lab3 documents', { band: 'lab' }), tr('Monday', {})] }] };
  R.cells = tmodel(t).map(r => [r.kind, r.cells[0]]);""")
        self.assertEqual(out["cells"], [["group", "lab"], ["group", "Monday"]])
        # The fold is keyed by the band's model cell, which is that name, not the band's words.
        self.assertIn("const label = s.head.cells[0]", js)

    def test_a_band_fold_keeps_a_data_tkeep_control_beside_its_button_not_inside_it(self):
        """sd:5497: Review's band carries a checkbox; a control inside the fold button would nest one control in another."""
        js = (STATIC / "shell.js").read_text(encoding="utf-8")
        found = re.search(r"\n  (function tbandFold\(td\) \{.*?\n  \})\n", js, re.S)
        self.assertIsNotNone(found, "shell.js has no tbandFold")
        out = run_js(found.group(1) + """
  const node = (name, keep) => ({ name, matches: s => keep && s === '[data-tkeep]' });
  const kids = [node('words'), node('count'), node('box', true)];
  const td = { childNodes: kids, prepend(b) { kids.unshift(b); }, append(b) { kids.push(b); } };
  const put = window.markup.put, bare = document.createElement;
  document.createElement = tag => { if (tag === 'template') return bare(tag); const e = El(tag); e.kids = []; e.append = (...n) => { n.forEach(x => { const i = kids.indexOf(x); if (i >= 0) kids.splice(i, 1); }); e.kids.push(...n); }; return e; };
  const b = tbandFold(td);
  R.td = kids.map(k => k.name || k.className || k.id);
  R.inButton = b.kids.flatMap(k => k.kids ? k.kids.map(x => x.name) : [k.name || 'icon']);""")
        self.assertEqual(out["td"], ["tfold", "box"])
        self.assertEqual([k for k in out["inButton"] if k != "icon"], ["words", "count"])

    def test_the_grammar_keeps_its_state_in_local_storage_inside_try_and_its_chips_apart(self):
        js = (STATIC / "shell.js").read_text(encoding="utf-8")
        self.assertIn("const TKEY = `sd.tables.${location.pathname}`;", js)
        self.assertIn("try { localStorage.setItem(TKEY, JSON.stringify(TKEPT)); } catch", js)
        self.assertIn("list.chips(chipped, shown, total, 'tunfilter')", js)
        self.assertIn("tr[data-tout] { display: none !important; }", SHELL_CSS)
        self.assertIn(':is(td, th) > .tfold[aria-expanded="true"] > .i:first-child { transform: rotate(90deg); }', SHELL_CSS)

    def test_a_folded_row_stays_hidden_under_every_page_rule_that_sets_a_row_display(self):
        # A page's mobile `.ledger tbody tr { display: grid }` outranks a plain `tr[data-tout]`, so a shut fold hid nothing at
        # 899px and narrower (sd:5490). The shell's hide rule must win over every page rule that gives a table row a display.
        rule = re.compile(r"([^{}@]+)\{([^{}]*)\}")
        setters = {}
        for css in sorted(STATIC.glob("*.css")):
            if css.name == "shell.css":
                continue
            for sel, body in rule.findall(css.read_text(encoding="utf-8")):
                disp = re.search(r"(?<![-\w])display\s*:\s*([^;]+)", body)
                if disp and re.search(r"(^|[\s,>+~])tr\b", sel):
                    setters.setdefault(css.name, []).append((sel.strip(), disp.group(1).strip()))
        for page in ("activity.css", "briefs.css", "designs.css", "contributions.css", "documents.css"):
            self.assertIn(page, setters, page + " sets a mobile row display")
        for page, rules in setters.items():
            for sel, value in rules:
                self.assertNotIn("!important", value, f"{page}: {sel} would beat the fold")
        self.assertIn("tr[data-tout] { display: none !important; }", SHELL_CSS)
        self.assertNotIn("tr[data-tout]", (STATIC / "documents.css").read_text(encoding="utf-8"))


class _Tree(HTMLParser):
    """Each start tag with the classes and ids of its ancestors, enough to place a control on its row."""
    VOID = {"input", "br", "img", "meta", "link", "hr", "source", "use", "path"}

    def __init__(self):
        super().__init__()
        self.stack, self.tags = [], []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        me = (tag, set((a.get("class") or "").split()), a.get("id"), a)
        self.tags.append((me, list(self.stack)))
        if tag not in self.VOID:
            self.stack.append(me)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self.VOID:
            self.stack.pop()

    def handle_endtag(self, tag):
        while self.stack and self.stack.pop()[0] != tag:
            pass


def tree(page):
    t = _Tree()
    t.feed(page.read_text(encoding="utf-8"))
    return t.tags


#: Scripts every page loads that draw no table of a page's own.
SHELL_SCRIPTS = {"shell", "markup", "sections", "read", "commands", "theme"}
TABLE = re.compile(r'<table\b([^>]*)>\s*(?:<caption(?:[^>]*\bid="([^"]+)")?[^>]*>([^<$]*))?')


def page_tables(page):
    """Each table the page or a script it loads draws, as (key, attributes); the key is its label, id, class or caption."""
    text = page.read_text(encoding="utf-8")
    sources = [text] + [(STATIC / f"{name}.js").read_text(encoding="utf-8")
                        for name in re.findall(r'<script src="/ui/([\w-]+)\.js"', text)
                        if name not in SHELL_SCRIPTS and (STATIC / f"{name}.js").exists()]
    for source in sources:
        for found in TABLE.finditer(source):
            attrs = dict(re.findall(r'([\w-]+)="([^"]*)"', found[1]))
            if "sr" in attrs.get("class", "").split():
                continue
            yield (attrs.get("aria-labelledby") or attrs.get("id") or attrs.get("aria-label")
                   or (attrs.get("class", "").split() or [None])[-1] or found[2] or (found[3] or "").strip()), attrs


def page_finds(page):
    """The ids of the rows (row 1 or a filter row) that hold the page's own text filter."""
    rows = set()
    for (tag, _c, _i, a), up in tree(page):
        if tag == "input" and a.get("type", "text") in ("text", "search") and a.get("role") != "combobox":
            row = next((i for _t, c, i, _x in reversed(up) if c & {"toolbar", "filters"}), None)
            if row:
                rows.add(row)
    return rows


class TheToolbarStandard(unittest.TestCase):
    """sd:5281: every page puts its controls on at most two rows from the shared standard, not in its head."""

    PAGES = sorted(V2.glob("*.html"))
    HEAD = {"head", "home-head"}
    # The controls the standard moved out of the head: Shapeshift, a page's view group, its range and its filter box.
    MOVED = {"shift", "views", "range", "filter", "repo-scope", "place-switch"}

    def test_no_page_head_holds_a_control(self):
        for page in self.PAGES:
            for (tag, cls, _id, _a), up in tree(page):
                in_head = any(c & self.HEAD for _t, c, _i, _x in up)
                with self.subTest(page=page.name, tag=tag, cls=sorted(cls)):
                    self.assertFalse(in_head and (tag in ("input", "select", "textarea") or cls & self.MOVED))

    def test_the_view_switch_and_the_moved_controls_sit_in_row_one(self):
        for page in self.PAGES:
            for (_tag, cls, id_, _a), up in tree(page):
                # A panel's own range (HOA's Trends) belongs to its panel head, not to the page's rows.
                if id_ == "viewswitch" or (cls & self.MOVED and not any("panel-head" in c for _t, c, _i, _x in up)):
                    with self.subTest(page=page.name, el=id_ or sorted(cls)):
                        self.assertTrue(any("toolbar" in c for _t, c, _i, _x in up))

    def test_a_page_has_one_row_one_and_its_filter_rows_are_row_two(self):
        for page in self.PAGES:
            tags = tree(page)
            with self.subTest(page=page.name):
                self.assertLessEqual(sum(1 for (_t, _c, i, _a), _u in tags if i == "toolbar"), 1)
                for (_tag, cls, _i, _a), _u in tags:
                    if cls & {"filters", "facets"}:
                        self.assertIn("toolbar-2", cls)
        for name in ("health.js", "management.js"):
            self.assertNotIn('<div class="filters"', (STATIC / name).read_text(encoding="utf-8"))

    def test_every_page_with_a_table_has_one_bar_and_no_box_of_its_own(self):
        # sd:5539 (operator, 2026-10-20): one "Filter, capture or ask" bar on every page with a table, and no table-level
        # "Filter rows" box. The shell draws the bar in row 1 marked data-shift; a page whose own Shapeshift has readings of
        # its own (a .shift in its markup) keeps that one. A page-filtered table's column filter joins the bar's row.
        for page in self.PAGES:
            tags, tables = tree(page), list(page_tables(page))
            marked = [i for (_t, _c, i, a), _u in tags if "data-shift" in a]
            own = [c for (_t, c, _i, _a), _u in tags if "shift" in c]
            finds = page_finds(page)
            with self.subTest(page=page.name):
                if tables:
                    self.assertEqual(len(marked) + len(own), 1, "a page with a table has one bar")
                self.assertEqual(set(marked), {"toolbar"} if marked else set(), "the shell's bar goes in row 1")
                if marked:
                    self.assertFalse(finds, "a text box of the page's own beside the shell's bar")
            for key, attrs in tables:
                with self.subTest(page=page.name, table=key):
                    if attrs.get("data-tfilter") == "page":
                        self.assertIn(attrs.get("data-toolbar"), set(marked) | finds, "its column filter is not in the bar's row")

    def test_a_page_with_its_own_bar_hands_its_text_to_every_other_table(self):
        # sd:5539 follow-up (operator, note 25755): the tables 697344e6 left with no text filter (Skills' tracked
        # repositories, Tasks' issues) take the page's own bar's text: the page calls shell.filter(q) at each change, and the
        # shell keeps it as the text filter of every table the page does not filter itself.
        for page in self.PAGES:
            own = any("shift" in c for (_t, c, _i, _a), _u in tree(page))
            others = [k for k, a in page_tables(page) if a.get("data-tfilter") != "page"]
            if not own or not others:
                continue
            text = page.read_text(encoding="utf-8")
            scripts = "".join((STATIC / f"{n}.js").read_text(encoding="utf-8") for n in re.findall(r'<script src="/ui/([\w-]+)\.js"', text)
                              if n not in SHELL_SCRIPTS and (STATIC / f"{n}.js").exists())
            with self.subTest(page=page.name, tables=others):
                self.assertTrue(re.search(r"\bshell\.filter\((q|Q)\)", scripts), "the page's bar never hands its text to the shell")
        self.assertIn("const shiftQ = () => shiftIn ? (shiftMode === 'filter' ? shiftIn.value.trim() : '') : pageQ;", SHELL_JS)
        self.assertIn("else if (a.length && v.trim() !== pageQ) { pageQ = v.trim(); if (!tq) tq = requestAnimationFrame(tscan); }", SHELL_JS)

    def test_a_page_that_filters_its_own_rows_hears_the_bar(self):
        # One hook (sd:5539): a page-filtered table on a page with the shell's bar takes the text from shell:filter, and
        # no page script keeps a box, a / key or a Shapeshift of its own.
        for page in self.PAGES:
            text = page.read_text(encoding="utf-8")
            if "data-shift" not in text:
                continue
            scripts = "".join((STATIC / f"{n}.js").read_text(encoding="utf-8") for n in re.findall(r'<script src="/ui/([\w-]+)\.js"', text)
                              if n not in SHELL_SCRIPTS and (STATIC / f"{n}.js").exists())
            with self.subTest(page=page.name):
                if any(a.get("data-tfilter") == "page" for _k, a in page_tables(page)):
                    self.assertIn("addEventListener('shell:filter'", scripts)
                self.assertNotIn("getElementById('shift')", scripts)
                self.assertNotIn("$('q')", scripts)
                self.assertNotRegex(scripts, r"\[\s*'/'\s*,", "a / key of the page's own: the shell's bar owns /")
                self.assertNotIn("shift-as", scripts)

    def test_each_view_keeps_row_one_visible(self):
        # Review round 1: HOA's row 1 sat inside view-overview, so picking Tasks hid the tabs; pack sd:7084: Today's row 1
        # sat inside Needs you. Select each view as shell.viewApply (view-<id>) and places.place ([data-place-view]) do:
        # every other panel turns hidden.
        def panel(a):
            return a.get("data-place-view") or ((a.get("id") or "").startswith("view-") and a["id"])
        for page in self.PAGES:
            tags = tree(page)
            views = [panel(a) for (_t, _c, _i, a), _u in tags if panel(a)]
            for (_tag, cls, id_, _a), up in tags:
                if id_ not in ("toolbar", "viewswitch") and "place-switch" not in cls:
                    continue
                inside = {panel(a) for _t, _c, _i, a in up if panel(a)}
                for shown in views:
                    with self.subTest(page=page.name, el=id_ or sorted(cls), view=shown):
                        self.assertEqual(inside - {shown}, set(), "selecting this view hides the switch")

    def test_the_shell_draws_the_view_switch_in_the_rail_alone(self):
        # pack sd:7084: the rail's sub-entries are the one view switch; no page gets a tab row.
        self.assertIn("rail.querySelector(`[data-section=\"${page}\"]`)?.closest('li')?.append(sub);", SHELL_JS)
        self.assertNotIn("role', 'tablist'", SHELL_JS)
        self.assertNotIn("toolbar.prepend(bar)", SHELL_JS)


class TheToolbarBoxesAlign(unittest.TestCase):
    """sd:5619: in row 1 the view switch and the Shapeshift box share one height and one top and bottom edge. The JXA harness
    has no layout engine, so these pin the CSS that makes them equal. Cause: .place-switch (places.css) kept a margin-bottom
    of --space-md, so the box that wraps Home's, HOA's and Tasks' switch stood 16px taller than the switch and, centred in
    the row, put the Shapeshift box 7px lower at its top and 9px lower at its bottom; the switch (42px) and the box (44px)
    also took their heights from different numbers."""

    PLACES_CSS = (STATIC / "places.css").read_text(encoding="utf-8")

    @staticmethod
    def rule(css, selector):
        m = re.search(r"(?m)^" + re.escape(selector) + r" \{([^}]*)\}", css)
        return m.group(1) if m else None

    def test_the_box_around_a_places_switch_has_no_margin_in_row_one(self):
        self.assertRegex(self.PLACES_CSS, r"\.place-switch \{ margin: 0 0 var\(--space-md\); \}")  # outside a toolbar it still spaces
        body = self.rule(SHELL_CSS, ".toolbar .place-switch, .toolbar .vseg, .toolbar > .fgroup")
        self.assertIsNotNone(body, "no row-1 rule zeroes the margin of the switch, the box around it and a chip group")
        self.assertIn("margin: 0;", body)

    def test_the_switch_and_the_shapeshift_box_take_one_height_token(self):
        row = self.rule(SHELL_CSS, ".toolbar")
        self.assertIsNotNone(row, "the row-1 height token is declared on .toolbar")
        self.assertIn("--bar-h: var(--row-touch);", row)
        self.assertIn("--bar-h: calc(var(--row-touch) + 2 * (var(--space-3xs) + var(--rule-hair)));",
                      re.search(r"@media \(pointer: coarse\) \{ \.toolbar \{[^}]*\} \}", SHELL_CSS).group(0))
        both = self.rule(SHELL_CSS, ".toolbar .vseg, .toolbar .shift .field")
        self.assertIsNotNone(both, "the pressed group and the box share one rule")
        self.assertIn("height: var(--bar-h);", both)

    def test_a_segment_fills_its_group_instead_of_setting_its_height(self):
        # The touch rule gave a tab 44px, which made the switch 50px beside a 44px box; in row 1 a segment stretches instead.
        body = self.rule(SHELL_CSS, ".toolbar .vseg > button")
        self.assertIsNotNone(body)
        self.assertIn("min-height: 0;", body)


class EveryToolbarHasOneOrder(unittest.TestCase):
    """sd:6693 (operator, 2026-10-29): "the filter should be to the left" of the page's actions, and every segment group the
    size of Tasks'. Every row-1 toolbar on every page holds, in order, its segments (the shell's view switch, a place switch
    or a .vseg), then the filter (the Shapeshift bar, then the column filter), then the page's actions in one .tools at the
    right. HOA's Alarms row put the column filter after Acknowledge all; Queue's Show chips, Contributions' scope radios,
    Metrics' range chips and HOA's Tasks segments each drew a segment group of another size."""

    PAGES = sorted(V2.glob("*.html"))
    SEGMENTS = {"viewswitch", "place-switch", "vseg"}

    @staticmethod
    def rows(page):
        """Each toolbar on `page` as its direct children's (classes, id), in order."""
        tags, out = tree(page), {}
        for (_t, cls, id_, _a), up in tags:
            if up and "toolbar" in up[-1][1]:
                out.setdefault(id(up[-1][3]), []).append((cls, id_))
        return list(out.values())

    def kind(self, cls, id_):
        if cls & self.SEGMENTS or id_ == "viewswitch":
            return 0
        if cls & {"shift", "tgrid", "chips"}:
            return 1
        if "tools" in cls:
            return 2
        # sd:5281: a page whose table has no title line puts its count at the right of row 1 (Tasks, sd:9309).
        return 3 if "count" in cls else None

    def test_segments_then_the_filter_then_the_actions_in_every_toolbar(self):
        for page in self.PAGES:
            for row in self.rows(page):
                # A help button explains the control before it, wherever that sits.
                kinds = [self.kind(c, i) for c, i in row if "help" not in c]
                with self.subTest(page=page.name, row=[sorted(c) or i for c, i in row]):
                    self.assertNotIn(None, kinds, "a toolbar child that is no segment group, filter or .tools")
                    self.assertEqual(kinds, sorted(kinds), "out of order: segments, filter, then actions")

    def test_the_shell_puts_its_filters_after_the_segments_and_before_the_actions(self):
        js = (STATIC / "shell.js").read_text(encoding="utf-8")
        self.assertIn("const segs = shiftRow.querySelectorAll(':scope > :is(#viewswitch, .place-switch, .vseg)');", js)
        self.assertIn("segEnd ? (segHelp || segEnd).after(el) : shiftRow.prepend(el);", js)
        self.assertIn("tools = row?.querySelector(':scope > .tools');", js)
        self.assertIn("tools ? tools.before(bar) : row ? row.append(bar) : twrap(t).before(bar);", js)
        self.assertIn(".toolbar > .tools { display: flex; flex-wrap: wrap; align-items: center; gap: var(--space-2xs); margin-left: auto; }", SHELL_CSS)

    def test_one_segment_size_from_one_rule(self):
        # Every .vseg button takes one rule (pack sd:7084 dropped the tab row that shared it), so every group is one size.
        self.assertEqual(len(re.findall(r"(?m)^\.vseg > button \{ display: inline-flex;", SHELL_CSS)), 1)
        for name, css in PAGE_CSS.items():
            with self.subTest(css=name):
                self.assertNotRegex(wide(css), r"\.(vseg|viewswitch|repo-scope)\b[^{]*\{", "a page sizes a segment group")


class EverySetAsideIsOneSegmentGroup(unittest.TestCase):
    """sd:6701 (operator, 2026-10-29): "I want one consistent way everywhere". Writing's Parked chip, the Show hidden toggles
    (Contributions, Designs, GitHub's issues, a place's issues), Documents' Hidden chip and Tasks' Parked only in Scope each
    set rows aside their own way. Every one is now shell.aside: a .vseg of Open | Parked or Hidden with its counts.
    Pack sd:7263 (operator 2026-11-02) dropped All; an older <key>=all link lands on Open."""

    PAGES = {"writing.js": "parked", "contributions.js": "hidden", "designs.js": "hidden", "issues.js": "hidden",
             "places.js": "issues-hidden", "documents.js": "hidden", "tasks.js": "parked"}

    def test_the_shell_reads_the_choice_from_the_url_and_counts_each_segment(self):
        found = re.search(r"\n  // aside:start\n(.*?)\n  // aside:end\n", SHELL_JS, re.S)
        self.assertIsNotNone(found, "shell.js has no aside block")
        out = run_js(found.group(1) + """
  R.modes = ['', '?hidden=1', '?hidden=all', '?hidden=x'].map(s => { location.search = s; return aside.mode('hidden'); });
  R.shows = [['open', true], ['open', false], ['aside', true], ['aside', false]].map(([m, a]) => aside.shows(m, a));
  R.params = ['open', 'aside'].map(aside.param);
  R.seg = aside.seg('parked', 'Parked', 'aside', { open: 4, aside: 2 }).text;""")
        self.assertEqual(out["modes"], ["open", "aside", "open", "open"])
        self.assertEqual(out["shows"], [False, True, True, False])
        self.assertEqual(out["params"], [None, "1"])
        self.assertEqual(re.findall(r'data-v="(\w+)" aria-pressed="(\w+)">(\w+) <span class="n">(\d+)</span>', out["seg"]),
                         [("open", "false", "Open", "4"), ("aside", "true", "Parked", "2")])
        self.assertTrue(out["seg"].startswith('<div class="vseg" role="group" aria-label="Parked rows">'))

    def test_no_page_keeps_a_set_aside_toggle_of_its_own(self):
        pages = [p.read_text(encoding="utf-8") for p in V2.glob("*.html")] + list(PAGE_JS.values())
        for text in pages:
            for toggle in ("Show hidden", "show-parked", "f-hidden", "Parked only", "Hide hidden"):
                self.assertNotIn(toggle, text)

    def test_every_page_that_sets_rows_aside_draws_the_shells_group(self):
        for name, key in self.PAGES.items():
            with self.subTest(page=name):
                self.assertRegex(PAGE_JS[name], r"shell\.aside\.(seg|buttons)\('" + re.escape(key) + "'")
                self.assertRegex(PAGE_JS[name], rf"shell\.aside\.mode\('{key}'[,)]")


class EveryPageHasOneReadAgain(unittest.TestCase):
    """sd:6697 (operator, 2026-10-29): one Read again and Refresh model, as Research has it. The shell's head button
    (window.PAGE_AGAIN) is the one re-read control on a page: no Observed cell re-reads and no page draws its own Read
    again. Every source refresh is a Refresh <what> command, so its label names what it refreshes."""

    PAGES = {**PAGE_JS, **{p.name: p.read_text(encoding="utf-8") for p in V2.glob("*.html")}}

    def test_no_page_draws_a_second_read_again_control(self):
        for name, text in self.PAGES.items():
            with self.subTest(page=name):
                self.assertNotIn('id="refresh"', text, "an Observed cell that reads the page again")
                self.assertNotRegex(text, r">\s*Read again\s*<", "a page's own Read again button")

    def test_the_r_key_presses_read_again(self):
        line = SHELL_JS.split("if (e.key === 'r')")[1].split("\n")[0]
        self.assertIn("document.getElementById('read-again')", line)
        self.assertIn("a.click()", line)

    def test_every_refresh_label_names_what_it_refreshes(self):
        bare = re.compile(r"""label: '(Refresh(?: all)?)'|>\s*(Refresh(?: all)?)\s*</(?:button|span|h2)""")
        for name, text in self.PAGES.items():
            with self.subTest(page=name):
                self.assertEqual([m.group(0) for m in bare.finditer(text)], [], "a Refresh that names no object")

    def test_watches_refresh_says_it_runs_the_market_watch_job(self):
        js = PAGE_JS["watches.js"]
        self.assertIn("label: 'Refresh watches'", js)
        self.assertRegex(js, r"label: 'Refresh watches'[^}]*does: '[^']*market-watch job")


class EveryDisclosureHasOneArrow(unittest.TestCase):
    """sd:5637: every <details> summary but a filter pick wears one ▸/▾ arrow from shell.css. Cause: Briefs' and Activity's
    "as a table" summaries were inline-flex, which drops the native marker, while Notes drew its own arrow."""

    def test_shell_css_draws_the_arrow_for_every_summary(self):
        self.assertIn("details:not(.fpick, .usage-drop) > summary::before { content: '▸';", SHELL_CSS)
        self.assertIn("details:not(.fpick, .usage-drop)[open] > summary::before { content: '▾'; }", SHELL_CSS)

    def test_no_page_draws_its_own_arrow(self):
        for css in STATIC.glob("*.css"):
            if css.name != "shell.css":
                self.assertNotIn("summary::before", css.read_text(encoding="utf-8"), css.name)


SHELL_JS = (STATIC / "shell.js").read_text(encoding="utf-8")
#: The rail's markup as shell.js draws it: its head row (the mark and its controls) and its foot.
RAIL = re.search(r"put\(rail, html`(.*?)`\);", SHELL_JS, re.S).group(1)
RAIL_HEAD = re.search(r'<div class="rail-head">(.*?)\n    </div>', RAIL, re.S)
RAIL_FOOT = re.search(r'<div class="foot">(.*?)</div>\s*$', RAIL, re.S).group(1)


class TheRailHead(unittest.TestCase):
    """sd:3442, sd:3443: the rail's fold and Reload are small icon buttons beside the title, not rows in the foot."""

    def test_the_fold_sits_in_the_head_beside_the_mark_and_not_in_the_foot(self):
        self.assertIsNotNone(RAIL_HEAD, "the rail has no head row")
        head = RAIL_HEAD.group(1)
        self.assertLess(head.index('class="mark"'), head.index('id="fold-rail"'))
        self.assertRegex(head, r'<button class="icon-btn" type="button" id="fold-rail"[^>]* aria-label="Collapse sections \(\[\)"')
        self.assertNotIn("fold-rail", RAIL_FOOT)

    def test_reload_sits_in_the_head_between_the_mark_and_the_fold(self):
        # sd:3443: Reload moves up beside the fold; the shell still binds it by id within the rail.
        self.assertIsNotNone(RAIL_HEAD, "the rail has no head row")
        head = RAIL_HEAD.group(1)
        self.assertLess(head.index('class="mark"'), head.index('id="reload"'))
        self.assertLess(head.index('id="reload"'), head.index('id="fold-rail"'))
        self.assertRegex(head, r'<button class="icon-btn" type="button" id="reload"[^>]* aria-label="Reload this page \(r\)"')
        self.assertNotIn('id="reload"', RAIL_FOOT)
        self.assertIn("rail.querySelector('#reload').addEventListener('click', () => location.reload());", SHELL_JS)


WIDTH = re.search(r"^  // width:start\n(.*?)^  // width:end$", SHELL_JS, re.S | re.M)


class TheRailWidth(unittest.TestCase):
    """sd:3445: the rail's width is set on its edge, clamped, kept per browser as sd.rail.width, and reset to the default."""

    def run_width(self, body, stored=None):
        self.assertIsNotNone(WIDTH, "shell.js has no width block")
        script = ("var OUT = {}, STORE = " + json.dumps({} if stored is None else {"sd.rail.width": stored}) + ", ATTR = {}, PROPS = {};\n"
                  "var EVENTS = []; function CustomEvent(type, o) { this.type = type; this.detail = o.detail; }\n"
                  "var document = { documentElement: { style: { setProperty: (k, v) => { PROPS[k] = v; }, removeProperty: k => { delete PROPS[k]; } } },\n"
                  "  dispatchEvent: e => EVENTS.push([e.type, e.detail]) };\n"
                  "const store = (k, v) => { if (v === undefined) return k in STORE ? STORE[k] : null; STORE[k] = v; return v; };\n"
                  "const rail = { getBoundingClientRect: () => ({ width: 216 }) }, grip = { setAttribute: (k, v) => { ATTR[k] = v; } };\n"
                  + WIDTH.group(1) + "\nvar R = {};\n" + body
                  + "\nfunction run() { return JSON.stringify({ R, STORE, ATTR, PROPS, EVENTS }); }\n")
        result = subprocess.run([OSASCRIPT, "-l", "JavaScript", "-e", script], capture_output=True, text=True, timeout=60, check=False)
        if result.returncode:
            raise AssertionError(result.stderr)
        return json.loads(result.stdout)

    def test_the_nav_never_scrolls_sideways_at_any_width(self):
        # sd:9311: at the default 216 px the nav's auto column grew to its widest entry (182 px in 168), so a sideways
        # scrollbar showed and cut the g-chord hints and the badges. Headless Chrome at 176, 216, 288 and 384 px: the
        # columns below held the width (scrollWidth == clientWidth) and the name gave way.
        css = re.sub(r"/\*.*?\*/", "", SHELL_CSS, flags=re.S)
        rule = lambda sel: " ".join(re.findall(r"(?:^|\})\s*" + re.escape(sel) + r"(?:\s*,[^{]*)?\s*\{([^}]*)\}", css))  # noqa: E731
        self.assertIn("grid-template-columns: minmax(0, 1fr)", rule(".rail nav, .rail nav ul"))
        name = rule(".rail nav a .nm")
        for want in ("min-width: 0", "overflow: hidden", "text-overflow: ellipsis"):
            self.assertIn(want, name)
        self.assertIn("flex: none", rule(".rail nav a :is(.i, .count, kbd, .n)"))

    def test_a_width_is_clamped_set_on_the_root_and_kept(self):
        out = self.run_width("R.set = setWidth(250.4); R.low = railWidth(20); R.high = railWidth('900'); R.junk = railWidth('wide');")
        self.assertEqual(out["R"], {"set": 250, "low": 176, "high": 384, "junk": None})
        self.assertEqual(out["PROPS"], {"--rail": "250px"})
        self.assertEqual(out["STORE"], {"sd.rail.width": "250"})
        self.assertEqual((out["ATTR"]["role"], out["ATTR"]["aria-valuenow"], out["ATTR"]["aria-valuemin"], out["ATTR"]["aria-valuemax"]),
                         ("separator", "250", "176", "384"))

    def test_a_drag_moves_without_storing_and_a_reset_clears_the_kept_width(self):
        out = self.run_width("setWidth(300, false); R.moved = Object.assign({}, PROPS); setWidth(null);", stored="240")
        self.assertEqual(out["R"]["moved"], {"--rail": "300px"})
        self.assertEqual(out["PROPS"], {})
        self.assertEqual(out["STORE"], {"sd.rail.width": ""})
        self.assertEqual(out["ATTR"]["aria-valuenow"], "216")

    def test_a_kept_width_tells_the_page_and_a_drag_in_progress_does_not(self):
        # Settings shows the width the edge set; without the event its control kept the old preset checked.
        out = self.run_width("setWidth(300, false); setWidth(250); setWidth(null);")
        self.assertEqual(out["EVENTS"], [["shell:rail-width", 250], ["shell:rail-width", None]])

    def test_the_shell_applies_the_kept_width_on_load_and_hands_pages_the_setter(self):
        self.assertIn("setWidth(store('sd.rail.width'), false);", SHELL_JS)
        self.assertIn("railWidth: (...a) => a.length ? setWidth(a[0]) : railWidth(store('sd.rail.width'))", SHELL_JS)

    def test_the_grip_sizes_come_from_tokens(self):
        # Foundation tokens.css covers space and rules, so the grip's own rules carry no raw px (the 2px focus ring aside: no token).
        rules = re.findall(r"^[^\n]*\.rail-grip[^{\n]*\{([^}]*)\}", SHELL_CSS, re.M)
        self.assertTrue(rules)
        self.assertEqual([r for r in rules if re.search(r"\d+px", r)], [])




PANE = re.search(r"^  // pane:start\n(.*?)^  // pane:end$", SHELL_JS, re.S | re.M)
IPAD = {"maxTouchPoints": 5, "platform": "MacIntel"}


class ThePaneFold(unittest.TestCase):
    """sd:5725: each page keeps its own pane fold, and an iPad starts folded whatever is kept."""

    def run_pane(self, body, stored=None, path="/v2/tasks", nav=None):
        self.assertIsNotNone(PANE, "shell.js has no pane block")
        script = ("var STORE = " + json.dumps(stored or {}) + ", ATTR = {};\n"
                  "var location = { pathname: " + json.dumps(path) + " }, navigator = "
                  + json.dumps(nav or {"maxTouchPoints": 0, "platform": "MacIntel"}) + ";\n"
                  "const body = { dataset: {} };\n"
                  "const store = (k, v) => { if (v === undefined) return k in STORE ? STORE[k] : null; STORE[k] = v; return v; };\n"
                  "const foldPane = { setAttribute: (k, v) => { ATTR[k] = v; }, querySelector: () => ({ setAttribute: () => {} }) };\n"
                  + PANE.group(1) + "\nvar R = {};\n" + body
                  + "\nfunction run() { return JSON.stringify({ R, STORE, ATTR, pane: body.dataset.pane || null }); }\n")
        result = subprocess.run([OSASCRIPT, "-l", "JavaScript", "-e", script], capture_output=True, text=True, timeout=60, check=False)
        if result.returncode:
            raise AssertionError(result.stderr)
        return json.loads(result.stdout)

    def test_a_fold_is_kept_for_its_page_alone(self):
        out = self.run_pane("setPane(true);")
        self.assertEqual(out["STORE"], {"sd.pane./v2/tasks": "min"})
        self.assertEqual(out["ATTR"]["aria-expanded"], False)
        # Reports kept its pane open; Tasks folding does not fold it.
        out = self.run_pane("R.kept = paneKept();", stored={"sd.pane./v2/tasks": "min", "sd.pane./v2/reports": ""}, path="/v2/reports")
        self.assertIs(out["R"]["kept"], False)

    def test_a_page_with_no_fold_yet_takes_the_old_browser_wide_one(self):
        out = self.run_pane("R.kept = paneKept();", stored={"sd.pane": "min"})
        self.assertIs(out["R"]["kept"], True)
        out = self.run_pane("R.kept = paneKept();", stored={"sd.pane": "min", "sd.pane./v2/tasks": ""})
        self.assertIs(out["R"]["kept"], False)

    def test_an_ipad_starts_folded_whatever_is_kept_and_a_click_still_toggles(self):
        out = self.run_pane("R.kept = paneKept(); setPane(false);", stored={"sd.pane./v2/tasks": ""}, nav=IPAD)
        self.assertIs(out["R"]["kept"], True)
        self.assertIsNone(out["pane"])
        # An older iPad names itself; a Mac without touch and an iPhone do not count.
        self.assertIs(self.run_pane("R.kept = paneKept();", nav={"maxTouchPoints": 5, "platform": "iPad"})["R"]["kept"], True)
        self.assertIs(self.run_pane("R.kept = paneKept();", nav={"maxTouchPoints": 0, "platform": "MacIntel"})["R"]["kept"], False)
        self.assertIs(self.run_pane("R.kept = paneKept();", nav={"maxTouchPoints": 5, "platform": "iPhone"})["R"]["kept"], False)

    def test_a_row_select_opens_the_pane_for_the_visit_and_keeps_the_pages_fold(self):
        """sd:5730: a select is a one-time open; the fold the operator chose for the page stays kept."""
        out = self.run_pane("setPane(false, false);", stored={"sd.pane./v2/notes": "min"}, path="/v2/notes")
        self.assertIsNone(out["pane"])
        self.assertEqual(out["STORE"], {"sd.pane./v2/notes": "min"})
        self.assertIn("if (body.dataset.pane) setPane(false, false);", SHELL_JS)

    def test_every_pages_row_click_opens_details_at_every_width(self):
        """sd:5730: Notes, Tasks and Writing opened Details only below 1240 px, so a folded pane stayed folded; Notes' row
        click did not open it at all. Each page with Details opens it on a row click, the shell's openPane unfolding it."""
        pages = {name: js for name, js in PAGE_JS.items() if "openPane('tab-details')" in js}
        self.assertTrue({"notes.js", "tasks.js", "writing.js", "reports.js"} <= set(pages))
        for name, js in pages.items():
            with self.subTest(name):
                self.assertNotRegex(js, r"innerWidth\s*<\s*1240\)\s*(window\.shell\.)?openPane")
        self.assertIn("if (r && !e.target.closest('button, a, time')) select(r.dataset.id, true);", PAGE_JS["notes.js"])

    def test_the_shell_applies_the_kept_fold_on_load_and_the_rail_keeps_one_key(self):
        self.assertIn("if (paneKept()) setPane(true);", SHELL_JS)
        self.assertIn("if (store('sd.rail') === 'min') setRail(true);", SHELL_JS)


WALK = re.search(r"^  // walk:start\n(.*?)^  // walk:end$", SHELL_JS, re.S | re.M)


class TheRowWalk(unittest.TestCase):
    """sd:5113: j / k walk only the rows of the shown view; a row in a panel the view switch hid is never selected."""

    def walk(self, body):
        self.assertIsNotNone(WALK, "shell.js has no walk block")
        # Every row has a box, as a stylesheet that shows a hidden panel would give it; only `hidden` tells them apart.
        script = ("var OUT = { picked: [] };\n"
                  "function panel(hidden) { return { hidden }; }\n"
                  "function row(id, p) { return { dataset: { id }, getClientRects: () => [1], scrollIntoView() {}, getAttribute: () => null,\n"
                  "  closest: sel => sel === '[hidden]' && p.hidden ? p : null }; }\n"
                  "var shown = panel(false), gone = panel(true);\n"
                  "var ROWS = [row('a1', gone), row('a2', gone), row('b1', shown), row('b2', shown)];\n"
                  "var SEL = null; window = { PAGE_LIST: { rows: () => ROWS, current: () => SEL, select: id => { SEL = id; OUT.picked.push(id); } } };\n"
                  + WALK.group(1) + "\nvar R = {};\n" + body
                  + "\nfunction run() { return JSON.stringify({ R, OUT }); }\n")
        result = subprocess.run([OSASCRIPT, "-l", "JavaScript", "-e", script], capture_output=True, text=True, timeout=60, check=False)
        if result.returncode:
            raise AssertionError(result.stderr)
        return json.loads(result.stdout)

    def test_j_and_k_walk_only_the_shown_views_rows(self):
        out = self.walk("step(1); step(1); step(1); step(-1); step(-1); step(-1);")
        self.assertEqual(out["OUT"]["picked"], ["b1", "b2", "b2", "b1", "b1", "b1"])

    def test_a_selection_left_in_a_hidden_view_moves_to_the_shown_views_first_row(self):
        out = self.walk("SEL = 'a2'; step(1);")
        self.assertEqual(out["OUT"]["picked"], ["b1"])

    def test_reconcile_reads_the_same_rule(self):
        self.assertIn("const all = [...rows], vis = all.filter(shownRow);", SHELL_JS)

# The view switch's stand-in: elements that keep their attributes, a rail with one current page, and an address whose
# pushState writes the hash. Events on window (hashchange, popstate) and document (DOMContentLoaded, keydown, shell:view)
# are recorded, so a test fires them as the browser would.
VIEW_STAND_IN = r"""
var window = globalThis, OUT = { events: [], pushed: [], error: null };
var WIN = {}, DOC = {};
function addEventListener(t, f) { (WIN[t] = WIN[t] || []).push(f); }
const fireWin = t => (WIN[t] || []).forEach(f => f({ type: t }));
function El(tag) { const e = { tagName: tag, id: '', attrs: {}, hidden: false, dataset: {}, children: [], listeners: {}, textContent: '', className: '', tabIndex: 0,
  setAttribute(k, v) { e.attrs[k] = String(v); if (k === 'id') e.id = String(v); }, getAttribute(k) { return k in e.attrs ? e.attrs[k] : null; },
  removeAttribute(k) { delete e.attrs[k]; }, append(...cs) { e.children.push(...cs); }, before(c) { OUT.before = [c.getAttribute('role'), e.id]; },
  addEventListener(t, f) { (e.listeners[t] = e.listeners[t] || []).push(f); }, fire(t, ev) { (e.listeners[t] || []).forEach(f => f(Object.assign({ type: t, target: e, preventDefault() {} }, ev))); },
  focus() { OUT.focused = e.id; }, closest() { return null; } };
  return e; }
var PANELS = {}, MADE = [], HOST = WITH_HOST ? El('div') : null;
['reports', 'cadence'].forEach(id => { const p = El('section'); p.id = 'view-' + id; PANELS[p.id] = p; });
var document = { readyState: 'loading', activeElement: null,
  getElementById: id => (id === 'viewswitch' ? HOST : PANELS[id]) || null, createElement: tag => { const e = El(tag); MADE.push(e); return e; },
  querySelector: () => null,
  addEventListener(t, f) { (DOC[t] = DOC[t] || []).push(f); },
  dispatchEvent(e) { if (e.type === 'shell:view') OUT.events.push(e.detail); (DOC[e.type] || []).forEach(f => f(e)); } };
function CustomEvent(type, o) { this.type = type; this.detail = o && o.detail; }
var location = { hash: HASH, pathname: '/reports', search: '?job=nightly' };
var history = { state: null, pushState(s, t, u) { OUT.pushed.push(u); location.hash = u.slice(u.indexOf('#')); } };
var STORE = KEPT_STORE;
var localStorage = STORE === null ? undefined : { getItem: k => k in STORE ? STORE[k] : null, setItem: (k, v) => { STORE[k] = String(v); } };
var LI = El('li'), LINK = El('a');
LINK.closest = sel => sel === 'li' ? LI : null;
var rail = { querySelector: sel => sel === '[data-section="Reports"]' ? LINK : null };
var page = 'Reports', hrefOf = name => name === 'Reports' ? '/reports' : undefined;
window.PAGE_KEYS = [['/', 'Filter reports']];
window.PAGE_VIEWS = [{ id: 'reports', label: 'Reports' }, { id: 'cadence', label: 'Cadence', api: '/api/reports/cadence' }, { id: 'gone', label: 'No panel' }];
"""


class TheViewSwitch(unittest.TestCase):
    """The view switch, cut out of shell.js: one declaration, window.PAGE_VIEWS, draws the rail's sub-entries, the one
    switch (pack sd:7084)."""

    def run_view(self, body, hash="", host=False, store=None, before=""):
        block = re.search(r"^  // view:start\n(.*?)^  // view:end$", SHELL_JS, re.S | re.M)
        self.assertIsNotNone(block, "shell.js has no view:start block")
        stand_in = (VIEW_STAND_IN.replace("HASH", json.dumps(hash)).replace("WITH_HOST", json.dumps(host))
                    .replace("KEPT_STORE", json.dumps({} if store is None else store or None)))
        # `before` runs ahead of the block: a page's own listeners, registered before shell.js's.
        script = (stand_in + before + "\n" + block.group(1) + r"""
var R = {};
const shown = () => Object.values(PANELS).filter(p => !p.hidden).map(p => p.id);
const RAIL = () => LI.children.length ? LI.children[0].children.map(li => li.children[0]) : [];
try {
(DOC.DOMContentLoaded || []).forEach(f => f({ type: 'DOMContentLoaded' }));
""" + body + """
} catch (e) { OUT.error = String(e) + ' ' + e.stack; }
function run() { OUT.R = R; OUT.STORE = STORE; return JSON.stringify(OUT); }
""")
        result = subprocess.run([OSASCRIPT, "-l", "JavaScript", "-e", script], capture_output=True, text=True, timeout=60, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        out = json.loads(result.stdout)
        self.assertIsNone(out["error"])
        return out

    def test_the_last_chosen_view_is_kept_per_page_and_the_address_wins(self):
        # sd:5064: a view the reader picked opens again when the address names none; #view= in the address wins.
        out = self.run_view("view.show('cadence'); R.cur = view.current();")
        self.assertEqual(out["STORE"], {"sd.view./reports": "cadence"})
        out = self.run_view("R.cur = view.current(); R.shown = shown();", store={"sd.view./reports": "cadence"})
        self.assertEqual((out["R"]["cur"], out["R"]["shown"]), ("cadence", ["view-cadence"]))
        self.assertEqual(out["events"], [{"id": "cadence", "first": True, "api": "/api/reports/cadence"}])
        self.assertEqual(out["pushed"], [])
        out = self.run_view("R.cur = view.current();", hash="#view=reports", store={"sd.view./reports": "cadence"})
        self.assertEqual(out["R"]["cur"], "reports")
        # Another page's key, a view the page no longer declares and a junk value all fall back to the first view.
        for kept in ({"sd.view./tasks": "cadence"}, {"sd.view./reports": "gone-for-good"}, {"sd.view./reports": "<b>"}):
            self.assertEqual(self.run_view("R.cur = view.current();", store=kept)["R"]["cur"], "reports", kept)

    def test_a_hidden_view_does_not_show_and_hiding_the_shown_one_shows_the_first_left(self):
        # sd:5064: Writing hides a realm view no piece is in: its rail entry goes, and v-keys and show() skip it.
        out = self.run_view("view.hide('reports', true); R.cur = view.current(); R.li = MADE.filter(e => e.tagName === 'li')[0].hidden;"
                            " R.back = view.show('reports'); R.cur2 = view.current(); view.hide('reports', false); R.again = view.show('reports');")
        r = out["R"]
        self.assertEqual((r["cur"], r["li"], r["back"], r["cur2"], r["again"]), ("cadence", True, False, "cadence", True))
        # A panel's own display (Today's grid) would beat the hidden attribute without this rule.
        self.assertIn('main [id^="view-"][hidden]', SHELL_CSS)

    def test_none_shows_no_view_until_the_next_show_even_of_the_last_one(self):
        # pack sd:7216: Tasks' ?realm=all lists every realm at once, so no rail entry is current. A page calls none() in
        # its DOMContentLoaded listener, before the shell's start, which then fires no view; the next show(), even of the
        # view shown before, shows it and fires shell:view.
        out = self.run_view("R.cur = view.current(); R.links = RAIL().map(a => a.getAttribute('aria-current'));"
                            " R.back = view.show('reports'); R.cur2 = view.current(); R.links2 = RAIL().map(a => a.getAttribute('aria-current'));",
                            host=True, before="document.addEventListener('DOMContentLoaded', () => view.none());")
        r = out["R"]
        self.assertEqual((r["cur"], r["links"]), (None, [None, None, None]))
        self.assertEqual((r["back"], r["cur2"], r["links2"]), (True, "reports", ["true", None, None]))
        self.assertEqual(out["events"], [{"id": "reports", "first": True, "api": None}])

    def test_storage_that_throws_is_no_view(self):
        # sd:5753: keep: false went with the last views that set it (Designs' and Contributions' Hidden); a stored id no page
        # declares any more is not restored (test_the_last_chosen_view_is_kept_per_page_and_the_address_wins covers it).
        # Storage off (a private window): no localStorage at all, and a show still works.
        out = self.run_view("view.show('cadence'); R.cur = view.current();", store=False)
        self.assertEqual(out["R"]["cur"], "cadence")

    def test_the_first_view_is_the_default_and_only_its_panel_renders(self):
        out = self.run_view("R.cur = view.current(); R.shown = shown(); R.links = RAIL().map(a => [a.getAttribute('href'), a.getAttribute('aria-current')]);"
                            " R.labels = Object.values(PANELS).map(p => [p.getAttribute('role'), p.getAttribute('aria-label')]);")
        self.assertEqual(out["R"]["cur"], "reports")
        self.assertEqual(out["R"]["shown"], ["view-reports"])
        # A view whose page has no panel is not drawn.
        self.assertEqual(out["R"]["links"], [["/reports#view=reports", "true"], ["/reports#view=cadence", None]])
        # A panel the page did not label takes its view's name; it is no tabpanel, as no tab names it.
        self.assertEqual(out["R"]["labels"], [[None, "Reports"], [None, "Cadence"]])
        # No tab row goes on the page, and the page hears the first view once, at DOMContentLoaded.
        self.assertNotIn("before", out)
        self.assertEqual(out["events"], [{"id": "reports", "first": True, "api": None}])
        self.assertEqual(out["pushed"], [])

    def test_the_hash_round_trips(self):
        out = self.run_view("""R.start = [view.current(), shown()];
view.show('reports'); R.shown = [location.hash, shown()];
location.hash = '#view=cadence'; fireWin('hashchange'); R.back = [view.current(), shown()];
location.hash = '#main'; fireWin('hashchange'); R.other = view.current();
location.hash = ''; fireWin('popstate'); R.none = view.current();
R.unknown = view.show('nope');""", hash="#view=cadence")
        self.assertEqual(out["R"]["start"], ["cadence", ["view-cadence"]])
        self.assertEqual(out["R"]["shown"], ["#view=reports", ["view-reports"]])
        self.assertEqual(out["R"]["back"], ["cadence", ["view-cadence"]])
        # Another hash (the skip link's #main) leaves the view alone; no hash at all is the first view.
        self.assertEqual((out["R"]["other"], out["R"]["none"], out["R"]["unknown"]), ("cadence", "reports", False))
        self.assertEqual(out["pushed"], ["#view=reports"])
        self.assertEqual(out["events"], [{"id": "cadence", "first": True, "api": "/api/reports/cadence"},
                                         {"id": "reports", "first": True, "api": None},
                                         {"id": "cadence", "first": False, "api": "/api/reports/cadence"},
                                         {"id": "reports", "first": False, "api": None}])

    def test_a_rail_entry_and_a_v_chord_switch_the_view(self):
        out = self.run_view("""RAIL()[1].fire('click'); R.click = view.current();
const key = k => (DOC.keydown || []).forEach(f => f({ type: 'keydown', key: k, target: { matches: () => false }, preventDefault() {}, stopImmediatePropagation() {} }));
key('v'); key('r'); R.chord = view.current(); R.keys = window.PAGE_KEYS;""")
        self.assertEqual(out["R"]["click"], "cadence")
        self.assertEqual(out["R"]["chord"], "reports")
        self.assertEqual(out["R"]["keys"], [["/", "Filter reports"], ["v r / v c", "Reports or Cadence view"]])

    def test_the_rail_lists_the_views_under_the_page_and_marks_the_current_one(self):
        out = self.run_view("""const links = () => LI.children[0].children.map(li => li.children[0]);
R.links = links().map(a => [a.getAttribute('href'), a.children[0].textContent, a.getAttribute('aria-current')]);
let stopped = false; links()[1].fire('click', { preventDefault() { stopped = true; } }); R.after = [view.current(), stopped, location.hash];
R.marked = links().map(a => a.getAttribute('aria-current'));""")
        self.assertEqual(out["R"]["links"], [["/reports#view=reports", "Reports", "true"], ["/reports#view=cadence", "Cadence", None]])
        # On the page a sub-entry switches in place, so the page keeps its filters (?job=); the href opens a new tab there.
        self.assertEqual(out["R"]["after"], ["cadence", True, "#view=cadence"])
        self.assertEqual(out["R"]["marked"], [None, "true"])

    def test_a_count_shows_on_its_rail_entry_and_null_hides_it(self):
        out = self.run_view("""view.count('cadence', 7); const n = RAIL()[1].children[1]; R.on = [n.textContent, n.hidden];
view.count('cadence', null); R.off = n.hidden; R.label = RAIL()[1].children[0].textContent;""")
        self.assertEqual(out["R"]["on"], ["7", False])
        self.assertEqual((out["R"]["off"], out["R"]["label"]), (True, "Cadence"))

    def test_a_page_with_a_marker_keeps_views_without_a_panel_and_gets_no_tab_row(self):
        out = self.run_view("""R.hostEmpty = HOST.children.length === 0;
R.links = RAIL().map(a => a.getAttribute('href'));
view.show('gone'); R.gone = [view.current(), shown(), location.hash];
view.show('cadence'); R.back = shown();""", host=True)
        # Tasks filters one board, matrix and list: its views have no panels, so the page's #viewswitch marks that it keeps
        # them; the rail lists them, and the marker stays empty (pack sd:7084).
        self.assertTrue(out["R"]["hostEmpty"], "a tab row went in the page's #viewswitch")
        self.assertNotIn("before", out)
        self.assertEqual(out["R"]["links"], ["/reports#view=reports", "/reports#view=cadence", "/reports#view=gone"])
        self.assertEqual(out["R"]["gone"], ["gone", [], "#view=gone"])
        self.assertEqual(out["R"]["back"], ["view-cadence"])
        self.assertEqual([e["id"] for e in out["events"]], ["reports", "gone", "cadence"])

    def test_the_palette_lists_the_page_views_and_the_saved_view_chips_are_gone(self):
        # sd:5064: "views" are the page's panels only; the saved-view chips (shell.views) and their CSS are gone.
        self.assertNotRegex(SHELL_JS, r"\bfunction views\(|\bviewHref\b|\bVIEWS\b")
        self.assertNotRegex(SHELL_JS, r"window\.shell = \{[^\n]*\bviews\b")
        self.assertNotIn("Saved views", SHELL_CSS)
        self.assertIn("data-pview", SHELL_JS)
        self.assertIn("viewShow(o.dataset.pview)", SHELL_JS)

    def test_the_shell_hands_pages_the_view_api_and_styles_the_rail_entries(self):
        self.assertRegex(SHELL_JS, r"window\.shell = \{[^\n]*\bview\b")
        self.assertIn('.rail nav .subnav a[aria-current="true"]', SHELL_CSS)
        self.assertIn(".rail nav .subnav .n { margin-left: auto;", SHELL_CSS)



SHIFT = re.search(r"^  // shift:start\n(.*?)^  // shift:end$", SHELL_JS, re.S | re.M)


class TheShapeshiftBar(unittest.TestCase):
    """sd:5539: one Filter, capture or ask bar, in shell.js; the pages that drew a copy only mark row 1 for it."""

    def run_shift(self, body):
        if SHIFT is None:
            raise AssertionError("shell.js has no shift:start block")
        return run_js(SHIFT.group(1) + body)

    def test_the_bar_guesses_the_reading_and_the_arrow_cycles_filter_task_ask(self):
        out = self.run_shift("""R.guess = ['port 8767', 'why is it red?', 'what failed', 'p1 fix the gate', 'add a test', 'renew it due friday', ''].map(shiftGuess);
  R.next = ['filter', 'task', 'ask'].map(shiftNext);""")
        self.assertEqual(out["guess"], ["filter", "ask", "ask", "task", "task", "task", "filter"])
        self.assertEqual(out["next"], ["task", "ask", "filter"])

    def test_the_ask_line_names_the_page_and_the_filter_line_what_it_filters(self):
        out = self.run_shift("""R.ask = shiftAs('ask', 'why?', 'Reports', 'reports');
  R.task = shiftAs('task', 'p1 fix', 'Reports', 'reports');
  R.filter = shiftAs('filter', 'x', 'Reports', 'reports, bodies included'); R.none = shiftAs('filter', 'x', 'Metrics', '');
  R.what = [[], ['Timeline'], ['Repos', 'Lane'], ['CI runs', 'Mail', 'Briefs'], ['A', 'B', 'C', 'D'], ['Now', 'Now', '']].map(shiftWhat);""")
        self.assertEqual(out["ask"], ["message-square", "ask chat about reports"])
        self.assertEqual(out["task"], ["list-todo", "task “p1 fix”"])
        self.assertEqual(out["filter"], ["filter", "filter reports, bodies included"])
        self.assertEqual(out["none"], ["filter", "filter this view"])
        self.assertEqual(out["what"], ["", "timeline", "repos and lane", "CI runs, mail and briefs", "4 tables", "now"])

    def test_no_table_draws_a_filter_rows_box(self):
        # The operator's ruling: no table-level text box anywhere; Filter by column, the folds and the count stay.
        self.assertNotIn("tfind", SHELL_JS)
        self.assertNotIn("tfind", SHELL_CSS)
        self.assertNotIn("Filter rows", SHELL_JS)
        self.assertIn('<label class="tfacet"><span class="sr">Filter by a column value</span>', SHELL_JS)
        self.assertIn('data-tfold-all="open">Expand all</button>', SHELL_JS)

    def test_no_page_carries_a_copy_of_the_bar(self):
        # Reports, Activity, Briefs and Today drew the bar in their markup and wired it in their script; the shell draws it
        # now on every page that marks row 1 (data-shift). The pages whose own bar has readings of its own keep theirs.
        marked = [p for p in sorted(V2.glob("*.html")) if "data-shift" in p.read_text(encoding="utf-8")]
        for name in ("reports", "activity", "briefs", "today", "health", "contributions", "designs", "management", "decide",
                     "github"):
            self.assertIn(V2 / f"{name}.html", marked)
        for page in marked:
            text = page.read_text(encoding="utf-8")
            files = [(page.name, text)] + [(f"{n}.js", (STATIC / f"{n}.js").read_text(encoding="utf-8"))
                                           for n in re.findall(r'<script src="/ui/([\w-]+)\.js"', text)
                                           if n not in SHELL_SCRIPTS and (STATIC / f"{n}.js").exists()]
            for name, body in files:
                with self.subTest(page=page.name, file=name):
                    self.assertNotIn('class="shift"', body)
                    self.assertNotIn("shift-as", body)
                    self.assertNotIn("shell.capture(null, v)", body)

    def test_the_shell_wires_the_bar_once(self):
        # Enter: a task opens Capture with the text, an ask goes to chat; Esc clears; / focuses; each view keeps its text.
        self.assertIn("if (shiftMode === 'task') { shiftSet(''); openCapture(null, v); }", SHELL_JS)
        self.assertIn("if (shiftMode === 'ask') { shiftSet(''); openChat(); send(v); }", SHELL_JS)
        self.assertIn("document.addEventListener('shell:view', e => { if (e.detail.id !== shiftOn) shiftSet(SQ[e.detail.id] || ''); });", SHELL_JS)
        self.assertIn("document.dispatchEvent(new CustomEvent('shell:filter', { detail: { q, view: shiftView() } }));", SHELL_JS)
        self.assertIn("if (window.PAGE_LIST?.clear?.() || shiftClear()) e.preventDefault();", SHELL_JS)
        self.assertIn("window.PAGE_KEYS = [['/', 'Filter, capture a task or ask']", SHELL_JS)
        self.assertRegex(SHELL_JS, r"window\.shell = \{[^\n]*\bfilter\b")

    def test_shell_filter_is_the_bars_and_not_the_palettes(self):
        # 697344e6 declared a second function filter (the palette's) in the same scope; the later one won, so shell.filter
        # ran the palette filter: a page's Clear all left the bar's text, and Skills and Tasks could not hand theirs over.
        self.assertEqual(len(re.findall(r"\bfunction filter\(", SHELL_JS)), 1)
        self.assertIn("function filter(...a) {", SHELL_JS)
        self.assertIn("palIn.addEventListener('input', palFilter);", SHELL_JS)

class TheSelect(unittest.TestCase):
    """sd:5064: one native select style in shell.css; a page sets only size and width, so every select reads the same."""

    def test_the_shell_styles_every_select_from_tokens(self):
        rule = re.search(r"^:where\(select:not\(\.tile-f\)\) \{([^}]*)\}", SHELL_CSS, re.M)
        self.assertIsNotNone(rule, "shell.css has no shared select rule")
        body = rule.group(1)
        for want in ("appearance: none", "var(--color-panel)", "var(--color-ink)", "var(--color-rule)",
                     "var(--color-muted) 50%", "color-scheme: inherit"):
            self.assertIn(want, body)
        self.assertRegex(SHELL_CSS, r":where\(select:not\(\.tile-f\):focus-visible\) \{ outline: 2px solid var\(--color-focus\)")
        self.assertRegex(SHELL_CSS, r":where\(select:not\(\.tile-f\):hover:not\(:disabled\)\) \{ border-color: var\(--color-ink-2\)")
        self.assertRegex(SHELL_CSS, r":where\(select:not\(\.tile-f\):disabled\) \{[^}]*var\(--color-well\)")
        self.assertRegex(SHELL_CSS, r"@media \(forced-colors: active\) \{ :where\(select:not\(\.tile-f\)\) \{ appearance: auto")
        # The option popup follows the theme: tokens.css sets color-scheme on :root.
        self.assertIn("color-scheme", (STATIC / "tokens.css").read_text(encoding="utf-8"))

    def test_a_filter_bar_select_has_the_chips_shape_and_a_form_select_the_inputs(self):
        # The operator's ruling: a filter-bar select sits with chips, so it takes their height and pill radius; a form
        # select (Details, Settings, Management, dialogs) keeps the text inputs' radius, and its page sets their height.
        chip = re.search(r"^\.chip \{([^}]*)\}", SHELL_CSS, re.M).group(1)
        select = re.search(r"^:where\(select:not\(\.tile-f\)\) \{([^}]*)\}", SHELL_CSS, re.M).group(1)
        bar = re.search(r"^:where\(\.fsel, \.filters select, \.tgrid select, \.toolbar select\) \{([^}]*)\}", SHELL_CSS, re.M)
        self.assertIsNotNone(bar, "shell.css has no filter-bar select rule")
        for prop in ("min-height", "border-radius"):
            want = re.search(rf"\b{prop}: ([^;]+);", chip).group(1)
            self.assertEqual(re.search(rf"\b{prop}: ([^;]+);", bar.group(1)).group(1), want, prop)
        self.assertIn("border-radius: var(--radius-md);", select)
        for name in ("settings.css", "management.css", "task-details.css"):
            css = (STATIC / name).read_text(encoding="utf-8")
            field = re.search(r"select\.fld \{ min-height: ([^;]+);", css).group(1)
            self.assertEqual(re.search(r"\.fld:not\(select\) \{ min-height: ([^;]+);", css).group(1), field, name)
        # A filter bar sizes no select of its own (Reports' .filters select was row-touch tall); a coarse pointer's
        # row-touch comes from the shared rule.
        reports = (STATIC / "reports.css").read_text(encoding="utf-8")
        self.assertNotRegex(reports, r"(?m)^\.filters select \{[^}]*min-height")

    FILTERS = ".fsel, .filters select, .tgrid select, .toolbar select"
    EVERY = 'select:not(.tile-f, [multiple], [size]:not([size="1"]))'

    def test_every_filter_dropdown_is_one_shared_pill_enumerated_from_the_code(self):
        """sd:5281: every filter dropdown on every page wears the chip's pill: shell.list.pick's single select (.fsel, on
        Tasks, Briefs and Documents' facet, which sits outside a .filters bar) and its multi menu (.fpick), the column
        filter (.tgrid), a page's .filters selects (Reports) and the toolbar row's."""
        self.assertRegex(SHELL_CSS, r"(?m)" + r"^:where\(\.fsel, \.filters select, \.tgrid select, \.toolbar select\) \{([^}]*)\}")
        self.assertIn('<select id="f-${key}" class="fsel" data-pick="${key}">', SHELL_JS)
        self.assertIn('put(bar, html`<label class="tfacet">', SHELL_JS)
        summary = re.search(r"^\.fpick > summary, [^{]*\{([^}]*)\}", SHELL_CSS, re.M).group(1)
        self.assertIn("border-radius: 999px", summary)

    def test_a_filter_narrowed_to_a_value_wears_the_accent_pill(self):
        # The picked .fpick and an active column filter wear the accent; a single select with a value does too.
        self.assertIn(":where(.fsel, .filters select, .toolbar select):where(:has(option:checked:not([value=\"\"]))) "
                      "{ border-color: var(--color-accent); background-color: var(--color-accent-bg); }", SHELL_CSS)

    def test_the_open_list_follows_the_theme_where_the_browser_allows(self):
        # Customizable select (appearance: base-select) draws the open list as the panel's box; elsewhere the native
        # popup follows :root's color-scheme. The OS chevron gives way to the shared one.
        block = re.search(r"@supports \(appearance: base-select\) \{(.*?)\n\}", SHELL_CSS, re.S)
        self.assertIsNotNone(block, "shell.css does not theme the open list")
        body = block.group(1)
        # Decision #24588: every select's open list, not only a filter's; a list box (multiple, size) draws no popup.
        self.assertIn(f":where({self.EVERY})::picker(select)", body)
        self.assertIn(f":where({self.EVERY}) option:checked", body)
        self.assertIn("::picker-icon { display: none; }", body)
        for token in ("var(--color-panel)", "var(--color-rule)", "var(--shadow-overlay)", "var(--color-accent-bg)"):
            self.assertIn(token, body)

    def test_a_page_listbox_button_wears_the_filter_pill(self):
        # Decision #24588: a custom combobox (Activity's Repo button) reads as the shared dropdown, not its own box.
        self.assertRegex(SHELL_CSS, r'(?m)^\.fpick > summary, button\[aria-haspopup="listbox"\] \{')
        self.assertIn('.fpick:has(:checked) > summary, button[aria-haspopup="listbox"]:has(> .n) {', SHELL_CSS)
        buttons = set()
        for path in sorted(STATIC.glob("*.js")):
            for cls in re.findall(r'<button[^>]*class="([\w-]+)"[^>]*aria-haspopup="listbox"', path.read_text(encoding="utf-8")):
                buttons.add(cls)
        self.assertEqual(buttons, {"pick"})
        for name, css in PAGE_CSS.items():
            for cls in buttons:
                with self.subTest(page=name, cls=cls):
                    self.assertNotRegex(css, rf"(?m)^\.{cls}(?::[^\s{{,]+)?\s*\{{[^}}]*\b(?:border|background|border-radius):")

    def test_no_page_redraws_the_select_box(self):
        # Enumerated from every stylesheet: a rule that names a select may size it, never redraw its box, chevron, ink or
        # face (decision #24588). The card tiles' borderless pickers (.tile-f) are the one exception, outside the shared rule.
        redraw = re.compile(r"(?:^|;)\s*(?:background|border|appearance|border-radius|padding|outline|font|font-family|color|box-shadow)\s*:")
        found = []
        for path in sorted(STATIC.glob("*.css")):
            css = re.sub(r"/\*.*?\*/", "", path.read_text(encoding="utf-8"), flags=re.S)
            for selector, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css):
                if selector.strip().startswith(":where("):
                    continue  # the shell's shared rules, which may list several selects in one :where()
                names = [part.strip() for part in selector.split(",") if re.search(r"\bselect\b(?![-\w])", part)]
                names = [n for n in names if not n.startswith(":where(") and "tile-f" not in n and ":not(select)" not in n]
                if names and redraw.search(body):
                    found.append(f"{path.name}: {', '.join(names)}")
        self.assertEqual(found, [])


class TheOneViewModel(unittest.TestCase):
    """sd:5080: a page with several areas shows them through the shell's view switch (shell.js, view:start) and no other.

    The shell alone draws tabs: no page's markup or script carries a tab role or the old Management sub-view strip. Every
    view-<id> panel is a view its page declares, in PAGE_VIEWS or as a places.js data-place-view block, and every view a
    page declares by name has its panel, unless the page draws the switch into #viewswitch (a view that filters one list).
    """

    PAGES = {p.stem: p.read_text(encoding="utf-8") for p in V2.glob("*.html")}
    # Tasks' List, Board and Matrix and Writing's Board and Editor show one list three or two ways: a layout, not an area.
    # Their ids predate the switch; each page keeps a realm of the same id out of PAGE_VIEWS (writing.js, tasks.js).
    LAYOUT = {"tasks": {"btn", "menu", "board", "matrix", "list"}, "writing": {"btn", "menu", "board", "editor"}}
    TAB = re.compile(r"""role=["']?tab(list|panel)?\b|'role', 'tab|class="(subviews|sv)\b|\bsv-[a-z]""")

    def declared(self, stem):
        js = (STATIC / f"{stem}.js").read_text(encoding="utf-8") if (STATIC / f"{stem}.js").exists() else ""
        block = re.search(r"PAGE_VIEWS = \[(.*?)\];", js, re.S)
        # A view that names another view's panel (panel: 'reports', sd:5461) has no panel of its own.
        own = [m.group(1) for m in re.finditer(r"\{ id: '([a-z0-9-]+)'[^}]*\}", block.group(1)) if "panel:" not in m.group(0)] if block else []
        return set(own) | \
            set(re.findall(r'data-place-view="([a-z0-9-]+)"', self.PAGES[stem]))

    def test_every_rail_page_is_read(self):
        from sd_dashboard import v2
        self.assertEqual(len(self.PAGES), len(v2.SECTIONS), "a page joined the rail without this test reading it")

    def test_no_page_draws_tabs_outside_the_shells_view_switch(self):
        found = [(name, m.group(0)) for name, text in [*((f"{k}.html", v) for k, v in self.PAGES.items()), *PAGE_JS.items()]
                 for m in self.TAB.finditer(text)]
        found += [(name, m.group(0)) for name, text in PAGE_CSS.items() for m in re.finditer(r"\.(subviews|sv)\b", text)]
        self.assertEqual(found, [])

    def test_every_view_panel_is_a_declared_view_and_every_declared_view_has_its_panel(self):
        stray, missing = [], []
        for stem, page in sorted(self.PAGES.items()):
            views, panels = self.declared(stem), set(re.findall(r'id="view-([a-z0-9-]+)"', page)) - self.LAYOUT.get(stem, set())
            stray += [f"{stem}: view-{p}" for p in sorted(panels - views)]
            if 'id="viewswitch"' not in page:
                missing += [f"{stem}: {v}" for v in sorted(views - panels)]
        self.assertEqual((stray, missing), ([], []))


class TheFilterBar(unittest.TestCase):
    """sd:5463: Activity's filter row, drawn by the shell once: segments, picks, tokens, then Clear all at the right."""

    def test_a_segment_group_names_its_filter_and_marks_the_pressed_value(self):
        out = run_js("""R.seg = String(list.fseg('state', 'State', [{ v: 'warning', label: html`<span class="g-warning">■</span><span class="n">2</span>`, pressed: true, title: 'failed: 2' },
  { v: 'ok', label: 'ok', off: true }]));""")
        self.assertEqual(out["seg"], '<div class="fseg" role="group" aria-label="State">'
                         '<button type="button" data-f="state" data-v="warning" aria-pressed="true" title="failed: 2"><span class="g-warning">■</span><span class="n">2</span></button>'
                         '<button type="button" data-f="state" data-v="ok" aria-pressed="false" aria-disabled="true">ok</button></div>')

    def test_the_bar_ends_with_clear_all_only_while_a_filter_is_on(self):
        out = run_js("""R.on = String(list.fbar([list.tok('repo', 'system', 'system')], { clear: true }));
  R.off = String(list.fbar([], { clear: false }));""")
        self.assertEqual(out["on"], '<div class="fbar" role="group" aria-label="Filters">'
                         '<button type="button" class="tok" data-f="repo" data-v="system" aria-label="Remove filter: system"><span>system</span><span class="x" aria-hidden="true">×</span></button>'
                         '<div class="fsum"><button class="linkbtn" type="button" data-fclear>Clear all</button></div></div>')
        self.assertEqual(out["off"], '<div class="fbar" role="group" aria-label="Filters"><div class="fsum"></div></div>')

    def test_the_count_names_every_row_or_how_many_the_filters_leave(self):
        out = run_js("R.c = [list.count(3, 3, 'report'), list.count(1, 1, 'report'), list.count(2, 12, 'report')];")
        self.assertEqual(out["c"], ["3 reports", "1 report", "2 of 12 reports"])


class TheWholeShell(unittest.TestCase):
    """sd:5463: the suites run cut blocks of shell.js, so a name one block declares twice in the file's scope passed them
    while the browser refused the whole shell (the filter bar's tok beside the badge's tok). The file must compile whole."""

    def test_shell_js_compiles_as_one_script(self):
        script = f"(() => {{ try {{ new Function({json.dumps(SHELL_JS)}); return 'ok'; }} catch (e) {{ return String(e); }} }})()"
        result = subprocess.run([OSASCRIPT, "-l", "JavaScript", "-e", script], capture_output=True, text=True, timeout=60, check=False)
        self.assertEqual(result.stdout.strip(), "ok", result.stderr)


class TheStateSlot(unittest.TestCase):
    """Pack sd:7536: the state banner says what holds in plain words; the route or file it read is not shown text."""

    STATE = re.search(r"  const STATES = .*?\n  function state\(s, at\) \{.*?\n  \}\n", SHELL_JS, re.S).group(0)
    STATE = re.sub(r"  let stateSlot = .*?\n  if \(!stateSlot.*?\n", "", STATE)

    def draw(self, spec):
        script = (STAND_IN + MARKUP_JS + "\nconst { html, put: realPut } = window.markup;\n"
                  + "const SLOT = { className: '', hidden: true, dataset: {}, attrs: {}, setAttribute(k, v) { this.attrs[k] = v; }, replaceChildren() {} };\n"
                  + "let stateSlot = SLOT; const put = (el, m) => { el.html = String(m); };\n" + self.STATE
                  + f"\nvar R = {{}};\ntry {{ state({spec}); R.html = SLOT.html; R.source = SLOT.dataset.source; }}"
                  + " catch (e) { OUT.error = String(e) + ' ' + e.stack; }\n"
                  + "function run() { OUT.R = R; return JSON.stringify(OUT); }\n")
        result = subprocess.run([OSASCRIPT, "-l", "JavaScript", "-e", script], capture_output=True, text=True, timeout=60, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        out = json.loads(result.stdout)
        self.assertIsNone(out["error"])
        return out["R"]

    def test_the_banner_shows_the_text_and_keeps_the_route_off_the_page(self):
        got = self.draw("{ kind: 'error', text: 'The rows were not read: boom.', source: '/api/rows' }")
        self.assertIn("The rows were not read: boom.", got["html"])
        self.assertNotIn("/api/rows", got["html"])
        # The route stays on the slot's data for a reader with dev tools.
        self.assertEqual(got["source"], "/api/rows")

    def test_the_review_hooks_say_plain_words(self):
        hooks = re.search(r"const hookState = \{.*?\}\[location\.hash\];", SHELL_JS, re.S).group(0)
        self.assertNotIn("collector", hooks)


class TheRowActions(unittest.TestCase):
    """sd:5457: a row's act is its icon when the command declares one, a text button when it does not, then ⋯."""

    ROW_ACTIONS = re.search(r"  function rowActions\(id, \{ icons = false \} = \{\}\) \{.*?\n  \}\n", SHELL_JS, re.S).group(0)

    def row(self, command, call="rowActions('r1')", extra=""):
        script = (STAND_IN + MARKUP_JS + "\nconst { html, plural } = window.markup;\n"
                  + "const ICON = n => html`<svg class=\"i\" aria-hidden=\"true\"><use href=\"#i-${n}\"/></svg>`;\n"
                  + SETTLE_JS + "\nconst OBJ = new Map([['r1', { id: 'r1', type: 'row', label: 'notes.html' }]]);\n"
                  + f"const CMD = {command};\nconst live = () => [CMD{extra}];\n" + self.ROW_ACTIONS
                  + f"\nvar R = {{}};\n(async () => {{ try {{\nR.html = String({call});\n}} catch (e) {{ OUT.error = String(e) + ' ' + e.stack; }} }})();\n"
                  + "function run() { OUT.R = R; return JSON.stringify(OUT); }\n")
        result = subprocess.run([OSASCRIPT, "-l", "JavaScript", "-e", script], capture_output=True, text=True, timeout=60, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        out = json.loads(result.stdout)
        self.assertIsNone(out["error"])
        return out["R"]["html"]

    def test_a_command_with_an_icon_is_an_icon_button_named_by_its_label(self):
        html = self.row("{ id: 'x.hide', label: 'Hide', does: 'Hides this row.', key: 'h', icon: 'eye-off', risk: 'undo', undo() {},"
                        " primary: () => true, cli: () => 'no CLI: view state' }")
        self.assertIn('<button class="icon-btn" type="button" data-cmd="x.hide" data-obj="r1" aria-label="Hide" aria-keyshortcuts=". h"'
                      ' data-tip="Hide (. h): Hides this row. Undo offered.">'
                      '<svg class="i" aria-hidden="true"><use href="#i-eye-off"/></svg></button>', html)
        self.assertNotIn(">Hide<", html)
        self.assertIn('data-menu-for="r1"', html)

    def test_a_command_without_an_icon_stays_a_text_button(self):
        html = self.row("{ id: 'x.run', label: 'Run', does: 'Runs it.', risk: 'safe', primary: () => true, cli: () => 'sd run' }")
        self.assertIn('<button class="btn quiet sm" type="button" data-cmd="x.run" data-obj="r1" data-tip="Runs it.">Run</button>', html)

    def test_icons_shows_every_live_command_with_an_icon_and_keeps_text_ones_in_the_menu(self):
        # sd:5747: a row that offers what its Details offer; a text-only command stays in the ⋯ menu.
        html = self.row("{ id: 'x.deck', label: 'Build deck', does: 'Builds it.', icon: 'file-text', risk: 'safe', cli: () => 'sd deck' }",
                        call="rowActions('r1', { icons: true })",
                        extra=", { id: 'x.slides', label: 'Import', does: 'Imports it.', icon: 'send', risk: 'safe', cli: () => 'no CLI: here' },"
                              " { id: 'x.apply', label: 'Apply', does: 'Applies it.', risk: 'safe', cli: () => 'sd apply' }")
        self.assertEqual(re.findall(r'data-cmd="([^"]+)"', html), ["x.deck", "x.slides"])
        self.assertIn('data-menu-for="r1"', html)

    def test_icons_as_a_list_shows_only_those_and_an_empty_list_leaves_the_menu_alone(self):
        # pack sd:7700: a Studio project row keeps its commands in ⋯; a primary one is not drawn on the row.
        extra = (", { id: 'x.pin', label: 'Pin', does: 'Pins it.', icon: 'pin', risk: 'undo', undo() {}, cli: () => 'no CLI: view' },"
                 " { id: 'x.stage', label: 'Next stage', does: 'Moves it.', icon: 'chevron-right', primary: () => true,"
                 " risk: 'confirm', cli: () => 'no CLI: here' }")
        deck = "{ id: 'x.deck', label: 'Build deck', does: 'Builds it.', icon: 'file-text', risk: 'safe', cli: () => 'sd deck' }"
        html = self.row(deck, call="rowActions('r1', { icons: ['x.pin'] })", extra=extra)
        self.assertEqual(re.findall(r'data-cmd="([^"]+)"', html), ["x.pin"])
        html = self.row(deck, call="rowActions('r1', { icons: [] })", extra=extra)
        self.assertEqual(re.findall(r'data-cmd="([^"]+)"', html), [])
        self.assertIn('data-menu-for="r1"', html)


if __name__ == "__main__":
    unittest.main()


class ThePaneWithoutDetails(unittest.TestCase):
    """sd:5592. A page whose Details never shows a row has no Details element; the shell hides the tab and opens on Chat."""

    def test_only_decide_and_settings_have_no_details_and_the_shell_shows_chat(self):
        v2 = V2
        bare = sorted(p.stem for p in v2.glob("*.html") if 'id="details"' not in p.read_text(encoding="utf-8"))
        # Queue's open lane shows in Details since sd:5921.
        self.assertEqual(bare, ["decide", "settings"])
        self.assertIn("if (!details) { pane.querySelector('#tab-details').hidden = true; showTab('tab-chat'); }", SHELL_JS)
        self.assertIn('.pane [role="tab"][hidden] { display: none; }', (v2 / "static" / "shell.css").read_text(encoding="utf-8"))


AGAIN = re.search(r"^  // again:start\n(.*?)^  // again:end$", SHELL_JS, re.S | re.M)
USAGE = re.search(r"^  // usage:start\n(.*?)^  // usage:end$", SHELL_JS, re.S | re.M)
STALENESS = re.search(r"^  function staleness\(\) \{\n.*?^  \}$", SHELL_JS, re.S | re.M)


class TheReadAgainButton(unittest.TestCase):
    """sd:5638: the shell draws one Read again button for a page's window.PAGE_AGAIN, the same on every page; pack
    sd:9569: beside it, on every page, the Usage dropdown."""

    def run_again(self, body, slot=False):
        self.assertIsNotNone(AGAIN, "shell.js has no again:start block")
        self.assertIsNotNone(USAGE, "shell.js has no usage:start block, so no page has the Usage dropdown")
        script = "var window = globalThis;\n" + MARKUP_JS + r"""
const { html, put } = window.markup;
const ICON = n => html`<svg class="i" aria-hidden="true"><use href="#i-${n}"/></svg>`;
var OUT = { toasts: [], gets: [], error: null }, R = {}, WIN = [], ELS = {}, location = { hash: "" }, innerHeight = 900;
var setInterval = () => 0;
const toast = m => OUT.toasts.push(m), flush = async () => { for (let i = 0; i < 50; i++) await null; };
var ANSWER = { error: '', rows: [], nosource: [] };
const getJSON = async path => { OUT.gets.push(path); if (typeof ANSWER === 'string') throw new Error(ANSWER); return typeof ANSWER === 'function' ? ANSWER() : ANSWER; };
function addEventListener(t, f) { WIN.push([t, f]); }
function El(id) { const e = { id, html: '', attrs: {}, dataset: {}, listeners: {}, style: {}, getBoundingClientRect: () => ({ top: 60, bottom: 52 }), setAttribute(k, v) { e.attrs[k] = v; }, removeAttribute(k) { delete e.attrs[k]; },
  addEventListener(t, f) { (e.listeners[t] = e.listeners[t] || []).push(f); }, append(f) { e.html += f.parsed; }, replaceChildren(f) { e.html = f ? f.parsed : ''; } }; return e; }
var HEAD = El('head'), SLOT = """ + ("El('slot')" if slot else "null") + r""";
const main = { querySelector: s => s === '.head, .home-head' ? HEAD : null };
var document = { readyState: 'loading', querySelector: s => s === '[data-again]' ? SLOT : null,
  getElementById: id => new RegExp(`id="${id}"`).test(HEAD.html + (SLOT ? SLOT.html : '')) ? (ELS[id] = ELS[id] || El(id)) : null,
  createElement: () => { const t = {}; Object.defineProperty(t, 'innerHTML', { set(v) { t.content = { parsed: v }; } }); return t; } };
const loaded = () => WIN.filter(([t]) => t === 'DOMContentLoaded').forEach(([, f]) => f());
const click = () => ELS['read-again'].listeners.click.forEach(f => f());
(async () => { try {
""" + body.replace("SHELL_AGAIN", USAGE.group(1) + AGAIN.group(1)).replace("BTN", "ELS['read-again']") + r"""
} catch (e) { OUT.error = String(e) + ' ' + e.stack; } })();
function run() { OUT.R = R; return JSON.stringify(OUT); }
"""
        result = subprocess.run([OSASCRIPT, "-l", "JavaScript", "-e", script], capture_output=True, text=True, timeout=60, check=False)
        if result.returncode:
            raise AssertionError(result.stderr)
        out = json.loads(result.stdout)
        if out["error"]:
            raise AssertionError(out["error"])
        return out

    def test_a_page_that_declares_it_in_its_own_load_handler_gets_the_button_at_the_end_of_its_head(self):
        out = self.run_again("""let done; R.runs = 0;
addEventListener('DOMContentLoaded', () => { window.PAGE_AGAIN = { title: 'Read the rows again', run: () => { R.runs++; return new Promise(f => { done = f; }); } }; });
SHELL_AGAIN
loaded(); R.head = HEAD.html;
click(); await flush(); R.busy = BTN.attrs['aria-busy'];
done(); await flush(); R.after = BTN.attrs['aria-busy'] || null;""")
        self.assertIn('<button class="btn quiet sm" type="button" id="read-again" title="Read the rows again">'
                      '<svg class="i" aria-hidden="true"><use href="#i-rotate-ccw"/></svg><span>Read again</span></button></div>',
                      out["R"]["head"])
        self.assertEqual((out["R"]["runs"], out["R"]["busy"], out["R"]["after"]), (1, "true", None))

    def test_a_page_slot_holds_it_and_a_page_without_page_again_gets_none(self):
        placed = self.run_again("window.PAGE_AGAIN = { run() {} };\nSHELL_AGAIN\nloaded(); R.head = HEAD.html; R.slot = SLOT.html;", slot=True)
        self.assertEqual(placed["R"]["head"], "")
        self.assertIn('id="read-again" title="Read this page again"', placed["R"]["slot"])
        bare = self.run_again("SHELL_AGAIN\nloaded(); R.head = HEAD.html;")
        self.assertNotIn('id="read-again"', bare["R"]["head"])

    def test_a_page_with_no_observed_cell_gets_the_staleness_mark_on_read_again(self):
        """sd:5750: Home's Read again is the shell's button now, so the band marks #read-again when no #refresh is drawn."""
        out = self.run_again("""window.PAGE_AGAIN = { run() {} };
SHELL_AGAIN
loaded(); const b = document.getElementById('read-again'); b.dataset = {}; b.querySelector = () => null;
var body = { dataset: { observed: new Date(Date.now() - 3 * 60000).toISOString(), interval: '1' } };
""" + STALENESS.group(0) + """
staleness(); R.state = b.dataset.state || null; R.html = b.html;""")
        self.assertEqual(out["R"]["state"], "caution")
        self.assertIn('<small class="stale">stale since ', out["R"]["html"])

    def test_the_usage_dropdown_sits_beside_read_again_and_reads_on_load_and_on_open(self):
        """pack sd:9569: Today's Usage strip moved into a dropdown beside Read again, the same on every page."""
        out = self.run_again("""window.PAGE_AGAIN = { run() {} };
SHELL_AGAIN
loaded(); await flush(); R.head = HEAD.html; R.gets = OUT.gets.length;
const d = ELS['usage-drop']; d.open = true; d.listeners.toggle.forEach(f => f()); await flush();
R.menu = ELS['usage-menu'].html;""")
        head = out["R"]["head"]
        self.assertRegex(head, r'^<div class="head-acts" id="head-acts"><details class="usage-drop" id="usage-drop">'
                               r'<summary class="btn quiet sm" id="usage-sum">.*</details><button class="btn quiet sm" type="button" id="read-again"')
        self.assertIn('<div class="fpick-menu ustrip" id="usage-menu" role="group" aria-label="Usage">', head)
        self.assertEqual((out["R"]["gets"], out["gets"]), (1, ["/api/usage/strip", "/api/usage/strip"]))
        self.assertIn("Providers in Settings", out["R"]["menu"])

    def test_a_malformed_usage_answer_is_unknown_with_its_reason(self):
        """Lead review of 8f483c6: a 200 without rows ({} from getJSON) left "Reading usage…" and no state."""
        for answer in ("{}", "{ rows: null }", "{ rows: [{ state: 'ok' }], nosource: [] }"):
            with self.subTest(answer=answer):
                out = self.run_again(f"ANSWER = {answer};\nSHELL_AGAIN\nloaded(); await flush();"
                                     " R.menu = ELS['usage-menu'].html; R.state = ELS['usage-sum'].dataset.state;")
                self.assertIn("Not read: ", out["R"]["menu"])
                self.assertNotIn("Reading usage", out["R"]["menu"])
                self.assertEqual(out["R"]["state"], "unknown")

    def test_read_again_reads_usage_again(self):
        """Lead review of 8f483c6: Read again redrew the page but left the Usage summary at its load-time state."""
        out = self.run_again("""window.PAGE_AGAIN = { run() {} };
SHELL_AGAIN
loaded(); await flush(); R.before = OUT.gets.length; click(); await flush(); R.after = OUT.gets.length;""")
        self.assertEqual((out["R"]["before"], out["R"]["after"]), (1, 2))
        self.assertEqual(out["gets"], ["/api/usage/strip", "/api/usage/strip"])

    def test_a_page_without_read_again_still_gets_the_usage_dropdown(self):
        """Settings has no Read again (AGAIN_ALLOWED in test_v2_registry); the dropdown is on every page all the same."""
        out = self.run_again("SHELL_AGAIN\nloaded(); R.head = HEAD.html;")
        self.assertIn('id="usage-drop"', out["R"]["head"])
        self.assertNotIn('id="read-again"', out["R"]["head"])
        slot = self.run_again("window.PAGE_AGAIN = { run() {} };\nSHELL_AGAIN\nloaded(); R.slot = SLOT.html;", slot=True)
        self.assertIn('id="usage-drop"', slot["R"]["slot"])

    def test_service_status_sits_beside_usage_only_while_a_service_is_down(self):
        """pack sd:9656: a Service status dropdown beside Usage, hidden while every watched service is ok, unknown
        or not read; shown, with its worst state, while one is degraded or out. A later read that finds none hides it."""
        out = self.run_again("""window.PAGE_AGAIN = { run() {} };
ANSWER = { rows: [{ id: 'usage:codex', label: 'codex', state: 'ok', windows: [], service: null }], nosource: [], down: [] };
SHELL_AGAIN
loaded(); await flush(); R.head = HEAD.html; const s = ELS['status-drop']; R.quiet = s.hidden;
ANSWER = { rows: [], nosource: [], down: [
  { name: 'openai', status: 'outage', state: 'warning', title: 'API errors', url: 'https://status.example.test/i/9', lasted: '1h' },
  { name: 'github', status: 'degraded', state: 'caution', title: '', url: 'https://status.example.test', lasted: '' }] };
click(); await flush(); R.loud = s.hidden; R.state = ELS['status-sum'].dataset.state; R.menu = ELS['status-menu'].html;
s.open = true; ANSWER = 'not read'; click(); await flush(); R.failed = [s.hidden, s.open];""")
        head = out["R"]["head"]
        self.assertRegex(head, r'id="usage-drop">.*</details><details class="usage-drop" id="status-drop" hidden>'
                               r'<summary class="btn quiet sm" id="status-sum">.*<span>Service status</span>')
        self.assertLess(head.index('id="status-drop"'), head.index('id="read-again"'))
        self.assertEqual((out["R"]["quiet"], out["R"]["loud"], out["R"]["state"]), (True, False, "warning"))
        menu = out["R"]["menu"]
        self.assertIn('href="https://status.example.test/i/9"', menu)
        self.assertIn("openai outage: API errors", menu)
        self.assertIn("for 1h", menu)
        self.assertIn("github degraded", menu)
        self.assertIn('href="/fleet-health#view=status"', menu)
        self.assertEqual(out["R"]["failed"], [True, False])

    def test_a_run_that_fails_says_so_and_ends_busy(self):
        out = self.run_again("""window.PAGE_AGAIN = { run: () => Promise.reject(new Error('no answer')) };
SHELL_AGAIN
loaded(); click(); await flush(); R.busy = BTN.attrs['aria-busy'] || null;""")
        self.assertEqual((out["toasts"], out["R"]["busy"]), (["Not read again: no answer"], None))


# Pack sd:6019: the palette's why search, a path or a rule's text to the items whose pages say why.
WHY = re.search(r"^  // why:start\n(.*?)^  // why:end$", SHELL_JS, re.S | re.M)


class TheWhySearch(unittest.TestCase):
    """The palette reads /api/why for what is typed and lists each item as an option that opens its page."""

    DOC = {"query": "dashboard/page.py", "kind": "path", "uncited": ["pack/CLAUDE.md:4"],
           "errors": ["git log in system failed: fatal: bad object"],
           "items": [{"item": 42, "title": "Queue <times>", "status": "done", "repo": "pack",
                      "why": "pack commit abc123: feat: first (#1)"},
                     {"item": 999, "title": None, "status": None, "repo": None, "why": "~/.claude/CLAUDE.md:1: rule"}]}

    def run_why(self, body, answer):
        self.assertIsNotNone(WHY, "shell.js has no why:start block, so the palette has no why search")
        return run_js("const itemHref = n => `/tasks?row=${n}`;\nANSWER = " + answer + ";\n" + WHY.group(1) + body)

    def test_each_item_is_an_option_that_opens_its_page_and_values_stay_text(self):
        out = self.run_why("Object.assign(WHY, await whyRead('dashboard/page.py')); R.group = String(whyGroup('dashboard/page.py'));"
                           " R.stale = String(whyGroup('dashboard/other.py')); R.gets = OUT.gets;",
                           f"(path) => [200, {json.dumps(self.DOC)}]")
        self.assertEqual(out["gets"], ["/api/why?q=dashboard%2Fpage.py"])
        group = out["group"]
        self.assertIn("Why · commits on this path", group)
        self.assertIn('data-why data-href="/tasks?row=42"', group)
        self.assertIn("#42 Queue &lt;times&gt;", group)
        self.assertIn("#999 not in this database", group)
        self.assertIn("▨ git log in system failed: fatal: bad object", group)
        self.assertIn("1 matching rule line cites no item", group)
        # A reply for other text draws nothing.
        self.assertEqual(out["stale"], "")

    def test_a_failed_read_says_so(self):
        out = self.run_why("Object.assign(WHY, await whyRead('some rule text')); R.group = String(whyGroup('some rule text'));",
                           "(path) => [500, { error: 'database locked' }]")
        self.assertIn("Why · not read: database locked", out["group"])
