// Watches (sd:6264): the store's market rows and the market-watch job that refreshes them (watches_screen.py). The page
// reads /api/watches through the shell's reader and draws the Documents list grammar: a filter bar, a sorted and paged
// ledger, and the one Details pane. Refresh watches (sd:6697) is Run now on the job (commands.retry, /api/jobs/market-watch/retry);
// Refresh watch, Add watch, Remove, Restore and Delete run watch.refresh, watch.add, watch.remove, watch.restore and
// watch.delete through
// /api/actions/run (commands.runAction), and Details shows
// the answer. A refresh date is the profile's own: an active watch past the bound reads stale, never fresh.
(() => {
  const { html, put, plural } = window.markup;
  const watches = n => plural(n, 'watch', 'watches');
  const $ = id => document.getElementById(id);
  const shq = s => `'${String(s ?? '').replace(/'/g, "'\\''")}'`;
  const link = u => /^https?:\/\//i.test(u || '');
  const STAGES = [['active', 'active'], ['candidate', 'candidate'], ['paused', 'paused'], ['declined', 'declined']];
  // What a refresh date is worth (design.md, State grammar): fresh is normal, stale and never ask for attention, and a
  // watch the routine does not refresh is queued.
  const FRESH = { fresh: ['ok', '●', 'fresh'], stale: ['caution', '▲', 'stale'], never: ['caution', '▲', 'no profile yet'], idle: ['queued', '◌', 'not refreshed'] };
  const fresh = r => FRESH[r.fresh] || ['unknown', '▨', r.fresh || 'unknown'];
  const COLS = [[null, html`<span class="sr">Freshness</span>`, 'g'], ['title', 'Watch'], ['kind', 'Kind', 'repo'], ['stage', 'Status', 'kind'],
    ['refreshed', 'Last refresh', 'num'], ['host', 'URL', 'mod'], [null, html`<span class="sr">Actions</span>`, 'acts']];
  let DOC = null, ROWS = [], SELECTED = null;
  // sd:8013: ?stage= opens on one status, as Today's Market Watch link opens the candidates.
  const F = { stage: STAGES.find(([s]) => s === new URLSearchParams(location.search).get('stage'))?.[0] || '', q: '' }, L = { sort: 'refreshed', dir: 1, page: 1, size: 50 };
  const JOB = () => DOC?.job || {};
  const order = s => { const i = STAGES.findIndex(x => x[0] === s); return i < 0 ? STAGES.length : i; };
  function shown() {
    // The oldest refresh first: what needs a refresh leads, active watches before the rest, and within each a watch with no
    // date before any date.
    const key = { title: r => r.title.toLowerCase(), stage: r => order(r.stage), refreshed: r => `${r.stage === 'active' ? 0 : 1}${r.refreshed || ''}`, host: r => r.host }[L.sort] || (r => String(r[L.sort] || '').toLowerCase());
    const q = F.q.toLowerCase().split(/\s+/).filter(Boolean);
    return ROWS.filter(r => (!F.stage || r.stage === F.stage) && q.every(w => `${r.title} ${r.kind} ${r.area} ${r.description} ${r.host}`.toLowerCase().includes(w))).sort((a, b) => (key(a) < key(b) ? -1 : key(a) > key(b) ? 1 : 0) * L.dir);
  }
  const age = r => r.refreshed ? `${r.refreshed} · ${r.age === 0 ? 'today' : `${r.age} d ago`}` : '— never';
  function row(r) {
    const [g, mark, word] = fresh(r);
    return html`<tr data-id="${r.id}" aria-selected="${String(r.id === SELECTED)}">
      <td class="g g-${g}" title="${word}"><span aria-hidden="true">${mark}</span><span class="sr">${word}</span></td>
      <td class="what"><button type="button">${r.title}</button>${r.area ? html`<span class="file">${r.area}</span>` : ''}</td>
      <td class="repo" data-meta="${` · ${r.stage} · ${word}`}">${r.kind || '—'}</td>
      <td class="kind">${r.stage}</td>
      <td class="num">${age(r)}${r.fresh === 'stale' ? html` <span class="g-caution">stale</span>` : ''}</td>
      <td class="mod">${link(r.url) ? html`<a class="ext" href="${r.url}" target="_blank" rel="noopener noreferrer">${r.host}</a>` : r.url_text || '—'}</td>
      <td class="acts">${window.shell.commands.rowActions(r.id)}</td></tr>`;
  }
  // The job's line: its last run and how it ended. A failed run says why, from the last line it printed.
  function jobLine() {
    const j = JOB(), run = j.last_run || {};
    if (!DOC) return html``;
    if (j.error) return html`<span class="g-warning" aria-hidden="true">■</span> ${j.error}`;
    const when = t => html`<time class="rel" datetime="${t}">${t}</time>`;
    const state = j.state === 'running' ? html`<span class="g-ok" aria-hidden="true">●</span> market-watch is running now`
      : run.state === 'finished' && run.exit === 0 ? html`<span class="g-ok" aria-hidden="true">●</span> The last run ended ${when(run.ended)}, exit 0`
      : run.state === 'finished' ? html`<span class="g-warning" aria-hidden="true">■</span> The last run failed ${when(run.ended)}, exit ${run.exit}${j.said ? `: ${j.said}` : ''}`
      : run.state === 'open' ? html`<span class="g-caution" aria-hidden="true">▲</span> A run started ${when(run.started)} and has not ended`
      : html`<span class="g-unknown" aria-hidden="true">▨</span> The last run is not known: ${run.reason || 'no run stamp was read'}`;
    const asked = j.last_request ? html` · Last request: ${j.last_request.action} ${j.last_request.status}${j.last_request.message ? `, ${j.last_request.message}` : ''}` : '';
    return html`${state}${asked}`;
  }
  function draw() {
    const L_ = window.shell.list, list = shown(), slice = L_.pageOf(list, L), n = s => ROWS.filter(r => r.stage === s).length;
    const stale = ROWS.filter(r => r.fresh === 'stale' || r.fresh === 'never').length;
    put($('sub'), html`${DOC ? `${watches(ROWS.length)} · ${n('active')} active${stale ? ` · ${stale} stale or never refreshed` : ''}` : 'The watches were not read'}`);
    put($('job'), jobLine());
    put($('filters'), L_.fbar(html`${L_.fseg('stage', 'Status', STAGES.map(([s, w]) => ({ v: s, pressed: F.stage === s, title: `${w}: ${n(s)}`,
      label: html`${w} <span class="n">${n(s)}</span>` })))}`, { clear: !!F.stage }));
    put($('rows-head'), L_.sortHead(COLS, L));
    put($('rows'), html`${slice.map(row)}`);
    const empty = $('empty');
    empty.hidden = !ROWS.length || list.length > 0;
    put(empty, html`No watch matches these filters. <button class="linkish" type="button" data-fclear>Clear filters</button>`);
    put($('tally'), html`${DOC ? (list.length === ROWS.length ? watches(ROWS.length) : `${list.length} of ${watches(ROWS.length)}`) : ''}`);
    put($('pager'), L_.pager(list.length, L, 'watches'));
    put($('help'), L_.help('', Object.values(FRESH).map(([g, m, w]) => [html`<span class="g-${g}" aria-hidden="true">${m}</span>`, w])));
    window.shell.reconcile({ rows: $('rows').querySelectorAll('tr[data-id]'), current: SELECTED, select: show,
      clear: () => { SELECTED = null; put($('details'), html`<p class="why">No watch matches these filters, so nothing is selected.</p>`); } });
  }
  function show(key) {
    const r = ROWS.find(x => x.id === key); if (!r) return;
    SELECTED = key;
    $('rows').querySelectorAll('tr[data-id]').forEach(tr => tr.setAttribute('aria-selected', tr.dataset.id === key));
    const [g, mark, word] = fresh(r);
    const facts = [['Kind', r.kind], ['Area', r.area], ['Slug', r.slug], ['Added', r.added]].filter(([, x]) => x);
    put($('details'), html`<p class="kind"><span class="g-${g}" aria-hidden="true">${mark}</span> ${r.stage} · ${word}</p>
      <h2>${r.title}</h2>
      ${r.description ? html`<p>${r.description}</p>` : ''}
      <h3>Act</h3><div>${window.shell.commands.bar(r.id)}</div>
      <h3>Facts</h3>
      <dl><dt>Status</dt><dd>${r.stage}</dd>
        <dt>Last refresh</dt><dd>${age(r)} <span class="why">(${word}${r.fresh === 'stale' ? `: older than ${DOC.stale_days} days` : ''})</span></dd>
        ${facts.map(([k, x]) => html`<dt>${k}</dt><dd>${x}</dd>`)}
        <dt>URL</dt><dd>${link(r.url) ? html`<a class="ext" href="${r.url}" target="_blank" rel="noopener noreferrer">${r.url}</a>` : r.url_text ? html`${r.url_text} <span class="why">(not a link that parses)</span>` : '— none in its Sources'}</dd>
        <dt>Item</dt><dd><a href="${window.shell.itemHref(r.item)}">#${r.item}</a></dd></dl>
      <p class="why">The date is the newest one market-watch wrote into the profile. Refresh watch opens a Claude session on this one watch; the date moves when its run writes the profile. Activate, Pause and Unpause move the status.</p>`);
    window.shell.commands.select(r.id);
  }
  function unselect() {
    SELECTED = null;
    put($('details'), html`<p class="why">${!DOC ? 'The watches were not read, so nothing is selected.' : !ROWS.length ? 'No watch is in the store yet: Add watch files one.' : 'No watch matches these filters. Clear them to see the list.'}</p>`);
  }
  // The reader and the commands start once shell.js has loaded, after this file.
  let reader = null;
  const start = () => window.shell.read({ source: '/api/watches', what: 'the watches',
    adopt: doc => {
      DOC = doc;
      document.body.dataset.observed = doc.read; // shell time cells count from the reading, not from now
      ROWS = (doc.rows || []).map(r => ({ ...r, id: `watch:${r.id}`, item: r.id, type: 'watch', label: r.title }));
      const job = { ...(doc.job || {}), id: 'job:market-watch', type: 'job', label: 'market-watch', job: 'market-watch' };
      return { objects: [...ROWS, job, { id: 'watches', type: 'watch list', label: 'Watches' }],
        state: ROWS.length ? null : { kind: 'empty', text: 'No market row is in the store yet: Add watch files one.', source: '/api/watches' } };
    },
    clear: () => { DOC = null; ROWS = []; draw(); },
    draw, current: () => SELECTED, first: () => shown().slice((L.page - 1) * L.size)[0]?.id,
    select: key => show(key), unselect });
  const redraw = () => { L.page = 1; draw(); };
  $('filters').addEventListener('click', e => {
    if (e.target.closest('[data-fclear]')) { F.stage = ''; return redraw(); }
    const c = e.target.closest('[data-f="stage"]'); if (!c) return;
    F.stage = F.stage === c.dataset.v ? '' : c.dataset.v;
    redraw();
  });
  // The shell's bar filters the ledger by its words.
  document.addEventListener('shell:filter', e => { if (e.detail.q === F.q) return; F.q = e.detail.q || ''; redraw(); });
  // The empty state's Clear filters clears the bar's words too: a search that matched nothing kept the list empty (sd:6285).
  $('empty').addEventListener('click', e => { if (e.target.closest('[data-fclear]')) { F.stage = ''; F.q = ''; window.shell.filter(''); redraw(); } });
  $('rows-head').addEventListener('click', e => { if (window.shell.list.sortBy(e, L)) window.shell.list.keepFocus(e, draw); });
  $('pager').addEventListener('click', e => { if (window.shell.list.paging(e, L)) window.shell.list.keepFocus(e, draw); });
  $('rows').addEventListener('click', e => {
    if (e.target.closest('.rowact, a')) return;
    const tr = e.target.closest('tr[data-id]'); if (tr) { show(tr.dataset.id); window.shell.openPane('tab-details'); }
  });
  // Refresh watches: Run now on the job, with the revision the page read; the job's line comes back with the next read.
  const job = o => ({ name: 'market-watch', service: o.service, failed: o.state === 'failed', exit: o.last_exit,
    capability: o.error ? { allowed: false, reason: o.error } : o.state === 'running' ? { allowed: false, reason: 'market-watch is running now' } : o.capabilities?.retry });
  const kick = o => window.shell.post('/api/jobs/market-watch/retry', { revision: o.revision })
    .then(out => reader.reread().then(() => window.commands.answered(out, 'market-watch', 'started', 'Refresh watches started · market-watch refreshes the next three active watches')));
  // Activate, Pause and Unpause (sd:6694): the stage move Tasks' Stage field makes, POST /api/items/<n>/stage with the row's
  // revision; each shows only on the stage it moves from, so a declined watch gets none. Undo moves it back.
  const stage = (o, to, revision) => window.shell.post(`/api/items/${o.item}/stage`, { revision, stage: to }).then(out => reader.reread().then(() => out));
  const move = (id, label, key, from, to, does, done) => ({ id, on: 'watch', label, does, key, risk: 'undo', executes: true,
    primary: o => o.stage === from, when: o => o.stage === from || `${o.stage}: ${label} moves ${from === 'active' ? 'an' : 'a'} ${from} watch`,
    cli: () => 'no sd verb: sd store set refuses a status the kind keeps for a person', sends: o => `POST /api/items/${o.item}/stage {revision, stage: ${to}}`,
    run: o => stage(o, to, o.revision).then(out => ({ text: `${o.title} is ${to} · ${done}${out.mirror && !out.mirror.ok
      ? ` · mirror not updated: ${out.mirror.reason || 'see Health'}` : ''}`, revision: out.revision })),
    undo: (o, v) => stage(o, from, v.revision) });
  const register = () => window.shell.commands.register(
    { ...window.commands.retry({ job, run: kick }), label: 'Refresh watches', primary: () => true,
      does: 'Starts the market-watch job now; it refreshes the next three active watches.' },
    { id: 'watch.refresh', on: 'watch', label: 'Refresh watch', does: 'Opens a Claude session that refreshes only this watch\'s profile.', key: 'r', icon: 'rotate-ccw', risk: 'confirm', executes: true,
      primary: o => o.fresh === 'stale' || o.fresh === 'never', when: o => o.stage === 'active' || `${o.stage}: market-watch refreshes active watches only`,
      consequence: o => `This opens a Claude session (herdr workspace watch-${o.item}) in the vault that refreshes ${o.title} only. It rewrites the profile's sections; the session is yours to steer or close.`,
      cli: o => `claude ${shq(`Run the market-watch routine's per-entity refresh for ${o.title} only.`)}`, sends: o => `POST /api/actions/run {action: watch.refresh, item: ${o.item}}`,
      run: o => window.commands.runAction(o, 'watch.refresh', { item: o.item }) },
    { id: 'watch.open', on: 'watch', label: 'Open link', does: 'Opens the watch\'s first source in a new tab.', key: 'o', icon: 'arrow-up-right', risk: 'safe',
      when: o => link(o.url) || 'no http or https link in its Sources', cli: o => `open ${shq(o.url)}`,
      run: o => { window.open(o.url, '_blank', 'noopener'); return `Opened in a new tab · ${o.title}`; } },
    { id: 'watch.item', on: 'watch', label: 'Open item', does: 'Opens the watch\'s row in Tasks.', key: 'i', risk: 'safe',
      cli: o => `sd task show ${o.item}`, run: o => window.shell.openItem(o.item) },
    { id: 'watch.add', on: 'watch list', label: 'Add watch', does: 'Files a candidate watch for a URL; Activate makes it active.', key: 'a', icon: 'plus', risk: 'safe', executes: true, primary: () => true,
      fields: () => [{ name: 'url', label: 'URL', required: true, placeholder: 'https://', help: 'An http or https link: a company site or a repository.' },
        { name: 'name', label: 'Name', placeholder: 'optional', help: 'Empty: the repository\'s name or the site\'s host.' }],
      consequence: () => 'This adds the company as a candidate watch, with the URL as its first source. It refreshes once you make it active.',
      cli: (o, v) => `sd store add sdw.market ${shq(v?.name || 'NAME')} --field content-type=company --section ${shq(`Sources=- ${v?.url || 'URL'}`)}`,
      sends: () => 'POST /api/actions/run {action: watch.add, url, name}',
      run: (o, v) => window.commands.runAction(o, 'watch.add', { url: v?.url || '', name: v?.name || '' }).then(said => reader.reread().then(() => said)) },
    // After Refresh watch, so a stale active row keeps Refresh as its primary.
    move('watch.activate', 'Activate', 'a', 'candidate', 'active', 'Makes this candidate an active watch; market-watch starts refreshing it.', 'market-watch refreshes it from its next run'),
    move('watch.pause', 'Pause', 'p', 'active', 'paused', 'Pauses this watch; market-watch stops refreshing it.', 'market-watch skips it until it is unpaused'),
    move('watch.unpause', 'Unpause', 'p', 'paused', 'active', 'Makes this paused watch active again.', 'market-watch refreshes it from its next run'),
    // sd:6703: Remove retires (stage declined, which market-watch skips); Delete removes the row for good.
    { id: 'watch.remove', on: 'watch', label: 'Remove', does: 'Declines the watch; it stops refreshing. Its profile stays.', key: 'm', risk: 'confirm', executes: true,
      when: o => o.stage !== 'declined' || 'declined already', consequence: o => `This moves ${o.title} to declined. market-watch refreshes it no more; its profile and Sources stay, and Delete removes it for good.`,
      cli: o => `no sd verb: the dashboard moves #${o.item} to declined, as Tasks moves it`, sends: o => `POST /api/actions/run {action: watch.remove, item: ${o.item}}`,
      run: o => window.commands.runAction(o, 'watch.remove', { item: o.item }).then(said => reader.reread().then(() => said)) },
    { id: 'watch.restore', on: 'watch', label: 'Restore', does: 'Brings a declined watch back as a candidate; Activate makes it active.', key: 'e', risk: 'confirm', executes: true,
      when: o => o.stage === 'declined' || `${o.stage}: Restore brings back a declined watch`, consequence: o => `This moves ${o.title} from declined to candidate. Activate makes it active again.`,
      cli: o => `no sd verb: the dashboard moves #${o.item} to candidate, as Tasks moves it`, sends: o => `POST /api/actions/run {action: watch.restore, item: ${o.item}}`,
      run: o => window.commands.runAction(o, 'watch.restore', { item: o.item }).then(said => reader.reread().then(() => said)) },
    { id: 'watch.delete', on: 'watch', label: 'Delete', does: 'Deletes the watch\'s row for good, with its profile and Sources.', key: 'x', risk: 'confirm', executes: true,
      consequence: o => `This deletes ${o.title} (#${o.item}) for good: its profile, its Sources and its notes are lost. A backup and a removal record are made first. Remove keeps them.`,
      cli: o => `sd-db item remove ${o.item} --who dashboard --reason ${shq(`Deleted on the dashboard: sdw.market ${o.title}`)}`, sends: o => `POST /api/actions/run {action: watch.delete, item: ${o.item}}`,
      run: o => window.commands.runAction(o, 'watch.delete', { item: o.item }).then(said => reader.reread().then(() => said)) });
  window.PAGE_LIST = { rows: () => $('rows').querySelectorAll('tr[data-id]'), current: () => SELECTED, select: key => show(key) };
  document.addEventListener('DOMContentLoaded', () => {
    reader = start();
    register();
    window.PAGE_AGAIN = { title: 'Read the watches again', run: () => reader.load() };
    reader.load();
  });
})();
