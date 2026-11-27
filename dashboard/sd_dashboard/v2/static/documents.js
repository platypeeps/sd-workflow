// Documents (sd:2114): the design source's products/system/designs/v2/documents.js at 7f50ad4, ported. Each change from the
// reference is marked "build:". The rows are /api/documents (documents_screen.py): the files /documents/<key>/<file> serves,
// never sample data. Pin, Hide and Tag are the workflow database's view state (sd:3428): each posts /api/documents/view,
// offers Undo, and the reader reads the list again. When the store was not read, those commands are off with its reason.
// A root's name and Hide root are its own view row (sd:3986): Rename root and Hide root post { root, label | hidden }.
const { html, put, plural } = window.markup;
const I = n => html`<svg class="i" aria-hidden="true"><use href="#i-${n}"/></svg>`;
const shq = s => `'${String(s ?? '').replace(/'/g, "'\\''")}'`; // shell-safe: single quotes, so $(), backticks and \ stay literal
const shPath = p => (p.startsWith('~/') ? `~/${shq(p.slice(2))}` : shq(p)); // ~ stays outside the quotes, so the shell expands it
// The tab carries the list's count (sd:5524); the panel is the list's section, view-documents. Books (sd:6260) reads its
// own document when it first shows; learning.js draws it.
window.PAGE_VIEWS = [{ id: 'documents', label: 'Documents' }, { id: 'books', label: 'Books and courses', api: '/api/documents/books' },
  { id: 'videos', label: 'Videos', api: '/api/documents/videos' }];
// sd:6260: Books shares #details, so this list draws Details only while its view shows, and again when it shows again.
const onDocs = () => (window.shell?.view?.current?.() || 'documents') === 'documents';
// build: the page claims nothing for the rail until /api/documents answers; load() sets it from the rows.
window.PAGE_ATTENTION = { state: 'unknown', n: 0, what: 'documents not read' };
// build: the reading arrives by fetch, not as data/documents-data.js globals. OBSERVED is its time; ROOTS and DOCS its rows.
let OBSERVED = Date.now(), ROOTS = [], LABEL = {}, DOCS = [], STORE = '', DOC = null, FOLD = false;
const KINDS = ['report', 'research', 'design', 'dashboard'];
const FRESH = { today: 'modified today', week: '1–7 days', older: 'older than 7 days', stale: 'render-stale' };
function days(iso) { return Math.floor((Date.parse(new Date(OBSERVED).toISOString().slice(0, 10)) - Date.parse(iso.slice(0, 10))) / 864e5); }
// A destination is on when the row designates it or already holds a copy there; a row with neither is None.
const DEST = { drive: ['Drive', 'triangle'], notion: ['Notion', 'box'], obsidian: ['Obsidian', 'gem'], none: ['None', ''] };
function dests(d) {
  const p = d.pub || {}, on = ['drive', 'notion', 'obsidian'].filter(ch => (p.choice && p.choice[ch]) || (p.copies || []).some(c => c.channel === ch));
  return on.length ? on : ['none'];
}
function fresh(d) { const n = days(d.mod); return n <= 0 ? 'today' : n <= 7 ? 'week' : 'older'; }
const kb = b => b >= 1048576 ? `${(b / 1048576).toFixed(1)} MB` : `${Math.round(b / 1024).toLocaleString()} KB`;
const rootOf = d => ROOTS.find(x => x.key === d.key);
// A hidden root leaves the Repo chips for the Hidden roots fold; the server sends no rows for it.
const shownRoots = () => ROOTS.filter(r => !r.hidden);
// build: a root is a checkout's docs/dashboard; the checkout is where its render runs.
const checkoutOf = d => rootOf(d).path.replace(/\/docs\/dashboard$/, '');
// sd:5666: a computed research.conf.py is read from its render's research.json, as on Research; the root says what was not.
const confNote = r => r.conf ? `research.conf.py was not read: ${r.conf}`
  : r.manifest === 'older' ? `docs/dashboard/research.json is from an older research.conf.py: run sd-research-kit render in ${checkoutOf(r)} to refresh it` : '';

// ---------- State ----------
const F = { repo: new Set(), kind: new Set(), fresh: new Set(), dest: new Set(), text: '', pinned: false, hidden: 'open' };
// The list's sort, page and size (shell.list, sd:2682): newest first, 50 a page.
const LIST = { sort: 'mod', dir: -1, size: 50 }, L = { ...LIST, page: 1 };
let selected = null, picked = [];

function matches(d) {
  if (!window.shell.aside.shows(F.hidden, d.hidden)) return false;
  if (F.pinned && !d.pinned) return false;
  if (F.repo.size && !F.repo.has(d.key)) return false;
  if (F.kind.size && !F.kind.has(d.kind)) return false;
  if (F.dest.size && !dests(d).some(ch => F.dest.has(ch))) return false;
  if (F.fresh.size && !([...F.fresh].some(f => f === 'stale' ? d.stale : fresh(d) === f))) return false;
  if (F.text) { const hay = `${d.title} ${d.h1} ${d.desc} ${d.file} ${d.tags.join(' ')}`.toLowerCase(); if (!F.text.split(/\s+/).every(w => hay.includes(w))) return false; }
  return true;
}
const cmp = { title: d => d.title.toLowerCase(), repo: d => (LABEL[d.key] || d.key).toLowerCase(), mod: d => d.mod, size: d => d.bytes };
function sorted(list) {
  const k = cmp[L.sort];
  return [...list].sort((a, b) => (b.pinned - a.pinned) || (k(a) < k(b) ? -1 : k(a) > k(b) ? 1 : 0) * L.dir);
}
// sd:5484: the list groups by repo, one band row each, and the shell's table grammar folds a band and keeps it shut across
// sessions. Bands run by repo name, turned around while Repo sorts descending; the sort orders the rows inside each band.
function grouped(list) {
  const by = new Map(), name = k => (LABEL[k] || k).toLowerCase(), dir = L.sort === 'repo' ? L.dir : 1;
  sorted(list).forEach(d => { if (!by.has(d.key)) by.set(d.key, []); by.get(d.key).push(d); });
  return [...by.keys()].sort((a, b) => (name(a) < name(b) ? -1 : name(a) > name(b) ? 1 : 0) * dir).flatMap(k => by.get(k));
}
const ordered = () => grouped(DOCS.filter(matches));

