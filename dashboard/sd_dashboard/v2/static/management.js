// Management (sd:2118): the design source's products/system/designs/v2/management.js at d82daa1, ported. Each change from the
// reference is marked "build:". The rows are /api/management (management_screen.py), never sample data. A command that
// executes posts to a route server.action_route answers, and its toast comes after the write lands, not before.
const { html, put, plural, cells, tile } = window.markup;
// sd:5080: the four areas are the shell's views (shell.js, view:start): the shell draws the tabs, keeps #view=<id> and the
// last choice, and fires 'shell:view'. Each lamp shows its view. Set before shell.js reads it.
window.PAGE_VIEWS = [{ id: 'repos', label: 'Repos' }, { id: 'sessions', label: 'Sessions' },
  { id: 'deploys', label: 'Deploys' }, { id: 'schedules', label: 'Schedules', key: 'c' }, { id: 'machine', label: 'Machine' }];
// Chat scope = the selected repo. Set before shell.js reads it; a repo page is its own URL, so the scope is right on load.
(() => { const r = new URLSearchParams(location.search).get('repo'); if (r) document.body.dataset.scope = r.replace(/^~\/(repos\/)?/, ''); })();

const I = n => html`<svg class="i" aria-hidden="true"><use href="#i-${n}"/></svg>`;
const shq = s => `'${String(s ?? '').replace(/'/g, "'\\''")}'`; // shell-safe: single quotes, so $(), backticks and \ stay literal
const GLYPH = { ok: '●', caution: '▲', warning: '■', queued: '◌', unknown: '▨' };
// The PATH name, as sessions call pack commands (pack sd:1609, sd:5987).
const SDDB = 'sd-db';

// ---------- Data (build): /api/management, read through shell.read on load and after each write ----------
let DOC = null, READ = '';
let REPOS = [], ALL = [], GIT = null, SESS = null, CLAUDE = null, SERVICES = [], CRON = [], SATS = null, UPLOAD = null;
const tilde = p => { const h = GIT?.root ? GIT.root.replace(/\/repos\/?$/, '') : ''; return h && p.startsWith(h + '/') ? '~' + p.slice(h.length) : p; };
const nameOf = p => p.replace(/^~\/(repos\/)?/, '');
const hhmm = iso => iso ? iso.slice(11, 16) : '';
// a fleet section's time: when the snapshot read it, and ▨ with the reason when its newest read failed.
const seen = d => html`${hhmm(d?.observed || READ)} UTC${d?.stale ? html` · <span class="g-unknown">▨</span> stale: ${d.stale}` : ''}`;
// build: the document's sources say which reads failed; a failed one renders as unknown with its reason, never as empty.
const why = s => DOC?.sources?.[s] || '';
function absorb(doc) {
  DOC = doc; READ = doc.read || new Date().toISOString();
  document.body.dataset.observed = READ;
  GIT = doc.git;
  const gitBy = Object.fromEntries((GIT?.repos || []).map(g => [tilde(g.path), g]));
  REPOS = (doc.repos || []).map(r => {
    const m = (r.remote || '').match(/github\.com[:/]([^/]+)\/(.+?)(\.git)?$/);
    return { path: r.path, remote: r.remote, managed: r.managed, merge: r.runner_merge, lane: r.lane_host || 'hub', mode: r.mode, ci: r.ci,
      channels: r.channels || 'none', chProblem: r.channels_problem || null,
      dependabot: r.dependabot ?? null, depGate: r.dependabot_gate || null, github_issues: r.github_issues ?? null, issues_scope: r.issues_scope ?? null, jira_project: r.jira_project ?? null,
      pieces: r.pieces_source, created: r.created_at, updated: r.updated_at, review: r.review, protection: r.protection, runtimes: r.runtimes,
      owner: m ? m[1] : '?', slug: m ? `${m[1]}/${m[2]}` : '', name: nameOf(r.path), registered: true, git: gitBy[r.path] || null };
  });
  const unreg = (GIT?.repos || []).filter(g => !REPOS.some(r => r.path === tilde(g.path)))
    .map(g => ({ path: tilde(g.path), remote: '', managed: '—', merge: '—', lane: '—', mode: '—', owner: '—', slug: '', name: nameOf(tilde(g.path)), registered: false, git: g }));
  ALL = [...REPOS, ...unreg];
  // A new reading of a declaration redrafts each pending line on it (restateDeclaration), so no line writes back an old one.
  [...new Set(pending.filter(x => x.edit).map(x => x.repo?.path))].forEach(restateDeclaration);
  ALL.forEach(r => { const g = r.git; r.branch = g ? g.branch : '—'; r.lag = g && g.behind_default !== null ? g.behind_default : -1; r.fetched = g ? g.fetched_iso || '' : ''; });
  // build (sd:1629): the settings columns sort on their shown words.
  ALL.forEach(r => { const rv = r.review || {}, rt = r.runtimes || {}; r.floor = rv.severity_floor || ''; r.copilot = rv.automatic_deep == null ? '' : String(rv.automatic_deep);
    r.python = rt.python?.value || ''; r.node = rt.node?.value || ''; });
  SESS = doc.sessions; CLAUDE = doc.claude; SERVICES = doc.services || []; SATS = doc.satellites || null; UPLOAD = doc.upload || null;
  CRON = (doc.jobs || []).map(j => ({ ...j, next: nextRun(j.schedule), rank: stateOf(j) }));
}
// Age of the last fetch at the reading, as v1 words it (repos_screen._age).
const age = iso => { const m = Math.max(0, Math.floor((Date.parse(READ) - Date.parse(iso)) / 60000)); return isNaN(m) ? 'no fetch' : m < 60 ? `${m} min` : m < 1440 ? `${Math.floor(m / 60)} h` : `${Math.floor(m / 1440)} d`; };
// Why no pull is offered, or true. build: the refusal is repos_screen.primary()'s remedy, read by the server, not rebuilt here.
const pullWhy = o => { const g = o.row.git;
  if (!g) return why('git') ? `git state was not read: ${why('git')}` : 'this checkout is not under the fleet root, and the fleet reads git state there only';
  if (g.error) return "the tree's state was not fully read";
  if (g.state === 'behind') return g.remedy.startsWith('git ') ? true : g.remedy.replace(/^No pull offered: /, '').replace(/\.$/, '');
  return g.state === 'current' ? `nothing to pull: ${g.detail.replace(/\.$/, '')}` : (g.detail || g.headline).replace(/\.$/, ''); };

// ---------- Time ----------
// build: launchd calendars (StartCalendarInterval dicts) in the viewer's clock, which is the Mac's when the dashboard runs there.
// A calendar entry names any of Month, Day, Weekday, Hour and Minute; the next run is the earliest entry's next match.
function nextRun(schedule, now = Date.now()) {
  if (!Array.isArray(schedule) || !schedule.length) return '';
  const from = new Date(now); from.setSeconds(0, 0); from.setMinutes(from.getMinutes() + 1);
  const next = schedule.map(e => nextOf(e, from)).filter(Boolean).sort((a, b) => a - b)[0];
  return next ? next.toISOString().replace('.000', '') : '';
}
// build: day by day with no one-week horizon, so a monthly or yearly entry has a next run; eight years reach a 29 February.
function nextOf(e, from) {
  const hours = e.Hour != null ? [e.Hour] : [...Array(24).keys()], minutes = e.Minute != null ? [e.Minute] : [...Array(60).keys()];
  for (let d = 0; d <= 8 * 366; d++) {
    const day = new Date(from.getFullYear(), from.getMonth(), from.getDate() + d);
    if ((e.Month != null && e.Month !== day.getMonth() + 1) || (e.Day != null && e.Day !== day.getDate())
      || (e.Weekday != null && e.Weekday % 7 !== day.getDay())) continue;
    for (const h of hours) for (const m of minutes) {
      const t = new Date(day.getFullYear(), day.getMonth(), day.getDate(), h, m);
      if (t >= from) return t;
    }
  }
  return null;
}
function human(schedule) {
  // build (sd:4965): no calendar is a manual job, JOB_SCHEDULE="manual"; launchd starts it only on Run now.
  if (!Array.isArray(schedule) || !schedule.length) return 'manual';
  const W = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
  const two = n => String(n).padStart(2, '0');
  const M = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  // build (review, PR #50): an entry pinned to a Month, Day or Weekday names it; daily and hourly mean no such field is set.
  const pinned = e => e.Month != null || e.Day != null || e.Weekday != null;
  // build (sd:2385): 'every N min' only when the marks are evenly spaced around the hour; '5,30' names its marks.
  if (schedule.every(e => e.Hour == null && e.Minute != null && !pinned(e))) {
    const mins = [...new Set(schedule.map(e => e.Minute))].sort((a, b) => a - b);
    const gaps = mins.map((m, i) => (i + 1 < mins.length ? mins[i + 1] : mins[0] + 60) - m);
    if (mins.length > 1 && gaps.every(g => g === gaps[0])) return `every ${gaps[0]} min`;
    return `hourly at ${mins.map(m => `:${two(m)}`).join(', ')}`;
  }
  const date = e => e.Month != null ? `${M[e.Month - 1]}${e.Day != null ? ` ${e.Day}` : ''}` : e.Day != null ? `day ${e.Day}` : '';
  const one = e => [date(e), e.Weekday != null ? W[e.Weekday] : '', `${e.Hour != null ? two(e.Hour) : '*'}:${e.Minute != null ? two(e.Minute) : '*'}`].filter(Boolean).join(' ');
  const daily = schedule.some(pinned) ? '' : 'daily ';
  return schedule.length > 2 ? `${daily}${schedule.length}× (${one(schedule[0])}…${one(schedule.at(-1))})` : `${daily}${schedule.map(one).join(', ')}`;
}
// build (sd:2210): a job's last run is the cron-jobs wrapper's stamp, logs/.<job>.stamp; launchd keeps no run time.
// A stamp with no end is a run in progress when launchd says running, and a run no trap saw end when it does not.
const runOf = c => c.last_run || { state: 'unread', reason: why('jobs') || 'not read' };
function lastCell(c) {
  const r = runOf(c);
  if (r.state === 'finished') return html`<time class="rel" datetime="${r.ended}"></time>${r.exit ? ` · exit ${r.exit}` : ''}`;
  if (r.state === 'open') return c.state === 'running' ? html`<span class="g-queued" aria-hidden="true">◌</span> since <time class="rel" datetime="${r.started}"></time>`
    : html`<span class="g-caution" aria-hidden="true">▲</span> no end · <time class="rel" datetime="${r.started}"></time>`;
  return html`<span class="g-unknown" aria-hidden="true">▨</span> ${r.state === 'none' ? 'not stamped' : 'unread'}`;
}
function lastDetail(c) {
  const r = runOf(c);
  if (r.state === 'finished') return html`started <time class="rel" datetime="${r.started}" data-long></time> · ended <time class="rel" datetime="${r.ended}" data-long></time> · exit ${r.exit}`;
  if (r.state === 'open') return html`${c.state === 'running' ? 'running since' : 'no end recorded; started'} <time class="rel" datetime="${r.started}" data-long></time>`;
  return html`<span class="g-unknown" aria-hidden="true">▨</span> ${r.reason}`;
}
// build: a job's state is operations.inventory's; failed is warning, unknown and unloaded are unknown, the rest ok.
const stateOf = j => j.state === 'failed' ? 'warning' : ['unknown', 'unloaded'].includes(j.state) ? 'unknown' : j.state === 'interrupted' ? 'caution' : 'ok';

// ---------- Selection + URL ----------
const params = new URLSearchParams(location.search);
const details = document.getElementById('details');
const repoParam = params.get('repo');
// The shell owns the view (#view=<id>); an older ?view= link still opens its view (DOMContentLoaded below). Either is
// checked against the four names before anything indexes by it.
const VIEWS = window.PAGE_VIEWS.map(v => v.id);
let view = 'repos';
const LIST_STATE = () => ({ repos: [RS, 'name', 1, repoCols()], deploys: [DS, 'name', 1, SVC_COLS], schedules: [SS, 'rank', 1, SCHED_COLS] })[view];
function setURL() {
  const p = new URLSearchParams();
  if (repoParam) p.set('repo', repoParam);
  if (text.q) p.set('q', text.q);
  const L = LIST_STATE();
  if (L) { const [st, sort, dir] = L; shell.list.listQuery(p, st, { sort, dir, size: 25 }); }
  if (view === 'repos') ['managed', 'merge', 'research'].forEach(k => RS[k] && p.set(k, RS[k]));
  if (view === 'repos' && RS.cols === 'settings') p.set('cols', 'settings');
  shell.url(p);
}
function readURL() {
  if (params.get('q')) text.q = params.get('q'); // the shell's bar reads ?q= too
  if (params.get('cols') === 'settings') RS.cols = 'settings';
  const L = LIST_STATE(); if (!L) return;
  const [st, , , cols] = L;
  shell.list.listParams(params, st, { sorts: cols.map(c => c[0]).filter(Boolean), size: 25 });
  // An older link's ?p= and ?n= still open their page and size.
  if (!params.get('page') && /^[1-9]\d*$/.test(params.get('p') || '')) st.page = +params.get('p');
  if (!params.get('size') && [25, 50, 100].includes(+params.get('n'))) st.size = +params.get('n');
  if (view === 'repos') ['managed', 'merge', 'research'].forEach(k => { if (params.get(k)) RS[k] = params.get(k); });
}
function swap() { details.setAttribute('data-swap', ''); requestAnimationFrame(() => details.removeAttribute('data-swap')); }
const ACT = id => html`<td class="act end">${shell.commands.rowActions(id)}</td>`;
const ACTH = html`<th scope="col" class="act"><span class="sr">Actions</span></th>`;
function markRow(id) { document.querySelectorAll('.ledger tbody tr[data-id]').forEach(tr => tr.setAttribute('aria-selected', tr.dataset.id === id)); }
const unknown = (what, reason, read) => html`<div class="unknown"><span><span class="g-unknown" aria-hidden="true">▨</span> <b>${what}</b></span><span>${reason}</span>${read ? html`<span class="mono">${read}</span>` : ''}</div>`;

// ---------- Views (sd:5080: the shell's tabs) ----------
function showView(v) {
  view = v;
  if (v === 'machine' && !MACHINE) readMachine();
  const row = shell.row(); if (row && !document.querySelector(`#view-${v} tr[data-id="${CSS.escape(row)}"]`)) shell.row(null);
  applyText();
  defaultDetails();
}
// build: the four lamps count from the reading; a source that failed is unknown and says so. A lamp shows its view.
const LOOK = { repos: 'folder-code', sessions: 'bot', deploys: 'rocket', schedules: 'calendar-clock', machine: 'monitor' };
function lamps() {
  const L = {};
  const set = (v, state, val) => { L[v] = { state, val }; };
  if (DOC?.repos) set('repos', 'ok', html`<b>${REPOS.length}</b> rows · ${REPOS.filter(r => r.merge === 'auto').length} auto`);
  else set('repos', 'unknown', html`▨ not read`);
  // build (sd:5106): a Claude session that waits on the operator lights the lamp first. Sessions not read, or one whose
  // status was not read, are not "nobody waits": the lamp is unknown unless an abandoned worktree already lights it.
  const waiting = CLAUDE ? CLAUDE.sessions.filter(r => r.waiting).length : 0;
  const unheard = CLAUDE ? CLAUDE.sessions.filter(r => r.waiting === null).length : null;
  if (waiting) set('sessions', 'caution', html`<b>${waiting}</b> ▲ ${waiting === 1 ? 'waits' : 'wait'} on you`);
  else if (unheard !== 0 && !SESS?.abandoned) set('sessions', 'unknown', unheard === null ? html`▨ Claude sessions not read` : html`<b>${unheard}</b> ▨ not known to wait`);
  else if (SESS?.stale) set('sessions', 'unknown', html`▨ stale since ${hhmm(SESS.observed)} UTC`);
  else if (SESS) set('sessions', SESS.abandoned ? 'caution' : 'ok', html`<b>${SESS.processes.length}</b> sd-* · ${SESS.abandoned ? `▲ ${SESS.abandoned} abandoned` : 'no abandoned'}`);
  else set('sessions', 'unknown', html`▨ not read`);
  // sd:5108: Deploys counts the satellites' refresh results; with none listed it stays unknown, as no deploy source.
  if (SATS?.satellites.length) { const n = k => SATS.satellites.filter(x => SATSTATE(x) === k).length;
    set('deploys', n('warning') ? 'warning' : n('queued') ? 'queued' : n('unknown') ? 'unknown' : 'ok', html`<b>${SATS.satellites.length}</b> satellites · ${n('warning') ? `■ ${n('warning')} failed` : n('queued') ? `${n('queued')} waiting` : 'refreshed'}`); }
  else set('deploys', 'unknown', html`▨ no deploy source`);
  if (DOC?.jobs) { const failed = CRON.filter(c => c.rank === 'warning').length; set('schedules', failed ? 'warning' : 'ok', html`<b>${failed}</b> failed ${failed ? '■ ' : ''}of ${CRON.length}`); }
  else set('schedules', 'unknown', html`▨ not read`);
  if (MACHINE?.rows) set('machine', MACHINE.rows.length ? 'caution' : 'ok', MACHINE.rows.length ? html`<b>${MACHINE.rows.length}</b> ▲ ${MACHINE.rows.length === 1 ? 'wants' : 'want'} you` : html`nothing wants you`);
  else set('machine', 'unknown', MACHINE ? html`▨ not read` : html`▨ read when shown`);
  put(document.getElementById('annunciator'), cells([...window.PAGE_VIEWS.map(v => ({ button: true, state: L[v.id].state, pressed: v.id === view,
    attrs: html` data-view="${v.id}"`, label: v.label, mark: I(LOOK[v.id]), val: L[v.id].val, sr: `state ${L[v.id].state} · shows ${v.label}` }))]));
}

