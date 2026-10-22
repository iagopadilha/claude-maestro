/* "Current turn" view: pipeline per feature (one lane per feature). Pixel layout on a stage scaled
   to fit the screen; Preact reconciles lanes, nodes and edges by key, so a poll without changes does not touch the DOM.
   Pan/zoom stays imperative on the .dg-wrap host (transform on .dg-stage, label in #zoom-val). */
import { $, setStyle } from '../../util/dom.js';
import { html, useRef, useLayoutEffect } from '../../util/preact.js';
import { dur, tok, cost, txt } from '../../util/format.js';
import { ui, pal, agents, features, featById, projectName } from './context.js';
import { selectAgent } from './roadmap_sidebar.js';

const PIPE = { NW: 236, NH: 112, ORCHW: 150, GAPY: 10, GAPX: 34, PADX: 12, PADY: 8, LANE_TOP: 32, LANE_BOT: 10, LANE_GAP: 8 };
/** What each role does, in plain words (the second line of the node). */
const ROLE_WORDS = { 'spec-writer': 'writes the spec', 'spec-reviewer': 'reviews the spec', 'executor-back': 'implements (back end)', 'executor-front': 'implements (front end)',
  'executor-generic': 'implements (infra, script)', verifier: 'verifies (build, types, lint)', 'verifier-flow': 'validates the flow (E2E)', 'brownfield-mapper': 'maps the existing code', orchestrator: 'plans and dispatches' };
const WAITING_TASK = { queued: 'waiting for the previous step', idle: 'waiting for the previous step' };
/** One line for 'what is it doing now': decision, summary or last action; else the state in words. */
const currentTask = (a) => {
  const text = (a.last_action && a.last_action.text) || a.decision || a.summary;
  if (text) return String(text);
  if (a.state === 'running') return 'working on it now';
  return WAITING_TASK[a.state] || (a.state === 'done' ? 'finished' : '');
};
const SCALE_MAX = 1.2;
const MARKERS = { gray: '#4A4F57', amber: '#F2B544', red: '#F06A5E', green: '#5BD38A', white: '#E6E6E3' };
const STAGE_COL = { 'spec-writer': 1, 'spec-reviewer': 2, 'executor-back': 3, 'executor-front': 3, 'executor-generic': 3, verifier: 4, 'verifier-flow': 4 };
const dg = { view: { s: 1, x: 0, y: 0 }, manual: false, wrap: null, stage: null, w: 0, h: 0 };
export const retColor = (r) => (/^(APPROVED|PASS|DONE)$/i.test(r) ? '#5BD38A' : /^(BLOCKED|FAIL|TOO_BIG)$/i.test(r) ? '#F06A5E' : '#F2B544');
export const isBad = (a) => a.state === 'failed' || a.state === 'blocked' || /^(BLOCKED|FAIL)$/i.test(String(a.return_status || ''));
export const actText = (a) => (a.last_action && a.last_action.text ? '› ' + a.last_action.text : a.summary ? '› ' + a.summary : '');
// Visual-only drag offsets by node id, in memory (a refresh resets them); layoutSnapshot = last model geometry for the drag.
const nodeOffsets = new Map();
const layoutSnapshot = { base: {}, edges: [], laneBase: {}, laneOf: {} };
/** Dragging an agent moves its whole feature lane (the lane and every agent in it); the orchestrator moves alone. */
const dragKeyOf = (nodeId) => layoutSnapshot.laneOf[nodeId] || nodeId;
const offsetOfNode = (nodeId) => nodeOffsets.get(dragKeyOf(nodeId)) || { x: 0, y: 0 };
const orNull =(s) => (s == null || s === '' ? null : String(s));

/** Empty message in #canvas (plain mode). */
export const EmptyMsg = ({ text }) => html`<div class="empty-canvas">${text}</div>`;

/** #zoom-val is rendered once by the Panel with a constant text; pan/zoom rewrites that text node in place
    (no render per wheel tick, and Preact never diffs it back). */
