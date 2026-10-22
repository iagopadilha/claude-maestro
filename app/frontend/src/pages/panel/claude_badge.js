/* Which command and sign-in a turn (and every agent inside it) ran with, in plain words. Shared by the account chip, the turn strip, the
   turn header, the pipeline diagram, the feature dialog and the agent dialog.
   Short form (badge): "claude · login", "cc · token", "API key" (warning colour: it is billed per token). Long form (tooltip and chip):
   "subscription · browser login", "subscription · sign-in token", "API key · billed per token".
   Data: the fields `claude`, `auth_method` ("login" | "token" | "api_key" | ""), `auth_email`, `auth_plan`, `cc_account` that the back end
   puts on every turn and, inherited from its turn, on every agent. Turns logged before those fields still carry `claude_account`, read here. */
import { html, useState } from '../../util/preact.js';
import { ctx } from './context.js';

export const METHOD_WORDS = { login: 'subscription · browser login', token: 'subscription · sign-in token', api_key: 'API key · billed per token' };
const METHOD_SHORT = { login: 'login', token: 'token', api_key: 'API key' };
const LABEL_WORDS = { login: 'login', token: 'token', 'API key': 'api_key', api_key: 'api_key' };
const DEFAULT_MAX_PARALLEL = 3;

/** "claude", "cc": the command of a turn without its folder and extension ('' when it was not recorded). */
export function commandOf(source) {
  const path = String((source && (source.claude || source.command)) || '');
  return path.split(/[\\/]/).pop().replace(/\.(cmd|bat|exe|py)$/i, '');
}

/** "login" | "token" | "api_key" | '' from `auth_method`, else from the start of the account label of an older turn. */
export function methodCodeOf(source) {
  const data = source || {};
  if (data.auth_method && LABEL_WORDS[data.auth_method]) return LABEL_WORDS[data.auth_method];
  const label = String(data.claude_account || data.method || '').trim();
  const word = Object.keys(LABEL_WORDS).find((candidate) => label.startsWith(candidate));
  return word ? LABEL_WORDS[word] : '';
}

/** The same long wording written as a sentence ("subscription, browser login"). */
export const methodSentence = (code) => (METHOD_WORDS[code] || '').replace(' · ', ', ');

/** What a badge shows for one turn or agent: { command, method, short, words, warning, title }; null when nothing is known. */
export function describeClaude(source, { inherited = false } = {}) {
  const command = commandOf(source), method = methodCodeOf(source);
  if (!command && !method) return null;
  const data = source || {};
  const short = method === 'api_key' ? METHOD_SHORT.api_key : [command, METHOD_SHORT[method]].filter(Boolean).join(' · ');
  const who = data.auth_email || (data.cc_account ? 'cc account ' + data.cc_account : '');
  const lines = [`command: ${command || 'not recorded'}`, `sign-in: ${METHOD_WORDS[method] || 'unknown'}`];
  if (who) lines.push('account: ' + who);
  if (data.auth_plan) lines.push('plan: ' + data.auth_plan);
  if (inherited) lines.push('', 'An agent runs inside the process of its turn, so it uses the same command, account and sign-in as the turn.');
  return { command, method, short, words: METHOD_WORDS[method] || '', warning: method === 'api_key', title: lines.join('\n') };
}

/** Compact badge. `inherited` = an agent's badge (smaller and quieter: the agent has no sign-in of its own). Nothing when unknown. */
export function ClaudeBadge({ source, inherited = false }) {
  const info = describeClaude(source, { inherited });
  if (!info) return null;
  return html`<span class=${'z-cbadge mono is-' + (info.method || 'unknown') + (inherited ? ' inherited' : '')} title=${info.title}>${info.warning ? '⚠ ' : ''}${info.short}</span>`;
}

/** Text of the "who spawns the agents" note; the limit comes from the ROADMAP header (`Max parallel tasks`) when the panel state has it. */
export function whoSpawnsLines() {
  const meta = (ctx.state && ctx.state.meta) || {};
  const limit = Number(meta.max_parallel_tasks) || DEFAULT_MAX_PARALLEL;
  const limitText = meta.max_parallel_tasks_in_header ? `${limit} (Max parallel tasks, in the ROADMAP header)` : `${limit} (the default of Max parallel tasks)`;
  return [
    'The orchestrator (Opus, one session per turn) is always the one that spawns every agent. No agent spawns another agent.',
    `Executors go in waves of up to ${limitText}; fewer when the feature has fewer independent tasks or when the turn budget or minutes are running short.`,
    "Every agent of a turn runs inside that turn's process, so it uses the turn's account and sign-in method.",
  ];
}

/** Small "?" button that opens the explanation under it; closed by default so the diagram stays clean. */
export function WhoSpawnsToggle() {
  const [open, setOpen] = useState(false);
  return html`<span class=${'z-who' + (open ? ' is-open' : '')}>
    <button type="button" class="z-who-btn" aria-expanded=${String(open)} aria-label="Who spawns the agents?" title="Who spawns the agents?" onClick=${() => setOpen(!open)}>?</button>
    ${open ? html`<div class="z-who-text" role="note">${whoSpawnsLines().map((line, index) => html`<p key=${index}>${line}</p>`)}</div>` : null}
  </span>`;
}

const money = (value) => '$' + (Number(value) || 0).toFixed(2);
const plural = (count, word) => `${count} ${word}${count === 1 ? '' : 's'}`;

/** Turns, panel calls and cost per sign-in method and per command (the back end computes them: `state.meta.claude_totals`). */
export function ClaudeTotals() {
  const totals = ctx.state && ctx.state.meta ? ctx.state.meta.claude_totals : null;
  if (!totals || (!totals.turns && !totals.calls)) return null;
  const line = (label, row) => html`<div key=${label} class="z-totals-row"><span>${label}</span><span class="mono">${plural(row.turns, 'turn')} · ${plural(row.calls, 'panel call')} · ${money(row.cost_usd)}</span></div>`;
  return html`<div class="z-totals" aria-label="Usage by sign-in method and command">
    <div class="z-figure-k">BY SIGN-IN</div>${totals.by_method.map((row) => line(METHOD_WORDS[row.method] || 'sign-in unknown', row))}
    <div class="z-figure-k">BY COMMAND</div>${totals.by_command.map((row) => line(row.command, row))}
  </div>`;
}
