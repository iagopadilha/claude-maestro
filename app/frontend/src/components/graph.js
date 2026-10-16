/* Graph engineering: feature graph in plain SVG (columns per stage; inside a stage, sub-columns by
   dependency), dependency edges colored by the status of the feature being waited on, and the feature state
   machine with the count in each node. Keyboard: Tab reaches every node; Enter/Space opens the feature. */
import { h, sv } from '../util/dom.js';

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

/**
 * Feature graph. createFeatureGraph({ onSelect(id) }) -> { element, update({features, stages, selected}),
 * zoom(factor), fit(), highlightStatus(status|null) }.
 */
export function createFeatureGraph({ onSelect }) {
  const defs = sv('defs');
  for (const [k, c] of Object.entries({ green: '#5BD38A', amber: '#F2B544', red: '#F06A5E', grey: '#4A4F57' })) {
    defs.appendChild(sv('marker', { id: 'gf-arrow-' + k, viewBox: '0 0 8 8', refX: 7, refY: 4, markerWidth: 8, markerHeight: 8, orient: 'auto', markerUnits: 'userSpaceOnUse' },
      sv('path', { d: 'M0 0 L8 4 L0 8 z', fill: c })));
  }
  const headLayer = sv('g', { class: 'gf-heads' });
  const edgeLayer = sv('g', { class: 'gf-edges' });
  const nodeLayer = sv('g', { class: 'gf-nodes' });
  const vp = sv('g', { class: 'gf-vp' }, headLayer, edgeLayer, nodeLayer);
  const svg = sv('svg', { class: 'gf-svg', role: 'group', 'aria-label': 'Feature dependency graph, one column per stage' }, defs, vp);
  const empty = h('div', { class: 'gf-empty', hidden: true });
  const element = h('div', { class: 'gf' }, svg, empty);
  const st = { view: { s: 1, x: 0, y: 0 }, manual: false, w: 0, h: 0, nodes: new Map(), edges: [], focus: null, selected: null, status: null, parents: {}, children: {} };

  function apply() { vp.setAttribute('transform', `translate(${st.view.x.toFixed(1)} ${st.view.y.toFixed(1)}) scale(${st.view.s.toFixed(3)})`); }
  function fit() {
    const r = svg.getBoundingClientRect();
    if (!st.w || !r.width || !r.height) return;
    const s = Math.max(MIN_SCALE, Math.min((r.width - 24) / st.w, (r.height - 24) / st.h, 1.15));
    st.view = { s, x: (r.width - st.w * s) / 2, y: Math.max(8, (r.height - st.h * s) / 2) };
    st.manual = false; apply();
  }
  function zoom(f, cx, cy) {
    const r = svg.getBoundingClientRect();
    const px = cx == null ? r.width / 2 : cx - r.left, py = cy == null ? r.height / 2 : cy - r.top, v = st.view;
    const s = Math.max(MIN_SCALE, Math.min(MAX_SCALE, v.s * f)), k = s / v.s;
    st.view = { s, x: px - (px - v.x) * k, y: py - (py - v.y) * k };
    st.manual = true; apply();
  }
  let drag = null;
  svg.addEventListener('pointerdown', (e) => {
    if (e.button !== 0 || e.target.closest('.gf-node')) return;
    svg.setPointerCapture(e.pointerId);
    drag = { id: e.pointerId, x: e.clientX, y: e.clientY, vx: st.view.x, vy: st.view.y };
    element.classList.add('dragging');
  });
  svg.addEventListener('pointermove', (e) => {
    if (!drag || drag.id !== e.pointerId) return;
    st.view = { s: st.view.s, x: drag.vx + e.clientX - drag.x, y: drag.vy + e.clientY - drag.y }; st.manual = true; apply();
  });
  const end = (e) => { if (drag && drag.id === e.pointerId) { drag = null; element.classList.remove('dragging'); } };
  svg.addEventListener('pointerup', end); svg.addEventListener('pointercancel', end);
  svg.addEventListener('wheel', (e) => {
    e.preventDefault();
    if (e.ctrlKey || e.metaKey) zoom(e.deltaY < 0 ? 1.12 : 1 / 1.12, e.clientX, e.clientY);
    else { st.view = { s: st.view.s, x: st.view.x - e.deltaX, y: st.view.y - e.deltaY }; st.manual = true; apply(); }
  }, { passive: false });
  if (typeof ResizeObserver === 'function') new ResizeObserver(() => { if (!st.manual) fit(); }).observe(element);

  const walk = (start, map) => { const out = new Set(), stack = [start]; while (stack.length) { const n = stack.pop(); for (const m of map[n] || []) if (!out.has(m)) { out.add(m); stack.push(m); } } return out; };
  function highlight() {
    const focus = st.focus || st.selected;
    const ok = focus && st.nodes.has(focus);
    const above = ok ? walk(focus, st.parents) : new Set(), below = ok ? walk(focus, st.children) : new Set();
    const onPath = (id) => !ok || id === focus || above.has(id) || below.has(id);
    for (const [id, g] of st.nodes) {
      const byStatus = !st.status || g._status === st.status;
      g.classList.toggle('dim', !onPath(id) || !byStatus);
      g.classList.toggle('on', id === st.selected);
      g.setAttribute('aria-pressed', id === st.selected ? 'true' : 'false');
    }
    for (const a of st.edges) {
      const hot = ok && (((above.has(a.from) || a.from === focus) && (above.has(a.to) || a.to === focus)) || ((below.has(a.to) || a.to === focus) && (below.has(a.from) || a.from === focus)));
      a.el.classList.toggle('hot', !!hot);
      a.el.classList.toggle('dim', (ok && !hot) || (!!st.status && !(st.nodes.get(a.from)._status === st.status || st.nodes.get(a.to)._status === st.status)));
    }
  }

  function update({ features, stages, selected }) {
    const fs = Array.isArray(features) ? features : [];
    st.selected = selected || null;
    if (!fs.length) {
      svg.hidden = true; empty.hidden = false;
      empty.textContent = 'No feature in the roadmap yet. Generate the roadmap from the trail at the top; the dependency graph shows up here.';
      return;
    }
    svg.hidden = false; empty.hidden = true;
    const L = computeGraphLayout(fs, stages);
    const sizeChanged = L.w !== st.w || L.h !== st.h;
    st.w = L.w; st.h = L.h;
    const byId = Object.fromEntries(fs.map((f) => [f.id, f]));
    st.parents = {}; st.children = {};
    for (const a of L.edges) { (st.parents[a.to] = st.parents[a.to] || []).push(a.from); (st.children[a.from] = st.children[a.from] || []).push(a.to); }
    headLayer.replaceChildren(...L.headers.map((c) => sv('g', { class: 'gf-head' },
      sv('rect', { x: c.x, y: 6, width: c.w, height: L.h - 12, rx: 12 }),
      sv('text', { x: c.x + 12, y: 26, class: 'gf-head-t', text: clip(c.title, Math.max(12, Math.floor(c.w / 7.5))) }),
      sv('text', { x: c.x + c.w - 12, y: 26, class: 'gf-head-n', 'text-anchor': 'end', text: `${c.verified}/${c.total}` }))));
    const pos = Object.fromEntries(L.nodes.map((n) => [n.f.id, n]));
    st.edges = L.edges.map((a) => {
      const s = byId[a.from].status;
      const kind = s === 'verified' ? 'green' : s === 'blocked' ? 'red' : 'amber';
      const p1 = pos[a.from], p2 = pos[a.to];
      const el = sv('path', { class: 'gf-edge ' + kind, d: curve(p1.x + G.NW, p1.y + G.NH / 2, p2.x - 4, p2.y + G.NH / 2), 'marker-end': `url(#gf-arrow-${kind})` });
      if (kind !== 'green') el.setAttribute('stroke-dasharray', '5 4');
      return { ...a, el };
    });
    edgeLayer.replaceChildren(...st.edges.map((a) => a.el));
    st.nodes = new Map();
    nodeLayer.replaceChildren(...L.nodes.map((n) => {
      const f = n.f, c = color(f.status);
      const deps = depsOf(f), unblocks = st.children[f.id] || [], dirs = dirsOf(f);
      const waiting = deps.filter((d) => byId[d] && byId[d].status !== 'verified');
      const line2 = f.status === 'blocked' ? 'blocked' : waiting.length ? 'waiting on: ' + waiting.join(', ') : f.status === 'verified' ? 'verified' : 'can run';
      const label = `${f.name || f.id}, ${f.status || 'no status'}, stage ${f.stage == null ? 'none' : f.stage}` +
        (deps.length ? `, depends on ${deps.join(', ')}` : ', no dependencies') + (unblocks.length ? `, unblocks ${unblocks.join(', ')}` : '');
      const g = sv('g', { class: 'gf-node' + (f.status === 'building' ? ' run' : '') + (f.status === 'blocked' ? ' fail' : ''), transform: `translate(${n.x} ${n.y})`, role: 'button', tabindex: '0', 'aria-label': label },
        sv('title', { text: `${f.id} · ${f.status || '—'}\n${line2}${unblocks.length ? '\nunblocks: ' + unblocks.join(', ') : ''}${dirs.length ? '\nimports: ' + dirs.join(', ') : ''}` }),
        sv('rect', { class: 'gf-box', width: G.NW, height: G.NH, rx: 8, fill: STATUS_BG[f.status] || '#15171A', stroke: c }),
        sv('rect', { class: 'gf-bar', width: 4, height: G.NH - 12, x: 6, y: 6, rx: 2, fill: c }),
        sv('text', { class: 'gf-title', x: 16, y: 20, text: clip(f.name || f.id, 24) }),
        sv('text', { class: 'gf-status', x: G.NW - 8, y: 20, 'text-anchor': 'end', fill: c, text: f.status || '—' }),
        sv('text', { class: 'gf-sub', x: 16, y: 38, text: clip(f.name && f.name !== f.id ? f.id : (f.kind ? 'kind ' + f.kind : ''), 30) }),
        sv('text', { class: 'gf-line', x: 16, y: 55, fill: f.status === 'blocked' ? '#F06A5E' : waiting.length ? '#F2B544' : '#8B9098', text: clip(line2 + (dirs.length ? ` · ${dirs.length} folder(s)` : ''), 32) }));
      g._status = f.status;
      const pick = () => { if (onSelect) onSelect(f.id); };
      g.addEventListener('click', pick);
      g.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); pick(); } });
      const enter = () => { st.focus = f.id; highlight(); }, leave = () => { if (st.focus === f.id) { st.focus = null; highlight(); } };
      g.addEventListener('mouseenter', enter); g.addEventListener('mouseleave', leave);
      g.addEventListener('focus', enter); g.addEventListener('blur', leave);
      st.nodes.set(f.id, g);
      return g;
    }));
    highlight();
    if (!st.manual || sizeChanged) requestAnimationFrame(fit); else apply();
  }

  function highlightStatus(s) { st.status = s || null; highlight(); }
  return { element, update, zoom, fit, highlightStatus, scale: () => st.view.s };
}

