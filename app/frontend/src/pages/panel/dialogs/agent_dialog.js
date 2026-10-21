/* Agent dialog: everything one agent (a step of the fixed flow) did in one feature, across all turns, plus a chat with that agent.
   Facts come from the pipeline model (events replayed per turn); the full transcript of a run is read on demand from
   `GET agent/<agent_id>/log`. The chat is about the latest run of the step; a step that never ran chats about the project. */
import { html, useState } from '../../../util/preact.js';
import { list, txt } from '../../../util/format.js';
import { openBigDialog } from '../../../components/big_dialog.js';
import { ctx } from '../context.js';
import { stepRuns, steps, stateAt, curN, featureName } from '../pipeline_model.js';
import { FeatureLabel } from '../feature_label.js';

const BADGE_COLOR = '#7fb2ff';
const ORCHESTRATOR = 'orchestrator';
const LOG_TIMEOUT_MS = 20000;
const RUN_STATUS_COLOR = { DONE: '#3fb950', RUNNING: '#f5b82e', QUEUED: '#7fb2ff', FAIL: '#f85149', BLOCKED: '#f85149', PENDING: '#6b727c' };
const LOG_TYPE_COLOR = { sys: '#8b929c', ai: '#e6e8eb', tool: '#7fb2ff', guard: '#f85149', user: '#f5b82e' };
const NOT_RUN_MESSAGE = 'This step has not run yet: it is pending in the flow.';

const statusColor = (status) => RUN_STATUS_COLOR[status] || '#8b929c';
/** Newest turn first; inside one turn the later run goes on top (stepRuns is chronological). */
const newestFirst = (runs) => [...list(runs)].reverse().sort((first, second) => second.turn - first.turn);

/** Status of the step at the selected turn (state replayed up to curN()); PENDING when the pipeline has nothing for it. */
function stepStatus(feature, step) {
  const turn = curN();
  const byFeature = turn == null ? null : stateAt(turn)[feature];
  const current = byFeature ? byFeature[step] : null;
  return current && current.st ? current.st : 'PENDING';
}

function RunLog({ log }) {
  if (log.loading) return html`<div class="bd-dim-note">loading the log…</div>`;
  if (log.error) return html`<div class="bd-inline-error" role="alert">${'Log unavailable: ' + log.error}</div>`;
  if (!log.lines.length) return html`<div class="bd-dim-note">The log of this run is empty.</div>`;
  return html`<div class="bd-log">${log.lines.map((line, index) => html`<div key=${index} class="bd-log-line">
      <span class="bd-log-type" style=${{ color: LOG_TYPE_COLOR[line.type] || '#8b929c' }}>${txt(line.type)}</span>
      <span class="bd-log-text">${txt(line.text)}${line.result ? html`<span class="bd-log-result">${String(line.result)}</span>` : null}</span>
    </div>`)}${log.truncated ? html`<div class="bd-dim-note">log truncated by the server</div>` : null}</div>`;
}

function RunCard({ run }) {
  const [log, setLog] = useState(null); // null = not asked; otherwise { loading, error, lines, truncated, open }
  async function loadLog() {
    setLog({ loading: true, error: null, lines: [], truncated: false, open: true });
    try {
      const response = await ctx.api.get('agent/' + encodeURIComponent(run.agent_id) + '/log', LOG_TIMEOUT_MS);
      setLog({ loading: false, error: null, lines: list(response.log), truncated: !!response.truncated, open: true });
    } catch (error) {
      setLog({ loading: false, error: error.message, lines: [], truncated: false, open: true });
    }
  }
  function toggleLog() {
    if (!log || log.error) { loadLog(); return; } // first click, or retry after an error
    if (!log.loading) setLog({ ...log, open: !log.open });
  }
  return html`<div class="bd-run">
    <div class="bd-run-head"><span class="bd-turn">${'T' + run.turn}</span><span class="bd-mono" style=${{ color: statusColor(run.st) }}>${txt(run.st)}</span>
      <span class="bd-spacer"></span>
      ${run.agent_id ? html`<button type="button" class="bd-link" disabled=${!!(log && log.loading)} onClick=${toggleLog}>${log && log.open ? 'hide the full log' : 'load the full log'}</button>` : null}</div>
    <div class="bd-run-out">${run.out ? run.out : 'No output recorded.'}</div>
    ${log && log.open ? html`<${RunLog} log=${log} />` : null}
  </div>`;
}

function AgentBody({ feature, step }) {
  const definitions = steps(), definition = definitions[step] || {};
  const runs = newestFirst(stepRuns(feature, step));
  const status = stepStatus(feature, step);
  const calledBy = step === 0 ? ORCHESTRATOR : (definitions[step - 1] || {}).agent || ORCHESTRATOR;
  const handsOffTo = (definitions[step + 1] || {}).agent || ORCHESTRATOR;
  const facts = [
    { label: 'STATUS', value: status, color: statusColor(status) },
    { label: 'RUNS', value: String(runs.length), color: '#e6e8eb' },
    { label: 'CALLED BY', value: calledBy, color: BADGE_COLOR },
    { label: 'HANDS OFF TO', value: handsOffTo, color: BADGE_COLOR },
  ];
  return html`<div class="bd-agent">
    ${definition.role ? html`<div class="bd-agent-role">${definition.role}</div>` : null}
    <div class="bd-facts-grid">${facts.map((fact) => html`<div key=${fact.label} class="bd-fact">
      <div class="bd-label">${fact.label}</div><div class="bd-fact-value" style=${{ color: fact.color }}>${fact.value}</div></div>`)}</div>
    <div class="bd-section-label">RUNS OF THIS AGENT IN THIS FEATURE</div>
    <div class="bd-runs">${runs.length ? runs.map((run, index) => html`<${RunCard} key=${(run.agent_id || 'run') + ':' + index} run=${run} />`)
      : html`<div class="bd-dim-note">${NOT_RUN_MESSAGE}</div>`}</div>
  </div>`;
}

/** Opens the dialog of one step (index in the fixed flow) of one feature. */
export function openAgentDialog({ feature, step }) {
  const definition = steps()[step] || {};
  const agentName = definition.agent || 'agent';
  const newestRun = newestFirst(stepRuns(feature, step))[0];
  const scope = newestRun && newestRun.agent_id
    ? { type: 'agent', agent_id: newestRun.agent_id, turn: newestRun.turn }
    : { type: 'general', agent_id: null, turn: null };
  return openBigDialog({
    badge: 'AGENT', badgeColor: BADGE_COLOR, title: html`${agentName} @ <${FeatureLabel} slug=${feature} />`,
    body: () => html`<${AgentBody} feature=${feature} step=${step} />`,
    chat: {
      title: 'Ask ' + agentName, placeholder: 'Ask something about this run…', scope,
      greeting: newestRun && newestRun.agent_id
        ? `I am ${agentName} on "${featureName(feature)}". Ask what I did, which files I touched or why I ended with this status.`
        : `${agentName} has not run on "${featureName(feature)}" yet. I can still answer questions about the feature and the project.`,
      suggestions: ['What did you do in this step?', 'Which files did you change?', 'Why this status?'],
    },
  });
}
