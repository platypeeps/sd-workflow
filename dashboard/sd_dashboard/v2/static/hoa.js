// HOA (sd:2116): the design source's products/system/designs/pages/hoa.js, ported. Each change from the reference is marked
// "build:". The data is /api/hoa (hoa_screen.py): the asset map, the Mission export and the open items of the checkout
// the repo table's hoa folder names, never sample data. A part that did not read is unknown with its reason.
// build: no Leaflet. The design vendored Leaflet 1.9.4; this page draws the same vectors as one SVG with Fit, zoom and drag,
// so the dashboard ships no third-party script. Clusters and the aerial button are not built (docs/pages/hoa.md).
(() => {
  const { html, put, plural, cells } = window.markup;
  const $ = id => document.getElementById(id);
  const GLYPH = { ok: '●', caution: '▲', warning: '■', queued: '◌', unknown: '▨' };
  const dueText = o => o.due_days == null ? 'no due date' : o.due_days < 0 ? `overdue ${plural(-o.due_days, 'day')}` : o.due_days === 0 ? 'due today' : `in ${plural(o.due_days, 'day')}`;
  const reduce = () => matchMedia('(prefers-reduced-motion: reduce)').matches;
  let DOC = null, M = null, ASSETS = [], LOTS = [], OB = [], AL = [], CELLS = [];
  let selected = null, range = 7, selDay = null, zoom = 1, center = null, P = null, ringsDrawn = false;
  // sd:4967, sd:5286: the Alarms view shows the open events, the acknowledged ones or all (alView); ACKS is why Acknowledge
  // is off, or ''.
  let alView = 'open', ACKS = '';
  const AL_VIEWS = [['open', 'Open', a => !a.acked], ['acked', 'Acknowledged', a => a.acked], ['all', 'All', () => true]];
  // sd:4965: the age lamps, tools/source-ages.py's rows for the Mission export. A source past its end date or
  // retired is not drawn.
  let SRC = [];
  const SRC_STATE = { ok: 'ok', stale: 'caution', unknown: 'unknown' };
  const srcOf = key => SRC.find(r => r.source === key);
  // sd:5095: the checkout's tools/sweep.py, as /api/hoa's `sweep` part reads it (sweep.status): running, and the last run.
  let SWEEP = null;
  // sd:5626: each portal download's last run (/api/hoa's `downloads`, hoa_download.status), and its head command.
  let DL = {};
  const DOWNLOADS = {};
  // sd:5621: the Alarms toolbar's Acknowledge all command, registered on DOMContentLoaded.
  let ACK_ALL = null;
  // sd:5671: a Tasks row's Details and commands, the Tasks page's (task-details.js), attached on DOMContentLoaded.
  let TD = null;
  // sd:5752: OB is the open items, which the annunciator and the badge count; OBD the week's done ones (/api/hoa's
  // items.done). Tasks shows OB, or both with All (obShow, the URL's ?tasks=all); each row's act is Done (view 'place').
  let OBD = [], obShow = new URLSearchParams(location.search).get('tasks') === 'all' ? 'all' : 'open';
  const TASKS = () => [...OB, ...OBD];
  // Whether the pane is HOA's: Briefs and Reports draw their own Details there (places.js).
  const own = () => !['briefs', 'reports'].includes(window.shell.view?.current?.());
// A source's refresh job (its source-ages row's `job`) as the jobs inventory reads it: its last run, in words.
const refreshText = j => !j ? '' : j.error ? 'refresh job not installed' : j.state === 'running' ? 'refresh running'
  : ['failed', 'interrupted'].includes(j.state) ? `last refresh ${j.state}${j.last_exit != null ? `, exit ${j.last_exit}` : ''}${j.said ? `: ${j.said}` : ''}`
  : j.state === 'idle' ? (j.last_exit === 0 ? 'last refresh finished' : 'refresh not run yet') : `refresh job ${j.state}`;
// The sweep's last run, in words, and its lamp: running is queued, a failed run a warning, a part not read unknown.
const sweepText = w => !w || w.error ? '' : w.running ? `sweep running${w.last?.started ? ` since ${w.last.started.slice(11, 16)} UTC` : ''}`
  : !w.last || w.last.state === 'never' ? 'sweep not run yet' : w.last.state === 'done' ? `last sweep done${w.last.finished ? ` ${w.last.finished.slice(0, 16).replace('T', ' ')} UTC` : ''}`
  : `last sweep ${w.last.state}${w.last.exit != null ? `, exit ${w.last.exit}` : ''}${w.last.reason ? `: ${w.last.reason}` : ''}`;
// A download's run, in words: while it runs, its phase; after, how it ended. The row reads it as the sweep row reads its run.
const downloadText = d => !d ? '' : d.running ? (d.phase === 'waiting' ? 'waiting for you to log in' : d.phase === 'downloading' ? 'downloading'
  : `opening ${d.label} in Chrome`) : d.state === 'done' ? `last download done${d.finished ? ` ${d.finished.slice(0, 16).replace('T', ' ')} UTC` : ''}`
  : d.state === 'unavailable' ? `last download not possible: ${d.reason}` : d.state === 'failed' ? `last download failed${d.reason ? `: ${d.reason}` : ''}` : '';
const sweepState = w => !w || w.error || w.last?.state === 'unknown' ? 'unknown' : w.running || !w.last || w.last.state === 'never' ? 'queued' : w.last.state === 'done' ? 'ok' : 'warning';
// While a refresh job runs, HOA reads again every SRC_POLL ms, so the lamps follow the run to its end.
const SRC_POLL = 15000;
let polling = 0;
  const hidden = new Set();

  const dl = pairs => html`<dl>${pairs.filter(p => p[1] !== '' && p[1] != null).map(([k, v]) => html`<dt>${k}</dt><dd>${v}</dd>`)}</dl>`;
  const num = v => v !== '' && v != null && !Number.isNaN(+v);
  const gal = v => num(v) ? (+v).toLocaleString('en-US') : '—';
  const day = s => String(s || '').slice(5, 10);
  const unknownBlock = why => html`<p class="state-unknown"><b>▨ Not read.</b> ${why}</p>`;

  // ---------- Annunciator ----------
  // build: every cell is a reading of the document or unknown with its reason; the design's mail-only facts (the 05:50 fault,
  // "9 overdue") have no reader, so Water is unknown and Tasks counts the checkout's open items (sd:4959: every kind).
  // sd:10589: each daily feed is current, stale or unknown on its own (hoa_screen.FEEDS); the alarm log has no state.
  const FEED_STATE = { current: 'ok', stale: 'caution', unknown: 'unknown' };
  const feedText = f => f.state === 'unknown' ? (f.error ? `unknown: ${f.error}` : 'unknown: no rows') : `${f.state} · latest ${f.latest.slice(0, 10)} · ${f.age_days} d old`;
  const feedLine = m => m.files.filter(f => f.state).map(f => `${f.name.replace(/-daily\.csv$/, '')} ${f.state}`).join(' · ');
  function readCells() {
    const m = DOC.mission, la = m.analog[m.analog.length - 1], lp = m.pump[m.pump.length - 1], f = DOC.items;
    const why = m.error || 'no rows in the export';
    // sd:5298: a lamp unknown for want of the export opens its evidence, the export's source row (its job and last run).
    const src = m.dir ? 'source:mission' : null;
    const out = [{ id: 'water', label: 'Water', state: 'unknown', val: html`<b>—</b> not read`, small: 'no live reader: the export holds daily rows, not the pump now' }];
    out.push(la && num(la.tank_ft_mean) ? { id: 'tank', label: 'Tank level', state: 'ok', open: `day:${la.date}`, val: html`<b>${(+la.tank_ft_mean).toFixed(1)}</b> ft mean`, small: `${la.tank_ft_min}–${la.tank_ft_max} · ${day(la.date)} (latest row)` }
      : { id: 'tank', label: 'Tank level', state: 'unknown', open: src, val: html`<b>—</b> not read`, small: why });
    out.push(lp && num(lp.a2_runtime_hours) ? { id: 'pumps', label: 'Pumps', state: 'ok', open: `day:${lp.date}`, val: html`<b>${(+lp.a2_runtime_hours).toFixed(1)}</b> h A2`, small: `${plural(+lp.a2_starts || 0, 'start')} · ${gal(lp.a2_gallons)} gal · ${day(lp.date)}` }
      : { id: 'pumps', label: 'Pumps', state: 'unknown', open: src, val: html`<b>—</b> not read`, small: why });
    out.push(la && num(la.aquifer_ft_mean) ? { id: 'aquifer', label: 'Aquifer', state: 'ok', open: `day:${la.date}`, val: html`<b>${Math.round(+la.aquifer_ft_mean)}</b> ft mean`, small: `${la.aquifer_ft_min}–${la.aquifer_ft_max} · ${day(la.date)}` }
      : { id: 'aquifer', label: 'Aquifer', state: 'unknown', open: src, val: html`<b>—</b> not read`, small: why });
    const at = m.alarms_today, real = AL.filter(a => a.st === 'warning').length, tests = AL.filter(a => a.kind === 'generator' && /Running/.test(a.event)).length;
    out.push(at.count == null ? { id: 'alarms', label: 'Alarms today', state: 'unknown', open: AL[0]?.id, val: html`<b>—</b> ${at.reason}`, small: `${plural(real, 'alarm')} in 30 d · ${plural(tests, 'generator test')}` }
      : { id: 'alarms', label: 'Alarms today', state: at.count ? 'warning' : 'ok', open: AL[0]?.id, val: html`<b>${at.count}</b> today${at.acked ? ` · ${at.acked} acknowledged` : ''}`, small: `${plural(real, 'alarm')} in 30 d · ${plural(tests, 'generator test')}` });
    const late = OB.filter(o => o.state === 'warning').length, soon = OB.filter(o => o.state === 'caution').length;
    out.push(f.error ? { id: 'tasks', label: 'Tasks', state: 'unknown', val: html`<b>—</b> not read`, small: f.error }
      : late ? { id: 'tasks', label: 'Tasks', state: 'warning', open: OB[0].key, val: html`<b>${late}</b> overdue`, small: `${OB.length} open · ${soon} due within a week` }
      : { id: 'tasks', label: 'Tasks', state: soon ? 'caution' : 'ok', open: OB[0]?.key, val: html`<b>${soon || OB.length}</b> ${soon ? 'due soon' : 'open'}`, small: soon ? `${OB.length} open · none overdue` : 'none overdue or due within a week' });
    // sd:10589: the cell is the worst feed's state; each feed's own state is in its line.
    out.push(m.state !== 'unknown' ? { id: 'mission', label: 'Mission data', state: FEED_STATE[m.state], open: 'source:mission', val: html`<b>${m.age_days} d</b> old`, small: `${feedLine(m)}${m.error ? ' · a file did not read' : ''}` }
      : { id: 'mission', label: 'Mission data', state: 'unknown', open: src, val: html`<b>—</b> not read`, small: m.files.length ? feedLine(m) : why });
    return out;
  }
  // Each lamp's hover and focus text (sd:5287): what it measures, its source, and the line that reads it where one exists.
  const TIPS = {
    water: 'What the water system is doing now. No live reader yet: the Mission export holds daily rows only.',
    tank: 'Mean tank level on the newest daily row, with that day\'s low and high, from Mission\'s daily export.',
    pumps: 'A2 pump runtime, starts and gallons on the newest daily row, from Mission\'s daily export.',
    aquifer: 'Mean aquifer level on the newest daily row, with that day\'s low and high, from Mission\'s daily export.',
    alarms: 'Alarm events today; returns to normal and generator tests do not count. From Mission\'s alarm export.',
    tasks: 'Open HOA tasks: overdue first, then due within a week.',
    mission: 'How old each daily Mission feed is: the oldest one dates the export.',
  };
  function lamps() {
    // sd:6697: no Observed clock; the shell's Read again (or r) is the page's one re-read.
    put($('annunciator'), cells([...CELLS.map(c => ({ button: true, state: c.state, attrs: html` data-cell="${c.id}"${c.open ? html` data-open="${c.open}"` : ''}`,
      label: c.label, mark: html`<span class="${c.state === 'ok' ? 'g' : ''}" aria-hidden="true">${GLYPH[c.state]}</span>`, val: html`<span class="ph">${c.val}</span>`, small: c.small, tip: TIPS[c.id] }))]));
  }
  // The badge is the read's own (sd:3446, hoa_screen._attention), the cells above that can light.
  function attention() {
    window.PAGE_ATTENTION = DOC && DOC.attention || { state: 'unknown', n: 0, what: 'HOA not read' };
    window.shell.attention(window.PAGE_ATTENTION);
  }

  // ---------- Map ----------
  // build: the design's glyphs, drawn into one SVG. Coordinates project equirectangular around the HOA's middle latitude: at
  // this size the error is far below a glyph. Glyphs keep their screen size at every zoom.
  const TYPES = {
    // sd:6691: the one tank is the buried cistern, so the type reads Cistern: a tank under a ground line, its riser through it.
    tank:    { label: 'Cistern', size: 26, svg: html`<path class="halo" d="M2 8h20M5 12v7c0 1.3 3.1 2.4 7 2.4s7-1.1 7-2.4v-7"/><ellipse class="halo" cx="12" cy="12" rx="7" ry="2.4"/><path class="body" d="M5 12v7c0 1.3 3.1 2.4 7 2.4s7-1.1 7-2.4v-7c0 1.3-3.1 2.4-7 2.4S5 13.3 5 12z"/><ellipse class="body" cx="12" cy="12" rx="7" ry="2.4" fill-opacity=".55"/><path class="line ground" d="M2 8h20M12 8v1.6"/>` },
    well:    { label: 'Well',    size: 22, svg: html`<circle class="halo" cx="12" cy="12" r="8"/><circle class="body" cx="12" cy="12" r="8" fill-opacity=".2"/><circle class="body" cx="12" cy="12" r="3.2"/>` },
    hydrant: { label: 'Hydrant', size: 18, svg: html`<path class="halo" d="M8 8a4 4 0 0 1 8 0v11H8z"/><path class="body" d="M8 8a4 4 0 0 1 8 0v11H8z"/><path class="body" d="M5 11h3v4H5zM16 11h3v4h-3zM6 19h12v2.5H6z"/>` },
    valve:   { label: 'Valve',   size: 16, svg: html`<path class="halo" d="M3 6l9 6-9 6zM21 6l-9 6 9 6z"/><path class="body" d="M3 6l9 6-9 6zM21 6l-9 6 9 6z"/><path class="line" d="M12 12V4M9 4h6"/>` },
    // sd:7516: hoa's booster type is the booster station, the "submarine": a pump box buried under a ground line, its
    // riser through it, as the cistern's.
    booster: { label: 'Booster station', size: 22, svg: html`<path class="halo" d="M2 8h20M4 11h16v10H4z"/><path class="body" d="M4 11h16v10H4z" fill-opacity=".55"/><circle class="body" cx="12" cy="16" r="2.6"/><path class="line ground" d="M2 8h20M12 8v3"/>` },
    // sd:7516: hoa #413's types. A street light is a pole, its arm and the lamp head; an irrigation point a sprinkler on its
    // riser. An irrigation line is drawn as its run (drawMap); this glyph is its legend key.
    light:   { label: 'Street light', size: 18, svg: html`<path class="halo" d="M7.2 5h1.6v16H7.2zM8 4h8v1.6H8zM14 5.6h6l-1.6 3.2h-2.8z"/><path class="body" d="M7.2 5h1.6v16H7.2zM8 4h8v1.6H8zM14 5.6h6l-1.6 3.2h-2.8z"/><path class="line" d="M4.5 21h7"/>` },
    irrigation: { label: 'Irrigation point', size: 14, svg: html`<path class="halo" d="M10.5 14h3v7h-3z"/><circle class="halo" cx="12" cy="13" r="2.8"/><path class="body" d="M10.5 14h3v7h-3z"/><circle class="body" cx="12" cy="13" r="2.8"/><path class="line" d="M12 9V4M9.2 10.2 5.5 6.5M14.8 10.2l3.7-3.7"/>` },
    irrline: { label: 'Irrigation line', line: true, svg: html`<path class="line run" d="M3 18c6 0 6-12 18-12"/>` },
  };
  const ORDER = ['tank', 'booster', 'well', 'hydrant', 'valve', 'light', 'irrigation', 'irrline'];
  const glyph = type => html`<svg viewBox="0 0 24 24" aria-hidden="true">${TYPES[type].svg}</svg>`;
  const stClass = s => s === 'in service' ? 'st-ok' : s === 'unknown' ? 'st-unknown' : 'st-out';
  const W = 1000, ZOOMS = [1, 2, 4, 8, 16, 32], LABEL_ZOOM = 4;
  // sd:5623: every common tract (the county's land use TRCT) is the HOA's; the road parcel is not. A tract whose longest side
  // is under TRACT_MIN_PX on screen is too small to see, so a ring marks it until the zoom shows it.
  const owned = l => l.kind === 'tract' && l.luc === 'TRCT', TRACT_MIN_PX = 12;

  function project() {
    const pts = [...LOTS.flatMap(l => l.rings.flat()), ...ASSETS.flatMap(a => a.path || [[a.lng, a.lat]])];
    if (!pts.length) return null;
    const xs = pts.map(p => p[0]), ys = pts.map(p => p[1]);
    const minX = Math.min(...xs), maxX = Math.max(...xs), minY = Math.min(...ys), maxY = Math.max(...ys);
    const k = Math.cos(((minY + maxY) / 2) * Math.PI / 180), span = Math.max((maxX - minX) * k, maxY - minY) || 1e-6, s = W * 0.94 / span;
    const ox = (W - (maxX - minX) * k * s) / 2, h = (maxY - minY) * s + W * 0.06;
    return { x: lng => ox + (lng - minX) * k * s, y: lat => W * 0.03 + (maxY - lat) * s, h: Math.max(h, W * 0.4) };
  }
  function view() {
    const w = W / zoom, h = P.h / zoom, c = center || [W / 2, P.h / 2];
    return [c[0] - w / 2, c[1] - h / 2, w, h];
  }
  function drawMap() {
    const box = $('map');
    if (!P) { put(box, unknownBlock(DOC ? [DOC.base.error, DOC.assets.error].filter(Boolean).join(' · ') || 'the map files hold no features' : 'the HOA document did not arrive')); return; }
    const [vx, vy, vw, vh] = view(), u = vw / (box.clientWidth || W);
    const ring = r => `M${r.map(([x, y]) => `${P.x(x).toFixed(1)},${P.y(y).toFixed(1)}`).join('L')}Z`;
    // sd:4964: a lot is a tap target (data-lot) titled by its address; zoomed in to LABEL_ZOOM, the address is drawn on it.
    const key = l => l.kind === 'tract' ? html` data-tract="${l.ain || l.label}"` : l.kind === 'lot' ? html` data-lot="${l.index}"` : '';
    const mid = r => [r.reduce((a, p) => a + p[0], 0) / r.length, r.reduce((a, p) => a + p[1], 0) / r.length];
    const labels = zoom >= LABEL_ZOOM ? LOTS.filter(l => l.kind === 'lot' && l.address).map(l => { const [x, y] = mid(l.rings[0].slice(0, -1));
      return html`<text class="lot-label" x="${P.x(x).toFixed(1)}" y="${P.y(y).toFixed(1)}" font-size="${(11 * u).toFixed(2)}">${l.address}</text>`; }) : '';
    const extent = l => { const xs = l.rings[0].map(p => P.x(p[0])), ys = l.rings[0].map(p => P.y(p[1])); return Math.max(Math.max(...xs) - Math.min(...xs), Math.max(...ys) - Math.min(...ys)); };
    const marks = LOTS.filter(l => owned(l) && extent(l) / u < TRACT_MIN_PX).map(l => { const [x, y] = mid(l.rings[0].slice(0, -1));
      return html`<circle class="hoa-mark" data-tract="${l.ain || l.label}" cx="${P.x(x).toFixed(1)}" cy="${P.y(y).toFixed(1)}" r="${(8 * u).toFixed(2)}"><title>HOA-owned tract${l.label ? `, ${l.label}` : ''}</title></circle>`; });
    // The legend's ring key reads only while a ring is on the map: redraw it when that changes.
    const had = ringsDrawn; ringsDrawn = marks.length > 0;
    const shown = ASSETS.filter(a => !hidden.has(a.type) && TYPES[a.type] && !TYPES[a.type].line === !a.path);
    put(box, html`<svg class="map" viewBox="${vx} ${vy} ${vw} ${vh}" preserveAspectRatio="xMidYMid meet" aria-label="HOA water assets map">
      ${LOTS.map(l => html`<path class="lot k-${l.kind}"${key(l)}${owned(l) ? html` data-owned` : ''}${l.kind === 'lot' && `lot:${l.index}` === selected ? html` aria-current="true"` : ''} d="${l.rings.map(ring).join('')}"><title>${l.address || l.label || (owned(l) ? 'HOA-owned tract' : l.kind)}</title></path>`)}${labels}${marks}
      ${[...shown.filter(a => a.path), ...shown.filter(a => !a.path)].map(a => { const t = TYPES[a.type], g = t.size * u;
        const head = html`<g class="asset t-${a.type} ${stClass(a.status)}" data-asset="${a.id}" tabindex="0" role="button" aria-label="${a.name}, ${t.label}, ${a.status}"${a.id === selected?.slice(6) ? html` aria-current="true"` : ''}`;
        // sd:7516: a line asset is its run under the glyphs; the wide clear stroke beneath it takes the tap.
        if (a.path) { const d = `M${a.path.map(([x, y]) => `${P.x(x).toFixed(1)},${P.y(y).toFixed(1)}`).join('L')}`;
          return html`${head}><title>${a.name} · ${a.status}</title><path class="hit" d="${d}"/><path class="run" d="${d}"/></g>`; }
        return html`${head} transform="translate(${(P.x(a.lng) - g / 2).toFixed(2)} ${(P.y(a.lat) - g / 2).toFixed(2)}) scale(${(g / 24).toFixed(4)})"><title>${a.name} · ${a.status}</title>${TYPES[a.type].svg}</g>`; })}
    </svg>`);
    if (had !== ringsDrawn) legend();
  }
  function zoomTo(z, at) { zoom = Math.max(ZOOMS[0], Math.min(ZOOMS[ZOOMS.length - 1], z)); if (at) center = at; drawMap(); }
  function legend() {
    const byType = {}, byStatus = {};
    const tracts = LOTS.filter(owned).length;
    ASSETS.forEach(a => { byType[a.type] = (byType[a.type] || 0) + 1; byStatus[a.status] = (byStatus[a.status] || 0) + 1; });
    put($('legend'), html`${ORDER.filter(t => byType[t]).map(t => html`<li><button type="button" data-type="${t}" aria-pressed="${hidden.has(t) ? 'false' : 'true'}">${glyph(t)}${TYPES[t].label}s <b>${byType[t]}</b></button></li>`)}<li class="sep" aria-hidden="true"></li>${
      Object.entries(byStatus).map(([s, n]) => html`<li class="status"><i class="${s === 'in service' ? '' : s === 'unknown' ? 'unk' : 'out'}" aria-hidden="true"></i>${s} <b class="data">${n}</b></li>`)}${
      tracts ? html`<li class="status"><i class="own" aria-hidden="true"></i>HOA-owned ${tracts === 1 ? 'tract' : 'tracts'} <b class="data">${tracts}</b>${ringsDrawn ? html`<i class="mark" aria-hidden="true"></i>ring: tract too small to draw at this zoom` : ''}</li>` : ''}`);
  }

  // ---------- Details ----------
  function show(m, id, open) {
    put($('details'), m);
    window.shell.commands.select(id);
    if (open) window.shell.openPane('tab-details');
  }
  function mark() {
    document.querySelectorAll('.ledger tbody tr[data-id]').forEach(tr => tr.setAttribute('aria-selected', tr.dataset.id === selected ? 'true' : 'false'));
  }
  function selectAsset(id, open) {
    const a = ASSETS.find(x => `asset:${x.id}` === id); if (!a) return;
    selected = id; mark();
    if (open && P) { center = [P.x(a.lng), P.y(a.lat)]; zoom = Math.max(zoom, 4); }
    drawMap();
    const m = DOC.mission, g = a.status === 'in service' ? ['g-ok', '●'] : a.status === 'unknown' ? ['g-unknown', '▨'] : ['g-queued', '◌'];
    let extra = '';
    // build: the design keyed these on two sample ids; here a tank reads the analog file and the A2 well the pump file.
    if (a.type === 'well' && /A2/.test(a.id)) {
      const lp = m.pump[m.pump.length - 1], a2 = AL.filter(x => /A2/.test(x.event));
      extra = html`${lp ? html`<h3 class="label">Latest pump row · ${lp.date}</h3>${dl([['Runtime', `${lp.a2_runtime_hours} h`], ['Starts', lp.a2_starts], ['Gallons', gal(lp.a2_gallons)], ['Flow running', `${lp.a2_flow_gpm_running} gpm`]])}` : ''}
        <h3 class="label">A2 alarms · last 30 days of export</h3>${a2.length ? html`<ul class="note">${a2.slice(0, 12).map(x => html`<li><code>${x.datetime.slice(0, 16)}</code> ${x.event}</li>`)}</ul>` : html`<p class="note">None.</p>`}`;
    }
    if (a.type === 'tank') {
      const la = m.analog[m.analog.length - 1];
      if (la) extra = html`<h3 class="label">Latest level · ${la.date}</h3>${dl([['Mean', `${la.tank_ft_mean} ft`], ['Min–max', `${la.tank_ft_min}–${la.tank_ft_max} ft`], ['Samples', la.samples]])}`;
    }
    show(html`<div class="kind"><span class="${g[0]}" aria-hidden="true">${g[1]}</span><span class="label">${TYPES[a.type]?.label || a.type} · ${a.id}</span></div>
      <h2>${a.name}</h2>
      ${dl([['Status', a.status], ['Accuracy', a.accuracy], ['Position', `${a.lat.toFixed(5)}, ${a.lng.toFixed(5)}${a.path ? `, the middle of ${a.path.length} vertices` : ''}`], ['Updated', a.updated], ['Source', a.source]])}
      ${a.notes ? html`<p class="note">${a.notes}</p>` : ''}${extra}
      <h3 class="label">Attachments</h3><div id="attach" class="attach"></div>
      <h3 class="label">Act</h3>${window.shell.commands.bar(id)}`, id, open);
    drawAttachments(a);
  }
  // ---------- Attachments (sd:6690) ----------
  // An asset's documents and pictures are its Drive folder's listing (hoa_attach.py). Upload and Reference write there,
  // on the operator's press only; a Reference is a small .url file naming a Drive link. Pictures come through the server.
  function attachRow(f) {
    const href = f.kind === 'link' ? (f.target || f.link) : f.link;
    return html`<li>${f.kind === 'picture' ? html`<a href="${f.link}" target="_blank" rel="noopener"><img class="thumb" src="/api/hoa/attachments/picture?id=${encodeURIComponent(f.id)}" alt="${f.name}" loading="lazy"></a>` : ''}<a href="${href}" target="_blank" rel="noopener">${f.name}</a> <span class="k">${f.kind}</span></li>`;
  }
  const attachForms = () => html`<form class="attach-form" data-attach="upload"><input type="file" name="file" required aria-label="File to upload"><button class="btn quiet sm" type="submit">Upload</button></form>
    <form class="attach-form" data-attach="reference"><input type="url" name="link" required placeholder="Drive link" aria-label="Drive link to reference"><input type="text" name="name" placeholder="Name" aria-label="Reference name"><button class="btn quiet sm" type="submit">Reference</button></form>
    <p class="why" role="status" data-attach-said>Upload and Reference write into this asset's Drive folder.</p>`;
  async function drawAttachments(a) {
    const live = window.shell.shown('attach', `asset:${a.id}`, () => selected); if (!live || !$('attach')) return;
    put($('attach'), html`<p class="note">Reading Drive…</p>`);
    let out;
    try { out = await window.shell.getJSON(`/api/hoa/attachments?asset=${encodeURIComponent(a.id)}`); }
    catch (err) { if (live() && $('attach')) put($('attach'), unknownBlock(err.message)); return; }
    if (!live() || !$('attach')) return;
    put($('attach'), html`${out.files.length ? html`<ul class="attach-list">${out.files.map(attachRow)}</ul>` : html`<p class="note">None yet.</p>`}${
      out.folder ? html`<p class="why"><a href="${out.folder}" target="_blank" rel="noopener">Open the folder in Drive</a></p>` : ''}${attachForms()}`);
    $('attach').querySelectorAll('form[data-attach]').forEach(form => form.addEventListener('submit', e => { e.preventDefault(); attach(a, form); }));
  }
  async function attach(a, form) {
    const S = window.shell, button = form.querySelector('button'), said = $('attach').querySelector('[data-attach-said]');
    button.disabled = true; button.setAttribute('aria-busy', 'true'); said.textContent = 'Writing to Drive…';
    try {
      if (form.dataset.attach === 'upload') {
        const f = form.elements.file.files[0];
        const staged = await S.post('/api/hoa/attach/file', f, { 'X-SD-File-Name': encodeURIComponent(f.name) });
        await S.post('/api/hoa/attach', { asset: a.id, sha256: staged.sha256, name: f.name });
      } else {
        const name = form.elements.name.value.trim();
        await S.post('/api/hoa/attach', { asset: a.id, link: form.elements.link.value.trim(), ...(name ? { name } : {}) });
      }
      drawAttachments(a);
    } catch (err) {
      said.textContent = err.message; said.className = 'state-unknown';
      button.disabled = false; button.removeAttribute('aria-busy');
    }
  }
  function selectTract(key, open) {
    const l = LOTS.find(x => (x.ain || x.label) === key); if (!l) return;
    show(html`<div class="kind"><span class="label">Tract</span></div><h2>${l.label || `Tract ${l.ain}`}</h2>${dl([['AIN', l.ain], ['Land use', l.luc], ['Acres', l.acres], ['Source', 'base.geojson']])}`, null, open);
  }
  // sd:4964: a lot's address, owner of record and parcel come from the checkout's lot data (hoa_screen._lots); without it
  // the lot is unknown with the reason, which names the script to run.
  function selectLot(index, open) {
    const l = LOTS.find(x => x.kind === 'lot' && String(x.index) === String(index)); if (!l) return;
    const lots = DOC.lots;
    selected = `lot:${l.index}`; mark(); drawMap();
    show(html`<div class="kind"><span class="label">Lot</span></div><h2>${l.address || `Lot ${l.index + 1}`}</h2>
      ${lots.error ? unknownBlock(lots.error) : dl([['Address', l.address || 'no match in the lot data'], ['Owner of record', l.owner], ['AIN', l.parcel],
        ['Source', `county parcel layer, fetched ${lots.fetched_at.slice(0, 10)}`]])}`, null, open);
  }
  // sd:5671: a task's Details are the Tasks page's (task-details.js), drawn into this pane; its key is the item number.
  function selectTask(key, open) {
    const o = TASKS().find(x => x.key === key); if (!o || !TD) return;
    selected = key; mark(); drawMap();
    window.shell.commands.select(key); TD.readDet(o.id); TD.renderDetails();
    if (open) window.shell.openPane('tab-details');
  }
  function selectAlarm(id, open) {
    const a = AL.find(x => x.id === id); if (!a) return;
    selected = id; mark(); drawMap();
    show(html`<div class="kind"><span class="g-${a.st}" aria-hidden="true">${GLYPH[a.st]}</span><span class="label">Alarm event · ${a.kind}</span></div><h2>${a.event}</h2>
      ${dl([['When', `${a.datetime} (unit clock)`], ['Reads as', a.why], ['Minutes', a.minutes], ['Notified', a.notified], ['Acknowledged', a.acked ? 'yes, on this dashboard' : 'no'], ['Source', 'alarm-events.csv']])}
      <h3 class="label">Act</h3>${window.shell.commands.bar(id)}
      <p class="why">Acknowledge marks the event on this dashboard only. Nothing reaches the Mission portal.</p>`, id, open);
  }
  // The source-ages row for Details: its lamp state, age and what to do.
  const ageBlock = r => !r ? '' : html`<h3 class="label">Age · ${DOC.sources.command}</h3>${dl([['State', r.state === 'unknown' ? `not read: ${r.error}` : r.state],
    ['Newest', r.latest ? `${r.latest} · ${r.age_days} d old` : 'no date found'], ['Allowed', `${r.allowed_days} d`], ['Ends', r.ends || ''], ['To do', r.state === 'stale' ? r.todo : ''],
    ['Refresh job', !r.job ? '' : r.job.error || `${r.job.name} · ${refreshText(r.job)}`]])}`;
  // sd:5680: a source with a Download shows the end of that download's start.log, as Management shows a job's log; a log
  // that was not read is unknown with its reason, a missing one says so.
  const logBlock = k => { const d = DL[k], g = d?.tail; if (!d) return '';
    const head = html`<h3 class="label">Log${g?.cut ? ' · last lines' : ''}</h3><p class="note">${d.log}</p>`;
    return !g || g.state === 'unread' ? html`${head}${unknownBlock(g ? g.reason : 'this reading carries no log')}`
      : g.state === 'none' ? html`${head}<p class="why">${g.reason}.</p>`
      : html`${head}<div class="mailbody">${g.text || html`<span class="note">The log is empty.</span>`}</div>`; };
  function selectSourceRow(id, open) {
    const r = srcOf(id.slice(7)); if (!r) return;
    const st = SRC_STATE[r.state] || 'unknown';
    selected = id; mark();
    show(html`<div class="kind"><span class="g-${st}" aria-hidden="true">${GLYPH[st]}</span><span class="label">Source</span></div>
      <h2>${r.label}</h2>${ageBlock(r)}
      <h3 class="label">Act</h3>${window.shell.commands.bar(id)}${logBlock(r.source)}`, id, open);
  }
  function selectSource(open) {
    const m = DOC.mission;
    selected = 'source:mission'; mark();
    const st = FEED_STATE[m.state] || 'unknown';
    show(html`<div class="kind"><span class="g-${st}" aria-hidden="true">${GLYPH[st]}</span><span class="label">Source · Mission export</span></div>
      <h2>Mission telemetry export</h2>
      ${dl([['Path', m.dir], ['Cadence', 'daily export, one row per day'], ['State', m.state === 'unknown' ? 'unknown: a daily feed is not read' : `${m.state} · every daily feed through ${m.latest} · ${m.age_days} d old`]])}
      <h3 class="label">Files in this page</h3>${dl(m.files.map(f => [f.name, f.state ? `${feedText(f)}${f.error ? '' : ` · ${f.rows} rows`}` : f.error ? `not read: ${f.error}` : `${f.rows} rows · latest ${f.latest.slice(0, 10)}`]))}
      <p class="note">Tank level and Aquifer read analog-daily.csv; Pumps reads pump-daily.csv; Alarms reads alarm-events.csv. The pump export lags the analog export by one day.</p>
      ${ageBlock(srcOf('mission'))}<h3 class="label">Act</h3>${window.shell.commands.bar('source:mission')}${logBlock('mission')}`, 'source:mission', open);
  }
  function selectDay(d, open, series) {
    const m = DOC.mission, a = m.analog.find(r => r.date === d), p = m.pump.find(r => r.date === d);
    selected = `day:${d}`; selDay = d; mark(); drawTrends();
    if (series) $('trend-list').querySelector(`.scrub[data-series="${series}"]`)?.focus();
    const C = window.shell.commands;
    if (!C.get(selected)) C.put({ id: selected, type: 'day', label: `day ${d}` });
    show(html`<div class="kind"><span class="label">Mission day</span></div><h2>${d}</h2>
      <h3 class="label">analog-daily.csv</h3>${a ? dl([['Tank', `${a.tank_ft_mean} ft (${a.tank_ft_min}–${a.tank_ft_max})`], ['Aquifer', `${a.aquifer_ft_mean} ft (${a.aquifer_ft_min}–${a.aquifer_ft_max})`], ['A2 flow', `${a.a2_flow_gpm_mean} gpm mean`], ['Booster', `${a.booster_psi_mean} psi mean`], ['Samples', a.samples]]) : html`<p class="note">No analog row for this day.</p>`}
      <h3 class="label">pump-daily.csv</h3>${p ? dl([['A2 runtime', `${p.a2_runtime_hours} h`], ['Starts', p.a2_starts], ['Gallons', gal(p.a2_gallons)], ['Metered', gal(p.a2_gallons_metered)]]) : html`<p class="note">No pump row for this day yet; the export lags one day.</p>`}`, selected, open);
  }
  function select(id, open) {
    if (!id) return;
    if (id.startsWith('asset:')) selectAsset(id, open);
    else if (TASKS().some(o => o.key === id)) selectTask(id, open);
    else if (id.startsWith('lot:')) selectLot(id.slice(4), open);
    else if (id.startsWith('alarm:')) { if (window.shell.view?.current() !== 'alarms') window.shell.view?.show('alarms'); selectAlarm(id, open); }
    else if (id.startsWith('day:')) selectDay(id.slice(4), open);
    else if (id === 'source:mission') selectSource(open);
    else if (id.startsWith('source:')) selectSourceRow(id, open);
    else if (id === 'sweep') selectSweep(open);
  }
  // The sweep's Details: its last run as tools/sweep.py recorded it, the log to read, and Run sweep.
  function selectSweep(open) {
    if (!SWEEP) return;
    const st = sweepState(SWEEP), l = SWEEP.last || {};
    selected = 'sweep'; mark();
    show(html`<div class="kind"><span class="g-${st}" aria-hidden="true">${GLYPH[st]}</span><span class="label">Sweep · ${SWEEP.command}</span></div>
      <h2>HOA sweep</h2>${SWEEP.error ? unknownBlock(SWEEP.error) : dl([['State', sweepText(SWEEP)], ['Started', l.started || ''], ['Ended', l.finished || ''],
        ['Log', l.log || ''], ['Branch', l.branch || ''], ['Worktree', l.worktree || '']])}
      <h3 class="label">Act</h3>${window.shell.commands.bar('sweep')}
      <p class="why">Followups are Gmail drafts in the hoa account; nothing is sent. The commit lands through the lane.</p>`, 'sweep', open);
  }

  // ---------- Trends ----------
  const SERIES = [
    { key: 'tank', title: 'Tank level', unit: 'ft', file: 'analog', y: 'tank_ft_mean', lo: 'tank_ft_min', hi: 'tank_ft_max', kind: 'line', src: 'analog-daily.csv' },
    { key: 'runtime', title: 'A2 pump runtime', unit: 'h', file: 'pump', y: 'a2_runtime_hours', kind: 'bar', src: 'pump-daily.csv', zero: true },
    { key: 'aquifer', title: 'Aquifer', unit: 'ft', file: 'analog', y: 'aquifer_ft_mean', lo: 'aquifer_ft_min', hi: 'aquifer_ft_max', kind: 'line', src: 'analog-daily.csv' },
  ];
  const TW = 300, TH = 72;
  // range is days; All is 0 and draws every row the export holds (sd:6689).
  const rowsOf = s => (DOC ? DOC.mission[s.file] : []).filter(r => num(r[s.y]) && (!s.lo || (num(r[s.lo]) && num(r[s.hi])))).slice(range ? -range : 0);
  // sd:4969: the window's min is its lowest daily low and its max its highest daily high (the plotted value on a series with
  // no band); the median is of the plotted daily values, the middle one or the mean of the middle two.
  const lowOf = (s, r) => +r[s.lo || s.y], highOf = (s, r) => +r[s.hi || s.y];
  function stats(s, rows) {
    const ys = rows.map(r => +r[s.y]).sort((a, b) => a - b), mid = ys.length >> 1;
    const lows = rows.map(r => lowOf(s, r)), highs = rows.map(r => highOf(s, r));
    return { min: Math.min(...lows), median: ys.length % 2 ? ys[mid] : (ys[mid - 1] + ys[mid]) / 2, max: Math.max(...highs),
      at: { min: lows.indexOf(Math.min(...lows)), max: highs.indexOf(Math.max(...highs)) } };
  }
  // The problem level (hoa_screen.thresholds): a day is beyond it when its low is under a `below` level or its high over an
  // `above` one.
  const levelOf = s => DOC?.thresholds?.levels?.[s.key] || null;
  const beyond = (s, r, lv) => lv.dir === 'below' ? lowOf(s, r) < lv.value : highOf(s, r) > lv.value;
  function levelText(s, rows, lv) {
    const t = DOC?.thresholds;
    if (!lv) return t?.error ? html`<span class="g-unknown">▨ problem level not read: ${t.error}</span>` : html`no problem level set`;
    const n = rows.filter(r => beyond(s, r, lv)).length;
    return html`<span class="${n ? 'g-warning' : ''}">${n ? '■ ' : ''}problem level ${lv.dir} ${lv.value} ${s.unit} · ${n ? `${plural(n, 'day')} beyond` : 'no day beyond'}</span>`;
  }
  function spark(s) {
    const rows = rowsOf(s);
    if (!rows.length) return html`<figure class="spark" data-series="${s.key}"><header><span class="label">${s.title}</span></header>${unknownBlock(DOC?.mission.error || `${s.src} has no rows`)}</figure>`;
    const st = stats(s, rows), lv = levelOf(s), fix = v => (+v).toFixed(1);
    let min = s.zero ? 0 : st.min, max = st.max;
    if (lv) { min = Math.min(min, lv.value); max = Math.max(max, lv.value); }
    if (max === min) max = min + 1;
    const n = rows.length, step = TW / n;
    const X = i => (i + 0.5) * step, Y = v => TH - ((v - min) / (max - min)) * (TH - 6) - 3;
    const marks = [];
    if (s.kind === 'line') {
      if (s.lo) marks.push(html`<path class="band" d="M${rows.map((r, i) => `${X(i)},${Y(+r[s.hi])}`).join('L')}L${rows.map((r, i) => `${X(i)},${Y(+r[s.lo])}`).reverse().join('L')}Z"/>`);
      marks.push(html`<polyline class="ln" points="${rows.map((r, i) => `${X(i)},${Y(+r[s.y])}`).join(' ')}"/>`);
      marks.push(html`<circle class="last" cx="${X(n - 1)}" cy="${Y(+rows[n - 1][s.y])}" r="2.5"/>`);
    } else {
      marks.push(rows.map((r, i) => html`<rect class="bar${r.date === selDay ? ' sel' : ''}${lv && beyond(s, r, lv) ? ' over' : ''}" x="${X(i) - step * 0.35}" y="${Y(+r[s.y])}" width="${step * 0.7}" height="${TH - 3 - Y(+r[s.y])}"/>`));
    }
    marks.push(html`<circle class="ext" data-ext="min" cx="${X(st.at.min)}" cy="${Y(st.min)}" r="2"/><circle class="ext" data-ext="max" cx="${X(st.at.max)}" cy="${Y(st.max)}" r="2"/>`);
    if (lv) {
      marks.push(html`<line class="limit" x1="0" x2="${TW}" y1="${Y(lv.value)}" y2="${Y(lv.value)}"/>`);
      if (s.kind === 'line') marks.push(rows.map((r, i) => beyond(s, r, lv) ? html`<circle class="over" cx="${X(i)}" cy="${Y(lv.dir === 'below' ? lowOf(s, r) : highOf(s, r))}" r="3"/>` : ''));
    }
    const picked = rows.find(r => r.date === selDay);
    const hits = html`${s.kind === 'line' ? rows.map((r, i) => html`<circle class="mk${r.date === selDay ? ' sel' : ''}" cx="${X(i)}" cy="${Y(+r[s.y])}" r="3.5"/>`) : ''}<rect class="hit scrub" x="0" y="0" width="${TW}" height="${TH}" tabindex="0" role="button" data-series="${s.key}" data-n="${n}" aria-label="${s.title}, ${n} days. ${picked ? `${picked.date}: ${picked[s.y]} ${s.unit}. ` : ''}Tap a day, or use the left and right arrow keys."/>`;
    const last = rows[n - 1];
    return html`<figure class="spark" data-series="${s.key}">
      <header><span class="label">${s.title}</span><span class="now">${fix(last[s.y])} <small>${s.unit} · ${day(last.date)}</small></span></header>
      <svg viewBox="0 0 ${TW} ${TH}" preserveAspectRatio="none" role="img" aria-label="${s.title}, ${n} days">
        <line class="grid" x1="0" x2="${TW}" y1="${TH - 3}" y2="${TH - 3}"/>${marks}${hits}</svg>
      <div class="axis"><span>${day(rows[0].date)}</span><span>${day(last.date)}</span></div>
      <p class="stats"><span>min <b>${fix(st.min)}</b></span><span>median <b>${fix(st.median)}</b></span><span>max <b>${fix(st.max)}</b> ${s.unit}</span></p>
      <p class="level">${levelText(s, rows, lv)}</p>
      <div class="sr"><table><caption>${s.title} (${s.unit}), from ${s.src}</caption><tbody>${rows.map(r => html`<tr><th scope="row">${r.date}</th><td>${r[s.y]}</td></tr>`)}</tbody></table></div>
    </figure>`;
  }
  const drawTrends = () => put($('trend-list'), html`${SERIES.map(spark)}`);
  const daysOf = sc => rowsOf(SERIES.find(x => x.key === sc.dataset.series)).map(r => r.date);

  // The sweep is one more ledger row, after the sources: its lamp, its last run and Run sweep.
  function sweepRow() {
    const st = sweepState(SWEEP);
    return html`<tr data-id="sweep" aria-selected="${selected === 'sweep' ? 'true' : 'false'}"><td class="g g-${st}" aria-label="${st}">${GLYPH[st]}</td>
      <td class="what"><button type="button">HOA sweep</button><div class="detail">${SWEEP.error ? SWEEP.error : sweepText(SWEEP)}</div></td>
      <td class="src hide-sm">one at a time</td><td class="detail hide-sm">${sweepText(SWEEP)}</td>
      <td class="act">${window.shell.commands.rowActions('sweep')}</td></tr>`;
  }
  function drawSources() {
    const C = window.shell.commands, sr = DOC?.sources;
    put($('src-body'), !DOC ? html`` : html`${drawSourceRows(C, sr)}${SWEEP ? sweepRow() : ''}`);
    // sd:5626: one Download button per source row with a download, in the rows' order. aria-disabled, not disabled, so an
    // off button keeps focus and its reason.
    put($('src-tools'), html`${SRC.map(r => r.source).filter(k => DOWNLOADS[k] && C.get(`download:${k}`)).map(k => {
      const c = DOWNLOADS[k], o = C.get(`download:${k}`), w = c.when(o);
      const icon = html`<svg class="i" aria-hidden="true"><use href="#i-download"/></svg>${c.label}`;
      return w === true ? html`<button class="btn quiet sm" type="button" data-download="${k}" data-tip="${c.does}">${icon}</button>`
        : html`<button class="btn quiet sm" type="button" data-download="${k}" aria-disabled="true" aria-describedby="dl-${k}-off" data-tip="${w}">${icon}</button><span class="sr" id="dl-${k}-off">${w}</span>`; })}`);
  }
  // The head button and the palette entry: an off Download toasts its reason, as the shell's run does not check it.
  function downloadFrom(k) {
    const S = window.shell, c = DOWNLOADS[k], o = S.commands.get(`download:${k}`);
    const w = !c ? 'HOA is not read' : !o ? `${c.label} has no source row on this page` : c.when(o);
    if (w === true) S.commands.run(c, o); else S.toast(w);
  }
  // The toolbar button and the palette entry, the same as Download.
  function acknowledgeAll() {
    const S = window.shell, o = S.commands.get('alarms'), w = o && ACK_ALL ? ACK_ALL.when(o) : 'HOA is not read';
    if (w === true) S.commands.run(ACK_ALL, o); else S.toast(w);
  }
  function drawSourceRows(C, sr) {
    return sr.error ? html`<tr><td colspan="5">${unknownBlock(sr.error)}</td></tr>`
      : SRC.length ? html`${SRC.map(r => { const st = SRC_STATE[r.state] || 'unknown', id = `source:${r.source}`;
        return html`<tr data-id="${id}" aria-selected="${id === selected ? 'true' : 'false'}"><td class="g g-${st}" aria-label="${st}">${GLYPH[st]}</td>
        <td class="what"><button type="button">${r.label}</button><div class="detail">${r.state === 'unknown' ? r.error : r.latest ? `newest ${r.latest} · ${r.age_days} d old` : 'no date found'}${DL[r.source]?.running ? ` · ${downloadText(DL[r.source])}` : ''}</div></td>
        <td class="src hide-sm">allowed ${r.allowed_days} d</td><td class="detail hide-sm">${downloadText(DL[r.source]) || refreshText(r.job)}</td>
        <td class="act">${C.rowActions(id)}</td></tr>`; })}`
      : html`<tr><td colspan="5" class="why">tools/source-ages.py lists no current source.</td></tr>`;
  }
  // ---------- Tasks (sd:4959) ----------
  // build: one table of every open item of the checkout, followups, tasks and work, where the design had followups alone.
  // hoa_screen ranks them: overdue, due within a week, the rest. The id is its own first column; every column with data sorts
  // through shell.list, and a third click on the sorted header returns to the rank. Kind narrows the rows; the default is all.
  const KINDS = ['followup', 'task', 'work'], OB_ST = { sort: '', dir: 1 };
  let kind = 'all';
  const OB_COLS = [['item', 'ID', 'num'], ['rank', html`<span class="sr">Urgency</span>`], ['title', 'What'], ['kind', 'Kind', 'hide-sm'],
    ['due', 'Due', 'hide-sm'], ['status', 'Status', 'hide-sm'], ['', html`<span class="sr">Actions</span>`]];
  const OB_BY = { item: o => o.id, rank: o => TASKS().indexOf(o), title: o => o.title.toLowerCase(), kind: o => o.kind, due: o => o.due_days ?? Infinity, status: o => o.status };
  function obRows() {
    const rows = (obShow === 'all' ? TASKS() : OB).filter(o => kind === 'all' || o.kind === kind), by = OB_BY[OB_ST.sort];
    return by ? rows.sort((a, b) => { const x = by(a), y = by(b); return (x < y ? -1 : x > y ? 1 : 0) * OB_ST.dir; }) : rows;
  }
  function drawTasks() {
    const C = window.shell.commands, f = DOC?.items, shown = obShow === 'all' ? TASKS() : OB, n = k => shown.filter(o => o.kind === k).length;
    const kinds = [...new Set([...KINDS, ...shown.map(o => o.kind)])].filter(n);
    if (!kinds.includes(kind)) kind = 'all';
    // sd:6693: Open | All is the shell's .vseg, the size of every other segment group.
    const seg = (v, word, count) => html`<button type="button" data-f="tasks" data-v="${v}" aria-pressed="${String(obShow === v)}">${word} <span class="n">${String(count)}</span></button>`;
    put($('ob-show'), DOC && !f.error ? html`${seg('open', 'Open', OB.length)}${seg('all', 'All', TASKS().length)}` : html``);
    // pack sd:7084: no All chip; none pressed is every kind, and a second press on the pressed one clears it.
    put($('ob-kinds'), html`${kinds.map(k => html`<button class="chip" type="button" data-kind="${k}" aria-pressed="${String(k === kind)}">${k} <b>${n(k)}</b></button>`)}`);
    put($('ob-head'), window.shell.list.sortHead(OB_COLS, OB_ST));
    const rows = obRows();
    put($('ob-body'), !DOC ? html`` : f.error ? html`<tr><td colspan="7">${unknownBlock(f.error)}</td></tr>`
      : rows.length ? html`${rows.map(o => html`<tr data-id="${o.key}" aria-selected="${o.key === selected ? 'true' : 'false'}">
        <td class="id num">sd:${o.id}</td><td class="g g-${o.state}" aria-label="${o.state}">${GLYPH[o.state]}</td>
        <td class="what"><button type="button">${o.title}</button>${o.p == null ? '' : html` <span class="tag real">P${o.p}</span>`}<div class="detail">${dueText(o)}</div></td>
        <td class="hide-sm">${o.kind}</td><td class="detail hide-sm due">${dueText(o)}</td><td class="src hide-sm">${o.status}</td>
        <td class="act">${C.rowActions(o.key)}</td></tr>`)}`
      : html`<tr><td colspan="7" class="why">No open item in the checkout.${obShow === 'open' && OBD.length ? ` All lists the ${plural(OBD.length, 'item')} done this week.` : ''}</td></tr>`);
  }

  // ---------- Alarm ledger ----------
  const alarmsErr = () => DOC?.mission?.files.find(x => x.name === 'alarm-events.csv')?.error;
  const openAlarms = () => AL.filter(a => !a.acked);
  function drawLedgers() {
    const C = window.shell.commands, alErr = alarmsErr();
    const shown = AL.filter(AL_VIEWS.find(v => v[0] === alView)[2]);
    // sd:5621: Acknowledge all, the toolbar's one button. aria-disabled, not disabled, so an off button keeps focus and its reason.
    const o = C.get('alarms'), w = o && ACK_ALL ? ACK_ALL.when(o) : '';
    const label = html`<svg class="i" aria-hidden="true"><use href="#i-check"/></svg>${ACK_ALL?.label} <span class="n">${String(openAlarms().length)}</span>`;
    put($('al-tools'), !o || !ACK_ALL ? html`` : w === true
      ? html`<button class="btn quiet sm" type="button" id="al-ack-all" data-tip="${ACK_ALL.does} Asks first.">${label}</button>`
      : html`<button class="btn quiet sm" type="button" id="al-ack-all" aria-disabled="true" aria-describedby="al-ack-all-off" data-tip="${w}">${label}</button><span class="sr" id="al-ack-all-off">${w}</span>`);
    put($('al-show'), DOC && !alErr ? html`${AL_VIEWS.map(([id, label, test]) => html`<button type="button" data-alview="${id}" aria-pressed="${String(id === alView)}">${label} <span class="n">${String(AL.filter(test).length)}</span></button>`)}` : html``);
    put($('al-body'), !DOC ? html`` : alErr ? html`<tr><td colspan="7">${unknownBlock(alErr)}</td></tr>`
      : shown.length ? html`${shown.map(a => html`<tr data-id="${a.id}" aria-selected="${a.id === selected ? 'true' : 'false'}"${a.acked ? html` data-acked` : ''}><td class="g g-${a.st}" aria-label="${a.st}">${GLYPH[a.st]}</td><td class="what"><button type="button">${a.event}</button>${a.acked ? html` <span class="tag">acknowledged</span>` : ''}</td><td class="src">${a.datetime.slice(5, 16)}</td><td class="detail hide-sm">${a.kind}</td><td class="num hide-sm">${a.minutes}</td><td class="detail hide-sm">${a.notified}</td><td class="act">${C.rowActions(a.id)}</td></tr>`)}`
      : alView === 'acked' && AL.length ? html`<tr><td colspan="7" class="why">No alarm event in the last 30 days is acknowledged.</td></tr>`
      : AL.length ? html`<tr><td colspan="7" class="why">Every alarm event in the last 30 days is acknowledged. The Acknowledged view lists them.</td></tr>`
      : html`<tr><td colspan="7" class="why">No alarm event in the last 30 days of the export.</td></tr>`);
  }
  const alarmState = a => a.kind === 'generator' ? ['queued', 'weekly generator test'] : /Normal/.test(a.event) ? ['ok', 'return to normal'] : ['warning', 'alarm'];

  // A read retires the objects it did not put: the open task's notes and assignments are put again (task-details.js).
  function draw() { TD?.putAll(); lamps(); drawSources(); drawMap(); legend(); drawTrends(); drawTasks(); drawLedgers(); }

  // ---------- Reader (read.js) ----------
  let reader = null;
  const load = () => reader.load();
  // Read again (sd:5638): the shell's head button reads /api/hoa and the shown Briefs or Reports
  // view again. Download from Mission, on Data sources, starts a download.
  window.PAGE_AGAIN = { title: 'Read the HOA readings and the shown view again', run: () => Promise.all([load(), window.places.again()]) };
  // One timer at a time; each read that still sees a running job sets the next. sd:10610: the timer reads the small
  // /api/hoa/status, and reads the whole document (its trends near a megabyte) only when that status moved from the
  // last full read's, the run ended, or the status did not read.
  function poll() { if (!polling) polling = setTimeout(check, SRC_POLL); }
  async function check() {
    const now = await window.shell.getJSON('/api/hoa/status').catch(() => null);
    polling = 0;
    if (now?.running && DOC?.status && JSON.stringify(now.parts) === JSON.stringify(DOC.status.parts)) return poll();
    reader.reread();
  }
  const spec = {
    source: '/api/hoa', what: 'the HOA readings',
    adopt: doc => {
      DOC = doc; M = doc.mission;
      ASSETS = doc.assets.features; LOTS = doc.base.features;
      // sd:5671: each item is an /api/tasks row (hoa_screen._items) in task-details.js's task shape, with HOA's due state.
      OB = doc.items.rows.map(r => Object.assign(TD.shape(r), { state: r.state, due_days: r.due_days }));
      OBD = (doc.items.done || []).map(r => Object.assign(TD.shape(r), { state: r.state, due_days: r.due_days }));
      TD.adopt(TASKS());
      // An event's id is its datetime and text, the server's view key (hoa_screen.alarm_key), so it holds across reads.
      AL = M.alarms.slice().reverse().map(a => { const [st, why] = alarmState(a); return { ...a, id: `alarm:${a.datetime} ${a.event}`, st, why }; });
      ACKS = M.acks && !M.acks.available ? `Acknowledge is off: ${M.acks.reason}` : '';
      // The end date is the site's calendar day, so the read's time is compared in the browser's zone, not UTC.
      const today = new Date(doc.read).toLocaleDateString('en-CA');
      SRC = (doc.sources?.rows || []).filter(r => r.state !== 'retired' && !(r.ends && r.ends < today));
      DL = doc.downloads || {};
      P = project(); if (!P) { zoom = 1; center = null; }
      CELLS = readCells(); attention();
      document.body.dataset.observed = doc.read;
      const c = doc.config, failed = [M.error && `Mission export: ${M.error}`, doc.assets.error && `Assets: ${doc.assets.error}`, doc.base.error && `Base map: ${doc.base.error}`, !doc.base.error && doc.lots.error && `Lots: ${doc.lots.error}`, doc.items.error && `Tasks: ${doc.items.error}`].filter(Boolean);
      put($('subhead'), html`Water system: ${plural(ASSETS.length, 'mapped asset')}, ${plural(LOTS.length, 'lot and tract outline', 'lot and tract outlines')}; Mission export ${M.latest ? `through ${M.latest}` : M.files?.length ? 'with a daily feed unknown' : 'not read'} · read <time class="rel" datetime="${doc.read}"></time>`);
      const objects = [...ASSETS.map(a => ({ id: `asset:${a.id}`, type: 'asset', label: `${a.id} · ${a.name}`, asset: a.id, name: a.name })),
        ...TASKS().map(TD.objectOf),
        // sd:5621: the Alarms toolbar, which Acknowledge all acts on; its label is the count the confirm names.
        ...(M.error ? [] : [{ id: 'alarms', type: 'alarms', label: plural(AL.filter(a => !a.acked).length, 'alarm') }]),
        ...AL.map(a => ({ id: a.id, type: 'alarm', label: `${a.datetime.slice(5, 16)} ${a.event}`, key: a.key, acked: a.acked })),
        ...(M.dir && !srcOf('mission') ? [{ id: 'source:mission', type: 'source', label: 'Mission export', source: 'mission' }] : []),
        // A source with a refresh job is a job row: Management's shared Retry and Show log act on it.
        ...SRC.map(r => ({ id: `source:${r.source}`, type: r.job ? 'job' : 'source', label: r.label, source: r.source })),
        // sd:5626: a download for each source row the page shows that has one.
        ...SRC.filter(r => DL[r.source]).map(r => ({ id: `download:${r.source}`, type: `${r.source} download`, label: DL[r.source].label, source: r.source }))];
      SWEEP = doc.sweep || null;
      if (SWEEP) objects.push({ id: 'sweep', type: 'sweep', label: 'HOA sweep' });
      if (SRC.some(r => r.job?.state === 'running') || SWEEP?.running || Object.values(DL).some(d => d.running)) poll();
      const st = c.state === 'missing' ? { kind: 'empty', title: 'No HOA checkout', text: `No HOA folder is registered. Add the repo whose folder is named hoa (sd-db repo add).`, source: c.source }
        : c.problems.length ? { kind: 'partial', text: `${c.source}: ${c.problems.join(' · ')}.`, source: c.source }
        : failed.length ? { kind: 'partial', text: `${failed.join(' · ')}. The other parts are current.`, source: '/api/hoa' } : null;
      return { objects, state: st };
    },
    clear: () => {
      DOC = null; M = null; ASSETS = []; LOTS = []; OB = []; OBD = []; AL = []; P = null; CELLS = []; SRC = []; SWEEP = null; DL = {};
      delete document.body.dataset.observed;
      put($('subhead'), html`The HOA readings could not be read.`);
      window.shell.attention({ state: 'unknown', n: 0, what: 'HOA not read' });
    },
    draw,
    current: () => selected,
    first: () => OB[0]?.key,
    // sd:5620: a Briefs row fills Details in its view (places.js); a read selects HOA's own rows only outside Briefs and Reports.
    active: () => own(),
    select: (id, opened) => select(id, opened),
    unselect: () => { selected = null; mark(); put($('details'), html`<p class="note">${DOC ? 'Select a marker, a task, an alarm or a trend day to see it here.' : 'Nothing is selected: the HOA readings could not be read.'}</p>`); },
  };

  window.PAGE_COMMANDS = [
    { label: 'Read HOA again', icon: 'rotate-ccw', run: () => load() },
    { label: 'Fit the map to the HOA', icon: 'maximize-2', run: () => { center = null; zoomTo(1); } },
    { label: 'Download from Mission', icon: 'download', run: () => downloadFrom('mission') },
    { label: 'Acknowledge all alarms', icon: 'check', run: () => acknowledgeAll() },
  ];
  // The shell walks the ledger rows on j / k; a row selects by its own click.
  window.PAGE_LIST = { rows: () => document.querySelectorAll('.ledger tbody tr[data-id]'), current: () => selected, select: id => select(id, false) };

  document.addEventListener('DOMContentLoaded', () => {
    const S = window.shell, C = S.commands;
    // ---------- Tasks' Details (sd:5671) ----------
    // A task's Details, commands, writes and dialogs are the Tasks page's (task-details.js), as on Home and the realm places.
    // HOA gives it the rows, the selection and the read; its due line; a link to Tasks; and Draft email (sd:3463, which
    // HOA's Draft followup posted too) as a followup's first action. Email is never sent from the dashboard.
    TD = window.taskDetails.attach({ details: $('details'), rows: TASKS, selected: () => own() ? selected : null, read: () => DOC?.read, view: () => 'place',
      render: () => { drawTasks(); if (own() && TASKS().some(o => o.key === selected)) TD.renderDetails(); }, reread: () => reader.reread(), load: () => reader.load(),
      select: (key, open) => select(key, open), draftFirst: t => t.kind === 'followup',
      note: t => html`<p class="why"><span class="g-${t.state}" aria-hidden="true">${GLYPH[t.state]}</span> ${t.due ? `${t.due.slice(0, 10)} · ${dueText(t)}` : 'no due date'}</p>`,
      foot: () => DOC ? html`Rows read <time class="rel" datetime="${DOC.read}"></time> from /api/hoa.` : '',
      more: t => html`<p class="why"><a href="${S.itemHref(t.id)}">Open on Tasks</a>: the board, the matrix and every task.</p>` });
    reader = S.read(spec);
    // ---------- Commands (products/system/commands.md) ----------
    C.register(
      { id: 'asset.ask', on: 'asset', label: 'Ask', key: 'a', icon: 'message-square', risk: 'safe', primary: () => true,
        does: o => `Asks Claude in chat what is known about ${o.asset}.`,
        run: o => { S.openChat(); S.send(`What do we know about ${o.asset} (${o.name})?`); return ''; } },
    );

    $('annunciator').addEventListener('click', e => {
      const c = e.target.closest('button.cell'); if (!c) return;
      const k = c.dataset.open; if (!k) return;
      if (k.startsWith('day:')) $('trends').scrollIntoView({ behavior: reduce() ? 'auto' : 'smooth' });
      select(k, true);
    });
    $('map').addEventListener('click', e => {
      if (dragged) return;
      const a = e.target.closest('[data-asset]'); if (a) { select(`asset:${a.dataset.asset}`, true); return; }
      const t = e.target.closest('[data-tract]'); if (t) { selectTract(t.dataset.tract, true); return; }
      const l = e.target.closest('[data-lot]'); if (l) selectLot(l.dataset.lot, true);
    });
    $('map').addEventListener('keydown', e => {
      const a = e.target.closest('[data-asset]');
      if (a && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); e.stopPropagation(); select(`asset:${a.dataset.asset}`, true); }
    });
    // Drag pans: a pointer move turns into viewBox units at the map's drawn width.
    let drag = null, dragged = false;
    $('map').addEventListener('pointerdown', e => { if (!P || zoom === 1) return; drag = { x: e.clientX, y: e.clientY, c: view() }; dragged = false; });
    $('map').addEventListener('pointermove', e => {
      if (!drag) return;
      const u = drag.c[2] / ($('map').clientWidth || W), dx = (e.clientX - drag.x) * u, dy = (e.clientY - drag.y) * u;
      if (Math.abs(dx) + Math.abs(dy) > 2 * u) dragged = true;
      center = [drag.c[0] + drag.c[2] / 2 - dx, drag.c[1] + drag.c[3] / 2 - dy]; drawMap();
    });
    addEventListener('pointerup', () => { drag = null; setTimeout(() => { dragged = false; }, 0); });
    $('fit').addEventListener('click', () => { center = null; zoomTo(1); });
    $('zoom-in').addEventListener('click', () => zoomTo(zoom * 2));
    $('zoom-out').addEventListener('click', () => { zoomTo(zoom / 2); if (zoom === 1) { center = null; drawMap(); } });
    $('legend').addEventListener('click', e => {
      const b = e.target.closest('button[data-type]'); if (!b) return;
      hidden.has(b.dataset.type) ? hidden.delete(b.dataset.type) : hidden.add(b.dataset.type);
      legend(); drawMap();
    });
    $('trend-list').addEventListener('click', e => {
      const sc = e.target.closest('.scrub'); if (!sc) return;
      const r = sc.getBoundingClientRect(), days = daysOf(sc), i = Math.max(0, Math.min(days.length - 1, Math.floor((e.clientX - r.left) / r.width * days.length)));
      selectDay(days[i], true, sc.dataset.series);
    });
    // The chart's own keys stop here, so the shell's Enter (shell:open) does not also run.
    $('trend-list').addEventListener('keydown', e => {
      const sc = e.target.closest('.scrub'); if (!sc || !['ArrowLeft', 'ArrowRight', 'Enter', ' '].includes(e.key)) return;
      e.preventDefault(); e.stopPropagation();
      const days = daysOf(sc), at = days.indexOf(selDay), i = at < 0 ? days.length - 1 : at + (e.key === 'ArrowLeft' ? -1 : e.key === 'ArrowRight' ? 1 : 0);
      selectDay(days[Math.max(0, Math.min(days.length - 1, i))], true, sc.dataset.series);
    });
    $('range').addEventListener('click', e => {
      const c = e.target.closest('.chip'); if (!c) return;
      if (c.getAttribute('aria-disabled') === 'true') { S.toast('Daily data: 24h needs the hourly Mission export.'); return; }
      range = +c.dataset.range;
      $('range').querySelectorAll('.chip').forEach(x => x.setAttribute('aria-pressed', x === c ? 'true' : 'false'));
      drawTrends();
    });
    // sortBy flips a sorted header; a third click, on a descending one, returns to the rank instead.
    $('ob-head').addEventListener('click', e => {
      const b = e.target.closest?.('button[data-sort]');
      if (b && OB_ST.sort === b.dataset.sort && OB_ST.dir < 0) { OB_ST.sort = ''; S.list.keepFocus(e, drawTasks); }
      else if (S.list.sortBy(e, OB_ST)) S.list.keepFocus(e, drawTasks);
    });
    $('ob-kinds').addEventListener('click', e => {
      const b = e.target.closest('button[data-kind]'); if (!b) return;
      kind = b.dataset.kind === kind ? 'all' : b.dataset.kind; drawTasks();
    });
    // ---------- Acknowledge (sd:4967) ----------
    // The workflow database's view state, as Designs' Hide: POST /api/hoa/alarm. Undo is the same post with the old value.
    // A 409 (the export no longer holds the event) names the event, and the shell reads again.
    const ack = (o, acked) => S.post('/api/hoa/alarm', { key: o.key, acked }).then(v => reader.reread().then(() => v));
    const NO_CLI = 'no CLI: view state; the dashboard writes it (POST /api/hoa/alarm)';
    C.register(
      { id: 'alarm.ack', on: 'alarm', label: 'Acknowledge', does: 'Hides the event here only; the Mission portal is not told. Undo shows it.',
        key: 'a', icon: 'check', risk: 'undo', bulk: true, primary: o => !o.acked, when: o => o.acked ? 'already acknowledged' : ACKS || true, cli: () => NO_CLI,
        run: o => ack(o, true).then(() => `Acknowledged: ${o.label}. The Acknowledged view lists it.`), undo: o => ack(o, false) },
      { id: 'alarm.unack', on: 'alarm', label: 'Unacknowledge', does: 'Shows the event in the list again. Undo acknowledges it again.',
        key: 'a', risk: 'undo', bulk: true, when: o => !o.acked ? 'not acknowledged' : ACKS || true, cli: () => NO_CLI,
        run: o => ack(o, false).then(() => `Unacknowledged: ${o.label}`), undo: o => ack(o, true) },
    );
    // ---------- Acknowledge all (sd:5621) ----------
    // One post for every unacknowledged event: POST /api/hoa/alarms writes each key as the single route does, and names the
    // keys it did not write (an event the export dropped, or a refused write). It asks first and names the count; the toast
    // says how many landed and which did not, and Undo posts back only the keys that landed.
    const ackMany = (keys, acked) => S.post('/api/hoa/alarms', { keys, acked }).then(v => reader.reread().then(() => v));
    ACK_ALL = { id: 'alarms.ack', on: 'alarms', label: 'Acknowledge all', icon: 'check', risk: 'undo', primary: () => true, executes: true,
      askFirst: true, does: 'Hides every open alarm event here only. Undo shows them.',
      when: () => alarmsErr() ? `alarm-events.csv did not read: ${alarmsErr()}` : ACKS || (openAlarms().length ? true : 'no alarm event is unacknowledged'),
      consequence: () => `Hides ${plural(openAlarms().length, 'unacknowledged alarm event')} in this list. The Mission portal is not told; Undo shows them again.`,
      cli: () => NO_CLI, sends: () => 'POST /api/hoa/alarms {keys, acked: true}',
      run: () => { const rows = openAlarms(), named = new Map(rows.map(a => [a.key, `${a.datetime.slice(5, 16)} ${a.event}`]));
        return ackMany(rows.map(a => a.key), true).then(out => {
          const got = out.acked.map(x => x.key), bad = out.failed.map(x => `${named.get(x.key) || x.key} (${x.error})`);
          return { keys: got, text: bad.length ? `Acknowledged ${got.length} of ${plural(rows.length, 'alarm')}. ${bad.length} not acknowledged: ${bad.join(', ')}`
            : `Acknowledged ${plural(got.length, 'alarm')}. The Acknowledged view lists them.` }; }); },
      undo: (o, v) => !v?.keys?.length ? false : ackMany(v.keys, false).then(out => {
        if (out.failed.length) throw new Error(`${out.acked.length} of ${v.keys.length} shown again; ${out.failed.length} not: ${out.failed.map(x => x.error).join(', ')}`);
        return true; }) };
    C.register(ACK_ALL);
    $('al-tools').addEventListener('click', e => { if (e.target.closest('#al-ack-all')) acknowledgeAll(); });
    // ---------- Refresh a source (sd:4965) ----------
    // Each refreshable source has a manual cron job, named by its source-ages row (sd:5280). Its row registers Management's shared Retry
    // (commands.js), which posts /api/jobs/<name>/retry with the job's revision; the jobs inventory's answer is the off
    // reason. The run's state and its last output line come back with the next read.
    const facts = o => { const j = srcOf(o.source)?.job || {};
      return { name: j.name, service: j.service, failed: j.state === 'failed', exit: j.last_exit,
        capability: j.error ? { allowed: false, reason: j.error } : j.state === 'running' ? { allowed: false, reason: `${j.name} is running now` } : j.capabilities?.retry }; };
    const kick = j => S.post(`/api/jobs/${encodeURIComponent(j.name)}/retry`, { revision: j.revision });
    const refresh = o => { const j = srcOf(o.source).job;
      return kick(j).then(out => reader.reread().then(() => { poll(); return window.commands.answered(out, j.name, 'started', `${o.label} refresh started · ${j.name} · HOA reads again until it ends`); })); };
    // sd:5678: a source with a Download runs that Download from its row: its job exits 3 on a login page, the Download
    // waits for the login. Its off reason, line, post and answer are the Download's, so one lock refuses both alike.
    const via = o => DL[o.source] && DOWNLOADS[o.source];
    const ran = o => { const d = DL[o.source], w = via(o).when();
      return { name: via(o).label, failed: ['failed', 'unavailable'].includes(d.state), exit: d.exit, sends: via(o).sends(),
        capability: { allowed: w === true, reason: w === true ? '' : w } }; };
    const retry = window.commands.retry({ job: o => via(o) ? ran(o) : facts(o), run: o => via(o) ? via(o).run() : refresh(o) });
    // sd:5680: so Show log on that row is the download's start.log, in the row's Details; the job's log opens on Management.
    const shown = window.commands.showLog({ job: facts }), shq = s => `'${String(s).replace(/'/g, `'\\''`)}'`;
    C.register(
      // sd:5728: Run now is the source row's act (its play icon) whatever the last run did, not only after a failure;
      // a job whose last run did not find its command (exit 127) keeps it in the menu (sd:3392).
      { ...retry,
        primary: o => !!via(o) || facts(o).exit !== 127,
        does: o => via(o) ? via(o).does : o.source === 'mission' ? 'Exports and imports Mission data once in signed-in Chrome, up to 30 min.'
          : `Pulls ${o.label} once through its refresh job. Nothing to undo.`,
        cli: o => via(o) ? via(o).cli() : retry.cli(o) },
      { ...shown,
        does: o => via(o) ? 'Shows the last download\'s log in Details.' : shown.does,
        cli: o => via(o) ? `cat ${shq(DL[o.source].log)}` : shown.cli(o),
        run: o => { if (!via(o)) return shown.run(o); select(`source:${o.source}`, true); return `${DL[o.source].label} download log is in Details`; } },
    );
    // ---------- Download (sd:5626) ----------
    // POST /api/hoa/download {source} starts hoa_download.py detached: a Claude Chrome session opens the portal, waits for
    // the operator's login, then the checkout's own download runs. A 409 names the run going; a 400 the missing procedure.
    // HOA reads again until it ends; the source row shows "waiting for you to log in" while the session waits.
    for (const [k, label] of [['mission', 'Mission']]) {
      DOWNLOADS[k] = { id: `download.${k}`, on: `${k} download`, label: `Download from ${label}`, icon: 'download', risk: 'safe', primary: () => true, executes: true,
        does: `Opens ${label} in Chrome, waits for your login, then downloads.`,
        when: () => { const d = DL[k]; return !d ? 'HOA is not read' : d.error || (d.running ? `a ${label} download is running now: ${downloadText(d)}` : true); },
        cli: () => DL[k]?.command || '', sends: () => `POST /api/hoa/download {"source": "${k}"}`,
        run: () => S.post('/api/hoa/download', { source: k }).then(
          out => reader.reread().then(() => { poll(); return `${label} download started ${String(out.started).slice(11, 16)} UTC · log in in Chrome if it asks`; }),
          err => { if ([400, 409].includes(err.status)) return err.message; throw err.status ? err : new Error(`no answer (${err.message}), so whether the ${label} download started is not known`); }) };
      C.register(DOWNLOADS[k]);
    }
    $('src-tools').addEventListener('click', e => { const b = e.target.closest('[data-download]'); if (b) downloadFrom(b.dataset.download); });
    // ---------- Run sweep (sd:5095) ----------
    // POST /api/hoa/sweep starts the checkout's tools/sweep.py in the background (sd_dashboard/sweep.py); a 409 names the run going.
    // HOA reads again until the run ends; the row's lamp and text follow it. It asks first: the run lands a commit.
    C.register({ id: 'sweep.run', on: 'sweep', label: 'Run sweep', does: 'Checks HOA mail and files, then drafts follow-ups. Sends nothing.',
      key: 'w', icon: 'play', risk: 'safe', askFirst: true, executes: true, primary: () => true,
      when: () => SWEEP?.error || (SWEEP?.running ? 'a sweep is running now' : true),
      consequence: () => 'Reviews the week, both Drives and the alias mail, updates the status and the VP brief, saves followups as Gmail drafts, and hands the commit to the lane. Up to 90 min.',
      cli: () => SWEEP?.command || 'python3 tools/sweep.py run', sends: () => 'POST /api/hoa/sweep {}',
      run: () => S.post('/api/hoa/sweep', {}).then(
        out => reader.reread().then(() => { poll(); return `Sweep started ${String(out.started).slice(11, 16)} UTC · HOA reads again until it ends`; }),
        err => { if ([400, 409].includes(err.status)) return err.message; throw err.status ? err : new Error(`no answer (${err.message}), so whether a sweep started is not known`); }) });
    // sd:5752: Open | All on Tasks; the URL keeps All as ?tasks=all, and Open leaves none.
    $('ob-show').addEventListener('click', e => {
      const b = e.target.closest('[data-f="tasks"]'); if (!b || b.dataset.v === obShow || !['open', 'all'].includes(b.dataset.v)) return;
      obShow = b.dataset.v;
      const p = new URLSearchParams(location.search);
      if (obShow === 'all') p.set('tasks', 'all'); else p.delete('tasks');
      S.url(p); drawTasks();
    });
    $('al-show').addEventListener('click', e => {
      const b = e.target.closest('[data-alview]'); if (!b || b.dataset.alview === alView) return;
      alView = b.dataset.alview; drawLedgers();
    });
    document.addEventListener('shell:picked', e => $('al-body').querySelectorAll('tr[data-id]').forEach(tr => tr.toggleAttribute('data-picked', e.detail.includes(tr.dataset.id))));
    ['ob-body', 'al-body', 'src-body'].forEach(b => $(b).addEventListener('click', e => {
      if (e.target.closest('.rowact')) return;
      const tr = e.target.closest('tr[data-id]'); if (tr) select(tr.dataset.id, true);
    }));
    document.addEventListener('shell:open', e => select(e.detail, true));
    S.suggest(['Which HOA task is most overdue, and who is it waiting on?', 'How did A2 pump runtime change over the last 30 days?', 'Which assets are not in service?']);
    load();
  });
})();
// Top level, not inside DOMContentLoaded: shell.js builds the ? sheet from PAGE_KEYS when it loads.
window.PAGE_KEYS = [['↵', 'On a map asset: show it in Details'], ['← / →', 'On a trend: the previous or next day, shown in Details']];
