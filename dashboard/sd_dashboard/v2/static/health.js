// Health v2 (sd:2115): the page script. It loads before shell.js, which reads what it declares.
// Views: one at a time, Checkouts, Machine, GitHub, Jobs and Status (pack sd:9260), each its own reader of /api/health/<view> (health_screen.part),
// started the first time the view opens; PAGE_VIEWS declares them to the shell's view switch, which owns #view=<id>. The
// lamps, the badge and each view's count are /api/health/summary, read at start and again after a view's read; it waits on
// no walker, so the strip draws before any view's rows. State starts on the states that want something (warning, caution,
// unknown), so ok and queued rows wait until their State is pressed; with no State pressed every row shows (sd:7207). A
// State that differs from that start goes in ?state=, an empty one too; an old ?show=all link opens with none pressed.
// Show ignored (sd:5470) lists the rows Ignore hid, a snooze with no end, instead of the areas' rows, each with Unignore;
// ?show=ignored keeps it.
// Rows: the areas health_screen.document builds from the fleet child (worktree registrations),
// Operations > Ports' reader (ports), protection.rows (branch protection, and the
// alerts behind Dependencies and Security, sd:2205 and sd:2206), the nightly credentials heartbeat (sd:2203) and
// health_collectors (disk and merged branches, sd:2202 and sd:2204, and the machine-setup doctor, sd:3393).
// Ported from the design source's products/system/designs/v2/health page, its stylesheet and script, at d82daa1.
// An area with no reader shows as unknown with what it does not read, never as a clean lamp.
// Most fixes are a CLI line for Copy; Re-check reads the document again. The writes are Re-run collector, which starts
// the server's shadow sync (sd:2894), Snooze (sd:1896): an area's `snoozed` rows leave it, and snooze.js draws them, Ignore
// (sd:5470), the same write with no end, whose rows Show ignored lists, and
// the cleanups (sd:3393, sd:5105) and Clean all safe, which lists every ready one first, each behind a confirm that names what goes, posting the row's revision, and a service's
// Restart and Start (sd:3707), each a confirm that posts /api/services/<label>/<action>, `sd services`' own code.
// Markup is html`…` from markup.js: every value put in it is escaped, and put() is the only way into the page.
(() => {
  const { html, put, plural, cells } = window.markup;
  const GLYPH = { warning: '■', caution: '▲', queued: '◌', ok: '●', unknown: '▨' };
  const RANK = { warning: 0, caution: 1, unknown: 2, queued: 3, ok: 4 };
  const STATES = ['warning', 'caution', 'unknown', 'queued', 'ok'];
  // The design's icon and help per area; the help is page markup the shell renders with <b> and <code> only.
  const LOOK = {
    disk: ['hard-drive', '<b>Where the space went.</b> Each volume\'s use, then the biggest things the tools create: the storage folders Settings names (Disk storage) and build output left in worktrees. A merged worktree should keep no build output. A volume shows caution at 80% and warning at 90%. A storage folder on a network drive shows the drive\'s use; its folders are not measured. Build output is found, not sized.'],
    cred: ['key-round', '<b>Presence and expiry only.</b> Token values are never read into this page. GitHub reports a classic PAT expiry in a response header; Home Assistant tokens carry none the dashboard can read.'],
    wt: ['folder-x', '<b>Registered is not present.</b> A worktree whose folder is gone still holds its branch. Prune forgets it; it never deletes a folder.'],
    br: ['git-branch', '<b>Merged but not deleted.</b> Local branches already in origin’s default branch. Delete removes only merged ones, so no work is lost. Merged worktrees are those the merge queue landed: Remove deletes the folder, its branch and origin’s, and keeps one with changed files, a running process or a branch that moved.'],
    dep: ['package', '<b>Dependabot, per repo.</b> Open alerts on the unarchived managed repos, as the nightly sync stored them. A repo whose alerts could not be read is unknown, never clean. A repo with alerts off is allowed and listed, not flagged.'],
    sec: ['shield-alert', '<b>Secret scanning, public repos only.</b> Private repos are not scanned, by policy. A public repo with scanning off is a finding.'],
    ports: ['link-2', '<b>Who holds which port.</b> Configured service ports, then every other TCP listener the Mac shows. <b>Unknown is not free</b>: an uninspected listener says nothing about the port.'],
    doc: ['shield-check', '<b>The machine\'s own self-check.</b> Each warning and failure, one row each, and the checks that pass. A result stays good for ten minutes; a check still running, stopped at its deadline or failed is unknown, never clean.'],
    ctr: ['box', '<b>Running containers.</b> Each running Docker container, read with Ports: its image, status, project and folder, and the repo folders it mounts. A Docker that was not read is unknown, never empty. Start and stop them from the terminal.'],
    svc: ['activity', '<b>Background services that stay running.</b> Restart and Start act on the row as shown. The dashboard checks that it serves the newest code; it cannot restart itself, so a stale dashboard gives you the command to run. A one-off or scheduled job shows only when its last run failed, as a warning with no controls.'],
    mir: ['book-open', '<b>The vault copy of your records.</b> When it last updated. Read-only: this page writes no vault note.'],
    stat: ['siren', '<b>The services you depend on.</b> One row per line of Settings\' status services list, read every 5 minutes: degraded is caution, an outage a warning. A status read over 15 minutes ago, or not read, is unknown. Each service\'s providers flag their row in the Usage dropdown beside Read again.'],
    prot: ['lock', '<b>What each default branch enforces.</b> One column per registered repo, one line per check: a filled cell is a gap, a short mark passes, an empty cell does not apply. <b>Unknown is not protected</b>: a hatched column was not read and shows no cells. Every column opens its repo; the table below carries the same cells.'],
  };
  // Protection has no lamp: the annunciator holds 2 to 8 (the design's rule), and its matrix is Health's evidence strip.
  // Services (sd:3707) and Containers (sd:5840) are further areas past a full strip: no lamp, their chip and section only
  // (design.md, lamp: false).
  // Service status (pack sd:9260) is one more: its view's rail count and the rail badge carry it.
  const NO_LAMP = new Set(['prot', 'svc', 'ctr', 'stat']);
  const PH = 10; // one matrix line in the SVG's own units
  const I = n => html`<svg class="i" aria-hidden="true"><use href="#i-${n}"/></svg>`;
  const $ = id => document.getElementById(id);
  // The views, for the shell's view switch: each panel is #view-<id> in health.html. `areas` is the page's own: the view
  // an area's lamp or an ?area= value opens. health_screen.VIEWS is the same list.
  window.PAGE_VIEWS = [
    { id: 'checkouts', label: 'Checkouts', api: '/api/health/checkouts', areas: ['wt', 'br'] },
    { id: 'machine', label: 'Machine', api: '/api/health/machine', areas: ['disk', 'svc', 'ports', 'ctr', 'doc', 'aws'] },
    { id: 'github', label: 'GitHub', api: '/api/health/github', areas: ['prot', 'dep', 'sec'] },
    { id: 'jobs', label: 'Jobs', api: '/api/health/jobs', areas: ['cred', 'mir'] },
    { id: 'status', label: 'Status', api: '/api/health/status', areas: ['stat'] },
  ];
  // The shell's Shapeshift bar is the text filter (sd:5539): it matches a row's repo, path and branch in every view.
  window.PAGE_FILTER = Object.fromEntries(window.PAGE_VIEWS.map(v => [v.id, 'repo, path and branch']));

  let DOC = null, AREAS = [], ROWS = [], SNOOZED = [], IGNORED = [], selected = null, query = '';
  let SUMMARY = null, VIEW = null, showing = 'findings', repos = 'managed';
  const S = {}, readers = {}; // each view's last reading and its reader
  // The Machine view's chooser (sd:9757): the hub first, then each machine with a stored reading. The viewer's pick is
  // kept in localStorage; the hub is no ?host=, another machine its stored reading at /api/health/machine?host=<name>.
  const HOST_KEY = 'health.machine';
  let host = (() => { try { return localStorage.getItem(HOST_KEY) || ''; } catch (_) { return ''; } })();
  const machineSource = () => host ? `/api/health/machine?host=${encodeURIComponent(host)}` : '/api/health/machine';
  function chooseHost(value) {
    const hub = S.machine?.DOC?.machines?.hub;
    host = value === hub ? '' : value;
    try { localStorage.setItem(HOST_KEY, host); } catch (_) { /* blocked: the choice lasts this page */ }
    selected = null;
    return load();
  }
  function chooser() {
    const m = DOC?.machines; if (!m || VIEW !== 'machine') return '';
    const shown = DOC.host || m.hub;
    return html`<p class="counts machine-pick"><label>Machine <select id="machine-host">${m.hosts.map(h => html`<option value="${h}"${h === shown ? html` selected` : ''}>${h === m.hub ? `${h} · hub` : h}</option>`)}</select></label>${
      DOC.host ? html` · Readings of ${DOC.host}, as its own job stored them` : ''}${m.error ? html` · ${m.error}` : ''}</p>`;
  }
  const F = { state: new Set(), area: new Set() };
  const byId = id => ROWS.find(r => r.id === id);
  // Show: the areas' rows, or the ignored ones. State does the rest: it starts on the states that want something
  // (PROBLEMS) and Clear all returns it there, the page's home; with no State pressed every row shows (sd:7207).
  const QUIET = new Set(['ok', 'queued']);
  const PROBLEMS = STATES.filter(s => !QUIET.has(s));
  const atStart = () => F.state.size === PROBLEMS.length && PROBLEMS.every(s => F.state.has(s));
  // Repos: a row of a registered repository carries managed (health_screen._tag_managed); Managed hides the others. A row of no repo stays.
  const scoped = r => repos === 'all' || r.managed !== false;
  // An ok or queued row counts as the area's only while its State is pressed or none is: the tally and the quiet line read it.
  const drawn = r => scoped(r) && (!QUIET.has(r.state) || !F.state.size || F.state.has(r.state));
  const listed = () => (showing === 'ignored' ? IGNORED : ROWS).filter(scoped);
  const viewOf = area => PAGE_VIEWS.find(v => v.areas.includes(area))?.id;

  // A clock time in the viewer's zone, with its label (pack sd:9260): Thu 14:05 MST.
  const zoned = iso => new Date(iso).toLocaleString([], { weekday: 'short', hour: '2-digit', minute: '2-digit', timeZoneName: 'short' });
  const clockCell = iso => html`<time datetime="${iso}">${zoned(iso)}</time>`;
  // The design's size wording, from a count of KiB.
  const size = kb => kb >= 1024 ** 3 ? `${(kb / 1024 ** 3).toFixed(2)} TiB` : kb >= 1024 ** 2 ? `${(kb / 1024 ** 2).toFixed(1)} GiB` : `${Math.round(kb / 1024)} MiB`;
  // A lamp's value is the summary's phrases (health_screen._lamp): `b` bold, then `t`.
  const lampValue = l => l.value.map((v, i) => html`${i ? ' · ' : ''}<span class="ph">${v.b ? html`<b>${v.b}</b> ` : ''}${v.t}</span>`);

  // The box says "repo, path, branch": a path can sit only in a row's facts (a checkout) or its list (registered worktrees).
  const text = r => [r.what, r.detail, r.kind, ...Object.values(r.facts || {}), ...(r.list || []).map(x => x.text ?? x)].join(' ').toLowerCase();
  const passes = r => drawn(r) && (!F.state.size || F.state.has(r.state)) && (!F.area.size || F.area.has(r.area))
    && (!query || text(r).includes(query));
  const filtering = () => !!(!atStart() || F.area.size || query);
  const quiet = a => a.rows.filter(r => scoped(r) && !drawn(r)).length;
  // An area stays on the page while it has a row the filters pass, or while State is at its start or empty and only the
  // Area filter names it: a lamp for an area with no reader then shows what that area does not read.
  const areaShown = a => a.rows.some(passes) || ((!F.state.size || atStart()) && !query && (!F.area.size || F.area.has(a.id)));
  const loud = { warning: 0, caution: 1, unknown: 2 };

  // The summary's lamps, with a chosen machine's own in place of the hub's Disk and Services (review round 6).
  const ownLamps = () => S.machine?.DOC?.host && S.machine.DOC.lamps;
  const lampsNow = () => {
    const L = SUMMARY ? SUMMARY.lamps : [], own = ownLamps();
    return own ? L.map(l => own.find(o => o.id === l.id) || l) : L;
  };
  // Every area's lamp, from the summary: pressing one opens its view with its area alone, or toggles it in this view.
  function lamps() {
    const L = lampsNow();
    put($('annunciator'), cells([...L.filter(l => !NO_LAMP.has(l.id)).map(l => ({ button: true, state: l.state, pressed: F.area.has(l.id),
      attrs: html` data-area="${l.id}"`, label: l.name, mark: I(LOOK[l.id]?.[0] || 'circle-help'), val: lampValue(l),
      sr: `state ${l.state} · ${l.view === VIEW ? `shows only ${l.name} rows` : `opens ${PAGE_VIEWS.find(v => v.id === l.view)?.label}`}` }))]));
  }

  // ---------- Views ----------
  // The shell draws the switch from PAGE_VIEWS (shell.js, view:start); onView reads a view on its first shell:view.
  const hashView = () => (/(?:^#|&)view=([a-z0-9-]+)/.exec(location.hash || '') || [])[1];
  // A view's reading becomes the page's rows; an unread view has none.
  function use(id) {
    ({ DOC, AREAS, ROWS, SNOOZED, IGNORED } = S[id] || { DOC: null, AREAS: [], ROWS: [], SNOOZED: [], IGNORED: [] });
    if (DOC) document.body.dataset.observed = DOC.read; else delete document.body.dataset.observed;
  }
  // A view opens with its own rows, keeping only the Area values it holds; the first time, its reader starts.
  function onView({ id, first }) {
    VIEW = id; use(id);
    const keep = PAGE_VIEWS.find(v => v.id === id).areas;
    [...F.area].forEach(a => { if (!keep.includes(a)) F.area.delete(a); });
    lamps(); window.snooze.draw($('snoozed'), SNOOZED); apply();
    if (first) { readers[id] = shell.read(spec(id)); load(); }
  }

  // sd:5504: Activity's bar (shell.list.fbar), as on Reports: Show and State as segments with their counts, Area as a pick
  // (a lamp presses it too), the picked areas and the query as tokens, then Clear all. An area's title line counts its rows
  // while a filter hides some of them (sd:5281).
  function filters() {
    const L = listed(), list = shell.list, n = f => L.filter(f).length;
    const mine = ROWS.filter(scoped), shows = [['findings', mine.filter(r => !QUIET.has(r.state)).length], ['ignored', IGNORED.filter(scoped).length]];
    const tagged = ROWS.filter(r => 'managed' in r);
    return html`<div class="filters toolbar-2">${list.fbar(html`${list.fseg('show', 'Show', shows.map(([v, c]) => ({ v, pressed: showing === v, label: html`${v} <span class="n">${String(c)}</span>` })))}${
      tagged.length ? list.fseg('repos', 'Repos', [['managed', tagged.filter(r => r.managed).length], ['all', tagged.length]].map(([v, c]) => ({ v, pressed: repos === v, label: html`${v} <span class="n">${String(c)}</span>` }))) : ''}${
      list.fseg('state', 'State', STATES.filter(s => F.state.has(s) || L.some(r => r.state === s)).map(s => { const c = n(r => r.state === s);
        return { v: s, pressed: F.state.has(s), title: `${s}: ${c}`, label: html`<span class="g-${s}" aria-hidden="true">${GLYPH[s]}</span><span class="n">${String(c)}</span><span class="sr"> ${s}</span>` }; }))}${
      AREAS.length > 1 ? list.pick('area', 'Area', AREAS.map(a => [a.id, a.name, n(r => r.area === a.id)]), F.area, { multi: true }) : ''}${
      [...F.area].map(a => list.tok('area', a, AREAS.find(x => x.id === a)?.name || a))}${query ? list.tok('q', '', `“${query}”`) : ''}`, { clear: filtering() })}</div>`;
  }

  function areas() {
    const count = (a, s) => a.rows.filter(r => r.state === s && scoped(r)).length;
    return html`${AREAS.filter(areaShown).map(a => { const rows = a.rows.filter(passes); return html`<section class="area" id="a-${a.id}" aria-labelledby="h-${a.id}">
      <div class="sec-head"><h2 id="h-${a.id}">${I(LOOK[a.id]?.[0] || 'circle-help')}${a.name}<button class="help" type="button" aria-label="Help: ${a.name}" data-help="${LOOK[a.id]?.[1] || ''}">${I('circle-help')}</button></h2>
        <p class="tally">${rows.length < a.rows.filter(drawn).length ? html`<span>${shell.list.count(rows.length, a.rows.filter(drawn).length, 'row')}</span>` : ''}${a.read && !a.error ? STATES.filter(s => count(a, s)).map(s => html`<span class="g-${s}">${GLYPH[s]} ${count(a, s)} ${s}</span>`) : html`<span class="g-unknown">${GLYPH.unknown} not read</span>`}${a.at ? html`<span>read <time class="rel" datetime="${a.at}"></time></span>` : ''}</p></div>
      ${!a.read ? html`<p class="unread"><b>No reader yet.</b> The dashboard has no collector for this area, so nothing here is known: ${a.missing.join(', ')}.</p>`
        : a.error ? html`<p class="unread"><b>Not read:</b> ${a.error}</p>`
        : a.missing.length ? html`<p class="unread"><b>Not read here:</b> ${a.missing.join(', ')}.</p>` : ''}
      ${a.stale ? html`<p class="unread"><b>Not re-read:</b> ${a.stale}</p>` : ''}
      ${a.read && !a.error ? pre(a) : ''}
      ${rows.length ? html`<table class="ledger" aria-labelledby="h-${a.id}" data-tfilter="page" data-toolbar="toolbar">
        <thead><tr><th scope="col"><span class="sr">State</span></th><th scope="col">Finding</th><th scope="col">Fix</th></tr></thead>
        <tbody>${rows.map((r, i) => html`<tr data-id="${r.id}" aria-selected="${r.id === selected ? 'true' : 'false'}"${i > 0 && rows[i - 1].state !== r.state ? html` class="band-start"` : ''}>
          <td class="g g-${r.state}"><span aria-hidden="true">${GLYPH[r.state]}</span><span class="sr">${r.state}</span></td>
          <td class="what"><button type="button">${r.what}</button><span class="detail">${r.detail}${r.read_at ? html` · read ${clockCell(r.read_at)}` : ''}</span></td>
          <td class="fix">${shell.commands.rowActions(r.id)}</td></tr>`)}</tbody></table>` : ''}
      ${a.read && !a.error && quiet(a) && !filtering() ? html`<p class="quiet">${rows.length ? '' : 'Nothing wants you here. '}${plural(quiet(a), 'ok or queued row')} not shown: press ok or queued in State to list ${quiet(a) === 1 ? 'it' : 'them'}.</p>` : ''}
    </section>`; })}`;
  }

  // Show ignored (sd:5470): the rows Ignore hid, which the filters narrow, in place of the areas. A row here has no Details,
  // only Unignore, so it carries no data-id and the selection passes it by.
  function ignored() {
    const rows = IGNORED.filter(passes), name = id => AREAS.find(a => a.id === id)?.name;
    return html`<section class="area" aria-labelledby="h-ignored"><div class="sec-head"><h2 id="h-ignored">${I('eye-off')}Ignored</h2>${rows.length < IGNORED.length ? html`<p class="tally">${shell.list.count(rows.length, IGNORED.length, 'row')}</p>` : ''}</div>
      ${rows.length ? html`<table class="ledger" aria-labelledby="h-ignored" data-tfilter="page" data-toolbar="toolbar"><thead><tr><th scope="col"><span class="sr">State</span></th><th scope="col">Finding</th><th scope="col">Unignore</th></tr></thead>
        <tbody>${rows.map(r => html`<tr><td class="g g-${r.state}"><span aria-hidden="true">${GLYPH[r.state]}</span><span class="sr">${r.state}</span></td>
          <td class="what">${r.what}<span class="detail">${name(r.area)} · ${r.detail}</span></td>
          <td class="fix">${shell.commands.rowActions(`ignored:${r.id}`)}</td></tr>`)}</tbody></table>`
        : html`<p class="quiet">${IGNORED.length ? 'No ignored row matches the filter.' : 'No row here is ignored. Ignore on a row hides it until its problem changes.'}</p>`}
    </section>`;
  }

  // What an area draws above its ledger: v1's counts line and warnings, and for Protection the matrix and its table.
  const warn = w => html`<p class="warn" role="status"><span class="g-unknown" aria-hidden="true">${GLYPH.unknown}</span> ${w}</p>`;
  function pre(a) {
    const x = a.extra || {};
    if (a.id === 'disk') return x.volumes?.length ? html`<div class="bars" role="group" aria-label="Volume use: ${x.volumes.map(v => `${v.name} ${v.capacity}%`).join(', ')}">${x.volumes.map(v => html`<span class="name" title="${v.mount}">${v.name}</span><svg aria-hidden="true"><rect class="track" x="0.5" y="0.5" width="99%" height="11" rx="2"/><rect class="fill" x="0.5" y="0.5" width="${(v.used_kb / v.size_kb * 100 || 0).toFixed(1)}%" height="11" rx="2"/></svg><span class="v">${v.capacity}% · ${size(v.avail_kb)} free</span>`)}</div>` : '';
    // Clean all safe (sd:5105) heads Checkouts: one button for every cleanup row there whose own command would run.
    if (a.id === 'wt') return html`<p class="counts">${plural(safe().length, 'cleanup')} ready in Checkouts · ${shell.commands.rowActions('cleanup:safe')}</p>`;
    // Service status (pack sd:9260): its counts, and Settings' status services list for add and remove.
    if (a.id === 'stat') return html`<p class="counts">${plural(Object.values(x.counts || {}).reduce((n, c) => n + c, 0), 'service')} · ${['outage', 'degraded', 'unknown', 'ok'].map(s => `${x.counts?.[s] || 0} ${s}`).join(' · ')} · <a href="${x.settings}">Add or remove services in Settings</a></p>`;
    if (a.id === 'ctr') return html`<p class="counts">${plural(x.running, 'container')} running · ${x.holding} mount a checkout under ${x.root}</p>`;
    if (a.id === 'ports') return html`<p class="counts">${x.counts.configured} configured ports · ${x.counts.listening} listening · ${x.counts.unknown} unknown</p>${x.warnings.map(warn)}`;
    if (a.id !== 'prot' || !x.columns) return '';
    const cols = x.columns.map(byId).filter(r => r && scoped(r));
    const n = Object.fromEntries(Object.keys(x.counts).map(k => [k, cols.filter(r => r.status === k).length]));
    const lines = x.checks.length + 1;
    return html`<p class="counts">${n.protected} protected · ${n.unprotected} unprotected · ${n.unknown} unknown · ${shell.commands.rowActions('collector:protection')}</p>
      ${x.reasons.map(r => warn(`${n.unknown} unknown, ${x.reasons.length === 1 ? 'all for one reason' : 'among them'}: ${r}.`))}
      ${cols.length ? html`<p class="legend"><span><span class="sw gap"></span>gap</span><span><span class="sw pass"></span>ok</span><span><span class="sw na"></span>does not apply</span><span><span class="sw unk"></span>not read</span></p>
      <div class="pmx-wrap" role="region" aria-label="Protection matrix: ${lines} lines across ${cols.length} repositories" tabindex="0">
        <div class="pmx">
          <ul class="pmx-labels" aria-hidden="true"><li>Status</li>${x.checks.map(l => html`<li>${l}</li>`)}</ul>
          <div class="pmx-cols">${cols.map(r => html`<button type="button" class="pmx-col" data-row="${r.id}" data-status="${r.status}" aria-current="${r.id === selected ? 'true' : 'false'}" aria-label="${r.name}: ${r.status}${r.status === 'unknown' ? '' : `, ${plural(r.gaps, 'gap')}`}"><svg viewBox="0 0 10 ${lines * PH}" preserveAspectRatio="none" aria-hidden="true">
            ${r.status === 'unknown' ? '' : html`<rect class="st-${r.status}" x="1" y="1" width="8" height="${PH - 2}"/>${r.cells.map(([, c], i) => { const y = (i + 1) * PH;
              return c === 'gap' ? html`<rect class="c-gap" x="1" y="${y + 1}" width="8" height="${PH - 2}"/>` : c === 'ok' ? html`<rect class="c-ok" x="1" y="${y + PH / 2 - 0.75}" width="8" height="1.5"/>` : ''; })}`}</svg></button>`)}</div>
          <ul class="pmx-n" aria-hidden="true"><li>${n.unprotected}</li>${x.checks.map((_, i) => html`<li>${cols.filter(r => r.cells?.[i]?.[1] === 'gap').length}</li>`)}</ul>
        </div>
      </div>
      <details class="pmx-table"><summary>Table: ${cols.length} repositories × ${x.checks.length} checks</summary>
        <div class="tscroll" role="region" aria-labelledby="pmx-cap" tabindex="0"><table>
          <caption id="pmx-cap">Branch protection per registered repository, unprotected first, then unknown, then protected. GAP carries its sentence; — does not apply; not read where the repository was not read.</caption>
          <thead><tr><th scope="col">Repository</th><th scope="col">Status</th><th scope="col">Branch</th>${x.checks.map(l => html`<th scope="col">${l}</th>`)}<th scope="col">Reason</th></tr></thead>
          <tbody>${cols.map(r => html`<tr><th scope="row">${r.slug ? html`<a href="https://github.com/${r.slug}/settings/branches" target="_blank" rel="noopener noreferrer">${r.slug}</a>` : r.facts.Repository}</th><td>${r.status}</td><td>${r.branch}</td>
            ${x.checks.map((_, i) => { const c = r.cells[i]; return html`<td>${r.status === 'unknown' ? 'not read' : c?.[1] === 'gap' ? html`<b>GAP</b> <span class="sent">${c[2]}</span>` : c?.[1] === 'ok' ? 'ok' : '—'}</td>`; })}<td>${r.reason}</td></tr>`)}</tbody>
        </table></div></details>` : ''}`;
  }
  const cellText = c => c[1] === 'gap' ? `GAP · ${c[2]}` : c[1] === 'ok' ? 'ok' : '—';

  function show(id) {
    const r = byId(id); if (!r) return;
    const a = AREAS.find(x => x.id === r.area);
    put($('details'), html`<p class="kind"><span class="g-${r.state}" aria-hidden="true">${GLYPH[r.state]}</span> ${r.kind} · ${r.state}</p>
      <h2>${r.what}</h2>
      <dl>${Object.entries(r.facts || {}).map(([k, v]) => html`<dt>${k}</dt><dd>${v}</dd>`)}<dt>Source</dt><dd>${a.source}</dd><dt>Read</dt><dd>${r.read_at ? html`${clockCell(r.read_at)} · <time class="rel" datetime="${r.read_at}"></time>` : html`<time class="rel" data-long datetime="${a.at || DOC.read}"></time>`}</dd></dl>
      ${r.type === 'branch protection' ? (r.status === 'unknown' ? html`<h3>Checks</h3><p class="why">No cell is shown: ${r.reason}.</p>`
        : html`<h3>Checks</h3>${r.sentence ? html`<p class="why">${r.sentence}</p>` : ''}<dl class="checks">${r.cells.map(c => html`<dt>${c[0]}</dt><dd class="c-${c[1]}">${cellText(c)}</dd>`)}</dl>`) : ''}
      ${r.type === 'service status' && r.url ? html`<p><a class="ext" href="${r.url}" target="_blank" rel="noopener noreferrer">Status page${I('arrow-up-right')}</a></p>` : ''}
      ${r.slug ? html`<p><a class="ext" href="https://github.com/${r.slug}/settings/branches" target="_blank" rel="noopener noreferrer">${r.slug} · settings/branches${I('arrow-up-right')}</a></p>` : ''}
      ${r.list?.length ? html`<h3>${r.list_head || 'Rows behind it'}</h3><ul class="list">${r.list.map(x => html`<li>${x.href ? html`<a class="ext" href="${x.href}" target="_blank" rel="noopener noreferrer">${x.text}${I('arrow-up-right')}</a>` : x.text ?? x}</li>`)}</ul>` : ''}
      <h3>Fix</h3>${shell.commands.bar(r.id)}
      ${r.note ? html`<p class="why">${r.note}</p>` : ''}`);
    shell.commands.select(r.id);
    shell.suggest([`Why is this ${r.state}?`, 'What does the fix touch?', 'Which Health row should I fix first?']);
  }
  function select(id, open) {
    selected = id;
    document.querySelectorAll('.ledger tbody tr[data-id]').forEach(tr => tr.setAttribute('aria-selected', tr.dataset.id === id ? 'true' : 'false'));
    document.querySelectorAll('.pmx-col').forEach(b => b.setAttribute('aria-current', b.dataset.row === id ? 'true' : 'false'));
    show(id);
    if (open) shell.openPane('tab-details');
  }

  function writeURL() {
    const q = new URLSearchParams();
    if (!atStart()) q.set('state', [...F.state].join(','));
    if (F.area.size) q.set('area', [...F.area].join(','));
    if (query) q.set('q', query);
    if (showing !== 'findings') q.set('show', showing);
    if (repos !== 'managed') q.set('repos', repos);
    shell.url(q); // the shell keeps ?row=
  }
  // The open view's panel: its filters, the filter note and its areas. Another view's panel keeps what it last drew.
  function apply() {
    if (!VIEW) return;
    put($(`body-${VIEW}`), html`${chooser()}${DOC ? filters() : ''}${DOC && showing === 'ignored' ? ignored() : areas()}`);
    // The selection is always a row the viewer can see: the first visible one, or none with the reason.
    // sd:9312: S[VIEW] is undefined until the view's first read settles, null after a failed one.
    shell.reconcile({ rows: document.querySelectorAll('.ledger tbody tr[data-id]'), current: selected, select: id => select(id, false),
      clear: () => { selected = null; put($('details'), html`<p class="why">${!DOC ? (S[VIEW] === undefined ? 'Nothing is selected: Health is still being read.' : 'Nothing is selected: Health could not be read.') : showing === 'ignored' ? 'Nothing is selected: an ignored row has Unignore only. Show findings to pick a row.' : !ROWS.some(scoped) ? (ROWS.length ? 'Nothing is selected: every row here is of a repo that is not managed. Repos all lists them.' : 'Nothing is selected: no area raised a row.') : filtering() ? 'No row matches the filter, so nothing is selected.' : 'Nothing is selected: no row here wants you. Press ok or queued in State to list those rows.'}</p>`); } });
    writeURL();
  }
  // Clear all returns State to its start, the problem states, not to no State pressed: the page's home is what wants you.
  function clearFilters() { const had = filtering(); F.state = new Set(PROBLEMS); F.area.clear(); query = ''; shell.filter(''); lamps(); apply(); return had; }

  // The badge is the summary's (sd:3446, health_screen._attention over every area); the subhead says it in words.
  function attention() {
    window.PAGE_ATTENTION = SUMMARY && SUMMARY.attention || { state: 'unknown', n: 0, what: 'rows' };
    shell.attention(window.PAGE_ATTENTION);
    const a = window.PAGE_ATTENTION, failed = lampsNow().filter(l => l.error).length;
    put($('subhead'), SUMMARY ? html`${plural(PAGE_VIEWS.length, 'view')} · ${a.n ? `${a.n} ${a.state} ${a.n === 1 ? 'row wants' : 'rows want'} you` : 'no row wants you'}${failed ? ` · ${plural(failed, 'area')} not read` : ''} · read <time class="rel" datetime="${SUMMARY.read}"></time>`
      : html`Health's summary could not be read.`);
  }
  // The summary: every lamp, the badge and each view's count of rows that want you, read at start and again after a view's
  // read. A failed one clears the strip; a view with an area not read has no count.
  const readSummary = () => shell.getJSON('/api/health/summary').then(doc => { SUMMARY = doc; }, () => { SUMMARY = null; })
    .then(() => {
      attention(); lamps();
      // A chosen machine's tab counts its own areas alone: the view shows no other.
      PAGE_VIEWS.forEach(v => { const L = v.id === 'machine' && ownLamps() || lampsNow().filter(l => l.view === v.id);
        shell.view.count(v.id, L.length && L.every(l => l.n != null) ? L.reduce((n, l) => n + l.n, 0) : null); });
    });

  // One reader per view (read.js, sd:2418) holds the guards: of overlapping re-reads only the newest draws, a row the read no
  // longer lists runs no command and loses its pick, and a failed read leaves no row, lamp or command from the last one.
  // Each puts and retires only its own view's rows; one for a view not open keeps its reading and draws nothing.
  const load = () => Promise.resolve(readers[VIEW] && readers[VIEW].load()).then(() => readSummary());
  // Read again (sd:5638): the shell's head button reads the shown view and the lamps again; sd:6697
  // dropped the Observed lamp. Re-run collector, on the protection collector, runs the sync first.
  window.PAGE_AGAIN = { title: 'Refresh this view and the status lights', run: load };
  const spec = view => ({
    source: view === 'machine' ? machineSource : `/api/health/${view}`, what: `the ${PAGE_VIEWS.find(v => v.id === view).label} rows`,
    // Each view's reading state sits in its own panel, and a late read of a view not open leaves the selection alone.
    slot: `state-${view}`, active: () => view === VIEW,
    adopt: doc => {
      const areas = doc.areas;
      // Each row carries its area: the filter reads r.area on a.rows as well as on ROWS (sd:3708).
      areas.forEach(a => { [...a.rows, ...a.snoozed].forEach(r => { r.area = a.id; }); a.rows.sort((x, y) => RANK[x.state] - RANK[y.state]); });
      // An ignored row (sd:5470) is a snooze with no end: Show ignored lists it, not the Snoozed list under the page.
      const hidden = areas.flatMap(a => a.snoozed), forever = r => r.until === window.snooze.FOREVER;
      S[view] = { DOC: doc, AREAS: areas, ROWS: areas.flatMap(a => a.rows), SNOOZED: hidden.filter(r => !forever(r)), IGNORED: hidden.filter(forever) };
      if (view === VIEW) use(view);
      const { ROWS: rows } = S[view], snoozed = hidden, failed = areas.filter(a => a.error || a.stale), source = view === 'machine' ? machineSource() : `/api/health/${view}`;
      return { objects: [...rows.map(r => ({ ...r, label: r.what })), ...snoozed.map(window.snooze.object)],
        state: failed.length ? { kind: 'partial', text: `${failed.map(a => `${a.name}: ${a.error || a.stale}`).join(' · ')}. The other areas are current.`, source }
          : doc.snooze_error ? window.snooze.unread(doc.snooze_error, source) : null };
    },
    // Nothing from the last read stays on screen: rows, lamps, the observed time and the badge.
    clear: () => { S[view] = null; if (view === VIEW) use(view); },
    draw: () => { if (view === VIEW) { lamps(); window.snooze.draw($('snoozed'), SNOOZED); } },
    current: () => view === VIEW ? selected : null,
    first: () => S[view]?.ROWS[0]?.id,
    // apply() reconciles against the rows the filters show; the new reading's Details are drawn here.
    select: id => { if (view !== VIEW) return; selected = id; apply(); if (byId(selected)) show(selected); },
    unselect: () => { if (view !== VIEW) return; selected = null; apply(); },
  });

  // ---------- Commands (the design source's commands.md) ----------
  // Ids, labels and keys are the design's. Prune, Delete merged and Remove build output run here (sd:3393): each confirms
  // with the count and the names, then posts the row and the revision it showed; the server reads the subject again and
  // refuses a changed one. A row with `disabled` (a merged branch a worktree holds) names why git would refuse it.
  // None runs in the shell's bulk: its confirm names four rows at most, and its tally counts an unknown outcome as landed.
  // Clean all safe (sd:5105) is its own command instead: its confirm names every row, and an unknown outcome stops it.
  const copyOnly = o => `Not run here: copy the line from Details and run it in a terminal · ${o.label}`;
  // sd:5886: the read actions run on the server (run_actions.py) from the row's own id: `rs:<path>` or `vol:<mount>`.
  // None reads Health again after it: a reread draws Details anew, and Read again shows what a run changed.
  const run = (o, action, ids) => window.commands.runAction(o, action, ids);
  const pathOf = o => o.id.slice(o.id.indexOf(':') + 1), RECHECK = /^(doc|cred):/;
  const openAlerts = (o, page) => { window.open(`https://github.com/${o.slug}/security/${page}`, '_blank', 'noopener'); return 'Opened on GitHub in a new tab'; };
  const DOCTOR_FIX = window.commands.fix('doctor check', { item: o => o.filed.item.id, report: () => false,
    when: o => o.state === 'ok' ? 'a passing doctor line has nothing to fix' : true, post: (o, path) => shell.post(path, { revision: o.filed.revision }) });
  const names = xs => xs.length > 6 ? `${xs.slice(0, 6).join(', ')} and ${xs.length - 6} more` : xs.join(', ');
  // A 4xx wrote nothing (health_cleanup); a 502 or no answer may follow a step that landed, so the page reads again either way.
  const cleanup = o => shell.post('/api/health/cleanup', { row: o.id, revision: o.revision }).then(out => {
    load();
    const kept = out.refused.map(x => `${x.name} (${x.why})`);
    return `${o.label}: ${out.done.length} done${kept.length ? ` · kept ${kept.length}: ${kept.join(', ')}` : ''}`;
  }, e => {
    load();
    if (e.status >= 400 && e.status < 500) throw e;
    return `Not known what changed · ${o.label}: ${e.message}. Health was read again; check the row before you act again.`;
  });
  const revised = o => !!o.revision || 'the row carries no revision: read Health again';
  // The Checkouts cleanups by row type: when each runs, what it does in a few words, and its line. Each command's `when`
  // is its `ready`, and Clean all safe runs a row only where that answers true.
  const CLEAN = {
    'worktree registrations': { ready: o => o.unreadable ? `${plural(o.unreadable, 'registration')} of this checkout could not be read, and prune would judge them itself` : revised(o),
      what: o => `prunes ${names(o.list)}`, cli: o => `git -C ${shell.shq(o.repo_path)} worktree prune -v` },
    'merged branches': { ready: o => o.disabled || revised(o), what: o => `deletes ${names(o.branches)}`, cli: o => o.cli },
    'merged worktrees': { ready: o => revised(o) === true ? (o.removable.length ? true : 'every merged worktree has a reason to stay: Details lists each') : revised(o),
      what: o => `removes ${names(o.removable)} with their branches`, cli: o => o.cli },
  };
  const safe = () => (S.checkouts?.ROWS || []).filter(r => CLEAN[r.type]?.ready(r) === true);
  // It runs the rows its confirm listed, each with its own revision, so a read in between adds none the operator did not
  // see. A refusal (4xx) wrote nothing for that row and the run goes on; an unknown outcome stops it there.
  const cleanAll = async rows => {
    const said = [];
    for (const r of rows) {
      try {
        const out = await shell.post('/api/health/cleanup', { row: r.id, revision: r.revision });
        said.push(`${r.what}: ${out.done.length} done${out.refused.length ? `, kept ${out.refused.length}` : ''}`);
      } catch (e) {
        if (!(e.status >= 400 && e.status < 500)) {
          load();
          return `Clean all safe stopped · not known what changed · ${r.what}: ${e.message}.${said.length ? ` Before it: ${said.join(' · ')}.` : ''} Health was read again; check the rows before you act again.`;
        }
        said.push(`${r.what}: not changed (${e.message})`);
      }
    }
    load();
    return `Clean all safe · ${said.join(' · ')}`;
  };
  // A service write (sd:3707) answers the request launchd took and what it reads now; the page reads again either way.
  const service = action => o => shell.post(`/api/services/${encodeURIComponent(o.facts.Label)}/${action}`, { revision: o.revision }).then(out => {
    load();
    return `${o.facts.Label}: ${out.request.status} · ${out.request.message}`;
  }, e => {
    load();
    if (e.status >= 400 && e.status < 500) throw e;
    return `Not known what changed · ${o.facts.Label}: ${e.message}. Health was read again; check the row before you act again.`;
  });
  const statusHref = o => (o.list || []).find(x => x.href)?.href || o.url || '';
  // A satellite's row is a stored reading: an action here would run on this hub, so its line is for that host.
  const local = o => !o.host || `read on ${o.host}: this hub would run it on itself; run the line on ${o.host}`;
  const can = action => o => local(o) !== true ? local(o)
    : revised(o) === true ? (o.can[action].allowed || o.can[action].reason) : revised(o);
  window.PAGE_COMMANDS = [
    { label: 'Read Health again', icon: 'rotate-ccw', run: () => load() },
    { label: 'Open Sessions in Management', icon: 'folder-x', run: () => { location.href = '/management#view=sessions'; } },
  ];
  window.PAGE_LIST = { rows: () => document.querySelectorAll('.ledger tbody tr[data-id]'), current: () => selected,
    select: id => select(id, false), clear: () => clearFilters() };

  document.addEventListener('DOMContentLoaded', () => {
    const C = shell.commands;
    // The protection collector is one object for the page, not a row of a reading: no read retires it.
    C.put({ id: 'collector:protection', type: 'collector', label: 'protection collector' });
    C.put({ id: 'cleanup:safe', type: 'safe cleanups', label: 'Clean all safe' });
    C.register(
      // sd:5886: a doctor or credentials row runs its read and shows it in Details; every other check reads Health again.
      { id: 'check.recheck', on: 'check', label: 'Re-check', does: o => RECHECK.test(o.id) ? 'Runs this check again and shows its output in Details.' : 'Reads Health again.', key: 'e', icon: 'rotate-ccw', risk: 'safe', executes: true, primary: () => true,
        cli: o => o.cli, sends: o => RECHECK.test(o.id) ? `POST /api/actions/run {action: check.run, row: ${o.id}}` : 'GET /api/health/summary',
        run: o => RECHECK.test(o.id) ? run(o, 'check.run', { row: o.id }) : (load(), 'Reading Health again') },
      // pack sd:6684: a doctor row runs the doctor again, and Fix files its finding in the system repo, or reuses the open item
      // with its ref, then starts a builder on it through the item's Fix (commands.js).
      { id: 'doctor check.run', on: 'doctor check', label: 'Run doctor', does: 'Runs the machine self-check and shows the result in Details.', key: 'e', icon: 'rotate-ccw', risk: 'safe', executes: true, primary: () => true,
        cli: o => o.cli, sends: o => `POST /api/actions/run {action: check.run, row: ${o.id}}`, run: o => run(o, 'check.run', { row: o.id }) },
      { ...DOCTOR_FIX, sends: o => `POST /api/items {title, body, ref: doctor:${pathOf(o)}}, then POST /api/items/<id>/fix {revision}`,
        consequence: o => `Files "${o.what}" in the system repo, or reuses the open item that has it, then opens a Claude session that fixes it (herdr).`,
        run: o => shell.post('/api/items', { title: o.what, body: `Doctor: ${o.detail}`, ref: `doctor:${pathOf(o)}` }).then(filed => DOCTOR_FIX.run({ ...o, filed })) },
      { id: 'storage folder.du', on: 'storage folder', label: 'Show biggest', does: 'Lists this folder\'s biggest entries in Details.', key: 'b', icon: 'chart-no-axes-column', risk: 'safe', executes: true, primary: () => true,
        cli: o => o.cli, sends: o => `POST /api/actions/run {action: storage.du, path: ${pathOf(o)}}`, run: o => run(o, 'storage.du', { path: pathOf(o) }), when: local },
      { id: 'build output.rm', on: 'build output', label: 'Remove build output', does: 'Deletes build output in merged, clean worktrees; this cannot be undone.', key: 'r', icon: 'folder-x', risk: 'confirm', executes: true, primary: () => true,
        when: o => revised(o) === true ? (o.remove.length ? true : 'every merged worktree has uncommitted changes: nothing is removed') : revised(o),
        cli: o => o.cli, consequence: o => `Deletes ${o.remove.length} build output folders in merged, clean worktrees: ${names(o.remove)}.`
          + (o.kept.length ? ` Keeps ${o.kept.length} with uncommitted changes: ${names(o.kept)}.` : '') + ' Each worktree is checked again first. This cannot be undone.', run: cleanup },
      // Volume rows are the build's: the design draws volumes as bars only. Its line shows the volume's use in a terminal.
      { id: 'volume.df', on: 'volume', label: 'Show use', does: 'Shows this volume\'s use in Details.', key: 'b', icon: 'gauge', risk: 'safe', executes: true, primary: () => true,
        cli: o => o.cli, sends: o => `POST /api/actions/run {action: volume.df, path: ${pathOf(o)}}`, run: o => run(o, 'volume.df', { path: pathOf(o) }), when: local },
      { id: 'worktree registrations.prune', on: 'worktree registrations', label: 'Prune registrations', does: 'Removes registrations of worktrees whose folders are gone; cannot be undone.', key: 'p', risk: 'confirm',
        executes: true, primary: () => true, cli: CLEAN['worktree registrations'].cli, when: CLEAN['worktree registrations'].ready,
        consequence: o => `Removes ${o.facts.Registered} worktree registrations whose directories are gone: ${names(o.list)}. No directory is touched.`, run: cleanup },
      { id: 'merged branches.delete', on: 'merged branches', label: 'Delete merged', does: 'Deletes local branches already merged; this cannot be undone.', key: 'd', risk: 'confirm', executes: true,
        primary: () => true, when: CLEAN['merged branches'].ready, cli: o => o.cli,
        consequence: o => `Deletes ${o.facts.Merged} local branches already in origin's default branch: ${names(o.branches)}. An unmerged branch is never deleted.`, run: cleanup },
      { id: 'merged worktrees.remove', on: 'merged worktrees', label: 'Remove merged', does: 'Removes worktrees the lane merged, with their branches; cannot be undone.', key: 'r', icon: 'folder-x', risk: 'confirm',
        executes: true, primary: () => true, when: CLEAN['merged worktrees'].ready, cli: o => o.cli,
        consequence: o => `Removes ${o.removable.length} worktrees the lane merged, with their build output, their local branch and origin's: ${names(o.removable)}.`
          + (o.kept.length ? ` Keeps ${o.kept.length}: ${names(o.kept.map(x => `${x.name} (${x.why})`))}.` : '') + ' Each is checked again first. This cannot be undone.', run: cleanup },
      { id: 'safe cleanups.clean', on: 'safe cleanups', label: 'Clean all safe', does: 'Runs every Checkouts cleanup that is ready, after listing each.', key: 'a', risk: 'confirm',
        executes: true, primary: () => true, when: () => safe().length ? true : 'no Checkouts cleanup is ready: each row says why',
        cli: () => safe().map(r => CLEAN[r.type].cli(r)).join('\n'),
        consequence: o => { o.listed = safe(); return `Runs ${plural(o.listed.length, 'cleanup')} in turn, each checked again first: ${o.listed.map(r => `${r.what} (${CLEAN[r.type].what(r)})`).join('; ')}. This cannot be undone.`; },
        run: o => cleanAll(o.listed || safe()) },
    );
    // Services (sd:3707): Restart and Start run sd services' code through Operations' route. sd_db.services protects the
    // dashboard's own service, and a restart from its page would end the request that asks, so that row is copy only.
    C.register(
      { id: 'service.restart', on: 'service', label: 'Restart', does: 'Restarts the service; open connections drop.', key: 'r', icon: 'rotate-ccw', risk: 'confirm', executes: true,
        primary: o => o.facts.State === 'running', when: can('restart'),
        cli: o => `sd services restart ${shell.shq(o.facts.Label)}`,
        consequence: o => `Stops ${o.facts.Label} and starts it again. Its open connections drop.`,
        run: service('restart') },
      { id: 'service.start', on: 'service', label: 'Start', does: 'Starts the service.', key: 's', icon: 'play', risk: 'confirm', executes: true,
        primary: o => o.facts.State !== 'running', when: can('start'),
        cli: o => `sd services start ${shell.shq(o.facts.Label)}`,
        consequence: o => `Starts ${o.facts.Label}.`,
        run: service('start') },
      { id: 'dashboard service.kickstart', on: 'dashboard service', label: 'Kickstart', does: 'The dashboard cannot restart itself; gives you the command to run.', key: 'l', icon: 'rocket', risk: 'safe', executes: false,
        primary: () => true, cli: o => o.kickstart, run: copyOnly },
    );
    // Ports and Protection are read-only: each line is copy only.
    C.register(
      { id: 'port.inspect', on: 'port', label: 'Inspect listener', does: 'Shows which program uses this port, in Details.', key: 'i', icon: 'search', risk: 'safe', primary: () => true, executes: true,
        when: o => !!o.port || 'no port configured: there is nothing to inspect', cli: o => `lsof -nP -iTCP:${o.port || '<port>'} -sTCP:LISTEN`,
        sends: o => `POST /api/actions/run {action: port.lsof, port: ${o.port}}`, run: o => run(o, 'port.lsof', { port: Number(o.port) }) },
      { id: 'protection.settings', on: 'branch protection', label: 'Open branch settings', does: 'Opens the repo\'s branch protection settings on GitHub.', key: 'o', icon: 'settings', risk: 'safe', primary: () => true, executes: false,
        when: o => !!o.slug || 'no github.com remote: there is no settings page', cli: o => o.slug ? `open ${shell.shq(`https://github.com/${o.slug}/settings/branches`)}` : 'open https://github.com/<owner>/<repo>/settings/branches',
        run: o => { window.open(`https://github.com/${o.slug}/settings/branches`, '_blank', 'noopener'); return 'Opens in a new tab'; } },
      // Re-run collector (commands.js); Health reads again once the run ends.
      window.commands.syncCollector({ reread: () => load(), page: 'Health' }),
      // Dependencies, Security and Credentials (sd:2203, sd:2205, sd:2206) read stored rows. Review on GitHub opens the repo's
      // alerts page in a new tab (sd:5628); its gh line, which lists the alert links, stays for copying.
      { id: 'dependabot alerts.review', on: 'dependabot alerts', label: 'Review on GitHub', does: 'Opens the Dependabot alerts on GitHub in a new tab.', key: 'o', icon: 'arrow-up-right', risk: 'safe', primary: () => true,
        executes: false, when: o => !!o.slug || 'no github.com remote: there is no alerts page', cli: o => o.cli, run: o => openAlerts(o, 'dependabot') },
      // Fix (sd:6685): commands.js dependabotFix(), which GitHub's Dependabot groups register too (pack sd:7515). The clean
      // and alerts-off rows are checks, so they have no Fix.
      window.commands.dependabotFix(),
      { id: 'secret scanning.review', on: 'secret scanning', label: 'Review on GitHub', does: 'Opens the secret scanning alerts on GitHub in a new tab.', key: 'o', icon: 'arrow-up-right', risk: 'safe', primary: () => true,
        executes: false, when: o => !!o.slug || 'no github.com remote: there is no alerts page', cli: o => o.cli, run: o => openAlerts(o, 'secret-scanning') },
      // sd:6722: one row's probe family only, so a Google check does not wait on claude mcp list.
      { id: 'credential.probe', on: 'credential', label: 'Probe again', does: 'Probes this credential again and shows the result in Details.', key: 'r', icon: 'rotate-ccw', risk: 'safe', primary: () => true, executes: true,
        cli: o => o.cli, sends: o => `POST /api/actions/run {action: check.run, row: ${o.id}}`, run: o => run(o, 'check.run', { row: o.id }) },
      // sd:6722, sd:6957: Sign in shows a Google or SSO login's URL in Details to copy into that account's browser profile; the
      // page opens none. Any other login starts in a herdr pane. Probe again reads the row after.
      { id: 'credential.signin', on: 'credential', label: 'Sign in', does: 'Shows this login\'s URL to copy into the right browser profile.', key: 'i', icon: 'log-in', risk: 'safe', executes: true,
        primary: o => o.state !== 'ok', when: o => !!o.signin || 'this credential has no sign-in', cli: o => o.signin || o.cli,
        sends: o => `POST /api/actions/run {action: credentials.signin, probe: ${o.id.replace(/^cred:/, '')}}`, run: o => run(o, 'credentials.signin', { probe: o.id.replace(/^cred:/, '') }) },
      // pack sd:9260: a service's first incident, else its status page, in a new tab. The list's add and remove are Settings'.
      { id: 'service status.open', on: 'service status', label: 'Open status page', does: 'Opens the open incident, or the service\'s status page, in a new tab.', key: 'o', icon: 'arrow-up-right', risk: 'safe', primary: () => true,
        executes: false, when: o => !!statusHref(o) || 'the reading names no status page', cli: o => `open ${statusHref(o) ? shell.shq(statusHref(o)) : '<url>'}`,
        run: o => { window.open(statusHref(o), '_blank', 'noopener'); return 'Opened in a new tab'; } },
    );
    // Snooze (sd:1896) and Ignore (sd:5470) on every row type; an ok row wants nothing, so it has nothing to snooze.
    window.snooze.register(C, { page: 'health', ignore: true, reread: () => load(), off: o => o.state === 'ok' && 'an ok row has nothing to snooze',
      types: ['check', 'doctor check', 'storage folder', 'build output', 'volume', 'worktree registrations', 'unread registrations', 'merged branches', 'merged worktrees', 'port',
        'branch protection', 'dependabot alerts', 'secret scanning', 'credential', 'service', 'dashboard service', 'launch agent', 'vault mirror', 'service status'] });

    const u = new URLSearchParams(location.search);
    // No ?state= is the start; an empty one is no State pressed, as is an old ?show=all link (sd:7207).
    F.state = new Set(u.get('state') !== null ? u.get('state').split(',').filter(s => STATES.includes(s)) : u.get('show') === 'all' ? [] : PROBLEMS);
    (u.get('area') || '').split(',').filter(Boolean).forEach(a => F.area.add(a));
    query = (u.get('q') || '').trim().toLowerCase(); // the shell's bar reads ?q= too
    showing = u.get('show') === 'ignored' ? 'ignored' : 'findings';
    repos = u.get('repos') === 'all' ? 'all' : 'managed';
    // An ?area= link opens that area's view unless #view= names one. The shell's first shell:view fires after this listener.
    const asked = viewOf([...F.area][0]);
    if (asked && !hashView()) { history.replaceState(null, '', `${location.pathname}${location.search}#view=${asked}`); shell.view.show(asked); }

    // Each panel: its filter bar (segments, tokens, Clear all), a matrix column and a row.
    PAGE_VIEWS.forEach(v => $(`view-${v.id}`).addEventListener('click', e => {
      if (e.target.closest('[data-fclear]')) { clearFilters(); return; }
      const c = e.target.closest('[data-f]');
      if (c && c.dataset.f === 'show') { if (c.dataset.v !== showing) { showing = c.dataset.v; apply(); } return; }
      if (c && c.dataset.f === 'repos') { if (c.dataset.v !== repos) { repos = c.dataset.v; apply(); } return; }
      if (c && c.dataset.f === 'q') { query = ''; shell.filter(''); apply(); return; }
      if (c) { const set = F[c.dataset.f]; set.has(c.dataset.v) ? set.delete(c.dataset.v) : set.add(c.dataset.v); lamps(); apply(); return; }
      if (e.target.closest('.rowact') || e.target.closest('.help')) return;
      const col = e.target.closest('.pmx-col'); if (col) { select(col.dataset.row, true); return; }
      const tr = e.target.closest('tbody tr[data-id]'); if (tr) select(tr.dataset.id, true);
    }));
    PAGE_VIEWS.forEach(v => $(`view-${v.id}`).addEventListener('change', e => {
      if (e.target.id === 'machine-host') { chooseHost(e.target.value); return; }
      shell.list.pickChange(e, F, () => { lamps(); apply(); });
    }));
    // A lamp is the Area pick's value for its area: pressing it adds the area to the filter, pressing it again takes it out.
    $('annunciator').addEventListener('click', e => {
      const c = e.target.closest('button.cell'); if (!c) return;
      const id = c.dataset.area, lamp = lampsNow().find(l => l.id === id);
      if (lamp && lamp.view !== VIEW) { F.area.clear(); F.area.add(id); shell.view.show(lamp.view); return; }
      F.area.has(id) ? F.area.delete(id) : F.area.add(id);
      lamps(); apply();
    });
    // The text filter is the shell's Shapeshift bar (sd:5539).
    document.addEventListener('shell:filter', e => { const q = e.detail.q.toLowerCase(); if (q !== query) { query = q; apply(); } });
    document.addEventListener('shell:open', e => { if (byId(e.detail)) select(e.detail, true); });
    document.addEventListener('shell:picked', e => { document.querySelectorAll('.ledger tbody tr[data-id]').forEach(tr => tr.toggleAttribute('data-picked', e.detail.includes(tr.dataset.id))); });

    document.addEventListener('shell:view', e => onView(e.detail));
    readSummary();
  });
})();
