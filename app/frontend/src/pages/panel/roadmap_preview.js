/* Roadmap preview (planning screen, ROADMAP tab): the SAME FeatureGraph component of the loop (components/graph.js), fed by
   GET roadmap: pan, zoom, fit, keyboard and node selection. Clicking a node shows a details card under the graph. Read-only.
   Its pan/zoom view lives here (module level), apart from the loop panel's. */
import { html, useState, useEffect } from '../../util/preact.js';
import { ctx } from './context.js';
import { computeGraphLayout, FeatureGraph, newGraphView } from '../../components/graph.js';
import { list } from '../../util/format.js';

const NW = 184, NH = 66; // node size of computeGraphLayout
const stageNumber = (s) => { const n = Number(s); return s !== '' && s != null && Number.isFinite(n) ? n : null; };

/** Roadmap endpoint payload -> the shape computeGraphLayout reads (id, status, stage as number, deps as slugs). */
export function previewModel(data) {
  const fs = list(data && data.features).map((f) => ({
    id: f.slug, status: f.status || 'pending', stage: stageNumber(f.stage), kind: f.kind,
    deps: String(f.deps || '').split(',').map((d) => d.trim()).filter((d) => d && !d.startsWith('dir:')),
    regression: !!f.regression && f.regression !== 'no',
  }));
  const stages = list(data && data.stages).map((s) => ({ n: stageNumber(s.n), title: s.title }));
  if (!fs.length) return null;
  const L = computeGraphLayout(fs, stages);
  const pos = Object.fromEntries(L.nodes.map((n) => [n.f.id, n]));
  const edges = L.edges.map((a) => {
    const p1 = pos[a.from], p2 = pos[a.to];
    const x1 = p1.x + NW, y1 = p1.y + NH / 2, x2 = p2.x - 4, y2 = p2.y + NH / 2, dx = Math.max(40, Math.abs(x2 - x1) / 2);
    return { ...a, d: `M${x1} ${y1} C${x1 + dx} ${y1},${x2 - dx} ${y2},${x2} ${y2}` };
  });
  return { L, edges };
}

// kept across re-renders and tab switches of the planning screen: view = pan/zoom, ctl = reached by the zoom buttons
const rmView = newGraphView(), rmCtl = {};

/** Roadmap payload -> the features FeatureGraph reads (id, status, stage number, kind, deps = slugs) + the raw fields for the card. */
function graphFeatures(data) {
  return list(data.features).map((f) => ({
    id: f.slug, status: f.status || 'pending', stage: stageNumber(f.stage), kind: f.kind,
    deps: String(f.deps || '').split(',').map((d) => d.trim()).filter(Boolean), raw: f,
  }));
}
const csv = (v) => String(v || '').split(',').map((x) => x.trim()).filter(Boolean);
/** `paths=<repo>:<path>` prefixes (a Windows drive letter is not a repo prefix). */
function reposOfPaths(paths) {
  const out = [];
  for (const p of csv(paths)) { const m = /^([\w.-]{2,}):(?![\\/])/.exec(p); if (m && !out.includes(m[1])) out.push(m[1]); }
  return out;
}

function Row({ k, children }) {
  return html`<div class="plan-rm-row"><span class="plan-rm-k">${k}</span><span class="plan-rm-v">${children}</span></div>`;
}

