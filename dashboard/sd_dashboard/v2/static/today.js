// Today v2 (sd:2110): the page script. It loads before shell.js, which reads what it declares.
// pack sd:7084: the annunciator heads the page; the rail switches Needs you, Queue, Since yesterday and Timeline, each
// a view-<id> panel, from /api/now (now_screen.document); Queue reads the lane and build documents too (pack sd:7234). A lamp that filters Needs you shows that view.
// Needs you is the document's `needs`: every fact that asks for an act, ranked in the bands, a kind of many rows folded into
// one group row. A group opens in place to its first rows, then "N more". A lamp or an intent shows only its rows; the
// Shapeshift bar's text filters the same list. The badge is the read's own (now_screen._attention), the tally's count.
// A snoozed row (sd:1896) is in the document's `snoozed`: snooze.js draws it under the list with its Unsnooze.
// Markup is html`…` from markup.js: every value put in it is escaped, and put() is the only way into the page.
// moved:start
// sd:5781: an old address of a view that moved opens the view's new page, keeping its query (?row=). sd:5921: an old Today
// view (#view=now|work|inbox|status) is this page now, so its hash leaves the address.
{
  const was = (/^#view=([a-z]+)$/.exec(location.hash) || [])[1];
  const moved = { decisions: '/decide#view=decisions', github: '/github#view=pulls', issues: '/issues#view=issues' }[was];
  if (moved) location.replace(moved.replace('#', `${location.search}#`));
  else if (['now', 'work', 'inbox', 'status'].includes(was)) history.replaceState(null, '', `/today${location.search}`);
  // pack sd:7234: Queues and In motion are the Queue view now.
  else if (was === 'queues' || was === 'motion') history.replaceState(null, '', `/today${location.search}#view=queue`);
}
// moved:end
window.PAGE_FILTER = { '': 'Needs you' };
// pack sd:7218: what runs now comes second, after Needs you; pack sd:7234: the Queue view holds it, In motion folded in.
window.PAGE_VIEWS = [{ id: 'needs', label: 'Needs you', key: 'n' }, { id: 'queue', label: 'Queue', key: 'q' },
  { id: 'digest', label: 'Since yesterday', key: 'y' }, { id: 'timeline', label: 'Timeline', key: 't' }];
(() => {
  // The rail's map (built sections, and the old screen each unported one opens) is /ui/sections.js, loaded before this.
  // Capture (n) and the field's task mode file through the shell's default capture (sd:3520): POST /api/items.

  // now_screen bands → design.md state grammar.
  const STATE = { broken: 'warning', look: 'caution', queued: 'queued' };
  const GLYPH = { warning: '■', caution: '▲', queued: '◌', ok: '●', unknown: '▨' };
  // Each source's label, icon, and the page that shows it in full, with that page's name (sd:3402).
  const SRC = { jobs: ['Jobs', 'activity', '/management', 'Management'], prs: ['Pull requests', 'git-pull-request', '/github', 'GitHub'], sessions: ['Sessions', 'bot', '/management#view=sessions', 'Management'], repos: ['Repos', 'hard-drive-download', '/management', 'Management'],
    lane: ['Lane', 'git-merge', '/today#view=queue', 'Queue'], reports: ['Reports', 'file-text', '/reports', 'Reports'], mail: ['Mail', 'mail', '', ''], tasks: ['Tasks', 'list-checks', '/tasks', 'Tasks'], briefs: ['Briefs', 'book-open', '/briefs', 'Briefs'],
    queues: ['Decision queues', 'inbox', '', ''] };
  const TYPE = { job: 'job', pr: 'pull request', ahead: 'repo', dirty: 'repo', pinned: 'repo', worktree: 'sessions', dark: 'collector', item: 'work item',
    report: 'report', mail: 'mail thread', lane: 'lane entry', brief: 'brief', flaky: 'flaky head', group: 'group', triage: 'decision queues' };
  const KIND = { job: 'Job', pr: 'Pull request', ahead: 'Repo', dirty: 'Repo', pinned: 'Repo', worktree: 'Sessions', dark: 'Collector', item: 'Work item',
    report: 'Report', mail: 'Mail', lane: 'Lane entry', brief: 'Brief', flaky: 'Flaky head', group: 'Group', triage: 'Decision queues' };
  // A group row's command type: the briefs and the flaky heads (pack sd:7663) have their own acts; the rest are work items.
  const GROUP = { 'group:briefs': 'brief group', 'group:flaky': 'flaky group' };
  // The intents (sd:5921): views over the one list, not tabs. Each says which rows it keeps.
  const INTENTS = [['fix', 'Fix', r => r.band === 'broken'], ['reply', 'Reply', r => ['reports', 'mail', 'pulls'].includes(r.lamp)],
    ['ship', 'Ship', r => r.kind === 'lane' || r.id === 'group:ready' || r.state === 'ready_to_send'], ['read', 'Read', r => r.source === 'briefs']];
  const SHOWN = 5; // a group's members before "N more"
  const { html, put } = window.markup;
  const I = n => html`<svg class="i" aria-hidden="true"><use href="#i-${n}"/></svg>`;

  let DOC = null, NEEDS = [], ALL = new Map(), OTHER = new Map(), SNOOZED = [], ACTIVE = null, srcFilter = null, intent = null, query = '', picked = [];
  // pack sd:7271: the Queue view's suggested rows, keyed by item number as Tasks keys a task (?row=<id>): SUGG is each one
  // the read suggests, SROW its /api/tasks row once its Details were read, and TD the Tasks page's Details (task-details.js).
  // OPENI is All items' rows (#40961): each repo's other open tasks, in TD's task shape, from /api/tasks while it is on.
  // TASKS is every /api/tasks row in that shape, read while All items or Suggested shows: a suggested row's task before
  // its Details are read, so its status commands are on from the start (pack sd:7334).
  let SUGG = new Map(), OPENI = new Map(), TASKS = new Map(), TD = null;
  const SROW = new Map();
  const listedTask = k => OPENI.get(k) || (SUGG.has(k) ? TASKS.get(k) : undefined);
  const isItem = id => SUGG.has(id) || OPENI.has(id), taskOf = k => SROW.get(k) || listedTask(k);
  const OPEN = new Set(), FULL = new Set(); // the groups opened, and the ones showing every member
  const $ = id => document.getElementById(id);
  const row = () => new URLSearchParams(location.search).get('row'); // the selected row
  const low = s => String(s || '').toLowerCase();
  const plural = (n, one, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;
  const SR = text => html`<span class="sr">${text}</span>`;
  const COLS = html`<tr><th class="g">${SR('State')}</th><th>What</th><th>Why · next step</th><th>Source</th><th class="num">Age</th><th class="act">${SR('Actions')}</th></tr>`;

  // Age: a row's time against the read's, in the largest whole unit. A row with no time has none.
  function age(at) {
    const t = Date.parse(String(at || '').replace(' ', 'T')), now = Date.parse(DOC.now);
    if (!at || isNaN(t) || isNaN(now)) return '';
    const m = Math.max(0, Math.round((now - t) / 6e4));
    return m < 60 ? `${m}m` : m < 2880 ? `${Math.round(m / 60)}h` : `${Math.round(m / 1440)}d`;
  }
  // A row passes the lamp, the intent and the text; a group passes when it or any member does.
  function passes(r) {
    const one = x => (!srcFilter || x.lamp === srcFilter) && (!intent || INTENTS.find(i => i[0] === intent)[2](x))
      && (!query || low(`${x.what} ${x.detail} ${x.source}`).includes(query));
    return one(r) || (r.members || []).some(one);
  }

  function facts(r) {
    const f = { Detail: r.detail, Source: SRC[r.source]?.[0] || r.source, Band: `${STATE[r.band]} · rank ${r.rank}`, Id: r.id };
    if (r.retry) f['Retry line'] = r.retry;
    return f;
  }

  // One selection (sd:5654): pick names the row in ?row=, marks it alone aria-selected, puts its Details and makes it the
  // shell's selection.
  function pick(id, draw, open) {
    mark(id);
    put($('details'), draw());
    shell.commands.select(id);
    const u = new URL(location.href); u.searchParams.set('row', id); history.replaceState(null, '', u);
    $('details').setAttribute('data-swap', ''); requestAnimationFrame(() => $('details').removeAttribute('data-swap'));
    if (open) shell.openPane('tab-details');
  }
  const listed = id => ALL.has(id) || OTHER.has(id) || QOTHER.has(id) || isItem(id);
  const day = iso => String(iso || '').slice(0, 10), hm = iso => String(iso || '').slice(11, 16);

  function details(r) {
    const s = STATE[r.band], head = html`<p class="kind"><span class="g-${s}" aria-hidden="true">${GLYPH[s]}</span> ${KIND[r.kind] || r.kind} · ${s}</p>`;
    const act = html`<h3>Act</h3>${shell.commands.bar(r.id)}`;
    if (r.group) return html`${head}<h2>${r.what}</h2><p class="why">${r.detail}</p>
      <ul class="members">${r.members.slice(0, 10).map(m => html`<li>${m.item ? html`<a href="${shell.itemHref(m.item)}">${m.what}</a>` : m.what}<span class="via">${m.detail}</span></li>`)}</ul>
      ${r.members.length > 10 ? html`<p class="why">${r.members.length - 10} more; open the group in the list for each.</p>` : ''}${act}`;
    if (r.kind === 'report') return html`${head}<h2>${r.title}</h2>
      <dl><dt>Filed</dt><dd>${day(r.at)} ${hm(r.at)} UTC</dd><dt>Job</dt><dd>${r.job || 'not recorded'}</dd>
        <dt>Host</dt><dd>${r.host || 'not recorded'}</dd><dt>Exit</dt><dd>${r.exit ?? 'not recorded'}</dd>
        ${r.repeats ? html`<dt>Repeats</dt><dd>${r.repeats}</dd>` : ''}<dt>Item</dt><dd><a href="${shell.itemHref(r.item)}">#${r.item}</a></dd>
        ${r.claim ? html`<dt>Taken</dt><dd>${r.claim}</dd>` : ''}</dl>
      ${r.basis ? html`<h3>Why</h3><div class="mailbody">${r.basis}</div>` : ''}
      <h3>Log${r.cut ? ' · last lines' : ''}</h3><div class="mailbody">${r.body || 'no text kept'}</div>
      <p class="why">Acknowledge closes the report, not the fault. The next failure opens a new one.</p>${act}`;
    if (r.kind === 'mail') return html`${head}<h2><a href="${r.url}">${r.subject || '(no subject)'}</a></h2>
      <dl><dt>From</dt><dd>${r.who || 'not recorded'}</dd><dt>Repo</dt><dd>${r.repo}</dd><dt>Sent</dt><dd>${r.at}</dd></dl>${act}`;
    if (r.kind === 'lane') return html`${head}<h2><a href="${shell.itemHref(r.item)}">sd:${r.item} ${r.title}</a></h2>
      <dl><dt>Repo</dt><dd>${r.repo}</dd><dt>Why</dt><dd>${r.detail}</dd><dt>Head</dt><dd>${(r.expected_head || '').slice(0, 12) || 'not recorded'}</dd>
        <dt>Ended</dt><dd>${r.at || 'not recorded'}</dd><dt>Merge</dt><dd>${r.runner_merge}</dd></dl>
      <p><a href="#view=queue">Open the lane in Queue</a></p>${act}`;
    // pack sd:7663: a flaky head, Metrics' Flaky tests row until then. Flakiness is per head: no store names the test.
    if (r.kind === 'flaky') return html`${head}<h2>${r.item ? html`<a href="${shell.itemHref(r.item)}">sd:${r.item} ${r.title}</a>` : r.what}</h2>
      <dl><dt>Repo</dt><dd>${r.repo}</dd><dt>Head</dt><dd>${r.head.slice(0, 12)}</dd>
        <dt>Gate failed</dt><dd>${r.failed.map(t => `${day(t)} ${hm(t)} UTC`).join(' · ')}</dd><dt>Passed</dt><dd>${day(r.passed)} ${hm(r.passed)} UTC · ${r.status}</dd></dl>
      <p class="why">The gate failed at this head, then passed at it with no new commit: a test that fails by chance. No store names the test.</p>${act}`;
    if (r.kind === 'brief') return html`${head}<h2>${r.subj}</h2>
      <dl><dt>Day</dt><dd>${r.day}</dd><dt>Series</dt><dd>${r.src}</dd><dt>Words</dt><dd>${r.words}</dd></dl>
      <h3>Body · first lines</h3><div class="mailbody">${r.lead || ''}</div>${act}`;
    if (r.kind === 'item') return html`${head}<h2><a href="${shell.itemHref(r.item)}">${r.what}</a></h2>
      <dl><dt>Status</dt><dd>${r.state}</dd><dt>Due</dt><dd>${r.due || html`<span class="ph">none</span>`}</dd>
        ${r.repo ? html`<dt>Repo</dt><dd>${r.repo}</dd>` : ''}${r.item_kind ? html`<dt>Kind</dt><dd>${r.item_kind}</dd>` : ''}<dt>Id</dt><dd>sd:${r.item}</dd></dl>${act}`;
    return html`${head}<h2>${r.what}</h2>
      <dl>${Object.entries(facts(r)).map(([k, v]) => html`<dt>${k}</dt><dd>${v}</dd>`)}<dt>Read</dt><dd><time class="rel" data-long datetime="${DOC.now}"></time></dd></dl>
      ${SRC[r.source]?.[2] ? html`<p><a href="${SRC[r.source][2]}">Open ${SRC[r.source][0]} in ${SRC[r.source][3]}</a></p>` : ''}${act}`;
  }
  // An item row's Details are the Tasks page's: drawn from the row its /api/tasks/<id> reading names, read again on
  // each select, so a read of Today shows the item as it is now. Until the reading answers they say it is being read.
  function pickItem(id, open) {
    pick(id, () => html``, open);
    TD.renderDetails();
    TD.redrawDet(Number(id));
  }
  function select(id, open) {
    if (isItem(id)) return pickItem(id, open);
    const r = ALL.get(id), o = OTHER.get(id) || QOTHER.get(id);
    if (!r) return o && pick(id, o.draw, open);
    pick(id, () => details(r), open);
    shell.suggest([`Why is ${r.what.split(' ')[0]} on this list?`, 'What else changed around this time?', 'Rank what I should do in the next hour']);
  }

  // The rows outside Needs you (sd:6714): the Queue view's live items and build runs and the day track's marks. A click selects
  // each as a Needs you row: its object for the shell's commands and how its Details draw. A job or brief mark whose row
  // left Needs you (snoozed, seen, or cleared by a later read) says so in Details.
  const markId = m => m.lane === 'landed' ? `landed:${m.item}` : m.lane === 'live' ? `live:${m.item}` : m.id;
  function others(doc) {
    const found = new Map(), runs = (doc.builders || {}).rows || [], T = doc.track || {}, live = new Map();
    ((doc.active || {}).rows || []).forEach(r => { if (!live.has(r.item)) live.set(r.item, []); live.get(r.item).push(r); });
    live.forEach((rows, item) => {
      const b = runs.find(x => x.items.some(it => it.item === item)), n = b && b.items.find(it => it.item === item).assignment;
      found.set(`live:${item}`, { obj: { type: 'live item', label: `#${item} ${rows[0].title}`, item, revision: rows[0].revision, itemKind: rows[0].item_kind, run: b && b.run, n },
        draw: () => liveDetails(item, rows, b, n) });
    });
    runs.forEach(b => found.set(`run:${b.run}`, { obj: { type: 'build run', label: `Build run ${b.run}` }, draw: () => runDetails(b) }));
    (T.landed || []).forEach(m => found.set(`landed:${m.item}`, { obj: { type: 'delivered item', label: m.what, item: m.item }, draw: () => landedDetails(m) }));
    [['jobs', 'Job', '/management', 'Jobs in Management'], ['briefs', 'Brief', '/briefs', 'Briefs']].forEach(([k, kind, href, where]) => (T[k] || [])
      .filter(m => !ALL.has(m.id)).forEach(m => found.set(m.id, { obj: { type: 'day mark', label: m.what }, draw: () => html`<p class="kind"><span class="g-unknown" aria-hidden="true">${GLYPH.unknown}</span> ${kind} · not listed</p>
        <h2>${m.what}</h2><dl><dt>At</dt><dd>${day(m.at)} ${hm(m.at)} UTC</dd></dl>
        <p class="why">This mark's row is no longer on Needs you: it was snoozed, marked seen, or a later read cleared it.</p><p><a href="${href}">Open ${where}</a></p>` })));
    return found;
  }
  function liveDetails(item, rows, b, n) {
    const sure = rows.every(r => r.verified), s = sure ? 'ok' : 'unknown', top = rows[0];
    return html`<p class="kind"><span class="g-${s}" aria-hidden="true">${GLYPH[s]}</span> Live · ${sure ? 'live' : 'a launch not confirmed'}</p>
      <h2><a href="${shell.itemHref(item)}">#${item} ${top.title}</a></h2>
      <dl><dt>Status</dt><dd>${top.status}</dd><dt>Lane</dt><dd>${top.lane || 'not in a lane'}</dd>
        ${b ? html`<dt>Builder</dt><dd><b>${b.stage}</b> · step ${b.step} · ${b.state}</dd><dt>ETA</dt><dd>${b.eta ? clock(b.eta) : 'not known'} · ${b.eta_why}</dd><dt>Assignment</dt><dd>#${String(n)}</dd>`
          : html`<dt>Builder</dt><dd>no build run holds it</dd>`}</dl>
      <h3>Sessions</h3><ul class="sessions">${rows.map(r => html`<li>${r.verified ? '' : html`<span class="g-unknown" title="not confirmed">${GLYPH.unknown} </span>`}${r.session}${r.elapsed ? html` · ${r.elapsed}` : ''}<span class="via">${r.via}</span></li>`)}</ul>
      <h3>Act</h3>${shell.commands.bar(`live:${item}`)}`;
  }
  function runDetails(b) {
    const s = BSTATE[b.state] || 'warning';
    return html`<p class="kind"><span class="g-${s}" aria-hidden="true">${GLYPH[s]}</span> ${b.teammate ? 'Teammate' : 'Builder'} run · ${b.state}</p>
      <h2>${b.repo} · ${b.stage}</h2>
      <dl><dt>Run</dt><dd>${b.run}</dd>${b.worktree ? html`<dt>Worktree</dt><dd>${b.worktree}</dd>` : ''}<dt>Stage</dt><dd>${b.stage} · step ${b.step}</dd><dt>Started</dt><dd>${clock(b.started)} · ${b.elapsed} ago</dd>
        <dt>ETA</dt><dd>${b.eta ? clock(b.eta) : 'not known'} · ${b.eta_why}</dd><dt>Model</dt><dd>${b.model || 'not recorded'}</dd><dt>Spent</dt><dd>${usd(b.usd)}</dd></dl>
      <h3>Items</h3><ul class="members">${b.items.map(it => html`<li><a href="${shell.itemHref(it.item)}">#${it.item} ${it.title}</a>${it.assignment ? html`<span class="via">assignment #${String(it.assignment)}</span>` : ''}</li>`)}</ul>
      <p><a href="#view=queue">Open the builds in Queue</a></p>`;
  }
  function landedDetails(m) {
    return html`<p class="kind"><span class="g-ok" aria-hidden="true">${GLYPH.ok}</span> Landed · ok</p>
      <h2><a href="${shell.itemHref(m.item)}">${m.what}</a></h2><dl><dt>Delivered</dt><dd>${day(m.at)} ${hm(m.at)} UTC</dd></dl>
      <h3>Act</h3>${shell.commands.bar(`landed:${m.item}`)}`;
  }

  // The lamps (sd:5921): each number counts the rows its filter shows; a source that was not read is unknown, never ok.
  // A snoozed dark row still names its lamp (sd:5954): the snooze hides the row, not the unread source.
  // What each lamp counts and what a press does (pack sd:7383): its title, then the lamp's own detail on a second line.
  const HELP = {
    jobs: 'Failed scheduled jobs. Press to list them in Needs you.',
    lane: 'Lane entries blocked until someone acts; merging, next and flaky heads (a gate that failed, then passed at one head) are counted under it. Press to list them in Needs you.',
    reports: 'Open job alerts and reports. Press to list them in Needs you.',
    pulls: 'Pull requests waiting on you. Press to list them in Needs you.',
    mail: 'Mail waiting for you. Press to list it in Needs you.',
    repos: 'Checkouts to tidy: ahead of origin, dirty or pinned. Press to list them in Needs you.',
    sessions: 'Work items with a live session; the small count is sessions. Press to open the Queue view.',
    gate: 'Gate slots running of the cap, and runs waiting for one; load5 is the machine load. Press to open the Queue view.',
  };
  const tip = (cell, key, detail) => { cell.title = detail ? `${HELP[key]}\n${detail}` : HELP[key]; };
  const darkOn = lamp => NEEDS.find(r => r.lamp === lamp && r.kind === 'dark') || SNOOZED.find(r => r.lamp === lamp && r.kind === 'dark');
  function lamps() {
    const L = DOC.lane || {}, gates = L.gates || {}, why = { ...DOC.sources, reports: DOC.problems_error, mail: DOC.mail_error }, unread = DOC.mail_unread || [];
    document.querySelectorAll('.cell[data-src]').forEach(cell => {
      const s = cell.dataset.src, rows = NEEDS.filter(r => r.lamp === s && r.kind !== 'dark'), val = cell.querySelector('.val');
      const dark = darkOn(s), seen = (DOC.observed || {})[s];
      if ((dark && !dark.partial) || why[s]) { cell.dataset.state = 'unknown'; tip(cell, s, dark?.detail || why[s]); put(val, html`<span class="ph">not read</span>`); return; }
      let note = seen ? `observed ${seen.at.slice(11, 16)} UTC${seen.stale ? ` · stale: ${seen.stale}` : ''}` : '';
      const st = rows.map(r => STATE[r.band]);
      cell.dataset.state = seen?.stale ? 'unknown' : st.includes('warning') ? 'warning' : st.includes('caution') ? 'caution' : st.length ? 'queued' : 'ok';
      const n = k => rows.filter(r => r.kind === k).length;
      // pack sd:7663: the flaky heads light the Lane lamp too, a lone row or a group's members, and count apart from blocked.
      const flaky = rs => rs.reduce((c, r) => c + (r.kind === 'flaky' ? 1 : r.id === 'group:flaky' ? r.members.length : 0), 0);
      // sd:2014: interrupted, unloaded and unknown jobs are rows too; the lamp counts the failed ones and names the rest.
      const failed = rows.filter(r => r.state === 'failed').length, other = rows.length - failed;
      put(val, s === 'jobs' ? html`<b>${failed}</b> failed${other ? html`<small>${other} other</small>` : ''}`
        : s === 'lane' ? html`<b>${n('lane')}</b> blocked<small>${L.merging ?? 0} merging · ${L.next ?? 0} next${flaky(rows) ? ` · ${flaky(rows)} flaky` : ''}${L.unread ? ` · ${L.unread} not read` : ''}</small>`
        : s === 'reports' ? html`<b>${rows.length}</b> open${DOC.problems_more ? html`<small>${DOC.problems_more}${DOC.problems_capped ? '+' : ''} more on Reports</small>` : ''}`
        : s === 'pulls' ? html`<b>${rows.length}</b> for you`
        : s === 'mail' ? html`<b>${rows.length}</b> waiting${DOC.mail_more || unread.length ? html`<small>${DOC.mail_more ? `${DOC.mail_more} more in Gmail` : ''}${DOC.mail_more && unread.length ? ' · ' : ''}${unread.length ? `${plural(unread.length, 'repo')} not read` : ''}</small>` : ''}`
        : html`<b>${rows.length}</b> to tidy<small>${n('ahead')} ahead · ${n('dirty')} dirty${n('pinned') ? ` · ${n('pinned')} pinned` : ''}</small>`);
      if (s === 'lane' && L.unread) note = L.unread_why;
      // A repo whose mail could not be read is unknown, never none: the lamp names each reason (sd:4191).
      if (s === 'mail' && unread.length) note = unread.map(u => `${u.repo}: mail not read: ${u.reason}`).join('\n');
      // A source read only in part (sd:5939) keeps its counts but reads unknown, never ok: its dark row says what is missing.
      if (dark) { cell.dataset.state = 'unknown'; note = note || dark.detail; }
      tip(cell, s, note);
    });
    const a = ACTIVE || {}, items = new Set((a.rows || []).map(r => r.item)), ses = $('lamp-sessions');
    const liveDark = darkOn('sessions');
    ses.dataset.state = a.error || liveDark ? 'unknown' : 'ok'; tip(ses, 'sessions', a.error || liveDark?.detail);
    put(ses.querySelector('.val'), a.error ? html`<span class="ph">not read</span>` : html`<b>${items.size}</b> live<small>${plural((a.rows || []).length, 'session')}</small>`);
    const gate = $('lamp-gate'), load = (L.load || {}).load5, off = DOC.sources.lane || gates.error;
    gate.dataset.state = off ? 'unknown' : gates.waiting ? 'caution' : 'ok';
    tip(gate, 'gate', off);
    put(gate.querySelector('.val'), off ? html`<span class="ph">not read</span>`
      : html`<b>${gates.running}/${gates.cap}</b> ${gates.waiting} waiting${load != null ? html`<small>load5 ${load}</small>` : ''}`);
  }

  // build (sd:2483): one object per row, for shell.read to put, and to retire once a read stops listing the row.
  function objectOf(r) {
    const pr = r.kind === 'pr' && r.id.match(/^pr:(.+)#(\d+):\d+$/);
    const job = r.kind === 'job' && r.id.match(/^job:(.+):[^:]+$/);
    return { id: r.id, type: r.group ? GROUP[r.id] || 'item group' : TYPE[r.kind] || r.kind, label: r.what,
             failed: r.kind === 'job' && !!r.retry, retry: r.retry, job: job && job[1], service: r.service,
             exit: r.exit, revision: r.revision, capability: r.capability, repo: pr ? pr[1] : r.repo, number: pr && +pr[2], seen: r.seen,
             item: r.item, itemKind: r.item_kind, state: r.state, url: r.url, fieldsRead: r.fields_read, until: r.until, login: r.login, claim: r.claim,
             repoPath: r.repo_path, laneRevision: r.lane_revision, head: r.expected_head, runnerMerge: r.runner_merge,
             members: r.members && r.members.map(m => ({ id: m.id, seen: m.seen, until: m.until, item: m.item })), group: r.id,
             band: r.band, detail: r.detail, source: r.source };
  }

  // The selected row, and the picked ones, marked again on the rows a redraw made.
  function mark(id) {
    $('rows').querySelectorAll('tr[data-id]').forEach(tr => { tr.setAttribute('aria-selected', tr.dataset.id === id); tr.toggleAttribute('data-picked', picked.includes(tr.dataset.id)); });
    ['queue', 'track-rows'].forEach(t => $(t).querySelectorAll('tr[data-id]').forEach(tr => tr.setAttribute('aria-selected', tr.dataset.id === id)));
  }
  const BAND_NAME = { broken: 'Broken', look: 'Look', queued: 'Queued' };
  // pack sd:7776: a pr-attention row's pull (now_screen's pr_url) opens in a new tab from an icon beside its actions.
  const prOpen = r => r.pr_url ? html`<a class="icon-btn" href="${r.pr_url}" target="_blank" rel="noopener noreferrer" aria-label="Open the pull request on GitHub" data-tip="Open the pull request on GitHub in a new tab">${I('arrow-up-right')}</a>` : '';
  function line(r, member) {
    const s = STATE[r.band], src = SRC[r.source];
    return html`<tr data-id="${r.id}" data-src="${r.lamp || ''}"${member ? html` class="member" data-in="${member}"` : r.group ? html` class="grp"` : ''}>
      <td class="g g-${s}" title="${s}">${GLYPH[s]}</td>
      <td class="what">${r.group ? html`<button type="button" class="disc" data-toggle="${r.id}" aria-expanded="${String(OPEN.has(r.id))}" aria-label="${OPEN.has(r.id) ? 'Close' : 'Open'} ${r.what}">${I('chevron-right')}</button>` : ''}<button type="button" class="pick">${r.what}</button></td>
      <td class="detail">${r.links /* sd:8013: each decision queue links its home */ ? r.links.map((l, i) => html`${i ? ' · ' : ''}<a href="${l.href}">${l.label} ${l.n}</a>`) : r.detail}</td>
      <td class="src">${src ? I(src[1]) : ''}${r.source}</td>
      <td class="num age">${age(r.at)}</td>
      <td class="act">${prOpen(r)}${shell.commands.rowActions(r.id, { icons: r.band === 'broken' || r.kind === 'report' })}</td></tr>`;
  }
  // Needs you: the rows the lamp, the intent and the text keep, a band head where the band changes, and each open group's
  // members under it. Nothing hides a row after the draw: a filter draws the list again.
  function needsTable() {
    const rows = NEEDS.filter(passes);
    put($('rows'), rows.length ? html`${rows.map((r, i) => {
      const head = !i || rows[i - 1].band !== r.band ? html`<tr class="band"><th colspan="6" scope="rowgroup">${BAND_NAME[r.band]} · ${rows.filter(x => x.band === r.band).length}</th></tr>` : '';
      if (!r.group || !OPEN.has(r.id)) return html`${head}${line(r)}`;
      const shown = FULL.has(r.id) ? r.members : r.members.slice(0, SHOWN), more = r.members.length - shown.length;
      return html`${head}${line(r)}${shown.map(m => line(m, r.id))}${more ? html`<tr class="more"><td></td><td colspan="5"><button type="button" class="linkbtn" data-more="${r.id}">${more} more</button></td></tr>` : ''}`;
    })}`
      : html`<tr class="empty"><td colspan="6">${NEEDS.length ? 'Nothing matches the filter. Esc clears it.' : 'Nothing needs you: every source was read and raised no row.'}</td></tr>`);
    const f = $('filtered'), n = rows.length;
    f.hidden = !(srcFilter || intent || query);
    f.textContent = n ? `${n} of ${NEEDS.length} shown${srcFilter ? ' · lamp ' + srcFilter : ''}${intent ? ' · ' + intent : ''}${query ? ` · “${query}”` : ''}. Esc clears.`
      : `Nothing matches${query ? ` “${query}”` : ''}. Press → to capture it as a task instead.`;
    mark(row());
  }
  function intents() {
    // pack sd:7084: no All chip; a second press on the picked intent clears it.
    put($('intents'), html`${INTENTS.map(([id, label, test]) => {
      const n = NEEDS.filter(r => test(r) || (r.members || []).some(test)).length;
      return html`<button class="chip" type="button" data-intent="${id}" aria-pressed="${String(intent === id)}"${n ? '' : html` disabled`}>${label} <b>${n}</b></button>`; })}`);
  }
  function render() {
    const count = s => NEEDS.filter(r => STATE[r.band] === s).length;
    put($('tally'), html`<span class="g-warning">■ ${count('warning')} broken</span><span class="g-caution">▲ ${count('caution')} look</span><span class="g-queued">◌ ${count('queued')} queued</span>${SNOOZED.length ? html`<span>${SNOOZED.length} snoozed</span>` : ''}`);
    const d = new Date(DOC.now), live = new Set(((ACTIVE || {}).rows || []).map(r => r.item)).size;
    put($('subhead'), html`${d.toLocaleDateString([], { weekday: 'long', day: 'numeric', month: 'long' })} · <b>${count('warning')} broken</b>, ${count('caution')} to look at · ${plural(live, 'item')} live · read <time class="rel" datetime="${DOC.now}"></time>`);
    attend();
    lamps();
    intents();
    needsTable();
    // A part of the list that could not be read is said under it, never left out quietly.
    const unread = [['Work', DOC.work_error], ['Briefs', DOC.briefs_error]].filter(([, why]) => why);
    $('unread').hidden = !unread.length;
    $('unread').textContent = unread.map(([k, why]) => `${k} could not be read: ${why}. Read again tries again.`).join(' ');
    queueView();
    if (QV.seen) loadQueue();
    digest();
    dayTrack();
    if (AV.seen) loadActivity();
    spendLine();
    window.snooze.draw($('snoozed'), SNOOZED);
  }

  // The live items' sources (sd:3466), named when one was not read; a build run's state in its Details (sd:6003).
  const SOURCE = { fleet: 'worktrees and processes', lane: 'lane entries', launch: 'Fix notes', herdr: 'herdr agents' };
  const clock = iso => new Date(iso).toLocaleString([], { weekday: 'short', hour: '2-digit', minute: '2-digit' });
  const BSTATE = { running: 'ok', stalled: 'caution' };

  // Queue (pack sd:7234): Queue's Overview, Today's Queues and In motion in one view. One parent row per repo; a click opens
  // its child rows under it and closes the row that was open, and a click on the open row closes it. The children, in order:
  // working (the live items of `active`, the build runs of /api/queue/builds, a builder's gate in the lane and the runner's
  // assignments no run holds), merging, next and blocked (the lane entries of /api/queue), suggested (what sd-next offers next,
  // from `queues.suggested`, at most nine, pack sd:7222, sd:7314, sd:7869 and sd:8008), open (All items: its other open tasks, from
  // /api/tasks) and ended (ended build runs, and the cancelled
  // and landed lane entries). Each row's marker is its state glyph and its type in words; the parent's glyph is its worst
  // child's. Suggested is on by default (pack sd:7314), All items and Ended builds are off; on, a repo with only those rows
  // shows too. ?show= keeps the choice: no key is the default, and an empty one is every filter off.
  // The lane's writes are Queue's (sd:2585): up, down, top, hold, release and cancel post /api/queue/move with the lane's
  // revision; the server runs sd-ship lane move|hold|release|cancel|retry and refuses a stale revision. Cancel asks first
  // (sd:3012). A cancelled row offers Re-queue (sd:3398) when the installed pack's retry takes a cancelled entry
  // (QV.lanes.requeue, pack sd:3389): it posts `requeue` with the head the row showed, and `lane retry --expected-head`
  // refuses a moved branch, a newer entry or a merged head. A running row with a claim offers Release (sd:3337): it asks
  // first, naming the holder, claim age and what the pack does at that step, then posts `reclaim` with the claim the row
  // showed, so a claim that moved on releases nothing. A blocked row offers Retry and, on a runner_merge=manual repo,
  // Approve, only when the installed pack's retry takes --expected-head (QV.lanes.retry); Approve asks first: it grants
  // the merge, which nothing takes back. A build run's Cancel asks first and posts the assignment cancel route; an ended
  // blocked or cancelled bundle's Retry and Send to builder post /api/queue/builds/retry (pack sd:6020). The view reads its
  // three documents when it first shows and on each Today read after; a source that was not read is said under the table.
  const QTYPES = ['working', 'merging', 'next', 'blocked', 'suggested', 'open', 'ended'], QSHOW = ['suggested', 'open', 'ended'], NEXT = 9;
  const BOXES = ['merging', 'review', 'working', 'next', 'blocked', 'suggested']; // a repo row's boxes, left to right (pack sd:7470, sd:8083, sd:8137)
  const QV = { lanes: null, lanesErr: '', built: null, builtErr: '', runner: null, runnerErr: '', tasks: null, tasksErr: '', seen: false, busy: false };
  const QOTHER = new Map(); // the lane entries, selectable as Today's other rows are
  const qShow = new Set();
  let qOpen = null, qSaid = null; // the one open repo; the last write's result
  let qTile = null; // pack sd:8138: the open repo's pressed box, { repo, box }; it holds only while that repo is open
  // ponytail: the repo's folder name keys its row; two checkouts of one name share a row.
  const repoKey = p => p ? String(p).replace(/\/+$/, '').split('/').pop() : '';
  const HM = new Intl.DateTimeFormat(undefined, { hour: '2-digit', minute: '2-digit', hourCycle: 'h23' });
  const at = s => s ? HM.format(new Date(s)) : 'not recorded';
  const dur = s => s == null ? 'not recorded' : s < 3600 ? `${Math.floor(s / 60)}m ${s % 60}s` : `${Math.floor(s / 3600)}h ${Math.floor(s % 3600 / 60)}m`;
  const WORD = { ok: 'running', caution: 'look', warning: 'needs you', queued: 'waiting', unknown: 'not known' };
  const marker = (s, type) => html`<td class="mk"><span class="g-${s}" aria-hidden="true">${GLYPH[s]}</span><span class="sr">${WORD[s]} · </span>${type}</td>`;
  const sb = (act, r, label, aria, extra = '') => html`<button class="btn quiet sm" type="button" data-act="${act}" data-item="${String(r.item)}" data-repo="${r.lane}" aria-label="${aria}"${extra}${QV.busy ? html` disabled` : ''}>${label}</button>`;
  function controls(r, last) {
    const off = on => on ? html` disabled` : '';
    return html`<div class="ctl" role="group" aria-label="Order of sd:${String(r.item)}">${sb('up', r, I('arrow-up'), `Move up sd:${r.item}`, off(r.position === 1))}${sb('down', r, I('arrow-down'), `Move down sd:${r.item}`, off(r.position === last))}${sb('top', r, 'Top', `Move sd:${r.item} to the top`, off(r.position === 1))}${sb(r.held ? 'release' : 'hold', r, r.held ? 'Release' : 'Hold', `${r.held ? 'Release' : 'Hold'} sd:${r.item}`)}${sb('cancel', r, 'Cancel…', `Cancel sd:${r.item} in this lane`)}</div>`;
  }
  const again = (r, manual) => !QV.lanes.retry ? '' : html`<div class="ctl" role="group" aria-label="Unblock sd:${String(r.item)}">${sb('retry', r, 'Retry', `Retry sd:${r.item}: queue it again at the same head`, html` data-head="${r.expected_head}"`)}${manual ? sb('approve', r, 'Approve…', `Approve sd:${r.item}: queue it again with the merge granted`, html` data-head="${r.expected_head}"`) : ''}</div>`;
  const back = r => !QV.lanes.requeue ? '' : html`<div class="ctl" role="group" aria-label="Cancel of sd:${String(r.item)}">${sb('requeue', r, 'Re-queue', `Re-queue sd:${r.item} at head ${r.head}`, html` data-head="${r.expected_head}"`)}</div>`;
  const free = r => !r.claim ? '' : html`<div class="ctl" role="group" aria-label="Claim of sd:${String(r.item)}">${sb('reclaim', r, 'Release…', `Release sd:${r.item} from ${r.host || 'its holder'}`, html` data-claim="${r.claim}"`)}</div>`;
  // Each lane state's glyph, type and fact line. A cancelled or landed entry is an ended row.
  const ENTRY = {
    merging: r => [r.lease_passed ? 'caution' : 'ok', 'merging', html`<b>${r.step || 'step not recorded'}</b> on <b>${r.host || 'host not recorded'}</b> · claimed ${r.claim_age == null ? 'at a time not recorded' : `${dur(r.claim_age)} ago`} · ${r.lease_until ? `lease ${r.lease_passed ? 'passed' : 'to'} ${at(r.lease_until)}` : 'lease not recorded'} · head ${r.head || 'not recorded'}`],
    next: r => [r.held ? 'caution' : 'queued', 'next', html`${r.held ? 'held' : 'position'} ${r.position} · gate <b>${r.gate}</b>${r.gate_summary ? ` (${r.gate_summary})` : ''} · head ${r.head || 'not recorded'}${r.held ? ' · skipped until released' : ''}`],
    building: r => [{ running: 'ok', pass: 'ok', fail: 'warning' }[r.gate] || 'unknown', 'working', html`gate <b>${r.gate}</b>${r.summary ? ` · ${r.summary}` : ''}${r.head ? ` · head ${r.head}` : ''} · log changed ${at(new Date(r.changed * 1000).toISOString())}`],
    blocked: r => ['warning', 'blocked', html`<b>${r.who} acts</b> · ${r.status}${r.step ? ` at ${r.step}` : ''} · ${r.reason} · head ${r.head || 'not recorded'}`],
    cancelled: r => ['queued', 'ended', html`cancelled at ${at(r.finished)} · head ${r.head || 'not recorded'}${QV.lanes.requeue ? '' : ` · Re-queue off: ${QV.lanes.requeue_off || 'not known whether the pack takes it'}`}`],
    landed: r => ['ok', 'ended', html`landed · ${r.pr ? `PR #${r.pr}` : 'PR not named'} · merge ${r.commit || 'not recorded'} · ${at(r.finished)}`],
  };
  const entryId = (l, r) => `entry:${l.path}:${r.item}`;
  // The PNGs the lane drew of the row's changed dashboard pages (sd:6728), or why it drew none.
  const shots = r => r.shots ? html` · screenshots ${r.shots.map((f, i) => html`${i ? ', ' : ''}<a href="${f.url}" target="_blank" rel="noopener">${f.name}</a>`)}`
    : r.shots_warning ? html` · screenshots not drawn: ${r.shots_warning}` : '';
  // `p`, a prepared entry's pull-request wait (pack sd:7859): in a manual-merge repo the lane stops prepared and the PR
  // waits on a person's review, so the row leads with that wait and keeps its Retry and Approve.
  function entryRow(l, r, p) {
    const [s0, type, facts] = ENTRY[r.state](r), last = l.rows.filter(x => x.state === 'next').length, e = { ...r, lane: l.path };
    const s = p ? (p.on === 'you' ? 'warning' : 'queued') : s0;
    const ctl = r.state === 'next' ? controls(e, last) : r.state === 'blocked' ? again(e, l.runner_merge === 'manual') : r.state === 'cancelled' ? back(e) : r.state === 'merging' ? free(e) : '';
    const id = r.state === 'building' ? null : entryId(l, r);
    return [type, s, html`<tr class="child" data-type="${type}"${id ? html` data-id="${id}" aria-selected="${String(row() === id)}"` : ''}${r.held ? html` data-held=""` : ''}${p ? html` data-pr="${String(p.item)}"` : ''}>${marker(s, type)}
      <td class="what">${r.state === 'building' ? html`<code>${r.builder}</code>` : html`<a href="${shell.itemHref(r.item)}">#${r.item} ${r.title}</a>`}</td>
      <td class="detail">${p ? html`${prFacts(p)} · ` : ''}${facts}${shots(r)}</td><td class="num">${r.state === 'next' ? `#${r.position}` : p ? dur(p.waited) : ''}</td>
      <td class="act">${ctl}${p ? prWaitLink(p) : ''}${id ? shell.commands.rowActions(id) : ''}</td></tr>`, p && p.on];
  }
  // A build run's state (pack sd:6095): working; waiting on me (held, or an open question asks); stalled (the log has not
  // grown for 15 min); lost (no supervisor beat for 3 min, which the dispatcher's next reap settles).
  const RUNSTATE = { working: 'ok', waiting: 'warning', stalled: 'caution', lost: 'unknown' };
  const names = r => r.items.length ? html`${r.items.map((m, i) => html`${i ? '; ' : ''}<a href="${shell.itemHref(m.item)}">#${m.item} ${m.title}</a>`)}` : 'no items recorded';
  const bb = (act, r, label, aria, off) => html`<button class="btn quiet sm" type="button" data-build="${act}" data-run="${r.run}" aria-label="${aria}"${off ? html` disabled title="${off}"` : QV.busy ? html` disabled` : ''}>${label}</button>`;
  // Open PR (pack sd:6020 ruling #31553): the pull request `sd-ship` opened for the row's branch; none shows nothing.
  const prLink = r => r.pull ? html`<a class="btn quiet sm" href="${r.pull}" target="_blank" rel="noopener noreferrer" aria-label="Open the pull request of run ${r.run} on GitHub">Open PR</a>` : '';
  // A satellite's run (pack sd:7933) logs on that host: the row names the host and the log path, and copies no tail here.
  const hostFacts = r => r.host ? html` · on ${r.host} · log <code>${r.log || 'not recorded'}</code>` : '';
  const logBtn = r => r.log && !r.host ? html`<button class="btn quiet sm" type="button" data-copy="${`tail -f ${shell.shq(r.log)}`}" aria-label="Copy the command that follows the log of run ${r.run}">Log</button>` : '';
  const asgBtn = n => html`<button class="btn quiet sm" type="button" data-copy="sd assignments get ${String(n)}" aria-label="Copy the command that shows assignment ${String(n)}">Show assignment</button>`;
  // A lead's in-session builder (pack sd:7221): its worktree, branch, budget's end and last beat; its lead ends it.
  const teamFacts = r => html`<b>${r.state}</b> · teammate · worktree <code>${r.worktree}</code> · branch ${r.branch || 'not recorded'} · due ${at(r.due)} · ${r.beat == null ? 'no beat yet' : `beat ${dur(r.beat)} ago`}`;
  function runRow(r) {
    const s = RUNSTATE[r.state] || 'unknown', id = `run:${r.run}`;
    return ['working', s, html`<tr class="child" data-type="working" data-run="${r.run}" data-id="${id}" aria-selected="${String(row() === id)}">${marker(s, 'working')}
      <td class="what">${names(r)}</td>
      <td class="detail">${r.teammate ? teamFacts(r) : html`<b>${r.state}</b> · build ${r.run} · ${r.model || 'model not recorded'} · step <b>${r.step}</b> · ${r.quiet == null ? 'no output yet' : `output ${dur(r.quiet)} ago`} · ${usd(r.usd || 0)} so far`}${hostFacts(r)}${r.why ? ` · ${r.why}` : ''}</td>
      <td class="num">${dur(r.elapsed)}</td>
      <td class="act"><div class="ctl" role="group" aria-label="Run ${r.run}">${bb('cancel', r, 'Cancel…', `Cancel build run ${r.run}`, r.cancel.allowed ? '' : r.cancel.reason || 'cancel is off')}${prLink(r)}${logBtn(r)}</div></td></tr>`];
  }
  // A blocked or cancelled bundle keeps its worktree: Retry queues it again at once; Send to builder asks for a finding,
  // notes it on each item and queues it again, so the next builder resumes that worktree with it.
  function endedRun(r) {
    const s = r.outcome === 'done' ? 'ok' : r.retry.bundle === 'blocked' ? 'warning' : 'queued', off = r.retry.allowed ? '' : r.retry.reason;
    return ['ended', s, html`<tr class="child" data-type="ended" data-run="${r.run}">${marker(s, 'ended')}
      <td class="what">${names(r)}</td>
      <td class="detail"><b>${r.retry.bundle === 'blocked' ? 'blocked' : r.outcome || 'ended'}</b> · build ${r.run} at ${r.step} · ended ${at(r.ended)} · ${usd(r.usd || 0)}${hostFacts(r)}${r.detail ? ` · ${r.detail}` : ''}</td>
      <td class="num"></td>
      <td class="act"><div class="ctl" role="group" aria-label="Run ${r.run}">${['blocked', 'cancelled'].includes(r.retry.bundle) ? html`${bb('retry', r, 'Retry', `Retry build run ${r.run}: queue its items again`, off)}${bb('send', r, 'Send to builder…', `Send a finding to the builder of run ${r.run}`, off)}` : ''}${asgBtn(r.assignment)}${prLink(r)}${logBtn(r)}</div></td></tr>`];
  }
  // Runner assignments (pack sd:7115, sd:7217): the queued and running ones no open build run holds.
  // A queued build of another host's lane whose dispatcher beats that it claims nothing names that reason (pack sd:7933).
  const waitOn = a => ((QV.built || {}).unclaimed || {})[String(a.id)] || '';
  const asgRow = a => ['working', a.status === 'running' ? 'ok' : 'queued', html`<tr class="child" data-type="working">${marker(a.status === 'running' ? 'ok' : 'queued', 'working')}
    <td class="what">${a.item ? html`<a href="${shell.itemHref(a.item)}">#${a.item} ${a.title || a.role}</a>` : a.title || a.role}</td>
    <td class="detail"><b>${a.status}</b> · assignment ${String(a.id)} · ${a.role} · since ${at(a.started || a.queued_at)}${waitOn(a) ? ` · ${waitOn(a)}` : ''}</td><td class="num"></td>
    <td class="act"><div class="ctl" role="group" aria-label="Assignment ${String(a.id)}">${asgBtn(a.id)}</div></td></tr>`];
  // A live item (In motion's sources: worktrees and processes, herdr agents, Fix notes, lane-less sessions): its sessions in
  // the row, Open item, Show assignment and Fix in its actions, and its Details on a click (sd:6714).
  function liveRow(item, rows) {
    const sure = rows.every(r => r.verified), s = sure ? 'ok' : 'unknown', top = rows[0], id = `live:${item}`;
    return ['working', s, html`<tr class="child" data-type="working" data-item="${item}" data-id="${id}" aria-selected="${String(row() === id)}">${marker(s, 'working')}
      <td class="what"><a href="${shell.itemHref(item)}">#${item} ${top.title}</a></td>
      <td class="detail"><ul class="sessions">${rows.map(r => html`<li>${r.verified ? '' : html`<span class="g-unknown" title="not confirmed">${GLYPH.unknown} </span>`}${r.session}<span class="via">${r.via}</span></li>`)}</ul></td>
      <td class="num">${top.elapsed || 'not known'}</td><td class="act">${shell.commands.rowActions(id)}</td></tr>`];
  }
  // pack sd:7859: an item whose pull request waits on review (/api/queue's `prs`) is blocked by its PR, not working: on
  // you (changes requested, failing checks, a comment to answer) needs you; on reviewers waits. It counts in the blocked box.
  // sd:8116: a draft says so, as does a wait that rests on GitHub's newest event; a stale one, read over a day ago,
  // waits on no one until the next read.
  const prTags = p => `${p.draft ? ' · draft' : ''}${p.approximate ? ' · approx.' : ''}`;
  const prFacts = p => p.stale ? html`<b>PR not read for a day</b> · last wait since ${at(p.since)}${prTags(p)}`
    : html`<b>blocked: PR</b> · on ${p.on} · waited ${dur(p.waited)} since ${at(p.since)}${prTags(p)}`;
  const prWaitLink = p => html`<div class="ctl" role="group" aria-label="Pull request of sd:${String(p.item)}"><a class="btn quiet sm" href="${p.url}" target="_blank" rel="noopener noreferrer" aria-label="Open the pull request of sd:${p.item} on GitHub">Open PR</a></div>`;
  // sd:8142: a PR no item links (`item` null) is keyed and linked by its URL, named `owner/repo#N`.
  const prRow = p => { const s = p.stale ? 'unknown' : p.on === 'you' ? 'warning' : 'queued';
    return ['blocked', s, html`<tr class="child" data-type="blocked" data-pr="${p.item == null ? p.url : String(p.item)}">${marker(s, 'blocked')}
    <td class="what">${p.item == null ? html`<a href="${p.url}" target="_blank" rel="noopener noreferrer">${p.repo}#${p.url.split('/').pop()} ${p.title}</a>`
      : html`<a href="${shell.itemHref(p.item)}">#${p.item} ${p.title}</a>`}</td>
    <td class="detail">${prFacts(p)}</td><td class="num">${p.stale ? '' : dur(p.waited)}</td>
    <td class="act">${p.item == null ? '' : prWaitLink(p)}</td></tr>`, p.on]; };
  // A suggested row (pack sd:7271) is a task: a click shows the Tasks page's Details, and its act is Build on a ready one,
  // else its next status, as on Tasks. Its detail says why it is suggested (pack sd:7314): ready, due today or before
  // (pack sd:8238), planned for its due date, or unscheduled, with its priority; a row not ready shows its status too.
  const P = r => r.priority == null ? '' : `P${r.priority}`;
  const WHY = { ready: r => ['ready', P(r)], due: r => [`due ${r.due}`, P(r), r.status], planned: r => [`planned for ${r.due}`, P(r), r.status], unscheduled: r => [`unscheduled ${P(r)}`.trim(), r.status] };
  const suggestRow = r => ['suggested', 'queued', html`<tr class="child" data-type="suggested" data-item="${r.id}" data-id="${String(r.id)}" aria-selected="${String(row() === String(r.id))}">${marker('queued', 'suggested')}
    <td class="what"><a href="${shell.itemHref(r.id)}">#${r.id} ${r.title}</a></td>
    <td class="detail">${(WHY[r.why] || WHY.ready)(r).filter(Boolean).join(' · ')}</td><td class="num">${r.since ? age(r.since) : ''}</td><td class="act">${shell.commands.rowActions(String(r.id))}</td></tr>`];
  // What sd-next offers next (pack sd:7314): the read's `queues.suggested`, ranked by sd_db.today.suggested (ready, due,
  // planned, then unscheduled), less the items live now, at most NEXT per repo, in that order. An item with no repo is
  // no row (pack sd:7731): Suggested draws no Unassociated group; Tasks still lists it.
  function suggested(doc) {
    const live = new Set(((doc.active || {}).rows || []).map(r => r.item)), counted = {}, found = new Map();
    ((doc.queues || {}).suggested || []).filter(r => r.repo && !live.has(r.id)).forEach(r => {
      const k = repoKey(r.repo); counted[k] = (counted[k] || 0) + 1; if (counted[k] <= NEXT) found.set(String(r.id), r); });
    return found;
  }
  // All items (#40961): an open task of a repo, as Tasks reads it, that is no suggested row while Suggested shows (pack
  // sd:7251), so All items alone keeps a repo's first suggested tasks. Its act is Tasks' list row's.
  const OPENS = { blocked: 'caution', in_progress: 'ok' }; // a blocked task is a look, not a lane's needs-you
  const openRow = t => ['open', OPENS[t.status] || 'queued', html`<tr class="child" data-type="open" data-item="${t.key}" data-id="${t.key}" aria-selected="${String(row() === t.key)}">${marker(OPENS[t.status] || 'queued', 'open')}
    <td class="what"><a href="${shell.itemHref(t.id)}">#${t.id} ${t.title}</a></td>
    <td class="detail">${String(t.status).replace(/_/g, ' ')}${t.p ? ` · P${t.p}` : ''}</td><td class="num">${t.since ? age(t.since) : ''}</td><td class="act">${shell.commands.rowActions(t.key)}</td></tr>`];
  // pack sd:7699: an idea kind (/api/tasks' `ideas`, reads.IDEA_KINDS) is no Queue row; ideas live on Writing.
  // pack sd:8127: `all` gives them while All items is off too, so its tile counts them; its rows stay hidden.
  function openItems(all) {
    TASKS = new Map((QV.tasks ? QV.tasks.rows.map(TD.shape) : []).map(t => [t.key, t]));
    const ideas = new Set((QV.tasks && QV.tasks.ideas) || []);
    return new Map([...TASKS].filter(([k, t]) => (all || qShow.has('open')) && t.status !== 'done' && !t.parked && t.repo_path && !ideas.has(t.kind)
      && !(qShow.has('suggested') && SUGG.has(k))));
  }
  // A task's read row (SROW) holds while it is still an item row at the revision its /api/tasks row has (pack sd:7251): a
  // row read at another revision is dropped, so taskOf acts on the fresh row, not the status it had when it was selected.
  const freshRows = () => [...SROW].filter(([k, t]) => !isItem(k) || (listedTask(k) && listedTask(k).revision !== t.revision)).forEach(([k]) => SROW.delete(k));
  // Each repo's children, from the four reads, each with the rows the filters show; queueView draws a repo that shows one.
  function qGroups() {
    const G = new Map(), add = (repo, [type, s, tr, pr]) => {
      const k = repoKey(repo);
      if (!G.has(k)) G.set(k, { key: k, host: '', rows: [] });
      G.get(k).rows.push({ type, s, tr, pr });
    };
    const live = new Map(), prs = ((QV.lanes || {}).prs || {}).rows || [], waits = new Set(prs.filter(p => p.item != null).map(p => Number(p.item)));
    // pack sd:7859: an item waiting on its PR is its PR row alone: no live-session, suggested or open row beside it.
    ((ACTIVE || {}).rows || []).filter(r => !waits.has(Number(r.item))).forEach(r => { if (!live.has(r.item)) live.set(r.item, []); live.get(r.item).push(r); });
    live.forEach((rows, item) => add(rows[0].repo, liveRow(item, rows)));
    const B = QV.built && !QV.built.error ? QV.built : { runs: [], recent: [] }, held = new Set();
    B.runs.forEach(r => { r.items.forEach(m => held.add(m.assignment)); add(r.repo, runRow(r)); });
    ((QV.runner || {}).live || []).filter(a => !held.has(a.id)).forEach(a => add(a.repo, asgRow(a)));
    // A prepared entry and its item's PR wait are one row (pack sd:7859): the lane's, leading with the wait.
    const waitOf = new Map(prs.map(p => [Number(p.item), p])), drawn = new Set();
    (QV.lanes ? QV.lanes.lanes : []).forEach(l => { l.rows.forEach(r => {
      const p = r.state === 'blocked' && r.status === 'prepared' ? waitOf.get(Number(r.item)) : undefined;
      if (p) drawn.add(p);
      add(l.path, entryRow(l, r, p)); });
      if (G.has(repoKey(l.path))) G.get(repoKey(l.path)).host = l.host; });
    prs.filter(p => !drawn.has(p)).forEach(p => add(p.repo, prRow(p)));
    SUGG.forEach(r => { if (!waits.has(Number(r.id))) add(r.repo, suggestRow(r)); });
    OPENI.forEach(t => { if (!waits.has(Number(t.id))) add(t.repo_path, openRow(t)); });
    B.recent.forEach(r => add(r.repo, endedRun(r)));
    const RANK = { warning: 0, caution: 1, unknown: 2, ok: 3, queued: 4 };
    // pack sd:7679: the busiest repo first, box by box in the boxes' order, each most first; then the name. Unassociated last.
    const busy = (a, b) => BOXES.reduce((d, t) => d || b.n[t] - a.n[t], 0);
    return [...G.values()].map(g => {
      const shown = g.rows.filter(c => !QSHOW.includes(c.type) || qShow.has(c.type)).sort((a, b) => QTYPES.indexOf(a.type) - QTYPES.indexOf(b.type));
      // pack sd:8083: a PR wait counts in review, not blocked, so no row counts twice.
      const n = Object.fromEntries(BOXES.map(t => [t, g.rows.filter(inBox(t)).length]));
      return { ...g, shown, n, s: shown.reduce((w, c) => RANK[c.s] < RANK[w] ? c.s : w, 'queued') };
    }).sort((a, b) => (a.key === '') - (b.key === '') || busy(a, b) || a.key.localeCompare(b.key));
  }
  // A box's rows: review holds the PR waits, each other box its type less those (pack sd:8083).
  // sd:8116: a stale PR row (unknown, no one to wait on) shows under its repo and counts in no box.
  const stalePr = c => c.type === 'blocked' && c.s === 'unknown';
  const inBox = t => t === 'review' ? c => c.pr : c => c.type === t && !c.pr && !stalePr(c);
  const tally = rows => QTYPES.map(t => [t, rows.filter(c => c.type === t && !stalePr(c)).length]).filter(([, n]) => n);
  // pack sd:8005: the page's PR waits, split by who they wait on; '' when there is none. Each repo's are its review box.
  const prTally = rows => { const you = rows.filter(c => c.pr === 'you').length, them = rows.filter(c => c.pr && c.pr !== 'you').length;
    return you + them ? `${you + them} PR review: ${you} on you, ${them} on reviewers` : ''; };
  // A repo row's six boxes, left to right: merging, review, working, next, blocked and suggested (pack sd:7272, sd:7314,
  // sd:8083, sd:8137), counted from all the repo's rows, so a filter hides no count. A box is lit only when its count is not zero; blocked lights the warning lamp, review the caution lamp when
  // a PR waits on you; the label says all six in words. A lane that was not read counts nothing: the cell says so, not 0.
  // pack sd:8138: each box is a toggle button that shows only its rows under the repo; a zero box is disabled.
  function pips(g) {
    const pip = (box, n, on, means) => html`<button type="button" class="tile" data-repo="${g.key}" data-tile="${box}" aria-pressed="${String(!!qTile && qTile.repo === g.key && qTile.box === box)}" aria-label="${`Only the ${n} ${box} rows`}"${n ? '' : html` disabled`}><span class="pip${n ? on : ''}" title="${`${n} ${means}`}">${String(n)}</span></button>`;
    if (g.key && (!QV.lanes || QV.lanes.problems.some(p => repoKey(p.repo) === g.key)))
      return html`<td class="detail"><span class="g-unknown" aria-hidden="true">${GLYPH.unknown}</span> lane not read</td>`;
    // pack sd:7470: merging is the leftmost box; pack sd:8137: review follows it.
    const { working: w, merging: m, next: n, blocked: b, review: v, suggested: s } = g.n, you = g.rows.filter(c => c.pr === 'you').length;
    // Each box says its count and what it counts, in its title (pack sd:7383); the label above says all six at once.
    return html`<td class="detail pipe" aria-label="${m} merging, ${v} review, ${w} working, ${n} next, ${b} blocked, ${s} suggested">${pip('merging', m, ' on', 'merging: claimed by the lane runner, being merged')}${pip('review', v, you ? ' caution' : ' on', `review: pull requests waiting on a review, ${you} on you, ${v - you} on reviewers`)}${pip('working', w, ' on', 'working: live sessions, build runs and lane gates now running')}${pip('next', n, ' on', 'next: queued in the lane, not started')}${pip('blocked', b, ' warn', 'blocked: stopped in the lane until someone acts')}${pip('suggested', s, ' on', `suggested: up to ${NEXT} items to do next: ready, due today or overdue, planned or unscheduled`)}</td>`;
  }
  function queueView() {
    const unread = [];
    if (!QV.seen) return;
    if (!DOC) { put($('queue'), html`<tr class="empty"><td colspan="5">The queue could not be read: ${failed}. Read again tries again.</td></tr>`); return; }
    if (!QV.lanes) unread.push(`Lanes not read: ${QV.lanesErr || 'no answer yet'}`);
    else QV.lanes.problems.forEach(p => unread.push(`${p.repo}: lane not read. ${p.error}`));
    if (QV.lanes && QV.lanes.prs && QV.lanes.prs.error) unread.push(QV.lanes.prs.error);
    if (!QV.built || QV.built.error) unread.push(`Build runs not read: ${QV.built ? QV.built.error : QV.builtErr || 'no answer yet'}`);
    if (!QV.runner) unread.push(`Runner assignments not read: ${QV.runnerErr || 'no answer yet'}`);
    Object.entries((ACTIVE || {}).sources || {}).filter(([, why]) => why).forEach(([k, why]) => unread.push(`${SOURCE[k] || k} not read: ${why}`));
    if (ACTIVE && ACTIVE.error) unread.push(`Sessions not read: ${ACTIVE.error}`);
    if (qShow.has('suggested') && (DOC.queues || {}).error) unread.push(`Suggested items not read: ${DOC.queues.error}`);
    if (qShow.has('open') && !QV.tasks) unread.push(`Open items not read: ${QV.tasksErr || 'no answer yet'}`);
    else if (qShow.has('suggested') && !QV.tasks) unread.push(`Suggested items' tasks not read: ${QV.tasksErr || 'no answer yet'}`);
    // pack sd:7251: a filter counts its rows in every repo, before a repo with no row shown is left out.
    const all = qGroups(), every = all.flatMap(g => g.rows), groups = all.filter(g => g.shown.length), active = every.filter(c => !QSHOW.includes(c.type)).length;
    if (!groups.some(g => g.key === qOpen)) qOpen = null;
    if (qTile && qTile.repo !== qOpen) qTile = null;
    // pack sd:8127: All items counts its open items with the filter off as on, less those waiting on a PR; a failed read counts nothing.
    const waits = new Set((((QV.lanes || {}).prs || {}).rows || []).map(p => Number(p.item)));
    const opens = QV.tasks ? [...openItems(true).values()].filter(t => !waits.has(Number(t.id))).length : null;
    const n = v => v !== 'open' ? every.filter(c => c.type === v).length : opens;
    put($('queue-filters'), shell.list.fseg('qshow', 'Show', [['suggested', 'Suggested'], ['open', 'All items'], ['ended', 'Ended builds']].map(([v, label]) =>
      ({ v, pressed: qShow.has(v), label: n(v) === null ? html`${label}` : html`${label} <span class="n">${String(n(v))}</span>` }))));
    const prs = prTally(every);
    put($('queue-tally'), html`${tally(every.filter(c => !QSHOW.includes(c.type))).map(([t, n]) => html`<span>${n} ${t}</span>`)}${prs ? html`<span>${prs}</span>` : ''}`);
    // Auto-pickup (pack sd:6097): its state, and On or Off, bound to the end this read saw.
    const pk = QV.built && QV.built.pickup;
    put($('queue-pickup'), !pk ? html`` : pk.error ? html`Auto-pickup not read: ${pk.error}`
      : pk.on ? html`Auto-pickup is on until ${at(pk.until)}: each tick queues ready items into free builder room. <button class="btn quiet sm" type="button" data-pickup="off"${QV.busy ? html` disabled` : ''}>Turn off</button>`
      : html`Auto-pickup is off. <button class="btn quiet sm" type="button" data-pickup="on" aria-label="Turn auto-pickup on until ${at(pk.next)}"${QV.busy ? html` disabled` : ''}>On until ${at(pk.next)}</button>`);
    const said = $('queue-said'); said.hidden = !qSaid; said.textContent = qSaid ? qSaid.text : ''; said.setAttribute('role', qSaid && !qSaid.ok ? 'alert' : 'status');
    const parent = g => { const on = qOpen === g.key, name = g.key || 'Unassociated';
      return html`<tr class="repo" data-repo="${g.key}" aria-expanded="${String(on)}">${marker(g.s, 'repo')}
        <td class="what"><button type="button" class="disc" data-open="${g.key}" aria-expanded="${String(on)}" aria-label="${on ? 'Close' : 'Open'} ${name}">${I('chevron-right')}</button><button type="button" class="pick" data-open="${g.key}">${name}</button>${g.host ? html`<small>lane on ${g.host}</small>` : ''}</td>
        ${pips(g)}<td class="num"></td><td class="act"></td></tr>`; };
    // pack sd:8138: a pressed box shows its rows, hidden by a Show filter or not, under a line that clears it.
    const kids = g => { if (!qTile) return g.shown.map(c => c.tr);
      const rows = g.rows.filter(inBox(qTile.box)).sort((a, b) => QTYPES.indexOf(a.type) - QTYPES.indexOf(b.type));
      return [html`<tr class="child tiled"><td colspan="5"><button type="button" class="linkbtn" data-tile-clear="${g.key}">Showing ${qTile.box}: ${String(rows.length)} of ${String(g.rows.length)} · Show all</button></td></tr>`, ...rows.map(c => c.tr)]; };
    put($('queue'), groups.length ? html`${groups.map(g => html`${parent(g)}${qOpen === g.key ? kids(g) : ''}`)}`
      : html`<tr class="empty"><td colspan="5">${unread.length ? 'No row in what was read; the line under the table names what was not.'
        : `Nothing is working, merging, next or blocked${qShow.size ? ', and no row matches the filters' : ''}.`}</td></tr>`);
    if (QV.prCheck) unread.push(...QV.prCheck);
    const note = $('queue-note'); note.hidden = !unread.length; note.textContent = unread.map(u => `▨ ${u}`).join(' · ');
    shell.view.count('queue', active);
  }
  async function loadQueue() {
    QV.seen = true;
    const get = path => shell.getJSON(path).then(doc => [doc, ''], err => [null, err.message]);
    // The Tasks page's rows feed All items' rows and count (pack sd:8127), and Suggested's rows' tasks (pack sd:7334).
    const [[l, le], [b, be], [r, re], [t, te]] = await Promise.all(['/api/queue', '/api/queue/builds?show=all', '/api/queue/runner', '/api/tasks'].map(get));
    Object.assign(QV, { lanes: l, lanesErr: le, built: b, builtErr: be, runner: r, runnerErr: re, tasks: t, tasksErr: te });
    qAdopt();
  }
  // The view's rows from what QV holds: the open items and lane entries become command objects, and one put before that
  // no read lists now retires (pack sd:7251), as the reader retires a Needs you row: its pick drops, and it is 'not listed'.
  let qPut = new Set();
  function qAdopt() {
    OPENI = openItems(); TD.adopt([...OPENI.values()]);
    freshRows();
    const now = new Set();
    OPENI.forEach(x => { shell.commands.put(TD.objectOf(taskOf(x.key))); now.add(x.key); });
    QOTHER.clear();
    (QV.lanes ? QV.lanes.lanes : []).forEach(lane => lane.rows.filter(x => x.state !== 'building').forEach(x => {
      const id = entryId(lane, x);
      shell.commands.put({ id, type: 'entry', label: `sd:${x.item}`, item: x.item, worktree: x.worktree, repo: lane.repo });
      QOTHER.set(id, { draw: () => entryDetails(lane, x) }); now.add(id);
    }));
    qPut.forEach(id => { if (now.has(id) || listed(id)) return;
      if (picked.includes(id)) shell.commands.pick(id);
      shell.commands.put({ id, type: 'not listed', label: `${shell.commands.get(id)?.label || id} (no longer listed)` }); });
    qPut = now;
    queueView();
  }
  function entryDetails(l, r) {
    const [s, type, facts] = ENTRY[r.state](r);
    return html`<p class="kind"><span class="g-${s}" aria-hidden="true">${GLYPH[s]}</span> Lane entry · ${type}</p>
      <h2><a href="${shell.itemHref(r.item)}">#${r.item} ${r.title}</a></h2>
      <dl><dt>Repo</dt><dd>${l.repo}</dd><dt>Lane</dt><dd>${l.lane}</dd><dt>State</dt><dd>${r.state}</dd><dt>Facts</dt><dd>${facts}</dd></dl>
      <h3>Act</h3>${shell.commands.bar(entryId(l, r))}<div id="adv"></div>`;
  }
  // The last entry a review started for shows its run under its Details; commands.js reads the run every 10 s until it settles.
  document.addEventListener('adversarial:run', e => {
    const adv = document.getElementById('adv');
    if (adv && e.detail.key.startsWith('change:')) put(adv, html`${window.commands.reviewPane('change', e.detail.key.slice('change:'.length))}`);
  });

  const DONE = { up: 'moved up', down: 'moved down', top: 'moved to the top', hold: 'held', release: 'released', cancel: 'cancelled',
    retry: 'queued again', approve: 'approved and queued again', requeue: 're-queued', reclaim: 'released from its holder' };
  // A lane write that did not land, said one way on Queue and on Today (pack sd:7315). Only a 4xx wrote nothing
  // (queue_screen.move); a 502, another status or no answer may follow a landed write (sd:3339). A 409 is a queue that
  // changed since the read: the page has read it again, so the line drops the server's "Read it again, then retry", says
  // when, and says a press acts on that read. `read` is false when that read failed or left this repo's lane out: then
  // nothing claims a fresh read.
  // `why` is the reason alone, for a toast that names the row.
  function notDone(repo, item, action, err, read = true) {
    const why = String(err.message).replace(/([^.!?])$/, '$1.'), known = err.status >= 400 && err.status < 500;
    const again = read ? `Read again at ${HM.format(new Date())}; a press now acts on that read.` : 'It was not read again: Read again tries it.';
    const after = err.stale ? `${why.replace(/\s*Read it again, then retry\.$/, '')} ${again}`
      : known ? `${why} The queue is unchanged.`
      : `${why} The lane was ${read ? '' : 'not '}read again; check sd:${item} there before you act again.`;
    return { why: after, text: known ? `${repo}: sd:${item} not ${DONE[action]}: ${after}` : `${repo}: not known whether sd:${item} was ${DONE[action]}: ${after}` };
  }
  // Cancel, Approve and Release are the writes no verb undoes; each dialog names the entry and the command it runs.
  function grant(l, item, head) {
    const r = l.rows.find(x => x.state === 'blocked' && x.item === item);
    return shell.confirm({ title: `Approve sd:${item}${r && r.title ? ` · ${r.title}` : ''} in ${l.repo}?`,
      body: `The entry is queued again at head ${head.slice(0, 12)}, and the runner may merge it to the default branch. A merge is not taken back.`,
      cli: `sd-ship -C ${shell.shq(l.path)} lane retry ${item} --expected-head ${head} --manual`, ok: 'Approve merge', keep: 'Leave it blocked' });
  }
  function sure(l, item) {
    const r = l.rows.find(x => x.state === 'next' && x.item === item);
    return shell.confirm({ title: `Cancel sd:${item}${r && r.title ? ` · ${r.title}` : ''} in ${l.repo}?`,
      body: `The entry leaves the queue and the runner never ships it. ${QV.lanes.requeue
        ? 'Re-queue on its cancelled row puts it back, last, while its branch stays at this head.'
        : 'To ship it later, enqueue it again from its worktree.'}`,
      cli: `sd-ship -C ${shell.shq(l.path)} lane cancel ${item}`, ok: 'Cancel entry', keep: 'Keep it queued' });
  }
  // Release names what the pack's reclaim does at the step the row showed; the claim binds it to that step.
  function letGo(l, item, claim) {
    const r = l.rows.find(x => x.state === 'merging' && x.item === item && x.claim === claim) || {};
    const host = r.host || 'a host not recorded', ago = r.claim_age == null ? 'at a time not recorded' : `${dur(r.claim_age)} ago`;
    const outcome = r.step === 'prepare'
      ? 'The entry goes back to pending at its place and runs again; its third reclaim fails it instead.'
      : 'The entry fails with the release as its reason. A merge it already started may still land, so read its pull request before you enqueue it again.';
    return shell.confirm({ title: `Release sd:${item}${r.title ? ` · ${r.title}` : ''} from ${host}?`,
      body: `${host} claimed it ${ago}, at ${r.step || 'a step not recorded'}. ${outcome} The holder's next queue write is refused. If the entry moved on since this read, nothing is released. sd-ship refuses the release when the holder runs alive on this machine.`,
      cli: `sd-ship -C ${shell.shq(l.path)} lane cancel ${item} --expected-claim ${shell.shq(claim)}`, ok: 'Release entry', keep: 'Keep it running' });
  }
  async function laneAct(b) {
    const l = QV.lanes && QV.lanes.lanes.find(x => x.path === b.dataset.repo);
    if (!l || QV.busy) return;
    const item = Number(b.dataset.item), action = b.dataset.act, head = b.dataset.head || '', claim = b.dataset.claim || '';
    if (action === 'cancel' && !(await sure(l, item))) return;
    if (action === 'reclaim' && !(await letGo(l, item, claim))) return;
    if (action === 'approve' && !(await grant(l, item, head))) return;
    QV.busy = true; queueView();
    let failed = null;
    try {
      const body = { repo: l.path, item, action, revision: l.revision };
      if (action === 'retry' || action === 'approve' || action === 'requeue') body.head = head;
      if (action === 'reclaim') body.claim = claim;
      const out = await shell.post('/api/queue/move', body);
      const order = out.pending ? ` Order now: ${out.pending.map(n => `sd:${n}`).join(', ')}.` : '';
      qSaid = { ok: true, text: `${l.repo}: sd:${item} ${DONE[action]}.${order} ${out.edits || ''}` };
    } catch (err) {
      failed = err; qSaid = null;
    }
    QV.busy = false;
    await loadQueue();
    // A failed write is said after the read again, and claims that read only when it listed this repo's lane.
    if (failed) {
      qSaid = { ok: false, text: notDone(l.repo, item, action, failed, !!(QV.lanes && QV.lanes.lanes.some(x => x.path === l.path))).text };
      queueView();
    }
  }
  async function buildAct(b) {
    if (QV.busy || !QV.built || QV.built.error) return;
    const act = b.dataset.build, r = QV.built.runs.concat(QV.built.recent).find(x => x.run === b.dataset.run);
    if (!r) return;
    const what = r.items.map(m => `sd:${m.item}`).join(', ');
    if (act === 'cancel' && !(await shell.confirm({ title: `Cancel build run ${r.run} (${what})?`,
      body: `The supervisor stops the ${r.step} step at its next beat, keeps the worktree, ends the bundle cancelled and moves ${r.items.length > 1 ? 'its items' : 'the item'} back to ready. Nothing is enqueued.`,
      cli: `sd assignments cancel ${r.cancel.assignment} --if-revision ${r.cancel.revision}`, ok: 'Cancel build', keep: 'Keep it running' }))) return;
    let finding = '';
    if (act === 'send') {
      const { yes, values } = await shell.ask({ title: `Send to builder: run ${r.run} (${what})?`, ok: 'Send', keep: 'Cancel', danger: false,
        body: `The finding goes on ${r.items.length > 1 ? 'each item' : 'the item'} as a note, and the bundle queues again. Its next builder resumes ${r.worktree || 'the same worktree'} on its branch and fixes the finding first.`,
        fields: [{ name: 'finding', label: 'Finding', required: true, placeholder: 'What the builder should fix' }] });
      finding = (values && values.finding || '').trim();
      if (!yes || !finding) return;
    }
    const done = act === 'cancel' ? 'cancelled' : act === 'send' ? 'sent to the builder' : 'retried';
    QV.busy = true; queueView();
    try {
      if (act === 'cancel') await shell.post(`/api/assignments/${r.cancel.assignment}/cancel`, { revision: r.cancel.revision });
      else await shell.post('/api/queue/builds/retry', { assignment: r.retry.assignment, revision: r.retry.revision, ...(finding ? { finding } : {}) });
      qSaid = { ok: true, text: act === 'cancel' ? `Run ${r.run} (${what}): cancel requested; it stops at its next beat.`
        : act === 'send' ? `${what} sent to the builder with your finding.` : `${what} queued again for build.` };
    } catch (err) {
      qSaid = { ok: false, text: err.status >= 400 && err.status < 500 ? `Run ${r.run} not ${done}: ${err.message}`
        : `Not known whether run ${r.run} was ${done}: ${err.message} Read the view before you act again.` };
    }
    QV.busy = false;
    await loadQueue();
  }

  // The Timeline (sd:5921, pack sd:7681): one chart on one time axis. First Today's four lanes, the document's `track`
  // (7 days of marks); then one lane per Activity kind, /api/activity?range=7d, read when the view first shows and on each
  // Read again after. The 24h / 7d tiles pick the window the chart and its table draw; ?range=7d keeps the longer one.
  // Each mark links to its row, the same marks are a table in a details, and the key's four counts link to Tasks. A track
  // that was not read says so; a window with no mark says that in one line, never an empty axis. Landed links the items
  // done less than a day ago (sd:5934): Tasks' In status bucket 0, as a task has no merged or delivered status.
  const LANES = [['landed', 'Landed', 18], ['jobs', 'Jobs', 40], ['briefs', 'Briefs', 58], ['live', 'Live', 74]];
  // The Activity kinds and their lanes, moved from Activity's Cadence view (sd:5064). A kind is unknown when every source
  // behind it failed to read: its lane is hatched with the reason, as unknown is not zero.
  const KINDS = [{ id: 'merge', name: 'Merges', src: ['merge'] }, { id: 'run', name: 'Runs', src: ['run', 'job'] },
    { id: 'review', name: 'Reviews', src: ['review'] }, { id: 'command', name: 'Commands', src: ['command'] }];
  const KIND_Y = 92, KIND_STEP = 18, TRACK_H = KIND_Y + KIND_STEP * KINDS.length + 12;
  const SPAN = { '24h': 864e5, '7d': 7 * 864e5 }, SPAN_SAID = { '24h': 'Last 24 hours', '7d': 'Last 7 days' };
  let span = new URLSearchParams(location.search).get('range') === '7d' ? '7d' : '24h';
  const AV = { seen: false, doc: null, error: '', n: 0 };
  const kindWhy = k => AV.error || (AV.doc ? (k.src.every(s => AV.doc.sources[s]) ? k.src.map(s => AV.doc.sources[s]).join('; ') : '') : '');
  // Of overlapping reads only the newest draws; a failed one leaves no event from the last read.
  function loadActivity() {
    const n = ++AV.n; AV.seen = true;
    return shell.getJSON('/api/activity?range=7d').then(doc => { if (n === AV.n) { AV.doc = doc; AV.error = ''; } },
      err => { if (n === AV.n) { AV.doc = null; AV.error = err.message || 'the read failed'; } }).then(() => { if (n === AV.n && DOC) dayTrack(); });
  }
  const markHref = (lane, m) => lane === 'landed' || lane === 'live' ? shell.itemHref(m.item) : `?row=${encodeURIComponent(m.id)}`;
  // Activity reads 24 hours unless asked, so a 7d link carries the range, or an older event opens with no row.
  const eventHref = e => `/activity?kind=${e.k}&row=${encodeURIComponent(e.id)}${span === '7d' ? '&range=7d' : ''}`;
  function rangeTiles() {
    put($('track-range'), html`${Object.keys(SPAN).map(v => html`<button type="button" data-range="${v}" aria-pressed="${String(span === v)}">${v}</button>`)}`);
    $('track-h').textContent = `The ${SPAN_SAID[span].toLowerCase()}`;
  }
  function dayTrack() {
    rangeTiles();
    const T = DOC.track || { error: 'the document carried no track' };
    if (T.error) {
      put($('track-plot'), html`<p class="why">The day track was not read: ${T.error}. Read again tries again.</p>`);
      put($('flowkey'), html``); put($('track-rows'), html``); $('track-table').hidden = true; return;
    }
    const t1 = Date.parse(T.until), t0 = t1 - SPAN[span], X0 = 84, W = 900;
    const x = at => (X0 + (W - X0 - 8) * (Math.max(Date.parse(at), t0) - t0) / (t1 - t0)).toFixed(1);
    const inside = at => Date.parse(at) >= t0 && Date.parse(at) <= t1;
    // A live session spans from its start or the window's, whichever is later, so Live keeps every session running now.
    const marks = LANES.flatMap(([k, label]) => T[k].filter(m => k === 'live' || inside(m.at)).map(m => ({ ...m, lane: k, label })));
    const of = k => marks.filter(m => m.lane === k).length;
    const events = AV.doc ? AV.doc.events.filter(e => inside(e.at)) : [];
    const evs = k => events.filter(e => e.k === k.id);
    // A failed Activity read draws its hatched lanes even in a window with no mark: unknown is not zero.
    const unknown = KINDS.some(k => kindWhy(k));
    const said = `${SPAN_SAID[span]}: ${of('landed')} landed, ${plural(of('jobs'), 'job')} failed, ${plural(of('briefs'), 'brief')} filed, ${plural(of('live'), 'session')} live`
      + (AV.doc || AV.error ? `; ${KINDS.map(k => kindWhy(k) ? `${k.name.toLowerCase()} not read` : `${evs(k).length} ${k.name.toLowerCase()}`).join(', ')}` : '');
    const ticks = [];
    // A day tick is each local midnight, stepped by the calendar, so a day of 23 or 25 hours (DST) keeps one tick a day.
    if (span === '7d') for (const d = new Date(t0); d.setHours(24, 0, 0, 0) <= t1;) ticks.push([d.getTime(), d.toLocaleDateString([], { weekday: 'short', day: 'numeric' })]);
    else for (let t = Math.ceil(t0 / 36e5) * 36e5; t <= t1; t += 36e5) if (new Date(t).getHours() % 3 === 0) ticks.push([t, `${String(new Date(t).getHours()).padStart(2, '0')}:00`]);
    const mark = m => { const at = x(m.at), tip = html`<title>${hm(m.at)} UTC · ${m.what}</title>`;
      return html`<a href="${markHref(m.lane, m)}" data-id="${markId(m)}">${m.lane === 'landed' ? html`<line class="land" x1="${at}" y1="10" x2="${at}" y2="26">${tip}</line>`
        : m.lane === 'jobs' ? html`<rect class="fail" x="${(at - 4).toFixed(1)}" y="36" width="8" height="8">${tip}</rect>`
        // sd:6232: a solid dot, a shape no other lane uses, inside a clear 24-unit square that takes the press.
        : m.lane === 'briefs' ? html`<rect class="hit" x="${(at - 12).toFixed(1)}" y="46" width="24" height="24">${tip}</rect><circle class="brief" cx="${at}" cy="58" r="4.5"/>`
        : html`<rect class="sess" x="${at}" y="72" width="${Math.max(2, W - 8 - at).toFixed(1)}" height="3">${tip}</rect>`}</a>`; };
    // An Activity lane: a tick per event at its time, its state its colour; ok first, so the loudest ticks sit on top.
    // A tick links to its event on Activity.
    const ORDER = { ok: 0, queued: 0, unknown: 0, caution: 1, warning: 2 };
    const kindLane = (k, i) => {
      const y = KIND_Y + i * KIND_STEP, why = kindWhy(k);
      const label = html`<text class="lane-l" x="0" y="${y + 4}">${k.name}</text>`;
      if (!AV.doc && !AV.error) return html`${label}<text class="lane-note" x="${X0}" y="${y + 4}">reading…</text>`;
      if (why) return html`${label}<rect class="unread" x="${X0}" y="${y - 7}" width="${W - 8 - X0}" height="14"/><text class="lane-note" x="${X0 + 4}" y="${y + 4}">▨ unknown · ${k.name.toLowerCase()} not read: ${why}</text>`;
      return html`${label}${evs(k).sort((a, b) => ORDER[a.s] - ORDER[b.s]).map(e => html`<a href="${eventHref(e)}"><rect class="tick ${e.s}" x="${(+x(e.at) - 1.5).toFixed(1)}" y="${y - 6}" width="3" height="12"><title>${hm(e.at)} UTC · ${e.what}</title></rect></a>`)}`;
    };
    put($('track-plot'), marks.length || events.length || unknown ? html`<svg viewBox="0 0 ${W} ${TRACK_H + 14}" role="img" aria-label="${said}">
      <defs><pattern id="track-hatch" width="7" height="7" patternUnits="userSpaceOnUse" patternTransform="rotate(45)"><line class="hatch" x1="0" y1="0" x2="0" y2="7"/></pattern></defs>
      ${ticks.map(([t, label]) => { const gx = x(new Date(t).toISOString()); return html`<line class="grid" x1="${gx}" y1="6" x2="${gx}" y2="${TRACK_H - 4}"/><text class="axis" x="${gx}" y="${TRACK_H + 10}" text-anchor="middle">${label}</text>`; })}
      ${LANES.map(([, label, y]) => html`<text class="lane-l" x="0" y="${y + 4}">${label}</text>`)}
      ${marks.map(mark)}${KINDS.map(kindLane)}<line class="now" x1="${x(T.until)}" y1="4" x2="${x(T.until)}" y2="${TRACK_H - 2}"/></svg>`
      : html`<p class="why">Nothing landed, failed, was filed or ran in the ${SPAN_SAID[span].toLowerCase()}.</p>`);
    const K2 = T.key;
    put($('flowkey'), html`<li><a href="/tasks?view=list&amp;status=done&amp;age=0&amp;realm=all">Landed 24h</a></li><li><b>${K2.landed}</b></li>
      <li><a href="#view=queue">Landing</a></li><li><b>${K2.landing}</b></li>
      <li><a href="/tasks?view=list&amp;due=week&amp;realm=all">Due this week</a></li><li><b>${K2.due_week}</b></li>
      <li><a href="/tasks?view=list&amp;status=blocked&amp;realm=all">Blocked</a></li><li><b>${K2.blocked}</b></li>`);
    $('track-table').hidden = !marks.length && !events.length;
    const rows = [...marks.map(m => ({ at: m.at, tr: html`<tr data-id="${markId(m)}" aria-selected="${String(row() === markId(m))}"><td class="when"><time datetime="${m.at}">${clock(m.at)}</time></td><td>${m.label}</td><td class="what"><a href="${markHref(m.lane, m)}">${m.what}</a></td></tr>` })),
      ...events.map(e => ({ at: e.at, tr: html`<tr><td class="when"><time datetime="${e.at}">${clock(e.at)}</time></td><td>${KINDS.find(k => k.id === e.k).name}</td><td class="what"><a href="${eventHref(e)}">${e.what}</a></td></tr>` }))];
    put($('track-rows'), html`${rows.sort((a, b) => a.at < b.at ? 1 : -1).map(r => r.tr)}`);
  }

  // Since yesterday (sd:6002): the document's `digest`, merged per repo with the PRs folded under the lines, failed with
  // why and one fix each, and the questions waiting on Decide. A read that failed says so, never a quiet night.
  function digest() {
    const D = DOC.digest || { error: 'the document carried no digest' };
    if (D.error) return put($('digest'), html`<p class="why">The digest was not read: ${D.error}. Read again tries again.</p>`);
    // sd:6259: a job or lane read that failed leaves the failures unknown, so the card never reads as a quiet night.
    if (!D.merged.length && !D.failed.length && !D.questions && !D.unknown) return put($('digest'), html`<p class="why">Nothing merged, failed or waiting since yesterday.</p>`);
    put($('digest'), html`<ul class="lines">${D.merged.map(m => html`<li><b>${m.repo}</b>: ${m.n} merged · ${m.titles.join(', ')}${m.n > m.titles.length ? ', …' : ''}</li>`)}
      ${D.more ? html`<li>and ${plural(D.more, 'more repo')}</li>` : ''}
      ${D.failed.map(f => html`<li class="g-warning"><b>${f.what}</b> · ${f.why} · ${f.fix}</li>`)}
      ${D.unknown ? html`<li class="g-warning">Failed jobs and blocked lane entries unknown: ${D.unknown}. Read again tries again.</li>` : ''}
      ${D.questions ? html`<li><a href="/decide">${plural(D.questions, 'question')} ${D.questions === 1 ? 'waits' : 'wait'}</a></li>` : ''}</ul>
      ${D.prs.length ? html`<details><summary>${plural(D.prs.length, 'merged item')}</summary><ul class="prs">${D.prs.map(p => html`<li><a href="${shell.itemHref(p.item)}">#${p.item} ${p.title}</a> · ${p.repo}${p.pr ? ` · PR #${p.pr}` : ''}</li>`)}</ul></details>` : ''}`);
  }

  // The spend line (sd:6031): the document's `spend`, spent in the last 24 hours and in each of the last 7, as bars oldest
  // first. Visibility only: no cap. A read that failed says so, never $0.00.
  const usd = n => `$${Number(n).toFixed(2)}`;
  function spendLine() {
    const S = DOC.spend || { error: 'the document carried no spend' };
    if (S.error) return put($('spend'), html`Spend not read: ${S.error}. Read again tries again.`);
    const top = Math.max(...S.days, 0.01), H = 16;
    put($('spend'), html`<span class="lbl">Spend</span> 24h <b>${usd(S.day)}</b> · 7 days <b>${usd(S.week)}</b>
      <svg class="spark" viewBox="0 0 ${S.days.length * 6} ${H}" role="img" aria-label="${`Spent per day, oldest first: ${S.days.map(usd).join(', ')}`}">${S.days.map((d, i) => {
        const h = d > 0 ? Math.max(1, Math.round(H * d / top)) : 0;
        return html`<rect x="${i * 6}" y="${H - h}" width="4" height="${h}"><title>${usd(d)}</title></rect>`; })}</svg>
      <a href="/metrics">Metrics</a>`);
  }

  // The badge is the read's own (sd:3446, now_screen._attention): Needs you's broken rows, else its look rows.
  function attend() {
    shell.attention(DOC && DOC.attention || { state: 'unknown', n: 0, what: 'rows' });
  }
  function unselect() {
    const u = new URL(location.href); u.searchParams.delete('row'); history.replaceState(null, '', u);
    put($('details'), html`<p class="why">${!DOC ? 'Nothing is selected: Today could not be read.' : NEEDS.length ? 'No row matches the filter, so nothing is selected. Esc clears it.' : 'Nothing is selected: nothing needs you.'}</p>`);
  }
  const reconcile = () => shell.reconcile({ rows: $('rows').querySelectorAll('tr[data-id]'), current: row(),
    keep: id => OTHER.has(id) || QOTHER.has(id) || isItem(id), select: id => select(id, false), clear: unselect });
  function applyFilter() { needsTable(); reconcile(); }

  // ---------- Reading (build, sd:2483): shell.read reads /api/now ----------
  // The reader (read.js, sd:2418) holds the guards: of overlapping reads only the newest draws, a row the read no longer lists
  // runs no command, a selection whose row is gone moves to the first row the filter shows, and a failed read clears the page.
  let reading = null, failed = '';
  function adopt(doc) {
    DOC = doc; NEEDS = doc.needs || []; SNOOZED = doc.snoozed; ACTIVE = doc.active || null;
    ALL = new Map(); NEEDS.forEach(r => { ALL.set(r.id, r); (r.members || []).forEach(m => ALL.set(m.id, m)); });
    OTHER = others(doc);
    SUGG = suggested(doc); OPENI = openItems();
    freshRows();
    document.body.dataset.observed = doc.now;
    const items = [...[...SUGG].map(([id, r]) => SROW.has(id) ? TD.objectOf(SROW.get(id)) : { id, type: 'item', label: `#${r.id} ${r.title}`, item: r.id }),
      ...[...OPENI.keys()].map(k => TD.objectOf(taskOf(k)))];
    return { objects: [...[...ALL.values()].map(objectOf), ...[...OTHER].map(([id, o]) => ({ id, ...o.obj })), ...items, ...SNOOZED.map(window.snooze.object)],
             state: doc.snooze_error ? window.snooze.unread(doc.snooze_error, '/api/now') : null };
  }
  function clear(err) { DOC = null; NEEDS = []; ALL = new Map(); OTHER = new Map(); SUGG = new Map(); OPENI = new Map(); TASKS = new Map(); SROW.clear(); SNOOZED = []; ACTIVE = null; failed = err.message; delete document.body.dataset.observed; }
  function draw() {
    if (DOC) return render();
    // Nothing from the last read stays on screen: rows, counts, lamps, the observed time, the selection and the badge.
    $('subhead').textContent = `Today could not be read: ${failed}. Read again tries again.`;
    document.querySelectorAll('#annunciator .cell:not(.clock)').forEach(cell => { cell.dataset.state = 'unknown'; tip(cell, cell.dataset.src || cell.id.replace('lamp-', ''), 'Today could not be read'); put(cell.querySelector('.val'), html`<span class="ph">not read</span>`); });
    put($('tally'), html``); put($('intents'), html``);
    put($('rows'), html`<tr class="empty"><td colspan="6">Needs you could not be read: ${failed}</td></tr>`);
    queueView();
    $('unread').hidden = true;
    put($('track-plot'), html`<p class="why">The day track was not read: ${failed}.</p>`); put($('flowkey'), html``); $('track-table').hidden = true;
    put($('spend'), html`Spend not read: ${failed}.`);
    put($('digest'), html`<p class="why">The digest was not read: ${failed}.</p>`);
    window.snooze.draw($('snoozed'), []);
    attend();
  }
  function load() {
    return reading.load();
  }

  function clearSource() { srcFilter = null; intent = null; document.querySelectorAll('.cell[data-src]').forEach(x => x.setAttribute('aria-pressed', 'false')); if (DOC) { intents(); applyFilter(); } }

  // build (sd:3521): capture's repo default is the row's: a repo row names it in its id, a pull request as owner/name.
  window.PAGE_REPO = o => o && o.type === 'repo' ? o.id.split(':')[1] : (o && o.repo) || null;

  // Read again (sd:5638): the shell's head button reads Today again; sd:6697 dropped the Observed lamp.
  // sd:8375: on the Queue view it first asks GitHub about the pull request of each item a PR row shows, at most 20: the
  // server closes an item whose PR merged. What it could not read or close stays open and is named under the table.
  async function prCheck() {
    const items = [...new Set((((QV.lanes || {}).prs || {}).rows || []).filter(p => p.item != null).map(p => Number(p.item)))].slice(0, 20);
    // Posted with no items too: the server also checks an open item whose PR it recorded merged but did not close.
    try { QV.prCheck = (await shell.post('/api/queue/prs/check', { items })).unread || []; }
    catch (e) { QV.prCheck = [`Pull requests not checked on GitHub: ${e.message}`]; }
  }
  window.PAGE_AGAIN = { title: 'Read Today again', run: () => shell.view.current() === 'queue' ? prCheck().then(load) : load() };

  window.PAGE_COMMANDS = [
    { label: 'Clear the filter', icon: 'filter', run: clearSource },
    { label: 'Read Today again', icon: 'rotate-ccw', run: () => load() },
  ];

  document.addEventListener('DOMContentLoaded', () => {
    const C = shell.commands;
    const K = window.commands;
    const post = (path, body) => window.shell.post(path, body).then(out => reading.reread().then(() => out), err => { load(); throw err; });
    // jobs.retry (commands.js). build (sd:3386): it posts /api/jobs/<job>/retry with the revision the read brought, as
    // Management does; launchd starts the run. A refused retry reads Today again; one with no answer may have started, so it
    // says the outcome is not known.
    const retry = K.retry({ job: o => ({ name: o.job, failed: o.failed, exit: o.exit, capability: o.capability, sends: o.retry }),
      run: o => window.shell.post(`/api/jobs/${encodeURIComponent(o.job)}/retry`, { revision: o.revision }).then(
        out => reading.reread().then(() => K.answered(out, o.job, 'retried', `Retry started · ${o.job}`)),
        err => { load(); throw err.status ? err : new Error(`no answer (${err.message}), so whether launchd started it is not known; Today is read again`); }) });
    const open = K.openPullRequest({ pr: o => ({ repo: o.repo, number: o.number }), primary: o => !!o.repo });
    // Fix (sd:5652, sd:6709): commands.fix's label, key and icon, always claude in herdr on the row (fix_session.start_row).
    // A row whose act is something else shows that command under its own name: a job's Run now, a pull request's Open on
    // GitHub, the pull request collector's Re-run collector.
    const sync = K.syncCollector({ reread: () => reading.reread(), page: 'Today' });
    const prs = o => o.id === 'dark:prs';
    const session = (on, when) => ({ ...K.fix(on, { item: o => o.id, report: () => false, when, post: null }), primary: () => true,
      does: 'Asks first, then opens a Claude session (herdr) to fix this row.',
      cli: () => 'no sd verb: the dashboard runs local-herdr/herdr.sh launch', sends: o => `POST /api/now/fix ${JSON.stringify({ row: o.id })}`,
      consequence: o => `Opens a Claude session on "${o.label}", in ${o.type === 'repo' ? 'its repo' : 'the system repo'}. Pressing Fix again opens a second session: look in herdr first.`,
      run: o => window.shell.post('/api/now/fix', { row: o.id }).then(
        out => reading.reread().then(() => `Fix started · ${o.label} · claude in herdr workspace ${out.fix.workspace}, pane ${out.fix.pane}`),
        err => { load(); throw err.status ? err : new Error(`no answer (${err.message}), so whether a session started is not known; look in herdr before you press Fix again`); }) });
    // A group's act on every member (sd:5921): one write each, through the route a member row's own act posts.
    const each = (o, body) => Promise.all(o.members.map(m => window.shell.post('/api/snooze', body(m))))
      .then(() => reading.reread(), err => { load(); throw err; });
    const week = () => new Date(Date.now() + 7 * 864e5).toISOString();
    const BULK = 25; // shell.js asks first for a bulk act on more rows than this; a group act on more members asks too
    // A lane row's Retry and Approve (pack sd:7315): a press clears the line above Needs you; a write that did not land
    // reads Today again first, then says why on that line, as Queue's does, until the next press. Nothing retries itself.
    const needsSaid = text => { const s = $('needs-said'); s.hidden = !text; s.textContent = text || ''; s.setAttribute('role', text ? 'alert' : 'status'); };
    const lane = action => async o => {
      needsSaid('');
      try {
        const out = await window.shell.post('/api/queue/move', { repo: o.repoPath, item: o.item, action, revision: o.laneRevision, head: o.head });
        await reading.reread();
        return out;
      } catch (err) {
        // The read again counts for this row only when it read this repo's lane too.
        const read = await Promise.resolve(load()).catch(() => false) !== false && DOC && !DOC.sources.lane
          && !((DOC.lane || {}).unread_paths || []).includes(o.repoPath);
        const said = notDone(o.repo, o.item, action, err, read);
        needsSaid(said.text);
        throw new Error(said.why);
      }
    };
    const go = (href, said) => () => { location.href = href; return said; };
    // pack sd:7271: an item row's Details, fields and item commands are the Tasks page's (task-details.js), read from
    // /api/tasks/<id>; a write folds into the row, then Today reads again. A ready row's act is Tasks' Build
    // (commands.build), from the row the reading named, or before that from the read's ready row: ready, its repo, no
    // build holding it. Build registers ahead of TD's commands, so it is the act wherever it is on; elsewhere the act is
    // the move to the next status, as on Tasks' list (view 'list').
    const sel = () => isItem(row()) ? row() : null;
    const ready = o => taskOf(o.id) || (SUGG.has(o.id) ? { id: SUGG.get(o.id).id, status: SUGG.get(o.id).status, repo: SUGG.get(o.id).repo, repo_path: SUGG.get(o.id).repo, assignment: null } : null);
    C.register(K.build({ row: ready, reread: () => reading.reread(), primary: () => true }));
    TD = window.taskDetails.attach({ details: $('details'), rows: () => [...new Map([...OPENI, ...SROW]).values()], byKey: taskOf, selected: sel,
      read: () => DOC && DOC.now, view: () => 'list', render: () => { if (sel()) TD.renderDetails(); }, reread: () => reading.reread(),
      load: () => load().then(() => !!DOC, () => false), select: (key, open) => select(key, open),
      got: (id, got) => { if (got.row && isItem(String(id))) { const t = TD.shape(got.row); SROW.set(t.key, t); C.put(TD.objectOf(t)); } return false; },
      empty: () => TD.DET.failed[sel()] ? `#${sel()} did not open: ${TD.DET.failed[sel()]}.` : `Reading #${sel()}…`,
      more: t => html`<p class="why"><a href="${shell.itemHref(t.id)}">Open on Tasks</a>: the board, the matrix and every task.</p>` });
    // The Fix commands Today registers (pack sd:8113): a broken row with none of these live gains review-and-fix below.
    const FIXES = [session('collector', o => !prs(o) || 'Re-run collector fixes the pull request collector'), session('repo'), session('sessions'),
      K.fix('work item', { item: o => o.item, report: o => o.itemKind === 'report', post: (o, path) => post(path, { revision: o.revision }) }),
      { ...K.fix('report', { item: o => o.item, report: () => true, login: o => o.login, post: (o, path) => post(path, { revision: o.revision }),
        when: o => !!o.login || !o.claim || `${o.claim}: open its session in herdr, or Fix it from Reports` }), primary: o => !!o.login },
      K.fix('live item', { item: o => o.item, report: o => o.itemKind === 'report', when: o => !!o.revision || 'its revision was not read; Fix it from Tasks',
        post: (o, path) => post(path, { revision: o.revision }) })];
    const ownFix = o => FIXES.some(c => c.on === o.type && (!c.when || c.when(o) === true));
    // Fix and Fix all on flaky heads (pack sd:8113): POST /api/items {fix: [row ids]} files one task per head, each under
    // --ref flaky:<repo>:<head> with a brief the server writes from its lane read, and queues its build (the Build
    // button's build.queue). A head whose task is open files nothing: Fix opens that task. Fix all counts each outcome.
    const fixHeads = ids => window.shell.post('/api/items', { fix: ids }).then(out => reading.reread().then(() => out), err => { load(); throw err; });
    const failedText = out => out.failed.map(f => `${f.row.replace(/^flaky:/, '')}: ${f.error}`).join('; ');
    // A broken row with no Fix of its own (pack sd:8113): file a review-and-fix task under --ref broken:<row id> in the
    // row's repo (none: fleet), naming the row's what, detail and source, then queue its build; an open one opens.
    // Each takes commands.fix's label, key and icon (sd:6709) and files instead of opening a session, so it asks nothing.
    const fileFix = on => ({ ...K.fix(on, { item: () => null, report: () => false, post: null }), askFirst: false, primary: () => true, consequence: undefined });
    const brokenFix = on => ({ ...fileFix(on), id: `today.broken.fix.${on.replace(/\W+/g, '-')}`,
      does: 'Files a review-and-fix task and queues its build, or opens the open one.',
      when: o => o.band !== 'broken' ? 'only a broken row files a review-and-fix task' : !ownFix(o) || 'this row has its own Fix',
      cli: o => `sd task add "Review and fix: ${o.label}" --ref broken:${o.id}${o.repoPath ? ` --belongs-to ${o.repoPath}` : ''}, then sd assignments build <id>`,
      sends: o => `POST /api/items {ref: broken:${o.id}, build: true}`,
      run: o => window.shell.post('/api/items', { title: `Review and fix: ${o.label}`.slice(0, 200), ref: `broken:${o.id}`, build: true,
        ...(o.repoPath ? { repo: o.repoPath } : {}),
        body: [`What: ${o.label}`, `Detail: ${o.detail || 'none given'}`, `Source: ${o.source || 'not named'}`, '',
          'Review why Today shows this row as broken, fix the cause fail-first, and say in a note what changed.'].join('\n') })
        .then(out => reading.reread().then(() => out), err => { load(); throw err; })
        .then(out => out.existing ? (shell.openItem(out.item.id), `sd:${out.item.id} is already open for this row; nothing filed`)
          : out.build_error ? `Filed sd:${out.item.id}; its build did not queue: ${out.build_error}` : `Filed sd:${out.item.id} and queued its build`) });
    C.register(
      // Each lane entry is a command object (sd:5064): its menu offers Adversarial review of the worktree the lane recorded.
      K.adversarialReview('entry', { payload: o => ({ kind: 'change', target: o.worktree, item: o.item }),
        when: o => !!o.worktree || 'the lane recorded no worktree for this entry' }),
      ...FIXES.slice(0, 3),
      { ...sync, when: o => prs(o) || 'only the pull request collector re-runs from Today' },
      retry,
      // Each row kind's commands from the page that shows it in full (sd:3449).
      K.showLog({ job: o => ({ name: o.job }) }),
      K.printJob({ job: o => ({ name: o.job, service: o.service, exit: o.exit }) }),
      open,
      // Fix (sd:3456, commands.js) on a work row, with the revision the read gave; a refused one reads Today again.
      FIXES[3],
      { id: 'today.item.open', on: 'work item', label: 'Open', does: 'Opens the item on Tasks.', key: 'o', icon: 'arrow-up-right', risk: 'safe', executes: false, primary: () => true,
        cli: o => `sd task show ${o.item}`, run: o => shell.openItem(o.item) },
      // A problem report whose output shows an expired login (sd:6964): Fix opens its Credentials row, ahead of Acknowledge.
      // Any other report's Fix is the Reports page's debug session, posted with the read's revision (pack sd:7683); one
      // another session took names who has it instead, so a second session does not start on it.
      FIXES[4],
      // A problem report (sd:5921): Acknowledge posts the Reports page's route with the revision the read gave.
      { id: 'today.report.ack', on: 'report', label: 'Acknowledge', does: 'Closes the report, not the fault: the next failure opens a new one.', key: 'a', icon: 'check', risk: 'safe', bulk: true, primary: o => !o.login,
        when: o => o.fieldsRead || `its fields cannot be read: sd reports acknowledge ${o.item}`,
        cli: o => `sd reports acknowledge ${o.item}`, sends: o => `POST /api/reports/${o.item}/acknowledge {revision}`,
        run: o => post(`/api/reports/${o.item}/acknowledge`, { revision: o.revision }).then(() => `Acknowledged · ${o.label}`) },
      { id: 'today.report.open', on: 'report', label: 'Open report', does: 'Opens the report on Tasks.', key: 'o', risk: 'safe', executes: false,
        cli: o => `sd task show ${o.item}`, run: o => shell.openItem(o.item) },
      { id: 'today.mail.open', on: 'mail thread', label: 'Open thread', does: 'Opens the thread in Gmail in a new tab.', key: 'o', icon: 'mail', risk: 'safe', executes: false, primary: () => true,
        cli: () => 'no CLI: the thread is in Gmail', run: o => { window.open(o.url, '_blank', 'noopener'); return `${o.label} opens in Gmail`; } },
      // A blocked lane entry (sd:5921): Queue's Retry and Approve, posted with the lane's revision and the head the read showed.
      { id: 'today.lane.retry', on: 'lane entry', label: 'Retry', does: 'Queues the entry again at the head it ended at.', key: 'r', icon: 'rotate-ccw', risk: 'safe', primary: () => true,
        cli: o => `sd-ship -C ${o.repoPath} lane retry ${o.item} --expected-head ${o.head}`, sends: () => 'POST /api/queue/move {action: retry}',
        when: o => !!o.head || 'the entry recorded no head; retry it from Queue', run: o => lane('retry')(o).then(() => `Queued again · sd:${o.item}`) },
      // Approve grants the merge, which nothing takes back, so it asks first as Queue's does; Retry runs at once there too.
      { id: 'today.lane.approve', on: 'lane entry', label: 'Approve', does: 'Asks first, then queues the entry again and lets the runner merge it.', key: 'p', risk: 'safe', askFirst: true,
        consequence: o => `The entry is queued again at head ${String(o.head).slice(0, 12)}, and the runner may merge it to the default branch. A merge is not taken back.`,
        cli: o => `sd-ship -C ${o.repoPath} lane retry ${o.item} --expected-head ${o.head} --manual`, sends: () => 'POST /api/queue/move {action: approve}',
        when: o => o.runnerMerge === 'manual' || 'this repo merges automatically, so it needs no approval', run: o => lane('approve')(o).then(() => `Approved · sd:${o.item}`) },
      { id: 'today.lane.queue', on: 'lane entry', label: 'Open in Queue', does: 'Opens its repo in the Queue view, where Cancel is.', key: 'q', risk: 'safe', executes: false,
        cli: o => `sd-ship -C ${o.repoPath} lane list`, run: o => { qOpen = repoKey(o.repoPath); shell.view.show('queue'); queueView(); return 'Queue opens'; } },
      // A new brief: Seen is the Today snooze held until the brief changes (sd:5752), the mark the place pages read too.
      { id: 'today.brief.seen', on: 'brief', label: 'Seen', does: 'Hides the brief from Today until its text grows.', key: 's', icon: 'eye', risk: 'safe', bulk: true, primary: () => true,
        cli: () => 'no sd verb: the dashboard writes the snooze (POST /api/snooze)',
        run: o => post('/api/snooze', { page: 'today', row: o.id, until: o.until, seen: o.seen }).then(() => `Seen · ${o.label}`) },
      { id: 'today.brief.read', on: 'brief', label: 'Read', does: 'Opens the brief on Tasks.', key: 'o', risk: 'safe', executes: false,
        cli: o => `sd task show ${o.item}`, run: o => shell.openItem(o.item) },
      // A group row (sd:5921): one act for every member.
      { id: 'today.group.triage', on: 'item group', label: 'Triage', does: 'Opens Tasks on the past-due items, every realm.', key: 't', icon: 'list-checks', risk: 'safe', executes: false,
        primary: o => o.group === 'group:late', when: o => o.group === 'group:late' || 'only the past-due group triages',
        cli: () => 'sd today', run: go('/tasks?view=list&due=overdue&realm=all', 'Tasks opens on the past-due items') },
      { id: 'today.group.open', on: 'item group', label: 'Open', does: 'Opens the members on Tasks or, for posts ready to send, on Writing.', key: 'o', icon: 'arrow-up-right', risk: 'safe', executes: false,
        primary: o => o.group !== 'group:late', cli: () => 'sd today',
        run: o => { location.href = o.group === 'group:ready' ? '/writing' : '/tasks?view=list&realm=all'; return 'Opens'; } },
      { id: 'today.group.snooze', on: 'item group', label: 'Snooze all 1w', does: 'Hides every member for a week, one snooze each.', key: 'z', risk: 'safe',
        askFirst: o => o.members.length > BULK, consequence: o => `Writes ${o.members.length} snoozes, one per member; each member comes back on its own after a week or by Unsnooze.`,
        cli: () => 'no sd verb: the dashboard writes one snooze per member (POST /api/snooze)',
        run: o => each(o, m => ({ page: 'today', row: m.id, until: week(), seen: m.seen })).then(() => `Snoozed ${o.members.length} for a week · ${o.label}`) },
      // A flaky head (pack sd:7663): Fix (pack sd:8113), Open item, and Snooze below; its group fixes or snoozes every head.
      { ...fileFix('flaky head'), id: 'today.flaky.fix', does: 'Files a fix task and queues its build, or opens the open one.',
        cli: o => `sd task add "Flaky test in ..." --ref ${o.id}, then sd assignments build <id>`, sends: o => `POST /api/items {fix: ["${o.id}"]}`,
        run: o => fixHeads([o.id]).then(out => {
          if (out.failed.length) throw new Error(failedText(out));
          const r = out.results[0];
          if (r.existing) { shell.openItem(r.item); return `sd:${r.item} is already open for this head; nothing filed`; }
          return `Filed sd:${r.item} and queued its build`; }) },
      { id: 'today.flaky.fix-all', on: 'flaky group', label: 'Fix all', does: 'Asks first, then files a task per flaky head and queues each build.',
        key: 'f', icon: 'bot', risk: 'safe', executes: true, primary: () => true, askFirst: true,
        consequence: o => `Files one task per flaky head and queues up to ${o.members.length} builds, one each. A head whose task is open files nothing.`,
        cli: () => 'sd task add --ref flaky:<repo>:<head> per head, then sd assignments build <ids>', sends: o => `POST /api/items {fix: [${o.members.length} row ids]}`,
        run: o => fixHeads(o.members.map(m => m.id)).then(out => {
          const text = `Fix all · ${out.filed} filed, ${out.existing} already open, ${out.failed.length} failed`;
          if (out.failed.length) throw new Error(`${text}: ${failedText(out)}`);
          return text; }) },
      { id: 'today.flaky.open', on: 'flaky head', label: 'Open item', does: 'Opens the item whose head the gate failed, then passed.', key: 'o', icon: 'arrow-up-right', risk: 'safe', executes: false, primary: () => true,
        when: o => !!o.item || 'the lane entry named no item', cli: o => `sd task show ${o.item}`, run: o => shell.openItem(o.item) },
      { id: 'today.flaky.snooze', on: 'flaky group', label: 'Snooze all 1w', does: 'Hides every flaky head for a week, one snooze each.', key: 'z', icon: 'clock', risk: 'safe', primary: () => true,
        askFirst: o => o.members.length > BULK, consequence: o => `Writes ${o.members.length} snoozes, one per head; each comes back after a week, by Unsnooze, or when its gate fails again.`,
        cli: () => 'no sd verb: the dashboard writes one snooze per head (POST /api/snooze)',
        run: o => each(o, m => ({ page: 'today', row: m.id, until: week(), seen: m.seen })).then(() => `Snoozed ${o.members.length} for a week · ${o.label}`) },
      // The Queue view and the day track (sd:6714): a live item opens, shows its builder's assignment, or gets a Fix
      // session, posted with the revision the read gave; a landed item opens.
      { id: 'today.live.open', on: 'live item', label: 'Open item', does: 'Opens the item on Tasks.', key: 'o', icon: 'arrow-up-right', risk: 'safe', executes: false, primary: () => true,
        cli: o => `sd task show ${o.item}`, run: o => shell.openItem(o.item) },
      { ...K.showAssignment({ primary: () => false, run: o => { select(`run:${o.run}`, true); return `Assignment #${o.n} shown in Details`; } }),
        id: 'today.live.assignment', on: 'live item', key: 'a', does: 'Shows the builder run that holds the item, with its assignment, in Details.', when: o => !!o.n || 'no build run holds this item' },
      FIXES[5],
      { id: 'today.landed.open', on: 'delivered item', label: 'Open item', does: 'Opens the item on Tasks.', key: 'o', icon: 'arrow-up-right', risk: 'safe', executes: false, primary: () => true,
        cli: o => `sd task show ${o.item}`, run: o => shell.openItem(o.item) },
      { id: 'today.group.read', on: 'brief group', label: 'Read', does: 'Opens Briefs.', key: 'o', icon: 'book-open', risk: 'safe', executes: false, primary: () => true,
        cli: () => 'sd reports brief list', run: go('/briefs', 'Briefs opens') },
      { id: 'today.group.seen', on: 'brief group', label: 'Mark all seen', does: 'Marks every new brief seen, one mark each, until its text grows.', key: 's', risk: 'safe',
        askFirst: o => o.members.length > BULK, consequence: o => `Marks ${o.members.length} briefs seen; each comes back when its text grows.`,
        cli: () => 'no sd verb: the dashboard writes one snooze per brief (POST /api/snooze)',
        run: o => each(o, m => ({ page: 'today', row: m.id, until: m.until, seen: m.seen })).then(() => `Seen · ${o.members.length} briefs`) },
    );
    C.register(...[...new Set([...Object.values(TYPE), 'item group'])].filter(t => t !== 'flaky head').map(brokenFix));
    // Snooze (sd:1896) on every row type that ends by time: the row leaves Today until then, and Unsnooze brings it back.
    window.snooze.register(C, { page: 'today', types: ['job', 'pull request', 'repo', 'sessions', 'collector', 'work item', 'report', 'mail thread', 'flaky head'], reread: () => load() });

    $('rows').addEventListener('click', e => {
      if (e.target.closest('.rowact')) return;
      const t = e.target.closest('[data-toggle]'), more = e.target.closest('[data-more]');
      if (t) { OPEN.has(t.dataset.toggle) ? OPEN.delete(t.dataset.toggle) : OPEN.add(t.dataset.toggle); return needsTable(); }
      if (more) { FULL.add(more.dataset.more); return needsTable(); }
      const tr = e.target.closest('tr[data-id]'); if (tr) select(tr.dataset.id, true);
    });
    // Queue (pack sd:7234): a write button acts; a repo row's name, chevron or the rest of its row opens it and closes the
    // one that was open, and a click on the open one closes it; a child row selects into Details.
    $('queue').addEventListener('click', e => {
      if (e.target.closest('.rowact')) return;
      const w = e.target.closest('button[data-act]'); if (w) { if (!w.disabled) laneAct(w); return; }
      const b = e.target.closest('button[data-build]'); if (b) { if (!b.disabled) buildAct(b); return; }
      // pack sd:8138: a box opens its repo filtered to its rows; a second press clears, another switches. No disclosure toggle.
      const t = e.target.closest('button[data-tile]');
      if (t) { if (t.disabled) return; const { repo, tile: box } = t.dataset, same = qOpen === repo && qTile && qTile.box === box;
        qOpen = repo; qTile = same ? null : { repo, box }; return queueView(); }
      const c = e.target.closest('button[data-tile-clear]'); if (c) { qTile = null; return queueView(); }
      const p = e.target.closest('tr.repo'); if (p && !e.target.closest('a, [data-copy]')) { qOpen = qOpen === p.dataset.repo ? null : p.dataset.repo; return queueView(); }
      pickRow('tr[data-id]')(e);
    });
    $('queue-pickup').addEventListener('click', async e => {
      const b = e.target.closest('button[data-pickup]'); if (!b || b.disabled || !QV.built) return;
      const on = b.dataset.pickup === 'on';
      QV.busy = true; queueView();
      try {
        const out = await shell.post('/api/queue/builds/pickup', { on, until: QV.built.pickup.until });
        qSaid = { ok: true, text: out.on ? `Auto-pickup is on until ${at(out.until)}.` : 'Auto-pickup is off.' };
      } catch (err) {
        qSaid = { ok: false, text: `Auto-pickup not turned ${on ? 'on' : 'off'}: ${err.message}` };
      }
      QV.busy = false;
      await loadQueue();
    });
    $('queue-filters').addEventListener('click', e => {
      const b = e.target.closest('button[data-f="qshow"]'); if (!b) return;
      if (!qShow.delete(b.dataset.v)) qShow.add(b.dataset.v);
      // An empty show= is every filter off (pack sd:7314): no key would turn Suggested back on at the next load.
      shell.url({ show: QSHOW.filter(v => qShow.has(v)).join(',') });
      // All items reads /api/tasks, or drops its rows; Suggested moves the ready rows All items shows (pack sd:7251) at
      // once, then reads /api/tasks for its rows' tasks when no read holds it (pack sd:7334).
      if (b.dataset.v === 'open') loadQueue();
      else if (b.dataset.v === 'suggested') { qAdopt(); if (qShow.has('suggested') && !QV.tasks) loadQueue(); } else queueView();
    });
    const showKey = new URLSearchParams(location.search).get('show');
    (showKey === null ? 'suggested' : showKey).split(',').filter(v => QSHOW.includes(v)).forEach(v => qShow.add(v));
    document.addEventListener('shell:view', e => { if (e.detail.id === 'queue' && !QV.seen) loadQueue(); });
    // pack sd:7681: the Timeline reads its Activity lanes when it first shows; the tiles pick its window, kept in ?range=.
    document.addEventListener('shell:view', e => { if (e.detail.id === 'timeline' && !AV.seen) loadActivity(); });
    $('track-range').addEventListener('click', e => {
      const b = e.target.closest('[data-range]'); if (!b || b.dataset.range === span) return;
      span = b.dataset.range;
      const u = new URL(location.href); if (span === '7d') u.searchParams.set('range', '7d'); else u.searchParams.delete('range'); history.replaceState(null, '', u);
      if (DOC) dayTrack(); else rangeTiles();
    });
    if (typeof setInterval === 'function') setInterval(() => { if (QV.seen && !document.hidden && !QV.busy && shell.view.current() === 'queue') loadQueue(); }, 30000);
    // sd:6714: a plain click on a row of the Queue view or the day track selects it, as a Needs you row's
    // does, its link included; a modifier click leaves the link to the browser, so it opens a new tab.
    const pickRow = sel => e => {
      const a = e.target.closest('a'); if (a && (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey)) return;
      const t = e.target.closest(sel); if (!t || !listed(t.dataset.id)) return;
      if (a) e.preventDefault();
      select(t.dataset.id, true);
    };
    $('track-rows').addEventListener('click', pickRow('tr[data-id]'));
    $('track-plot').addEventListener('click', pickRow('[data-id]'));
    $('intents').addEventListener('click', e => {
      const c = e.target.closest('[data-intent]'); if (!c) return;
      intent = c.dataset.intent && c.dataset.intent !== intent ? c.dataset.intent : null; intents(); applyFilter();
    });
    document.addEventListener('shell:open', e => { if (listed(e.detail)) select(e.detail, true); });
    document.addEventListener('shell:picked', e => { picked = e.detail; mark(row()); });
    $('annunciator').addEventListener('click', e => {
      const c = e.target.closest('button.cell'); if (!c) return;
      srcFilter = srcFilter === c.dataset.src ? null : c.dataset.src;
      if (srcFilter) window.shell.view.show('needs');
      document.querySelectorAll('.cell[data-src]').forEach(x => x.setAttribute('aria-pressed', x.dataset.src === srcFilter));
      if (DOC) applyFilter();
    });
    put($('rows-head'), COLS); // drawn before the read, so a failed one keeps the columns

    // The shell's Shapeshift bar (sd:5539) filters Needs you through shell:filter.
    document.addEventListener('shell:filter', e => { query = e.detail.q.toLowerCase(); if (DOC) applyFilter(); });
    // The shell walks the rows on j / k, and Esc clears the lamp and intent through clear() (sd:3525).
    window.PAGE_LIST = { rows: () => $('rows').querySelectorAll('tr[data-id]'), select: id => select(id, false),
      clear: () => !!(srcFilter || intent) && (clearSource(), true) };

    reading = shell.read({
      source: '/api/now', what: 'The Today rows', adopt, clear, draw,
      current: row,
      first: () => $('rows').querySelector('tr[data-id]')?.dataset.id,
      // ?row= on a group member opens its group, so the selection is a row on screen.
      select: id => {
        if (OTHER.has(id) || QOTHER.has(id) || isItem(id)) return select(id, false);
        const g = NEEDS.find(r => (r.members || []).some(m => m.id === id));
        if (g && !OPEN.has(g.id)) { OPEN.add(g.id); FULL.add(g.id); needsTable(); }
        return [...$('rows').querySelectorAll('tr[data-id]')].some(tr => tr.dataset.id === id) ? select(id, false) : reconcile();
      },
      unselect,
    });
    load();
  });
})();
