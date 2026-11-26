// Books (sd:6260; courses too, sd:10634) and Videos (sd:6261): Documents views over the vault's learning rows (learning_screen.py), beside the Documents list.
// The view reads its document when it first shows (shell.read with its api, its own state slot and active only while it
// shows), and draws the Documents list grammar: a filter bar, a sorted and paged ledger, and the one Details pane, which
// the Documents list leaves alone while another view shows. Nothing here writes: a row's stage and kind stay as the
// store holds them, and Open item opens the row in Tasks, where its stage moves. Add book and Add video file a new row.
// pack sd:7669: a Category tile per category of the view's fixed set (learning_screen.CATEGORIES), then Other for a row
// off the set or with none.
(() => {
  const { html, put } = window.markup;
  const $ = id => document.getElementById(id);
  const shq = s => `'${String(s ?? '').replace(/'/g, "'\\''")}'`;
  // Only an http(s) URL is a link: the server sends no other, and the page does not trust that alone.
  const link = u => /^https?:\/\//i.test(u || '');
  // A stage's glyph (design.md, State grammar): not started is queued, under way and done are normal, and a stage the map
  // does not name is unknown. The words beside it say which.
  const GLYPH = { queued: ['queued', '◌'], 'in-progress': ['ok', '●'], done: ['ok', '●'], dropped: ['queued', '◌'] };
  const glyph = stage => GLYPH[stage] || ['unknown', '▨'];
  // A video's duration is whole seconds; anything else shows as the row holds it.
  const minutes = d => /^\d+$/.test(d || '') ? `${Math.floor(d / 60)}:${String(d % 60).padStart(2, '0')}` : d;
  // Each type: its view, read, object type, nouns, the stages in its words, and the facts its Details list.
  const TYPES = {
    books: { api: '/api/documents/books', title: 'Books and courses', added: 'to read', what: 'the books, articles and courses', type: 'book', noun: 'book, article or course', nouns: 'books, articles and courses', counted: 'title',
      states: [['queued', 'to read'], ['in-progress', 'reading'], ['done', 'done'], ['dropped', 'dropped']], stateName: 'Read state',
      by: ['by', 'Author or source'], sort: 'title', dir: 1,
      none: 'No book, article or course in the vault\'s Learning base: its content-type book, article or course rows show here.',
      facts: r => [['Author or source', r.by || '— none recorded'], ['Kind', r.kind], ['Subtitle', r.subtitle], ['Published', r.published],
        ['Pages', r.pages], ['ISBN', r.isbn]] },
    // sd:6261: a video's date is the newest first.
    videos: { api: '/api/documents/videos', title: 'Videos', added: 'to watch', what: 'the videos', type: 'video', noun: 'video', nouns: 'videos', counted: 'video',
      states: [['queued', 'to watch'], ['in-progress', 'watching'], ['done', 'watched'], ['dropped', 'dropped']], stateName: 'Watched state',
      by: ['channel', 'Channel'], when: ['date', 'Date'], sort: 'date', dir: -1,
      none: 'No video in the vault\'s Learning base: its content-type youtube rows and its "YYYY-MM-DD - YouTube - …" notes show here.',
      facts: r => [['Channel', r.channel || '— none recorded'], ['Date', r.date], ['Duration', minutes(r.duration)]] },
  };
  const COLS = t => [[null, html`<span class="sr">State</span>`, 'g'], ['title', 'Title'], [t.by[0], t.by[1], 'repo'],
    ...(t.when ? [[t.when[0], t.when[1], 'num']] : []), ['state', t.stateName, 'kind'], ['host', 'URL', 'mod'], [null, html`<span class="sr">Actions</span>`, 'acts']];
  const V = Object.fromEntries(Object.entries(TYPES).map(([id, t]) => [id, { rows: [], doc: null, reader: null, selected: null,
    F: { state: '', category: '', kind: '', q: '' }, L: { sort: t.sort, dir: t.dir, page: 1, size: 50 } }]));
  const current = () => window.shell.view?.current?.();
  const words = (t, stage) => t.states.find(s => s[0] === stage)?.[1] || stage || 'no stage';
  // A row's category tile: the set's value the read filed it under (pack sd:7695), else other.
  const catOf = (v, r) => (v.doc?.categories || []).includes(r.filed) ? r.filed : 'other';
  const order = (t, stage) => { const i = t.states.findIndex(s => s[0] === stage); return i < 0 ? t.states.length : i; };
  function shown(id) {
    const v = V[id], t = TYPES[id], q = v.F.q.toLowerCase().split(/\s+/).filter(Boolean);
    const key = { title: r => r.title.toLowerCase(), state: r => order(t, r.stage), host: r => r.host || r.url_text }[v.L.sort] || (r => String(r[v.L.sort] || '').toLowerCase());
    return v.rows.filter(r => (!v.F.state || r.stage === v.F.state) && (!v.F.category || catOf(v, r) === v.F.category) && (!v.F.kind || r.kind === v.F.kind)
      && q.every(w => `${r.title} ${r.by || ''} ${r.channel || ''} ${r.category || ''} ${r.url || r.url_text} ${r.description}`.toLowerCase().includes(w)))
      .sort((a, b) => (key(a) < key(b) ? -1 : key(a) > key(b) ? 1 : 0) * v.L.dir);
  }
  function row(id, r) {
    const t = TYPES[id], [g, mark] = glyph(r.stage), w = words(t, r.stage), v = V[id];
    return html`<tr data-id="${r.id}" aria-selected="${String(r.id === v.selected)}">
      <td class="g g-${g}" title="${w}"><span aria-hidden="true">${mark}</span><span class="sr">${w}</span></td>
      <td class="what"><button type="button">${r.title}</button>${r.subtitle ? html`<span class="file">${r.subtitle}</span>` : ''}</td>
      <td class="repo" data-meta="${` · ${w}${r.host ? ` · ${r.host}` : ''}`}">${r[t.by[0]] || '—'}</td>
      ${t.when ? html`<td class="num">${r[t.when[0]] || '—'}</td>` : ''}
      <td class="kind">${w}</td>
      <td class="mod">${link(r.url) ? html`<a class="ext" href="${r.url}" target="_blank" rel="noopener noreferrer">${r.host}</a>` : r.url_text || '—'}</td>
      <td class="acts">${window.shell.commands.rowActions(r.id)}</td></tr>`;
  }
  function draw(id) {
    const v = V[id], t = TYPES[id], L_ = window.shell.list, list = shown(id), slice = L_.pageOf(list, v.L);
    const n = stage => v.rows.filter(r => r.stage === stage).length;
    const nc = c => v.rows.filter(r => catOf(v, r) === c).length;
    const cats = [...(v.doc?.categories || []).map(c => [c, c]), ...(nc('other') ? [['other', 'Other']] : [])];
    // sd:10634: a Kind tile per content type the read lists (Books: book, article, course), so a course is one press away.
    const nk = k => v.rows.filter(r => r.kind === k).length, kinds = v.doc?.kinds || [];
    put($(`${id}-filters`), L_.fbar(html`${L_.fseg('state', t.stateName, t.states.map(([s, w]) => ({ v: s, pressed: v.F.state === s, title: `${w}: ${n(s)}`,
      label: html`<span class="g-${glyph(s)[0]}" aria-hidden="true">${glyph(s)[1]}</span>${w} <span class="n">${n(s)}</span>` })))}${cats.length
      ? L_.fseg('category', 'Category', cats.map(([c, w]) => ({ v: c, pressed: v.F.category === c, title: `${w}: ${nc(c)}`,
        label: html`${w} <span class="n">${nc(c)}</span>` }))) : ''}${kinds.length
      ? L_.fseg('kind', 'Kind', kinds.map(k => ({ v: k, pressed: v.F.kind === k, title: `${k}: ${nk(k)}`,
        label: html`${k} <span class="n">${nk(k)}</span>` }))) : ''}`, { clear: !!(v.F.state || v.F.category || v.F.kind) }));
    put($(`${id}-head`), L_.sortHead(COLS(t), v.L));
    put($(`${id}-rows`), html`${slice.map(r => row(id, r))}`);
    const empty = $(`${id}-empty`);
    // A read with no rows is the state slot's empty state; this line is for filters that leave none.
    empty.hidden = !v.rows.length || list.length > 0;
    put(empty, html`No ${t.noun} matches these filters. <button class="linkish" type="button" data-fclear>Clear filters</button>`);
    put($(`${id}-tally`), html`${v.doc ? L_.count(list.length, v.rows.length, t.counted) : ''}`);
    put($(`${id}-pager`), L_.pager(list.length, v.L, t.nouns));
    put($(`${id}-help`), L_.help('', t.states.map(([s, w]) => [html`<span class="g-${glyph(s)[0]}" aria-hidden="true">${glyph(s)[1]}</span>`, w])));
    window.shell.view.count(id, v.doc ? list.length : null);
    if (current() === id) window.shell.reconcile({ rows: $(`${id}-rows`).querySelectorAll('tr[data-id]'), current: v.selected, select: k => show(id, k),
      clear: () => { v.selected = null; put($('details'), html`<p class="why">No ${t.noun} matches these filters, so nothing is selected.</p>`); } });
  }
  function show(id, key) {
    const v = V[id], t = TYPES[id], r = v.rows.find(x => x.id === key); if (!r) return;
    v.selected = key;
    $(`${id}-rows`).querySelectorAll('tr[data-id]').forEach(tr => tr.setAttribute('aria-selected', tr.dataset.id === key));
    const [g, mark] = glyph(r.stage), facts = [...t.facts(r), ['Category', r.category || '— none recorded'], ['Added', r.added]].filter(([, x]) => x);
    put($('details'), html`<p class="kind"><span class="g-${g}" aria-hidden="true">${mark}</span> ${words(t, r.stage)} · ${t.type}</p>
      <h2>${r.title}</h2>
      ${r.description ? html`<p>${r.description}</p>` : ''}
      <h3>Act</h3><div>${window.shell.commands.bar(r.id)}</div>
      <h3>Facts</h3>
      <dl>${facts.map(([k, x]) => html`<dt>${k}</dt><dd>${x}</dd>`)}
        <dt>URL</dt><dd>${link(r.url) ? html`<a class="ext" href="${r.url}" target="_blank" rel="noopener noreferrer">${r.url}</a>` : r.url_text ? html`${r.url_text} <span class="why">(not an http or https link)</span>` : '— none recorded'}</dd>
        <dt>${t.stateName}</dt><dd>${words(t, r.stage)} <span class="why">(stage ${r.stage || 'none'})</span></dd>
        <dt>Item</dt><dd><a href="${window.shell.itemHref(r.item)}">#${r.item}</a></dd></dl>
      <p class="why">Read from the vault's Learning base. The state is the row's stage; Open item moves it in Tasks.</p>`);
    window.shell.commands.select(r.id);
    window.shell.suggest?.([`Summarise ${r.title} in five lines`]);
  }
  function select(id, key, open) { show(id, key); if (open) window.shell.openPane('tab-details'); }
  function unselect(id) {
    V[id].selected = null;
    const t = TYPES[id];
    put($('details'), html`<p class="why">${!V[id].doc ? `The ${t.nouns} were not read, so nothing is selected.` : !V[id].rows.length ? `No ${t.noun} is in the vault yet, so nothing is selected.` : `No ${t.noun} matches these filters. Clear them to see the list.`}</p>`);
  }
  function reader(id) {
    const v = V[id], t = TYPES[id];
    return window.shell.read({ source: t.api, what: t.what, slot: `state-${id}`, active: () => current() === id,
      adopt: doc => {
        v.doc = doc;
        v.rows = (doc.rows || []).map(r => ({ ...r, id: `${t.type}:${r.id}`, item: r.id, type: t.type, label: r.title }));
        return { objects: [...v.rows, { id, type: `${t.type} list`, label: t.title }], state: v.rows.length ? null : { kind: 'empty', text: t.none, source: t.api } };
      },
      clear: () => { v.doc = null; v.rows = []; },
      draw: () => draw(id),
      current: () => v.selected,
      first: () => shown(id).slice((v.L.page - 1) * v.L.size)[0]?.id,
      select: key => select(id, key, false),
      unselect: () => unselect(id) });
  }
  // A view shows: the facets are the Documents list's, so they hide; the view reads on its first show and draws its
  // selection in Details again on a later one. The page's one search field filters the shown view: its plain words, since
  // repo:, kind: and the other tokens are the Documents list's.
  const typedWords = () => $('shift').value.split(/\s+/).filter(w => w && !w.includes(':')).join(' ');
  const typed = () => { const id = current(); if (TYPES[id] && V[id].F.q !== typedWords()) { V[id].F.q = typedWords(); V[id].L.page = 1; draw(id); } };
  $('shift').addEventListener('input', typed);
  $('shift').addEventListener('keyup', e => { if (e.key === 'Escape') typed(); });
  document.addEventListener('shell:view', e => {
    const id = e.detail.id;
    $('facets').hidden = id !== 'documents';
    if (!TYPES[id]) return;
    const v = V[id];
    v.F.q = typedWords();
    if (!v.reader) { v.reader = reader(id); v.reader.load(); return; }
    draw(id);
    const key = v.rows.some(r => r.id === v.selected) ? v.selected : shown(id)[0]?.id;
    if (key) show(id, key); else unselect(id);
  });
  Object.keys(TYPES).forEach(id => {
    const v = V[id], redraw = () => { v.L.page = 1; draw(id); };
    $(`${id}-filters`).addEventListener('click', e => {
      if (e.target.closest('[data-fclear]')) { v.F.state = ''; v.F.category = ''; v.F.kind = ''; return redraw(); }
      const c = e.target.closest('[data-f="state"]') || e.target.closest('[data-f="category"]') || e.target.closest('[data-f="kind"]'); if (!c) return;
      v.F[c.dataset.f] = v.F[c.dataset.f] === c.dataset.v ? '' : c.dataset.v;
      redraw();
    });
    // The empty state's Clear filters empties the search field too, and tells the Documents list (sd:6285).
    $(`${id}-empty`).addEventListener('click', e => {
      if (!e.target.closest('[data-fclear]')) return;
      v.F.state = ''; v.F.category = ''; v.F.kind = ''; v.F.q = ''; $('shift').value = ''; $('shift').dispatchEvent?.(new Event('input')); redraw();
    });
    $(`${id}-head`).addEventListener('click', e => { if (window.shell.list.sortBy(e, v.L)) window.shell.list.keepFocus(e, () => draw(id)); });
    $(`${id}-pager`).addEventListener('click', e => { if (window.shell.list.paging(e, v.L)) window.shell.list.keepFocus(e, () => draw(id)); });
    $(`${id}-rows`).addEventListener('click', e => {
      if (e.target.closest('.rowact, a')) return;
      const tr = e.target.closest('tr[data-id]'); if (tr) select(id, tr.dataset.id, true);
    });
  });
  // Open link and Open item, on each type's rows. Neither writes.
  document.addEventListener('DOMContentLoaded', () => Object.values(TYPES).forEach(t => window.shell.commands.register(
    { id: `${t.type}.open`, on: t.type, label: 'Open link', does: `Opens the ${t.noun}'s URL in a new tab.`, key: 'o', icon: 'arrow-up-right', risk: 'safe', primary: () => true,
      when: o => link(o.url) || 'no http or https URL on this row', cli: o => `open ${shq(o.url)}`,
      run: o => { window.open(o.url, '_blank', 'noopener'); return `Opened in a new tab · ${o.title}`; } },
    { id: `${t.type}.item`, on: t.type, label: 'Open item', does: 'Opens the row\'s item in Tasks, where its stage moves.', key: 'i', risk: 'safe',
      cli: o => `sd task show ${o.item}`, run: o => window.shell.openItem(o.item) })));
  // Add book and Add video (pack sd:7081): a URL becomes a row at the view's first stage, through learning.add.
  document.addEventListener('DOMContentLoaded', () => Object.entries(TYPES).forEach(([id, t]) => window.shell.commands.register(
    { id: `${t.type}.add`, on: `${t.type} list`, label: `Add ${t.type}`, does: `Files a ${t.added} learning row for a URL.`, key: 'a', icon: 'plus', risk: 'safe', executes: true, primary: () => true,
      fields: () => [{ name: 'url', label: 'URL', required: true, placeholder: 'https://', help: 'An http or https link.' },
        { name: 'title', label: 'Title', placeholder: 'optional', help: 'Empty: the page\'s own title.' }],
      consequence: () => `This adds the link to your ${t.added} list in the vault. It shows here and in Tasks.`,
      cli: (o, v) => `sd store add sdw.learning ${shq(v?.title || 'TITLE')} --field content-type=${t.type === 'book' ? 'book' : 'video'} --field url=${shq(v?.url || 'URL')}`,
      sends: () => `POST /api/actions/run {action: learning.add, view: ${id}, url, title}`,
      run: (o, v) => window.commands.runAction(o, 'learning.add', { view: id, url: v?.url || '', title: v?.title || '' })
        .then(said => (V[id].reader ? V[id].reader.reread() : Promise.resolve()).then(() => said)) })));
  // j / k walk the shown view's rows: the Documents list's, or this view's.
  const docs = window.PAGE_LIST, mine = () => TYPES[current()] ? current() : null;
  window.PAGE_LIST = {
    rows: () => mine() ? $(`${mine()}-rows`).querySelectorAll('tr[data-id]') : docs.rows(),
    current: () => mine() ? V[mine()].selected : docs.current(),
    select: (key, n) => mine() ? select(mine(), key, false) : docs.select(key, n),
  };
  // Read again reads the shown view (sd:6280): a Books or Videos read that failed recovers here, as the Documents list does.
  const again = window.PAGE_AGAIN;
  window.PAGE_AGAIN = { ...again, run: () => mine() ? (V[mine()].reader ||= reader(mine())).load() : again.run() };
})();
