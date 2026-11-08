// Home's Map view (pack sd:8053): the lot's assets over the county aerial, one SVG as HOA's map.
// Data: GET /api/home/map (home_map.document): the square (EPSG:2232 feet), the aerial's address, the assets with their care.
// The SVG's viewBox is the square with y flipped, so an asset's feet are its coordinates. Add: press Add, pick a kind,
// click the map (a line kind takes a click per point, then Finish line), fill the short form, Save. Drag a point to move it.
// A row or a mark opens its form: Save, Delete, and Add care task, a recurring sd item naming the asset (POST /api/home/care).
// A plant or a tree whose care is overdue carries the warning ring.
(() => {
  const { html, put } = window.markup;
  const $ = id => document.getElementById(id);
  const S = { doc: null, adding: false, kind: '', points: [], sel: null, drag: null };
  const label = k => k.replace(/-/g, ' ');
  const isLine = k => (S.doc?.lines || []).includes(k);
  const plant = k => k === 'tree' || k === 'plant';

  // The square: the address's, else the assets' extent, else a 400 ft square at 0.
  function box() {
    if (S.doc.bbox) return S.doc.bbox;
    const pts = S.doc.assets.flatMap(a => !a.geometry ? [] : a.geometry.type === 'Point' ? [a.geometry.coordinates] : a.geometry.coordinates);
    if (!pts.length) return [0, 0, 400, 400];
    const xs = pts.map(p => p[0]), ys = pts.map(p => p[1]), pad = 40;
    return [Math.min(...xs) - pad, Math.min(...ys) - pad, Math.max(...xs) + pad, Math.max(...ys) + pad];
  }
  const xy = p => `${p[0].toFixed(2)},${(-p[1]).toFixed(2)}`;

  function mark(a, r) {
    const cls = `asset k-${a.kind}${a.care.overdue ? ' overdue' : ''}${S.sel === a.id ? ' sel' : ''}`;
    const title = html`<title>${a.name} · ${label(a.kind)}${a.care.overdue ? ' · care overdue' : ''}</title>`;
    if (a.geometry.type === 'LineString') {
      const d = a.geometry.coordinates.map(xy).join(' ');
      return html`<g class="${cls}" data-id="${String(a.id)}">${title}<polyline class="run" points="${d}"/><polyline class="hit" points="${d}"/></g>`;
    }
    const [x, y] = a.geometry.coordinates;
    return html`<g class="${cls}" data-id="${String(a.id)}" data-drag="1" transform="translate(${x.toFixed(2)} ${(-y).toFixed(2)})">${title}${a.care.overdue ? html`<circle class="ring" r="${String(r * 1.8)}"/>` : ''}<circle class="body" r="${String(r)}"/></g>`;
  }

  function draw() {
    const d = S.doc, [x0, y0, x1, y1] = box(), w = x1 - x0, h = y1 - y0, r = w / 120;
    const placed = d.assets.filter(a => a.geometry), pending = S.points.length ? html`<polyline class="pending" points="${S.points.map(xy).join(' ')}"/>` : '';
    put($('map-note'), d.reason ? html`<p class="note">${d.reason}</p>` : html``);
    put($('map-svg'), html`<svg class="map${S.adding ? ' adding' : ''}" id="map-canvas" viewBox="${x0} ${-y1} ${w} ${h}" preserveAspectRatio="xMidYMid meet" role="img" aria-label="Property map: ${String(placed.length)} assets">
      ${d.aerial ? html`<image href="${d.aerial}" x="${x0}" y="${-y1}" width="${w}" height="${h}" preserveAspectRatio="none"/>` : html`<rect class="ground" x="${x0}" y="${-y1}" width="${w}" height="${h}"/>`}
      ${placed.map(a => mark(a, r))}${pending}</svg>`);
    put($('map-tools'), html`<button class="btn sm" type="button" id="map-add" aria-pressed="${String(S.adding)}">${S.adding ? 'Stop adding' : 'Add'}</button>
      ${S.adding ? html`<label class="sr" for="map-kind">Kind</label><select id="map-kind">${d.kinds.map(k => html`<option value="${k}"${k === S.kind ? html` selected` : ''}>${label(k)}</option>`)}</select>
      <span class="why">${isLine(S.kind) ? `Click each point of the ${label(S.kind)}, then Finish line.` : `Click the map where the ${label(S.kind)} is.`}</span>
      ${isLine(S.kind) ? html`<button class="btn sm" type="button" id="map-finish"${S.points.length < 2 ? html` disabled` : ''}>Finish line</button>` : ''}` : ''}`);
    put($('map-rows'), html`<table class="ledger place-rows"><thead><tr><th scope="col">Asset</th><th scope="col">Kind</th><th scope="col">Care</th></tr></thead><tbody>
      ${d.assets.length ? d.assets.map(a => html`<tr data-asset="${String(a.id)}" tabindex="0" aria-selected="${String(S.sel === a.id)}"><td>${a.name}</td><td>${label(a.kind)}</td>
        <td>${a.care.overdue ? html`<span class="st warning">■ overdue</span>` : a.care.items.length ? `next ${a.care.items[0].due || 'undated'}` : '—'}</td></tr>`)
        : html`<tr class="empty"><td colspan="3">No assets yet: press Add, pick a kind and click the map.</td></tr>`}</tbody></table>`);
    form();
  }

  // The form: a new asset at its geometry, or the selected one. Plants and trees offer their fields.
  function form(geometry) {
    const a = S.doc.assets.find(x => x.id === S.sel), kind = a ? a.kind : S.kind;
    if (!a && !geometry) { put($('map-form'), html``); return; }
    const f = a ? a.fields : {}, more = plant(kind) ? S.doc.plant_fields : [];
    put($('map-form'), html`<form class="asset-form" id="asset-form" aria-label="${a ? `Edit ${a.name}` : `New ${label(kind)}`}">
      <h3>${a ? a.name : `New ${label(kind)}`}</h3>
      <label>Name <input name="name" required maxlength="200" value="${a ? a.name : ''}"></label>
      ${more.map(k => html`<label>${k} <input name="f-${k}" value="${f[k] == null ? '' : String(f[k])}"${k === 'planted' ? html` type="date"` : ''}></label>`)}
      <label>Photo address <input name="photo" type="url" value="${a?.photo || ''}"></label>
      <div class="ed-act"><button class="btn" type="submit">Save</button>
      ${a ? html`<button class="btn quiet" type="button" id="asset-delete">Delete</button>` : html`<button class="btn quiet" type="button" id="asset-cancel">Cancel</button>`}</div>
      </form>${a ? html`<form class="asset-form" id="care-form" aria-label="Add care task"><h3>Care</h3>
      ${a.care.items.map(i => html`<p><a href="${window.shell.itemHref(i.id)}">${i.title}</a> · due ${i.due || '—'}</p>`)}
      <label>Task <input name="title" required value="Water the ${a.name}"></label>
      <label>First due <input name="due" type="date" required></label>
      <label>Repeats <select name="recurrence">${S.doc.recur.map(r => html`<option>${r}</option>`)}</select></label>
      <div class="ed-act"><button class="btn" type="submit">Add care task</button></div></form>` : ''}`);
    $('asset-form').dataset.geometry = geometry ? JSON.stringify(geometry) : '';
  }

  const toast = m => window.shell?.toast ? window.shell.toast(m) : null;
  async function load() {
    try { S.doc = await window.shell.getJSON('/api/home/map'); S.kind = S.kind || S.doc.kinds[0]; draw(); }
    catch (e) { put($('map-note'), html`<p class="state-unknown"><b>▨ Not read.</b> ${e.message}</p>`); }
  }
  const send = (path, body, done) => window.shell.post(path, body).then(out => { toast(done); return load().then(() => out); }, e => toast(`Not saved: ${e.message}`));

  // A point in feet from a pointer event on the SVG.
  function feet(e) {
    const svg = $('map-canvas'), m = svg.getScreenCTM().inverse(), p = new DOMPoint(e.clientX, e.clientY).matrixTransform(m);
    return [p.x, -p.y];
  }

  document.addEventListener('click', e => {
    const t = e.target;
    if (t.closest?.('#map-add')) { S.adding = !S.adding; S.points = []; S.sel = null; draw(); return; }
    if (t.closest?.('#map-finish')) { const g = { type: 'LineString', coordinates: S.points }; S.points = []; draw(); form(g); return; }
    if (t.closest?.('#asset-cancel')) { S.points = []; form(); return; }
    if (t.closest?.('#asset-delete')) {
      const a = S.doc.assets.find(x => x.id === S.sel);
      window.shell.confirm({ title: `Delete ${a.name}?`, body: 'Its care tasks stay open.', ok: 'Delete' }).then(yes => {
        if (yes) { S.sel = null; send('/api/home/asset/delete', { id: a.id }, `${a.name} deleted`); }
      });
      return;
    }
    const row = t.closest?.('[data-asset]');
    if (row) { S.sel = Number(row.dataset.asset); draw(); return; }
    const g = t.closest?.('#map-canvas .asset');
    if (g && !S.adding) { S.sel = Number(g.dataset.id); draw(); return; }
    if (S.adding && t.closest?.('#map-canvas')) {
      const p = feet(e);
      if (isLine(S.kind)) { S.points.push(p); draw(); } else form({ type: 'Point', coordinates: p });
    }
  });
  document.addEventListener('change', e => { if (e.target.id === 'map-kind') { S.kind = e.target.value; S.points = []; draw(); } });
  document.addEventListener('keydown', e => { const row = e.target.closest?.('[data-asset]'); if (row && e.key === 'Enter') { S.sel = Number(row.dataset.asset); draw(); } });
  document.addEventListener('submit', e => {
    if (e.target.id === 'asset-form') {
      e.preventDefault();
      const fd = new FormData(e.target), a = S.doc.assets.find(x => x.id === S.sel), kind = a ? a.kind : S.kind;
      const fields = { ...(a ? a.fields : {}) };
      for (const [k, v] of fd.entries()) if (k.startsWith('f-')) { if (v) fields[k.slice(2)] = v; else delete fields[k.slice(2)]; }
      const geometry = e.target.dataset.geometry ? JSON.parse(e.target.dataset.geometry) : a ? a.geometry : null;
      const body = { kind, name: fd.get('name'), geometry, fields, photo: fd.get('photo') || null, ...(a ? { id: a.id } : {}) };
      send('/api/home/asset', body, `${body.name} saved`).then(out => { if (out?.id) { S.sel = out.id; S.adding = false; draw(); } });
    } else if (e.target.id === 'care-form') {
      e.preventDefault();
      const fd = new FormData(e.target);
      send('/api/home/care', { asset: S.sel, title: fd.get('title'), due: fd.get('due'), recurrence: fd.get('recurrence') }, 'Care task filed');
    }
  });

  // Drag a point to move it; the drop posts the new point.
  document.addEventListener('pointerdown', e => {
    const g = e.target.closest?.('#map-canvas .asset[data-drag]');
    if (!g || S.adding) return;
    S.drag = { id: Number(g.dataset.id), g, moved: false };
  });
  document.addEventListener('pointermove', e => {
    if (!S.drag) return;
    const [x, y] = feet(e);
    S.drag.moved = true; S.drag.at = [x, y];
    S.drag.g.setAttribute('transform', `translate(${x.toFixed(2)} ${(-y).toFixed(2)})`);
  });
  document.addEventListener('pointerup', () => {
    const d = S.drag; S.drag = null;
    if (!d || !d.moved) return;
    const a = S.doc.assets.find(x => x.id === d.id);
    send('/api/home/asset', { id: a.id, kind: a.kind, name: a.name, geometry: { type: 'Point', coordinates: d.at }, fields: a.fields, photo: a.photo }, `${a.name} moved`);
  });

  document.addEventListener('shell:view', e => { if (e.detail.id === 'map') load(); });
  window.homeMap = { load, state: S, draw, form };
})();
