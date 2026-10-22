/* Inspector (right column of the LOOP zone, 300 px): "NOW · T<last>" (the orchestrator and the agents running right now: always
   the live loop) and "SELECTED · STATE AT T<N>" (the node picked in the pipeline, as it was at the selected turn). It only
   summarizes: talking to an agent (its history and the chat) happens in the agent dialog, deciding a pause in the pause dialog. */
import { html } from '../../util/preact.js';
import { Markdown, inlineNodes } from '../../components/markdown.js';
import { txt, list } from '../../util/format.js';
import { ctx, ui, pal, STATUS, ROLE_DESC, status, loop, agentById } from './context.js';
import { renderAll } from '../project_panel.js';
import { steps, turnsList, liveN, curN, selectTurn, selectNode, stateAt, activeNode, STEP_COLOR } from './pipeline_model.js';
import { openAgentDialog, openPauseDialog, openFeatureDialog } from './dialogs/index.js';
import { FeatureLabel } from './feature_label.js';

const NEUTRAL_COLOR = '#6b727c';
const ORCHESTRATOR_ID = 'orch';

/** Reads the full log of an agent into ui.agentLog (the chat of that agent and the feature history open it). */
export async function fetchAgentLog(id) {
  const mine = () => ui.sel && ui.sel.kind === 'agent' && ui.sel.id === id;
  try {
    const r = await ctx.api.get('agent/' + encodeURIComponent(id) + '/log', 20000);
    if (mine()) { ui.agentLog = { id, log: list(r.log), truncated: !!r.truncated }; ui.logLoading = null; renderAll(); }
  } catch (e) {
    if (mine()) { ui.agentLog = { id, log: [], err: e.message }; ui.logLoading = null; renderAll(); }
  }
}

// ---------- helpers ----------
/** Index of the pipeline step an agent role belongs to (executor-back -> executor, verifier-flow -> verifier); -1 when none. */
function stepIndexOfRole(role) {
  const wanted = String(role || '');
  return steps().findIndex((step) => wanted === step.agent || wanted.startsWith(step.agent + '-'));
}
const firstFeature = (feature) => String(feature || '').split(',')[0].trim();
const isQuarantined = (slug) => list(ctx.state && ctx.state.quarantine).some((q) => q.id === slug);
const clipText = (text, max) => { const flat = String(text == null ? '' : text).replace(/\s+/g, ' ').trim(); return flat.length > max ? flat.slice(0, max - 1) + '…' : flat; };
const ORCHESTRATOR_CHIPS = String(ROLE_DESC.orchestrator || '').split(' · ').filter(Boolean);
const NOTE_CLIP = 320;

/** A read-only data field (label + recorded text, a few lines at most): not a chat. The full text and the chat live in the dialog. */
function OutputField({ label, empty = 'No decision recorded for this turn.', children }) {
  const has = children != null && children !== false;
  return html`<div class="z-field"><div class="z-field-k mono">${label}</div>
    <div class=${'z-field-text' + (has ? '' : ' z-none')}>${has ? children : empty}</div></div>`;
}

function KeyValue({ k, children }) {
  return html`<span class="z-kv-k mono">${k}</span><span class="z-kv-v mono">${children}</span>`;
}

// ---------- NOW ----------
function OrchestratorCard() {
  const orch = agentById(ORCHESTRATOR_ID), info = STATUS[status()] || STATUS.no_loop;
  const stateText = orch ? txt(orch.state) : info.label.toLowerCase();
  const [color] = orch ? pal(orch.state) : [info.c];
  const note = orch ? (orch.decision || orch.summary || (orch.last_action && orch.last_action.text)) : null;
  const waiting = status() === 'awaiting_user' ? loop().reason : null;
  const text = clipText(note || waiting, NOTE_CLIP);
  return html`<button type="button" class="z-orch" title="select the orchestrator" onClick=${() => { selectTurn(null); selectNode({ orch: true }); }}>
    <span class="z-orch-top"><span class="z-orch-name mono"><span class="z-dot" style=${{ background: color }}></span>orchestrator</span><span class="z-orch-state mono" style=${{ color }}>${stateText}</span></span>
    <span class="z-chips">${ORCHESTRATOR_CHIPS.map((chip) => html`<span class="z-chip mono" key=${chip}>${chip}</span>`)}</span>
    ${text ? html`<span class="z-orch-note">${inlineNodes(text)}</span>` : null}
  </button>`;
}

function RunningList() {
  const running = list(ctx.state && ctx.state.agents).filter((agent) => agent.state === 'running' && agent.id !== ORCHESTRATOR_ID && agent.role !== 'orchestrator');
  const pick = (agent) => { // selects it in the pipeline and opens its dialog (summary, runs, chat)
    selectTurn(null);
    const step = stepIndexOfRole(agent.role), feature = firstFeature(agent.feature);
    if (step >= 0 && feature) { selectNode({ f: feature, s: step }); openAgentDialog({ feature, step }); } else selectNode({ orch: true });
  };
  return html`<div class="z-label">${'RUNNING · ' + running.length}</div>
    <div class="z-running">${running.length ? running.map((agent) => html`<button type="button" class="z-run" key=${agent.id} onClick=${() => pick(agent)}>
        <span class="z-dot z-dot-run"></span><span class="z-run-text"><span class="z-run-agent mono">${txt(agent.role)}</span><span class="z-run-feature">${firstFeature(agent.feature) ? html`<${FeatureLabel} slug=${firstFeature(agent.feature)} stacked=${true} />` : txt(agent.feature)}</span></span></button>`)
      : html`<div class="z-none">No agent running. Waiting for your decision.</div>`}</div>`;
}

