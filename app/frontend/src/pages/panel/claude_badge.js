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

const WINDOW_WORDS = { five_hour: '5-hour', seven_day: 'weekly' };
const pad = (number) => String(number).padStart(2, '0');
const WEEKDAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];

/** "14:30" for a reset today, "Mon 27/10 09:00" for another day (local time of the browser, from the epoch the loop recorded). */
export function resetText(epochSeconds) {
  if (!epochSeconds) return '';
  const moment = new Date(epochSeconds * 1000), today = new Date();
  const time = `${pad(moment.getHours())}:${pad(moment.getMinutes())}`;
  return moment.toDateString() === today.toDateString() ? time : `${WEEKDAYS[moment.getDay()]} ${pad(moment.getDate())}/${pad(moment.getMonth() + 1)} ${time}`;
}

/** "5-hour 60 % (reset 14:30)" or "weekly 100 % - exhausted (reset Mon 27/10 09:00)" for one window ({key, label, pct, reset_at, exhausted}). */
export function windowText(window) {
  const label = window.label || WINDOW_WORDS[window.key] || String(window.key).replace(/_/g, ' ');
  const reset = resetText(window.reset_at);
  return `${label} ${Math.round(window.pct * 10) / 10} %${window.exhausted || window.pct >= 100 ? ' · exhausted' : ''}${reset ? ` (reset ${reset})` : ''}`;
}

/** The windows of one turn (`usage_windows`, an object keyed by window) as tooltip lines; [] when the turn has none. */
function turnWindowLines(data) {
  const windows = data.usage_windows || {};
  const keys = Object.keys(windows).sort((a, b) => (a === 'five_hour' ? -1 : b === 'five_hour' ? 1 : a === 'seven_day' ? -1 : b === 'seven_day' ? 1 : 0));
  return keys.map((key) => windowText({ ...windows[key], key, exhausted: windows[key].pct >= 100 }));
}

/** Usage line of the account row ("read 12 min ago"), or "usage unknown". */
export function ageText(seconds) {
  if (seconds === null || seconds === undefined) return '';
  const minutes = Math.floor(seconds / 60);
  return minutes < 1 ? 'read just now' : minutes < 120 ? `read ${minutes} min ago` : `read ${Math.floor(minutes / 60)} h ago`;
}

/** Tooltip lines for one command/account row of `state.meta.claude_usage_view`: its windows, or "usage unknown". */
export function usageViewLines(row) {
  const who = row.cc_account ? `${row.command} (cc account ${row.cc_account})` : row.command;
  if (!row.windows || !row.windows.length) return [`${who}: usage unknown`];
  return [`${who}: ${row.windows.map(windowText).join(' · ')}${ageText(row.age_seconds) ? ' · ' + ageText(row.age_seconds) : ''}`];
}

/** Every command/account with its 5-hour and weekly usage; a window at 100 % is drawn in the warning colour. Nothing without rows. */
export function ClaudeUsageWindows() {
  const rows = (ctx.state && ctx.state.meta && ctx.state.meta.claude_usage_view) || [];
  if (!rows.length) return null;
  return html`<div class="z-totals" aria-label="Usage windows per command and account">
    <div class="z-figure-k">5-HOUR AND WEEKLY USAGE</div>
    ${rows.map((row) => html`<div key=${row.key} class="z-totals-row"><span>${row.cc_account ? `${row.command} · ${row.cc_account}` : row.command}</span>
      <span class="mono">${row.windows.length ? row.windows.map((window, index) => html`${index ? ' · ' : ''}<span class=${window.exhausted ? 'is-exhausted' : ''}>${windowText(window)}</span>`) : 'usage unknown'}${ageText(row.age_seconds) ? ' · ' + ageText(row.age_seconds) : ''}</span></div>`)}
  </div>`;
}

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
  const windowLines = turnWindowLines(data);
  if (windowLines.length) lines.push('usage after this turn: ' + windowLines.join(' · '));
  if (data.limit_status === 'rejected') lines.push('this turn hit a usage limit');
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
