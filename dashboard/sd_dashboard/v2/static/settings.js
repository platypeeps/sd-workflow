// Settings (sd:3438): the theme buttons are theme.js's; this script wires the sections width to shell.railWidth (sd:3445)
// and says Saved after a change. Fleet (sd:3989) lists the sd.* keys one value serves on every machine, Sweeps (sd:5109)
// the Run sweep settings, and Dashboard (sd:5109) the dashboard lists and the adversarial review's model, from /api/settings.
// It loads before shell.js, so it reads the shell once the page has loaded.
window.PAGE_ATTENTION = { state: 'ok', n: 0, what: 'settings' };
document.addEventListener('DOMContentLoaded', () => {
  const S = window.shell, set = document.getElementById('rail-width'), out = document.getElementById('rail-width-out');
  // A preset's value is its width in px; Default's is empty (tokens.css --rail, 216 px). A dragged width checks no preset.
  // A change autosaves and says so quietly (foundation patterns.md § Forms).
  const show = (w, saved) => {
    for (const r of set.querySelectorAll('input')) r.checked = r.value === (w ? String(w) : '');
    out.textContent = (w ? `${w} px` : 'default, 216 px') + (saved ? ' · Saved' : '');
  };
  set.addEventListener('change', e => show(S.railWidth(e.target.value || null), true));
  // The rail's edge and its keys set the width too; the shell says so once the width is kept.
  document.addEventListener('shell:rail-width', e => show(e.detail, true));
  // theme.js stores the choice on its own click listener; this one only says Saved.
  document.getElementById('theme').addEventListener('click', e => {
    if (e.target.closest('[data-theme-choice]')) document.getElementById('theme-saved').textContent = 'Saved';
  });
  show(S.railWidth());
  sections(S);
});

