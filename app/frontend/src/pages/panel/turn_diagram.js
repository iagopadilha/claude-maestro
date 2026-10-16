/* "Current turn" view: pipeline per feature (one lane per feature). Pixel layout on a stage scaled
   to fit the screen; Preact reconciles lanes, nodes and edges by key, so a poll without changes does not touch the DOM.
   Pan/zoom stays imperative on the .dg-wrap host (transform on .dg-stage, label in #zoom-val). */
import { $, setStyle } from '../../util/dom.js';
import { html, useRef, useLayoutEffect } from '../../util/preact.js';
import { dur, tok, cost, txt } from '../../util/format.js';
import { ui, pal, agents, features, featById, projectName } from './context.js';
import { selectAgent } from './roadmap_sidebar.js';

const PIPE = { NW: 182, NH: 80, ORCHW: 132, GAPY: 10, GAPX: 26, PADX: 12, PADY: 8, LANE_TOP: 22, LANE_BOT: 8, LANE_GAP: 6 };
const SCALE_MAX = 1.2;
const MARKERS = { gray: '#4A4F57', amber: '#F2B544', red: '#F06A5E', green: '#5BD38A', white: '#E6E6E3' };
const STAGE_COL = { 'spec-writer': 1, 'spec-reviewer': 2, 'executor-back': 3, 'executor-front': 3, 'executor-generic': 3, verifier: 4, 'verifier-flow': 4 };
const dg = { view: { s: 1, x: 0, y: 0 }, manual: false, wrap: null, stage: null, w: 0, h: 0 };
export const retColor = (r) => (/^(APPROVED|PASS|DONE)$/i.test(r) ? '#5BD38A' : /^(BLOCKED|FAIL|TOO_BIG)$/i.test(r) ? '#F06A5E' : '#F2B544');
export const isBad = (a) => a.state === 'failed' || a.state === 'blocked' || /^(BLOCKED|FAIL)$/i.test(String(a.return_status || ''));
export const actText = (a) => (a.last_action && a.last_action.text ? '› ' + a.last_action.text : a.summary ? '› ' + a.summary : '');
const orNull = (s) => (s == null || s === '' ? null : String(s));

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
  return html`<div class=${'dg-lane' + (l.alt ? ' alt' : '') + (l.col ? ' col' : '')} style=${{ left: l.x + 'px', top: l.y + 'px', width: l.w + 'px', height: l.h + 'px' }}>
    <span class="dg-lane-label">${l.label}</span><span class="dg-lane-sub" style=${{ color: l.subColor || '' }}>${orNull(l.sub)}</span>
  </div>`;
}
function DNode({ n }) {
  const cls = 'dnode' + (n.pulse ? ' run' : '') + (n.fail ? ' fail' : '') + (n.on ? ' on' : '') + (n.small ? ' small' : '');
  const style = { left: n.x + 'px', top: n.y + 'px', width: n.w + 'px', height: n.h + 'px', '--c': n.color, '--cg': n.color + '33', opacity: String(n.opacity) };
  return html`<button type="button" class=${cls} style=${style} title=${n.tip} aria-pressed=${n.on ? 'true' : 'false'} onClick=${() => { if (n.select) n.select(); }}>
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
      ${m.edges.map((e) => html`<path key=${e.key} class=${'edge' + (e.flow ? ' flow' : '')} fill="none" stroke-linecap="round" d=${e.d} stroke=${MARKERS[e.color] || MARKERS.gray}
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
    return () => { if (dg.wrap === el) dg.wrap = null; };
  }, []);
  useLayoutEffect(() => { dg.w = model.w; dg.h = model.h; dgScale(); });
  return html`<div class="dg-wrap" ref=${ref}><${TurnDiagram} model=${model} /></div>`;
}
function vpSize() { const b = document.querySelector('.canvas-wrap'); return b ? { w: b.clientWidth, h: b.clientHeight } : { w: 0, h: 0 }; }
function dgApply() {
  if (!dg.stage) return;
  const v = dg.view;
  setStyle(dg.stage, 'transform', `translate(${Math.round(v.x * 10) / 10}px,${Math.round(v.y * 10) / 10}px) scale(${Math.round(v.s * 1000) / 1000})`);
  showZoom(v.s);
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