// ---------- Facets ----------
const facets = document.getElementById('facets');
function facetCount(field, value) {
  const save = F[field]; F[field] = new Set([value]);
  const n = DOCS.filter(matches).length; F[field] = save; return n;
}
// build: no reader tells a dashboard or a design document yet; kind is research (a checkout with research.conf.py) or report.
const KIND_OFF = k => (k === 'design' || k === 'dashboard') && !DOCS.some(d => d.kind === k) ? `No ${k} documents: kind is derived as research (a checkout with research.conf.py) or report until the document index stores it.` : '';
function renderFacets() {
  let offs = 0;
  const chip = (field, v, label, extra = '') => {
    const on = F[field].has(v), n = facetCount(field, v);
    const off = field === 'kind' ? KIND_OFF(v) : '';
    // build: aria-disabled, not disabled: a disabled button leaves the tab order, and its reason with it. The reason is
    // an element the chip describes itself by, shown on focus; a title alone is a hover tooltip.
    const id = off ? `facet-off-${++offs}` : '';
    const dis = off ? html` aria-disabled="true" aria-describedby="${id}" title="${off}"` : '';
    const chipEl = html`<button class="chip" type="button" data-f="${field}" data-v="${v}" aria-pressed="${String(on)}"${dis}>${extra}${label} <span class="n">${n}</span></button>`;
    return off ? html`${chipEl}<span class="offwhy" id="${id}">${off}</span>` : chipEl;
  };
  // sd:6701: Open | Hidden, the shell's one set-aside group, in the Show facet beside Pinned only.
  const hid = DOCS.filter(d => d.hidden).length;
  put(facets, html`
    <div class="facet">${window.shell.list.pick('repo', 'Repo', shownRoots().map(r => [r.key, LABEL[r.key] || r.key, facetCount('repo', r.key), r.n === 0 ? `Nothing generated yet in ${r.path}` : '']), F.repo, { multi: true, help: hiddenRoots() })}</div>
    <div class="facet"><span class="label">Kind <button class="help" type="button" aria-label="Help: Kind" data-help="<b>Research</b>: a document from a research project (a repo with research.conf.py). <b>Report</b>: every other document. Design and dashboard need a per-file record that is not built yet.">${I('circle-help')}</button></span><div class="chips">${KINDS.map(k => chip('kind', k, k))}</div></div>
    <div class="facet"><span class="label">Freshness</span><div class="chips">${Object.entries(FRESH).map(([k, l]) => chip('fresh', k, l, k === 'stale' ? html`<span class="g-caution" aria-hidden="true">▲</span>` : ''))}</div></div>
    <div class="facet"><span class="label">Destination</span><div class="chips">${Object.entries(DEST).map(([k, [l, i]]) => chip('dest', k, l, i ? I(i) : ''))}</div></div>
    <div class="facet"><span class="label">Show</span><div class="chips">${window.shell.aside.seg('hidden', 'Hidden', F.hidden, { open: DOCS.length - hid, aside: hid })}
      <button class="chip" type="button" id="f-pinned" aria-pressed="${String(F.pinned)}">${I('pin')}Pinned only <span class="n">${DOCS.filter(d => d.pinned && !d.hidden).length}</span></button></div></div>`);
}
// The fold of hidden roots, each with Unhide root; it stays open across a reread while the operator has it open.
function hiddenRoots() {
  const hid = ROOTS.filter(r => r.hidden); if (!hid.length) return '';
  return html`<details class="rootfold"${FOLD ? ' open' : ''}><summary>Hidden roots <span class="n">${hid.length}</span></summary><ul>${hid.map(r => html`<li><span>${r.label}</span>${shell.commands.rowActions(`root:${r.key}`)}</li>`)}</ul></details>`;
}
// build (sd:5158): Repo is a dropdown of checkboxes (shell.list.pick), one per shown root; a root with nothing generated
// stays in it, off with its reason.
facets.addEventListener('change', e => window.shell.list.pickChange(e, F, () => { L.page = 1; render(); }));
facets.addEventListener('click', e => {
  const b = e.target.closest('button[data-aside="hidden"]');
  if (b) { F.hidden = b.dataset.v; L.page = 1; render(); return; }
  const c = e.target.closest('.chip');
  if (!c && e.target.closest('.rootfold summary')) { FOLD = !FOLD; return; }
  if (!c || c.getAttribute('aria-disabled') === 'true') return;
  if (c.id === 'f-pinned') F.pinned = !F.pinned;
  else { const s = F[c.dataset.f]; s.has(c.dataset.v) ? s.delete(c.dataset.v) : s.add(c.dataset.v); }
  L.page = 1; render();
});

// ---------- List ----------
const tbody = document.getElementById('rows');
const COLS = [[null, html`<span class="sr">State</span>`, 'g'], ['title', 'Document'], ['repo', 'Repo'], [null, 'Kind', 'kind'], ['mod', 'Modified'], ['size', 'Size', 'num'], [null, html`<span class="sr">Actions</span>`, 'acts']];
// Active filters, one chip each; the key names the field and, for a set, its value.
function activeFilters() {
  const act = [];
  F.repo.forEach(v => act.push({ key: `repo:${v}`, label: `repo:${v}` })); F.kind.forEach(v => act.push({ key: `kind:${v}`, label: `kind:${v}` }));
  F.dest.forEach(v => act.push({ key: `dest:${v}`, label: `dest:${v}` }));
  F.fresh.forEach(v => act.push({ key: `fresh:${v}`, label: v === 'stale' ? 'is:stale' : `age:${v}` }));
  if (F.pinned) act.push({ key: 'pinned', label: 'is:pinned' });
  if (F.text) act.push({ key: 'q', label: `“${F.text}”` });
  return act;
}
// The render state a row's first cell draws: [state class, glyph, words]. The help line under the table lists this map with
// the pin and the publish fills (sd:5731), so a new state shows its meaning there too.
const RENDER = { stale: ['caution', '▲', 'render-stale: source newer than page'], ok: ['ok', '●', 'rendered after its source'],
  unknown: ['unknown', '▨', 'unknown: no source Markdown to compare'] };
const legend = () => [...Object.values(RENDER).map(([s, g, w]) => [html`<span class="g-${s}" aria-hidden="true">${g}</span>`, w]),
  [html`<span class="pinmark" aria-hidden="true">${I('pin')}</span>`, 'pinned'], ...window.commands.pubLegend()];
