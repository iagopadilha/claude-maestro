/* "Current turn" view: pipeline per feature (one lane per feature). Pixel layout on a stage scaled
   to fit the screen; nodes and edges are reconciled by key, so a poll without changes does not touch the DOM. */
import { $, h, sv, fill, setAttr, setText, setStyle } from '../../util/dom.js';
import { dur, tok, cost, txt } from '../../util/format.js';
import { ui, pal, agents, features, featById, projectName } from './context.js';
import { selectAgent } from './roadmap_sidebar.js';

const PIPE = { NW: 182, NH: 80, ORCHW: 132, GAPY: 10, GAPX: 26, PADX: 12, PADY: 8, LANE_TOP: 22, LANE_BOT: 8, LANE_GAP: 6 };
const SCALE_MAX = 1.2;
const MARKERS = { gray: '#4A4F57', amber: '#F2B544', red: '#F06A5E', green: '#5BD38A', white: '#E6E6E3' };
const STAGE_COL = { 'spec-writer': 1, 'spec-reviewer': 2, 'executor-back': 3, 'executor-front': 3, 'executor-generic': 3, verifier: 4, 'verifier-flow': 4 };
const dg = { view: { s: 1, x: 0, y: 0 }, manual: false, mode: null, wrap: null, stage: null, svg: null, nodes: new Map(), edges: new Map(), lanes: new Map(), w: 0, h: 0 };
export const retColor = (r) => (/^(APPROVED|PASS|DONE)$/i.test(r) ? '#5BD38A' : /^(BLOCKED|FAIL|TOO_BIG)$/i.test(r) ? '#F06A5E' : '#F2B544');
export const isBad = (a) => a.state === 'failed' || a.state === 'blocked' || /^(BLOCKED|FAIL)$/i.test(String(a.return_status || ''));
export const actText = (a) => (a.last_action && a.last_action.text ? '› ' + a.last_action.text : a.summary ? '› ' + a.summary : '');