export function showZoom(scale) {
  const z = $('zoom-val'), t = Math.round(scale * 100) + '%';
  if (z && z.firstChild && z.firstChild.data !== t) z.firstChild.data = t;
}

// ----- diagram markup -----
function Lane({ l }) {
  return html`<div data-lane-key=${l.key} class=${'dg-lane' + (l.alt ? ' alt' : '') + (l.col ? ' col' : '')} style=${{ left: l.x + 'px', top: l.y + 'px', width: l.w + 'px', height: l.h + 'px' }}>
    <span class="dg-lane-label" title=${l.label}>${l.label}</span><span class="dg-lane-sub" style=${{ color: l.subColor || '', borderColor: l.subColor || '' }}>${orNull(l.sub)}</span>
  </div>`;
}
function DNode({ n }) {
  const cls = 'dnode' + (n.pulse ? ' run' : '') + (n.fail ? ' fail' : '') + (n.on ? ' on' : '') + (n.small ? ' small' : '') + (n.state ? ' st-' + n.state : '');
  const style = { left: n.x + 'px', top: n.y + 'px', width: n.w + 'px', height: n.h + 'px', '--c': n.color, '--cg': n.color + '33', opacity: String(n.opacity) };
  return html`<button type="button" data-node-id=${n.key} class=${cls} style=${style} title=${n.tip} aria-pressed=${n.on ? 'true' : 'false'} onClick=${() => { if (n.select) n.select(); }}>
    <span class="dn-top"><span class="dot" style=${{ background: n.color }} /><span class="dn-title">${orNull(n.title)}</span><span class="dn-badge" style=${{ color: n.color }}>${orNull(n.badge)}</span></span>
    <span class="dn-sub"><span class="dn-subtxt">${orNull(n.sub)}</span><span class="dn-chip" style=${{ color: n.chipColor || '', borderColor: n.chipColor || '' }}>${orNull(n.chip)}</span></span>
    <span class="dn-act" style=${{ color: n.actColor || '' }}>${orNull(n.act)}</span>
    <span class="dn-meta">${orNull(n.meta)}</span>
    <span class="dn-chips">${(n.chips || []).map((c, i) => html`<span key=${i} class="dn-pill">${c}</span>`)}</span>
  </button>`;
}
const setStage = (el) => { dg.stage = el; };
/** Content of .dg-wrap: the stage with the edges (SVG), the lanes and the agent nodes. */
export function TurnDiagram({ model: m }) {
  return html`<div class="dg-stage" ref=${setStage} style=${{ width: m.w + 'px', height: m.h + 'px' }}>
    <svg class="dg-svg" aria-hidden="true" width=${m.w} height=${m.h} viewBox=${`0 0 ${m.w} ${m.h}`}>
      <defs>${Object.entries(MARKERS).map(([k, c]) => html`<marker key=${k} id=${'arr-' + k} viewBox="0 0 8 8" refX="7" refY="4" markerWidth="8" markerHeight="8" orient="auto" markerUnits="userSpaceOnUse"><path d="M0 0 L8 4 L0 8 z" fill=${c} /></marker>`)}</defs>
      ${m.edges.map((e) => html`<path key=${e.key} data-edge-key=${e.key} class=${'edge' + (e.flow ? ' flow' : '')} fill="none" stroke-linecap="round" d=${e.d} stroke=${MARKERS[e.color] || MARKERS.gray}
        stroke-width=${e.w} opacity=${e.op} stroke-dasharray=${e.dash || null} marker-end=${`url(#arr-${e.color})`} />`)}
    </svg>
    ${m.lanes.map((l) => html`<${Lane} key=${l.key} l=${l} />`)}
    ${m.nodes.map((n) => html`<${DNode} key=${n.key} n=${n} />`)}
  </div>`;
}

/** Content of #canvas in the turn view with agents: the pan/zoom host and the diagram. Mounting it (turn tab shown
    again, first agents of the turn) goes back to automatic fit; only the user panning/zooming leaves it. */