// ---------- Machine (pack sd:7527): Operations > Resources > Toolbox, sd_tile.py toolbox, read when the view opens ----------
// The launchd jobs, other agents, containers and machine-setup drift, as markup.tile draws them; a failed read says why.
let MACHINE = null;
function renderMachine() {
  const el = document.getElementById('view-machine');
  put(el, html`<div class="sec-head"><h2 id="machine-h">Machine</h2><p class="tally">This Mac's local jobs, other agents, containers and setup drift</p></div>
    ${!MACHINE ? html`<p class="why">Reading the machine…</p>` : MACHINE.rows ? tile(MACHINE)
      : unknown('The machine was not read', MACHINE.error, '/api/management/machine')}`);
}
const readMachine = () => { MACHINE = null; renderMachine();
  return shell.getJSON('/api/management/machine').then(d => { MACHINE = d; }, e => { MACHINE = { error: e.message }; }).then(() => { renderMachine(); lamps(); }); };

// ---------- Lists: sort, filter, page (shell.list, sd:2682) ----------
// build: a page from the URL or an older reading can pass the last page; shell.list clamps it before the slice, and the URL follows.
function pageOf(rows, st) { const was = st.page, slice = shell.list.pageOf(rows, st); if (st.page !== was) setURL(); return slice; }
const REPO_COLS = [[null, html`<span class="sr">Settings</span>`, 'g'], ['name', 'Repo'], ['managed', 'Managed'], ['merge', 'Lane merge'], ['lane', html`Lane<span class="sr"> host</span>`], ['branch', html`Branch<span class="sr"> and uncommitted files</span>`], ['lag', html`<span aria-hidden="true">+/−</span><span class="sr">Ahead and behind default</span>`], ['fetched', 'Fetched'], [null, html`<span class="sr">Actions</span>`, 'act']];
// build (sd:1629): the condensed overview, one row per repo with its settings, swaps the git columns for these.
const SETTING_COLS = [[null, html`<span class="sr">Settings</span>`, 'g'], ['name', 'Repo'], ['managed', 'Managed'], ['merge', 'Lane merge'], ['lane', html`Lane<span class="sr"> host</span>`], ['floor', html`Floor<span class="sr"> (review severity floor)</span>`], ['copilot', html`Copilot<span class="sr"> automatic deep review</span>`], [null, 'Required checks'], ['python', 'Python'], ['node', 'Node'], [null, html`<span class="sr">Actions</span>`, 'act']];
const repoCols = () => RS.cols === 'settings' ? SETTING_COLS : REPO_COLS;
// build (sd:3388): the services list sorts, filters and pages as v1 Operations > Services did.
const SVC_COLS = [[null, html`<span class="sr">State</span>`, 'g'], ['name', 'Service'], ['what', 'What'], ['state', 'State'], [null, 'PID', 'num'], [null, 'Previous exit', 'num'], [null, html`<span class="sr">Actions</span>`, 'act']];
const SCHED_COLS = [[null, html`<span class="sr">State</span>`, 'g'], ['name', 'Job'], [null, 'Schedule'], [null, 'State'], [null, 'Last run'], ['next', 'Next run'], [null, 'Exit', 'num'], [null, html`<span class="sr">Actions</span>`, 'act']];
// The text filter is one token on every list; Repos adds its managed and merge filters.
const textChip = () => text.q ? [{ key: 'q', label: `Text: ${text.q}` }] : [];
// sd:5504: each list's filter bar is Activity's (shell.list.fbar): its segments, the text as a token, then Clear all.
// The count sits on the list's title line while a filter hides rows (sd:5281).
// A segment keeps its visible label: yes and no say nothing alone. The token holds the free words only, since a pressed
// segment already shows a managed:, merge: or state: word, and removing the token keeps those words.
const FIELD = /^(managed|merge|state):\w+$/i;
const filterSeg = (k, name, vals, on) => html`<div class="fgroup"><span class="label" aria-hidden="true">${name}</span>${shell.list.fseg(k, name, vals.map(v => ({ v, label: v, pressed: on === v })))}</div>`;
const filterBar = (parts, clear) => { const free = text.q.split(/\s+/).filter(w => w && !FIELD.test(w)).join(' ');
  return shell.list.fbar(html`${parts}${free ? shell.list.tok('q', '', `“${free}”`) : ''}`, { clear: !!clear }); };
const hides = (n, all, noun) => n < all ? html`<span>${shell.list.count(n, all, noun)}</span>` : '';
const repoChips = () => [RS.managed && { key: 'managed', label: `Managed: ${RS.managed}` }, RS.merge && { key: 'merge', label: `Lane merge: ${RS.merge}` }, RS.research && { key: 'research', label: `Research: ${RS.research}` }, ...textChip()].filter(Boolean);
const text = { q: '' };
function tokens() {
  const chips = {}, free = [];
  text.q.split(/\s+/).filter(Boolean).forEach(w => { const m = w.match(/^(managed|merge|state):(\w+)$/i); m ? chips[m[1].toLowerCase()] = m[2].toLowerCase() : free.push(w.toLowerCase()); });
  return { chips, free };
}

// ---------- Repos list ----------
const RS = { sort: 'name', dir: 1, page: 1, size: 25, managed: null, merge: null, research: null, cols: 'git' };
// Research (pack sd:7527): Operations > Resources > Research as a Repos filter. yes keeps the sd-research-kit checkouts
// and draws their documents below the table, no keeps the rest; /api/management/research (sd_tile.py research) is read
// the first time the filter is set, and again on Read again. Until it answers, or when it fails, no row is said to be
// one or not.
let RESEARCH = null;
const researchPaths = () => RESEARCH?.paths ? new Set(RESEARCH.paths.map(tilde)) : null;
const readResearch = () => { RESEARCH = { reading: true };
  return shell.getJSON('/api/management/research').then(d => { RESEARCH = d; }, e => { RESEARCH = { error: e.message }; }).then(() => { if (view === 'repos') renderRepos(); }); };
function researchPart() {
  if (!RS.research) return '';
  if (!RESEARCH || RESEARCH.reading) return html`<p class="why">Reading the research checkouts…</p>`;
  if (!RESEARCH.paths) return unknown('The research checkouts were not read', RESEARCH.error, '/api/management/research');
  return RS.research === 'yes' ? html`<h3>Research documents</h3>${tile(RESEARCH)}` : '';
}
function repoRows() {
  const { chips, free } = tokens();
  const managed = chips.managed || RS.managed, merge = chips.merge || RS.merge, research = RS.research && researchPaths();
  if (RS.research && !RESEARCH) readResearch();
  return ALL.filter(r => (!managed || r.managed === managed) && (!merge || r.merge === merge)
      && (!RS.research || (!!research && research.has(r.path) === (RS.research === 'yes')))
      && free.every(f => (r.path + ' ' + r.remote).toLowerCase().includes(f)))
    .sort((a, b) => String(a[RS.sort]).localeCompare(String(b[RS.sort]), 'en', { numeric: true }) * RS.dir);
}
function gitCells(r) {
  const g = r.git;
  if (!g) return html`<td class="d mono unk" data-k="git" colspan="3"><span class="g-unknown" aria-hidden="true">▨</span> ${why('git') ? 'git state not read' : 'not read: outside the fleet root'}</td>`;
  const lag = g.state === 'behind' ? html`<span class="g-caution">+${g.ahead}/−${g.behind_default}</span>`
    : g.state === 'current' ? `+${g.ahead}/−0` : html`<span class="unk"><span class="g-unknown" aria-hidden="true">▨</span> ${g.headline}</span>`;
  return html`<td class="d mono" data-k="branch">${g.branch || '?'}${g.dirty === null ? html` <span class="unk">· dirty ?</span>` : g.dirty ? html` <span class="dirty">· ${g.dirty} dirty</span>` : ''}</td>
    <td class="d mono" data-k="±">${lag}</td><td class="d mono" data-k="fetched">${age(g.fetched_iso)}</td>`;
}
// build (sd:1629): the settings cells. Floor and Copilot are read from the row's review declaration (sd:5159); a reading that
// failed or was not taken is unknown with its reason, and nothing pinned is a dash that says so.
const unk = words => html`<span class="unk"><span class="g-unknown" aria-hidden="true">▨</span> ${words}</span>`;
function reviewCell(rv, key) {
  if (!rv) return unk('not in the reading');
  if (rv.declaration == null) return rv.error ? unk(rv.error) : html`<span class="no">none declared</span>`;
  if (rv.error) return unk(rv.error);
  const v = key === 'floor' ? rv.severity_floor : rv.automatic_deep;
  return v == null ? html`<span class="no">unset</span>` : String(v);
}
function checksCell(pr) {
  if (!pr || pr.status === 'unknown' || !Array.isArray(pr.required)) return unk(pr?.required_error || pr?.reason || why('repos') || 'no protection reading');
  if (!pr.required.length) return html`<span class="no">none</span>`;
  return html`<span class="checks">${pr.required.map(c => html`<code>${c}</code>`)}</span>${pr.strict ? html`<span class="sr"> (branch must be up to date)</span>` : ''}`;
}
function pinCell(rt, key) {
  if (!rt) return unk('no checkout here');
  const pin = rt[key];
  if (!pin) return html`<span class="no" aria-hidden="true">—</span><span class="sr">not pinned</span>`;
  return pin.error ? unk(pin.error) : html`<span title="${pin.source}">${pin.value}</span>`;
}
function settingCells(r) {
  if (!r.registered) return html`<td class="d mono" data-k="settings" colspan="7"><span class="no">not registered</span></td>`;
  return html`<td class="d mono" data-k="floor">${reviewCell(r.review, 'floor')}</td><td class="d mono" data-k="copilot">${reviewCell(r.review, 'copilot')}</td>
    <td class="d mono" data-k="checks">${checksCell(r.protection)}</td><td class="d mono" data-k="python">${pinCell(r.runtimes, 'python')}</td><td class="d mono" data-k="node">${pinCell(r.runtimes, 'node')}</td>`;
}
function renderRepos() {
  const el = document.getElementById('view-repos');
  if (!DOC.repos) { put(el, unknown('The repo table was not read', why('repos'), 'sd-db repo list')); return; }
  if (repoParam) return renderRepoPage(el);
  const rows = repoRows(), shown = pageOf(rows, RS);
  const auto = REPOS.filter(r => r.merge === 'auto').length, managed = REPOS.filter(r => r.managed === 'yes').length;
  const c = GIT?.counts || {};
  shell.view.count('repos', rows.length);
  put(el, html`<div class="sec-head"><h2 id="repos-h">Repos <button class="help" type="button" aria-label="Help: Repos" data-help="<b>Every registered repo.</b> Open one for its settings, review rules and GitHub protection.">${I('circle-help')}</button></h2>
      <p class="tally">${hides(rows.length, ALL.length, 'repo')}<span>${REPOS.length} registered</span><span>${managed} managed</span><span>${auto} lane merge auto</span></p>
      <p class="tally" id="git-tally">${GIT ? html`${c.repos ?? 0} checkouts under ${tilde(GIT.root || '') || 'the fleet root'} · ${c.dirty ?? 0} dirty · ${c.ahead ?? 0} ahead · ${GIT.repos.filter(g => g.state === 'behind').length} behind default · git read ${seen(GIT)}` : html`<span class="g-unknown">▨</span> git state not read: ${why('git')}`}</p>
      <div class="tools"><button class="btn quiet sm" type="button" data-cmd="repos.refresh" data-obj="repos:git">${I('rotate-ccw')}<span>Refresh git state</span></button></div></div>
    <div class="filters toolbar-2">${filterBar(html`${filterSeg('managed', 'Managed', ['yes', 'no'], RS.managed)}${filterSeg('merge', 'Lane merge', ['auto', 'manual'], RS.merge)}${filterSeg('research', 'Research', ['yes', 'no'], RS.research)}${filterSeg('cols', 'Columns', ['git', 'settings'], RS.cols)}`, repoChips().length)}</div>
    ${rows.length ? html`<table class="ledger" aria-labelledby="repos-h" data-tfilter="page" data-toolbar="toolbar"><thead>${shell.list.sortHead(repoCols(), RS)}</thead>
      <tbody>${shown.map(r => html`<tr data-id="${'repo:' + r.path}" data-repo="${r.path}">
        <td class="g" aria-hidden="true">${I(r.registered ? 'settings' : 'folder-code')}</td>
        <td class="what"><button type="button">${r.name}</button>${r.registered ? '' : html`<span class="unreg">not registered</span>`}</td>
        <td class="d mono ${r.managed}" data-k="managed">${r.managed}</td>
        <td class="d mono ${r.merge === 'auto' ? 'yes' : 'no'}" data-k="merge">${r.merge}</td>
        <td class="d mono" data-k="lane">${r.lane}</td>
        ${RS.cols === 'settings' ? settingCells(r) : gitCells(r)}
        <td class="act end">${shell.commands.rowActions('repo:' + r.path)}</td></tr>`)}</tbody></table>${shell.list.pager(rows.length, RS, 'repos')}`
      : html`<p class="empty">No repo matches ${text.q ? `“${text.q}”` : 'these filters'}. <button class="linkbtn" type="button" data-clear>Clear filters</button></p>`}
    ${researchPart()}
    ${RS.cols === 'settings' ? html`<p class="note">Managed and Merge switch from a row's actions, each one sd-db repo verb on the hub. Floor and Copilot are read-only here: they change through the row's review declaration, drafted from its ${I('settings')} settings page. Required checks are the nightly protection reading; Python and Node are the versions the checkout pins.</p>` : ''}`);
}
// Refresh git state (sd:5614, sd:6697): the post writes nothing, but an answered post makes the next read wait for a new git reading (sd:4955).
const refreshRepos = () => window.shell.post('/api/repos/refresh', {}).then(reread).then(() => 'Git state refreshed');
document.getElementById('view-repos').addEventListener('click', e => {
  if (e.target.closest('.rowact')) return;
  const cols = e.target.closest('[data-f="cols"]');
  if (cols) { RS.cols = cols.dataset.v === 'settings' ? 'settings' : 'git'; if (!repoCols().some(c => c[0] === RS.sort)) RS.sort = 'name'; renderRepos(); return setURL(); }
  const f = e.target.closest('[data-f="managed"], [data-f="merge"], [data-f="research"]'), tr = e.target.closest('tr[data-repo]');
  if (e.target.closest('[data-clear]')) { RS.managed = RS.merge = RS.research = null; text.q = ''; shell.filter(''); RS.page = 1; renderRepos(); return setURL(); }
  if (shell.list.sortBy(e, RS) || shell.list.paging(e, RS)) { shell.list.keepFocus(e, renderRepos); return setURL(); }
  if (f) { RS[f.dataset.f] = RS[f.dataset.f] === f.dataset.v ? null : f.dataset.v; RS.page = 1; renderRepos(); return setURL(); }
  if (tr) selectRow(tr.dataset.id, true);
});

// ---------- One settings page per repo ----------
const pending = [];
function setRow(k, v, ctl = '', attrs = '') { return html`<div class="row"${attrs}><span class="k">${k}</span><span class="v">${v}</span><span class="ctl">${ctl}</span></div>`; }
const lock = w => html`<span class="lock">${I('lock')}${w}</span>`;
// sd:3075: Move lane offers the hub, each host a row already names, and any typed name; the server checks the name.
const laneHosts = () => ['hub', ...new Set(REPOS.map(r => r.lane).filter(h => h && h !== 'hub').sort())];
// Turning managed on opens Standard setup (sd:5610): its yes runs the command, which asks Manage only or Apply.
const manageSeg = r => html`<span class="segctl" role="group" aria-label="managed"><button type="button" data-cmd="repo.standard" data-obj="repo:${r.path}" aria-pressed="false">yes</button><button type="button" aria-pressed="true">no</button></span>`;
const seg = (key, opts, cur) => html`<span class="segctl" role="group" aria-label="${key}">${opts.map(o => html`<button type="button" data-set="${key}" data-v="${o}" aria-pressed="${String(o === cur)}">${o}</button>`)}</span>`;
// sd:3984: the daily sweep's consent for a repo outside the owner rule. The owner rule decides first, so an owner's or a
// denied repo shows the rule and offers no toggle; an unread flag is unknown, not no.
const DEP_GATE = { owner: ['always (owner)', 'every platypeeps repo, ROUTINE.md'], denied: ['denied by name', 'the deny list in ROUTINE.md'] };
const depShown = r => r.depGate ? DEP_GATE[r.depGate][0] : r.dependabot ?? html`<span class="no">not read</span>`;
const depCtl = r => r.depGate ? lock(DEP_GATE[r.depGate][1]) : r.dependabot == null ? lock(why('repos') || 'the row has no dependabot column') : seg('dependabot', ['yes', 'no'], r.dependabot);