function dgBuild(mode) {
  const cv = $('canvas');
  cv.className = 'canvas dg'; cv.style.minHeight = ''; cv.style.minWidth = '';
  const svg = sv('svg', { class: 'dg-svg', 'aria-hidden': 'true' });
  const defs = sv('defs');
  for (const [k, c] of Object.entries(MARKERS)) {
    const m = sv('marker', { id: 'arr-' + k, viewBox: '0 0 8 8', refX: 7, refY: 4, markerWidth: 8, markerHeight: 8, orient: 'auto', markerUnits: 'userSpaceOnUse' });
    m.appendChild(sv('path', { d: 'M0 0 L8 4 L0 8 z', fill: c }));
    defs.appendChild(m);
  }
  svg.appendChild(defs);
  const stage = h('div', { class: 'dg-stage' }, svg), wrap = h('div', { class: 'dg-wrap' }, stage);
  fill(cv, wrap);
  const box = document.querySelector('.canvas-wrap'); if (box) { box.classList.add('dgmode'); box.classList.remove('fullmode'); }
  bindPanZoom(wrap);
  dg.manual = false;
  dg.mode = mode; dg.wrap = wrap; dg.stage = stage; dg.svg = svg;
  dg.nodes = new Map(); dg.edges = new Map(); dg.lanes = new Map();
}
function syncKeyed(map, items, mk, patch, parent) {
  const seen = new Set();
  for (const it of items) {
    seen.add(it.key);
    let el = map.get(it.key);
    if (!el) { el = mk(); map.set(it.key, el); parent.appendChild(el); }
    patch(el, it);
  }
  for (const [k, el] of [...map]) if (!seen.has(k)) { el.remove(); map.delete(k); }
}
const mkEdge = () => sv('path', { fill: 'none', 'stroke-linecap': 'round' });
function patchEdge(el, e) {
  setAttr(el, 'd', e.d); setAttr(el, 'stroke', MARKERS[e.color] || MARKERS.gray); setAttr(el, 'stroke-width', e.w); setAttr(el, 'opacity', e.op);
  setAttr(el, 'stroke-dasharray', e.dash || null); setAttr(el, 'marker-end', `url(#arr-${e.color})`);
  setAttr(el, 'class', 'edge' + (e.flow ? ' flow' : ''));
}
function mkLane() {
  const label = h('span', { class: 'dg-lane-label' }), sub = h('span', { class: 'dg-lane-sub' });
  const el = h('div', { class: 'dg-lane' }, label, sub);
  el._r = { label, sub };
  return el;
}
function patchLane(el, l) {
  el.className = 'dg-lane' + (l.alt ? ' alt' : '') + (l.col ? ' col' : '');
  setStyle(el, 'left', l.x + 'px'); setStyle(el, 'top', l.y + 'px'); setStyle(el, 'width', l.w + 'px'); setStyle(el, 'height', l.h + 'px');
  setText(el._r.label, l.label); setText(el._r.sub, l.sub || ''); setStyle(el._r.sub, 'color', l.subColor || '');
}
function mkNode() {
  const dot = h('span', { class: 'dot' }), title = h('span', { class: 'dn-title' }), badge = h('span', { class: 'dn-badge' });
  const sub = h('span', { class: 'dn-subtxt' }), chip = h('span', { class: 'dn-chip' });
  const act = h('span', { class: 'dn-act' }), meta = h('span', { class: 'dn-meta' }), chips = h('span', { class: 'dn-chips' });
  const el = h('button', { type: 'button', class: 'dnode' }, h('span', { class: 'dn-top' }, dot, title, badge), h('span', { class: 'dn-sub' }, sub, chip), act, meta, chips);
  el._r = { dot, title, badge, sub, chip, act, meta, chips, chipsSig: '' };
  el.addEventListener('click', () => { if (el._n && el._n.select) el._n.select(); });
  return el;
}
function patchNode(el, n) {
  el._n = n;
  const r = el._r;
  setStyle(el, 'left', n.x + 'px'); setStyle(el, 'top', n.y + 'px'); setStyle(el, 'width', n.w + 'px'); setStyle(el, 'height', n.h + 'px');
  const cls = 'dnode' + (n.pulse ? ' run' : '') + (n.fail ? ' fail' : '') + (n.on ? ' on' : '') + (n.small ? ' small' : '');
  if (el.className !== cls) el.className = cls;
  el.style.setProperty('--c', n.color); el.style.setProperty('--cg', n.color + '33');
  setStyle(el, 'opacity', String(n.opacity)); setAttr(el, 'title', n.tip); setAttr(el, 'aria-pressed', n.on ? 'true' : 'false');
  setText(r.title, n.title); setText(r.badge, n.badge); setStyle(r.badge, 'color', n.color); setStyle(r.dot, 'background', n.color);
  setText(r.sub, n.sub); setText(r.chip, n.chip); setStyle(r.chip, 'color', n.chipColor || ''); setStyle(r.chip, 'borderColor', n.chipColor || '');
  setText(r.act, n.act); setStyle(r.act, 'color', n.actColor || ''); setText(r.meta, n.meta);
  const sig = (n.chips || []).join('\u0001');
  if (sig !== r.chipsSig) { r.chipsSig = sig; r.chips.replaceChildren(...(n.chips || []).map((c) => h('span', { class: 'dn-pill', text: c }))); }
}
export function dgRender(m) {
  const cv = $('canvas');
  if (!dg.wrap || dg.wrap.parentNode !== cv || dg.mode !== m.mode) dgBuild(m.mode);
  dg.w = m.w; dg.h = m.h;
  setStyle(dg.stage, 'width', m.w + 'px'); setStyle(dg.stage, 'height', m.h + 'px');
  setAttr(dg.svg, 'width', m.w); setAttr(dg.svg, 'height', m.h); setAttr(dg.svg, 'viewBox', `0 0 ${m.w} ${m.h}`);
  syncKeyed(dg.lanes, m.lanes, mkLane, patchLane, dg.stage);
  syncKeyed(dg.edges, m.edges, mkEdge, patchEdge, dg.svg);
  syncKeyed(dg.nodes, m.nodes, mkNode, patchNode, dg.stage);
  dgScale();
}
function vpSize() { const b = document.querySelector('.canvas-wrap'); return b ? { w: b.clientWidth, h: b.clientHeight } : { w: 0, h: 0 }; }
function dgApply() {
  if (!dg.stage) return;
  const v = dg.view;
  setStyle(dg.stage, 'transform', `translate(${Math.round(v.x * 10) / 10}px,${Math.round(v.y * 10) / 10}px) scale(${Math.round(v.s * 1000) / 1000})`);
  const z = $('zoom-val'); if (z) setText(z, Math.round(v.s * 100) + '%');
}
// fits the pane and centers (default); only the user panning/zooming takes it out of automatic mode
export function dgFit() {
  const { w, h: hh } = vpSize();
  if (!dg.stage || !dg.w || !w || !hh) return;
  const pad = 16;
  const s = Math.max(0.25, Math.min((w - pad * 2) / dg.w, (hh - pad * 2) / dg.h, SCALE_MAX));
  dg.view = { s, x: (w - dg.w * s) / 2, y: (hh - dg.h * s) / 2 };
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
function bindPanZoom(wrap) {
  let pan = null;
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

/** Center in text mode (history, empty messages). */
export function plainCanvas(...kids) {
  const cv = $('canvas'); const bx = document.querySelector('.canvas-wrap');
  if (bx) { bx.classList.remove('dgmode'); bx.classList.remove('fullmode'); }
  cv.className = 'canvas plain'; cv.style.minHeight = ''; cv.style.minWidth = ''; fill(cv, ...kids);
}
/** Center filling everything (graph, files, whiteboard): the content handles its own scrolling. */
export function fullCanvas(node) {
  const cv = $('canvas'); const bx = document.querySelector('.canvas-wrap');
  if (bx) { bx.classList.remove('dgmode'); bx.classList.add('fullmode'); }
  cv.className = 'canvas full'; cv.style.minHeight = ''; cv.style.minWidth = '';
  if (cv.firstChild !== node || cv.childNodes.length !== 1) fill(cv, node);
}
export const emptyMsg = (t) => h('div', { class: 'empty-canvas', text: t });

// ----- 'Current turn' view: pipeline per feature -----
function agentNode(a, x, y, w, selId, extra) {
  const [c] = pal(a.state), ret = a.return_status ? String(a.return_status) : '';
  const tip = [`${a.id} · ${txt(a.state)}`, a.decision || a.summary || '', a.last_action && a.last_action.text ? 'last action: ' + a.last_action.text : ''].filter(Boolean).join('\n');
  return { key: a.id, x, y, w, h: PIPE.NH, color: c, pulse: a.state === 'running', fail: isBad(a), on: selId === a.id,
    opacity: a.state === 'idle' || a.state === 'queued' ? 0.62 : 1, title: txt(a.role), badge: txt(a.state),
    sub: (extra && extra.sub) || a.feature || '', chip: ret, chipColor: ret ? retColor(ret) : '', act: actText(a), actColor: a.last_action && a.last_action.type === 'tool' ? '#8AA4FF' : '',
    meta: `${dur(a.duration_s)} · ${tok(a.tokens)} tok${typeof a.cost_usd === 'number' ? ' · ' + cost(a.cost_usd) : ''}`, tip, select: () => selectAgent(a.id) };
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
  const used = [...new Set(rest.map((a) => (STAGE_COL[a.role] != null ? STAGE_COL[a.role] : 3)))].sort((a, b) => a - b);
  const colIdx = Object.fromEntries(used.map((c, i) => [c, i]));
  const x0 = P.PADX + P.ORCHW + P.GAPX, colX = (i) => x0 + i * (P.NW + P.GAPX);
  const W = used.length ? colX(used.length - 1) + P.NW + P.PADX : x0 + P.PADX;
  const nodes = [], edges = [], lanes = [], posOf = {};
  let y = P.PADY;
  keys.forEach((k, li) => {
    const cols = {};
    for (const a of laneMap.get(k)) { const c = STAGE_COL[a.role] != null ? STAGE_COL[a.role] : 3; (cols[c] = cols[c] || []).push(a); }
    const maxStack = Math.max(...Object.values(cols).map((v) => v.length));
    const laneH = P.LANE_TOP + maxStack * (P.NH + P.GAPY) - P.GAPY + P.LANE_BOT;
    const f = fById[k];
    lanes.push({ key: 'l:' + k, x: P.PADX + P.ORCHW + P.GAPX / 2 - 4, y, w: W - (P.PADX + P.ORCHW + P.GAPX / 2 - 4) - 4, h: laneH, alt: li % 2 === 1, label: k,
      sub: f ? f.status : k === 'general' ? 'no feature' : '', subColor: f ? pal(f.status)[0] : '' });
    let prev = null;
    for (const c of Object.keys(cols).map(Number).sort((a, b) => a - b)) {
      const arr = cols[c], top = y + P.LANE_TOP + ((maxStack - arr.length) * (P.NH + P.GAPY)) / 2;
      arr.forEach((a, j) => { const nx = colX(colIdx[c]), ny = top + j * (P.NH + P.GAPY); posOf[a.id] = { x: nx, y: ny }; nodes.push(agentNode(a, nx, ny, P.NW, selId)); });
      prev = prev || { first: true };
      for (const a of arr) {
        const srcs = prev.list || (orch ? [orch] : []);
        for (const s of srcs) edges.push({ from: s.id, to: a.id });
      }
      prev = { list: arr };
    }
    y += laneH + P.LANE_GAP;
  });
  const H = keys.length ? y - P.LANE_GAP + P.PADY : P.NH + 2 * P.PADY;
  if (orch) {
    const oy = (H - P.NH) / 2; posOf[orch.id] = { x: P.PADX, y: oy };
    const orchNode = agentNode(orch, P.PADX, oy, P.ORCHW, selId, { sub: txt(projectName()) });
    nodes.unshift(Object.assign(orchNode, { small: true, badge: '', chip: txt(orch.state), chipColor: orchNode.color }));
  }
  const byId = Object.fromEntries(ag.map((a) => [a.id, a]));
  const edgeModels = edges.filter((e) => posOf[e.from] && posOf[e.to]).map((e) => {
    const a = byId[e.from], b = byId[e.to], pa = posOf[e.from], pb = posOf[e.to];
    const w1 = a.role === 'orchestrator' ? P.ORCHW : P.NW;
    const active = a.state === 'running' || b.state === 'running', bad = isBad(a) || isBad(b), touch = selId === a.id || selId === b.id;
    const waiting = b.state === 'queued' || b.state === 'idle';
    return { key: e.from + '>' + e.to, d: bez(pa.x + w1, pa.y + P.NH / 2, pb.x - 3, pb.y + P.NH / 2), color: bad ? 'red' : active ? 'amber' : 'gray', w: touch ? 2.2 : 1.4,
      dash: bad ? '3 4' : active ? '6 6' : waiting ? '2 4' : null, flow: active && !bad, op: touch || !selId ? 1 : 0.65 };
  });
  return { mode: 'pipe', w: W, h: H, lanes, nodes, edges: edgeModels };
}