// Fleet (sd:3989), Sweeps and Dashboard (sd:5109), from one read of /api/settings. A single value autosaves through its
// section's POST and says Saved, or names the value the set replaced when it is not the one shown. A refusal keeps what
// was typed and says why. A set past its bound has an unknown outcome, and a row changed since the read refuses (409):
// either way the row says so, and the page reads the values again. A row post names the row value shown; an empty value
// unsets. A list (lines) saves on its Save button through /api/settings/list with the digest of what the page showed; a
// text its grammar refuses keeps what was typed and names each problem.
function sections(S) {
  const { html, put } = window.markup;
  const SECTIONS = {
    fleet: { post: '/api/settings/fleet', name: k => `sd.${k.key}`, ok: 'One value serves every machine. A change saves at once.' },
    sweeps: { post: '/api/settings/row', name: k => k.key, before: true,
              ok: 'What Run sweep reads on Tasks, for every machine. A change saves at once; an empty value unsets it.' },
    dashboard: { post: '/api/settings/row', name: k => k.key, before: true,
                 ok: 'What the dashboard pages read. A field saves at once; a list saves on Save. Empty unsets.' },
  };
  const SOURCE = { database: 'workflow database', file: 'config.json on this machine', default: 'unset · default applies', unset: 'unset' };
  // A list's file source is its old file under <config>/project-dashboard, read until the first Save writes the row.
  const LIST_SOURCE = { database: 'workflow database', file: 'old file in the config folder · Save moves it to the database', unset: 'unset' };
  const KEYS = {};
  const field = k => k.choices ? html`<select class="fld" id="f-${k.key}" data-key="${k.key}" aria-labelledby="f-${k.key}-h">${k.value === null ? html`<option value="" selected disabled>unset</option>` : ''}${k.choices.map(c => html`<option value="${c}"${c === k.value ? html` selected` : ''}>${c}</option>`)}</select>`
    : html`<input class="fld" id="f-${k.key}" data-key="${k.key}" type="text" value="${k.value ?? ''}" placeholder="unset" spellcheck="false" autocapitalize="off" autocomplete="off" aria-labelledby="f-${k.key}-h">`;
  const row = (k, name) => k.lines ? html`<li class="list" data-key="${k.key}">
    <div class="what"><h3 id="f-${k.key}-h">${name}</h3><p>${k.description}</p></div>
    <div class="control">
      <textarea class="fld" id="f-${k.key}" data-list="${k.key}" rows="6" placeholder="unset" spellcheck="false" autocapitalize="off" autocomplete="off" aria-labelledby="f-${k.key}-h">${k.value ?? ''}</textarea>
      <div class="act"><button class="btn" type="button" data-save="${k.key}">Save</button>
      <span class="src">${k.problem ? `not read: ${k.problem}` : LIST_SOURCE[k.source] || k.source}</span></div>
      <span class="saved" id="f-${k.key}-out" role="status"></span>
    </div></li>` : html`<li data-key="${k.key}">
    <div class="what"><h3 id="f-${k.key}-h">${name}</h3><p>${k.description}</p></div>
    <div class="control">
      ${field(k)}
      <span class="src">${SOURCE[k.source] || k.source}</span>
      <span class="saved" id="f-${k.key}-out" role="status"></span>
    </div></li>`;
  async function read(said = {}) {
    let doc;
    try { doc = await S.getJSON('/api/settings'); } catch (err) { doc = { reason: `the page did not read it: ${err.message}` }; }
    for (const [id, section] of Object.entries(SECTIONS)) {
      KEYS[id] = doc[id] || [];
      put(document.getElementById(id), html`${KEYS[id].map(k => row(k, section.name(k)))}`);
      document.getElementById(`${id}-note`).textContent = doc[id] ? section.ok : `Not read: ${doc[`${id}_reason`] || doc.reason}.`;
    }
    for (const [key, text] of Object.entries(said)) { const out = document.getElementById(`f-${key}-out`); if (out) out.textContent = text; }
  }
  // One answer for every write: a 409 reads again, another refusal keeps what was typed, a failure past the bound is unknown.
  async function save(key, shown, out, post) {
    out.textContent = 'Saving…';
    let set;
    try { set = await post(); }
    catch (err) {
      if (err.status === 409) return read({ [key]: `Not saved: ${err.message}. The value shown is the one read now.` });
      if (err.status && err.status < 500) { out.textContent = `Not saved: ${err.message}`; return; }
      return read({ [key]: `Outcome unknown: ${err.message}. The value shown is the one read after.` });
    }
    await read({ [key]: set.was === shown ? 'Saved' : `Saved · it held ${set.was ?? 'unset'}, not ${shown ?? 'unset'} as shown` });
  }
  for (const [id, section] of Object.entries(SECTIONS)) {
    const list = document.getElementById(id);
    list.addEventListener('change', e => {
      const key = e.target.dataset?.key, shown = KEYS[id].find(k => k.key === key);
      if (!shown || shown.lines) return;
      save(key, section.before ? shown.row : shown.value, document.getElementById(`f-${key}-out`),
           () => S.post(section.post, section.before ? { key, value: e.target.value, before: shown.row } : { key, value: e.target.value }));
    });
    list.addEventListener('click', e => {
      const key = e.target.closest?.('[data-save]')?.dataset.save, shown = KEYS[id].find(k => k.key === key);
      if (!shown) return;
      const text = document.getElementById(`f-${key}`).value;
      // The list's `was` is its row; a value read from the old file had none, so Saved compares with the row shown.
      save(key, shown.source === 'database' ? shown.value : null, document.getElementById(`f-${key}-out`),
           () => S.post('/api/settings/list', { key, text, before: shown.digest }));
    });
  }
  // A link to one row, as Health's and Today's to the status services and usage providers lists (pack sd:9260),
  // names its field: the rows draw after the read, so the browser's own jump to the anchor finds nothing.
  read().then(() => location.hash && document.getElementById(decodeURIComponent(location.hash.slice(1)))?.focus());
}
