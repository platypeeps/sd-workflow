// Contributions (sd:2113): the design source's products/system/designs/v2/contributions.js at b2b8c2b, ported. Each change from the
// reference is marked "build:". The rows are /api/contributions/page (contribution_screen.document): the projection v1
// /classic/contributions renders, never sample data. The dashboard makes no GitHub call: the "Settled on GitHub" lamp, each
// row's settle date and the settled-per-day chart read the dates the shadow collector records (sd:5301). Acknowledge, Make task and
// Dismiss run through dashboard routes and ask first: none has a verb that reverses it. Open on GitHub opens a tab (sd:5628);
// Re-run collector asks the server to run sd shadow sync (sd:2207).
// build (sd:5486): Hide and Unhide post /api/contributions/view, the view state Designs' Hide writes, each with Undo. A hidden
// row leaves the Open rows, the lamps, the tallies and the badge; Hidden, in the Open table's Open | Hidden, lists it with Unhide.
// Hide persists until Unhide, where Dismiss ends on new activity. An unread store hides nothing and turns both commands off.
// build (sd:5488): the Filed view lists the Jira tickets and GitHub issues you filed this year, as sd shadow sync stored them
// (pack sd:5496), with Details and Edit, which opens one in Jira or on GitHub in a new tab; the dashboard writes nothing there.
// A row with no created date yet is listed and says so; a tracker that did not read or is stale is named with its reason.
const { html, put, plural, cells } = window.markup;
// Palette "This page" group. shell.js reads it at start, so it is set before the shell runs.
// The shell's view switch (shell.js, view block, sd:5064): Open, the rows acted on, first; Settled, the record, second;
// Filed (sd:5488), what you filed in Jira and on GitHub this year, on its own table. The rows the operator hid (sd:5486) are a
// filter on Open, Hidden in Open | Hidden, kept as ?hidden=1 (sd:5753, sd:6701); an older #view=hidden link, from when they were a view, lands on it.
window.PAGE_VIEWS = [{ id: 'open', label: 'Open' }, { id: 'settled', label: 'Settled' }, { id: 'filed', label: 'Filed' }];
if (location.hash === '#view=hidden') {
  const p = new URLSearchParams(location.search); p.set('hidden', '1');
  try { history.replaceState(history.state, '', `${location.pathname}?${p}#view=open`); } catch (_) {}
}
const BY_TEXT = 'repo, title and reason';
window.PAGE_FILTER = { open: BY_TEXT, settled: BY_TEXT, filed: 'tickets and issues' };
window.PAGE_COMMANDS = [
  { label: 'Show the GitHub sync', icon: 'activity', run: () => document.dispatchEvent(new CustomEvent('shell:open', { detail: 'collector' })) },
  // sd:8146: the head's Refresh from GitHub, which runs Re-run collector's sync.
  { label: 'Refresh from GitHub', icon: 'rotate-ccw', run: () => document.dispatchEvent(new CustomEvent('contributions:refresh')) },
  ...['all', 'internal', 'external'].map(v => ({ label: `Show ${v} repositories`, icon: 'filter', run: () => document.dispatchEvent(new CustomEvent('contributions:scope', { detail: v })) })),
];
// build: nothing is read yet, so the page claims nothing for the rail until /api/contributions/page answers.
window.PAGE_ATTENTION = { state: 'unknown', n: 0, what: 'contributions not read' };
addEventListener('DOMContentLoaded', () => {
  const C = window.shell.commands;
  const LABEL = { newly_unblocked: 'Newly unblocked', awaiting_you: 'Awaiting you', awaiting_them: 'Awaiting them', merged: 'Merged', closed: 'Closed', github: 'Settled on GitHub' };
  const ORDER = ['newly_unblocked', 'awaiting_you', 'awaiting_them'];
  const STATE = { newly_unblocked: 'caution', awaiting_you: 'caution', awaiting_them: 'queued' };
  const GLYPH = { warning: '■', caution: '▲', queued: '◌', ok: '●', unknown: '▨' };
  const SCOPES = ['all', 'internal', 'external'];
  const FRESH = { fresh: 'ok', stale: 'caution', degraded: 'warning', never: 'unknown', unknown: 'unknown' };
  const I = n => html`<svg class="i" aria-hidden="true"><use href="#i-${n}"/></svg>`;
  const n = x => Number(x || 0).toLocaleString('en-US');
  // The scope chips (pack sd:7084): Internal and External, each with its open count; none pressed is every repository,
  // and a second press on the pressed one clears it.
  const scopeSeg = count => put(document.getElementById('scope'), window.shell.list.fseg('scope', 'Repositories', SCOPES.slice(1).map(v =>
    ({ v, pressed: v === scope, label: html`${v[0].toUpperCase() + v.slice(1)} <span class="n">${count(v)}</span>` }))));
  // build: a POSIX single-quoted word, so a title with $ or a quote copies as itself.
  const shq = s => `'${String(s).replace(/'/g, `'\\''`)}'`;
    // build: only an https github.com URL becomes a link, as v1's _link allows.
  const github = u => /^https:\/\/github\.com\/[^/@\s]+\/[^/@\s]+\//.test(String(u || ''));
  // sd:5488: an https link with a host and no credentials, for a Jira or GitHub row the Filed view opens.
  const safeLink = u => /^https:\/\/[^/@\s]+\//.test(String(u || ''));
  const TRACKER = { jira: 'Jira', github: 'GitHub' }, KIND = { jira: 'Jira ticket', github: 'GitHub issue' };
  const isIssue = r => /\/issues\/\d+$/.test(String(r.url || ''));
  const filed = r => r.url ? (isIssue(r) ? 'Issue' : 'Pull request') : r.has_draft ? 'Unfiled issue draft' : 'Unfiled local work';
  const num = r => r.url ? '#' + String(r.url).split('/').pop() : '';
  // sd:5301: when GitHub merged or closed a row, as the shadow collector last read it; a date that does not parse is none.
  const dated = v => isFinite(Date.parse(v || ''));
  const settle = r => dated(r.merged_at) ? ['merged', r.merged_at] : dated(r.closed_at) ? ['closed', r.closed_at] : null;

  // ---------- Data (build: /api/contributions/page) ----------
  let DOC = null, ROWS = [], BY = new Map(), selected = null, FAILED = '', STORE = '', SYNCING = false, SYNC = null;
  // The Filed view (sd:5488): its rows, by key, and whether it is the shown view.
  let FILED = [], FBY = new Map(), FILEDVIEW = false;
  const u0 = new URLSearchParams(location.search);
  // Open | Hidden (sd:6701, after sd:5753's toggle): which rows the Open table draws, kept as ?hidden=1.
  let HID = window.shell.aside.mode('hidden');
  const inView = r => window.shell.aside.shows(HID, r.hidden), HIDDEN = () => HID === 'aside';
  let scope = SCOPES.includes(u0.get('scope')) ? u0.get('scope') : 'all';
  // The lane filter: an open lane, or `github`, the open rows GitHub already merged or closed (the fourth lamp).
  let lane = [...ORDER, 'github'].includes(u0.get('lane')) ? u0.get('lane') : null;
  const inLane = r => !lane || (lane === 'github' ? !!settle(r) : r.lane === lane);
  // The text filter is the shell's Shapeshift bar (sd:5539), which reads ?q= too.
  const q = { value: u0.get('q') || '' };
  const inScope = r => scope === 'all' || (scope === 'internal') === !!r.internal;
  const scoped = () => ROWS.filter(r => inScope(r) && inView(r));
  // build: counts come from the document's per-scope totals, which cover every open row; ROWS may be cut at OPEN_LIMIT.
  const tot = (k, sc = scope) => (sc === 'all' ? ['internal', 'external'] : [sc]).reduce((a, s) => a + ((DOC.counts || {})[s]?.[k] || 0), 0);
  const openN = sc => ORDER.reduce((a, l) => a + tot(l, sc), 0);
  const words = r => [r.title, r.repo, r.url, LABEL[r.lane], ...(r.reasons || [])].join(' ').toLowerCase();
  // The header sorts the rows inside their lane (sd:3455, the shell's list grammar). Observed, newest first, is the server's
  // order and the default; a tie keeps it. The lane carries the state, so State has no order of its own.
  const SORTS = { repo: r => String(r.repo || 'local').toLowerCase(), what: r => String(r.title || '').toLowerCase(), at: r => Date.parse(r.observed_at) || 0 };
  const LIST = { sort: 'at', dir: -1, size: 50 }, L = window.shell.list.listParams(u0, { ...LIST, page: 1 }, { sorts: Object.keys(SORTS), size: LIST.size });
  const COLS = [[null, html`<span class="sr">State</span>`], ['repo', 'Repo'], ['what', 'Contribution'], ['at', 'Observed'], [null, 'Act']];
  const order = (a, b) => { const x = SORTS[L.sort](a), y = SORTS[L.sort](b); return (x < y ? -1 : x > y ? 1 : 0) * L.dir; };
  const shown = () => { const v = q.value.trim().toLowerCase(); return scoped().filter(r => inLane(r) && (!v || words(r).includes(v))).sort(order); };
  // The reader (read.js) puts the objects adopt returns and retires the ones a reading no longer lists: the pick is dropped
  // and the object becomes a type no command is on, so no stale Acknowledge can act.
  function adopt(doc) {
    DOC = doc;
    ROWS = (doc.rows || []).filter(r => ORDER.includes(r.lane)).map(r => ({ ...r, id: r.key, type: 'contribution', label: r.title, s: STATE[r.lane], hidden: !!r.hidden,
      event_ids: r.event_ids || [], reasons: r.reasons || [], freshness: r.freshness || { status: 'unknown', reason: '' },
      ...Object.fromEntries(['evidence', 'dependencies', 'delivery'].map(k => [k, r[k] || { state: 'unknown', reason: 'the server sent no ' + k }])) }));
    BY = new Map(ROWS.map(r => [r.key, r]));
    FILED = ((doc.filed || {}).rows || []).map(r => ({ ...r, id: r.key, type: 'filed', label: r.title || r.ref }));
    FBY = new Map(FILED.map(r => [r.key, r]));
    STORE = doc.store && !doc.store.available ? doc.store.reason || 'the view store was not read' : '';
    document.body.dataset.observed = doc.read || '';
    // Rail badge (shell.js): the read's own, the rule every page's rail shows (sd:3446, contribution_screen._attention).
    window.PAGE_ATTENTION = doc.attention || { state: 'unknown', n: 0, what: 'contributions not read' };
    window.shell.attention?.();
    return { objects: [...ROWS, ...FILED, { id: 'collector', type: 'collector', label: 'GitHub sync' }], state: stateOf(doc) };
  }
  // A failed read clears the page: no rows, lamps or counts, and the rail claims nothing.
  function clear(err) {
    DOC = null; ROWS = []; BY = new Map(); FILED = []; FBY = new Map(); FAILED = err.message; STORE = '';
    window.PAGE_ATTENTION = { state: 'unknown', n: 0, what: 'contributions not read' };
    window.shell.attention?.();
  }

  // ---------- Commands (products/system/commands.md) ----------
  // build: Acknowledge and Make task ask first, since no verb un-acknowledges or deletes a task; the reference gave both Undo.
  // Draft nudge (sd:5887) puts an editable draft in Details and posts nothing; the draft is kept per row until it posts or the
  // page reloads. Post nudge asks first, naming the pull request or issue, then the server posts the draft as the operator's
  // comment through gh, with the URL from the row's key and the text on stdin (/api/actions/run contribution.nudge).
  // A write's answer is its own: the reread after it is started, not awaited, so a failed reread never reports a landed write
  // as "not changed". Until the reread lands, the row itself records the write, so the command that made it goes off.
  // A refused write reads again with load(): only reread() says a change landed.
  // The object may be retired by now (a failed reread): it stays retired, since only a read lists a row again.
  const landed = (o, change) => { Object.assign(o, change); if (C.get(o.id) === o) C.put(o); if (DOC) { renderRows(); if (selected === o.key) show(o.key); } };
  const ackCli = o => `sd task contribution ack ${shq(o.key)} ${o.event_ids.map(e => `--event ${shq(e)}`).join(' ')} --if-revision ${o.revision}`;
  // build: Make task files the contribution task `sd task contribution add` files: the row's URL is its identity, so the
  // projection links the task to the row after a reread, and the library refuses a second task for the same URL.
  const linkOf = o => /^github:/.test(o.key) ? 'pull_url' : /^issue:/.test(o.key) ? 'issue_url' : null;
  const taskJson = o => JSON.stringify({ [linkOf(o)]: o.key.slice(o.key.indexOf(':') + 1) });
  const kindOf = o => isIssue(o) ? 'issue' : 'pr';
  const NO_VIEW_CLI = 'no CLI: view state; the dashboard writes it (POST /api/contributions/view)';
  // The row moves at once, then the page reads again; a refused write reads again with load(), as the other writes do.
  const view = (o, hidden) => window.shell.post('/api/contributions/view', { key: o.key, hidden })
    .then(v => { landed(o, { hidden }); reread(); return v; }, e => { load(); throw e; });
  const DRAFTS = new Map();
  const nudgeable = o => !o.url ? 'not filed on GitHub' : o.lane !== 'awaiting_them' ? 'the next step is yours, not theirs' : true;
  const nudgeText = o => `Hi, a friendly nudge on this ${isIssue(o) ? 'issue' : 'pull request'}. Is there anything I can change or add to help it along?`;
  const nudgeDraft = r => DRAFTS.has(r.key) ? html`<section class="nudge" aria-labelledby="nudge-h"><h3 id="nudge-h">Nudge draft</h3>
      <textarea class="cap-in" id="nudge-text" rows="5" maxlength="4000" aria-describedby="nudge-why">${DRAFTS.get(r.key)}</textarea>
      <p class="why" id="nudge-why">Nothing is posted until Post nudge, which asks first and names ${r.url}.</p></section>` : '';
  function registerCommands() {
    C.register(
      { id: 'contribution.ack', on: 'contribution', label: 'Acknowledge', does: 'Clears this row\'s attention events; this cannot be undone.', key: 'a', icon: 'check', risk: 'confirm', bulk: true, primary: () => true,
        when: o => o.event_ids.length ? true : 'no attention event on this row',
        consequence: o => `This clears ${plural(o.event_ids.length, 'attention event')} on ${o.title}. No verb restores them; it completes no task and sends nothing.`,
        cli: ackCli,
        run: o => window.shell.post('/api/contributions/acknowledge', { key: o.key, revision: o.revision, event_ids: o.event_ids })
          .then(() => { landed(o, { event_ids: [] }); reread(); return `Acknowledged · ${o.label}`; }, e => { load(); throw e; }) },
      { id: 'contribution.nudge', on: 'contribution', label: 'Draft nudge', does: 'Puts a nudge draft in Details to edit; it posts nothing.', key: 'd', icon: 'message-square', risk: 'safe', executes: false, primary: o => !DRAFTS.has(o.key),
        when: nudgeable,
        cli: o => `gh ${kindOf(o)} comment ${o.url} --body-file nudge.md`,
        run: o => { if (!DRAFTS.has(o.key)) DRAFTS.set(o.key, nudgeText(o)); select(o.key, true); document.getElementById('nudge-text')?.focus(); return 'Nudge drafted in Details: edit it, then Post nudge'; } },
      { id: 'contribution.post', on: 'contribution', label: 'Post nudge', does: 'Posts the draft in Details as your comment on GitHub.', key: 'n', icon: 'send', risk: 'confirm', executes: true, primary: o => DRAFTS.has(o.key),
        when: o => { const w = nudgeable(o); return w !== true ? w : DRAFTS.has(o.key) || 'draft a nudge first'; },
        consequence: o => `This posts the draft in Details as your comment on ${o.url}. Anyone who follows that ${isIssue(o) ? 'issue' : 'pull request'} sees it; no verb takes it back.`,
        cli: o => `gh ${kindOf(o)} comment ${o.url} --body-file nudge.md`, sends: o => `POST /api/actions/run {action: contribution.nudge, key: ${o.key}, revision, body}`,
        run: o => window.shell.post('/api/actions/run', { action: 'contribution.nudge', key: o.key, revision: o.revision, body: DRAFTS.get(o.key) || '' })
          .then(r => { if (r.exit !== 0) throw new Error(`gh exited ${r.exit}: ${r.output}`); DRAFTS.delete(o.key); landed(o, {}); return `Nudge posted on ${o.url}`; },
            e => { if (e.status === 409) load(); throw e.status === 504 ? new Error(`${e.message} Whether it posted is not known: look on GitHub before you post again.`) : e; }) },
      { id: 'contribution.task', on: 'contribution', label: 'Make task', does: 'Files a task that tracks this contribution; no Undo, cancel it in Tasks.', key: 'k', risk: 'confirm', executes: true,
        when: o => o.item_id ? `already tracked as item #${o.item_id}` : linkOf(o) ? true : 'not filed on GitHub',
        consequence: o => `This files a task, "${o.title}", that tracks ${o.url || o.key} as this contribution. No verb deletes it; cancel it from Tasks.`,
        cli: o => `printf '%s\\n' ${shq(taskJson(o))} > contribution.json && sd task contribution add ${shq(o.title)} --file contribution.json`,
        run: o => window.shell.post('/api/contributions/task', { key: o.key, title: o.title })
          .then(out => { landed(o, { item_id: out.item?.id }); reread(); return `Task #${out.item?.id} filed · ${o.label.slice(0, 48)}`; }, e => { load(); throw e; }) },
      // sd:5628: opens the page in a new tab, as every Open on GitHub does; the gh line stays for copying.
      { id: 'contribution.open', on: 'contribution', label: 'Open on GitHub', does: 'Opens it on GitHub in a new tab.', key: 'o', icon: 'arrow-up-right', risk: 'safe', executes: false,
        when: o => github(o.url) || 'not filed on GitHub', cli: o => `gh ${kindOf(o)} view --web ${o.url}`,
        run: o => { window.open(o.url, '_blank', 'noopener'); return 'Opened on GitHub in a new tab'; } },
      // build (sd:3432): Dismiss closes a row with no item that will never settle, such as a pull request that now 404s.
      // The row leaves the open lanes; new attention shows it again, and a lifecycle change ends the dismissal.
      { id: 'contribution.dismiss', on: 'contribution', label: 'Dismiss', does: 'Moves this contribution to Closed; no Undo, new activity shows it again.', key: 'x', risk: 'confirm',
        when: o => o.dismissed ? 'dismissed; the page reads again' : o.item_id ? `tracked as item #${o.item_id}; close the item instead`
          : linkOf(o) ? true : 'not filed on GitHub',
        consequence: o => `This moves ${o.title} to Closed. No verb restores it; new attention or a change on GitHub shows it again, and Make task tracks it.`,
        cli: o => `sd task contribution dismiss ${shq(o.key)} --if-revision ${o.revision}`,
        run: o => window.shell.post('/api/contributions/dismiss', { key: o.key, revision: o.revision })
          .then(() => { landed(o, { dismissed: true }); reread(); return `Dismissed · ${o.label}`; }, e => { load(); throw e; }) },
      // build (sd:5486): Hide and Unhide, the view state Designs' Hide writes; Undo posts the other value. Nothing expires it.
      ...[[true, 'hide', 'Hide', 'Hides this contribution from the open rows and counts; Undo shows it again.', 'eye-off', 'Hidden', '. Hidden lists it.'],
        [false, 'unhide', 'Unhide', 'Shows this contribution in the open rows again; Undo hides it.', 'eye', 'Unhidden', '']].map(([hidden, id, label, does, icon, done, after]) => (
        { id: `contribution.${id}`, on: 'contribution', label, does, key: 'h', icon, risk: 'undo',
          when: o => o.hidden === hidden ? (hidden ? 'already hidden' : 'not hidden') : STORE ? `Hide is off: ${STORE}` : true,
          cli: () => NO_VIEW_CLI,
          run: o => view(o, hidden).then(() => `${done} · ${o.label}${after}`),
          undo: o => view(o, !hidden) })),
      // build (sd:5488): Edit opens the ticket or issue where it lives, in a new tab; nothing is posted, and the next sync reads it back.
      { id: 'filed.edit', on: 'filed', label: 'Edit', does: 'Opens it in Jira or on GitHub in a new tab; writes nothing.', key: 'e', icon: 'arrow-up-right', risk: 'safe',
        when: o => safeLink(o.url) || 'no https link recorded',
        cli: o => `open ${shq(o.url)}`,
        run: o => { window.open(o.url, '_blank', 'noopener'); return `Opened in ${TRACKER[o.tracker]} · ${o.ref}`; } },
      // Re-run collector (commands.js); Contributions reads again once the run ends.
      // sd:8146: while the run it follows is live, the head's Refresh from GitHub is off and says Refreshing….
      SYNC = window.commands.syncCollector({ reread: () => reread(), page: 'Contributions', running: on => { SYNCING = on; if (DOC) renderHead(); } }),
    );
  }

  // ---------- Focus (sd:2426) ----------
  // Replacing a box's markup drops the focused control, and focus falls to the body. Focus its replacement instead: the
  // control with the same command, object, menu or lamp. If it went off, focus the nearest action: the same row's action
  // menu or command, else the box's first command; a control with no name goes to its row's first button.
  const NAMES = ['cmd', 'obj', 'menuFor', 'cell'];
  const rowOf = el => el.closest('tr[data-id]')?.dataset.id;
  function putKeep(box, content) {
    const a = document.activeElement;
    if (!a || a === box || !box.contains(a)) return put(box, content);
    const row = rowOf(a), named = NAMES.some(k => a.dataset[k]);
    put(box, content);
    const all = [...box.querySelectorAll('button')], mine = all.filter(b => rowOf(b) === row);
    const near = row != null && mine.length ? mine : all;
    const next = named ? near.find(b => NAMES.every(k => b.dataset[k] === a.dataset[k])) || near.find(b => b.dataset.menuFor) || near.find(b => b.dataset.cmd) || near[0]
      : near[0];
    next?.focus();
  }

  // ---------- Head: scope counts, lamps, source line ----------
  function renderHead() {
    const c = DOC.collector || {}, st = FRESH[c.state] || 'unknown';
    put(document.getElementById('sub'), html`Pull requests and issues you opened or owe · <span id="open-n">${n(openN(scope))}</span> open · read <time class="rel" datetime="${DOC.read}"></time>`);
    scopeSeg(v => n(openN(v)));
    const cnt = g => tot(g);
    const LANES = [
      ['newly_unblocked', 'Newly unblocked', 'git-merge', 'caution', 'ready'],
      ['awaiting_you', 'Awaiting you', 'flag-triangle-right', 'caution', 'rows'],
      ['awaiting_them', 'Awaiting them', 'inbox', 'ok', 'rows'],
    ];
    // A lane with no rows has nothing to filter: a div that says so, not a disabled button (design.md § Lamps).
    const lamp = ([id, lbl, icon, lit, unit]) => {
      const k = cnt(id), s = k ? lit : 'ok';
      return { button: !!k, pressed: k ? lane === id : undefined, state: s, attrs: html` data-cell="${id}"`, label: lbl, mark: I(icon),
        val: html`<span class="ph"><b>${k}</b> ${unit}</span>`, sr: `state ${s}` };
    };
    // The fourth lamp: open rows GitHub already merged or closed, from the collector's dates (sd:5301). A pack that records
    // none leaves it unknown, never zero.
    const settledLamp = DOC.github_dates ? lamp(['github', 'Settled on GitHub', 'circle-dot', 'caution', 'rows'])
      : { state: 'unknown', attrs: html` data-cell="github"`, label: 'Settled on GitHub', mark: I('circle-dot'),
        val: html`<span class="ph">not recorded</span> · <span class="ph">this sd records no merge or close dates</span>`, sr: 'state unknown' };
    putKeep(document.getElementById('annunciator'), cells([...LANES.map(lamp), settledLamp]));
    // The Filed tab counts the open rows (sd:8018); the view still lists the closed and merged ones.
    window.shell.view.count('filed', FILED.filter(r => !['closed', 'merged'].includes(r.state)).length);
    const unknown = tot('unknown');
    put(document.getElementById('source'), html`<span class="g-${unknown ? 'unknown' : 'ok'}" aria-hidden="true">${GLYPH[unknown ? 'unknown' : 'ok']}</span>
      <button class="linkish" type="button" data-collector>Collector</button>
      <span><b>${n(unknown)}</b> of ${n(openN(scope))} open rows unknown freshness</span>
      <span class="g-${st}">GitHub sync ${c.state || 'unknown'}${c.last_success_at ? html`, last success <time class="rel" datetime="${c.last_success_at}"></time>` : ''}</span>
      <button class="linkish" type="button" data-gh-refresh${SYNCING ? html` disabled` : ''}>${SYNCING ? 'Refreshing…' : 'Refresh from GitHub'}</button>
      <span>${n(tot('linked'))} linked items · ${n(tot('unfiled'))} unfiled</span>`);
  }

  // ---------- Ledger ----------
  const tbody = document.getElementById('rows'), title = document.getElementById('open-title');
  function rowHtml(r) {
    const f = r.freshness;
    return html`<tr data-id="${r.key}" data-lane="${r.lane}" aria-selected="${String(r.key === selected)}">
      <td class="g g-${r.s}"><span aria-hidden="true">${GLYPH[r.s]}</span><span class="sr">${r.s}</span></td>
      <td class="repo">${r.repo || 'local'}</td>
      <td class="what"><button type="button">${[num(r), r.title].filter(Boolean).join(' ')}</button>
        <span class="meta">${filed(r)}${settledMeta(r)}</span>
        ${links(r).length ? html`<span class="links">${links(r)}</span>` : ''}
        ${r.reasons.length ? html`<span class="reason">${String(r.reasons[r.reasons.length - 1]).slice(0, 180)}</span>` : ''}</td>
      <td class="obs">${f.status === 'current' ? html`<span class="g-ok" aria-hidden="true">●</span>` : html`<span class="g-unknown" title="${f.reason}">▨</span>`}<time class="rel" datetime="${r.observed_at || ''}" data-empty="never"></time><span class="sr"> · ${f.status}</span></td>
      <td class="act"><span class="row-acts">${C.rowActions(r.key)}</span></td></tr>`;
  }
  // sd:5308: the issues a row links, GitHub as owner/repo#N and Jira as its key, each in a new tab; none draws nothing.
  const links = r => (Array.isArray(r.linked) ? r.linked : []).filter(l => l && /^https:\/\/[^/@\s]+\//.test(String(l.url || '')) && l.key)
    .map((l, i) => html`${i ? ' ' : ''}<a class="ext" href="${l.url}" target="_blank" rel="noopener noreferrer">${l.key}</a>`);
  const settledMeta = r => { const s = settle(r); return s ? html` · GitHub ${s[0]} <time class="rel" datetime="${s[1]}"></time>` : ''; };
  // No carried row in the scope: a cut document may still count open rows there, which the limit left out (review 4, PR #72).
  function empty() {
    // Rows are left out only when the document says it cut some; otherwise a count from the last read may lag a write.
    const where = scope === 'all' ? 'any' : scope, left = !DOC.truncated ? 0 : HIDDEN() ? tot('hidden') : openN(scope);
    if (HID !== 'open' && STORE) return `Hidden not read: ${STORE}`;
    return left ? `${plural(left, HIDDEN() ? 'hidden row' : 'open row')} in ${where} repositories ${left === 1 ? 'was' : 'were'} left out by the limit; v1 /classic/contributions lists every one.`
      : HIDDEN() ? `Nothing hidden in ${where} repositories` : `Nothing open in ${where} repositories`;
  }
  function renderRows() {
    const all = scoped(), rows = shown(), v = q.value.trim();
    put(document.getElementById('rows-head'), window.shell.list.sortHead(COLS, L));
    const head = l => html`<tr class="lane" data-lane="${l}"><td colspan="5"><span class="label">${LABEL[l]} · ${rows.filter(r => r.lane === l).length}</span></td></tr>`;
    putKeep(tbody, html`${rows.length ? ORDER.filter(l => rows.some(r => r.lane === l)).map(l => [head(l), rows.filter(r => r.lane === l).map(rowHtml)])
      : html`<tr class="lane"><td colspan="5"><span class="label">${all.length ? `Nothing matches${v ? ` “${v}”` : ''}` : empty()}</span></td></tr>`}`);
    const f = document.getElementById('filtered');
    const cut = DOC.truncated ? `The document lists ${n(DOC.rows.length)} of ${n(DOC.open_total)} open rows; v1 /classic/contributions lists every one.` : '';
    f.hidden = !(lane || v || cut);
    f.textContent = [lane || v ? (rows.length ? `${rows.length} of ${all.length} shown${lane ? ' · ' + LABEL[lane] : ''}${v ? ` · “${v}”` : ''}. Esc clears.` : `Nothing matches${v ? ` “${v}”` : ''}. Esc clears.`) : '', cut].filter(Boolean).join(' ');
    title.textContent = HIDDEN() ? 'Hidden' : 'Open';
    // sd:7211: Re-run collector heads the Open view, as Clean all safe heads Health's Checkouts: the collector's row actions,
    // so the sync needs no Collector pick first.
    putKeep(document.getElementById('tally'), html`${HIDDEN() ? html`${n(tot('hidden'))} hidden`
      : html`<span class="g-caution">▲ ${n(tot('newly_unblocked') + tot('awaiting_you'))} yours</span><span class="g-queued">◌ ${n(tot('awaiting_them'))} theirs</span><span class="g-unknown">▨ ${n(tot('unknown'))} unknown freshness</span>`}${C.rowActions('collector')}`);
    const sort = L.sort !== LIST.sort || L.dir !== LIST.dir ? { sort: L.sort, dir: L.dir > 0 ? 'asc' : 'desc' } : {};
    // The Open table's filter bar: Open | Hidden (sd:6701), counted in the scope.
    put(document.getElementById('hidden-seg'), window.shell.aside.seg('hidden', 'Hidden', HID, { open: openN(scope), aside: tot('hidden') }));
    window.shell.url({ ...(scope !== 'all' && { scope }), ...(lane && { lane }), ...(HID !== 'open' && { hidden: window.shell.aside.param(HID) }), ...(v && { q: v }), ...sort });
  }

  // ---------- Filed (sd:5488) ----------
  const ftbody = document.getElementById('filed-rows');
  const FSTATE = { fresh: 'ok', stale: 'caution', never: 'unknown', unread: 'warning', unrecorded: 'unknown' };
  const unread = s => s.state === 'unread' || s.state === 'unrecorded';
  const trackerOf = t => ((DOC.filed || {}).trackers || {})[t] || { state: 'unread', reason: 'the server sent no Filed rows', filer_unknown: 0 };
  const fwords = r => [r.ref, r.title, r.project, r.status, r.assignee, TRACKER[r.tracker]].join(' ').toLowerCase();
  const fshown = () => { const v = q.value.trim().toLowerCase(); return FILED.filter(r => !v || fwords(r).includes(v)); };
  const fstate = r => r.state === 'closed' ? 'ok' : r.state === 'open' ? 'queued' : 'unknown';
  const assigned = r => r.assignee == null ? 'assignee not synced yet' : r.assignee ? `assigned to ${r.assignee}` : 'unassigned';
  function filedRow(r) {
    const g = fstate(r);
    return html`<tr data-id="${r.key}" aria-selected="${String(r.key === selected)}">
      <td class="g g-${g}"><span aria-hidden="true">${GLYPH[g]}</span><span class="sr">${r.state || 'state not recorded'}</span></td>
      <td class="what"><button type="button">${r.title || r.ref}</button>
        <span class="meta">${r.ref} · ${KIND[r.tracker]}${r.status ? ` · ${r.status}` : ''} · ${assigned(r)}</span></td>
      <td class="obs">${r.created_at ? html`<time class="rel" datetime="${r.created_at}"></time>` : 'created date not synced yet'}</td>
      <td class="act"><span class="row-acts">${C.rowActions(r.key)}</span></td></tr>`;
  }
  // No row is never "nothing filed" on its own: a tracker that did not read is named with its reason.
  function filedEmpty() {
    const year = (DOC.filed || {}).year || 'this year', bad = Object.keys(TRACKER).filter(t => unread(trackerOf(t)));
    const why = bad.map(t => `${TRACKER[t]}: ${trackerOf(t).reason}`).join('; ');
    if (bad.length === 2) return `Filed not read: ${why}`;
    const read = Object.keys(TRACKER).filter(t => !bad.includes(t)).map(t => t === 'jira' ? 'in Jira' : 'on GitHub').join(' or ');
    return `Nothing filed in ${year} ${read}, as the last sync saw it${bad.length ? `; not read: ${why}` : ''}`;
  }
  function renderFiled() {
    const rows = fshown(), v = q.value.trim(), undated = FILED.filter(r => !r.created_at).length;
    putKeep(ftbody, rows.length ? html`${rows.map(filedRow)}`
      : html`<tr class="lane"><td colspan="4"><span class="label">${FILED.length ? `Nothing matches “${v}”` : filedEmpty()}</span></td></tr>`);
    put(document.getElementById('filed-tally'), html`<span>${n(FILED.length)} filed in ${(DOC.filed || {}).year || 'this year'}</span>${undated ? html`<span class="g-unknown">▨ ${n(undated)} created date not synced yet</span>` : ''}`);
    put(document.getElementById('filed-source'), html`${Object.keys(TRACKER).map(t => {
      const s = trackerOf(t), g = FSTATE[s.state] || 'unknown', name = TRACKER[t];
      return html`<span class="g-${g}"><span aria-hidden="true">${GLYPH[g]}</span> ${unread(s) ? `${name} not read: ${s.reason}` : `${name} sync ${s.state}`}${!unread(s) && s.last_success_at ? html`, last success <time class="rel" datetime="${s.last_success_at}"></time>` : ''}${!unread(s) && s.reason ? ` · ${s.reason}` : ''}</span>${s.filer_unknown ? html`<span>${plural(s.filer_unknown, KIND[t])}: whether you filed ${s.filer_unknown === 1 ? 'it' : 'them'} is not synced yet</span>` : ''}`;
    })}`);
  }

  // ---------- Settled: per local day over 28 days, then counts per repository ----------
  // A day is the browser's local day; the document carries 29 days of dates, so any zone's 28 are inside it (sd:5301).
  const DAYS = 28, dayOf = t => { const d = new Date(t); return `${d.getFullYear()}-${d.getMonth() + 1}-${d.getDate()}`; };
  const fmt = t => new Date(t).toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
  function renderDays(sides) {
    const tp = document.getElementById('tp'), note = document.getElementById('tp-note');
    if (!DOC.github_dates) {
      tp.setAttribute('aria-label', 'Settle dates not recorded');
      put(tp, html``);
      note.textContent = 'This version records no merge or close dates: update the command pack, then press Re-run collector.';
      return;
    }
    const end = new Date(Date.parse(DOC.read)); end.setHours(12, 0, 0, 0);
    const days = Array.from({ length: DAYS }, (_, i) => { const d = new Date(end); d.setDate(d.getDate() - (DAYS - 1 - i)); return d.getTime(); });
    const at = new Map(days.map((d, i) => [dayOf(d), i])), merged = days.map(() => 0), closed = days.map(() => 0);
    const S = (DOC.settled_recent || []).filter(inScope);
    let older = sides.reduce((a, s) => a + ((DOC.settled_older || {})[s] || 0), 0);
    S.forEach(e => { const t = Date.parse(e.at), i = at.get(dayOf(t)); if (i != null) (e.how === 'merged' ? merged : closed)[i]++; else if (t < days[0]) older++; });
    const day = days.map((_, i) => merged[i] + closed[i]), max = Math.max(1, ...day), pk = day.indexOf(max);
    const sumM = merged.reduce((a, b) => a + b, 0), sumC = closed.reduce((a, b) => a + b, 0);
    tp.setAttribute('aria-label', `${sumM} merged and ${sumC} closed from ${fmt(days[0])} to ${fmt(days[DAYS - 1])}${sumM + sumC ? `; busiest day ${fmt(days[pk])} with ${max}` : ''}`);
    put(tp, html`<svg viewBox="0 0 280 80" preserveAspectRatio="none" aria-hidden="true">${days.map((d, i) => { const hm = merged[i] / max * 76, hc = closed[i] / max * 76;
      return html`<rect class="m" x="${i * 10 + 1}" y="${(79 - hm).toFixed(2)}" width="8" height="${hm.toFixed(2)}"><title>${fmt(d)}: ${merged[i]} merged, ${closed[i]} closed</title></rect><rect class="c" x="${i * 10 + 1}" y="${(79 - hm - hc).toFixed(2)}" width="8" height="${hc.toFixed(2)}"/>`; })}<line class="base" x1="0" x2="280" y1="79.5" y2="79.5"/></svg>
      <div class="tp-axis"><span>${fmt(days[0])}</span><span>${sumM + sumC ? `busiest ${fmt(days[pk])} · ${max}` : 'none in this window'}</span><span>${fmt(days[DAYS - 1])}</span></div>`);
    const undated = sides.reduce((a, s) => a + ((DOC.settled_undated || {})[s] || 0), 0);
    note.textContent = [`When GitHub merged or closed each row, as the collector last read it; days are local.`,
      older ? `${n(older)} settled before this window.` : '',
      undated ? `${plural(undated, 'settled row')} ${undated === 1 ? 'has' : 'have'} no merge or close date.` : ''].filter(Boolean).join(' ');
  }
  function renderSettled() {
    const S = (DOC.settled || []).filter(inScope), other = DOC.settled_other || {};
    const sides = scope === 'all' ? ['internal', 'external'] : [scope];
    const otherN = sides.reduce((a, s) => a + (other[s]?.merged || 0) + (other[s]?.closed || 0), 0);
    const merged = S.reduce((a, e) => a + e.merged, 0) + sides.reduce((a, s) => a + (other[s]?.merged || 0), 0);
    const closed = S.reduce((a, e) => a + e.closed, 0) + sides.reduce((a, s) => a + (other[s]?.closed || 0), 0);
    document.getElementById('settled-tally').textContent = `${n(merged + closed)} rows · ${n(merged)} merged · ${n(closed)} closed`;
    renderDays(sides);
    const top = S.slice(0, 12), rest = S.slice(12).reduce((a, e) => a + e.merged + e.closed, 0) + otherN;
    const list = [...top.map(e => [e.repo, e.merged + e.closed]), ...(rest ? [['other repositories', rest]] : [])];
    const max = Math.max(1, ...list.map(x => x[1]));
    const bars = document.getElementById('bars');
    bars.setAttribute('aria-label', list.length ? 'Settled contributions per repository: ' + list.map(([r, c]) => `${r} ${c}`).join(', ') : 'No settled contributions');
    put(bars, html`${list.length ? list.map(([r, c]) => html`<span class="name" title="${r}">${r}</span><span class="v">${n(c)}</span><svg aria-hidden="true"><rect class="fill" x="0" y="0" width="${(c / max * 100).toFixed(1)}%" height="12" rx="2"/></svg>`) : html`<span class="name">No settled rows in ${scope === 'all' ? 'any' : scope} repositories</span>`}`);
  }

  // ---------- Details ----------
  // build (sd:3394): dependencies, evidence and notification delivery, which only /classic/contributions showed. The server
  // cuts every local path to its last part. A section it could not read says so with the reason; it never draws as empty.
  const glyph = s => html`<span class="g-${s}" aria-hidden="true">${GLYPH[s]}</span>`;
  const notRead = s => html`<p class="note">${glyph('unknown')} Not read: ${s.reason}</p>`;
  const ext = u => github(u) ? html`<a class="ext" href="${u}" target="_blank" rel="noopener noreferrer">${String(u).replace('https://github.com/', '')}</a>` : u;
  function dependency(d) {
    if (d.kind === 'item') return html`<a href="${window.shell.itemHref(d.item)}">Complete local item #${d.item}</a>`;
    if (d.kind === 'merge') return html`Merge ${ext(d.url)}`;
    if (d.kind === 'issue') return html`Resolve issue ${ext(d.url)}`;
    if (d.kind === 'release') return html`Publish ${d.repo} release <code>${d.tag || 'tag not selected'}</code> containing ${ext(d.contains_pull)}${d.package ? `; package ${d.package.name}, version ${d.package.version || 'not selected'}` : ''}`;
    return html`${glyph('unknown')} Not read: ${d.reason}`;
  }
  const dependencies = s => s.state !== 'read' ? notRead(s) : s.entries.length ? html`<ul class="facts">${s.entries.map(d => html`<li>${dependency(d)}</li>`)}</ul>`
    : html`<p class="note">No dependencies recorded.</p>`;
  const exit = c => c.exit_code == null ? ['unknown', 'exit code not recorded'] : c.exit_code === 0 ? ['ok', 'exit 0'] : ['warning', `exit ${c.exit_code}`];
  function evidence(s) {
    if (s.state !== 'read') return notRead(s);
    if (!s.checks.length) return html`<p class="note">No test evidence recorded.</p>`;
    return html`<p class="note">${s.verified ? 'Evidence identity verified: each check ran in the clone at the tested commit and passed.'
      : 'Evidence identity is unverified: sd could not tie every check to the tested commit in the clone.'}</p>
      ${s.checks.map(c => { const [st, said] = exit(c); return html`<dl class="check"><dt>Result</dt><dd>${glyph(st)} ${said}</dd>
        <dt>Command</dt><dd><code>${c.command || 'not recorded'}</code></dd><dt>Directory</dt><dd>${c.directory || 'not recorded'}</dd>
        <dt>Commit</dt><dd>${c.commit || 'not recorded'}</dd><dt>Artifact</dt><dd>${c.artifact || 'not recorded'}</dd>
        <dt>SHA-256</dt><dd>${c.sha256 || 'not recorded'}</dd></dl>`; })}
      <p class="note">Recorded commands are evidence. This page does not run them.</p>`;
  }
  const DELIVERY = { sent: ['ok', 'sent'], pending: ['queued', 'waiting to send'], sending: ['queued', 'sending'],
    uncertain: ['unknown', 'delivery uncertain: the sender did not confirm it'], unknown: ['unknown', 'delivery status not recorded'] };
  const delivery = s => s.state !== 'read' ? notRead(s) : s.entries.length ? html`<ul class="facts">${s.entries.map(e => {
    const [st, said] = DELIVERY[e.status] || ['unknown', `delivery status ${e.status} is not one sd records`];
    return html`<li>${glyph(st)} ${e.reason} · ${said}${e.acknowledged_at ? html` · acknowledged <time class="rel" datetime="${e.acknowledged_at}"></time>` : ''}</li>`;
  })}</ul>` : html`<p class="note">No notifications recorded.</p>`;
  // sd:5301: the settle date in local time, from the collector; unknown when this sd records none.
  const onGitHub = r => { const s = settle(r); return s ? html`${s[0]} <time class="rel" datetime="${s[1]}" data-long=""></time>`
    : DOC.github_dates ? 'no merge or close date recorded' : 'not recorded: this sd records no merge or close dates'; };
  const details = document.getElementById('details');
  // The draft follows what is typed, so a redraw of Details or Post nudge reads the edited text.
  details.addEventListener('input', e => { if (e.target.id === 'nudge-text' && DRAFTS.has(selected)) DRAFTS.set(selected, e.target.value); });
  const swap = () => { details.setAttribute('data-swap', ''); requestAnimationFrame(() => details.removeAttribute('data-swap')); };
  function showCollector() {
    selected = 'collector'; mark();
    const c = DOC.collector || {}, st = FRESH[c.state] || 'unknown';
    putKeep(details, html`<p class="kind"><span class="g-${st}" aria-hidden="true">${GLYPH[st]}</span> Collector · contributions projection</p>
      <h2>GitHub sync is ${c.state || 'unknown'}</h2>
      <dl><dt>Rows</dt><dd>${n(DOC.total)}</dd><dt>Freshness unknown</dt><dd>${n(DOC.fresh?.unknown)} of ${n(DOC.total)}</dd><dt>Freshness current</dt><dd>${n(DOC.fresh?.current)}</dd>
        <dt>Last success</dt><dd>${c.last_success_at ? html`<time class="rel" datetime="${c.last_success_at}"></time>` : 'never'}</dd>
        ${c.reason ? html`<dt>Reason</dt><dd>${c.reason}</dd>` : ''}<dt>Source</dt><dd>/api/contributions/page · progress.tracker_freshness</dd></dl>
      <h3>Act</h3>${C.bar('collector')}
      <p class="why">One sync refreshes the trackers and the contributions projection. The nightly job runs the same verb.</p>`);
    C.select('collector'); swap();
    window.shell.suggest?.(['Why is the GitHub sync stale?', 'Which rows would move after a sync?']);
  }
  function showFiled(key) {
    const r = FBY.get(key), g = fstate(r), where = TRACKER[r.tracker];
    selected = key; mark();
    // sd:5629: the Key is the link to the ticket or issue; a row with no https link shows the bare key and says so.
    const keyCell = safeLink(r.url) ? html`<a class="ext" href="${r.url}" target="_blank" rel="noopener noreferrer">${r.ref}${I('arrow-up-right')}</a>` : r.ref;
    const facts = [['Key', keyCell], ['Status', r.status || 'not synced yet'], [r.tracker === 'jira' ? 'Project' : 'Repository', r.project || 'not recorded'],
      ['Assignee', r.assignee == null ? 'not synced yet' : r.assignee || 'unassigned'], ['Filed by', r.author || 'you']];
    putKeep(details, html`<p class="kind"><span class="g-${g}" aria-hidden="true">${GLYPH[g]}</span> ${KIND[r.tracker]} · filed by you</p>
      <h2>${r.title || r.ref}</h2>
      ${safeLink(r.url) ? '' : html`<p class="note">No https link recorded, so it opens nowhere.</p>`}
      <dl>${facts.map(([k, v]) => html`<dt>${k}</dt><dd>${v}</dd>`)}
        <dt>Created</dt><dd>${r.created_at ? html`<time class="rel" datetime="${r.created_at}" data-long=""></time>` : 'created date not synced yet'}</dd>
        <dt>Updated</dt><dd>${r.updated_at ? html`<time class="rel" datetime="${r.updated_at}" data-long=""></time>` : 'not synced yet'}</dd>
        <dt>Last synced</dt><dd><time class="rel" datetime="${r.seen || ''}" data-empty="never"></time></dd></dl>
      <h3>Act</h3>${C.bar(r.key)}
      <p class="why">Edit opens it in ${where}; the dashboard changes nothing there, and the next GitHub sync reads your edit back.</p>`);
    C.select(r.key); swap();
    return true;
  }
  function show(key) {
    if (key === 'collector') return showCollector();
    if (FBY.has(key)) return showFiled(key);
    const r = BY.get(key); if (!r) return false;
    selected = key; mark();
    const where = r.internal ? `internal (${r.why_internal === 'managed' ? 'sd manages it' : 'registered with sd'})` : 'external';
    const facts = [['Repo', `${r.repo || 'local'} · ${where}`], ['Filed as', filed(r) + (r.url ? ' ' + num(r) : '')], ['Lane', LABEL[r.lane]],
      ['Collector says', r.external_state || 'not filed'], ['Local status', r.local_status || 'none'], ['Local item', r.item_id ? '#' + r.item_id : 'none'],
      ['Branch', r.local_branch || 'none'], ['Tested commit', r.tested_commit || 'none'], ['Freshness', r.freshness.status + (r.freshness.reason ? ': ' + r.freshness.reason : '')]];
    putKeep(details, html`<p class="kind"><span class="g-${r.s}" aria-hidden="true">${GLYPH[r.s]}</span> Contribution · ${LABEL[r.lane]}</p>
      <h2>${r.title}</h2>
      ${github(r.url) ? html`<p><a class="ext" href="${r.url}" target="_blank" rel="noopener noreferrer">${String(r.url).replace('https://github.com/', '')}${I('arrow-up-right')}</a></p>` : ''}
      <dl>${facts.map(([k, v]) => html`<dt>${k}</dt><dd>${v}</dd>`)}<dt>On GitHub</dt><dd>${onGitHub(r)}</dd>${links(r).length ? html`<dt>Linked</dt><dd>${links(r)}</dd>` : ''}<dt>Observed</dt><dd><time class="rel" datetime="${r.observed_at || ''}" data-empty="never"></time></dd></dl>
      ${r.reasons.length ? html`<h3>Why it is here</h3><ul class="reasons">${r.reasons.map(x => html`<li>${x}</li>`)}</ul>` : ''}
      <h3>Act</h3>${C.bar(r.key)}${nudgeDraft(r)}
      <p class="why">Acknowledge clears attention only. It does not complete a task or send a notification.</p>
      <h3>Dependencies</h3>${dependencies(r.dependencies)}
      <h3>Evidence</h3>${evidence(r.evidence)}
      <h3>Notification delivery</h3>${delivery(r.delivery)}`);
    C.select(r.key); swap();
    window.shell.suggest?.(r.lane === 'awaiting_them' && r.url ? ['Draft a short, polite nudge for this', 'What changed upstream since I filed it?']
      : ['Why is this still open locally?', 'What would unblock this?']);
    return true;
  }
  function mark() { [tbody, ftbody].forEach(b => b.querySelectorAll('tr[data-id]').forEach(tr => tr.setAttribute('aria-selected', String(tr.dataset.id === selected)))); }
  // The shown view's rows: the Filed table on Filed, the Open table otherwise.
  const listed = () => (FILEDVIEW ? ftbody : tbody).querySelectorAll('tr[data-id]');
  function select(key, open) { if (show(key) !== false && open) window.shell.openPane('tab-details'); }
  const none = () => { selected = null; put(details, html`<p class="why">No contribution matches the filter, so nothing is selected. Esc clears it.</p>`); };
  // The reader found no row to select: the filter hides them all, the scope has none, or the read failed.
  function nothing() {
    if (DOC && (FILEDVIEW ? FILED.length : scoped().length)) return none();
    selected = null; mark();
    put(details, html`<p class="why">${DOC ? (FILEDVIEW ? 'Nothing filed this year is listed, so nothing is selected.' : 'Nothing open is listed in this scope, so nothing is selected.') : 'The contributions were not read, so nothing is selected. Reload retries it.'}</p>`);
  }

  // ---------- Filter ----------
  const reconcile = () => window.shell.reconcile({ rows: listed(), current: selected, keep: k => k === 'collector', select: k => select(k, false), clear: none });
  function applyFilter() { renderRows(); renderFiled(); reconcile(); }
  function setScope(v) { if (!SCOPES.includes(v) || !DOC) return; scope = v; lane = null; renderHead(); renderSettled(); applyFilter(); }
  function setLane(v) { lane = lane === v ? null : v; renderHead(); applyFilter(); }
  function render() { renderHead(); renderSettled(); renderRows(); renderFiled(); }

  // ---------- Events ----------
  // A lane lamp filters the Open rows it counts, so it shows the Open view and shows Open rows.
  document.getElementById('annunciator').addEventListener('click', e => { const c = e.target.closest('button.cell'); if (c) { HID = 'open'; setLane(c.dataset.cell); window.shell.view.show('open'); } });
  document.getElementById('open-filters').addEventListener('click', e => { const b = e.target.closest('button[data-aside="hidden"]'); if (DOC && b) { HID = b.dataset.v; applyFilter(); } });
  document.getElementById('scope').addEventListener('click', e => { const b = e.target.closest('button[data-f="scope"]'); if (b) setScope(b.dataset.v === scope ? 'all' : b.dataset.v); });
  // Open | Hidden sets the Open view's rows aside, so it leaves the filter row on the other views.
  document.addEventListener('shell:view', e => { document.getElementById('hidden-seg').hidden = e.detail.id !== 'open'; });
  document.getElementById('rows-head').addEventListener('click', e => { if (DOC && window.shell.list.sortBy(e, L)) window.shell.list.keepFocus(e, applyFilter); });
  put(document.getElementById('rows-head'), window.shell.list.sortHead(COLS, L)); // drawn before the read, so a failed one keeps the columns
  document.addEventListener('contributions:scope', e => setScope(e.detail));
  // The view switch (shell.js): Filed (sd:5488) moves the selection to its own table, as a filter change moves it. The shell
  // fires the first shell:view on the document's DOMContentLoaded, before this window listener runs, so the view the page
  // opens on is read here too.
  const onView = id => {
    const was = FILEDVIEW; FILEDVIEW = id === 'filed';
    if (DOC && was !== FILEDVIEW) applyFilter();
  };
  document.addEventListener('shell:view', e => onView(e.detail.id));
  onView(window.shell.view?.current?.());
  const refresh = () => { const o = C.get('collector'); if (!SYNCING && o) C.run(SYNC, o); };
  document.getElementById('source').addEventListener('click', e => { if (e.target.closest('[data-collector]')) select('collector', true);
    if (e.target.closest('[data-gh-refresh]')) refresh(); });
  document.addEventListener('contributions:refresh', refresh);
  [tbody, ftbody].forEach(b => b.addEventListener('click', e => { if (e.target.closest('.rowact') || e.target.closest('a')) return; const tr = e.target.closest('tr[data-id]'); if (tr) select(tr.dataset.id, true); }));
  document.addEventListener('shell:open', e => select(e.detail, true));
  document.addEventListener('shell:filter', e => { if (e.detail.q === q.value) return; q.value = e.detail.q; if (DOC) applyFilter(); });
  // The shell walks the shown rows on j / k; Esc clears the lane filter.
  window.PAGE_LIST = { rows: listed, current: () => selected, select: id => select(id, false), keep: k => k === 'collector',
    clear: () => { if (!lane) return false; lane = null; renderHead(); applyFilter(); return true; } };

  // ---------- Start (build: read /api/contributions/page through shell.read, then draw) ----------
  // The reader (read.js, sd:2488) numbers each read so only the newest draws, retires the objects a reading drops, moves a
  // selection whose row is gone, and queues a reread behind a read that started before its write landed.
  function stateOf(doc) {
    const c = doc.collector || {};
    if (!doc.total) return { kind: 'empty', title: 'No contributions', text: 'No contributions yet. Register local work, or press Re-run collector to get your pull requests from GitHub.', source: '/api/contributions/page' };
    if (c.state && c.state !== 'fresh') return { kind: 'partial', title: 'GitHub sync not fresh', text: `The GitHub sync is ${c.state}${c.reason ? ': ' + c.reason : ''}. Rows show what the last sync saw.`, source: 'progress.tracker_freshness' };
    return null;
  }
  // Not read: every box says so, and the state slot above says why.
  function blank() {
    put(document.getElementById('sub'), html`Pull requests and issues you opened or owe · not read`);
    scopeSeg(() => '');
    put(document.getElementById('annunciator'), html``);
    put(document.getElementById('source'), html``);
    put(tbody, html`<tr class="lane"><td colspan="5"><span class="label">Not read: ${FAILED}</span></td></tr>`);
    put(ftbody, html`<tr class="lane"><td colspan="4"><span class="label">Not read: ${FAILED}</span></td></tr>`);
    put(document.getElementById('filed-tally'), html``);
    put(document.getElementById('filed-source'), html``);
    document.getElementById('filtered').hidden = true;
    put(document.getElementById('tally'), html``);
    document.getElementById('settled-tally').textContent = '';
    document.getElementById('tp-note').textContent = '';
    document.getElementById('tp').setAttribute('aria-label', 'Settle dates not read');
    put(document.getElementById('tp'), html``);
    const bars = document.getElementById('bars');
    bars.setAttribute('aria-label', 'Settled contributions not read');
    put(bars, html``);
  }
  const visible = id => id === 'collector' || (FILEDVIEW ? fshown() : shown()).some(r => r.key === id);
  const reading = window.shell.read({
    source: '/api/contributions/page', what: 'the contributions', adopt, clear,
    draw: () => (DOC ? render() : blank()),
    current: () => selected,
    first: () => (FILEDVIEW ? fshown() : shown())[0]?.key ?? null,
    // A ?row= or a kept selection the filter hides goes to the first shown row, as a filter change does.
    select: (id, opened) => (visible(id) ? select(id, opened) : reconcile()),
    unselect: nothing,
  });
  const load = () => reading.load(), reread = () => reading.reread();
  // Read again (sd:5638): the shell's head button reads the table again. The sync is the collector's Re-run collector
  // (sd:5750 took out the toolbar's Refresh, a second way to it).
  window.PAGE_AGAIN = { title: 'Refresh contributions', run: load };
  registerCommands();
  load();
});