// ---------- SELECTED ----------
function OrchestratorSelected({ turnNumber }) {
  const turn = turnsList().find((item) => item.n === turnNumber) || null, quarantined = list(ctx.state && ctx.state.quarantine)[0];
  const [color] = turn ? [turn.dot || NEUTRAL_COLOR] : [NEUTRAL_COLOR];
  return html`<div class="z-sel-head"><span class="z-sel-name mono"><span class="z-dot" style=${{ background: color }}></span>orchestrator</span><span class="z-sel-state mono" style=${{ color }}>${turn ? txt(turn.status) : '—'}</span></div>
    <div class="z-sel-role">${ROLE_DESC.orchestrator}</div>
    <div class="z-kv-grid"><${KeyValue} k="TURN">${turn ? `${turn.n} · ${txt(turn.label)}` : '—'}<//>
      <${KeyValue} k="CALLS">${steps().map((step) => step.agent).join(', ') || '—'}<//></div>
    <${OutputField} label=${'DECISION · T' + turnNumber}>${turn && turn.decision ? html`<div class="md-doc"><${Markdown} source=${String(turn.decision)} /></div>` : null}<//>
    ${quarantined ? html`<button type="button" class="z-btn-wide z-btn-amber" onClick=${() => openPauseDialog(quarantined.id)}>Understand and resolve</button>` : null}`;
}

function StepSelected({ node, turnNumber }) {
  const stepList = steps(), step = stepList[node.s];
  if (!step) return html`<div class="z-none">This step does not exist.</div>`;
  const cells = (turnNumber != null && stateAt(turnNumber)[node.f]) || [], cell = cells[node.s] || { st: 'PENDING', runs: [] };
  const runs = list(cell.runs), lastRun = runs[runs.length - 1] || null;
  const color = STEP_COLOR[cell.st] || NEUTRAL_COLOR;
  const calledBy = node.s === 0 ? 'orchestrator' : stepList[node.s - 1].agent, calls = node.s + 1 < stepList.length ? stepList[node.s + 1].agent : '—';
  return html`<div class="z-sel-head"><span class="z-sel-name mono"><span class="z-dot" style=${{ background: color }}></span>${step.agent}</span><span class="z-sel-state mono" style=${{ color }}>${cell.st}</span></div>
    <div class="z-sel-role">${txt(step.role)}</div>
    <div class="z-kv-grid"><${KeyValue} k="FEATURE"><${FeatureLabel} slug=${node.f} stacked=${true} block=${true} /><//><${KeyValue} k="CALLED BY"><span class="z-link">${calledBy}</span><//>
      <${KeyValue} k="CALLS"><span class="z-link">${calls}</span><//><${KeyValue} k="RUNS">${runs.length}<//></div>
    <${OutputField} label=${lastRun ? 'LAST OUTPUT · T' + lastRun.turn : 'LAST OUTPUT'} empty="Not run in this state yet.">${lastRun && lastRun.out ? inlineNodes(String(lastRun.out)) : null}<//>
    ${isQuarantined(node.f) ? html`<button type="button" class="z-btn-wide z-btn-amber" onClick=${() => openPauseDialog(node.f)}>Understand and resolve</button>` : null}
    <button type="button" class="z-btn-wide" onClick=${() => openAgentDialog({ feature: node.f, step: node.s })}>Open history and ask the agent</button>
    <button type="button" class="z-btn-wide" onClick=${() => openFeatureDialog({ feature: node.f })}>Open feature view</button>`;
}

function SelectedCard() {
  const node = activeNode(), turnNumber = curN();
  return html`<section class="z-card" aria-label="Selected node">
    <div class="z-label">${'SELECTED · STATE AT T' + (turnNumber == null ? '—' : turnNumber)}</div>
    ${!node ? html`<div class="z-none">Click a node of the pipeline to see its state at this turn.</div>`
      : node.orch ? html`<${OrchestratorSelected} turnNumber=${turnNumber} />` : html`<${StepSelected} node=${node} turnNumber=${turnNumber} />`}
  </section>`;
}

/** The whole inspector. `on` = the loop panel shows (state arrived and the project is not in planning mode). */
export function Inspector({ on }) {
  const latest = liveN();
  return html`<aside class="right z-inspector" id="inspector" aria-label="Inspector">
    <div class="insp-body z-insp-body" id="insp-body">
      ${!on ? null : html`<section class="z-card" aria-label="Loop now">
          <div class="z-label">${'NOW · T' + (latest == null ? '—' : latest)}</div>
          <${OrchestratorCard} /><${RunningList} />
        </section>
        <${SelectedCard} />`}
    </div>
  </aside>`;
}
