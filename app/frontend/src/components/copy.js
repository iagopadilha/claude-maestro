/* Copy to the clipboard, with a fallback (textarea + execCommand) and a toast. */
import { h } from '../util/dom.js';
import { toast } from './toast.js';

export async function copyText(text, okMessage = 'Copied.') {
  try { await navigator.clipboard.writeText(text); toast(okMessage, 'ok'); return true; } catch (_) { /* fallback */ }
  try {
    const ta = h('textarea', { 'aria-hidden': 'true', style: 'position:fixed;left:-9999px;top:0' });
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

/** <pre> block with a Copy button next to it. */
export function copyableBlock(text, label = 'Copy', cls = 'cmd') {
  return h('div', { class: 'cmd-row' }, h('pre', { class: cls, text }),
    h('button', { type: 'button', class: 'btn btn-sm', onclick: () => copyText(text) }, label));
}
