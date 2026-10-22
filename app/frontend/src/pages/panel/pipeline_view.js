/* Pipeline view of the LOOP zone: orchestrator at the left, one lane per dispatched feature grouped by WAVE, the
   fixed 6 steps per lane. Shows the state accumulated up to the selected turn (see pipeline_model.js).
   Free canvas: background drag = pan, wheel = scroll (Shift = horizontal), Ctrl/Cmd + wheel or pinch = zoom at the
   cursor; dragging a node or lane moves the whole feature, the wave header moves every feature of the wave.
   Camera and offsets live in this module (not in the component), so switching tabs keeps them.
   The canvas is not infinite: the camera is clamped so the content can never leave the viewport (content smaller than the
   viewport on an axis stays centered on it). The default view is fitted AND centered, on first show and whenever the viewport
   or the content changes, until the user pans or zooms (`view.auto`); "Fit" and "Reset layout" go back to it. */
import { html, useLayoutEffect, useReducer, useRef } from '../../util/preact.js';
import {
  pipe, loadPipeline, steps, curN, liveN, stateAt, featureStatusAt, featuresWithWaves, changedStepsAt, activeNode,
  turnAt, emptyReason, selectNode, featureName, STEP_COLOR, FEATURE_COLOR,
} from './pipeline_model.js';
import { openAgentDialog } from './dialogs/index.js';
import { FeatureLabel } from './feature_label.js';
import { ctx } from './context.js';
import { clampCameraTo, fitCameraTo, sameCamera } from './camera_clamp.js';

// ---------- geometry (px). Kept in JS because the SVG edges need the same coordinates as the nodes ----------
const GEOMETRY = { ORCH_W: 150, ORCH_H: 64, LANE_X: 190, LANE_PAD: 12, NODE_W: 136, NODE_H: 50, STEP_GAP: 166, LANE_H: 92, LANE_TOP: 32, WAVE_H: 26, LANE_GAP: 10, WAVE_GAP: 8 };
const ZOOM_MIN = 0.3, ZOOM_MAX = 2.5, ZOOM_BUTTON_FACTOR = 1.2, FIT_ZOOM_MAX = 1.2, FIT_MARGIN_X = 48, FIT_MARGIN_Y = 120, TEASER_HEIGHT = 70;
const WHEEL_STEP = 1.1, WHEEL_PINCH_LIMIT = 50, DRAG_THRESHOLD_PX = 3;
const ORCHESTRATOR_KEY = 'orch';
const WAVE_DRAG_PREFIX = 'wave:';
const EDGE_KIND = { DONE: 'done', RUNNING: 'running', QUEUED: 'pending', PENDING: 'pending', FAIL: 'fail', BLOCKED: 'fail' };
const EDGE_COLOR = { DONE: STEP_COLOR.DONE, RUNNING: STEP_COLOR.RUNNING, QUEUED: 'var(--line3)', PENDING: 'var(--line3)', FAIL: STEP_COLOR.FAIL, BLOCKED: STEP_COLOR.BLOCKED };
const ARROW_COLORS = { done: 'var(--green)', running: 'var(--amber)', pending: 'var(--line3)', fail: 'var(--red)' };
const ZERO = { x: 0, y: 0 };
const PENDING_STATE = 'PENDING';

/** Module store: survives tab switches and re-renders. offsets = manual displacement over the automatic layout, by feature id / 'orch'. */
const view = { camera: { x: 0, y: 0, z: 1 }, offsets: {}, dragging: false, auto: true, viewport: null, content: null, waveMembers: {} };
let repaint = null, suppressNextClick = false;
const repaintNow = () => { if (repaint) repaint(); };
const clamp = (value, low, high) => Math.max(low, Math.min(high, value));

// ---------- camera ----------
/** Camera moved by the user (pan, zoom, drag): the default fit-and-center view stops following the viewport. */
function takeControl() { view.auto = false; }
/** The camera with the content kept in view (unchanged while the viewport or the content is unknown). */
function clampCamera(camera) {
  const element = view.viewport;
  return element ? clampCameraTo(camera, view.content, element.clientWidth, element.clientHeight) : camera;
}
function setCamera(camera) { view.camera = clampCamera(camera); repaintNow(); }

