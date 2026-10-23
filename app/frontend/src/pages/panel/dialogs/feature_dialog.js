/* Feature dialog: the central place to watch one feature being executed. Header (name, status, stage, wave, deps, paths, current step,
   command and account of the last turn), the step timeline, the task graph (one column per wave, "parallel ×N", "split by the limit of N",
   arrows for task deps, one agent chip per task) and every agent call of the feature. Data: `GET feature/<slug>`, refreshed every
   3 s while the loop runs and the dialog is open (the body unmounts on close, which stops the poll). When the route or some fields are
   missing (older back end) it falls back to the feature in /state: waves from `task.wave`, no arrows, no agent chips claimed. */
import { html, useState, useEffect } from '../../../util/preact.js';
import { list, txt, dur } from '../../../util/format.js';
import { openBigDialog } from '../../../components/big_dialog.js';
import { ctx, pal } from '../context.js';
import { steps, featureName } from '../pipeline_model.js';
import { FeatureLabel } from '../feature_label.js';
import { featureCalls } from '../feature_history.js';
import { retColor } from '../turn_diagram.js';
import { ClaudeBadge, WhoSpawnsToggle, describeClaude } from '../claude_badge.js';
import { openAgentDialog } from './agent_dialog.js';
import { taskGraphOf, waveModeLabel, splitNoteOf, agentChipOf, statusCounts, layoutTaskGraph, agentRowsOf, fallbackSteps } from '../feature_dialog_model.js';

const BADGE_COLOR = '#f5b82e';
const POLL_MS = 3000;
const REQUEST_TIMEOUT_MS = 15000;
const STEP_STATE_COLOR = { done: '#3fb950', running: '#f5b82e', pending: '#6b727c', failed: '#f85149', skipped: '#6b727c' };
const TASK_COLOR = { done: '#3fb950', running: '#f5b82e', failed: '#f85149', pending: '#6b727c' };
const EDGE_COLOR = { done: '#3fb950', running: '#f5b82e', failed: '#f85149', pending: '#3a3e45' };

const stateFeature = (slug) => list(ctx.state && ctx.state.features).find((item) => item && item.id === slug) || null;
const loopIsRunning = () => !!(ctx.state && ctx.state.loop && ctx.state.loop.running);
/** Index of the pipeline step an agent role belongs to (executor-back -> executor); -1 when none. */
function stepIndexOfRole(role) {
  const wanted = String(role || '');
  return steps().findIndex((step) => wanted === step.agent || wanted.startsWith(step.agent + '-'));
}
/** Opens the agent dialog on the run spawned for this task (its log open); without a known step, on the newest run of the role. */
function openTaskAgent(slug, role, agent, task) {
  const step = stepIndexOfRole(role);
  if (step < 0) return;
  const focus = agent && agent.agent_id ? { agent_id: agent.agent_id, turn: agent.turn, st: String(agent.return_status || agent.state || '').toUpperCase(),
    out: task.id ? `Spawned by the orchestrator for task ${task.id} (${task.title || 'no title'}).` : (task.title || 'Spawned by the orchestrator.'),
    task: task.id || null } : null;
  openAgentDialog({ feature: slug, step, focus });
}

/** Reads the feature endpoint now and every POLL_MS while the loop runs. A failed read keeps the last data and shows the error. */
function useFeatureDetail(slug) {
  const [detail, setDetail] = useState({ data: null, error: null, loading: true });
  const running = loopIsRunning();
  useEffect(() => {
    let alive = true;
    async function readFeature() {
      try {
        const data = await ctx.api.get('feature/' + encodeURIComponent(slug), REQUEST_TIMEOUT_MS);
        if (alive) setDetail({ data, error: null, loading: false });
      } catch (error) {
        if (alive) setDetail((previous) => ({ data: previous.data, error: error.message, loading: false }));
      }
    }
    readFeature();
    const timer = running ? setInterval(readFeature, POLL_MS) : null;
    return () => { alive = false; if (timer) clearInterval(timer); };
  }, [slug, running]);
  return detail;
}

function Fact({ label, children }) {
  return html`<div class="bd-fact"><div class="bd-label">${label}</div><div class="bd-fact-value fd-fact-value">${children}</div></div>`;
}

