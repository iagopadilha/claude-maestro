/* "Dependencies" tab: ONE pan/zoom canvas with the lifecycle state machine (counts) over the feature graph by stage
   (plain SVG; the graph owns pan/zoom, the lifecycle layer follows its transform) and, when the back end offers GET graph, the mermaid text as copyable blocks. */
import { html, useEffect, useLayoutEffect, useRef } from '../../util/preact.js';
import { list } from '../../util/format.js';
import { copyText } from '../../components/copy.js';
import { FeatureGraph, MachineContent, newGraphView } from '../../components/graph.js';
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
// auto = the canvas keeps itself fitted/centered (cleared by the first drag, wheel or zoom button; set again by Center, double-click or re-entering the tab)
const dp = { view: newGraphView(), ctl: {}, status: null, mermaid: null, mermaidKey: undefined, mermaidLoading: false, auto: true, host: null, life: null };
// lifecycle layer size in graph units (graph.js draws the machine at 774x148; plus title and total line) and the gap above the graph
const LIFE_W = 774, LIFE_H = 190, LIFE_GAP = 40, FIT_MARGIN = 56, FIT_MIN_SCALE = 0.55, FIT_MAX_SCALE = 1.1;

/** Where the lifecycle layer sits in graph space: above the first stage (centered when the graph is narrower than it). */
function lifecycleOrigin() { return { x: Math.max(0, (dp.view.w - LIFE_W) / 2) * (dp.view.w < LIFE_W * 1.5 ? 1 : 0), y: -(LIFE_H + LIFE_GAP) }; }
/** Everything that must be visible: graph box plus lifecycle layer. */
function contentBounds() {
  const o = lifecycleOrigin(), w = dp.view.w, h = dp.view.h;
  const minX = Math.min(0, o.x), maxX = Math.max(w, o.x + LIFE_W);
  return { minX, minY: o.y, w: maxX - minX, h: Math.max(h, 0) - o.y };
}
/** Mirrors the graph's pan/zoom onto the lifecycle layer. */
function syncLifecycleLayer() {
  const el = dp.life, v = dp.view.view;
  if (!el) return;
  const o = lifecycleOrigin();
  el.style.transform = `translate(${(v.x + o.x * v.s).toFixed(1)}px, ${(v.y + o.y * v.s).toFixed(1)}px) scale(${v.s.toFixed(3)})`;
  el.style.opacity = '1';
}
function fitDependencyCanvas() {
  const host = dp.host, g = dp.view;
  if (!host || !dp.ctl.apply) return;
  const r = host.getBoundingClientRect();
  if (!r.width || !r.height) return;
  const b = contentBounds();
  const s = Math.max(FIT_MIN_SCALE, Math.min((r.width - FIT_MARGIN) / b.w, (r.height - FIT_MARGIN) / b.h, FIT_MAX_SCALE));
  const fits = b.w * s <= r.width - FIT_MARGIN / 2; // too wide at a readable scale: start at the left edge (lifecycle + first stages), the user pans to the rest
  g.view = { s, x: fits ? (r.width - b.w * s) / 2 - b.minX * s : FIT_MARGIN / 2 - b.minX * s, y: (r.height - b.h * s) / 2 - b.minY * s };
  g.manual = true; // keeps the graph's own graph-only fit from running on resize; this module refits instead
  dp.ctl.apply();
  syncLifecycleLayer();
}
/** Refit after the graph finished its own layout pass (it fits itself one frame later). */
function fitDependencyCanvasSoon() {
  requestAnimationFrame(() => requestAnimationFrame(() => { if (dp.auto) fitDependencyCanvas(); }));
}

