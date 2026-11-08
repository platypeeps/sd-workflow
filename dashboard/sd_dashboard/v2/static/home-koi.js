// Home's Koi view (pack sd:8054): the fish, the pond's care and water tests, and its water changes from Home Assistant.
// Data: GET /api/home/koi (home_koi.document). A fish is a koi asset and the pond a pond asset (the property map's table):
// Add fish and a fish's form post /api/home/asset (a measurement appends to its log); a water test posts
// /api/home/koi/test. Care tasks
// post /api/home/care on the pond. When Home Assistant holds a change the pond's log lacks, the view posts
// /api/home/koi/water once, which logs it, and reads again; it also posts when the pond's last synced window is
// stale (water.sync_due). A total whose window a gap reaches reads "incomplete since <date>" (water.incomplete_since).
// A saved fish's form shows its Drive folder (pack sd:8147) through home-attach.js, the Map's: a koi asset's folder.
(() => {
  const { html, put } = window.markup;
  const $ = id => document.getElementById(id);
  const S = { doc: null, sel: null, synced: false };
  const num = v => v === '' || v == null ? null : Number(v);
  const last = f => (f.fields.log || []).slice(-1)[0] || {};
  const pct = n => `${Math.round(n * 10) / 10} %`;
  const gap = d => d ? html` <small class="st warning">incomplete since ${d}</small>` : '';

  function draw() {
    const d = S.doc, w = d.water, pond = d.pond;
    put($('koi-note'), html`${d.reason ? html`<p class="note">${d.reason}</p>` : ''}${d.pond_reason ? html`<p class="note">${d.pond_reason}</p>` : ''}`);
    put($('koi-water'), html`<dl class="figures">
      <div><dt>Since the last change</dt><dd>${w.days_since == null ? '—' : `${w.days_since} d`}</dd></div>
      <div><dt>Replaced, last 7 days</dt><dd>${pct(w.week_percent)}${w.week_gallons ? ` · ${Math.round(w.week_gallons)} gal` : ''}${gap(w.incomplete_since?.week)}</dd></div>
      <div><dt>Weekly average, 28 days</dt><dd>${pct(w.average_week_percent)}${gap(w.incomplete_since?.month)}</dd></div></dl>
      <table class="ledger"><thead><tr><th scope="col">Changed</th><th scope="col">Percent</th><th scope="col">Script</th></tr></thead><tbody>
      ${w.log.length ? w.log.slice(0, 12).map(r => html`<tr><td><time datetime="${r.at}">${r.at.slice(0, 16).replace('T', ' ')}</time></td><td>${pct(r.percent)}</td><td>${r.entity}</td></tr>`)
        : html`<tr class="empty"><td colspan="3">No water change logged yet.</td></tr>`}</tbody></table>`);
    put($('koi-fish'), html`<div class="tools"><button class="btn sm" type="button" id="koi-add"${pond ? '' : html` disabled`}>Add fish</button></div>
      <table class="ledger place-rows"><thead><tr><th scope="col">Name</th><th scope="col">Variety</th><th scope="col">Size</th><th scope="col">Weight</th><th scope="col">Acquired</th><th scope="col">Health</th></tr></thead><tbody>
      ${d.fish.length ? d.fish.map(f => html`<tr data-fish="${String(f.id)}" tabindex="0" aria-selected="${String(S.sel === f.id)}"><td>${f.name}</td><td>${f.fields.variety || '—'}</td>
        <td>${last(f).size_in ? `${last(f).size_in} in` : '—'}</td><td>${last(f).weight_lb ? `${last(f).weight_lb} lb` : '—'}</td><td>${f.fields.acquired || '—'}</td><td>${f.fields.health || '—'}</td></tr>`)
        : html`<tr class="empty"><td colspan="6">No fish yet.</td></tr>`}</tbody></table>`);
    fishForm();
    put($('koi-care'), pond ? html`
      ${pond.care.items.map(i => html`<p${pond.care.overdue && i.due && i.due < new Date().toISOString().slice(0, 10) ? html` class="st warning"` : ''}><a href="${window.shell.itemHref(i.id)}">${i.title}</a> · due ${i.due || '—'}</p>`)}
      <form class="asset-form" id="koi-care-form" aria-label="Add pond care task"><label>Task <select name="preset">${d.care.map((c, n) => html`<option value="${String(n)}">${c.title}</option>`)}</select></label>
        <label>First due <input name="due" type="date" required></label><div class="ed-act"><button class="btn" type="submit">Add care task</button></div></form>
      <form class="asset-form" id="koi-test-form" aria-label="Log a water test"><h3>Water test</h3>${d.tests.map(t => html`<label>${t} <input name="${t}" inputmode="decimal"></label>`)}
        <div class="ed-act"><button class="btn" type="submit">Log test</button></div></form>
      <table class="ledger"><thead><tr><th scope="col">Tested</th>${d.tests.map(t => html`<th scope="col">${t}</th>`)}</tr></thead><tbody>
      ${(pond.fields.tests || []).slice(-8).reverse().map(r => html`<tr><td>${r.date}</td>${d.tests.map(t => html`<td>${r[t] == null ? '—' : String(r[t])}</td>`)}</tr>`)}</tbody></table>` : html``);
  }

  function fishForm() {
    const f = S.doc.fish.find(x => x.id === S.sel);
    if (!f && S.sel !== 'new') { put($('koi-form'), html``); return; }
    const v = f ? f.fields : {};
    put($('koi-form'), html`<form class="asset-form" id="koi-fish-form" aria-label="${f ? `Edit ${f.name}` : 'New fish'}"><h3>${f ? f.name : 'New fish'}</h3>
      <label>Name <input name="name" required maxlength="200" value="${f ? f.name : ''}"></label>
      <label>Variety <input name="variety" value="${v.variety || ''}"></label>
      <label>Acquired <input name="acquired" type="date" value="${v.acquired || ''}"></label>
      <label>Health notes <textarea name="health" rows="2">${v.health || ''}</textarea></label>
      <label>Photo address <input name="photo" type="url" value="${f?.photo || ''}"></label>
      <fieldset><legend>Measurement</legend><label>Date <input name="m-date" type="date"></label><label>Size (in) <input name="m-size" inputmode="decimal"></label><label>Weight (lb) <input name="m-weight" inputmode="decimal"></label></fieldset>
      <div class="ed-act"><button class="btn" type="submit">Save</button>${f ? html`<button class="btn quiet" type="button" id="koi-delete">Delete</button>` : ''}</div></form>
      ${f ? html`<section class="asset-form" aria-label="Attachments"><h3>Attachments</h3><div id="koi-attach" class="attach"></div></section>` : ''}`);
    if (f) window.homeAttach.show('koi-attach', f.id, () => S.sel === f.id);
  }

  const toast = m => window.shell?.toast ? window.shell.toast(m) : null;
  async function load() {
    try {
      S.doc = await window.shell.getJSON('/api/home/koi'); draw();
      // Home Assistant keeps ten days: log what it holds that the pond's log lacks, and the window it covered, once per view.
      if ((S.doc.water.unlogged || S.doc.water.sync_due) && S.doc.pond && !S.synced) { S.synced = true; await window.shell.post('/api/home/koi/water', {}).then(load, e => toast(`Water changes not logged: ${e.message}`)); }
    } catch (e) { put($('koi-note'), html`<p class="state-unknown"><b>▨ Not read.</b> ${e.message}</p>`); }
  }
  const send = (path, body, done) => window.shell.post(path, body).then(out => { toast(done); return load().then(() => out); }, e => toast(`Not saved: ${e.message}`));
  const pond = () => S.doc.pond;

  document.addEventListener('click', e => {
    const t = e.target;
    if (t.closest?.('#koi-add')) { S.sel = 'new'; fishForm(); return; }
    if (t.closest?.('#koi-delete')) {
      const f = S.doc.fish.find(x => x.id === S.sel);
      window.shell.confirm({ title: `Delete ${f.name}?`, body: 'Its measurements go with it.', ok: 'Delete' })
        .then(yes => { if (yes) { S.sel = null; send('/api/home/asset/delete', { id: f.id }, `${f.name} deleted`); } });
      return;
    }
    const row = t.closest?.('[data-fish]');
    if (row) { S.sel = Number(row.dataset.fish); draw(); }
  });
  document.addEventListener('submit', e => {
    if (e.target.closest?.('#koi-attach') && e.target.dataset.attach) {
      e.preventDefault(); const fish = S.sel; window.homeAttach.send(e.target, 'koi-attach', fish, () => S.sel === fish); return;
    }
    const id = e.target.id;
    if (!['koi-fish-form', 'koi-care-form', 'koi-test-form'].includes(id)) return;
    e.preventDefault();
    const fd = new FormData(e.target);
    if (id === 'koi-fish-form') {
      const f = S.doc.fish.find(x => x.id === S.sel), fields = { ...(f ? f.fields : {}) };
      for (const k of ['variety', 'acquired', 'health']) { if (fd.get(k)) fields[k] = fd.get(k); else delete fields[k]; }
      if (fd.get('m-date') && (fd.get('m-size') || fd.get('m-weight'))) {
        fields.log = [...(fields.log || []), { date: fd.get('m-date'), size_in: num(fd.get('m-size')), weight_lb: num(fd.get('m-weight')) }];
      }
      const body = { kind: 'koi', name: fd.get('name'), geometry: null, fields, photo: fd.get('photo') || null, ...(f ? { id: f.id } : {}) };
      send('/api/home/asset', body, `${body.name} saved`).then(out => { if (out?.id) { S.sel = out.id; draw(); } });
    } else if (id === 'koi-care-form') {
      const c = S.doc.care[Number(fd.get('preset'))];
      send('/api/home/care', { asset: pond().id, title: c.title, due: fd.get('due'), recurrence: c.recurrence }, 'Care task filed');
    } else {
      // Only the reading: the server appends it to the pond's current row, so a water change logged meanwhile stays.
      const reading = {};
      S.doc.tests.forEach(t => { if (fd.get(t) !== '' && fd.get(t) != null) reading[t] = Number(fd.get(t)); });
      send('/api/home/koi/test', reading, 'Water test logged');
    }
  });
  document.addEventListener('shell:view', e => { if (e.detail.id === 'koi') load(); });
  window.homeKoi = { load, state: S };
})();
