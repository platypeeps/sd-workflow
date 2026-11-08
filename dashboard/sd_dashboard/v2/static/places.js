// Places: the HOA and Home pages' views (Overview | Tasks | Briefs | Reports; HOA adds Alarms and Votes), a realm place's (sd:5663,
// place.js: Overview | Tasks | Briefs | Reports) and Tasks' place views.
// Data: a place view with data-api reads its own document when it is shown, /api/<place>/<view> (sd_dashboard/places.py).
// Overview is the page's own read, and so is HOA's Tasks: hoa.js draws it from /api/hoa, which the annunciator and the badge
// read anyway, with the Tasks page's Details (task-details.js, sd:5671). Which rows are a place's is decided once (places.area_of): Tasks' HOA and Home views filter on the `area` each
// /api/tasks row carries, so they hold the place pages' Tasks rows. A view that did not read is unknown with its reason, never
// an empty list. Nothing here writes; a brief's Details (brief-details.js) carry the Briefs page's commands, a report's
// (report-details.js) the Reports page's, and a task's (task-details.js) the Tasks page's. Markup is html`…` from markup.js:
// every value is escaped.
// Open | All (sd:5752): Tasks, Briefs and Reports show their open rows, and each row's primary act is its acknowledge: a
// task's Done (task-details.js, view 'place'), a brief's Seen (Today's mark, below), a report's Acknowledge (report.ack).
// All adds the week's done tasks (places.task_rows), the seen briefs or the acknowledged reports. The choice is the URL's
// ?<view>=all. HOA's Votes (pack sd:8050) shows its open votes, and All adds the closed ones. Add vote and each row's Edit
// (pack sd:8058) post /api/hoa/vote, as `sd votes add` and `sd votes edit --if-revision` write.
//
// Adapter: viewSwitch(show) is the one place a page meets the shell's view switch (shell.js view:start). The page declares
// window.PAGE_VIEWS = [{ id, label, key?, api? }] before shell.js; show(id) runs for the view shown, at shell:ready and on
// each change. The shell draws the switch into #viewswitch, hides the view-<id> panels and fires 'shell:view'.
(() => {
  const { html, put, plural } = window.markup;
  const $ = id => document.getElementById(id);

  // Called at shell:ready, after the shell's first 'shell:view': the view already shown is shown here.
  function viewSwitch(show) {
    document.addEventListener('shell:view', e => show(e.detail.id));
    if (window.shell.view.current()) show(window.shell.view.current());
  }

  const GLYPH = { ok: '●', caution: '▲', warning: '■', queued: '◌', unknown: '▨' };
  // sd:5752: the views with Open | All, each a row's open test; SHOW the choice per view, from the URL.
  const OPEN = { tasks: r => r.status !== 'done', briefs: r => !r.marked, reports: r => r.status !== 'done', votes: r => r.state === 'open' };
  const SHOW = Object.fromEntries(Object.keys(OPEN).map(v => [v, new URLSearchParams(location.search).get(v) === 'all' ? 'all' : 'open']));
  const act = id => html`<td class="act">${window.shell.commands.rowActions(id)}</td>`, ACT = [html`<span class="sr">Actions</span>`];
  const unknown = why => html`<p class="state-unknown"><b>▨ Not read.</b> ${why}</p>`;
  const day = s => String(s || '').slice(0, 10);
  // pack sd:7210: the table names its view's Open | All row (draw) as its toolbar, so the column filter joins that row.
  const table = (view, head, rows, empty) => html`<table class="ledger place-rows" data-toolbar="${view}-filters"><thead><tr>${head.map(([label, cls = '']) => html`<th scope="col" class="${cls}">${label}</th>`)}</tr></thead>
    <tbody>${rows.length ? rows : html`<tr class="empty"><td colspan="${head.length}">${empty}</td></tr>`}</tbody></table>`;

  // sd:6162, sd:6164: a realm place's Overview tile (place_tiles.py): its link, whether it answers, and a button per
  // command with that command's last run. A tile whose folder is not on this machine shows why, and its buttons run nothing.
  const t = s => s ? html`<time class="rel" datetime="${s}"></time>` : 'at a time not known';
  const ANSWER = { true: ['ok', p => `answers, ${p.status}`], false: ['queued', p => `not answering: ${p.status}`], null: ['unknown', p => p.status] };
  // sd:6387: a hosted tile's action carries its host, and three more states from the rows its host answers: pending
  // (asked, not yet answered), starting, and refused (nothing ran there, and why).
  function lastHTML(l) {
    const out = l.output ? html` <details class="pt-out"><summary>Output${l.cut ? ', the end' : ''}</summary><pre>${l.output}</pre></details>` : '';
    const on = l.host ? ` on ${l.host}` : '';
    if (l.state === 'none') return html`<span class="pt-last">Not run${on} from here yet.</span>`;
    if (l.state === 'pending') return html`<span class="pt-last g queued"><span aria-hidden="true">${GLYPH.queued}</span> Asked ${l.host} ${t(l.requested)}; it starts within five minutes.</span>`;
    if (l.state === 'starting') return html`<span class="pt-last g queued"><span aria-hidden="true">${GLYPH.queued}</span> Starting${on} since ${t(l.started)}.</span>`;
    if (l.state === 'running') return html`<span class="pt-last g queued"><span aria-hidden="true">${GLYPH.queued}</span> Running${on} since ${t(l.started)}.</span>${out}`;
    if (l.state === 'done') return html`<span class="pt-last g ${l.exit ? 'warning' : 'ok'}"><span aria-hidden="true">${GLYPH[l.exit ? 'warning' : 'ok']}</span> ${l.exit ? 'Failed' : 'Done'}${on}, exit ${String(l.exit)}, ${t(l.ended)}.</span>${out}`;
    if (l.state === 'refused') return html`<span class="pt-last g warning"><span aria-hidden="true">${GLYPH.warning}</span> Not run${on}: ${l.reason}.</span>`;
    return html`<span class="pt-last g unknown"><span aria-hidden="true">${GLYPH.unknown}</span> Not known${on}: ${l.reason}.</span>${out}`;
  }
  // pack sd:7675: an action asked, starting or running turns its button's icon (aria-busy); its last-run line says which.
  const RUNS = new Set(['pending', 'starting', 'running']);
  function tileHTML(r) {
    const [st, words] = r.probe ? ANSWER[String(r.probe.answers)] : ['', () => 'no link'];
    const off = r.here || r.host ? '' : r.why;
    // Pack sd:7536: the act line's help says what the button does; with none, the hover names the action. The command
    // is the hover's second line.
    const tip = a => `${a.help || (r.host ? `Asks ${r.host} to run the ${a.label} action in this tile's folder there.` : `Runs the ${a.label} action in this tile's folder.`)}\n${a.command}`;
    return html`<li class="place-tile" data-tile="${r.id}" data-label="${r.label}">
      <div class="pt-head"><h3>${r.label}</h3><span class="pt-st g ${st}">${st ? html`<span aria-hidden="true">${GLYPH[st]}</span> ` : ''}${words(r.probe)}</span></div>
      ${r.url ? html`<p><a class="btn quiet sm" href="${r.url}" target="_blank" rel="noopener">${window.shell.ICON('arrow-up-right')} Visit</a></p>` : ''}
      ${off ? html`<p class="why">${off}.</p>` : ''}
      ${r.host ? html`<p class="why">Runs on ${r.host}; a press takes up to five minutes to start.</p>` : ''}
      <ul class="pt-acts">${r.actions.map(a => html`<li>${off
        ? html`<button class="btn sm" type="button" data-tile-act="${a.label}" aria-disabled="true" data-tip="${off}">${a.label}</button>`
        : RUNS.has(a.last.state) ? html`<button class="btn sm" type="button" data-tile-act="${a.label}" aria-busy="true" data-tip="${tip(a)}">${window.shell.ICON('rotate-ccw')}${a.label}</button>`
        : html`<button class="btn sm" type="button" data-tile-act="${a.label}" data-tip="${tip(a)}">${a.label}</button>`} ${lastHTML(a.last)}</li>`)}</ul>
    </li>`;
  }
  // A button posts the tile's names; the server runs only the command its file names (POST /api/place/tiles). A start
  // answers at once, so the toast says it started and the view reads again: Read again shows how it ended. A hosted
  // tile's answer is the request it wrote (`requested`), which its host's follow job runs.
  async function runTile(b) {
    const li = b.closest('[data-tile]'), tile = li.dataset.tile, label = li.dataset.label, action = b.dataset.tileAct;
    if (b.getAttribute('aria-disabled') === 'true') { window.shell.toast(`${label} · ${action} not run: ${b.dataset.tip}.`); return; }
    b.setAttribute('aria-busy', 'true');
    try {
      const out = await window.shell.post('/api/place/tiles', { realm: document.body.dataset.place, tile, action });
      window.shell.toast(out.requested ? `${label} · ${action} asked of ${out.host}; it starts within about five minutes.`
        : `${label} · ${action} started. Read again shows how it ended.`);
    } catch (err) {
      window.shell.toast(err.status ? `${label} · ${action} not run: ${err.message}` : `${label} · ${action}: no answer, so whether it started is not known: ${err.message}`);
    }
    await window.places.reread('overview');
  }

  // sd:6164: a realm place's Tasks also lists the realm's open GitHub and Jira issues (places.realm_issues), each marked by
  // its source and linked to its tracker in a new tab. An issue Hide hid (sd:6175) shows under Hidden in the list's
  // Open | Hidden (sd:6701), kept as ?issues-hidden=.
  let issuesHid = null;  // read from the URL at the first draw: shell.js loads after this script
  function issuesHTML(doc) {
    if (!doc.issues) return '';
    issuesHid ??= window.shell.aside.mode('issues-hidden');
    const hidden = doc.issues.filter(r => r.hidden).length, rows = doc.issues.filter(r => window.shell.aside.shows(issuesHid, r.hidden));
    const notes = [doc.issues_reason && `Not every issue was read: ${doc.issues_reason}.`, doc.issues_store?.reason && `Hide is off: ${doc.issues_store.reason}.`].filter(Boolean);
    return html`<div class="place-issues-head"><h3>Issues</h3><div class="filters toolbar-2" id="place-issues-filters">${window.shell.aside.seg('issues-hidden', 'Hidden', issuesHid, { open: doc.issues.length - hidden, aside: hidden })}</div><p class="note">The realm's open GitHub and Jira issues assigned to you or filed by you, as Issues' Your issues lists them.</p></div>
      ${notes.map(n => html`<p class="note">${n}</p>`)}
      <table class="ledger place-issues" data-toolbar="place-issues-filters"><thead><tr><th scope="col">Source</th><th scope="col">Issue</th><th scope="col">Title</th><th scope="col" class="hide-sm">Repo</th><th scope="col" class="hide-sm">Why</th></tr></thead>
      <tbody>${rows.length ? rows.map(r => html`<tr${r.hidden ? html` class="hidden-row"` : ''}><td class="src">${r.tracker}</td>
        <td class="ref">${r.url ? html`<a href="${r.url}" target="_blank" rel="noopener">${r.ref}</a>` : r.ref}</td>
        <td class="what">${r.title}${r.hidden ? html` <span class="note">(hidden)</span>` : ''}</td><td class="hide-sm">${r.repo}</td><td class="hide-sm">${r.reason || '—'}</td></tr>`)
        : html`<tr class="empty"><td colspan="5">${hidden ? `No issue shown: ${plural(hidden, 'hidden issue')}.` : 'No open issue of yours in this realm.'}</td></tr>`}</tbody></table>`;
  }

  // Each place view: its table head, one row, the empty text and the tally.
  // The view's rail entry carries the row count (sd:5524, shell.view.auto), so a tally says only what the count does not.
  const DRAW = {
    overview: (doc, rows) => [rows.length ? html`<ul class="place-tiles">${rows.map(tileHTML)}</ul>` : '', ''],
    // sd:5635: a task row is selected in place; Details is the Tasks page's (task-details.js), with a link to Tasks.
    tasks: (doc, rows) => [html`${table('tasks', [['ID', 'num'], ['What'], ['Kind', 'hide-sm'], ['Status'], ['Due', 'hide-sm'], ACT],
      rows.map(r => html`<tr data-id="${r.id}" aria-selected="${String(String(r.id) === sel.tasks)}"><td class="ref num">sd:${r.id}</td><td class="what"><button type="button">${r.title}</button></td>
        <td class="hide-sm">${r.kind}</td><td>${r.status}</td><td class="hide-sm due">${day(r.due) || '—'}</td>${act(String(r.id))}</tr>`),
      rows.length || !doc.done?.length ? $('view-tasks')?.dataset.empty || 'No open item without a repo.'
        : `No open item. All lists the ${plural(doc.done.length, 'item')} done this week.`)}${issuesHTML(doc)}`, ''],
    // sd:5620: a brief row is selected in place; Details is the Briefs page's (brief-details.js), with a link to Briefs.
    briefs: (doc, rows) => [table('briefs', [['Day'], ['Brief'], ['Words', 'num hide-sm'], ['Lead', 'hide-sm'], ACT],
      rows.map(r => html`<tr data-id="${r.id}" aria-selected="${String(r.id === sel.briefs)}"><td class="when">${r.day}</td><td class="what"><button type="button">${r.subj}</button></td>
        <td class="num hide-sm">${r.words}</td><td class="lead hide-sm">${r.lead}</td>${act(r.id)}</tr>`),
      rows.length || !doc.rows.length ? 'No brief is filed for this place.' : `No unseen brief. All lists the ${plural(doc.rows.length, 'seen brief')}.`), ''],
    // sd:5636: a report row is selected in place too; Details is the Reports page's (report-details.js), with a link to Reports.
    reports: (doc, rows) => {
      const state = r => r.status === 'done' ? 'ok' : r.attention ? 'warning' : 'queued';
      return [table('reports', [[html`<span class="sr">State</span>`], ['Job'], ['Report'], ['Status', 'hide-sm'], ['When'], ACT],
        rows.map(r => html`<tr data-id="r${r.id}" aria-selected="${String('r' + r.id === sel.reports)}"><td class="g ${state(r)}"><span aria-hidden="true">${GLYPH[state(r)]}</span><span class="sr">${state(r)}</span></td>
          <td class="job">${r.job}</td><td class="what"><button type="button">${r.title}</button></td><td class="hide-sm">${r.status}</td><td class="when">${day(r.at)}</td>${act('r' + r.id)}</tr>`),
        rows.length || !doc.rows.length ? `No report from ${doc.jobs.join(', ')} among the newest ${doc.limit}.`
          : `No open report from ${doc.jobs.join(', ')}. All lists the ${plural(doc.rows.length, 'acknowledged report')}.`),
        `${plural(doc.rows.filter(r => r.status !== 'done').length, 'open report')} · ${doc.jobs.length} jobs`];
    },
    // pack sd:8050: a vote row; its act is Edit (pack sd:8058). The tally groups the directors by answer (sd_db.votes.parse_votes); a cell
    // it could not read shows as written.
    votes: (doc, rows) => {
      const glyph = r => r.state === 'open' ? 'queued' : 'ok';
      const tally = r => r.tally ? ['yes', 'no', 'abstain', 'absent', 'none'].map(a => [a, Object.keys(r.tally).filter(n => r.tally[n] === a)])
        .filter(([, names]) => names.length).map(([a, names]) => `${a[0].toUpperCase()}${a.slice(1)}: ${names.join(', ')}`).join(' · ') : r.votes || '—';
      const open = doc.rows.filter(OPEN.votes).length;
      doc.rows.forEach(r => window.shell.commands.put({ ...r, id: `vote:${r.key}`, type: 'vote', label: r.key, belongs: doc.repo }));
      window.shell.commands.put({ id: 'votes', type: 'vote list', label: 'Board votes', belongs: doc.repo });
      return [table('votes', [[html`<span class="sr">State</span>`], ['Vote', 'hide-sm'], ['Date'], ['Motion'], ['Type', 'hide-sm'], ['Moved / seconded', 'hide-sm'], ['Votes'], ['Outcome'], ['Source', 'hide-sm'], ACT],
        rows.map(r => html`<tr><td class="g ${glyph(r)}"><span aria-hidden="true">${GLYPH[glyph(r)]}</span><span class="sr">${r.state}</span></td>
          <td class="ref hide-sm">${r.key}</td><td class="when">${r.date || '—'}</td><td class="what">${r.topic ? html`<b>${r.topic}</b>: ` : ''}${r.motion}</td>
          <td class="hide-sm">${r.type || '—'}</td><td class="hide-sm">${[r.moved, r.seconded].filter(Boolean).join(' / ') || '—'}</td>
          <td>${tally(r)}</td><td>${r.outcome || '—'}</td><td class="hide-sm">${r.source || '—'}</td>${act(`vote:${r.key}`)}</tr>`),
        rows.length || !doc.rows.length ? 'No vote is recorded for the hoa checkout.'
          : `No open vote. All lists the ${plural(doc.rows.length, 'closed vote')}.`),
        `${plural(open, 'open vote')} · ${doc.rows.length} in all`];
    },
  };

  // sd:5481: a click anywhere on a row opens what its link opens; a click on the link itself, or with a modifier, stays the
  // browser's. sd:5620, sd:5636, sd:5635: a row with an id (a brief, a report or a task) is selected here instead, and its
  // Details fill the pane.
  document.addEventListener('click', e => {
    const run = e.target.closest('[data-tile-act]');
    if (run) { runTile(run); return; }
    const hb = e.target.closest('button[data-aside="issues-hidden"]');
    if (hb) {
      issuesHid = hb.dataset.v;
      const q = new URLSearchParams(location.search), v = window.shell.aside.param(issuesHid);
      if (v) q.set('issues-hidden', v); else q.delete('issues-hidden');
      window.shell.url(q.toString());
      if (RAW.tasks?.state === 'read') draw('tasks'); return;
    }
    // A view that reads its own document (data-api) has its Open | All here; HOA's Tasks is hoa.js's.
    const f = e.target.closest('.place-view[data-api] [data-f]');
    if (OPEN[f?.dataset?.f]) { show(f.dataset.f, f.dataset.v); return; }
    const tr = e.target.closest('.place-rows tbody tr');
    if (tr?.dataset?.id && ROWS[shownView()]?.has(tr.dataset.id)) { if (!e.target.closest('a, .rowact')) pick(tr.dataset.id, true); return; }
    const a = tr?.querySelector('a[href]');
    if (!a || e.target.closest('a, button') || e.button || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey || getSelection().toString()) return;
    location.assign(a.href);
  });

  // ---------- Selection in place (sd:5620) ----------
  // A view whose rows have a shared Details renderer (DETAILS) puts each row as a command object, selects it on a click, on
  // j / k and on Enter (the shell's grammar, through PAGE_LIST), and draws its Details in the pane. ROWS holds the view's
  // rows by id from its last read; PUT the ids of the objects it put; sel the selected id per view. An object the next read
  // no longer puts is put again as not listed, so no command runs on it, and Details says a row gone is gone.
  // A renderer is { rows(doc), objects(rows), show(row), forget(), key? }: rows(doc) are the read's rows, as Details reads them,
  // by key(row), else by id.
  const ROWS = {}, PUT = {}, sel = {}, RAW = {}, DETAILS = {};
  const shownView = () => window.shell?.view?.current?.();
  const details = () => $('details');
  // The Briefs page's Details (brief-details.js), with a link that opens the brief on Briefs.
  // sd:5752: Seen is Today's (today.js): the Today snooze under the brief's Today row id (`mark`) and fingerprint
  // (`seen`), until it changes (snooze.js FOREVER), so a brief seen here or on Today is seen in both. Not seen clears it;
  // each is the other's Undo. Registered before the Briefs page's commands, so Seen, not Read, is an open row's act.
  function briefs(view) {
    const B = window.briefDetails; if (!B) return null;
    // pack sd:7852: a bulk Seen reads the view again once, after the group (shell.afterBulk).
    const mark = (o, until) => window.shell.post('/api/snooze', { page: 'today', row: o.mark, until, seen: o.seen })
      .then(() => window.shell.afterBulk(`places:${view}`, () => window.places.reread(view)));
    const cli = () => 'no sd verb: the dashboard writes the Today snooze (POST /api/snooze)';
    window.shell.commands.register(
      { id: 'brief.seen', on: 'brief', label: 'Seen', does: 'Marks the brief seen here and on Today until it changes; Undo clears it.', icon: 'check', risk: 'undo', bulk: true,
        primary: o => !o.marked, when: o => !o.marked || 'already seen', cli, run: o => mark(o, 'forever').then(() => `Seen · ${o.label}`), undo: o => mark(o, null) },
      { id: 'brief.unseen', on: 'brief', label: 'Not seen', does: 'Clears the Seen mark here and on Today; Undo marks it again.', icon: 'eye', risk: 'undo',
        when: o => o.marked || 'not marked seen', cli, run: o => mark(o, null).then(() => `Not seen · ${o.label}`), undo: o => mark(o, 'forever') });
    const d = B.attach({ details: details(), reread: () => window.places.reread(view), read: () => RAW[view]?.read || new Date().toISOString(),
      watch: () => RAW[view]?.watchdog?.available ? 'read' : (RAW[view]?.watchdog?.reason || 'no watchdog reader'),
      more: b => html`<p class="why"><a href="/briefs?row=${encodeURIComponent(b.id)}">Open on Briefs</a>: the cadence strip and every source.</p>` });
    return { rows: doc => doc.rows, objects: rows => rows.map(b => ({ ...B.object(b), mark: b.mark, seen: b.seen, marked: b.marked })), show: d.show, forget: d.forget };
  }
  // The Reports page's Details (report-details.js), read from the view's `details`: the Reports page's document for the
  // place's jobs. A write posts, then reads the view again; a refused stale one reads it again too, as Reports does.
  function reports(view) {
    const R = window.reportDetails; if (!R) return null;
    let M = R.model({});
    const again = () => window.places.reread(view);
    const write = async (path, body) => {
      let out;
      try { out = await window.shell.post(path, body); } catch (err) { if (err.stale) again(); throw err; }
      await again();
      return out;
    };
    // HOA's source rows are 'job' objects with HOA's own Run now (hoa.js), so a report's job is a 'report-job' here.
    const d = R.attach({ details: details(), model: () => M, write, load: again, job: 'report-job',
      more: r => html`<p class="why"><a href="/reports?row=${encodeURIComponent(r.id)}">Open on Reports</a>: the run cadence, the family lamps and every report.</p>` });
    return { rows: doc => (M = R.model(doc.details || {})).rows, objects: () => d.objects(), show: d.show, forget: () => {} };
  }
  // The Tasks page's Details (task-details.js), from the view's rows, which are /api/tasks rows (places.tasks). A write
  // folds into its row, then reads the view again; a refused one reads it again too, as Tasks does.
  function tasks(view) {
    const TD = window.taskDetails; if (!TD) return null;
    let T = [];
    const shown = () => shownView() === view ? sel[view] ?? null : null, api = () => window.PAGE_VIEWS?.find(x => x.id === view)?.api;
    const d = TD.attach({ details: details(), rows: () => T, selected: shown, read: () => RAW[view]?.read, view: () => 'place',
      render: () => { if (shown() != null) d.renderDetails(); }, reread: () => window.places.reread(view),
      load: () => window.places.reread(view).then(() => RAW[view]?.state === 'read'), select: (key, open) => pick(key, open), lead: () => !!RAW[view]?.lead,
      foot: () => RAW[view]?.read ? html`Rows read <time class="rel" datetime="${RAW[view].read}"></time> from ${api()}.` : '',
      more: t => html`<p class="why"><a href="${window.shell.itemHref(t.id)}">Open on Tasks</a>: the board, the matrix and every task.</p>` });
    // sd:5752: the week's done rows are objects too, so a row Done just moved out of Open still has its Undo.
    return { key: t => t.key, rows: doc => { T = [...doc.rows || [], ...doc.done || []].map(TD.shape); d.adopt(T); return T; }, objects: rows => rows.map(d.objectOf),
      show: t => { window.shell.commands.select(t.key); d.readDet(t.id); d.renderDetails(); }, forget: () => {} };
  }
  // pack sd:8058: Add vote and Edit, one form each (the shell's confirm fields), posting /api/hoa/vote (places.write_vote).
  // Add sends the fields typed; Edit sends only the fields changed, with the revision the view read, so a vote changed
  // since is refused as stale (409) with nothing written. State "from the outcome" sends none: the outcome sets it.
  // Each write reads the view again, a refused stale one too.
  function votes() {
    const S = window.shell, TEXT = [['date', 'Date', 'YYYY-MM-DD'], ['topic', 'Topic', 'the subject, a few words'], ['motion', 'Motion', 'as worded'],
      ['type', 'Type', 'meeting or email vote'], ['moved', 'Moved by'], ['seconded', 'Seconded by'], ['votes', 'Votes', 'Ada, Ben Yes; Cy No'],
      ['outcome', 'Outcome', 'adopted, failed, or pending: why'], ['follow_up', 'Follow-up'], ['source', 'Source', 'where the record is'], ['notes', 'Notes']];
    const state = { name: 'state', label: 'State', options: [{ value: '', label: 'From the outcome' }, { value: 'open', label: 'Open' }, { value: 'closed', label: 'Closed' }] };
    const fields = o => [...TEXT.map(([name, label, placeholder]) => ({ name, label, value: o?.[name] || '', placeholder: placeholder || '' })), state];
    const typed = (o, v) => Object.fromEntries(Object.entries(v || {}).filter(([k, x]) => k !== 'key' && (k === 'state' ? x : x !== (o?.[k] || ''))));
    const flags = f => Object.entries(f).map(([k, x]) => ` --${k.replace('_', '-')} ${S.shq(x)}`).join('');
    const send = async (body, said) => {
      try { await S.post('/api/hoa/vote', body); } catch (err) { if (err.stale) await window.places.reread('votes'); throw err; }
      await window.places.reread('votes');
      return said;
    };
    S.commands.register(
      { id: 'vote.add', on: 'vote list', label: 'Add vote', icon: 'plus', does: 'Adds a board vote to the workflow database.', risk: 'safe', executes: true, sends: () => 'POST /api/hoa/vote',
        fields: () => [{ name: 'key', label: 'Key', required: true, placeholder: 'such as CI-2' }, ...fields(null)],
        cli: (o, v) => `sd votes add ${S.shq(v?.key || 'KEY')}${flags(typed(null, v))} --belongs-to ${S.shq(o.belongs || 'PATH')}`,
        run: (o, v) => send({ key: v.key, revision: null, fields: typed(null, v) }, `Vote added · ${v.key}`) },
      { id: 'vote.edit', on: 'vote', label: 'Edit', icon: 'pen-line', does: 'Changes the vote\'s fields; refused if it changed since this read.', risk: 'safe', executes: true, primary: () => true,
        sends: () => 'POST /api/hoa/vote', fields,
        cli: (o, v) => `sd votes edit ${S.shq(o.key)} --if-revision ${o.revision}${flags(typed(o, v))} --belongs-to ${S.shq(o.belongs || 'PATH')}`,
        run: (o, v) => { const f = typed(o, v); return Object.keys(f).length ? send({ key: o.key, revision: o.revision, fields: f }, `Vote edited · ${o.key}`) : `Nothing changed · ${o.key}`; } });
    S.commands.put({ id: 'votes', type: 'vote list', label: 'Board votes' });
  }
  // The shell's picked rows, so a retired row's pick is dropped first, as read.js retires: the shell refuses to toggle a
  // pick on a type no bulk command is on, and the bulk bar would run on the retired object.
  let picks = [];
  document.addEventListener('shell:picked', e => { picks = e.detail || []; });
  function adopt(view, doc) {
    const D = DETAILS[view]; if (!D) return;
    const C = window.shell.commands, before = PUT[view] || new Set(), rows = D.rows(doc), objs = D.objects(rows);
    RAW[view] = doc; ROWS[view] = new Map(rows.map(r => [(D.key || (x => x.id))(r), r])); PUT[view] = new Set(objs.map(o => o.id));
    const gone = [...before].filter(id => !PUT[view].has(id));
    picks.filter(id => gone.includes(id)).forEach(id => C.pick(id));
    gone.forEach(id => C.put({ id, type: 'not listed', label: `${C.get(id)?.label || id} (no longer listed)` }));
    objs.forEach(o => C.put(o));
  }
  function mark(view) {
    document.querySelectorAll(`#${view}-rows tbody tr[data-id]`).forEach(tr => tr.setAttribute('aria-selected', String(tr.dataset.id === sel[view])));
  }
  function pick(id, open) {
    const view = shownView(), r = ROWS[view]?.get(id), D = DETAILS[view]; if (!r || !D) return;
    sel[view] = id; mark(view); D.show(r);
    if (open) window.shell.openPane('tab-details');
  }
  // After a read: the selected row again, or the ?row= the page opened with; a row gone says so in Details. A read that
  // failed says nothing about the row: it stays selected for the next read, runs no command, and Details says it is unknown.
  function settle(view, first) {
    const D = DETAILS[view]; if (!D || view !== shownView()) return;
    const want = sel[view] ?? (first ? window.shell.row?.() : null);
    if (want != null && ROWS[view].has(want)) { pick(want, false); return; }
    const read = RAW[view]?.state === 'read';
    if (sel[view] != null) { D.forget(); window.shell.commands.select(null); put(details(), read ? html`<p class="why">The selected row is not in the last read of ${view}.</p>`
      : html`<p class="why">The ${view} were not read again, so the selected row is not known: ${RAW[view]?.reason || 'no reason given'}.</p>`); }
    if (read || sel[view] == null) sel[view] = null;
  }
  // The shell walks rows through PAGE_LIST (shell.js walk). The page's own list stays, and the shown place view's rows
  // follow it: a row j / k reaches in a hidden view is skipped by the shell.
  function list() {
    const own = window.PAGE_LIST, owns = id => !!ROWS[shownView()]?.has(id) && !!DETAILS[shownView()];
    window.PAGE_LIST = { ...own,
      rows: () => [...(own?.rows?.() || []), ...document.querySelectorAll('.place-rows tbody tr[data-id]')],
      current: () => DETAILS[shownView()] ? sel[shownView()] ?? null : own?.current?.() ?? null,
      select: (id, el) => owns(id) ? pick(id, false) : own?.select?.(id, el) };
    document.addEventListener('shell:open', e => { if (owns(e.detail)) pick(e.detail, true); });
  }

  const seen = {}, loaded = {};
  async function load({ id: view, api }) {
    const box = $(`${view}-rows`), tally = $(`${view}-tally`), mine = seen[view] = (seen[view] || 0) + 1;
    put(box, html`<p class="note">Reading ${view}…</p>`); put(tally, html``);
    let doc;
    try { doc = await window.shell.getJSON(api); } catch (e) { doc = { state: 'unknown', reason: `${api} did not answer: ${e.message}`, rows: [] }; }
    if (mine !== seen[view]) return;
    if (doc.state !== 'read') { adopt(view, { ...doc, rows: [] }); put(box, unknown(doc.reason)); settle(view, !loaded[view]++); return; }
    adopt(view, doc); RAW[view] = doc;
    draw(view);
    settle(view, !loaded[view]++);
  }
  // A read view's rows: the open ones, or with All every row it read (sd:5752), under the filter bar's Open | All.
  function draw(view) {
    const doc = RAW[view], box = $(`${view}-rows`), tally = $(`${view}-tally`), open = OPEN[view];
    const every = view === 'tasks' ? [...doc.rows, ...doc.done || []] : doc.rows, rows = !open || SHOW[view] === 'all' ? every : every.filter(open);
    const seg = (v, word, n) => ({ v, pressed: SHOW[view] === v, title: `${word}: ${n}`, label: html`${word} <span class="n">${String(n)}</span>` });
    // pack sd:7210: one filter row, as HOA's Tasks: Open | All, then the table's column filter. No .fbar: it takes the row's width.
    const bar = open ? html`<div class="filters toolbar-2" id="${view}-filters" role="group" aria-label="Filters">${window.shell.list.fseg(view, 'Shown', [seg('open', 'Open', every.filter(open).length), seg('all', 'All', every.length)])}</div>` : '';
    const [body, count] = DRAW[view](doc, rows);
    put(box, html`${doc.reason ? html`<p class="note">${doc.reason}</p>` : ''}${bar}${body}`);
    put(tally, count ? html`<span>${count}</span>` : html``);
  }
  // Open | All: the choice goes in the URL (?<view>=all; Open is the default and leaves none), and the view draws again.
  function show(view, v) {
    if (!['open', 'all'].includes(v) || SHOW[view] === v) return;
    SHOW[view] = v;
    const p = new URLSearchParams(location.search);
    if (v === 'all') p.set(view, 'all'); else p.delete(view);
    window.shell.url(p);
    if (RAW[view]?.state === 'read') draw(view);
  }

  // A place page: <body data-place="hoa|home|<realm id>"> and one block per view, id="view-<id>" with data-place-view, its data-label,
  // and data-api when the view reads its own document. A view reads each time it shows, not only the first ('shell:view'
  // first), since nothing else reads it again. The wall display (data-kiosk on the root element, home-kiosk.js) stays on
  // the Overview: it declares no views.
  const blocks = [...document.querySelectorAll('[data-place-view]')];
  function place() {
    if (blocks.some(b => b.dataset.placeView === 'briefs')) DETAILS.briefs = briefs('briefs');
    if (blocks.some(b => b.dataset.placeView === 'reports')) DETAILS.reports = reports('reports');
    // HOA's Tasks is hoa.js's table from /api/hoa, which attaches task-details.js itself (sd:5671): only a Tasks view that reads its rows.
    if (blocks.some(b => b.dataset.placeView === 'tasks' && b.dataset.api)) DETAILS.tasks = tasks('tasks');
    if (blocks.some(b => b.dataset.placeView === 'votes')) votes();
    Object.keys(DETAILS).forEach(v => { if (!DETAILS[v]) delete DETAILS[v]; });
    list();
    viewSwitch(id => {
      blocks.forEach(b => { b.hidden = b.dataset.placeView !== id; });
      const v = window.PAGE_VIEWS.find(x => x.id === id);
      if (v?.api) load(v);
    });
  }

  // reread(view): read a place view again after a write its Details made (a brief's Publish, a report's Acknowledge).
  const reread = view => { const v = window.PAGE_VIEWS?.find(x => x.id === view); return v?.api ? load(v) : Promise.resolve(); };
  // again(): Read again (sd:5638) reads the shown place view again; a view without its own read has nothing to read.
  const again = () => reread(window.shell.view.current());
  window.places = { viewSwitch, place, reread, again };
  // Top level: places.js loads before shell.js, which reads PAGE_VIEWS when it loads.
  if (document.body.dataset.place && !document.documentElement.hasAttribute('data-kiosk')) {
    window.PAGE_VIEWS = blocks.map(b => ({ id: b.dataset.placeView, label: b.dataset.label, ...(b.dataset.api ? { api: b.dataset.api } : {}) }));
    document.addEventListener('shell:ready', place, { once: true });
  }
})();
