// Briefs (sd:2112): the design source's products/system/designs/v2/briefs.js at b2b8c2b, ported. Each change from the reference
// is marked "build:". The rows are /api/briefs (briefs_screen.py): the routine briefs the workflow database holds (sd:3459), never
// sample data. Read opens a brief's item page, which renders its Markdown. The design reads brief mail; no mail reader exists, so
// unread state, follow-up flags and the watchdog's failures are left out, and the Missed lamp is unknown with the document's
// reason. The one write is Make task, which opens the shell's capture form; the form files it with POST /api/items.
const { html, put, plural, cells } = window.markup;
// build: nothing here can be loud. The watchdog is not read, so the page claims nothing for the rail or Today.
window.PAGE_ATTENTION = { state: 'unknown', n: 0, what: 'watchdog not read' };
window.PAGE_COMMANDS = [];
// sd:5080: the list and the cadence strip are the shell's views (shell.js, view:start). The Range chips sit in each view's
// head and set one range for both; a tick or a lane shows the list with that brief or that source.
// system sd:5778: Topics is the second view, one row per research topic (briefs_screen.topics).
window.PAGE_VIEWS = [{ id: 'list', label: 'Briefs', key: 'b' }, { id: 'topics', label: 'Topics' }, { id: 'cadence', label: 'Cadence' }];
// The shell's Shapeshift bar (sd:5539) filters the list through shell:filter, and the cadence fallback table itself.
window.PAGE_FILTER = { list: 'subject, source and first lines' };
addEventListener('DOMContentLoaded', () => {
  const shell = window.shell, C = shell.commands;
  const I = n => html`<svg class="i" aria-hidden="true"><use href="#i-${n}"/></svg>`;
  // build: capture files through the shell's default capture (sd:3520): POST /api/items, as Today and Tasks do.

  // ---------- Data (build) ----------
  // BRIEFS: { id (the stem), item, subj, src, day, at (null when not known), words, lead, rel, open, copy }. rel and open are
  // null unless the last render vouches for the vault copy, and copy says why. READ: when the server read them.
  // TOPICS: briefs_screen.topics, { state, reason, rows: [{ item, title, slug, stage, memory, memory_lines, sources }] }.
  let BRIEFS = [], SOURCES = [], READ = null, READER = null, WATCH = '', COPIES = null, LOADED = false, TOPICS = null;
  const DAY_MS = 864e5;
  const RANGES = { '24h': 1, '7d': 7, '30d': 30 };
  // build: the reference had one fixed week of sample mail; the range here is the last 1, 7 or 30 days before the read.
  let range = '7d';
  const days = () => { const end = Date.parse((READ || new Date().toISOString()).slice(0, 10) + 'T00:00:00Z');
    return Array.from({ length: RANGES[range] }, (_, i) => new Date(end - (RANGES[range] - 1 - i) * DAY_MS).toISOString().slice(0, 10)); };
  let DAYS = [];
  const inRange = b => b.day >= DAYS[0] && b.day <= DAYS[DAYS.length - 1];
  const { dayLabel } = window.briefDetails;
  const hhmm = iso => iso ? iso.slice(11, 16) : '–';
  const srcOf = id => SOURCES.find(s => s.id === id);
  const ago = iso => { const m = Math.round((Date.parse(READ) - Date.parse(iso)) / 60000); return m < 60 ? `${Math.max(m, 0)}m ago` : m < 2880 ? `${Math.round(m / 60)}h ago` : `${Math.round(m / 1440)}d ago`; };

  // ---------- State (mirrored in the URL) ----------
  // build: no kind or follow-up filter; no reader gives either.
  const F = { src: new Set(), day: '', q: '' };
  let page = 1, size = 50, selected = null;
  function readURL() {
    const p = new URLSearchParams(location.search);
    // A source is the free end of a file name and may hold a comma, so each one is its own `src` (review 39d6c524a35c).
    p.getAll('src').filter(Boolean).forEach(v => F.src.add(v));
    F.day = /^\d{4}-\d{2}-\d{2}$/.test(p.get('day') || '') ? p.get('day') : ''; F.q = p.get('q') || '';
    range = RANGES[p.get('range')] ? p.get('range') : '7d';
    page = Math.max(1, Math.floor(+p.get('page')) || 1); size = [25, 50, 100, 200].includes(+p.get('size')) ? +p.get('size') : 50;
  }
  function writeURL() {
    const q = [...F.src].map(s => ['src', s]);
    if (F.day) q.push(['day', F.day]);
    if (F.q) q.push(['q', F.q]);
    if (range !== '7d') q.push(['range', range]);
    if (page > 1) q.push(['page', String(page)]);
    if (size !== 50) q.push(['size', String(size)]);
    shell.url(q); // pairs, so a source repeats; the shell keeps ?row=
  }
  const active = () => F.src.size + (F.day ? 1 : 0) + (F.q ? 1 : 0);
  const match = b => inRange(b) && (!F.src.size || F.src.has(b.src)) && (!F.day || b.day === F.day) &&
    (!F.q || `${b.subj} ${b.src} ${b.lead}`.toLowerCase().includes(F.q.toLowerCase()));
  const ranged = () => BRIEFS.filter(inRange);

  // ---------- Annunciator ----------
  // build: three lamps. The reference's follow-up, quiet-days and Slack lamps read mail and the watchdog's digest; no reader
  // supplies either here. Missed is unknown with the reason, never a count.
  // A capped read holds the newest rows only, so a count reaching past its oldest day is a floor, not a total.
  // The reader says capped only when it cut rows; a skipped row is not a cut, so total > shown does not decide it.
  const capped = () => !!READER && !!READER.capped;
  const oldest = () => BRIEFS.reduce((m, b) => b.day < m ? b.day : m, '9999-12-31');
  const LAMP = { ok: 'ok', off: 'ok', failed: 'caution', unknown: 'unknown' };
  // sd:5298: a failed or unknown copies lamp links to its evidence, Health's Brief copies row (the refused stems, the reason).
  const COPIES_ROW = '/health?row=mir%3Abriefs#view=jobs';
  function renderAnnunciator() {
    const c = COPIES || { state: 'unknown', reason: 'not read', at: null };
    const n = ranged().length, srcs = new Set(ranged().map(b => b.src)).size;
    // No read behind the lamp is unknown, never ok: a refused or failed read is not zero briefs.
    const got = !!READER && READER.state !== 'error', floor = capped() && BRIEFS.length && oldest() >= DAYS[0];
    const count = got ? html`<span class="ph">${floor ? 'at least ' : ''}<b>${n}</b> in ${range === '24h' ? '24 hours' : `${RANGES[range]} days`}</span> · <span class="ph">${plural(srcs, 'source')}</span>` : html`<span class="ph"><b>not read</b></span>`;
    const read = READ ? html`<span class="ph"><b>${hhmm(READ)}</b> UTC</span> · <span class="ph">${dayLabel(READ.slice(0, 10))}</span>` : html`<span class="ph"><b>not yet</b></span>`;
    put(document.getElementById('annunciator'), cells([
      { state: got ? 'ok' : 'unknown', label: 'Briefs', mark: I('inbox'), val: count },
      { state: 'unknown', attrs: html` title="${WATCH}"`, label: 'Missed', mark: I('siren'), val: html`<span class="ph"><b>unknown</b></span> · <span class="ph">no watchdog reader</span>` },
      // build (sd:3459): the brief-copies heartbeat. Off is quiet; failed is caution, since Read never needs a copy.
      { state: LAMP[c.state] || 'unknown', href: LAMP[c.state] === 'ok' ? undefined : COPIES_ROW, attrs: html` title="${c.reason || `rendered ${c.at}`}"`, label: 'Vault copies', mark: I('hard-drive-download'),
        val: html`<span class="ph"><b>${c.state}</b></span>${c.at ? html` · <span class="ph">${c.at.slice(5, 16).replace('T', ' ')} UTC</span>` : ''}` },
    ]));
  }

  // ---------- Cadence strip (hand-rolled SVG, patterns.md § Charts) ----------
  const W = 700, H = 24;
  // build: a tick sits at its time; a brief with no known time sits at noon of its day.
  const x = b => { const t0 = Date.parse(DAYS[0] + 'T00:00:00Z'), span = DAYS.length * DAY_MS;
    return ((Date.parse(b.at || b.day + 'T12:00:00Z') - t0) / span) * W; };
  const RANGE_HOSTS = () => ['range', 'range-cad'].map(id => document.getElementById(id));
  function renderRange() {
    RANGE_HOSTS().forEach(el => put(el, html`${Object.keys(RANGES).map(r => html`<button class="chip" type="button" data-r="${r}" aria-pressed="${String(r === range)}">${r}</button>`)}`));
  }
  // 7d, not 24h, is this view's default: most sources write once a day, so 24h shows one tick per lane and no cadence.
  RANGE_HOSTS().forEach(el => el.addEventListener('click', e => {
    const c = e.target.closest('.chip'); if (!c || !RANGES[c.dataset.r]) return;
    range = c.dataset.r; DAYS = days(); if (F.day && !DAYS.includes(F.day)) F.day = ''; page = 1; renderRange(); update();
  }));
  function renderLanes() {
    const step = W / DAYS.length, label = DAYS.length <= 7 ? 1 : 5;
    const grid = html`${DAYS.map((d, i) => html`<line x1="${(i * step).toFixed(1)}" x2="${(i * step).toFixed(1)}" y1="0" y2="${H}"/>`)}<line x1="${W}" x2="${W}" y1="0" y2="${H}"/>`;
    const lanes = SOURCES.map(s => {
      const mine = ranged().filter(b => b.src === s.id);
      const btn = html`<button class="lane-btn" type="button" data-src="${s.id}" aria-pressed="${String(F.src.has(s.id))}"><span class="nm">${s.id}</span><span class="n">${mine.length}</span></button>`;
      const ticks = mine.map(b => html`<rect class="tick${b.id === selected ? ' sel' : ''}" data-id="${b.id}" x="${(x(b) - 1.5).toFixed(1)}" y="4" width="3" height="${H - 8}"><title>${b.subj} · ${b.day} ${b.at ? hhmm(b.at) + 'Z' : 'time not known'}</title></rect>`);
      return html`<div class="lane">${btn}<div class="track"><svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img" aria-label="${s.id}: ${plural(mine.length, 'brief')} in ${DAYS.length} days"><g class="grid">${grid}</g>${ticks}</svg></div></div>`;
    });
    const axis = html`<div class="lane"><span class="axis axis-label"></span><div class="axis"><svg viewBox="0 0 ${W} 16" preserveAspectRatio="none" aria-hidden="true">${DAYS.map((d, i) => i % label ? '' : html`<text x="${(i * step + 4).toFixed(1)}" y="12">${d.slice(8)}</text>`)}</svg></div></div>`;
    put(document.getElementById('lanes'), html`${lanes}${axis}`);
    // Table fallback: counts per source per day.
    put(document.getElementById('cad-table'), html`<table><caption class="sr">Briefs per source and day</caption><thead><tr><th scope="col">Source</th>${DAYS.map(d => html`<th scope="col">${d.slice(5)}</th>`)}<th scope="col">Total</th></tr></thead><tbody>${SOURCES.map(s => html`<tr><td>${s.id}</td>${DAYS.map(d => html`<td>${BRIEFS.filter(b => b.src === s.id && b.day === d).length}</td>`)}<td>${ranged().filter(b => b.src === s.id).length}</td></tr>`)}</tbody></table>`);
  }
  document.getElementById('lanes').addEventListener('click', e => {
    const t = e.target.closest('rect.tick'); if (t) { shell.view.show('list'); return select(t.dataset.id, true); }
    const b = e.target.closest('.lane-btn'); if (!b) return;
    const s = b.dataset.src;
    F.src.has(s) ? F.src.delete(s) : F.src.add(s); page = 1; update(); shell.view.show('list');
    if (F.src.has(s)) showSource(s);
  });

  // ---------- Topics (system sd:5778) ----------
  // A brief names its signal topics only once pack sd:5798 copies them into its record (`topics`, else null). A brief with
  // null is not recorded, never quiet: with none recorded, every topic's signal says so instead of a count.
  function renderTopics() {
    // A failed brief read says nothing about the briefs' records: signal is not read. A range whose briefs record no topics
    // is not recorded, though an older brief does; a count of none needs every brief in the range to record its topics.
    const rows = TOPICS?.state === 'read' ? TOPICS.rows : [], recorded = BRIEFS.filter(b => Array.isArray(b.topics));
    const unread = !READER || READER.state === 'error';
    const unrecorded = ranged().filter(b => !Array.isArray(b.topics)).length;
    const lines = (n, none, one, many) => n === null || n === undefined ? html`<span class="u">no ${none} section</span>` : n ? plural(n, one) : many;
    put(document.getElementById('topic-rows'), html`${rows.map(t => {
      const mine = recorded.filter(b => t.slug && b.topics.includes(t.slug));
      const last = mine.reduce((m, b) => b.day > m ? b.day : m, ''), on = new Set(mine.filter(inRange).map(b => b.day)).size;
      const days = on ? plural(on, 'day') : unrecorded ? html`<span class="u">not recorded</span>` : `none in ${range === '24h' ? '24 hours' : `${RANGES[range]} days`}`;
      const signal = unread ? html`<td class="u">not read</td><td class="u">not read</td>`
        : recorded.length ? html`<td>${last ? html`<time datetime="${last}">${last.slice(5)}</time>` : 'none read'}</td><td>${days}</td>`
        : html`<td class="u">not recorded</td><td class="u">not recorded</td>`;
      return html`<tr data-topic="${t.title}"><td class="what"><a href="${shell.itemHref(t.item)}">${t.title}</a></td><td>${t.slug || '–'}</td><td>${t.stage || '–'}</td>${signal}<td>${t.memory || (t.memory_lines === 0 ? 'empty' : lines(t.memory_lines, 'Memory'))}${t.memory_lines > 1 ? html`<small>${plural(t.memory_lines, 'line')}</small>` : ''}</td><td class="r">${lines(t.sources, 'Sources', 'source', 'empty')}</td><td class="act">${C.rowActions(`topic:${t.item}`)}</td></tr>`;
    })}`);
    const note = TOPICS?.state === 'error' ? `The topics were not read: ${TOPICS.reason}. Reload retries it.`
      : !TOPICS ? '' : !rows.length ? 'The workflow database holds no topic row.'
      : unread ? 'Signal was not read: the briefs were not read. Reload retries it.'
      : !recorded.length ? 'No brief records its signal topics yet (pack sd:5798), so signal is not recorded, never quiet.'
      : unrecorded ? `${plural(unrecorded, 'brief')} in range ${unrecorded === 1 ? 'records' : 'record'} no signal topics; ${unrecorded === 1 ? 'it counts' : 'they count'} as not recorded, never as quiet.` : '';
    put(document.getElementById('topic-note'), html`${note}`);
    shell.view.count('topics', rows.filter(t => t.stage !== 'retired').length);  // sd:8018: open topics only
  }

  // ---------- Filters ----------
  // build (sd:5158): Source is a dropdown of checkboxes, so any sources at once, and Day a select of the days with a brief;
  // both are shell.list.pick. sd:5463: Activity's bar (shell.list.fbar): the picked sources and the query as removable
  // tokens, then Clear all; the count sits once, on the title line.
  function renderFilters() {
    const L = shell.list, all = ranged();
    const busy = DAYS.slice().reverse().filter(d => all.some(b => b.day === d));
    put(document.getElementById('filters'), L.fbar(html`${L.pick('src', 'Source', SOURCES.map(s => [s.id, s.id, all.filter(b => b.src === s.id).length]), F.src, { multi: true })}${
      L.pick('day', 'Day', busy.map(d => [d, dayLabel(d), all.filter(b => b.day === d).length]), F.day, { all: 'Any day' })}${
      [...F.src].map(s => L.tok('src', s, s))}${F.q ? L.tok('q', '', `“${F.q}”`) : ''}`, { clear: active() }));
  }
  document.getElementById('filters').addEventListener('change', e => shell.list.pickChange(e, F, () => { page = 1; update(); }));
  document.getElementById('filters').addEventListener('click', e => {
    if (e.target.closest('[data-fclear]')) return clearAll();
    const u = e.target.closest('[data-f]'); if (!u) return;
    const { f: k, v } = u.dataset;
    if (k === 'src') F.src.delete(v);
    if (k === 'day') F.day = '';
    if (k === 'q') { F.q = ''; shell.filter(''); }
    page = 1; update();
  });
  function clearAll() { F.src.clear(); F.day = ''; F.q = ''; shell.filter(''); page = 1; update(); }

  // ---------- Ledger ----------
  const tbody = document.getElementById('rows');
  function renderRows() {
    const all = BRIEFS.filter(match);
    const pages = Math.max(1, Math.ceil(all.length / size)); page = Math.min(page, pages);
    const rows = all.slice((page - 1) * size, page * size);
    let lastDay = '';
    // build: no unread dot, bold or ▲: no reader says which brief is unread or wants a follow-up.
    put(tbody, html`${rows.map(b => {
      const head = b.day !== lastDay ? html`<tr class="day" aria-hidden="true"><td colspan="5">${dayLabel(b.day)}</td></tr>` : ''; lastDay = b.day;
      return html`${head}<tr data-id="${b.id}" aria-selected="${String(b.id === selected)}">
        <td class="g"><span class="sr">brief</span></td>
        <td class="what"><button type="button">${b.subj}<small>${b.lead}</small></button></td>
        <td class="src">${b.src}</td>
        <td class="at">${b.at ? html`<time datetime="${b.at}" title="${ago(b.at)}">${b.day.slice(5)} ${hhmm(b.at)}</time>` : html`<span title="time not known: the note changed after its day">${b.day.slice(5)} –</span>`}</td>
        <td class="act">${window.commands.published(b.pub)}${C.rowActions(b.id)}</td></tr>`;
    })}`);
    const empty = document.getElementById('empty');
    empty.hidden = !LOADED || !!all.length;
    // One delegated listener: an onclick= attribute in markup is inline script under the dashboard CSP.
    empty.onclick ??= e => { if (e.target.closest('[data-act="clear"]')) clearAll(); };
    put(empty, html`${all.length || !LOADED ? html`` : active() ? html`No brief matches these filters.${F.q ? ' Press → in the search box to capture it as a task instead.' : ''} <button class="linkbtn" type="button" data-act="clear">Clear all</button>` : html`No brief in this range.`}`);
    const a = all.length ? (page - 1) * size + 1 : 0, z = Math.min(page * size, all.length);
    put(document.getElementById('pager'), html`<span>${a}–${z} of ${all.length}</span>
      <div class="chips" role="group" aria-label="Page">${page > 1 ? html`<button class="chip" type="button" data-p="${page - 1}">${I('chevron-left')}Previous</button>` : ''}${page < pages ? html`<button class="chip" type="button" data-p="${page + 1}">Next${I('chevron-right')}</button>` : ''}</div>
      <div class="chips" role="group" aria-label="Rows per page">${[25, 50, 100, 200].map(n => html`<button class="chip" type="button" data-size="${n}" aria-pressed="${String(n === size)}">${n}</button>`)}</div>`);
    put(document.getElementById('tally'), html`<span>${plural(new Set(ranged().map(b => b.src)).size, 'source')}</span>`);
    // What each destination icon's fill says, the legend Documents draws too (sd:5731).
    put(document.getElementById('list-help'), shell.list.help('', window.commands.pubLegend()));
    shell.view.count('list', all.length);
    document.getElementById('sub').textContent = READER ? (capped() ? `newest ${plural(READER.shown, 'brief')} read` : `All read${READER.skipped ? `, ${READER.skipped} skipped` : ''}`) : 'Reading the briefs…';
  }
  document.getElementById('pager').addEventListener('click', e => {
    const c = e.target.closest('.chip'); if (!c) return;
    if (c.dataset.p) page = +c.dataset.p;
    if (c.dataset.size) { size = +c.dataset.size; page = 1; }
    update();
  });
  tbody.addEventListener('click', e => { if (e.target.closest('.rowact')) return; const tr = e.target.closest('tr[data-id]'); if (tr) select(tr.dataset.id, true); });

  function update() { renderAnnunciator(); renderLanes(); renderTopics(); renderFilters(); renderRows(); writeURL(); }

  // ---------- Details ----------
  const details = document.getElementById('details');
  function swap() { details.setAttribute('data-swap', ''); requestAnimationFrame(() => details.removeAttribute('data-swap')); }
  // sd:5620: a brief's Details and commands are brief-details.js's, which the HOA and Home pages' Briefs views draw too.
  const brief = window.briefDetails.attach({ details, reread: () => briefs.reread(), read: () => READ, watch: () => WATCH });
  function show(id) {
    const b = BRIEFS.find(r => r.id === id); if (!b) return false;
    tbody.querySelectorAll('tr[data-id]').forEach(tr => tr.setAttribute('aria-selected', tr.dataset.id === id));
    document.querySelectorAll('#lanes rect.tick').forEach(r => r.classList.toggle('sel', r.dataset.id === id));
    brief.show(b);
    return true;
  }
  // build: a source has no Act bar. The reference's Retry and Show log ran on the job behind the source; the file name gives a
  // kind, not a job, so there is nothing to run them on.
  function showSource(id) {
    const s = srcOf(id); if (!s) return;
    brief.forget();
    const mine = ranged().filter(b => b.src === id), on = new Set(mine.map(b => b.day)).size;
    put(details, html`<div class="brief-det"><p class="kind">Source · ${mine.length ? 'quiet days are gaps' : 'no brief in range'}</p>
      <h2>${s.id}</h2>
      <dl><dt>Briefs, ${DAYS.length} days</dt><dd>${mine.length} on ${on} of ${DAYS.length} days</dd><dt>Newest</dt><dd>${s.newest}</dd>
        <dt>All read</dt><dd>${s.n}${capped() ? ` of the newest ${READER.shown}` : ''}</dd><dt>Watchdog</dt><dd><span class="g-unknown" aria-hidden="true">▨</span> ${WATCH}</dd></dl>
      <p class="why">A quiet day is a gap, not a fault. Without a watchdog reader the page cannot say whether the job ran and had nothing to write, or failed. The file name gives the kind, not the job, so there is no job to retry from here.</p></div>`);
    C.select(null); shell.suggest([`Why is ${s.id} quiet?`, 'Which jobs missed a run this week?']); shell.openPane('tab-details'); swap();
  }
  function select(id, open) { if (!show(id)) return; selected = id; if (open) shell.openPane('tab-details'); }

  document.addEventListener('publish:run', e => { if (selected === e.detail.key) show(selected); });
  document.addEventListener('adversarial:run', e => { const b = BRIEFS.find(r => r.id === selected); if (b && e.detail.key === `brief:${b.item}`) show(b.id); });

  document.addEventListener('shell:open', e => { if (BRIEFS.some(b => b.id === e.detail)) select(e.detail, true); });

  // ---------- Shapeshift bar (the shell's, sd:5539) ----------
  document.addEventListener('shell:filter', e => { const q = e.detail.q.toLowerCase(); if (q !== F.q) { F.q = q; page = 1; update(); } });
  // The shell walks these rows on j / k and clears the filter on Esc.
  window.PAGE_LIST = { rows: () => tbody.querySelectorAll('tr[data-id]'), current: () => selected, select: id => select(id, false), clear: () => !!active() && (clearAll(), true) };

  // ---------- Start (build: read /api/briefs through shell.read, then draw) ----------
  // The reader (read.js, sd:2418) holds the guards: of overlapping reads only the newest draws, a brief the
  // read no longer lists runs no command, and a selection whose brief is gone moves to the first shown brief or clears.
  // sd:6703: Add topic files a candidate; Remove retires a topic (the brief routines read active ones only), Restore
  // brings a retired one back as a candidate, and Delete removes its row for good. Each runs through /api/actions/run and reads the page again.
  const shq = v => `'${String(v ?? '').replace(/'/g, "'\\''")}'`;
  const ranAgain = (o, action, ids) => window.commands.runAction(o, action, ids).then(said => briefs.reread().then(() => said));
  C.register(
    { id: 'topic.add', on: 'topic list', label: 'Add topic', does: 'Files a candidate topic; you activate it in Tasks.', key: 'a', icon: 'plus', risk: 'safe', executes: true,
      fields: () => [{ name: 'title', label: 'Title', required: true, help: 'Its slug is the title in lower case, words joined by hyphens.' }],
      consequence: () => 'This adds a candidate topic. The brief routines read it once you make it active.',
      cli: (o, v) => `sd store add sdw.topic ${shq(v?.title || 'TITLE')}`, sends: () => 'POST /api/actions/run {action: topic.add, title}',
      run: (o, v) => ranAgain(o, 'topic.add', { title: v?.title || '' }) },
    { id: 'topic.remove', on: 'topic', label: 'Remove', does: 'Retires the topic: no brief routine reads it; its Memory and Sources stay.', key: 'm', risk: 'confirm', executes: true,
      when: o => o.stage !== 'retired' || 'retired already', consequence: o => `This moves ${o.label} to retired. The brief routines read it no more; its Memory and Sources stay, and Delete removes it for good.`,
      cli: o => `no sd verb: the dashboard moves #${o.item} to retired, as Tasks moves it`, sends: o => `POST /api/actions/run {action: topic.remove, item: ${o.item}}`,
      run: o => ranAgain(o, 'topic.remove', { item: o.item }) },
    { id: 'topic.delete', on: 'topic', label: 'Delete', does: 'Deletes the topic\'s row for good, with its Memory and Sources.', key: 'x', risk: 'confirm', executes: true,
      consequence: o => `This deletes ${o.label} (#${o.item}) for good: its body, its Memory and its Sources are lost. A backup and a removal record are made first. Remove keeps them.`,
      cli: o => `sd-db item remove ${o.item} --who dashboard --reason ${shq(`Deleted on the dashboard: sdw.topic ${o.label}`)}`, sends: o => `POST /api/actions/run {action: topic.delete, item: ${o.item}}`,
      run: o => ranAgain(o, 'topic.delete', { item: o.item }) },
    { id: 'topic.restore', on: 'topic', label: 'Restore', does: 'Brings a retired topic back as a candidate; you activate it in Tasks.', key: 'e', risk: 'confirm', executes: true,
      when: o => o.stage === 'retired' || `${o.stage || 'no stage'}: Restore brings back a retired topic`, consequence: o => `This moves ${o.label} from retired to candidate. The brief routines read it once you make it active.`,
      cli: o => `no sd verb: the dashboard moves #${o.item} to candidate, as Tasks moves it`, sends: o => `POST /api/actions/run {action: topic.restore, item: ${o.item}}`,
      run: o => ranAgain(o, 'topic.restore', { item: o.item }) });
  const briefs = shell.read({
    source: '/api/briefs', what: 'the briefs',
    adopt: doc => {
      READ = doc.read; READER = doc.reader || { state: 'error', reason: 'no reader in the answer', total: 0, shown: 0 };
      WATCH = doc.watchdog?.available ? 'read' : (doc.watchdog?.reason || 'no watchdog reader');
      COPIES = READER.copies || null;
      TOPICS = doc.topics || { state: 'error', reason: 'no topics in the answer', rows: [] };
      BRIEFS = READER.state === 'error' ? [] : (doc.briefs || []);
      const by = new Map();
      BRIEFS.forEach(b => { const s = by.get(b.src) || { id: b.src, n: 0, newest: b.day }; s.n++; if (b.day > s.newest) s.newest = b.day; by.set(b.src, s); });
      // build: lanes in name order; the reference's lane order was a hand-made list of its sample jobs.
      SOURCES = [...by.values()].sort((a, b) => a.id.localeCompare(b.id));
      [...F.src].forEach(s => { if (!by.has(s)) F.src.delete(s); });
      DAYS = days(); LOADED = true;
      const source = READER.source || '/api/briefs';
      const state = READER.state === 'error' ? { kind: 'error', text: `The briefs were not read: ${READER.reason}. Reload retries it.`, source }
        : !BRIEFS.length ? { kind: 'empty', title: 'No briefs', text: 'The workflow database holds no routine brief.', source }
        : READER.skipped || capped() ? { kind: 'partial', text: [READER.skipped ? `${plural(READER.skipped, 'row')} skipped: not a brief.` : '', capped() ? `Showing the newest ${READER.shown}; older briefs are in the database. Counts before ${oldest().slice(5)} are not read.` : ''].filter(Boolean).join(' '), source }
        : null;
      const topics = TOPICS.rows.map(t => ({ id: `topic:${t.item}`, type: 'topic', label: t.title, item: t.item, stage: t.stage }));
      return { objects: [...BRIEFS.map(window.briefDetails.object), ...topics, { id: 'topics', type: 'topic list', label: 'Topics' }], state };
    },
    // Nothing from the last read stays on screen: rows, lanes and the observed time.
    clear: err => { READ = null; READER = { state: 'error', reason: err.message, total: 0, shown: 0 }; COPIES = null; BRIEFS = [];
      TOPICS = { state: 'error', reason: err.message, rows: [] }; SOURCES = []; F.src.clear(); LOADED = true; },
    draw: () => { renderRange(); update(); },
    current: () => selected,
    first: () => (BRIEFS.filter(match)[0] || BRIEFS[0])?.id,
    select,
    // No current brief: nothing from an earlier read stays selected, and Details offers no command on it.
    unselect: () => { selected = null; put(details, html`<p class="why">Select a brief to see it.</p>`); },
  });
  readURL();
  briefs.load();
  // Read again (sd:5638): the shell's head button reads the briefs again; sd:6697 dropped the Observed lamp.
  window.PAGE_AGAIN = { title: 'Read the briefs again', run: () => briefs.load() };
});
