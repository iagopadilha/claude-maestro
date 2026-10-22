/* Dependencies dialog: UML component diagram of the whole roadmap (a global view, no chat).
   «wave» packages group the features of a wave in a column; each «feature» box shows stage, wave and status at the selected
   turn (dashed border = not dispatched yet); a dashed «use» arrow with an open head goes from the dependent feature to the
   feature it depends on. Mouse wheel scrolls, Ctrl + wheel zooms at the cursor, dragging the background pans, dragging a
   box moves the feature and dragging a «wave» tab moves the whole package. The camera is clamped so the diagram can never be
   panned out of view (smaller than the viewport on an axis = centered there); the default view is fitted and centered, also
   when the dialog is resized, until the user pans or zooms ("Fit" goes back to it). */
import { html, useState, useEffect, useLayoutEffect, useRef } from '../../../util/preact.js';
import { list } from '../../../util/format.js';
import { openBigDialog } from '../../../components/big_dialog.js';
import { featuresWithWaves, featureStatusAt, curN, FEATURE_COLOR } from '../pipeline_model.js';
import { featureParts } from '../feature_label.js';
import { clampCameraTo, fitCameraTo, sameCamera } from '../camera_clamp.js';

const BADGE_COLOR = '#7fb2ff';
const NEUTRAL_COLOR = '#6b727c';
const BOX_WIDTH = 250, BOX_HEIGHT = 136;
const COLUMN_STEP = 310, ROW_STEP = 166; // distance between wave columns / feature rows
const ORIGIN_X = 20, ORIGIN_Y = 44;
const PACKAGE_MARGIN = 10, PACKAGE_TAB_HEIGHT = 15, PACKAGE_TAB_RISE = 26;
const EDGE_CURVE = 30;
const MIN_ZOOM = 0.3, MAX_ZOOM = 2.5, WHEEL_ZOOM_STEP = 1.1, BUTTON_ZOOM_STEP = 1.2, FIT_MAX_ZOOM = 1.2, FIT_PADDING = 48;
const DRAG_THRESHOLD_PX = 3;
const NOT_DISPATCHED = 'NOT DISPATCHED';

const clampZoom = (zoom) => Math.max(MIN_ZOOM, Math.min(MAX_ZOOM, zoom));

/** Positions of the boxes, the «wave» packages, the «use» edges and the bounds of everything, from the manual offsets. */
function buildLayout(features, offsets) {
  const waveOf = (feature) => (Number.isFinite(feature.wave) ? feature.wave : 1);
  const waves = [...new Set(features.map(waveOf))].sort((first, second) => first - second);
  const rowOfWave = {};
  const positions = {};
  features.forEach((feature) => {
    const wave = waveOf(feature), column = waves.indexOf(wave);
    rowOfWave[wave] = rowOfWave[wave] == null ? 0 : rowOfWave[wave] + 1;
    const offset = offsets[feature.id] || { x: 0, y: 0 };
    positions[feature.id] = { x: ORIGIN_X + column * COLUMN_STEP + offset.x, y: ORIGIN_Y + rowOfWave[wave] * ROW_STEP + offset.y };
  });
  // the package follows the boxes of its wave, also after dragging them
  const packages = waves.map((wave) => {
    const inside = features.filter((feature) => waveOf(feature) === wave).map((feature) => positions[feature.id]);
    const left = Math.min(...inside.map((point) => point.x)) - PACKAGE_MARGIN, top = Math.min(...inside.map((point) => point.y)) - PACKAGE_TAB_RISE;
    const bodyTop = top + PACKAGE_TAB_HEIGHT - 1;
    const right = Math.max(...inside.map((point) => point.x)) + BOX_WIDTH + PACKAGE_MARGIN, bottom = Math.max(...inside.map((point) => point.y)) + BOX_HEIGHT + PACKAGE_MARGIN;
    return { wave, left, top, bodyTop, width: right - left, height: bottom - bodyTop };
  });
  // dependent (client) -> dependency (supplier): leaves the left side of the dependent, enters the right side of the dependency
  const edges = features.flatMap((feature) => list(feature.deps).filter((dependencyId) => positions[dependencyId]).map((dependencyId) => {
    const dependent = positions[feature.id], dependency = positions[dependencyId];
    const startX = dependent.x, startY = dependent.y + BOX_HEIGHT / 2, endX = dependency.x + BOX_WIDTH, endY = dependency.y + BOX_HEIGHT / 2;
    return { key: feature.id + '>' + dependencyId, path: `M${startX},${startY} C${startX - EDGE_CURVE},${startY} ${endX + EDGE_CURVE},${endY} ${endX + 2},${endY}`,
      labelX: (startX + endX) / 2, labelY: (startY + endY) / 2 - 6 };
  }));
  const bounds = packages.length
    ? { x0: Math.min(...packages.map((p) => p.left)), y0: Math.min(...packages.map((p) => p.top)),
      x1: Math.max(...packages.map((p) => p.left + p.width)), y1: Math.max(...packages.map((p) => p.bodyTop + p.height)) }
    : { x0: 0, y0: 0, x1: 1, y1: 1 };
  return { positions, packages, edges, bounds };
}

