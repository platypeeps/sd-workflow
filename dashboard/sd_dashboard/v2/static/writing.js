// Writing (sd:2125). Built from the design source's products/system/designs/pages/writing.js at 2729265.
// build: pieces come from /api/writing through shell.read; Stage, Correct, Park and Revive post the routes the old item page
// posts. The draft editor's save and the evidence summaries have no reader or writer yet (see below).
// Page commands for the palette; the shell reads this when it loads.
const { html, put } = window.markup;
window.PAGE_COMMANDS = [
  { label: 'Capture an idea or find a piece', icon: 'lightbulb', key: '/', run: () => document.getElementById('shift-in').focus() },
  { label: 'Board view', icon: 'kanban', key: 'v b', run: () => window.writingView?.('board') },
  { label: 'Editor view for the selected piece', icon: 'pen-line', key: 'v e', run: () => window.writingView?.('editor') },
  { label: 'Show parked pieces', icon: 'archive', run: () => document.querySelector('#parked-seg [data-v="aside"]')?.click() },
];
window.PAGE_ATTENTION = { state: 'unknown', n: 0, what: 'pieces not read' };
// Realm views (sd:5064): Personal, then the dashboard.realms realms (SHELL_REALMS, from /ui/sections.js). A piece's realm is
// places.realm_of of its repo, as on Tasks; a realm view no piece is in hides once the pieces are read, but Personal stays
// (sd:5642). Board and editor are this page's panels and b and e its v keys, so a realm takes neither; nor btn or menu,
// the layout menu's view-btn and view-menu (sd:5641), which the shell would hide as panels.
// sd:7213: no All view. Every piece sits in one view: its realm's, else Personal, which takes a piece of no realm, of one
// not read (realm_of None) and of one that has no view here. An old #view=all link opens Personal; a stays unused, so
// the other views keep their keys. pack sd:7687: HOA comes last, the hoa checkout's pieces (places.realm_of), so they
// leave Personal as on Tasks; like a realm, it hides with no piece in it.
window.PAGE_VIEWS = [{ id: 'personal', label: 'Personal' },
  ...(window.SHELL_REALMS || []).filter(r => /^[a-z0-9][a-z0-9-]*$/.test(r.id) && !['personal', 'hoa', 'all', 'board', 'editor', 'btn', 'menu'].includes(r.id)).map(r => ({ id: r.id, label: r.label })),
  { id: 'hoa', label: 'HOA' }];
{
  const used = new Set(['b', 'e', 'a']);
  window.PAGE_VIEWS.forEach(v => { if (!v.key) { v.key = [...v.id].find(c => /[a-z]/.test(c) && !used.has(c)); used.add(v.key); } });
}
if (location.hash === '#view=all') history.replaceState(history.state, '', '#view=personal');