function zoomAround(factor, anchorX, anchorY) {
  const camera = view.camera, zoom = clamp(camera.z * factor, ZOOM_MIN, ZOOM_MAX);
  takeControl();
  setCamera({ z: zoom, x: anchorX - ((anchorX - camera.x) * zoom) / camera.z, y: anchorY - ((anchorY - camera.y) * zoom) / camera.z });
}
function zoomFromCenter(factor) {
  const element = view.viewport;
  zoomAround(factor, element ? element.clientWidth / 2 : 400, element ? element.clientHeight / 2 : 300);
}
/** Whole content visible and centered on both axes. Returns false while the viewport or the content is not measurable. */
function fitToViewport({ user = false } = {}) {
  const element = view.viewport, content = view.content;
  if (!element || !content || !element.clientWidth || !element.clientHeight) return false;
  const target = fitCameraTo(content, element.clientWidth, element.clientHeight, { minZoom: ZOOM_MIN, maxZoom: FIT_ZOOM_MAX, marginX: FIT_MARGIN_X, marginY: FIT_MARGIN_Y });
  if (user) view.auto = true;
  if (!sameCamera(view.camera, target)) { view.camera = target; repaintNow(); } else if (user) repaintNow();
  return true;
}
function resetLayout() { view.offsets = {}; view.auto = true; if (!fitToViewport({ user: true })) repaintNow(); }

/** Wheel scrolls (Shift = horizontal); Ctrl/Cmd + wheel (a trackpad pinch arrives with ctrlKey) zooms at the cursor. */
function onWheel(event) {
  event.preventDefault();
  const element = view.viewport;
  if (!element) return;
  if (!event.ctrlKey && !event.metaKey) {
    const deltaX = event.shiftKey ? event.deltaY : event.deltaX, deltaY = event.shiftKey ? 0 : event.deltaY;
    takeControl();
    setCamera({ ...view.camera, x: view.camera.x - deltaX, y: view.camera.y - deltaY });
    return;
  }
  const rect = element.getBoundingClientRect();
  const factor = Math.abs(event.deltaY) < WHEEL_PINCH_LIMIT ? Math.exp(-event.deltaY * 0.01) : event.deltaY < 0 ? WHEEL_STEP : 1 / WHEEL_STEP;
  zoomAround(factor, event.clientX - rect.left, event.clientY - rect.top);
}

// ---------- drag: background = pan, node / lane = its feature, wave header = every feature of the wave ----------
function dragKeysOf(target) {
  if (!target) return [];
  if (target === ORCHESTRATOR_KEY) return [ORCHESTRATOR_KEY];
  if (target.startsWith(WAVE_DRAG_PREFIX)) return view.waveMembers[target.slice(WAVE_DRAG_PREFIX.length)] || [];
  return [target];
}
function beginDrag(event) {
  if (event.button !== 0) return;
  const holder = event.target.closest('[data-pl-drag]');
  const keys = dragKeysOf(holder ? holder.getAttribute('data-pl-drag') : null);
  const startX = event.clientX, startY = event.clientY, camera = view.camera, startOffsets = {};
  keys.forEach((key) => { startOffsets[key] = view.offsets[key] || ZERO; });
  let moved = false;
  const onMove = (moveEvent) => {
    const deltaX = moveEvent.clientX - startX, deltaY = moveEvent.clientY - startY;
    if (!moved && Math.hypot(deltaX, deltaY) < DRAG_THRESHOLD_PX) return;
    if (!moved) { moved = true; view.dragging = true; takeControl(); }
    if (keys.length) {
      const next = { ...view.offsets };
      keys.forEach((key) => { next[key] = { x: startOffsets[key].x + deltaX / camera.z, y: startOffsets[key].y + deltaY / camera.z }; });
      view.offsets = next;
    } else view.camera = clampCamera({ ...camera, x: camera.x + deltaX, y: camera.y + deltaY });
    repaintNow();
  };
  const onEnd = () => {
    window.removeEventListener('pointermove', onMove); window.removeEventListener('pointerup', onEnd); window.removeEventListener('pointercancel', onEnd);
    view.dragging = false;
    if (moved) { suppressNextClick = true; setTimeout(() => { suppressNextClick = false; }, 0); }
    repaintNow();
  };
  window.addEventListener('pointermove', onMove); window.addEventListener('pointerup', onEnd); window.addEventListener('pointercancel', onEnd);
}
/** A drag must not count as a click on the node it ended on. */
function swallowClickAfterDrag(event) { if (suppressNextClick) { event.stopPropagation(); event.preventDefault(); } }
const stopPropagation = (event) => event.stopPropagation();