// sd:3465: a repo's publish channels. `r.channels` is the canonical text the server read (`none`: no setting), posted back
// as `before`; a value the library refuses shows its reason and offers Clear, never the defaults.
const CH = ['obsidian', 'notion', 'drive'];
const chParse = t => { try { const v = JSON.parse(t); return v && typeof v === 'object' && !Array.isArray(v) ? v : null; } catch { return null; } };
const chCanon = v => v === null ? 'none' : JSON.stringify(Object.fromEntries(Object.keys(v).sort().map(k => [k, v[k]])));
const chWords = v => v === null ? 'none' : CH.filter(c => v[c]).map(c => `${c}=${v[c].folder || ''}`).join(' ');
// sd:4632, sd:4192: the stored object also holds inbound keys (the repo's feed), which the form shows and never edits.
// `chStored` is all of it; `chSetting` is the publish channels alone, null when the row names none.
const chStored = r => r.channels === 'none' || r.chProblem ? null : chParse(r.channels);
const chSetting = r => { const v = chStored(r); return v && CH.some(c => v[c]) ? Object.fromEntries(CH.filter(c => v[c]).map(c => [c, v[c]])) : null; };
// The mail aliases (sd:4191) have their own field below, so they are not among the display-only keys.
const chInbound = r => Object.entries(chStored(r) || {}).filter(([k]) => !CH.includes(k) && k !== 'email');
const chDefault = (r, c) => { const n = r.path.split('/').pop(); return c === 'notion' ? n : `Briefs/${n}`; };
function chShown(r) {
  const inbound = chInbound(r).map(([k, v]) => html`<p class="why">Inbound ${k}, display only: <code>${v && v.url || JSON.stringify(v)}</code></p>`);
  return html`${chOutbound(r)}${inbound}`;
}
function chOutbound(r) {
  if (r.chProblem) return html`<code>${r.channels}</code><p class="why">Cannot be used: ${r.chProblem}. Publishing waits until it is set again or cleared.</p>`;
  const v = chSetting(r);
  if (!v) return html`<span class="no">none · defaults</span><p class="why">Each document's research.conf.py designation, in ${CH.map(c => chDefault(r, c)).filter((f, i, a) => a.indexOf(f) === i).join(' and ')}.</p>`;
  return CH.filter(c => v[c]).map(c => `${c} ${v[c].folder || chDefault(r, c) + ' (default)'}`).join(' · ');
}
function chForm(r) {
  const v = chSetting(r), on = c => r.chProblem ? false : !v || !!v[c];
  return html`<fieldset class="chans" id="ch-form"><legend class="sr">Channels for ${r.name}</legend>
    ${CH.map(c => html`<label class="chk"><input type="checkbox" data-set="channels" data-ch="${c}"${on(c) ? html` checked` : ''}>${c}</label><input class="fld" type="text" data-set="channels" data-chf="${c}" value="${v?.[c]?.folder || ''}" placeholder="${chDefault(r, c)}" aria-label="${c} folder" autocomplete="off" spellcheck="false">`)}
    ${r.channels !== 'none' ? html`<button class="btn quiet sm" type="button" data-ch-clear>${I('x')}Clear setting</button>` : ''}</fieldset>`;
}
// The form's value: a checked channel with its folder, or {} for the default; undefined when no box is checked.
function chRead() {
  const out = {};
  CH.forEach(c => { if (document.querySelector(`#ch-form [data-ch="${c}"]`).checked) { const f = document.querySelector(`#ch-form [data-chf="${c}"]`).value.trim(); out[c] = f ? { folder: f } : {}; } });
  return Object.keys(out).length ? out : undefined;
}
function proposeChannels(r, value) {
  if (value === undefined) { withdraw('channels', true); shell.toast('Not proposed: no channel is checked. Clear setting removes the setting.'); return; }
  if (!r.chProblem && chSetting(r) && chCanon(value) === chCanon(chSetting(r))) return withdraw('channels');
  const was = r.chProblem ? r.channels : chWords(chSetting(r));
  addProposal({ key: 'channels', value, shown: chWords(value), before: r.channels, verb: 'channels', route: 'sd-db', icon: 'terminal',
    title: `Publishing: ${was} → ${chWords(value)}`, cmd: `${SDDB} repo channels ${shq(r.path)} ${value === null ? 'none' : CH.filter(c => value[c]).map(c => shq(`${c}=${value[c].folder || ''}`)).join(' ')}`,
    // Undo writes the whole setting the page read back. A value the library refuses cannot be written back, and a row that
    // held only inbound keys cannot be: a set that names no channel keeps the new ones, and none would drop the feed.
    back: r.chProblem || (r.channels !== 'none' && !chSetting(r)) ? undefined : chStored(r),
    note: `Takes effect at the next render: your next commit, or sd-research-kit render in ${r.path} now.` });
}
// sd:4191: the repo's mail aliases, labels of local-mail-intake's conf whose threads Today shows under this repo. The write
// names only `email`, so the library keeps the channels and the other inbound keys. Undo writes the old setting back, which
// replaces `email` only when that setting named it; over NULL it clears. An emptied field is no write: the library keeps
// a key a set leaves out, so only Clear setting removes the aliases.
const chAliases = r => chStored(r)?.email?.aliases || [];
function proposeAliases(r, text) {
  const labels = [...new Set(text.split(/[\s,]+/).filter(Boolean))].sort(), was = chAliases(r).join(',') || 'none';
  if (labels.join(',') === chAliases(r).join(',')) return withdraw('mail_aliases');
  if (!labels.length) { withdraw('mail_aliases', true); shell.toast('Not proposed: an alias field cannot be emptied alone. Clear setting removes the whole setting.'); return; }
  addProposal({ key: 'mail_aliases', value: { email: { aliases: labels } }, shown: labels.join(','), before: r.channels, verb: 'channels', route: 'sd-db',
    icon: 'terminal', title: `Email aliases: ${was} → ${labels.join(',')}`, cmd: `${SDDB} repo channels ${shq(r.path)} ${shq(`email=${labels.join(',')}`)}`,
    back: r.channels === 'none' ? null : !r.chProblem && chAliases(r).length ? chStored(r) : undefined,
    note: "Today shows these aliases' threads under this repo from its next read." });
}

