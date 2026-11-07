// Reports (sd:2121): the design source's products/system/designs/v2/reports.js at 7f50ad4, ported. Each change from the
// reference is marked "build:". The rows are /api/reports (reports_screen.document), never sample data: the newest 200
// database reports, the scheduled jobs, each job's run cadence from its log, and the job families the dashboard.report_families setting row names.
// A command that executes posts to a route server.action_route answers, and its toast comes after the write lands.
// The page reads /api/reports through the shell's reader (read.js, sd:2487), which holds the generation, retirement and
// failure rules; Acknowledge's Undo posts the reopen route with the revision the acknowledge answered (sd:2395).
// A report's Details and commands are report-details.js's (sd:5636), which the HOA and Home pages' Reports views draw too.
const { html, put, plural, cells } = window.markup;
const RD = window.reportDetails, { G, I, dayName, glyphClass } = RD;
const byId = id => document.getElementById(id);

// ---------- Data (build): /api/reports, read through shell.read on load and after each write ----------
let M = null, DOC = null, READ = '', DAYS = [], ROWS = [], JOBS = {}, CAD = {}, FAMILIES = [], FAM_OF = {}, byJob = {};
// build: the document's sources say which reads failed; a failed one renders as unknown with its reason, never as empty.
const why = s => DOC?.sources?.[s] || '';
// The reading's rows, jobs, cadence and families are report-details.js's model, which a report's Details read too.
function absorb(doc) {
  M = RD.model(doc);
  ({ doc: DOC, read: READ, days: DAYS, rows: ROWS, jobs: JOBS, cad: CAD, families: FAMILIES, famOf: FAM_OF, byJob } = M);
  document.body.dataset.observed = READ; // shell time cells count from the reading, not from now
}
// A job's cadence mark on a day (report-details.js): the run table and a report's Last 7 days draw the same marks.
const mark = (job, day) => RD.mark(M, job, day);
// build: the newest scheduled day with a mark, so a run still to come today does not hide yesterday's failure. A job
// scheduled less often than weekly has no mark in the seven days; its own last scheduled run, as the server read it, counts.
function lastState(job) {
  for (let i = DAYS.length - 1; i >= 0; i--) { const s = mark(job, DAYS[i])[0]; if (s !== 'none') return s; }
  const last = CAD[job]?.last;
  if (!last || !last.day || last.day >= DAYS[0]) return 'none';
  if (!last.read) return 'unknown';
  const [ok, bad] = last.runs;
  return bad && !ok ? 'warning' : bad || !ok ? 'caution' : 'ok';
}

