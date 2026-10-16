/* Graph engineering: feature graph in plain SVG (columns per stage; inside a stage, sub-columns by
   dependency), dependency edges colored by the status of the feature being waited on, and the feature state
   machine with the count in each node. Keyboard: Tab reaches every node; Enter/Space opens the feature.
   Markup in Preact; pan/zoom/fit stay imperative (transform on g.gf-vp, ResizeObserver on div.gf). */
import { html, useState, useEffect, useLayoutEffect, useRef, useMemo } from '../util/preact.js';

const STATUS_COLOR = {
  pending: '#7D838C', speccing: '#8AA4FF', specced: '#A58BFF', approved: '#5CC8D6',
  building: '#F2B544', verified: '#5BD38A', blocked: '#F06A5E',
};
const STATUS_BG = {
  pending: '#1E2024', speccing: '#1A1F2B', specced: '#201B2C', approved: '#16252A',
  building: '#2A2416', verified: '#15251C', blocked: '#2A1716',
};
const color = (s) => STATUS_COLOR[s] || '#7D838C';
const G = { NW: 184, NH: 66, GX: 46, GY: 14, STAGE_GAP: 30, PAD: 18, HEAD: 34 };
const MIN_SCALE = 0.2, MAX_SCALE = 2.5;

function clip(t, n) { t = String(t == null ? '' : t); return t.length > n ? t.slice(0, n - 1) + '…' : t; }
function depsOf(f) {
  if (Array.isArray(f.depends_on)) return f.depends_on.map((d) => d && d.id).filter(Boolean);
  return (Array.isArray(f.deps) ? f.deps : []).filter((d) => !String(d).startsWith('dir:'));
}
function dirsOf(f) {
  if (Array.isArray(f.dep_dirs)) return f.dep_dirs;
  return (Array.isArray(f.deps) ? f.deps : []).filter((d) => String(d).startsWith('dir:')).map((d) => String(d).slice(4));
}

/** Computes positions. Returns { nodes:[{f,x,y}], edges:[{from,to}], headers:[{x,w,title}], w, h }. */
export function computeGraphLayout(features, stages) {
  const fs = Array.isArray(features) ? features : [];
  const byId = Object.fromEntries(fs.map((f) => [f.id, f]));
  const titles = {};
  for (const e of Array.isArray(stages) ? stages : []) titles[e.n] = e.title;
  const groups = new Map();
  for (const f of fs) { const n = typeof f.stage === 'number' ? f.stage : null; if (!groups.has(n)) groups.set(n, []); groups.get(n).push(f); }
  const order = [...groups.keys()].sort((a, b) => (a == null) - (b == null) || a - b);
  const edges = [];
  for (const f of fs) for (const d of depsOf(f)) if (byId[d] && d !== f.id) edges.push({ from: d, to: f.id });
  const parents = {};
  for (const a of edges) (parents[a.to] = parents[a.to] || []).push(a.from);
  const nodes = [], headers = [], rowOf = {};
  let x = G.PAD, maxRows = 0;
  const columns = [];
  for (const n of order) {
    const items = groups.get(n);
    const inStage = new Set(items.map((f) => f.id));
    const depth = {};
    const calc = (id, seen) => {
      if (depth[id] != null) return depth[id];
      if (seen.has(id)) return 0;
      seen.add(id);
      const ps = (parents[id] || []).filter((p) => inStage.has(p));
      return (depth[id] = ps.length ? 1 + Math.max(...ps.map((p) => calc(p, seen))) : 0);
    };
    items.forEach((f) => calc(f.id, new Set()));
    const sub = {};
    items.forEach((f) => (sub[depth[f.id]] = sub[depth[f.id]] || []).push(f));
    const keys = Object.keys(sub).map(Number).sort((a, b) => a - b);
    const x0 = x;
    keys.forEach((k, ci) => {
      const arr = sub[k];
      const mean = (f) => { const ps = (parents[f.id] || []).map((p) => rowOf[p]).filter((v) => v != null); return ps.length ? ps.reduce((s, v) => s + v, 0) / ps.length : 1e3 + items.indexOf(f); };
      arr.sort((a, b) => mean(a) - mean(b));
      arr.forEach((f, i) => { rowOf[f.id] = i; });
      columns.push({ x, items: arr, stage: n });
      maxRows = Math.max(maxRows, arr.length);
      x += G.NW + (ci < keys.length - 1 ? G.GX : 0);
    });
    headers.push({ x: x0 - 10, w: x - x0 + 20, title: n == null ? 'No stage' : (titles[n] || `Stage ${String(n).padStart(2, '0')}`), n, total: items.length, verified: items.filter((f) => f.status === 'verified').length });
    x += G.GX + G.STAGE_GAP;
  }
  const height = G.PAD + G.HEAD + Math.max(1, maxRows) * (G.NH + G.GY) - G.GY + G.PAD;
  for (const c of columns) {
    const top = G.PAD + G.HEAD + ((maxRows - c.items.length) * (G.NH + G.GY)) / 2;
    c.items.forEach((f, i) => nodes.push({ f, x: c.x, y: top + i * (G.NH + G.GY) }));
  }
  const width = Math.max(G.PAD * 2 + G.NW, x - G.GX - G.STAGE_GAP + G.PAD);
  return { nodes, edges, headers, w: width, h: height };
}

