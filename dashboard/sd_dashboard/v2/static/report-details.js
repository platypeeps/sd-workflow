// A report's Details and commands (sd:5636): the one renderer for a reading of /api/reports's shape (reports_screen.document).
// Reports draws it for its selected row, and the HOA and Home pages' Reports views (places.js) draw it in their Details pane
// from the same document, read for the place's jobs alone, so a report reads and acts the same on all three. It loads after
// commands.js and before the page script.
//   model(doc)          one reading's rows, jobs, cadence and families: what show() and objects() read
//   mark(M, job, day)   a job's cadence mark on a day, as the Reports page's run table draws it
//   attach({ details, model, write, load, more, job }) registers the commands once and gives show(r) and objects()
//     details  the Details element; model  the last reading's model
//     write    posts a command's write, then reads again; a refused stale write loads (the page's own write)
//     load     reads again after a refused reason note
//     more     optional markup under the report: the place pages' link to Reports
//     job      the type of a job object, 'job' by default; HOA's source rows are jobs with their own Run now, so a place
//              page names another, and the report's Run now registers under it as <job>.retry
(() => {
  const { html, put } = window.markup;
  const G = { warning: '■', caution: '▲', queued: '◌', ok: '●', unknown: '▨' };
  const shq = s => `'${String(s ?? '').replace(/'/g, "'\\''")}'`; // shell-safe: single quotes, so $(), backticks and \ stay literal
  const I = n => html`<svg class="i" aria-hidden="true"><use href="#i-${n}"/></svg>`;
  const dayName = d => new Date(d + 'T12:00:00Z').toLocaleDateString('en-GB', { weekday: 'short', timeZone: 'UTC' });
  // build: a log path names the account's home; Details shows it from ~ (the design replaced one fixed home).
  const tilde = p => String(p || '').replace(/^\/(Users|home)\/[^/]+(?=\/|$)/, '~');

  function model(doc) {
    const M = { doc, read: doc.read || new Date().toISOString(), days: doc.days || [], cad: doc.cadence || {},
      jobs: Object.fromEntries((doc.jobs || []).map(j => [j.name, j])),
      families: (doc.families?.list || []).map(f => [f.key, f.label, f.icon, f.jobs]), famOf: {}, byJob: {} };
    M.families.forEach(([k, , , jobs]) => jobs.forEach(j => M.famOf[j] = k));
    // Rows: database reports.
    M.rows = (doc.reports || []).map(r => {
      // build: attention is the report's own fields.attention, not a match on its title.
      const attn = r.attention, open = r.status !== 'done';
      return { id: 'r' + r.id, n: r.id, job: r.job || 'unknown job', what: r.title, at: r.at, day: r.day || r.at.slice(0, 10), open, attn,
        act: open && attn, s: open ? (attn ? 'warning' : 'queued') : 'ok',
        detail: [r.basis, r.exit != null && !/^job exited/.test(r.basis || '') ? 'exit ' + r.exit : '', r.repeats ? `${r.repeats.count} runs` : ''].filter(Boolean).join(' · '), source: r };
    });
    M.rows.sort((a, b) => Date.parse(b.at) - Date.parse(a.at));
    M.rows.forEach(r => (M.byJob[r.job] ||= []).push(r));
    return M;
  }
  const prevOf = (M, r) => { const l = M.byJob[r.job]; return l[l.indexOf(r) + 1] || null; };

  // Cadence mark for a job on a day. build: the marks come from the job's log as reports_screen.cadence reads it; a day
  // the launchd calendar leaves out is not scheduled.
  function mark(M, job, day) {
    const c = M.cad[job], i = M.days.indexOf(day);
    if (!c || i < 0) return ['unknown', '▨', 'not read'];
    // build (sd:4965, sd:5538): a manual job is never due, nor an unscheduled day; a day it ran reads as any run does,
    // so a rerun after a failed scheduled run supersedes it.
    if (c.manual || !c.scheduled[i]) {
      const [ok, bad] = c.runs[day] || [0, 0];
      if (!ok && !bad) return ['none', '·', c.manual ? 'manual: runs when started' : 'not scheduled'];
    }
    if (!c.log || (c.from && day < c.from)) return ['unknown', '▨', 'no log yet'];
    // build: a cut read can begin partway through its first day, so that day is not read either (as the server's `last`);
    // a cut with no whole stamped line (no read_from) read no day at all.
    if (c.cut && (!c.read_from || day <= c.read_from)) return ['unknown', '▨', 'not read'];
    const [ok, bad] = c.runs[day] || [0, 0], n = ok + bad;
    // build: today is not over; a job that has not run yet today is not a gap.
    if (!n && i === M.days.length - 1) return ['none', '·', 'no run yet today'];
    if (!n) return ['caution', '▲', 'no run logged'];
    if (bad && !ok) return ['warning', '■', `${bad} failed`];
    if (bad) return ['caution', '▲', `${ok} ok, ${bad} failed`];
    return ['ok', '●', n > 1 ? `${ok} ran` : 'ran'];
  }
  const glyphClass = s => `g-${s === 'none' ? 'queued' : s}`;

  // Line diff against the previous report of the same job (LCS on lines).
  function diff(a, b) {
    const A = a.split('\n'), B = b.split('\n'), n = A.length, m = B.length;
    const L = Array.from({ length: n + 1 }, () => new Array(m + 1).fill(0));
    for (let i = n - 1; i >= 0; i--) for (let j = m - 1; j >= 0; j--) L[i][j] = A[i] === B[j] ? L[i + 1][j + 1] + 1 : Math.max(L[i + 1][j], L[i][j + 1]);
    const out = []; let i = 0, j = 0;
    while (i < n && j < m) { if (A[i] === B[j]) { out.push(['same', A[i]]); i++; j++; } else if (L[i + 1][j] >= L[i][j + 1]) out.push(['del', A[i++]]); else out.push(['add', B[j++]]); }
    while (i < n) out.push(['del', A[i++]]); while (j < m) out.push(['add', B[j++]]);
    return out;
  }
  // Run ids and timestamps change every run; mask them so the diff shows what the job said, not when.
  const norm = t => String(t || '').replace(/\d{4}-\d{2}-\d{2}T[\d:.]+(Z|[+-]\d{2}:?\d{2})?/g, '‹time›').replace(/\b\d{4}-\d{2}-\d{2} \d{2}:\d{2}(:\d{2})?/g, '‹time›').trim();

  // ---------- Commands (products/system/commands.md) ----------
  // Each row is an object; each command is declared once and the shell renders row, menu, Details and palette.
  const logOf = o => /\.log$/.test(o.source.src || '');
  // The reader (read.js) retires what a reading no longer lists: the pick is dropped and the object becomes a type no command
  // is on, so the bulk bar cannot acknowledge a report or retry a job from a stale reading.
  const objects = (M, job) => [...M.rows.map(r => Object.assign(r, { type: 'report', label: r.what })),
    // Each scheduled job is its own object; Retry acts on the job, not on the report (commands.md, one declaration per command).
    // build: the job is operations.inventory's, as Management puts it, with the revision and capability Retry posts and reads.
    // A failed run is the job's state: a pack that runs any job now allows a retry of an idle one too (sd:4965).
    ...Object.values(M.jobs).map(j => ({ id: 'job:' + j.name, type: job, label: j.name, job: j.name, service: j.service,
      failed: j.state === 'failed' || j.state === 'interrupted', exit: j.last_exit, revision: j.revision, caps: j.capabilities || {} }))];

  function attach({ details, model: now, write, load, more = () => '', job = 'job' }) {
    const shell = window.shell, C = shell.commands;
    // The acknowledge answers the report's new revision; the Undo reopens under it, so a report changed since is refused (sd:2395).
    // sd:5064: an optional reason is a comment note written just before, and the acknowledge goes under the revision it answered.
    // A refused acknowledge leaves that comment on the open report; it says why, so nothing needs taking back.
    async function acknowledge(o, v = {}) {
      let revision = o.source.revision;
      if (v.reason) {
        try { revision = (await shell.post(`/api/items/${o.n}/notes`, { body: v.reason, kind: 'comment', revision })).revision; }
        catch (err) { if (err.stale) load(); throw err; }
      }
      const out = await write(`/api/reports/${o.n}/acknowledge`, { revision });
      return { text: `Acknowledged · #${o.n}`, revision: out.revision };
    }
    // sd:5064: Resolve followup resolves the oldest open followup that holds a report, through the item routes' /resolve.
    const resolveFollowup = o => write(`/api/notes/${o.source.followups[0]}/resolve`, { revision: o.source.revision })
      .then(() => `Followup resolved · note ${o.source.followups[0]}`);
    const reopen = (o, acked) => write(`/api/reports/${o.n}/reopen`, { revision: acked.revision }).then(() => true);
    const task = on => ({ id: `${on}.task`, on, label: 'Make task', does: 'Opens Capture to file a task about this report.', key: 'k', risk: 'safe', journal: () => 'new', cli: o => `sd task add --priority 3 ${shq(o.label)}`, run: o => { shell.capture(o, '', `${on}.task`); return null; } }); // the filed task is the write
    // sd:5886: the server prints the job's log, as commands.js's Show log does, and Details shows it under the report.
    const log = on => ({ id: `${on}.log`, on, label: 'Show log', does: 'Prints the job\'s last 200 log lines in Details.', key: 'l', risk: 'safe', executes: true, when: o => logOf(o) || 'this report came from sd-db, not a job log', cli: o => window.commands.logLine(o.job),
      sends: o => `POST /api/actions/run {action: job.log, job: ${o.job}}`, run: o => window.commands.runAction(o, 'job.log', { job: o.job }) });
    C.register(
      // build: Acknowledge posts /api/reports/<n>/acknowledge with the report's revision, the route v1's form posts. It is off,
      // with the library's reason, for a report an open followup holds or whose fields cannot be read.
      // The design's risk, 'undo': reporting.reopen moves the report back to the status it was acknowledged from (sd:2395). It
      // was 'confirm' while sd-db had no verb that reopens a report (operator ruling, sd:2121).
      { id: 'report.ack', on: 'report', label: 'Acknowledge', does: 'Marks the report done; Undo puts it back.', key: 'a', icon: 'check', risk: 'undo', journal: o => o.n, bulk: true, primary: o => o.open && !o.source.login, executes: true,
        when: o => !o.open ? 'already acknowledged' : !o.source.fields_read ? `its fields cannot be read: sd reports acknowledge ${o.n} finishes it`
          : o.source.followups.length ? `an open followup holds it: sd note resolve ${o.source.followups[0]}` : true,
        fields: () => [{ name: 'reason', label: 'Reason (optional)', placeholder: 'why it can close', help: 'Written on the report as a comment just before it is acknowledged. Leave it empty for none.' }],
        cli: (o, v = {}) => `${v.reason ? `sd task note ${o.n} ${shq(v.reason)} && ` : ''}sd reports acknowledge ${o.n}`, run: acknowledge, undo: reopen },
      // sd:5064: an open followup holds the report; this resolves it, as sd note resolve does, so Acknowledge can follow. No
      // route reopens a note, so it asks first.
      { id: 'report.resolve', on: 'report', label: 'Resolve followup', does: 'Resolves the followup that holds this report.', key: 'e', icon: 'check-check', risk: 'confirm', journal: o => o.n, executes: true,
        primary: o => o.open, when: o => !o.open ? 'already acknowledged' : !o.source.followups.length ? 'no open followup holds it' : true,
        consequence: o => `Note ${o.source.followups[0]} is resolved. No dashboard control reopens it.`,
        cli: o => `sd note resolve ${o.source.followups[0] ?? '<note>'}`, sends: o => `POST /api/notes/${o.source.followups[0]}/resolve {revision}`, run: resolveFollowup },
      // build: the report opens as its row on Tasks, the item address every page uses (sd:3526).
      { id: 'report.show', on: 'report', label: 'Open item', does: 'Opens the report\'s work item.', key: 'o', risk: 'safe', journal: o => o.n, executes: false, cli: o => `sd task show ${o.n}`,
        run: o => shell.openItem(o.n) },
      log('report'), task('report'),
      // Fix (sd:3456): the shared declaration (commands.js), on a report: a debug session for it, with the report's revision.
      // sd:6964: a report whose output shows an expired login opens that login's Credentials row instead, and leads the bar.
      { ...window.commands.fix('report', { item: o => o.n, report: () => true, login: o => o.source.login, when: o => !o.open ? 'already acknowledged' : true,
        // pack sd:7783: a failed launch noted its cause on the report, so the page reads again (commands.js `again`).
        post: (o, path) => write(path, { revision: o.source.revision }), again: () => load() }), primary: o => o.open && !!o.source.login },
      // Retry (commands.js): it posts /api/jobs/<name>/retry with the job's revision; operations.inventory's capability is the
      // off reason (sd:2118).
      { ...window.commands.retry({ job: o => ({ name: o.job, service: o.service, failed: o.failed, exit: o.exit, capability: o.caps.retry }),
        run: async o => window.commands.answered(await write(`/api/jobs/${encodeURIComponent(o.job)}/retry`, { revision: o.revision }), o.job, 'retried', `Run started · ${o.job}`) }),
      ...(job === 'job' ? {} : { id: `${job}.retry`, on: job }) },
    );

    // ---------- Details ----------
    function show(r) {
      const M = now(), p = prevOf(M, r), x = r.source;
      let dff = '';
      if (p) {
        const ops = diff(norm(p.source.body), norm(x.body));
        const changed = ops.filter(o => o[0] !== 'same').length;
        dff = html`<h3>${I('git-compare')} Since the previous report · <time class="rel" datetime="${p.at}"></time></h3>${changed
          ? html`<pre class="diff" aria-label="Line diff">${ops.map(([k, t]) => html`<span class="${k}">${t || ' '}</span>`)}</pre><p class="note">${ops.filter(o => o[0] === 'add').length} lines added, ${ops.filter(o => o[0] === 'del').length} removed. Times are masked.${x.cut || p.source.cut ? ' One side is cut to its last 1400 characters, so the diff is partial.' : ''}</p>`
          : html`<p class="why">Same text as the previous report, times aside.</p>`}`;
      } else dff = html`<h3>${I('git-compare')} Since the previous report</h3><p class="why">No earlier stored report from ${r.job} in the newest ${M.doc.limit || 200}.</p>`;
      const cjob = M.jobs[r.job] ? r.job : null;
      const jobAct = cjob ? html`<h3>Job · ${cjob}</h3>${C.bar('job:' + cjob)}` : '';
      const strip = M.cad[r.job] ? html`<h3>Last 7 days</h3><p class="tally">${M.days.map(d => { const [s, g, t] = mark(M, r.job, d); return html`<span title="${d}: ${t}"><span class="${glyphClass(s)}" aria-hidden="true">${g}</span> ${dayName(d)}<span class="sr"> ${t}</span></span>`; })}</p>` : '';
      const rep = x.repeats ? `${x.repeats.count} runs, last ended ${x.repeats.last ? new Date(x.repeats.last).toISOString().slice(0, 16).replace('T', ' ') + ' UTC' : 'not recorded'}` : 'one run';
      // build: a report's open followups, and fields the store cannot parse, are named where the design had only the attention line.
      const waits = !x.fields_read ? html`<p class="why">This report's stored fields are missing or not valid JSON, so its source cannot be read. This page withholds Acknowledge: one click would close a report nobody has read. Read its log, then acknowledge report ${x.id} in a terminal: <code>sd reports acknowledge ${x.id}</code>.</p>`
        : r.open && x.followups.length ? html`<p class="why">An open followup (${x.followups.map(n => `note ${n}`).join(', ')}) holds this report. Resolve it with Resolve followup, or <code>sd note resolve ${x.followups[0]}</code>, then acknowledge.</p>` : '';
      put(details, html`<div class="report-det"><p class="kind"><span class="g-${r.s}" aria-hidden="true">${G[r.s]}</span> Database report #${x.id} · ${r.open ? (r.attn ? 'needs attention' : 'open') : 'acknowledged'}</p>
        <h2>${r.what}</h2>
        <h3>Act</h3>${C.bar(r.id)}${jobAct}
        ${r.open && r.attn ? html`<p class="why">Acknowledge closes the report, not the fault. The next failure opens a new one.</p>` : ''}${waits}
        <h3>Report source</h3>
        <dl><dt>Job</dt><dd>${x.job || 'not recorded'}</dd><dt>Started</dt><dd>${x.started ? html`<time class="rel" datetime="${x.started}"></time>` : 'not recorded'}</dd><dt>Ended</dt><dd>${x.ended ? html`<time class="rel" datetime="${x.ended}"></time>` : 'not recorded'}</dd><dt>Exit code</dt><dd>${x.exit ?? '—'}</dd>
          <dt>Source log</dt><dd><code>${tilde(x.src) || '—'}</code></dd><dt>Attention evidence</dt><dd>${x.basis || '—'}</dd><dt>Repeats</dt><dd>${rep}</dd><dt>Run</dt><dd><code>${x.run || '—'}</code></dd><dt>Recorded</dt><dd><time class="rel" datetime="${x.at}"></time></dd></dl>
        ${x.truncated ? html`<p class="why">The job wrote more than the store keeps; this report is truncated. The source log has the rest.</p>` : ''}
        ${strip}
        <h3>Report</h3>${x.cut ? html`<p class="note">Last 1400 characters shown.</p>` : ''}<div class="mailbody">${x.body || html`<span class="note">No text stored.</span>`}</div>
        ${dff}${more(r)}</div>`);
      C.select(r.id);
      shell.setContext?.(`${r.job} · report #${x.id}`);
      shell.suggest([`What changed in ${r.job} since its last good run?`, `Why did ${r.job} ${r.attn ? 'fail' : 'report'} this time?`, `Show every open report from ${M.families.find(f => f[0] === M.famOf[r.job])?.[1] || 'this family'}`]);
      details.setAttribute('data-swap', ''); requestAnimationFrame(() => details.removeAttribute('data-swap'));
    }
    return { show, objects: () => objects(now(), job) };
  }

  window.reportDetails = { model, mark, attach, G, I, dayName, glyphClass };
})();