// Family lamps.
function famState(jobs) {
  const rows = ROWS.filter(r => jobs.includes(r.job));
  const act = rows.filter(r => r.act);
  // Failing = the last logged run failed outright: the run cadence table's "■ failed last run", so the lamps and the table count the same jobs.
  // A partial last day (▲) is named apart; a fresh report without a log is a report that needs action, not a failing job.
  const bad = jobs.filter(j => CAD[j] && lastState(j) === 'warning');
  const partial = jobs.filter(j => CAD[j] && lastState(j) === 'caution');
  // build: a job whose own last scheduled run was not read (the cadence was not read, the job is not installed, it has no
  // log, the log begins later or its read was cut, or the calendar names no day) makes the lamp unknown, never all clear,
  // whatever the job's cadence: no failure seen is not health when nothing was seen. A manual job is never due (sd:4965).
  const unread = jobs.filter(j => !CAD[j]?.manual && !CAD[j]?.last?.read);
  const s = bad.length ? 'warning' : act.length || partial.length ? 'caution' : unread.length ? 'unknown' : 'ok';
  return { s, act: act.length, bad, partial, unread };
}
function lamps() {
  const out = FAMILIES.map(([k, label, icon, jobs]) => {
    const f = famState(jobs);
    const unread = f.unread.length ? html` · <span class="ph">${f.unread.length} not read</span>` : '';
    const val = f.s === 'ok' ? html`<span class="ph">all clear</span>` : f.s === 'unknown' ? html`<span class="ph"><b>${f.unread.length}</b> not read</span> · <span class="ph">none failing seen</span>`
      : f.bad.length ? html`<span class="ph"><b>${f.bad.length}</b> failing</span> · <span class="ph">${f.act} need action</span>${unread}` : html`<span class="ph"><b>${f.act}</b> need action</span> · <span class="ph">${f.partial.length ? `${f.partial.length} partial` : 'none failing now'}</span>${unread}`;
    return { button: true, state: f.s, pressed: F.fam === k, label, mark: I(icon), val,
      attrs: html` data-fam="${k}" title="${[...f.bad, ...f.partial.map(j => j + ' (partial)'), ...f.unread.map(j => j + ' (not read)')].join(', ')}"` };
  });
  // build: without a family list the page draws no family lamp; this one says which file it reads.
  const fam = DOC.families || {};
  if (fam.state !== 'read' && !FAMILIES.length) out.push({ state: 'unknown', attrs: html` title="${fam.problems?.join('; ') || ''}"`, label: 'Job families', mark: I('clipboard-list'),
    val: html`<span class="ph">no family list</span> · <span class="ph">${fam.source || 'dashboard.report_families'}</span>` });
  // A lamp with nothing to filter is a div that says why (design.md § Lamps); the partial read above the table says the rest.
  // build: the HTML count is the documents roots.
  out.push({ state: 'unknown', label: 'HTML reports', mark: I('file-text'), val: html`${DOC.html == null ? html`<span class="ph">not read</span>` : html`<span class="ph"><b>${DOC.html}</b> folders</span>`} · <span class="ph">not indexed</span>` });
  put(byId('annunciator'), cells(out));
}
// The shell's state slot (design.md § State slot). build: it names every source that was not read and a family list
// problem; with none, the slot stays empty.
function pageState() {
  const failed = Object.entries(DOC.sources || {}).filter(([, v]) => v).map(([k, v]) => `${k} (${v})`);
  const fam = DOC.families || {};
  const parts = [];
  if (failed.length) parts.push(`Not read: ${failed.join(', ')}.`);
  if (fam.state === 'missing') parts.push('No family list: set dashboard.report_families on Settings, under Dashboard.');
  else if (fam.problems?.length) parts.push(`${fam.source}: ${fam.problems.join('; ')}.`);
  return parts.length ? { kind: 'partial', text: parts.join(' '), source: `/api/reports · ${READ.slice(0, 16).replace('T', ' ')} UTC` } : null;
}

// Cadence table. build: a job no family names is listed under "Other jobs", so no installed job is hidden.
let CAD_JOBS = [];
function cadJobs() {
  const named = FAMILIES.flatMap(([k, , , jobs]) => jobs.filter(j => CAD[j]).map(j => [k, j]));
  const rest = Object.keys(CAD).filter(j => !FAM_OF[j]).sort().map(j => ['', j]);
  return [...named, ...rest];
}
const interesting = ([, j]) => DAYS.some(d => { const [s, , t] = mark(j, d); return s !== 'ok' && s !== 'none' && t !== 'no log yet'; }) || byJob[j]?.[0]?.act;
let cadAll = false, jobFilter = '';
function renderCad() {
  if (!DOC.cadence) {
    put(byId('cad-head'), html``);
    put(byId('cad-body'), html`<tr><td class="m unk">The run cadence was not read: ${why('cadence') || 'no reason given'}.</td></tr>`);
    put(byId('cad-tally'), html``); byId('cad-more').hidden = true;
    window.shell.view.count('cadence', null);
    return;
  }
  put(byId('cad-head'), html`<tr><th scope="col">Job</th>${DAYS.map(d => html`<th scope="col"><abbr title="${d}">${dayName(d)}</abbr></th>`)}</tr>`);
  put(byId('cad-caption'), html`${DAYS.length ? `${dayName(DAYS[0])} ${DAYS[0].slice(8)} – ${dayName(DAYS[DAYS.length - 1])} ${DAYS[DAYS.length - 1].slice(8)}` : ''}, local time.`);
  const list = cadAll ? CAD_JOBS : CAD_JOBS.filter(interesting);
  let lastFam = null;
  put(byId('cad-body'), html`${list.map(([f, j]) => {
    const fs = f !== lastFam; lastFam = f;
    return html`<tr data-job="${j}"${fs ? html` class="fam"` : ''}${j === jobFilter ? html` aria-current="true"` : ''}><th scope="row" class="job"><button type="button" aria-label="${j}: show its reports">${j}</button></th>${DAYS.map(d => { const [s, g, t] = mark(j, d); return html`<td class="m${s === 'none' ? ' none' : s === 'unknown' ? ' unk' : ''}" title="${d}: ${t}"><span class="${glyphClass(s)}" aria-hidden="true">${g}</span><span class="sr">${t}</span></td>`; })}</tr>`;
  })}`);
  const b = byId('cad-more');
  b.hidden = false; b.textContent = cadAll ? 'Show only jobs with gaps' : `Show all ${CAD_JOBS.length} jobs`; b.setAttribute('aria-expanded', String(cadAll));
  const counts = { warning: 0, caution: 0 };
  CAD_JOBS.forEach(([, j]) => { const s = lastState(j); if (s in counts) counts[s]++; });
  put(byId('cad-tally'), html`<span class="g-warning">■ ${counts.warning} failed last run</span><span class="g-caution">▲ ${counts.caution} partial</span>`);
  window.shell.view.count('cadence', list.length);
}
byId('cad-more').addEventListener('click', () => { cadAll = !cadAll; renderCad(); });
byId('cad-body').addEventListener('click', e => { const tr = e.target.closest('tr[data-job]'); if (!tr) return; setJob(jobFilter === tr.dataset.job ? '' : tr.dataset.job); window.shell.view.show('reports'); byId('led-h').scrollIntoView?.({ block: 'start' }); });