export function TurnView({ model }) {
  const ref = useRef(null);
  useLayoutEffect(() => {
    const el = ref.current;
    dg.wrap = el; dg.manual = false; bindPanZoom(el);
    const refit = () => dgScale();
    window.addEventListener('resize', refit);
    requestAnimationFrame(refit); // after the layout of this paint (dialog closed, tab shown again)
    return () => { window.removeEventListener('resize', refit); if (dg.wrap === el) dg.wrap = null; };
  }, []);
  useLayoutEffect(() => { dg.w = model.w; dg.h = model.h; dgScale(); });
  return html`<div class="dg-wrap" ref=${ref}><${TurnDiagram} model=${model} /></div>`;
}
/** Visible part of the pane (the .main can be taller than the window, with the rest scrolled or under the timeline). */
function vpSize() {
  const box = document.querySelector('.canvas-wrap');
  if (!box) return { w: 0, h: 0, top: 0 };
  const rect = box.getBoundingClientRect(), timeline = document.querySelector('.timeline');
  const limit = Math.min(window.innerHeight, timeline && timeline.getBoundingClientRect().top > rect.top ? timeline.getBoundingClientRect().top : Infinity);
  const top = Math.max(0, -rect.top), visibleHeight = Math.min(rect.bottom, limit) - Math.max(rect.top, 0);
  return { w: box.clientWidth, h: Math.min(box.clientHeight, visibleHeight), top };
}
function dgApply() {
  if (!dg.stage) return;
  const v = dg.view;
  setStyle(dg.stage, 'transform', `translate(${Math.round(v.x * 10) / 10}px,${Math.round(v.y * 10) / 10}px) scale(${Math.round(v.s * 1000) / 1000})`);
  showZoom(v.s);
}
// fits the pane and centers (default); only the user panning/zooming takes it out of automatic mode
export function dgFit() {
  const { w, h: hh, top } = vpSize();
  if (!dg.stage || !dg.w || !dg.h || !(w > 0) || !(hh > 0)) return;
  const pad = 16;
  const s = Math.max(0.25, Math.min((w - pad * 2) / dg.w, (hh - pad * 2) / dg.h, SCALE_MAX));
  const view = { s, x: (w - dg.w * s) / 2, y: top + (hh - dg.h * s) / 2 };
  if (![view.s, view.x, view.y].every(Number.isFinite)) return;
  dg.view = view;
  dg.manual = false; dgApply();
}
export function dgScale() { if (!dg.wrap || !dg.wrap.isConnected) return; if (!dg.manual) dgFit(); else dgApply(); }
export function zoomAt(f, cx, cy) {
  const box = document.querySelector('.canvas-wrap'); if (!box || !dg.stage) return;
  const r = box.getBoundingClientRect(), px = cx == null ? r.width / 2 : cx - r.left, py = cy == null ? r.height / 2 : cy - r.top, v = dg.view;
  const s = Math.max(0.2, Math.min(3, v.s * f)), k = s / v.s;
  dg.view = { s, x: px - (px - v.x) * k, y: py - (py - v.y) * k };
  dg.manual = true; dgApply();
}
/** Moves one node (and the edges touching it) in the DOM while dragging; the offset also feeds the next model. */
function moveNodeBy(dragKey, offsetX, offsetY) {
  nodeOffsets.set(dragKey, { x: offsetX, y: offsetY });
  if (!dg.stage) return;
  const isLane = dragKey.startsWith('l:');
  const movedIds = isLane ? Object.keys(layoutSnapshot.laneOf).filter((id) => layoutSnapshot.laneOf[id] === dragKey) : [dragKey];
  const place = (element, base) => { if (element && base) { element.style.left = base.x + offsetX + 'px'; element.style.top = base.y + offsetY + 'px'; } };
  if (isLane) place([...dg.stage.querySelectorAll('.dg-lane')].find((el) => el.dataset.laneKey === dragKey), layoutSnapshot.laneBase[dragKey]);
  for (const id of movedIds) place([...dg.stage.querySelectorAll('.dnode')].find((el) => el.dataset.nodeId === id), layoutSnapshot.base[id]);
  const positionOf = (id) => { const o = offsetOfNode(id); return { x: layoutSnapshot.base[id].x + o.x, y: layoutSnapshot.base[id].y + o.y }; };
  for (const edge of layoutSnapshot.edges) {
    if (!movedIds.includes(edge.from) && !movedIds.includes(edge.to)) continue;
    const pathElement = [...dg.stage.querySelectorAll('path.edge')].find((el) => el.dataset.edgeKey === edge.key);
    if (!pathElement || !layoutSnapshot.base[edge.from] || !layoutSnapshot.base[edge.to]) continue;
    const from = positionOf(edge.from), to = positionOf(edge.to);
    pathElement.setAttribute('d', bez(from.x + edge.fromWidth, from.y + PIPE.NH / 2, to.x - 3, to.y + PIPE.NH / 2));
  }
}
const DRAG_THRESHOLD_PX = 4;
function bindNodeDrag(wrap) {
  let drag = null, suppressClick = false;
  wrap.addEventListener('pointerdown', (e) => {
    const nodeElement = e.button === 0 && e.target.closest('.dnode');
    if (!nodeElement || !nodeElement.dataset.nodeId || !layoutSnapshot.base[nodeElement.dataset.nodeId]) return;
    const dragKey = dragKeyOf(nodeElement.dataset.nodeId), start = nodeOffsets.get(dragKey) || { x: 0, y: 0 };
    drag = { id: e.pointerId, nodeId: dragKey, x: e.clientX, y: e.clientY, startX: start.x, startY: start.y, moved: false };
  });
  wrap.addEventListener('pointermove', (e) => {
    if (!drag || drag.id !== e.pointerId) return;
    const deltaX = e.clientX - drag.x, deltaY = e.clientY - drag.y;
    if (!drag.moved) {
      if (Math.hypot(deltaX, deltaY) < DRAG_THRESHOLD_PX) return;
      drag.moved = true; wrap.setPointerCapture(e.pointerId); wrap.classList.add('node-dragging');
    }
    moveNodeBy(drag.nodeId, drag.startX + deltaX / dg.view.s, drag.startY + deltaY / dg.view.s);
  });
  const end = (e) => {
    if (!drag || drag.id !== e.pointerId) return;
    if (drag.moved) { suppressClick = true; setTimeout(() => { suppressClick = false; }, 0); wrap.classList.remove('node-dragging'); }
    drag = null;
  };
  wrap.addEventListener('pointerup', end); wrap.addEventListener('pointercancel', end);
  wrap.addEventListener('click', (e) => { if (suppressClick) { e.stopPropagation(); e.preventDefault(); } }, true);
}
/** Back to the automatic layout (offsets empty); the next render also repaints from the model. */
export function resetNodeLayout() {
  for (const dragKey of [...nodeOffsets.keys()]) moveNodeBy(dragKey, 0, 0);
  nodeOffsets.clear();
}
function bindPanZoom(wrap) {
  let pan = null;
  bindNodeDrag(wrap);
  wrap.addEventListener('pointerdown', (e) => {
    if (e.button !== 0 || e.target.closest('.dnode')) return;
    wrap.setPointerCapture(e.pointerId);
    pan = { id: e.pointerId, x: e.clientX, y: e.clientY, vx: dg.view.x, vy: dg.view.y };
    wrap.classList.add('panning');
  });
  wrap.addEventListener('pointermove', (e) => {
    if (!pan || pan.id !== e.pointerId) return;
    dg.view = { s: dg.view.s, x: pan.vx + e.clientX - pan.x, y: pan.vy + e.clientY - pan.y }; dg.manual = true; dgApply();
  });
  const end = (e) => { if (pan && pan.id === e.pointerId) { pan = null; wrap.classList.remove('panning'); } };
  wrap.addEventListener('pointerup', end); wrap.addEventListener('pointercancel', end);
  wrap.addEventListener('wheel', (e) => {
    e.preventDefault();
    if (e.ctrlKey || e.metaKey) zoomAt(e.deltaY < 0 ? 1.12 : 1 / 1.12, e.clientX, e.clientY);
    else { dg.view = { s: dg.view.s, x: dg.view.x - e.deltaX, y: dg.view.y - e.deltaY }; dg.manual = true; dgApply(); }
  }, { passive: false });
}
export const bez = (x1, y1, x2, y2) => { const mx = (x1 + x2) / 2; return `M${x1} ${y1} C${mx} ${y1},${mx} ${y2},${x2} ${y2}`; };