function curve(x1, y1, x2, y2) {
  const dx = Math.max(40, Math.abs(x2 - x1) / 2);
  return `M${x1} ${y1} C${x1 + dx} ${y1},${x2 - dx} ${y2},${x2} ${y2}`;
}

const ARROWS = { green: '#5BD38A', amber: '#F2B544', red: '#F06A5E', grey: '#4A4F57' };
const walk = (start, map) => { const out = new Set(), stack = [start]; while (stack.length) { const n = stack.pop(); for (const m of map[n] || []) if (!out.has(m)) { out.add(m); stack.push(m); } } return out; };
const graphSig = (fs, stages) => JSON.stringify([fs.map((f) => [f.id, f.name, f.status, f.stage, f.kind, f.deps, f.depends_on, f.dep_dirs]), stages]);
/** Pan/zoom state of one graph (kept outside the component so a remount keeps the user's view). */
export const newGraphView = () => ({ view: { s: 1, x: 0, y: 0 }, manual: false, w: 0, h: 0 });

/** Layout plus everything derived from it (paths, labels): the expensive part, memoized by the data signature. */
function graphModel(fs, stages) {
  const L = computeGraphLayout(fs, stages);
  const byId = Object.fromEntries(fs.map((f) => [f.id, f]));
  const parents = {}, children = {};
  for (const a of L.edges) { (parents[a.to] = parents[a.to] || []).push(a.from); (children[a.from] = children[a.from] || []).push(a.to); }
  const pos = Object.fromEntries(L.nodes.map((n) => [n.f.id, n]));
  const edges = L.edges.map((a) => {
    const s = byId[a.from].status;
    const kind = s === 'verified' ? 'green' : s === 'blocked' ? 'red' : 'amber';
    const p1 = pos[a.from], p2 = pos[a.to];
    return { ...a, kind, d: curve(p1.x + G.NW, p1.y + G.NH / 2, p2.x - 4, p2.y + G.NH / 2) };
  });
  const nodes = L.nodes.map((n) => {
    const f = n.f, c = color(f.status);
    const deps = depsOf(f), unblocks = children[f.id] || [], dirs = dirsOf(f);
    const waiting = deps.filter((d) => byId[d] && byId[d].status !== 'verified');
    const line2 = f.status === 'blocked' ? 'blocked' : waiting.length ? 'waiting on: ' + waiting.join(', ') : f.status === 'verified' ? 'verified' : 'can run';
    const label = `${f.name || f.id}, ${f.status || 'no status'}, stage ${f.stage == null ? 'none' : f.stage}` +
      (deps.length ? `, depends on ${deps.join(', ')}` : ', no dependencies') + (unblocks.length ? `, unblocks ${unblocks.join(', ')}` : '');
    const tip = `${f.id} · ${f.status || '—'}\n${line2}${unblocks.length ? '\nunblocks: ' + unblocks.join(', ') : ''}${dirs.length ? '\nimports: ' + dirs.join(', ') : ''}`;
    return { id: f.id, f, x: n.x, y: n.y, c, waiting, dirs, line2, label, tip };
  });
  return { w: L.w, h: L.h, headers: L.headers, edges, nodes, byId, parents, children };
}