// Filters.
const F = { job: '', status: '', day: '', fam: '', q: '' };
let picks = new Set(), days = [], jobs = [];
function fillFilters() {
  // Every job the reading names, not only jobs with a report row: a cadence row sets this filter, and readURL takes ?job=
  // only when the list has it (sd:2430). A job with no report shows "Nothing matches".
  jobs = [...new Set([...ROWS.map(r => r.job), ...Object.keys(JOBS), ...Object.keys(CAD)])].sort();
  days = [...new Set(ROWS.map(r => r.at.slice(0, 10)))].sort().reverse();
}
function setJob(j) { jobFilter = j; F.job = j; L.page = 1; renderCad(); renderRows(); }
// The filter bar (sd:5463), Activity's layout from shell.list: Status as a segment with its counts, Job and Day as picks,
// the lamp's family and the query as removable tokens, then Clear all. The count sits on the title line.
function renderFilters() {
  const list = window.shell.list, n = f => ROWS.filter(f).length;
  const st = (v, word, count, g) => ({ v, pressed: F.status === v, title: `${word}: ${count}`,
    label: html`${g ? html`<span class="g-warning" aria-hidden="true">${g}</span>` : ''}${word} <span class="n">${String(count)}</span>` });
  put(byId('filters'), list.fbar(html`${list.fseg('status', 'Status', [st('act', 'Needs action', n(r => r.act), '■'), st('open', 'Open', n(r => r.open)), st('done', 'Acknowledged', n(r => !r.open))])}
    ${list.pick('job', 'Job', jobs.map(j => [j, j]), F.job, { all: 'All jobs' })}${list.pick('day', 'Day', days.map(d => [d, shortDay(d)]), F.day, { all: 'Any day' })}
    ${F.fam ? list.tok('fam', F.fam, `Family: ${FAMILIES.find(f => f[0] === F.fam)?.[1] || F.fam}`) : ''}${F.q ? list.tok('q', '', `“${F.q}”`) : ''}`, { clear: !!activeFilters().length }));
}
byId('filters').addEventListener('change', e => window.shell.list.pickChange(e, F, () => { jobFilter = F.job; L.page = 1; renderCad(); renderRows(); }));
byId('filters').addEventListener('click', e => {
  if (e.target.closest('[data-fclear]')) return clearAll();
  const b = e.target.closest('[data-f]'); if (!b) return;
  const { f, v } = b.dataset;
  if (f === 'status') { F.status = F.status === v ? '' : v; L.page = 1; return renderRows(); }
  unfilter([f]);
});
byId('annunciator').addEventListener('click', e => {
  const c = e.target.closest('button.cell'); if (!c) return;
  F.fam = F.fam === c.dataset.fam ? '' : c.dataset.fam;
  document.querySelectorAll('.cell[data-fam]').forEach(x => x.setAttribute('aria-pressed', String(x.dataset.fam === F.fam)));
  L.page = 1; renderRows(); window.shell.view.show('reports'); // a lamp filters the list, so it shows the list
});

