/* Short notices in the corner (aria-live). Errors stay 8 s, the rest 4 s. */
import { h } from '../util/dom.js';

function zone() {
  let el = document.getElementById('toasts');
  if (!el) { el = h('div', { id: 'toasts', class: 'toasts', 'aria-live': 'polite' }); document.body.appendChild(el); }
  return el;
}

export function toast(msg, kind) {
  const el = h('div', { class: 'toast ' + (kind || ''), role: kind === 'err' ? 'alert' : 'status', text: msg });
  zone().appendChild(el);
  setTimeout(() => el.remove(), kind === 'err' ? 8000 : 4000);
}