function row(d) {
  // Every state cell carries its glyph and its words; no source means the render state is unknown, and the cell says why.
  const st = RENDER[d.stale ? 'stale' : d.src ? 'ok' : 'unknown'];
  const g = html`<td class="g g-${st[0]}" title="${st[2]}"><span aria-hidden="true">${st[1]}</span><span class="sr">${st[2]}</span></td>`;
  const meta = ` · ${d.kind} · ${days(d.mod) <= 0 ? 'today' : days(d.mod) + 'd'} · ${kb(d.bytes)}`;
  return html`<tr data-id="${d.id}"${d.hidden ? html` class="hid"` : ''} aria-selected="${String(d.id === selected)}"${picked.includes(d.id) ? html` data-picked` : ''}>
    ${g}
    <td class="what"><button type="button">${d.title}</button>${d.pinned ? html`<span class="pinmark" title="Pinned">${I('pin')}<span class="sr">pinned</span></span>` : ''}${d.tags.length ? html`<span class="tags">${d.tags.map(t => html`<span class="tagc">${t}</span>`)}</span>` : ''}<span class="file">${d.file}</span></td>
    <td class="repo" data-meta="${meta}">${LABEL[d.key] || d.key}</td>
    <td class="kind">${d.kind}</td>
    <td class="mod repo"><time class="rel" datetime="${d.mod}"></time></td>
    <td class="num">${kb(d.bytes)}</td>
    <td class="acts">${window.commands.published(d.pub)}${shell.commands.rowActions(d.id)}</td></tr>`;
}
// A band names its repo and how many of the filtered documents it holds; data-band keys its fold, so a count that changes
// with a filter does not open it again.
const band = (key, n) => html`<tr class="band" data-band="${key}" aria-hidden="true"><td colspan="${COLS.length}">${LABEL[key] || key}<span class="n">${plural(n, 'document')}</span></td></tr>`;
// build: no request is filed (document.request is copy only), so the list has no queued request rows.
function render() {
  renderFacets();
  const L_ = window.shell.list, list = ordered(), slice = L_.pageOf(list, L), n = {};
  list.forEach(d => { n[d.key] = (n[d.key] || 0) + 1; });
  put(document.getElementById('rows-head'), L_.sortHead(COLS, L));
  put(tbody, html`${slice.map((d, i) => html`${i && slice[i - 1].key === d.key ? '' : band(d.key, n[d.key])}${row(d)}`)}`);
  const empty = document.getElementById('empty');
  empty.hidden = list.length > 0;
  // build: an unread store hides nothing, and the empty Hidden view says why.
  put(empty, html`${F.hidden === 'aside' ? html`Nothing is hidden. ${STORE ? `${STORE}.` : 'Hide a document from its row or from Details.'}` : html`No document matches these filters. <button class="linkish" type="button" data-clear>Clear filters</button>`}`);
  put(document.getElementById('chips'), L_.chips(activeFilters(), list.length, DOCS.filter(d => window.shell.aside.shows(F.hidden, d.hidden)).length));
  put(document.getElementById('pager'), L_.pager(list.length, L, 'documents'));
  put(document.getElementById('list-help'), L_.help('', legend()));
  window.shell?.view.count('documents', list.length);
  // A filter that hides the selected row moves the selection to the first shown row, or clears it (design.md, Page contract).
  // No selection counts too: after an empty filter clears it, the next non-empty list selects its first row again.
  if (onDocs()) shell.reconcile({ rows: tbody.querySelectorAll('tr[data-id]'), current: selected, select: id => show(id),
    clear: () => { selected = null; put(details, html`<p class="why">No document matches these filters, so nothing is selected.</p>`); } });
  writeURL();
  const stale = DOCS.filter(d => d.stale && !d.hidden).length;
  const nosrc = DOCS.filter(d => !d.src && !d.hidden).length;
  put(document.getElementById('tally'), html`<span class="g-caution">▲ ${stale} render-stale</span><span class="g-unknown">▨ ${nosrc} no source</span><span>${DOCS.filter(d => d.pinned).length} pinned</span>`);
  // build: the subhead names the reading's time from /api/documents, not a fixed observation.
  put(document.getElementById('subhead'), html`${plural(shownRoots().length, 'root')} · read ${DOC ? html`<time class="rel" datetime="${DOC.read}"></time>` : 'not yet'}`);
}
document.getElementById('rows-head').addEventListener('click', e => { if (window.shell.list.sortBy(e, L)) window.shell.list.keepFocus(e, render); });
document.getElementById('pager').addEventListener('click', e => { if (window.shell.list.paging(e, L)) window.shell.list.keepFocus(e, render); });
document.getElementById('chips').addEventListener('click', e => {
  if (e.target.closest('[data-unfilter-all]')) return clearAll();
  const b = e.target.closest('[data-unfilter]'); if (!b) return;
  const [f, v] = b.dataset.unfilter.split(/:(.*)/);
  if (f === 'pinned') F.pinned = false; else if (f === 'q') { F.text = ''; input.value = ''; } else F[f].delete(v);
  L.page = 1; render();
});
function clearAll() { F.repo.clear(); F.kind.clear(); F.fresh.clear(); F.dest.clear(); F.text = ''; F.pinned = false; F.hidden = 'open'; input.value = ''; L.page = 1; renderShift(); render(); }
document.getElementById('main').addEventListener('click', e => {
  if (e.target.closest('[data-clear]')) return clearAll();
});

tbody.addEventListener('click', e => {
  if (e.target.closest('.rowact')) return;
  const tr = e.target.closest('tr[data-id]'); if (tr) select(tr.dataset.id, true);
});