// Active filters (sd:2529): Clear all removes every one; a token removes its own.
// Status folds the old Needs action view in (sd:5064): act is an open report that needs attention.
const STATUS = { act: 'Needs action', open: 'Open', done: 'Acknowledged' };
const shortDay = d => new Date(d + 'T12:00:00Z').toLocaleDateString('en-GB', { weekday: 'short', day: 'numeric', month: 'short', timeZone: 'UTC' });
function activeFilters() {
  return [F.job && { key: 'job', label: `Job: ${F.job}` },
    F.status && { key: 'status', label: `Status: ${STATUS[F.status]}` }, F.day && { key: 'day', label: `Day: ${shortDay(F.day)}` },
    F.fam && { key: 'fam', label: `Family: ${FAMILIES.find(f => f[0] === F.fam)?.[1] || F.fam}` },
    F.q && { key: 'q', label: `Text: ${F.q}` }].filter(Boolean);
}
// Each remover leaves the list to draw once, after it.
const UNFILTER = {
  job: () => { jobFilter = ''; F.job = ''; renderCad(); },
  status: () => { F.status = ''; },
  day: () => { F.day = ''; },
  fam: () => { F.fam = ''; document.querySelectorAll('.cell[data-fam]').forEach(x => x.setAttribute('aria-pressed', 'false')); },
  q: () => { F.q = ''; window.shell.filter(''); },
};
function unfilter(keys) { keys.forEach(k => UNFILTER[k]()); L.page = 1; renderRows(); }
const clearAll = () => unfilter(activeFilters().map(f => f.key));

// The list (sd:2527, sd:2528): newest first by default; every column with an order sorts; 50 a page, numbered pages.
const RANK = { warning: 0, caution: 1, queued: 2, ok: 3, unknown: 4 };
const SORTS = { s: r => RANK[r.s], what: r => (r.what || '').toLowerCase(), detail: r => r.detail.toLowerCase(), at: r => Date.parse(r.at), st: r => r.open ? 0 : 1 };
const LIST = { sort: 'at', dir: -1, size: 50 }, L = { ...LIST, page: 1 };
const COLS = [['s', html`<span class="sr">State</span>`, 'g'], ['what', 'Report'], ['detail', 'Detail'], ['at', 'Recorded'], ['st', 'Status'], [null, html`<span class="sr">Actions</span>`, 'act']];
// Ties keep the newest first, so a sort on status still reads down the days.
const order = (a, b) => { const x = SORTS[L.sort](a), y = SORTS[L.sort](b); return (x < y ? -1 : x > y ? 1 : 0) * L.dir || Date.parse(b.at) - Date.parse(a.at); };
let shown = [];
const tbody = byId('rows');
byId('rows-head').addEventListener('click', e => { if (window.shell.list.sortBy(e, L)) window.shell.list.keepFocus(e, renderRows); });
byId('pager').addEventListener('click', e => { if (window.shell.list.paging(e, L)) window.shell.list.keepFocus(e, renderRows); });
function matches(r) {
  if (F.job && r.job !== F.job) return false;
  if (F.status === 'act' && !r.act) return false;
  if (F.status === 'open' && !r.open) return false;
  if (F.status === 'done' && r.open) return false;
  if (F.day && r.at.slice(0, 10) !== F.day) return false;
  if (F.fam && FAM_OF[r.job] !== F.fam) return false;
  if (F.q && !(r.what + ' ' + r.detail + ' ' + r.job + ' ' + (r.source.body || '')).toLowerCase().includes(F.q)) return false;
  return true;
}
function stCell(r) {
  return r.open ? html`<td class="st open">${I('inbox')}open</td>` : html`<td class="st">${I('check')}acknowledged</td>`;
}
// read: the reader draws, and its settle picks the selection, so no reconcile runs here.
function renderRows(read) {
  // Rail badge (shell.js): the read's own, open reports that ask for attention (sd:3446, reports_screen._attention).
  window.PAGE_ATTENTION = DOC.reports && DOC.attention || { state: 'unknown', n: 0, what: 'reports not read' }; window.shell?.attention?.();
  const list = window.shell.list;
  shown = ROWS.filter(matches).sort(order);
  // The tab counts the open reports the filters show (sd:8018), not the acknowledged ones.
  window.shell.view.count('reports', DOC.reports ? shown.filter(r => r.open).length : null);
  const slice = list.pageOf(shown, L);
  if (started) writeURL();
  put(byId('rows-head'), list.sortHead(COLS, L));
  renderFilters();
  // Day bands read only in time order; another sort draws the rows without them.
  const byDay = L.sort === 'at';
  let lastDay = '';
  put(tbody, html`${slice.length ? slice.map(r => {
    const d = r.at.slice(0, 10), head = byDay && d !== lastDay; lastDay = d;
    return [head ? html`<tr class="day" aria-hidden="true"><td colspan="6">${new Date(d + 'T12:00:00Z').toLocaleDateString('en-GB', { weekday: 'long', day: 'numeric', month: 'long', timeZone: 'UTC' })} · UTC</td></tr>` : '',
    html`<tr data-id="${r.id}" data-changed="${r.at}" aria-selected="${String(r.id === sel)}"${picks.has(r.id) ? html` data-picked` : ''}><td class="g g-${r.s}">${G[r.s]}<span class="sr">${r.s}</span></td><td class="what"><button type="button">${r.what}</button></td><td class="detail">${r.detail}</td><td class="when"><time class="rel" datetime="${r.at}"></time></td>${stCell(r)}<td class="act">${window.shell.commands.rowActions(r.id)}</td></tr>`];
  }) : html`<tr class="day"><td colspan="6">${DOC.reports ? 'Nothing matches. Clear a filter, or press → in the bar to capture it as a task.' : `The reports were not read: ${why('reports')}.`}</td></tr>`}`);
  put(byId('pager'), list.pager(shown.length, L, 'reports'));
  // A filter or a page change that drops the selected report from the slice moves the selection to a row on screen (sd:2306).
  // With no row on screen, Details clears too, so its buttons never act on a report the list does not show.
  // A filter that clears it leaves cleared set, so the next render with rows selects the first one again.
  if (!read && started && (sel != null || cleared)) window.shell.reconcile({ rows: tbody.querySelectorAll('tr[data-id]'), current: sel, select: id => { cleared = false; select(id, false); },
    clear: clearDetails });
}