const MACHINE = [
  { id: 'pending', title: 'pending', hint: 'in the roadmap, not started yet' },
  { id: 'speccing', title: 'speccing', hint: 'spec-writer writing the spec' },
  { id: 'specced', title: 'specced', hint: 'spec ready, at the spec-reviewer gate' },
  { id: 'approved', title: 'approved', hint: 'spec approved, waiting for the executor' },
  { id: 'building', title: 'building', hint: 'executor implementing / verifier checking' },
  { id: 'verified', title: 'verified', hint: 'passed verification' },
];

/** Feature state machine with the count in each node. Clicking a node highlights the features in that status. */
export function createStateMachine({ onPick }) {
  const NW = 104, NH = 46, GX = 26, X0 = 10, Y0 = 10, YB = 92;
  const W = X0 * 2 + MACHINE.length * NW + (MACHINE.length - 1) * GX, H = YB + NH + 10;
  const defs = sv('defs', {}, sv('marker', { id: 'me-arrow', viewBox: '0 0 8 8', refX: 7, refY: 4, markerWidth: 7, markerHeight: 7, orient: 'auto', markerUnits: 'userSpaceOnUse' }, sv('path', { d: 'M0 0 L8 4 L0 8 z', fill: '#4A4F57' })),
    sv('marker', { id: 'me-arrow-r', viewBox: '0 0 8 8', refX: 7, refY: 4, markerWidth: 7, markerHeight: 7, orient: 'auto', markerUnits: 'userSpaceOnUse' }, sv('path', { d: 'M0 0 L8 4 L0 8 z', fill: '#F06A5E' })));
  const svg = sv('svg', { class: 'me-svg', viewBox: `0 0 ${W} ${H}`, role: 'group', 'aria-label': 'Feature state machine with the count in each state' }, defs);
  const xOf = (i) => X0 + i * (NW + GX);
  MACHINE.slice(0, -1).forEach((_, i) => svg.appendChild(sv('path', { class: 'me-edge', d: `M${xOf(i) + NW} ${Y0 + NH / 2} L${xOf(i + 1) - 3} ${Y0 + NH / 2}`, 'marker-end': 'url(#me-arrow)' })));
  const xBlocked = xOf(2) + NW / 2 + (NW + GX) / 2 - NW / 2;
  for (const i of [1, 2, 4]) {
    svg.appendChild(sv('path', { class: 'me-edge fail', d: `M${xOf(i) + NW / 2} ${Y0 + NH} C${xOf(i) + NW / 2} ${YB - 10},${xBlocked + NW / 2} ${Y0 + NH + 10},${xBlocked + NW / 2} ${YB - 3}`, 'marker-end': 'url(#me-arrow-r)' }));
  }
  const nodes = {};
  const all = [...MACHINE.map((m, i) => ({ ...m, x: xOf(i), y: Y0 })), { id: 'blocked', title: 'blocked', hint: 'got stuck: goes to quarantine', x: xBlocked, y: YB }];
  let chosen = null;
  for (const m of all) {
    const c = color(m.id);
    const num = sv('text', { class: 'me-num', x: m.x + NW / 2, y: m.y + 33, 'text-anchor': 'middle', fill: c, text: '—' });
    const g = sv('g', { class: 'me-node', role: 'button', tabindex: '0', 'aria-pressed': 'false' },
      sv('title', { text: `${m.title}: ${m.hint}` }),
      sv('rect', { x: m.x, y: m.y, width: NW, height: NH, rx: 8, fill: STATUS_BG[m.id], stroke: c }),
      sv('text', { class: 'me-t', x: m.x + NW / 2, y: m.y + 15, 'text-anchor': 'middle', text: m.title }), num);
    const pick = () => { chosen = chosen === m.id ? null : m.id; mark(); if (onPick) onPick(chosen); };
    g.addEventListener('click', pick);
    g.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); pick(); } });
    nodes[m.id] = { g, num, m };
    svg.appendChild(g);
  }
  svg.appendChild(sv('text', { class: 'me-legend', x: xBlocked + NW + 14, y: YB + 20, text: 'failed at the gate, in the build or in verification' }));
  function mark() { for (const [id, n] of Object.entries(nodes)) { n.g.classList.toggle('on', id === chosen); n.g.setAttribute('aria-pressed', id === chosen ? 'true' : 'false'); } }
  const total = h('span', { class: 'me-total mono' });
  const element = h('div', { class: 'me' }, svg, total);
  function update(counts) {
    const c = counts || {};
    for (const [id, n] of Object.entries(nodes)) {
      const v = c[id];
      n.num.textContent = typeof v === 'number' ? String(v) : '—';
      n.g.setAttribute('aria-label', `${n.m.title}: ${typeof v === 'number' ? v : 'no data'} feature(s). ${n.m.hint}`);
    }
    total.textContent = typeof c.total === 'number' ? `${c.total} in total` : '';
  }
  return { element, update, clear: () => { chosen = null; mark(); } };
}