// ---------- Commands (products/system/commands.md) ----------
// Each row is a document; each tag on the open document is a document tag. Pin, hide, tag and untag are view state: they act at
// once with Undo. build: the reference kept them in page memory; here each is one POST of the fields it changes (sd:3428), and
// Undo posts the value the page showed before. A store the read could not reach turns all six off with its reason.
const NO_CLI = 'no CLI: view state; the dashboard writes it (POST /api/documents/view)';
const storeOff = () => STORE || true;
const view = (o, change) => window.shell.post('/api/documents/view', { root: o.key, file: o.file, ...change }).then(v => reading.reread().then(() => v));
let lastTag = '', tagFocus = false;
// A tag is one lower-case word: spaces become hyphens, as the reference has it.
const tagOf = s => String(s || '').trim().toLowerCase().replace(/\s+/g, '-');
const tagDraft = () => tagOf(document.getElementById('tag-in')?.value) || lastTag;
// build: Disable render has no switch to set. SD_SKIP_RENDER skips every render, and a skip|<key>|<file> or skip|<key> line in
// documents.conf hides a document or a root from this list without stopping its render (sd:1904). Both are off with that reason.
const NO_SWITCH = 'no render switch: SD_SKIP_RENDER skips every render';
const DRAFT = { id: 'draft:request', type: 'document request draft', label: 'new document request', title: '', cmd: '' };
// sd:5887: Request files the task the line names through /api/items, as Capture does; Undo closes it, as sd task cancel
// does. r is Render on a document (sd:3525), so Request has no key. sd:5958: the item belongs to the root's checkout, as the
// line's --belongs-to says; a root that is no checkout's docs/dashboard names none.
const REQUEST = { id: 'document.request', on: 'document request draft', label: 'Request', does: 'Files this request as a task; Undo closes it.', icon: 'plus', risk: 'undo', primary: () => true, executes: true,
  when: o => (o.repoError || (!o.title ? 'type what you need first' : !o.repo ? 'name the repository: add repo:<name>' : true)), cli: o => o.cmd,
  sends: o => `POST /api/items {title, body, priority${o.checkout ? ', repo' : ''}${o.due ? ', due' : ''}}`,
  run: o => window.shell.post('/api/items', { title: `Document request: ${o.title}`, body: `repo=${o.repo} kind=${o.kind} skill=${o.skill}`, priority: 3,
    ...(o.checkout ? { repo: o.checkout } : {}), ...(o.due ? { due: o.due } : {}) })
    .then(out => ({ text: `Request filed · #${out.item.id} · ${o.title}`, item: out.item.id, revision: out.revision })),
  undo: (o, v) => window.shell.post(`/api/items/${v.item}/cancel-task`, { reason: 'withdrawn on Documents right after filing', revision: v.revision }) };
// Tag asks for the tag in a field filled with the one typed under Tags, else the last one applied, so no tag lands unseen
// (sd:3523); Enter in the Tags field runs it without asking, since that field shows the tag. The toast names the tag.
const TAG = { id: 'document.tag', on: 'document', label: 'Tag', does: 'Adds a tag to the document; Undo removes it.', key: 't', icon: 'tag', risk: 'undo', bulk: true, when: storeOff,
  fields: () => [{ name: 'tag', label: 'Tag', required: true, value: tagDraft(), placeholder: 'one word, such as q4', help: 'Spaces become hyphens.' }],
  cli: () => NO_CLI,
  run: (o, v) => {
    const t = v ? tagOf(v.tag) : tagDraft(), was = o.tags;
    if (was.includes(t)) return Promise.reject(new Error(`already tagged ${t}`));
    lastTag = t; return view(o, { tags: [...was, t] }).then(() => ({ text: `Tagged ${t} · ${o.title}`, was }));
  },
  undo: (o, v) => view(o, { tags: v.was }) };
// A root's own view row: its name (empty goes back to the derived one) and Hide root (sd:3986).
const rootView = (o, change) => window.shell.post('/api/documents/view', { root: o.key, ...change }).then(v => reading.reread().then(() => v));
const ROOT_CLI = 'no CLI: the root\'s view row; the dashboard writes it (POST /api/documents/view)';
const ROOT_CMDS = [
  { id: 'root.rename', on: 'document root', label: 'Rename root', does: 'Gives this root a display name; Undo restores the old name.', icon: 'pen-line', risk: 'undo', when: storeOff, cli: () => ROOT_CLI,
    fields: o => [{ name: 'label', label: 'Name', value: o.set || '', placeholder: o.derived, help: `Up to 60 characters; empty goes back to ${o.derived}.` }],
    run: (o, v) => {
      const label = String(v?.label ?? '').trim(), was = o.set || '';
      if (label === was) return Promise.reject(new Error(label ? `already named ${label}` : `already ${o.derived}`));
      return rootView(o, { label }).then(() => ({ text: label ? `Renamed ${o.label} to ${label}` : `${o.label} is ${o.derived} again`, was }));
    },
    undo: (o, v) => rootView(o, { label: v.was }) },
  { id: 'root.hide', on: 'document root', label: 'Hide root', does: 'Hides this root from the list; Undo shows it again.', icon: 'eye-off', risk: 'undo', when: o => o.hidden ? 'already hidden' : storeOff(),
    cli: () => ROOT_CLI, run: o => rootView(o, { hidden: true }).then(() => `Hidden: ${o.label}. Hidden roots, under Repo, lists it.`),
    undo: o => rootView(o, { hidden: false }) },
  { id: 'root.unhide', on: 'document root', label: 'Unhide root', does: 'Shows this root in the list again; Undo hides it.', icon: 'eye', risk: 'undo', primary: () => true, when: o => !o.hidden ? 'not hidden' : storeOff(),
    cli: () => ROOT_CLI, run: o => rootView(o, { hidden: false }).then(() => `Unhidden: ${o.label}`), undo: o => rootView(o, { hidden: true }) },
];
// Publish to and Publish now (sd:5102): commands.js declares both, for a research-kit document here and a brief on Briefs.
const PUBLISH_CMDS = window.commands.publishCommands('document', { base: '/api/documents', payload: o => ({ root: o.key, file: o.file }), reread: () => reading.reread(),
  cli: (o, channel) => `add ${channel}=dict() to the ${o.file.replace(/\.html$/, '')} entry in research.conf.py, then: cd ${shq(checkoutOf(o))} && sd-research-kit publish` });
// Render (sd:5884) runs as Research's does: POST /api/research/render with the root's checkout key (`render`), then one
// poll per run until it settles. One render at a time: Render is off while a run goes. When it ends, the rows are read again.
const RENDER_POLL = 5000;
let RENDERING = null;
const refused = err => { if ([400, 409].includes(err.status)) return err.message; throw err.status ? err : new Error(`no answer (${err.message}), so whether it started is not known`); };
const followRender = (run, o) => (['done', 'failed'].includes(run.state)
  ? (RENDERING = null, shell.toast(run.state === 'done' ? `Rendered · ${o.title}` : `Render failed · ${o.title}: ${run.reason || 'no reason recorded'}`), reading.reread())
  : setTimeout(() => window.shell.getJSON(`/api/research/deck/${encodeURIComponent(run.id)}`).then(next => followRender(next, o),
    e => { RENDERING = null; shell.toast(`Render ${run.id}: not read (${e.message}); whether it ended is not known`); }), RENDER_POLL));