function GraphNode({ n, cls, pressed, onSelect, setHover }) {
  const f = n.f, id = n.id;
  const pick = () => { if (onSelect) onSelect(id); };
  const enter = () => setHover(id), leave = () => setHover((cur) => (cur === id ? null : cur));
  return html`<g class=${cls} transform=${`translate(${n.x} ${n.y})`} role="button" tabindex="0" aria-label=${n.label} aria-pressed=${pressed ? 'true' : 'false'}
      onClick=${pick} onKeyDown=${(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); pick(); } }}
      onMouseEnter=${enter} onMouseLeave=${leave} onFocus=${enter} onBlur=${leave}>
    <title>${n.tip}</title>
    <rect class="gf-box" width=${G.NW} height=${G.NH} rx="8" fill=${STATUS_BG[f.status] || '#15171A'} stroke=${n.c} />
    <rect class="gf-bar" width="4" height=${G.NH - 12} x="6" y="6" rx="2" fill=${n.c} />
    <text class="gf-title" x="16" y="20">${clip(f.name || f.id, 24)}</text>
    <text class="gf-status" x=${G.NW - 8} y="20" text-anchor="end" fill=${n.c}>${f.status || '—'}</text>
    <text class="gf-sub" x="16" y="38">${clip(f.name && f.name !== f.id ? f.id : (f.kind ? 'kind ' + f.kind : ''), 30)}</text>
    <text class="gf-line" x="16" y="55" fill=${f.status === 'blocked' ? '#F06A5E' : n.waiting.length ? '#F2B544' : '#8B9098'}>${clip(n.line2 + (n.dirs.length ? ` · ${n.dirs.length} folder(s)` : ''), 32)}</text>
  </g>`;
}

/**
 * Content of div.gf (svg + empty note). host = ref to div.gf (drag class, ResizeObserver); view = newGraphView();
 * ctl (optional object) receives { zoom(factor, cx, cy), fit(), scale() } for the zoom buttons outside.
 */