addEventListener('DOMContentLoaded', () => {
  const { ICON, toast, suggest, openPane } = window.shell;
  const C = window.shell.commands;
  const shq = s => `'${String(s ?? '').replace(/'/g, "'\\''")}'`; // shell-safe: single quotes, so $(), backticks and \ stay literal
    const utc = t => t ? String(t).slice(0, 16).replace('T', ' ') + ' UTC' : 'not recorded';
  const ago = s => { const n = Math.round(((Date.parse(D?.read) || Date.now()) - new Date(s)) / 86400000); return n <= 0 ? 'today' : `${n}d ago`; };

  // Stages: sd_db.writing.STAGES minus inbox and declined (no piece holds them); the vault inbox leads as "Ideas".
  const STAGES = [['accepted', 'Accepted'], ['researching', 'Researching'], ['drafting', 'Drafting'], ['review', 'Review'], ['ready', 'Ready'], ['published', 'Published']];
  const SL = Object.fromEntries(STAGES);
  let D = null, pieces = [], REALM = window.shell.view?.current() || 'personal';
  const viewOf = p => (p.realm !== 'personal' && window.PAGE_VIEWS.some(v => v.id === p.realm) ? p.realm : 'personal');
  const inRealm = (p, r = REALM) => viewOf(p) === r;
  // Another realm reads again quietly, so the reader's objects and first pick follow the pieces the view shows.
  document.addEventListener('shell:view', e => { if (e.detail.id === REALM) return; REALM = e.detail.id; if (D) reading.load(true); });
  const byId = id => pieces.find(p => p.id === +id);

  // ---------- Gate lamps from sd_db.writing._problems ----------
  const GLYPH = { ok: '●', caution: '▲', warning: '■', queued: '◌', unknown: '▨' };
  function gates(p) {
    const has = s => p.problems.filter(x => x.includes(s));
    const L = [];
    const file = n => p.files[n];
    // Draft
    L.push(has('draft is missing').length ? ['D', 'Draft', 'queued', 'Not written yet (index.md has no draft).'] : ['D', 'Draft', 'ok', `${p.words.toLocaleString()} words`]);
    // Research
    const r = has('research.md');
    if (!r.length) L.push(['R', 'Research', 'ok', 'Stamped against this draft.']);
    else L.push(['R', 'Research', file('research.md') ? 'caution' : 'queued', file('research.md') ? 'research.md is unstamped or stale against this draft.' : 'research.md is missing.']);
    for (const [k, name, fn] of [['F', 'Fact-check', 'fact-check'], ['A', 'Adversarial', 'adversarial']]) {
      const pr = has(fn);
      if (!pr.length) { L.push([k, name, 'ok', 'Verdict pass, recorded against this draft.']); continue; }
      if (!file(fn + '.md')) { L.push([k, name, 'queued', `${fn}.md is missing.`]); continue; }
      if (pr.some(x => x.includes('verdict is not pass'))) { L.push([k, name, 'warning', `Verdict is not pass; ${pr.filter(x => x.includes('unresolved')).length} findings unresolved.`]); continue; }
      const open = pr.filter(x => x.includes('unresolved'));
      if (open.length) { L.push([k, name, 'caution', `${markup.plural(open.length, 'finding')} unresolved: ${open.map(x => x.match(/finding (\S+)/)[1]).join(', ')}.`]); continue; }
      if (pr.some(x => x.includes('no explicit current gate record'))) { L.push([k, name, 'unknown', `${fn}.md exists, but no gate verdict is recorded for this draft.`]); continue; }
      L.push([k, name, 'caution', pr.join('; ')]);
    }
    return L;
  }
  const worst = L => L.some(l => l[2] === 'warning') ? 'warning' : L.some(l => l[2] === 'caution') ? 'caution' : '';
  const lamps = p => html`<span class="lamps" role="img" aria-label="${gates(p).map(([, n, s]) => `${n} ${s}`).join(', ')}">${gates(p).map(([k, , s]) => html`<span class="lamp" data-s="${s}"><span class="g" aria-hidden="true">${GLYPH[s]}</span>${k}</span>`)}</span>`;

  // ---------- Stage rules: sd_db.writing._normal and change_stage ----------
  // build: the dashboard's stage and park routes require row ownership (require_row), so a file-owned piece moves nowhere here.
  const owned = p => p.owner === 'row' || (p.owner === 'retiring' ? 'writing cutover is in progress' : 'the piece files own this piece; the dashboard moves it after the writing cutover');
  function legal(p, to) {
    if (p.stage === to) return { ok: false, same: true, reason: 'Already here.' };
    if (p.parked) return { ok: false, reason: 'Parked. Revive it first; a parked piece keeps its stage.' };
    if (owned(p) !== true) return { ok: false, reason: `${owned(p)[0].toUpperCase()}${owned(p).slice(1)}.` };
    if (to === 'ideas') return { ok: false, reason: 'A piece does not return to the vault inbox. Park it, or correct it to an earlier stage.' };
    if (to === 'published') return { ok: false, reason: 'Publishing runs through Publish after the ready gate, with a recorded URL.' };
    if (p.next.includes(to)) {
      if (to === 'ready' && !p.gates_ok) return { ok: false, reason: `Ready gate refused: ${markup.plural(p.problems.length, 'problem')} (${p.problems[0]}${p.problems.length > 1 ? ' …' : ''}).` };
      return { ok: true, fwd: true };
    }
    if (p.corrections.includes(to)) return { ok: true, correct: true };
    return { ok: false, reason: `From ${SL[p.stage] || p.stage} a piece moves to ${p.next.map(s => SL[s]).join(' or ') || 'nothing'}; a correction goes back to ${p.corrections.map(s => SL[s]).join(', ') || 'nothing'}.` };
  }
  function publishState(p) {
    const why = [];
    if (p.parked) why.push('The piece is parked.');
    if (p.publication) why.push(`Already published (claim ${String(p.publication.claim).slice(0, 8)}).`);
    else if (p.stage !== 'ready') why.push(`Stage is ${SL[p.stage] || p.stage}; publishing needs Ready.`);
    if (!p.gates_ok) why.push(`The ready gate fails: ${markup.plural(p.problems.length, 'problem')}.`);
    if (p.stage === 'ready' && p.gates_ok && !p.ready_recorded && !p.publication) why.push('No readiness decision is recorded for this draft. Stage it to Review and back to Ready; with gates passing, that records the digest publishing checks.');
    return { ok: !why.length, why };
  }

  // ---------- Strip ----------
  // Rail badge (shell.js): the read's own, the loudest gate state across active pieces (sd:3446, writing_screen._attention).
  function renderStrip() {
    window.PAGE_ATTENTION = D && D.attention || { state: 'unknown', n: 0, what: 'pieces not read' }; window.shell?.attention?.(window.PAGE_ATTENTION);
    if (!D) { put(document.getElementById('strip'), html``); return; }
    const act = pieces.filter(p => !p.parked);
    const bad = act.filter(p => !p.gates_ok).length;
    const pub = act.filter(p => publishState(p).ok).length;
    put(document.getElementById('strip'), html`<span class="fact"><b>${act.length}</b> active</span>
       <span class="fact"><span class="g-caution" aria-hidden="true">▲</span><b>${bad}</b> gates want you</span>
       <span class="fact"><b>${act.filter(p => p.stage === 'ready').length}</b> at Ready, <b>${pub}</b> publishable</span>
       ${D.ideas?.state === 'read' ? html`<span class="fact"><b>${D.ideas.count}</b> ideas in the vault inbox</span>` : html`<span class="fact" title="${D.ideas?.reason || 'the read gave no ideas'}"><span class="g-unknown" aria-hidden="true">▨</span>vault ideas not read</span>`}
       <button class="help" type="button" aria-label="Help: lamps" data-help="<b>D R F A</b>: draft, research, fact-check, adversarial, in that order on every card. <b>●</b> passes against this draft, <b>▲</b> stale or findings open, <b>■</b> verdict not pass, <b>◌</b> not written yet, <b>▨</b> file exists but no gate verdict is recorded.">${ICON('circle-help')}</button>
       <span class="src">read ${String(D.read).slice(11, 16)} UTC</span>`);
    const parkedN = pieces.filter(p => p.parked).length;
    put(document.getElementById('parked-seg'), window.shell.aside.buttons('parked', 'Parked', parked, { open: pieces.length - parkedN, aside: parkedN }));
  }

  // ---------- Board ----------
  // selected is the piece the editor and the URL follow.
  // parked: Open | Parked (sd:6701), kept as ?parked=.
  let selected = null, parked = window.shell.aside.mode('parked'), view = 'board';
  // pack sd:7670: the Ideas column's Area tile, '' for none pressed; writing_screen.AREAS.
  const IDEA_AREAS = ['Personal', 'Work', 'RWBP'];
  let ideaArea = '';
  const keep = () => { const v = window.shell.aside.param(parked); return v ? { parked: v } : {}; };
  function card(p) {
    const L = gates(p), st = worst(L);
    return html`<article class="card${p.parked ? ' parked' : ''}" tabindex="0" role="option" data-id="${p.id}" aria-selected="${String(p.id === selected)}"${st ? html` data-state="${st}"` : ''} aria-label="#${p.id} ${p.title}${p.parked ? ', parked' : ''}">
      <div class="top"><span class="id">#${p.id}</span>${p.parked ? html`<span class="tag">parked</span>` : ''}${p.publication ? html`<span class="tag">published</span>` : ''}${lamps(p)}</div>
      <div class="t">${p.title}</div>
      <div class="meta"><span>${p.piece.split('/').pop()}</span><span>${p.words ? p.words.toLocaleString() + ' w' : 'no draft'}</span><span title="${p.updated}">${ago(p.updated)}</span></div>
      <div class="act">${C.rowActions(String(p.id))}</div>
    </article>`;
  }
  function renderBoard() {
    const v = pieces.filter(p => window.shell.aside.shows(parked, p.parked));
    // pack sd:7244: the inbox blog ideas, highest score first, from writing_screen's `ideas`. An idea card is no piece: it
    // takes no selection, and its title links out only where the read gave an http href. The drop zone refuses a piece.
    const I = D?.ideas || { state: 'unknown', reason: 'the read gave no ideas', rows: [] };
    // pack sd:7670: Personal, Work and RWBP tiles, then No area while an idea has none; `rows` holds each area's highest,
    // and with no tile pressed the column shows the first `top` of them, the highest overall.
    const nArea = a => (I.areas || {})[a === 'none' ? '' : a] || 0;
    const areaTiles = [...IDEA_AREAS.map(a => [a, a]), ...(nArea('none') ? [['none', 'No area']] : [])];
    // pack sd:7695: a pressed tile a read again no longer draws (No area once every idea has one) clears, else the column
    // stays narrowed to nothing with no tile to press off.
    if (ideaArea && !(I.state === 'read' && I.count && areaTiles.some(([a]) => a === ideaArea))) ideaArea = '';
    const ideasShown = ideaArea ? I.rows.filter(i => (ideaArea === 'none' ? !i.area : i.area === ideaArea)) : I.rows.slice(0, I.top || I.rows.length);
    const ofWhat = !ideaArea ? `${I.count} inbox ideas` : ideaArea === 'none' ? `${nArea('none')} ideas with no area` : `${nArea(ideaArea)} ${ideaArea} ideas`;
    const idea = i => html`<article class="card idea" aria-label="Idea: ${i.title}"><div class="top"><span class="id">${i.created || ''}</span>${i.area ? html`<span class="area">${i.area}</span>` : ''}<span class="tag">${i.score == null ? 'no score' : `${i.score}/10`}</span></div>
      <div class="t">${i.href ? html`<a class="ext" href="${i.href}" target="_blank" rel="noopener noreferrer">${i.title}</a>` : i.title}</div></article>`;
    const ideaCol = html`<div class="col vault" id="ideas" data-drop="stage:ideas">
      <header><span class="label">Ideas · vault</span>${I.state === 'read' ? html`<span class="n" title="inbox ideas in the vault">${I.count}</span>` : html`<span class="n" title="vault ideas not read">—</span>`}</header>
      ${I.state === 'read' && I.count ? html`<div class="idea-areas">${window.shell.list.fseg('area', 'Area', areaTiles.map(([a, w]) => ({ v: a, pressed: ideaArea === a,
        title: `${w}: ${nArea(a)}`, label: html`${w} <span class="n">${nArea(a)}</span>` })))}</div>` : ''}
      <div><p class="refusal" role="note"></p><div class="drop">
        ${I.state === 'read' ? html`${ideasShown.map(idea)}<p class="empty">${I.count ? `Highest ${ideasShown.length} of ${ofWhat}.` : 'No idea in the vault inbox.'} <a href="/tasks?base=blog-idea">Tasks</a> lists them all.</p>`
          : html`<p class="empty">Not read: ${I.reason}</p>`}
      </div></div></div>`;
    // sd:8013: the inbox tips beside the ideas, the home Today's Tips link opens; no piece drops here.
    const T = D?.tips || { state: 'unknown', reason: 'the read gave no tips', rows: [] };
    const tipCol = html`<div class="col vault" id="tips">
      <header><span class="label">Tips · vault</span>${T.state === 'read' ? html`<span class="n" title="inbox tips in the vault">${T.count}</span>` : html`<span class="n" title="vault tips not read">—</span>`}</header>
      <div><div class="drop">
        ${T.state === 'read' ? html`${T.rows.slice(0, T.top || T.rows.length).map(idea)}<p class="empty">${T.count ? `Highest ${Math.min(T.count, T.top || T.count)} of ${T.count} inbox tips.` : 'No tip in the vault inbox.'} <a href="/tasks?base=tip">Tasks</a> lists them all.</p>`
          : html`<p class="empty">Not read: ${T.reason}</p>`}
      </div></div></div>`;
    put(document.getElementById('view-board'), html`<div class="board" id="board" role="listbox" aria-label="Pieces by stage">${ideaCol}${tipCol}${STAGES.map(([s, name]) => {
      const rows = v.filter(p => p.stage === s);
      return html`<div class="col" data-drop="stage:${s}">
        <header><span class="label">${name}</span><span class="n">${rows.length}</span></header>
        <div><p class="refusal" role="note"></p><div class="drop">${rows.length ? rows.map(card) : html`<p class="empty">Nothing ${name.toLowerCase()}.</p>`}</div></div>
      </div>`;
    })}</div>${window.shell.list.help('Four lamps per piece: draft, research, fact-check, adversarial.')}`);
  }

  // ---------- Details ----------
  function renderDetails() {
    const p = byId(selected), el = document.getElementById('details');
    if (!p) { put(el, html`<p class="note">${D ? 'Select a piece to see its gates and moves.' : 'The pieces were not read. Reload retries it.'}</p>`); return; }
    const L = gates(p), P = publishState(p);
    put(el, html`<div class="kind"><span class="label">Piece</span><span class="tag">observed</span></div>
      <h2>${p.title}</h2>
      <dl>
        <dt>Id</dt><dd>#${p.id}</dd>
        <dt>Piece</dt><dd>${p.piece}</dd>
        <dt>Stage</dt><dd>${SL[p.stage] || p.stage}${p.parked ? ' · parked' : ''}</dd>
        <dt>Draft</dt><dd>${p.words ? p.words.toLocaleString() + ' words' : 'none'}</dd>
        <dt>Updated</dt><dd>${utc(p.updated)}</dd>
        <dt>Readiness</dt><dd>${p.ready_recorded ? 'recorded for this draft' : 'not recorded'}</dd>
        ${p.publication ? html`<dt>Published</dt><dd>${p.publication.href ? html`<a class="ext" href="${p.publication.href}" target="_blank" rel="noopener noreferrer">${p.publication.url}</a>` : html`<code>${p.publication.url}</code>`}</dd>` : ''}
      </dl>
      <h3 class="label">Gates</h3>
      <ul class="gates">${L.map(([k, n, s, w]) => html`<li><span class="g g-${s}" aria-hidden="true">${GLYPH[s]}</span><span>${n}</span><span class="what">${w}</span><span class="acts">${gateActs(p, k, n)}</span></li>`)}</ul>
      <div class="publish">
        <span class="label">Publish gate</span>
        ${P.ok ? html`<p class="why">Open: stage Ready, gates pass, readiness recorded for this draft. Publish is copy only, since it posts outside: <code>sd writing publication-claim</code>, then <code>publication-dispatch</code> on the recorded claim.</p>` : html`<p class="why">Closed. Publish stays off until each of these clears:</p><ul>${P.why.map(w => html`<li>${w}</li>`)}</ul>`}
      </div>
      ${grillPane(p)}
      <h3>Act</h3>${C.bar(String(p.id))}
      <p class="foot">Observed ${utc(D.read)} from the workflow database and the piece files in <code>${p.repo}/${p.folder}/${p.piece}/</code>.</p>`);
  }

  // pack sd:7242: each gate's refresh and view icons. Draft reads the pieces again and shows the draft; Adversarial runs
  // Refresh review. No route runs research or a fact-check, so theirs copies the line that does, run in the content
  // repository. A report's view opens it read only in the editor's evidence pane; a missing one's view is off.
  const GATE = { R: ['research', 'research.md'], F: ['factcheck', 'fact-check.md'], A: ['adversarial', 'adversarial.md'] };
  const cmdIcon = (id, p, icon, label) => {
    const why = C.offWhy(CMDS.find(c => c.id === id), { id: String(p.id) }), say = why ? `${label}: ${why}` : label;
    return html`<button class="icon-btn" type="button" data-cmd="${id}" data-obj="${p.id}" aria-label="${say}" title="${say}"${why ? html` disabled` : ''}>${ICON(icon)}</button>`;
  };
  // pack sd:7243: each file's Drive copy, from Review on, in the account its realm picks. A link once a drain delivered
  // one; off with the reason until then: queued, not mirrored at this stage, missing, or unknown.
  function driveIcon(p, file, n) {
    const m = p.mirror || {}, c = (m.copies?.[file] || [])[0];
    if (c?.url) {
      const say = `${n}: open the Drive copy${c.state === 'queued' ? '; an update is queued' : ''}`;
      return html`<a class="icon-btn" href="${c.url}" target="_blank" rel="noopener" data-drive="${file}" aria-label="${say}" title="${say}">${ICON('arrow-up-right')}</a>`;
    }
    const why = m.unread ? `unknown: ${m.unread}` : c ? 'queued, not yet drained' : !p.files[file] ? 'missing'
      : m.folder ? `not drained yet; it goes to ${m.folder}` : 'none; a piece mirrors from Review on';
    const say = `${n}: Drive copy ${why}`;
    return html`<button class="icon-btn" type="button" data-drive="${file}" aria-label="${say}" title="${say}" disabled>${ICON('arrow-up-right')}</button>`;
  }
  function gateActs(p, k, n) {
    if (k === 'D') return html`<button class="icon-btn" type="button" data-gate-again="" aria-label="Draft: read the pieces again" title="Draft: read the pieces again">${ICON('rotate-ccw')}</button>${cmdIcon('piece.draft', p, 'eye', 'Draft: Show draft')}${driveIcon(p, 'index.md', n)}`;
    const line = { R: `claude "/sd-research piece=${p.piece}"`, F: `claude "/sd-fact-check input=${p.folder}/${p.piece}/index.md"` }[k];
    const again = line ? html`<button class="icon-btn" type="button" data-copy="${line}" aria-label="${n}: no route runs it; copy the line to run in ${p.repo}" title="Copy, to run in ${p.repo}: ${line}">${ICON('copy')}</button>`
      : cmdIcon('piece.review', p, 'rotate-ccw', 'Adversarial: Refresh review');
    const [tab, file] = GATE[k], say = p.files[file] ? `${n}: view ${file}` : `${n}: missing`;
    return html`${again}<button class="icon-btn" type="button" data-gate-view="${tab}" aria-label="${say}" title="${say}"${p.files[file] ? '' : html` disabled`}>${ICON('eye')}</button>${driveIcon(p, file, n)}`;
  }

  // pack sd:6700: the first open grill question, with a button per option or a box for words.
  function grillPane(p) {
    const g = p.grill || { open: [], answered: 0 }, q = g.open[0];
    if (!q) return g.answered ? html`<h3 class="label">Grill</h3><p class="why">${g.answered} answered, none open. Apply answers edits the draft to follow them.</p>` : '';
    return html`<h3 class="label">Grill</h3><div class="grill" aria-label="Grill question">
      <p class="why">Question 1 of ${g.open.length}${g.answered ? ` · ${g.answered} answered` : ''}</p>
      <p class="q">${q.question}</p>
      ${q.options.length ? html`<div class="actions">${q.options.map(o => html`<button class="btn sm" type="button" data-grill-answer="${o}">${o}</button>`)}</div>`
        : html`<label class="label" for="grill-text">Your answer</label><textarea id="grill-text" rows="3"></textarea>
          <div class="actions"><button class="btn sm" type="button" data-grill-answer="">Answer</button></div>`}
    </div>`;
  }

  // ---------- Editor ----------
  // build: read only. No route writes a draft, so the design's Edit button and its save state are not drawn. The evidence
  // tabs show each file's facts, its lamp and its text, read once per change time from /api/writing/<id>/<gate> (pack sd:7242).
  // The change time is whole seconds, so two versions in one second share a key (pack sd:7328): every read of the pieces
  // drops the cache, and a read that answers after that drop does not fill it.
  let evTab = 'research';
  const reports = new Map();
  function evText(p, file, gate) {
    const key = `${p.id}/${file}/${p.files[file].mtime}`, got = reports.get(key), tab = evTab;
    if (!got) {
      const pending = { state: 'reading' };
      reports.set(key, pending);
      window.shell.getJSON(`/api/writing/${p.id}/${gate}`).then(r => ({ state: 'read', ...r }), e => ({ state: 'failed', reason: e.message }))
        .then(r => { if (reports.get(key) !== pending) return; reports.set(key, r); if (view === 'editor' && +selected === p.id && evTab === tab) renderEditor(); });
    }
    const r = reports.get(key);
    if (r.state === 'reading') return html`<p>Reading ${file}…</p>`;
    if (r.state === 'failed') return html`<p>${file} not read: ${r.reason}. Reloading the page retries it.</p>`;
    return html`<pre class="ev-text" tabindex="0" aria-label="${file}, read only">${r.text}</pre>${r.cut ? html`<p>The first ${r.text.length.toLocaleString()} characters; the rest stays in the file.</p>` : ''}`;
  }
  function renderEditor() {
    const p = byId(selected), el = document.getElementById('view-editor');
    if (!p) { put(el, html`<p class="note">${D ? 'Select a piece on the board first.' : 'The pieces were not read. Reload retries it.'}</p>`); return; }
    const P = publishState(p), fx = p.files;
    const fm = n => fx[n] ? `${(fx[n].bytes / 1024).toFixed(1)} KB · ${utc(fx[n].mtime)}` : 'missing';
    const g = gates(p);
    const ev = { research: ['research.md', 1, 'research'], factcheck: ['fact-check.md', 2, 'fact-check'], adversarial: ['adversarial.md', 3, 'adversarial'] };
    const [file, at, gate] = ev[evTab];
    put(el, html`<div class="ed-head">
        <button class="btn quiet sm" type="button" id="back">${ICON('chevron-left')}Board</button>
        <h2>#${p.id} ${p.title}</h2>${lamps(p)}
        <span class="save" id="save" data-s="saved" role="status">${ICON('check')}Read only · the file on disk</span>
        <button class="btn sm" type="button" data-cmd="piece.publish" data-obj="${p.id}"${P.ok ? '' : html` disabled aria-describedby="ed-why"`}>${ICON('send')}Publish</button>
      </div>
      ${P.ok ? '' : html`<p class="why ed-why" id="ed-why">Publish is off: ${P.why[0]}${P.why.length > 1 ? ` (+${P.why.length - 1} more in Details)` : ''}</p>`}
      <div class="editor">
        <div class="draft">
          <p class="path">${p.repo}/${p.folder}/${p.piece}/index.md · ## Draft</p>
          <article class="page" aria-label="Draft">${p.draft.length ? html`<div id="draft-text" role="textbox" aria-multiline="true" aria-label="Draft text, #${p.id}" aria-readonly="true" tabindex="0">${p.draft.map(x => html`<p>${x.replace(/\[([^\]]+)\]\([^)]+\)/g, '$1')}</p>`)}</div><p class="more">${p.draft_cut ? `The first ${markup.plural(p.draft.length, 'paragraph')} of ${p.words.toLocaleString()} words; the rest stays in the file.` : `The whole draft, ${p.words.toLocaleString()} words. Edit it in its file.`}</p>` : html`<p class="more">No draft yet. <code>sdw-research</code> comes first, then <code>sdw-draft</code>.</p>`}</article>
        </div>
        <aside class="evidence" aria-label="Evidence">
          <div class="seg" role="group" aria-label="Evidence file">${[['research', 'Research'], ['factcheck', 'Fact-check'], ['adversarial', 'Adversarial']].map(([k, n]) => html`<button type="button" data-ev="${k}" aria-pressed="${String(evTab === k)}">${n}</button>`)}</div>
          <div class="ev"><h3 class="label">${file}</h3>${fx[file] ? html`<dl><dt>File</dt><dd>${fm(file)}</dd><dt>Lamp</dt><dd>${GLYPH[g[at][2]]} ${g[at][3]}</dd></dl>${evText(p, file, gate)}` : html`<p>${file} is missing.</p>`}</div>
          <p class="why">An edit changes the draft digest, so every gate goes stale until research is re-stamped and both verdicts are recorded again.</p>
        </aside>
      </div>`);
    el.querySelector('#back')?.addEventListener('click', () => setView('board'));
    el.querySelectorAll('[data-ev]').forEach(b => b.addEventListener('click', () => { evTab = b.dataset.ev; renderEditor(); }));
  }

  function render() { renderStrip(); if (view === 'board') renderBoard(); else renderEditor(); renderDetails(); }

  // ---------- Commands (products/system/commands.md): stage, stage --correct, park, park --revive, readiness, publish ----------
  // build: each write posts the old item page's route with the piece's revision, then rereads; a refused write reads again.
  // Stage has no Undo: going back is a correction, which takes a reason and only the operator may make. Park and Revive are
  // each other's inverse, so each undoes the other with the revision its own write returned.
  let target = null, reading = null;
  const P_ = o => byId(o.id);
  // pack sd:6977: the report's digest stamp says whether it answers this draft; file times do not.
  const reviewed = p => { const a = p.adversarial; return a?.state === 'current' || `adversarial.md is ${a ? `${a.state}: ${a.detail}` : 'not readable'}; run Refresh review first`; };
  const nextOf = p => target && p.next.includes(target) ? target : p.next[0];
  const write = (p, suffix, body) => window.shell.post(`/api/items/${p.id}/${suffix}`, { ...body, revision: p.revision })
    .then(out => { reading?.reread(); return out; }, e => { reading?.load(); throw e; });
  const parkWrite = (o, parked) => { const p = P_(o); return write(p, parked ? 'park' : 'revive', {})
    .then(out => ({ text: parked ? `#${p.id} parked` : `#${p.id} revived at ${SL[p.stage] || p.stage}`, revision: out.revision })); };
  const parkUndo = parked => (o, v) => window.shell.post(`/api/items/${o.id}/${parked ? 'revive' : 'park'}`, { revision: v.revision })
    .then(() => reading?.reread(), e => { reading?.load(); throw e; });
  const asked = p => { const n = p.grill?.open.length; return n ? `${markup.plural(n, 'grill question')} open; answer them first` : ''; };
  // A refused stale press reads again too, as `write` does, so the next press carries the revision the page now holds.
  const fixPost = (o, path, body) => window.shell.post(path, { ...body, revision: P_(o).revision })
    .then(out => { reading?.reread(); return out; }, e => { if (e.stale) reading?.load(); throw e; });
  const fixAgain = () => reading?.load();
  // An answer is Decide's: /api/decisions/answer writes the ruling and resolves the question under the piece's revision.
  // pack sd:7242: a gate's view opens its report in the editor; Draft's refresh reads the pieces again.
  document.getElementById('details').addEventListener('click', e => {
    const v = e.target.closest?.('[data-gate-view]');
    if (v) { evTab = v.dataset.gateView; setView('editor'); return; }
    if (e.target.closest?.('[data-gate-again]')) { reading?.load(); return; }
    const b = e.target.closest?.('[data-grill-answer]'), p = byId(selected), q = p?.grill?.open[0];
    if (!b || !q) return;
    const option = (b.dataset.grillAnswer || document.getElementById('details').querySelector('#grill-text')?.value || '').trim();
    if (!option) { toast('Type an answer first.'); return; }
    window.shell.post('/api/decisions/answer', { item: p.id, note: q.note, option, revision: p.revision })
      .then(() => reading?.reread(), err => { toast(`#${p.id} not answered: ${err.message}`); reading?.load(); });
  });
  const CMDS = [
    { id: 'piece.stage', on: 'piece', label: 'Stage', does: 'Moves the piece to its next stage; going back needs a reason.', key: 's', icon: 'arrow-up-right', risk: 'confirm',
      cli: o => `sd writing stage --piece ${P_(o).piece} --stage ${nextOf(P_(o)) || '…'}`,
      primary: () => true,
      when: o => { const p = P_(o); if (!p) return 'not listed'; if (p.parked || owned(p) !== true) return legal(p, '').reason;
        const to = nextOf(p); if (!to) return 'no forward stage from here'; const V = legal(p, to); return V.ok || V.reason; },
      consequence: o => { const p = P_(o), to = nextOf(p); return `Moves #${p.id} from ${SL[p.stage] || p.stage} to ${SL[to] || to}${to === 'ready' ? ' and records readiness for this draft' : ''}. Going back is a correction with a reason.`; },
      run: o => { const p = P_(o), to = nextOf(p), from = p.stage; target = null;
        return write(p, 'stage', { stage: to }).then(() => `#${p.id} ${SL[from] || from} → ${SL[to] || to}${to === 'ready' ? ' · readiness recorded' : ''}`); } },
    { id: 'piece.correct', on: 'piece', label: 'Correct', does: 'Moves the piece back to an earlier stage, with a reason.', key: 'o', icon: 'undo-2', risk: 'safe', fills: true,
      cli: o => `sd writing stage --piece ${P_(o).piece} --stage ${target && P_(o).corrections.includes(target) ? target : (P_(o).corrections.at(-1) || '…')} --correct --reason "…"`,
      when: o => { const p = P_(o); if (!p) return 'not listed'; if (owned(p) !== true) return owned(p); return p.parked ? 'parked; revive first' : p.corrections.length ? true : 'no earlier stage to correct to'; },
      run: o => { const p = P_(o), to = target && p.corrections.includes(target) ? target : null; target = null; askReason(p, to); return null; } }, // the dialog's Correct is the write
    { id: 'piece.park', on: 'piece', label: 'Park', does: 'Parks the piece off the active board; Undo revives it.', key: 'p', icon: 'archive', risk: 'undo', bulk: true,
      cli: o => `sd writing park --piece ${P_(o).piece}`,
      when: o => { const p = P_(o); if (!p) return 'not listed'; if (owned(p) !== true) return owned(p); return p.publication ? 'published pieces stay on the board' : !p.parked || 'already parked'; },
      run: o => parkWrite(o, true), undo: parkUndo(true) },
    { id: 'piece.revive', on: 'piece', label: 'Revive', does: 'Brings a parked piece back to the board; Undo parks it again.', key: 'v', icon: 'archive-restore', risk: 'undo', bulk: true,
      cli: o => `sd writing park --piece ${P_(o).piece} --revive`,
      primary: o => !!P_(o)?.parked,
      when: o => { const p = P_(o); if (!p) return 'not listed'; if (owned(p) !== true) return owned(p); return p.parked || 'not parked'; },
      run: o => parkWrite(o, false), undo: parkUndo(false) },
    { id: 'piece.readiness', on: 'piece', label: 'Readiness', does: 'Checks the piece\'s review gates and names the first problem.', key: 'r', icon: 'shield-check', risk: 'safe',
      cli: o => `sd writing readiness --piece ${P_(o).piece}`,
      run: o => { const p = P_(o); return p.gates_ok ? `#${p.id}: review checks pass` : `#${p.id}: ${markup.plural(p.problems.length, 'gate problem')}; first: ${p.problems[0]}`; } },
    { id: 'piece.publish', on: 'piece', label: 'Publish', does: 'Gives the two lines that publish the piece; copy only, since it posts outside.', key: 'u', icon: 'send', risk: 'confirm',
      // sd writing publication-claim writes the claim (--piece, --context-file, --payload-file); dispatch then publishes it.
      // build: copy only, with the reference's own executes: false: publishing posts outside, so it stays the operator's (sd:5887).
      executes: false,
      cli: o => `sd writing publication-claim --piece ${P_(o).piece} --context-file context.json --payload-file payload.json\nsd writing publication-dispatch --piece ${P_(o).piece} --claim <claim> --context-file context.json --confirmed`,
      primary: o => !!P_(o) && publishState(P_(o)).ok,
      when: o => { const P = publishState(P_(o)); return P.ok || P.why.join(' '); },
      consequence: () => 'Gives you two commands that publish the piece, main site first, and record its URL. The dashboard sends nothing outside: copy the two lines.',
      run: o => `Copy the two lines to publish #${P_(o).id}; publishing posts outside, so the dashboard does not run them` },
    // pack sd:7242: the row menu's and the palette's way to the draft; Details show it in their bar. The editor is the answer.
    { id: 'piece.draft', on: 'piece', label: 'Show draft', does: 'Selects the piece and opens its whole draft in the read-only editor.', key: 'd', icon: 'file-text', risk: 'safe',
      cli: o => `no CLI: the draft is the ## Draft section of ${P_(o).repo}/${P_(o).folder}/${P_(o).piece}/index.md`,
      when: o => { const p = P_(o); return !p ? 'not listed' : p.draft.length ? true : 'no draft yet'; },
      run: o => { select(o.id); setView('editor'); return null; } },
    // pack sd:6700: Grill writes the piece's questions as notes; Details walks them; Apply answers edits the draft to follow them.
    // pack sd:7783: a launch that failed, or gave no answer, may have noted its cause on the piece: `fixAgain` reads again.
    window.commands.grill('piece', 'ask', { item: o => P_(o).id, post: fixPost, again: fixAgain,
      ready: o => { const p = P_(o); return !p ? 'not listed' : !p.words ? 'no draft to grill yet' : asked(p) || true; } }),
    window.commands.grill('piece', 'apply', { item: o => P_(o).id, post: fixPost, again: fixAgain,
      ready: o => { const p = P_(o); return !p ? 'not listed' : asked(p) || (p.grill?.answered ? true : 'no grill question answered yet; press Grill first'); } }),
    // pack sd:6699: Refresh review is the shared adversarial review with kind piece; it rewrites the piece's adversarial.md.
    window.commands.adversarialReview('piece', { id: 'piece.review', label: 'Refresh review',
      does: 'Starts a background review of the draft; it replaces the last review.',
      payload: o => ({ kind: 'piece', target: String(o.id) }),
      when: o => { const p = P_(o); return !p ? 'not listed' : p.words ? true : 'no draft to review yet'; } }),
    // Apply review is the shared Fix with apply: adversarial; the page reads again once the session starts.
    window.commands.applyReview('piece', { item: o => P_(o).id,
      ready: o => { const p = P_(o); return !p ? 'not listed' : reviewed(p); },
      post: fixPost, again: fixAgain }),
  ];
  // A piece's review that lands rewrites adversarial.md, so the page reads again for its time.
  document.addEventListener('adversarial:run', e => { if (e.detail.run.kind === 'piece' && e.detail.run.filed) reading?.reread(); });
  C.register(...CMDS);
  const cmd = id => CMDS.find(c => c.id === id);
  function move(id, to) {
    const p = byId(id), V = legal(p, to);
    if (!V.ok) { if (!V.same) toast(`#${p.id} not moved: ${V.reason}`); return; }
    target = to;
    C.run(cmd(V.correct ? 'piece.correct' : 'piece.stage'), C.get(String(id)));
  }
  // Correction reason (change_stage --correct needs one; only the operator may correct).
  const dlg = document.createElement('dialog'); dlg.className = 'palette dlg'; dlg.setAttribute('aria-labelledby', 'dlg-h'); put(dlg, html`<h2 id="dlg-h"></h2>`); document.body.append(dlg);
  function correct(p, to, reason) {
    return write(p, 'stage', { stage: to, correct: true, reason }).then(() => toast(`#${p.id} corrected to ${SL[to] || to}`), e => toast(`#${p.id} not changed: ${e.message}`));
  }
  function askReason(p, to) {
    const opts = p.corrections;
    put(dlg, html`<form method="dialog" class="dlg-form"><h2 id="dlg-h">Correct #${p.id}</h2>${window.shell.closeX}
      <p class="why">A correction sends the piece back and records why. Its review checks must be renewed afterwards.</p>
      <label class="label" for="to-in">Back to</label><select id="to-in">${opts.map(s => html`<option value="${s}"${s === (to || opts.at(-1)) ? html` selected` : ''}>${SL[s]}</option>`)}</select>
      <label class="label" for="why-in">Reason</label><textarea id="why-in" rows="3" required></textarea>
      <div class="cli"><code id="corr-cli"></code></div>
      <div class="actions"><button class="btn quiet" value="cancel" type="submit" formnovalidate data-cancel>Keep it</button><button class="btn" value="ok" type="submit">Correct</button></div></form>`);
    const sel = dlg.querySelector('#to-in'), why = dlg.querySelector('#why-in'), cli = dlg.querySelector('#corr-cli');
    const upd = () => { cli.textContent = `sd writing stage --piece ${p.piece} --stage ${sel.value} --correct --reason ${shq(why.value)}`; };
    sel.addEventListener('change', upd); why.addEventListener('input', upd); upd();
    dlg.onclose = () => { if (dlg.returnValue === 'ok' && why.value.trim()) correct(p, sel.value, why.value.trim()); else toast(`#${p.id} not moved.`); };
    dlg.returnValue = ''; dlg.showModal(); why.focus();
  }

  // ---------- Selection ----------
  const curId = () => selected == null ? null : String(selected);
  function select(id, open) {
    selected = +id;
    document.querySelectorAll('.card[data-id]').forEach(n => n.setAttribute('aria-selected', n.dataset.id === curId()));
    if (view === 'editor') renderEditor();
    renderDetails();
    const scope = document.getElementById('scope-name'); if (scope && byId(selected)) scope.textContent = byId(selected).piece; // chat scope = the selected piece
    C.select(curId()); // the shell writes ?row=
    window.shell.url({ view, ...keep() });
    if (open) openPane('tab-details');
  }
  function unselect() { selected = null; renderDetails(); if (view === 'editor') renderEditor(); }
  document.getElementById('main').addEventListener('click', e => { const c = e.target.closest?.('.card[data-id]'); if (c && !suppress) select(c.dataset.id, true); });
  // Selection follows focus on a card, so Enter, which the shell sends as shell:open, opens the focused one.
  document.getElementById('main').addEventListener('focusin', e => { const c = e.target.closest?.('.card[data-id]'); if (c && e.target === c && c.dataset.id !== curId()) select(c.dataset.id, false); });
  document.addEventListener('shell:open', e => { if (byId(e.detail)) select(e.detail, true); });
  document.addEventListener('shell:picked', e => document.querySelectorAll('.card[data-id]').forEach(c => c.toggleAttribute('data-picked', e.detail.includes(c.dataset.id))));
  document.getElementById('view-board').addEventListener('click', e => {
    const c = e.target.closest?.('[data-f="area"]'); if (!c) return;
    ideaArea = ideaArea === c.dataset.v ? '' : c.dataset.v;
    renderBoard();
  });
  document.getElementById('parked-seg').addEventListener('click', e => { const b = e.target.closest('button[data-aside]'); if (b && b.dataset.v !== parked) { parked = b.dataset.v; window.shell.url({ view, ...keep() }); render(); } });

  // ---------- Drag (mouse and pen; touch uses the Details buttons) ----------
  let drag = null, suppress = false;
  const verdict = (p, z) => { const to = z.split(':')[1], V = legal(p, to); return { ok: V.ok, same: V.same, text: V.ok ? (V.correct ? `correct to ${SL[to]} (reason asked)` : `→ ${SL[to]}`) : V.reason }; };
  document.addEventListener('pointerdown', e => {
    const c = e.target.closest?.('.card[data-id]:not(.idea)');
    if (!c || e.button !== 0 || e.pointerType === 'touch' || e.target.closest('button')) return;
    drag = { id: +c.dataset.id, el: c, x: e.clientX, y: e.clientY, on: false, over: null };
  });
  document.addEventListener('pointermove', e => {
    if (!drag) return;
    if (!drag.on) {
      if (Math.hypot(e.clientX - drag.x, e.clientY - drag.y) < 5) return;
      drag.on = true;
      const p = byId(drag.id), r = drag.el.getBoundingClientRect();
      drag.dx = e.clientX - r.left; drag.dy = e.clientY - r.top;
      drag.ghost = drag.el.cloneNode(true); drag.ghost.classList.add('ghost-card'); drag.ghost.removeAttribute('tabindex'); drag.ghost.setAttribute('aria-hidden', 'true');
      drag.ghost.style.width = r.width + 'px'; put(drag.ghost, html`<div class="verdict"></div>`, 'append');
      document.body.append(drag.ghost); drag.el.classList.add('lifted');
      document.getElementById('main').setAttribute('data-dragging', '');
      document.querySelectorAll('[data-drop]').forEach(z => {
        const V = verdict(p, z.dataset.drop);
        z.dataset.legal = V.ok || V.same ? 'yes' : 'no';
        const rf = z.querySelector('.refusal'); if (rf) put(rf, html`${V.ok || V.same ? html`` : html`<b>■ Refused.</b> ${V.text}`}`);
      });
    }
    drag.ghost.style.left = e.clientX - drag.dx + 'px'; drag.ghost.style.top = e.clientY - drag.dy + 'px';
    const z = document.elementFromPoint(e.clientX, e.clientY)?.closest('[data-drop]');
    if (z !== drag.over) {
      drag.over?.removeAttribute('data-over'); drag.over = z; z?.setAttribute('data-over', 'yes');
      const V = z ? verdict(byId(drag.id), z.dataset.drop) : null, vd = drag.ghost.querySelector('.verdict');
      vd.textContent = V ? (V.same ? 'no change' : V.ok ? V.text : '■ refused: ' + V.text) : '';
      vd.classList.toggle('no', !!V && !V.ok && !V.same);
    }
  });
  function endDrag(commit) {
    if (!drag) return; const d = drag; drag = null; if (!d.on) return;
    suppress = true; setTimeout(() => { suppress = false; }, 0);
    d.ghost.remove(); d.el.classList.remove('lifted'); document.getElementById('main').removeAttribute('data-dragging');
    document.querySelectorAll('[data-drop]').forEach(z => { z.removeAttribute('data-over'); z.removeAttribute('data-legal'); });
    if (commit && d.over) move(d.id, d.over.dataset.drop.split(':')[1]);
  }
  document.addEventListener('pointerup', () => endDrag(true));
  document.addEventListener('pointercancel', () => endDrag(false));

  // ---------- Views and keys ----------
  function subhead() {
    const sub = document.getElementById('subhead');
    if (!D) { sub.textContent = 'The pieces were not read.'; return; }
    // Each realm's tab carries its open pieces (sd:5524, sd:8018): not a published or declined one.
    (window.PAGE_VIEWS || []).forEach(v => window.shell.view?.count(v.id, (D.pieces || []).filter(p => !['published', 'declined'].includes(p.stage) && inRealm(p, v.id)).length));
    sub.textContent = `Read ${String(D.read).slice(11, 16)} UTC · ` + (view === 'board' ? 'Pieces by stage.' : 'Focused editor: the draft on the left, its evidence on the right.');
  }
  // Board and Editor are the layout menu Tasks uses (shell.viewPick, sd:5641): one icon button; its menu lists the two.
  const pickView = window.shell.viewPick(document.getElementById('view-btn'), document.getElementById('view-menu'),
    { board: ['kanban', 'Board'], editor: ['pen-line', 'Editor'] }, setView);
  function setView(v) {
    view = v;
    pickView(v);
    ['board', 'editor'].forEach(x => { const el = document.getElementById('view-' + x); el.hidden = x !== v; if (x !== v) el.replaceChildren(); });
    document.getElementById('strip').hidden = v === 'editor';
    subhead();
    window.shell.url({ view: v, ...keep() }); // the shell keeps ?row=
    render();
  }
  window.writingView = setView;
  let vChord = 0;
  document.addEventListener('keydown', e => {
    if (e.target.matches?.('input, textarea, select, [contenteditable="true"]') || e.metaKey || e.ctrlKey || e.altKey || document.querySelector('dialog[open]') || window.shell.chording()) return;
    if (vChord && Date.now() - vChord < 1500) { const m = { b: 'board', e: 'editor' }[e.key]; vChord = 0; if (m) { e.preventDefault(); setView(m); return; } }
    if (e.key === 'v') { vChord = Date.now(); return; }
  });
  // The shell walks the board's cards on j / k (a card takes focus) and sends Esc here to cancel a drag.
  window.PAGE_LIST = { rows: () => document.querySelectorAll('#board .card[data-id]'), current: () => curId(), when: () => view === 'board',
    select: (id, n) => { select(id); n.focus(); }, clear: () => !!drag && (endDrag(false), true) };

  // ---------- Shapeshift: capture an idea, or find a piece ----------
  // Capture (sd:5887): Enter posts /api/actions/run, and the server runs `sd store add` with the idea as one argument, never a shell.
  // A refused or failed capture puts the idea back in the box, so nothing typed is lost.
  const inp = document.getElementById('shift-in'), prev = document.getElementById('shift-preview'), ghost = document.getElementById('shift-ghost');
  const captureCli = t => `sd store add sdw.blog-idea ${shq(t)} --field status=inbox`;
  function parse(t) {
    const s = t.trim(); let m;
    if ((m = s.match(/^#(\d+)$/))) return { find: byId(m[1]) ? [byId(m[1])] : [], id: m[1] };
    const q = s.replace(/^idea:\s*/i, '');
    const hits = q.length > 2 ? pieces.filter(p => (p.title + ' ' + p.piece).toLowerCase().includes(q.toLowerCase())).slice(0, 3) : [];
    return { idea: q, find: hits };
  }
  function showPrev() {
    const t = inp.value; ghost.hidden = !!t;
    if (!t.trim()) { prev.hidden = true; inp.setAttribute('aria-expanded', 'false'); return; }
    const P = parse(t);
    put(prev, html`${(P.idea ? html`<div class="as"><span class="label">Enter captures</span>${ICON('lightbulb')}<span>${P.idea}</span></div><div class="cli"><code>${captureCli(P.idea)}</code><button class="icon-btn" type="button" aria-label="Copy: ${captureCli(P.idea)}" data-copy="${captureCli(P.idea)}">${ICON('copy')}</button></div>` : '') }${
      (P.find.length ? html`<p class="hint">${P.idea ? 'Or open' : 'Enter opens'}</p><div class="suggest">${P.find.map(p => html`<button type="button" data-open-find="${p.id}">${ICON('search')}<span>#${p.id} · ${p.title}</span></button>`)}</div>` : P.id ? html`<p class="hint">No piece #${P.id}.</p>` : '') }${
      html`<p class="hint">Capture adds a blog idea to the vault's inbox · Esc clears</p>`}`);
    prev.hidden = false; inp.setAttribute('aria-expanded', 'true');
  }
  inp.addEventListener('input', showPrev); inp.addEventListener('focus', showPrev);
  inp.addEventListener('keydown', e => {
    if (e.key === 'Escape') { if (inp.value) { e.stopPropagation(); inp.value = ''; showPrev(); } return; }
    if (e.key !== 'Enter') return; e.preventDefault();
    const P = parse(inp.value);
    if (!P.idea && P.find[0]) { inp.value = ''; showPrev(); select(P.find[0].id, true); return; }
    if (!P.idea) return;
    const idea = P.idea, back = () => { if (!inp.value) { inp.value = idea; showPrev(); } };
    inp.value = ''; showPrev();
    window.shell.post('/api/actions/run', { action: 'idea.capture', text: idea }).then(r => {
      if (r.exit === 0) return toast(`Idea captured in the vault inbox: ${idea}`);
      back(); toast(`Idea not captured: ${captureCli(idea)} exited ${r.exit}: ${r.output}`);
    }, e => { back(); toast(`Idea not captured: ${e.message}`); });
  });
  prev.addEventListener('click', e => { const b = e.target.closest?.('[data-open-find]'); if (b) { inp.value = ''; showPrev(); select(b.dataset.openFind, true); } });
  document.addEventListener('click', e => { if (!e.target.closest?.('#shift')) { prev.hidden = true; inp.setAttribute('aria-expanded', 'false'); } });

  // ---------- Start (build: read /api/writing through shell.read, then draw) ----------
  // The reader (read.js) holds the guards: of overlapping reads only the newest draws, a piece the read no longer lists runs no
  // command, and a failed read clears the board, the rail and Details.
  const q = new URLSearchParams(location.search);
  view = q.get('view') === 'editor' ? 'editor' : 'board';
  const firstShown = () => (pieces.find(p => window.shell.aside.shows(parked, p.parked)) || pieces[0])?.id;
  reading = window.shell.read({
    source: '/api/writing', what: 'the pieces',
    adopt: doc => {
      // Personal stays, as on Tasks (sd:5642): it is where personal writing goes, so its tab shows with nothing in it.
      (window.PAGE_VIEWS || []).forEach(v => { if (v.id !== 'personal') window.shell.view?.hide(v.id, !(doc.pieces || []).some(p => inRealm(p, v.id))); });
      D = doc; pieces = (doc.pieces || []).filter(p => inRealm(p)); reports.clear();
      document.body.dataset.observed = D.read;
      const known = ['read', 'partial'].includes(doc.realms?.state);
      // Each repo once, with the realm that claims it: "sd-writing under Work".
      const label = id => (window.PAGE_VIEWS || []).find(v => v.id === id)?.label || id;
      const claimed = ps => [...new Map(ps.filter(p => p.realm).map(p => [p.repo, `${p.repo} under ${label(p.realm)}`])).values()].join(', ');
      return { objects: pieces.map(p => ({ id: String(p.id), type: 'piece', label: `#${p.id} ${p.title}` })),
        state: pieces.length ? null : REALM === 'personal' && (doc.pieces || []).length
          ? { kind: 'empty', title: 'No personal pieces', source: '/api/writing', text: known
            ? `Pieces from a repo a realm claims show in that realm: ${claimed(doc.pieces)}. dashboard.realms on Settings decides which repos each realm claims.`
            : 'The dashboard.realms list was not read, so a piece\'s realm is not known. Set it on Settings.' }
          : { kind: 'empty', title: 'No pieces', text: 'No writing piece is registered. Import the writing collection to see its pieces here.', source: '/api/writing' } };
    },
    clear: () => { D = null; pieces = []; delete document.body.dataset.observed; },
    draw: () => { setView(view); },
    current: () => curId(),
    first: () => (firstShown() == null ? null : String(firstShown())),
    select: (id, opened) => select(id, opened),
    unselect,
  });
  reading.load();
  // Read again (sd:5638): the shell's head button reads the pieces again.
  window.PAGE_AGAIN = { title: 'Read the pieces again', run: () => reading.load() };
  suggest(['Which fact-check findings block this piece?', 'Summarise what changed since research was stamped', 'Draft the reason to correct this piece to Review']);
});
// Top level, not inside DOMContentLoaded: shell.js builds the ? sheet from PAGE_KEYS when it loads.
window.PAGE_KEYS = [['v b / v e', 'Board or editor view']];
