/* "Standards (optional)" block of the installers: one text input per slot (git, backend, frontend, app-map, observability) bound to the
   company's skill, saved in the `## Standards` section of the CLAUDE.md of the repository or subproject. Empty = generic defaults.
   It is optional by design: nothing here gates the installer's Next / Continue / Done.
   <StandardsBlock api scope? title? />  api = { get(): Promise<{bindings, notes, slots}>, put(bindings): Promise<{bindings, notes, slots}> }
   (both reject with an Error that carries `message`); `scope` is any string that changes when `api` points elsewhere (reload). */
import { html, useState, useEffect } from '../util/preact.js';
import { toast } from './toast.js';

export const STANDARDS_HINT = "Bind a slot to your company's skill; empty = generic defaults.";
const DEFAULT_SLOTS = ['git', 'backend', 'frontend', 'app-map', 'observability'];
const arrayOf = (value) => (Array.isArray(value) ? value : []);
const clean = (values) => Object.fromEntries(Object.entries(values).map(([slot, value]) => [slot, String(value || '').trim()]).filter(([, value]) => value));
const sameBindings = (a, b) => JSON.stringify(clean(a)) === JSON.stringify(clean(b));

export function StandardsBlock({ api, scope = '', title = 'Standards (optional)' }) {
  const [state, setState] = useState({ loaded: false, error: '', slots: DEFAULT_SLOTS, notes: [] });
  const [saved, setSaved] = useState({});
  const [values, setValues] = useState({});
  const [saving, setSaving] = useState(false);
  const apply = (result) => {
    const bindings = (result && result.bindings) || {};
    setSaved(bindings); setValues({ ...bindings });
    setState({ loaded: true, error: '', slots: arrayOf(result && result.slots).length ? result.slots : DEFAULT_SLOTS, notes: arrayOf(result && result.notes) });
  };
  useEffect(() => {
    let current = true;
    setState((previous) => ({ ...previous, loaded: false, error: '' }));
    api.get().then((result) => { if (current) apply(result); })
      .catch((error) => { if (current) setState((previous) => ({ ...previous, loaded: true, error: (error && error.message) || 'request failed' })); });
    return () => { current = false; };
  }, [scope]);
  const dirty = !sameBindings(values, saved);
  const save = async () => {
    setSaving(true);
    try { apply(await api.put(clean(values))); toast('Standards saved.', 'ok'); }
    catch (error) { toast('Standards not saved: ' + ((error && error.message) || 'request failed'), 'err'); }
    finally { setSaving(false); }
  };
  return html`<div class="sp-std" data-scope=${scope || undefined}>
    <div class="sp-std-head"><strong class="sp-inst-name">${title}</strong>${dirty ? html`<span class="sp-muted" role="status">unsaved</span>` : null}</div>
    <p class="sp-muted sp-std-hint">${STANDARDS_HINT}</p>
    ${state.error ? html`<div class="sp-note sp-note-bad" role="alert">${'Could not read the Standards: ' + state.error}</div>` : null}
    ${!state.loaded ? html`<div class="sp-muted" role="status"><span class="spinner sm" aria-hidden="true"></span> Reading…</div>`
      : html`<div class="sp-std-grid">
        ${state.slots.map((slot) => html`<label key=${slot} class="nl-field sp-std-field"><span class="sp-std-slot">${slot}</span>
          <input type="text" data-slot=${slot} value=${values[slot] || ''} placeholder="generic default" autocomplete="off" spellcheck="false"
            disabled=${saving} onInput=${(event) => { const value = event.currentTarget.value; setValues((current) => ({ ...current, [slot]: value })); }} /></label>`)}
      </div>`}
    ${state.notes.map((note) => html`<div key=${note} class="sp-muted">${note}</div>`)}
    <div class="sp-std-foot"><button type="button" class="btn btn-sm" data-action="save-standards" disabled=${saving || !state.loaded || !dirty} onClick=${save}>${saving ? 'Saving…' : 'Save standards'}</button></div>
  </div>`;
}
