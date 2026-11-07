// Shared command declarations (sd:3524): one declaration per command, as products/system/commands.md asks. A page that
// lists a job, a work item, an assignment, a pull request or a collector registers these, not a copy. It loads before the
// page script, as snooze.js does; the page calls them once shell.js has made shell.commands.
// Each takes what differs per page: how a row names its facts, and the write, which goes through the page's own reader
// (a reread after a landed write, a load after a refused one). The id, label, key, risk, when, consequence, CLI line and
// toast text are written here once, so a change lands on every page together. tests/test_v2_commands.py fails a command
// id declared in two page scripts.
(() => {
  // A job write's 200 carries launchctl's answer (operations._act): only `accepted` sent the request; `failed` sent nothing;
  // anything else, or an answer with no request, is not known (sd:3388).
  function answered(out, name, verb, done) {
    const r = out && out.request || {};
    return r.status === 'accepted' ? done : r.status === 'failed' ? `${name} not ${verb}: ${r.message}`
      : `Not known whether ${name} was ${verb}: ${r.message || 'the answer named no outcome'}`;
  }

  // Fix (sd:3456): a herdr workspace in the item's checkout with claude on it, a builder session for a task and a debug
  // session for a report. It asks first, naming that, and posts /api/items/<id>/fix with the row's revision through
  // post(o, path), which answers the server's reply. A post with no answer may have started a session, so it says that. No Undo:
  // a session runs on. `item(o)` is the item id; `report(o)` is true for a report.
  // pack sd:7783: `again(o)` reads the page again after a launch that may have noted its cause on the item (`noted`), so
  // the page's next press carries the moved revision; Fix, Grill, Apply answers and Apply review each take it.
  // sd:6964: `login(o)` is a report's expired login (job_failures.login): no session can sign in for the operator, so Fix
  // then opens that login's row on Health > Credentials, where Sign in is, and asks nothing first.
  function fix(on, { item, report, login = () => null, when = () => true, post, again = () => {} }) {
    return { id: `${on}.fix`, on, label: 'Fix', does: o => login(o) ? 'Opens the expired login\'s row on Health, where Sign in is.' : report(o) ? 'Asks first, then opens a Claude session (herdr) to fix this report.' : 'Asks first, then opens a Claude session (herdr) to build this item.', key: 'f', risk: 'safe', askFirst: o => !login(o), executes: true, icon: 'bot', when,
      cli: o => login(o) ? `no CLI: opens ${login(o).href}` : 'no sd verb: the dashboard runs local-herdr/herdr.sh launch',
      sends: o => login(o) ? `GET ${login(o).href}` : `POST /api/items/${item(o)}/fix {revision}`,
      consequence: o => report(o)
        ? `Opens a Claude session on report #${item(o)} in herdr. It finds the cause, fixes it, and acknowledges the report after a clean run.`
        : `Opens a Claude session on #${item(o)} in herdr, in the item's repo. It builds the change in its own folder and sends it to the merge queue.`,
      run: o => login(o) ? (location.href = login(o).href, `Health opens on ${login(o).row ? login(o).row.replace(/^cred:/, '') : 'Credentials'}`) : Promise.resolve().then(() => post(o, `/api/items/${item(o)}/fix`)).then(
        out => `Fix started · #${item(o)} · ${out.fix.session} session in herdr pane ${out.fix.pane}`,
        err => { if (noted(err)) again(o); throw err.status ? err : new Error(`no answer (${err.message}), so whether a session started is not known; look in herdr before you press Fix again`); }) };
  }

  // pack sd:7783: a failed launch (5xx) writes a comment note naming its cause (fix_session, pack sd:7756), and a post
  // with no answer may have written one, so either moved the item's revision. A 4xx refusal wrote nothing.
  const noted = err => !err?.status || err.status >= 500;

  // Fix on a repository's Dependabot alerts (sd:6685), on Health's Dependencies rows and GitHub's Dependabot groups
  // (pack sd:7515): files the repo's item under dependabot:<slug>, or reuses the open one, then starts claude on it through
  // fix(). The brief: land an open Dependabot PR through sd-ship, else bump the dependency in a worktree; never re-enable
  // Actions. The object names slug, repo_path, grave and facts { Repository, Shown }, as both pages' rows carry them.
  function dependabotFix() {
    const inner = fix('dependabot alerts', { item: o => o.filed ?? '<filed>', report: () => false,
      post: (o, path) => window.shell.post(path, { revision: o.filedRevision }) });
    return { ...inner, when: o => !!o.slug || 'no github.com remote: there is no alerts page to fix from',
      does: 'Asks first, files a task in this repo, then opens a Claude session to fix it.',
      sends: o => `POST /api/items {ref: dependabot:${o.slug}}, then POST /api/items/<id>/fix {revision}`,
      consequence: o => `Files a task in ${o.facts.Repository} for ${o.slug}'s ${o.facts.Shown} open Dependabot alerts (reused while one is open), `
        + 'then opens a Claude session there (herdr): it merges an open Dependabot pull request, or updates the dependency itself.',
      run: o => window.shell.post('/api/items', { title: `Dependabot: ${o.facts.Shown} open alerts in ${o.slug}`, repo: o.repo_path, ref: `dependabot:${o.slug}`,
        body: `${o.facts.Shown} open Dependabot alerts in ${o.slug}, ${o.grave} critical or high. Land an open Dependabot PR through sd-ship if one exists; `
          + 'else bump the dependency in a worktree. Never re-enable Actions.' })
        // pack sd:7756: the item is filed whatever the launch answers, so a failed launch names it; the server's words
        // say whether a note on it keeps the cause.
        .then(filed => inner.run({ ...o, filed: filed.item.id, filedRevision: filed.revision }).catch(err => {
          throw Object.assign(new Error(`filed sd:${filed.item.id}, but ${err.message}`), { status: err.status, stale: err.stale }); })) };
  }

  // Grill and Apply answers on a piece (pack sd:6700): Fix with grill: ask or apply. Ask starts claude to write the piece's
  // grill questions as question notes; apply starts claude to edit the draft to follow each ruling, uncommitted. `ready(o)`
  // is true or why not; `post(o, path, body)` answers the server's reply.
  const GRILL = {
    ask: { label: 'Grill', does: 'Asks first, then has Claude write 5 to 10 questions about the draft.',
      consequence: n => `Opens a Claude session on #${n} in the piece's repo (herdr). It reads the draft and writes 5 to 10 questions. You answer them here in Details.` },
    apply: { label: 'Apply answers', does: 'Asks first, then has Claude edit the draft to match your answers.',
      consequence: n => `Opens a Claude session on #${n} in the piece's repo (herdr). It edits the draft to match each answer. The edit stays uncommitted for you to read; nothing is published.` },
  };
  function grill(on, step, { item, ready, post, again = () => {} }) {
    const g = GRILL[step];
    return { id: `${on}.${step === 'ask' ? 'grill' : 'apply-answers'}`, on, label: g.label, does: g.does,
      icon: 'bot', risk: 'safe', askFirst: true, executes: true, when: ready,
      cli: () => 'no sd verb: the dashboard runs local-herdr/herdr.sh launch',
      sends: o => `POST /api/items/${item(o)}/fix {grill: "${step}", revision}`,
      consequence: o => g.consequence(item(o)),
      run: o => Promise.resolve().then(() => post(o, `/api/items/${item(o)}/fix`, { grill: step })).then(
        out => `${g.label} started · #${item(o)} · herdr pane ${out.fix.pane}`,
        err => { if (noted(err)) again(o); throw err.status ? err : new Error(`no answer (${err.message}), so whether a session started is not known; look in herdr before you press ${g.label} again`); }) };
  }

  // Apply review (pack sd:6699): Fix with apply: adversarial. claude edits the draft for each adversarial.md finding or
  // rejects it with a reason, and records each there; the edit stays uncommitted and nothing is published. `ready(o)` is
  // true or why not; `post(o, path, body)` answers the server's reply.
  function applyReview(on, { item, ready, post, again = () => {} }) {
    return { id: `${on}.apply-review`, on, label: 'Apply review', does: 'Asks first, then has Claude fix the draft for each review finding.',
      icon: 'bot', risk: 'safe', askFirst: true, executes: true, when: ready,
      cli: () => 'no sd verb: the dashboard runs local-herdr/herdr.sh launch',
      sends: o => `POST /api/items/${item(o)}/fix {apply: "adversarial", revision}`,
      consequence: o => `Opens a Claude session on #${item(o)} in the piece's repo (herdr). For each review finding, it fixes the draft or says why not, and records each answer with the review. The edit stays uncommitted for you to read; nothing is published.`,
      run: o => Promise.resolve().then(() => post(o, `/api/items/${item(o)}/fix`, { apply: 'adversarial' })).then(
        out => `Apply review started · #${item(o)} · herdr pane ${out.fix.pane}`,
        err => { if (noted(err)) again(o); throw err.status ? err : new Error(`no answer (${err.message}), so whether a session started is not known; look in herdr before you press Apply review again`); }) };
  }

  // Run now (jobs.retry): `sd jobs retry`, which sends the kickstart; the run-now pack allows an idle job's run too (sd:4982).
  // `job(o)` names the row's facts: { name, service, failed, exit,
  // capability }; capability is operations.inventory's retry answer, and its reason is the off text. Exit 127 is "command
  // not found": Retry is not the row's act, and it asks first, naming the exit, so a path fixed outside the dashboard can be
  // tested (sd:3392). run(o) is the page's write; it words the answer with answered().
  function retry({ job, run }) {
    return { id: 'jobs.retry', on: 'job', label: 'Run now', does: 'Starts the job now; after a failure, this reruns it.', key: 't', icon: 'play', risk: 'safe', bulk: true,
      primary: o => !!job(o).failed && job(o).exit !== 127, busy: o => !!job(o).running, // pack sd:7675
      when: o => { const j = job(o), c = j.capability; return !c ? (j.failed || 'no failed run to retry') : c.allowed || c.reason || 'launchd refuses a retry now'; },
      askFirst: o => job(o).exit === 127,
      consequence: o => job(o).exit === 127 ? `${job(o).name}'s last run ended with exit 127: command not found. A retry tests the path, and fails the same way unless the path was fixed.` : '',
      cli: o => `sd jobs retry ${job(o).name}`, sends: o => job(o).service ? `launchctl kickstart ${job(o).service}` : job(o).sends || '',
      run };
  }

  // Run (sd:5886): a button that gave a line to copy posts /api/actions/run {action, ...ids}; the server checks the ids against
  // its own rows and runs a fixed command from its registry (run_actions.py), never a line from the page. The answer
  // stays with the row while the page is open, and Details shows it after the row's facts: the label, the line, the exit
  // and the output's tail. A 4xx ran nothing; a 504 ran past its bound, so whether a write landed is not known. `o` is
  // the row the output belongs to. It answers the toast text.
  const RAN = new Map();
  let watched = null;
  function drawRan(id) {
    const details = document.getElementById('details'), had = details?.querySelector('.ran');
    id = id || window.shell.commands.selected?.()?.id;
    if (!details || had && had.dataset.obj === id) return;
    had?.remove();
    const r = id && RAN.get(id); if (!r) return;
    const { html, put } = window.markup;
    // sd:6957: a login's URL (and device code) shows here to copy into the right browser profile; the page opens none.
    const link = r.url ? html`<p class="why">Open on this machine, in the browser profile for ${r.account}: <a href="${r.url}" rel="noopener noreferrer">${r.url}</a> <button class="icon-btn" type="button" data-copy="${r.url}" aria-label="Copy the URL">Copy</button>${r.code ? html` · code <code>${r.code}</code> <button class="icon-btn" type="button" data-copy="${r.code}" aria-label="Copy the code">Copy</button>` : ''}. After consent, Probe again reads this login.</p>` : '';
    put(details, html`<section class="ran" data-obj="${id}" aria-live="polite"><h3>${r.label}</h3><p class="why">Exit ${r.exit} · <code>${r.line}</code>${r.cut ? ' · the last part shown' : ''}</p>${link}<pre class="code">${r.output || '(no output)'}</pre></section>`, 'append');
  }
  // The output shows at once, for a row or for an object no row selects (Scan's use surfaces); a later Details for the
  // selected row draws that row's output again, or none.
  function runAction(o, action, ids = {}) {
    const details = document.getElementById('details');
    // The page draws Details again on a select or a reread; the observer adds the row's output back, and skips its own.
    const own = recs => recs.every(x => [...x.addedNodes, ...x.removedNodes].every(n => n.classList?.contains('ran')));
    if (details && watched !== details && typeof MutationObserver === 'function') { watched = details; new MutationObserver(recs => own(recs) || drawRan()).observe(details, { childList: true }); }
    return window.shell.post('/api/actions/run', { action, ...ids }).then(r => {
      RAN.set(o.id, r); details?.querySelector('.ran')?.remove(); drawRan(o.id); window.shell.openPane('tab-details');
      return `${r.label} · exit ${r.exit} · the output is in Details`;
    }, err => { throw err.status === 504 ? new Error(`${err.message} Whether it finished is not known.`) : err; });
  }

  // Show log (sd:5886): the server runs the cron-jobs wrapper for an installed job and Details shows the log's last 200
  // lines. Management passes its own run: its Details show the log already (sd:2392). `job(o)` names { name }.
  const logLine = name => `local-cron-jobs/cron-jobs.sh logs ${name}`;
  function showLog({ job, run }) {
    return { id: 'jobs.log', on: 'job', label: 'Show log', does: run ? 'Shows the job\'s log in Details.' : 'Prints the job\'s last 200 log lines in Details.', key: 'l', risk: 'safe', executes: !run,
      cli: o => logLine(job(o).name), sends: o => `POST /api/actions/run {action: job.log, job: ${job(o).name}}`,
      run: run || (o => runAction(o, 'job.log', { job: job(o).name })) };
  }

  // Print (sd:5886): launchd's whole record of the job, in Details. It is the row's act after exit 127, where Retry is
  // not (sd:3392). The server finds the service from the installed job, not from the row. `job(o)` names { name, service, exit }.
  function printJob({ job }) {
    return { id: 'jobs.print', on: 'job', label: 'Print', does: 'Shows everything macOS knows about this job in Details.', key: 'p', icon: 'terminal', risk: 'safe', executes: true, primary: o => job(o).exit === 127,
      when: o => !!job(o).service || 'the row names no launchd service', cli: o => `launchctl print ${job(o).service}`,
      sends: o => `POST /api/actions/run {action: job.print, job: ${job(o).name}}`,
      run: o => runAction(o, 'job.print', { job: job(o).name }) };
  }

  // Show assignment (sd:6682): the server runs `sd assignments get` for the row's number and Details shows its output,
  // which a row click does not. `o.n` is the assignment's number.
  function showAssignment({ primary = () => true } = {}) {
    return { id: 'asg.get', on: 'assignment', label: 'Show assignment', does: 'Prints the assignment\'s record in Details.', key: 'o', icon: 'clipboard-list', risk: 'safe', primary, executes: true,
      cli: o => `sd assignments get ${o.n}`, sends: o => `POST /api/actions/run {action: assignment.get, assignment: ${o.n}}`,
      run: o => runAction(o, 'assignment.get', { assignment: o.n }) };
  }

  // The dispatcher's kickstart after a build queues (tasks_screen.build): started now, or at its next slot and why.
  const kickText = k => !k ? '' : k.ok ? ' · the dispatcher was started' : ` · the dispatcher starts at its next slot (${k.why})`;
  // A satellite's row waits for that host's own tick; the hub's kickstart claims none of it (pack sd:7933).
  const hostText = q => q.filter(r => r.host).map(r => ` · ${r.host}'s dispatcher claims #${r.item} at its next tick`).join('');
  const queuedText = out => `Queued ${out.queued.map(q => `#${q.item}`).join(', ')} for build${kickText(out.kickstart)}${hostText(out.queued)}`;
  // Build (pack sd:6095): the press queues a headless build of each picked ready item (`sd assignments build`). The
  // dialog reads the server's plan first (builds.plan): each repository's bundles of up to 3 items, the model, the
  // room now, and every item the press would refuse with why. One refusal refuses the whole press, so the dialog then
  // offers no Build. Build posts the items with the revisions the plan read; a 409 changed nothing. The dispatcher claims
  // a bundle when there is room; until then each row shows its assignment as queued. Tasks and Today's suggested Queue
  // rows (pack sd:7271) register it: `row(o)` is the item's { id, status, repo, assignment }, or nothing once the page no
  // longer lists it; `reread()` reads the page again after the press.
  function build({ row, reread, primary = () => false }) {
    const { html, put, plural } = window.markup;
    const toast = m => window.shell.toast(m);
    const dlg = document.createElement('dialog');
    dlg.className = 'palette date-dlg start-dlg'; dlg.setAttribute('aria-labelledby', 'build-h');
    document.body.append(dlg);
    async function ask(objs, done) {
      const rows = objs.map(row).filter(Boolean);
      if (!rows.length) { toast('Build: no listed task is selected'); return; }
      let P;
      try { P = await window.shell.getJSON(`/api/tasks/build/plan?items=${rows.map(t => t.id).join(',')}`); }
      catch (err) { toast(`Build plan not read: ${err.message}`); return; }
      const room = P.room.why ? `No builder starts now: ${P.room.why}. The press still queues; a claim waits for room.`
        : `${P.room.machine} of ${P.room.cap} builders free now · at most ${P.room.repo_cap} per repository`;
      const items = P.bundles.flatMap(b => b.items), ok = !P.refused.length && items.length;
      put(dlg, html`<form method="dialog" class="date-form">
        <h2 id="build-h">Build ${plural(rows.length, 'task')}</h2>${window.shell.closeX}
        <p class="why">${room}</p>
        ${P.refused.length ? html`<p class="why">▲ Nothing can be queued while one item is refused:</p>
          <ul class="start-list">${P.refused.map(r => html`<li><code>#${String(r.id)}</code> ${r.why}</li>`)}</ul>` : ''}
        ${P.bundles.map(b => html`<section class="build-bundle" aria-label="Bundle in ${b.name}"><h3>${b.name} <small>${b.model} · branch ${b.branch} · ${b.host ? `runs on ${b.host}, whose dispatcher beat ${b.beat} s ago` : b.room ? `${b.room} may start now` : 'waits for room'}</small></h3>
          <ol class="start-list">${b.items.map(r => html`<li><code>#${String(r.id)}</code> ${r.title}</li>`)}</ol></section>`)}
        <p class="why">One run per bundle, ${String(P.budget_minutes)} min each. The items lock until each run ends; Cancel on Queue › Builds stops one.</p>
        <div class="actions">
          ${ok ? html`<button class="btn" value="build" type="submit">Build</button>` : ''}
          <button class="btn quiet" value="cancel" type="submit" formnovalidate data-cancel>Cancel</button>
        </div></form>`);
      dlg.onclose = () => {
        if (dlg.returnValue !== 'build' || !ok) return;
        const revisions = Object.fromEntries(items.map(r => [String(r.id), r.revision]));
        window.shell.post('/api/tasks/build', { items: items.map(r => r.id), revisions }).then(out => {
          toast(queuedText(out)); done?.(); reread();
        }, err => {
          // A 409 is a refused press that changed nothing; no status is no answer, so whether it queued is not known.
          toast(err.status === 409 ? err.message : err.status ? `Nothing queued: ${err.message}` : `Build got no answer, so what queued is not known: read the tasks again (${err.message})`);
          reread();
        });
      };
      dlg.returnValue = '';
      dlg.showModal(); (dlg.querySelector('[value="build"]') || dlg.querySelector('[value="cancel"]'))?.focus();
    }
    return { id: 'item.build', on: 'item', label: 'Build', icon: 'rocket', risk: 'safe', bulk: true, executes: true, primary,
      does: 'Queues an unattended build for each item, grouped by repo.',
      when: o => { const t = row(o); return !t ? 'the task is no longer listed' : t.status !== 'ready' ? `a build takes a ready task; this one is ${String(t.status || '').replace(/_/g, ' ').toLowerCase()}`
        : !t.repo_path ? 'a build needs a repository' : t.assignment === 'running' || t.assignment === 'queued' ? `an agent run is ${t.assignment} for it` : true; },
      cli: o => `sd assignments build ${row(o)?.id ?? o.id}`,
      sends: () => 'POST /api/tasks/build {items, revisions}',
      batch: (objs, done) => ask(objs, done),
      run: o => { ask([o]); return null; } };
  }

  // Open a pull request on GitHub, in a new tab, from a pull request row or a review of one (`on`). `pr(o)` names
  // { repo: 'owner/name', number, url }.
  function openPullRequest({ on = 'pull request', pr, primary = () => true }) {
    return { id: on === 'pull request' ? 'pr.open' : `${on}.open`, on, label: 'Open on GitHub', does: 'Opens the pull request on GitHub in a new tab.', key: 'o', icon: 'arrow-up-right', risk: 'safe', primary,
      when: o => !!(pr(o).repo && pr(o).number) || 'the row names no pull request',
      cli: o => `gh pr view ${pr(o).number} --repo ${pr(o).repo} --web`,
      run: o => { window.open(pr(o).url || `https://github.com/${pr(o).repo}/pull/${pr(o).number}`, '_blank', 'noopener'); return 'Opened on GitHub in a new tab'; } };
  }

  // Re-run collector (sd:2207, sd:2894): the server runs sd shadow sync in a thread, bounded to 120 s, one run at a time; a
  // second start is refused while one is live. The page asks how it went every 5 s and calls reread() once it ends. Success
  // is quiet: the reread shows it. A failed tracker or a run that broke stays as a toast with its first reason. `page`
  // names who reads again.
  const SYNC_POLL = 5000;
  function syncCollector({ reread, page }) {
    const wait = () => setTimeout(() => window.shell.getJSON('/api/shadow/state').then(s => {
      if (s.running) return wait();
      reread();
      const bad = s.error || s.trackers.filter(t => !t.ok && t.configured).map(t => `${t.tracker}: ${t.reason || 'failed'}`)[0];
      if (bad) window.shell.toast(`Shadow sync ended with a problem: ${bad}`);
    }, e => window.shell.toast(`The sync's state was not read: ${e.message}`)), SYNC_POLL);
    return { id: 'collector.sync', on: 'collector', label: 'Re-run collector', does: 'Gets the latest from GitHub now, up to 2 minutes, then refreshes.', key: 'r', icon: 'rotate-ccw', risk: 'safe', primary: () => true,
      cli: () => 'sd shadow sync --max-seconds 120',
      run: () => window.shell.post('/api/shadow/sync', {}).then(() => { wait(); return `Shadow sync started · ${page} reads again when it ends`; }) };
  }

  // Adversarial review (sd:5064): POST /api/adversarial starts a codex review of the row in the background (adversarial.py).
  // `payload(o)` is the row's { kind, target } (a change adds item and body). A 202 is followed here: GET
  // /api/adversarial/<id> every 10 s while the page is open, until the run settles: failed with no item, or `filed` set.
  // Each answer fires 'adversarial:run' on document, so a Details pane that shows reviewPane(kind, target) draws again.
  // A refusal is its toast in the server's words: a 409 names the run already going. The run is one row's, so no bulk.
  // sd:5100: a page may name its own `id`, `label`, `does`, `fields` (the shell's dialog fields, whose values reach
  // payload(o, v)) and `consequence`; Adversarial review: PR and Deep review on a repo row are two more of these.
  // sd:5101: a source review's payload adds `source`, its ledger row's key; its run key is `source:<checkout> <row key>`.
  const REVIEW_POLL = 10000, RUNS = new Map();
  const runKey = (kind, target) => `${kind}:${target}`;
  const settled = r => (r.state === 'failed' && r.item == null) || r.filed != null;
  function settledText(r) {
    if (r.state === 'failed') return `Adversarial review failed · run ${r.id}: ${r.reason || 'no reason recorded'}${r.filed && r.filed !== 'written' ? ` · ${r.filed}` : ''}`;
    if (r.kind === 'piece' && r.filed === 'written') return `Adversarial review written · run ${r.id} · ${r.result}`;
    return r.filed === 'written' ? `Adversarial review filed · run ${r.id} · brief sd:${r.brief_item}; Details links it` : `Adversarial review done · run ${r.id} · ${r.filed}`;
  }
  function follow(key, run) {
    RUNS.set(key, run);
    document.dispatchEvent(new CustomEvent('adversarial:run', { detail: { key, run } }));
    if (settled(run)) { window.shell.toast(settledText(run)); return; }
    // A failed read is not known, not settled: Details says so and the next poll tries again. A newer run on the same row
    // ends this run's polling: an answer for it would draw over the newer one.
    const still = window.shell.shown(`adversarial ${key}`, run.id, () => RUNS.get(key)?.id);
    setTimeout(() => still() && window.shell.getJSON(`/api/adversarial/${encodeURIComponent(run.id)}`).then(next => still() && follow(key, next),
      e => still() && follow(key, { ...run, unread: e.message })), REVIEW_POLL);
  }
  function adversarialReview(on, { payload, when = () => true, id = `${on}.adversarial-review`, label = 'Adversarial review',
    does = 'Starts a background review (Codex); you pick fixes from its report.', fields, consequence }) {
    return { id, on, label, does, risk: 'safe', executes: true, icon: 'shield-alert', when, fields, consequence, askFirst: !!consequence,
      cli: () => 'no sd verb: the dashboard runs local-adversarial-gate in the background',
      sends: (o, v) => `POST /api/adversarial ${JSON.stringify(payload(o, v || {}))}`,
      run: (o, v) => { const p = payload(o, v || {});
        return window.shell.post('/api/adversarial', p).then(run => { follow(runKey(p.kind, p.source ? `${p.target} ${p.source}` : p.target), run); return `Review started · run ${run.id}`; },
          err => { if ([400, 404, 409].includes(err.status)) return err.message; throw err.status ? err : new Error(`no answer (${err.message}), so whether a review started is not known`); }); } };
  }
  // The Details section for the row's latest run on this page, or nothing: its state, then the brief and the item it filed.
  // A running run says how long it has run of its bound (sd:5100: a deep review runs up to 90 min). `title` names the pane.
  const minutes = s => Math.max(0, Math.round(s / 60));
  function reviewPane(kind, target, title = 'Adversarial review') {
    const r = RUNS.get(runKey(kind, target)); if (!r) return '';
    const { html } = window.markup;
    const S = window.shell;
    const links = r.brief_item ? html` · <a href="${S.itemPage(r.brief_item)}">brief sd:${r.brief_item}</a>` : '';
    const item = r.item != null ? html` · <a href="${S.itemHref(r.item)}">#${r.item}</a>` : '';
    const ran = r.state === 'running' && r.started && r.timeout ? `, ${minutes((Date.now() - Date.parse(r.started)) / 1000)} of at most ${minutes(r.timeout)} min` : '';
    const state = settled(r) ? settledText(r) : `Run ${r.id} is ${r.state}${ran}${r.unread ? `; its state was not read: ${r.unread}` : ''}. This page reads it every 10 s.`;
    return html`<section class="adv-run" aria-live="polite"><h3>${title}</h3><p class="why">${state}${links}${item}</p></section>`;
  }

  // Published copies (sd:5102): one icon per destination of the row's document or brief, from its `pub` (publishing.py), in
  // the order Drive, Notion, Obsidian. sd:5300: every destination the row is configured for shows (pub.choice: designated,
  // or Obsidian in the repo's channels), with a copy or not, so the state is a fill and an edge, not colour alone: a copy
  // that exists is filled, one queued is a dashed edge, a designated one with no copy an outline, and one whose read failed
  // a dashed unknown edge naming why: unknown is not "no copy". The hover names the destination and its link or vault path.
  // A copy with a URL is a link that opens it in a new tab. Briefs and Documents both draw it, so shell.css styles it once.
  const PUB_ICON = { drive: 'triangle', notion: 'box', obsidian: 'gem' };
  // What each fill says (sd:5731): published() draws its tips from it and the Documents legend lists it, so the two agree.
  const PUB_STATE = { has: 'published', queued: 'copy queued', missing: 'designated, no copy yet', unread: 'not read', off: 'not a destination' };
  function pubGlyph(ch) {
    const { html } = window.markup;
    return html`<svg class="i" aria-hidden="true"><use href="#i-${PUB_ICON[ch]}"/></svg>`;
  }
  // One sample mark per fill, as a table's help line takes it (shell.list.help): [[mark, meaning]].
  function pubLegend() {
    const { html } = window.markup;
    return Object.entries(PUB_STATE).map(([k, w]) => [html`<span class="pub"><span class="pub-notion ${k}" aria-hidden="true">${pubGlyph('notion')}</span></span>`, w]);
  }
  // The Drive and Notion destinations published() draws as `missing`: designated, every read answered, and no copy.
  const missingCopies = pub => !pub || !pub.choice || pub.unread ? []
    : ['drive', 'notion'].filter(ch => pub.choice[ch] === true && !(pub.copies || []).some(c => c.channel === ch));
  const PUB_WHERE = (ch, url) => { const f = ch === 'obsidian' && /[?&]file=([^&]*)/.exec(url); return f ? decodeURIComponent(f[1]) : url; };
  function published(pub) {
    const { html } = window.markup;
    if (!pub) return '';
    const copies = (pub && pub.copies) || [], choice = pub && pub.choice, unread = (pub && pub.unread) || '';
    const glyph = pubGlyph;
    const mark = (cls, tip) => html`<span class="${cls}" role="img" aria-label="${tip}" title="${tip}">`;
    let said = false;
    const marks = Object.keys(PUB_ICON).map(ch => {
      const c = copies.find(x => x.channel === ch);
      if (c) {
        // Only a link the server vouched for opens: http(s) for Notion and Drive, obsidian://open for the vault.
        const url = c.url && /^(https?:|obsidian:\/\/open\?)/.test(c.url) ? c.url : null;
        const cls = `pub-${ch} ${c.state === 'queued' ? 'queued' : 'has'}`, tip = url ? `${c.title}: ${PUB_WHERE(ch, url)}` : c.title;
        return url ? html`<a class="${cls}" href="${url}" target="_blank" rel="noopener noreferrer" aria-label="Open in ${c.title}" title="${tip}">${glyph(ch)}</a>`
          : html`${mark(cls, tip)}${glyph(ch)}</span>`;
      }
      const on = choice ? choice[ch] : false;
      // sd:5485: every destination shows; one that is not this row's is dim, so the active ones stand out.
      if (on === false || on === undefined) return html`${mark(`pub-${ch} off`, `${PUB_NAME[ch]}: ${PUB_STATE.off}`)}${glyph(ch)}</span>`;
      // A designated destination with no copy is missing only when every read behind it answered.
      const why = on === true ? unread : unread || choice.why || 'the channels setting was not read';
      if (why) said = true;
      return why ? html`${mark(`pub-${ch} unread`, `${PUB_NAME[ch]}: ${PUB_STATE.unread} · ${why}`)}${glyph(ch)}</span>`
        : html`${mark(`pub-${ch} missing`, `${PUB_NAME[ch]}: ${PUB_STATE.missing}`)}${glyph(ch)}</span>`;
    }).filter(Boolean);
    const lone = unread && !said ? html`<span class="pub-unknown" role="img" aria-label="${unread}" title="${unread}">▨</span>` : '';
    return marks.length || lone ? html`<span class="pub">${marks}${lone}</span>` : '';
  }

  // Publish to and Publish now (sd:5102), for a research document on Documents and a routine brief on Briefs. `on` is the
  // row's noun; `base` the page's API (`/api/documents`, `/api/briefs`); `payload(o)` what names the row; `cli(o, channel)`
  // the line by hand; `reread()` the page's reread. Publish to ticks one destination: POST <base>/publish adds its designation
  // (research.conf.py for a document, the item's fields.publish for a brief) and queues it through the pack. Unticking
  // (Remove copy, sd:5489) posts <base>/unpublish: the pack queues the copy for the trash, then the designation goes; it
  // asks first, and Undo ticks it again. A document's Obsidian box (sd:5500) writes its vault copy at once and its untick moves
  // the copy into the vault's .trash; where pub.choice.repo_wide names why (a brief, or the vault's own repository) Obsidian
  // is the repo's channels setting (Management > Publishing) and the box only says so.
  // Publish now posts <base>/publish-now, which starts one headless drain of the row's queued requests (publish_drain.py); the
  // page reads <base>/publish-now/<id> every 10 s until it settles, fires 'publish:run' so Details draws again, then rereads.
  const PUB_NAME = { drive: 'Google Drive', notion: 'Notion', obsidian: 'Obsidian' };
  const DRAINS = new Map(), DRAIN_POLL = 10000;
  const refusal = (err, what) => { if ([400, 404, 409].includes(err.status)) return err.message; throw err.status ? err : new Error(`no answer (${err.message}), so whether ${what} is not known`); };
  function followDrain(base, key, run, reread) {
    DRAINS.set(key, run);
    document.dispatchEvent(new CustomEvent('publish:run', { detail: { key, run } }));
    if (run.state === 'done' || run.state === 'failed') {
      window.shell.toast(run.state === 'done' ? `Published · run ${run.id}` : `Publish now failed · run ${run.id}: ${run.reason || 'no reason recorded'}`);
      reread(); return;
    }
    // A failed read is not known, not settled: Details says so and the next poll tries again.
    setTimeout(() => window.shell.getJSON(`${base}/publish-now/${encodeURIComponent(run.id)}`).then(next => followDrain(base, key, next, reread),
      e => followDrain(base, key, { ...run, unread: e.message }, reread)), DRAIN_POLL);
  }
  function publishCommands(on, { base, payload, cli, reread }) {
    return [
      { id: `${on}.publish`, on: `${on} destination`, label: 'Publish to', does: 'Marks this destination and queues the copy; Publish now sends it.', icon: 'send', risk: 'safe', askFirst: true, executes: true,
        when: o => o.channel === 'obsidian' && o.repoWide ? o.repoWide
          : o.set ? `already designated for ${o.name}; untick it to remove the copy` : o.why || true,
        cli: o => cli(o.row, o.channel),
        sends: o => `POST ${base}/publish ${JSON.stringify({ ...payload(o.row), channel: o.channel })}`,
        consequence: o => o.channel === 'obsidian' ? `Designates ${o.row.label} for Obsidian and writes its Markdown into the vault now. Nothing leaves the machine.`
          : `Marks ${o.row.label} for ${o.name} and queues it. Nothing leaves the machine until you press Publish now, or publish it from a Claude session.`,
        run: o => window.shell.post(`${base}/publish`, { ...payload(o.row), channel: o.channel }).then(
          r => reread().then(() => r.wrote ? `Written to the vault · ${o.row.label}` : r.queued ? `Queued for ${o.name} · ${o.row.label}`
            : `${o.name} designated; not ${o.channel === 'obsidian' ? 'written' : 'queued'}: ${(r.lines || []).slice(-1)[0] || 'publish said nothing'}`),
          err => refusal(err, 'it was designated')) },
      { id: `${on}.unpublish`, on: `${on} destination`, label: 'Remove copy', does: 'Queues the copy for the trash, which can restore it. Undo re-ticks.', icon: 'x', risk: 'undo', askFirst: true, executes: true,
        when: o => o.channel === 'obsidian' && o.repoWide ? o.repoWide
          : !o.set ? `not designated for ${o.name}` : o.why || true,
        cli: () => 'no sd verb: remove the designation by hand, then trash the copy by the publication contract',
        sends: o => `POST ${base}/unpublish ${JSON.stringify({ ...payload(o.row), channel: o.channel })}`,
        consequence: o => o.channel === 'obsidian' ? `Drops ${o.row.label}'s Obsidian designation and moves its vault copy into the vault's .trash now, where Obsidian restores it. Undo ticks it again and writes it back.`
          : `Drops ${o.row.label}'s ${o.name} designation and queues its ${o.name} copy for the trash (${o.channel === 'notion' ? 'Notion\'s Trash' : 'the Drive bin'}), where it can be restored. Nothing leaves the machine until Publish now. Undo ticks it again and republishes.`,
        run: o => window.shell.post(`${base}/unpublish`, { ...payload(o.row), channel: o.channel }).then(
          r => reread().then(() => !r.dropped ? `Trash ${r.trash === 'trashed' ? 'done' : 'queued'}, but ${o.name} is still designated: ${(r.lines || []).slice(-1)[0] || 'no reason given'}`
            : r.trash === 'trashed' ? `${o.name} removed · its vault copy is in the vault's .trash`
            : r.trash === 'queued' ? `${o.name} removed · its copy is queued for the trash; Publish now drains it` : `${o.name} removed · no copy was queued or delivered`),
          err => refusal(err, 'the trash was queued')),
        undo: o => window.shell.post(`${base}/publish`, { ...payload(o.row), channel: o.channel }).then(() => reread()) },
      // A row whose icon reads "designated, no copy yet": POST <base>/publish again keeps the designation and queues the copy.
      { id: `${on}.queue-copy`, on, label: 'Queue copy', does: 'Queues each missing copy for its destination; Publish now sends them.', icon: 'send', risk: 'safe', askFirst: true, executes: true,
        when: o => !o.pub ? `not a ${on} the dashboard publishes` : missingCopies(o.pub).length > 0 || 'every designated destination has its copy or is queued',
        cli: o => missingCopies(o.pub).map(ch => cli(o, ch)).join('\n'),
        sends: o => missingCopies(o.pub).map(ch => `POST ${base}/publish ${JSON.stringify({ ...payload(o), channel: ch })}`).join('\n'),
        consequence: o => `Queues ${o.label} for ${missingCopies(o.pub).map(ch => PUB_NAME[ch]).join(' and ')}. Nothing leaves the machine until you press Publish now, or publish it from a Claude session.`,
        // A failure after an earlier destination queued leaves that copy queued (sd:5924): the page reads again, so the row and
        // Publish now show it, and the toast names what queued and what failed. A failed reread shows on the page itself.
        run: o => {
          const want = missingCopies(o.pub), queued = [];
          let at = null;
          return want.reduce((done, ch) => done.then(() => { at = ch; return window.shell.post(`${base}/publish`, { ...payload(o), channel: ch }).then(r => { if (r.queued) queued.push(ch); }); }), Promise.resolve())
            .then(() => reread().then(() => { const n = queued.length; return n === want.length ? `Queued ${n === 1 ? 'one copy' : `${n} copies`} · ${o.label}; Publish now sends ${n === 1 ? 'it' : 'them'}`
              : `${n} of ${want.length} copies queued · ${o.label}; the publish said nothing for the rest`; }),
            err => !queued.length ? refusal(err, 'the copy was queued')
              : reread().catch(() => {}).then(() => `Queued for ${queued.map(ch => PUB_NAME[ch]).join(' and ')} · ${o.label}; `
                + ([400, 404, 409].includes(err.status) ? `${PUB_NAME[at]} refused: ${err.message}` : `whether ${PUB_NAME[at]} queued is not known: ${err.message}`)));
        } },
      { id: `${on}.publish-now`, on, label: 'Publish now', does: 'Copies the queued documents to Notion or Drive now.', icon: 'send', risk: 'safe', askFirst: true, executes: true,
        when: o => !o.pub ? `not a ${on} the dashboard publishes` : (o.pub.copies || []).some(c => c.state === 'queued') || 'nothing is queued: tick Drive or Notion under Publish to first',
        cli: () => 'no sd verb: drain the queue in a Claude session by the publication contract',
        sends: o => `POST ${base}/publish-now ${JSON.stringify(payload(o))}`,
        consequence: o => `Has Claude copy ${o.label} to each place it is queued for, in the background. It creates or updates pages and files only in the folders you set.`,
        run: o => window.shell.post(`${base}/publish-now`, payload(o)).then(run => { followDrain(base, o.id, run, reread); return `Drain started · run ${run.id}`; },
          err => refusal(err, 'a drain started')) },
    ];
  }
  // The Details section: one checkbox per destination, each a registered `<on> destination` object, and the last drain's state.
  function publishPane(on, o) {
    const p = o && o.pub; if (!p || !p.choice) return '';
    const { html } = window.markup;
    const c = p.choice;
    const box = ch => {
      const id = `pub:${o.id}:${ch}`, set = c[ch];
      window.shell.commands.put({ id, type: `${on} destination`, label: `${PUB_NAME[ch]} for ${o.label}`, row: o, channel: ch, name: PUB_NAME[ch], set, why: c.why, repoWide: c.repo_wide || '' });
      return html`<button class="pubbox" type="button" role="checkbox" aria-checked="${set == null ? 'mixed' : String(!!set)}" data-cmd="${on}.${set ? 'unpublish' : 'publish'}" data-obj="${id}"><svg class="i" aria-hidden="true"><use href="#i-${PUB_ICON[ch]}"/></svg><span>${PUB_NAME[ch]}</span></button>`;
    };
    const run = DRAINS.get(o.id);
    const state = !run ? '' : run.state === 'done' || run.state === 'failed' ? `Last drain ${run.state}${run.reason ? `: ${run.reason}` : ''}.`
      : `Drain ${run.id} is ${run.state}${run.unread ? `; its state was not read: ${run.unread}` : ''}. This page reads it every 10 s.`;
    return html`<section class="pubpane" aria-live="polite"><h3>Publish to</h3><div class="pubboxes">${['drive', 'notion', 'obsidian'].map(box)}</div>
      ${c.why ? html`<p class="why">${c.why}</p>` : ''}${c.repo_wide ? html`<p class="why">Obsidian: ${c.repo_wide}.</p>` : ''}${c.obsidian == null ? html`<p class="why">Obsidian not known: ${p.unread || c.why || 'the channels setting was not read'}.</p>` : ''}
      ${state ? html`<p class="why">${state}</p>` : ''}</section>`;
  }

  // sd:8014: the bulk Decline of one decision queue, on the queue object `{id, type: 'queue', kind, label, pool}`. `pool`
  // is the queue's waiting rows from triage.pool, each with its score, age and revision. The button names how many rows
  // the defaults move; the dialog asks for the score and the age, lists the rows they move, and posts those rows with
  // their revisions to /api/triage/decline. Only this press declines: nothing declines on a schedule. A market row has
  // no score, so its Decline is by age alone. The rows move to declined and stay listed under the Declined filter.
  const DECLINE = { score: 6, days: 30 };
  const numOr = (v, d) => { const t = String(v ?? '').trim(), n = Number(t); return t && Number.isFinite(n) ? n : d; };
  function declinePick(o, v) {
    const s = numOr(v?.score, DECLINE.score), d = Math.max(0, Math.trunc(numOr(v?.days, DECLINE.days)));
    return { s, d, rows: (o.pool || []).filter(r => r.age != null && r.age > d && (!o.scored || r.score == null || r.score < s)) };
  }
  const declineLabel = (o, v) => { const { s, d, rows } = declinePick(o, v); return `Decline ${rows.length}${o.scored ? ` below score ${s}` : ''} older than ${d} days`; };
  const declineRows = rows => rows.length ? rows.map(r => `#${r.item} ${r.title}`).join(' · ') : 'none';
  function decline({ post }) {
    return { id: 'queue.decline', on: 'queue', label: 'Decline', icon: 'archive', risk: 'confirm',
      does: 'Asks first, then declines the stale rows; Declined still lists them.',
      fields: o => [...(o.scored ? [{ name: 'score', label: 'Below score', value: String(DECLINE.score) }] : []),
        { name: 'days', label: 'Older than (days)', value: String(DECLINE.days) }],
      when: o => declinePick(o).rows.length ? true : 'no row is that old and low',
      // The confirm calls it again as the score and age are typed, so it names the rows OK will post (sd:8014 review).
      consequence: (o, v) => `${declineLabel(o, v)} in ${o.label}: ${declineRows(declinePick(o, v).rows)}. They stay listed under Declined; nothing is removed.`,
      cli: (o, v) => `no sd verb: ${declineLabel(o, v)} posts /api/triage/decline: ${declineRows(declinePick(o, v).rows)}`,
      run: (o, v) => { const { s, d, rows } = declinePick(o, v);
        if (!rows.length) return `Nothing declined: no row in ${o.label} is that old and low`;
        return post('/api/triage/decline', { kind: o.kind, score: s, days: d, items: rows.map(r => ({ item: r.item, revision: r.revision })) })
          .then(out => `Declined ${out.declined.length} in ${o.label}${out.refused.length ? ` · ${out.refused.length} refused: ${out.refused.map(r => `#${r.item} ${r.reason}`).join('; ')}` : ''}`); } };
  }

  window.commands = { answered, fix, dependabotFix, grill, applyReview, retry, runAction, showLog, logLine, printJob, showAssignment, queuedText, build, openPullRequest, syncCollector, adversarialReview, reviewPane, published, PUB_STATE, pubLegend, publishCommands, publishPane, decline, declineLabel, declinePick };
})();