function registerCommands() {
  const C = shell.commands;
  C.register(
    { id: 'document.render', on: 'document', label: 'Render', does: 'Renders this research page again in its checkout.', key: 'r', icon: 'rotate-ccw', risk: 'safe', executes: true, primary: o => o.stale && rootOf(o).research,
      when: o => !rootOf(o).research ? 'not a research repo: no source renders this page'
        : !rootOf(o).render ? `${checkoutOf(o)} is not a research checkout under the repo root, so the dashboard cannot run its render`
        : RENDERING ? `a render is running now (run ${RENDERING})` : true,
      cli: o => `cd ${shq(checkoutOf(o))} && sd-research-kit render`, sends: o => `POST /api/research/render {"repo": "${rootOf(o).render}"}`,
      run: o => window.shell.post('/api/research/render', { repo: rootOf(o).render }).then(run => { RENDERING = run.id; followRender(run, o); return `Rendering ${o.title} · run ${run.id}`; }, refused) },
    // build: the document's own address on this dashboard, not a fixed host.
    { id: 'document.open', on: 'document', label: 'Open document', does: 'Opens the rendered document in a new tab.', key: 'o', icon: 'arrow-up-right', risk: 'safe', primary: o => !(o.stale && rootOf(o).research),
      cli: o => `open ${location.origin}${o.href}`, run: o => { window.open(o.href, '_blank', 'noopener'); return `Opened in a new tab · ${o.title}`; } },
    { id: 'document.pin', on: 'document', label: 'Pin', does: 'Pins the document to the top of its repo; Undo unpins it.', key: 'p', icon: 'pin', risk: 'undo', when: o => o.pinned ? 'already pinned' : storeOff(),
      cli: () => NO_CLI, run: o => view(o, { pinned: true }).then(() => `Pinned ${o.title}`), undo: o => view(o, { pinned: false }) },
    { id: 'document.unpin', on: 'document', label: 'Unpin', does: 'Unpins the document; Undo pins it again.', key: 'p', icon: 'pin-off', risk: 'undo', when: o => !o.pinned ? 'not pinned' : storeOff(),
      cli: () => NO_CLI, run: o => view(o, { pinned: false }).then(() => `Unpinned ${o.title}`), undo: o => view(o, { pinned: true }) },
    // Hiding the shown document moves Details to the next row (the reader's reconcile); Undo brings the row back.
    { id: 'document.hide', on: 'document', label: 'Hide', does: 'Hides the document from the list; Undo shows it again.', key: 'h', icon: 'eye-off', risk: 'undo', bulk: true, when: o => o.hidden ? 'already hidden' : storeOff(),
      cli: () => NO_CLI, run: o => view(o, { hidden: true }).then(() => `Hidden: ${o.title}. The Hidden filter shows it.`), undo: o => view(o, { hidden: false }) },
    { id: 'document.unhide', on: 'document', label: 'Unhide', does: 'Shows the document in the list again; Undo hides it.', key: 'h', icon: 'eye', risk: 'undo', bulk: true, when: o => !o.hidden ? 'not hidden' : storeOff(),
      cli: () => NO_CLI, run: o => view(o, { hidden: false }).then(() => `Unhidden: ${o.title}`), undo: o => view(o, { hidden: true }) },
    TAG,
    { id: 'document.untag', on: 'document tag', label: 'Untag', does: 'Removes this tag from the document; Undo puts it back.', key: 'u', icon: 'x', risk: 'undo', when: storeOff, cli: () => NO_CLI,
      run: o => view(o.doc, { tags: o.doc.tags.filter(x => x !== o.tag) }).then(() => `Removed tag ${o.tag}`),
      undo: o => view(o.doc, { tags: o.doc.tags }) },
    { id: 'document.skip', on: 'document', label: 'Disable render', does: 'Gives you the documents.conf line that hides this document from the list.', key: 's', icon: 'ban', risk: 'safe',
      when: () => `${NO_SWITCH}, and skip|<key>|<file> in documents.conf hides a document from this list without stopping its render`,
      cli: o => `no CLI: proposes skip|${o.key}|${o.file} in documents.conf` },
    { id: 'document.skip-repo', on: 'document', label: 'Disable render for repo', does: 'Gives you the documents.conf line that hides this repo\'s documents from the list.', key: 'e', icon: 'ban', risk: 'safe',
      when: () => `${NO_SWITCH}, and skip|<key> in documents.conf hides a root from this list without stopping its render`,
      cli: o => `no CLI: proposes skip|${o.key} in documents.conf` },
    REQUEST,
    ...ROOT_CMDS,
    ...PUBLISH_CMDS,
  );
}
document.addEventListener('shell:open', e => { if (DOCS.some(d => d.id === e.detail)) select(e.detail, true); });
document.addEventListener('shell:view', e => {
  if (e.detail.id !== 'documents' || !DOC) return;
  const id = DOCS.some(d => d.id === selected) && ordered().some(d => d.id === selected) ? selected : shownPage()[0]?.id;
  if (id) show(id); else unselect();
});
document.addEventListener('publish:run', e => { if (selected === e.detail.key) show(selected); });
document.addEventListener('shell:picked', e => { picked = e.detail; tbody.querySelectorAll('tr[data-id]').forEach(tr => tr.toggleAttribute('data-picked', picked.includes(tr.dataset.id))); });