function renderRepoPage(el) {
  const r = REPOS.find(x => x.path === repoParam);
  if (!r) { put(el, html`<p class="empty">No registered repo at <code>${repoParam}</code>. <a href="?#view=repos">All repos</a></p>`); return; }
  const url = r.slug ? `https://github.com/${r.slug}` : null;
  const rv = r.review || { declaration: null, error: 'not in the reading' }, pr = r.protection;
  put(el, html`
    <div class="crumb"><a class="btn quiet sm" href="?#view=repos">${I('chevron-left')}All repos</a><h2>${r.name}</h2>${url ? html`<a class="ext" href="${url}" target="_blank" rel="noopener">${r.slug}${I('arrow-up-right')}</a>` : ''}</div>
    <ul class="routes" aria-label="How edits land">
      <li><span class="route">${I('terminal')}sd-db · local</span> saves the change on this machine</li>
      <li><span class="route">${I('terminal')}sd-db declare · copy</span> rewrites the review rules when you press Write</li>
      <li><span class="route">${I('shield-check')}GitHub · API</span> shown here; change it on the classic Protection screen</li>
    </ul>

    <section class="set" aria-labelledby="set-db"><header><h3 id="set-db">Repo settings <button class="help" type="button" aria-label="Help: Repo settings" data-help="<b>What the merge runner and the dashboard read for this repo.</b> A field with a control changes here. One without changes only when you register the repo again.">${I('circle-help')}</button></h3><span class="route">${I('terminal')}sd-db · local</span>
      <p class="src">read ${hhmm(READ)} UTC</p></header>
      ${setRow('path', r.path, lock('primary key'))}
      ${setRow('remote', r.remote || html`<span class="no">none</span>`, lock('from the checkout'))}
      ${setRow(html`Merge automatically <button class="help" type="button" aria-label="Help: Merge automatically" data-help="<b>auto</b>: a finished, reviewed change goes into the merge queue and merges on its own. <b>manual</b>: it waits for you. New repos start manual.">${I('circle-help')}</button>`, r.merge, seg('runner_merge', ['manual', 'auto'], r.merge), html` data-key="runner_merge"`)}
      ${setRow(html`Managed <button class="help" type="button" aria-label="Help: Managed" data-help="<b>yes</b>: the workflow keeps this repo to the standard setup and lists it everywhere. <b>no</b>: it is registered but left alone.">${I('circle-help')}</button>`, r.managed, r.managed === 'no' ? manageSeg(r) : seg('managed', ['yes', 'no'], r.managed), html` data-key="managed"`)}
      ${r.managed === 'yes' ? setRow(html`Standard setup <button class="help" type="button" aria-label="Help: standard setup" data-help="<b>The standard setup:</b> these settings, the GitHub settings and the shared files every managed repo carries. Check shows a preview; nothing changes until you apply. Dependabot alerts you turned off stay off.">${I('circle-help')}</button>`, html`<span class="no">not checked</span>`,
        html`<button class="btn quiet sm" type="button" data-cmd="repo.standard" data-obj="repo:${r.path}">Check</button>`, html` data-key="standard"`) : ''}
      ${setRow(html`Auto-merge dependency updates <button class="help" type="button" aria-label="Help: Auto-merge dependency updates" data-help="<b>Whether the daily dependency sweep may merge small version bumps here.</b> Repos of your own organisation are swept unless the sweep's rules name them; for any other owner, this is your consent. It takes effect at the sweep's next run.">${I('circle-help')}</button>`, depShown(r), depCtl(r), html` data-key="dependabot"`)}
      ${setRow(html`Merge machine <button class="help" type="button" aria-label="Help: Merge machine" data-help="<b>The machine that merges this repo's changes:</b> <b>hub</b>, where every repo starts, or a machine's short name in lower case. Every other machine refuses to merge here.">${I('circle-help')}</button>`, r.lane,
        html`<input class="fld" type="text" data-set="lane_host" list="lane-hosts" value="${r.lane}" aria-label="Move lane to" autocomplete="off" spellcheck="false"><datalist id="lane-hosts">${laneHosts().map(h => html`<option value="${h}"></option>`)}</datalist>`, html` data-key="lane_host"`)}
      ${setRow(html`GitHub issues <button class="help" type="button" aria-label="Help: GitHub issues" data-help="<b>Whether this repo takes GitHub issues from other people.</b> <b>off</b>, where every repo starts: work stays in tasks. <b>read</b>: lists outside issues to turn into tasks. <b>ask</b>: agents may also close and update issues after you approve each one, and tasks offer File GitHub issue.">${I('circle-help')}</button>`, r.github_issues ?? html`<span class="no">not read</span>`,
        r.github_issues == null ? lock(why('repos') || 'the policy declaration did not read') : seg('github_issues', ['off', 'read', 'ask'], r.github_issues), html` data-key="github_issues"`)}
      ${setRow(html`Issue intake <button class="help" type="button" aria-label="Help: Issue intake" data-help="<b>Which outside issues the intake list shows for this repo.</b> <b>all</b>, where every repo starts: every open issue no task carries. <b>mine</b>: only issues assigned to you, mentioning you or asking your review. It changes what you see, not what agents may write.">${I('circle-help')}</button>`, r.issues_scope ?? html`<span class="no">not read</span>`,
        r.issues_scope == null ? lock(why('repos') || 'the policy declaration did not read') : seg('issues_scope', ['all', 'mine'], r.issues_scope), html` data-key="issues_scope"`)}
      ${setRow(html`Jira project <button class="help" type="button" aria-label="Help: Jira project" data-help="<b>The Jira project that File Jira issue files in,</b> by its key, such as EXAMPLE. <b>off</b>, where every repo starts, hides that action. The Jira sign-in lives in the dashboard's settings file.">${I('circle-help')}</button>`, r.jira_project ?? html`<span class="no">not read</span>`,
        r.jira_project == null ? lock(why('repos') || 'the row has no jira_project column') : html`<input class="fld" type="text" data-set="jira_project" value="${r.jira_project}" placeholder="KEY or off" aria-label="Jira project" autocomplete="off" spellcheck="false">`, html` data-key="jira_project"`)}
      ${setRow(html`Publishing <button class="help" type="button" aria-label="Help: Publishing" data-help="<b>Where this repo's briefs and documents publish:</b> a folder in Obsidian, a page in Notion, a path in Drive. A checked place is on; an empty folder uses the default, shown greyed. A document still opts in to Notion or Drive under Publish to on Documents. <b>none</b>: each document's own settings and the default folders. A change takes effect at the next render.">${I('circle-help')}</button>`, chShown(r), chForm(r), html` data-key="channels"`)}
      ${setRow(html`Email aliases <button class="help" type="button" aria-label="Help: Email aliases" data-help="<b>The mail aliases this repo owns.</b> Today lists mail sent to them that waits on you. Names only: the addresses stay in the mail settings file.">${I('circle-help')}</button>`, chAliases(r).join(', ') || html`<span class="no">none</span>`,
        html`<input class="fld" type="text" data-set="mail_aliases" value="${chAliases(r).join(', ')}" placeholder="label, label" aria-label="Email aliases" autocomplete="off" spellcheck="false">`, html` data-key="mail_aliases"`)}
      ${setRow('Mode', r.mode || html`<span class="no">unset</span>`, lock('set when the repo is registered'))}
      ${setRow('Checks', r.ci, lock('sd-db repo ci'))}
      ${setRow('Writing pieces from', r.pieces, lock('changes only with a database update'))}
      ${setRow('registered', html`<time class="rel" datetime="${r.created}"></time> <span class="no">· updated</span> <time class="rel" datetime="${r.updated}"></time>`)}
    </section>

    <section class="set" aria-labelledby="set-review"><header><h3 id="set-review">Review declaration <button class="help" type="button" aria-label="Help: review declaration" data-help="<b>How code review treats this repo:</b> which tiers and categories it runs, the severity that blocks a merge, and the Copilot choice. A value review would refuse is refused here too.">${I('circle-help')}</button></h3><span class="route">${I('terminal')}sd-db declare · copy</span>
      <p class="src">${rv.error && rv.declaration == null ? 'not read' : `read ${hhmm(READ)} UTC`}</p></header>
      ${rv.declaration == null && rv.error ? unknown('Not read', rv.error)
        : rv.declaration == null ? setRow('declaration', html`<span class="no">none</span><p class="why">The repo inherits the machine's review settings.</p>`, html`<button class="btn quiet sm" type="button" data-create-review>${I('plus')}Add declaration</button>`)
        : rv.error ? html`${setRow('declaration', html`<span class="no">${rv.error}</span>`)}<details class="srcfile"><summary>Show the declaration</summary><pre class="code">${rv.declaration}</pre></details>`
        : html`${setRow(html`Blocking severity <button class="help" type="button" aria-label="Help: Blocking severity" data-help="A finding at this level or above blocks the merge: high, medium, low or unspecified.">${I('circle-help')}</button>`, rv.severity_floor || html`<span class="no">unset</span>`, html`<select class="fld" data-set="severity_floor" aria-label="Blocking severity">${['', 'high', 'medium', 'low', 'unspecified'].map(v => html`<option value="${v}"${(rv.severity_floor || '') === v ? html` selected` : ''}>${v || 'unset'}</option>`)}</select>`, html` data-key="severity_floor"`)}
           ${rv.automatic_deep == null ? setRow('Automatic deep Copilot review', html`<span class="no">unset</span>`) : setRow('Automatic deep Copilot review', String(rv.automatic_deep), seg('automatic_deep', ['true', 'false'], String(rv.automatic_deep)), html` data-key="automatic_deep"`)}
           <details class="srcfile"><summary>Show the declaration</summary><pre class="code">${rv.declaration}</pre></details>`}
    </section>

    <section class="set" aria-labelledby="set-local"><header><h3 id="set-local">CLAUDE.local.md overrides</h3><span class="route">${I('terminal')}local file</span>
      <p class="src">not read</p></header>
      ${unknown('Not read by the dashboard', 'The file is gitignored and can hold personal notes, so the build does not read it. Open it in the checkout to see its sd block.')}
    </section>

    <section class="set" aria-labelledby="set-gh"><header><h3 id="set-gh">GitHub · <code>${pr?.default_branch || 'default branch'}</code> <button class="help" type="button" aria-label="Help: GitHub protection" data-help="<b>What GitHub enforces on the default branch,</b> as of last night's check. Nothing here calls GitHub.">${I('circle-help')}</button></h3><span class="route">${I('shield-check')}GitHub · API</span>
      <p class="src">${pr?.observed_at ? html`checked <time class="rel" datetime="${pr.observed_at}"></time>${pr.borrowed_from ? html` · borrowed from <code>${pr.borrowed_from}</code>` : ''}` : 'not observed'}</p></header>
      ${!pr || pr.status === 'unknown' ? unknown(pr ? 'Protection unknown' : 'Not read', pr?.reason || why('repos') || 'no protection reading for this repo', r.slug ? `gh api repos/${r.slug}/rulesets` : '')
        : html`${setRow('status', pr.status)}
           ${pr.gaps.length ? pr.gaps.map(g => setRow(html`<code>${g.id}</code>`, html`<span class="g-caution" aria-hidden="true">▲</span> gap<p class="why">${g.gap || ''}</p>`)) : setRow('gaps', 'none found')}
           <p class="note">Every repo's gaps side by side: <a href="/protection">Protection (classic)</a>.</p>`}
    </section>`);
  pending.forEach(p => markPending(p.key));
}
function markPending(key) {
  const row = document.querySelector(`.row[data-key="${key}"]`), p = pending.find(x => x.key === key); if (!row || !p) return;
  row.setAttribute('data-pending', ''); row.querySelector('.v').dataset.proposed = p.shown ?? (p.value === '' || p.value == null ? 'unset' : String(p.value));
  row.querySelectorAll('.segctl [data-v]').forEach(b => b.toggleAttribute('data-proposed', b.dataset.v === String(p.value)));
}

// ---------- Proposals: two routes (build: CLAUDE.local.md and GitHub edits are not ported) ----------
function withdraw(key, quiet) {
  const p = pending.find(x => x.key === key); if (!p) return;
  const row = document.querySelector(`.row[data-key="${key}"]`);
  row?.removeAttribute('data-pending'); row?.querySelectorAll('[data-proposed]').forEach(b => b.removeAttribute('data-proposed'));
  dropProposal(p);
  if (!quiet) shell.toast(`Proposal withdrawn · ${p.title}`);
}
// Pack sd:7536: the plain names the settings page and its proposals show; the column each sets stays in the line.
const LABEL = { runner_merge: 'Merge automatically', managed: 'Managed', dependabot: 'Auto-merge dependency updates', github_issues: 'GitHub issues', issues_scope: 'Issue intake',
  severity_floor: 'Blocking severity', automatic_deep: 'Automatic deep Copilot review' };
function propose(key, value) {
  const r = REPOS.find(x => x.path === repoParam), rv = r.review || {};
  let p;
  if (key === 'channels') return proposeChannels(r, value === null ? null : chRead());
  if (key === 'mail_aliases') return proposeAliases(r, value);
  if (key === 'runner_merge' || key === 'managed' || key === 'dependabot' || key === 'github_issues' || key === 'issues_scope') {
    const before = key === 'runner_merge' ? r.merge : r[key];
    if (value === before) return withdraw(key);
    const verb = key.replace('_', '-');
    p = { key, value, before, verb, route: 'sd-db', icon: 'terminal', title: `${LABEL[key]}: ${before} → ${value}`, cmd: `${SDDB} repo ${verb} ${shq(r.path)} ${value}`,
      note: key === 'runner_merge' && value === 'manual' ? 'Changes to this repo will wait for you before they merge.' : key === 'runner_merge' ? 'A finished, reviewed change to this repo goes into the merge queue on its own.'
        : key === 'dependabot' ? `The daily Dependabot sweep ${value === 'yes' ? 'may merge' : 'skips'} this repo's bumps from its next run.`
        : key === 'issues_scope' ? (value === 'mine' ? 'Issue intake lists only issues assigned to you, mentioning you or asking your review.' : 'Issue intake lists every open outside issue no task carries.')
        : key === 'github_issues' ? (value === 'ask' ? "Agents may close and update issues on this repo's own remote, each write approved by you; its tasks offer File GitHub issue." : value === 'read' ? "Agents read this repo's issues and map each to an sd item; they write none." : 'Agents write no GitHub issues here; work stays in sd items.') : 'Writes one column; no new row.' };
  } else if (key === 'lane_host') {
    if (!value || value === r.lane) return withdraw(key);
    p = { key, value, before: r.lane, verb: 'lane-host', route: 'sd-db', icon: 'terminal', title: `Merge machine: ${r.lane} → ${value}`,
      cmd: `${SDDB} repo lane-host ${shq(r.path)} ${shq(value)}`,
      note: `Only ${value === 'hub' ? 'the hub' : value} merges this repo's changes; every other machine refuses and says how to move it back. Queued changes follow, since the queue lives on the hub.` };
  } else if (key === 'jira_project') {
    // sd:5103: a key or off; an empty field is off. repos.set_jira_project refuses a bad key when the change runs.
    const want = !value || value.toLowerCase() === 'off' ? 'off' : value.toUpperCase();
    if (want === r.jira_project) return withdraw(key);
    p = { key, value: want, before: r.jira_project, verb: 'jira-project', route: 'sd-db', icon: 'terminal', title: `Jira project: ${r.jira_project} → ${want}`,
      cmd: `${SDDB} repo jira-project ${shq(r.path)} ${shq(want)}`,
      note: want === 'off' ? "This repo's tasks offer no File Jira issue." : `File Jira issue files this repo's tasks in Jira project ${want}, after a preview you confirm.` };
  } else if (key === 'severity_floor' || key === 'automatic_deep') {
    if (value === String(key === 'severity_floor' ? rv.severity_floor || '' : rv.automatic_deep)) return withdraw(key);
    const edit = body => {
      if (key === 'severity_floor') { if (value) body.severity_floor = value; else delete body.severity_floor; return; }
      const copilot = body.copilot_review && typeof body.copilot_review === 'object' && !Array.isArray(body.copilot_review) ? body.copilot_review : {};
      body.copilot_review = { ...copilot, automatic_deep: value === 'true' };
    };
    if (!reviewDiff(rv.declaration || '', edit)) { shell.toast('Not proposed: the review declaration is not a JSON object'); return; }
    // The diff and the line are set by restateDeclaration, with every other pending edit for this repo.
    p = { key, value, route: 'declare', icon: 'terminal', title: `Review rules · ${LABEL[key]}: ${value || 'unset'}`, edit,
      note: 'Replaces the review declaration on the row with every pending review edit; the next review reads it. Nothing changes until Write runs.' };
  }
  if (p) addProposal(p);
}
// build (review, PR #50): a declaration change is made on the parsed object, checked to parse again, and shown as a line
// diff of the two texts, so a proposal is always valid JSON. The shown text is JSON.stringify's two-space layout, the
// layout the server sends; `after` is the compact text the sd-db line writes (sd:5159). null when it is not a JSON object.
// No declaration is the empty object, so the starter and a declaration cleared since the proposal draft on it (sd:5958).
function reviewDiff(text, change) {
  let body;
  try { body = text.trim() ? JSON.parse(text) : {}; } catch { return null; }
  if (!body || typeof body !== 'object' || Array.isArray(body)) return null;
  change(body);
  const after = JSON.stringify(body, null, 2);
  JSON.parse(after);
  return { diff: lineDiff(text.trim() ? text.replace(/\n$/, '').split('\n') : [], after.split('\n')), after: JSON.stringify(body) };
}
// The longest common run of lines is kept; the rest is a del or an add (the declarations are at most 16 KiB, DECLARATION_BYTES).
function lineDiff(a, b) {
  const n = a.length, m = b.length, L = Array.from({ length: n + 1 }, () => new Array(m + 1).fill(0));
  for (let i = n - 1; i >= 0; i--) for (let j = m - 1; j >= 0; j--) L[i][j] = a[i] === b[j] ? L[i + 1][j + 1] + 1 : Math.max(L[i + 1][j], L[i][j + 1]);
  const out = [];
  let i = 0, j = 0;
  while (i < n && j < m) {
    if (a[i] === b[j]) { out.push(['', a[i]]); i++; j++; } else if (L[i + 1][j] >= L[i][j + 1]) out.push(['del', a[i++]]); else out.push(['add', b[j++]]);
  }
  while (i < n) out.push(['del', a[i++]]);
  while (j < m) out.push(['add', b[j++]]);
  return out;
}
const ROUTE = { 'sd-db': ['sd-db change', 'sd-db · local'], declare: ['declaration change', 'sd-db declare · local'] };
// sd:5159 review: the row holds one review declaration, so each pending line for a repo carries every pending edit for it,
// made on the reading in the order they were proposed. Running any one line, or all of them, leaves the same row.
// sd:5958: `base` is the declaration `after` was drafted on, the `before` Write sends, so the two always come from one
// reading. A reading that cannot be drafted on leaves no `after` and says why: Write is off until a reading can be.
function restateDeclaration(path) {
  const r = REPOS.find(x => x.path === path), edits = pending.filter(x => x.edit && x.repo?.path === path).reverse();
  if (!r || !edits.length) return;
  const base = r.review?.declaration || '', change = reviewDiff(base, body => edits.forEach(x => x.edit(body)));
  edits.forEach(x => Object.assign(x, change
    ? { diff: change.diff, after: change.after, base, why: '', cmd: `${SDDB} repo declare ${shq(path)} review ${shq(change.after)}` }
    : { after: null, base: null, cmd: '', why: 'the review declaration on the row is not a JSON object now; fix it there, or withdraw this proposal' }));
}
function addProposal(p) {
  const r = REPOS.find(x => x.path === repoParam);
  p.id = `prop:${p.key}`; p.type = ROUTE[p.route][0]; p.label = p.title; p.repo = r;
  const i = pending.findIndex(x => x.key === p.key);
  if (i >= 0 && pending[i].title === p.title) return;
  if (i >= 0) pending.splice(i, 1);
  pending.unshift(p);
  if (p.edit) restateDeclaration(r.path);
  shell.commands.put(p);
  markPending(p.key);
  showProposals(true);
}
function dropProposal(p) {
  const i = pending.indexOf(p); if (i >= 0) pending.splice(i, 1);
  if (p.edit) restateDeclaration(p.repo.path);
  renderRepos(); pending.length ? showProposals() : defaultDetails();
}
function diffHTML(d) { return html`<pre class="diff">${d.map(([k, l]) => html`<span class="${k}">${k === 'add' ? '+ ' : k === 'del' ? '− ' : k === 'hd' ? '' : '  '}${l}</span>`)}</pre>`; }
function showProposals(open) {
  const r = REPOS.find(x => x.path === repoParam);
  put(details, html`<p class="kind">${I('settings')} Repo settings · ${r.name}</p><h2>${plural(pending.length, 'proposal')}</h2>
    ${pending.map(p => html`<div class="proposal"><header>${I(p.icon)}<span class="label">${ROUTE[p.route][1]}</span></header>
      <div class="body"><b>${p.title}</b>${p.diff ? diffHTML(p.diff) : ''}<span class="why">${p.note}</span>${shell.commands.bar(p.id)}</div></div>`)}`);
  swap();
  shell.commands.select(pending[0]?.id);
  shell.setContext(`${r.name} settings`);
  if (open) shell.openPane('tab-details');
}
document.getElementById('view-repos').addEventListener('change', e => {
  const c = e.target.closest('select[data-set], input[data-set]'); if (c) propose(c.dataset.set, c.value.trim());
});
document.getElementById('view-repos').addEventListener('click', e => {
  const b = e.target.closest('.segctl [data-set]');
  if (b) propose(b.dataset.set, b.dataset.v);
  if (e.target.closest('[data-ch-clear]')) propose('channels', null);
  if (e.target.closest('[data-create-review]')) {
    const r = REPOS.find(x => x.path === repoParam);
    // `edit` lets restateDeclaration fold the starter into a declaration a later reading finds, rather than replace it.
    addProposal({ key: 'review-declaration', route: 'declare', icon: 'terminal', title: 'review declaration · new', edit: body => { body.severity_floor = 'high'; },
      cmd: `${SDDB} repo declare ${shq(r.path)} review ${shq('{"severity_floor":"high"}')}`,
      diff: [['hd', '@@ new review declaration @@'], ['add', '{'], ['add', '  "severity_floor": "high"'], ['add', '}']],
      note: 'A starting declaration with the floor the managed repos use. Nothing changes until Write runs.' });
  }
});

// ---------- Writes (build) ----------
// landing(): the run returns the write's promise, the shell's run contract (shell.js, bulk:start; sd:2124). The shell toasts
// the text once the write landed, offers Undo only then, and folds a bulk group into one toast. `msg` and `inverse` get
// what the write answered; `inverse` reverses this write, once. A landed write rereads (shell.read's barrier); a 409 loads
// again, since the row it named has moved and nothing landed.
function landing(promise, msg, inverse) {
  // A 409, or a write with no answer (no status: it may have landed, pack sd:9485), loads again; the latter is unknown.
  const stale = err => { if (err && !err.status) err.unknown = true; if (err && (err.stale || err.unknown)) load(); throw err; };
  return promise.then(v => {
    let spent = false;
    const undo = inverse && (() => { if (spent) return Promise.resolve(false); spent = true; return inverse(v).catch(stale); });
    return { text: msg(v), undo };
  }, stale);
}
// The command's undo: the shell passes what the run answered.
const undoOf = (o, r) => r && r.undo ? r.undo() : false;
async function setRepo(field, r, value, before) {
  const got = await window.shell.post(`/api/repos/${field}`, { path: r.path, value, before });
  await reread();
  return got;
}
async function declare(r, value, before) {
  const got = await window.shell.post('/api/actions/run', { action: 'declaration.write', path: r.path, kind: 'review', value, before });
  await reread();
  return got;
}
const wordOf = (f, r) => f === 'runner-merge' ? r.merge : r.managed;
const other = (f, v) => f === 'runner-merge' ? (v === 'auto' ? 'manual' : 'auto') : (v === 'yes' ? 'no' : 'yes');
// A flip's Undo writes the old value back, with the value this flip wrote as its before.
function flipRepo(field, o) {
  const r = o.row, before = wordOf(field, r), value = other(field, before);
  return landing(setRepo(field, r, value, before), () => `${colOf(field)} ${value} · ${o.label}`,
    () => setRepo(field, REPOS.find(x => x.path === o.path) || r, before, value));
}
const colOf = field => field === 'runner-merge' ? 'runner_merge' : 'managed';

// ---------- Commands (products/system/commands.md) ----------
// build (sd:2485): one object per drawn row, for shell.read to put and to retire once a read stops listing it. The ship
// lane's assignment rows moved to Queue's Overview (pack sd:7115, sd:7217). Process and live-worktree rows take no command; their objects
// let a selection or a ?row= link survive a read. A pending proposal is the page's own and stays until it is withdrawn.
function objects() {
  const out = ALL.map(r => ({ id: `repo:${r.path}`, type: 'repo', label: r.name, path: r.path, row: r }));
  (CLAUDE?.sessions || []).forEach(r => out.push({ id: `cs:${r.id}`, type: 'claude session', label: r.name || r.id, sid: r.id, pid: r.pid, row: r }));
  if (SESS) out.push({ id: 'wt:abandoned', type: 'worktrees', label: `${SESS.abandoned} abandoned worktrees`, n: SESS.abandoned },
    { id: 'wt:live', type: 'live worktrees', label: `${liveTrees()} live worktrees` },
    ...SESS.processes.map((p, i) => ({ id: `proc:${i}`, type: 'process', label: p.command })),
    ...(SESS.worktrees || []).map((t, i) => ({ id: `tree:${i}`, type: 'worktree', label: t.name })));
  SERVICES.forEach(s => out.push({ id: `svc:${s.label}`, type: 'service', label: s.name, name: s.label, running: s.state === 'running', revision: s.revision, caps: s.capabilities || {} }));
  out.push({ id: 'repos:git', type: 'git state', label: 'Git state' });
  if (SATS) out.push({ id: 'sats:request', type: 'satellite refresh', label: 'Refresh satellites', request: SATS.request },
    ...SATS.satellites.map(x => ({ id: `sat:${x.host}`, type: 'satellite', label: x.host })));
  if (UPLOAD?.set_up) out.push({ id: 'upload:link', type: 'upload link', label: `${UPLOAD.prefix} upload link`, state: UPLOAD.link_state });
  CRON.forEach(c => out.push({ id: `cron:${c.name}`, type: 'job', label: c.name, job: c.name, name: c.name, service: c.service, failed: c.rank === 'warning', running: c.state === 'running', pid: c.pid, exit: c.last_exit, revision: c.revision, caps: c.capabilities || {} }));
  return [...out, ...pending];
}
const registeredWhy = o => o.row.registered || 'register this repo first (sd-db repo add)';
// sd:5100: the repo's open pull requests as `sd shadow sync` last saw them, keyed by its lower-cased GitHub slug.
const pullsOf = o => (o.row.slug && DOC?.pulls?.[o.row.slug.toLowerCase()]) || [];
// sd:5109: the Deep review's settings as the document read them (management_screen's `deep` source).
// sd:5784: they are global rows on the Settings page; Details only shows them.
const DEEP_FIELDS = [['model', 'Model'], ['effort', 'Effort'], ['bound_minutes', 'Bound in minutes'], ['agents', 'Sub-agents at most']];
const deepOf = k => DOC?.deep?.[`adversarial.deep.${k}`]?.value ?? '?';
const deepLine = () => (DOC?.deep ? html`<p class="why">Deep review settings: ${DEEP_FIELDS.map(([k, label], i) => html`${i ? ' · ' : ''}${label.toLowerCase()} <b>${deepOf(k)}</b>${DOC.deep[`adversarial.deep.${k}`].from === 'default' ? ' (default)' : ''}`)}. <a href="/settings">Settings</a> changes them for every repository.</p>`
  : html`<p class="why">Deep review settings not read: ${why('deep') || 'the reading carries none'}.</p>`);
