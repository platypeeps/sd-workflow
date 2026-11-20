// Scoring (sd:3709, the Review page until sd:5781): the labelling queue, Decide's Scoring view. It loads before shell.js,
// which reads PAGE_LIST and PAGE_KEYS from it. One-off questions are Decide's Decisions view (decide.js, decisions.js).
// Rows: /api/review (review_screen.document): one lamp per open batch, one ledger row per question, nightly suggestions
// first. A row carries its question (sd:5123): the first line and its subject and evidence, the options as numbered buttons,
// and the whole question folded in place. No Details pane: the shell's pane opens only for Chat. One click on an option
// labels that question; its row hides at once and the next open question takes the selection. Show reviewed, in the tally,
// brings back the ruled and expired rows of the open batches, each with its choice and a Withdraw for a standing label.
// Keys (products/system/design.md, Keys): 1-9 label the selected question and open the next open one; j / k move without
// a label (a skip); Enter opens or folds the selected question; u is the shell's Undo while the label's toast shows (10 s);
// n is the shell's capture, which files a comment "On note #N: ..." on the batch; Esc clears the batch filter and Show
// reviewed; ? lists the keys. No other digit is bound.
// Bulk (sd:5497): the rows band by batch, one fold each (the shell's table grammar). A checkbox on each open row, one on each
// band and Select all / Clear in the bar pick rows; the bar labels every picked row with an option they all offer, or each
// with its own recommendation. Each is the row's write, one per question in ledger order; one toast names the rows that did
// not land, each row says why, and those rows stay picked. Undo withdraws every label that landed.
// Context (sd:5759): each band carries its batch's purpose, rubric and what the labels feed. A question's recommendation
// shows with its reason; a blind or sealed batch's arrives only with the label's reply or a ruled row, and a ruled row says
// whether the label matched it.
// Writes run one at a time, each with the batch revision the last write returned, so ten quick digits are ten labels.
// A refused write puts its question back; a stale one (409) rereads and keeps the place; a write with no answer is unknown
// and rereads. Markup is html`…` from markup.js: every value put in it is escaped, and put() is the only way in.
(() => {
  const { html, put, plural, cells } = window.markup;
  const $ = id => document.getElementById(id);
  const G = { caution: '▲', queued: '◌', unknown: '▨', ok: '●' };
  const rowId = note => `q${note}`;
  const noteOf = id => +String(id || '').replace(/^q/, '');

  // DOC is the last read (null when it failed); B the batches by item, Q every question in ledger order, open or reviewed.
  // PENDING holds a label sent and not yet answered (note -> option); its row reads as reviewed until the answer.
  // REVIEWED is Show reviewed (?reviewed=1); OPENED the questions unfolded in place.
  let DOC = null, FAILED = '', READ = null, sel = null, batch = null, labelled = 0, REVIEWED = false;
  let B = new Map(), Q = [];
  const PENDING = new Map(), SKIPPED = new Set(), OPENED = new Set();
  // PICKED holds the checked open questions; REFUSED a bulk label's refusal by note, shown on its row until the next try.
  const PICKED = new Set(), REFUSED = new Map();
  let chain = Promise.resolve();
  const queue = f => (chain = chain.catch(() => {}).then(f));

  const isOpen = q => q.open !== false && !PENDING.has(q.note);
  const inBatch = q => batch === null || q.item === batch;
  const shown = () => Q.filter(q => inBatch(q) && (REVIEWED || isOpen(q)));
  const find = note => Q.find(q => q.note === note);
  const firstLine = t => String(t || '').split('\n')[0];
  const clock = d => d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', hour12: false, timeZoneName: 'short' });
  // The digits and j / k act on Scoring alone: on Decisions they would label a question the reader cannot see.
  const onScoring = () => (window.shell?.view?.current?.() ?? 'scoring') === 'scoring';


  // ---------- Draw ----------
  function lamps() {
    const ul = $('annunciator'), list = DOC ? DOC.batches : [];
    ul.hidden = !list.length;
    put(ul, cells(list.map(b => b.error
      // sd:5298: a batch that did not read links to its evidence, the sd item that files it.
      ? { label: b.title, href: window.shell.itemHref(b.item), mark: html`<span class="g g-unknown" aria-hidden="true">${G.unknown}</span>`, state: 'unknown',
          val: 'not read', small: b.error, attrs: html` data-batch="${b.item}" title="${`sd:${b.item}`}"` }
      : { label: b.title, button: true, pressed: batch === b.item, state: b.open ? 'caution' : 'ok',
          mark: html`<span class="g" aria-hidden="true">${b.open ? G.caution : G.ok}</span>`,
          val: html`<b>${b.open}</b> of ${b.questions} open`, small: `${b.nightly ? 'nightly · ' : ''}${b.mode} · sd:${b.item}`,
          sr: `${b.title}: ${b.open} of ${b.questions} open`, attrs: html` data-batch="${b.item}"` })));
  }

  function progress() {
    const open = [...B.values()].reduce((n, b) => n + (b.open || 0), 0) - PENDING.size;
    $('sub').textContent = DOC
      ? `${labelled} labelled · ${Math.max(0, open)} open · ${SKIPPED.size} skipped · read ${clock(READ)}`
      : 'The review queue was not read';
    const list = shown().filter(isOpen), hidden = Q.filter(q => inBatch(q) && !isOpen(q)).length;
    // Show reviewed sits in the tally beside the batch filter: the page's one filter line, kept in the address.
    const toggle = html` · <button class="btn quiet sm" type="button" id="show-reviewed" aria-pressed="${String(REVIEWED)}">Show reviewed <span class="n">${hidden}</span></button>`;
    put($('tally'), html`${batch === null ? html`${plural(list.length, 'question')} open` : header(list.length)}${toggle}`);
    // The Scoring tab's count, and its part of Decide's badge (decide.js), kept current as its labels land.
    window.decideCount?.('scoring', DOC ? Math.max(0, open) : null);
    bar();
  }

  // The batch header, open while a lamp filters the ledger: the batch's own actions, none waiting on a selected question.
  // Download is on whenever the batch exports: an open batch at once, a blind or sealed one once nothing is open.
  function header(count) {
    const b = B.get(batch) || {}, blind = b.mode === 'blind' || b.mode === 'sealed';
    const exportable = !b.error && b.mode && (!blind || b.open === 0);
    return html`${plural(count, 'question')} open in <a href="${window.shell.itemHref(batch)}">sd:${batch}</a> · <button class="linkbtn" type="button" id="clear-batch">Show every batch</button>
      · <button class="btn quiet sm" type="button" id="export"${exportable ? '' : html` disabled`}>Download labels</button>${exportable ? ''
      : html` <span class="why">${b.error ? 'Not read, so it does not download.' : `A ${b.mode} batch downloads once every question is ruled.`}</span>`}`;
  }

  // A Kev file's proposal is an evidence line (sd:4789); its digit is the accept. A blind batch shows none.
  const proposal = (q, blind) => blind ? null : ((q.evidence || []).find(e => e.startsWith('Proposed label: ')) || '').slice(16) || null;
  const blindOf = q => ['blind', 'sealed'].includes((B.get(q.item) || {}).mode);
  // The recommendation (sd:5759): the one the server sends, which a blind batch holds back until the question is ruled,
  // else the proposal, with no reason.
  const adviceOf = (q, blind) => q.recommendation || (proposal(q, blind) ? { option: proposal(q, blind), reason: null } : null);
  // The recommendation a bulk accept takes, when the question offers it.
  const recOf = q => { const a = adviceOf(q, blindOf(q)); return a && q.options.includes(a.option) ? a.option : null; };

  // One row per question: the first line and one line of context (subject, evidence) under it, then on one line the options
  // or the choice made and the whole question, folded in place (Enter, or its summary, opens it).
  function row(q) {
    const b = B.get(q.item) || {}, blind = b.mode === 'blind' || b.mode === 'sealed', open = isOpen(q);
    const advice = adviceOf(q, blind), proposed = advice && advice.option;
    const context = (q.evidence || []).filter(e => !e.startsWith('Proposed label: '));
    const label = PENDING.get(q.note) || q.label;
    const state = open ? html`<td class="g g-caution" title="open">${G.caution}<span class="sr"> open</span></td>`
      : label ? html`<td class="g g-ok" title="labelled">${G.ok}<span class="sr"> labelled</span></td>`
      : html`<td class="g g-queued" title="expired">${G.queued}<span class="sr"> expired</span></td>`;
    const choose = open
      ? html`<div class="opts" role="group" aria-label="${`Label note ${q.note}`}">${q.options.map((o, i) => html`<button type="button" class="btn quiet sm opt" data-note="${q.note}" data-i="${i}" aria-keyshortcuts="${i + 1}"><kbd>${i + 1}</kbd><span>${o}</span>${o === proposed ? html`<span class="tag">recommended</span>` : ''}</button>`)}</div>`
      : html`<p class="chose">${label ? html`${PENDING.has(q.note) ? 'Labelling' : 'Labelled'} <b>${label}</b>` : 'Expired with no label'}${!PENDING.has(q.note) && q.ruling
        ? html` · <button class="linkbtn" type="button" data-withdraw="${q.note}">Withdraw label</button>` : ''}</p>`;
    // sd:5497: the recommendation in words on every open row of a batch that shows its evidence. sd:5759: its reason, and on
    // a labelled row whether the label matched it.
    const matched = !open && label && advice ? html` · <span class="agree">${label === advice.option ? 'matches' : 'differs from'} your label</span>` : '';
    const rec = advice && (open || label) ? html`<p class="rec">Recommended: <b>${advice.option}</b>${advice.reason ? html` · ${advice.reason}` : ''}${matched}</p>`
      : open && !blind ? html`<p class="rec none">No recommendation</p>` : '';
    const pick = open ? html`<td class="pk"><label class="pickl"><input type="checkbox" data-pick="${q.note}" aria-label="${`Select note ${q.note}`}"${PICKED.has(q.note) ? html` checked` : ''}></label></td>` : html`<td class="pk"></td>`;
    return html`<tr data-id="${rowId(q.note)}"${open ? '' : html` data-reviewed=""`} aria-selected="${String(sel === q.note)}">
      ${pick}${state}
      <td class="what"><p class="ask">${firstLine(q.text)}</p>${q.subject || context.length ? html`<p class="ctx">${q.subject ? html`<span class="subject">${q.subject}</span>` : ''}${context.map((e, i) => html`${i || q.subject ? ' · ' : ''}${e}`)}</p>` : ''}${rec}${REFUSED.has(q.note) && open
        ? html`<p class="refused" role="alert">Not labelled: ${REFUSED.get(q.note)}</p>` : ''}
        <div class="pick">${choose}
        <details class="more"${OPENED.has(q.note) ? html` open` : ''}><summary>Whole question</summary><p class="full">${q.text}</p>${blind
          ? html`<p class="why">A ${b.mode} batch: its subject and evidence stay hidden until it is exported.</p>` : ''}<dl>${q.subject ? html`<dt>Subject</dt><dd>${q.subject}</dd>` : ''}${(q.evidence || []).map(e => html`<dt>Evidence</dt><dd>${e}</dd>`)}
          <dt>Note</dt><dd>#${q.note} · <a href="${window.shell.itemHref(q.item)}">sd:${q.item}</a></dd><dt>Queue</dt><dd>${b.queue || '—'} · ${b.mode || '—'}</dd><dt>Source</dt><dd>${b.source || '—'}</dd></dl></details></div></td>
      <td class="at"><time class="rel" datetime="${q.asked}"></time></td></tr>`;
  }

  // sd:5497: one band per batch, the shell's fold (data-band keys it, so a moving count keeps it shut). Its checkbox picks
  // every open question of the batch and stays out of the fold button (data-tkeep); a band partly picked reads as mixed.
  function band(b, list) {
    const open = list.filter(isOpen), on = open.filter(q => PICKED.has(q.note)).length, name = b.title || `sd:${b.item}`;
    const state = open.length && on === open.length ? html` checked` : on ? html` data-mixed=""` : '';
    return html`<tr class="band" data-band="${`b${b.item}`}"><td colspan="4">${name}${b.nightly ? html`<span class="tag">nightly</span>` : ''}<span class="n">${open.length} open · sd:${b.item}</span>${open.length
      ? html`<label class="bandpick" data-tkeep=""><input type="checkbox" data-band-pick="${b.item}" aria-label="${`Select every open question in ${name}`}"${state}><span>Select batch</span></label>` : ''}${brief(b.context, b.item)}</td></tr>`;
  }

  // sd:5759: the batch's context under its band, above its questions. It stays out of the fold button (data-tkeep), as the
  // batch checkbox does, and a shut fold hides it (review.css). A batch filed with none, before pack sd:9479 required
  // one, says so and names itself, so its questions never stand bare.
  function brief(c, item) {
    const rubric = (c && c.rubric) || [];
    if (!c || !(c.purpose || rubric.length || c.feeds)) return html`<p class="brief nobrief" data-tkeep="">No description: ask the filer (sd:${item})</p>`;
    return html`<dl class="brief" data-tkeep="">${c.purpose ? html`<dt>Purpose</dt><dd>${c.purpose}</dd>` : ''}${rubric.length
      ? html`<dt>Rubric</dt><dd><ul>${rubric.map(r => html`<li>${r}</li>`)}</ul></dd>` : ''}${c.feeds ? html`<dt>Feeds</dt><dd>${c.feeds}</dd>` : ''}</dl>`;
  }

  // A redraw replaces the buttons: focus on one comes back to the same button, else to the selected row's first option.
  function refocus(tb, was) {
    if (!was) return;
    const d = was.dataset || {}, same = d.i !== undefined ? `button[data-note="${d.note}"][data-i="${d.i}"]`
      : d.withdraw ? `button[data-withdraw="${d.withdraw}"]` : d.pick ? `input[data-pick="${d.pick}"]`
      : d.bandPick ? `input[data-band-pick="${d.bandPick}"]` : null;
    (same && tb.querySelector(same) || tb.querySelector(`tr[data-id="${rowId(sel)}"] .opt`))?.focus({ preventScroll: true });
  }

  function rows() {
    const tb = $('rows'), list = shown(), was = tb.contains?.(document.activeElement) ? document.activeElement : null;
    if (!DOC) return put(tb, html`<tr class="empty"><td colspan="4"><span class="g-unknown" aria-hidden="true">${G.unknown}</span> Not read: ${FAILED}. Read again retries it.</td></tr>`);
    const hidden = REVIEWED ? 0 : Q.filter(q => inBatch(q) && !isOpen(q)).length;
    const by = new Map();
    list.forEach(q => { if (!by.has(q.item)) by.set(q.item, []); by.get(q.item).push(q); });
    put(tb, html`${[...by].map(([item, qs]) => html`${band(B.get(item) || { item }, qs)}${qs.map(row)}`)}${list.some(isOpen) ? '' : html`<tr class="empty"><td colspan="4">${batch === null ? 'No question is open. A new review batch appears here.' : 'No question is open in this batch.'}${hidden
      ? ` ${plural(hidden, 'reviewed question')} hidden: Show reviewed lists them.` : ''}</td></tr>`}`);
    tb.querySelectorAll('input[data-mixed]').forEach(box => { box.indeterminate = true; });
    refocus(tb, was);
  }

  // The bar over the table: how many are picked, Select all and Clear, and the labels for the picked rows: each option every
  // picked question offers, and Accept recommendation for those that carry one.
  const picked = () => Q.filter(q => PICKED.has(q.note) && isOpen(q));
  function bar() {
    const el = $('bulk'); if (!el) return;
    const list = picked(), open = shown().filter(isOpen).length;
    const common = list.length ? list[0].options.filter(o => list.every(q => q.options.includes(o))) : [];
    const recs = list.filter(recOf).length, off = !DOC;
    put(el, html`<span class="count"><b>${list.length}</b> selected</span>
      <button class="btn quiet sm" type="button" id="select-all"${off || !open ? html` disabled` : ''}>Select all <span class="n">${open}</span></button>
      <button class="btn quiet sm" type="button" id="clear-sel"${list.length ? '' : html` disabled`}>Clear</button>${list.length ? html`
      <span class="as">Label selected as</span>${common.length ? common.map(o => html`<button class="btn sm" type="button" data-bulk-option="${o}">${o}</button>`)
        : html`<span class="why">No option common to the selection</span>`}
      <button class="btn quiet sm" type="button" id="accept-rec"${recs ? '' : html` disabled`}>Accept recommendation <span class="n">${recs}</span></button>` : ''}`);
  }

  // The weekly advisory sweep (sd:9505): one row per week from `sweeps`, newest first. A count the log does not hold is
  // a dash, never a zero; Fixed is the week's filed items done, of all it filed. The tally says why a log did not read.
  function sweeps() {
    const s = DOC && DOC.sweeps, weeks = s ? s.weeks : [], n = v => (v === null || v === undefined ? '—' : v);
    const why = !s ? 'not read' : s.state === 'read' ? '' : `log: ${s.reason}`;
    $('sweep-tally').textContent = `${plural(weeks.length, 'week')}${why ? ` · ${why}` : ''}`;
    const fixed = w => (w.fixed + w.unfixed ? `${w.fixed} of ${w.fixed + w.unfixed}` : '—');
    put($('sweeps'), weeks.length
      ? html`${weeks.map(w => html`<tr><td>${w.week}</td><td>${n(w.packets)}</td><td>${n(w.open)}</td><td>${n(w.filed)}</td><td>${n(w.skipped)}</td><td>${fixed(w)}</td></tr>`)}`
      : html`<tr class="empty"><td colspan="6">${s ? 'No sweep has run yet.' : 'The sweep was not read.'}</td></tr>`);
  }

  function draw() { lamps(); rows(); progress(); sweeps(); }

  // ---------- Selection ----------
  function select(note) {
    if (sel !== null && sel !== note && find(sel) && isOpen(find(sel))) SKIPPED.add(sel);
    sel = note;
    const q = note === null ? null : find(note);
    $('rows').querySelectorAll('tr[data-id]').forEach(tr => tr.setAttribute('aria-selected', String(tr.dataset.id === rowId(note))));
    // The shell's selection only while Scoring shows: a hidden question takes no Enter and no note (sd:5781).
    if (q && onScoring()) {
      window.shell.commands.put({ id: rowId(note), type: 'question', label: `note #${note}`, item: q.item });
      window.shell.commands.select(rowId(note));
    } else window.shell.commands.select(null);
    // Focus in the ledger follows the selection (j / k, Undo), so a digit and the focused button name the same question.
    const tb = $('rows'), tr = q && tb.querySelector(`tr[data-id="${rowId(note)}"]`);
    if (tr && tb.contains?.(document.activeElement) && !tr.contains(document.activeElement)) tr.querySelector('.opt, [data-withdraw], summary')?.focus({ preventScroll: true });
    progress();
  }
  // The open question after `note` in the ledger, else the one before it, else none.
  function neighbour(note) {
    const list = shown().filter(q => q.note === note || isOpen(q)), i = list.findIndex(q => q.note === note);
    const next = list[i + 1] || list[i - 1];
    return next ? next.note : null;
  }
  // The selection keeps the next row in view; a label sent from a row's button hands focus to the next row's first option.
  function reveal(note, focus) {
    if (note === null) return;
    const tb = $('rows'), tr = tb.querySelector(`tr[data-id="${rowId(note)}"]`);
    if (focus) tb.querySelector(`tr[data-id="${rowId(note)}"] .opt`)?.focus({ preventScroll: true });
    tr?.scrollIntoView({ block: 'nearest', inline: 'nearest' });
  }
  function writeUrl() {
    const p = new URLSearchParams();
    if (batch !== null) p.set('batch', batch);
    if (REVIEWED) p.set('reviewed', 1);
    window.shell.url(p);
  }
  function first() {
    const list = shown(), open = list.filter(isOpen);
    select(list.some(q => q.note === sel) ? sel : (open[0] || list[0] || { note: null }).note);
  }
  function filter(item, reviewed = REVIEWED) {
    batch = item; REVIEWED = reviewed;
    writeUrl(); draw(); first();
  }

  function pickAll(list, on) { list.filter(isOpen).forEach(q => on ? PICKED.add(q.note) : PICKED.delete(q.note)); rows(); progress(); }

  // ---------- Read ----------
  async function load() {
    // The shell's stale-response guard (shell.shown): only the newest read applies its answer.
    const still = window.shell.shown('review', 'queue', () => 'queue');
    if (!DOC) window.shell.state({ kind: 'loading', text: 'Reading the review queue. Questions appear when it answers.', source: '/api/review' });
    let doc;
    try { doc = await window.shell.getJSON('/api/review'); } catch (e) {
      if (!still()) return;
      // A failed read is unknown, never an empty queue: nothing from the last read stays on screen as current.
      DOC = null; FAILED = e.message; B = new Map(); Q = [];
      window.shell.state({ kind: 'error', text: `The review queue was not read: ${e.message}. Read again retries it.`, source: '/api/review' });
      draw(); return;
    }
    if (!still()) return;
    DOC = doc; READ = new Date(); FAILED = '';
    B = new Map(doc.batches.map(b => [b.item, b]));
    // The open questions and, for Show reviewed, the ruled and expired ones (`reviewed`), in one ledger order.
    const at = new Map(doc.batches.map((b, i) => [b.item, i]));
    Q = doc.questions.concat(doc.reviewed || []).sort((x, y) => at.get(x.item) - at.get(y.item) || x.note - y.note);
    if (batch !== null && !B.has(batch)) batch = null;
    // A pick or a refusal outlives a read only while its question is still open.
    [...PICKED].forEach(n => { const q = find(n); if (!q || !isOpen(q)) PICKED.delete(n); });
    [...REFUSED.keys()].forEach(n => { const q = find(n); if (!q || !isOpen(q)) REFUSED.delete(n); });
    window.shell.state(doc.batches.length ? null : { kind: 'empty', text: 'No review batch is open. A new one appears here.', source: '/api/review' });
    draw(); first();
  }

  // ---------- Write ----------
  // The one label write, a row's or the bar's: under the batch revision the last write returned, which it then carries on.
  async function rule(q, option) {
    const b = B.get(q.item);
    const out = await window.shell.post('/api/review/rule', { item: q.item, note: q.note, option, revision: b ? b.revision : '' });
    PENDING.delete(q.note); REFUSED.delete(q.note); PICKED.delete(q.note);
    if (b) { b.revision = out.revision; b.open -= 1; b.labels += 1; }
    // The reply names the recommendation now that the label is in (sd:5759), so a blind row shows it at once.
    Object.assign(q, { open: false, label: option, ruling: out.ruling }, out.recommendation ? { recommendation: out.recommendation } : {}); labelled += 1;
    return out;
  }

  function label(note, i, focus) {
    const q = find(note);
    if (!q || !isOpen(q) || !(i in q.options)) return;
    const option = q.options[i], next = neighbour(note);
    PENDING.set(note, option);
    SKIPPED.delete(note);
    sel = null; draw(); select(next); reveal(next, focus);
    return queue(async () => {
      try {
        const out = await rule(q, option);
        draw();
        const said = out.recommendation && blindOf(q) ? ` · recommended ${out.recommendation.option}` : '';
        window.shell.toast(`Labelled note #${note}: ${option}${said}`, () => undo(q, out.ruling));
      } catch (e) {
        PENDING.delete(note);
        if (e.stale) { window.shell.toast(`${G.caution} The batch changed; reread. Note #${note} was not labelled.`); return load(); }
        if (!e.status) { window.shell.toast(`Not known whether note #${note} was labelled: ${e.message}. Reading the queue again.`); return load(); }
        draw();
        window.shell.toast(`Not labelled · note #${note}: ${e.message}`);
      }
    });
  }

  // Undo and a reviewed row's Withdraw label: the withdrawal reopens the question, which takes the selection.
  function undo(q, ruling) {
    return queue(async () => {
      const b = B.get(q.item);
      try {
        await window.shell.post('/api/review/withdraw', { item: q.item, ruling, revision: b ? b.revision : '' });
        if (labelled > 0) labelled -= 1;
        await load();
        // The label moved the selection on by itself, so going back is no skip.
        sel = null; select(q.note);
        window.shell.toast(`Label withdrawn · note #${q.note} is open again`);
      } catch (e) {
        window.shell.toast(`Not undone · note #${q.note}: ${e.message}`);
        if (e.stale || !e.status) await load();
      }
    });
  }

  // The bar's label: every picked open question in ledger order, each with chosen(q) or skipped when that is null. Each is
  // the row's write in turn. A batch that answers stale (409) or not at all gets no more writes this run, since its
  // revision is no longer known; the page then reads again. One toast names each row that did not land, and its Undo
  // withdraws every label that did.
  function bulk(chosen, how) {
    const list = picked(), plan = list.map(q => [q, chosen(q)]).filter(([, o]) => o != null), left = list.length - plan.length;
    if (!plan.length) return;
    plan.forEach(([q, o]) => { PENDING.set(q.note, o); SKIPPED.delete(q.note); REFUSED.delete(q.note); });
    if (sel !== null && PENDING.has(sel)) { const next = neighbour(sel); sel = null; draw(); select(next); } else draw();
    const lost = new Map(), landed = [], failed = [];
    let reread = false;
    return Promise.all(plan.map(([q, o]) => queue(async () => {
      if (lost.has(q.item)) { PENDING.delete(q.note); failed.push([q, lost.get(q.item)]); return; }
      try { landed.push([q, (await rule(q, o)).ruling]); } catch (e) {
        PENDING.delete(q.note);
        const why = e.stale ? 'the batch changed; read again' : !e.status ? `no answer, so not known: ${e.message}` : e.message;
        if (e.stale || !e.status) { lost.set(q.item, e.stale ? 'not sent: the batch changed' : 'not sent: an earlier label got no answer'); reread = true; }
        failed.push([q, why]);
      }
    }))).then(async () => {
      failed.forEach(([q, why]) => REFUSED.set(q.note, why));
      draw();
      const not = failed.length ? ` · not labelled: ${failed.map(([q, why]) => `note #${q.note} (${why})`).join(', ')}` : '';
      const rest = left ? ` · ${left} with no recommendation stay selected` : '';
      window.shell.toast(`Labelled ${landed.length} of ${plan.length} as ${how}${not}${rest}`, landed.length ? () => undoAll(landed) : null);
      if (reread) await load();
    });
  }

  // A bulk label's Undo: each landed label withdrawn in turn, then one read; one toast names what was not withdrawn.
  function undoAll(landed) {
    const not = [];
    return Promise.all(landed.map(([q, ruling]) => queue(async () => {
      const b = B.get(q.item);
      try {
        const out = await window.shell.post('/api/review/withdraw', { item: q.item, ruling, revision: b ? b.revision : '' });
        if (b && out.revision) b.revision = out.revision;
        if (labelled > 0) labelled -= 1;
      } catch (e) { not.push(`note #${q.note} (${e.message})`); }
    }))).then(async () => {
      await load();
      window.shell.toast(`Withdrew ${landed.length - not.length} of ${landed.length} labels${not.length ? ` · not withdrawn: ${not.join(', ')}` : ''}`);
    });
  }

  // n: the shell's capture. A note on a question is a comment on its batch, "On note #N: ...", under the batch revision.
  window.SHELL_CAPTURE = async ({ kind, title, item, priority, repo, about }) => {
    if (kind === 'note') {
      const note = noteOf(about && about.id), b = B.get(item);
      if (!note || !b) throw new Error('Not filed: select a question first.');
      const out = await queue(() => window.shell.post(`/api/items/${item}/notes`, { body: `On note #${note}: ${title}`, kind: 'comment', revision: b.revision }))
        .catch(e => { if (e.stale) load(); throw new Error(`Not filed: ${e.message}`); });
      b.revision = out.revision;
      return `Comment on note #${note} added to sd:${item}`;
    }
    // A task or a followup files as on every page, with the box's priority and repo (shell.js fileCapture, sd:3521).
    return window.shell.fileCapture({ kind, title, item, priority, repo });
  };

  // The batch's labels as the JSONL sd-db review export writes, saved by the browser; the key never leaves its file.
  async function download(item) {
    try {
      const out = await window.shell.getJSON(`/api/review/${item}/export`);
      const text = out.lines.map(line => JSON.stringify(line)).join('\n') + '\n';
      const a = document.createElement('a');
      a.href = URL.createObjectURL(new Blob([text], { type: 'application/x-ndjson' }));
      a.download = out.name; a.click();
      setTimeout(() => URL.revokeObjectURL(a.href), 1000);
      window.shell.toast(`Downloaded ${out.name} · ${plural(out.lines.length - 1, 'question')}`);
    } catch (e) { window.shell.toast(`Not downloaded: ${e.message}`); }
  }

  // ---------- Keys and the shell's list ----------
  window.PAGE_KEYS = [['1 – 9', 'Label the selected question with that option and open the next one'],
    ['j / k', 'Next / previous question, without a label (a skip)'], ['↵', 'Open or fold the whole question in its row'],
    ['u', 'Undo the last label while its toast shows'], ['n', 'Comment on the selected question'],
    ['Esc', 'Show every batch again and hide reviewed questions']];
  window.PAGE_LIST = { rows: () => $('rows').querySelectorAll('tr[data-id]'), select: id => select(noteOf(id)), when: onScoring,
    current: () => (sel === null ? null : rowId(sel)), clear: () => (batch !== null || REVIEWED) && (filter(null, false), true) };

  // Scoring shown again selects its question again; another view takes the selection off it.
  document.addEventListener('shell:view', e => { if (e.detail?.id === 'scoring') select(sel); else window.shell.commands.select(null); });

  document.addEventListener('keydown', e => {
    if (!/^[1-9]$/.test(e.key) || e.repeat || e.metaKey || e.ctrlKey || e.altKey) return;
    if (e.target?.matches?.('input, textarea, select, [contenteditable]') || document.querySelector('dialog[open]')) return;
    if (window.shell?.chording?.() || sel === null || !onScoring()) return;
    e.preventDefault();
    label(sel, +e.key - 1);
  });

  document.addEventListener('DOMContentLoaded', () => {
    const asked = new URLSearchParams(location.search).get('batch');
    if (/^[1-9][0-9]*$/.test(asked || '')) batch = +asked;
    REVIEWED = new URLSearchParams(location.search).get('reviewed') === '1';
    // One click on an option labels its row's question; Withdraw label pulls a reviewed row's standing label; a click
    // elsewhere in a row selects it, and its summary records the fold so a redraw keeps it.
    $('rows').addEventListener('click', e => {
      const o = e.target.closest('button[data-i]');
      if (o) return void label(+o.dataset.note, +o.dataset.i, true);
      const w = e.target.closest('button[data-withdraw]'), q = w && find(+w.dataset.withdraw);
      if (q) { if (q.ruling && !PENDING.has(q.note)) undo(q, q.ruling); return; }
      const s = e.target.closest('summary'), tr = e.target.closest('tr[data-id]');
      if (!tr) return;
      const note = noteOf(tr.dataset.id);
      if (s) OPENED.has(note) ? OPENED.delete(note) : OPENED.add(note);
      if (note !== sel) select(note);
    });
    // A row's checkbox picks its question; a band's picks every open question of its batch.
    $('rows').addEventListener('change', e => {
      const one = e.target.closest('input[data-pick]');
      if (one) { const n = +one.dataset.pick; one.checked ? PICKED.add(n) : PICKED.delete(n); rows(); return void progress(); }
      const all = e.target.closest('input[data-band-pick]');
      if (all) pickAll(shown().filter(q => q.item === +all.dataset.bandPick), all.checked);
    });
    $('bulk').addEventListener('click', e => {
      const b = e.target.closest('button[data-bulk-option]');
      if (b && !b.disabled) return void bulk(q => q.options.includes(b.dataset.bulkOption) ? b.dataset.bulkOption : null, b.dataset.bulkOption);
      const a = e.target.closest('#accept-rec');
      if (a && !a.disabled) return void bulk(recOf, 'recommended');
      if (e.target.closest('#select-all')) return void pickAll(shown(), true);
      if (e.target.closest('#clear-sel')) { PICKED.clear(); rows(); progress(); }
    });
    $('annunciator').addEventListener('click', e => { const c = e.target.closest('button[data-batch]'); if (c) filter(batch === +c.dataset.batch ? null : +c.dataset.batch); });
    $('tally').addEventListener('click', e => {
      if (e.target.closest('#show-reviewed')) return void filter(batch, !REVIEWED);
      if (e.target.closest('#clear-batch')) return void filter(null);
      const x = e.target.closest('#export'); if (x && !x.disabled && batch !== null) download(batch);
    });
    // Read again (sd:5638): the shell's head button reads the review queue again.
    window.PAGE_AGAIN = { title: 'Read the review queue again', run: () => load() };
    // Enter on the selected row: open or fold its whole question in place.
    document.addEventListener('shell:open', e => {
      const n = noteOf(e.detail); if (!find(n)) return;
      OPENED.has(n) ? OPENED.delete(n) : OPENED.add(n);
      rows(); select(n);
    });
    load();
  });
})();
