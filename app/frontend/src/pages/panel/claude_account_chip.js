/* Which Claude account the loop uses, always visible at the top of the loop zone: the command (`claude` = main, `cc` = wrapper on
   another account), the auth method (login and token are subscriptions; API key is pay per token, drawn as a warning), the
   e-mail (org and plan when known) and, for `cc`, the account it picked. The data is `state.meta.claude_account_info`. */
import { html } from '../../util/preact.js';
import { ctx } from './context.js';

const METHODS = ['API key', 'login', 'token'];
const METHOD_TITLE = {
  login: 'login: signed in with the claude.ai browser login (subscription)',
  token: 'token: OAuth token from `claude setup-token` (subscription)',
  'API key': 'API key: billed per token, not by the subscription',
};

/** "login", "token" or "API key" from the start of an account label ('' when the label does not start with one). */
export function methodOf(label) {
  const text = String(label || '').trim();
  return METHODS.find((method) => text.startsWith(method)) || '';
}

/** Small tag with the auth method; the API key one is the warning colour. Nothing when the method is unknown. */
export function MethodTag({ method }) {
  if (!method) return null;
  const kind = method === 'API key' ? 'key' : method;
  return html`<span class=${'z-method mono is-' + kind.replace(' ', '-')} title=${METHOD_TITLE[method] || method}>${method}</span>`;
}

/** The text of the chip, e.g. "claude · login · desenv10@x.com (iData, team)" or "cc · token · account claude2 · 2 switches". */
function chipParts(info) {
  const extra = [info.org, info.plan].filter(Boolean).join(', ');
  const who = info.email ? info.email + (extra ? ` (${extra})` : '') : '';
  const switches = Number(info.switches) || 0;
  return [info.command || '—', info.method, who, info.cc_account ? 'account ' + info.cc_account : '',
    switches ? `${switches} ${switches === 1 ? 'switch' : 'switches'}` : ''].filter(Boolean);
}

/** The whole tooltip: every known field on its own line. */
function chipTitle(info) {
  const lines = [`command: ${info.command || '—'}${info.role ? ' (' + (info.role === 'main' ? 'main' : 'fallback') + ')' : ''}`];
  if (info.method) lines.push('method: ' + (METHOD_TITLE[info.method] || info.method));
  if (info.email) lines.push('e-mail: ' + info.email);
  if (info.org) lines.push('organization: ' + info.org);
  if (info.plan) lines.push('plan: ' + info.plan);
  if (info.cc_account) lines.push('cc account: ' + info.cc_account);
  if (info.switches) lines.push('account switches in this run: ' + info.switches);
  return lines.join('\n');
}

/** Fixed chip of the loop zone. Before the first turn it shows the configured main command (from the diagnostics), or "—". */
export function ClaudeAccountChip() {
  const info = (ctx.state && ctx.state.meta && ctx.state.meta.claude_account_info) || {};
  const parts = chipParts(info);
  const warning = info.method === 'API key';
  const kind = info.method ? info.method.replace(' ', '-') : 'unknown';
  return html`<div class="z-claude-row"><span class="z-figure-k">CLAUDE ACCOUNT</span>
    <span class=${'z-claude-chip mono is-' + kind} title=${chipTitle(info)} role=${warning ? 'alert' : null}>${warning ? '⚠ ' : ''}${parts.join(' · ')}</span></div>`;
}
