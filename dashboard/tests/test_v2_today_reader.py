"""Today reads /api/now through the shared reader (sd:2483).

What this slice promises: `today.js` reads its document through `shell.read`
(`read.js`, sd:2418) and draws its `needs` (sd:5921). Of two overlapping reads only the newest draws; a row a
read no longer lists runs no command; a failed read clears the rows and
retires their objects; a selection whose row is gone moves to the first row
the filter shows.

`today.js` runs under JavaScriptCore (osascript) against the Tasks stand-in
page and shell, with the real reader and the shell's own `reconcile`, and
with the document `now_screen.document` builds as its fetch answer.
"""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

from sd_dashboard import now_screen, v2
from support import NOW, ScreenCase
from test_now_screen import JobsBackend, fleet_document, repo
from test_today_needs import blocked, lane, work
from test_v2_read import READ_SHELL
from test_v2_tasks import SHELL, STAND_IN
from test_v2_today import OSASCRIPT

V2 = Path(v2.__file__).resolve().parent
TODAY_JS = (V2 / "static" / "today.js").read_text(encoding="utf-8")
SNOOZE_JS = (V2 / "static" / "snooze.js").read_text(encoding="utf-8")
SHELL_JS = (V2 / "static" / "shell.js").read_text(encoding="utf-8")
MARKUP_JS = (V2 / "static" / "markup.js").read_text(encoding="utf-8")
COMMANDS_JS = (V2 / "static" / "commands.js").read_text(encoding="utf-8")
# pack sd:7271: a suggested Queue row's Details are the Tasks page's, so today.html loads task-details.js before today.js.
TASK_DETAILS_JS = (V2 / "static" / "task-details.js").read_text(encoding="utf-8")
# reconcile reads the walk's rule for a shown row (sd:5113), so the extract carries that line too.
RECONCILE = (re.search(r"^  const shownRow = .*$", SHELL_JS, re.M).group(0) + "\n"
             + re.search(r"^  function reconcile\(.*?^  }$", SHELL_JS, re.S | re.M).group(0))

# What today.js reads beyond the Tasks stand-in: the address it writes ?row= into, and the ledger's rows as elements that
# keep their hidden flag and aria-selected between calls. The shell's row is the address's, as shell.js reads it.
PAGE = r"""
function URL(h) { var i = h.indexOf('?'), m = new Map();
  (i < 0 ? '' : h.slice(i + 1)).split('&').filter(Boolean).forEach(p => { var kv = p.split('='); m.set(kv[0], decodeURIComponent(kv[1] || '')); });
  this.searchParams = { set: (k, v) => m.set(k, String(v)), delete: k => m.delete(k), get: k => m.has(k) ? m.get(k) : null };
  Object.defineProperty(this, 'search', { get: () => { var s = [...m].map(([k, v]) => k + '=' + encodeURIComponent(v)).join('&'); return s ? '?' + s : ''; } }); }
Object.defineProperty(location, 'href', { get: () => 'http://example.test/today' + location.search });
var history = { replaceState(a, b, u) { location.search = u.search; } };
var ROWS_EL = document.getElementById('rows'), ROW_CACHE = { html: null, els: [] };
ROWS_EL.querySelectorAll = () => {
  if (ROW_CACHE.html !== ROWS_EL.html) { ROW_CACHE.html = ROWS_EL.html;
    ROW_CACHE.els = [...(ROWS_EL.html || '').matchAll(/<tr data-id="([^"]*)" data-src="([^"]*)"/g)].map(m => { var e = El('tr'), attrs = {};
      e.dataset = { id: m[1], src: m[2] }; e.textContent = m[1]; e.getClientRects = () => e.hidden ? [] : [1]; e.closest = sel => sel === '[hidden]' && e.hidden ? e : null;
      e.setAttribute = (k, v) => { attrs[k] = String(v); }; e.getAttribute = k => k in attrs ? attrs[k] : null; return e; }); }
  return ROW_CACHE.els; };
ROWS_EL.querySelector = () => ROWS_EL.querySelectorAll()[0] || null;
// The Sessions and Gate lamps are links today.js fills in place: each holds a value.
['lamp-sessions', 'lamp-gate'].forEach(id => { const c = document.getElementById(id), v = El(id + '-val'); c.querySelector = () => v; c.val = v; });
"""
SHELL_MORE = r"""
window.shell.row = () => new URLSearchParams(location.search).get('row');
OUT.shown = []; OUT.counts = {}; window.shell.view = { show: id => { OUT.shown.push(id); return true; }, current: () => 'now', count: (id, n) => { OUT.counts[id] = n; } };
{ const commands = C;
""" + RECONCILE + """
  window.shell.reconcile = reconcile; }
const refresh = () => window.PAGE_COMMANDS.find(c => c.label === 'Read Today again').run();
const selected = () => ROWS_EL.querySelectorAll().filter(tr => tr.getAttribute('aria-selected') === 'true').map(tr => tr.dataset.id);
"""
# Every read waits until the test answers it: held[i](doc) answers the i-th read since the page started.
HELD = r"""
const held = [], copy = d => JSON.parse(JSON.stringify(d));
ANSWER = () => new Promise(ok => held.push(doc => ok(doc && doc.error ? [500, doc] : [200, doc])));
"""


