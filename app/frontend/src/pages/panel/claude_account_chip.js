/* Which Claude account the loop uses, always visible at the top of the loop zone: the command (`claude` = main, `cc` = wrapper on
   another account), the sign-in in words ("subscription, browser login", "subscription, sign-in token"; an API key is billed per token
   and is drawn as a warning), the e-mail (organization and plan when known) and, for `cc`, the account it picked. The data is
   `state.meta.claude_account_info`; the usage per sign-in method and per command opens from the "usage" button. */
import { html, useState } from '../../util/preact.js';
import { ctx } from './context.js';
import { ClaudeTotals, METHOD_WORDS, methodCodeOf, methodSentence } from './claude_badge.js';

/** The text of the chip, e.g. "claude · dev@x.com · subscription, browser login (iData, team)" or "cc · cc account claude2 · subscription, sign-in token · 2 switches". */
function chipParts(info, method) {
  const extra = [info.org, info.plan].filter(Boolean).join(', ');
  const who = info.email || (info.cc_account ? 'cc account ' + info.cc_account : '');
  const how = method ? methodSentence(method) + (extra ? ` (${extra})` : '') : (extra ? `(${extra})` : '');
  const switches = Number(info.switches) || 0;
  return [info.command || '—', who, how, switches ? `${switches} ${switches === 1 ? 'switch' : 'switches'}` : ''].filter(Boolean);
}

/** The whole tooltip: every known field on its own line. */
function chipTitle(info, method) {
  const lines = [`command: ${info.command || '—'}${info.role ? ' (' + (info.role === 'main' ? 'main' : 'fallback') + ')' : ''}`];
  if (method) lines.push('sign-in: ' + METHOD_WORDS[method]);
  if (info.email) lines.push('e-mail: ' + info.email);
  if (info.org) lines.push('organization: ' + info.org);
  if (info.plan) lines.push('plan: ' + info.plan);
  if (info.cc_account) lines.push('cc account: ' + info.cc_account);
  if (info.switches) lines.push('account switches in this run: ' + info.switches);
  return lines.join('\n');
}

/** Fixed chip of the loop zone. Before the first turn it shows the configured main command (from the diagnostics), or "—". */
export function ClaudeAccountChip() {
  const [usageOpen, setUsageOpen] = useState(false);
  const info = (ctx.state && ctx.state.meta && ctx.state.meta.claude_account_info) || {};
  const method = methodCodeOf(info);
  const warning = method === 'api_key';
  const totals = ctx.state && ctx.state.meta ? ctx.state.meta.claude_totals : null;
  const hasUsage = !!totals && (totals.turns > 0 || totals.calls > 0);
  return html`<div class="z-claude-row"><span class="z-figure-k">CLAUDE ACCOUNT</span>
    <span class=${'z-claude-chip mono is-' + (method || 'unknown')} title=${chipTitle(info, method)} role=${warning ? 'alert' : null}>${warning ? '⚠ ' : ''}${chipParts(info, method).join(' · ')}</span>
    ${hasUsage ? html`<button type="button" class="z-who-btn z-usage-btn" aria-expanded=${String(usageOpen)} title="Turns and cost per sign-in method and per command" onClick=${() => setUsageOpen(!usageOpen)}>usage ${usageOpen ? '▴' : '▾'}</button>` : null}
    ${hasUsage && usageOpen ? html`<${ClaudeTotals} />` : null}</div>`;
}
