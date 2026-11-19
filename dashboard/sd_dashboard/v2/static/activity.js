// Activity (sd:2111): the design source's products/system/designs/v2/activity.js at d82daa1, ported. Each change from the
// reference is marked "build:". The events are /api/activity (activity_screen.py), never sample data. A command that executes
// posts to the route v1 already answers, and its toast comes after the write lands, not before.
const { html, put, plural, cells } = window.markup;
// The shell's view switch (shell.js, view block): the Timeline alone. pack sd:7681: the lane chart that was the Cadence
// view is Today's Timeline now, one lane per kind under Today's own lanes.
window.PAGE_VIEWS = [{ id: 'timeline', label: 'Timeline' }];
// The shell's Shapeshift bar (sd:5539) filters the timeline through shell:filter.
window.PAGE_FILTER = { timeline: 'the timeline' };
addEventListener('DOMContentLoaded', () => {
  const { ICON, toast, suggest, openPane } = window.shell;
  const C = window.shell.commands;
  const $ = id => document.getElementById(id);

  // build: the sources are the document's. The design's Deploys and Mail are left out: nothing records either (sd:2212).
  const KINDS = [
    { id: 'merge', name: 'Merges', icon: 'git-pull-request', src: 'sd-ship delivery notes' },
    { id: 'run', name: 'Runs', icon: 'play', src: 'runner assignments and launchd jobs' },
    // build: a review is the one its merge carried, from the delivery note (sd:2211); one that never shipped is not recorded.
    { id: 'review', name: 'Reviews', icon: 'bot', src: 'sd-ship delivery notes: the review each merge carried' },
    { id: 'command', name: 'Commands', icon: 'terminal', src: 'the execution journal: notes of kind exec' },
  ];
  const SOURCES = { merge: ['merge'], run: ['run', 'job'], review: ['review'], command: ['command'] };
  const GLYPH = { ok: '●', caution: '▲', warning: '■', queued: '◌', unknown: '▨' };
  const kindOf = id => KINDS.find(k => k.id === id);
  const hhmm = iso => iso.slice(11, 16);
  const ago = iso => { const m = Math.round((OBSERVED - Date.parse(iso)) / 60000); return m < 60 ? `${m}m ago` : `${Math.round(m / 60)}h ago`; };
  // The document holds the 24 hours, or the 7 days a ?range=7d link asks for (Today's Timeline, pack sd:7681), and, for the
  // journal, older records too; the document's range is the window.
  const inWindow = e => Date.parse(e.at) >= RANGE.from;

  // ---------- Data (build) ----------
  let DOC = null, EVENTS = [], OBSERVED = Date.now(), RANGE = { from: OBSERVED - 864e5, to: OBSERVED }, REPOS = [];
  // A kind is unknown when every source behind it failed to read this time: unknown is not zero.
  const unknownWhy = k => {
    if (!DOC) return 'not read yet';
    const src = SOURCES[k] || [];
    return src.length && src.every(s => DOC.sources[s]) ? src.map(s => DOC.sources[s]).join('; ') : '';
  };
    // build: the reader (read.js, sd:2418; adopted by sd:2489) holds the guards. Of overlapping reads only the newest draws;
  // an event the read no longer lists loses its pick and runs no command; a failed load leaves no row, object or observed
  // time from the last one; and a failed reread after a write keeps the rows and says the write landed.
  const activity = window.shell.read({
    source: () => F.week ? '/api/activity?range=7d' : '/api/activity', what: 'the events',
    adopt: doc => {
      DOC = doc; EVENTS = doc.events; OBSERVED = Date.parse(doc.read); RANGE = { from: Date.parse(doc.from), to: Date.parse(doc.to) };
      document.body.dataset.observed = doc.read;
      REPOS = Object.entries(EVENTS.reduce((a, e) => (e.repo && (a[e.repo] = (a[e.repo] || 0) + 1), a), {})).sort((a, b) => b[1] - a[1]);
      const ids = new Set(EVENTS.map(e => e.id));
      Object.keys(OUTPUT).forEach(id => { if (!ids.has(id)) delete OUTPUT[id]; });
      const failed = Object.entries(doc.sources).filter(([, why]) => why);
      return { objects: EVENTS.map(objectOf),
        state: failed.length ? { kind: 'partial', text: failed.map(([s, why]) => `${s}: ${why}`).join(' · '), source: '/api/activity' }
          : !EVENTS.some(inWindow) ? { kind: 'empty', text: `No merge, run, review or command in the last ${F.week ? '7 days' : '24 hours'}.` } : null };
    },
    clear: () => { DOC = null; EVENTS = []; REPOS = []; Object.keys(OUTPUT).forEach(id => delete OUTPUT[id]); delete document.body.dataset.observed; },
    draw: () => update(),
    // update() has already reconciled the selection against the rows the filters show, so the reader settles on that
    // one: a ?row= the filters hide gives way to the first shown row, as a hidden row does on every redraw.
    current: () => selected,
    first: () => selected,
    select: (id, opened) => { const shown = selected; if (shown) select(shown, opened && shown === id); else unselect(); },
    unselect: () => unselect(),
  });
  const load = () => activity.load(), reread = () => activity.reread();
  // Read again (sd:5638): the shell's head button reads the events again.
  window.PAGE_AGAIN = { title: 'Read the events again', run: load };

  // ---------- State (mirrored in the URL) ----------
  const F = { kind: new Set(), repo: new Set(), state: '', q: '', hour: false, all: false, week: false }; // all: every event read, the journal's older records too; only a link sets it (palette.py)
  // week: the 7-day read; only a link sets it (Today's Timeline at 7d), and a range press or Clear all reads 24 hours again
  let page = 1, size = 50, selected = null;
  function readURL() {
    const p = new URLSearchParams(location.search);
    // A kind the page does not draw (an old link's deploy or mail) is dropped, or it would hide every row.
    (p.get('kind') || '').split(',').filter(v => kindOf(v)).forEach(v => F.kind.add(v));
    (p.get('repo') || '').split(',').filter(Boolean).forEach(v => F.repo.add(v));
    F.state = p.get('state') || ''; F.q = p.get('q') || ''; F.hour = p.get('range') === '1h'; F.all = p.get('range') === 'all'; F.week = p.get('range') === '7d';
    const asked = Number(p.get('page'));
    page = Number.isInteger(asked) && asked > 0 ? asked : 1; // a page is a positive integer; anything else is the first page
    size = [25, 50, 100, 200].includes(+p.get('size')) ? +p.get('size') : 50;
    selected = window.shell.row() || null;
  }
  function writeURL() {
    const p = new URLSearchParams();
    if (F.kind.size) p.set('kind', [...F.kind].join(','));
    if (F.repo.size) p.set('repo', [...F.repo].join(','));
    if (F.state) p.set('state', F.state);
    if (F.q) p.set('q', F.q);
    if (F.hour) p.set('range', '1h');
    if (F.all) p.set('range', 'all');
    if (F.week) p.set('range', '7d');
    if (page > 1) p.set('page', page);
    if (size !== 50) p.set('size', size);
    window.shell.url(p); // build: the shell keeps ?row=
  }
  const since = () => F.hour ? OBSERVED - 36e5 : 0;
  const active = () => F.kind.size + F.repo.size + (F.state ? 1 : 0) + (F.q ? 1 : 0) + (F.hour || F.all || F.week ? 1 : 0);
  const match = e => (!F.kind.size || F.kind.has(e.k)) && (!F.repo.size || F.repo.has(e.repo)) && (!F.state || e.s === F.state) && (F.all || Date.parse(e.at) >= (since() || RANGE.from)) &&
    (!F.q || `${e.what} ${e.repo || ''} ${e.ref} ${e.detail || ''}`.toLowerCase().includes(F.q.toLowerCase())); // ?q= keeps its case

  // ---------- Annunciator ----------
  // Rail badge (shell.js): the read's own, the loudest state in the 24 h window (sd:3446, activity_screen._attention).
  function pageAttention() {
    window.shell.attention?.(DOC && DOC.attention || { state: 'unknown', n: 0, what: 'events' });
  }
  function renderAnnunciator() {
    pageAttention();
    // The lamps count the timeline's Range (1h, 24h, All): a 24-hour page said "55 in the journal" of weeks-old records.
    const inRange = e => F.all || Date.parse(e.at) >= (since() || RANGE.from);
    const shown = EVENTS.filter(inRange);
    const n = (k, s) => shown.filter(e => e.k === k && (!s || e.s === s)).length;
    const worst = k => unknownWhy(k) ? 'unknown' : n(k, 'warning') ? 'warning' : n(k, 'caution') ? 'caution' : 'ok';
    // A lamp and its Kind chip are one control (design.md § Interaction rules): pressed is F.kind.
    // build: a kind whose read failed is a lamp that cannot be pressed: it has no answer to filter.
    const cell = (k, val) => unknownWhy(k.id)
      ? { state: 'unknown', attrs: html` data-kind="${k.id}"`, label: k.name, mark: ICON(k.icon), val: html`<span class="ph"><b>unknown</b></span> · <span class="ph">not read</span>` }
      : { button: true, state: worst(k.id), pressed: F.kind.has(k.id), attrs: html` data-kind="${k.id}"`, label: k.name, mark: ICON(k.icon), val };
    // Runs mixes two sources: a job carries `job`, an assignment carries `status`; count each by its own field.
    const asg = shown.filter(e => e.k === 'run' && e.status);
    const jobsFailed = shown.filter(e => e.k === 'run' && e.job && e.s === 'warning').length;
    const VAL = {
      merge: html`<span class="ph"><b>${n('merge')}</b> merged</span> · <span class="ph">${plural(new Set(shown.filter(e => e.k === 'merge').map(e => e.repo)).size, 'repo')}</span>`,
      run: html`<span class="ph"><b>${jobsFailed}</b> jobs failed</span> · <span class="ph">${asg.filter(e => e.status === 'blocked').length} of ${asg.length} runs blocked</span>`,
      command: html`<span class="ph"><b>${n('command', 'warning')}</b> failed</span> · <span class="ph">${n('command')} ${F.all ? 'in the journal' : F.hour ? 'in 1 h' : F.week ? 'in 7 d' : 'in 24 h'}</span>`,
      // build: the design's "want changes · approve" has no source; a delivery records clean or adjudicated (sd:2211).
      review: html`<span class="ph"><b>${n('review', 'caution')}</b> adjudicated</span> · <span class="ph">${n('review', 'ok')} clean</span>${DOC?.review_unrecorded ? html` · <span class="ph">${plural(DOC.review_unrecorded, 'merge')} unrecorded</span>` : ''}`,
    };
    // sd:6697: no Observed cell; the shell's Read again is the page's one re-read.
    put($('annunciator'), cells(KINDS.map(k => cell(k, VAL[k.id] || ''))));
  }
  $('annunciator').addEventListener('click', e => {
    const c = e.target.closest('button.cell'); if (!c) return;
    const k = c.dataset.kind;
    F.kind.has(k) ? F.kind.delete(k) : F.kind.add(k); page = 1; update(); window.shell.view.show('timeline'); // adds to the kind filter
  });

  // ---------- Filters: one bar (state, range, repo, the picked tokens, Clear all) ----------
  // A lamp already toggles each kind, so the bar draws no kind chips; a picked kind shows as a removable token.
  // sd:5463: the shell draws the bar (shell.list.fbar), as on every table page; the count sits on the Timeline title line.
  const STATES = [['warning', '■', 'failed'], ['caution', '▲', 'caution'], ['ok', '●', 'ok']];
  const pop = $('repo-pop'), repoQ = $('repo-q'), repoList = $('repo-list');
  let cursor = 0; // the active option in the repo list, for the arrow keys and Enter
  pop.hidden = true; // the list starts closed, whatever a restored page left
  function renderFilters() {
    const shown = EVENTS.filter(match).length;
    const count = s => String(EVENTS.filter(e => e.s === s).length);
    const { fseg, tok, fbar } = window.shell.list;
    put($('filters'), fbar(html`${fseg('state', 'State', STATES.map(([s, g, w]) => ({ v: s, pressed: F.state === s, title: `${w}: ${count(s)}`,
      label: html`<span class="g-${s}" aria-hidden="true">${g}</span><span class="n">${count(s)}</span><span class="sr"> ${w}</span>` })))}
      ${fseg('range', 'Range', [{ v: '1h', label: '1h', pressed: F.hour }, { v: '24h', label: '24h', pressed: !F.hour && !F.all && !F.week }])}
      ${REPOS.length || F.repo.size ? html`<button type="button" class="pick" id="repo-btn" aria-haspopup="listbox" aria-expanded="${String(!pop.hidden)}" aria-controls="repo-pop">Repo${F.repo.size ? html` <span class="n">· ${String(F.repo.size)}</span>` : ''}</button>` : ''}
      ${[...F.kind].map(k => tok('kind', k, kindOf(k).name))}${[...F.repo].map(r => tok('repo', r, r))}${F.q ? tok('q', '', `“${F.q}”`) : ''}${F.all ? tok('range', 'all', 'Older journal records') : ''}${F.week ? tok('range', '7d', 'Last 7 days') : ''}`, { clear: active() }));
    window.shell.view.count('timeline', shown);
  }
  $('filters').addEventListener('click', e => {
    if (e.target.closest('[data-fclear]')) return clearAll();
    if (e.target.closest('#repo-btn')) return pop.hidden ? openRepos() : closeRepos(true);
    const r = e.target.closest('[data-f="range"]');
    if (r) {
      const v = r.dataset.v;
      // The All and 7d tokens remove themselves to 24h; leaving 7 days reads the 24 hours again.
      const was = F.week; F.hour = v === '1h'; F.all = false; F.week = false; page = 1; return was ? load() : update();
    }
    const c = e.target.closest('[data-f]'); if (!c) return;
    const { f, v } = c.dataset;
    if (f === 'kind' || f === 'repo') F[f].has(v) ? F[f].delete(v) : F[f].add(v);
    if (f === 'state') F.state = F.state === v ? '' : v;
    if (f === 'q') { F.q = ''; window.shell.filter(''); }
    page = 1; update();
  });
  function clearAll() { const was = F.week; F.kind.clear(); F.repo.clear(); F.state = ''; F.q = ''; F.hour = false; F.all = false; F.week = false; window.shell.filter(''); page = 1; was ? load() : update(); }

  // The repo picker: a listbox under the Repo button, every repo with its count, most events first. Typing filters it;
  // the arrows move, Enter or a tap picks, Esc closes and gives focus back to the button. A picked repo with no events
  // (an address from another day) stays in the list, so it can be unpicked.
  function repoOptions() {
    const q = repoQ.value.trim().toLowerCase(), all = new Map(REPOS);
    F.repo.forEach(r => { if (!all.has(r)) all.set(r, 0); });
    return [...all].filter(([r]) => !q || r.toLowerCase().includes(q));
  }
  function renderRepos() {
    const opts = repoOptions();
    cursor = Math.max(0, Math.min(cursor, opts.length - 1));
    put(repoList, opts.length ? html`${opts.map(([r, n], i) => html`<li role="option" id="repo-opt-${String(i)}" data-repo="${r}" aria-selected="${String(F.repo.has(r))}"${i === cursor ? html` data-active=""` : ''}><span class="ck" aria-hidden="true">✓</span><span class="nm">${r}</span><span class="n">${String(n)}</span></li>`)}`
      : html`<li class="none" role="presentation">No repo matches “${repoQ.value.trim()}”</li>`);
    if (opts.length) repoQ.setAttribute('aria-activedescendant', `repo-opt-${cursor}`); else repoQ.removeAttribute('aria-activedescendant');
    repoList.querySelector('[data-active]')?.scrollIntoView?.({ block: 'nearest' });
  }
  const touch = () => matchMedia('(pointer: coarse)').matches; // a tap does not raise the keyboard
  function openRepos() {
    repoQ.value = ''; cursor = 0; pop.hidden = false;
    const btn = $('repo-btn');
    if (btn && btn.offsetParent) { pop.style.setProperty('--pop-x', `${btn.offsetLeft}px`); pop.style.setProperty('--pop-y', `${btn.offsetTop + btn.offsetHeight + 4}px`); }
    renderRepos(); renderFilters();
    if (!touch()) repoQ.focus();
  }
  // A press outside closes in place: a redraw on pointerdown would swap the token under the pointer and lose its click.
  function closeRepos(focus) {
    if (pop.hidden) return;
    pop.hidden = true;
    if (!focus) return $('repo-btn')?.setAttribute('aria-expanded', 'false');
    renderFilters(); $('repo-btn')?.focus();
  }
  function pickRepo(r) { F.repo.has(r) ? F.repo.delete(r) : F.repo.add(r); page = 1; update(); renderRepos(); }
  repoQ.addEventListener('input', () => { cursor = 0; renderRepos(); });
  repoList.addEventListener('click', e => { const o = e.target.closest('[role="option"]'); if (!o) return; pickRepo(o.dataset.repo); if (!touch()) repoQ.focus(); });
  // Keys stop here, so the shell's Esc (clear the filter) and Enter do not also run.
  pop.addEventListener('keydown', e => {
    const opts = repoOptions();
    if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); return closeRepos(true); }
    if (e.key === 'Tab') return closeRepos(false);
    if ((e.key === 'ArrowDown' || e.key === 'ArrowUp') && opts.length) {
      e.preventDefault(); cursor = (cursor + (e.key === 'ArrowDown' ? 1 : -1) + opts.length) % opts.length; return renderRepos();
    }
    if (e.key === 'Enter' && opts.length) { e.preventDefault(); e.stopPropagation(); pickRepo(opts[cursor][0]); }
  });
  // Esc on the Repo button itself closes the list too; a press outside the bar and the list closes it.
  $('filters').addEventListener('keydown', e => { if (e.key === 'Escape' && !pop.hidden) { e.preventDefault(); e.stopPropagation(); closeRepos(true); } });
  document.addEventListener('pointerdown', e => { if (!pop.hidden && !e.target.closest?.('#repo-pop, #repo-btn')) closeRepos(false); });

  // ---------- Timeline ----------
  const tbody = $('rows');
  const whereOf = e => e.k === 'command' || e.k === 'run' ? (e.item ? `sd:${e.item}` : e.job || e.ref || '') : e.ref || '';
  function renderRows() {
    const all = EVENTS.filter(match);
    const pages = Math.max(1, Math.ceil(all.length / size)); page = Math.min(page, pages);
    const rows = all.slice((page - 1) * size, page * size);
    const perHour = all.reduce((a, e) => (a[e.at.slice(0, 13)] = (a[e.at.slice(0, 13)] || 0) + 1, a), {});
    let last = '';
    put(tbody, html`${rows.map(e => {
      const h = e.at.slice(0, 13), band = h !== last ? html`<tr class="hour" aria-hidden="true"><td colspan="6">${h.slice(11)}:00 UTC · ${new Date(e.at).toLocaleDateString('en-GB', { day: 'numeric', month: 'short', timeZone: 'UTC' })}<span class="n">${plural(perHour[h], 'event')}</span></td></tr>` : ''; last = h;
      const sub = e.k === 'merge' ? e.repo : e.detail;
      return html`${band}<tr data-id="${e.id}" data-changed="${e.at}" aria-selected="${String(e.id === selected)}">
        <td class="t"><time datetime="${e.at}" title="${ago(e.at)}">${hhmm(e.at)}</time></td>
        <td class="g g-${e.s}">${GLYPH[e.s] || GLYPH.unknown}<span class="sr">${e.s}</span></td>
        <td class="k">${ICON(kindOf(e.k).icon)}${e.k}</td>
        <td class="what"><button type="button">${e.what}<small>${sub || ''}</small></button></td>
        <td class="ref">${whereOf(e)}</td>
        <td class="act">${C.rowActions(e.id)}</td></tr>`;
    })}`);
    const empty = $('empty');
    empty.hidden = !!all.length || !EVENTS.some(inWindow);
    put(empty, all.length || !EVENTS.some(inWindow) ? html`` : html`No event matches these filters. <button class="linkbtn" type="button" data-clear>Clear all</button>`);
    const a = all.length ? (page - 1) * size + 1 : 0, z = Math.min(page * size, all.length);
    put($('pager'), html`<span>${a}–${z} of ${all.length}</span>
      <div class="chips" role="group" aria-label="Page">${page > 1 ? html`<button class="chip" type="button" data-p="${String(page - 1)}">${ICON('chevron-left')}Previous</button>` : ''}${page < pages ? html`<button class="chip" type="button" data-p="${String(page + 1)}">Next${ICON('chevron-right')}</button>` : ''}</div>
      <div class="chips" role="group" aria-label="Rows per page">${[25, 50, 100, 200].map(n => html`<button class="chip" type="button" data-size="${String(n)}" aria-pressed="${String(n === size)}">${n}</button>`)}</div>`);
    put($('sub'), html`${plural(EVENTS.filter(inWindow).length, 'event')} across the fleet · last ${F.week ? '7 days' : '24 hours'} to ${new Date(OBSERVED).toISOString().slice(11, 16)} UTC, ${new Date(OBSERVED).toLocaleDateString('en-GB', { day: 'numeric', month: 'long', timeZone: 'UTC' })}`);
    // The journal note shows only what the timeline misses: skipped exec notes and ones past the read cap (sd:2183).
    const skipped = Object.values(DOC?.journal_skipped || {}).reduce((a, n) => a + n, 0);
    $('journal-note').hidden = !skipped && !DOC?.journal_unread;
    put($('journal-note'), html`${skipped ? `${plural(skipped, 'exec note')} no known writer left ${skipped === 1 ? 'is' : 'are'} skipped.` : ''}${skipped && DOC?.journal_unread ? ' ' : ''}${DOC?.journal_unread ? `The journal reads the latest ${String(DOC.journal_cap)} exec notes; ${String(DOC.journal_unread)} older are not read.` : ''}`);
    const undated = DOC?.undated || [];
    $('undated').hidden = !undated.length;
    put($('undated'), undated.length ? html`${undated.join(', ')} failed or ${undated.length === 1 ? 'was' : 'were'} interrupted with no readable log time, so ${undated.length === 1 ? 'it has' : 'they have'} no place on this timeline. Management lists every job's state.` : html``);
  }
  $('empty').addEventListener('click', e => { if (e.target.closest('[data-clear]')) clearAll(); });
  $('pager').addEventListener('click', e => {
    const c = e.target.closest('.chip'); if (!c) return;
    if (c.dataset.p) page = +c.dataset.p;
    if (c.dataset.size) { size = +c.dataset.size; page = 1; }
    update();
  });
  tbody.addEventListener('click', e => { const tr = e.target.closest('tr[data-id]'); if (tr) select(tr.dataset.id, true); });

  function update() {
    renderAnnunciator(); renderFilters(); renderRows(); writeURL();
    // Selection follows the filter (design.md, Page contract): a hidden row gives way to the first visible one.
    const kept = window.shell.reconcile({ rows: tbody.querySelectorAll('tr[data-id]'), current: selected, select: id => select(id, false), clear: unselect });
    if (kept !== undefined) selected = kept;
  }
  // Rows still shown with nothing selected means a reread after a write failed: the reader retired their objects.
  function unselect() {
    selected = null;
    put(details, html`<p class="why">${!DOC ? 'Activity was not read, so nothing is selected.' : EVENTS.some(match) ? 'The events were not read again after the change, so nothing is selected. Reload reads them.'
      : 'No event matches the filters, so nothing is selected.'}</p>`);
  }

  // ---------- Details ----------
  const details = $('details');
  const OUTPUT = {}; // build: an execution's output, read by command.output, shown under its facts
  function swap() { details.setAttribute('data-swap', ''); requestAnimationFrame(() => details.removeAttribute('data-swap')); }
  const utc = iso => iso ? `${iso.slice(0, 16).replace('T', ' ')} UTC` : '—';
  function facts(e) {
    const f = { When: utc(e.at) };
    if (e.k === 'merge') Object.assign(f, { Repo: `${e.owner}/${e.repo}`, 'Pull request': e.ref, Item: e.item ? `sd:${e.item}` : '—', Commit: e.commit });
    // build: the design's Verdict and Summary, from the delivery note; When is the merge's, as the review's own is not recorded.
    if (e.k === 'review') Object.assign(f, { When: `${utc(e.at)} · the merge it let through`, Repo: `${e.owner}/${e.repo}`, 'Pull request': e.ref,
      Item: e.item ? `sd:${e.item}` : '—', 'Reviewed by': e.reviewers.join(', '), Requested: e.requested || 'no provider named', Verdict: e.verdict,
      'Reviewed head': e.head || 'not recorded' });
    if (e.k === 'run' && e.n) Object.assign(f, { Assignment: `#${e.n}`, Item: e.item ? `sd:${e.item}` : '—', Role: e.role, Provider: e.provider || '—', Status: e.status, Started: utc(e.started), Ended: utc(e.ended) });
    if (e.k === 'run' && e.job) Object.assign(f, { Job: e.job, 'Last run': e.rc, Detail: e.detail });
    if (e.k === 'command') Object.assign(f, { Note: String(e.note), Item: e.item ? `sd:${e.item} · ${e.title || ''}` : '—', Command: e.command, Scope: e.scope || 'not recorded',
      Started: utc(e.started), Ended: utc(e.ended), Exit: e.exit == null ? (e.ended ? 'none recorded' : 'still running') : String(e.exit), Session: e.who || '—',
      Output: e.source === 'runner' ? 'in the retained clone (a runner record)' : e.expired ? `expired ${e.expired}` : 'Show output reads it' });
    f.Source = e.src;
    return f;
  }
  function show(id) {
    const e = EVENTS.find(v => v.id === id); if (!e) return;
    tbody.querySelectorAll('tr[data-id]').forEach(tr => tr.setAttribute('aria-selected', String(tr.dataset.id === id)));
    const k = kindOf(e.k), out = OUTPUT[id];
    put(details, html`<p class="kind"><span class="g-${e.s}" aria-hidden="true">${GLYPH[e.s] || GLYPH.unknown}</span> ${k.name.replace(/s$/, '')} · ${e.s}</p>
      <h2>${e.what}</h2>
      <dl>${Object.entries(facts(e)).map(([a, b]) => html`<dt>${a}</dt><dd>${b}</dd>`)}</dl>
      <h3>Act</h3>${C.bar(id)}
      ${out ? html`<h3>Output</h3>${out.error ? html`<p class="why">${out.error}</p>` : html`<p class="why">${out.state}${out.expired ? ' · the prune removed the output file' : ''}</p><pre class="cli"><code>${out.output || '(no output)'}</code></pre>`}` : ''}
      ${e.k === 'run' && e.status === 'blocked' ? html`<p class="why">Blocked means the runner stopped and wants a decision. The reason is in the assignment log: <code>sd assignments get ${String(e.n)}</code>.</p>` : ''}`);
    C.select(id);
    suggest([`Why did this ${e.k} happen?`, 'What else changed in this hour?', e.repo ? `Show all activity in ${e.repo}` : 'Rank what I should look at first']);
    swap();
  }
  function select(id, open) { selected = id; show(id); if (open) openPane('tab-details'); }

  // ---------- Commands (products/system/commands.md): declared once, rendered by the shell ----------
  // build: an event's object carries what its commands need from the document: the pull request, the assignment and its queue
  // revision, the job and its revision and retry capability, the execution record.
  const TYPE = { merge: 'pull request', review: 'review', command: 'command' };
  function objectOf(e) { return { ...e, id: e.id, type: e.k === 'run' ? (e.job ? 'job' : 'assignment') : TYPE[e.k], label: e.what, event: e.id }; }
  const ev = o => EVENTS.find(e => e.id === o.event) || o;
  // A command's run returns landing(...): the shell's run contract (shell.js, bulk:start) waits for the write, toasts its
  // text, and offers Undo only for what landed, for a bulk group too. A landed write rereads (the reader's write barrier); a
  // refused one loads the document again, so a retry sends the revision the read brought. Undo is spent once and reverses
  // this write only.
  // A 409, or a write with no answer (no status: it may have landed, pack sd:9485), loads again; the latter is unknown.
  const again = err => { if (err && !err.status) err.unknown = true; if (err && (err.stale || err.unknown)) load(); throw err; };
  function landing(promise, text, inverse) {
    return promise.then(v => {
      let spent = false;
      const undo = inverse && (() => { if (spent) return Promise.resolve(false); spent = true;
        return inverse(v).then(() => true, again); });
      return { text: text(v), undo };
    }, again);
  }
  // The command's undo: the shell passes what the run answered.
  const undoOf = (o, r) => r && r.undo ? r.undo() : false;
  // build: "Open item" goes to the item on Tasks, by the shell's section map (no page names another page's address).
  const openItem = item => window.shell.openItem(item);
  C.register(
    window.commands.openPullRequest({ pr: o => ({ repo: `${o.owner}/${o.repo}`, number: o.pr, url: o.url }) }),
    { id: 'pr.item', on: 'pull request', label: 'Open item', does: 'Opens the work item this delivery names.', key: 'i', risk: 'safe', when: o => !!o.item || 'the delivery names no work item', cli: o => `sd task show ${o.item}`, run: o => openItem(o.item) },
    // build: the design's review.open; its review.lane re-runs a review, which a recorded one cannot ask for, so it is left out.
    window.commands.openPullRequest({ on: 'review', pr: o => ({ repo: `${o.owner}/${o.repo}`, number: o.pr, url: o.url }) }),
    { id: 'review.item', on: 'review', label: 'Open item', does: 'Opens the work item this review names.', key: 'i', risk: 'safe', when: o => !!o.item || 'the delivery names no work item', cli: o => `sd task show ${o.item}`, run: o => openItem(o.item) },
    window.commands.showAssignment(),
    { id: 'asg.item', on: 'assignment', label: 'Open item', does: 'Opens the work item this assignment works on.', key: 'i', risk: 'safe', when: o => !!o.item || 'the assignment names no item', cli: o => `sd task show ${o.item}`, run: o => openItem(o.item) },
    // Retry (commands.js): it posts /api/jobs/<job>/retry with the job's revision, as Operations > Jobs does; launchd starts
    // the run.
    window.commands.retry({ job: o => ({ name: o.job, service: o.service, failed: ev(o).failed, exit: ev(o).exit, capability: ev(o).retry }), run: retry }),
    // Show log (commands.js) opens the job's row on Management > Schedules, as ruled (sd:2392).
    window.commands.showLog({ job: o => ({ name: o.job }) }),
    // build (sd:3391): Reconcile outcome, from v1 Operations > Commands, which this page replaces. It posts
    // /api/palette/<note>/reconcile, which observes the target and never replays the command; no sd verb does it, so it has no
    // line to copy. It is the primary act on an unfinished palette record, so it registers ahead of Show output.
    { id: 'command.reconcile', on: 'command', label: 'Reconcile', does: 'Checks how the run ended and records it; it never reruns it.', key: 'r', risk: 'safe',
      // A page's command (sd:5541) is the dashboard's to end: it reads the run, or ends it unknown when it restarts.
      primary: o => ev(o).source === 'palette' && !ev(o).ended,
      when: o => ev(o).source === 'runner' ? "a runner record is the runner's to finish; no palette request waits on it"
        : ev(o).source === 'dashboard' ? 'a page\'s command is the dashboard\'s to finish; it records how the run ended'
        : ev(o).ended ? 'the run ended; its outcome is recorded' : true,
      run: o => landing(reconcile(o), out => out.unknown
        || `Outcome of note ${o.note} recorded · ${out.exit_code == null ? 'no exit code' : `exit ${out.exit_code}`}`) },
    // The journal (sd:2180). build: output reads /api/executions/<note>, as v1 Operations > Commands does.
    { id: 'command.output', on: 'command', label: 'Show output', does: 'Reads the run\'s saved output and shows it in Details.', key: 'o', icon: 'terminal', risk: 'safe', primary: () => true,
      when: o => ev(o).source === 'runner' ? 'a runner record keeps its log in the retained clone, not in the execution log directory'
        : ev(o).expired ? `the output expired ${ev(o).expired}` : true,
      run: o => landing(readOutput(o), () => `Output of note ${o.note} shown in Details`) },
    { id: 'command.item', on: 'command', label: 'Open item', does: 'Opens the work item this command record names.', key: 'i', risk: 'safe', when: o => !!o.item || 'the record names no item', cli: o => `sd task show ${o.item}`, run: o => openItem(o.item) },
  );
  // read_execution answers at most 64 KiB a read (sd:2416): a full page means more follows at next_offset, up to the
  // 2 MiB it keeps of an output.
  const OUTPUT_PAGE = 65536, OUTPUT_MAX = 2 * 1024 * 1024;
  async function readOutput(o) {
    try {
      let offset = 0, text = '', out;
      for (;;) {
        out = await window.shell.getJSON(`/api/executions/${o.note}?offset=${offset}`);
        text += out.output || '';
        const full = out.next_offset - offset === OUTPUT_PAGE;
        offset = out.next_offset;
        if (!full || offset >= OUTPUT_MAX) break;
      }
      OUTPUT[o.id] = { state: out.state, output: text, expired: !!out.output_expired };
    } catch (err) { OUTPUT[o.id] = { error: `The output was not read: ${err.message}` }; throw err; } finally { select(o.id, true); }
  }
  // The reconcile answers with the record as read_execution reads it: its first page goes to Details, as Show output's does.
  // A 4xx recorded nothing and says why. Any other failure, or no answer, may follow an outcome that was recorded, so the page
  // reads again and says the outcome is not known; a second Reconcile on a recorded outcome only reads it.
  async function reconcile(o) {
    let out;
    try { out = await window.shell.post(`/api/palette/${o.note}/reconcile`, {}); }
    catch (err) {
      if (err.status >= 400 && err.status < 500) { if (err.stale) load(); throw err; }
      load();
      return { unknown: `Not known whether note ${o.note} was reconciled: ${err.message}` };
    }
    OUTPUT[o.id] = { state: out.state, output: out.output || '', expired: !!out.output_expired };
    await reread();
    select(o.id, true);
    return out;
  }
  // A retry's outcome (sd:3391), worded as Management words it (sd:3388): a 4xx wrote nothing and says why; any other failure,
  // or no answer, may follow a kickstart that was sent, so the page reads again and the toast says the outcome is not known.
  async function retry(o) {
    let out;
    try { out = await window.shell.post(`/api/jobs/${encodeURIComponent(o.job)}/retry`, { revision: ev(o).revision }); }
    catch (err) {
      if (err.status >= 400 && err.status < 500) { if (err.stale) load(); throw err; }
      load();
      return { text: `Not known whether ${o.job} was retried: ${err.message}` };
    }
    await reread();
    return { text: window.commands.answered(out, o.job, 'retried', `Run started · ${o.job}`) };
  }

  document.addEventListener('shell:open', e => { if (EVENTS.some(v => v.id === e.detail)) select(e.detail, true); });
  document.addEventListener('shell:picked', e => tbody.querySelectorAll('tr[data-id]').forEach(tr => tr.toggleAttribute('data-picked', e.detail.includes(tr.dataset.id))));

  // ---------- Shapeshift bar (the shell's, sd:5539) ----------
  document.addEventListener('shell:filter', e => { if (e.detail.q !== F.q) { F.q = e.detail.q; page = 1; update(); } });
  // build: the shell owns j/k, Enter, Esc and ?row= through PAGE_LIST; Esc with nothing open clears the filters.
  window.PAGE_LIST = { rows: () => tbody.querySelectorAll('tr[data-id]'), current: () => selected,
    select: id => select(id, false), clear: () => { if (!active()) return false; clearAll(); return true; } };

  readURL();
  load();
});