const capWhy = (o, action, fallback) => { const c = o.caps?.[action]; return !c ? fallback : c.allowed || c.reason || fallback; };
function registerCommands() {
  const C = shell.commands;
  C.register(
    // Repo rows: the two sd-db verbs, each an Undo-able flip of one column. build: they post /api/repos/<verb>.
    { id: 'repo.runner-merge', on: 'repo', label: 'Switch runner-merge', does: 'Switches automatic merging on or off; Undo switches it back.', key: 'm', icon: 'git-merge', risk: 'undo', bulk: true, primary: o => o.row.managed === 'yes', when: o => o.row.registered || 'register this repo first (sd-db repo add)',
      cli: o => `${SDDB} repo runner-merge ${shq(o.path)} ${o.row.merge === 'auto' ? 'manual' : 'auto'}`, executes: true,
      run: o => flipRepo('runner-merge', o), undo: undoOf },
    { id: 'repo.managed', on: 'repo', label: 'Switch managed', does: 'Switches whether the workflow manages this repo; Undo switches it back.', key: 'g', risk: 'undo', bulk: true, primary: o => o.row.managed !== 'yes', when: o => o.row.registered || 'register this repo first (sd-db repo add)',
      cli: o => o.row.managed === 'yes' ? `${SDDB} repo managed ${shq(o.path)} no` : `cd ${shq(o.path)} && sd fleet standard`, executes: true,
      run: o => o.row.managed === 'yes' ? flipRepo('managed', o) : sheet(() => standardRepo(o, true)), undo: undoOf },
    // sd:5610: the managed-repo standard, previewed by `sd fleet standard` and applied with the digest the sheet showed.
    { id: 'repo.standard', on: 'repo', label: 'Standard setup', does: 'Previews the standard setup for this repo; applies it when you confirm.', icon: 'shield-check', risk: 'confirm', executes: true,
      when: registeredWhy, cli: o => `cd ${shq(o.path)} && sd fleet standard --json`,
      // previews: run() reads the plan and asks with it, so the shell's own confirm does not ask first.
      previews: true, run: o => sheet(() => standardRepo(o, o.row.managed !== 'yes')) },
    // sd:3075: Move lane needs a host, so it opens the repo's settings page, where the lane_host field proposes the move.
    { id: 'repo.lane-host', on: 'repo', label: 'Move lane', does: 'Opens the repo\'s settings, where you pick its merge machine.', key: 'h', risk: 'safe', executes: false, when: o => o.row.registered || 'register this repo first (sd-db repo add)',
      cli: o => `${SDDB} repo lane-host ${shq(o.path)} hub`,
      run: o => { location.href = `?repo=${encodeURIComponent(o.path)}#view=repos`; return `Move lane: ${o.label}'s settings page opens`; } },
    // sd:5888: Pull runs `git pull --ff-only` through /api/actions/run (run_actions.repo_pull). when() carries v1's refusals; the
    // server checks again under the lane's runner lock, just before the pull, and refuses a pinned checkout, a linked
    // worktree, another branch, a dirty tree or a lane that runs. Details shows git's output; no verb undoes a pull.
    { id: 'repo.pull', on: 'repo', label: 'Pull', does: 'Pulls the newest code into this checkout\'s main branch.', key: 'l', icon: 'download', risk: 'confirm', executes: true, primary: o => o.row.git?.state === 'behind',
      when: o => o.row.registered ? pullWhy(o) : 'this checkout is not registered; Pull works only in a registered one',
      consequence: o => `Pulls the newest code into ${o.path}. It moves the main branch only, and refuses when there are local changes, another branch is out, or a merge runs there.`,
      cli: o => `git -C ${shq(o.row.git ? o.row.git.path : o.path)} pull --ff-only`, sends: o => `POST /api/actions/run {action: repo.pull, path: ${o.path}}`,
      run: o => window.commands.runAction(o, 'repo.pull', { path: o.path }).then(t => { reread(); return t; }) },
    // sd:5064: a codex review of the repository, in the background; the backend reviews only a registered checkout.
    window.commands.adversarialReview('repo', { payload: o => ({ kind: 'repo', target: o.path }), when: registeredWhy }),
    // sd:5100: one open pull request, picked from the shadow rows the document carries (`pulls`), reviewed by codex
    // as a change at its head. The server checks the number against the same rows and fetches nothing.
    window.commands.adversarialReview('repo', { id: 'repo.adversarial-pr', label: 'Adversarial review: PR',
      does: 'Starts a background review of one open pull request; you pick fixes from its report.',
      payload: (o, v) => ({ kind: 'change', target: o.path, pr: Number(v.pr || pullsOf(o)[0]?.number) }),
      fields: o => [{ name: 'pr', label: 'Pull request', options: pullsOf(o).map(p => ({ value: String(p.number), label: `#${p.number} ${p.title}` })) }],
      when: o => { const r = registeredWhy(o); if (r !== true) return r;
        if (!DOC?.pulls) return `pull requests not read: ${why('pulls') || 'the reading carries none'}`;
        if (!o.row.slug) return 'not a GitHub repository, so it has no pull requests';
        return pullsOf(o).length > 0 || 'no open pull request as of the last GitHub sync'; } }),
    // sd:5100: headless claude reads the whole checkout with read-only sub-agents; one runs per repository.
    window.commands.adversarialReview('repo', { id: 'repo.deep-review', label: 'Deep review',
      does: 'Starts a deep read-only review of the repo; you pick fixes from its report.',
      payload: o => ({ kind: 'deep', target: o.path }), when: registeredWhy,
      consequence: o => `Has Claude (${deepOf('model')}, ${deepOf('effort')} effort) read ${o.label} with up to ${deepOf('agents')} read-only helpers, for up to ${deepOf('bound_minutes')} minutes. It changes nothing in the checkout. Its report lists each fix as a question to approve or reject.` }),
    // Land (sd:5104): the preview first, in the confirm, then the write with the preview's revision (land.py). A changed
    // Items field previews again, so the confirm always shows what lands. build: it posts /api/repos/land.
    { id: 'repo.land', on: 'repo', label: 'Land', does: 'Commits the work once per item and queues it; asks first with a preview.', key: 'n', icon: 'rocket', risk: 'confirm', executes: true,
      primary: o => !!(o.row.git && (o.row.git.dirty || o.row.git.ahead)), when: landWhy,
      cli: o => `sd-ship -C ${shq(o.path)} lane enqueue --item <n> --title <title> --body-file <body> --expected-head <head> --deliver`,
      // previews: run() reads the preview and asks with it, so the shell's own confirm does not ask first.
      previews: true, run: o => landRepo(o) },
    { id: 'sddb.run', on: 'sd-db change', label: 'Run', does: 'Saves this setting change; Undo puts the old value back.', key: 'u', icon: 'play', risk: 'undo', primary: () => true, cli: o => o.cmd, executes: true,
      run: o => { dropProposal(o);
        // sd:3465: a channels write answers the render remedy, and its Undo writes the setting back (`back`) over the row the
        // write answered (sd:4632): the library keeps an inbound key the form never posts, so that row is not the posted value.
        const undo = o.verb !== 'channels' ? () => setRepo(o.verb, o.repo, o.before, o.value) : o.back !== undefined ? v => setRepo(o.verb, o.repo, o.back, v?.value) : null;
        return landing(setRepo(o.verb, o.repo, o.value, o.before), v => `Ran locally · ${o.title}${v?.remedy ? ` · ${v.remedy}` : ''}`, undo); },
      undo: undoOf },
    // sd:5887: Write does what the sd-db repo declare line does, through /api/actions/run (run_actions.declaration_write): the
    // server refuses a row that changed since this reading, checks the value with the pack's own validator, and writes the
    // row. Undo writes the reading back, or removes a declaration the row did not have.
    // sd:5958: `before` is `base`, the declaration restateDeclaration drafted `after` on, never another reading; a refused
    // write keeps its proposal, and only a landed one leaves the list.
    { id: 'declaration.write', on: 'declaration change', label: 'Write', does: 'Writes this change to the repo row; Undo writes the old declaration back.', key: 'p', icon: 'play', risk: 'undo', primary: () => true, executes: true,
      when: o => o.why || true, cli: o => o.cmd, sends: o => `POST /api/actions/run {action: declaration.write, path: ${o.repo.path}, kind: review}`,
      run: o => { const { after, base: before } = o;
        if (!after) return Promise.reject(new Error(o.why || 'no declaration is drafted for this proposal'));
        return landing(declare(o.repo, JSON.parse(after), before).then(v => { dropProposal(o); return v; }), () => `Written · ${o.title}`,
          () => declare(o.repo, before.trim() ? JSON.parse(before) : null, after)); },
      undo: undoOf },
    ...Object.values(ROUTE).map(([type]) => ({ id: `${type.split(' ')[0].toLowerCase().replace('-', '')}.withdraw`, on: type, label: 'Withdraw', does: 'Drops this proposal from the list; nothing was run.', risk: 'safe', executes: false,
      run: o => { withdraw(o.key, true); return `Proposal withdrawn · ${o.title}`; } })),
    { id: 'wt.prune', on: 'worktrees', label: 'Prune', does: 'Removes records of worktrees whose folders are gone; this cannot be undone.', key: 'p', risk: 'confirm', primary: () => true, when: () => 'no dashboard route: run the CLI line', cli: () => 'sd sessions prune --abandoned',
      consequence: o => `Removes ${o.n} worktree registrations whose directories are gone. Branches and files stay.` },
    // Services on this Mac. build: each posts /api/services/<label>/<action> with the service's revision; when() is its capability.
    { id: 'svc.restart', on: 'service', label: 'Restart', does: 'Stops and starts the service; what it serves drops for seconds.', key: 't', icon: 'rotate-ccw', risk: 'confirm', primary: () => true, when: o => capWhy(o, 'restart', 'not read'), cli: o => `sd services restart ${o.name}`,
      consequence: o => `Stops and starts ${o.name}. Anything it serves drops for a few seconds.`, run: o => landing(service(o, 'restart'), () => `Restart requested · ${o.label}`) },
    { id: 'svc.stop', on: 'service', label: 'Stop', does: 'Stops the service; Start brings it back.', key: 's', risk: 'confirm', when: o => capWhy(o, 'stop', o.running || 'not running'), cli: o => `sd services stop ${o.name}`,
      consequence: o => `Stops ${o.name} through launchd. Start brings it back.`, run: o => landing(service(o, 'stop'), () => `Stop requested · ${o.label}`) },
    { id: 'svc.start', on: 'service', label: 'Start', does: 'Starts the service.', key: 'a', risk: 'safe', when: o => capWhy(o, 'start', !o.running || 'already running'), cli: o => `sd services start ${o.name}`,
      run: o => landing(service(o, 'start'), () => `Start requested · ${o.label}`) },
    // sd:6697: Refresh <what>, a registered command whose tip names the source and what it writes.
    { id: 'repos.refresh', on: 'git state', label: 'Refresh git state', does: 'Checks every repo\'s branch and changes again; writes nothing.', icon: 'rotate-ccw', risk: 'safe', primary: () => true,
      cli: () => 'no CLI: the next fleet reading reads git', sends: () => 'POST /api/repos/refresh {}', executes: true, run: () => refreshRepos() },
    // sd:5108: one request row in the hub database; each satellite's next repo-sync follow run (every 5 min) answers it.
    { id: 'sats.refresh', on: 'satellite refresh', label: 'Refresh satellites', does: 'Asks each other machine to update to the hub\'s versions.', icon: 'rotate-ccw', risk: 'confirm', primary: () => true,
      cli: () => 'no CLI: the dashboard writes the refresh.request row', executes: true,
      consequence: () => 'Each other machine checks for this request every five minutes. It waits for its merges to finish, updates to the hub\'s versions, and reports here.',
      run: () => landing(refreshSatellites(), v => `Refresh requested · ${v.id}`) },
    // sd:5405: local-shared-upload's signer, run by the server; the link is one setting row and the toast never holds it.
    { id: 'upload.regenerate', on: 'upload link', label: 'Regenerate', does: 'Signs a new 7-day upload link under inbox/ and shows it here.', icon: 'link-2', risk: 'confirm',
      primary: () => true, cli: () => 'no CLI: the dashboard signs the link with local-shared-upload and keeps it as the dashboard.shared_upload_link row', executes: true,
      sends: () => 'aws configure export-credentials --profile <SHARED_UPLOAD_PROFILE>',
      consequence: () => `Signs a new link for uploads under ${UPLOAD?.prefix || 'inbox/'} that works for 7 days and shows it here in place of the current one. Links already handed out keep working until they expire.`,
      run: () => landing(regenerateUpload(), v => `Upload link signed · expires ${v.expiry.slice(0, 16).replace('T', ' ')} UTC`) },
    // launchd calendar jobs. Retry (commands.js) posts /api/jobs/<name>/retry; operations.inventory's capability is the off
    // reason. Exit 127 is "command not found": Print is the row's act (sd:3392).
    window.commands.retry({ job: o => ({ name: o.job, service: o.service, failed: o.failed, exit: o.exit, running: o.running, capability: o.caps.retry }),
      run: o => landing(job(o, 'retry'), out => window.commands.answered(out, o.job, 'retried', `Run started · ${o.job}`)) }),
    // build (sd:3388): Cancel is `sd jobs cancel`, which sends SIGTERM to the running job through launchd. It posts
    // /api/jobs/<name>/cancel with the row's revision; the confirm names the job, since the run stops part way.
    { id: 'jobs.cancel', on: 'job', label: 'Cancel', does: 'Stops the running job now; the stopped run cannot resume.', key: 'x', icon: 'ban', risk: 'confirm', primary: o => o.running,
      when: o => capWhy(o, 'cancel', o.running || 'not running'), cli: o => `sd jobs cancel ${o.job}`, sends: o => `launchctl kill SIGTERM ${o.service}`,
      consequence: o => `Stops ${o.job}${o.pid ? ` (PID ${o.pid})` : ''} now. The run stops part way through; its next scheduled run still starts.`,
      run: cancelJob },
    window.commands.showLog({ job: o => ({ name: o.job }), run: o => { selectRow(o.id, true); return `${o.job}'s log is in Details`; } }),
    window.commands.printJob({ job: o => ({ name: o.job, service: o.service, exit: o.exit }) }),
    // sd:5126: End session sends SIGTERM to a Claude session on this Mac, so a full builder cap can be cleared. A subagent
    // runs inside its session's process, so a stalled agent ends with its session; the confirm names each agent that does.
    { id: 'cs.end', on: 'claude session', label: 'End session', does: 'Ends the Claude session; its current turn and all its helpers stop.', key: 'e', icon: 'square', risk: 'confirm',
      primary: o => o.row.agents.some(a => a.state === 'stalled'), when: o => o.pid != null || 'claude agents gave no pid, so there is nothing to signal',
      cli: o => `kill -TERM ${o.pid}`, executes: true, sends: o => `POST /api/claude-sessions/end {"id":"${o.sid}","pid":${o.pid}}`,
      consequence: endWhat, run: o => landing(endSession(o), v => `${v.ended === 'already gone' ? 'Already gone' : 'End requested'} · ${o.label}`) },
  );
}
// What End session stops, from the row the page read; the server checks the pid again before it signals.
function endWhat(o) {
  const r = o.row, agents = r.agents.map(a => `${a.name} (${a.state})`);
  return `Sends SIGTERM to ${o.label} (PID ${o.pid}). ${agents.length ? `${plural(agents.length, 'agent')} under it stop${agents.length === 1 ? 's' : ''} with it: ${agents.join(', ')}.` : 'No agent runs under it.'}`
    + `${r.safe.state === 'yes' ? '' : ` Safe to exit: ${r.safe.state}${r.safe.why.length ? ` · ${r.safe.why.join('; ')}` : ''}.`}`
    + ` Commits and files in its checkouts stay${r.watches.length ? `; ${plural(r.watches.length, 'lane watch', 'lane watches')} it started ${r.watches.length === 1 ? 'is' : 'are'} not signalled` : ''}.`;
}
async function endSession(o) { const got = await window.shell.post('/api/claude-sessions/end', { id: o.sid, pid: o.pid }); await reread(); return got; }
async function refreshSatellites() { const got = await window.shell.post('/api/satellites/refresh', {}); await reread(); return got; }
async function regenerateUpload() { const got = await window.shell.post('/api/shared-upload/regenerate', {}); await reread(); return got; }
// Land (sd:5104): no git state, an unread tree or nothing new is the off reason; the server reads the tree again anyway.
const landWhy = o => { const g = o.row.git;
  if (!o.row.registered) return 'register this repo first (sd-db repo add)';
  if (!g) return 'git state was not read, so nothing says what would land';
  if (g.error || g.dirty == null) return "the tree's state was not fully read";
  return g.dirty || g.ahead ? true : 'nothing to land: no uncommitted change and no unpushed commit'; };
