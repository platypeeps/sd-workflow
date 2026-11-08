// Tasks (sd:2124): the design source's products/system/designs/v2/tasks.js at d82daa1, ported. Each change from the reference is marked
// "build:". The rows are /api/tasks and the Details reading /api/tasks/<id> (tasks_screen.py), never sample data. A command
// that executes posts to the route v1 already answers, and its toast comes after the write lands, not before.
// Palette "This page" group. shell.js reads it at start, so it is set before the shell runs.
const { html, put, plural } = window.markup;
const tasksView = v => () => document.dispatchEvent(new CustomEvent('tasks:view', { detail: v }));
window.PAGE_COMMANDS = [
  { label: 'Add or find a task', icon: 'plus', run: () => document.getElementById('shift-in').focus() },
  { label: 'Show the list', icon: 'list-todo', key: 'v l', run: tasksView('list') },
  { label: 'Show the board', icon: 'kanban', key: 'v b', run: tasksView('board') },
  { label: 'Show the matrix', icon: 'grid-2x2', key: 'v m', run: tasksView('matrix') },
  { label: 'Start next ready tasks', icon: 'play', run: () => document.dispatchEvent(new CustomEvent('tasks:start-next')) },
];
// sd:6263: each realm is a sub tab, in place of sd:5753's Realm filter. The tabs narrow the one board, matrix and list, so
// they have no view-<id> panel: the shell draws the switch in #viewswitch. Personal, then the realms the dashboard.realms
// row names (SHELL_REALMS, from /ui/sections.js), then HOA and Home. A realm named for one of those tabs, or for a
// layout panel (list, board, matrix) or the layout menu's view-btn and view-menu (sd:5641), takes no tab: the shell
// would hide that panel. Home takes v o (g o opens the Home page), since v h is HOA's; a realm takes the first letter
// of its id that no other tab, and not the layouts' v l, v b and v m, holds. pack sd:7216: no All tab; every task sits in
// Personal or a realm (inPlace), so All only repeated them. An old #view=all opens Personal; ?realm=all, Today's and
// Metrics' drilldowns, still lists every realm (PLACE below). 'all' stays reserved and v a unused, so no key moves.
if (location.hash === '#view=all') history.replaceState(history.state, '', '#view=personal');
window.PAGE_VIEWS = [{ id: 'personal', label: 'Personal' },
  ...(window.SHELL_REALMS || []).filter(r => /^[a-z0-9][a-z0-9-]*$/.test(r.id) && !['personal', 'hoa', 'home', 'all', 'list', 'board', 'matrix', 'btn', 'menu'].includes(r.id)).map(r => ({ id: r.id, label: r.label })),
  { id: 'hoa', label: 'HOA', key: 'h' }, { id: 'home', label: 'Home', key: 'o' }];
{
  const used = new Set(['l', 'b', 'm', 'a', ...window.PAGE_VIEWS.map(v => v.key).filter(Boolean)]);
  window.PAGE_VIEWS.forEach(v => { if (!v.key) { v.key = [...v.id].find(c => /[a-z]/.test(c) && !used.has(c)); used.add(v.key); } });
}
addEventListener('DOMContentLoaded', () => {
  const { ICON, toast, suggest, openPane } = window.shell;
  const C = window.shell.commands;
  // A task's Details, its commands, writes and dialogs are task-details.js's (sd:5635), as the Home and realm place pages
  // draw them too. This page gives it its rows, selection, read and redraw, and the lines only Tasks says.
  const TD = window.taskDetails.attach({ details: document.getElementById('details'), rows: () => tasks, byKey: k => byKey(k),
    selected: () => selected, read: () => READ, view: () => view, render: () => render(), reread: () => reread(), load: () => load(),
    select: (key, open) => select(key, open), landed: key => landed(key), lead: () => LEAD, got: (id, got) => pin(id, got), empty: () => empty(), note: t => note(t),
    foot: () => READ ? html`Rows read <time class="rel" datetime="${READ}"></time> from /api/tasks (the reads v1 /backlog makes).` : '' });
  const { label, opens, STATUSES, SLABEL, slabel, QUADS, TODAY, plus, fmt, days, withinWeek, quadOf, dueText, legal, quadChange, EDITABLE, repeats, live,
    objectOf, putAll, readDet, redrawDet, write, renderDetails, fieldChange, move, toQuad, askEdit, STATUS_CMD, COMPLETE } = TD;
  const WD = ['sun','mon','tue','wed','thu','fri','sat'];

  // ---------- Data (build) ----------
  // /api/tasks rows, in task-details.js's task shape (TD.shape).
  let tasks = [], READ = null, AGES = [], ATTENTION = null;
  // pack sd:5648: whether the store takes a lead (tasks_screen._has_lead). A store before schema 41 has none, so the page
  // shows no Lead field and no Lead column, and writes no lead.
  let LEAD = false, IDEAS = new Set();
  // sd:5462: a linked item the read does not list, a quiet report or one done before the week, opens in Details all the
  // same. PIN is that item: its key, and its row once its /api/tasks/<id> reading answers (details' `row`), marked with why
  // the list leaves it out. The row is kept beside `tasks`, so no view, count or filter lists it; a read that lists it ends PIN.
  let PIN = null;
  // LINK is the ?row= the page opened at when the read lists it: a view or filter that hides it keeps it selected, with a
  // note, until another is picked (a done report the board hid lost its selection to the first card, sd:5462).
  let LINK = null;
  const pinned = k => !!PIN && PIN.key === k, held = k => pinned(k) || (!!LINK && LINK === k);
  const byKey = k => tasks.find(t => t.key === k) || (pinned(k) && PIN.row) || undefined;
  // The linked item's reading names its row: it becomes PIN's, marked with why the list leaves it out. Its first selects it.
  function pin(id, got) {
    if (!pinned(String(id)) || !got.row) return false;
    const first = !PIN.row;
    PIN.row = { ...TD.shape(got.row), unlisted: got.left_out || 'the read does not hold it' }; C.put(objectOf(PIN.row));
    return first && selected === PIN.key;
  }
  // Details with nothing selected, and the line under a task's title: why the list, view or filters leave it out.
  const empty = () => !READ ? 'The tasks were not read, so nothing is selected.'
    : pinned(selected) ? (TD.DET.failed[selected] ? `#${selected} did not open: ${TD.DET.failed[selected]}.` : `Reading #${selected}…`)
    : tasks.some(x => !live(x.key)) ? 'The tasks were not read again after the change, so nothing is selected. Reload reads them.'
    : !tasks.length ? 'No open task, so nothing is selected.' : !visible().length ? 'No task matches these filters. Clear them to see the list.'
    : 'Select a task to see its fields and moves.';
  const note = t => t.unlisted ? html`<p class="why">The list, board and matrix leave #${t.id} out: ${t.unlisted}.</p>`
    : LINK === t.key && !visible().some(x => x.key === t.key) ? html`<p class="why">The view or filters hide #${t.id}; it stays selected until you pick another.</p>` : '';
  // The rows every view lists: a parked row shows only under Parked or All (sd:3012, sd:6701), and a scheduled occurrence,
  // a repeating task's next row before due minus its lead (reads.opens_on_clause, pack sd:5648), under Scheduled only.
  // pack sd:6743: an idea kind (/api/tasks' `ideas`) shows only when Kind picks its kind or base, as the Vault view.
  const picked = t => F.kind.has(t.kind) || (!!t.base && F.kind.has(`base:${t.base}`));
  const work = t => !IDEAS.has(t.kind) || picked(t);
  const listed = () => tasks.filter(t => !t.parked && !t.scheduled && work(t));
  let PARKED = 'open';
  const scoped = t => t.parked ? window.shell.aside.shows(PARKED, true)
    : window.shell.aside.shows(PARKED, false) && !!t.scheduled === F.active.has('scheduled');
  // sd:6263: the shown realm tab (PAGE_VIEWS above). A row's area is the server's (places.area_of), the rule the HOA and
  // Home pages' Tasks views use, so HOA and Home hold their rows; its realm is places.realm_of (sd:5064). An address's
  // ?realm= shows that tab. Metrics' and Today's drilldowns count every realm and name ?realm=all (pack sd:7216): PLACE is
  // 'all', every row shows, no tab is current and the head says All realms, until a tab is picked.
  const asked = new URLSearchParams(location.search).get('realm');
  if (asked === 'all') window.shell.view.none(); else if (asked) window.shell.view.show(asked);
  let PLACE = asked === 'all' ? 'all' : window.shell.view.current() || window.PAGE_VIEWS[0].id, REALMS = null;
  const place = () => PLACE;
  document.addEventListener('shell:view', e => { if (e.detail.id === PLACE) return; PLACE = e.detail.id; L.page = 1; if (READ) { subhead(); render(); } });
  // sd:5096, sd:5097: the Personal and Work sweeps, as /api/tasks' `sweeps` reads them (sweep.status): running, the last run.
  let SWEEPS = null, sweepTimer = 0;
  const SWEEP_NAME = { personal: 'Personal', work: 'Work' }, SWEEP_POLL = 15000;
  const sweepOf = o => SWEEPS?.[String(o.id).slice(6)] || null;
  const sweepText = w => !w ? '' : w.error ? `sweep not read: ${w.error}` : w.running ? `sweep running${w.last?.started ? ` since ${w.last.started.slice(11, 16)} UTC` : ''}`
    : !w.last || w.last.state === 'never' ? 'sweep not run yet' : w.last.state === 'done' ? `sweep done${w.last.finished ? ` ${w.last.finished.slice(0, 16).replace('T', ' ')} UTC` : ''}${w.last.gaps?.length ? ` · not read: ${w.last.gaps.join(', ')}` : ''}`
    : `sweep ${w.last.state}${w.last.exit != null ? `, exit ${w.last.exit}` : ''}${w.last.reason ? `: ${w.last.reason}` : ''}`;
  // A row's realm tab: its realm when that has a tab, else Personal (no realm list read, or a realm with no tab: pack sd:7216).
  // pack sd:7687: the hoa checkout's rows are realm hoa (places.realm_of), so they sit in HOA, not Personal too.
  // pack sd:8010: a row with its own area (area_set, sd task edit --area) sits in that area's tab alone. pack sd:8062:
  // the area may be personal or a realm id; one with no tab (a realm since dropped) falls back to the repo rule.
  const tabOf = t => t.area_set && window.PAGE_VIEWS.some(v => v.id === t.area) ? t.area : t.realm !== 'home' && window.PAGE_VIEWS.some(v => v.id === t.realm) ? t.realm : 'personal';
  const inPlace = (t, p = place()) => p === 'all' || (p === 'hoa' || p === 'home' ? t.area === p : tabOf(t) === p);
  const placed = () => listed().filter(t => inPlace(t));

  function state(t) {
    if (t.status === 'done') return '';
    if (t.overdue || (t.due && days(t) < 0)) return 'warning';
    if (t.due && days(t) <= 2) return 'caution';
    return '';
  }
  const GLYPH = { warning: '■', caution: '▲' };

  // Page attention: the read's own badge (sd:3446), the rule every page's rail shows: v2/pages/tasks.py. build: set after each read.
  function attention() {
    window.shell.attention?.(READ && ATTENTION ? ATTENTION : { state: 'unknown', n: 0, what: 'tasks not read' });
  }

  // ---------- Filters ----------
  // build: Kind joins Repo, Priority and Due, as v1 /backlog filters by kind and repo (review 2026-09-29, item 17).
  // build (sd:2589): Status, In status (the reads.age_bucket key each row carries), Open only, the text filter and the list's
  // page take the query v1 /backlog took (status, age, active=1, q, page), so Operations' histogram bars and the capture
  // check land here with their filter. sd:6233: Status has no dropdown now; an address's ?status= (Today's Landed 24h and
  // Blocked) still narrows, and its chip clears it.
  // build (sd:3012): Parked (sd:6701) swaps the listed rows for the ones the nightly prune parked, for triage's Unpark.
  // build (sd:3977): Base narrows to one vault base's rows; it shows once a row has a base.
  const F = { kind: new Set(), repo: new Set(), p: new Set(), due: new Set(), status: new Set(), age: new Set(), active: new Set() };
  // sd:5649: the list's optional columns the Columns menu shows, kept in the address as ?cols=.
  // sd:6233: and whether the board shows its Done column, ?done=1; hidden by default (operator decision, note 30400).
  const SHOW = { cols: new Set(), done: false }, COL_OPTS = [['lead', 'Lead (days)']];
  const FKEYS = Object.keys(F);
  const find = document.getElementById('find-in');
  let Q = '';
  const on = () => FKEYS.reduce((n, k) => n + F[k].size, 0) + (Q ? 1 : 0);
  const dueBucket = t => t.overdue || (t.due && days(t) < 0) ? 'overdue' : !t.due ? 'none' : withinWeek(t) ? 'week' : 'later';
  const DUE_OPTS = [['overdue', 'overdue'], ['week', '≤ 7 days'], ['later', 'later'], ['none', 'no due']];
  function renderFilters() {
    const repos = [...new Set(tasks.map(t => t.repo))].sort(), kinds = [...new Set(tasks.map(t => t.kind))].sort();
    const bases = [...new Set(tasks.map(t => t.base).filter(Boolean))].sort();
    // build (sd:3441, sd:5064, sd:5158): every filter is a dropdown (shell.list.pick), one value or All, and Repo a menu of
    // checkboxes, any repos at once; the active-filter chips under them are the only chips. Values the address names that no
    // row has are options too, and more than one (?p=1,2) shows as a count until one is picked. Each option is [value, label];
    // `all` names the empty choice.
    const combo = (key, name, opts, help, all = 'All', multi = false) => window.shell.list.pick(key, name,
      [...new Map([...opts, ...[...F[key]].map(v => [v, fvalue(key, v)])])], F[key], { all, help, multi });
    const same = vs => vs.map(v => [v, v]);
    // sd:5644: one Kind filter. A plugin kind (the row's base: lead, blog-idea…) is a `base:` value after the item kinds.
    put(document.getElementById('filters'), html`${combo('kind', 'Kind', [...same(kinds), ...bases.map(b => [`base:${b}`, kindWord(`base:${b}`)])])}${combo('repo', 'Repo', same(repos), '', 'All', true)}${
      combo('p', 'Priority', [['1', 'P1'], ['2', 'P2'], ['3', 'P3'], ['4', 'P4'], ['', 'unset']].map(([v, l]) => [v || 'unset', l]))}${
      combo('due', 'Due', DUE_OPTS, html`<button class="help" type="button" aria-label="Help: due filter" data-help="Buckets count from today. <b>≤ 7 days</b> is the same window the matrix calls urgent. An old link may name two buckets; they widen the view, and filters narrow it.">${ICON('circle-help')}</button>`)}${
      combo('age', 'In status', AGES.map(a => [a.key, a.label]), html`<button class="help" type="button" aria-label="Help: in status filter" data-help="Days since the task last changed status, in the buckets the Progress histogram counts. A bar there opens its bucket here.">${ICON('circle-help')}</button>`)}${
      // sd:5643: Scope is one choice: open and done (the default) or open only; sd:5649: or Scheduled only, the repeating
      // tasks' next rows that have not opened yet. Parked left it for the toolbar's Open | Parked (sd:6701).
      combo('active', 'Scope', [['1', 'Open only'], ['scheduled', 'Scheduled only']], html`<button class="help" type="button" aria-label="Help: scope filter" data-help="<b>Open only</b> hides Done tasks, as the Operations histogram counts only open ones. The nightly prune parks an open P4 task untouched for 30 days, and a planning task that never ran untouched for 90 days; parked tasks show under the toolbar's <b>Parked</b> or <b>All</b>, and <b>Unpark</b> brings one back with its fields and history. Completing a repeating task opens its next row at once; it stays off every other scope, and off Today, until its due date less its lead. <b>Scheduled only</b> lists those rows with the day each opens.">${ICON('circle-help')}</button>`, 'Open and done')}${
      // sd:5649: the list's optional columns, hidden by default. Not a filter: no chip, and Clear all keeps it.
      view === 'list' && LEAD ? window.shell.list.pick('cols', 'Columns', COL_OPTS, SHOW.cols, { all: 'Default', multi: true }) : ''}${
      // sd:6233: the board's Done column, off by default. Not a filter either: the list and the matrix ignore it.
      view === 'board' ? html`<div class="fgroup"><label class="chk"><input type="checkbox" id="f-done"${SHOW.done ? html` checked` : ''}> Show done</label></div>` : ''}${
      (on() ? window.shell.list.chips(activeFilters(), visible().length, placed().length) : html`<div class="fsum">${ICON('filter')}<span>${placed().length} tasks</span></div>`)}`);
  }
  // Active filters above the list (shell.list, sd:2682): one chip per value, keyed field:value, and one for the text.
  const FNAME = { kind: 'Kind', repo: 'Repo', p: 'Priority', due: 'Due', status: 'Status', age: 'In status', active: 'Scope' };
  const kindWord = v => (v.startsWith('base:') ? `${v.slice(5)} (plugin kind)` : v);
  const fvalue = (k, v) => k === 'kind' ? kindWord(v) : k === 'p' ? (v && v !== 'unset' ? 'P' + v : 'unset') : k === 'due' ? DUE_OPTS.find(o => o[0] === v)?.[1] || v : k === 'status' ? slabel(v)
    : k === 'age' ? AGES.find(a => a.key === v)?.label || v : k === 'active' ? (v === 'scheduled' ? 'Scheduled only' : 'Open only') : v;
  const activeFilters = () => [...FKEYS.flatMap(k => [...F[k]].map(v => ({ key: `${k}:${v}`, label: `${FNAME[k]}: ${fvalue(k, v)}` }))), ...(Q ? [{ key: 'q', label: `Text: ${Q}` }] : [])];
  document.getElementById('filters').addEventListener('click', e => {
    if (e.target.closest('[data-unfilter-all]')) { clearFilters(); return; }
    const u = e.target.closest('[data-unfilter]'); if (!u) return;
    const [k, v] = u.dataset.unfilter.split(/:(.*)/);
    if (k === 'q') { Q = ''; find.value = ''; } else F[k].delete(v);
    L.page = 1; render();
  });
  document.getElementById('filters').addEventListener('change', e => {
    // The redraw replaces the checkbox, so focus goes back to the new one.
    if (e.target?.id === 'f-done') { SHOW.done = !!e.target.checked; render(); document.getElementById('f-done')?.focus(); return; }
    window.shell.list.pickChange(e, F, () => { L.page = 1; render(); }) || window.shell.list.pickChange(e, SHOW, render);
  });
  function clearFilters() { Object.values(F).forEach(s => s.clear()); Q = ''; find.value = ''; L.page = 1; render(); }
  // The text filter narrows on what a row shows: id, title, repo, status and kind, as v1's ?q= read the visible columns.
  find.addEventListener('input', () => { Q = find.value.trim().toLowerCase(); L.page = 1; render(); });
  find.addEventListener('keydown', e => { if (e.key === 'Escape' && find.value) { e.stopPropagation(); find.value = ''; Q = ''; L.page = 1; render(); } });
  const words = t => `#${t.id} ${t.title} ${t.repo} ${slabel(t.status)} ${t.kind} ${t.stage || ''}`.toLowerCase();
  const passes = t => (!F.kind.size || picked(t)) && work(t) && (!F.repo.size || F.repo.has(t.repo)) && (!F.p.size || F.p.has(t.p ? String(t.p) : 'unset')) && (!F.due.size || F.due.has(dueBucket(t)))
    && (!F.status.size || F.status.has(t.status)) && (!F.age.size || F.age.has(t.age)) && (!F.active.has('1') || t.status !== 'done') && scoped(t) && inPlace(t) && (!Q || words(t).includes(Q));
  const visible = () => tasks.filter(passes);


  // ---------- Rendering ----------
  let view = 'board', selected = null;
  let checked = new Set(); // mirror of the shell's picked rows
  // build (sd:3440): a card's priority and due date are dropdowns, each the Details field's write (fieldChange). Due offers
  // the near dates; Pick a date opens Edit. A row edit_item refuses, or one not read again, keeps the plain labels.
  const tileF = t => EDITABLE(t) && live(t.key);
  function dueSelect(t, dt, dc) {
    const near = [[plus(0), 'Today'], [plus(1), 'Tomorrow'], [plus(7), 'In a week']].filter(([d]) => d !== t.due);
    return html`<select class="tile-f due ${dc}" data-edit="due" data-key="${t.key}" aria-label="Due date of ${label(t)}"><option value="${String(t.due || '')}" selected>${t.due ? dt : 'No due date'}</option>${
      near.map(([d, w]) => html`<option value="${d}">${w} · ${fmt(d)}</option>`)}${t.due && !repeats(t) ? html`<option value="">No due date</option>` : ''}<option value="pick">Pick a date…</option></select>`;
  }
  // sd:5103: the issue File issue filed and linked, from the item's ref (tasks_screen's issue_link), on its card and row.
  const issueLink = t => t.issueLink ? html`<a class="ext issue-link" href="${t.issueLink}" target="_blank" rel="noopener noreferrer" aria-label="Filed issue for ${label(t)}" title="${t.issueLink}">${ICON('link-2')}</a>` : '';
  // sd:5917: a card's Done as an icon: the Done command, or Done → next occurrence on a repeating task, with its toast and
  // Undo. The shell runs a data-cmd click before the card's own click; a button starts no drag. Off, it says why.
  function doneBtn(t) {
    const c = repeats(t) ? COMPLETE : STATUS_CMD.done, o = C.get(t.key), off = o ? C.offWhy(c, o) : 'the task is no longer listed';
    return html`<button class="icon-btn tile-done" type="button" data-cmd="${c.id}" data-obj="${t.key}" aria-label="Mark ${t.title} done"${off ? html` disabled title="${off}"` : ''}>${ICON('check')}</button>`;
  }
  function card(t, compact) {
    const st = state(t), [dt, dc] = dueText(t);
    return html`<article class="card" tabindex="0" data-key="${t.key}"${t.key === selected ? html` aria-current="true"` : ''}${checked.has(t.key) ? html` data-picked` : ''}${st ? html` data-state="${st}"` : ''} aria-label="${label(t) + ' ' + t.title}">
      <div class="top">
        <label class="pick"><input type="checkbox" data-check="${t.key}" aria-label="Select ${label(t)}"${checked.has(t.key) ? html` checked` : ''}><span></span></label>
        ${st ? html`<span class="g g-${st}" aria-hidden="true">${GLYPH[st]}</span>` : ''}
        <span class="id">${label(t)}</span>${issueLink(t)}
        ${tileF(t) ? html`<select class="tile-f pri" data-edit="p" data-key="${t.key}" data-p="${String(t.p || '')}" aria-label="Priority of ${label(t)}">${t.p ? '' : html`<option value="" selected>P–</option>`}${[1, 2, 3, 4].map(n => html`<option value="${n}"${t.p === n ? html` selected` : ''}>P${n}</option>`)}</select>`
          : html`<span class="pri" data-p="${String(t.p || '')}" title="Priority">${t.p ? 'P' + t.p : 'P–'}</span>`}
        ${t.assignment === 'running' ? html`<span class="lock" title="Runner assignment running">${ICON('lock')}</span>` : t.assignment === 'queued' ? html`<span class="queued" title="Build queued; it starts when a build slot frees up">queued</span>` : ''}
        ${doneBtn(t)}${C.rowActions(t.key)}
      </div>
      <div class="t">${t.title}</div>
      <div class="meta"><span class="repo">${t.repo}</span>${tileF(t) ? dueSelect(t, dt, dc) : html`<span class="due ${dc}">${dt}</span>`}${opens(t) ? html`<span class="opens">${opens(t)}</span>` : ''}${compact ? html`<span>${slabel(t.status).toLowerCase()}</span>` : ''}</div>
    </article>`;
  }
  // sd:5305: the board's Done column holds only what was done in the 7 days before the read; the list keeps every done item.
  // A done item with no known time stays: unknown is not old. sd:6233: the column shows only with Show done.
  const DONE_DAYS = 7, DONE_TIP = `done in the last ${DONE_DAYS} days`;
  const recent = t => t.status !== 'done' || !t.since || !READ || Date.parse(READ) - Date.parse(t.since) <= DONE_DAYS * 864e5;
  function renderBoard() {
    const v = visible().filter(recent);
    // build: backlog_items also returns statuses with no column (a message's ready_to_send); they get one lane, which
    // takes no drop and no key, so every row the page counts is on the board (review, PR #46).
    const other = v.filter(t => !SLABEL[t.status]), cols = STATUSES.filter(([s]) => SHOW.done || s !== 'done');
    put(document.getElementById('view-board'), html`<div class="board" id="board" role="group" aria-label="Tasks by status"${other.length ? html` data-other` : ''}${SHOW.done ? '' : html` data-nodone`}>${cols.map(([s, name]) => { const i = STATUSES.findIndex(x => x[0] === s);
      const rows = v.filter(t => t.status === s);
      return html`<div class="col" data-drop="status:${s}" role="group" aria-label="${name}">
        <header><span class="label"${s === 'done' ? html` title="${DONE_TIP}"` : ''}>${name}</span><kbd>${i + 1}</kbd><span class="n">${rows.length}</span></header>
        <div><p class="refusal" role="note"></p><div class="drop">${(rows.length ? rows.map(t => card(t)) : html`<p class="empty">${on() ? 'None match the filters.' : 'Nothing ' + name.toLowerCase() + '.'}</p>`)}</div></div>
      </div>`;
    })}${other.length ? html`<div class="col" role="group" aria-label="Other statuses">
        <header><span class="label">Other</span><span class="n">${other.length}</span></header>
        <div><div class="drop">${other.map(t => card(t, true))}</div></div>
      </div>` : ''}</div>${window.shell.list.help(HOW.board)}`);
  }
  function renderMatrix() {
    const v = visible().filter(t => t.status !== 'done');
    const q = QUADS.map(([k, name, , , sub]) => {
      const rows = v.filter(t => quadOf(t) === k);
      const worst = rows.some(t => state(t) === 'warning') ? 'warning' : rows.some(t => state(t) === 'caution') ? 'caution' : '';
      return html`<div class="quad" data-q="${k}" data-drop="quad:${k}">
        <header><h3>${name}</h3><span class="sub">${sub}</span><span class="n">${rows.length}</span><span class="lamp"${worst ? html` data-state="${worst}"` : ''}></span></header>
        <div><div class="drop" role="group" aria-label="${name}: ${sub}">${(rows.length ? rows.map(t => card(t, true)) : html`<p class="empty">Empty.</p>`)}</div><p class="refusal" role="note"></p></div>
        ${k === 'do' ? html`<span class="reticle" aria-hidden="true"></span>` : ''}
      </div>`;
    });
    put(document.getElementById('view-matrix'), html`<div class="matrix" id="matrix">
      <span></span>
      <div class="axis x">Urgent <span class="rule">due ≤ 7d or overdue</span></div>
      <div class="axis x">Not urgent</div>
      <div class="axis y">Important <span class="rule">P1–P2</span></div>${q[0]}${q[1]}
      <div class="axis y">Not important</div>${q[2]}${q[3]}
    </div>
    <p class="mnote">Done tasks stay off the matrix. A drop edits the task: into Important sets P2, out of it sets P3; crossing into or out of Urgent asks you for the due date, because urgency comes from it. Cancel changes nothing.</p>${window.shell.list.help(HOW.matrix)}`);
  }
  // The list's sort, page and size (shell.list, sd:2682): priority first, 50 a page. Every column with an order sorts.
  const LIST = { sort: 'p', dir: 1, size: 50 }, L = { ...LIST, page: 1 };
  const ORDER = Object.fromEntries(STATUSES.map(([st], i) => [st, i]));
  const SORTS = { id: t => t.id, title: t => t.title.toLowerCase(), repo: t => t.repo.toLowerCase(), status: t => ORDER[t.status] ?? 99, p: t => t.p ?? 9,
    due: t => t.overdue && !t.due ? -99 : t.due ? days(t) : 999, stage: t => t.stage || '~', lead: t => t.recurrence ? t.lead ?? 0 : Infinity,
    // A base row's score, highest first; a row with none sorts last (sd:3977).
    score: t => t.score === null || t.score === '' || isNaN(+t.score) ? Infinity : -t.score };
  // slim: a control or short cell (pick, glyph, P, actions) keeps the narrow gutter that lets the list fit its column (sd:4037).
  // id, rp and sc mark the columns that fold into a .fold line of their neighbour in a narrower column (tasks.css).
  const COLS = [[null, html`<span class="sr">Select</span>`, 'slim'], [null, html`<span class="sr">State</span>`, 'slim'], ['id', 'Id', 'label id'], ['title', 'Title', 'label'], ['repo', 'Repo', 'label rp'],
    ['status', 'Status', 'label'], ['stage', 'Stage', 'label'], ['score', 'Score', 'label sc'], ['p', 'P', 'label slim'], ['due', 'Due', 'label'], ['lead', 'Lead (days)', 'label'], [null, html`<span class="sr">Actions</span>`, 'slim']];
  // Due's head says the word, so a cell drops it (the design source's shortDue).
  const shortDue = s => s === 'no due' ? 'none' : s.replace(/^due /, '');
  let seek = false;
  function renderList() {
    const list = window.shell.list, k = SORTS[L.sort];
    const all = visible().slice().sort((a, b) => { const x = k(a), y = k(b); return (x < y ? -1 : x > y ? 1 : 0) * L.dir; });
    // build (sd:2589): the list pages as v1 /backlog paged, 50 a page; board and matrix show every filtered task. A ?row= with
    // no ?page= opens the page that holds it.
    if (seek) { seek = false; const i = all.findIndex(t => t.key === selected); if (i >= 0) L.page = Math.floor(i / L.size) + 1; }
    const v = list.pageOf(all, L);
    // Stage and Score show once a row has a base (sd:3977), as Base does: two empty columns would push Due and Actions off.
    // Lead (days) shows only when the Columns menu picks it and the store has a lead (sd:5649).
    const based = tasks.some(t => t.base), lead = LEAD && SHOW.cols.has('lead');
    const cols = COLS.filter(c => (based || (c[0] !== 'stage' && c[0] !== 'score')) && (lead || c[0] !== 'lead'));
    put(document.getElementById('view-list'), html`<div class="list-wrap"><table class="list" data-tfilter="page" data-toolbar="filterbar"><caption class="sr">Tasks</caption><thead>${list.sortHead(cols, L)}</thead><tbody>
      ${(v.length ? v.map(t => { const st = state(t), [dt, dc] = dueText(t); return html`<tr data-key="${t.key}"${t.key === selected ? html` aria-current="true"` : ''}${checked.has(t.key) ? html` data-picked` : ''}><td class="slim"><label class="pick"><input type="checkbox" data-check="${t.key}" aria-label="Select ${label(t)}"${checked.has(t.key) ? html` checked` : ''}><span></span></label></td><td class="g g-${st} slim">${st ? GLYPH[st] : ''}</td><td class="mono id">${label(t)}</td><td class="title"><button type="button" data-open="${t.key}">${t.title}</button>${issueLink(t)}<span class="fold mono">${label(t)} · ${t.repo}</span></td><td class="mono rp"><span title="${t.repo}">${t.repo}</span></td><td class="st">${slabel(t.status)}</td>${based ? html`<td>${t.stage || ''}${t.score != null && t.score !== '' ? html`<span class="fold mono">score ${t.score}</span>` : ''}</td><td class="mono sc">${t.score ?? ''}</td>` : ''}<td class="slim"><span class="pri" data-p="${String(t.p || '')}">${t.p ? 'P' + t.p : 'P–'}</span></td><td class="mono due ${dc}">${shortDue(dt)}${opens(t) ? html`<small class="opens">${opens(t)}</small>` : ''}</td>${lead ? html`<td class="mono lead">${t.recurrence ? String(t.lead ?? 0) : ''}</td>` : ''}<td class="slim">${C.rowActions(t.key)}</td></tr>`; }) : html`<tr><td colspan="${cols.length}" class="empty">None match the filters. <button class="linkbtn" type="button" id="clear-f2">Clear filters</button></td></tr>`)}
    </tbody></table></div>${list.pager(all.length, L, 'tasks')}`);
  }
  document.getElementById('view-list').addEventListener('click', e => {
    if (window.shell.list.sortBy(e, L)) { window.shell.list.keepFocus(e, renderList); writeURL(); return; }
    if (e.target.id === 'clear-f2') { clearFilters(); return; }
    if (window.shell.list.paging(e, L)) { window.shell.list.keepFocus(e, render); return; }
    const o = e.target.closest('[data-open]'); if (o) select(o.dataset.open, true);
  });

  function render() {
    putAll();
    renderFilters();
    writeURL();
    if (view === 'board') renderBoard(); else if (view === 'matrix') renderMatrix(); else renderList();
    // design.md § Selection: a filter or view change that hides the selected task moves the selection to the first visible
    // one, so x, . and the palette never act on a hidden card (review 2026-09-29, item 10).
    shell.reconcile({ rows: document.querySelectorAll('main [data-key]'), id: n => n.dataset.key, current: selected, keep: held,
      select: key => select(key, false), clear: () => { selected = null; writeURL(); } });
    renderDetails();
  }

  // ---------- Selection ----------
  function select(key, open) {
    if (key !== LINK) LINK = null;
    selected = key;
    // The selected card or row is aria-current: a card holds a checkbox and buttons, so it is an article, not a listbox
    // option, whose descendants assistive technology treats as presentational (review, PR #46).
    document.querySelectorAll('[data-key]').forEach(n => n.dataset.key === key ? n.setAttribute('aria-current', 'true') : n.removeAttribute('aria-current'));
    C.select(key);
    readDet(byKey(key)?.id); // build: the Details reading, once per task until a write changes it
    renderDetails();
    writeURL();
    if (open) openPane('tab-details');
  }
  document.getElementById('main').addEventListener('click', e => {
    if (e.target.closest('.rowact, .pick, .tile-f, .tile-done')) return;
    const c = e.target.closest('.card'); if (c && !suppressClick) select(c.dataset.key, true);
  });
  // Selection follows focus on a card, so Enter, which the shell sends as shell:open, opens the focused one.
  document.getElementById('main').addEventListener('focusin', e => { const c = e.target.closest('.card'); if (c && e.target === c && c.dataset.key !== selected) select(c.dataset.key, false); });
  document.getElementById('main').addEventListener('change', e => {
    const { edit: f, key } = e.target.dataset, t = f && byKey(key);
    if (t) { if (f === 'due' && e.target.value === 'pick') { render(); askEdit(t); return; }
      if (!fieldChange(t, `det-${f}`, e.target.value)) render(); return; }
    const k = e.target.dataset.check; if (!k) return;
    C.pick(k);
  });
  // The shell owns the picked set and the bulk bar; mirror it on rows and boxes.
  document.addEventListener('shell:picked', e => {
    checked = new Set(e.detail);
    document.querySelectorAll('main [data-key]').forEach(n => n.toggleAttribute('data-picked', checked.has(n.dataset.key)));
    document.querySelectorAll('main [data-check]').forEach(b => { b.checked = checked.has(b.dataset.check); });
  });
  // A note or an assignment opens its item: they are listed in the item's Details, not as rows of their own.
  document.addEventListener('shell:open', e => { const id = /^(note|asg):/.test(e.detail) ? String(C.get(e.detail)?.item ?? '') : e.detail; if (byKey(id)) select(id, true); });
  document.addEventListener('tasks:view', e => setView(e.detail));

  // ---------- Moves with Undo ----------
  function landed(key) { requestAnimationFrame(() => document.querySelector(`.card[data-key="${key}"]`)?.classList.add('landed')); }
  // Picked rows and 1–5: the status command runs as one bulk group on each picked row it is on for, as the shell's bulk bar
  // runs it; each row it is off for is skipped and named with its reason (a repeating task's Done is item.complete, not bulk).
  function movePicked(to) {
    const c = STATUS_CMD[to], objs = [...checked].map(k => C.get(k)).filter(Boolean);
    const on = objs.filter(o => c.when(o) === true), off = objs.filter(o => c.when(o) !== true);
    if (on.length) C.runBulk(c, on);
    if (off.length) toast(`Skipped ${off.map(o => `${label(byKey(o.id))}: ${c.when(o)}`).join('; ')}`);
  }
  // Start next (sd:5107): the server's plan first (start_next.plan), then Build posts the first N it listed, each with the
  // revision it read; the server plans again and queues nothing when the plan moved. A refusal leaves no Build button.
  // Since Build step 5 (pack sd:6096) the post queues a headless build of each, as Build does; herdr is Fix's alone.
  const startDlg = document.createElement('dialog');
  startDlg.className = 'palette date-dlg start-dlg'; startDlg.setAttribute('aria-labelledby', 'start-h');
  document.body.append(startDlg);
  const unread = v => v === null || v === undefined ? '▨' : v;
  const queuedText = out => window.commands.queuedText(out);
  async function askStart() {
    let P;
    try { P = await window.shell.getJSON('/api/tasks/start-next'); } catch (err) { toast(`Start next not read: ${err.message}`); return; }
    put(startDlg, html`<form method="dialog" class="date-form">
      <h2 id="start-h">Start next ready tasks</h2>${window.shell.closeX}
      <p class="why">${unread(P.running)} of ${P.cap} builders run · five-minute load ${unread(P.load)} of ${unread(P.load_max)}</p>
      ${P.refusal ? html`<p class="why">▲ Nothing can start: ${P.refusal}.</p>
        ${P.holders?.length ? html`<p><a class="btn quiet" href="/management?view=sessions">Open Management › Sessions</a></p>` : ''}` : html`
        <label class="label" for="start-n">Tasks to build (1–${P.room})</label>
        <input id="start-n" type="number" min="1" max="${P.room}" value="${P.room}" required>
        <ol class="start-list">${P.start.map((r, i) => html`<li data-i="${i}"><code>#${r.id}</code> ${r.title}</li>`)}</ol>
        <p class="why">Each queues an unattended build, grouped by repo. Cancel on Queue › Builds stops one.</p>`}
      ${P.skipped.length ? html`<details><summary>${plural(P.skipped.length, 'ready task')} left out</summary>
        <ul class="start-list">${P.skipped.map(r => html`<li><code>#${r.id}</code> ${r.title}: ${r.why}</li>`)}</ul></details>` : ''}
      <div class="actions">
        ${P.refusal ? '' : html`<button class="btn" value="start" type="submit">Build</button>`}
        <button class="btn quiet" value="cancel" type="submit" formnovalidate data-cancel>Cancel</button>
      </div></form>`);
    const n = () => Math.max(1, Math.min(P.room, parseInt(startDlg.querySelector('#start-n')?.value, 10) || 0));
    // The list marks what Build would queue now, so the count and the items never disagree.
    const mark = () => startDlg.querySelectorAll('.start-list li[data-i]').forEach(li => li.classList.toggle('off', +li.dataset.i >= n()));
    startDlg.querySelector('form')?.addEventListener('input', mark);
    startDlg.onclose = () => {
      if (startDlg.returnValue !== 'start' || P.refusal) return;
      const items = P.start.slice(0, n()).map(r => ({ id: r.id, revision: r.revision }));
      window.shell.post('/api/tasks/start-next/run', { items }).then(out => { toast(queuedText(out)); reread(); }, err => {
        // A 400 or 409 queued nothing. No status: no answer, so what queued is not known; the next plan says, so it asks for that.
        toast(err.status ? err.message : `Start next got no answer, so what queued is not known: open Start next again (${err.message})`);
        reread();
      });
    };
    startDlg.returnValue = '';
    startDlg.showModal(); (startDlg.querySelector('#start-n') || startDlg.querySelector('[value="cancel"]'))?.focus();
  }
  document.addEventListener('tasks:start-next', askStart);
  document.getElementById('start-next')?.addEventListener('click', askStart);
  // Build (pack sd:6095): commands.build, the one declaration Today's suggested Queue rows register too (pack sd:7271).
  C.register(window.commands.build({ row: o => byKey(o.id), reread }));
  // ---------- Pointer drag (mouse and pen; touch uses Move to) ----------
  let drag = null, suppressClick = false;
  const verdict = (t, target) => {
    const [kind, v] = target.split(':');
    if (kind === 'status') { const L = legal(t, v); return { ok: L.ok, text: L.ok ? `→ ${slabel(v)}` : L.reason, same: t.status === v }; }
    const Q = quadChange(t, v); return { ok: Q.ok, text: Q.ok ? Q.words.join(', ') : Q.reason, same: quadOf(t) === v };
  };
  document.addEventListener('pointerdown', e => {
    const c = e.target.closest('.card');
    if (!c || e.button !== 0 || e.pointerType === 'touch' || e.target.closest('button, input, select')) return;
    drag = { key: c.dataset.key, el: c, x: e.clientX, y: e.clientY, on: false, over: null };
  });
  document.addEventListener('pointermove', e => {
    if (!drag) return;
    if (!drag.on) {
      if (Math.hypot(e.clientX - drag.x, e.clientY - drag.y) < 5) return;
      drag.on = true;
      const t = byKey(drag.key), r = drag.el.getBoundingClientRect();
      drag.dx = e.clientX - r.left; drag.dy = e.clientY - r.top;
      drag.ghost = drag.el.cloneNode(true);
      drag.ghost.classList.add('ghost-card'); drag.ghost.removeAttribute('tabindex'); drag.ghost.setAttribute('aria-hidden', 'true');
      drag.ghost.style.width = r.width + 'px';
      put(drag.ghost, html`<div class="verdict"></div>`, 'append');
      document.body.append(drag.ghost);
      drag.el.classList.add('lifted');
      document.getElementById('main').setAttribute('data-dragging', '');
      document.querySelectorAll('[data-drop]').forEach(z => {
        const V = verdict(t, z.dataset.drop);
        z.dataset.legal = V.ok || V.same ? 'yes' : 'no';
        const r2 = z.querySelector('.refusal'); if (r2) put(r2, html`${V.ok || V.same ? html`` : html`<b>■ Refused.</b> ${V.text}`}`);
      });
    }
    drag.ghost.style.left = e.clientX - drag.dx + 'px';
    drag.ghost.style.top = e.clientY - drag.dy + 'px';
    const z = document.elementFromPoint(e.clientX, e.clientY)?.closest('[data-drop]');
    if (z !== drag.over) {
      drag.over?.removeAttribute('data-over'); drag.over = z;
      z?.setAttribute('data-over', 'yes');
      const V = z ? verdict(byKey(drag.key), z.dataset.drop) : null, vd = drag.ghost.querySelector('.verdict');
      vd.textContent = V ? (V.same ? 'no change' : V.ok ? V.text : '■ refused: ' + V.text) : '';
      vd.classList.toggle('no', !!V && !V.ok && !V.same);
    }
  });
  function endDrag(commit) {
    if (!drag) return;
    const d = drag; drag = null;
    if (!d.on) return;
    suppressClick = true; setTimeout(() => { suppressClick = false; }, 0);
    d.ghost.remove(); d.el.classList.remove('lifted');
    document.getElementById('main').removeAttribute('data-dragging');
    document.querySelectorAll('[data-drop]').forEach(z => { z.removeAttribute('data-over'); z.removeAttribute('data-legal'); });
    if (!commit || !d.over) return;
    const [kind, v] = d.over.dataset.drop.split(':');
    if (kind === 'status') { if (byKey(d.key).status !== v) move(d.key, v); } else toQuad(d.key, v);
  }
  document.addEventListener('pointerup', () => endDrag(true));
  document.addEventListener('pointercancel', () => endDrag(false));

  // ---------- Keys: 1–5 run the status commands on the selected card (board), 1–4 place it (matrix); v l/b/m switch view.
  // The shell owns j/k (PAGE_LIST below), Enter (shell:open), Esc, ., x, n, c, r, g and the action menu; a key that ends the
  // shell's g-chord is the shell's.
  let vChord = 0;
  document.addEventListener('keydown', e => {
    if (e.target.matches('input, textarea, select, [contenteditable]') || e.metaKey || e.ctrlKey || e.altKey || document.querySelector('dialog[open]') || e.target.closest?.('.actmenu') || shell.chording()) return;
    if (vChord && Date.now() - vChord < 1500) {
      const m = { l: 'list', b: 'board', m: 'matrix' }[e.key]; vChord = 0;
      if (m) { e.preventDefault(); setView(m); return; }
    }
    if (e.key === 'v') { vChord = Date.now(); return; }
    const n = parseInt(e.key, 10);
    if (checked.size && view !== 'matrix' && n >= 1 && n <= 5) { e.preventDefault(); movePicked(STATUSES[n - 1][0]); return; }
    if (!selected || !byKey(selected) || !(n >= 1)) return;
    if (view === 'board' && n <= 5) { e.preventDefault(); move(selected, STATUSES[n - 1][0]); document.querySelector(`.card[data-key="${selected}"]`)?.focus(); }
    if (view === 'matrix' && n <= 4) { e.preventDefault(); toQuad(selected, QUADS[n - 1][0]); document.querySelector(`.card[data-key="${selected}"]`)?.focus(); }
  });
  // The shell walks rows and cards on j / k (a card takes focus) and sends Esc here to cancel a drag.
  window.PAGE_KEYS = [['1 – 5', 'Board: move the selected task to that column; with rows picked, move the picked rows'], ['1 – 4', 'Matrix: place the selected task in that quadrant'],
    ['v l / v b / v m', 'List, board or matrix view']];
  window.PAGE_LIST = { rows: () => document.querySelectorAll('main [data-key]'), id: n => n.dataset.key, current: () => selected,
    select: (key, n) => { select(key, false); if (n.matches('.card')) n.focus(); }, clear: () => !!drag && (endDrag(false), true) };

  // ---------- Views ----------
  const SUB = {
    board: 'Board of the framework statuses.',
    matrix: 'Importance is priority 1–2; urgency is derived from the due date.',
    list: 'Every task, sortable by any column.',
  };
  // How to move a card, in the help line under the board or the matrix (sd:5733); the view text says what the view is.
  const HOW = { board: 'Drag a card, or select it and press 1–5 or use Move to.', matrix: 'Drag a card, or select it and press 1–4.' };
  function subhead() {
    const parked = tasks.filter(t => t.parked).length, scheduled = tasks.filter(t => !t.parked && t.scheduled).length;
    // Without a readable realm list (dashboard.realms) every repo row sits in Personal; a realm view says why (sd:5064).
    const why = REALMS && !['hoa', 'home', 'all'].includes(place()) && !['read', 'partial'].includes(REALMS.state)
      ? ` · repos in no realm: ${REALMS.state === 'missing' ? 'no realm list; set dashboard.realms on Settings' : `${REALMS.source} ${REALMS.problems[0] || 'was not read'}`}` : '';
    // Personal and Work name their sweep's last run and carry Run sweep (sd:5096, sd:5097).
    const w = SWEEP_NAME[place()] && SWEEPS?.[place()];
    // Each realm tab carries its open tasks (sd:5524, sd:8018): a done row, cancelled ones too, is listed but not counted.
    window.PAGE_VIEWS.forEach(v => window.shell.view.count(v.id, listed().filter(t => t.status !== 'done' && inPlace(t, v.id)).length));
    // Open counts as the realm tab does: the listed rows, not the scheduled ones.
    const held = tasks.filter(t => t.parked && inPlace(t)).length;
    put(document.getElementById('parked-seg'), window.shell.aside.buttons('parked', 'Parked', PARKED, { open: placed().length, aside: held }));
    put(document.getElementById('subhead'), html`${place() === 'all' ? 'All realms · ' : ''}${parked ? `${parked} parked · ` : ''}${scheduled ? `${scheduled} scheduled · ` : ''}read ${READ ? html`<time class="rel" datetime="${READ}"></time>` : 'not yet'} · ${SUB[view]}${why}${w ? html` · <span class="sweep" data-sweep="${place()}">${SWEEP_NAME[place()]} ${sweepText(w)} ${C.rowActions('sweep:' + place())}</span>` : ''}`);
  }
  // The view menu (sd:5064): one button shows the current view's icon; its menu lists the three, one checked. The shell
  // wires it (shell.viewPick, sd:5641), as on Writing.
  const pickView = window.shell.viewPick(document.getElementById('view-btn'), document.getElementById('view-menu'),
    { list: ['list-todo', 'List'], board: ['kanban', 'Board'], matrix: ['grid-2x2', 'Matrix'] }, setView);
  function setView(v) {
    view = v;
    pickView(v);
    ['board', 'matrix', 'list'].forEach(x => { const el = document.getElementById('view-' + x); el.hidden = x !== v; if (x !== v) el.replaceChildren(); });
    subhead();
    render();
  }
  document.getElementById('parked-seg').addEventListener('click', e => {
    const b = e.target.closest('button[data-aside="parked"]'); if (!b) return;
    PARKED = b.dataset.v; L.page = 1; subhead(); render();
  });
  function readFilterURL() {
    const q = new URLSearchParams(location.search);
    FKEYS.forEach(key => (q.get(key) || '').split(',').filter(Boolean).forEach(v => F[key].add(v)));
    // An older address's ?base= is Kind's plugin kinds now (sd:5644).
    (q.get('base') || '').split(',').filter(Boolean).forEach(v => F.kind.add(`base:${v}`));
    // An age v1 would not accept is dropped, as v1 dropped it; active takes 1 only, as v1's ?active=1 did.
    [...F.age].forEach(v => { if (!AGES.some(a => a.key === v)) F.age.delete(v); });
    // ?parked=1 or all picks Parked or All (sd:6701); sd:5643's ?active=parked is Parked too.
    PARKED = window.shell.aside.mode('parked', q);
    if (F.active.delete('parked')) PARKED = 'aside';
    [...F.active].forEach(v => { if (!['1', 'scheduled'].includes(v)) F.active.delete(v); });
    (q.get('cols') || '').split(',').filter(v => COL_OPTS.some(o => o[0] === v)).forEach(v => SHOW.cols.add(v));
    SHOW.done = q.get('done') === '1';
    find.value = (q.get('q') || '').trim(); Q = find.value.toLowerCase();
    // A page is decimal digits, as on Documents (sd:2427).
    window.shell.list.listParams(q, L, { sorts: Object.keys(SORTS), size: LIST.size });
    if (!/^[1-9]\d*$/.test(q.get('page') || '')) seek = !!selected;
  }
  function writeURL() {
    const q = new URLSearchParams(); q.set('view', view);
    if (place() === 'all') q.set('realm', 'all');
    FKEYS.forEach(key => { if (F[key].size) q.set(key, [...F[key]].join(',')); });
    if (Q) q.set('q', Q);
    const pp = window.shell.aside.param(PARKED); if (pp) q.set('parked', pp);
    if (SHOW.cols.size) q.set('cols', [...SHOW.cols].join(','));
    if (SHOW.done) q.set('done', '1');
    if (view === 'list') window.shell.list.listQuery(q, L, LIST);
    shell.url(q); // the shell keeps ?row=
    shell.filter(Q); // the shell's bar shows the same text (sd:5539)
  }

  // ---------- Shapeshift: add or find ----------
  const inp = document.getElementById('shift-in'), prev = document.getElementById('shift-preview'), ghost = document.getElementById('shift-ghost');
  function nextWeekday(w) { const d = new Date(TODAY); let n = (w - d.getDay() + 7) % 7 || 7; return plus(n); }
  function parse(text) {
    const out = { title: [], p: null, due: null, repo: null, find: null };
    const toks = text.trim().split(/\s+/).filter(Boolean);
    for (let i = 0; i < toks.length; i++) {
      const w = toks[i], lw = w.toLowerCase();
      let m;
      if ((m = lw.match(/^p([1-4])$/))) { out.p = +m[1]; continue; }
      if ((m = lw.match(/^#(\d{1,})$/))) { out.find = m[1]; continue; }
      if ((m = lw.match(/^#([a-z][\w./-]*)$/))) { out.repo = m[1]; continue; }
      if (lw === 'due' && toks[i + 1]) {
        const n = toks[i + 1].toLowerCase();
        const wd = WD.findIndex(d => n.startsWith(d));
        if (n === 'today') out.due = plus(0);
        else if (n === 'tomorrow' || n === 'tmrw') out.due = plus(1);
        else if (/^\d{4}-\d{2}-\d{2}$/.test(n)) out.due = n;
        else if (wd >= 0) out.due = nextWeekday(wd);
        if (out.due) { i++; continue; }
      }
      out.title.push(w);
    }
    out.title = out.title.join(' ');
    return out;
  }
  // build: #repo names a repository by the label this page shows; the write needs its path.
  const repoFor = name => tasks.find(t => t.repo.toLowerCase() === name)?.repo_path || null;
  // build (sd:3521): a capture with no #repo lands in the one repo the filter shows, else the selected task's; P3 unless typed.
  const filterRepo = () => F.repo.size === 1 ? repoFor([...F.repo][0].toLowerCase()) : null;
  window.PAGE_REPO = o => filterRepo() || (o && o.item ? byKey(String(o.item))?.repo_path : null) || null;
  function showPreview() {
    const txt = inp.value, P = parse(txt);
    ghost.hidden = !!txt;
    if (!txt.trim()) { prev.hidden = true; inp.setAttribute('aria-expanded', 'false'); return; }
    const found = P.find ? tasks.filter(t => String(t.id) === P.find) : P.title.length > 2 ? tasks.filter(t => t.title.toLowerCase().includes(P.title.toLowerCase())).slice(0, 3) : [];
    const fr = !P.repo && filterRepo() && [...F.repo][0];
    const chips = [P.p ? `P${P.p}` : 'P3 (default)', P.due && `due ${fmt(P.due)}`, P.repo && `#${P.repo}${repoFor(P.repo) ? '' : ' (not shown here)'}`, fr && `#${fr} (filter)`].filter(Boolean);
    put(prev, html`${(P.find ? '' : html`<div class="as"><span class="label">Enter adds</span>${ICON('plus')}<span>${P.title || '(title needed)'}</span></div>
      <div class="chips">${chips.map(c => html`<span class="chip" aria-pressed="true">${c}</span>`)}<span class="chip">planning</span></div>`) }${
      (found.length ? html`<p class="hint">${P.find ? 'Enter opens' : 'Matching'}</p><div class="suggest">${found.map(t => html`<button type="button" data-open-find="${t.key}">${ICON('search')}<span>${label(t)} · ${t.title}</span></button>`)}</div>` : P.find ? html`<p class="hint">No task #${P.find} here.</p>` : '') }${
      html`<p class="hint">Enter commits · Esc clears</p>`}`);
    prev.hidden = false; inp.setAttribute('aria-expanded', 'true');
  }
  inp.addEventListener('input', showPreview);
  inp.addEventListener('focus', showPreview);
  inp.addEventListener('keydown', e => {
    if (e.key === 'Escape') { if (inp.value) { e.stopPropagation(); inp.value = ''; showPreview(); } return; }
    if (e.key !== 'Enter') return;
    e.preventDefault();
    const P = parse(inp.value);
    if (P.find) { const t = tasks.find(x => String(x.id) === P.find); if (t) { inp.value = ''; showPreview(); select(t.key, true); } return; }
    if (!P.title) return;
    if (P.repo && !repoFor(P.repo)) { toast(`Not added: no repo #${P.repo} is shown on this page.`); return; }
    // build: files it with POST /api/items (workflow.capture_task, sd task add). No verb deletes a task, so no Undo.
    const where = P.repo ? repoFor(P.repo) : filterRepo();
    const body = { title: P.title, priority: P.p || 3, ...(P.due ? { due: P.due } : {}), ...(where ? { repo: where } : {}) };
    const typed = inp.value; inp.value = ''; showPreview();
    // The new task is selected only where it shows: a filter that hides it keeps the selection on a visible one (review, PR #46).
    window.shell.post('/api/items', body).then(out => reread().then(() => { const key = String(out.item.id), t = byKey(key);
      if (!t || !passes(t)) { toast(`Added #${out.item.id} to Planning · the filters hide it; Clear all shows it`); return; }
      select(key, false); landed(key); toast(`Added #${out.item.id} to Planning`); }),
      // A refusal gives the text back as typed, unless the operator has typed the next one meanwhile.
      err => { if (!inp.value) inp.value = typed; toast(`Not added: ${err.message}`); });
  });
  prev.addEventListener('click', e => { const b = e.target.closest('[data-open-find]'); if (b) { inp.value = ''; showPreview(); select(b.dataset.openFind, true); } });
  document.addEventListener('click', e => { if (!e.target.closest('#shift')) { prev.hidden = true; inp.setAttribute('aria-expanded', 'false'); } });

  // build: capture (n) files through the routes v1 uses: POST /api/items for a task or followup, POST /api/items/<id>/notes for
  // a note on the selected task, with that task's revision.
  window.SHELL_CAPTURE = async ({ kind, title, item, priority, repo }) => {
    if (kind === 'note') {
      const t = byKey(String(item)); if (!t) throw new Error('Not filed: pick a task for the note.');
      await write(t.key, x => `/api/items/${x.id}/notes`, { body: title, kind: 'comment' }); redrawDet(t.id);
      return `Note added to ${label(t)}`;
    }
    const said = await window.shell.fileCapture({ kind, title, item, priority, repo });
    await reread();
    return said;
  };

  // ---------- Start (build, sd:2484: shell.read reads /api/tasks, then draws; it rereads after each landed write) ----------
  // The reader (read.js, sd:2418) holds the guards: of overlapping reads only the newest draws; a task the read no longer
  // lists loses its pick and runs no command; a failed load leaves no row; and a failed reread after a write keeps the rows
  // and says the write landed. The Details keep their own per-item generation (DET.gen).
  let drawn = false;
  function draw() {
    if (!drawn) {
      // The address is read before the first draw: that draw's reconcile selects the first shown card and rewrites ?row=.
      drawn = true;
      const q = new URLSearchParams(location.search), row = shell.row();
      if (row && byKey(row)) { selected = row; LINK = row; }
      // sd:5462: a linked item the read does not list opens in Details (PIN above), not on its classic item page (sd:3526)
      // and not as the first card in its place. READ is set only by a read that answered.
      else if (row && READ) { PIN = { key: row }; selected = row; readDet(row); }
      readFilterURL();
      setView(['list', 'board', 'matrix'].includes(q.get('view')) ? q.get('view') : 'board');
    } else { subhead(); render(); }
    attention();
  }
  const reading = shell.read({
    source: '/api/tasks', what: 'the tasks',
    adopt: doc => { tasks = doc.rows.map(TD.shape); READ = doc.read; LEAD = !!doc.lead; IDEAS = new Set(doc.ideas || []); AGES = doc.ages || []; ATTENTION = doc.attention || null; REALMS = doc.realms || null; SWEEPS = doc.sweeps || null;
      // While a sweep runs, Tasks reads again every SWEEP_POLL ms, so its words follow the run to its end.
      if (!sweepTimer && Object.values(SWEEPS || {}).some(w => w.running)) sweepTimer = setTimeout(() => { sweepTimer = 0; reread(); }, SWEEP_POLL);
      // A row read at another revision than its cached Details changed elsewhere: they are read again (review 2026-10-12).
      TD.adopt(tasks);
      if (PIN && tasks.some(t => t.key === PIN.key)) PIN = null;
      const sweeps = Object.keys(SWEEP_NAME).filter(r => SWEEPS?.[r]).map(r => ({ id: `sweep:${r}`, type: 'sweep', label: `${SWEEP_NAME[r]} sweep` }));
      return { objects: [...tasks.map(objectOf), ...(PIN?.row ? [objectOf(PIN.row)] : []), ...sweeps], state: tasks.length ? null : { kind: 'empty', text: 'No open task, and none done this week.', source: '/api/tasks' } }; },
    clear: () => { tasks = []; READ = null; ATTENTION = null; SWEEPS = null; PIN = null; },
    draw,
    // draw() has already reconciled the selection against the cards the filters show, so the reader settles on that one.
    current: () => selected,
    first: () => selected,
    select: key => select(key, false),
    // The pinned item is not a row the read lists, so the reader finds no row for it: its selection stays.
    unselect: () => { if (!held(selected)) selected = null; renderDetails(); },
  });
  function load() { return reading.load(); }
  // Read again (sd:5638): the shell's head button reads the tasks again. The decision queues are Today's (sd:8013).
  window.PAGE_AGAIN = { title: 'Read the tasks again', run: load };
  // ---------- Run sweep (sd:5096, sd:5097) ----------
  // POST /api/tasks/sweep {realm} starts that realm's sweep in the background (sd_dashboard/sweep.py); a 409 names the run going. It asks
  // first: the run files a brief and followups and saves drafts, and takes up to an hour.
  C.register({ id: 'realm-sweep.run', on: 'sweep', label: 'Run sweep', does: 'Reads this realm\'s mail and changes; writes a brief, follow-ups and drafts. Sends nothing.',
    icon: 'play', risk: 'safe', askFirst: true, executes: true, primary: () => true,
    when: o => { const w = sweepOf(o); return !w ? 'the sweep was not read' : w.error || (w.running ? 'a sweep is running now' : true); },
    consequence: o => `Reads the ${o.id === 'sweep:work' ? 'work mail, Drive, Notion, Jira and GitHub' : 'personal mail and My Drive'} since the last run, files a dated brief and sd followups, and saves Gmail drafts. Nothing is sent or committed. Up to 60 min.`,
    cli: o => sweepOf(o)?.command || `python3 -m sd_dashboard.realm_sweep run ${String(o.id).slice(6)}`,
    sends: o => `POST /api/tasks/sweep {"realm": "${String(o.id).slice(6)}"}`,
    run: o => window.shell.post('/api/tasks/sweep', { realm: String(o.id).slice(6) }).then(
      out => reread().then(() => `${SWEEP_NAME[String(o.id).slice(6)]} sweep started ${String(out.started).slice(11, 16)} UTC · Tasks reads again until it ends`),
      err => { if ([400, 409].includes(err.status)) return err.message; throw err.status ? err : new Error(`no answer (${err.message}), so whether a sweep started is not known`); }) });
  function reread() { return reading.reread(); }
  load();
  suggest(['What is overdue across repos?', 'Which ready tasks are oldest?', 'Draft the reply that closes the selected task']);
});