// ---------- Details ----------
const details = document.getElementById('details');
function show(id) {
  const d = DOCS.find(x => x.id === id); if (!d) return;
  selected = id;
  tbody.querySelectorAll('tr[data-id]').forEach(tr => tr.setAttribute('aria-selected', tr.dataset.id === id));
  const root = rootOf(d);
  put(details, html`<p class="kind">${d.stale ? html`<span class="g-caution" aria-hidden="true">▲</span> render-stale · ` : d.src ? '' : html`<span class="g-unknown" aria-hidden="true">▨</span> render unknown · `}${d.kind} · ${LABEL[d.key] || d.key}</p>
    <h2>${d.title}</h2>
    <div class="preview-doc" aria-label="Preview">
      <p class="ttl">${d.h1 || d.title}</p>${d.h1 ? html`<p class="h1">&lt;title&gt; ${d.title}</p>` : ''}
      ${d.desc ? html`<p>${d.desc}</p>` : html`<p>No stand line in the page.</p>`}
      <p class="src">Read from the file's &lt;h1&gt; and stand line. The document opens whole, as its author rendered it.</p>
    </div>
    <h3>Act</h3><div id="act">${shell.commands.bar(d.id)}</div>
    <p class="why">Disable render has no switch yet: <code>SD_SKIP_RENDER</code> skips every render.</p>
    <h3>Facts</h3>
    <dl><dt>File</dt><dd>${d.file}</dd><dt>Root</dt><dd>${root.path}</dd><dt>Size</dt><dd>${d.bytes.toLocaleString()} bytes</dd>
      <dt>Modified</dt><dd><time class="rel" datetime="${d.mod}"></time></dd><dt>Source</dt><dd>${d.src ? d.src : '— none matched'}</dd>
      <dt>Render</dt><dd>${d.stale ? html`<span class="g-caution">▲</span> source newer than page` : d.src ? html`<span class="g-ok">●</span> rendered after source` : html`<span class="g-unknown">▨</span> unknown: no source Markdown to compare`}</dd>
      <dt>Label</dt><dd>${LABEL[d.key] || d.key}${root.research ? ' · research repo' : ''}</dd></dl>
    ${confNote(root) ? html`<p class="why">${confNote(root)}.</p>` : ''}
    ${window.commands.publishPane('document', d)}
    <h3>Root</h3><div>${shell.commands.bar(`root:${d.key}`)}</div>
    ${root.file_label ? html`<p class="why">documents.conf still holds <code>label|${d.key}|${root.file_label}</code>, which is not read (sd:3986): Rename root sets the name here.</p>` : ''}
    <h3>Tags</h3>
    <div class="tagedit" id="tagedit">${d.tags.map(t => { const tid = `tag:${d.id}:${t}`; shell.commands.put({ id: tid, type: 'document tag', label: `${t} on ${d.title}`, doc: d, tag: t });
      return html`<button class="chip" type="button" data-cmd="document.untag" data-obj="${tid}" aria-label="Untag ${t}">${t}${I('x')}</button>`; })}
      ${STORE ? '' : html`<label class="sr" for="tag-in">Tag</label><input id="tag-in" placeholder="Type a tag, Enter tags" autocomplete="off">`}</div>
    ${STORE ? html`<p class="why">Tags were not read: ${STORE}.</p>` : ''}`);
  // build: with no store read there is no tag field; the reason stands where the field was.
  shell.commands.select(d.id);
  shell.setContext(d.file);
  shell.suggest([`Summarise ${d.title} in five lines`, `What changed since the previous ${LABEL[d.key] || d.key} render?`, 'Which documents here have not been opened in a month?']);
  details.setAttribute('data-swap', ''); requestAnimationFrame(() => details.removeAttribute('data-swap'));
  // Enter in the tag field tagged this document; the reread drew a new field, so focus goes back to it.
  if (tagFocus) { tagFocus = false; document.getElementById('tag-in')?.focus(); }
}
function select(id, open) { show(id); writeURL(); if (open) shell.openPane('tab-details'); }
// The URL carries facets, search, sort, page and the row, as on Tasks and Reports, so a filtered list can be bookmarked or sent.
// Lists join with commas; defaults are left out. Unknown values are dropped on read.
// sd:5484: the same query, less the page, is kept in localStorage per page, as the shell keeps a page's view and its folds;
// an address with none of the page's own params opens on it. A blocked store keeps the filters for this visit only.
const SORTS = Object.keys(cmp), KEEP = `sd.filters.${location.pathname}`;
function readURL() {
  const own = new URLSearchParams(location.search); own.delete('row');
  let kept = ''; if (!own.toString()) try { kept = localStorage.getItem(KEEP) || ''; } catch { /* blocked: nothing kept */ }
  const q = kept ? new URLSearchParams(kept) : own, list = k => (q.get(k) || '').split(',').filter(Boolean);
  F.repo = new Set(list('repo').filter(v => ROOTS.some(r => r.key === v)));
  F.kind = new Set(list('kind').filter(v => KINDS.includes(v)));
  F.fresh = new Set(list('fresh').filter(v => v in FRESH));
  F.dest = new Set(list('dest').filter(v => v in DEST));
  F.text = (q.get('q') || '').trim().toLowerCase(); F.pinned = q.get('pinned') === '1'; F.hidden = window.shell.aside.mode('hidden', q);
  // A page is decimal digits: ?page=1.5 sliced mid-page and pressed no pager button, and Number('0x2') is 2 (sd:2427).
  window.shell.list.listParams(q, L, { sorts: SORTS, size: LIST.size });
  const [col, dir] = (q.get('sort') || '').split('.'); if (dir && SORTS.includes(col)) { L.sort = col; L.dir = dir === 'asc' ? 1 : -1; } // an older link's ?sort=col.dir
  if (F.text) input.value = F.text;
}
function writeURL() {
  const q = new URLSearchParams();
  ['repo', 'kind', 'fresh', 'dest'].forEach(k => { if (F[k].size) q.set(k, [...F[k]].join(',')); });
  if (F.text) q.set('q', F.text); if (F.pinned) q.set('pinned', '1'); const hp = window.shell.aside.param(F.hidden); if (hp) q.set('hidden', hp);
  window.shell.list.listQuery(q, L, LIST);
  // Only once the first read has restored the filters: a draw before it would keep an empty query over the kept one.
  if (linked) { const k = new URLSearchParams(q.toString()); k.delete('page'); try { localStorage.setItem(KEEP, k.toString()); } catch { /* blocked */ } }
  shell.url(q); // the shell keeps ?row=
}
// build: no proposal card. The reference drafted a documents.conf line in chat; neither line it proposed stops a render.
// The tag field feeds the Tag command: Enter runs Tag on this document with the typed tag.
details.addEventListener('keydown', e => {
  if (e.target.id !== 'tag-in' || e.key !== 'Enter') return;
  e.preventDefault();
  const d = DOCS.find(x => x.id === selected);
  if (!e.target.value.trim() || !d || TAG.when(d) !== true) return;
  tagFocus = true; shell.commands.run({ ...TAG, fields: null }, d);
});