function DepsBody() {
  const features = list(featuresWithWaves());
  const turn = curN();
  const [camera, setCamera] = useState({ x: 0, y: 0, z: 1 });
  const [offsets, setOffsets] = useState({});
  const [isDragging, setIsDragging] = useState(false);
  const viewportRef = useRef(null);
  const layout = buildLayout(features, offsets);

  const boundsRef = useRef(layout.bounds), offsetsRef = useRef(offsets), autoRef = useRef(true);
  boundsRef.current = layout.bounds; offsetsRef.current = offsets;
  const sizeOf = () => { const viewport = viewportRef.current; return viewport ? [viewport.clientWidth, viewport.clientHeight] : [0, 0]; };
  /** Camera that keeps the diagram in view (never lets it be panned out; centers it on an axis where it is smaller than the viewport). */
  const clampNow = (next) => { const [width, height] = sizeOf(); return clampCameraTo(next, boundsRef.current, width, height); };
  /** A camera change made by the user: it stops the automatic fit. */
  const moveCamera = (update) => { autoRef.current = false; setCamera((current) => clampNow(typeof update === 'function' ? update(current) : update)); };
  /** Default view: the whole diagram (bounds for the given offsets) fitted and centered in the viewport. */
  function fitCamera(forOffsets) {
    const [width, height] = sizeOf();
    if (!width || !height || !features.length) return;
    autoRef.current = true;
    const target = fitCameraTo(buildLayout(features, forOffsets).bounds, width, height, { minZoom: MIN_ZOOM, maxZoom: FIT_MAX_ZOOM, marginX: FIT_PADDING, marginY: FIT_PADDING });
    setCamera((current) => (sameCamera(current, target) ? current : target));
  }
  // until the user takes the camera: fitted and centered when shown, when the features change and when the dialog is resized
  useLayoutEffect(() => { if (autoRef.current) fitCamera(offsetsRef.current); });
  useLayoutEffect(() => {
    const viewport = viewportRef.current;
    if (!viewport || typeof ResizeObserver !== 'function') return undefined;
    const observer = new ResizeObserver(() => {
      if (autoRef.current) fitCamera(offsetsRef.current);
      else setCamera((current) => { const clamped = clampNow(current); return sameCamera(clamped, current) ? current : clamped; });
    });
    observer.observe(viewport);
    return () => observer.disconnect();
  }, []);

  // native listener with passive:false: Preact attaches onWheel as passive, and preventDefault would be ignored
  useEffect(() => {
    const viewport = viewportRef.current;
    if (!viewport) return undefined;
    const onWheel = (event) => {
      event.preventDefault();
      if (!event.ctrlKey && !event.metaKey) { // plain wheel scrolls (Shift = horizontal)
        const deltaX = event.shiftKey ? event.deltaY : event.deltaX, deltaY = event.shiftKey ? 0 : event.deltaY;
        moveCamera((current) => ({ ...current, x: current.x - deltaX, y: current.y - deltaY }));
        return;
      }
      const rect = viewport.getBoundingClientRect(), pointerX = event.clientX - rect.left, pointerY = event.clientY - rect.top;
      moveCamera((current) => { // zoom anchored at the cursor (a trackpad pinch also arrives with ctrlKey)
        const zoom = clampZoom(current.z * (event.deltaY < 0 ? WHEEL_ZOOM_STEP : 1 / WHEEL_ZOOM_STEP));
        return { z: zoom, x: pointerX - (pointerX - current.x) * zoom / current.z, y: pointerY - (pointerY - current.y) * zoom / current.z };
      });
    };
    viewport.addEventListener('wheel', onWheel, { passive: false });
    return () => viewport.removeEventListener('wheel', onWheel);
  }, []);

  function zoomAroundCenter(factor) {
    const viewport = viewportRef.current, centerX = viewport ? viewport.clientWidth / 2 : 0, centerY = viewport ? viewport.clientHeight / 2 : 0;
    moveCamera((current) => {
      const zoom = clampZoom(current.z * factor);
      return { z: zoom, x: centerX - (centerX - current.x) * zoom / current.z, y: centerY - (centerY - current.y) * zoom / current.z };
    });
  }

  /** Background = pan; a «feature» box = move the feature; a «wave» tab = move every feature of the package. */
  function onViewportMouseDown(event) {
    if (event.button !== 0) return;
    const target = event.target.closest('[data-uml]'), raw = target && target.getAttribute('data-uml');
    const movedIds = !raw ? [] : raw.startsWith('wave:') ? features.filter((feature) => 'wave:' + (Number.isFinite(feature.wave) ? feature.wave : 1) === raw).map((feature) => feature.id) : [raw];
    const startX = event.clientX, startY = event.clientY, startCamera = camera, startOffsets = {};
    movedIds.forEach((id) => { startOffsets[id] = offsets[id] || { x: 0, y: 0 }; });
    setIsDragging(true);
    let moved = false;
    const onMove = (moveEvent) => {
      const deltaX = moveEvent.clientX - startX, deltaY = moveEvent.clientY - startY;
      if (Math.abs(deltaX) + Math.abs(deltaY) < DRAG_THRESHOLD_PX) return;
      if (!moved) { moved = true; autoRef.current = false; }
      if (movedIds.length) {
        setOffsets((current) => {
          const next = { ...current };
          movedIds.forEach((id) => { next[id] = { x: startOffsets[id].x + deltaX / startCamera.z, y: startOffsets[id].y + deltaY / startCamera.z }; });
          return next;
        });
      } else {
        setCamera(clampNow({ ...startCamera, x: startCamera.x + deltaX, y: startCamera.y + deltaY }));
      }
    };
    const onUp = () => {
      window.removeEventListener('mousemove', onMove);
      window.removeEventListener('mouseup', onUp);
      setIsDragging(false);
    };
    window.addEventListener('mousemove', onMove);
    window.addEventListener('mouseup', onUp);
  }

  const resetLayout = () => { setOffsets({}); fitCamera({}); };

  if (!features.length) return html`<div class="bd-empty">No features in the roadmap yet.</div>`;
  return html`<div class="bd-deps">
    <p class="bd-deps-text">${turn != null ? html`State at <span class="bd-turn">${'T' + turn}</span>. ` : 'State at the latest turn. '}${'The dashed «use» arrow points to the feature it depends on. The wheel scrolls and Ctrl + wheel zooms. Drag a «feature» or a «wave» tab to rearrange.'}</p>
    <div class="bd-deps-viewport" ref=${viewportRef} style=${{ cursor: isDragging ? 'grabbing' : 'grab' }} onMouseDown=${onViewportMouseDown}>
      <div class="bd-deps-canvas" style=${{ transform: `translate(${camera.x}px,${camera.y}px) scale(${camera.z})` }}>
        ${layout.packages.map((pack) => html`<div key=${pack.wave}>
          <div class="bd-uml-package" style=${{ left: pack.left + 'px', top: pack.bodyTop + 'px', width: pack.width + 'px', height: pack.height + 'px' }}></div>
          <div class="bd-uml-tab" data-uml=${'wave:' + pack.wave} title="Drag to move the whole package" style=${{ left: pack.left + 'px', top: pack.top + 'px', height: PACKAGE_TAB_HEIGHT + 'px' }}>${'«wave» ' + pack.wave}</div>
        </div>`)}
        <svg class="bd-uml-edges" width="1" height="1" aria-hidden="true">
          <defs><marker id="bd-uml-open" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="9" markerHeight="9" orient="auto"><path d="M0,0 L10,5 L0,10" fill="none" stroke="#8b929c" stroke-width="1.4"></path></marker></defs>
          ${layout.edges.map((edge) => html`<g key=${edge.key}>
            <path d=${edge.path} fill="none" stroke="#8b929c" stroke-width="1.2" stroke-dasharray="5 4" marker-end="url(#bd-uml-open)"></path>
            <text x=${edge.labelX} y=${edge.labelY} fill="#8b929c" font-size="10" font-family="IBM Plex Mono, monospace" text-anchor="middle">«use»</text>
          </g>`)}
        </svg>
        ${features.map((feature) => {
          const status = featureStatusAt(feature.id, turn) || NOT_DISPATCHED, color = FEATURE_COLOR[status] || NEUTRAL_COLOR, position = layout.positions[feature.id];
          const dependencyCount = list(feature.deps).length, parts = featureParts(feature.id);
          return html`<div key=${feature.id} class="bd-uml-feature" data-uml=${feature.id}
            style=${{ left: position.x + 'px', top: position.y + 'px', width: BOX_WIDTH + 'px', height: BOX_HEIGHT + 'px', borderColor: color, borderStyle: status === NOT_DISPATCHED ? 'dashed' : 'solid' }}>
            <div class="bd-uml-feature-head"><div class="bd-uml-stereotype">«feature»</div><div class="bd-uml-name" title=${parts.tip}>${parts.name}</div>${parts.same ? null : html`<div class="bd-uml-slug">${parts.slug}</div>`}</div>
            <div class="bd-uml-attributes"><div>${'stage: ' + (feature.stage != null ? feature.stage : '—')}</div><div>${'wave: ' + (feature.wave != null ? feature.wave : '—')}</div>
              <div>status: <span style=${{ color }}>${status.toLowerCase()}</span></div></div>
            <div class="bd-uml-foot">${dependencyCount ? '+ depends on ' + dependencyCount : '+ no dependencies'}</div>
          </div>`;
        })}
      </div>
      <div class="bd-deps-toolbar" onMouseDown=${(event) => event.stopPropagation()}>
        <span class="bd-deps-hints"><kbd>wheel</kbd>scroll<kbd>Ctrl + wheel</kbd>zoom<kbd>drag</kbd>move</span>
        <button type="button" class="bd-icon-button" aria-label="Zoom out" onClick=${() => zoomAroundCenter(1 / BUTTON_ZOOM_STEP)}>−</button>
        <span class="bd-zoom-value">${Math.round(camera.z * 100) + '%'}</span>
        <button type="button" class="bd-icon-button" aria-label="Zoom in" onClick=${() => zoomAroundCenter(BUTTON_ZOOM_STEP)}>+</button>
        <button type="button" class="bd-button bd-button-small" onClick=${() => fitCamera(offsets)}>Fit</button>
        <button type="button" class="bd-button bd-button-small bd-button-quiet" onClick=${resetLayout}>Reset layout</button>
      </div>
    </div>
  </div>`;
}

/** Opens the dependency diagram of the whole roadmap (global view: no chat column). */
export function openDepsDialog() {
  return openBigDialog({ badge: 'GLOBAL', badgeColor: BADGE_COLOR, title: 'Feature dependencies', body: () => html`<${DepsBody} />`, chat: null });
}
