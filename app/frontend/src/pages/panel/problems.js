/* Problems of the loop with ONE layout everywhere: "Errors" (what really went wrong: loop stops, failed turns, quarantines,
   launch problems and the FAIL/BLOCKED steps of the agents) apart from "Guard blocks" (the guard stopped an agent from
   breaking a rule: it worked, nothing to fix). The GLOBAL zone shows every turn; the LOOP zone shows only the selected turn.
   Data: the error center (GET errors, store ui.errors of errors.js) and the turn events of the pipeline model. */
import { html } from '../../util/preact.js';
import { txt, list } from '../../util/format.js';
import { ui } from './context.js';
import { renderAll } from '../project_panel.js';
import { loadErrors } from './errors.js';
import { steps, turnFailures, STEP_COLOR } from './pipeline_model.js';
import { openErrorDialog, openAgentDialog } from './dialogs/index.js';
import { FeatureLabel } from './feature_label.js';

const SEVERITY_COLOR = { error: '#f06a5e', warning: '#f5b82e', info: '#6b727c' };
const NEUTRAL_COLOR = '#6b727c';
const KIND_LABEL = { loop: 'loop stopped', turn: 'turn', guard: 'guard', feature: 'feature', launch: 'launch', step: 'agent step' };
const GUARD_NOTE = 'The guard stopped an agent from doing something the loop does not allow. It worked: nothing was changed. Many blocks of one rule mean an agent kept retrying.';

const segment = { global: 'errors', turn: 'errors' }; // open segment per scope ('errors' | 'guard'), kept while the panel lives

const isGuard = (item) => item.kind === 'guard';
/** Turns an error-center item happened in: `turns` when the server sends it, else the single `turn`. */
export function itemTurns(item) {
  const turns = list(item.turns).filter((n) => Number.isFinite(n));
  return turns.length ? turns : Number.isFinite(item.turn) ? [item.turn] : [];
}
/** How many times an item happened in turn n (null when unknown). */
function countInTurn(item, n) {
  const counts = item.turn_counts || {};
  if (counts[n] != null || counts[String(n)] != null) return Number(counts[n] != null ? counts[n] : counts[String(n)]);
  const turns = itemTurns(item);
  return turns.length === 1 && turns[0] === n && Number.isFinite(item.count) ? item.count : null;
}

/** { errors, guard, loaded } for the whole loop (turn null) or for one turn: errors = error-center problems (+ the failed
    agent steps of the turn), guard = guard items, newest-relevant first as the server sorts them. */
export function problemsAt(turn) {
  const store = ui.errors, items = list(store.data && store.data.items), loaded = !!store.data;
  const inTurn = (item) => turn == null || itemTurns(item).includes(turn);
  const errors = items.filter((item) => !isGuard(item) && inTurn(item)).map((item) => ({ source: 'center', item }));
  if (turn != null) turnFailures(turn).forEach((event) => errors.push({ source: 'step', event }));
  const guard = items.filter((item) => isGuard(item) && inTurn(item)).map((item) => ({ source: 'center', item })); // the "older history" line has no turn
  return { errors, guard, loaded };
}
/** Count for a tab label: real errors only (guard blocks are never counted as problems). */
export const errorCountAt = (turn) => problemsAt(turn).errors.length;

function TurnChips({ item }) {
  const turns = itemTurns(item);
  if (!turns.length) return html`<span class="pb-chip mono">${KIND_LABEL[item.kind] || txt(item.kind)}</span>`;
  const shown = turns.length > 3 ? [turns[0], '…', turns[turns.length - 1]] : turns;
  return html`<span class="pb-chips">${shown.map((n, index) => html`<span class="pb-chip mono" key=${index}>${n === '…' ? '…' : 'T' + n}</span>`)}</span>`;
}