// ----- 'Current turn' view: pipeline per feature -----
function agentNode(a, x, y, w, selId, extra) {
  const [c] = pal(a.state), ret = a.return_status ? String(a.return_status) : '';
  const tip = [`${a.id} · ${txt(a.state)}`, a.decision || a.summary || '', a.last_action && a.last_action.text ? 'last action: ' + a.last_action.text : ''].filter(Boolean).join('\n');
  const faded = a.state === 'done' ? 0.68 : a.state === 'idle' || a.state === 'queued' ? 0.6 : 1;
  const timing = a.duration_s != null || a.tokens != null ? `${dur(a.duration_s)} · ${tok(a.tokens)} tok${typeof a.cost_usd === 'number' ? ' · ' + cost(a.cost_usd) : ''}` : '';
  return { key: a.id, x, y, w, h: PIPE.NH, color: c, state: a.state, pulse: a.state === 'running', fail: isBad(a), on: selId === a.id,
    opacity: faded, title: txt(a.role), badge: txt(a.state),
    sub: (extra && extra.sub) || ROLE_WORDS[a.role] || '', chip: ret.toLowerCase() === String(a.state).toLowerCase() ? '' : ret, chipColor: ret ? retColor(ret) : '', act: (extra && extra.act) || currentTask(a), actColor: a.last_action && a.last_action.type === 'tool' ? '#8AA4FF' : '',
    meta: timing, tip, select: () => selectAgent(a.id) };
}
export function agentsModel() {
  const ag = agents();
  if (!ag.length) {
    const n = ui.pastN;
    return { mode: 'plain', msg: n == null ? 'No active agent in this turn.'
      : ui.hist.loading ? `Loading the agents of turn ${n}…` : ui.hist.err ? `Agents of turn ${n} unavailable: ${ui.hist.err}` : `No agents recorded for turn ${n}.` };
  }
  const P = PIPE, selId = ui.sel && ui.sel.kind === 'agent' ? ui.sel.id : null, fById = featById();
  const orch = ag.find((a) => a.role === 'orchestrator'), rest = ag.filter((a) => a !== orch);
  const order = features().map((f) => f.id);
  const laneMap = new Map();
  for (const a of rest) { const k = a.feature || 'general'; if (!laneMap.has(k)) laneMap.set(k, []); laneMap.get(k).push(a); }
  const rank = (k) => (k === 'general' ? 1e6 : order.indexOf(k) >= 0 ? order.indexOf(k) : 1e5);
  const keys = [...laneMap.keys()].sort((a, b) => rank(a) - rank(b));
  const stageColumnOf = (a) => (STAGE_COL[a.role] != null ? STAGE_COL[a.role] : 3);
  const x0 = P.PADX + P.ORCHW + P.GAPX, colX = (i) => x0 + i * (P.NW + P.GAPX);
  // Each lane is its own pipeline (spec-writer -> spec-reviewer -> executor -> verifier), packed from the left; the stage is as wide as the longest.
  const pipelineColumns = (k) => {
    const cols = {};
    for (const a of laneMap.get(k)) (cols[stageColumnOf(a)] = cols[stageColumnOf(a)] || []).push(a);
    return Object.keys(cols).map(Number).sort((p, q) => p - q).map((c) => cols[c]);
  };
  const columnsOf = Object.fromEntries(keys.map((k) => [k, pipelineColumns(k)]));
  const widest = Math.max(0, ...keys.map((k) => columnsOf[k].length));
  const W = widest ? colX(widest - 1) + P.NW + P.PADX : x0 + P.PADX;
  const nodes = [], edges = [], lanes = [], posOf = {}, laneOf = {};
  layoutSnapshot.laneBase = {};
  let y = P.PADY;
  keys.forEach((k, li) => {
    const columns = columnsOf[k];
    const maxStack = Math.max(...columns.map((v) => v.length));
    const laneH = P.LANE_TOP + maxStack * (P.NH + P.GAPY) - P.GAPY + P.LANE_BOT;
    const f = fById[k], laneX = P.PADX + P.ORCHW + P.GAPX / 2 - 4;
    layoutSnapshot.laneBase['l:' + k] = { x: laneX, y };
    lanes.push({ key: 'l:' + k, x: laneX, y, w: W - laneX - 4, h: laneH, alt: li % 2 === 1,
      label: f ? `${f.stage != null ? 'Stage ' + f.stage + ' · ' : ''}${k}` : k === 'general' ? 'General · no feature' : k,
      sub: f ? txt(f.status) : '', subColor: f ? pal(f.status)[0] : '' });
    let previous = null;
    columns.forEach((arr, columnIndex) => {
      const top = y + P.LANE_TOP + ((maxStack - arr.length) * (P.NH + P.GAPY)) / 2;
      arr.forEach((a, j) => { const nx = colX(columnIndex), ny = top + j * (P.NH + P.GAPY); posOf[a.id] = { x: nx, y: ny }; laneOf[a.id] = 'l:' + k; nodes.push(agentNode(a, nx, ny, P.NW, selId)); });
      // the orchestrator points only at the first step of the lane; every other step is chained from the one before it
      for (const a of arr) for (const source of previous || (orch ? [orch] : [])) edges.push({ from: source.id, to: a.id });
      previous = arr;
    });
    y += laneH + P.LANE_GAP;
  });
  const H = keys.length ? y - P.LANE_GAP + P.PADY : P.NH + 2 * P.PADY;
  if (orch) {
    const oy = (H - P.NH) / 2; posOf[orch.id] = { x: P.PADX, y: oy };
    const orchNode = agentNode(orch, P.PADX, oy, P.ORCHW, selId, { sub: txt(projectName()) });
    nodes.unshift(Object.assign(orchNode, { small: true, badge: '', chip: txt(orch.state), chipColor: orchNode.color }));
  }
  const byId = Object.fromEntries(ag.map((a) => [a.id, a]));
  const basePos = Object.fromEntries(Object.entries(posOf).map(([id, p]) => [id, { x: p.x, y: p.y }]));
  layoutSnapshot.laneOf = laneOf; layoutSnapshot.base = basePos; layoutSnapshot.edges = [];
  for (const key of [...nodeOffsets.keys()]) if (!byId[key] && !layoutSnapshot.laneBase[key]) nodeOffsets.delete(key);
  for (const n of nodes) { const o = offsetOfNode(n.key); if (o.x || o.y) { n.x += o.x; n.y += o.y; posOf[n.key] = { x: n.x, y: n.y }; } }
  for (const l of lanes) { const o = nodeOffsets.get(l.key); if (o) { l.x += o.x; l.y += o.y; } }
  const edgeModels = edges.filter((e) => posOf[e.from] && posOf[e.to]).map((e) => {
    const a = byId[e.from], b = byId[e.to], pa = posOf[e.from], pb = posOf[e.to];
    const w1 = a.role === 'orchestrator' ? P.ORCHW : P.NW;
    layoutSnapshot.edges.push({ key: e.from + '>' + e.to, from: e.from, to: e.to, fromWidth: w1 });
    const active = a.state === 'running' || b.state === 'running', bad = isBad(a) || isBad(b), touch = selId === a.id || selId === b.id;
    const waiting = b.state === 'queued' || b.state === 'idle';
    return { key: e.from + '>' + e.to, d: bez(pa.x + w1, pa.y + P.NH / 2, pb.x - 3, pb.y + P.NH / 2), color: bad ? 'red' : active ? 'amber' : 'gray', w: touch ? 2.2 : 1.4,
      dash: bad ? '3 4' : active ? '6 6' : waiting ? '2 4' : null, flow: active && !bad, op: touch || !selId ? 1 : 0.65 };
  });
  return { mode: 'pipe', w: W, h: H, lanes, nodes, edges: edgeModels };
}
