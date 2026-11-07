// A task's Details, commands, writes and dialogs (sd:5635): the one renderer for a row of /api/tasks's shape
// (tasks_screen._rows) and its reading, /api/tasks/<id>. Tasks draws it for its selected card or row, and the Home and realm
// place pages' Tasks views (places.js) draw it in their Details pane from the same rows, read for the place alone, so a task
// reads and acts the same on each. Moved from tasks.js with its "build:" notes. It loads after commands.js and before the
// page script.
//   shape(r)        an /api/tasks row as the page's task
//   attach(host)    registers the commands once, listens for the Details fields and gives the API below
//     details   the Details element          rows      the read's tasks      byKey  a task by key (default: one of rows)
//     selected  the selected task's key      read      the read's stamp, for the due rule
//     render    redraws the page's rows and the Details after a write folds into its row
//     reread    reads the rows again after a landed write; load reads them after a refused one and answers whether it read
//     select    selects a task (File issue)  view      'list', 'board' or 'matrix', which a row's primary move follows;
//               'place' on a place page, where Done is every row's primary act, its acknowledge (sd:5752)
//     got       a Details reading answered (Tasks' linked item, sd:5462): true selects it
//     draftFirst  a task whose first action is Draft email, not its next status (HOA's followups, sd:5671)
//     empty, note, foot, more   the page's lines: nothing selected, under the title, the footer, a link to Tasks
//     landed    marks a landed write's card
//     lead      whether the store takes a lead (tasks_screen._has_lead, pack sd:5648): the Lead field shows and writes
(() => {
  const { html, put, plural } = window.markup;
  // build: today is the viewer's day, not the mockup's observation day.
  const DAY = 86400000;
  const now0 = new Date(), TODAY = new Date(now0.getFullYear(), now0.getMonth(), now0.getDate());
  const iso = d => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
  // Calendar days, not 24-hour steps: across a DST change a fixed step lands on the wrong local date (review, PR #46).
  const plus = n => iso(new Date(TODAY.getFullYear(), TODAY.getMonth(), TODAY.getDate() + n));
  const MON = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
  const fmt = s => { const d = new Date(s + 'T00:00'); return `${MON[d.getMonth()]} ${d.getDate()}`; };

  // Statuses: sd_db.workflow.TASK_STATUSES, in this order. Keys 1–5 follow it.
  const STATUSES = [['planning', 'Planning'], ['ready', 'Ready'], ['in_progress', 'In progress'], ['blocked', 'Blocked'], ['done', 'Done']];
  const SLABEL = Object.fromEntries(STATUSES);
  const slabel = s => SLABEL[s] || String(s || '').replace(/_/g, ' '); // build: a message item's ready_to_send has no column
  const QUADS = [
    ['do', 'Do', true, true, 'urgent · important'],
    ['schedule', 'Schedule', false, true, 'not urgent · important'],
    ['delegate', 'Delegate', true, false, 'urgent · not important'],
    ['drop', 'Drop', false, false, 'not urgent · not important'],
  ];

  // ---------- Data (build) ----------
  // /api/tasks rows become the reference's task shape: p is priority, repo the short label v1 shows, key the id.
  const shape = r => ({ id: r.id, key: String(r.id), title: r.title, repo: r.repo || 'no repo', repo_path: r.repo_path, p: r.priority, due: r.due,
    status: r.status, since: r.status_since, kind: r.kind, urgent: !!r.urgent, urgentOtherwise: !!r.urgent_otherwise, assignment: r.assignment, age: r.age, recurrence: r.recurrence, anchor: r.recurrence_anchor ?? null, lead: r.recurrence_lead ?? null, opensOn: r.opens_on ?? null, scheduled: !!r.scheduled, nextDue: r.next_due ?? null, revision: r.revision, allowed: r.allowed, edit: r.edit, run: r.run, parked: r.parked ?? null,
    base: r.base ?? null, stage: r.stage ?? null, score: r.score ?? null, area: r.area ?? null, area_set: !!r.area_set, realm: r.realm ?? null, issueTargets: r.issue_targets ?? null, issueLink: r.issue_link ?? null, real: true });
  const label = t => `#${t.id}`;

  function attach(host) {
    const { ICON, toast } = window.shell;
    const byKey = host.byKey || (k => host.rows().find(t => t.key === k)), selected = host.selected, view = host.view || (() => 'list');
    const draftFirst = t => !!t && !!host.draftFirst?.(t);
    // A write readback is workflow.item_state: fold its item into the row, so the row updates where it is.
    function absorb(key, state) {
      const t = byKey(key); if (!t || !state?.item) return;
      if (t.due !== state.item.due || t.recurrence !== state.item.recurrence) t.nextDue = undefined; // not known until the rows are read again
      const dueMoved = t.due !== state.item.due;
      Object.assign(t, { status: state.item.status, kind: state.item.kind ?? t.kind, p: state.item.priority, due: state.item.due, recurrence: state.item.recurrence,
        anchor: state.item.recurrence_anchor ?? null, revision: state.revision, title: state.item.title ?? t.title, stage: t.base ? state.item.stage : null });
        if ('recurrence_lead' in state.item) t.lead = state.item.recurrence_lead ?? null;
      if ('repo' in state.item && state.item.repo !== t.repo_path) Object.assign(t, { repo_path: state.item.repo,
        repo: state.item.repo ? host.rows().find(x => x.repo_path === state.item.repo)?.repo || state.item.repo.split('/').pop() : 'no repo' });
      // The server's urgency is is_urgent, and urgent_otherwise is is_urgent without its due rule; after a due edit the page
      // applies that rule to the new date until the rows are read again.
      if (dueMoved) t.urgent = t.urgentOtherwise || withinWeek(t);
      if ('body' in state.item) DET.body[t.id] = { text: bodyText(state.item.body), rev: state.revision };
      staleDet(t.id);
      if (selected() === key) readDet(t.id); // the open Details read again, so they show the write
    }

    const days = t => t.due ? Math.round((new Date(t.due + 'T00:00') - TODAY) / DAY) : null;
    // Urgency is the server's reads.is_urgent decision for the row (review, PR #50): due within 7 days, a message ready to
    // send for over 3 days, or a report that needs attention. withinWeek is its due rule, for a due edit before the re-read
    // and for the ≤ 7 days filter: (due at UTC midnight − the document's read stamp).days <= 7, timedelta.days flooring. Not
    // the viewer's calendar days, which put a task in another quadrant than the server's until the next read (review, PR #46).
    const withinWeek = t => t.due !== null && t.due !== undefined
      && Math.floor((Date.parse(t.due + 'T00:00:00Z') - Date.parse(host.read() || new Date().toISOString())) / DAY) <= 7;
    const urgent = t => t.overdue || t.urgent;
    const important = t => t.p !== null && t.p !== undefined && t.p <= 2;
    const quadOf = t => QUADS.find(q => q[2] === urgent(t) && q[3] === important(t))[0];
    function dueText(t) {
      if (t.overdue && !t.due) return ['overdue', 'warning'];
      if (!t.due) return ['no due', ''];
      const n = days(t);
      if (n < 0) return [`${-n}d overdue`, 'warning'];
      if (n === 0) return ['due today', 'caution'];
      if (n === 1) return ['due tomorrow', 'caution'];
      return [`due ${fmt(t.due)}`, n <= 2 ? 'caution' : ''];
    }

    // Legal moves: sd_db.workflow.allowed_statuses. build: the row carries what the library allows (t.allowed); the reasons
    // name why a move is off, in the reference's words where it had them.
    function legal(t, to) {
      if (t.status === to) return { ok: false, reason: 'Already here.' };
      if (t.assignment === 'running') return { ok: false, reason: 'An agent is working on this task. Status is locked until it ends.' };
      if (t.assignment === 'queued') return { ok: false, reason: 'An agent run is queued for this task. Status is locked until it ends.' };
      if (t.kind === 'work' && to === 'done') return { ok: false, reason: 'Work items close through verified completion (sd work), not a status write.' };
      if (!(t.allowed || []).includes(to)) return { ok: false, reason: t.allowed?.length ? `sd task status refuses ${slabel(to)} for this ${t.kind} item.` : `A ${t.kind} item takes no status write here; it has its own workflow.` };
      return { ok: true };
    }
    function quadChange(t, q) {
      const Q = QUADS.find(x => x[0] === q);
      if (!EDITABLE(t)) return { ok: false, reason: noEdit(t) };
      if (quadOf(t) === q) return { ok: false, reason: 'Already here.' };
      if (!Q[2] && t.urgentOtherwise) return { ok: false, reason: 'It is urgent for a reason a due date does not change (a waiting message or a report that needs attention).' };
      const ch = {}, words = [];
      if (Q[3] && !important(t)) { ch.p = 2; words.push(`priority ${t.p ? 'P' + t.p : 'unset'}→P2`); }
      if (!Q[3] && important(t)) { ch.p = 3; words.push(`priority P${t.p}→P3`); }
      // Urgency is derived from the due date, so crossing it means choosing a date. Never invent or erase one silently.
      let needsDate = null;
      if (Q[2] && !urgent(t)) { needsDate = 'into'; words.push('you pick a due date within 7 days'); }
      if (!Q[2] && urgent(t)) { needsDate = 'out'; words.push(`you pick a later due date${t.due ? ` (now ${fmt(t.due)})` : ''}`); }
      return { ok: true, ch, words, needsDate };
    }
    // build: the row carries what sd_db.workflow.edit_item takes (tasks_screen._edit_capability): a kind in DETAIL_KINDS.
    const EDITABLE = t => !!t?.edit?.allowed;
    const noEdit = t => t.edit?.reason || `a ${t.kind} item uses its own editing workflow`;
    // build: sd_db.workflow.TASK_STATUS_KINDS; workflow._recurring refuses a rule on any other kind.
    const RECURS = ['task', 'followup'];
    // build (sd:3012): sd_db.progress.CANCELLABLE_TASK_KINDS, the kinds task_guard lets sd task cancel close.
    const CLOSES = ['task', 'followup'];
    // sd:6231: the Kind field's kinds, sd_db.workflow.HAND_KINDS, the ones sd task edit --kind sets; REPO_LESS is
    // workflow.REPO_LESS_KINDS, which carry no repository, so a move to one clears the repo in the same edit (--no-repo).
    const KINDS = ['task', 'followup', 'work-idea', 'personal-idea'], REPO_LESS = ['work-idea', 'personal-idea'];
    const repeats = t => !!t?.recurrence;
    // build (sd:2250): sd stores a rule only from sd_db.recurrence.PARTS. A rule with any other part (COUNT), written around
    // the library, cannot be walked: completing it ends the series. test_v2_tasks holds this set to the library's.
    const PARTS = new Set(['FREQ', 'INTERVAL', 'BYMONTH', 'BYMONTHDAY', 'BYDAY']);
    const walkable = t => String(t.recurrence || '').replace(/^RRULE:/i, '').split(';').every(p => PARTS.has(p.split('=')[0].trim().toUpperCase()));
    // sd:5649: the Repeat field's rules, each in sd_db.recurrence.Rule.text()'s spelling, the one the store keeps, so a stored
    // rule selects its name; test_v2_tasks parses each with the library. A rule not here shows as itself.
    const REPEATS = [['FREQ=DAILY', 'Daily'], ['FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR', 'Weekdays'], ['FREQ=WEEKLY', 'Weekly'], ['FREQ=MONTHLY', 'Monthly'],
      ['FREQ=MONTHLY;INTERVAL=3', 'Quarterly'], ['FREQ=YEARLY;BYMONTH=3,6,9,12;BYMONTHDAY=21', 'Seasonal'], ['FREQ=YEARLY', 'Yearly']];
    const repeatName = rule => REPEATS.find(r => r[0] === rule)?.[1] || rule;
    // The Lead field's days: how long before its due date a repeating task's next row opens (pack sd:5648).
    const LEADS = [0, 1, 2, 3, 7, 14, 30];
    const opens = t => t.scheduled && t.opensOn ? `opens ${fmt(t.opensOn)}` : '';
    // A completion that opens nothing: a rule sd cannot walk, or one whose next date workflow.next_occurrence_due says is none.
    const ends = t => !walkable(t) || t.nextDue === null;
    const cliMove = (t, to) => `sd task status ${t.id} ${to}`;
    function cliQuad(t, ch) {
      const a = [];
      if ('kind' in ch) a.push(`--kind ${ch.kind}`);
      if ('p' in ch) a.push(ch.p ? `--priority ${ch.p}` : '--clear-priority');
      if ('due' in ch) a.push(ch.due ? `--due ${ch.due}` : '--clear-due');
      if ('repo' in ch) a.push(ch.repo ? `--belongs-to ${window.shell.shq(ch.repo)}` : '--no-repo');
      if ('title' in ch) a.push(`--title ${window.shell.shq(ch.title)}`);
      if ('body' in ch) a.push(`--body ${window.shell.shq(ch.body)}`);
      if ('recurrence' in ch) a.push(ch.recurrence ? `--recur ${window.shell.shq(ch.recurrence)}` : '--clear-recur');
      if ('recurrence_lead' in ch) a.push(`--recur-lead ${ch.recurrence_lead ?? 0}`);
      if ('area' in ch) a.push(ch.area ? `--area ${ch.area}` : '--clear-area');
      return `sd task edit ${t.id} ${a.join(' ')}`;
    }

    // ---------- Writes (build) ----------
    // One write per task at a time: each waits for the one before it, then sends the row's revision as it is then. A stale
    // revision (409) re-reads the rows. `sent` sees the row as the write leaves: the state this write changes, after every
    // write queued before it (review, PR #46). `check` asks again, as the write leaves, whether the command is still on: a
    // write queued behind another chose its command before that one landed (review round 3). `rev` is an Undo's: the revision
    // its own write answered. A row whose revision moved since, by a re-read or by any other write, refuses the Undo, so it
    // never overwrites a change it did not make (review round 3). A landed write rereads the rows (shell.read's barrier);
    // `wait` holds the write's promise until that read ends, for a toast that names what the read brings.
    const CHANGED = 'it changed after this write, and Undo would overwrite that change';
    // sd:8033: a status or field write (moveTo, edit) the server never answers rejects after WRITE_SECONDS, marked unknown:
    // it may have landed, so the page reads the rows again (landing) and says so, and the row's next write is not held
    // behind it. `seconds` is the per-call bound; a write without one (Draft email and Fix run a model or a launch) keeps
    // the browser's wait, as does a browser without AbortSignal.timeout. A failed fetch got no answer: its outcome is unknown.
    const WRITE_SECONDS = 30;
    const bound = seconds => seconds && typeof AbortSignal !== 'undefined' && AbortSignal.timeout ? AbortSignal.timeout(seconds * 1000) : undefined;
    const unknown = e => Object.assign(new Error(`${e && e.name === 'TimeoutError' ? `the dashboard did not answer within ${WRITE_SECONDS} s`
      : `no answer (${(e && e.message) || e})`}, so whether it changed is not known; the rows were read again`), { unknown: true });
    const pending = new Map();
    function write(key, path, body, { sent, check, rev, wait, seconds } = {}) {
      const p = (pending.get(key) || Promise.resolve()).then(() => { const t = byKey(key); if (!t) throw new Error('the task is no longer listed');
        if (rev !== undefined && t.revision !== rev) throw new Error(CHANGED);
        const on = check ? check(t) : true; if (on !== true) throw new Error(on);
        sent?.(t); return window.shell.post(path(t), { ...body, revision: t.revision }, undefined, bound(seconds)).catch(e => { throw e && e.status ? e : unknown(e); }); })
        .then(out => { absorb(key, out); host.render(); const again = host.reread(); return wait ? again.then(() => out) : out; });
      pending.set(key, p.catch(() => {}));
      return p;
    }
    // The item edit route takes workflow field names: priority, due, recurrence, recurrence_anchor.
    const edit = (key, ch, opts) => write(key, t => `/api/items/${t.id}`, Object.fromEntries(Object.entries(ch).map(([k, v]) => [k === 'p' ? 'priority' : k, v])), { seconds: WRITE_SECONDS, ...opts });
    const moveTo = (key, to, opts) => write(key, t => `/api/items/${t.id}/status`, { status: to }, { seconds: WRITE_SECONDS, ...opts });
    function failed(name, err) { toast(`${name ? name + ' ' : ''}not changed: ${err.message}`); }
    // A refused stale write loads the rows again (load(), not reread(): only a reread says a change landed), once however
    // many writes in a group were refused: a refusal joins the load it started. A landed write's reread never joins a read
    // sent before it landed (read.js), so a completion's next occurrence is never missed (review, PR #46). The open Details
    // are read again with the rows: a conflict means the item changed elsewhere, and they show it (review, PR #46).
    let refusing = null;
    function refused() {
      if (refusing) return refusing;
      const p = refusing = host.load().then(ok => { const id = ok && byKey(selected())?.id; if (id) redrawDet(id); })
        .finally(() => { if (refusing === p) refusing = null; });
      return p;
    }
    // ---------- Undo (build, review of PR #46 and #50) ----------
    // A command's run returns landing(...): the shell contract in shell.js (bulk:start) waits for it, toasts its text, and
    // offers Undo only when it landed, for a bulk group too. The Undo it resolves to belongs to this operation: `inverse` gets
    // what the write answered and reverses this write, no later one, once, with the revision that answer carried. A write
    // that failed has no Undo, so a refused row is never written again with the revision the re-read brought.
    function landing(promise, text, inverse) {
      return promise.then(v => {
        let spent = false;
        const undo = inverse && (() => { if (spent) return Promise.resolve(false); spent = true;
          return inverse(v).then(() => true, err => { if (err.stale) refused(); throw err; }); });
        return { text: text(v), undo };
      }, err => { if (err.stale || err.unknown) refused(); throw err; });
    }
    // The command's undo: the shell passes what the run answered.
    const undoOf = (o, r) => r && r.undo ? r.undo() : false;

    // ---------- Commands (products/system/commands.md) ----------
    // Each task is an item object. Each command is declared once; the shell renders the row button, the
    // action menu (. or ⋯), the Details bar with its CLI lines, the palette and the bulk bar from it.
    const C = window.shell.commands;
    const T = o => byKey(o.id);
    // A run's row: a picked row a refresh removed fails that row's run with this reason (review, PR #46).
    const must = o => { const t = T(o); if (!t) throw new Error('the task is no longer listed'); return t; };
    // Details beyond the row (sd:2180). build: /api/tasks/<id>, read when a task is selected, kept until a write changes it.
    // Each read carries the item's generation; a write bumps it, so an answer sent before the write is dropped, not cached.
    // DET.body keeps the last description read or written back: a write drops the reading, not the description field, so a
    // redraw before the next read still draws it and keeps the operator's draft (review 2026-10-12, lane entry 225).
    // The stored body is JSON, {"text": …} (workflow._fields), and the field edits its text, as the classic item page does
    // (controls.py); null marks a body that does not parse, whose field is withheld so no save overwrites it. Each cached
    // description and reading carries the revision it was read at: a row read at another revision makes it stale (adopt).
    const bodyText = stored => { try { const b = JSON.parse(stored || '{}'); return b && typeof b === 'object' && !Array.isArray(b) ? String(b.text ?? '') : ''; } catch { return null; } };
    const DET = { items: {}, reading: {}, failed: {}, gen: {}, body: {} }, detOf = t => (t && t.id && DET.items[t.id]) || null;
    async function readDet(id, force) {
      if (!id || (!force && (DET.items[id] || DET.reading[id] === (DET.gen[id] || 0)))) return;
      const gen = DET.gen[id] || 0;
      DET.reading[id] = gen; delete DET.failed[id];
      let got, err;
      try { got = await window.shell.getJSON(`/api/tasks/${encodeURIComponent(id)}`); } catch (e) { err = e; }
      if ((DET.gen[id] || 0) !== gen) return; // a write landed meanwhile; the read it started is the one that counts
      delete DET.reading[id];
      if (err) DET.failed[id] = err.message; else { DET.items[id] = got; DET.body[id] = { text: bodyText(got.item.body), rev: got.revision }; }
      // The page's linked item (Tasks' PIN, sd:5462) takes its row from the reading; its first one selects it.
      const first = !err && !!host.got?.(id, got);
      putAll();
      if (first) host.select(String(id), true);
      else if (String(id) === selected()) renderDetails();
    }
    const staleDet = id => { DET.gen[id] = (DET.gen[id] || 0) + 1; delete DET.items[id]; delete DET.reading[id]; };
    // A row read at another revision than its cached Details changed elsewhere: they are read again (review 2026-10-12).
    const adopt = rows => rows.forEach(t => { const d = DET.items[t.id]; if (d && d.revision !== t.revision) { staleDet(t.id); if (selected() === t.key) readDet(t.id); } });
    // The rows' objects are the reader's (adopt below): it puts them after each read and retires a row the read stopped
    // listing. The Details' notes and assignments follow their row: once it is not listed, they run nothing either.
    const objectOf = t => ({ id: t.key, type: 'item', label: `${label(t)} ${t.title}`, item: t.id });
    const live = key => { const o = C.get(key); return !!o && o.type !== 'not listed'; };
    function putAll() {
      Object.values(DET.items).forEach(d => {
        const as = live(String(d.item.id)) ? o => o : o => ({ id: o.id, type: 'not listed', label: `${o.label} (no longer listed)` });
        d.notes.forEach(n => C.put(as({ id: `note:${n.id}`, type: 'note', label: `${n.kind} ${n.id} on #${d.item.id}`, note: n.id, kind: n.kind, resolved: n.resolved, item: d.item.id })));
        d.assignments.forEach(a => C.put(as({ id: `asg:${a.id}`, type: 'assignment', label: `Assignment #${a.id}`, n: a.id, status: a.status, item: d.item.id, repo: d.item.repo, can: a.cancel })));
      });
      ISSUE_RUNS.forEach(r => C.put(issueObject(r)));
    }
    const redrawDet = id => { staleDet(id); readDet(id, true); };
    // ---------- File issue (sd:5103, issue_filing) ----------
    // The latest run an item's File GitHub issue or File Jira issue started on this page, read again every 3 s while it drafts
    // or files. Each run is an `issue draft` object, so its File issue and Discard are registered commands, drawn in Details.
    const TRACKER = { github: 'GitHub', jira: 'Jira' }, ISSUE_POLL = 3000, ISSUE_RUNS = new Map();
    const issueBusy = r => ['queued', 'drafting', 'filing'].includes(r.state);
    const issueWhere = x => x.kind === 'github' ? `GitHub ${x.repo}` : `Jira project ${x.project}`;
    const issueObject = r => ({ id: `issue:${r.id}`, type: 'issue draft', label: `${TRACKER[r.kind]} issue for #${r.item}`, run: r });
    function followIssue(run) {
      ISSUE_RUNS.set(run.item, run); C.put(issueObject(run));
      if (byKey(selected())?.id === run.item) renderDetails();
      if (issueBusy(run)) setTimeout(() => readIssue(run), ISSUE_POLL);
    }
    // A failed read is not known, not settled: Details says so and the next read tries again. A newer run stops the old one's.
    function readIssue(run) {
      if (ISSUE_RUNS.get(run.item)?.id !== run.id) return;
      window.shell.getJSON(`/api/issue-runs/${encodeURIComponent(run.id)}`).then(followIssue, e => followIssue({ ...run, unread: e.message }));
    }
    // The draft in Details: while it drafts, what runs; once ready, the exact target, title and body File issue sends; else
    // the reason, which names a category or a line, never the matched value, so a blocked draft shows no text.
    function issuePane(t) {
      const r = ISSUE_RUNS.get(t.id); if (!r || r.state === 'discarded') return '';
      const name = TRACKER[r.kind], unread = r.unread ? html`<p class="why">Its state was not read: ${r.unread}. This page tries again in 3 s.</p>` : '';
      const head = html`<h3>${name} issue <span class="n">run ${r.id} · ${r.state}</span></h3>`;
      if (issueBusy(r)) return html`<section aria-live="polite">${head}<p class="why">${r.state === 'filing' ? `Filing in ${issueWhere(r.target)}. Wait for the answer before you file it again.`
        : 'Claude drafts the issue with no tools; the sanitizer then strips internal names and checks the privacy patterns. Nothing is filed.'}</p>${unread}</section>`;
      if (r.state === 'linked') return html`<section aria-live="polite">${head}<p class="why">Filed: <a class="ext" href="${r.url}" target="_blank" rel="noopener noreferrer">${r.url}</a> · #${r.item} links it.</p></section>`;
      const removed = Object.entries(r.removed || {}).map(([k, n]) => `${n} ${k}`).join(', ');
      const preview = r.state === 'ready' ? html`<dl><dt>Target</dt><dd>${issueWhere(r.target)}</dd><dt>Title</dt><dd>${r.title}</dd></dl>
        <pre class="body">${r.body}</pre><p class="why">${removed ? `The sanitizer took out ${removed}.` : 'The sanitizer took nothing out.'} File issue sends exactly this text.</p>` : '';
      const filed = r.state === 'filed' ? html`<p class="why">Filed at <a class="ext" href="${r.url}" target="_blank" rel="noopener noreferrer">${r.url}</a>.</p>` : '';
      return html`<section aria-live="polite">${head}${filed}${preview}${r.reason ? html`<p class="why">${r.reason}</p>` : ''}${unread}${C.bar(`issue:${r.id}`)}</section>`;
    }
    // note.resolve: one declaration, copied on every page that lists followups (Tasks, Today; commands.md § Shared declarations).
    const NOTE_RESOLVE = done => ({ id: 'note.resolve', on: 'note', label: 'Resolve', key: 'v', risk: 'confirm', icon: 'check', primary: o => o.kind === 'followup' && !o.resolved,
      does: 'Closes this followup; nothing reopens it.',
      when: o => o.kind !== 'followup' ? `a ${o.kind} note has nothing to resolve` : o.resolved ? `resolved ${o.resolved.slice(0, 10)}` : true,
      cli: o => `sd note resolve ${o.note}`, consequence: () => 'Closes the followup. Nothing reopens it.',
      run: o => done(o) });
    const rowOr = (o, f) => { const t = T(o); return t ? f(t) : 'no CLI: the task is no longer listed'; };
    const coarse = matchMedia('(pointer: coarse)');
    const NEXT = { planning: 'ready', ready: 'in_progress', in_progress: 'done', blocked: 'ready' };
    // Status moves: keys 1–5 in the menu, as on the board. The list row shows the next status as its button.
    // build: run returns the move's landing, so the toast (with Undo) comes when the write lands; undo moves it back to
    // the status this move left, read as the write is sent. The `when` test runs again as the write leaves.
    const moveOn = (s, t) => { if (s === 'done' && repeats(t)) return !walkable(t) ? 'it repeats: 5 completes it and ends the series, since sd cannot walk its rule'
        : t.nextDue === null ? 'it repeats: 5 completes it and ends the series, since its rule gives no next date'
        : 'it repeats: 5 completes it and opens the next occurrence';
      const L = legal(t, s); return L.ok || L.reason; };
    // sd:5457: each move's icon names the status it moves to, so the row's act reads without its label.
    const STATUS_ICON = { planning: 'notebook-pen', ready: 'flag-triangle-right', in_progress: 'play', blocked: 'ban', done: 'check' };
    // sd:5752: on a place page Done, or Done → next occurrence for a repeating task, is the row's act whatever its status.
    const nextIs = (o, s) => view() === 'place' ? s === 'done' : view() === 'list' && NEXT[T(o)?.status] === s;
    const STATUS_CMD = Object.fromEntries(STATUSES.map(([s, name], i) => [s, {
      id: `item.status.${s}`, on: 'item', label: `Status → ${name}`, key: String(i + 1), risk: 'undo', bulk: true, icon: STATUS_ICON[s], bar: false, menu: false,
      primary: o => !draftFirst(T(o)) && nextIs(o, s) && !(s === 'done' && repeats(T(o))),
      does: `Moves the task to ${name}.`,
      when: o => { const t = T(o); return t ? moveOn(s, t) : 'the task is no longer listed'; },
      cli: o => rowOr(o, t => cliMove(t, s)),
      run: o => { const t = must(o); let from = t.status;
        return landing(moveTo(t.key, s, { sent: r => { from = r.status; }, check: r => moveOn(s, r) }).then(v => { host.landed?.(t.key); return v; }),
          () => `${label(t)} ${slabel(from)} → ${name} · ${cliMove(t, s)}`, v => moveTo(t.key, from, { rev: v.revision })); },
      undo: undoOf,
    }]));
    // A repeating task's completion makes workflow.change_status open the next occurrence and clear the rule on this one. A
    // move back would leave both open, so completing it is confirmed and has no Undo (review, PR #46). The rows are read
    // again when it lands, so the next occurrence is listed at once. Key 5 is Done's too: the shell runs the first command
    // on a key that is on, so the two `when` tests split on repeats() and never are both on. Not bulk: a picked repeating
    // row is skipped with its reason (keys below).
    const completeOn = t => { if (!repeats(t)) return 'the task does not repeat; Status → Done completes it'; const L = legal(t, 'done'); return L.ok || L.reason; };
    const COMPLETE = { id: 'item.complete', on: 'item', label: 'Done → next occurrence', key: '5', risk: 'confirm', executes: true, icon: 'calendar-clock', bar: false,
      primary: o => !draftFirst(T(o)) && nextIs(o, 'done') && repeats(T(o)),
      does: 'Completes this repeating task and opens its next occurrence.',
      when: o => { const t = T(o); return t ? completeOn(t) : 'the task is no longer listed'; },
      cli: o => rowOr(o, t => cliMove(t, 'done')),
      // build: next_due is workflow.next_occurrence_due, the date the completion computes. null is its "none": the
      // completion then ends the series and opens nothing, so the confirm says that (review, PR #46). The outcome is stated
      // in the design owner's three cases (sd:2250): a date the page has; a valid rule whose date the page has not read yet
      // since an edit (undefined), which sd sets; a rule sd cannot walk. A completion that opens nothing is a danger confirm.
      consequence: o => { const t = T(o), rule = t.recurrence;
        if (!walkable(t)) return `Completes ${label(t)} and ends the series: sd cannot walk ${rule}, so no next occurrence opens. There is no Undo.`;
        if (t.nextDue === null) return `Completes ${label(t)} and ends the series: its rule (${rule}) gives no next date, so no next occurrence opens. There is no Undo.`;
        return `Completes ${label(t)} and opens the next occurrence${t.nextDue ? `, due ${fmt(t.nextDue)} (${rule}).` : ` (${rule}); sd sets its date.`} A move back would leave two open tasks, so there is no Undo.`; },
      danger: o => ends(T(o)),
      run: o => { const t = T(o); let from = t.status;
        const next = v => v?.next_occurrence ? `next occurrence #${v.next_occurrence}${byKey(String(v.next_occurrence))?.due ? ` due ${fmt(byKey(String(v.next_occurrence)).due)}` : ''}`
          : `the series ended: ${v?.next_occurrence_reason || 'no next occurrence was made'}`;
        return landing(moveTo(t.key, 'done', { sent: r => { from = r.status; }, check: completeOn, wait: true }),
          v => `${label(t)} ${slabel(from)} → Done · ${next(v)} · ${cliMove(t, 'done')}`); } };
    // An edit's Undo sets back the fields it changed (`keep`, the row's names), as they were when the edit was sent, at the
    // revision the edit answered. A field the row does not carry (the description) takes `before(row)`, read then too.
    // `check` asks as the edit leaves whether it still changes anything: a second one queued
    // behind the first would write nothing, and its Undo would set back what the first one changed (review, PR #46).
    // `own` is the row's own area (pack sd:8021): `area` alone may be the repo rule's, which no edit sets back.
    const FIELD = { anchor: 'recurrence_anchor', lead: 'recurrence_lead', repo_path: 'repo', own: 'area' };
    // `before` holds a field the row does not carry (the description, read with the Details).
    function editRun(t, ch, msg, { check, keep = Object.keys(ch), before = () => ({}) } = {}) {
      let was = {};
      const sent = r => { const b = before(r); was = Object.fromEntries(keep.map(k => [FIELD[k] || k, k in r ? r[k] ?? null : b[k] ?? null])); };
      return landing(edit(t.key, ch, { sent, check }).then(v => { host.landed?.(t.key); return v; }), msg, v => edit(t.key, was, { rev: v.revision }));
    }
    // sd:5295: the Details' Status, Priority and Due fields do what Status →, Done → next occurrence, Edit → P1-4, Edit and Move
    // to do, so those stay off its Act bar (`bar: false`); 1-5, e, m, the ⋯ menu, the palette and the bulk bar still run them.
    // sd:6687: Status → and Edit → P leave the row ⋯ menu too (`menu: false`); keys 1-5, the palette and bulk keep them.
    C.register(
      ...Object.values(STATUS_CMD), COMPLETE,
      // Edit sets priority and due date in one dialog. Its save carries the Undo, so the shell's own feedback stays safe.
      { id: 'item.edit', on: 'item', label: 'Edit', key: 'e', risk: 'safe', icon: 'pen-line', bar: false,
        does: 'Opens a form to change the priority and due date.',
        when: o => { const t = T(o); return !t ? 'the task is no longer listed' : EDITABLE(t) || noEdit(t); },
        cli: o => rowOr(o, t => `sd task edit ${t.id} --priority ${t.p || 'N'} ${t.due ? '--due ' + t.due : '--due YYYY-MM-DD'}`),
        run: o => { askEdit(T(o)); return `Editing ${label(T(o))}`; } },
      // Move to: the status moves without drag (touch, or anyone). One form lists every column; a refused one is off with its reason.
      { id: 'item.move', on: 'item', label: 'Move to', key: 'm', risk: 'safe', icon: 'kanban', bar: false,
        primary: () => view() === 'board' && coarse.matches,
        does: 'Opens the list of statuses to move the task to.',
        when: o => { const t = T(o); if (!t) return 'the task is no longer listed'; const L = STATUSES.map(([s]) => legal(t, s)); return L.some(x => x.ok) || L.find(x => x.reason !== 'Already here.').reason; },
        cli: o => rowOr(o, t => `sd task status ${t.id} <status>`),
        run: o => { askMove(T(o)); return null; } },
      // Priorities 1–4, workflow.edit_item's range, each a bulk edit with Undo (sd:3012 triage re-prioritizes in bulk). A row
      // already at the priority is refused as its write leaves, not by `when`: the bar offers only what every picked row is
      // on for, so a pick that mixes priorities would otherwise offer none.
      ...[1, 2, 3, 4].map(n => ({ id: `item.p${n}`, on: 'item', label: `Edit → P${n}`, risk: 'undo', bulk: true, icon: 'flag-triangle-right', bar: false, menu: false,
        does: `Sets the priority to P${n}.`,
        when: o => { const t = T(o); return !t ? 'the task is no longer listed' : EDITABLE(t) || noEdit(t); },
        cli: o => rowOr(o, t => `sd task edit ${t.id} --priority ${n}`),
        run: o => { const t = must(o), was = t.p; return editRun(t, { p: n }, () => `${label(t)} P${was || '–'} → P${n}`, { check: r => r.p !== n || `it is already P${n}` }); },
        undo: undoOf })),
      // Close is sd task cancel (sd:3012): done, with a cancelled receipt and the reason, through progress.task_guard. A bulk
      // close asks once for one reason and runs every picked row with its own revision. No Undo: no sd verb takes it back.
      { id: 'item.close', on: 'item', label: 'Close', risk: 'confirm', executes: true, bulk: true, icon: 'ban',
        does: 'Closes the task as done, with your reason; it cannot be reopened.',
        when: o => { const t = T(o); return !t ? 'the task is no longer listed' : !CLOSES.includes(t.kind) ? `Close works on a task or followup; this is a ${t.kind} item`
          : t.status === 'done' ? 'it is done' : repeats(t) ? 'a recurring task cannot be cancelled; clear its recurrence first, or complete it'
          : t.assignment === 'running' || t.assignment === 'queued' ? `an agent run is ${t.assignment} for it` : true; },
        fields: () => [{ name: 'reason', label: 'Reason', required: true, placeholder: 'why nobody will do it', help: 'Recorded on each closed task. A close without a reason is refused.' }],
        cli: (o, v = {}) => rowOr(o, t => `sd task cancel ${t.id} --reason ${v.reason ? window.shell.shq(v.reason) : "'<why>'"}`),
        sends: o => `POST /api/items/${T(o).id}/cancel-task {reason}`,
        consequence: () => 'Each task closes as done, marked cancelled, with your reason. This cannot be undone.',
        run: (o, v) => { const t = must(o);
          return landing(write(t.key, x => `/api/items/${x.id}/cancel-task`, { reason: v.reason }), () => `${label(t)} closed · sd task cancel ${t.id} --reason ${window.shell.shq(v.reason)}`); } },
      // Park (sd:5906) sets parked_at to now through the item edit route, by hand, as the nightly prune does; its Undo
      // unparks. The route takes a parked_at only from a pack with sd task edit --park (pack sd:5905).
      { id: 'item.park', on: 'item', label: 'Park', risk: 'undo', executes: true, bulk: true, icon: 'archive',
        does: 'Sets the task aside; it leaves the open lists until you unpark it.',
        when: o => { const t = T(o); return !t ? 'the task is no longer listed' : t.parked ? 'it is already parked' : t.status === 'done' ? 'it is done' : EDITABLE(t) || noEdit(t); },
        cli: o => rowOr(o, t => `sd task edit ${t.id} --park`),
        sends: o => `POST /api/items/${T(o).id} {parked_at: <now>}`,
        run: o => { const t = must(o);
          return landing(write(t.key, x => `/api/items/${x.id}`, { parked_at: new Date().toISOString() }), () => `${label(t)} parked · sd task edit ${t.id} --park`,
            v => write(t.key, x => `/api/items/${x.id}`, { parked_at: null }, { rev: v.revision })); },
        undo: undoOf },
      // Unpark (sd:3012) clears parked_at through the item edit route, as the item page's Unpark does (sd:3007). No Undo:
      // Park is a separate command, so an unpark is taken back by Park.
      { id: 'item.unpark', on: 'item', label: 'Unpark', risk: 'safe', executes: true, bulk: true, icon: 'archive-restore',
        does: 'Brings a parked task back into the open lists.',
        when: o => { const t = T(o); return !t ? 'the task is no longer listed' : !t.parked ? 'it is not parked' : true; },
        cli: o => rowOr(o, t => `sd task edit ${t.id} --unpark`),
        sends: o => `POST /api/items/${T(o).id} {parked_at: null}`,
        run: o => { const t = must(o);
          return landing(write(t.key, x => `/api/items/${x.id}`, { parked_at: null }), () => `${label(t)} unparked · sd task edit ${t.id} --unpark`); } },
      { id: 'item.note', on: 'item', label: 'Note', key: 'n', risk: 'safe', icon: 'notebook-pen',
        does: 'Opens a form to add a note to this task.',
        when: o => !!T(o) || 'the task is no longer listed',
        cli: o => rowOr(o, t => `sd task note ${t.id} "…" --kind comment`),
        run: o => { window.shell.capture(o); const r = document.querySelector('dialog.capture input[value="note"]'); if (r && !r.disabled) { r.checked = true; r.form.dispatchEvent(new Event('input')); } return 'Write the note'; } },
      // Fix (sd:3456): the shared declaration (commands.js). The post carries the row's revision, so a click from a page read
      // before an earlier Fix's note is stale; the Details read again after it. pack sd:7756: a failed launch (5xx) notes its
      // cause on the item, so the page reads again too: Details keep the cause after the toast fades. pack sd:7783: so
      // does a post with no answer (commands.js `again`).
      window.commands.fix('item', { item: o => T(o).id, report: o => T(o).kind === 'report',
        when: o => { const t = T(o); return !t ? 'the task is no longer listed' : t.status === 'done' ? 'it is done' : true; },
        post: (o, path) => { const t = must(o);
          return write(t.key, () => path, {}).then(v => { redrawDet(t.id); return v; }, err => { if (err.stale) refused(); throw err; }); },
        // pack sd:7820: a read during the post may have dropped the task; then no Details to redraw, and the 5xx toast stays.
        again: o => { host.reread(); const t = T(o); if (t) redrawDet(t.id); } }),
      // Adversarial review (sd:5064): the shared declaration (commands.js); the server reviews the item in its repository,
      // so an item with none is off.
      window.commands.adversarialReview('item', { payload: o => ({ kind: 'task', target: T(o).id }),
        when: o => { const t = T(o); return !t ? 'the task is no longer listed' : !t.repo_path ? 'it has no repository to review in' : true; } }),
      // Draft email (sd:3463, email_draft): claude writes an email from the item's title and notes, and workspace-mcp saves it
      // as a Gmail draft with no recipient. Nothing sends it. It asks first, since it runs a model and writes to Gmail. An
      // answered refusal reads the rows again: a failure writes a note, so the row's revision moved. A post with no answer
      // may have saved a draft, so it says that. No Undo: the draft stays in Gmail for the operator to send or delete.
      // sd:5671: HOA's Draft followup posted this route too; merged here, it keeps its key d, and a host's draftFirst rows
      // (HOA's followups) take it as their first action.
      { id: 'item.email', on: 'item', label: 'Draft email', key: 'd', risk: 'safe', askFirst: true, executes: true, icon: 'mail',
        primary: o => draftFirst(T(o)),
        does: 'Has Claude draft an email about this task in Gmail; sends nothing.',
        when: o => { const t = T(o); return !t ? 'the task is no longer listed' : CLOSES.includes(t.kind) || `Draft email writes about a task or followup; this is a ${t.kind} item`; },
        cli: () => 'no sd verb: the dashboard runs claude -p and saves the draft through workspace-mcp',
        sends: o => `POST /api/items/${T(o).id}/draft-email {revision}`,
        consequence: o => `Claude writes an email from #${T(o).id}'s title and notes and saves it as a Gmail draft with no recipient. Nothing is sent. A note on the item names the draft.`,
        run: o => { const t = must(o);
          return write(t.key, x => `/api/items/${x.id}/draft-email`, {}).then(
            v => { redrawDet(t.id); return `Draft saved · #${t.id} · "${v.draft.subject}" · in Gmail Drafts, not sent`; },
            err => { if (!err.status) throw new Error(`no answer (${err.message}), so whether a draft was saved is not known; look in Gmail Drafts before you press Draft email again`);
              refused(); throw err; }); } },
      // File GitHub issue and File Jira issue (sd:5103, issue_filing): a tool-less claude drafts an issue about the item in the
      // background and the server strips internal names from it; nothing is filed here. Each is on where the item's repo
      // enables it (Management's github_issues and jira_project), and off with the reason otherwise.
      ...Object.entries(TRACKER).map(([kind, name]) => ({ id: `item.file-${kind}-issue`, on: 'item', label: `File ${name} issue`, risk: 'safe', executes: true, icon: 'send',
        when: o => { const t = T(o), x = t?.issueTargets?.[kind]; return !t ? 'the task is no longer listed' : !x ? "the repo's issue settings were not read" : x.ok || x.why; },
        does: () => `Drafts a public ${name} issue, internal names removed; files nothing yet.`,
        cli: () => 'no sd verb: the dashboard drafts with claude -p, then files through gh or the Jira API on File issue',
        sends: o => `POST /api/items/${T(o).id}/file-issue {revision, target: "${kind}"}`,
        run: o => { const t = must(o);
          // Not write(): the answer is a run, not the item's state, and the start changes nothing on the row.
          return window.shell.post(`/api/items/${t.id}/file-issue`, { target: kind, revision: t.revision }).then(
            run => { followIssue(run); host.select(t.key, true); return `Drafting ${name} issue · #${t.id} · run ${run.id}; Details shows it, and nothing is filed yet`; },
            err => { if (!err.status) throw new Error(`no answer (${err.message}), so whether a draft started is not known`); if (err.stale) refused(); throw err; }); } })),
      // The draft's commands (sd:5103). File issue posts the digest of the text Details shows, so a draft that changed is
      // refused; it asks first, naming the target, since a filed issue is public and stays. Once filed but not linked, it
      // links only. A refusal or no answer reads the run again: the server writes what is known. No Undo for either.
      { id: 'issue.file', on: 'issue draft', label: 'File issue', risk: 'confirm', executes: true, icon: 'send', primary: o => o.run.state !== 'unknown',
        when: o => ['ready', 'filed'].includes(o.run.state) || `the draft is ${o.run.state}`,
        does: o => o.run.state === 'filed' ? `Links #${o.run.item} to the filed issue; nothing is filed again.` : `Files this title and body in ${issueWhere(o.run.target)}.`,
        cli: () => 'no sd verb: the dashboard files through gh or the Jira REST API',
        sends: o => `POST /api/issue-runs/${o.run.id}/confirm {digest}`,
        consequence: o => o.run.state === 'filed' ? `Links #${o.run.item} to ${o.run.url}. Nothing is filed again.`
          : `Files the title and body shown in Details as a public issue in ${issueWhere(o.run.target)}, then links #${o.run.item} to it. The dashboard cannot delete a filed issue.`,
        run: o => window.shell.post(`/api/issue-runs/${encodeURIComponent(o.run.id)}/confirm`, { digest: o.run.digest }).then(
          run => { followIssue(run); host.reread(); return `${TRACKER[run.kind]} issue filed · ${run.url} · #${run.item} links it`; },
          err => { readIssue(o.run); if (!err.status) throw new Error(`no answer (${err.message}), so whether the issue was filed is not known; look in ${TRACKER[o.run.kind]} before you file it again`); throw err; }) },
      // Check again (sd:5103): a run whose outcome is not known may have filed, so it is never filed again; the server
      // lists the tracker's newest issues for this exact title and links one if found.
      { id: 'issue.check', on: 'issue draft', label: 'Check again', risk: 'safe', executes: true, icon: 'search', primary: o => o.run.state === 'unknown',
        when: o => o.run.state === 'unknown' || `the draft is ${o.run.state}`,
        does: o => `Looks for this issue in ${issueWhere(o.run.target)}; files nothing.`,
        cli: () => 'no sd verb: the dashboard lists the newest issues through gh or the Jira API',
        sends: o => `POST /api/issue-runs/${o.run.id}/check`,
        run: o => window.shell.post(`/api/issue-runs/${encodeURIComponent(o.run.id)}/check`, {}).then(
          run => { followIssue(run); host.reread(); return `${TRACKER[run.kind]} issue found · ${run.url} · #${run.item} links it`; },
          err => { readIssue(o.run); throw err; }) },
      { id: 'issue.discard', on: 'issue draft', label: 'Discard', risk: 'safe', executes: true, icon: 'x',
        when: o => ['ready', 'blocked', 'failed', 'unknown'].includes(o.run.state) || `the draft is ${o.run.state}`,
        does: o => o.run.state === 'unknown' ? 'Drops the run; it may have filed, so look in the tracker first.' : 'Drops the draft; nothing was filed.',
        cli: () => "no sd verb: the draft lives in the dashboard's state folder",
        sends: o => `POST /api/issue-runs/${o.run.id}/discard`,
        run: o => window.shell.post(`/api/issue-runs/${encodeURIComponent(o.run.id)}/discard`, {}).then(run => { followIssue(run); return `Draft discarded · run ${run.id}; ${o.run.state === 'unknown' ? 'whether it filed is not known' : 'nothing was filed'}`; }) },
      { id: 'item.delete', on: 'item', label: 'Delete', risk: 'confirm', icon: 'x',
        does: 'Deletes the task with its notes and history.',
        when: () => 'a task cannot be deleted; move it to Done to keep a record',
        cli: o => rowOr(o, t => `sd task delete ${t.id}`),
        consequence: () => 'The task, its notes and its history go. Its assignments stay in the runner log. This cannot be undone; to keep a record, move it to Done instead.' },
      // Artifact and cancel are sd work verbs (review item 17). build (sd:2200, the design's sd:2199): both post to the routes v1's
      // item page uses, /api/items/<id>/(relink|cancel), with the row's revision. The text only the operator has, a moved path or a
      // reason, is a confirm field; OK stays off until it is typed. Availability is progress.work_controls, read with the Details.
      // Neither has Undo: relinking back is another relink, and no sd verb reopens a cancelled item.
      { id: 'work.relink', on: 'item', label: 'Relink', key: 'l', risk: 'safe', executes: true, icon: 'link-2',
        does: 'Points the work item at its file again after the file moved.',
        when: o => { const t = T(o), d = detOf(t); return !t ? 'the task is no longer listed' : t.kind !== 'work' ? `Relink works on a work item; this is a ${t.kind}`
          : !d ? 'the artifact was not read for this row' : !d.work.relink ? d.work.reason : d.item.path ? true : 'the item has no artifact to relink'; },
        fields: o => { const d = detOf(T(o)); return [{ name: 'path', label: 'Moved path', required: true, placeholder: d.item.path,
          help: `Relative to ${d.item.repo}. The item keeps its history; only the artifact path changes.` }]; },
        cli: (o, v = {}) => rowOr(o, t => `sd work relink ${t.id} ${v.path ? window.shell.shq(v.path) : '<moved path>'}`),
        sends: o => `POST /api/items/${T(o).id}/relink {path}`,
        run: (o, v) => { const t = must(o);
          return landing(write(t.key, x => `/api/items/${x.id}/relink`, { path: v.path }), () => `${label(t)} relinked → ${v.path} · sd work relink ${t.id} ${window.shell.shq(v.path)}`); } },
      { id: 'work.cancel', on: 'item', label: 'Cancel', key: 'w', risk: 'confirm', executes: true, icon: 'ban',
        does: 'Stops the work item as cancelled, with your reason.',
        when: o => { const t = T(o), d = detOf(t); return !t ? 'the task is no longer listed' : t.kind !== 'work' ? `Cancel works on a work item; this is a ${t.kind}`
          : t.status === 'done' ? 'the work item is done' : !d ? 'its cancel availability was not read for this row' : d.work.cancel || d.work.reason; },
        fields: () => [{ name: 'reason', label: 'Reason', required: true, placeholder: 'why the work stops', help: 'Recorded with the cancellation. A cancel without a reason is refused.' }],
        cli: (o, v = {}) => rowOr(o, t => `sd work cancel ${t.id} --reason ${v.reason ? window.shell.shq(v.reason) : "'<why>'"}`),
        sends: o => `POST /api/items/${T(o).id}/cancel {reason}`,
        consequence: () => 'Closes the work item as cancelled, with your reason. This cannot be undone.',
        run: (o, v) => { const t = must(o);
          return landing(write(t.key, x => `/api/items/${x.id}/cancel`, { reason: v.reason }), () => `${label(t)} cancelled · sd work cancel ${t.id} --reason ${window.shell.shq(v.reason)}`); } },
      // A followup note closes with sd note resolve. sd note has resolve and list only, so nothing reopens it: confirm, no Undo.
      // build: POST /api/notes/<note>/resolve with the item's revision; the readback is the item's state.
      NOTE_RESOLVE(o => { const item = String(o.item);
        return landing(write(item, () => `/api/notes/${o.note}/resolve`, {}).then(() => redrawDet(o.item)), () => `Resolved note ${o.note} · sd note resolve ${o.note}`); }),
      // Assignments: Show assignment is the shared declaration (commands.js); Cancel is this page's alone.
      // build (sd:3041): cancel is sd assignments cancel, POST /api/assignments/<n>/cancel with the revision the Details read
      // (operations.assignment_state); the runner's cancel went with the runner. No verb reopens a cancelled row: confirm, no Undo.
      { id: 'asg.cancel', on: 'assignment', label: 'Cancel', key: 'x', risk: 'confirm',
        does: 'Ends this agent run; the task keeps its status.',
        when: o => o.can.allowed || o.can.reason, cli: o => `sd assignments cancel ${o.n}`,
        consequence: o => `Ends assignment #${o.n} in ${o.repo} as cancelled. The item's status is unchanged.`,
        run: o => landing(cancelAsg(o), () => `Cancelled · #${o.n} · sd assignments cancel ${o.n}`) },
      window.commands.showAssignment({ primary: o => o.status !== 'blocked' }),
    );
    // A refused cancel reads the Details again too: they hold the revision a retry sends.
    async function cancelAsg(o) {
      const a = Object.values(DET.items).flatMap(d => d.assignments).find(x => x.id === o.n);
      if (!a) throw new Error(`assignment #${o.n} was not read`);
      try { await window.shell.post(`/api/assignments/${o.n}/cancel`, { revision: a.revision }); } catch (err) { if (err.stale) redrawDet(o.item); throw err; }
      redrawDet(o.item); await host.reread();
    }

    let detKey = null; // the task whose Details are on screen, so a redraw of another one carries nothing over
    function renderDetails() {
      const t = byKey(selected()), el = host.details;
      if (!t) { detKey = null; put(el, html`<p class="note">${host.empty?.() || 'Select a task to see its fields and moves.'}</p>`); return; }
      const [dt] = dueText(t), act = C.bar(t.key), gone = !live(t.key);
      // build (sd:3439): status, priority, due, repo and quadrant are fields; a change writes at once (fieldChange below).
      const off = gone ? 'this task was not read again after the change' : EDITABLE(t) ? '' : noEdit(t);
      const dis = off ? html` disabled aria-describedby="det-off"` : '';
      // A redraw (a Details read, a refresh, another field's write) keeps the field the operator is in, as it stands: when its
      // text differs from the saved value, or a date is half typed, the live field moves into the new Details with its caret.
      // Review 2026-10-12 (high): the title and description were redrawn from saved data mid-edit.
      const a = document.activeElement, focused = a?.id, mine = detKey === t.key && focused?.startsWith('det-') && a.isConnected && a.closest?.('#details');
      const caret = mine ? (() => { try { return [a.selectionStart, a.selectionEnd]; } catch { return [null, null]; } })() : null;
      detKey = t.key;
      put(el, html`<div class="kind"><span class="label">${t.kind}</span><span class="tag real">observed</span></div>
        <h2>${t.title}</h2>
        ${host.note?.(t) || ''}
        <dl class="fields">
          <dt>Id${hint('Id', 'The item\'s number in the workflow database; every sd command names the item by it.')}</dt><dd>#${t.id}</dd>
          <dt><label for="det-title">Title</label>${hint('Title', 'The name lists, the board and briefs show for this item.')}</dt><dd><input id="det-title" class="fld" value="${t.title}" required${dis}></dd>
          <dt><label for="det-status">Status</label>${hint('Status', 'Where the work stands; a status this item cannot move to is off, with its reason.')}</dt><dd><select id="det-status" class="fld"${gone ? html` disabled aria-describedby="det-off"` : ''}>${STATUSES.map(([s, name]) => { const L = legal(t, s);
            return html`<option value="${s}"${s === t.status ? html` selected` : L.ok ? '' : html` disabled title="${L.reason}"`}>${name}</option>`; })}${SLABEL[t.status] ? '' : html`<option value="${t.status}" selected disabled>${slabel(t.status)}</option>`}</select></dd>
          ${kindField(t, gone)}
          <dt><label for="det-p">Priority</label>${hint('Priority', 'P1 is the most important and P4 the least; P1 and P2 count as important.')}</dt><dd><select id="det-p" class="fld"${dis}>${t.p ? '' : html`<option value="" selected>not recorded</option>`}${[1, 2, 3, 4].map(n => html`<option value="${n}"${t.p === n ? html` selected` : ''}>P${n}</option>`)}</select></dd>
          <dt><label for="det-due">Due</label>${hint('Due', 'The date the work is due; a date within 7 days makes the item urgent.')}</dt><dd><input id="det-due" class="fld" type="date" value="${String(t.due || '')}"${dis}>${t.due || t.overdue ? html` <span class="why">${t.due ? dt : 'overdue · date not recorded'}</span>` : ''}</dd>
          ${repeatFields(t, off)}
          <dt><label for="det-repo">Repo</label>${hint('Repo', 'The checkout this item belongs to; a work item keeps the one its folder is in.')}</dt><dd><select id="det-repo" class="fld"${t.kind === 'work' && !off ? html` disabled title="A work item belongs to the checkout its folder is in."` : dis}>${repoOptions(t, null)}</select></dd>
          ${areaField(t, dis)}
          <dt>Urgent${hint('Urgent', 'Set by the dashboard from the due date and the item\'s state, not by hand.')}</dt><dd>${urgent(t) ? 'yes' : 'no'} <span class="why">(derived)</span></dd>
          <dt><label for="det-q">Quadrant</label>${hint('Quadrant', 'Where urgency and importance place the item; a move sets its priority or due date.')}</dt><dd>${t.status === 'done' ? 'not placed' : html`<select id="det-q" class="fld"${dis}>${QUADS.map(q => html`<option value="${q[0]}"${q[0] === quadOf(t) ? html` selected` : ''}>${q[1]}</option>`)}</select>`}</dd>
          ${t.assignment ? html`<dt>Assignment${hint('Assignment', 'Whether an agent is working on this task now.')}</dt><dd>${t.assignment}</dd>` : ''}
        </dl>
        ${off ? html`<p class="why" id="det-off">The fields are off: ${off}.</p>` : ''}
        <h3>Act</h3>${gone ? html`<p class="why">This task was not read again after the change, so no command runs on it. Reload reads it.</p>` : act}
        ${window.commands.reviewPane('task', t.id)}
        ${issuePane(t)}
        ${more(t, dis)}${host.more?.(t) || ''}
        <p class="foot">${host.foot?.() || ''}</p>`);
      if (mine) {
        const fresh = document.getElementById(focused), keep = fresh && !fresh.disabled && (a.value !== fresh.value || a.validity?.badInput);
        const f = keep ? a : fresh;
        if (keep) fresh.replaceWith(a);
        f?.focus?.();
        if (f && caret[0] != null) try { f.setSelectionRange(caret[0], caret[1]); } catch { /* a date or select has no caret */ }
      } else if (focused?.startsWith('det-')) document.getElementById(focused)?.focus?.();
      if (!off) window.shell.repos?.().then(list => { const r = document.getElementById('det-repo'); if (r && selected() === t.key && !r.disabled) put(r, repoOptions(t, list)); }, () => {});
    }
    // sd:5649: Repeat and Lead, each a select whose change is one edit with Undo (fieldChange). Repeat offers REPEATS, and a
    // stored rule not among them as itself; Lead shows on a repeating row of a store that has a lead (host.lead).
    function repeatFields(t, off) {
      const why = off || (RECURS.includes(t.kind) ? '' : `a ${t.kind} item cannot recur`), dis = why ? html` disabled title="${why}"` : '';
      const rules = t.recurrence && !REPEATS.some(r => r[0] === t.recurrence) ? [...REPEATS, [t.recurrence, t.recurrence]] : REPEATS;
      const note = opens(t) || (!t.due && !repeats(t) && !why ? 'needs a due date' : '');
      return html`<dt><label for="det-recur">Repeat</label>${hint('Repeat', 'How often the item comes back after it is done; it needs a due date.')}</dt><dd><select id="det-recur" class="fld"${dis}><option value=""${t.recurrence ? '' : html` selected`}>Does not repeat</option>${
        rules.map(([v, name]) => html`<option value="${v}"${v === t.recurrence ? html` selected` : ''}>${name}</option>`)}</select>${note ? html` <span class="why">${note}</span>` : ''}</dd>${
        host.lead?.() && repeats(t) ? html`<dt><label for="det-lead">Lead</label>${hint('Lead', 'How many days before the due date the next one opens.')}</dt><dd><select id="det-lead" class="fld"${dis}>${
          [...new Set([...LEADS, t.lead ?? 0])].sort((a, b) => a - b).map(n => html`<option value="${n}"${n === (t.lead ?? 0) ? html` selected` : ''}>${n ? `${plural(n, 'day')} early` : 'On the due date'}</option>`)}</select></dd>` : ''}`;
    }
    // sd:6231: Kind, a select of the hand kinds. edit_item takes a kind change on any hand kind, so an idea, whose
    // other fields are off, can still be reclassified; a kind with a producer (work, report, idea) is off with the reason.
    function kindField(t, gone) {
      const why = gone ? 'this task was not read again after the change' : KINDS.includes(t.kind) ? '' : `${t.kind} items are owned by their producer; their kind cannot be changed here`;
      return html`<dt><label for="det-kind">Kind</label>${hint('Kind', 'What the item is: a task, a followup or an idea. Idea kinds carry no repo.')}</dt><dd><select id="det-kind" class="fld"${why ? html` disabled title="${why}"` : ''}>${
        (KINDS.includes(t.kind) ? KINDS : [t.kind, ...KINDS]).map(k => html`<option value="${k}"${k === t.kind ? html` selected` : ''}>${k}</option>`)}</select></dd>`;
    }
    // pack sd:8021: Area, the item's own place (sd task edit --area, pack sd:8010): Home or HOA shows it in that tab
    // alone; None lets its repo place it. `area_set` says the row's area is its own, not the repo rule's.
    const AREAS = [['', 'None'], ['home', 'Home'], ['hoa', 'HOA']];
    const ownArea = t => t.area_set ? t.area : '';
    const areaName = v => AREAS.find(a => a[0] === (v || ''))?.[1] || v;
    function areaField(t, dis) {
      return html`<dt><label for="det-area">Area</label>${hint('Area', 'Home or HOA shows the item only in that tab, whatever its repo; None lets its repo place it.')}</dt><dd><select id="det-area" class="fld"${dis}>${
        AREAS.map(([v, name]) => html`<option value="${v}"${v === ownArea(t) ? html` selected` : ''}>${name}</option>`)}</select></dd>`;
    }
    // The repos a task can move to: every registered one (the capture's list) once read, else the ones these rows name.
    function repoOptions(t, list) {
      const all = list || [...new Map(host.rows().filter(x => x.repo_path).map(x => [x.repo_path, { path: x.repo_path, name: x.repo }])).values()];
      if (t.repo_path && !all.some(r => r.path === t.repo_path)) all.push({ path: t.repo_path, name: t.repo });
      return html`<option value=""${t.repo_path ? '' : html` selected`}>No repo</option>${all.map(r => html`<option value="${r.path}"${r.path === t.repo_path ? html` selected` : ''}>${r.name}</option>`)}`;
    }
    // build (sd:3439): a Details field's change is one write through the command or edit it stands for, with its toast and
    // Undo. Status runs the status command, Quadrant the quadrant move (it asks for a date when it crosses the urgency line),
    // and the rest edit the item. `check` refuses a change a write queued before it already made. A refused change redraws,
    // so the field shows the row as it is.
    const STALE_BODY = 'the description changed since it was read. Your text was not saved; the Details are read again.';
    function fieldChange(t, id, v, base) {
      if (id === 'det-title') { const n = v.trim(); if (n === t.title) return false;
        if (!n) { toast(`${label(t)} not changed: a title is required`); return false; }
        commitEdit(t, { title: n }, `title → ${n}`, { check: r => r.title !== n || 'the title is already that' }); return true; }
      // The description is not in the row, so a write over it is refused unless the text it replaces is the row's (read at the
      // row's revision) and is the text the field was drawn from (`base`): another tool's edit is never written over.
      if (id === 'det-body') { const known = () => DET.body[t.id], text = () => known()?.text ?? '';
        if (v === text() && (base === undefined || base === text())) return false;
        if (known()?.rev !== t.revision || (base !== undefined && base !== text())) { toast(`${label(t)} not changed: ${STALE_BODY}`); redrawDet(t.id); return true; }
        commitEdit(t, { body: v }, 'description changed', { before: () => ({ body: text() }),
          check: r => known()?.rev !== r.revision ? STALE_BODY : v !== text() || 'the description is already that' }); return true; }
      if (id === 'det-status') return v !== t.status && move(t.key, v);
      // build (sd:3977): a base row's stage moves through records.edit_row; its render can fail after the move lands.
      if (id === 'det-stage') { if (v === t.stage) return false;
        write(t.key, x => `/api/items/${x.id}/stage`, { stage: v }).then(out => toast(`${label(t)} stage → ${v}${out.mirror && !out.mirror.ok
          ? ` · mirror not updated: ${out.mirror.reason || 'see Health'}` : ''}`), err => { failed(label(t), err); err.status === 409 ? refused() : redrawDet(t.id); });
        return true; }
      if (id === 'det-exp' || id === 'det-exp-row') { const x = detOf(t)?.experiment, r = v.trim();
        const spec = id === 'det-exp' ? (v && v !== x?.value ? v : null) : r === (x?.row || '') ? null : r ? `row:${r}` : 'new';
        if (!spec) return false;
        write(t.key, y => `/api/items/${y.id}/experiment`, { experiment: [spec] }).then(() => toast(`${label(t)} experiments → ${spec} · sd task edit ${t.id} --experiment ${window.shell.shq(spec)}`),
          err => { failed(label(t), err); err.status === 409 ? refused() : redrawDet(t.id); });
        return true; }
      // sd:6231: a move to a kind that carries no repository clears the repo in the same edit, as sd task edit --kind needs
      // --no-repo; the confirm says so, and its Undo sets back the kind only, since edit_item takes a repo with no kind change.
      if (id === 'det-kind') { if (v === t.kind || !KINDS.includes(v)) return false;
        const ch = REPO_LESS.includes(v) && t.repo_path ? { kind: v, repo: null } : { kind: v };
        const go = () => commitEdit(byKey(t.key) || t, ch, `kind ${t.kind} → ${v}${'repo' in ch ? ', repo cleared' : ''}`, { keep: ['kind'], check: r => r.kind !== v || `it is already ${v}` });
        if (!('repo' in ch)) { go(); return true; }
        window.shell.confirm({ title: `Kind → ${v}: ${label(t)}?`, ok: `Make it ${v}`, keep: 'Cancel', danger: false, cli: cliQuad(t, ch),
          body: `A ${v} item carries no repo, so this also clears its repo (${t.repo}). Undo sets the kind back, not the repo.` })
          .then(yes => { if (yes) go(); });
        return false; }
      // pack sd:8021: an area moves the item between tabs, so the change asks first; its Undo sets the own area back.
      if (id === 'det-area') { const a = v || null; if ((a || '') === ownArea(t) || !AREAS.some(x => x[0] === v)) return false;
        const ch = { area: a }, to = areaName(v).replace('None', 'none');
        window.shell.confirm({ title: `Area → ${to}: ${label(t)}?`, ok: a ? `Show in ${to}` : 'Clear the area', keep: 'Cancel', danger: false, cli: cliQuad(t, ch),
          body: a ? `It shows only in the ${to} tab, not in Personal or its realm tab.` : 'Its repo places it again.' })
          .then(yes => { if (yes) commitEdit(byKey(t.key) || t, ch, `area → ${to}`, { keep: ['own'], before: r => ({ own: (r.area_set && r.area) || null }),
            check: r => ((r.area_set && r.area) || null) !== a || `its area is already ${to}` }); });
        return false; }
      if (id === 'det-q') return v !== quadOf(t) && toQuad(t.key, v);
      if (id === 'det-p') { const n = v ? +v : null; if (n === t.p) return false;
        commitEdit(t, { p: n }, `priority ${t.p ? 'P' + t.p : 'unset'} → ${n ? 'P' + n : 'unset'}`, { check: r => r.p !== n || `it is already ${n ? 'P' + n : 'unset'}` }); return true; }
      if (id === 'det-due') { const d = v || null; if (d === t.due) return false;
        if (!d && repeats(t)) { toast(NO_CLEAR); return false; }
        commitEdit(t, { due: d }, d ? `due ${fmt(d)}` : 'due removed', { check: r => r.due !== d || 'the date is already set' }); return true; }
      // sd:5649: --recur needs a due date; clearing the rule clears its anchor and lead, which Undo sets again.
      if (id === 'det-recur') { const rule = v || null; if (rule === (t.recurrence || null)) return false;
        if (rule && !t.due) { toast(`${label(t)} not changed: --recur needs a due date`); return false; }
        commitEdit(t, { recurrence: rule }, rule ? `repeats ${repeatName(rule).toLowerCase()}` : 'no longer repeats',
          { keep: rule ? ['recurrence'] : ['recurrence', 'anchor', ...(host.lead?.() ? ['lead'] : [])], check: r => (r.recurrence || null) !== rule || 'it repeats so already' }); return true; }
      if (id === 'det-lead') { const n = +v; if (!host.lead?.() || !repeats(t) || n === (t.lead ?? 0)) return false;
        commitEdit(t, { recurrence_lead: n }, `lead ${plural(t.lead ?? 0, 'day')} → ${plural(n, 'day')}`, { keep: ['lead'], check: r => (r.lead ?? 0) !== n || 'the lead is already that' }); return true; }
      if (id === 'det-repo') { const r = v || null; if (r === (t.repo_path || null)) return false;
        commitEdit(t, { repo: r }, `repo → ${r ? r.split('/').pop() : 'none'}`, { keep: ['repo_path'], check: x => (x.repo_path || null) !== r || 'it is already there' }); return true; }
      return false;
    }
    host.details.addEventListener('change', e => {
      const t = byKey(selected()), id = e.target.id || '';
      if (!t || !id.startsWith('det-')) return;
      if (!fieldChange(t, id, e.target.value, e.target.defaultValue)) renderDetails();
    });
    // The v1 item page's sections (review item 17), each with the read it comes from. A row without a reading says so once.
    function more(t, dis = '') {
      if (!t.id) return '';
      const d = detOf(t), body = DET.body[t.id] && DET.body[t.id].text;
      // build (sd:3467): the description edits in place, as the classic item page's Details textarea did; it saves on leaving.
      // edit_item takes a body for a task or a followup only.
      const desc = body === undefined || !['task', 'followup'].includes(t.kind) ? ''
        : body === null ? html`<h3>Description</h3><p class="why">The saved description is not valid JSON, so it is hidden and nothing here can overwrite it. Fix it from the terminal: <code>sd task edit ${t.id} --body</code>.</p>`
        : html`<h3><label for="det-body">Description</label></h3><textarea id="det-body" class="fld" rows="4"${dis}>${body}</textarea>`;
      if (!d) return html`${desc}<h3>History, notes and assignments</h3><p class="why">${DET.failed[t.id] ? `Not read: ${DET.failed[t.id]}` : 'Reading the history, notes and assignments…'}</p>`;
      const it = d.item;
      // The item's cost (sd:6031): each assignment's spent cost rows, `~` when any is an estimate (`bound`); no cap.
      const spent = x => ({ usd: x.assignments.reduce((n, a) => n + (a.usd || 0), 0), estimated: x.assignments.some(a => a.estimated) });
      const cost = a => `${a.estimated ? '~' : ''}$${Number(a.usd).toFixed(2)}`;
      const open = d.notes.filter(n => n.kind === 'followup' && !n.resolved).length;
      const docs = d.documents || [];
      return html`${desc}${baseOf(t, d)}${pull(d)}
        <h3>Documents ${src('Documents', `The files linked to this task: its path, its work folder, design files that name sd:${t.id}, and paths its notes name.`)}</h3>
        ${docs.length ? html`<ol class="rec">${docs.map(x => html`<li><span>${x.url ? html`<a class="ext" href="${x.url}" target="_blank" rel="noopener noreferrer">${x.path}</a>` : html`<code>${x.path}</code>`} <small>${x.why}</small></span></li>`)}</ol>` : html`<p class="why">No design or document names this item.</p>`}
        ${published(t, d.published || [])}
        ${experiment(t, d.experiment)}
        <h3>Status history ${src('Status history', 'Every status change, from the task\'s history.', `sd task show ${t.id} --json`)}</h3>
        ${d.history.length ? html`<ol class="rec">${d.history.map(n => html`<li><time class="rel" datetime="${n.at}"></time><span>${n.body}</span></li>`)}</ol>` : html`<p class="why">This item has never changed status.</p>`}
        <h3>Notes <span class="n">${plural(d.notes.length, 'note')} · ${open} open ${plural.word(open, 'followup')}</span> ${src('Notes', 'Every note on this task but its status changes.', `sd task show ${t.id} --json`)}</h3>
        ${d.notes.length ? html`<ol class="rec">${d.notes.map(n => html`<li data-note="${String(n.id)}"><time class="rel" datetime="${n.at}"></time><span><b>${n.kind}</b>${n.kind === 'followup' ? (n.resolved ? ' · resolved' : ' · open') : ''} ${n.body}</span>${C.rowActions('note:' + n.id)}</li>`)}</ol>` : html`<p class="why">No notes on this item.</p>`}
        <h3>Assignments${d.assignments.length ? html` <span class="n">${cost(spent(d))} spent</span>` : ''} ${src('Assignments', 'Agent runs on this task, with what each cost; ~ marks an estimate.', 'sd assignments list --json')}</h3>
        ${d.assignments.length ? html`<ol class="rec">${d.assignments.map(a => html`<li><span class="mono">#${String(a.id)}</span><span>${a.role} · ${a.provider || 'no provider'} · <b>${a.status}</b>${a.usd ? ` · ${cost(a)}` : ''}${a.ended ? html` · ended <time class="rel" datetime="${a.ended.replace('+00:00', 'Z')}"></time>` : ''}${a.cancel.allowed ? '' : html`<br><small>sd assignments cancel: ${a.cancel.reason}</small>`}</span>${C.rowActions('asg:' + a.id)}</li>`)}</ol>` : html`<p class="why">No assignment has run on this item.</p>`}
        <h3>Artifact ${src('Artifact', 'The file, repo and branch this task points to.', `sd task show ${t.id} --json`)}</h3>
        <dl><dt>Path</dt><dd>${it.path || 'none'}</dd><dt>Repo</dt><dd>${it.repo || 'none'}</dd>${it.branch ? html`<dt>Branch</dt><dd>${it.branch}</dd>` : ''}</dl>
        <h3>Recurrence ${src('Recurrence', 'How this task repeats, and when its next occurrence opens.', `sd task show ${t.id} --json`)}</h3>
        <dl><dt>Repeats</dt><dd>${it.recurrence || 'no'}</dd>${it.recurrence_anchor ? html`<dt>Anchor</dt><dd>${it.recurrence_anchor}</dd>` : ''}${it.recurrence && 'recurrence_lead' in it ? html`<dt>Lead</dt><dd>${plural(it.recurrence_lead ?? 0, 'day')}</dd>` : ''}${opens(t) ? html`<dt>Opens</dt><dd>${t.opensOn}</dd>` : ''}<dt>Revision</dt><dd>${d.revision.slice(0, 12)}…</dd></dl>
        ${external(d)}`;
    }
    // sd:3465: where the item's work was published, each URL under its document's title; tasks_screen._published sets
    // `href` only for an address the page may open, so any other shows as text. Nothing published shows no section.
    function published(t, list) {
      if (!list.length) return '';
      return html`<h3>Published ${src('Published', 'Where this work was published, under each document\'s title.')}</h3>
        <ol class="rec">${list.map(p => html`<li><span><b>${p.title}</b> <small>${p.where}</small>${p.links.map(l => html`<br>${l.channel} ${l.href ? html`<a class="ext" href="${l.href}" target="_blank" rel="noopener noreferrer">${l.url}</a>` : html`<code>${l.url}</code>`}`)}</span></li>`)}</ol>`;
    }
    // sd:4639: the item's row in the team experiments database (pack sd:3982). The select and the row field post what
    // `sd task edit N --experiment` takes (fieldChange); clearing a pinned row is `new`, so the next drain creates one.
    // `off` keeps the row and is offered once the item is designated; a designation the page cannot read turns both off.
    function experiment(t, x) {
      if (!x) return '';
      const off = x.reason ? html` disabled aria-describedby="det-exp-off"` : live(t.key) ? '' : html` disabled`;
      const opt = (value, name) => html`<option value="${value}"${value === x.value ? html` selected` : ''}>${name}</option>`;
      const lost = x.value && x.value.startsWith('of:') && !x.choices.some(c => c.value === x.value);
      return html`<h3><label for="det-exp">Experiments</label> ${src('Experiments', 'This task\'s row in the experiments database. Its link shows under Published once it is copied there.')}</h3>
        <select id="det-exp" class="fld"${off}>${x.value ? '' : html`<option value="" selected>not projected</option>`}${opt('yes', 'experiment')}${x.choices.map(c => opt(c.value, c.label))}${lost ? opt(x.value, `task of #${x.value.slice(3)}`) : ''}${x.value ? opt('off', 'off, keep the row') : ''}</select>
        ${x.value ? html`<dl class="fields"><dt><label for="det-exp-row">Row</label>${hint('Row', 'The page id or URL of this item\'s row in the experiments database.')}</dt><dd><input id="det-exp-row" class="fld" value="${x.row || ''}" placeholder="a page id or URL"${off}></dd></dl>` : ''}
        ${x.reason ? html`<p class="why" id="det-exp-off">${x.reason}.</p>` : ''}`;
    }
    // build (sd:3977): a vault base row's stage, declared fields, body and its section of the vault mirror page.
    // The stage select is the row's own write: edit_item's refusal of an idea's other fields does not turn it off.
    function baseOf(t, d) {
      const b = d.base; if (!b) return '';
      const off = b.reason || (!live(t.key) && 'this task was not read again after the change') || (!b.stages.length && 'no move leaves this stage');
      return html`<h3>Base · ${b.kind} ${src('Base', `This record's stage and fields, as the ${b.kind} base defines them.`, `sd-db records render ${b.kind}`)}</h3>
        <dl class="fields"><dt><label for="det-stage">Stage</label>${hint('Stage', 'The row\'s stage in its base; a change moves the row.')}</dt><dd><select id="det-stage" class="fld"${off ? html` disabled aria-describedby="det-base-off"` : ''}><option value="${b.stage}" selected>${b.stage}</option>${b.stages.map(s => html`<option value="${s}">${s}</option>`)}</select></dd>
          ${b.declared.map(f => html`<dt>${f}${hint(f, `A field the ${b.kind} base declares, as the row holds it.`)}</dt><dd>${Array.isArray(b.fields[f]) ? b.fields[f].join(', ') : b.fields[f] ?? '—'}</dd>`)}
          <dt>Mirror${hint('Mirror', 'The section of the vault mirror page that shows this row.')}</dt><dd>${!b.mirror ? 'no vault named' : b.mirror.url ? html`<a href="${b.mirror.url}" target="_blank" rel="noopener">its section</a>` : html`<code>${b.mirror.path}</code>`}</dd></dl>
        ${off ? html`<p class="why" id="det-base-off">${off}.</p>` : ''}
        ${b.body ? html`<pre class="body">${b.body}</pre>` : ''}`;
    }
    // sd:5903: each Details field's label carries the same help icon, with one plain sentence on what the field holds.
    function hint(name, text) {
      return html`<button class="help" type="button" aria-label="Help: ${name}" data-help="${text}">${ICON('circle-help')}</button>`;
    }
    // Each Details section says what it shows in a help icon beside its heading (sd:3444). Pack sd:7536: plain words first;
    // a section with a command that prints the same reading names it after them, and pressing the icon copies it.
    function src(name, says, cmd = '') {
      if (!cmd) return hint(name, says);
      return html`<button class="help src" type="button" aria-label="Help: ${name}" data-help="${`${says} Press to copy the command: <code>${cmd}</code>`}" data-copy="${cmd}">${ICON('circle-help')}</button>`;
    }
    // pack sd:7868: the item's linked pull request (a build's draft in an unmanaged repository), with a link that opens it,
    // and its needs-you flag while the contribution lane says it waits on the operator, such as a draft not yet ready.
    function pull(d) {
      const p = d.pull; if (!p) return '';
      return html`<h3>Pull request ${src('Pull request', 'The pull request this task is linked to, and whether it waits on you.')}</h3>
        <dl><dt>Link</dt><dd><a class="ext" href="${p.url}" target="_blank" rel="noopener noreferrer" aria-label="Open the pull request of sd:${d.item.id} on GitHub">${p.url}</a></dd>
          <dt>State</dt><dd>${p.state || 'not observed yet'}${p.lane ? ` · ${p.lane.replace('_', ' ')}` : ''}</dd></dl>
        ${p.needs_you ? html`<p class="why" data-needs-you=""><b>Needs you:</b> ${p.reasons.join('; ') || 'it waits on you'}</p>` : ''}
        ${p.error ? html`<p class="why">${p.error}</p>` : ''}`;
    }
    // build: v1's External context (screens.external_context), the shadow row and its collector's freshness, when the item
    // has a tracker reference. Local status is the item's own; the tracker's state is dated context beside it.
    function external(d) {
      const x = d.external; if (!x) return '';
      return html`<h3>External context ${src('External context', 'What the tracker says about this task, and how fresh the last sync is.')}</h3>
        <dl><dt>Tracker</dt><dd>${x.tracker}</dd><dt>External state</dt><dd>${x.state || (x.last_seen ? 'unknown' : 'not in the local snapshot')}</dd>
          <dt>Reference</dt><dd>${x.url}</dd><dt>Last seen</dt><dd>${x.last_seen ? html`<time class="rel" datetime="${x.last_seen}"></time>` : 'never'}</dd>
          <dt>Sync health</dt><dd>${x.freshness.state}</dd><dt>Last sync</dt><dd>${x.freshness.last_success_at ? html`<time class="rel" datetime="${x.freshness.last_success_at}"></time>` : 'never'}</dd></dl>
        ${x.freshness.reason ? html`<p class="why">${x.freshness.reason}</p>` : ''}`;
    }
    function move(key, to) {
      const t = byKey(key), L = legal(t, to);
      if (!L.ok) { toast(`${label(t)} not moved: ${L.reason}`); return false; }
      C.run(to === 'done' && repeats(t) ? COMPLETE : STATUS_CMD[to], C.get(key));
      return true;
    }
    // build: a quadrant drop is an edit; it lands through the same write, toasts after, and Undo edits the fields back.
    function commitEdit(t, ch, words, opts) {
      // A page dialog, not a shell command: it toasts on its own, with the same Undo a command gets.
      editRun(t, ch, () => `${label(t)} ${words}${' · ' + cliQuad(t, ch)}`, opts).then(r => toast(r.text, () => r.undo().then(
        ok => { if (ok) toast(`Edit undone · ${label(t)}`); }, err => failed(label(t), err))), err => failed(label(t), err));
    }
    function toQuad(key, q) {
      const t = byKey(key), Q = quadChange(t, q);
      if (!Q.ok) { if (Q.reason !== 'Already here.') toast(`${label(t)} not moved: ${Q.reason}`); return false; }
      const name = QUADS.find(x => x[0] === q)[1];
      const commit = ch => {
        const w = [];
        if ('p' in ch) w.push(Q.words[0]);
        if ('due' in ch) w.push(`due ${ch.due ? fmt(ch.due) : 'removed'}`);
        commitEdit(t, ch, `→ ${name}: ${w.join(', ')}`);
      };
      if (!Q.needsDate) { commit(Q.ch); return true; }
      askDate(t, Q.needsDate, name, due => commit({ ...Q.ch, due }));
      return false;
    }
    // build: workflow._recurring refuses a repeating item with no due date, so neither dialog offers Remove due date on one;
    // it says why instead (review, PR #46).
    const NO_CLEAR = 'Remove due date is off: a repeating task needs a due date. Stop repeating first to remove it.';
    // Edit: priority and due date. Save applies both with Undo; Cancel changes nothing.
    function askEdit(t) {
      put(dateDlg, html`<form method="dialog" class="date-form">
        <h2 id="date-h">Edit ${label(t)}</h2>${window.shell.closeX}
        <label class="label" for="pri-in">Priority</label>
        <select id="pri-in">${t.p ? '' : html`<option value="" selected>not recorded</option>`}${[1, 2, 3, 4].map(v => html`<option value="${v}"${t.p === v ? html` selected` : ''}>P${v}</option>`)}</select>
        <label class="label" for="due-in">Due date</label>
        <input id="due-in" type="date" value="${String(t.due || '')}">
        ${t.due && repeats(t) ? html`<p class="why">${NO_CLEAR}</p>` : ''}
        <div class="cli"><code id="edit-cli"></code></div>
        <div class="actions">
          <button class="btn" value="set" type="submit">Save</button>
          ${t.due && !repeats(t) ? html`<button class="btn quiet" value="clear" type="submit" formnovalidate>Remove due date</button>` : ''}
          <button class="btn quiet" value="cancel" type="submit" formnovalidate data-cancel>Cancel</button>
        </div></form>`);
      const f = dateDlg.querySelector('form');
      const change = clear => { const ch = {}, p = f.querySelector('#pri-in').value, d = f.querySelector('#due-in').value; if (p && +p !== t.p) ch.p = +p; if (clear) ch.due = null; else if (d && d !== t.due) ch.due = d; return ch; };
      const upd = () => { const ch = change(false); f.querySelector('#edit-cli').textContent = Object.keys(ch).length ? cliQuad(t, ch) : 'no change yet'; };
      f.addEventListener('input', upd); upd();
      dateDlg.onclose = () => {
        const v = dateDlg.returnValue; if (v !== 'set' && v !== 'clear') return;
        const ch = change(v === 'clear'); if (!Object.keys(ch).length) { toast(`${label(t)} unchanged.`); return; }
        commitEdit(byKey(t.key), ch, 'edited');
      };
      dateDlg.returnValue = '';
      dateDlg.showModal(); f.querySelector('#pri-in').focus();
    }
    // Date prompt for a move across the urgency line. Cancel changes nothing.
    const dateDlg = document.createElement('dialog');
    dateDlg.className = 'palette date-dlg'; dateDlg.setAttribute('aria-labelledby', 'date-h'); put(dateDlg, html`<h2 id="date-h"></h2>`);
    document.body.append(dateDlg);
    function askDate(t, dir, name, done) {
      const into = dir === 'into';
      // The boundary is withinWeek's, the server's rule from the read stamp, not 7 local days: the last urgent date is the day
      // before the first one withinWeek calls not urgent, so a picked date always lands in the quadrant named (review, PR #46).
      let n = 0; while (withinWeek({ due: plus(n + 1) }) && n < 400) n++;
      const last = plus(n), min = into ? plus(0) : plus(n + 1), max = into ? last : '';
      put(dateDlg, html`<form method="dialog" class="date-form">
        <h2 id="date-h">${label(t)} → ${name}</h2>${window.shell.closeX}
        <p class="why">${into ? 'Urgent means due within 7 days. Pick the real deadline.' : `Not urgent means due after ${fmt(last)}. ${t.due ? `The current deadline is ${fmt(t.due)}; moving it changes the obligation.` : ''}`}</p>
        ${!into && repeats(t) ? html`<p class="why">${NO_CLEAR}</p>` : ''}
        <label class="label" for="due-in">Due date</label>
        <input id="due-in" type="date" required min="${min}"${max ? html` max="${max}"` : ''} value="">
        <div class="actions">
          <button class="btn" value="set" type="submit">Set date and move</button>
          ${!into && !repeats(t) ? html`<button class="btn quiet" value="none" type="submit" formnovalidate>Remove due date</button>` : ''}
          <button class="btn quiet" value="cancel" type="submit" formnovalidate data-cancel>Cancel</button>
        </div></form>`);
      dateDlg.onclose = () => {
        const v = dateDlg.returnValue, d = dateDlg.querySelector('#due-in').value;
        if (v === 'set' && d) done(d);
        else if (v === 'none') done(null);
        else toast(`${label(t)} not moved.`);
      };
      dateDlg.returnValue = '';
      dateDlg.showModal(); dateDlg.querySelector('#due-in').focus();
    }
    // Move to: one button per status in board order, keys 1–5 as on the board. The move runs the status command, so it keeps Undo.
    function askMove(t) {
      put(dateDlg, html`<form method="dialog" class="date-form">
        <h2 id="date-h">Move ${label(t)} to</h2>${window.shell.closeX}
        <p class="why">Now in ${slabel(t.status)}. A column this task cannot take is off and says why.</p>
        <div class="moves">${STATUSES.map(([s, name], i) => { const L = legal(t, s); return html`<div class="mv">
          <button class="btn${L.ok ? '' : ' quiet'}" type="submit" value="${s}" data-move="${s}"${L.ok ? '' : html` disabled aria-describedby="mv-${s}"`}><span>${name}</span><kbd>${i + 1}</kbd></button>
          ${L.ok ? html`<code>${cliMove(t, s)}</code>` : html`<p class="why" id="mv-${s}">${L.reason}</p>`}</div>`; })}</div>
        <div class="actions"><button class="btn quiet" value="cancel" type="submit" formnovalidate data-cancel>Cancel</button></div></form>`);
      const f = dateDlg.querySelector('form');
      f.addEventListener('keydown', e => { const S = STATUSES[parseInt(e.key, 10) - 1], b = S && f.querySelector(`[data-move="${S[0]}"]`); if (b && !b.disabled) { e.preventDefault(); b.click(); } });
      dateDlg.onclose = () => {
        const v = dateDlg.returnValue; if (!SLABEL[v]) return;
        if (move(t.key, v)) document.querySelector(`.card[data-key="${t.key}"]`)?.focus();
      };
      dateDlg.returnValue = '';
      dateDlg.showModal(); (f.querySelector('[data-move]:not(:disabled)') || f.querySelector('[value="cancel"]')).focus();
    }
    document.addEventListener('adversarial:run', e => { if (e.detail.key === `task:${byKey(selected())?.id}`) renderDetails(); });
    return { shape, label, opens, STATUSES, SLABEL, slabel, QUADS, TODAY, plus, fmt, days, withinWeek, quadOf, dueText, legal, quadChange,
      EDITABLE, repeats, DET, live, objectOf, putAll, readDet, redrawDet, adopt, write, renderDetails, fieldChange, move, toQuad, askEdit, STATUS_CMD, COMPLETE };
  }

  window.taskDetails = { shape, attach };
})();