function Header({ slug, detail }) {
  const base = detail || stateFeature(slug) || {}, claude = detail && detail.claude ? detail.claude : null;
  const deps = list(base.deps).map((dep) => (String(dep).startsWith('dir:') ? dep : featureName(dep)));
  const [color] = pal(base.status);
  return html`<div class="bd-facts-grid fd-facts">
    <${Fact} label="STATUS"><span style=${{ color }}>${txt(base.status)}</span><//>
    <${Fact} label="STAGE · WAVE">${txt(base.stage)} · ${txt(base.wave)}<//>
    <${Fact} label="CURRENT STEP">${txt(detail && detail.current_step)}<//>
    <${Fact} label="DEPENDS ON">${deps.length ? deps.join(', ') : 'nothing'}<//>
    <${Fact} label="PATHS">${list(base.paths).length ? list(base.paths).join(', ') : '—'}<//>
    <${Fact} label="LAST TURN RAN WITH">${claude && describeClaude({ ...claude, claude_account: claude.account }) ? html`<${ClaudeBadge} source=${{ ...claude, claude_account: claude.account }} /> ${describeClaude({ ...claude, claude_account: claude.account }).words || ''}` : '—'}<//>
  </div>`;
}

function StepTimeline({ stepList }) {
  return html`<div class="fd-steps" role="list">${stepList.map((item, index) => html`<div key=${item.step} role="listitem" class=${'fd-step ' + item.state}
      style=${{ '--fd-color': STEP_STATE_COLOR[item.state] || STEP_STATE_COLOR.pending }}>
      <span class="fd-step-dot">${item.state === 'running' ? html`<span class="spinner sm"></span>` : item.state === 'done' ? '✓' : item.state === 'failed' ? '✕' : index + 1}</span>
      <span class="fd-step-name">${item.step}</span><span class="fd-step-state">${item.state}</span></div>`)}</div>`;
}

function AgentChip({ slug, task }) {
  const chip = agentChipOf(task);
  if (chip.placeholder) return html`<div class="fd-chip fd-chip-none" title="The orchestrator spawns one executor per task">${chip.placeholder}</div>`;
  const resultColor = chip.result ? retColor(chip.result) : null;
  return html`<button type="button" class=${'fd-chip' + (chip.running ? ' running' : '')} title=${'Open ' + chip.role + ' · ' + chip.agent.agent_id}
    onClick=${() => openTaskAgent(slug, chip.role, chip.agent, task)}>
    <span class="fd-chip-top">${chip.running ? html`<span class="spinner sm"></span>` : html`<span class="fd-chip-dot" style=${{ background: resultColor || '#6b727c' }}></span>`}
      <span class="fd-chip-role"><span class="fd-chip-spawned">spawned</span> ${chip.role}</span><${ClaudeBadge} source=${chip.agent} inherited />
      ${chip.attempts > 1 ? html`<span class="fd-chip-attempts" title="attempts">${chip.attempts}×</span>` : null}</span>
    <span class="fd-chip-bottom">${chip.running ? 'running' : html`<span style=${{ color: resultColor || '#8b929c' }}>${txt(chip.result)}</span>`}${chip.duration ? ' · ' + chip.duration : ''}</span>
  </button>`;
}

function TaskNode({ slug, node, geometry }) {
  const task = node.task, color = TASK_COLOR[task.status] || TASK_COLOR.pending;
  return html`<div class=${'fd-task ' + task.status} style=${{ left: node.x + 'px', top: node.y + 'px', width: geometry.NODE_W + 'px', height: geometry.NODE_H + 'px', '--fd-color': color }}>
    <div class="fd-task-top"><span class="fd-task-id">${task.id}</span><span class="fd-task-status" style=${{ color }}>${task.status}</span>
      ${task.parallel ? html`<span class="fd-task-p" title="marked [P]: allowed to run in parallel">P</span>` : null}</div>
    <div class="fd-task-title" title=${task.note || task.title}>${task.title || '—'}</div>
    <${AgentChip} slug=${slug} task=${task} />
  </div>`;
}