// ---------- Shapeshift: search + filters, or a request ----------
const input = document.getElementById('shift'), prev = document.getElementById('shift-preview'), as = document.getElementById('shift-as'), ghost = document.getElementById('ghost');
const chipsEl = document.getElementById('shift-chips'), reqEl = document.getElementById('req');
let mode = 'search', lastGuess = 'search', streak = 0;
// build: repo: names a root by its key or label exactly, else by a prefix only one key has. The design took the first
// prefix match, and an unknown name fell back to the selected row's repository: a request for the wrong repository.
function repoOf(name) {
  const n = name.toLowerCase(), keys = ROOTS.map(r => r.key);
  const exact = keys.find(k => k.toLowerCase() === n || (LABEL[k] || '').toLowerCase() === n);
  if (exact) return { key: exact };
  const some = keys.filter(k => k.toLowerCase().startsWith(n));
  if (some.length === 1) return { key: some[0] };
  return { why: some.length ? `repo:${name} matches ${some.join(', ')}: name one` : `repo:${name} names no document root` };
}
function parse(v) {
  const out = { repo: [], kind: [], fresh: [], pinned: false, hidden: false, words: [], due: '', skill: '', repoError: '' };
  v.split(/\s+/).filter(Boolean).forEach(t => {
    let m;
    if ((m = t.match(/^repo:(.+)$/i))) { const k = repoOf(m[1]); if (k.key) out.repo.push(k.key); else if (mode === 'request') out.repoError ||= k.why; else out.words.push(t); }
    else if ((m = t.match(/^kind:(\w+)$/i)) && KINDS.includes(m[1].toLowerCase())) out.kind.push(m[1].toLowerCase());
    else if (/^(stale|is:stale)$/i.test(t)) out.fresh.push('stale');
    else if ((m = t.match(/^age[:>](today|week|older|\d+d)$/i))) out.fresh.push(/^\d+d$/.test(m[1]) ? (parseInt(m[1]) > 7 ? 'older' : 'week') : m[1].toLowerCase());
    else if (/^is:pinned$/i.test(t)) out.pinned = true;
    else if (/^is:hidden$/i.test(t)) out.hidden = true;
    else if ((m = t.match(/^due:(\S+)$/i))) out.due = m[1];
    // build: the hint says to name a skill with skill:, so a request reads it; search leaves it a word.
    else if ((m = t.match(/^skill:([\w.-]+)$/i)) && mode === 'request') out.skill = m[1];
    else if (KINDS.includes(t.toLowerCase()) && out.kind.length === 0 && mode === 'request') out.kind.push(t.toLowerCase());
    else out.words.push(t);
  });
  return out;
}
function guess(v) { return /^(request|need|write|generate|make)\b/i.test(v) ? 'request' : 'search'; }
const SKILL = { research: 'sd-research-repo', design: 'hallmark', report: 'choose', dashboard: 'choose' };
function renderShift() {
  const v = input.value.trim();
  prev.hidden = !v;
  prev.querySelectorAll('[data-as]').forEach(c => c.setAttribute('aria-pressed', c.dataset.as === mode));
  ghost.textContent = v ? `→ ${mode}` : '';
  const p = parse(v);
  if (mode === 'search') {
    put(as, html`${I('filter')} ${p.words.length ? `search “${p.words.join(' ')}”` : 'filter the list'}`);
    put(chipsEl, html`${[...p.repo.map(r => `repo:${r}`), ...p.kind.map(k => `kind:${k}`), ...p.fresh.map(f => f === 'stale' ? 'is:stale' : `age:${f}`), p.pinned ? 'is:pinned' : '', p.hidden ? 'is:hidden' : ''].filter(Boolean).map(c => html`<span class="chip" aria-pressed="true">${c}</span>`)}`);
    reqEl.hidden = true;
    F.text = p.words.join(' ').toLowerCase();
    F.repo = new Set(p.repo); F.kind = new Set(p.kind); F.fresh = new Set(p.fresh); F.pinned = p.pinned; if (p.hidden) F.hidden = 'aside';
    L.page = 1; render();
  } else {
    const title = p.words.filter((w, i) => !(i === 0 && /^(request|need|write|generate|make)$/i.test(w))).join(' ').replace(/^(a|an)\s+/i, '');
    // A write names where it runs: repo:, else the row selected when typing began, never a silent default.
    if (p.repoError) {
      put(as, html`${I('plus')} request “${title || '…'}”`);
      put(chipsEl, html`<span class="chip" aria-pressed="false">repo: none</span>`); reqEl.hidden = false;
      Object.assign(DRAFT, { title, repo: null, checkout: '', cmd: '', repoError: p.repoError });
      put(reqEl, html`<p class="why">${p.repoError}. The request goes nowhere until <code>repo:</code> names one root.</p>`);
      F.text = ''; render(); return;
    }
    DRAFT.repoError = '';
    const repo = p.repo[0] || reqScope;
    if (!repo) {
      put(as, html`${I('plus')} request “${title || '…'}”`);
      put(chipsEl, html`<span class="chip" aria-pressed="false">repo: none</span>`); reqEl.hidden = false;
      Object.assign(DRAFT, { title, repo: null, checkout: '', cmd: '' });
      put(reqEl, html`<p class="why">No document was selected when you started typing. Add <code>repo:&lt;name&gt;</code> to say which repository gets the request.</p>`);
      F.text = ''; render(); return;
    }
    const kind = p.kind[0] || (ROOTS.find(r => r.key === repo)?.research ? 'research' : 'report');
    const skill = p.skill || SKILL[kind];
    put(as, html`${I('plus')} request “${title || '…'}”`);
    put(chipsEl, html`${[`repo:${repo}`, `kind:${kind}`, `skill:${skill}`, 'budget:30m', p.due ? `due:${p.due}` : ''].filter(Boolean).map(c => html`<span class="chip" aria-pressed="true">${c}</span>`)}`);
    reqEl.hidden = false;
    const checkout = ROOTS.find(r => r.key === repo)?.checkout || '';
    Object.assign(DRAFT, { title, repo, checkout, kind, skill, due: p.due,
      cmd: `sd task add ${shq('Document request: ' + title)}${checkout ? ` --belongs-to ${shPath(checkout)}` : ''} --body ${shq(`repo=${repo} kind=${kind} skill=${skill}`)}${p.due ? ` --due ${shq(p.due)}` : ''} --priority 3` });
    put(reqEl, html`<p class="why">Request creates an item for ${repo}; Undo closes it. ${skill === 'choose' ? 'No skill matches this kind yet; name one with skill:.' : `The item names ${skill}.`}</p>
      <div class="actions">${shell.commands.rowActions(DRAFT.id)}</div>`);
    F.text = ''; render();
  }
}
// The request's repository is the row selected when typing began. Search may move or clear the selection while the
// viewer types; that must never change where a write goes.
let reqScope = null;
const scopeNow = () => (selected && DOCS.find(d => d.id === selected)?.key) || null;
input.addEventListener('beforeinput', () => { if (!input.value.trim()) reqScope = scopeNow(); });
input.addEventListener('input', () => {
  const g = guess(input.value.trim());
  if (g === lastGuess) streak++; else { lastGuess = g; streak = 1; }
  if (streak >= 2 || !input.value.trim()) mode = g; // change the reading only when it wins twice in a row
  renderShift();
});
input.addEventListener('keydown', e => {
  if (e.key === 'ArrowRight' && input.selectionStart === input.value.length && input.value) { e.preventDefault(); mode = mode === 'search' ? 'request' : 'search'; renderShift(); }
  if (e.key === 'Enter') { e.preventDefault(); if (mode === 'request') { if (DRAFT.title && DRAFT.repo) shell.commands.run(REQUEST, DRAFT); } else prev.hidden = true; }
  if (e.key === 'Escape' && input.value) { e.stopPropagation(); input.value = ''; mode = 'search'; renderShift(); }
});
prev.addEventListener('click', e => {
  const c = e.target.closest('[data-as]'); if (c) { mode = c.dataset.as; renderShift(); input.focus(); return; }
});
document.addEventListener('keydown', e => {
  if (e.target.matches('input, textarea, [contenteditable]') || e.metaKey || e.ctrlKey || e.altKey || document.querySelector('dialog[open]') || e.target.closest('.actmenu')) return;
  if (e.key === '/') { e.preventDefault(); input.focus(); }
});
// The shell walks these rows on j / k.
window.PAGE_KEYS = [['/', 'Search documents or request one'], ['→', 'In the field: search or request']];
window.PAGE_LIST = { rows: () => tbody.querySelectorAll('tr[data-id]'), current: () => selected, select: id => select(id, false) };

