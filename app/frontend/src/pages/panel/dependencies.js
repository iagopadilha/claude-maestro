/* "Dependencies" tab: state machine with counts (state.counts), feature graph by stage
   (plain SVG) and, when the back end offers GET graph, the mermaid text as copyable blocks. */
import { html, useEffect } from '../../util/preact.js';
import { list } from '../../util/format.js';
import { copyText } from '../../components/copy.js';
import { FeatureGraph, StateMachine, newGraphView } from '../../components/graph.js';
import { ctx, ui, features, featById, focusFeatureId } from './context.js';
import { selectFeature } from './roadmap_sidebar.js';
import { renderAll } from '../project_panel.js';

export function depInfo(f) {
  const byId = featById();
  const rawDep = Array.isArray(f.depends_on) ? f.depends_on.map((d) => d.id) : list(f.deps).filter((d) => !String(d).startsWith('dir:'));
  const dep = rawDep.map((id) => ({ id, status: byId[id] ? byId[id].status : (list(f.depends_on).find((d) => d.id === id) || {}).status || null, known: !!byId[id] }));
  const dirs = Array.isArray(f.dep_dirs) ? f.dep_dirs : list(f.deps).filter((d) => String(d).startsWith('dir:')).map((d) => String(d).slice(4));
  const unblocks = Array.isArray(f.unblocks) ? f.unblocks : features().filter((x) => list(x.deps).includes(f.id) || list(x.depends_on).some((d) => d.id === f.id)).map((x) => x.id);
  const waiting = Array.isArray(f.waiting_on) ? f.waiting_on : dep.filter((d) => d.known && d.status !== 'verified').map((d) => d.id);
  const blockedBy = Array.isArray(f.blocked_by) ? f.blocked_by : dep.filter((d) => d.status === 'blocked').map((d) => d.id);
  return { dep, dirs, unblocks, waiting, blockedBy };
}
const RUNNING_STATES = ['speccing', 'specced', 'approved', 'building'];
export function readiness(f, info) {
  if (info.blockedBy.length) return ['blocked by ' + info.blockedBy.join(', '), '#F06A5E'];
  if (f.status === 'blocked') return ['blocked (failed)', '#F06A5E'];
  if (f.status === 'verified') return ['verified', '#5BD38A'];
  if (info.waiting.length) return ['waiting on: ' + info.waiting.join(', '), '#F2B544'];
  if (RUNNING_STATES.includes(f.status)) return ['unblocked · in progress', '#5BD38A'];
  return ['ready to run', '#5BD38A'];
}

// kept across tab switches: view/ctl = pan/zoom of the graph (ctl is reached by the zoom buttons), status = highlighted state
const dp = { view: newGraphView(), ctl: {}, status: null, mermaid: null, mermaidKey: undefined, mermaidLoading: false };

function MermaidBlocks({ blocks }) {
  return html`<details>
    <summary>Graph as text <span class="dim-mono">mermaid for PR/GitHub and text for chat; copy and paste</span></summary>
    ${blocks.map((b) => html`<div key=${b.title} class="dep-block"><div class="small-label">${b.title}</div>
      <div class="cmd-row"><pre class="cmd">${b.text}</pre>
        <button type="button" class="btn btn-sm" onClick=${() => copyText(b.text, 'Copied.')}>Copy</button></div></div>`)}
  </details>`;
}

/** Content of #canvas in the "Dependencies" view (div.dep): lifecycle (state machine), feature graph and the mermaid text blocks. */
export function Dependencies() {
  useEffect(() => { loadMermaid(); });
  const status = dp.status, fs = features(), blocks = dp.mermaid || [];
  const pick = (s) => { dp.status = s; renderAll(); };
  return html`<div class="dep">
    <div class="dep-top">
      <div class="dep-top-t"><span class="small-label">Feature lifecycle</span>
        <span class="dep-note" aria-live="polite">${status ? `highlighting the features in "${status}" · click again to clear` : null}</span></div>
      <${StateMachine} counts=${ui.past ? countsOf(fs) : ctx.state && ctx.state.counts} chosen=${status} onPick=${pick} />
    </div>
    <div class="dep-graph"><${FeatureGraph} features=${fs} stages=${ctx.state && ctx.state.stages} selected=${focusFeatureId()}
      status=${status} onSelect=${selectFeature} view=${dp.view} ctl=${dp.ctl} /></div>
    <div class="dep-mermaid" hidden=${!blocks.length}>${blocks.length ? html`<${MermaidBlocks} blocks=${blocks} />` : null}</div>
  </div>`;
}

/** GET graph = {dependencies_mermaid, states_mermaid, text}; plain text is accepted too. */
function graphBlocks(r) {
  if (!r) return [];
  if (typeof r === 'string') return r.trim() ? [{ title: 'Mermaid', text: r }] : [];
  return [
    ['Dependencies (mermaid)', r.dependencies_mermaid || r.mermaid],
    ['State machine (mermaid)', r.states_mermaid],
    ['Plain text (chat and e-mail)', r.text],
  ].filter(([, t]) => typeof t === 'string' && t.trim()).map(([title, text]) => ({ title, text }));
}
async function loadMermaid() {
  const key = String(ctx.state && ctx.state.meta ? ctx.state.meta.turns : null) + '|' + list(ctx.state && ctx.state.features).map((f) => f.id + f.status).join(',');
  if (dp.mermaidLoading || dp.mermaidKey === key) return;
  dp.mermaidLoading = true; dp.mermaidKey = key;
  try {
    dp.mermaid = graphBlocks(await ctx.api.get('graph', 15000));
  } catch (_) {
    dp.mermaid = []; // without the route (or without a roadmap) the block simply does not show
  } finally {
    dp.mermaidLoading = false;
    renderAll();
  }
}

function countsOf(fs) {
  const c = { total: fs.length };
  for (const f of fs) c[f.status] = (c[f.status] || 0) + 1;
  return c;
}
export function zoomDependencies(f) { if (dp.ctl.zoom) dp.ctl.zoom(f); }
export function fitDependencies() { if (dp.ctl.fit) dp.ctl.fit(); }
export function dependenciesScale() { return dp.ctl.scale ? dp.ctl.scale() : 1; }