export function GraphContent({ host, view, ctl, features, stages, selected = null, status = null, onSelect }) {
  const st = view;
  const svgRef = useRef(null), vpRef = useRef(null);
  const [hover, setHover] = useState(null);
  const fs = Array.isArray(features) ? features : [];
  const sig = graphSig(fs, stages);
  const M = useMemo(() => (fs.length ? graphModel(fs, stages) : null), [sig]);

  const api = useMemo(() => {
    const apply = () => { if (vpRef.current) vpRef.current.setAttribute('transform', `translate(${st.view.x.toFixed(1)} ${st.view.y.toFixed(1)}) scale(${st.view.s.toFixed(3)})`); };
    const fit = () => {
      const svg = svgRef.current; if (!svg) return;
      const r = svg.getBoundingClientRect();
      if (!st.w || !r.width || !r.height) return;
      const s = Math.max(MIN_SCALE, Math.min((r.width - 24) / st.w, (r.height - 24) / st.h, 1.15));
      st.view = { s, x: (r.width - st.w * s) / 2, y: Math.max(8, (r.height - st.h * s) / 2) };
      st.manual = false; apply();
    };
    const zoom = (f, cx, cy) => {
      const svg = svgRef.current; if (!svg) return;
      const r = svg.getBoundingClientRect();
      const px = cx == null ? r.width / 2 : cx - r.left, py = cy == null ? r.height / 2 : cy - r.top, v = st.view;
      const s = Math.max(MIN_SCALE, Math.min(MAX_SCALE, v.s * f)), k = s / v.s;
      st.view = { s, x: px - (px - v.x) * k, y: py - (py - v.y) * k };
      st.manual = true; apply();
    };
    return { apply, fit, zoom, scale: () => st.view.s };
  }, [st]);
  if (ctl) Object.assign(ctl, api);

  useEffect(() => {
    const svg = svgRef.current, el = host && host.current;
    let drag = null;
    const down = (e) => {
      if (e.button !== 0 || e.target.closest('.gf-node')) return;
      svg.setPointerCapture(e.pointerId);
      drag = { id: e.pointerId, x: e.clientX, y: e.clientY, vx: st.view.x, vy: st.view.y };
      if (el) el.classList.add('dragging');
    };
    const move = (e) => {
      if (!drag || drag.id !== e.pointerId) return;
      st.view = { s: st.view.s, x: drag.vx + e.clientX - drag.x, y: drag.vy + e.clientY - drag.y }; st.manual = true; api.apply();
    };
    const end = (e) => { if (drag && drag.id === e.pointerId) { drag = null; if (el) el.classList.remove('dragging'); } };
    const wheel = (e) => {
      e.preventDefault();
      if (e.ctrlKey || e.metaKey) api.zoom(e.deltaY < 0 ? 1.12 : 1 / 1.12, e.clientX, e.clientY);
      else { st.view = { s: st.view.s, x: st.view.x - e.deltaX, y: st.view.y - e.deltaY }; st.manual = true; api.apply(); }
    };
    svg.addEventListener('pointerdown', down); svg.addEventListener('pointermove', move);
    svg.addEventListener('pointerup', end); svg.addEventListener('pointercancel', end);
    svg.addEventListener('wheel', wheel, { passive: false });
    const ro = typeof ResizeObserver === 'function' && el ? new ResizeObserver(() => { if (!st.manual) api.fit(); }) : null;
    if (ro) ro.observe(el);
    return () => {
      svg.removeEventListener('pointerdown', down); svg.removeEventListener('pointermove', move);
      svg.removeEventListener('pointerup', end); svg.removeEventListener('pointercancel', end);
      svg.removeEventListener('wheel', wheel);
      if (ro) ro.disconnect();
    };
  }, [api]);

  // new data: refit (automatic mode or size changed), otherwise keep the user's view
  useLayoutEffect(() => {
    if (!M) return;
    const sizeChanged = M.w !== st.w || M.h !== st.h;
    st.w = M.w; st.h = M.h;
    if (!st.manual || sizeChanged) requestAnimationFrame(api.fit); else api.apply();
  }, [M]);

  let heads = null, edges = null, nodes = null;
  if (M) {
    const focus = hover || selected, ok = !!focus && !!M.byId[focus];
    const above = ok ? walk(focus, M.parents) : new Set(), below = ok ? walk(focus, M.children) : new Set();
    const onPath = (id) => !ok || id === focus || above.has(id) || below.has(id);
    const statusOf = (id) => M.byId[id].status;
    heads = M.headers.map((c, i) => html`<g key=${i} class="gf-head">
      <rect x=${c.x} y="6" width=${c.w} height=${M.h - 12} rx="12" />
      <text x=${c.x + 12} y="26" class="gf-head-t">${clip(c.title, Math.max(12, Math.floor(c.w / 7.5)))}</text>
      <text x=${c.x + c.w - 12} y="26" class="gf-head-n" text-anchor="end">${`${c.verified}/${c.total}`}</text>
    </g>`);
    edges = M.edges.map((a) => {
      const hot = ok && (((above.has(a.from) || a.from === focus) && (above.has(a.to) || a.to === focus)) || ((below.has(a.to) || a.to === focus) && (below.has(a.from) || a.from === focus)));
      const dim = (ok && !hot) || (!!status && !(statusOf(a.from) === status || statusOf(a.to) === status));
      return html`<path key=${a.from + '>' + a.to} class=${'gf-edge ' + a.kind + (hot ? ' hot' : '') + (dim ? ' dim' : '')} d=${a.d}
        marker-end=${`url(#gf-arrow-${a.kind})`} stroke-dasharray=${a.kind !== 'green' ? '5 4' : null} />`;
    });
    nodes = M.nodes.map((n) => {
      const dim = !onPath(n.id) || (!!status && n.f.status !== status);
      const cls = 'gf-node' + (n.f.status === 'building' ? ' run' : '') + (n.f.status === 'blocked' ? ' fail' : '') + (dim ? ' dim' : '') + (n.id === selected ? ' on' : '');
      return html`<${GraphNode} key=${n.id} n=${n} cls=${cls} pressed=${n.id === selected} onSelect=${onSelect} setHover=${setHover} />`;
    });
  }
  return html`<svg class="gf-svg" ref=${svgRef} role="group" aria-label="Feature dependency graph, one column per stage" hidden=${M ? null : ''}>
      <defs>${Object.entries(ARROWS).map(([k, c]) => html`<marker key=${k} id=${'gf-arrow-' + k} viewBox="0 0 8 8" refX="7" refY="4" markerWidth="8" markerHeight="8" orient="auto" markerUnits="userSpaceOnUse"><path d="M0 0 L8 4 L0 8 z" fill=${c} /></marker>`)}</defs>
      <g class="gf-vp" ref=${vpRef}><g class="gf-heads">${heads}</g><g class="gf-edges">${edges}</g><g class="gf-nodes">${nodes}</g></g>
    </svg>
    <div class="gf-empty" hidden=${!!M}>${M ? null : 'No feature in the roadmap yet. Generate the roadmap from the trail at the top; the dependency graph shows up here.'}</div>`;
}

/** Feature graph (div.gf). Props: features, stages, selected, status (highlight), onSelect(id), view, ctl. */
export function FeatureGraph(props) {
  const host = useRef(null), own = useRef(null);
  if (!props.view && !own.current) own.current = newGraphView();
  return html`<div class="gf" ref=${host}><${GraphContent} ...${props} host=${host} view=${props.view || own.current} /></div>`;
}

const MACHINE = [
  { id: 'pending', title: 'pending', hint: 'in the roadmap, not started yet' },
  { id: 'speccing', title: 'speccing', hint: 'spec-writer writing the spec' },
  { id: 'specced', title: 'specced', hint: 'spec ready, at the spec-reviewer gate' },
  { id: 'approved', title: 'approved', hint: 'spec approved, waiting for the executor' },
  { id: 'building', title: 'building', hint: 'executor implementing / verifier checking' },
  { id: 'verified', title: 'verified', hint: 'passed verification' },
];
// fixed geometry of the machine (computed once)
const SM = (() => {
  const NW = 104, NH = 46, GX = 26, X0 = 10, Y0 = 10, YB = 92;
  const W = X0 * 2 + MACHINE.length * NW + (MACHINE.length - 1) * GX, H = YB + NH + 10;
  const xOf = (i) => X0 + i * (NW + GX);
  const xBlocked = xOf(2) + NW / 2 + (NW + GX) / 2 - NW / 2;
  const edges = MACHINE.slice(0, -1).map((_, i) => `M${xOf(i) + NW} ${Y0 + NH / 2} L${xOf(i + 1) - 3} ${Y0 + NH / 2}`);
  const fails = [1, 2, 4].map((i) => `M${xOf(i) + NW / 2} ${Y0 + NH} C${xOf(i) + NW / 2} ${YB - 10},${xBlocked + NW / 2} ${Y0 + NH + 10},${xBlocked + NW / 2} ${YB - 3}`);
  const all = [...MACHINE.map((m, i) => ({ ...m, x: xOf(i), y: Y0 })), { id: 'blocked', title: 'blocked', hint: 'got stuck: goes to quarantine', x: xBlocked, y: YB }];
  return { NW, NH, YB, W, H, xBlocked, edges, fails, all };
})();

/** Content of div.me: the state machine (svg) and the total. chosen = highlighted status; onPick(next) gets the toggled value. */
export function MachineContent({ counts, chosen = null, onPick }) {
  const c = counts || {}, { NW, NH } = SM;
  return html`<svg class="me-svg" viewBox=${`0 0 ${SM.W} ${SM.H}`} role="group" aria-label="Feature state machine with the count in each state">
      <defs>
        <marker id="me-arrow" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto" markerUnits="userSpaceOnUse"><path d="M0 0 L8 4 L0 8 z" fill="#4A4F57" /></marker>
        <marker id="me-arrow-r" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto" markerUnits="userSpaceOnUse"><path d="M0 0 L8 4 L0 8 z" fill="#F06A5E" /></marker>
      </defs>
      ${SM.edges.map((d) => html`<path key=${d} class="me-edge" d=${d} marker-end="url(#me-arrow)" />`)}
      ${SM.fails.map((d) => html`<path key=${d} class="me-edge fail" d=${d} marker-end="url(#me-arrow-r)" />`)}
      ${SM.all.map((m) => {
        const col = color(m.id), v = c[m.id], on = chosen === m.id;
        const pick = () => { if (onPick) onPick(on ? null : m.id); };
        return html`<g key=${m.id} class=${'me-node' + (on ? ' on' : '')} role="button" tabindex="0" aria-pressed=${on ? 'true' : 'false'}
            aria-label=${`${m.title}: ${typeof v === 'number' ? v : 'no data'} feature(s). ${m.hint}`}
            onClick=${pick} onKeyDown=${(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); pick(); } }}>
          <title>${`${m.title}: ${m.hint}`}</title>
          <rect x=${m.x} y=${m.y} width=${NW} height=${NH} rx="8" fill=${STATUS_BG[m.id]} stroke=${col} />
          <text class="me-t" x=${m.x + NW / 2} y=${m.y + 15} text-anchor="middle">${m.title}</text>
          <text class="me-num" x=${m.x + NW / 2} y=${m.y + 33} text-anchor="middle" fill=${col}>${typeof v === 'number' ? String(v) : '—'}</text>
        </g>`;
      })}
      <text class="me-legend" x=${SM.xBlocked + NW + 14} y=${SM.YB + 20}>failed at the gate, in the build or in verification</text>
    </svg>
    <span class="me-total mono">${typeof c.total === 'number' ? `${c.total} in total` : null}</span>`;
}

/** Feature state machine (div.me). Clicking a node highlights the features in that status. */
export function StateMachine(props) {
  return html`<div class="me"><${MachineContent} ...${props} /></div>`;
}