class ReaderCase(ScreenCase):
    def setUp(self):
        super().setUp()
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        jobs = JobsBackend(root.name, jobs=[("nightly-sync", "failed", 7, None)])
        self.doc = now_screen.document(self.connection, now=NOW, jobs=jobs,
                                       fleet=lambda area: fleet_document(area, repos=[repo("pushy", ahead=1)]))
        self.assertEqual([row["id"] for row in self.doc["needs"]], ["job:nightly-sync:7", "ahead:pushy:1"])

    def run_page(self, body, search="", after=""):
        script = (STAND_IN + MARKUP_JS + "\nconst mk = window.markup.html;\n" + SHELL + PAGE + SHELL_MORE + READ_SHELL
                  + f"\nconst DOC0 = {json.dumps(self.doc)};\nlocation.search = {json.dumps(search)};\n"
                  + "ANSWER = () => [200, DOC0];\n" + SNOOZE_JS + COMMANDS_JS + TASK_DETAILS_JS + TODAY_JS + after
                  + "\nvar R = {};\n(async () => { try {\n(DOC_LISTENERS.DOMContentLoaded || []).forEach(f => f());\nawait flush();\n"
                  + body + "\n} catch (e) { OUT.error = String(e) + ' ' + e.stack; } })();\n"
                  + "function run() { OUT.R = R; return JSON.stringify(OUT); }\n")
        result = subprocess.run([OSASCRIPT, "-l", "JavaScript", "-e", script],
                                capture_output=True, text=True, timeout=60, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        out = json.loads(result.stdout)
        self.assertIsNone(out["error"])
        return out


class TheReader(ReaderCase):
    def test_the_first_read_draws_the_rows_and_selects_the_first(self):
        """A guard: the page as it was, through the reader."""
        out = self.run_page("R.rows = ELS.rows.html; R.sel = selected(); R.row = location.search;")
        self.assertEqual(out["gets"], ["/api/now"])
        self.assertIn("nightly-sync", out["R"]["rows"])
        self.assertEqual(out["R"]["sel"], ["job:nightly-sync:7"])
        self.assertEqual(out["R"]["row"], "?row=job%3Anightly-sync%3A7")

    def test_only_the_newest_read_draws(self):
        out = self.run_page(HELD + """const gone = copy(DOC0); gone.needs = gone.needs.filter(r => r.kind !== 'job');
refresh(); await flush(); refresh(); await flush();
held[1](gone); await flush(); held[0](copy(DOC0)); await flush();
R.rows = ELS.rows.html; R.type = C.get('job:nightly-sync:7').type;""")
        self.assertNotIn("nightly-sync", out["R"]["rows"], "an older read answered last and drew over the newest")
        self.assertEqual(out["R"]["type"], "not listed")

    def test_a_row_the_read_no_longer_lists_runs_no_command(self):
        out = self.run_page("""DOC0.needs = DOC0.needs.filter(r => r.kind !== 'job'); refresh(); await flush();
R.obj = [C.get('job:nightly-sync:7').type, C.get('job:nightly-sync:7').label]; R.kept = C.get('ahead:pushy:1').type;""")
        self.assertEqual(out["R"]["obj"], ["not listed", "nightly-sync failed with exit 7 (no longer listed)"])
        self.assertEqual(out["R"]["kept"], "repo")

    def test_a_failed_read_clears_the_rows_and_retires_their_objects(self):
        out = self.run_page("""ANSWER = () => [500, { error: 'fleet collection was stopped at its budget' }]; refresh(); await flush();
R.rows = ELS.rows.html; R.sub = ELS.subhead.textContent; R.type = C.get('job:nightly-sync:7').type; R.row = location.search;""")
        self.assertEqual(out["states"][-1:], [{"kind": "error", "source": "/api/now",
                                               "text": "The Today rows were not read: fleet collection was stopped at its budget. Reload retries it."}])
        self.assertNotIn("nightly-sync", out["R"]["rows"])
        self.assertIn("Needs you could not be read: fleet collection was stopped at its budget", out["R"]["rows"])
        self.assertEqual(out["R"]["type"], "not listed")
        self.assertEqual(out["R"]["row"], "")
        self.assertEqual(out["attention"][-1], {"state": "unknown", "n": 0, "what": "rows"})

    def test_a_selection_whose_row_is_gone_moves_to_the_first_row(self):
        """A guard: shell.reconcile did this before the reader; the reader keeps it."""
        out = self.run_page("""DOC0.needs = DOC0.needs.filter(r => r.kind !== 'job'); refresh(); await flush();
R.sel = selected(); R.details = ELS.details.html;""", search="?row=job%3Anightly-sync%3A7")
        self.assertEqual(out["R"]["sel"], ["ahead:pushy:1"])
        self.assertIn("pushy", out["R"]["details"])

    def test_the_page_reads_through_the_shared_reader(self):
        self.assertIn("shell.read(", TODAY_JS)
        self.assertNotIn("fetch('/api/now'", TODAY_JS)

    # sd:3525 (review 2026-10-12, A2 F14): the shell owns j / k and Esc through PAGE_LIST, so a g-chord and the keys sheet
    # read the same on Today as on every other page. The page binds neither key itself.
    KEY = ("ROWS_EL.querySelectorAll().forEach(tr => { tr.scrollIntoView = () => {}; });\n"
           "const key = k => document.dispatchEvent({ type: 'keydown', key: k, target: El('main'), preventDefault() {} });\n")

    def test_the_shell_walks_the_rows_through_page_list_and_the_page_binds_no_j(self):
        out = self.run_page(self.KEY + """key('j'); R.afterJ = selected();
const L = window.PAGE_LIST, rows = [...L.rows()]; L.select(rows[1].dataset.id, rows[1]); R.sel = selected(); R.row = location.search;""")
        self.assertEqual(out["R"]["afterJ"], ["job:nightly-sync:7"], "today.js moved the row on j itself; the shell owns j")
        self.assertEqual(out["R"]["sel"], ["ahead:pushy:1"])
        self.assertEqual(out["R"]["row"], "?row=ahead%3Apushy%3A1")
        self.assertNotRegex(TODAY_JS, r"e\.key === '[jk]'")

    def test_esc_through_page_list_clears_the_source_filter_once(self):
        out = self.run_page(self.KEY + """const cell = { id: 'jobs-cell', dataset: { src: 'jobs' } };
ELS.annunciator.listeners.click[0]({ target: { closest: s => s === 'button.cell' ? cell : null } });
const ids = () => ROWS_EL.querySelectorAll().map(tr => tr.dataset.id);
R.shown = ids(); key('Escape'); R.afterEsc = ids();
R.cleared = window.PAGE_LIST.clear(); R.all = ids(); R.again = window.PAGE_LIST.clear();""")
        self.assertEqual(out["R"]["shown"], ["job:nightly-sync:7"])
        self.assertEqual(out["R"]["afterEsc"], ["job:nightly-sync:7"], "today.js cleared the filter on Esc itself; the shell sends Esc to PAGE_LIST.clear")
        self.assertEqual((out["R"]["cleared"], out["R"]["all"], out["R"]["again"]),
                         (True, ["job:nightly-sync:7", "ahead:pushy:1"], False))


class TheDecisionQueues(ReaderCase):
    def test_the_queues_line_links_each_queue_to_its_home(self):
        """sd:8013: the one Needs you line names each queue's count as a link to the page that decides it."""
        self.doc["needs"].append({"id": "triage", "kind": "triage", "rank": 4, "band": "queued", "lamp": "",
                                  "source": "queues", "what": "3 waiting on a decision", "detail": "Blog Ideas 2 · Tips 1",
                                  "links": [{"label": "Blog Ideas", "n": 2, "href": "/writing#ideas"},
                                            {"label": "Tips", "n": 1, "href": "/writing#tips"}]})
        out = self.run_page("R.rows = ELS.rows.html;")
        line = out["R"]["rows"].split('<tr data-id="triage"')[1].split("</tr>")[0]
        self.assertIn("3 waiting on a decision", line)
        self.assertIn('<a href="/writing#ideas">Blog Ideas 2</a> · <a href="/writing#tips">Tips 1</a>', line)


class TheList(ReaderCase):
    """sd:5921: one ranked list and no tabs; a group row opens to its first five members, then the rest; a lamp or an
    intent keeps only its rows; ?row= on a member opens its group."""

    def setUp(self):
        super().setUp()
        late = [work(n, due="2026-08-01", recurring=True) for n in range(1, 8)]
        self.doc["needs"] = now_screen.needs(self.doc["needs"], work=late, live=set(), today=NOW[:10], lanes=[], briefs=[])

    IDS = "const ids = () => ROWS_EL.querySelectorAll().map(tr => tr.dataset.id);\n"
    CLICK = ("const click = (el, attr, v) => ELS[el].listeners.click[0]({ target: { closest: s => s === `[data-${attr}]`"
             " ? { dataset: { [attr]: v } } : null } });\n")

    def test_today_sections_are_rail_views_and_a_lamp_shows_needs_you(self):
        """Pack sd:7084: Today's sections are views, each a view-<id> panel; a lamp filters Needs you, so it shows that
        view. Pack sd:7218: what runs now comes second. Pack sd:7234: Queue folds Queues and In motion in and keeps q."""
        page = (V2 / "today.html").read_text(encoding="utf-8")
        views = re.findall(r"\{ id: '([a-z]+)', label: '([^']+)', key: '([a-z])'", TODAY_JS)
        self.assertEqual(views, [("needs", "Needs you", "n"), ("queue", "Queue", "q"), ("digest", "Since yesterday", "y"),
                                 ("timeline", "Timeline", "t")])
        for view, _, _ in views:
            self.assertIn(f'id="view-{view}"', page)
        self.assertIn('href="#view=queue" id="lamp-sessions"', page)
        self.assertIn('href="#view=queue" id="lamp-gate"', page)
        self.assertIn("if (srcFilter) window.shell.view.show('needs');", TODAY_JS)
        self.assertNotIn("todayCount", TODAY_JS)

    def test_a_group_row_opens_to_its_first_five_members_then_the_rest(self):
        out = self.run_page(self.IDS + self.CLICK + """R.closed = ids();
click('rows', 'toggle', 'group:late'); R.open = ids(); R.html = ELS.rows.html;
click('rows', 'more', 'group:late'); R.full = ids();""")
        self.assertEqual(out["R"]["closed"], ["job:nightly-sync:7", "group:late", "ahead:pushy:1"])
        self.assertEqual(len(out["R"]["open"]), 3 + now_screen.GROUP_SHOWN)
        self.assertIn(">2 more</button>", out["R"]["html"])
        self.assertIn('class="member" data-in="group:late"', out["R"]["html"])
        self.assertEqual(sorted(i for i in out["R"]["full"] if i.startswith("item:")), [f"item:{n}" for n in range(1, 8)])

    def test_a_lamp_and_an_intent_keep_only_their_rows(self):
        out = self.run_page(self.IDS + self.CLICK + """const cell = { id: 'repos-cell', dataset: { src: 'repos' } };
ELS.annunciator.listeners.click[0]({ target: { closest: s => s === 'button.cell' ? cell : null } }); R.lamp = ids();
window.PAGE_LIST.clear(); click('intents', 'intent', 'fix'); R.fix = ids(); R.chips = ELS.intents.html; R.said = ELS.filtered.textContent;""")
        self.assertEqual(out["R"]["lamp"], ["ahead:pushy:1"])
        self.assertEqual(out["R"]["fix"], ["job:nightly-sync:7"])
        self.assertIn('data-intent="fix" aria-pressed="true">Fix <b>1</b>', out["R"]["chips"])
        self.assertEqual(out["R"]["said"], "1 of 3 shown · fix. Esc clears.")

    def test_a_member_in_the_address_opens_its_group_and_is_selected(self):
        out = self.run_page(self.IDS + "R.ids = ids(); R.sel = selected();", search="?row=item%3A7")
        self.assertIn("item:7", out["R"]["ids"])
        self.assertEqual(out["R"]["sel"], ["item:7"])


class TheConfirms(ReaderCase):
    """sd:5921 review: a Today act that moves state asks first where its home page does. Approve grants a manual repo's
    merge, so it asks as Queue's does; a group act on more members than the shell's bulk limit asks as a bulk act does.
    Retry runs at once, as on Queue. `decline` is the shell's run with the confirm answered Cancel."""

    ENTRY = "lane:system:5780:" + "a" * 12

    def setUp(self):
        super().setUp()
        late = [work(n, due="2026-08-01", recurring=True) for n in range(1, 32)]
        briefs = [{"id": f"b{n}", "subj": f"Brief {n}", "src": "hoa", "words": 90, "day": NOW[:10], "seen": "x"} for n in (1, 2)]
        self.doc["needs"] = now_screen.needs(self.doc["needs"], work=late, live=set(), today=NOW[:10], briefs=briefs,
                                             lanes=[{**lane(rows=[blocked(5780)]), "runner_merge": "manual"}])

    DECLINE = """const cmd = id => REG.find(c => c.id === id);
function decline(c, o) {
  const off = c.when ? c.when(o) : true; if (off !== true) { OUT.toasts.push({ msg: 'off: ' + off }); return; }
  if (c.risk === 'confirm' || c.fields || asksFirst(c, o)) { OUT.confirms.push(c.id); return; }
  c.run(o);
}
"""

    def test_approve_asks_first_and_posts_nothing_until_it_is_confirmed(self):
        out = self.run_page(self.DECLINE + f"""const o = C.get('{self.ENTRY}');
decline(cmd('today.lane.approve'), o); await flush(); R.said = (cmd('today.lane.approve').consequence || (() => null))(o);""")
        self.assertEqual([(path, body["action"]) for path, body, _ in out["posts"]], [])
        self.assertEqual(out["confirms"], ["today.lane.approve"])
        self.assertEqual(out["R"]["said"], "The entry is queued again at head aaaaaaaaaaaa, and the runner may merge it to"
                                           " the default branch. A merge is not taken back.")

    def test_retry_runs_at_once_as_on_queue(self):
        out = self.run_page(self.DECLINE + f"decline(cmd('today.lane.retry'), C.get('{self.ENTRY}')); await flush();")
        self.assertEqual(out["confirms"], [])
        self.assertEqual([(path, body["action"]) for path, body, _ in out["posts"]], [("/api/queue/move", "retry")])

    STALE = "The queue changed since the page read it. Read it again, then retry."

    def test_a_stale_retry_reads_today_again_and_says_so_until_the_next_press(self):
        # pack sd:7315: a 409 left only a passing toast, and the row looked the same. Today reads again, then a lasting
        # line above the list says the queue changed and that a press acts on the fresh read; it retries nothing itself.
        # The next press clears the line, and one that lands leaves it hidden.
        out = self.run_page(self.DECLINE + f"""let answer = [409, {{ error: {json.dumps(self.STALE)}, reload: true }}];
ANSWER = (path, body) => path === '/api/queue/move' ? answer : [200, DOC0];
const said = () => [ELS['needs-said'].hidden, ELS['needs-said'].textContent];
shellRun(cmd('today.lane.retry'), C.get('{self.ENTRY}')); await flush(); await flush();
R.stale = said(); R.gets = OUT.gets.slice(); R.toast = OUT.toasts[OUT.toasts.length - 1].msg;
answer = [200, {{ ok: true }}]; shellRun(cmd('today.lane.retry'), C.get('{self.ENTRY}')); await flush(); await flush();
R.landed = said();""")
        self.assertEqual([body["action"] for _, body, _ in out["posts"]], ["retry", "retry"], "nothing retries by itself")
        self.assertEqual(out["R"]["gets"], ["/api/now", "/api/now"])
        hidden, text = out["R"]["stale"]
        self.assertFalse(hidden)
        self.assertRegex(text, r"^system: sd:5780 not queued again: The queue changed since the page read it\. "
                               r"Read again at \d\d:\d\d; a press now acts on that read\.$")
        self.assertIn("Read again at", out["R"]["toast"])
        self.assertTrue(out["R"]["landed"][0], "a press that lands hides the line")

    def test_a_stale_retry_whose_read_again_failed_claims_no_fresh_read(self):
        out = self.run_page(self.DECLINE + f"""ANSWER = (path, body) => path === '/api/queue/move' ? [409, {{ error: {json.dumps(self.STALE)}, reload: true }}]
  : OUT.gets.length > 1 ? [500, {{ error: 'fleet collection was stopped' }}] : [200, DOC0];
shellRun(cmd('today.lane.retry'), C.get('{self.ENTRY}')); await flush(); await flush();
R.said = ELS['needs-said'].textContent;""")
        self.assertEqual(out["gets"], ["/api/now", "/api/now"])
        self.assertEqual(out["R"]["said"], "system: sd:5780 not queued again: The queue changed since the page read it."
                                           " It was not read again: Read again tries it.")

    def test_a_stale_retry_whose_today_read_left_its_lane_out_claims_no_fresh_read(self):
        # Review of pack sd:7315: Today was read again, but not this repo's lane; the line claims no fresh read.
        for name, part in (("lane dark", {"sources": {**self.doc["sources"], "lane": "lane collector failed"}}),
                           ("lane partial", {"lane": {**self.doc["lane"], "unread_paths": ["/repos/system"]}})):
            with self.subTest(name):
                out = self.run_page(self.DECLINE + f"""const DOC1 = {{ ...DOC0, ...{json.dumps(part)} }};
ANSWER = (path, body) => path === '/api/queue/move' ? [409, {{ error: {json.dumps(self.STALE)}, reload: true }}]
  : OUT.gets.length > 1 ? [200, DOC1] : [200, DOC0];
shellRun(cmd('today.lane.retry'), C.get('{self.ENTRY}')); await flush(); await flush();
R.said = ELS['needs-said'].textContent;""")
                self.assertEqual(out["gets"], ["/api/now", "/api/now"])
                self.assertEqual(out["R"]["said"], "system: sd:5780 not queued again: The queue changed since the page read it."
                                                   " It was not read again: Read again tries it.")

    def test_a_retry_with_no_answer_says_its_outcome_is_not_known(self):
        out = self.run_page(self.DECLINE + f"""ANSWER = (path, body) => path === '/api/queue/move' ? [502, {{ error: 'sd-ship lane gave no answer.' }}] : [200, DOC0];
shellRun(cmd('today.lane.retry'), C.get('{self.ENTRY}')); await flush(); await flush();
R.said = [ELS['needs-said'].hidden, ELS['needs-said'].textContent];""")
        self.assertEqual(out["R"]["said"], [False, "system: not known whether sd:5780 was queued again: sd-ship lane gave no"
                                                   " answer. The lane was read again; check sd:5780 there before you act again."])

    def test_a_group_act_past_the_bulk_limit_asks_first_and_a_small_one_runs(self):
        out = self.run_page(self.DECLINE + """decline(cmd('today.group.snooze'), C.get('group:late'));
decline(cmd('today.group.seen'), C.get('group:briefs')); await flush();
R.said = (cmd('today.group.snooze').consequence || (() => null))(C.get('group:late'));""")
        self.assertEqual(out["confirms"], ["today.group.snooze"])
        self.assertEqual([body["row"] for _, body, _ in out["posts"]], ["b1", "b2"])
        self.assertEqual(out["R"]["said"], "Writes 31 snoozes, one per member; each member comes back on its own after a week"
                                           " or by Unsnooze.")


class TheTrack(ReaderCase):
    """sd:5921: the day track draws one mark per entry of the document's `track`, the same marks as a table, and the key;
    a day with no mark is one line, never an empty axis; a track that was not read says so."""

    TRACK = {"since": "2026-09-05T12:00:00Z", "until": NOW, "key": {"landed": 2, "landing": 1, "due_week": 17, "blocked": 27},
             "landed": [{"at": "2026-09-06T10:00:00Z", "id": "item:7", "item": 7, "what": "sd:7 Seven"},
                        {"at": "2026-09-06T11:00:00Z", "id": "item:8", "item": 8, "what": "sd:8 <b>Eight</b>"}],
             "jobs": [{"at": "2026-09-06T09:00:00Z", "id": "job:nightly-sync:7", "what": "nightly-sync failed with exit 7"}],
             "briefs": [], "live": [{"at": "2026-09-06T11:30:00Z", "id": "session:9:0", "item": 9, "what": "sd:9 worktree"}]}
    DRAWN = "R.plot = ELS['track-plot'].html; R.rows = ELS['track-rows'].html; R.key = ELS.flowkey.html; R.table = ELS['track-table'].hidden;"

    def test_each_entry_is_one_mark_and_one_table_row(self):
        self.doc["track"] = self.TRACK
        out = self.run_page(self.DRAWN)
        plot = out["R"]["plot"]
        self.assertEqual((plot.count('<line class="land"'), plot.count('<rect class="fail"'), plot.count('<circle class="brief"'),
                          plot.count('<rect class="sess"')), (2, 1, 0, 1))
        self.assertIn('aria-label="Last 24 hours: 2 landed, 1 job failed, 0 briefs filed, 1 session live"', plot)
        # sd:6714: each mark names the row a click selects.
        self.assertIn('<a href="?row=job%3Anightly-sync%3A7" data-id="job:nightly-sync:7">', plot)
        self.assertIn("sd:8 &lt;b&gt;Eight&lt;/b&gt;", out["R"]["rows"])
        self.assertEqual(out["R"]["rows"].count("<tr data-id="), 4)
        self.assertIn('status=blocked&amp;realm=all">Blocked</a></li><li><b>27</b>', out["R"]["key"])
        self.assertFalse(out["R"]["table"])

    def test_each_key_link_filters_tasks_on_values_tasks_accepts(self):
        """sd:5934: Landed 24h linked status=merged,delivered, which no task has, so Tasks listed none. Each key link names
        only statuses `workflow.TASK_STATUSES` holds and age buckets `reads.age_histogram` keys, and Landed keeps 24 h."""
        from urllib.parse import parse_qs, urlsplit

        from sd_db import reads, workflow

        self.doc["track"] = self.TRACK
        key = self.run_page(self.DRAWN)["R"]["key"].replace("&amp;", "&")
        links = dict((label, href) for href, label in re.findall(r'<a href="([^"]+)">([^<]+)</a>', key))
        ages = {bucket.key for bucket in reads.age_histogram([], now=NOW)}
        for label, href in links.items():
            if not href.startswith("/tasks"):
                continue
            query = parse_qs(urlsplit(href).query)
            with self.subTest(label=label):
                self.assertLessEqual(set(",".join(query.get("status", [])).split(",")) - {""}, set(workflow.TASK_STATUSES))
                self.assertLessEqual(set(",".join(query.get("age", [])).split(",")) - {""}, ages)
        landed = parse_qs(urlsplit(links["Landed 24h"]).query)
        self.assertEqual((landed["status"], landed["age"], landed["realm"]), (["done"], ["0"], ["all"]))

    def test_a_brief_mark_is_solid_with_a_24_unit_hit_area(self):
        """sd:6232: the brief mark was a hollow ring, 3.5 units, and only the ring took the pointer. It is a solid dot now,
        a shape no other lane uses, inside a clear 24 x 24 square in the same link that takes the press; at the track's
        900-unit width that square is 24 px. The square sits between the Jobs (36-44) and Live (72-75) lanes."""
        self.doc["track"] = {**self.TRACK, "briefs": [{"at": "2026-09-06T08:00:00Z", "id": "brief:3", "what": "brief 3 filed"}]}
        plot = self.run_page(self.DRAWN)["R"]["plot"]
        link = re.search(r'<a href="\?row=brief%3A3" data-id="brief:3">(.*?)</a>', plot, re.S).group(1)
        hit = re.search(r'<rect class="hit" x="([\d.]+)" y="(\d+)" width="(\d+)" height="(\d+)">', link)
        dot = re.search(r'<circle class="brief" cx="([\d.]+)" cy="(\d+)" r="([\d.]+)"', link)
        self.assertIsNotNone(hit, link)
        self.assertIsNotNone(dot, link)
        self.assertEqual((int(hit[3]), int(hit[4])), (24, 24))
        self.assertAlmostEqual(float(hit[1]) + 12, float(dot[1]), places=1)
        self.assertEqual(int(hit[2]) + 12, int(dot[2]))
        self.assertTrue(44 < int(hit[2]) and int(hit[2]) + 24 < 72, "the hit square overlaps another lane")
        self.assertGreaterEqual(float(dot[3]), 4)
        css = (V2 / "static" / "today.css").read_text(encoding="utf-8")
        brief = re.search(r"^\.daytrack \.brief \{([^}]*)\}", css, re.M).group(1)
        self.assertRegex(brief, r"fill: var\(--color-[a-z0-9-]+\)")
        self.assertIn("pointer-events: none", brief)
        self.assertRegex(css, r"(?m)^\.daytrack \.hit \{ fill: transparent; \}")

    def test_a_day_with_no_mark_is_one_line_not_an_empty_axis(self):
        self.doc["track"] = {**self.TRACK, "landed": [], "jobs": [], "live": [], "key": {**self.TRACK["key"], "landed": 0}}
        out = self.run_page(self.DRAWN)
        self.assertIn("Nothing landed, failed, was filed or ran in the last 24 hours.", out["R"]["plot"])
        self.assertNotIn("<svg", out["R"]["plot"])
        self.assertTrue(out["R"]["table"])

    def test_a_track_that_was_not_read_says_so(self):
        self.doc["track"] = {"error": "database is locked"}
        out = self.run_page(self.DRAWN)
        self.assertIn("The day track was not read: database is locked.", out["R"]["plot"])
        self.assertEqual((out["R"]["key"], out["R"]["table"]), ("", True))


class TheTimeline(ReaderCase):
    """pack sd:7681: the Timeline is one chart on one time axis: Today's Landed, Jobs, Briefs and Live lanes, then one
    lane per Activity kind, read from /api/activity?range=7d when the view first shows. The 24h / 7d tiles drive the
    whole view: its axis, its marks and its table."""

    TRACK = {"since": "2026-08-30T12:00:00Z", "day": "2026-09-05T12:00:00Z", "until": NOW,
             "key": {"landed": 1, "landing": 0, "due_week": 0, "blocked": 0},
             "landed": [{"at": "2026-09-03T10:00:00Z", "id": "item:6", "item": 6, "what": "sd:6 Three days ago"},
                        {"at": "2026-09-06T10:00:00Z", "id": "item:7", "item": 7, "what": "sd:7 Seven"}],
             "jobs": [], "briefs": [], "live": []}
    ACTIVITY = {"read": NOW, "from": "2026-08-30T12:00:00Z", "to": NOW, "kinds": ["merge", "run", "review", "command"],
                "sources": {"merge": "", "review": "database locked", "run": "", "job": "", "command": ""},
                "events": [{"id": "merge:41", "k": "merge", "at": "2026-09-06T09:30:00Z", "s": "ok", "what": "Port the page"},
                           {"id": "run:5", "k": "run", "at": "2026-09-06T08:00:00Z", "s": "warning", "what": "nightly-sync failed"},
                           {"id": "cmd:9", "k": "command", "at": "2026-09-03T09:00:00Z", "s": "ok", "what": "sd status · exit 0"}]}
    ANSWER = ("const ACT = %s;\nANSWER = path => path === '/api/activity?range=7d' ? [200, ACT] : [200, DOC0];\n"
              "const showTimeline = () => (DOC_LISTENERS['shell:view'] || []).forEach(f => f({ detail: { id: 'timeline' } }));\n"
              "const range = v => ELS['track-range'].listeners.click[0]({ target: { closest: s => s === '[data-range]' ? { dataset: { range: v } } : null } });\n")
    DRAWN = "R.plot = ELS['track-plot'].html; R.rows = ELS['track-rows'].html; R.tiles = ELS['track-range'].html; R.h = ELS['track-h'].textContent;"

    def run_timeline(self, body):
        self.doc["track"] = self.TRACK
        return self.run_page(body, after=self.ANSWER % json.dumps(self.ACTIVITY))

    def test_the_timeline_carries_the_activity_lanes_under_todays(self):
        out = self.run_timeline("R.before = ELS['track-plot'].html; showTimeline(); await flush();" + self.DRAWN)
        self.assertEqual(out["gets"], ["/api/now", "/api/activity?range=7d"], "Activity is read once the Timeline shows")
        self.assertNotIn("tick", out["R"]["before"])
        plot = out["R"]["plot"]
        labels = re.findall(r'<text class="lane-l" x="0" y="\d+">(\w+)</text>', plot)
        self.assertEqual(labels, ["Landed", "Jobs", "Briefs", "Live", "Merges", "Runs", "Reviews", "Commands"])
        self.assertEqual(plot.count("<svg "), 1, "one chart, one time axis")
        # The 24 hours: the merge and the failed run; the command 3 days old is not drawn.
        self.assertEqual(re.findall(r'<rect class="tick (\w+)"', plot), ["ok", "warning"])
        self.assertIn('<a href="/activity?kind=merge&amp;row=merge%3A41">', plot)
        self.assertIn("▨ unknown · reviews not read: database locked", plot)
        self.assertIn('aria-label="Last 24 hours: 1 landed, 0 jobs failed, 0 briefs filed, 0 sessions live; 1 merges, 1 runs,'
                      ' reviews not read, 0 commands"', plot)
        self.assertIn("Port the page", out["R"]["rows"])
        self.assertNotIn("sd status", out["R"]["rows"])

    def test_the_range_tiles_change_the_axis_and_the_window(self):
        out = self.run_timeline("showTimeline(); await flush(); R.day = ELS['track-plot'].html; range('7d');" + self.DRAWN
                                + " R.search = location.search;")
        day, week = out["R"]["day"], out["R"]["plot"]

        def axis(plot):
            return re.findall(r'<text class="axis"[^>]*>([^<]+)</text>', plot)

        self.assertTrue(axis(day) and all(re.fullmatch(r"\d\d:00", a) for a in axis(day)), axis(day))
        self.assertEqual(len(axis(week)), 7, axis(week))
        self.assertFalse(any(a.endswith(":00") for a in axis(week)), "the 7d axis names days, not hours")
        self.assertIn('aria-label="Last 7 days: 2 landed,', week)
        # Lane by lane: the merge, the failed run, and the command 3 days old.
        self.assertEqual(re.findall(r'<rect class="tick (\w+)"', week), ["ok", "warning", "ok"])
        self.assertIn("sd status · exit 0", out["R"]["rows"])
        self.assertIn("sd:6 Three days ago", out["R"]["rows"])
        self.assertIn('data-range="7d" aria-pressed="true"', out["R"]["tiles"])
        self.assertIn('data-range="24h" aria-pressed="false"', out["R"]["tiles"])
        self.assertEqual(out["R"]["h"], "The last 7 days")
        self.assertTrue(out["R"]["search"].endswith("&range=7d"), "the address keeps the row and the range")

    def test_the_address_keeps_the_7_day_range(self):
        out = self.run_timeline("showTimeline(); await flush();" + self.DRAWN)
        self.assertIn("Last 24 hours", out["R"]["plot"])
        self.doc["track"] = self.TRACK
        out = self.run_page("showTimeline(); await flush();" + self.DRAWN, search="?range=7d",
                            after=self.ANSWER % json.dumps(self.ACTIVITY))
        self.assertIn('aria-label="Last 7 days:', out["R"]["plot"])

    def test_an_activity_read_that_fails_hatches_every_kind_with_the_reason(self):
        out = self.run_timeline("ACT.status = 1; ANSWER = path => path.startsWith('/api/activity') ? [500, { error: 'database is locked' }] : [200, DOC0];"
                                " showTimeline(); await flush();" + self.DRAWN)
        plot = out["R"]["plot"]
        for kind in ("merges", "runs", "reviews", "commands"):
            self.assertIn(f"▨ unknown · {kind} not read:", plot)
        self.assertNotIn('<rect class="tick', plot)

    def test_an_activity_read_that_fails_shows_even_when_no_mark_is_in_the_window(self):
        """A window with no Today mark and no event still says Activity was not read: unknown is not zero."""
        self.TRACK = {**self.TRACK, "landed": []}
        out = self.run_timeline("ANSWER = path => path.startsWith('/api/activity') ? [500, { error: 'database locked' }] : [200, DOC0];"
                                " showTimeline(); await flush();" + self.DRAWN)
        plot = out["R"]["plot"]
        self.assertNotIn("Nothing landed", plot)
        for kind in ("merges", "runs", "reviews", "commands"):
            self.assertIn(f"▨ unknown · {kind} not read: database locked", plot)
        self.assertIn("merges not read, runs not read", plot)

    def test_a_7_day_tick_and_row_open_activity_at_7_days(self):
        """Activity reads 24 hours unless asked: an older event's link carries the range, or it opens with no row."""
        out = self.run_timeline("showTimeline(); await flush(); R.day = ELS['track-plot'].html; range('7d');" + self.DRAWN)
        self.assertIn('<a href="/activity?kind=merge&amp;row=merge%3A41">', out["R"]["day"])
        self.assertIn('<a href="/activity?kind=command&amp;row=cmd%3A9&amp;range=7d">', out["R"]["plot"])
        self.assertIn('<a href="/activity?kind=command&amp;row=cmd%3A9&amp;range=7d">', out["R"]["rows"])


HELP = {
    "jobs": "Failed scheduled jobs. Press to list them in Needs you.",
    "lane": "Lane entries blocked until someone acts; merging, next and flaky heads (a gate that failed, then passed at one"
            " head) are counted under it. Press to list them in Needs you.",
    "reports": "Open job alerts and reports. Press to list them in Needs you.",
    "pulls": "Pull requests waiting on you. Press to list them in Needs you.",
    "mail": "Mail waiting for you. Press to list it in Needs you.",
    "repos": "Checkouts to tidy: ahead of origin, dirty or pinned. Press to list them in Needs you.",
    "sessions": "Work items with a live session; the small count is sessions. Press to open the Queue view.",
    "gate": "Gate slots running of the cap, and runs waiting for one; load5 is the machine load. Press to open the Queue view.",
}


class TheLamps(ReaderCase):
    """sd:TBD: a fleet area the server's snapshot kept says when it was read; a stale one is unknown, never green."""

    CELLS = """const CELLS = ['repos', 'sessions'].map(src => { const c = El('div'), val = El('span');
  c.dataset = { src }; c.querySelector = () => val; c.val = val; return c; });
document.querySelectorAll = sel => sel === '.cell[data-src]' ? CELLS : [];
DOC0.observed = { repos: { at: '2026-09-06T11:31:00Z', stale: 'git did not answer' }, sessions: { at: '2026-09-06T11:58:00Z', stale: null } };
refresh(); await flush();
R.cells = CELLS.map(c => [c.dataset.src, c.dataset.state, c.title || null]);"""

    def test_a_kept_fleet_area_shows_its_read_time_and_a_stale_one_is_unknown(self):
        out = self.run_page(self.CELLS)
        self.assertEqual(out["R"]["cells"], [["repos", "unknown", HELP["repos"] + "\nobserved 11:31 UTC · stale: git did not answer"],
                                            ["sessions", "ok", HELP["sessions"] + "\nobserved 11:58 UTC"]])

    def test_every_lamp_says_what_it_counts_and_what_a_press_does_on_hover(self):
        # Pack sd:7383: the six source cells and the Sessions and Gate links carry a native title; a lamp's own detail follows it.
        out = self.run_page("""const CELLS = Object.keys(HELP0).filter(k => k !== 'sessions' && k !== 'gate').map(src => { const c = El('button'), val = El('span');
  c.dataset = { src }; c.querySelector = () => val; return c; });
document.querySelectorAll = sel => sel === '.cell[data-src]' ? CELLS : [];
refresh(); await flush();
R.titles = Object.fromEntries([...CELLS.map(c => [c.dataset.src, c.title]), ['sessions', ELS['lamp-sessions'].title], ['gate', ELS['lamp-gate'].title]]);""".replace(
            "Object.keys(HELP0)", json.dumps(sorted(HELP))))
        self.assertEqual(out["R"]["titles"], HELP)


class TheUnreadLamps(ReaderCase):
    """sd:5939: a source read only in part keeps its counts on its lamp but reads unknown, never ok; the Sessions lamp
    reads unknown when a live-session source was not read."""

    def setUp(self):
        super().setUp()
        self.doc["needs"] = now_screen.needs(
            [*self.doc["needs"], {**now_screen.unread_row("lane", {"pack": "sd-ship gave no answer"}), "band": "broken"},
             {**now_screen.unread_row("live", {"herdr": "herdr did not answer"}), "band": "broken"}],
            work=[], live=set(), today=NOW[:10], lanes=[lane(rows=[blocked(5780)])], briefs=[])
        self.doc["lane"] = {**self.doc["lane"], "blocked": 1, "unread": 1, "unread_why": "pack: sd-ship gave no answer"}

    def test_a_lane_read_in_part_and_an_unread_live_source_read_unknown(self):
        out = self.run_page("""const lane = El('div'), val = El('span'); lane.dataset = { src: 'lane' }; lane.querySelector = () => val;
document.querySelectorAll = sel => sel === '.cell[data-src]' ? [lane] : [];
refresh(); await flush();
R.lane = [lane.dataset.state, lane.title, val.html]; const ses = ELS['lamp-sessions']; R.ses = [ses.dataset.state, ses.title];""")
        state, title, text = out["R"]["lane"]
        self.assertEqual((state, title), ("unknown", HELP["lane"] + "\npack: sd-ship gave no answer"))
        self.assertIn("<b>1</b> blocked", text)
        self.assertEqual(out["R"]["ses"], ["unknown", HELP["sessions"] + "\nherdr: herdr did not answer"])


class TheSnoozedDarkLamps(ReaderCase):
    """sd:5954: a dark row snoozes now; a snoozed one still makes its lamp read unknown, never ok (sd:5939)."""

    def test_a_snoozed_dark_row_keeps_its_lamp_unknown(self):
        for source, lamp in (("lane", "lane"), ("live", "sessions")):
            held = {**now_screen.unread_row(source, {"pack": "no answer"}), "band": "broken", "lamp": lamp,
                    "seen": "0" * 16, "until": "2026-09-07T08:00:00Z"}
            self.doc["snoozed"] = [held]
            out = self.run_page("""const lane = El('div'), val = El('span'); lane.dataset = { src: 'lane' }; lane.querySelector = () => val;
document.querySelectorAll = sel => sel === '.cell[data-src]' ? [lane] : [];
refresh(); await flush();
R.lane = lane.dataset.state; R.ses = ELS['lamp-sessions'].dataset.state;""")
            with self.subTest(source=source):
                self.assertEqual(out["R"]["lane" if lamp == "lane" else "ses"], "unknown")


class TheFix(ReaderCase):
    """sd:3456: Fix on a work row Today lists posts /api/items/<id>/fix with the revision the Now read gave."""

    def setUp(self):
        super().setUp()
        from sd_db import workflow

        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        self.task = self.item("Write the brief", kind="task", status="in_progress")
        self.report = self.item("nightly: needs attention", kind="report", status="in_progress")
        jobs = JobsBackend(root.name, jobs=[("nightly-sync", "failed", 7, None)])
        self.doc = now_screen.document(self.connection, now=NOW, jobs=jobs,
                                       fleet=lambda area: fleet_document(area, repos=[repo("pushy", ahead=1)]))
        self.revisions = {n: workflow.item_state(self.connection, n)["revision"] for n in (self.task, self.report)}

    def test_fix_asks_first_names_the_session_by_the_item_kind_and_posts_the_revision(self):
        out = self.run_page(f"""ANSWER = (path, body) => path.endsWith('/fix') ? [200, {{ revision: 'e'.repeat(64), fix: {{ session: 'builder', pane: 'w9:p1' }} }}] : [200, DOC0];
R.task = cmd('work item.fix').consequence(C.get('item:{self.task}')); R.report = cmd('work item.fix').consequence(C.get('item:{self.report}'));
R.on = cmd('work item.fix').when(C.get('item:{self.task}')) === true; // a listed work row is open, so Fix is always on
shellRun(cmd('work item.fix'), C.get('item:{self.task}')); await flush();""")
        self.assertIn(f"Opens a Claude session on #{self.task} in herdr", out["R"]["task"])
        self.assertIn(f"Opens a Claude session on report #{self.report} in herdr. It finds the cause", out["R"]["report"])
        self.assertTrue(out["R"]["on"])
        self.assertEqual(out["confirms"], ["work item.fix"])
        self.assertEqual(out["posts"], [[f"/api/items/{self.task}/fix", {"revision": self.revisions[self.task]}, 64]])
        self.assertEqual(out["toasts"][-1]["msg"], f"Fix started · #{self.task} · builder session in herdr pane w9:p1")
        self.assertEqual(out["gets"], ["/api/now", "/api/now"])

    def test_fix_on_a_report_with_an_expired_login_opens_its_credentials_row(self):
        """sd:6964: a job alert whose log shows claude signed out gets Fix, first, to Health > Credentials' claude row;
        a report with no login cause keeps Acknowledge first, and Fix there is the Reports page's (pack sd:7683)."""
        from sd_db import reporting

        def report(job, text):
            return reporting.ingest(self.connection, job=job, run_id="r1", started="2026-09-06T09:00:00Z",
                                    ended="2026-09-06T09:01:00Z", exit_code=1, text=text, source_path="/tmp/x.log",
                                    attention=True, attention_basis="job exited 1")["item"]["id"]
        login = report("blog-idea-accept", "[blog-idea-accept] 2026-09-28T12:01:09-0600 agent: claude\n"
                                           "Not logged in · Please run /login\n[blog-idea-accept] FAILED rc=1\n")
        plain = report("nas-full-copy", "mirror failed\n")
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        self.doc = now_screen.document(self.connection, now=NOW, jobs=JobsBackend(root.name, jobs=[]),
                                       fleet=lambda area: fleet_document(area, repos=[]))
        out = self.run_page(f"""const fix = cmd('report.fix'), ack = cmd('today.report.ack'), o = C.get('report:{login}'), p = C.get('report:{plain}');
R.primary = [fix.primary(o), ack.primary(o), fix.primary(p), ack.primary(p)]; R.off = fix.when(p);
// The stand-in's href only reads; this one records where the page goes.
location = {{ search: location.search, set href(u) {{ R.href = u; }}, get href() {{ return R.href; }} }};
shellRun(fix, o); await flush();""")
        self.assertEqual(out["R"]["href"], "/health?area=cred&row=cred%3Aclaude")
        self.assertEqual(out["toasts"][-1]["msg"], "Health opens on claude")
        self.assertEqual((out["posts"], out["confirms"]), ([], []))
        self.assertEqual(out["R"]["primary"], [True, False, False, True])
        self.assertIs(out["R"]["off"], True)

    def test_fix_on_a_report_is_the_reports_debug_session_unless_another_session_has_it(self):
        """pack sd:7683: a report row offers Fix as Reports does, a debug session posted with the read's revision, beside
        Acknowledge; a report another session took names who has it, in the row and Details, and offers no Fix."""
        from sd_db import reporting, workflow, writes

        def report(job):
            return reporting.ingest(self.connection, job=job, run_id="r1", started="2026-09-06T09:00:00Z",
                                    ended="2026-09-06T09:01:00Z", exit_code=1, text="mirror failed\n", source_path="/tmp/x.log",
                                    attention=True, attention_basis="job exited 1")["item"]["id"]
        plain, taken = report("nas-full-copy"), report("hoa-core-bills")
        writes.transition(self.connection, taken, "in_progress", who="hub-triage", reason="triage")
        revision = workflow.item_state(self.connection, plain)["revision"]
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        self.doc = now_screen.document(self.connection, now=NOW, jobs=JobsBackend(root.name, jobs=[]),
                                       fleet=lambda area: fleet_document(area, repos=[]))
        out = self.run_page(ICONS + f"""ANSWER = (path, body) => path.endsWith('/fix') ? [200, {{ revision: 'e'.repeat(64), fix: {{ session: 'debug', pane: 'w9:p1' }} }}] : [200, DOC0];
refresh(); await flush();
const fix = cmd('report.fix'), o = C.get('report:{plain}'), t = C.get('report:{taken}');
R.drawn = OUT.drawn['report:{plain}']; R.plain = icons('report:{plain}'); R.taken = icons('report:{taken}');
R.off = fix.when(t); R.says = fix.consequence(o);
window.PAGE_LIST.select('report:{taken}'); R.details = ELS.details.html;
shellRun(fix, o); await flush();""")
        self.assertTrue(out["R"]["drawn"], "a report row draws its icon actions, Fix beside Acknowledge")
        self.assertEqual([c for c, _, _ in out["R"]["plain"]], ["report.fix", "today.report.ack"])
        self.assertEqual([c for c, _, _ in out["R"]["taken"]], ["today.report.ack"])
        self.assertEqual(out["R"]["off"], "hub-triage has it (in_progress): open its session in herdr, or Fix it from Reports")
        self.assertIn(f"Opens a Claude session on report #{plain} in herdr", out["R"]["says"])
        self.assertIn("hub-triage has it (in_progress)", out["R"]["details"])
        self.assertEqual(out["confirms"], ["report.fix"])
        self.assertEqual(out["posts"], [[f"/api/items/{plain}/fix", {"revision": revision}, 64]])
        self.assertEqual(out["toasts"][-1]["msg"], f"Fix started · #{plain} · debug session in herdr pane w9:p1")

    def test_a_stale_fix_reads_now_again_and_says_why(self):
        out = self.run_page("""ANSWER = (path, body) => path.endsWith('/fix') ? [409, { error: 'item changed; reload it before applying this change', reload: true }] : [200, DOC0];
shellRun(cmd('work item.fix'), C.get('item:""" + str(self.task) + """')); await flush();""")
        self.assertEqual(len(out["posts"]), 1)
        self.assertEqual(out["gets"], ["/api/now", "/api/now"])
        self.assertIn("reload it", out["toasts"][-1]["msg"])

    def test_a_pr_attention_row_opens_its_pull_in_a_new_tab_and_another_report_has_no_link(self):
        """pack sd:7776: a pr-attention row in Needs you carries its pull's address (`places.pull_url`), and the row draws
        an Open PR icon link beside its actions; a report with no pull draws none."""
        from sd_db import reporting

        url = "https://github.com/example/bench/pull/617"

        def report(job, basis):
            return reporting.ingest(self.connection, job=job, run_id="r1", started="2026-09-06T09:00:00Z",
                                    ended="2026-09-06T09:01:00Z", exit_code=0 if job == "pr-attention" else 1,
                                    text="ran\n", source_path="sd shadow sync", attention=True,
                                    attention_basis=basis)["item"]["id"]
        pull, plain = report("pr-attention", f"review_requested {url}"), report("nas-full-copy", "job exited 1")
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        self.doc = now_screen.document(self.connection, now=NOW, jobs=JobsBackend(root.name, jobs=[]),
                                       fleet=lambda area: fleet_document(area, repos=[]))
        rows = {row["id"]: row for row in self.doc["needs"]}
        self.assertEqual((rows[f"report:{pull}"]["pr_url"], rows[f"report:{plain}"]["pr_url"]), (url, None))
        out = self.run_page("refresh(); await flush(); R.rows = ELS.rows.html;")
        line = {i: re.search(rf'<tr data-id="report:{i}".*?</tr>', out["R"]["rows"], re.S).group(0) for i in (pull, plain)}
        self.assertIn(f'<a class="icon-btn" href="{url}" target="_blank" rel="noopener noreferrer" '
                      'aria-label="Open the pull request on GitHub"', line[pull])
        self.assertNotIn("<a class=\"icon-btn\"", line[plain])


class TheSnooze(ReaderCase):
    """sd:1896: Snooze on every Today row, the snoozed rows apart with Unsnooze, and the fixed times."""

    def test_snooze_posts_the_row_and_reads_now_again(self):
        out = self.run_page("""ANSWER = (path, body) => path === '/api/now' ? [200, DOC0] : [200, { key: 'k', until: body.until }];
R.types = REG.filter(c => c.id.endsWith('.snooze')).map(c => c.on);
FIELD_VALUES.until = 'snooze-week'; shellRun(cmd('repo.snooze'), C.get('ahead:pushy:1')); await flush();
R.ahead = Date.parse(OUT.posts[0][1].until) - Date.now();""")
        self.assertEqual(out["R"]["types"], ["item group", "flaky group", "job", "pull request", "repo", "sessions", "collector",
                                            "work item", "report", "mail thread", "flaky head"])
        # The row's own fingerprint goes with it, so the snooze holds only while the row reads the same.
        seen = next(row["seen"] for row in self.doc["rows"] if row["id"] == "ahead:pushy:1")
        self.assertEqual([(path, body["page"], body["row"], body["seen"]) for path, body, _ in out["posts"]],
                         [("/api/snooze", "today", "ahead:pushy:1", seen)])
        self.assertTrue(7 * 864e5 - 60e3 <= out["R"]["ahead"] <= 7 * 864e5, out["R"]["ahead"])
        self.assertEqual(out["gets"], ["/api/now", "/api/now"])

    def test_a_snoozed_row_is_drawn_apart_with_unsnooze(self):
        row = self.doc["needs"].pop()
        self.doc["snoozed"] = [{**row, "until": "2026-09-07T08:00:00+00:00"}]
        out = self.run_page("""R.rows = ELS.rows.html; R.snoozed = ELS.snoozed.html; R.obj = C.get('snoozed:ahead:pushy:1');
R.primary = REG.filter(c => c.on === 'snoozed row').map(c => c.id);""")
        self.assertNotIn("pushy", out["R"]["rows"])
        self.assertIn("Snoozed · 1", out["R"]["snoozed"])
        self.assertIn(row["what"], out["R"]["snoozed"])
        self.assertEqual(out["R"]["obj"], {"id": "snoozed:ahead:pushy:1", "type": "snoozed row", "label": row["what"],
                                           "row": "ahead:pushy:1", "until": "2026-09-07T08:00:00+00:00", "seen": row["seen"]})
        self.assertEqual(out["R"]["primary"], ["snoozed row.unsnooze"])

    ITEM = {"id": "item:7", "kind": "item", "item": 7, "what": "#7 Write the brief", "detail": "in_progress · due 2026-09-05",
            "state": "in_progress", "due": "2026-09-05", "seen": "0123456789abcdef", "rank": 5, "band": "queued", "lamp": "",
            "source": "tasks"}

    def test_a_work_item_is_a_row_that_snoozes_under_its_own_key(self):
        """sd:3271: the items `sd today` lists get the same control as a Now row, keyed today:item:<id>."""
        self.doc["needs"].append(self.ITEM)
        out = self.run_page("""ANSWER = (path, body) => path === '/api/now' ? [200, DOC0] : [200, { key: 'k', until: body.until }];
R.rows = ELS.rows.html; R.obj = C.get('item:7');
FIELD_VALUES.until = 'snooze-hour'; shellRun(cmd('work item.snooze'), C.get('item:7')); await flush();""")
        self.assertIn('<tr data-id="item:7" data-src="">', out["R"]["rows"])
        self.assertIn("in_progress · due 2026-09-05", out["R"]["rows"])
        self.assertEqual((out["R"]["obj"]["type"], out["R"]["obj"]["seen"]), ("work item", "0123456789abcdef"))
        self.assertEqual([(path, body["page"], body["row"], body["seen"]) for path, body, _ in out["posts"]],
                         [("/api/snooze", "today", "item:7", "0123456789abcdef")])
        self.assertEqual(out["gets"], ["/api/now", "/api/now"])

    # A press on a row of a table: the click its tbody listens for, as the browser's closest() answers it.
    PRESS = "const press = (t, id) => ELS[t].listeners.click[0]({ target: { closest: s => s === 'tr[data-id]' ? { dataset: { id } } : null } });\n"
    SPY = "const SEL = []; C.select = id => SEL.push(id); R.panes = []; window.shell.openPane = p => R.panes.push(p);\n"

    def test_a_work_row_selects_into_the_details_and_a_read_keeps_it(self):
        """sd:5654: a press on a work row selects it: ?row=, the shell's selection, its Details and the pane."""
        self.doc["needs"].append({**self.ITEM, "item_kind": "task"})
        out = self.run_page(self.PRESS + self.SPY + """press('rows', 'item:7'); await flush();
R.details = ELS.details.html; R.row = location.search; R.sel = SEL.slice();
refresh(); await flush(); R.again = ELS.details.html; R.now = selected(); R.row2 = location.search;""")
        details = out["R"]["details"]
        self.assertIn('<h2><a href="/tasks?row=7">#7 Write the brief</a></h2>', details)
        for fact in ("Work item · queued", "<dt>Status</dt><dd>in_progress</dd>", "<dt>Due</dt><dd>2026-09-05</dd>",
                     "<dt>Kind</dt><dd>task</dd>", "<dt>Id</dt><dd>sd:7</dd>", '<h3>Act</h3><div class="bar"></div>'):
            self.assertIn(fact, details)
        self.assertEqual((out["R"]["row"], out["R"]["sel"], out["R"]["panes"]), ("?row=item%3A7", ["item:7"], ["tab-details"]))
        self.assertIn("#7 Write the brief", out["R"]["again"])
        self.assertEqual((out["R"]["now"], out["R"]["row2"]), (["item:7"], "?row=item%3A7"))

    def test_a_part_that_failed_is_said_under_the_list_never_an_empty_one(self):
        self.doc["work_error"], self.doc["briefs_error"] = "database is locked", "no routine_briefs"
        out = self.run_page("R.unread = [ELS.unread.hidden, ELS.unread.textContent];")
        self.assertEqual(out["R"]["unread"], [False, "Work could not be read: database is locked. Read again tries again."
                                                     " Briefs could not be read: no routine_briefs. Read again tries again."])

    def test_nothing_to_do_says_so(self):
        self.doc["needs"] = []
        out = self.run_page("R.rows = ELS.rows.html; R.unread = ELS.unread.hidden;")
        self.assertIn("Nothing needs you: every source was read and raised no row.", out["R"]["rows"])
        self.assertTrue(out["R"]["unread"])

    def test_a_snoozed_work_item_waits_under_snoozed_and_a_failed_read_retires_the_rows(self):
        self.doc["snoozed"] = [{**self.ITEM, "until": "2026-09-07T08:00:00+00:00"}]
        out = self.run_page("""R.snoozed = ELS.snoozed.html; R.obj = C.get('snoozed:item:7');""")
        self.assertIn("#7 Write the brief", out["R"]["snoozed"])
        self.assertEqual((out["R"]["obj"]["type"], out["R"]["obj"]["row"], out["R"]["obj"]["seen"]),
                         ("snoozed row", "item:7", "0123456789abcdef"))
        self.doc["needs"].append(self.ITEM)
        out = self.run_page("""ANSWER = () => [500, { error: 'fleet collection was stopped at its budget' }]; refresh(); await flush();
R.rows = ELS.rows.html; R.type = C.get('item:7').type; R.unread = ELS.unread.hidden;""")
        self.assertIn("Needs you could not be read: fleet collection was stopped at its budget", out["R"]["rows"])
        self.assertEqual((out["R"]["type"], out["R"]["unread"]), ("not listed", True))

    def test_a_snooze_read_that_failed_is_a_partial_read(self):
        self.doc["snooze_error"] = "database is locked"
        out = self.run_page("")
        self.assertEqual(out["states"][-1], {"kind": "partial", "source": "/api/now",
                                             "text": "Snoozes were not read: database is locked. Every row shows."})

    def test_the_fixed_times_and_how_a_toast_says_them(self):
        out = self.run_page("""const S = window.snooze, at = (h, m) => new Date(2026, 9, 10, h, m);
const show = d => [d.getDate(), d.getHours(), d.getMinutes()];
R.early = show(S.CHOICES[0].until(at(7, 59))); R.late = show(S.CHOICES[0].until(at(8, 0)));
R.hour = show(S.CHOICES[1].until(at(23, 30)));
R.today = S.when(at(8, 0), at(7, 0)); R.other = S.when(new Date(2026, 9, 11, 8, 0), at(7, 0));
R.keys = S.CHOICES.map(c => [c.id, c.label]);""")
        self.assertEqual(out["R"]["early"], [10, 8, 0])
        self.assertEqual(out["R"]["late"], [11, 8, 0])
        self.assertEqual(out["R"]["hour"], [11, 0, 30])
        self.assertEqual((out["R"]["today"], out["R"]["other"]), ("08:00", "Sun Oct 11 08:00"))
        self.assertEqual(out["R"]["keys"], [["snooze", "The next 08:00"], ["snooze-hour", "One hour from now"],
                                            ["snooze-week", "One week from now"]])

    def test_snooze_is_one_command_per_row_that_asks_which_time(self):
        """sd:3450: one Snooze in the row's menu, key z, whose dialog offers the three times; the next 08:00 is the default."""
        out = self.run_page("""ANSWER = (path, body) => path === '/api/now' ? [200, DOC0] : [200, { key: 'k', until: body.until }];
R.repo = REG.filter(c => c.on === 'repo').map(c => [c.id, c.label, c.key]);
R.fields = cmd('repo.snooze').fields(C.get('ahead:pushy:1'));
shellRun(cmd('repo.snooze'), C.get('ahead:pushy:1')); await flush();
R.until = new Date(OUT.posts[0][1].until); R.at8 = [R.until.getHours(), R.until.getMinutes()];
OUT.posts = []; FIELD_VALUES.until = 'snooze-hour'; const t0 = Date.now();
shellRun(cmd('repo.snooze'), C.get('ahead:pushy:1')); await flush();
R.hour = Date.parse(OUT.posts[0][1].until) - t0;""")
        # sd:5652: a repo row's other command is its Fix, on its own key.
        self.assertEqual(out["R"]["repo"], [["repo.fix", "Fix", "f"], ["repo.snooze", "Snooze", "z"]])
        self.assertEqual(out["R"]["fields"], [{"name": "until", "label": "Snooze until", "options": [
            {"value": "snooze", "label": "The next 08:00"}, {"value": "snooze-hour", "label": "One hour from now"},
            {"value": "snooze-week", "label": "One week from now"}]}])
        self.assertEqual(out["confirms"], ["repo.snooze", "repo.snooze"])
        self.assertEqual(out["R"]["at8"], [8, 0])
        self.assertTrue(3590e3 <= out["R"]["hour"] <= 3610e3, out["R"]["hour"])


DECISIONS_JS = (V2 / "static" / "decisions.js").read_text(encoding="utf-8")


class TheBadge(ReaderCase):
    """Today's badge (shell.attention) is the Now read's (sd:3446); a decision waiting counts on Decide's since sd:5781.

    decisions.js runs beside today.js here, as today.html loaded it until sd:5781, so a decision waits while Now reads:
    Today's badge stays the Now document's. Decide's badge is TheDecideBadge."""

    DECISION = {"note": 7, "item": 3012, "title": "Dashboard v2", "repo": "/repos/system", "asked": "2026-10-08T21:00:00Z",
                "question": "Which shape?", "options": ["Option lines", "Free text"], "revision": "a" * 64}

    def run_badge(self, body, decisions, keep_job=False):
        self.doc["needs"] = [row for row in self.doc["needs"] if keep_job or row["kind"] != "job"]
        self.assertEqual([row["band"] for row in self.doc["needs"]], ["broken", "look"] if keep_job else ["look"])
        now = dict(self.doc, attention=now_screen._attention(self.doc["needs"]))
        script = (STAND_IN + MARKUP_JS + "\nconst mk = window.markup.html;\n" + SHELL + PAGE + SHELL_MORE + READ_SHELL
                  # As shell.js does: a page passes its badge, or sets window.PAGE_ATTENTION and passes nothing.
                  + "\nwindow.shell.attention = a => { if (a) window.PAGE_ATTENTION = a; OUT.attention.push(window.PAGE_ATTENTION); };"
                  + f"\nconst DOC0 = {json.dumps(now)}, DECISIONS = {json.dumps({'decisions': decisions})};\n"
                  + "ANSWER = (path, body) => path === '/api/now' ? [200, DOC0]"
                    " : path === '/api/decisions' ? [200, DECISIONS]"
                    " : [200, { revision: 'b'.repeat(64), ruling: { id: 99, kind: 'decision', body: 'x' } }];\n"
                  + SNOOZE_JS + COMMANDS_JS + TASK_DETAILS_JS + TODAY_JS + "\n" + DECISIONS_JS
                  + "\nvar R = {};\nconst click = (note, i) => ELS.decisions.listeners.click[0]({ target: { closest: s => s === 'button[data-option]'"
                    " ? { dataset: { note: String(note), option: String(i) }, closest: () => null } : null } });\n"
                  + "(async () => { try {\n(DOC_LISTENERS.DOMContentLoaded || []).forEach(f => f());\nawait flush();\n"
                  + body + "\n} catch (e) { OUT.error = String(e) + ' ' + e.stack; } })();\n"
                  + "function run() { OUT.R = R; return JSON.stringify(OUT); }\n")
        result = subprocess.run([OSASCRIPT, "-l", "JavaScript", "-e", script],
                                capture_output=True, text=True, timeout=60, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        out = json.loads(result.stdout)
        self.assertIsNone(out["error"])
        return out

    def test_a_waiting_decision_no_longer_counts_on_today(self):
        out = self.run_badge("click(7, 0); await flush();", [self.DECISION])
        self.assertEqual(out["posts"][0][0], "/api/decisions/answer")
        self.assertEqual({tuple(sorted(a)) for a in out["attention"]}, {("n", "state", "what")})
        self.assertEqual(out["attention"][-1], {"state": "caution", "n": 1, "what": "rows to look at"})

    def test_an_unread_now_is_unknown_while_a_decision_waits(self):
        # sd:3446: the badge is the Now read's, so a failed read is unknown here as on every other page's rail.
        out = self.run_badge("ANSWER0 = ANSWER; ANSWER = (p, b) => p === '/api/now' ? [500, { error: 'stopped' }] : ANSWER0(p, b);"
                             " refresh(); await flush();", [self.DECISION])
        self.assertEqual(out["attention"][-1], {"state": "unknown", "n": 0, "what": "rows"})

    def test_a_warning_row_outranks_a_waiting_decision(self):
        """A guard: the badge counts its loudest state, and a decision is caution, as its ▲ in the section says."""
        out = self.run_badge("", [self.DECISION], keep_job=True)
        self.assertEqual(out["attention"][-1], {"state": "warning", "n": 1, "what": "broken rows"})


DECIDE_JS = (V2 / "static" / "decide.js").read_text(encoding="utf-8")


class TheDecideBadge(unittest.TestCase):
    """sd:5781: Decide's badge is what waits on its two tabs: the decisions still open and Scoring's open questions.

    decide.js and decisions.js run as decide.html loads them; review.js's part is its window.decideCount('scoring', n)
    call (test_v2_review runs review.js). An unread tab (n null) hides its count and adds nothing."""

    def run_page(self, body):
        decision = {**TheBadge.DECISION, "recommended": None}
        script = (STAND_IN + MARKUP_JS + "\nconst mk = window.markup.html;\n" + SHELL
                  + "\nwindow.shell.attention = a => OUT.attention.push(a);"
                  + f"\nvar LIST = {json.dumps({'decisions': [decision, {**decision, 'note': 8}]})};\n"
                  + "ANSWER = (path, body) => path === '/api/decisions' ? [200, LIST]"
                    " : [200, { revision: 'b'.repeat(64), ruling: { id: 99, kind: 'decision', body: 'x' } }];\n"
                  + DECIDE_JS + "\n" + DECISIONS_JS
                  + "\nvar R = {};\nconst click = (note, i) => ELS.decisions.listeners.click[0]({ target: { closest: s => s === 'button[data-option]'"
                    " ? { dataset: { note: String(note), option: String(i) }, closest: () => null } : null } });\n"
                  + "(async () => { try {\n(DOC_LISTENERS.DOMContentLoaded || []).forEach(f => f());\nawait flush();\n"
                  + body + "\n} catch (e) { OUT.error = String(e) + ' ' + e.stack; } })();\n"
                  + "function run() { OUT.R = R; return JSON.stringify(OUT); }\n")
        result = subprocess.run([OSASCRIPT, "-l", "JavaScript", "-e", script],
                                capture_output=True, text=True, timeout=60, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        out = json.loads(result.stdout)
        self.assertIsNone(out["error"])
        return out

    def test_the_badge_adds_the_decisions_waiting_and_the_questions_to_score(self):
        out = self.run_page("R.read = OUT.attention.slice(-1)[0]; window.decideCount('scoring', 3); R.both = OUT.attention.slice(-1)[0];"
                            " click(7, 0); await flush(); R.ruled = OUT.attention.slice(-1)[0];"
                            " window.decideCount('scoring', null); R.unread = OUT.attention.slice(-1)[0];"
                            " click(8, 1); await flush(); R.none = OUT.attention.slice(-1)[0];")
        self.assertEqual(out["R"]["read"], {"state": "caution", "n": 2, "what": "decisions waiting"})
        self.assertEqual(out["R"]["both"], {"state": "caution", "n": 5, "what": "decisions waiting and questions open"})
        self.assertEqual(out["R"]["ruled"], {"state": "caution", "n": 4, "what": "decisions waiting and questions open"})
        self.assertEqual(out["R"]["unread"], {"state": "caution", "n": 1, "what": "decisions waiting"})
        self.assertEqual(out["R"]["none"], {"state": "ok", "n": 0, "what": "decisions or questions"})
        self.assertEqual((out["counts"]["decisions"], out["counts"]["scoring"]), (0, None))


class TheJobRow(ReaderCase):
    """sd:3449: a job row carries the commands Management and Activity give a job, from the one declaration in commands.js."""

    def test_a_job_row_runs_its_log_and_its_launchd_record(self):
        out = self.run_page("""const o = C.get('job:nightly-sync:7'), log = cmd('jobs.log'), print = cmd('jobs.print');
R.got = [log.on, log.executes, log.cli(o), print.on, print.executes, print.when(o), print.cli(o), !!print.primary(o)];
const was = ANSWER; ANSWER = (p, b) => p === '/api/actions/run' ? [200, { action: b.action, label: 'Show log', line: `local-cron-jobs/cron-jobs.sh logs ${b.job}`, exit: 0, output: 'the log <tail>', cut: false }] : was(p, b);
shellRun(log, o); shellRun(print, o); await flush();""")
        self.assertEqual(out["R"]["got"], ["job", True, "local-cron-jobs/cron-jobs.sh logs nightly-sync",
                                           "job", True, True, "launchctl print fixture/nightly-sync", False])
        # sd:5886: both run on the server from the job's name; the row's service is shown, never sent.
        self.assertEqual([post[:2] for post in out["posts"]], [["/api/actions/run", {"action": "job.log", "job": "nightly-sync"}],
                                                               ["/api/actions/run", {"action": "job.print", "job": "nightly-sync"}]])

    def test_after_exit_127_print_is_the_rows_act(self):
        self.doc["needs"][0]["exit"] = 127
        out = self.run_page("R.print = !!cmd('jobs.print').primary(C.get('job:nightly-sync:7'));")
        self.assertTrue(out["R"]["print"])


class TheRetry(ReaderCase):
    """sd:3386: a failed job's Retry posts /api/jobs/<job>/retry with the row's revision, as Management and Activity do."""

    def test_retry_posts_the_job_route_with_the_rows_revision_and_reads_now_again(self):
        row = self.doc["needs"][0]
        out = self.run_page("""ANSWER = (path, body) => path === '/api/now' ? [200, DOC0] : [200, { request: { status: 'accepted', message: 'launchctl accepted the request' } }];
R.primary = cmd('jobs.retry').primary(C.get('job:nightly-sync:7'));
shellRun(cmd('jobs.retry'), C.get('job:nightly-sync:7')); await flush();
R.cli = cmd('jobs.retry').cli(C.get('job:nightly-sync:7')); R.sends = cmd('jobs.retry').sends(C.get('job:nightly-sync:7'));""")
        self.assertEqual(out["posts"], [["/api/jobs/nightly-sync/retry", {"revision": row["revision"]}, 64]])
        self.assertEqual(out["toasts"][-1]["msg"], "Retry started · nightly-sync")
        self.assertEqual(out["gets"], ["/api/now", "/api/now"])
        self.assertEqual((out["R"]["primary"], out["R"]["cli"], out["R"]["sends"]),
                         (True, "sd jobs retry nightly-sync", "launchctl kickstart fixture/nightly-sync"))

    def test_exit_127_keeps_retry_on_and_asks_first_naming_the_exit(self):
        """sd:3392, ruled 2026-09-30: the exit is the last run's, and a path fixed outside the dashboard needs Retry to test it."""
        self.doc["needs"][0]["exit"] = 127
        out = self.run_page("""ANSWER = (path, body) => path === '/api/now' ? [200, DOC0] : [200, { request: { status: 'accepted', message: 'launchctl accepted the request' } }];
const c = cmd('jobs.retry'), o = C.get('job:nightly-sync:7');
R.on = c.when(o); R.primary = !!c.primary(o); R.ask = !!c.askFirst(o); R.says = c.consequence(o);
shellRun(c, o); await flush();""")
        self.assertEqual((out["R"]["on"], out["R"]["primary"], out["R"]["ask"]), (True, False, True))
        self.assertIn("exit 127", out["R"]["says"])
        self.assertEqual(out["confirms"], ["jobs.retry"])
        self.assertEqual([p for p, _, _ in out["posts"]], ["/api/jobs/nightly-sync/retry"])
        self.assertEqual(out["toasts"][-1]["msg"], "Retry started · nightly-sync")

    def test_another_exit_retries_without_asking(self):
        out = self.run_page("""ANSWER = (path, body) => path === '/api/now' ? [200, DOC0] : [200, { request: { status: 'accepted', message: 'launchctl accepted the request' } }];
R.ask = !!cmd('jobs.retry').askFirst(C.get('job:nightly-sync:7')); shellRun(cmd('jobs.retry'), C.get('job:nightly-sync:7')); await flush();""")
        self.assertEqual((out["R"]["ask"], out["confirms"]), (False, []))

    def test_a_retry_the_read_refuses_is_off_with_the_reason(self):
        self.doc["needs"][0]["capability"] = {"allowed": False, "reason": "an unresolved restore prevents starting jobs"}
        out = self.run_page("shellRun(cmd('jobs.retry'), C.get('job:nightly-sync:7')); await flush();")
        self.assertEqual(out["posts"], [])
        self.assertEqual(out["toasts"][-1]["msg"], "off: an unresolved restore prevents starting jobs")

    def test_a_stale_retry_writes_nothing_says_so_and_reads_now_again(self):
        out = self.run_page("""ANSWER = path => path === '/api/now' ? [200, DOC0] : [409, { error: 'state changed; refresh before trying again' }];
shellRun(cmd('jobs.retry'), C.get('job:nightly-sync:7')); await flush();""")
        self.assertEqual(len(out["posts"]), 1)
        self.assertEqual(out["toasts"][-1]["msg"], "nightly-sync failed with exit 7 not changed: state changed; refresh before trying again")
        self.assertEqual(out["gets"], ["/api/now", "/api/now"])

    def test_a_retry_with_no_answer_is_unknown_not_refused(self):
        out = self.run_page("""ANSWER = path => path === '/api/now' ? [200, DOC0] : Promise.reject(new TypeError('Load failed'));
shellRun(cmd('jobs.retry'), C.get('job:nightly-sync:7')); await flush();""")
        self.assertEqual(out["toasts"][-1]["msg"], "nightly-sync failed with exit 7 not changed: no answer (Load failed), so whether"
                         " launchd started it is not known; Today is read again")
        self.assertEqual(out["gets"], ["/api/now", "/api/now"])

    def test_a_retry_shows_launchctls_answer_when_it_did_not_accept(self):
        """sd:3386, as sd:3388 words Management's: a 200 carries launchctl's answer; only `accepted` started a retry."""
        cases = {
            "{ request: { status: 'failed', message: 'launchctl refused the request (exit 3)' } }":
                "nightly-sync not retried: launchctl refused the request (exit 3)",
            "{ request: { status: 'unknown', message: 'launchctl timed out; the request outcome is unknown' } }":
                "Not known whether nightly-sync was retried: launchctl timed out; the request outcome is unknown",
            "{}": "Not known whether nightly-sync was retried: the answer named no outcome",
        }
        for answer, said in cases.items():
            with self.subTest(said=said):
                out = self.run_page(f"""ANSWER = (path, body) => path === '/api/now' ? [200, DOC0] : [200, {answer}];
shellRun(cmd('jobs.retry'), C.get('job:nightly-sync:7')); await flush();""")
                self.assertEqual(out["toasts"][-1]["msg"], said)
                self.assertEqual(out["gets"], ["/api/now", "/api/now"])


# The row's act, as shell.js's rowActions picks it: the first live command on the row's type that is primary for it.
ACT = """const act = id => { const o = C.get(id), on = REG.filter(c => c.on === o.type && (!c.when || c.when(o) === true));
  const p = on.find(c => c.primary && c.primary(o)); return p ? [p.id, p.label, p.icon] : null; };
window.open = u => { OUT.opened = u; };
const PR = { rank: 4, kind: 'pr', id: 'pr:example/project#4:4', what: 'example/project#4 open', detail: 'Pull 4', source: 'prs', band: 'queued', lamp: 'pulls' };
const DARK = s => ({ rank: 0, kind: 'dark', id: `dark:${s}`, what: `${s} could not be read`, detail: 'no reason', source: s, band: 'broken', lamp: s === 'prs' ? 'pulls' : s });
"""


# A row's icon actions, as shell.js's rowActions draws them with { icons: true }: every live command that has an icon.
ICONS = """const icons = id => { const o = C.get(id); return REG.filter(c => c.on === o.type && (!c.when || c.when(o) === true) && c.icon).map(c => [c.id, c.label, c.icon]); };
OUT.drawn = {}; C.rowActions = (id, opt) => { OUT.drawn[id] = !!(opt && opt.icons); return mk``; };
"""


class TheNowFix(ReaderCase):
    """sd:6709: one Fix, commands.fix's label and icon, always claude in herdr on the problem. A row whose act is
    something else shows that command under its own name: a job's Run now, a pull request's Open on GitHub, the pull
    request collector's Re-run collector. A broken row shows its icon actions, Fix among them."""

    def test_every_fix_is_the_one_fix_icon(self):
        out = self.run_page("R.fix = REG.filter(c => c.label === 'Fix').map(c => [c.id, c.icon, c.key]);")
        self.assertTrue(out["R"]["fix"])
        self.assertEqual({(icon, key) for _, icon, key in out["R"]["fix"]}, {("bot", "f")}, out["R"]["fix"])

    def test_a_job_and_a_pull_request_show_their_own_command_not_fix(self):
        out = self.run_page(ACT + """DOC0.needs.push(PR, DARK('prs')); refresh(); await flush();
R.fix = REG.filter(c => c.label === 'Fix').map(c => c.on);
R.acts = ['job:nightly-sync:7', 'pr:example/project#4:4', 'dark:prs', 'ahead:pushy:1'].map(act);""")
        self.assertNotIn("job", out["R"]["fix"])
        self.assertNotIn("pull request", out["R"]["fix"])
        self.assertEqual(out["R"]["acts"], [["jobs.retry", "Run now", "play"], ["pr.open", "Open on GitHub", "arrow-up-right"],
                                            ["collector.sync", "Re-run collector", "rotate-ccw"], ["repo.fix", "Fix", "bot"]])

    def test_a_broken_row_shows_fix_among_its_icon_actions(self):
        out = self.run_page(ICONS + ACT + """DOC0.needs.push(DARK('jobs'), DARK('prs')); refresh(); await flush();
R.drawn = OUT.drawn; R.jobs = icons('dark:jobs'); R.prs = icons('dark:prs');""")
        self.assertTrue(out["R"]["drawn"]["dark:jobs"], "a broken row draws its icon actions, not the primary alone")
        self.assertIn(["collector.fix", "Fix", "bot"], out["R"]["jobs"])
        self.assertNotIn("collector.fix", [c for c, _, _ in out["R"]["prs"]])
        self.assertIn(["collector.sync", "Re-run collector", "rotate-ccw"], out["R"]["prs"])

    def test_after_exit_127_print_stays_the_act(self):
        self.doc["needs"][0]["exit"] = 127
        out = self.run_page(ACT + "R.act = act('job:nightly-sync:7');")
        self.assertEqual(out["R"]["act"], ["jobs.print", "Print", "terminal"])

    def test_run_now_on_the_dark_pull_request_collector_runs_its_sync(self):
        out = self.run_page(ACT + """ANSWER = (path, body) => path === '/api/shadow/state' ? [200, { running: false, trackers: [] }] : path === '/api/now' ? [200, DOC0] : [202, { started: true }];
DOC0.needs.push(DARK('prs')); refresh(); await flush();
shellRun(cmd('collector.sync'), C.get('dark:prs')); await flush();""")
        self.assertEqual([p for p, _, _ in out["posts"]], ["/api/shadow/sync"])
        self.assertIn("/api/shadow/state", out["gets"])
        self.assertEqual(out["toasts"][-1]["msg"], "Shadow sync started · Today reads again when it ends")

    def test_fix_on_a_repo_row_asks_first_and_posts_the_row_for_a_herdr_session(self):
        out = self.run_page("""ANSWER = (path, body) => path === '/api/now/fix' ? [200, { fix: { pane: 'w9:p1', checkout: '/repos/pushy', workspace: 'fix-ahead-pushy' } }] : [200, DOC0];
const c = cmd('repo.fix'), o = C.get('ahead:pushy:1'); R.says = c.consequence(o);
shellRun(c, o); await flush();""")
        self.assertEqual(out["confirms"], ["repo.fix"])
        self.assertIn("herdr", out["R"]["says"])
        self.assertEqual(out["posts"], [["/api/now/fix", {"row": "ahead:pushy:1"}, 64]])
        self.assertEqual(out["toasts"][-1]["msg"], "Fix started · pushy has 1 unpushed commit · claude in herdr workspace fix-ahead-pushy, pane w9:p1")
        self.assertEqual(out["gets"], ["/api/now", "/api/now"])

    def test_fix_on_another_dark_collector_posts_the_row_and_a_stale_one_reads_now_again(self):
        out = self.run_page("""ANSWER = (path, body) => path === '/api/now/fix' ? [409, { error: 'the jobs collector reads now; read Now again', reload: true }] : [200, DOC0];
DOC0.needs.push({ rank: 0, kind: 'dark', id: 'dark:jobs', what: 'jobs could not be read', detail: 'no reason', source: 'jobs', band: 'broken', lamp: 'jobs' });
refresh(); await flush(); shellRun(cmd('collector.fix'), C.get('dark:jobs')); await flush();""")
        self.assertEqual(out["posts"], [["/api/now/fix", {"row": "dark:jobs"}, 64]])
        self.assertEqual(out["toasts"][-1]["msg"], "jobs could not be read not changed: the jobs collector reads now; read Now again")
        self.assertEqual(out["gets"][-1], "/api/now")


class TheFlakyHeads(ReaderCase):
    """pack sd:7663: the flaky heads moved from Metrics to Needs you: a look group on the Lane lamp that opens to each head;
    a head's Details name its repo, head, failures and pass, with Open item and Snooze; the group snoozes all."""

    def setUp(self):
        super().setUp()
        from test_v2_metrics import GATE, lane_run

        for item, head, repository in ((9, "1" * 40, "example/alpha"), (10, "2" * 40, "example/beta")):
            lane_run(self.connection, f"f{item}", "failed", "2026-09-04T10:00:00Z", expected=head, reason=GATE, item=item,
                     repository=repository)
            lane_run(self.connection, f"p{item}", "merged", "2026-09-04T14:00:00Z", head=head, item=item,
                     repository=repository)
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        self.doc = now_screen.document(self.connection, now=NOW, jobs=JobsBackend(root.name, jobs=[("nightly-sync", "failed", 7, None)]),
                                       fleet=lambda area: fleet_document(area, repos=[repo("pushy", ahead=1)]))

    ALPHA = "flaky:example/alpha:" + "1" * 40

    def test_the_group_opens_to_each_head_and_a_head_shows_its_details(self):
        out = self.run_page(TheList.IDS + TheList.CLICK + f"""R.closed = ids();
click('rows', 'toggle', 'group:flaky'); R.open = ids();
R.types = [C.get('group:flaky').type, C.get('{self.ALPHA}').type];
ROWS_EL.listeners.click[0]({{ target: {{ closest: s => s === 'tr[data-id]' ? {{ dataset: {{ id: '{self.ALPHA}' }} }} : null }} }});
R.details = ELS.details.html;
R.cmds = REG.filter(c => c.on === 'flaky head' || c.on === 'flaky group').map(c => c.id);""")
        r = out["R"]
        self.assertEqual(r["closed"], ["job:nightly-sync:7", "group:flaky", "ahead:pushy:1"])
        self.assertEqual(r["open"], ["job:nightly-sync:7", "group:flaky", self.ALPHA, "flaky:example/beta:" + "2" * 40,
                                     "ahead:pushy:1"])
        self.assertEqual(r["types"], ["flaky group", "flaky head"])
        for fact in ("Flaky head · caution", '<a href="/tasks?row=9">sd:9 Item 9</a>', "<dt>Repo</dt><dd>example/alpha</dd>",
                     "<dt>Head</dt><dd>111111111111</dd>", "<dt>Gate failed</dt><dd>2026-09-04 10:00 UTC</dd>",
                     "<dt>Passed</dt><dd>2026-09-04 14:00 UTC · merged</dd>"):
            self.assertIn(fact, r["details"])
        self.assertEqual(sorted(r["cmds"]), ["flaky head.snooze", "today.flaky.open", "today.flaky.snooze"])

    def test_the_lane_lamp_counts_blocked_entries_apart_from_flaky_heads(self):
        out = self.run_page("""const lane = El('div'), val = El('span'); lane.dataset = { src: 'lane' }; lane.querySelector = () => val;
document.querySelectorAll = sel => sel === '.cell[data-src]' ? [lane] : [];
refresh(); await flush(); R.lane = [lane.dataset.state, val.html];""")
        state, text = out["R"]["lane"]
        self.assertEqual(state, "caution")
        self.assertIn("<b>0</b> blocked", text)
        self.assertIn("2 flaky", text)

    def test_snooze_all_writes_one_snooze_per_head(self):
        out = self.run_page("""REG.find(c => c.id === 'today.flaky.snooze').run(C.get('group:flaky')); await flush();""")
        self.assertEqual(sorted(body["row"] for _, body, _ in out["posts"]),
                         [self.ALPHA, "flaky:example/beta:" + "2" * 40])