const landText = p => [
  p.mode === 'resume' ? `Resumes ${p.land_branch}: an earlier Land stopped part way.` : p.mode === 'branch'
    ? `On ${p.branch || 'a detached HEAD'}, so Land makes ${p.land_branch} in ${p.worktree}, restores this checkout and never pushes ${p.default}.`
    : `Commits on ${p.branch}.`,
  ...(p.groups || []).filter(g => g.paths.length).map(g => `sd:${g.item} ${g.title}: ${plural(g.paths.length, 'file')} (${g.paths.slice(0, 4).join(', ')}${g.paths.length > 4 ? ', …' : ''}).`),
  p.commits.length ? `Carries ${plural(p.commits.length, 'unpushed commit')}: ${p.commits.map(c => c.subject).slice(0, 3).join('; ')}.` : '',
  p.why || 'Then the branch goes into the merge queue.',
].filter(Boolean).join(' ');
// Ask with the preview until the Items field matches what was previewed, then post. A 4xx landed nothing; a 502 may follow
// a step that landed, so it says what did and reads the page again.
async function landRepo(o, items) {
  const q = `/api/repos/land/preview?path=${encodeURIComponent(o.path)}${items === undefined ? '' : `&items=${encodeURIComponent(items)}`}`;
  const p = await window.shell.getJSON(q);
  const { yes, values } = await window.shell.ask({ title: `Land ${o.label}?`, body: landText(p), ok: 'Land', keep: "Don't land", danger: true,
    cli: () => p.cli, fields: [
      { name: 'items', label: 'Items, one commit each', required: true, value: p.spec, placeholder: '5104', help: '5104 takes every change; 5104: lib/ docs/; 5105 splits by path prefix.' },
      { name: 'claim', label: 'The merge', options: [{ value: 'deliver', label: 'Closes the items (--deliver)' }, { value: 'associate-only', label: 'An earlier pull request of the items (--associate-only)' }] }] });
  if (!yes) return { text: `Land not run · ${o.label}` };
  if (values.items !== p.spec || !p.ready) return values.items !== p.spec ? landRepo(o, values.items) : { text: `Not landed · ${o.label}: ${p.why}` };
  let out;
  try { out = await window.shell.post('/api/repos/land', { path: o.path, items: p.spec, revision: p.revision, claim: values.claim }); }
  catch (err) {
    if (err.status >= 400 && err.status < 500) throw err;
    load();
    return { text: `Not known how far Land got · ${o.label}: ${err.message}. Read the checkout before you land again.` };
  }
  await reread();
  const lane = out.lane || {};
  const queued = lane.status === 'not queued' ? `not queued: ${lane.error}. Land again to retry the enqueue` : `lane ${lane.status}; Queue shows it`;
  return { text: `Landed · ${o.label} · ${plural(out.commits.length, 'commit')} on ${out.branch} · ${queued}` };
}
// Standard setup (sd:5610): the sheet lists the preview's steps by group; Apply posts the preview's digest, so only what
// the operator saw runs (standard.py). Turning managed on asks Manage only, the one column as before, or the standard, which
// writes managed last. Lane history (`used_before`) picks the first option: a repo the lane used was likely managed before.
// A 4xx ran nothing; any other failure may follow a write, so the page reads again and says Check again.
let sheets = Promise.resolve();
const sheet = f => (sheets = sheets.then(f, f));
const STD_GROUPS = [['row', 'Repo settings'], ['github', 'GitHub'], ['checkout', 'checkout']];
const stdValue = v => v == null ? 'none' : typeof v === 'object' ? JSON.stringify(v) : String(v);
const stdState = s => s.result || (s.state === 'change' ? `${stdValue(s.before)} → ${stdValue(s.after)}` : s.state);
const stdBody = (p, head) => html`${head ? html`${head}<br>` : ''}${STD_GROUPS.map(([g, label]) => {
  const steps = p.steps.filter(s => s.group === g);
  return steps.length ? html`<b>${label}</b><br>${steps.map(s => html`${s.key} ${s.subject}: ${stdState(s)}${s.detail ? html` <span class="no">· ${s.detail}</span>` : ''}<br>`)}` : ''; })}`;
const stdCount = (steps, of) => Object.entries(steps.reduce((n, s) => ({ ...n, [of(s)]: (n[of(s)] || 0) + 1 }), {})).map(([k, n]) => `${n} ${k}`).join(', ');
async function standardRepo(o, manage) {
  const p = await window.shell.getJSON(`/api/repos/standard/preview?path=${encodeURIComponent(o.path)}`);
  const changes = p.steps.filter(s => s.state === 'change').length;
  if (!manage && !changes) return { text: `Standard holds · ${o.label}: ${stdCount(p.steps, s => s.state)}` };
  const only = { value: 'only', label: 'Manage only: writes managed=yes, nothing else' };
  const apply = { value: 'apply', label: `Manage and apply standard: ${plural(changes, 'change')}, managed last` };
  const fields = manage ? [{ name: 'run', label: 'What runs', options: p.used_before ? [only, apply] : [apply, only],
    help: p.used_before ? 'This repo has lane history, so Manage only comes first.' : 'First manage: the standard comes first.' }] : [];
  const { yes, values } = await window.shell.ask({ title: manage ? `Manage ${o.label}?` : `Apply standard to ${o.label}?`, body: stdBody(p),
    ok: manage ? 'Manage' : 'Apply standard', keep: 'Keep it', danger: true, cli: `cd ${shq(o.path)} && sd fleet standard --apply --digest ${p.digest}`, fields });
  if (!yes) return { text: `Nothing ran · ${o.label}` };
  if (manage && values.run === 'only') {
    await setRepo('managed', o.row, 'yes', 'no');
    return { text: `managed yes · ${o.label}`, undo: () => setRepo('managed', REPOS.find(x => x.path === o.path) || o.row, 'no', 'yes') };
  }
  let out;
  try { out = await window.shell.post('/api/repos/standard', { path: o.path, digest: p.digest }); }
  catch (err) {
    if (err.status >= 400 && err.status < 500) return { text: `Nothing ran · ${o.label}: ${err.message}` };
    load();
    return { text: `Not known how far the standard got · ${o.label}: ${err.message}. Check again before you apply.` };
  }
  await reread();
  const ran = out.steps.filter(s => s.result), bad = ran.filter(s => s.result !== 'changed');
  const again = await window.shell.ask({ title: `${bad.length ? 'Standard partly applied' : 'Standard applied'} · ${o.label}`, ok: 'Check again', keep: 'Close', danger: false,
    body: stdBody(out, 'Health shows it after the next GitHub sync. The nightly setup run installs the leak guard on a public repo.') });
  if (again.yes) return standardRepo(o, false);
  return { text: `${bad.length ? 'Standard partly applied' : 'Standard applied'} · ${o.label}: ${stdCount(ran, s => s.result.split(':')[0])}` };
}
async function service(o, action) { await window.shell.post(`/api/services/${encodeURIComponent(o.name)}/${action}`, { revision: o.revision }); await reread(); }
// A cancel's outcome (sd:3388), as Queue words a lane write (queue_screen.py): a 4xx wrote nothing and says why; any other
// failure, or no answer, may follow a signal that was sent, so the jobs are read again and the toast says the outcome is not
// known. A 200 carries launchctl's own answer: accepted, failed (nothing sent) or unknown.
async function cancelJob(o) {
  let out;
  try { out = await window.shell.post(`/api/jobs/${encodeURIComponent(o.job)}/cancel`, { revision: o.revision }); }
  catch (err) {
    if (err.status >= 400 && err.status < 500) { if (err.stale) load(); throw err; }
    load();
    return { text: `Not known whether ${o.job} was cancelled: ${err.message}. The jobs were read again; check ${o.job} before you cancel it again.` };
  }
  await reread();
  return { text: window.commands.answered(out, o.job, 'cancelled', `Cancel sent · ${o.job}`) };
}
async function job(o, action) { const out = await window.shell.post(`/api/jobs/${encodeURIComponent(o.job)}/${action}`, { revision: o.revision }); await reread(); return out; }
document.addEventListener('shell:open', e => {
  const id = e.detail;
  if (id.startsWith('repo:') && repoParam) shell.openPane('tab-details');
  else if (!id.startsWith('prop:')) selectRow(id, true);
});
document.addEventListener('shell:picked', e => document.querySelectorAll('.ledger tbody tr[data-id]').forEach(tr => tr.toggleAttribute('data-picked', e.detail.includes(tr.dataset.id))));

