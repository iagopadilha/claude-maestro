/* Tabs of the LOOP zone that describe ONE turn (the selected one): decision and events, files changed, failures.
   They read the same pipeline model as the diagram (pipeline_model.js) and open the agent dialog on click. */
import { html } from '../../util/preact.js';
import { Markdown } from '../../components/markdown.js';
import { pipe, loadPipeline, steps, curN, turnAt, turnFailures, emptyReason, STEP_COLOR } from './pipeline_model.js';
import { openAgentDialog } from './dialogs/index.js';
import { FeatureLabel } from './feature_label.js';
import { ProblemsView } from './problems.js';
import { TurnDecisions } from './decisions.js';

const agentNameOf = (event) => event.agent || (steps()[event.step] && steps()[event.step].agent) || 'agent';
const openEvent = (event) => { if (event.feature && event.step >= 0) openAgentDialog({ feature: event.feature, step: event.step }); };
const onActivateKey = (action) => (event) => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); action(); } };

/** What to show instead of the tab when there is nothing to read yet (loading, error, no turn). */
function Unavailable({ message }) {
  return html`<div class="pl-empty">${message}${pipe.err && !pipe.loading ? html` <button type="button" class="pl-tool-text" onClick=${() => loadPipeline(true)}>Retry</button>` : null}</div>`;
}
/** The selected turn as recorded, or the message explaining why there is none. */
function selectedTurn() {
  const reason = emptyReason();
  if (reason) return { message: reason };
  const turn = turnAt(curN());
  return turn ? { turn } : { message: 'No turn to show.' };
}

/** "Decision & events" tab: what the orchestrator decided in the turn and every agent event it produced. */
export function TurnLog() {
  const { turn, message } = selectedTurn();
  if (!turn) return html`<${Unavailable} message=${message} />`;
  return html`<div class="pl-pane">
    <div class="pl-decision">
      <div class="pl-eyebrow amber">ORCHESTRATOR DECISION</div>
      ${turn.decision ? html`<div class="pl-decision-text md-doc"><${Markdown} source=${String(turn.decision)} /></div>` : html`<div class="pl-muted">No decision text recorded for this turn.</div>`}
    </div>
    <div class="pl-eyebrow spaced">TURN EVENTS</div>
    ${turn.events.length ? html`<div class="pl-events">
      ${turn.events.map((event, index) => {
        const clickable = !!event.feature && event.step >= 0;
        return html`<div key=${index} class=${'pl-event' + (clickable ? ' clickable' : '')} role=${clickable ? 'button' : null} tabindex=${clickable ? '0' : null}
          onClick=${() => openEvent(event)} onKeyDown=${onActivateKey(() => openEvent(event))}>
          <span class="pl-event-agent">${agentNameOf(event)}</span>
          <span class="pl-event-feature">${event.feature ? html`<${FeatureLabel} slug=${event.feature} stacked=${true} />` : '—'}</span>
          <span class="pl-event-status" style=${{ color: STEP_COLOR[event.status] || STEP_COLOR.PENDING }}>${event.status}</span>
          <span class="pl-event-out">${event.out}</span>
        </div>`;
      })}
    </div>` : html`<div class="pl-muted">No events recorded in this turn.</div>`}
    <${TurnDecisions} turn=${turn.n} />
  </div>`;
}

/** "Files" tab: files changed by the commits of the turn (empty when the server could not find them). */
export function TurnFiles() {
  const { turn, message } = selectedTurn();
  if (!turn) return html`<${Unavailable} message=${message} />`;
  return html`<div class="pl-pane">
    <div class="pl-eyebrow">FILES CHANGED IN THIS TURN</div>
    ${turn.files.length ? html`<div class="pl-files">
      ${turn.files.map((file) => html`<div key=${file.path} class="pl-file">
        <span class="pl-file-kind">M</span><span class="pl-file-path" title=${file.path}>${file.path}</span>
        <span class="pl-file-add">+${file.add == null ? 0 : file.add}</span><span class="pl-file-del">−${file.del == null ? 0 : file.del}</span>
      </div>`)}
    </div>` : html`<div class="pl-muted">No file changes recorded for this turn.</div>`}
  </div>`;
}

/** "Errors" tab: the problems of THIS turn with the same Errors | Guard blocks split as the GLOBAL zone (problems.js):
    the failed agent steps and the error-center items of the turn, apart from the guard blocks of the turn. */
export function TurnErrors() {
  const { turn, message } = selectedTurn();
  if (!turn) return html`<${Unavailable} message=${message} />`;
  return html`<div class="pl-pane pl-problems"><${ProblemsView} turn=${turn.n} /></div>`;
}
