/* Conformance choice (the "Conformance:" line in the project's CLAUDE.md): STANDARD, LEGACY or decide later. */
import { h } from '../util/dom.js';

let seq = 0;
const OPTIONS = [
  { value: 'STANDARD', title: 'STANDARD', text: 'The project follows the skill structure (back with Api/Data/Domain; front with src/pages and src/components). New code and touched classes follow that pattern.' },
  { value: 'LEGACY', title: 'LEGACY', text: 'The project has its own structure. New code mimics what already exists and old files only get the minimum edit needed.' },
  { value: null, title: 'Decide later', text: 'No line for now; the loop asks before writing the first spec.' },
];

/** Accessible radio group. conformanceField({ initial, allowLater }) -> { element, value() } */
export function conformanceField({ initial = null, allowLater = true } = {}) {
  seq += 1;
  const name = 'conformance-' + seq;
  const options = OPTIONS.filter((o) => allowLater || o.value !== null);
  let current = options.some((o) => o.value === initial) ? initial : options[0].value;
  const items = options.map((o, i) => {
    const id = `${name}-${i}`;
    const radio = h('input', { type: 'radio', name, id, value: o.value == null ? '' : o.value });
    radio.checked = o.value === current;
    radio.addEventListener('change', () => { if (radio.checked) current = o.value; });
    return h('label', { class: 'conf-op', for: id }, radio, h('span', { class: 'conf-t' }, h('strong', { text: o.title }), h('span', { class: 'conf-d', text: o.text })));
  });
  const element = h('fieldset', { class: 'conf' }, h('legend', { class: 'nl-label', text: 'Code conformance' }), items);
  return { element, value: () => current };
}