function TaskGraph({ slug, payload }) {
  const graph = taskGraphOf(payload), layout = layoutTaskGraph(graph), counts = statusCounts(graph.tasks);
  if (!graph.tasks.length) return html`<div class="bd-dim-note">This feature has no task list (no tasks.md): it runs as a single executor call.</div>`;
  return html`<div>
    <div class="fd-summary">${graph.tasks.length} tasks in ${graph.waves.length} waves · max ${graph.maxParallel == null ? '?' : graph.maxParallel} in parallel ·
      ${counts.map(([status, total]) => html`<span key=${status} style=${{ color: TASK_COLOR[status] || '#8b929c' }}>${total} ${status}</span>`).reduce((all, part) => (all ? [all, ' · ', part] : part), null)}</div>
    <div class="fd-graph-scroll"><div class="fd-graph" style=${{ width: layout.width + 'px', height: layout.height + 'px' }}>
      <svg class="fd-edges" width=${layout.width} height=${layout.height} aria-hidden="true">
        <defs>${Object.entries(EDGE_COLOR).map(([kind, color]) => html`<marker key=${kind} id=${'fd-arrow-' + kind} viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto"><path d="M0,0 L8,4 L0,8 z" fill=${color} /></marker>`)}</defs>
        ${layout.edges.map((edge) => { const kind = EDGE_COLOR[edge.status] ? edge.status : 'pending';
          return html`<path key=${edge.key} d=${edge.d} fill="none" stroke=${EDGE_COLOR[kind]} stroke-width="1.5" stroke-dasharray=${kind === 'pending' ? '4 4' : edge.skips ? '7 3' : null} marker-end=${`url(#fd-arrow-${kind})`}><title>${edge.from + ' → ' + edge.to + (edge.skips ? ' (skips a wave: it waited for a free slot, not for the tasks in between)' : '')}</title></path>`; })}
      </svg>
      ${layout.columns.map((column) => html`<div key=${column.wave.n} class=${'fd-wave-head' + (column.wave.parallel ? ' parallel' : '')} style=${{ left: column.x + 'px', width: layout.geometry.NODE_W + 'px', top: layout.geometry.PAD + 'px' }}>
        <span class="fd-wave-name">WAVE ${column.wave.n}</span><span class="fd-wave-mode">${waveModeLabel(column.wave)}</span>
        ${splitNoteOf(column.wave, graph.maxParallel) ? html`<span class="fd-wave-split">${splitNoteOf(column.wave, graph.maxParallel)}</span>` : null}</div>`)}
      ${layout.nodes.map((node) => html`<${TaskNode} key=${node.task.id} slug=${slug} node=${node} geometry=${layout.geometry} />`)}
    </div></div>
    ${graph.hasDeps ? null : html`<div class="bd-dim-note">This state has no task dependencies yet: arrows appear when the back end sends them.</div>`}
  </div>`;
}

function AgentCalls({ slug, rows, loading }) {
  if (!rows.length) return html`<div class="bd-dim-note">${loading ? 'loading…' : 'No agent has worked on this feature yet.'}</div>`;
  return html`<div class="fd-calls">${rows.map((row, index) => { const [color] = pal(row.state), resultColor = row.return_status ? retColor(row.return_status) : null;
    return html`<button type="button" key=${(row.id || 'call') + ':' + index} class="fd-call" title=${row.id || ''} onClick=${() => openTaskAgent(slug, row.role, { agent_id: row.id || row.agent_id, turn: row.turn, state: row.state, return_status: row.return_status }, { id: row.task || '', title: row.summary || '' })}>
      <span class="bd-turn">${'T' + txt(row.turn)}</span><span class="fd-call-dot" style=${{ background: color }}></span><span class="fd-call-role">${txt(row.role)}</span>
      ${row.return_status ? html`<span class="fd-call-return" style=${{ color: resultColor, borderColor: resultColor }}>${String(row.return_status)}</span>` : null}
      <span class="fd-call-meta">${txt(row.state)} · ${dur(row.duration_s)}</span><${ClaudeBadge} source=${row} inherited />
      <span class="fd-call-said">${row.summary ? String(row.summary) : ''}</span></button>`; })}</div>`;
}

function FeatureBody({ slug }) {
  const { data, error, loading } = useFeatureDetail(slug);
  const fallback = stateFeature(slug), base = data || fallback || {};
  const payload = (data && data.tasks && !Array.isArray(data.tasks) ? data.tasks : null) || (fallback && fallback.tasks) || null;
  const stepList = data && Array.isArray(data.steps) && data.steps.length ? data.steps : fallbackSteps(base.status);
  const rows = agentRowsOf(featureCalls(slug), data && data.agents);
  return html`<div class="fd">
    ${error ? html`<div class="bd-inline-error" role="alert">${'Live feature data unavailable: ' + error + (fallback ? ' (showing the panel state)' : '')}</div>` : null}
    <${Header} slug=${slug} detail=${data} />
    <div class="bd-section-label">STEPS</div><${StepTimeline} stepList=${stepList} />
    <div class="bd-section-label">TASKS · ONE AGENT SPAWNED PER TASK <${WhoSpawnsToggle} /></div>
    ${payload ? html`<${TaskGraph} slug=${slug} payload=${payload} />` : html`<div class="bd-dim-note">${loading ? 'loading…' : 'No task information for this feature yet.'}</div>`}
    <div class="bd-section-label">${'AGENT CALLS (' + rows.length + ')'}</div><${AgentCalls} slug=${slug} rows=${rows} loading=${loading} />
  </div>`;
}

/** Opens the Feature dialog of one feature slug. */
export function openFeatureDialog({ feature }) {
  return openBigDialog({
    badge: 'FEATURE', badgeColor: BADGE_COLOR, title: html`<${FeatureLabel} slug=${feature} />`,
    body: () => html`<${FeatureBody} slug=${feature} />`,
  });
}