const details = byId('details');
let sel = null;
let cleared = false; // set while a filter shows no report
function clearDetails() {
  sel = null; cleared = true;
  put(details, html`<p class="why">${DOC?.reports ? 'No report selected · none is visible under this filter.' : 'The reports were not read, so nothing is selected.'}</p>`);
}
// A report's Details are report-details.js's (sd:5636); this page marks its row and keeps ?row= with it.
function show(id) {
  const r = ROWS.find(x => x.id === id); if (!r) return;
  sel = id;
  tbody.querySelectorAll('tr[data-id]').forEach(tr => tr.setAttribute('aria-selected', String(tr.dataset.id === id)));
  D.show(r);
}
// The URL carries the filters and the row, so a filtered list can be bookmarked. The filter bar is the one filter control;
// an old ?act=1 link (the Needs action view and checkbox) lands on Status: Needs action (sd:5064).
function readURL() {
  const p = new URLSearchParams(location.search);
  if (p.get('act') === '1' && !p.get('status')) p.set('status', 'act');
  const offered = { job: jobs, status: Object.keys(STATUS), day: days };
  ['job', 'status', 'day'].forEach(k => { const v = p.get(k) || ''; if (v && offered[k].includes(v)) F[k] = v; });
  if (F.job) jobFilter = F.job;
  if (FAMILIES.some(([k]) => k === p.get('fam'))) { F.fam = p.get('fam'); document.querySelectorAll('.cell[data-fam]').forEach(x => x.setAttribute('aria-pressed', String(x.dataset.fam === F.fam))); }
  if (p.get('q')) F.q = p.get('q').toLowerCase(); // the shell's bar shows it
  window.shell.list.listParams(p, L, { sorts: Object.keys(SORTS), size: LIST.size });
  if (!p.get('page') && /^[1-9]\d*$/.test(p.get('p') || '')) L.page = +p.get('p'); // ?page= is the shared pager key that views reset; ?p= is an old link
}
function writeURL() {
  const p = new URLSearchParams();
  ['job', 'status', 'day'].forEach(k => { if (F[k]) p.set(k, F[k]); });
  if (F.fam) p.set('fam', F.fam);
  if (F.q) p.set('q', F.q);
  window.shell.list.listQuery(p, L, LIST);
  window.shell.url(p); // the shell keeps ?row=
}
function select(id, open) { show(id); writeURL(); if (open) window.shell.openPane('tab-details'); }
tbody.addEventListener('click', e => { const tr = e.target.closest('tr[data-id]'); if (tr) select(tr.dataset.id, true); });

