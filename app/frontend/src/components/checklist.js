/* Checklist as a trail (stepper): each step is ok, pending or optional; the current step is highlighted.
   Clicking a step (or pressing Enter on it) shows its detail on the line below (aria-live). */
import { h, fill, keepFocus } from '../util/dom.js';

const ICON = { ok: '✓', pending: '○', optional: '◌', current: '●' };
const LABEL = { ok: 'done', pending: 'pending', optional: 'optional', current: 'next step' };

/** createChecklist({ label }) -> { element, update(steps, currentId) }; steps: [{id, title, state, detail}] */
export function createChecklist({ label }) {
  const list = h('ol', { class: 'ck-list' });
  const detail = h('div', { class: 'ck-detail', 'aria-live': 'polite' });
  const element = h('div', { class: 'ck' }, h('nav', { class: 'ck-nav', 'aria-label': label }, list), detail);
  let chosen = null, last = [];

  function showDetail() {
    const p = last.find((x) => x.id === chosen);
    fill(detail, p ? [h('strong', { text: p.title + ': ' }), p.detail || LABEL[p.state] || ''] : []);
    detail.hidden = !p;
  }
  function update(steps, currentId) {
    last = steps || [];
    keepFocus(() => fill(list, last.map((p, i) => {
      const state = p.id === currentId && p.state !== 'ok' ? 'current' : p.state;
      const b = h('button', { type: 'button', id: 'ck-' + p.id, class: 'ck-step ck-' + state + (chosen === p.id ? ' open' : ''), 'aria-expanded': String(chosen === p.id),
        'aria-current': p.id === currentId ? 'step' : null, title: p.detail || '' },
      h('span', { class: 'ck-ic', 'aria-hidden': 'true', text: ICON[state] || '○' }),
      h('span', { class: 'ck-t', text: p.title }),
      h('span', { class: 'sr-only', text: ` (${LABEL[state] || state})` }));
      b.addEventListener('click', () => { chosen = chosen === p.id ? null : p.id; update(last, currentId); });
      return h('li', { class: 'ck-item' }, i ? h('span', { class: 'ck-line', 'aria-hidden': 'true' }) : null, b);
    })));
    showDetail();
  }
  detail.hidden = true;
  return { element, update };
}
