/* Copy to the clipboard, with a fallback (textarea + execCommand) and a toast. */
import { html } from '../util/preact.js';
import { toast } from './toast.js';

export async function copyText(text, okMessage = 'Copied.') {
  try { await navigator.clipboard.writeText(text); toast(okMessage, 'ok'); return true; } catch (_) { /* fallback */ }
  try {
    const ta = document.createElement('textarea');
    ta.setAttribute('aria-hidden', 'true');
    ta.style.cssText = 'position:fixed;left:-9999px;top:0';
    ta.value = text;
    document.body.appendChild(ta); ta.select();
    const ok = document.execCommand('copy');
    ta.remove();
    toast(ok ? okMessage : 'Could not copy; select the text and copy it.', ok ? 'ok' : 'err');
    return ok;
  } catch (_) {
    toast('Could not copy; select the text and copy it.', 'err');
    return false;
  }
}

/** Button that copies `text` (children = label). */
export function CopyButton({ text, okMessage, class: cls = 'btn btn-sm', children = 'Copy', ...rest }) {
  return html`<button type="button" class=${cls} ...${rest} onClick=${() => copyText(text, okMessage)}>${children}</button>`;
}

/** <pre> block with a Copy button next to it. */
export function CopyBlock({ text, label = 'Copy', cls = 'cmd' }) {
  return html`<div class="cmd-row"><pre class=${cls}>${text}</pre><${CopyButton} text=${text}>${label}<//></div>`;
}