/** Pan/zoom wiring of the tab: refit on enter, data change and resize; wheel over the lifecycle layer; double-click refits. */
function useDependencyCanvas(dataSignature) {
  const host = useRef(null);
  useLayoutEffect(() => {
    const el = host.current;
    dp.host = el; dp.auto = true;
    const vp = el.querySelector('.gf-vp');
    const follow = typeof MutationObserver === 'function' && vp ? new MutationObserver(syncLifecycleLayer) : null;
    if (follow) follow.observe(vp, { attributes: true, attributeFilter: ['transform'] });
    const resize = typeof ResizeObserver === 'function' ? new ResizeObserver(() => { if (dp.auto) fitDependencyCanvas(); }) : null;
    if (resize) resize.observe(el);
    const stopAuto = () => { dp.auto = false; };
    const dragMove = (e) => { if (e.buttons & 1) stopAuto(); };
    const wheel = (e) => {
      stopAuto();
      if (!e.target.closest || !e.target.closest('.dep-life')) return; // over the graph svg its own handler pans/zooms
      e.preventDefault();
      if (e.ctrlKey || e.metaKey) dp.ctl.zoom(e.deltaY < 0 ? 1.12 : 1 / 1.12, e.clientX, e.clientY);
      else { const v = dp.view.view; dp.view.view = { s: v.s, x: v.x - e.deltaX, y: v.y - e.deltaY }; dp.view.manual = true; dp.ctl.apply(); }
    };
    const refit = (e) => { if (e.target.closest && e.target.closest('.gf-node, .me-node, .dep-mermaid')) return; dp.auto = true; fitDependencyCanvas(); };
    el.addEventListener('pointermove', dragMove); el.addEventListener('wheel', wheel, { passive: false }); el.addEventListener('dblclick', refit);
    fitDependencyCanvasSoon();
    return () => {
      el.removeEventListener('pointermove', dragMove); el.removeEventListener('wheel', wheel); el.removeEventListener('dblclick', refit);
      if (follow) follow.disconnect();
      if (resize) resize.disconnect();
      dp.host = null; dp.life = null;
    };
  }, []);
  useLayoutEffect(() => { if (dp.auto) fitDependencyCanvasSoon(); }, [dataSignature]);
  return host;
}

/** Reading key of the graph: what an arrow means and what each edge color stands for. */
function GraphLegend() {
  return html`<div class="dep-legend" aria-label="Graph legend">
    <span class="dep-legend-item"><i class="dep-key green"></i>A → B: B depends on A (A verified)</span>
    <span class="dep-legend-item"><i class="dep-key amber"></i>A not verified yet, B waits</span>
    <span class="dep-legend-item"><i class="dep-key red"></i>A blocked</span>
    <span class="dep-legend-item">n/m in the header: verified / total</span>
  </div>`;
}

function MermaidBlocks({ blocks }) {
  return html`<details class="dep-details">
    <summary>Text version <span class="dim-mono">mermaid for PR/GitHub and text for chat; copy and paste</span></summary>
    ${blocks.map((b) => html`<div key=${b.title} class="dep-block"><div class="small-label">${b.title}</div>
      <div class="cmd-row"><pre class="cmd">${b.text}</pre>
        <button type="button" class="btn btn-sm" onClick=${() => copyText(b.text, 'Copied.')}>Copy</button></div></div>`)}
  </details>`;
}

/** Content of #canvas in the "Dependencies" view (div.dep): one pan/zoom surface with the lifecycle over the feature graph,
 *  plus the mermaid text as a collapsed corner section. */
export function Dependencies() {
  useEffect(() => { loadMermaid(); });
  const status = dp.status, fs = features(), blocks = dp.mermaid || [];
  const counts = ui.past ? countsOf(fs) : ctx.state && ctx.state.counts;
  const dataSignature = fs.map((f) => f.id + f.status + f.stage).join(',') + '|' + JSON.stringify(counts || {});
  const host = useDependencyCanvas(dataSignature);
  const pick = (s) => { dp.status = s; renderAll(); };
  return html`<div class="dep">
    <div class="dep-graph" ref=${host}>
      <${FeatureGraph} features=${fs} stages=${ctx.state && ctx.state.stages} selected=${focusFeatureId()}
        status=${status} onSelect=${selectFeature} view=${dp.view} ctl=${dp.ctl} />
      <div class="dep-life" ref=${(el) => { dp.life = el; }}>
        <div class="dep-life-t small-label">Feature lifecycle</div>
        <${MachineContent} counts=${counts} chosen=${status} onPick=${pick} />
      </div>
      <span class="dep-note" aria-live="polite">${status ? `highlighting the features in "${status}" · click again to clear` : null}</span>
      ${fs.length ? html`<${GraphLegend} />` : null}
    </div>
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
export function zoomDependencies(f) { if (dp.ctl.zoom) { dp.auto = false; dp.ctl.zoom(f); } }
export function fitDependencies() { dp.auto = true; fitDependencyCanvas(); }
export function dependenciesScale() { return dp.ctl.scale ? dp.ctl.scale() : 1; }