window.PAGE_COMMANDS = [
  { label: 'Request a document', icon: 'plus', run: () => { reqScope = scopeNow(); input.value = 'request '; mode = 'request'; renderShift(); input.focus(); } },
  { label: 'Show render-stale documents', icon: 'clock', run: () => { clearAll(); F.fresh.add('stale'); render(); } },
  { label: 'List hidden documents', icon: 'eye-off', run: () => { F.hidden = 'aside'; L.page = 1; render(); } },
];

// ---------- Start (build: read /api/documents through shell.read, then draw) ----------
// The reader (read.js, sd:2418) holds the guards: of overlapping reads only the newest draws, a document the read no longer
// lists runs no command, and a failed read clears the rows, the rail and Details.
// The badge is the read's own (sd:3446, documents_screen._attention): render-stale documents the operator has not hidden.
function attention() {
  window.PAGE_ATTENTION = DOC && DOC.attention || { state: 'unknown', n: 0, what: 'documents not read' };
  window.shell.attention?.(window.PAGE_ATTENTION);
}
const shownPage = () => ordered().slice((L.page - 1) * L.size, L.page * L.size);
let reading = null, linked = false;
function adopt(doc) {
  DOC = doc;
  OBSERVED = Date.parse(DOC.read) || Date.now();
  document.body.dataset.observed = DOC.read; // shell time cells count from the reading, not from now
  ROOTS = DOC.roots || []; LABEL = Object.fromEntries(ROOTS.map(r => [r.key, r.label])); STORE = DOC.store?.available ? '' : (DOC.store?.reason || 'the view store was not read');
  DOCS = (DOC.documents || []).map(r => ({ id: `${r.key}/${r.file}`, type: 'document', label: r.title || r.file, key: r.key, file: r.file, href: r.href,
    bytes: r.bytes, mod: r.modified, kind: r.kind, src: r.src, stale: !!r.stale, title: r.title || r.file, h1: r.h1, desc: r.desc, tags: r.tags || [], pinned: !!r.pinned, hidden: !!r.hidden, pub: r.pub || null }));
  if (!linked) {
    // The address is read before the first draw: that draw's reconcile selects the first shown row, and selecting rewrites
    // ?row=, so a link to any other row would be lost. A linked row the filters show moves the list to its page.
    linked = true;
    const q = shell.row();
    readURL();
    const at = ordered().findIndex(d => d.id === q);
    if (at >= 0) { L.page = Math.floor(at / L.size) + 1; selected = q; }
  }
  const contested = DOC.contested || [], unread = shownRoots().filter(confNote);
  // build: a key two checkouts claim is served by neither (documents.py, contested); the state slot names each one.
  const state = !ROOTS.length && !contested.length ? { kind: 'empty', title: 'No document roots', text: `No documents found. Publish into ${DOC.published} in a repo under ${DOC.base}, or add a root| line to ${DOC.config}.`, source: DOC.config }
    : contested.length ? { kind: 'partial', text: `${contested.map(c => `${plural(c.paths.length, 'repo')} use the name ${c.key} (${c.paths.join(', ')}), so none shows`).join('; ')}. Name the one you mean with a root| line in ${DOC.config}.`, source: DOC.config }
    // build: an unread view store pins and hides nothing, so every document shows; the slot says so (sd:3428).
    : STORE ? { kind: 'partial', text: `Pins, hidden documents and tags were not read: ${STORE}. Every document shows.`, source: '/api/documents' }
    : unread.length ? { kind: 'partial', text: `${unread.map(r => `${r.label}: ${confNote(r)}`).join('; ')}.`, source: '/api/documents' }
    : null;
  const roots = ROOTS.map(r => ({ id: `root:${r.key}`, type: 'document root', label: r.label, key: r.key, hidden: !!r.hidden,
    derived: r.derived || r.label, set: r.set || null }));
  return { objects: [...DOCS, ...roots, DRAFT], state };
}
function unselect() {
  selected = null;
  put(details, html`<p class="why">${!DOC ? 'The documents were not read, so nothing is selected.'
    : !DOCS.length ? 'No document is published yet, so nothing is selected.' : 'No document matches these filters. Clear them to see the list.'}</p>`);
}
// Only the newest of overlapping loads draws; a view write rereads through `view`.
const load = () => reading.load();
// Read again (sd:5638): the shell's head button reads the documents again.
window.PAGE_AGAIN = { title: 'Read the documents again', run: load };
document.addEventListener('DOMContentLoaded', () => {
  registerCommands();
  reading = shell.read({
    source: '/api/documents', what: 'the documents', adopt,
    clear: () => { DOC = null; ROOTS = []; LABEL = {}; DOCS = []; delete document.body.dataset.observed; },
    draw: () => { render(); attention(); },
    current: () => selected,
    first: () => shownPage()[0]?.id,
    select: id => select(id, false),
    unselect,
    active: onDocs,
  });
  load();
});