// ---------- Sessions and agents ----------
// build (sd:5106): each Claude session on this Mac (claude_sessions.py), the ones waiting on you first. Safe to exit is the
// server's: yes only when nothing runs under it and no checkout it answers for holds unpushed work; a fact not read is ▨.
const CSTATE = r => r.status === 'waiting' ? 'caution' : r.status === 'unknown' ? 'unknown' : r.status === 'busy' ? 'queued' : 'ok';
const SAFE = { yes: ['ok', 'yes'], no: ['queued', 'no'], unknown: ['unknown', 'unknown'] };
const taskText = r => r.tasks.length ? `#${r.tasks[0].item}${r.tasks[0].title ? ` ${r.tasks[0].title}` : ''}${r.tasks.length > 1 ? ` +${r.tasks.length - 1}` : ''}` : '—';
function claudeRows() {
  const { free } = tokens();
  return (CLAUDE?.sessions || []).filter(r => free.every(f => [r.name, r.cwd, r.id, ...r.tasks.map(t => `#${t.item} ${t.title}`)].join(' ').toLowerCase().includes(f)));
}
function renderClaude() {
  if (!CLAUDE) return unknown('Claude sessions were not read', why('claude'));
  const rows = claudeRows();
  if (!rows.length) return html`<p class="empty">${CLAUDE.sessions.length ? `No Claude session matches “${text.q}”.` : 'No Claude session runs on this Mac.'}</p>`;
  return html`<table class="ledger" aria-labelledby="claude-h" data-tfilter="page" data-toolbar="toolbar"><thead><tr><th scope="col" class="g"><span class="sr">State</span></th><th scope="col">Session</th><th scope="col">Task</th><th scope="col">Last action</th><th scope="col">Waits on you</th><th scope="col">Safe to exit</th>${ACTH}</tr></thead>
    <tbody>${rows.map(r => { const st = CSTATE(r), [sg, sw] = SAFE[r.safe.state]; return html`<tr data-id="cs:${r.id}"><td class="g g-${st}">${GLYPH[st]}<span class="sr">${r.status}</span></td>
      <td class="what"><button type="button">${r.name || r.id.slice(0, 8)}</button></td><td class="d mono" data-k="task">${taskText(r)}</td>
      <td class="d mono"><time class="rel" datetime="${r.last_action}" data-empty="not read"></time></td>
      <td class="d">${r.waiting ? html`<span class="g-caution" aria-hidden="true">▲</span> ${r.waiting_for || 'yes'}` : r.waiting === null ? html`<span class="g-unknown" aria-hidden="true">▨</span> not read` : 'no'}</td>
      <td class="d"><span class="g-${sg}" aria-hidden="true">${GLYPH[sg]}</span> ${sw}${r.safe.why.length ? html` · ${r.safe.why[0]}` : ''}</td>${ACT('cs:' + r.id)}</tr>`; })}</tbody></table>`;
}
function claudeDetails(r, act) {
  const st = CSTATE(r), [sg, sw] = SAFE[r.safe.state];
  const list = (xs, f, none) => xs.length ? html`<ul>${xs.map(x => html`<li>${f(x)}</li>`)}</ul>` : html`<p class="why">${none}</p>`;
  return html`<p class="kind"><span class="g-${st}" aria-hidden="true">${GLYPH[st]}</span> Claude session · ${r.status}</p><h2>${r.name || r.id}</h2>
    <dl><dt>Session</dt><dd class="mono">${r.id}</dd><dt>Directory</dt><dd class="mono">${r.cwd || '—'}</dd><dt>PID</dt><dd class="mono">${r.pid ?? '—'}</dd>
    <dt>Started</dt><dd><time class="rel" datetime="${r.started}" data-long data-empty="not read"></time></dd>
    <dt>Last action</dt><dd><time class="rel" datetime="${r.last_action}" data-long data-empty="not read"></time></dd>
    <dt>Waits on you</dt><dd>${r.waiting ? r.waiting_for || 'yes' : r.waiting === null ? 'not read' : 'no'}</dd>
    <dt>Safe to exit</dt><dd><span class="g-${sg}" aria-hidden="true">${GLYPH[sg]}</span> ${sw}</dd></dl>
    ${r.safe.why.length ? html`<h3>Why not</h3>${list(r.safe.why, w => w)}` : ''}
    <h3>Tasks</h3>${list(r.tasks, t => html`<span class="mono">#${t.item}</span> ${t.title || '(not read)'} · via ${t.via}`, 'No item is tied to this session.')}
    <h3>Agents</h3>${list(r.agents, a => html`${a.name} · ${a.state}${a.items.length ? html` · <span class="mono">${a.items.map(n => `#${n}`).join(' ')}</span>` : ''}`, 'No agent runs or stalled under it.')}
    <h3>Checkouts</h3>${list(r.checkouts, c => html`<span class="mono">${c.path}</span> · ${c.error ? html`<span class="g-unknown" aria-hidden="true">▨</span> ${c.error}` : `${c.branch} · ${plural(c.dirty, 'uncommitted file')} · ${plural(c.unpushed, 'unpushed commit')}`}`, 'No git checkout.')}
    <p class="why">From Claude's own session list, the session's transcripts, its processes and git. Sessions on other machines are not listed.</p>${act}`;
}
function renderSessions() {
  const el = document.getElementById('view-sessions');
  put(el, html`
    <div class="sec-head"><h2 id="claude-h">Claude sessions <button class="help" type="button" aria-label="Help: Claude sessions" data-help="<b>This Mac's Claude sessions.</b> Safe to exit means idle, no helper or merge watch running under it, and no unpushed work in its checkouts. A stalled or unread agent counts as running until it gives a final answer.">${I('circle-help')}</button></h2><p class="tally">${CLAUDE ? html`<span>${plural(CLAUDE.sessions.length, 'session')}</span><span>${CLAUDE.builders.running == null ? html`<span class="g-unknown" aria-hidden="true">▨</span> builders not counted` : plural(CLAUDE.builders.running, 'builder') + ' running'}</span><span>claude agents · ${hhmm(CLAUDE.read)} UTC</span>` : ''}</p></div>
    ${renderClaude()}

    <div class="sec-head"><h2 id="agents-h">Workflow commands running <button class="help" type="button" aria-label="Help: Workflow commands running" data-help="<b>Workflow commands running on this Mac now.</b> Claude sessions are listed above; Codex runs are not listed.">${I('circle-help')}</button></h2><p class="tally">${SESS ? html`<span>${SESS.processes_truncated ? '≥ ' : ''}${SESS.processes.length} running</span><span>fleet · ${seen(SESS)}</span>` : ''}</p></div>
    ${!SESS ? unknown('Sessions were not read', why('sessions'), 'fleet sessions')
      : SESS.processes.length ? html`<table class="ledger" aria-labelledby="agents-h"><thead><tr><th scope="col" class="g"><span class="sr">State</span></th><th scope="col">Command</th><th scope="col">Running for</th><th scope="col">PID</th></tr></thead>
      <tbody>${SESS.processes.map((p, i) => html`<tr data-id="proc:${i}"><td class="g g-ok">●<span class="sr">ok</span></td><td class="what"><button type="button">${p.command}</button></td><td class="d mono" data-k="up">${p.elapsed}</td><td class="d num">${p.pid}</td></tr>`)}</tbody></table>${SESS.processes_truncated ? html`<p class="why">The table was cut at ${SESS.processes_ceiling}, so these are a floor.</p>` : ''}`
      : html`<p class="empty">${SESS.processes_error ? html`<span class="g-unknown" aria-hidden="true">▨</span> ${SESS.processes_error}`
        : SESS.processes_truncated ? html`<span class="g-unknown" aria-hidden="true">▨</span> The table was cut at ${SESS.processes_ceiling}, so no command below the cut was read.` : 'No sd-* command is running.'}</p>`}

    <div class="sec-head"><h2 id="wt-h">Worktrees</h2><p class="tally">fleet sessions · ${SESS ? seen(SESS) : `${hhmm(READ)} UTC`}</p></div>
    ${SESS && !SESS.root_exists ? html`<p class="empty"><span class="g-unknown" aria-hidden="true">▨</span> The checkout root ${SESS.root} does not exist. REPO_ROOT names it; ~/repos is the default.</p>`
      : SESS ? html`<table class="ledger" aria-labelledby="wt-h"><tbody>
      <tr data-id="wt:abandoned"><td class="g g-${SESS.abandoned ? 'caution' : 'ok'}">${SESS.abandoned ? '▲' : '●'}<span class="sr">${SESS.abandoned ? 'caution' : 'ok'}</span></td><td class="what"><button type="button">${SESS.abandoned} abandoned worktrees</button></td><td class="d mono">registered, directory gone</td>${ACT('wt:abandoned')}</tr>
      <tr data-id="wt:live"><td class="g g-ok">●<span class="sr">ok</span></td><td class="what"><button type="button">${liveTrees()} live worktrees</button></td><td class="d mono">of ${SESS.registered} registered${SESS.unreadable ? ` · ${SESS.unreadable} unreadable` : ''}</td><td class="act end"></td></tr>
    </tbody></table>${treeTable()}` : unknown('Worktrees were not read', why('sessions'))}`);
}
// pack sd:7517: an unreadable registration is neither abandoned nor live.
const liveTrees = () => SESS.registered - SESS.abandoned - (SESS.unreadable || 0);
// pack sd:7517: each registered worktree, abandoned first, as Operations > Sessions listed them before it was retired.
const TREESTATE = { abandoned: 'caution', unknown: 'unknown', live: 'ok' };
const treeTable = () => !(SESS.worktrees || []).length ? '' : html`<div class="sec-head"><h2 id="wts-h">Each worktree</h2><p class="tally"><span>${plural(SESS.worktrees.length, 'worktree')}</span><span>abandoned first</span></p></div>
    <table class="ledger" aria-labelledby="wts-h"><thead><tr><th scope="col" class="g"><span class="sr">State</span></th><th scope="col">Worktree</th><th scope="col">Repository</th><th scope="col">Branch</th><th scope="col">Path</th></tr></thead>
    <tbody>${SESS.worktrees.map((t, i) => { const st = TREESTATE[t.state] || 'unknown'; return html`<tr data-id="tree:${i}"><td class="g g-${st}">${GLYPH[st]}<span class="sr">${t.state}</span></td><td class="what"><button type="button">${t.name}</button></td><td class="d mono">${t.repo}</td><td class="d mono">${t.branch || '—'}</td><td class="d mono">${t.path || '(unreadable registration)'}</td></tr>`; })}</tbody></table>`;
document.getElementById('view-sessions').addEventListener('click', e => {
  if (e.target.closest('.rowact')) return;
  const tr = e.target.closest('tr[data-id]'); if (tr) selectRow(tr.dataset.id, true);
});

// ---------- Deploys ----------
const SVCSTATE = s => s.state === 'running' ? 'ok' : s.state === 'failed' ? 'warning' : ['unknown'].includes(s.state) ? 'unknown' : 'queued';
const DS = { sort: 'name', dir: 1, page: 1, size: 25 };
// sd:5108: a satellite's result for the last request; one that failed is a warning, one not yet answered waits.
const SATSTATE = x => x.state === 'done' ? (x.result?.exit === 0 ? 'ok' : 'warning') : ['pending', 'running'].includes(x.state) ? 'queued' : 'unknown';
const svcWhat = s => `${s.category} · ${s.scope}`;
const SVCSORT = { name: s => s.name, what: svcWhat, state: s => s.state };
// build (sd:3388): free text matches every column v1's filter read (name, label, scope, domain, category, state); state:<word>
// is a chip, and the state buttons write it.
function serviceRows() {
  const { chips, free } = tokens();
  const key = SVCSORT[DS.sort] || SVCSORT.name;
  return SERVICES.filter(s => (!chips.state || String(s.state).toLowerCase() === chips.state)
    && free.every(f => [s.name, s.label, s.scope, s.domain, s.category, s.state].join(' ').toLowerCase().includes(f)))
    .sort((a, b) => String(key(a)).localeCompare(String(key(b))) * DS.dir || a.label.localeCompare(b.label));
}
// sd:5405: the shared upload link's state, its glyph and its words.
const UPSTATE = { current: 'ok', expired: 'caution', other: 'caution', none: 'queued', unread: 'unknown' };
const upWords = u => ({ current: 'expires', expired: 'expired', none: 'none signed yet', other: 'signed for another bucket', unread: 'not read' })[u.link_state] || u.link_state;
function uploadSection() {
  const u = UPLOAD, st = u ? UPSTATE[u.link_state] || 'unknown' : 'unknown', l = u?.link;
  return html`<div class="sec-head"><h2 id="up-h">Shared upload <button class="help" type="button" aria-label="Help: Shared upload" data-help="<b>One upload link.</b> Anyone holding it can upload files under inbox/ until it expires, each up to the size cap; it cannot read, list or delete. Every uploaded file is public, and so is the bucket's file list. Regenerate signs a new link; links already handed out keep working until they expire.">${I('circle-help')}</button></h2><p class="tally">${u?.set_up ? html`<span><a href="${u.console}" target="_blank" rel="noopener noreferrer">${u.bucket} in the S3 console</a></span><span>${u.region}</span>` : ''}</p></div>
    ${!u ? unknown('Shared upload was not read', why('upload'), 'local-shared-upload') : !u.set_up ? html`<p class="empty">Not set up. ${u.remedy}</p>`
      : html`<table class="ledger" aria-labelledby="up-h"><tbody><tr data-id="upload:link"><td class="g g-${st}">${GLYPH[st]}<span class="sr">${u.link_state}</span></td><td class="what"><button type="button">${u.prefix} upload link</button></td><td class="d mono">${upWords(u)}</td><td class="d mono">${l ? html`<time class="rel" datetime="${l.expiry}" data-future></time>` : u.link_reason || '—'}</td>${ACT('upload:link')}</tr></tbody></table>
      ${u.link_state === 'current' ? html`<p class="why">Expires <time class="rel" datetime="${l.expiry}" data-future data-long></time>. Links handed out before keep working until they expire.</p>
      <div class="cli fold"><button class="cli-line" type="button" aria-expanded="false" title="Show the full command"><code>${l.curl}</code></button><button class="icon-btn" type="button" data-copy="${l.curl}" aria-label="Copy the curl line">${I('copy')}</button></div>
      <p class="why">The uploader replaces /path/to/file. Each file is public at <code>${l.public}</code>.</p>${l.temporary ? html`<p class="why">Signed with temporary keys: the link stops when they expire (${l.temporary}), even before its own expiry.</p>` : ''}`
        : html`<p class="why">Regenerate signs a link for uploads under ${u.prefix} that works for 7 days. Each file is public at <code>${u.public}</code>.</p>`}`}`;
}
function renderDeploys() {
  const el = document.getElementById('view-deploys');
  const { chips } = tokens();
  const rows = DOC.services ? serviceRows() : [], shown = pageOf(rows, DS);
  const states = [...new Set(SERVICES.map(s => String(s.state).toLowerCase()))].sort();
  put(el, html`
    <div class="sec-head"><h2 id="dep-h">Deploys <button class="help" type="button" aria-label="Help: Deploys" data-help="<b>No deploy history is recorded yet,</b> so this list stays empty; that is unknown, not none. The services below are what is installed on this Mac now.">${I('circle-help')}</button></h2></div>
    ${unknown('No deploy history', 'Nothing records a deploy yet, so this list cannot fill. The services below are what runs on this Mac now.')}
    <div class="sec-head"><h2 id="sat-h">Satellites <button class="help" type="button" aria-label="Help: Satellites" data-help="<b>Refresh satellites</b> asks each other machine to update to the hub's versions. Each checks for the request every five minutes, updates and reports here. A machine is listed once it has reported once.">${I('circle-help')}</button></h2><p class="tally">${SATS ? html`<span>${plural(SATS.satellites.length, 'satellite')}</span><span>hub database · ${hhmm(READ)} UTC</span>` : ''}</p></div>
    ${!SATS ? unknown('Satellites were not read', why('satellites'), 'local-repo-sync/satellite_refresh.py') : html`<table class="ledger" aria-labelledby="sat-h"><thead><tr><th scope="col" class="g"><span class="sr">State</span></th><th scope="col">Satellite</th><th scope="col">State</th><th scope="col">When</th><th scope="col" class="num">Exit</th><th scope="col">Last line</th>${ACTH}</tr></thead>
      <tbody><tr data-id="sats:request"><td class="g g-ok">●<span class="sr">ok</span></td><td class="what"><button type="button">Refresh request</button></td><td class="d mono">${SATS.request ? SATS.request.id : 'none sent'}</td><td class="d mono"><time class="rel" datetime="${SATS.request?.at || ''}" data-empty="never"></time></td><td class="d num"></td><td class="d"></td>${ACT('sats:request')}</tr>
      ${SATS.satellites.map(x => { const st = SATSTATE(x), r = x.result || {}; return html`<tr data-id="sat:${x.host}"><td class="g g-${st}">${GLYPH[st]}<span class="sr">${x.state}</span></td><td class="what"><button type="button">${x.host}</button></td><td class="d mono">${x.state}${r.attempt > 1 ? ` · attempt ${r.attempt}` : ''}</td><td class="d mono"><time class="rel" datetime="${r.finished || r.started || ''}" data-empty="never"></time></td><td class="d num" data-k="exit">${r.exit ?? '—'}</td><td class="d mono">${x.state === 'unread' ? x.reason : r.step ? `${r.step}: ${r.line}` : '—'}</td><td class="act end"></td></tr>`; })}</tbody></table>
      ${SATS.satellites.length ? '' : html`<p class="empty">No satellite's repo-sync-follow job has reported a run yet.</p>`}`}
    ${uploadSection()}
    <div class="sec-head"><h2 id="svc-h">Services on this Mac</h2><p class="tally">${hides(rows.length, SERVICES.length, 'service')}<span>launchd · ${hhmm(READ)} UTC</span></p></div>
    ${!DOC.services ? unknown('Services were not read', why('services'), 'sd services list')
      : html`<div class="filters toolbar-2">${filterBar(filterSeg('state', 'State', states, chips.state), text.q)}</div>
    ${rows.length ? html`<table class="ledger" aria-labelledby="svc-h" data-tfilter="page" data-toolbar="toolbar"><thead>${shell.list.sortHead(SVC_COLS, DS)}</thead>
      <tbody>${shown.map(s => { const st = SVCSTATE(s); return html`<tr data-id="svc:${s.label}"><td class="g g-${st}">${GLYPH[st]}<span class="sr">${s.state}</span></td><td class="what"><button type="button">${s.name}</button></td><td class="d mono">${svcWhat(s)}</td><td class="d mono">${s.state}</td><td class="d num" data-k="pid">${s.pid ?? '—'}</td><td class="d num" data-k="exit">${s.last_signal != null ? `signal ${s.last_signal}` : s.last_exit ?? '—'}</td>${ACT('svc:' + s.label)}</tr>`; })}</tbody></table>${shell.list.pager(rows.length, DS, 'services')}`
      : html`<p class="empty">${SERVICES.length ? html`No service matches. <button class="linkbtn" type="button" data-clear-q>Clear filters</button>` : 'No installed service was found.'}</p>`}`}`);
}
document.getElementById('view-deploys').addEventListener('click', e => {
  if (shell.list.sortBy(e, DS) || shell.list.paging(e, DS)) { shell.list.keepFocus(e, renderDeploys); return setURL(); }
  const f = e.target.closest('[data-f="state"]');
  if (f) { const word = `state:${f.dataset.v}`, was = text.q.split(/\s+/).filter(Boolean);
    applyText(shell.filter((was.includes(word) ? was.filter(w => w !== word) : [...was.filter(w => !/^state:/i.test(w)), word]).join(' '))); return; }
  if (e.target.closest('.rowact')) return;
  const tr = e.target.closest('tr[data-id]'); if (tr) selectRow(tr.dataset.id, true);
});

// ---------- Schedules ----------
const SS = { sort: 'rank', dir: 1, page: 1, size: 25 };
const RANK = { warning: 0, caution: 1, unknown: 2, ok: 3 };
// The badge is the read's own (sd:3446, management_screen._attention): scheduled jobs whose last run failed.
function pageAttention() {
  window.PAGE_ATTENTION = DOC && DOC.attention || { state: 'unknown', n: 0, what: 'scheduled jobs not read' };
  window.shell?.attention?.(window.PAGE_ATTENTION);
}
function renderSchedules() {
  const el = document.getElementById('view-schedules');
  if (!DOC.jobs) { put(el, unknown('Jobs were not read', why('jobs'), 'sd jobs list')); return; }
  const { chips, free } = tokens();
  const rows = CRON.filter(c => (!chips.state || (chips.state === 'failed' ? c.rank === 'warning' : c.state === chips.state)) && free.every(f => c.name.toLowerCase().includes(f)))
    .sort((a, b) => SS.sort === 'rank' ? (RANK[a.rank] - RANK[b.rank]) || a.next.localeCompare(b.next)
      : SS.sort === 'name' ? a.name.localeCompare(b.name) * SS.dir : a.next.localeCompare(b.next) * SS.dir);
  const failed = CRON.filter(c => c.rank === 'warning').length;
  const shown = pageOf(rows, SS);
  put(el, html`
    <div class="sec-head"><h2 id="sch-h">Schedules <button class="help" type="button" aria-label="Help: Schedules" data-help="<b>Scheduled jobs,</b> failed first, then by next run. Last run is when each run started and ended, and how it exited. Times use this browser's clock; the jobs use the Mac's.">${I('circle-help')}</button></h2>
      <p class="tally">${hides(rows.length, CRON.length, 'job')}<span class="g-warning">■ ${failed} failed</span><span class="g-ok">● ${CRON.length - failed} not failed</span><span>launchd · ${hhmm(READ)} UTC</span><span class="g-unknown">▨ cloud routines not read</span></p></div>
    <div class="filters toolbar-2">${filterBar(filterSeg('state', 'State', ['failed'], chips.state), text.q)}</div>
    ${rows.length ? html`<table class="ledger" aria-labelledby="sch-h" data-tfilter="page" data-toolbar="toolbar"><thead>${shell.list.sortHead(SCHED_COLS, SS)}</thead>
      <tbody>${shown.map((c, i, a) => html`<tr data-id="cron:${c.name}"${i && a[i - 1].rank !== c.rank ? html` class="band-start"` : ''}><td class="g g-${c.rank}">${GLYPH[c.rank]}<span class="sr">${c.rank}</span></td>
        <td class="what"><button type="button">${c.name}</button></td><td class="d mono">${human(c.schedule)}</td><td class="d mono">${c.state}</td><td class="d mono" data-k="last">${lastCell(c)}</td><td class="d mono" data-k="next"><time class="rel" datetime="${c.next}" data-future data-empty="never"></time></td>
        <td class="d num" data-k="exit">${c.last_signal != null ? `sig ${c.last_signal}` : c.last_exit === null || c.last_exit === undefined ? html`<span class="no">—</span>` : c.last_exit}</td>${ACT('cron:' + c.name)}</tr>`)}</tbody></table>${shell.list.pager(rows.length, SS, 'jobs')}`
      : html`<p class="empty">${CRON.length ? html`No job matches. <button class="linkbtn" type="button" data-clear-q>Clear filters</button>` : 'No launchd calendar job is installed.'}</p>`}`);
}
document.getElementById('view-schedules').addEventListener('click', e => {
  if (shell.list.sortBy(e, SS) || shell.list.paging(e, SS)) { shell.list.keepFocus(e, renderSchedules); return setURL(); }
  const f = e.target.closest('[data-f="state"]');
  if (f) { applyText(shell.filter(/state:failed/.test(text.q) ? text.q.replace(/\s*state:failed/, '').trim() : (text.q + ' state:failed').trim())); return; }
  if (e.target.closest('.rowact')) return;
  const tr = e.target.closest('tr[data-id]'); if (tr) selectRow(tr.dataset.id, true);
});
document.querySelector('main').addEventListener('click', e => {
  if (e.target.closest('[data-clear-q]')) { applyText(shell.filter('')); return; }
  // The filter bar (shell.list.fbar): the text token removes the text, Clear all every filter; applyText draws the view once
  // and writes the URL.
  const q = e.target.closest('[data-f="q"]'), all = e.target.closest('[data-fclear]'); if (!q && !all) return;
  if (all) RS.managed = RS.merge = RS.research = null;
  applyText(shell.filter(all ? '' : text.q.split(/\s+/).filter(w => FIELD.test(w)).join(' ')));
});

// ---------- Details pane ----------
function defaultDetails() {
  detailsId = null;
  if (view === 'repos' && repoParam && pending.length && REPOS.some(x => x.path === repoParam)) return showProposals();
  if (view === 'repos') {
    const r = repoParam && REPOS.find(x => x.path === repoParam);
    put(details, html`<p class="kind">${I('settings')} ${r ? 'Repo settings' : 'Repos'}</p><h2>${r ? r.name : 'How an edit lands'}</h2>
      <ul class="legend">
        <li><span class="route">${I('terminal')}Applied here</span>Merge automatically, Managed, Auto-merge dependency updates, GitHub issues and Issue intake. Run applies the change here, with Undo.</li>
        <li><span class="route">${I('terminal')}You copy</span>Review rules. The proposal shows the change and the line to copy.</li>
        <li><span class="route">${I('shield-check')}GitHub</span>Branch protection, as of last night's check. The dashboard does not change it.</li>
      </ul>
      ${r ? html`<h3>Act</h3>${shell.commands.bar('repo:' + r.path)}` : ''}`);
    if (r) shell.commands.select('repo:' + r.path);
    shell.setContext(r ? `${r.name} settings` : '');
    shell.suggest(r ? [`What would change if ${r.name} went runner_merge manual?`, `Compare ${r.name}'s protection with system's`, 'Which settings here drift from the managed repos?'] : ['Which managed repos declare no review policy?', 'Which repos are auto-merge but unprotected on GitHub?', 'List repos registered but never managed']);
    return swap();
  }
  const first = document.querySelector(`#view-${view} tr[data-id]`);
  if (first) selectRow(first.dataset.id, false);
}
// build (sd:3388): the job's log and its last request, as v1 Operations > Jobs showed them. A log that was not read is unknown
// with its reason, never an empty log.
const requestDetail = r => r ? `${r.action} · ${r.status}${r.who ? ` · ${r.who}` : ''}${r.at ? ` · ${r.at}` : ''}${r.message ? ` · ${r.message}` : ''}` : 'none recorded';
function logDetail(c) {
  const g = c.log, file = `local-cron-jobs/logs/${c.name}.log`;
  const head = html`<h3>Log</h3>`;
  if (!g || g.state === 'unread') return html`${head}${unknown('The log was not read', g ? g.reason : 'this reading carries no log', file)}`;
  if (g.state === 'none') return html`${head}<p class="why">${g.reason}.</p>`;
  return html`${head}<p class="why">${g.cut ? `The last ${plural(g.text.split('\n').filter(Boolean).length, 'line')} of ` : ''}<span class="mono">${file}</span>, read ${hhmm(READ)} UTC.</p>
    ${g.text ? html`<pre class="code" tabindex="0" aria-label="${c.name} log">${g.text}</pre>` : html`<p class="empty">The log is empty.</p>`}`;
}
// sd:5064: an adversarial review's progress redraws the repo's Details while that repo is shown. Its runKey is
// `<kind>:<path>`: repo, change (a pull request, sd:5100) or deep, and the row id is `repo:<path>`.
let detailsId = null;
document.addEventListener('adversarial:run', e => { const k = e.detail.key, at = k.indexOf(':');
  if (detailsId && ['repo', 'change', 'deep'].includes(k.slice(0, at)) && detailsId === `repo:${k.slice(at + 1)}`) selectRow(detailsId, false); });
function selectRow(id, open) {
  markRow(id); detailsId = id;
  const [kind, key] = [id.slice(0, id.indexOf(':')), id.slice(id.indexOf(':') + 1)];
  const act = shell.commands.get(id) ? html`<h3>Act</h3>${shell.commands.bar(id)}` : '';
  let h = '';
  if (kind === 'cron') {
    const c = CRON.find(x => x.name === key); if (!c) return;
    h = html`<p class="kind"><span class="g-${c.rank}" aria-hidden="true">${GLYPH[c.rank]}</span> Schedule · ${c.state}</p><h2>${c.name}</h2>
      <dl><dt>Service</dt><dd>${c.service || '—'}</dd><dt>Schedule</dt><dd>${human(c.schedule)}</dd><dt>Next run</dt><dd><time class="rel" datetime="${c.next}" data-future data-long data-empty="never"></time></dd><dt>Last exit</dt><dd>${c.last_signal != null ? `signal ${c.last_signal}` : c.last_exit ?? 'not run since load'}${c.last_exit === 127 ? ' · command not found' : ''}</dd>
      <dt>Last run</dt><dd>${lastDetail(c)}</dd><dt>Run stamp</dt><dd class="mono">local-cron-jobs/logs/.${c.name}.stamp</dd>
      <dt>Last request</dt><dd>${requestDetail(c.last_request)}</dd></dl>
      ${c.last_exit === 127 ? html`<p class="why">Exit 127: the shell could not find the command. A retry fails the same way until the path is fixed.</p>` : ''}${act}${logDetail(c)}`;
    shell.suggest([`Why did ${c.name} exit ${c.last_exit ?? 0}?`, `What does ${c.name} do and who reads its output?`, 'Which schedules overlap tonight?']);
  } else if (kind === 'repo') {
    const r = ALL.find(x => x.path === key); if (!r) return;
    const g = r.git, pw = pullWhy({ row: r });
    const git = g ? html`<h3>Git state</h3><dl><dt>Branch</dt><dd>${g.branch || '?'}</dd><dt>Default</dt><dd>${g.default || '—'}</dd><dt>Dirty</dt><dd>${g.dirty === null ? 'not read' : plural(g.dirty, 'file')}</dd>
        <dt>Ahead / behind</dt><dd>+${g.ahead ?? '?'} / −${g.behind ?? '?'} on ${g.branch || '?'}</dd><dt>Default lag</dt><dd>${g.headline}${g.detail ? `: ${g.detail}` : ''}</dd>
        <dt>Last fetch</dt><dd>${age(g.fetched_iso)} before the reading</dd><dt>Last commit</dt><dd>${g.subject || '—'}</dd><dt>Read</dt><dd>${READ}</dd></dl>
        ${pw === true ? '' : html`<p class="why">No pull offered: ${pw}.</p>`}`
      : html`<h3>Git state</h3><p class="why"><span class="g-unknown" aria-hidden="true">▨</span> Not read: ${pw}.</p>`;
    h = html`<p class="kind">${I(r.registered ? 'settings' : 'folder-code')} Repo${r.registered ? '' : ' · not registered'}</p><h2>${r.name}</h2>
      <dl><dt>Path</dt><dd>${r.path}</dd><dt>Owner</dt><dd>${r.owner}</dd><dt>Managed</dt><dd>${r.managed}</dd><dt>Lane merge</dt><dd>${r.merge}</dd></dl>
      ${r.registered ? html`<p><a class="btn quiet" href="?repo=${encodeURIComponent(r.path)}#view=repos">${I('settings')} Open settings</a></p>
      <p class="why">The settings page shows the repo's settings, review rules and GitHub protection.</p>`
        : html`<p class="why">This checkout is not registered, so it has no settings page. Register it from the terminal: <code>sd-db repo add ${r.path}</code>.</p>`}${git}${act}
      ${window.commands.reviewPane('repo', r.path)}${window.commands.reviewPane('change', r.path, 'Adversarial review: PR')}${window.commands.reviewPane('deep', r.path, 'Deep review')}${r.registered ? deepLine() : ''}`;
  } else if (kind === 'cs') {
    const r = (CLAUDE?.sessions || []).find(x => x.id === key); if (!r) return;
    h = claudeDetails(r, act);
  } else if (kind === 'proc') {
    const p = SESS.processes[+key]; if (!p) return;
    h = html`<p class="kind"><span class="g-ok">●</span> sd-* process</p><h2>${p.command}</h2><dl><dt>PID</dt><dd>${p.pid}</dd><dt>Running for</dt><dd>${p.elapsed}</dd></dl>
      <p class="why">A process has no command here; stop it from its own terminal.</p>`;
  } else if (kind === 'tree') {
    const t = (SESS?.worktrees || [])[+key]; if (!t) return;
    const st = TREESTATE[t.state] || 'unknown';
    h = html`<p class="kind"><span class="g-${st}">${GLYPH[st]}</span> Worktree · ${t.state}</p><h2>${t.name}</h2><dl><dt>Repository</dt><dd>${t.repo}</dd><dt>Branch</dt><dd>${t.branch || '—'}</dd><dt>Path</dt><dd class="mono">${t.path || '(unreadable registration)'}</dd><dt>Source</dt><dd>fleet sessions</dd></dl>
      ${t.state === 'abandoned' ? html`<p class="why">Its directory is gone and it still holds its branch; Prune on the abandoned row clears it.</p>` : ''}`;
  } else if (kind === 'wt') {
    h = key === 'abandoned'
      ? html`<p class="kind"><span class="g-caution">▲</span> Worktrees · abandoned</p><h2>${SESS.abandoned} abandoned worktrees</h2><dl><dt>What</dt><dd>registered, directory gone</dd><dt>Source</dt><dd>fleet sessions</dd></dl>${act}`
      : html`<p class="kind"><span class="g-ok">●</span> Worktrees · live</p><h2>${liveTrees()} live worktrees</h2><dl><dt>Registered</dt><dd>${SESS.registered}</dd></dl>`;
  } else if (kind === 'sats') {
    const q = SATS?.request;
    h = html`<p class="kind">${I('rotate-ccw')} Refresh satellites</p><h2>Refresh satellites</h2><dl><dt>Last request</dt><dd>${q ? q.id : 'none sent'}</dd><dt>Sent</dt><dd><time class="rel" datetime="${q?.at || ''}" data-long data-empty="never"></time></dd></dl>
      <p class="why">Each other machine answers within five minutes, once its merges finish. A run stopped part way runs once more; a second stop is written as failed.</p>${act}`;
  } else if (kind === 'upload') {
    const u = UPLOAD; if (!u?.set_up) return;
    const st = UPSTATE[u.link_state] || 'unknown', l = u.link;
    h = html`<p class="kind"><span class="g-${st}">${GLYPH[st]}</span> Shared upload · ${upWords(u)}</p><h2>${u.prefix} upload link</h2>
      <dl><dt>Bucket</dt><dd><a href="${u.console}" target="_blank" rel="noopener noreferrer">${u.bucket}</a> · ${u.region}</dd><dt>Prefix</dt><dd class="mono">${u.prefix}</dd>
      <dt>Expires</dt><dd>${l ? html`<time class="rel" datetime="${l.expiry}" data-future data-long></time>` : '—'}</dd><dt>Signed</dt><dd>${l ? html`<time class="rel" datetime="${l.signed}" data-long></time>` : '—'}</dd>
      <dt>Public URL</dt><dd class="mono">${l ? l.public : u.public}</dd><dt>Config</dt><dd class="mono">${u.config}</dd></dl>
      ${u.link_reason ? html`<p class="why">${u.link_reason}.</p>` : ''}${u.link_state === 'current' ? html`<div class="cli fold"><button class="cli-line" type="button" aria-expanded="false" title="Show the full command"><code>${l.curl}</code></button><button class="icon-btn" type="button" data-copy="${l.curl}" aria-label="Copy the curl line">${I('copy')}</button></div>` : ''}
      <p class="why">Regenerate replaces the link shown here. Links handed out before keep working until they expire; to stop them all, deactivate the uploader's access key in IAM.</p>${act}`;
  } else if (kind === 'sat') {
    const x = SATS?.satellites.find(y => y.host === key); if (!x) return;
    const st = SATSTATE(x), r = x.result || {};
    h = html`<p class="kind"><span class="g-${st}">${GLYPH[st]}</span> Satellite · ${x.state}</p><h2>${x.host}</h2>
      ${x.state === 'unread' ? html`<p class="why"><span class="g-unknown" aria-hidden="true">▨</span> Its result was not read: ${x.reason}.</p>` : html`<dl><dt>Last follow</dt><dd><time class="rel" datetime="${x.followed || ''}" data-long data-empty="never"></time></dd><dt>Request</dt><dd>${r.request || '—'}${r.attempt ? ` · attempt ${r.attempt}` : ''}</dd><dt>Started</dt><dd><time class="rel" datetime="${r.started || ''}" data-long data-empty="never"></time></dd><dt>Finished</dt><dd><time class="rel" datetime="${r.finished || ''}" data-long data-empty="never"></time></dd>
      <dt>Exit</dt><dd>${r.exit ?? '—'}</dd><dt>Step</dt><dd>${r.step || '—'}</dd><dt>Last line</dt><dd class="mono">${r.line || '—'}</dd></dl>`}`;
  } else if (kind === 'svc') {
    const s = SERVICES.find(x => x.label === key); if (!s) return;
    const st = SVCSTATE(s);
    h = html`<p class="kind"><span class="g-${st}">${GLYPH[st]}</span> Service · ${s.state}</p><h2>${s.name}</h2><dl><dt>Label</dt><dd>${s.label}</dd><dt>What</dt><dd>${s.category} · ${s.scope} · ${s.domain}</dd><dt>PID</dt><dd>${s.pid ?? '—'}</dd><dt>Previous exit</dt><dd>${s.last_exit ?? '—'}</dd><dt>Last request</dt><dd>${requestDetail(s.last_request)}</dd><dt>Deployed at</dt><dd>▨ not recorded</dd></dl>${act}`;
  }
  put(details, h); swap();
  if (shell.commands.get(id)) shell.commands.select(id); else shell.setContext(details.querySelector('h2')?.textContent || '');
  if (kind !== 'cron') shell.suggest(['What changed here in the last day?', 'What should I look at first on this page?', 'Explain this row']);
  shell.row(id);
  if (open) shell.openPane('tab-details');
}