function CenterRow({ item, turn }) {
  const color = SEVERITY_COLOR[item.severity] || NEUTRAL_COLOR, times = turn == null ? item.count : countInTurn(item, turn);
  const title = isGuard(item) && item.rule ? `${item.rule}${item.profile ? ' (' + item.profile + ')' : ''}` : txt(item.title);
  return html`<button type="button" class="pb-row" onClick=${() => openErrorDialog(item)} title="open what happened and ask the AI">
    <span class="pb-bar" style=${{ background: color }}></span>
    <span class="pb-main"><span class="pb-top"><span class="pb-kind mono">${KIND_LABEL[item.kind] || txt(item.kind)}</span><${TurnChips} item=${item} /></span>
      <span class="pb-title">${title}</span>
      ${isGuard(item) && list(item.attempts).length ? html`<span class="pb-attempt mono" title=${list(item.attempts).join('\n')}>${'tried: ' + list(item.attempts)[0]}</span>` : null}</span>
    ${Number.isFinite(times) && times > 1 ? html`<span class="pb-times mono">${times + '×'}</span>` : null}
  </button>`;
}

function StepRow({ event }) {
  const agent = event.agent || (steps()[event.step] && steps()[event.step].agent) || 'agent';
  return html`<button type="button" class="pb-row" onClick=${() => { if (event.feature && event.step >= 0) openAgentDialog({ feature: event.feature, step: event.step }); }} title="open the agent: its runs and the chat">
    <span class="pb-bar" style=${{ background: STEP_COLOR[event.status] || STEP_COLOR.FAIL }}></span>
    <span class="pb-main"><span class="pb-top"><span class="pb-kind mono">${KIND_LABEL.step}</span><span class="pb-status mono" style=${{ color: STEP_COLOR[event.status] || STEP_COLOR.FAIL }}>${event.status}</span></span>
      <span class="pb-title"><span class="mono">${agent}</span>${event.feature ? html` @ <${FeatureLabel} slug=${event.feature} block=${true} />` : null}</span>
      ${event.out ? html`<span class="pb-out">${event.out}</span>` : null}</span>
  </button>`;
}

/** The problems with the Errors | Guard blocks switch. `turn` = null for the whole loop (GLOBAL), a number for one turn (LOOP). */
export function ProblemsView({ turn = null }) {
  const scope = turn == null ? 'global' : 'turn', store = ui.errors;
  const { errors, guard, loaded } = problemsAt(turn);
  if (!loaded) return html`<div class="empty-note">${store.loading || store.key === undefined ? 'Loading the errors…' : 'Errors unavailable: ' + (store.err || 'no response')}</div>`;
  const open = segment[scope], pick = (key) => { segment[scope] = key; renderAll(); };
  const guardTimes = guard.reduce((sum, { item }) => sum + (turn == null ? (item.count || 0) : (countInTurn(item, turn) || 0)), 0);
  return html`<div class="pb">
    <div class="pb-seg" role="group" aria-label="Kind of problem">
      <button type="button" class=${'pb-seg-btn' + (open === 'errors' ? ' is-on is-error' : '')} aria-pressed=${String(open === 'errors')} onClick=${() => pick('errors')}>${`Errors · ${errors.length}`}</button>
      <button type="button" class=${'pb-seg-btn' + (open === 'guard' ? ' is-on is-guard' : '')} aria-pressed=${String(open === 'guard')} onClick=${() => pick('guard')}>${`Guard blocks · ${guardTimes || guard.length}`}</button>
    </div>
    <div class="pb-scope mono">${turn == null ? 'every turn of the loop' : 'only turn ' + turn}</div>
    ${open === 'errors'
      ? (errors.length ? errors.map((entry, index) => entry.source === 'step' ? html`<${StepRow} key=${'s' + index} event=${entry.event} />` : html`<${CenterRow} key=${entry.item.id} item=${entry.item} turn=${turn} />`)
        : html`<div class="empty-note">${turn == null ? 'No errors. The loop only stops and notifies you for a real problem.' : 'No error in this turn.'}</div>`)
      : html`<div class="pb-note">${GUARD_NOTE}</div>
        ${guard.length ? guard.map(({ item }) => html`<${CenterRow} key=${item.id} item=${item} turn=${turn} />`) : html`<div class="empty-note">${turn == null ? 'No guard block.' : 'No guard block in this turn.'}</div>`}`}
    ${turn == null ? html`<button type="button" class="btn btn-sm btn-ghost pb-refresh" onClick=${() => loadErrors(true)}>refresh</button>` : null}
  </div>`;
}
