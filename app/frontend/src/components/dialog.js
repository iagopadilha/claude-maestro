/* Accessible modal dialog on top of the native <dialog>: focus trapped, Esc closes, focus returns to the opener.
   No alert/confirm/prompt: confirmations use confirmDialog() from here. */
import { h, fill } from '../util/dom.js';

let counter = 0;

/**
 * openDialog({ title, content, actions, width, onClose, closeOnBackdrop, cls })
 * actions: [{ text, cls, onClick(api) -> any, id, disabled }]; api = { body, element, close, setContent, setActions, busy(bool) }.
 */
export function openDialog({ title, content, actions = [], width = 560, onClose, closeOnBackdrop = true, cls = '' }) {
  counter += 1;
  const titleId = 'dlg-t-' + counter;
  const opener = document.activeElement;
  const body = h('div', { class: 'dlg-body' });
  const foot = h('div', { class: 'dlg-foot' });
  const btnX = h('button', { type: 'button', class: 'dlg-x', 'aria-label': 'Close' }, '×');
  const dlg = h('dialog', { class: 'dlg ' + cls, 'aria-labelledby': titleId },
    h('div', { class: 'dlg-head' }, h('h2', { class: 'dlg-title', id: titleId, text: title }), btnX),
    body, foot);
  dlg.style.width = `min(${width}px, calc(100vw - 32px))`;
  let closed = false;
  const api = {
    body, element: dlg,
    close() {
      if (closed) return;
      closed = true;
      try { dlg.close(); } catch (_) { /* already closed */ }
      dlg.remove();
      if (onClose) onClose();
      if (opener && opener.isConnected && typeof opener.focus === 'function') opener.focus();
    },
    setContent(...nodes) { fill(body, ...nodes); },
    setActions(list) { buildActions(list); },
    busy(yes) { for (const b of foot.querySelectorAll('button')) b.disabled = !!yes; },
  };
  function buildActions(list) {
    fill(foot, (list || []).map((a) => {
      const b = h('button', { type: 'button', class: 'btn ' + (a.cls || ''), id: a.id || null, disabled: a.disabled || null }, a.text);
      b.addEventListener('click', () => { if (a.onClick) a.onClick(api); else api.close(); });
      return b;
    }));
    foot.hidden = !(list && list.length);
  }
  btnX.addEventListener('click', () => api.close());
  dlg.addEventListener('cancel', (e) => { e.preventDefault(); api.close(); });
  if (closeOnBackdrop) dlg.addEventListener('mousedown', (e) => { if (e.target === dlg) api.close(); });
  fill(body, content == null ? [] : content);
  buildActions(actions);
  document.body.appendChild(dlg);
  if (typeof dlg.showModal === 'function') dlg.showModal(); else dlg.setAttribute('open', '');
  const target = body.querySelector('[autofocus], input, textarea, select') || foot.querySelector('.btn-primary, .btn') || btnX;
  setTimeout(() => target.focus(), 0);
  return api;
}

/** Confirmation in its own modal. Resolves true/false. */
export function confirmDialog({ title, text, yesText = 'Confirm', noText = 'Cancel', danger = false, extraContent = null }) {
  return new Promise((resolve) => {
    let answer = false;
    const d = openDialog({
      title, width: 460,
      content: [h('p', { class: 'dlg-text', text }), extraContent],
      actions: [
        { text: noText, onClick: (a) => a.close() },
        { text: yesText, cls: danger ? 'btn-danger' : 'btn-primary', onClick: (a) => { answer = true; a.close(); } },
      ],
      onClose: () => resolve(answer),
    });
    return d;
  });
}