// ---------- Filter: the shell's Shapeshift bar (sd:5539) ----------
// Its text is this view's filter: free words and the managed:, merge: and state: chips. A chip control sets the bar's text.
const RENDER = new Map([['repos', renderRepos], ['sessions', renderSessions], ['deploys', renderDeploys], ['schedules', renderSchedules], ['machine', renderMachine]]);
function render() {
  const draw = RENDER.get(view);
  if (!draw) throw new Error(`no such view: ${view}`);
  draw();
}
function applyText(q = text.q) {
  text.q = q.trim();
  RS.page = SS.page = DS.page = 1;
  if (DOC) render();
  setURL();
}
document.addEventListener('shell:filter', e => { if (e.detail.q !== text.q) applyText(e.detail.q); });
window.PAGE_FILTER = { repos: 'repos, e.g. merge:auto', sessions: 'Claude sessions', deploys: 'services, e.g. state:running', schedules: 'jobs, e.g. state:failed' };
window.PAGE_LIST = { rows: () => document.querySelectorAll(`#view-${view} tbody tr[data-id]`), select: id => selectRow(id, false) };

// build (sd:3521): capture's repo default is the selected repo row's.
window.PAGE_REPO = o => o && o.type === 'repo' ? o.path : null;

window.PAGE_COMMANDS = [
  { label: 'Show failed schedules', icon: 'calendar-clock', run: () => location.href = '?q=state:failed#view=schedules' },
  { label: 'Open system repo settings', icon: 'settings', run: () => location.href = `?repo=${encodeURIComponent('~/repos/system')}#view=repos` },
];

// ---------- Start (build, sd:2485: shell.read reads /api/management, then draws; it rereads after each write) ----------
// The reader (read.js, sd:2418) holds the guards: of overlapping reads only the newest draws, a row the read no longer lists
// runs no command, a selection whose row is gone moves to the view's first row, a failed load clears the page, and a failed
// reread after a write keeps the rows and says the write landed.
let reading = null, FAILED = '';
const load = () => reading.load();
// Read again (sd:5638): the shell's head button reads the management document again, and the Research reading while
// its filter is on: a failed read is kept until then (lead review of 960414d82).
window.PAGE_AGAIN = { title: 'Refresh Management', run: () => {
  if (view === 'machine') return readMachine();
  if (RS.research) RESEARCH = null;
  return load();
} };
const reread = () => reading.reread();
function adopt(doc) {
  absorb(doc);
  const failed = Object.entries(doc.sources || {}).filter(([, v]) => v).map(([k]) => k);
  return { objects: objects(), state: failed.length ? { kind: 'partial', text: `Not read: ${failed.join(', ')}. Each shows why in its view.`, source: '/api/management' } : null };
}
function clear(err) {
  DOC = null; READ = ''; GIT = SESS = CLAUDE = SATS = null; REPOS = []; ALL = []; SERVICES = []; CRON = [];
  FAILED = err.message;
}
function draw() {
  lamps();
  pageAttention();
  if (!DOC) { VIEWS.filter(v => v !== 'machine').forEach(v => put(document.getElementById(`view-${v}`), unknown('Management was not read', FAILED, '/api/management'))); return; }
  renderSessions(); renderDeploys(); renderSchedules(); renderRepos();
  document.getElementById('subhead').textContent = repoParam ? `Settings for ${repoParam}. Chat is scoped to this repo.`
    : `Every repo setting, sessions, services and schedules. Read ${hhmm(READ)} UTC.`;
}
// The repos view selects no row by default: Details explains how an edit lands, or shows the repo's settings.
const inView = id => !!id && !!document.querySelector(`#view-${view} tr[data-id="${CSS.escape(id)}"]`);
const firstRow = () => view === 'repos' ? null : document.querySelector(`#view-${view} tr[data-id]`)?.dataset.id ?? null;
function nothingSelected() {
  if (view === 'repos') return defaultDetails();
  markRow(null);
  put(details, html`<p class="why">Nothing is selected. Select a row to see it here.</p>`);
  swap();
}
document.addEventListener('DOMContentLoaded', () => {
  // A repo's settings page is in Repos; an older ?view= link opens its view unless #view= names one. The shell's first
  // 'shell:view' fires after this listener.
  const asked = repoParam ? 'repos' : VIEWS.includes(params.get('view')) && !/^#view=/.test(location.hash) ? params.get('view') : null;
  if (asked && shell.view.current() !== asked) shell.view.show(asked);
  view = VIEWS.includes(shell.view.current()) ? shell.view.current() : 'repos';
  if (view === 'machine') readMachine();
  registerCommands();
  readURL();
  document.addEventListener('shell:view', e => { if (e.detail.id !== view) { showView(e.detail.id); lamps(); } });
  document.getElementById('annunciator').addEventListener('click', e => {
    const c = e.target.closest('button.cell'); if (!c) return;
    shell.view.show(c.dataset.view);
  });
  reading = shell.read({
    source: '/api/management', what: 'repos, sessions, services and jobs', adopt, clear, draw,
    current: () => shell.row(), first: firstRow,
    // A ?row= of another view is not selected there: the view's first row is.
    select: id => { const at = inView(id) ? id : firstRow(); if (at) selectRow(at, false); else nothingSelected(); },
    unselect: nothingSelected,
  });
  load();
});
