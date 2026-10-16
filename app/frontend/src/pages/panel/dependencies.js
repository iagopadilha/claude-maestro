/* "Dependencies" tab: state machine with counts (state.counts), feature graph by stage
   (plain SVG) and, when the back end offers GET graph, the mermaid text as copyable blocks. */
import { h, fill } from '../../util/dom.js';
import { list } from '../../util/format.js';
import { copyText } from '../../components/copy.js';
import { createFeatureGraph, createStateMachine } from '../../components/graph.js';
import { ctx, ui, features, featById, focusFeatureId } from './context.js';
import { selectFeature } from './roadmap_sidebar.js';

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

const dp = { root: null, graph: null, machine: null, mermaid: null, mermaidKey: undefined, mermaidLoading: false, mermaidBox: null, note: null };

function build() {
  dp.graph = createFeatureGraph({ onSelect: (id) => selectFeature(id) });
  dp.machine = createStateMachine({ onPick: (s) => { dp.graph.highlightStatus(s); dp.note.textContent = s ? `highlighting the features in "${s}" · click again to clear` : ''; } });
  dp.note = h('span', { class: 'dep-note', 'aria-live': 'polite' });
  dp.mermaidBox = h('div', { class: 'dep-mermaid', hidden: true });
  dp.root = h('div', { class: 'dep' },
    h('div', { class: 'dep-top' },
      h('div', { class: 'dep-top-t' }, h('span', { class: 'small-label', text: 'Feature lifecycle' }), dp.note),
      dp.machine.element),
    h('div', { class: 'dep-graph' }, dp.graph.element),
    dp.mermaidBox);
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
    drawMermaid();
  }
}
function drawMermaid() {
  if (!dp.mermaidBox) return;
  const blocks = dp.mermaid || [];
  dp.mermaidBox.hidden = !blocks.length;
  fill(dp.mermaidBox, blocks.length ? h('details', {},
    h('summary', {}, 'Graph as text ', h('span', { class: 'dim-mono', text: 'mermaid for PR/GitHub and text for chat; copy and paste' })),
    blocks.map((b) => h('div', { class: 'dep-block' }, h('div', { class: 'small-label', text: b.title }),
      h('div', { class: 'cmd-row' }, h('pre', { class: 'cmd', text: b.text }),
        h('button', { type: 'button', class: 'btn btn-sm', onclick: () => copyText(b.text, 'Copied.') }, 'Copy'))))) : null);
}

/** Builds/updates the tab and returns the root element (the center shows it full size). */
export function dependenciesView() {
  if (!dp.root) build();
  dp.machine.update(ui.past ? countsOf(features()) : ctx.state && ctx.state.counts);
  dp.graph.update({ features: features(), stages: ctx.state && ctx.state.stages, selected: focusFeatureId() });
  loadMermaid();
  return dp.root;
}
function countsOf(fs) {
  const c = { total: fs.length };
  for (const f of fs) c[f.status] = (c[f.status] || 0) + 1;
  return c;
}
export function zoomDependencies(f) { if (dp.graph) dp.graph.zoom(f); }
export function fitDependencies() { if (dp.graph) dp.graph.fit(); }
export function dependenciesScale() { return dp.graph ? dp.graph.scale() : 1; }