function FeatureCard({ f, onClose }) {
  const r = f.raw, paths = csv(r.paths), deps = csv(r.deps), repos = reposOfPaths(r.paths), order = csv(r['repo-order']);
  const reg = !!r.regression && r.regression !== 'no';
  return html`<div class="plan-rm-card" role="region" aria-label=${'Details of ' + f.id}>
    <div class="plan-rm-card-top"><b class="mono">${f.id}</b>
      <button type="button" class="btn btn-sm btn-ghost" onClick=${onClose} aria-label="close the details">Close</button></div>
    <${Row} k="stage">${r.stage == null || r.stage === '' ? 'none' : r.stage}<//>
    <${Row} k="kind">${r.kind || '—'}<//>
    <${Row} k="paths">${paths.length ? html`<span class="mono">${paths.join('  ')}</span>` : '—'}<//>
    <${Row} k="depends on">${deps.length ? html`<span class="mono">${deps.join(', ')}</span>` : 'nothing'}<//>
    ${repos.length ? html`<${Row} k="repos"><span class="mono">${repos.join(', ')}</span><//>` : null}
    ${order.length ? html`<${Row} k="repo-order"><span class="mono">${order.join(' → ')}</span><//>` : null}
    ${r.isolation ? html`<${Row} k="isolation">${r.isolation}<//>` : null}
    <${Row} k="regression">${reg ? 'proof requested (regression=' + r.regression + ')' : 'no'}<//>
  </div>`;
}

function RoadmapGraph({ data }) {
  const [sel, setSel] = useState(null);
  const fs = graphFeatures(data);
  const cur = fs.find((f) => f.id === sel) || null;
  const stages = list(data.stages).map((s) => ({ n: stageNumber(s.n), title: s.title }));
  return html`<div class="plan-rm-graph">
    <div class="plan-rm-bar" role="group" aria-label="Roadmap graph zoom">
      <button type="button" class="btn btn-sm btn-ghost" title="zoom out (Ctrl + scroll)" aria-label="zoom out" onClick=${() => rmCtl.zoom && rmCtl.zoom(1 / 1.2)}>−</button>
      <button type="button" class="btn btn-sm btn-ghost" title="zoom in (Ctrl + scroll)" aria-label="zoom in" onClick=${() => rmCtl.zoom && rmCtl.zoom(1.2)}>+</button>
      <button type="button" class="btn btn-sm btn-ghost" title="fits and centers the graph" onClick=${() => rmCtl.fit && rmCtl.fit()}>Center</button>
    </div>
    <div class="dep-graph plan-rm-box" style="flex:none;height:420px;position:relative;inset:auto">
      <${FeatureGraph} features=${fs} stages=${stages} selected=${cur ? cur.id : null} onSelect=${(id) => setSel((c) => (c === id ? null : id))} view=${rmView} ctl=${rmCtl} />
    </div>
    ${cur ? html`<${FeatureCard} f=${cur} onClose=${() => setSel(null)} />` : null}
  </div>`;
}

const BASE_SOURCE_LABEL = { roadmap: 'ROADMAP', env: 'env LOOP_INTEGRATION_BRANCH', default: 'default' };
const BASE_BRANCH_RULE = 'By default the loop branches from `developer`. To change it, write `Branch base: main` (or `release/2.0`) in the ROADMAP header. Order: ROADMAP, then LOOP_INTEGRATION_BRANCH, then developer. The PR goes to the same branch.';

/** GET loop/settings -> only what the read-only block shows: { baseBranch: {name, source}|null, maxParallel: number|null }. */
export async function requestBranchAndParallel() {
  const data = await ctx.api.get('loop/settings', 15000);
  const resolved = data && data.base_branch_resolved;
  const maxParallel = data && data.settings ? Number(data.settings.max_parallel_tasks) : NaN;
  return {
    baseBranch: resolved && resolved.name ? { name: String(resolved.name), source: String(resolved.source || 'default') } : null,
    maxParallel: Number.isFinite(maxParallel) && maxParallel > 0 ? maxParallel : null,
  };
}

/** Read-only base branch (with where it came from) and max parallel tasks. Missing fields render nothing. `explain` adds the rule sentence. */
export function BranchAndParallelInfo({ info, explain }) {
  if (!info || (!info.baseBranch && info.maxParallel == null)) return null;
  const base = info.baseBranch;
  return html`<div class="plan-rm-settings">
    ${base ? html`<${Row} k="Base branch"><span class="mono">${base.name}</span> (${BASE_SOURCE_LABEL[base.source] || base.source})<//>` : null}
    ${base && explain ? html`<div class="sl-muted">${BASE_BRANCH_RULE}</div>` : null}
    ${info.maxParallel != null ? html`<${Row} k="Max parallel tasks">${info.maxParallel} (executors per wave inside one feature)<//>` : null}
  </div>`;
}

function LoopSettingsBlock({ version }) {
  const [info, setInfo] = useState(null);
  useEffect(() => {  // refetched whenever the roadmap payload changes (a new Branch base line shows without a reload)
    let alive = true;
    requestBranchAndParallel().then((v) => alive && setInfo(v)).catch(() => {});
    return () => { alive = false; };
  }, [version]);
  return html`<${BranchAndParallelInfo} info=${info} explain=${true} />`;
}

/** The graph, or the parse errors instead of it, or nothing (empty roadmap); then the read-only loop settings. `rm` = { data, error }. */
export function RoadmapPreview({ rm }) {
  const data = rm && rm.data;
  if (!data) return null;
  const errors = list(data.errors);
  if (errors.length) {
    return html`<div class="plan-rm-err" role="alert"><b>The ROADMAP has errors, so there is no diagram yet:</b>
      <ul>${errors.map((e, i) => html`<li key=${i}>${String(e)}</li>`)}</ul></div>`;
  }
  if (!list(data.features).length) return null;
  return html`<${RoadmapGraph} data=${data} /><${LoopSettingsBlock} version=${data} />`;
}