// ---------- layout ----------
const curve = (x1, y1, x2, y2, pullOut, pullIn) => `M${x1},${y1} C${x1 + pullOut},${y1} ${x2 - pullIn},${y2} ${x2 - 2},${y2}`;
const offsetOf = (key) => view.offsets[key] || ZERO;

/** Features that appear in the replayed state: the roadmap ones, plus any slug the events mention that the roadmap does not know. */
function featuresInState(state) {
  const known = featuresWithWaves(), ids = new Set(known.map((feature) => feature.id));
  const extra = Object.keys(state).filter((id) => !ids.has(id)).map((id) => ({ id, name: id, stage: null, wave: 1, deps: [] }));
  return [...known, ...extra];
}

/** Everything the diagram and the list draw for turn `turn`: lanes, nodes, edges, wave headers, orchestrator, teaser. */
function buildGraph(turn) {
  const G = GEOMETRY, stepList = steps(), state = stateAt(turn), changed = changedStepsAt(turn), selected = activeNode();
  const laneWidth = G.LANE_PAD * 2 + (stepList.length - 1) * G.STEP_GAP + G.NODE_W, width = G.LANE_X + laneWidth;
  const allFeatures = featuresInState(state), dispatched = allFeatures.filter((feature) => state[feature.id]);
  const waveNumbers = [...new Set(dispatched.map((feature) => feature.wave))].sort((a, b) => a - b);
  const lanes = [], nodes = [], edges = [], waves = [], starts = [];
  view.waveMembers = {};
  let y = 0;
  for (const waveNumber of waveNumbers) {
    const members = dispatched.filter((feature) => feature.wave === waveNumber);
    view.waveMembers[waveNumber] = members.map((feature) => feature.id);
    const concluded = members.every((feature) => featureStatusAt(feature.id, turn) === 'DONE');
    waves.push({ n: waveNumber, x: G.LANE_X, y, label: concluded ? 'concluded' : 'in progress', color: concluded ? FEATURE_COLOR.DONE : FEATURE_COLOR.RUNNING });
    y += G.WAVE_H;
    const firstLaneOfWave = lanes.length;
    for (const feature of members) {
      const status = featureStatusAt(feature.id, turn), offset = offsetOf(feature.id), nodeTop = y + G.LANE_TOP;
      const stepStates = state[feature.id];
      lanes.push({ id: feature.id, name: feature.name, stage: feature.stage, wave: feature.wave, x: G.LANE_X + offset.x, y: y + offset.y, w: laneWidth, h: G.LANE_H,
        status, color: FEATURE_COLOR[status] || FEATURE_COLOR['IN PROGRESS'], quarantined: status === 'QUARANTINE', steps: [] });
      let previous = null;
      stepStates.forEach((step, index) => {
        const x = G.LANE_X + G.LANE_PAD + index * G.STEP_GAP + offset.x, top = nodeTop + offset.y;
        const lastRun = step.runs[step.runs.length - 1] || null, isSelected = !!selected && !selected.orch && selected.f === feature.id && selected.s === index;
        const node = { key: `${feature.id}:${index}`, feature: feature.id, step: index, agent: (lastRun && lastRun.agent) || stepList[index].agent, role: stepList[index].role, st: step.st,
          color: STEP_COLOR[step.st] || STEP_COLOR.PENDING, x, y: top, attempts: step.runs.length > 1 ? `${step.runs.length}×` : '', tagged: changed.has(`${feature.id}:${index}`), tag: `T${turn}`, selected: isSelected };
        nodes.push(node);
        lanes[lanes.length - 1].steps.push(node);
        if (index === 0) starts.push({ x, y: top + G.NODE_H / 2, st: step.st });
        // bezier edges keep looking right after the user drags a feature somewhere else
        if (previous) edges.push({ key: `${previous.key}>${node.key}`, d: curve(previous.x + G.NODE_W, previous.y + G.NODE_H / 2, x, top + G.NODE_H / 2, 18, 20), st: step.st });
        previous = node;
      });
      y += G.LANE_H + G.LANE_GAP;
    }
    // the wave header follows its lanes: above the highest one and aligned to the leftmost one, even after a drag
    const waveLanes = lanes.slice(firstLaneOfWave), header = waves[waves.length - 1];
    header.x = Math.min(...waveLanes.map((lane) => lane.x)); header.y = Math.min(...waveLanes.map((lane) => lane.y)) - G.WAVE_H;
    y += G.WAVE_GAP;
  }
  const height = Math.max(y, 120), orchestratorOffset = offsetOf(ORCHESTRATOR_KEY);
  const orchestrator = { x: orchestratorOffset.x, y: Math.max(0, height / 2 - G.ORCH_H / 2) + orchestratorOffset.y };
  starts.forEach((start, index) => edges.push({ key: `orch>${index}`, d: curve(orchestrator.x + G.ORCH_W, orchestrator.y + G.ORCH_H / 2, start.x, start.y, 30, 30), st: start.st }));
  // teaser: the lowest wave that is not dispatched yet, shown only as text until the orchestrator dispatches it
  const waiting = allFeatures.filter((feature) => !state[feature.id]);
  const nextWave = waiting.length ? Math.min(...waiting.map((feature) => feature.wave)) : null;
  const next = nextWave == null ? [] : waiting.filter((feature) => feature.wave === nextWave).map((feature) => ({ id: feature.id, deps: feature.deps }));
  // box of everything the canvas shows (dragged features included): the camera is clamped against it
  const boxes = [...lanes.map((lane) => [lane.x, lane.y, lane.x + lane.w, lane.y + lane.h]), ...waves.map((wave) => [wave.x, wave.y, wave.x + G.LANE_X, wave.y + G.WAVE_H]),
    [orchestrator.x, orchestrator.y, orchestrator.x + G.ORCH_W, orchestrator.y + G.ORCH_H]];
  const bounds = { x0: Math.min(...boxes.map((box) => box[0])), y0: Math.min(...boxes.map((box) => box[1])),
    x1: Math.max(...boxes.map((box) => box[2])), y1: Math.max(height, ...boxes.map((box) => box[3])) + (next.length ? TEASER_HEIGHT : 0) };
  return { width, height, lanes, nodes, edges, waves, orchestrator, next, nextWave, selected, turn, changedCount: changed.size, bounds };
}

