// Help (sd:10917): draws /api/help, the phrase table and the help.md sections, each live value as the server read it.
// A value that did not read shows "unknown" and why; the head names every such problem. The page writes nothing.
(() => {
  const { html, put } = window.markup;
  let reads = 0;
  const part = p => p.code != null ? html`<code>${p.code}</code>`
    : p.href ? html`<a href="${p.href}">${p.text}</a>`
    : p.live ? (p.unknown != null ? html`<b>unknown</b> (${p.unknown})` : html`<b>${p.value}</b>`)
    : p.text;
  const parts = list => (list || []).map(part);
  function blocks(list) {
    const out = [];
    let items = [];
    const close = () => { if (items.length) out.push(html`<ul>${items}</ul>`); items = []; };
    (list || []).forEach(b => { if (b.kind === 'li') items.push(html`<li>${parts(b.parts)}</li>`); else { close(); out.push(html`<p>${parts(b.parts)}</p>`); } });
    close();
    return out;
  }
  function draw(doc) {
    const $ = id => document.getElementById(id);
    const rows = doc.phrases || [];
    put($('runs'), rows.length ? html`${rows.map(r => html`<tr><td><b>"${r.phrase}"</b></td><td>${r.does}</td><td>${r.budget}</td><td>${r.mode}</td></tr>`)}`
      : html`<tr><td colspan="4">No phrase read.</td></tr>`);
    // help-sections, not sections: that id is the shell's nav, so drawing into it replaced the nav.
    put($('help-sections'), html`${(doc.sections || []).map((s, i) => html`<section aria-labelledby="${`help-${i}`}"><div class="sec-head"><h2 id="${`help-${i}`}">${s.title}</h2></div>${blocks(s.blocks)}</section>`)}`);
    const problems = doc.error ? [`The help did not read: ${doc.error}`] : (doc.problems || []);
    $('problems').hidden = !problems.length;
    put($('problems'), html`${problems.map(p => html`${p} `)}`);
    put($('sub'), doc.error ? html`Not read` : html`Read <time class="rel" datetime="${doc.read}"></time>`);
  }
  async function load() {
    const n = ++reads;
    let doc;
    try { doc = await window.shell.getJSON('/api/help'); } catch (e) { doc = { error: e.message }; }
    if (n === reads) draw(doc);
  }
  document.addEventListener('DOMContentLoaded', load);
  // Read again (sd:5638): every live value is read again.
  window.PAGE_AGAIN = { title: 'Read the help again', run: () => load() };
})();
