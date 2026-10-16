/* Conformance choice (the "Conformance:" line in the project's CLAUDE.md): STANDARD, LEGACY or decide later. */
import { html, useState, useMemo } from '../util/preact.js';

let seq = 0;
const OPTIONS = [
  { value: 'STANDARD', title: 'STANDARD', text: 'The project follows the skill structure (back with Api/Data/Domain; front with src/pages and src/components). New code and touched classes follow that pattern.' },
  { value: 'LEGACY', title: 'LEGACY', text: 'The project has its own structure. New code mimics what already exists and old files only get the minimum edit needed.' },
  { value: null, title: 'Decide later', text: 'No line for now; the loop asks before writing the first spec.' },
];
const optionsFor = (allowLater) => OPTIONS.filter((o) => allowLater || o.value !== null);
/** Value the group starts with (what <Conformance> shows checked before any change). */
export const conformanceDefault = (initial, allowLater) => { const opts = optionsFor(allowLater); return opts.some((o) => o.value === initial) ? initial : opts[0].value; };

/** Content of fieldset.conf (legend + radios). onChange(value) on every choice. */
function ConformanceContent({ initial = null, allowLater = true, onChange }) {
  const name = useMemo(() => 'conformance-' + (++seq), []);
  const [current, setCurrent] = useState(() => conformanceDefault(initial, allowLater));
  return html`
    <legend class="nl-label">Code conformance</legend>
    ${optionsFor(allowLater).map((o, i) => {
      const id = `${name}-${i}`;
      const pick = (e) => { if (!e.currentTarget.checked) return; setCurrent(o.value); if (onChange) onChange(o.value); };
      return html`<label key=${id} class="conf-op" for=${id}>
        <input type="radio" name=${name} id=${id} value=${o.value == null ? '' : o.value} checked=${o.value === current} onChange=${pick} />
        <span class="conf-t"><strong>${o.title}</strong><span class="conf-d">${o.text}</span></span>
      </label>`;
    })}`;
}

/** Accessible radio group as a component: <Conformance initial allowLater onChange />. */
export function Conformance(props) {
  return html`<fieldset class="conf"><${ConformanceContent} ...${props} /></fieldset>`;
}
