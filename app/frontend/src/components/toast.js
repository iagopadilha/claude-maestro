/* Short notices in the corner (aria-live). Errors stay 8 s, the rest 4 s. */
import { html, render } from '../util/preact.js';

let items = [], seq = 0;

/** Content of #toasts: one line per live notice. */
export function Toasts({ items: list = items }) {
  return list.map((t) => html`<div key=${t.id} class=${'toast ' + (t.kind || '')} role=${t.kind === 'err' ? 'alert' : 'status'}>${t.msg}</div>`);
}

function zone() {
  let el = document.getElementById('toasts');
  if (!el) {
    el = document.createElement('div');
    el.id = 'toasts'; el.className = 'toasts'; el.setAttribute('aria-live', 'polite');
    document.body.appendChild(el);
  }
  return el;
}
const draw = () => render(html`<${Toasts} items=${items} />`, zone());

export function toast(msg, kind) {
  const t = { id: ++seq, msg: msg == null ? '' : String(msg), kind };
  items = [...items, t];
  draw();
  setTimeout(() => { items = items.filter((x) => x !== t); draw(); }, kind === 'err' ? 8000 : 4000);
}
