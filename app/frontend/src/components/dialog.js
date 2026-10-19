/* Accessible modal dialog on top of the native <dialog>: focus trapped, Esc closes, focus returns to the opener.
   No alert/confirm/prompt: confirmations use confirmDialog() from here.
   Content is Preact vnodes (and text). */
import { html, render, createRef, useRef, useMemo, useLayoutEffect } from '../util/preact.js';

let counter = 0;

const flat = (content) => (content == null ? [] : [content].flat(Infinity)).filter((k) => k != null && k !== false && k !== true);

/** Head, body and foot of a dialog (the content of the <dialog> element).
    busy: null = each action uses its own disabled; true/false = every button disabled/enabled. */
export function DialogFrame({ titleId, title, content, actions, busy = null, onAction, onX, bodyRef, footRef }) {
  const kids = flat(content);
  const acts = (actions || []).filter(Boolean);
  return html`
    <div class="dlg-head">
      <h2 class="dlg-title" id=${titleId}>${title}</h2>
      <button type="button" class="dlg-x" aria-label="Close" onClick=${onX}>×</button>
    </div>
    <div class="dlg-body" ref=${bodyRef}>${kids}</div>
    <div class="dlg-foot" ref=${footRef} hidden=${!acts.length}>
      ${acts.map((a, i) => html`<button key=${i} type="button" class=${'btn ' + (a.cls || '')} id=${a.id || undefined}
          disabled=${busy == null ? !!a.disabled : busy} onClick=${() => onAction(a)}>${a.text}</button>`)}
    </div>`;
}

function focusFirst(dlg, body, foot) {
  const target = (body && body.querySelector('[autofocus], input, textarea, select')) || (foot && foot.querySelector('.btn-primary, .btn')) || dlg.querySelector('.dlg-x');
  if (target) target.focus();
}

/**
 * openDialog({ title, content, actions, width, onClose, closeOnBackdrop, cls })
 * actions: [{ text, cls, onClick(api) -> any, id, disabled }]; api = { body, element, close, setContent, setActions, busy(bool) }.
 */
export function openDialog({ title, content, actions = [], width = 560, onClose, closeOnBackdrop = true, cls = '' }) {
  counter += 1;
  const titleId = 'dlg-t-' + counter;
  const opener = document.activeElement;
  const dlg = document.createElement('dialog');
  dlg.className = 'dlg ' + cls;
  dlg.setAttribute('aria-labelledby', titleId);
  dlg.style.width = `min(${width}px, calc(100vw - 32px))`;
  const bodyRef = createRef(), footRef = createRef();
  const view = { content, actions, busy: null };
  let closed = false;
  const draw = () => {
    if (closed) return;
    render(html`<${DialogFrame} titleId=${titleId} title=${title} content=${view.content} actions=${view.actions} busy=${view.busy}
      onAction=${(a) => { if (a.onClick) a.onClick(api); else api.close(); }} onX=${() => api.close()} bodyRef=${bodyRef} footRef=${footRef} />`, dlg);
  };
  const api = {
    get body() { return bodyRef.current; },
    element: dlg,
    close() {
      if (closed) return;
      closed = true;
      try { dlg.close(); } catch (_) { /* already closed */ }
      render(null, dlg);
      dlg.remove();
      if (onClose) onClose();
      if (opener && opener.isConnected && typeof opener.focus === 'function') opener.focus();
    },
    setContent(...nodes) { view.content = nodes; draw(); },
    setActions(list) { view.actions = list; view.busy = null; draw(); },
    busy(yes) { view.busy = !!yes; draw(); },
  };
  dlg.addEventListener('cancel', (e) => { e.preventDefault(); api.close(); });
  if (closeOnBackdrop) dlg.addEventListener('mousedown', (e) => { if (e.target === dlg) api.close(); });
  draw();
  document.body.appendChild(dlg);
  if (typeof dlg.showModal === 'function') dlg.showModal(); else dlg.setAttribute('open', '');
  setTimeout(() => { if (!closed) focusFirst(dlg, bodyRef.current, footRef.current); }, 0);
  return api;
}

/** Declarative modal: mount <Dialog title actions onClose>children</Dialog> to open it, unmount to close it.
    Action onClick receives { close: onClose }. */
export function Dialog({ title, actions = [], width = 560, onClose, closeOnBackdrop = true, cls = '', busy = null, children }) {
  const ref = useRef(null), bodyRef = useRef(null), footRef = useRef(null);
  const titleId = useMemo(() => 'dlg-t-' + (++counter), []);
  const close = () => { if (onClose) onClose(); };
  useLayoutEffect(() => {
    const dlg = ref.current, opener = document.activeElement;
    if (typeof dlg.showModal === 'function') dlg.showModal(); else dlg.setAttribute('open', '');
    const t = setTimeout(() => focusFirst(dlg, bodyRef.current, footRef.current), 0);
    return () => {
      clearTimeout(t);
      try { dlg.close(); } catch (_) { /* already closed */ }
      if (opener && opener.isConnected && typeof opener.focus === 'function') opener.focus();
    };
  }, []);
  return html`<dialog ref=${ref} class=${'dlg ' + cls} aria-labelledby=${titleId} style=${{ width: `min(${width}px, calc(100vw - 32px))` }}
      onCancel=${(e) => { e.preventDefault(); close(); }} onMouseDown=${(e) => { if (closeOnBackdrop && e.target === ref.current) close(); }}>
    <${DialogFrame} titleId=${titleId} title=${title} content=${children} actions=${actions} busy=${busy}
      onAction=${(a) => { if (a.onClick) a.onClick({ close }); else close(); }} onX=${close} bodyRef=${bodyRef} footRef=${footRef} />
  </dialog>`;
}

/** Confirmation in its own modal. Resolves true/false. */
export function confirmDialog({ title, text, yesText = 'Confirm', noText = 'Cancel', danger = false, extraContent = null }) {
  return new Promise((resolve) => {
    let answer = false;
    openDialog({
      title, width: 460,
      content: [html`<p class="dlg-text">${text}</p>`, extraContent],
      actions: [
        { text: noText, onClick: (a) => a.close() },
        { text: yesText, cls: danger ? 'btn-danger' : 'btn-primary', onClick: (a) => { answer = true; a.close(); } },
      ],
      onClose: () => resolve(answer),
    });
  });
}
