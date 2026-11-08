// Metrics (sd:2119): the design source's products/system/designs/pages/metrics.js, ported. Each change from the reference is
// marked "build:". The readings are /api/metrics (metrics_screen.py), read through the shared reader; none is sample data.
// A part that did not read, and each panel no reader covers, is unknown with its reason.
// sd:5473: CI health reads the merge lane's gate runs (metrics_screen._ci), not GitHub Actions. The flaky heads moved
// to Today's Needs you (pack sd:7663).
// Writes (sd:3387): Cap and Configure post /api/bills/<bill>/cap and /api/providers/configure with the revision the page
// read (controls.revision). A 400 or 409 wrote nothing; any other failure, or no answer, leaves the outcome unknown.
// build: bars are SVG rects sized by attribute, not CSSOM widths, and the age chart is the design's bar list only.
// sd:5080: the panels are the shell's views (shell.js, view:start): Spend (spend, the week, usage), Providers, CI and
// Age. A lamp shows the view that holds its reading. Skill use moved to Skills' Usage view (sd:5881).
// pack sd:7208: the range 6mo reads /api/metrics?range=6mo and draws its `ranged` parts: Spend adds each month by bill,
// and Providers and CI read the six months. The lamps and the badge stay on the 7d reading. ?range=6mo keeps the choice.
// pack sd:7233: Lane times, Queue's Last 7 days moved here, reads the document's `lanes`: 7 days whatever the range.
// pack sd:7524: Operations Usage moved here. The month picker reads /api/metrics?month=YYYY-MM, kept as ?month=, and Spend
// draws each capped bill's burn: spend to date, the cap and the straight projection from the month's rate.
// moved:start
// sd:5881: an old address of the Skills view opens Skills' Usage view, keeping its query (?row=).
{
  const moved = { skills: '/skills#view=usage' }[(/^#view=([a-z]+)$/.exec(location.hash) || [])[1]];
  if (moved) location.replace(moved.replace('#', `${location.search}#`));
}
// moved:end
window.PAGE_VIEWS = [{ id: 'spend', label: 'Spend' }, { id: 'providers', label: 'Providers' }, { id: 'ci', label: 'CI' },
  { id: 'times', label: 'Lane times' }, { id: 'age', label: 'Age' }];
(() => {
  const { html, put, plural, cells } = window.markup;
  const $ = id => document.getElementById(id);
  const G = { ok: '●', caution: '▲', warning: '■', unknown: '▨', queued: '◌' };
  const RANK = { warning: 3, caution: 2, unknown: 1, ok: 0 };
  const money = v => v == null ? '—' : '$' + (+v).toFixed(2);
  const fmt = n => (+n || 0).toLocaleString('en-US');
  const pctState = p => p >= 100 ? 'warning' : p >= 75 ? 'caution' : 'ok';
  const unknownBlock = why => html`<p class="state-unknown"><b>▨ Not read.</b> ${why}</p>`;
  const track = (pct, cls = '') => html`<svg class="track${pct == null ? ' hatch' : ''}" viewBox="0 0 100 1" preserveAspectRatio="none" aria-hidden="true">${pct == null ? '' : html`<rect class="fill ${cls}" width="${Math.max(0, Math.min(100, pct)).toFixed(1)}" height="1"/>`}</svg>`;
  let DOC = null, EV = {}, CELLS = [], selected = null, FIRST = null, SAID = null;
  // The range segment: 7d and 6mo are live; the others say why not (pack sd:7084, sd:7208).
  const RANGES = [{ id: '24h', off: 'Most sources here are daily or weekly; 24h would be one reading' }, { id: '7d' },
    { id: '30d', off: '30d switches on once the cost ledger holds 30 days of rows' }, { id: '6mo' }];
  const asked = new URLSearchParams(location.search);
  let range = asked.get('range') === '6mo' ? '6mo' : '7d';
  // The month the picker names, YYYY-MM, or '' for this one (pack sd:7524).
  let month = /^[0-9]{4}-(0[1-9]|1[0-2])$/.test(asked.get('month') || '') ? asked.get('month') : '';
  const query = () => [...(range === '7d' ? [] : [['range', range]]), ...(month ? [['month', month]] : [])];
  // The part a ranged panel draws: the 6mo read's own, else the 7d one.
  const part = k => DOC?.ranged ? DOC.ranged[k] : DOC?.[k];
  const span = () => DOC?.ranged ? `${DOC.ranged.months[0]} to ${DOC.ranged.months[DOC.ranged.months.length - 1]}` : 'this month';
  const within = c => DOC?.ranged ? `since ${c.since.slice(0, 10)}` : `in the last ${c.days} days`;
  // The registry's controls, by bill and by provider; a part that did not read leaves both empty.
  const ctlBill = name => DOC?.controls?.bills?.find(b => b.name === name) || null;
  const ctlProvider = name => DOC?.controls?.providers?.find(p => p.name === name) || null;

  // ---------- Evidence: every bar, row and cell names its facts and its CLI line ----------
  function evidence() {
    EV = {};
    const d = DOC, u = d.usage, put1 = (id, e) => { EV[id] = e; return id; };
    if (!u.error) {
      u.bills.forEach(b => put1(`bill:${b.name}`, { type: 'bill', kind: `Bill · ${u.month}`, title: `${b.name} · ${b.cost_basis}`, label: `${b.name} bill`, bill: b.name, basis: b.cost_basis, cap: b.cap,
        revision: d.controls.revision, control: ctlBill(b.name),
        facts: [['Spent', money(b.spent)], ['Estimated', money(b.estimated)], ['Held', money(b.held)], ['Cap', b.cap == null ? 'none' : money(b.cap)], ['Room', b.room == null ? '—' : money(b.room)], ['Source', 'usage.read: cost run + bound rows']], cli: `sd-db usage --month ${u.month}` }));
      u.meter.forEach(m => put1(`win:${m.provider}:${m.window_minutes}`, { kind: 'Budget window', title: `${m.provider} · ${m.window_minutes} min`, s: pctState(m.used_percent),
        facts: [['Used', `${m.used_percent}%`], ['Bill', m.bill], ['Read', m.timestamp], ['Source', 'cost, source meter']], cli: 'sd-db usage --json' }));
      u.roles.forEach((r, i) => put1(`role:${i}`, { kind: `Usage · ${u.month}`, title: `${r.bill || '-'} · ${r.provider || '-'} · ${r.role || '-'}`,
        facts: [['Calls', r.calls], ['Spent', money(r.spent)], ['Held', money(r.held)], ['Tokens in', fmt(r.tokens_in)], ['Tokens out', fmt(r.tokens_out)]], cli: `sd-db usage --month ${u.month}` }));
      put1('spend', { kind: `Spend · ${u.month}`, title: `${money(u.spent)} spent, ${money(u.held)} held`, facts: [...u.bills.map(b => [b.name, `${money(b.spent)}${b.cap == null ? '' : ` of ${money(b.cap)}`}`]), ...(u.unbilled == null ? [] : [['No bill', money(u.unbilled)]])],
        note: 'Dollars only. Subscription and plan bills spend windows, shown beside the dollars.', cli: `sd-db usage --month ${u.month}` });
      put1('bound', { kind: 'Usage · bound rows', title: `${plural(u.bound.length, 'bound row')}`, s: u.bound.length ? 'caution' : 'ok',
        facts: u.bound.map(r => [`${r.bill || '—'} · ${r.provider || '—'}`, `${money(r.usd)} · ${r.timestamp}`]),
        note: 'Money a provider may have billed for a lost response; counted at the bound until corrected against the invoice.', cli: `sd-db usage --month ${u.month}` });
    }
    const sm = d.ranged?.spend;
    if (sm && !sm.error) sm.months.forEach(m => put1(`mo:${m.month}`, { kind: 'Spend · month', title: m.rows ? `${m.month} · ${money(m.spent)}` : `${m.month} · no rows`, s: m.rows ? 'ok' : 'unknown',
      facts: m.rows ? [...sm.bills.map(b => [b.name, windowed(b) && !m.bills[b.name] ? 'window, not dollars' : money(m.bills[b.name])]), ['No bill', money(m.unbilled)]] : [['Ledger', 'no run or bound row this month']],
      note: m.rows ? '' : 'No row is not a zero: the ledger holds nothing for this month.', cli: `sd-db usage --month ${m.month}` }));
    if (!d.trend.error) d.trend.series.forEach(s => s.points.forEach(([day, p, at], i) => put1(`t:${s.provider}:${day}`, { kind: 'Window reading', title: `${s.provider} · ${day}`, s: pctState(p),
      facts: [['Used', `${p}%`], ['Window', `${d.trend.window} min (7 days)`], ['Reading', `last meter row of the day · ${at}`], ['Source', 'reads.meter_days']], cli: 'sd-db usage --json' })));
    if (!d.numbers.error) d.numbers.rows.forEach(n => put1(`num:${n.key}`, { kind: 'The week', title: n.label, s: n.value == null ? 'unknown' : 'ok', facts: [['Value', numText(n)], ...n.inputs, ['Source', 'reads.weekly_numbers']], cli: '—' }));
    const order = (role, name) => { const at = (d.controls.orders?.[role] || []).indexOf(name); return d.controls.error ? '— (not read)' : at < 0 ? 'not in the order' : `${at + 1} of ${d.controls.orders[role].length}`; };
    const sc = part('scorecard');
    if (!sc.error) sc.rows.forEach(r => put1(`sc:${r.provider}`, { type: 'provider', kind: `Provider · ${span()}`, title: r.provider, label: r.provider, p: r.provider, s: scState(r),
      revision: d.controls.revision, control: ctlProvider(r.provider),
      facts: [['Author order', order('author', r.provider)], ['Reviewer order', order('reviewer', r.provider)], ['Author rank', r.author_rank ?? '—'], ['Reviewer rank', r.reviewer_rank ?? '—'], ['Passes', r.passes], ['Blocking', r.blocking], ['$/pass', money(r.usd_per_pass)], ['Skipped', r.fallthrough], ['State', r.enabled ? 'enabled' : `disabled: ${r.reason || '—'}`]],
      note: scState(r) === 'caution' ? `Asked ${r.fallthrough} times and fell through every time, ${span()}.` : '', cli: 'sd providers configure --file CONFIG.json' }));
    if (!d.observed.error) put1('observed', { kind: 'Observed activity', title: `${d.observed.since} → ${d.observed.until}`, facts: [['Requests recorded', d.observed.recorded_requests], ['Finished review attempts', d.observed.finished_review_attempts], ['Recorded deliveries', d.observed.recorded_deliveries], ['Source', 'reporting.metrics']], note: d.observed.interpretation, cli: '—' });
    const pc = part('ci');
    if (!pc.error) {
      const lane = 'sd-ship -C CHECKOUT lane list', src = ['Source', 'reads.lane_entries: each lane entry\'s last checkpoint'];
      const said = f => f ? `sd:${f.item} · ${f.kind} · ${at(f.at)}` : '—';
      pc.repos.forEach(r => put1(`ci:${r.repository}`, { kind: `CI · ${d.ranged ? span() : `${pc.days} days`}`, title: r.repository, s: ciState(r),
        facts: [['Passed', r.passed], ['Gate failed', r.gate], ['Pass rate', rate(r.rate)], ['Stopped by review', r.review], ['Stopped otherwise', r.other], ['Failed, no reason kept', r.unknown],
          ['Last failure', said(r.newest)], ['Its reason', r.newest ? r.newest.reason || '—' : '—'], ['Last gate failure', said(r.newest_gate)], src],
        note: 'Pass rate is passed over gate runs. A review stop or a conflict ran no gate.', open: r.newest ? [window.shell.itemHref(r.newest.item), `Open sd:${r.newest.item} in Tasks`] : null, cli: lane }));
    }
    put1('ci', d.ci.error ? { kind: 'CI · not read', title: 'CI health', s: 'unknown', facts: [['Reason', d.ci.error]], cli: 'sd-ship -C CHECKOUT lane list' }
      : { kind: `CI · ${d.ci.days} days`, title: 'Gate runs through the merge lane', s: 'ok',
        facts: [['Since', at(d.ci.since)], ['Repositories', d.ci.repos.length], ['Unreadable entries', d.ci.unreadable], ['Source', 'reads.lane_entries']], cli: 'sd-ship -C CHECKOUT lane list' });
    if (d.lanes && !d.lanes.error) d.lanes.lanes.filter(l => l.times.error || l.times.entries).forEach(l => put1(`lane:${l.path}`, l.times.error
      ? { kind: 'Lane times · not read', title: l.repo, s: 'unknown', facts: [['Reason', l.times.error]], cli: `sd-ship -C ${l.path} lane list` }
      : { kind: `Lane times · ${l.times.days} days`, title: l.repo, s: l.times.over ? 'caution' : 'ok',
        facts: [['Entries', l.times.entries], ['Median wait', mins(l.times.wait)], ['Median gate', mins(l.times.gate)],
          ['Slowest entry', l.times.slowest ? `sd:${l.times.slowest.item} ${l.times.slowest.title} · ${mins(l.times.slowest.total)}` : 'none ended'],
          ['Source', 'sd_lane.lane_times: each entry\'s history rows']],
        note: l.times.over ? 'Median wait past 1 h: the lane run files the parallel-lanes question.' : '', cli: `sd-ship -C ${l.path} lane list` }));
    d.unread.forEach(x => put1(`unread:${x.id}`, { kind: 'Not read', title: x.name, s: 'unknown', facts: [['Reason', x.reason]], cli: '—' }));
  }
  const windowed = b => ['subscription', 'plan'].includes(b.cost_basis);
  const numText = n => n.value == null ? '—' : n.unit === 'usd' ? money(n.value) : n.unit === 'hours' ? `${(+n.value).toFixed(1)} h` : n.value % 1 ? (+n.value).toFixed(1) : String(n.value);
  const scState = r => r.fallthrough && !r.passes ? 'caution' : 'ok';
  const at = iso => `${String(iso).slice(5, 16).replace('T', ' ')}Z`;
  const rate = r => r == null ? '—' : `${Math.round(r * 100)}%`;
  const dur = s => s < 3600 ? `${Math.floor(s / 60)}m ${s % 60}s` : `${Math.floor(s / 3600)}h ${Math.floor(s % 3600 / 60)}m`;
  const mins = s => s == null ? 'none ended' : dur(Math.round(s));
  // A repository name may break after its owner, so a narrow table keeps every column on screen.
  const slashed = name => { const cut = name.indexOf('/'); return cut < 0 ? name : html`${name.slice(0, cut + 1)}<wbr>${name.slice(cut + 1)}`; };
  // A gate failure alone is the gate doing its job; a flaky head is a row on Today (pack sd:7663), not a lamp here.
  const ciState = r => r.passed + r.gate ? 'ok' : 'unknown';

  // ---------- Annunciator ----------
  function readCells() {
    // a stale month (sd:4955) keeps its numbers on show, and lights no lamp: each of its cells is unknown.
    const d = DOC, u = d.usage, out = [], stale = u.stale ? 'unknown' : '';
    out.push(u.error ? { id: 'spend', label: 'Spend month', view: 'spend', state: 'unknown', val: html`<b>—</b> not read`, small: u.error }
      : { id: 'spend', label: 'Spend month', view: 'spend', state: stale || 'ok', ev: 'spend', val: html`<b>${money(u.spent)}</b>`, small: `${u.month}${u.today == null ? '' : ' to date'} · ${plural(u.bills.length, 'bill')}` });
    if (!u.error) u.meter.filter(m => m.window_minutes === d.trend.window).forEach(m => out.push({ id: `w-${m.provider}`, label: `${m.provider} week`, view: 'spend', state: stale || pctState(m.used_percent), ev: `win:${m.provider}:${m.window_minutes}`,
      val: html`<b>${Math.round(m.used_percent)}%</b> used`, small: `meter ${String(m.timestamp).slice(5, 16).replace('T', ' ')}Z` }));
    if (!u.error) out.push({ id: 'bound', label: 'Estimated spend', view: 'spend', state: stale || (u.bound.length ? 'caution' : 'ok'), ev: 'bound', val: html`<b>${u.bound.length}</b> bound`, small: u.bound.length ? 'rows to check against invoices' : 'no row to check' });
    const ci = d.ci, runs = ci.error ? 0 : ci.repos.reduce((n, r) => n + r.passed + r.gate, 0), passed = ci.error ? 0 : ci.repos.reduce((n, r) => n + r.passed, 0);
    out.push(ci.error ? { id: 'ci', label: 'CI 7d', view: 'ci', state: 'unknown', ev: 'ci', val: html`<b>—</b> not read`, small: ci.error }
      : !runs ? { id: 'ci', label: 'CI 7d', view: 'ci', state: 'unknown', ev: 'ci', val: html`<b>—</b> no gate run`, small: `in ${ci.days} days` }
      : { id: 'ci', label: 'CI 7d', view: 'ci', state: 'ok', ev: 'ci', val: html`<b>${rate(passed / runs)}</b> passed`, small: plural(runs, 'gate run') });
    return out;
  }
  // the oldest reading on the page: a kept part (sd:4955) can be older than the request.
  const oldest = d => [d.read, d.usage.observed, d.numbers.observed].filter(Boolean).sort()[0];
  const hhmm = iso => `${iso.slice(11, 16)} UTC`;
  function lamps() {
    // sd:6697: no Observed clock; the shell's Read again (or r) is the page's one re-read.
    put($('annunciator'), cells([...CELLS.map(c => ({ button: true, state: c.state, attrs: html` data-cell="${c.id}" data-ev="${c.ev || ''}" data-view="${c.view}"`,
      label: c.label, mark: html`<span class="${c.state === 'ok' ? 'g' : ''}" aria-hidden="true">${G[c.state]}</span>`, val: html`<span class="ph">${c.val}</span>`, small: c.small }))]));
  }
  // The badge is the read's own (sd:3446, metrics_screen._attention), the cells above that can light.
  function attention() {
    window.PAGE_ATTENTION = DOC && DOC.attention || { state: 'unknown', n: 0, what: 'Metrics not read' };
    window.shell.attention(window.PAGE_ATTENTION);
  }

  // ---------- Panels ----------
  // A bar with an href is a link to the page that holds its row (sd:5476); the rest select their reading here.
  const bar = (id, k, sub, pct, v, cls, glyph, href) => { const body = html`<span class="k">${k}${sub ? html`<small>${sub}</small>` : ''}</span>${track(pct, cls)}<span class="v">${glyph ? html`<span class="g-${glyph}" aria-hidden="true">${G[glyph]}</span>` : ''}${v}</span>`;
    return href ? html`<li><a class="hbar" href="${href}">${body}</a></li>` : html`<li><button class="hbar" type="button" data-ev="${id}" aria-pressed="${id === selected ? 'true' : 'false'}">${body}</button></li>`; };
  function spend() {
    const u = DOC?.usage;
    $('bills-h').textContent = u && !u.error ? `Dollars in ${u.month}, by bill` : 'Dollars by bill';
    if (u && !u.error) $('month').value = u.month;
    if (!u || u.error) { put($('bills'), html`<li>${unknownBlock(u ? u.error : 'the Metrics document did not arrive')}</li>`); put($('windows'), html``); put($('burns'), html``); $('bills-note').textContent = ''; return; }
    // build: dollars are the month to date as sd usage reads them; a bill that spends a window shows hatched, not a zero.
    const max = Math.max(0.01, ...u.bills.map(b => b.spent));
    // build (hallmark audit): a bill at 75% of its cap is caution and at its cap a warning, each with its glyph, not colour alone.
    const capState = b => b.cap == null ? 'ok' : b.spent >= b.cap ? 'warning' : b.spent / b.cap >= 0.75 ? 'caution' : 'ok';
    put($('bills'), html`${u.bills.map(b => { const st = capState(b); return bar(`bill:${b.name}`, b.name, `${b.cost_basis}${b.cap == null ? '' : ` · cap ${money(b.cap)}`}`, windowed(b) && !b.spent ? null : (b.cap ? b.spent / b.cap * 100 : b.spent / max * 100), windowed(b) && !b.spent ? '▨ window' : money(b.spent), st === 'ok' ? '' : st, st === 'ok' ? '' : st); })}`);
    $('bills-note').textContent = `Total ${money(u.spent)} spent${u.unbilled == null ? '' : ` (${money(u.unbilled)} on no bill)`} and ${money(u.held)} held in ${u.month}. ▨ marks a bill that spends a vendor window, not a zero.`;
    burns(u);
    // A past month keeps this month's meter (metrics_screen._past); meter_error says that read failed.
    put($('windows'), u.meter_error ? html`<li>${unknownBlock(u.meter_error)}</li>` : u.meter.length ? html`${u.meter.map(m => { const st = pctState(m.used_percent); return bar(`win:${m.provider}:${m.window_minutes}`, m.provider, `${m.window_minutes} min window`, m.used_percent, `${m.used_percent}%`, st === 'ok' ? 's1' : st, st === 'ok' ? '' : st); })}` : html`<li class="why">No meter reading yet.</li>`);
  }
  // pack sd:7524: Operations Usage's burn line (charts.burn_svg), drawn here. A capped bill that is not a plan bill draws
  // spend to date as cumulative points, flat from its last row to today, the cap as a dashed rule, and, in the month
  // `now` is in, a straight projection at the month's rate. The projection stops where it leaves the chart, so its slope
  // is the rate, and data-cross names the day it passes the cap, rounded up (PR #430 review).
  function burns(u) {
    const capped = u.bills.filter(b => b.cap != null && b.cost_basis !== 'plan');
    if (!capped.length) { put($('burns'), html`<p class="why">No bill has a dollar cap in ${u.month}.</p>`); return; }
    put($('burns'), html`${capped.map(b => burn(b, u.days, u.today))}`);
  }
  function burn(b, days, today) {
    const W = 300, H = 80, pts = b.burn, last = pts[pts.length - 1], top = Math.max(b.cap, ...pts.map(p => p[1])) || 1;
    const X = d => (W * (d - 1) / Math.max(1, days - 1)).toFixed(2), Y = v => (H - 4 - (H - 8) * v / top).toFixed(2);
    const line = [...(!pts.length || pts[0][0] !== 1 ? [[1, 0]] : []), ...pts, ...(last && today > last[0] ? [[today, last[1]]] : [])];
    let proj = null;
    if (last && today) {
      const spent = last[1], rate = spent / today, end = spent + rate * (days - today);
      const cross = rate > 0 && end > b.cap ? Math.ceil(today + Math.max(0, b.cap - spent) / rate) : null;
      proj = { spent, end, cross, until: end <= top ? days : today + (top - spent) / rate };
    }
    const said = proj ? `projected ${money(proj.end)} by day ${days}${proj.cross == null ? '' : `; passes its cap on day ${proj.cross}`}` : 'no projection outside the month in progress';
    return html`<figure class="spark burn" data-burn="${b.name}"><header><span class="label">${b.name} · cap ${money(b.cap)}</span><span class="now">${money(b.spent)} <small>${said}</small></span></header>
      <svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img" aria-label="${b.name}: spend to date against its ${money(b.cap)} cap, ${said}">
        <line class="cap" data-cap="${(+b.cap).toFixed(2)}" x1="0" x2="${W}" y1="${Y(b.cap)}" y2="${Y(b.cap)}"/>
        <polyline class="ln" data-points="${pts.map(([d, v]) => `${d}:${(+v).toFixed(2)}`).join(';')}" points="${line.map(([d, v]) => `${X(d)},${Y(v)}`).join(' ')}"/>
        ${proj ? html`<line class="proj" data-end="${proj.end.toFixed(2)}" data-cross="${proj.cross == null ? '' : String(proj.cross)}" x1="${X(today)}" y1="${Y(proj.spent)}" x2="${X(proj.until)}" y2="${Y(Math.min(proj.end, top))}"/>` : ''}
      </svg><div class="axis"><span>day 1</span><span>dashed = cap · dotted = projection</span><span>day ${days}</span></div>
      <table class="sr"><caption>${b.name}, spend to date by day</caption><tbody>${pts.map(([d, v]) => html`<tr><th scope="row">day ${d}</th><td>${money(v)}</td></tr>`)}</tbody></table></figure>`;
  }
  function trends() {
    const t = DOC?.trend;
    if (!t || t.error) { put($('trends'), unknownBlock(t ? t.error : 'the Metrics document did not arrive')); return; }
    if (!t.series.length) { put($('trends'), html`<p class="why">No ${t.window}-minute meter reading in the last ${t.days.length} days.</p>`); return; }
    const W = 300, H = 80, n = t.days.length, step = W / n, X = i => (i + 0.5) * step, Y = p => H - 4 - (Math.min(p, 110) / 110) * (H - 8);
    put($('trends'), html`${t.series.map(s => { const pts = s.points.map(([day, p]) => [t.days.indexOf(day), p, day]).filter(x => x[0] >= 0), last = s.points[s.points.length - 1];
      return html`<figure class="spark" data-series="${s.provider}"><header><span class="label">${s.provider} · 7-day window</span><span class="now">${last[1]}% <small>${String(last[2]).slice(5, 16).replace('T', ' ')}Z</small></span></header>
      <svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img" aria-label="${s.provider} 7-day window, percent used, last reading per day ${t.days[0]} to ${t.days[n - 1]}">
        <line class="grid" x1="0" x2="${W}" y1="${Y(0)}" y2="${Y(0)}"/><line class="cap" x1="0" x2="${W}" y1="${Y(100)}" y2="${Y(100)}"/>
        <polyline class="ln" points="${pts.map(([i, p]) => `${X(i)},${Y(p)}`).join(' ')}"/>
        ${pts.map(([i, p, day]) => html`<circle class="${p >= 100 ? 'full' : 'mk'}${`t:${s.provider}:${day}` === selected ? ' sel' : ''}" cx="${X(i)}" cy="${Y(p)}" r="3.5" data-ev="t:${s.provider}:${day}" tabindex="0" role="button" aria-label="${s.provider} ${day}: ${p}%"/>`)}
      </svg><div class="axis"><span>${t.days[0].slice(5)}</span><span>dashed = 100%</span><span>${t.days[n - 1].slice(5)}</span></div>
      <table class="sr"><caption>${s.provider} 7-day window, percent used</caption><tbody>${s.points.map(([day, p]) => html`<tr><th scope="row">${day}</th><td>${p}%</td></tr>`)}</tbody></table></figure>`; })}`);
  }
  function unreadBlock(id) { const x = DOC?.unread.find(u => u.id === id); return x ? unknownBlock(`${x.reason}.`) : html``; }
  const row = (id, cells) => html`<tr data-ev="${id}" data-id="${id}" aria-selected="${id === selected ? 'true' : 'false'}">${cells}</tr>`;
  function tables() {
    const d = DOC, C = window.shell.commands;
    const nums = d && !d.numbers.error ? d.numbers.rows : [];
    put($('week-body'), !d ? html`` : html`${d.numbers.error ? html`<tr><td colspan="4">${unknownBlock(d.numbers.error)}</td></tr>` : ''}${nums.map(n => { const st = n.value == null ? 'unknown' : 'ok';
      return row(`num:${n.key}`, html`<td class="g g-${st}">${G[st]}<span class="sr">${st}</span></td><td class="what"><button type="button">${n.label}</button></td><td class="num">${numText(n)}</td><td class="mono hide-sm">${n.inputs.map(i => i.join(' ')).join(' · ')}</td>`); })}`);
    const sc = part('scorecard');
    $('sc-span').textContent = DOC?.ranged ? `${span()}.` : 'This month.';
    put($('sc-body'), !d ? html`` : sc.error ? html`<tr><td colspan="9">${unknownBlock(sc.error)}</td></tr>` : html`${sc.rows.map(r => { const st = scState(r);
      return row(`sc:${r.provider}`, html`<td class="g g-${st}">${G[st]}<span class="sr">${st === 'caution' ? 'caution: skipped every call' : 'normal'}</span></td><td class="what"><button type="button">${r.provider}</button>${r.enabled ? '' : html`<small>disabled</small>`}</td><td class="num hide-sm">${r.author_rank ?? '—'}</td><td class="num hide-sm">${r.reviewer_rank ?? '—'}</td><td class="num">${r.passes}</td><td class="num hide-sm">${r.blocking}</td><td class="num">${money(r.usd_per_pass)}</td><td class="num">${r.fallthrough}</td><td class="act">${C.rowActions(`sc:${r.provider}`)}</td>`); })}`);
    const u = d?.usage;
    put($('roles-body'), !d ? html`` : u.error ? html`<tr><td colspan="6">${unknownBlock(u.error)}</td></tr>` : u.roles.length ? html`${u.roles.map((r, i) => row(`role:${i}`, html`<td class="what"><button type="button">${r.bill || '—'}</button><small>${r.provider || '—'}</small></td><td class="hide-sm">${r.role || '—'}</td><td class="num">${r.calls}</td><td class="num">${money(r.spent)}</td><td class="num hide-sm">${fmt(r.tokens_in)}</td><td class="num hide-sm">${fmt(r.tokens_out)}</td>`))}`
      : html`<tr><td colspan="6" class="why">No call recorded in ${u.month} yet.</td></tr>`);
    $('usage-note').textContent = u && !u.error ? `${u.month}: ${money(u.spent)} spent, ${money(u.held)} held. ${u.bills.filter(b => b.cap != null).map(b => `${b.name} ${money(b.spent)} of its ${money(b.cap)} cap (room ${money(b.room)})`).join('; ')}`.trim() : '';
    $('us-h').textContent = u && !u.error ? `Usage · ${u.month}` : 'Usage';
  }
  function age() {
    const a = DOC?.age;
    if (!a || a.error) { put($('hist'), unknownBlock(a ? a.error : 'the Metrics document did not arrive')); return; }
    const max = Math.max(1, ...a.buckets.map(b => b.ready_to_send + b.other));
    put($('hist'), html`<ul class="hbars" aria-label="Open items by days in their current status">${a.buckets.map(b => { const tot = b.ready_to_send + b.other;
      // sd:5474: each bar opens Tasks' list at its bucket, open items only, on the All realm tab (sd:6263): the scope it counts (reads.age_bucket's key).
      return html`<li><a class="hbar" href="/tasks?view=list&age=${encodeURIComponent(b.key)}&active=1&realm=all" aria-label="${b.label}: ${tot} items, ${b.ready_to_send} ready to send"><span class="k">${b.label}<small>${b.ready_to_send ? `${b.ready_to_send} ready to send` : ''}</small></span><svg class="track" viewBox="0 0 100 1" preserveAspectRatio="none" aria-hidden="true"><rect class="fill s1" width="${(b.ready_to_send / max * 100).toFixed(1)}" height="1"/><rect class="fill" x="${(b.ready_to_send / max * 100).toFixed(1)}" width="${(b.other / max * 100).toFixed(1)}" height="1"/></svg><span class="v">${tot}</span></a></li>`; })}</ul>
      <table class="sr"><caption>Open items by age in status</caption><tbody>${a.buckets.map(b => html`<tr><th scope="row">${b.label}</th><td>${b.ready_to_send} ready to send, ${b.other} other</td></tr>`)}</tbody></table>`);
  }
  function ci() {
    const c = part('ci');
    if (!c || c.error) { put($('ci-state'), unknownBlock(c ? c.error : 'the Metrics document did not arrive')); put($('ci-body'), html``); return; }
    put($('ci-state'), c.unreadable ? html`<p class="foot-note"><span class="g-unknown" aria-hidden="true">▨</span> ${plural(c.unreadable, 'lane entry', 'lane entries')} ${c.unreadable === 1 ? 'is' : 'are'} not readable and ${c.unreadable === 1 ? 'is' : 'are'} not counted.</p>` : html``);
    put($('ci-body'), c.repos.length ? html`${c.repos.map(r => { const st = ciState(r), stopped = r.review + r.other + r.unknown;
      return row(`ci:${r.repository}`, html`<td class="g g-${st}">${G[st]}<span class="sr">${st}</span></td><td class="what"><button type="button">${slashed(r.repository)}</button></td><td class="num hide-sm">${r.passed}</td><td class="num">${r.gate}</td><td class="num">${rate(r.rate)}</td><td class="num hide-sm">${stopped}</td><td class="mono hide-sm">${r.newest ? `sd:${r.newest.item} · ${r.newest.kind}` : '—'}</td>`); })}`
      : html`<tr><td colspan="7" class="why">No lane entry ended ${within(c)}.</td></tr>`);
  }
  // Lane times (pack sd:6006, moved from Queue in pack sd:7233): one row per lane with an entry in 7 days, a read that
  // failed its own unknown row; the quiet lanes fold into one line. A median wait past an hour is caution, in words too.
  function laneTimes() {
    const T = DOC?.lanes;
    if (!T || T.error) { put($('times-body'), html`<tr><td colspan="6">${unknownBlock(T ? T.error : 'the Metrics document did not arrive')}</td></tr>`); $('times-note').textContent = ''; return; }
    const read = T.lanes.filter(l => l.times.error || l.times.entries), quiet = T.lanes.length - read.length;
    const days = (read.find(l => l.times.days) || { times: { days: 7 } }).times.days;
    put($('times-body'), read.length ? html`${read.map(l => { const t = l.times, st = t.error ? 'unknown' : t.over ? 'caution' : 'ok';
      return row(`lane:${l.path}`, html`<td class="g g-${st}">${G[st]}<span class="sr">${t.error ? 'not read' : t.over ? 'caution: median wait past 1 h' : 'normal'}</span></td><td class="what"><button type="button">${l.repo}</button></td>${t.error
        ? html`<td colspan="4" class="why">▨ not read: ${t.error}</td>`
        : html`<td class="num">${String(t.entries)}</td><td class="num">${mins(t.wait)}</td><td class="num">${mins(t.gate)}</td><td class="mono hide-sm">${t.slowest ? html`sd:${String(t.slowest.item)} ${t.slowest.title} · ${mins(t.slowest.total)}` : 'none ended'}</td>`}`); })}`
      : html`<tr><td colspan="6" class="why">No lane ran an entry in the last ${String(days)} days.</td></tr>`);
    const over = read.filter(l => !l.times.error && l.times.over).map(l => l.repo);
    $('times-note').textContent = [over.length ? `▲ Median wait past 1 h: ${over.join(', ')}. The lane run files the parallel-lanes question.` : '',
      quiet && read.length ? `No entry in ${days} days: ${plural(quiet, 'lane')}.` : ''].filter(Boolean).join(' ');
  }
  // PR waits (pack sd:7859): the totals row, then one row per repository with its own totals (sd:8084), a disclosure
  // closed by default as Today > Queue's repo rows are: open, it lists that repository's pull requests, slowest first.
  // On you lights caution on an open one; a wait not merged says so, never 0. A pull request not known to be the
  // operator's is named in the note, never counted.
  const merge = s => s == null ? 'not merged' : dur(Math.round(s));
  const median = g => g.merged ? `median ${merge(g.to_merge)}` : 'none merged';
  const I = n => html`<svg class="i" aria-hidden="true"><use href="#i-${n}"/></svg>`;
  let prOpen = null; // the one open repository
  function prWaits() {
    const P = DOC?.prs;
    if (!P || P.error) { put($('prs-body'), html`<tr><td colspan="5">${unknownBlock(P ? P.error : 'the Metrics document did not arrive')}</td></tr>`); $('prs-note').textContent = ''; return; }
    const unknown = P.unknown.length ? `▨ Not counted, not known whether yours: ${P.unknown.join('; ')}.` : '';
    if (!P.items) { put($('prs-body'), html`<tr><td colspan="5" class="why">No pull request of yours has a lane note yet.</td></tr>`); $('prs-note').textContent = unknown; return; }
    if (!P.repos.some(g => g.repo === prOpen)) prOpen = null;
    const total = html`<tr class="total"><td class="g g-ok">${G.ok}<span class="sr">total</span></td><td class="what">All ${plural(P.items, 'item')}</td><td class="num">${dur(P.on_you)}</td><td class="num">${dur(P.on_reviewers)}</td><td class="num hide-sm">${median(P)}</td></tr>`;
    const parent = g => { const on = prOpen === g.repo, st = g.prs.some(w => w.open && w.on === 'you') ? 'caution' : 'ok';
      return html`<tr class="repo" data-repo="${g.repo}" aria-expanded="${String(on)}"><td class="g g-${st}">${G[st]}<span class="sr">${st === 'caution' ? 'open on you' : 'normal'}</span></td>
        <td class="what"><button type="button" class="disc" data-open="${g.repo}" aria-expanded="${String(on)}" aria-label="${on ? 'Close' : 'Open'} ${g.repo}">${I('chevron-right')}</button><button type="button" class="pick" data-open="${g.repo}">${g.repo}</button><small>${plural(g.items, 'pull request')}, ${g.open} open</small></td><td class="num">${dur(g.on_you)}</td><td class="num">${dur(g.on_reviewers)}</td><td class="num hide-sm">${median(g)}</td></tr>`; };
    const child = w => { const st = w.open && w.on === 'you' ? 'caution' : 'ok';
      return html`<tr class="member"><td class="g g-${st}">${G[st]}<span class="sr">${w.open ? `open, on ${w.on}` : 'ended'}</span></td><td class="what"><a href="${w.url}" target="_blank" rel="noopener noreferrer">sd:${String(w.item)} ${w.title}</a></td><td class="num">${dur(w.on_you)}</td><td class="num">${dur(w.on_reviewers)}</td><td class="num hide-sm">${merge(w.to_merge)}</td></tr>`; };
    put($('prs-body'), html`${total}${P.repos.map(g => html`${parent(g)}${prOpen === g.repo ? g.prs.map(child) : ''}`)}`);
    $('prs-note').textContent = [`${plural(P.open, 'pull request')} open now; waiting in all: ${dur(P.on_you + P.on_reviewers)}.`, unknown].filter(Boolean).join(' ');
  }
  // pack sd:7208: each of the six months by bill. A month with no ledger row says so, never $0; a window bill is not a zero.
  function months() {
    const sm = DOC?.ranged?.spend;
    $('months-block').hidden = !sm;
    if (!sm) return;
    if (sm.error) { put($('months-head'), html``); put($('months-body'), html`<tr><td>${unknownBlock(sm.error)}</td></tr>`); $('months-note').textContent = ''; return; }
    // A run row may name no bill (a start session's): its dollars are their own column, shown when a month holds one.
    const loose = sm.months.some(m => m.unbilled != null), cols = sm.bills.length + 1 + (loose ? 1 : 0);
    put($('months-head'), html`<tr><th scope="col">Month</th><th scope="col" class="num">Total</th>${sm.bills.map(b => html`<th scope="col" class="num">${b.name}</th>`)}${loose ? html`<th scope="col" class="num">no bill</th>` : ''}</tr>`);
    put($('months-body'), html`${sm.months.map(m => row(`mo:${m.month}`, html`<td class="what"><button type="button">${m.month}</button>${m.month === DOC.usage?.month ? html`<small>to date</small>` : ''}</td>${m.rows
      ? html`<td class="num">${money(m.spent)}</td>${sm.bills.map(b => html`<td class="num">${windowed(b) && !m.bills[b.name] ? '▨ window' : money(m.bills[b.name])}</td>`)}${loose ? html`<td class="num">${money(m.unbilled)}</td>` : ''}`
      : html`<td class="why" colspan="${cols}">no rows</td>`}`))}`);
    const read = sm.months.filter(m => m.rows), empty = sm.months.length - read.length;
    $('months-note').textContent = `Total ${money(read.reduce((n, m) => n + m.spent, 0))} over ${span()}${empty ? `; ${plural(empty, 'month')} with no ledger row` : ''}. ▨ marks a bill that spends a vendor window, not a zero.`;
  }
  function draw() {
    lamps(); spend(); months(); trends(); tables(); age();
    put($('model'), unreadBlock('model'));
    ci();
    laneTimes();
    prWaits();
    const o = DOC?.observed;
    put($('observed'), !DOC ? html`` : o.error ? unknownBlock(o.error) : html`<p class="foot-note">Observed ${o.since} → ${o.until}: ${plural(o.recorded_requests, 'request')} recorded, ${plural(o.finished_review_attempts, 'finished review attempt')}, ${plural(o.recorded_deliveries, 'recorded delivery', 'recorded deliveries')}. <button class="linkbtn" type="button" data-ev="observed">Details</button></p>`);
  }

  // ---------- Details ----------
  function show(id, open) {
    const e = EV[id]; if (!e) return;
    selected = id;
    document.querySelectorAll('[data-ev]').forEach(el => { if (el.matches('.hbar')) el.setAttribute('aria-pressed', el.dataset.ev === id ? 'true' : 'false'); if (el.matches('tr')) el.setAttribute('aria-selected', el.dataset.ev === id ? 'true' : 'false'); });
    const C = window.shell.commands;
    C.select(id);
    const act = C.bar(id);
    put($('details'), html`<p class="kind">${e.s ? html`<span class="g-${e.s}" aria-hidden="true">${G[e.s]}</span> ` : ''}${e.kind}</p><h2>${e.title}</h2>
      <dl>${e.facts.map(([k, v]) => html`<dt>${k}</dt><dd>${v}</dd>`)}</dl>${e.note ? html`<p class="note">${e.note}</p>` : ''}
      ${e.open ? html`<p><a class="open-link" href="${e.open[0]}">${e.open[1]}</a></p>` : ''}
      ${String(act) ? html`<h3>Act</h3>${SAID && SAID.id === id ? html`<p class="note" data-said="${SAID.kind}">${SAID.kind === 'unknown' ? html`<span class="g-unknown" aria-hidden="true">▨</span> ` : ''}${SAID.text}</p>` : ''}${act}` : ''}
      <h3>Same reading from the CLI</h3><div class="cli"><code>${e.cli}</code></div>`);
    window.shell.suggest([`Why is ${e.title} where it is?`, 'What changed against last week?', 'Which of these needs me today?']);
    if (open) window.shell.openPane('tab-details');
  }

  // ---------- Writes ----------
  // One write: post, then read again. Landed: reread, and a toast with Undo when the command has an inverse. A 400 or 409
  // wrote nothing (server.action_route; queue_screen.py has the same contract): load again and say so. Anything else (a
  // 503, a refused session, no answer) may follow a landed write: load again and say the outcome is not known.
  const said = (id, kind, text) => { SAID = { id, kind, text }; if (selected === id) show(id, false); };
  function write(o, what, path, body, inverse) {
    const S = window.shell;
    S.post(path, body).then(answer => {
      reader.reread().then(() => said(o.id, 'landed', `${what}.`));
      S.toast(`${what} · ${o.label}`, inverse ? () => inverse(answer).then(() => reader.reread()).then(() => { said(o.id, 'landed', `${what} undone.`); S.toast(`${what} undone · ${o.label}`); },
        e => failed(o, e, `${what} not undone`, `the undo landed on ${o.label}`)) : undefined);
    }, e => failed(o, e, 'not changed', `${o.label} changed`));
    return null;
  }
  function failed(o, e, refusal, landed) {
    const refused = e.status === 400 || e.status === 409, why = e.message.replace(/\.$/, '');
    const text = refused ? `${o.label} ${refusal}: ${why}. Nothing was written.`
      : `Not known whether ${landed}: ${why}. Metrics was read again; check it before you act again.`;
    reader.load().then(() => said(o.id, refused ? 'refused' : 'unknown', text));
    window.shell.toast(text);
    return null;
  }
  // The cap field: none clears; a decimal goes as a number; anything else goes as typed, and the library refuses it (400).
  const capOf = typed => /^none$/i.test(typed) ? null : /^[-+]?(\d+\.?\d*|\.\d+)([eE][-+]?\d+)?$/.test(typed) && Number.isFinite(Number(typed)) ? Number(typed) : typed;
  function cap(o, v) {
    const path = `/api/bills/${encodeURIComponent(o.bill)}/cap`, before = o.control.cap_usd_month;
    const to = o.control.write === 'clear' ? null : capOf((v && v.cap) || '');
    // A legacy clear has no inverse: set_cap refuses a cap on a bill a start entry is billed to.
    return write(o, to == null ? 'Cap cleared' : `Cap set to ${typeof to === 'number' ? money(to) : to}`, path, { revision: o.revision, cap_usd_month: to },
      o.control.write === 'set' ? answer => window.shell.post(path, { revision: answer.revision, cap_usd_month: before }) : null);
  }
  // Configure's fields: blank keeps the current value, which the placeholder shows.
  const names = typed => typed.split(',').map(n => n.trim()).filter(Boolean);
  function configureFields() {
    const c = DOC?.controls; if (!c || c.error) return [];
    return [{ name: 'enabled', label: 'Enabled providers, comma-separated', placeholder: c.providers.filter(p => p.enabled).map(p => p.name).join(', '), help: `Blank keeps them. Known: ${c.providers.map(p => p.name).join(', ')}.` },
      ...['author', 'reviewer'].map(role => ({ name: role, label: `Automatic ${role} order, first choice first`, placeholder: c.orders[role].join(', '), help: 'Blank keeps it.' }))];
  }
  function configure(o, v = {}) {
    const c = DOC.controls, known = c.providers.map(p => p.name);
    const before = { enabled: Object.fromEntries(c.providers.map(p => [p.name, p.enabled])), orders: c.orders };
    const on = v.enabled ? names(v.enabled) : null, unknown = on ? on.filter(n => !known.includes(n)) : [];
    if (unknown.length) { const text = `${o.label} not changed: no provider ${unknown.join(', ')}; the registry names ${known.join(', ')}.`; said(o.id, 'refused', text); window.shell.toast(text); return null; }
    const enabled = on ? Object.fromEntries(known.map(n => [n, on.includes(n)])) : before.enabled;
    const orders = { author: v.author ? names(v.author) : c.orders.author, reviewer: v.reviewer ? names(v.reviewer) : c.orders.reviewer };
    return write(o, 'Providers configured', '/api/providers/configure', { revision: o.revision, enabled, orders },
      answer => window.shell.post('/api/providers/configure', { revision: answer.revision, ...before }));
  }

  function ranges() {
    put($('range'), html`${RANGES.map(r => html`<button type="button" data-range="${r.id}" aria-pressed="${String(r.id === range)}"${r.off ? html` aria-disabled="true" title="${r.off}"` : ''}>${r.id}</button>`)}`);
  }

  // ---------- Reader (read.js) ----------
  let reader = null;
  const load = () => reader.load();
  // Read again (sd:5638): the shell's head button reads the Metrics readings again.
  window.PAGE_AGAIN = { title: 'Read the Metrics readings again', run: load };
  const spec = {
    source: () => `/api/metrics${query().length ? `?${query().map(([k, v]) => `${k}=${v}`).join('&')}` : ''}`, what: 'the Metrics readings',
    adopt: doc => {
      DOC = doc; evidence(); CELLS = readCells(); attention();
      document.body.dataset.observed = doc.read;
      const parts = [...['usage', 'trend', 'numbers', 'scorecard', 'age', 'observed', 'controls', 'ci', 'lanes', 'prs'].map(k => [k, doc[k]]),
        ...(doc.ranged ? ['spend', 'scorecard', 'ci'].map(k => [`6mo ${k}`, doc.ranged[k]]) : [])];
      const failed = parts.filter(([, p]) => p.error || p.stale);
      FIRST = CELLS.find(c => c.state === 'warning' || c.state === 'caution')?.ev || Object.keys(EV).find(k => k.startsWith('win:')) || null;
      put($('subhead'), html`Spend against budget, the week, providers, CI and the month's usage · read <time class="rel" datetime="${doc.read}"></time>`);
      return { objects: Object.entries(EV).map(([id, e]) => ({ ...e, id, type: e.type || 'reading', label: e.label || e.title })),
        state: failed.length ? { kind: 'partial', text: `${failed.map(([k, p]) => `${k}: ${p.error || `stale since ${hhmm(p.observed)}: ${p.stale}`}`).join(' · ')}. The other readings are current.`, source: spec.source() } : null };
    },
    clear: () => {
      DOC = null; EV = {}; CELLS = []; FIRST = null;
      delete document.body.dataset.observed;
      put($('subhead'), html`The Metrics readings could not be read.`);
      window.shell.attention({ state: 'unknown', n: 0, what: 'Metrics not read' });
    },
    draw,
    current: () => selected,
    first: () => FIRST,
    select: (id, opened) => show(id, opened),
    unselect: () => { selected = null; put($('details'), html`<p class="why">${DOC ? 'Select a cell, bar or row to see its evidence.' : 'Nothing is selected: the Metrics readings could not be read.'}</p>`); },
  };

  window.PAGE_COMMANDS = [
    { label: 'Read Metrics again', icon: 'rotate-ccw', run: () => load() },
  ];
  window.PAGE_LIST = { rows: () => document.querySelectorAll('.tbl tbody tr[data-id]'), current: () => selected, select: id => show(id, false) };

  document.addEventListener('DOMContentLoaded', () => {
    const S = window.shell, C = S.commands;
    reader = S.read(spec);
    // ---------- Commands (products/system/commands.md) ----------
    // Metrics' two write homes (sd:3387). Each posts its route with the revision this page
    // read, and settles itself: write() toasts what landed with its Undo, and says in Details when the outcome is unknown.
    const offWhy = o => !o.control ? (DOC?.controls?.error ? `the provider registry was not read: ${DOC.controls.error}` : 'not in the provider registry')
      : o.revision ? true : 'the page holds no registry revision';
    C.register(
      { id: 'providers.configure', on: 'provider', label: 'Configure', does: 'Saves the enabled providers and role orders; Undo restores the old ones.', key: 'f', icon: 'settings', risk: 'undo', executes: true, primary: () => true,
        when: offWhy, fields: configureFields, consequence: () => 'Saves the enabled choices and both automatic role orders together, as the registry reads them now.',
        cli: () => 'sd providers configure --file CONFIG.json', run: (o, v) => configure(o, v) },
      { id: 'bill.cap', on: 'bill', label: 'Cap', does: o => o.control?.write === 'clear' ? 'Clears the legacy cap on this bill; this cannot be undone.' : 'Sets a monthly cap on this bill; Undo restores the old cap.', key: 'p', risk: 'undo', executes: true, primary: () => true,
        when: o => { const w = offWhy(o); return w !== true ? w : o.control.write ? true : o.control.why; },
        fields: o => o.control?.write === 'set' ? [{ name: 'cap', label: 'New cap, USD a month', required: true, placeholder: o.control.cap_usd_month == null ? 'none' : String(o.control.cap_usd_month),
          help: `Now ${o.control.cap_usd_month == null ? 'no cap' : money(o.control.cap_usd_month)}. Type none to clear it.` }] : [],
        askFirst: true, consequence: o => o.control?.write === 'clear' ? `Clears the legacy cap on ${o.bill}: ${o.control.why}.` : `The ledger refuses a reservation past the cap on ${o.bill} this month.`,
        cli: o => `no CLI: sd has no verb for bill caps (bill.cap_usd_month, ${o.bill}${o.cap == null ? '' : `, now ${money(o.cap)}`})`,
        run: (o, v) => cap(o, v) },
    );
    document.addEventListener('click', e => {
      if (e.target.closest?.('.rowact')) return;
      const repo = e.target.closest?.('#prs-body tr.repo'); // sd:8084: as Today > Queue's repo rows
      if (repo && !e.target.closest('a')) { prOpen = prOpen === repo.dataset.repo ? null : repo.dataset.repo; prWaits(); return; }
      const el = e.target.closest?.('[data-ev]'); if (!el) return;
      if (el.dataset.view) S.view.show(el.dataset.view);
      if (el.dataset.ev) show(el.dataset.ev, true);
    });
    // A chart point is the page's own control: its keys stop at the trends block, so the shell's Enter (shell:open) does not also run.
    $('trends').addEventListener('keydown', e => {
      const el = e.target.closest?.('svg [data-ev]'); if (el && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); e.stopPropagation(); show(el.dataset.ev, true); }
    });
    $('range').addEventListener('click', e => {
      const c = e.target.closest('button[data-range]'); if (!c) return;
      if (c.getAttribute('aria-disabled') === 'true') { S.toast(c.title); return; }
      if (c.dataset.range === range) return;
      range = c.dataset.range; S.url(query()); ranges(); load();
    });
    // The month picker (pack sd:7524): a month reads that month; a cleared one reads this one again.
    $('month').addEventListener('change', e => {
      const v = /^[0-9]{4}-(0[1-9]|1[0-2])$/.test(e.target.value) ? e.target.value : '';
      if (v === month) return;
      month = v; S.url(query()); load();
    });
    ranges();
    document.addEventListener('shell:open', e => { if (EV[e.detail]) show(e.detail, true); });
    load();
  });
})();
window.PAGE_KEYS = [['↵', 'On a trend point: show it in Details']];