// The shell's Shapeshift bar (sd:5539) gives the list its text, bodies included; the cadence table takes it from the shell.
document.addEventListener('shell:filter', e => { const q = e.detail.q.toLowerCase(); if (q === F.q) return; F.q = q; L.page = 1; if (DOC) renderRows(); });
window.PAGE_FILTER = { reports: 'reports, bodies included' };
// The shell's view switch (shell.js, view block): the list first, the run table second. Both read /api/reports, so neither
// names an api of its own.
// Status's Needs action replaces the old Needs attention view (sd:5724).
window.PAGE_VIEWS = [{ id: 'reports', label: 'Reports' }, { id: 'cadence', label: 'Cadence' }];
// The shell walks these rows on j / k.
window.PAGE_LIST = { rows: () => tbody.querySelectorAll('tr[data-id]'), select: id => select(id, false), clear: () => !!activeFilters().length && (clearAll(), true) };

window.PAGE_COMMANDS = [
  { label: 'Show reports that need action', icon: 'filter', run: () => { F.status = 'act'; L.page = 1; renderRows(); } },
];

// ---------- Commands (products/system/commands.md): report-details.js declares them (sd:5636) ----------
// A write that landed rereads (the reader shares one read among a group of writes); a refused one loads, so the page never
// says a change landed that did not.
let reader = null, D = null;
const load = () => reader.load(), reload = () => reader.reread();
// Read again (sd:5638): the shell's head button reads the reports again.
window.PAGE_AGAIN = { title: 'Read the reports again', run: load };
async function write(path, body) {
  let out;
  try { out = await window.shell.post(path, body); } catch (err) { if (err.stale) load(); throw err; }
  await reload();
  return out;
}
document.addEventListener('shell:open', e => { if (ROWS.some(r => r.id === e.detail)) select(e.detail, true); });
document.addEventListener('shell:picked', e => { picks = new Set(e.detail); tbody.querySelectorAll('tr[data-id]').forEach(tr => tr.toggleAttribute('data-picked', picks.has(tr.dataset.id))); });

// ---------- Start (build: read /api/reports through shell.read, then draw; again after each write) ----------
let started = false;
// The reports this page drew: the reader's settle selects among them, never a report a filter hides.
const onPage = () => shown.slice((L.page - 1) * L.size, L.page * L.size).map(r => r.id);
const firstOnPage = () => { const ids = onPage(); return ids.find(id => ROWS.find(r => r.id === id)?.act) || ids[0]; };
function draw() {
  CAD_JOBS = cadJobs();
  lamps();
  fillFilters();
  const needs = ROWS.filter(r => r.act).length;
  byId('sub').textContent = DOC.reports ? `${needs} want action · ${ROWS.filter(r => r.open).length} open in the store · newest first` : 'Reports not read';
  if (!started) readURL();
  renderCad();
  started = true;
  renderRows(true);
}
document.addEventListener('DOMContentLoaded', () => {
  D = RD.attach({ details, model: () => M, write, load });
  reader = window.shell.read({
    source: '/api/reports', what: 'the reports, jobs and job logs',
    adopt: doc => { absorb(doc); return { objects: D.objects(), state: pageState() }; },
    // Nothing from the last reading stays on screen: rows, jobs, cadence, lamps and the observed time.
    clear: err => {
      absorb({ reports: null, cadence: null, jobs: [], families: { state: 'read', list: [] },
        sources: { reports: err.message, cadence: err.message } });
      READ = ''; delete document.body.dataset.observed;
    },
    draw,
    current: () => sel,
    first: firstOnPage,
    // A requested row a filter hides selects the first report on the page instead (PR #31 review 2).
    select: id => { const at = onPage().includes(id) ? id : firstOnPage(); if (at) { cleared = false; select(at, false); } else clearDetails(); },
    unselect: clearDetails,
  });
  load();
});