function orchestratorSummary(turn) {
  const data = pipe.data, record = turnAt(turn), isLive = turn === liveN();
  if (!isLive) return { text: 'turn concluded', color: FEATURE_COLOR.DONE };
  if (data && data.paused) return { text: 'paused · waiting for you', color: FEATURE_COLOR.RUNNING };
  if (record && record.status === 'awaiting_user') return { text: 'waiting for you', color: FEATURE_COLOR.RUNNING };
  if (record && record.status === 'error') return { text: 'error', color: FEATURE_COLOR.QUARANTINE };
  const running = Object.values(stateAt(turn)).reduce((total, stepStates) => total + stepStates.filter((step) => step.st === 'RUNNING').length, 0);
  if (record && record.status === 'running') return { text: `dispatch · ${running} ${running === 1 ? 'agent' : 'agents'}`, color: FEATURE_COLOR.RUNNING };
  return { text: 'turn concluded', color: FEATURE_COLOR.DONE };
}

const openAgent = (feature, step) => openAgentDialog({ feature, step });
const onActivateKey = (action) => (event) => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); action(); } };
const waitsOn = (item) => { const waitedOn = item.deps.filter((dep) => !dep.startsWith('dir:')).map(featureName); return waitedOn.length ? `waits on ${waitedOn.join(', ')}` : 'ready to be dispatched'; };

