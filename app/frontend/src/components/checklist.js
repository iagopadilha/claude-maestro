/* Checklist as a trail (stepper): each step is ok, pending or optional; the current step is highlighted.
   Clicking a step (or pressing Enter on it) shows its detail on the line below (aria-live). */
import { html, useState } from '../util/preact.js';

const ICON = { ok: '✓', pending: '○', optional: '◌', current: '●' };
const LABEL = { ok: 'done', pending: 'pending', optional: 'optional', current: 'next step' };

/** Content of div.ck: steps = [{id, title, state, detail}]; the open step is local state. */
function ChecklistContent({ label, steps = [], currentId = null }) {
  const [chosen, setChosen] = useState(null);
  const p = steps.find((x) => x.id === chosen);
  return html`
    <nav class="ck-nav" aria-label=${label}>
      <ol class="ck-list">${steps.map((s, i) => {
        const state = s.id === currentId && s.state !== 'ok' ? 'current' : s.state;
        const open = chosen === s.id;
        return html`<li key=${s.id} class="ck-item">
          ${i ? html`<span class="ck-line" aria-hidden="true" />` : null}
          <button type="button" id=${'ck-' + s.id} class=${'ck-step ck-' + state + (open ? ' open' : '')} aria-expanded=${String(open)}
              aria-current=${s.id === currentId ? 'step' : null} title=${s.detail || ''} onClick=${() => setChosen(open ? null : s.id)}>
            <span class="ck-ic" aria-hidden="true">${ICON[state] || '○'}</span>
            <span class="ck-t">${s.title}</span>
            <span class="sr-only">${` (${LABEL[state] || state})`}</span>
          </button>
        </li>`;
      })}</ol>
    </nav>
    <div class="ck-detail" aria-live="polite" hidden=${!p}>${p ? [html`<strong>${p.title + ': '}</strong>`, p.detail || LABEL[p.state] || ''] : null}</div>`;
}

/** <Checklist label steps currentId />: the whole div.ck. */
export function Checklist(props) {
  return html`<div class="ck"><${ChecklistContent} ...${props} /></div>`;
}
