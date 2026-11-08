// Home's attachments (pack sd:8112, sd:8147): an asset's Drive folder (hoa_attach.HOME), shown in the Map's asset
// details and the Koi view's fish form; one copy for both. GET /api/home/attachments lists it: pictures as thumbnails
// through /api/home/attachments/picture, the rest as links (a Reference .url links its target). Upload stages at
// /api/home/attach/file, then it and Reference post /api/home/attach, on the operator's press only.
(() => {
  const { html, put } = window.markup;
  const $ = id => document.getElementById(id);
  const row = f => html`<li>${f.kind === 'picture' ? html`<a href="${f.link}" target="_blank" rel="noopener"><img class="thumb" src="/api/home/attachments/picture?id=${encodeURIComponent(f.id)}" alt="${f.name}" loading="lazy"></a>` : ''}<a href="${f.kind === 'link' ? (f.target || f.link) : f.link}" target="_blank" rel="noopener">${f.name}</a> <span class="k">${f.kind}</span></li>`;

  // `current()` is false once the operator picked another asset: a late answer draws nothing.
  async function show(box, id, current) {
    if (!current() || !$(box)) return;
    put($(box), html`<p class="note">Reading Drive…</p>`);
    let out;
    try { out = await window.shell.getJSON(`/api/home/attachments?asset=${id}`); }
    catch (err) { if (current() && $(box)) put($(box), html`<p class="state-unknown"><b>▨ Not read.</b> ${err.message}</p>`); return; }
    if (!current() || !$(box)) return;
    put($(box), html`${out.files.length ? html`<ul class="attach-list">${out.files.map(row)}</ul>` : html`<p class="note">${out.folder ? 'None yet.' : 'No folder yet: the first Upload or Reference makes it.'}</p>`}${
      out.folder ? html`<p class="why"><a href="${out.folder}" target="_blank" rel="noopener">Open the folder in Drive</a></p>` : ''}
      <form class="attach-form" data-attach="upload"><input type="file" name="file" required aria-label="File to upload"><button class="btn quiet sm" type="submit">Upload</button></form>
      <form class="attach-form" data-attach="reference"><input type="url" name="link" required placeholder="Drive or Docs link" aria-label="Drive link to reference"><input type="text" name="name" placeholder="Name" aria-label="Reference name"><button class="btn quiet sm" type="submit">Reference</button></form>
      <p class="why" role="status" data-attach-said>Upload and Reference write into this asset's Drive folder.</p>`);
  }

  async function send(form, box, id, current) {
    const button = form.querySelector('button'), said = $(box)?.querySelector('[data-attach-said]');
    const say = (text, cls) => { if (said) { said.textContent = text; if (cls) said.className = cls; } };
    button.disabled = true; button.setAttribute('aria-busy', 'true'); say('Writing to Drive…');
    try {
      if (form.dataset.attach === 'upload') {
        const f = form.elements.file.files[0];
        const staged = await window.shell.post('/api/home/attach/file', f, { 'X-SD-File-Name': encodeURIComponent(f.name) });
        await window.shell.post('/api/home/attach', { asset: id, sha256: staged.sha256, name: f.name });
      } else {
        const name = form.elements.name.value.trim();
        await window.shell.post('/api/home/attach', { asset: id, link: form.elements.link.value.trim(), ...(name ? { name } : {}) });
      }
      await show(box, id, current);
    } catch (err) {
      say(err.message, 'state-unknown');
      button.disabled = false; button.removeAttribute('aria-busy');
    }
  }

  window.homeAttach = { show, send };
})();
