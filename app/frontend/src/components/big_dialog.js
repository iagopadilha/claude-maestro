/* Big dialog of the panel (pause, agent, dependencies, error): native <dialog> so the focus is trapped, Esc closes and the
   browser puts the focus back. Header = badge + title + close; body on the left (scrolls); optional chat column on the right.
   Close also by clicking the backdrop. Content is Preact: `body` is a function returning a vnode, called on every rerender(). */
import { html, render } from '../util/preact.js';
import { ChatColumn } from '../pages/panel/dialogs/chat_column.js';
import { registerRenderer } from '../pages/project_panel.js';

let dialogCounter = 0;

// Calls `renderBody` as a component, so a body may use hooks without touching the frame's own hook order.
const BodyHost = ({ renderBody }) => renderBody();

function BigDialogFrame({ titleId, badge, badgeColor, title, renderBody, chat, onClose }) {
  return html`
    <div class="bd-head">
      ${badge ? html`<span class="bd-badge" style=${{ color: badgeColor, borderColor: badgeColor }}>${badge}</span>` : null}
      <h2 class="bd-title" id=${titleId}>${title}</h2>
      <button type="button" class="bd-x" aria-label="Close" onClick=${onClose}>✕</button>
    </div>
    <div class="bd-main">
      <div class="bd-body" tabindex="-1"><${BodyHost} renderBody=${renderBody} /></div>
      ${chat ? html`<${ChatColumn} key=${JSON.stringify(chat.scope)} chat=${chat} />` : null}
    </div>`;
}

/**
 * openBigDialog({ badge, badgeColor, title, body, chat, onClose }) -> { close(), rerender() }
 * body: () => vnode. chat: null | { title, placeholder, scope, greeting, suggestions, onUse?(text) }.
 */
export function openBigDialog({ badge, badgeColor = '#7fb2ff', title, body, chat = null, onClose }) {
  dialogCounter += 1;
  const titleId = 'bd-title-' + dialogCounter;
  const opener = document.activeElement;
  const dialog = document.createElement('dialog');
  dialog.className = 'bd-dialog';
  dialog.setAttribute('aria-labelledby', titleId);
  let closed = false, unregister = null;

  const handle = {
    close() {
      if (closed) return;
      closed = true;
      if (unregister) unregister();
      try { dialog.close(); } catch (_) { /* already closed */ }
      render(null, dialog);
      dialog.remove();
      if (onClose) onClose();
      if (opener && opener.isConnected && typeof opener.focus === 'function') opener.focus();
    },
    rerender() {
      if (closed) return;
      render(html`<${BigDialogFrame} titleId=${titleId} badge=${badge} badgeColor=${badgeColor} title=${title}
        renderBody=${body} chat=${chat} onClose=${handle.close} />`, dialog);
    },
  };

  dialog.addEventListener('cancel', (event) => { event.preventDefault(); handle.close(); });
  dialog.addEventListener('mousedown', (event) => { if (event.target === dialog) handle.close(); });
  document.body.appendChild(dialog);
  if (typeof dialog.showModal === 'function') dialog.showModal(); else dialog.setAttribute('open', '');
  handle.rerender(); // after showModal: the body can measure its own size on the first render (the deps diagram fits itself)
  // the dialog is a second Preact root: it follows every renderAll() of the panel, so bodies reading shared stores stay current
  unregister = registerRenderer(() => handle.rerender());
  setTimeout(() => {
    const bodyElement = dialog.querySelector('.bd-body');
    if (!closed && bodyElement) bodyElement.focus();
  }, 0);
  return handle;
}
