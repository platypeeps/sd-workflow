// Home's Map view (pack sd:8053): the lot's assets over the county aerial, one SVG as HOA's map.
// Data: GET /api/home/map (home_map.document): the box (EPSG:2232 feet, 1,000 by 400 ft), the aerial's address, the assets with their care.
// The SVG's viewBox is the box with y flipped, so an asset's feet are its coordinates. Add: press Add, pick a kind,
// click the map (a line kind takes a click per point, then Finish line), fill the short form, Save. Drag a point to move it.
// A row or a mark opens its form: Save, Delete, and Add care task, a recurring sd item naming the asset (POST /api/home/care).
// A plant or a tree whose care is overdue carries the warning ring.
// The tiles (sd:8085): one per kind group with its count; each toggles the group's marks and rows (all on by default,
// ?mapoff=<group,...> keeps the ones off). A group with no assets is dim; a kind in no group is Other's.
// Attachments (sd:8112): a selected asset's Drive folder, <group>/<name> under home.drive_folder (hoa_attach.HOME), as
// HOA's: pictures as thumbnails, Upload and Reference (a .url file) write there on the operator's press only.
(() => {
  const { html, put } = window.markup;
  const $ = id => document.getElementById(id);
  const S = { doc: null, adding: false, kind: '', points: [], sel: null, drag: null };
  const label = k => k.replace(/-/g, ' ');
  const isLine = k => (S.doc?.lines || []).includes(k);
  const plant = k => k === 'tree' || k === 'plant';
  const GROUPS = [['gas', 'Gas', ['gas-line', 'gas-shutoff']], ['water', 'Water', ['water-line', 'water-shutoff']],
    ['sewer', 'Sewer', ['sewer-line', 'sewer-shutoff']], ['electric', 'Electric', ['electric-line', 'electric-shutoff']],
    ['comms', 'Comms', ['comms-line', 'comms-shutoff']],
    ['irrigation', 'Irrigation', ['irrigation-line', 'irrigation-valve', 'irrigation-zone', 'backflow']],
    ['energy', 'Energy', ['solar', 'powerwall', 'inverter', 'ev-charger', 'hvac']], ['plants', 'Plants', ['tree', 'plant']],
    ['other', 'Other', ['light', 'drainage', 'fence', 'easement', 'pond']]];
  const group = k => (GROUPS.find(g => g[2].includes(k)) || GROUPS[GROUPS.length - 1])[0];
  const off = new Set((new URLSearchParams(location.search).get('mapoff') || '').split(',').filter(g => GROUPS.some(x => x[0] === g)));
  const shown = a => !off.has(group(a.kind));

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
    const placed = d.assets.filter(a => a.geometry && shown(a)), rows = d.assets.filter(shown), pending = S.points.length ? html`<polyline class="pending" points="${S.points.map(xy).join(' ')}"/>` : '';
    put($('map-note'), d.reason ? html`<p class="note">${d.reason}</p>` : html``);
    put($('map-svg'), html`<svg class="map${S.adding ? ' adding' : ''}" id="map-canvas" viewBox="${x0} ${-y1} ${w} ${h}" preserveAspectRatio="xMidYMid meet" role="img" aria-label="Property map: ${String(placed.length)} assets">
      ${d.aerial ? html`<image href="${d.aerial}" x="${x0}" y="${-y1}" width="${w}" height="${h}" preserveAspectRatio="none"/>` : html`<rect class="ground" x="${x0}" y="${-y1}" width="${w}" height="${h}"/>`}
      ${placed.map(a => mark(a, r))}${pending}</svg>`);
    const n = g => d.assets.filter(a => group(a.kind) === g).length;
    put($('map-filters'), window.shell.list.fseg('group', 'Kinds shown', GROUPS.map(([g, word, kinds]) => ({ v: g, pressed: !off.has(g), off: !n(g),
      title: `${word}: ${kinds.map(label).join(', ')} · ${n(g)}`, label: html`${word} <span class="n">${String(n(g))}</span>` }))));
    put($('map-tools'), html`<button class="btn sm" type="button" id="map-add" aria-pressed="${String(S.adding)}">${S.adding ? 'Stop adding' : 'Add'}</button>
      ${S.adding ? html`<label class="sr" for="map-kind">Kind</label><select id="map-kind">${d.kinds.map(k => html`<option value="${k}"${k === S.kind ? html` selected` : ''}>${label(k)}</option>`)}</select>
      <span class="why">${isLine(S.kind) ? `Click each point of the ${label(S.kind)}, then Finish line.` : `Click the map where the ${label(S.kind)} is.`}</span>
      ${isLine(S.kind) ? html`<button class="btn sm" type="button" id="map-finish"${S.points.length < 2 ? html` disabled` : ''}>Finish line</button>` : ''}` : ''}`);
    put($('map-rows'), html`<table class="ledger place-rows"><thead><tr><th scope="col">Asset</th><th scope="col">Kind</th><th scope="col">Care</th></tr></thead><tbody>
      ${rows.length ? rows.map(a => html`<tr data-asset="${String(a.id)}" tabindex="0" aria-selected="${String(S.sel === a.id)}"><td>${a.name}</td><td>${label(a.kind)}</td>
        <td>${a.care.overdue ? html`<span class="st warning">■ overdue</span>` : a.care.items.length ? `next ${a.care.items[0].due || 'undated'}` : '—'}</td></tr>`)
        : html`<tr class="empty"><td colspan="3">${d.assets.length ? 'No assets in the kinds shown: turn a tile on.' : 'No assets yet: press Add, pick a kind and click the map.'}</td></tr>`}</tbody></table>`);
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
      <div class="ed-act"><button class="btn" type="submit">Add care task</button></div></form>
      <section class="asset-form" aria-label="Attachments"><h3>Attachments</h3><div id="map-attach" class="attach"></div></section>` : ''}`);
    $('asset-form').dataset.geometry = geometry ? JSON.stringify(geometry) : '';
    if (a) attachments(a);
  }

  const attachRow = f => html`<li>${f.kind === 'picture' ? html`<a href="${f.link}" target="_blank" rel="noopener"><img class="thumb" src="/api/home/attachments/picture?id=${encodeURIComponent(f.id)}" alt="${f.name}" loading="lazy"></a>` : ''}<a href="${f.kind === 'link' ? (f.target || f.link) : f.link}" target="_blank" rel="noopener">${f.name}</a> <span class="k">${f.kind}</span></li>`;
  async function attachments(a) {
    const box = () => S.sel === a.id ? $('map-attach') : null;
    if (!box()) return;
    put(box(), html`<p class="note">Reading Drive…</p>`);
    let out;
    try { out = await window.shell.getJSON(`/api/home/attachments?asset=${a.id}`); }
    catch (err) { if (box()) put(box(), html`<p class="state-unknown"><b>▨ Not read.</b> ${err.message}</p>`); return; }
    if (!box()) return;
    put(box(), html`${out.files.length ? html`<ul class="attach-list">${out.files.map(attachRow)}</ul>` : html`<p class="note">${out.folder ? 'None yet.' : 'No folder yet: the first Upload or Reference makes it.'}</p>`}${
      out.folder ? html`<p class="why"><a href="${out.folder}" target="_blank" rel="noopener">Open the folder in Drive</a></p>` : ''}
      <form class="attach-form" data-attach="upload"><input type="file" name="file" required aria-label="File to upload"><button class="btn quiet sm" type="submit">Upload</button></form>
      <form class="attach-form" data-attach="reference"><input type="url" name="link" required placeholder="Drive link" aria-label="Drive link to reference"><input type="text" name="name" placeholder="Name" aria-label="Reference name"><button class="btn quiet sm" type="submit">Reference</button></form>
      <p class="why" role="status" data-attach-said>Upload and Reference write into this asset's Drive folder.</p>`);
  }
  async function attach(form) {
    const a = S.doc.assets.find(x => x.id === S.sel), button = form.querySelector('button'), said = $('map-attach').querySelector('[data-attach-said]');
    button.disabled = true; button.setAttribute('aria-busy', 'true'); said.textContent = 'Writing to Drive…';
    try {
      if (form.dataset.attach === 'upload') {
        const f = form.elements.file.files[0];
        const staged = await window.shell.post('/api/home/attach/file', f, { 'X-SD-File-Name': encodeURIComponent(f.name) });
        await window.shell.post('/api/home/attach', { asset: a.id, sha256: staged.sha256, name: f.name });
      } else {
        const name = form.elements.name.value.trim();
        await window.shell.post('/api/home/attach', { asset: a.id, link: form.elements.link.value.trim(), ...(name ? { name } : {}) });
      }
      attachments(a);
    } catch (err) {
      said.textContent = err.message; said.className = 'state-unknown';
      button.disabled = false; button.removeAttribute('aria-busy');
    }
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
    const tile = t.closest?.('#map-filters [data-f="group"]');
    if (tile) {
      if (tile.getAttribute?.('aria-disabled') === 'true') return;
      const g = tile.dataset.v;
      if (off.has(g)) off.delete(g); else off.add(g);
      const p = new URLSearchParams(location.search);
      if (off.size) p.set('mapoff', [...off].join(',')); else p.delete('mapoff');
      window.shell.url(p); draw(); return;
    }
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
    if (e.target.closest?.('#map-attach') && e.target.dataset.attach) { e.preventDefault(); attach(e.target); return; }
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
  window.homeMap = { load, state: S, draw, form, attachments };
})();