// ---------- markup ----------
function Teaser({ graph }) {
  if (!graph.next.length) return null;
  return html`<div class="pl-teaser" style=${{ maxWidth: graph.width + 'px' }}>
    <div class="pl-teaser-title">NEXT · not dispatched yet (each one starts when its stage and its dependencies allow)</div>
    <div class="pl-teaser-list">${graph.next.map((item) => html`<span key=${item.id} class="pl-teaser-item"><${FeatureLabel} slug=${item.id} className="pl-teaser-id" /> ${waitsOn(item)}</span>`)}</div>
  </div>`;
}

function Edges({ graph }) {
  return html`<svg class="pl-edges" width=${graph.width} height=${graph.height} aria-hidden="true">
    <defs>${Object.entries(ARROW_COLORS).map(([kind, color]) => html`<marker key=${kind} id=${'pl-arrow-' + kind} viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto"><path d="M0,0 L8,4 L0,8 z" fill=${color} /></marker>`)}</defs>
    ${graph.edges.map((edge) => html`<path key=${edge.key} d=${edge.d} fill="none" stroke=${EDGE_COLOR[edge.st] || EDGE_COLOR.PENDING} stroke-width="1.5"
      stroke-dasharray=${edge.st === 'PENDING' || edge.st === 'QUEUED' ? '4 4' : null} marker-end=${`url(#pl-arrow-${EDGE_KIND[edge.st] || 'pending'})`} />`)}
  </svg>`;
}

function StepNode({ node }) {
  const cls = 'pl-node' + (node.st === PENDING_STATE ? ' pending' : '') + (node.st === 'FAIL' || node.st === 'BLOCKED' ? ' failed' : '') + (node.selected ? ' selected' : '');
  const select = () => selectNode({ f: node.feature, s: node.step });
  return html`<div class=${cls} data-pl-drag=${node.feature} role="button" tabindex="0" title=${`${node.role} · ${node.st}`}
    style=${{ left: node.x + 'px', top: node.y + 'px', '--pl-color': node.color }}
    onClick=${select} onDblClick=${() => openAgent(node.feature, node.step)} onKeyDown=${onActivateKey(select)}>
    <div class="pl-node-top"><span class="pl-node-agent"><span class="pl-dot"></span><span class="pl-node-name">${node.agent}</span></span>${node.tagged ? html`<span class="pl-tag">${node.tag}</span>` : null}</div>
    <div class="pl-node-bottom"><span class="pl-node-status">${node.st}</span><span class="pl-node-attempts">${node.attempts}</span></div>
  </div>`;
}

function Diagram({ graph }) {
  const orchestrator = graph.orchestrator, summary = orchestratorSummary(graph.turn), orchestratorSelected = !!graph.selected && !!graph.selected.orch;
  return html`<div class="pl-layer" style=${{ transform: `translate(${view.camera.x}px,${view.camera.y}px) scale(${view.camera.z})` }}>
    <div class="pl-content" style=${{ width: graph.width + 'px', height: graph.height + 'px' }}>
      <${Edges} graph=${graph} />
      ${graph.waves.map((wave) => html`<div key=${wave.n} class="pl-wave" data-pl-drag=${WAVE_DRAG_PREFIX + wave.n} title="Drag to move the whole wave" style=${{ left: wave.x + 'px', top: wave.y + 'px' }}>
        <span class="pl-wave-name">WAVE ${wave.n}</span><span style=${{ color: wave.color }}>${wave.label}</span></div>`)}
      ${graph.lanes.map((lane) => html`<div key=${lane.id} class=${'pl-lane' + (lane.quarantined ? ' quarantined' : '')} data-pl-drag=${lane.id}
        style=${{ left: lane.x + 'px', top: lane.y + 'px', width: lane.w + 'px', height: lane.h + 'px' }}>
        <div class="pl-lane-head"><span class="pl-lane-stage">${lane.stage == null ? 'STAGE ?' : 'STAGE ' + lane.stage}</span><${FeatureLabel} slug=${lane.id} className="pl-lane-id" />
          <span class="pl-badge" style=${{ color: lane.color, borderColor: lane.color }}>${lane.status}</span></div>
      </div>`)}
      <div class=${'pl-orchestrator' + (orchestratorSelected ? ' selected' : '')} data-pl-drag=${ORCHESTRATOR_KEY} role="button" tabindex="0"
        style=${{ left: orchestrator.x + 'px', top: orchestrator.y + 'px' }}
        onClick=${() => selectNode({ orch: true })} onKeyDown=${onActivateKey(() => selectNode({ orch: true }))}>
        <div class="pl-orchestrator-name"><span class="pl-orchestrator-dot" style=${{ background: summary.color }}></span>orchestrator</div>
        <div class="pl-orchestrator-state">${summary.text}</div>
      </div>
      ${graph.nodes.map((node) => html`<${StepNode} key=${node.key} node=${node} />`)}
    </div>
    <${Teaser} graph=${graph} />
  </div>`;
}

function Toolbar() {
  return html`<div class="pl-toolbar" onPointerDown=${stopPropagation}>
    <div class="pl-hints"><kbd>wheel</kbd>scroll<kbd>Ctrl + wheel</kbd>zoom<kbd>drag</kbd>move</div>
    <button type="button" class="pl-tool" title="Zoom out" onClick=${() => zoomFromCenter(1 / ZOOM_BUTTON_FACTOR)}>−</button>
    <span class="pl-zoom">${Math.round(view.camera.z * 100)}%</span>
    <button type="button" class="pl-tool" title="Zoom in" onClick=${() => zoomFromCenter(ZOOM_BUTTON_FACTOR)}>+</button>
    <button type="button" class="pl-tool-text" onClick=${() => fitToViewport({ user: true })}>Fit</button>
    <button type="button" class="pl-tool-text subtle" onClick=${resetLayout}>Reset layout</button>
  </div>`;
}

function Legend({ turn }) {
  return html`<div class="pl-legend" onPointerDown=${stopPropagation}>
    <span><span class="pl-legend-line" style=${{ color: ARROW_COLORS.done }}>━</span> done</span>
    <span><span class="pl-legend-line" style=${{ color: ARROW_COLORS.running }}>━</span> running</span>
    <span><span class="pl-legend-line" style=${{ color: ARROW_COLORS.pending }}>╌</span> pending</span>
    <span><span class="pl-legend-line" style=${{ color: ARROW_COLORS.fail }}>━</span> failed / blocked</span>
    <span><span class="pl-tag">T${turn}</span> changed in this turn</span>
  </div>`;
}

/** Task chips of a building feature, one row per wave. Reads `tasks` from the panel state; no tasks = nothing drawn. */
function TaskWaves({ slug }) {
  const feature = (ctx.state && Array.isArray(ctx.state.features) ? ctx.state.features : []).find((item) => item && item.id === slug);
  const tasks = feature && feature.tasks && Array.isArray(feature.tasks.tasks) ? feature.tasks.tasks : [];
  if (!tasks.length) return null;
  const waves = [...new Set(tasks.map((task) => task.wave || 1))].sort((a, b) => a - b);
  return html`<div class="pl-task-waves">${waves.map((wave) => html`<div key=${wave} class="pl-task-wave"><span class="pl-task-wave-name mono">Wave ${wave}</span>
    ${tasks.filter((task) => (task.wave || 1) === wave).map((task) => html`<span key=${task.id} class=${'pl-task-chip ' + String(task.status || 'pending')}
      title=${task.status === 'failed' && task.note ? String(task.note) : String(task.title || task.id)}>${task.status === 'running' ? html`<span class="spinner sm"></span>` : null}${task.status === 'done' ? '✓ ' : ''}${task.id}</span>`)}</div>`)}</div>`;
}

/** List mode: the same information, one card per feature (useful on narrow screens). */
function ListMode({ graph }) {
  return html`<div class="pl-list">
    ${graph.lanes.map((lane) => html`<div key=${lane.id} class=${'pl-card' + (lane.quarantined ? ' quarantined' : '')}>
      <div class="pl-card-head"><span class="pl-lane-stage">W${lane.wave} · ${lane.stage == null ? 'STAGE ?' : 'STAGE ' + lane.stage}</span><${FeatureLabel} slug=${lane.id} className="pl-lane-id" />
        <span class="pl-badge" style=${{ color: lane.color, borderColor: lane.color }}>${lane.status}</span></div>
      <div class="pl-card-steps">${lane.steps.map((node) => html`<button type="button" key=${node.key} class=${'pl-step' + (node.tagged ? ' tagged' : '') + (node.selected ? ' selected' : '')}
        style=${{ color: node.color }} title=${node.role} onClick=${() => selectNode({ f: node.feature, s: node.step })} onDblClick=${() => openAgent(node.feature, node.step)}>${node.agent} · ${node.st}${node.attempts ? ' · ' + node.attempts : ''}</button>`)}</div>
      <${TaskWaves} slug=${lane.id} />
    </div>`)}
    <${Teaser} graph=${graph} />
  </div>`;
}

/** Pipeline tab of the LOOP zone (diagram or list mode, from pipe.view). */
export function PipelineView() {
  const [, bump] = useReducer((count) => count + 1, 0);
  const viewportRef = useRef(null);
  const reason = emptyReason(), turn = curN(), isList = pipe.view === 'list';
  const graph = !reason && turn != null ? buildGraph(turn) : null;
  const hasLanes = !!graph && graph.lanes.length > 0, showsCanvas = hasLanes && !isList;
  view.content = hasLanes ? graph.bounds : null;
  if (showsCanvas && view.viewport) view.camera = clampCamera(view.camera); // content moved (turn picked, feature dragged): keep it in view

  useLayoutEffect(() => { repaint = bump; return () => { if (repaint === bump) repaint = null; }; }, []);
  // the wheel handler is native because Preact's onWheel is passive and could not stop the page from scrolling
  useLayoutEffect(() => {
    const element = viewportRef.current;
    view.viewport = element;
    if (!element) return undefined;
    element.addEventListener('wheel', onWheel, { passive: false });
    return () => { element.removeEventListener('wheel', onWheel); if (view.viewport === element) view.viewport = null; };
  }, [showsCanvas]);
  // default view = fitted and centered: on first show, when the content changes and when the viewport is resized, until the user takes the camera
  useLayoutEffect(() => { if (showsCanvas && view.auto) fitToViewport(); });
  useLayoutEffect(() => {
    const element = viewportRef.current;
    if (!element || typeof ResizeObserver !== 'function') return undefined;
    const observer = new ResizeObserver(() => { if (view.auto) fitToViewport(); else { const clamped = clampCamera(view.camera); if (!sameCamera(clamped, view.camera)) { view.camera = clamped; repaintNow(); } } });
    observer.observe(element);
    return () => observer.disconnect();
  }, [showsCanvas]);

  if (reason) {
    return html`<div class="pl-empty">${reason}${pipe.err && !pipe.loading ? html` <button type="button" class="pl-tool-text" onClick=${() => loadPipeline(true)}>Retry</button>` : null}</div>`;
  }
  if (!graph) return html`<div class="pl-empty">No turn to show.</div>`;
  if (!hasLanes) {
    return html`<div class="pl-empty-wrap"><div class="pl-empty">No feature dispatched yet in turn ${turn}.</div><${Teaser} graph=${graph} /></div>`;
  }
  if (isList) return html`<div class="pl-canvas pl-canvas-list"><${ListMode} graph=${graph} /></div>`;
  // the legend sits in its own strip under the canvas, so it never covers a lane or the NEXT teaser
  return html`<div class="pl-shell">
    <div class=${'pl-viewport' + (view.dragging ? ' dragging' : '')} ref=${viewportRef} onPointerDown=${beginDrag} onClickCapture=${swallowClickAfterDrag}>
      <${Diagram} graph=${graph} />
      <${Toolbar} />
    </div>
    <${Legend} turn=${turn} />
  </div>`;
}
