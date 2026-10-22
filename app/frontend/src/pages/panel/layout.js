/* Layout of the panel: the width of the GLOBAL zone (resizable, persisted in localStorage without depending on it), the
   show/hide state of that zone on wide screens, and its drawer on narrow screens. The zone itself is drawn by global_zone.js;
   this module only owns the numbers and the one-time pointer/keyboard bindings of the resize handle (#sep-global). */
import { $ } from '../../util/dom.js';
import { readStored, writeStored } from '../../util/storage.js';
import { renderAll } from '../project_panel.js';

const LAYOUT = {
  global: { min: 320, max: 560, def: 400, v: '--global-w', id: 'sep-global', axis: 'x', dir: 1 }, // 400 = the five tabs fit without scrolling
};
const LKEY = 'harness.layout.v3'; // v3: new default width (older saved widths hid the last tabs)
const INSPECTOR_WIDTH = 300, LOOP_MIN_WIDTH = 340;
const NARROW_QUERY = '(max-width: 900px)';
/** global = width in px; hidden = the zone is collapsed on wide screens; drawer = the zone is open as a drawer on narrow screens. */
const lay = { global: LAYOUT.global.def, hidden: false, drawer: false };

const isNarrow = () => typeof window.matchMedia === 'function' && window.matchMedia(NARROW_QUERY).matches;

/** True when the GLOBAL zone takes no room on the page (collapsed on wide screens, closed drawer on narrow ones). */
export const globalZoneHidden = () => (isNarrow() ? !lay.drawer : lay.hidden);
/** True while the narrow-screen drawer of the GLOBAL zone is open. */
export const globalDrawerOpen = () => isNarrow() && lay.drawer;
/** Label of the toggle button in the turn strip: points to where the zone goes when clicked. */
export const globalToggleArrow = () => (globalZoneHidden() ? '▶' : '◀');

const appWidth = () => { const app = $('app'); return app ? app.clientWidth : window.innerWidth; };
/** LAY-01: the inspector gives its room back (collapses) before the GLOBAL column would have to go below its minimum. */
const inspectorCollapsed = () => appWidth() - INSPECTOR_WIDTH - LOOP_MIN_WIDTH < LAYOUT.global.min;
function layLimit(k) {
  const L = LAYOUT[k];
  const room = appWidth() - (inspectorCollapsed() ? 0 : INSPECTOR_WIDTH) - LOOP_MIN_WIDTH;
  return Math.max(L.min, Math.min(L.max, room));
}
const layClamp = (k, v) => Math.round(Math.max(LAYOUT[k].min, Math.min(layLimit(k), v)));
function layApply() {
  const app = $('app');
  if (!app) return;
  app.classList.toggle('is-inspector-collapsed', inspectorCollapsed());
  for (const k of Object.keys(LAYOUT)) {
    const L = LAYOUT[k], el = $(L.id);
    app.style.setProperty(L.v, lay[k] + 'px');
    if (!el) continue;
    el.setAttribute('aria-valuemin', String(L.min)); el.setAttribute('aria-valuemax', String(layLimit(k))); el.setAttribute('aria-valuenow', String(lay[k]));
  }
}
function laySave() { writeStored(LKEY, { global: lay.global, hidden: lay.hidden }); }
function layLoad() {
  const stored = readStored(LKEY, {}) || {};
  for (const k of Object.keys(LAYOUT)) {
    if (typeof stored[k] === 'number' && Number.isFinite(stored[k])) lay[k] = Math.max(LAYOUT[k].min, Math.min(LAYOUT[k].max, stored[k]));
  }
  lay.hidden = stored.hidden === true;
}
function laySet(k, v, persist) { lay[k] = layClamp(k, v); layApply(); if (persist) laySave(); }

/** Shows or hides the GLOBAL zone: collapses it on wide screens (remembered), opens or closes the drawer on narrow ones. */
export function toggleGlobalZone() {
  if (isNarrow()) lay.drawer = !lay.drawer; else { lay.hidden = !lay.hidden; laySave(); }
  renderAll();
}
/** Opens the GLOBAL zone (used when something asks for one of its tabs). `render: false` = the caller is drawing right now. */
export function showGlobalZone({ render = true } = {}) {
  if (isNarrow()) { if (lay.drawer) return; lay.drawer = true; }
  else if (lay.hidden) { lay.hidden = false; laySave(); }
  else return;
  if (render) renderAll();
}
function closeDrawer() { if (lay.drawer) { lay.drawer = false; renderAll(); } }

function initSep(k) {
  const L = LAYOUT[k], el = $(L.id);
  if (!el) return;
  let drag = null;
  el.addEventListener('pointerdown', (e) => {
    if (e.button !== 0) return;
    e.preventDefault(); el.setPointerCapture(e.pointerId);
    drag = { id: e.pointerId, p: e.clientX, v: lay[k] };
    el.classList.add('drag'); document.body.classList.add('resizing', 'resizing-' + L.axis);
  });
  el.addEventListener('pointermove', (e) => {
    if (!drag || drag.id !== e.pointerId) return;
    laySet(k, drag.v + (e.clientX - drag.p) * L.dir, false);
  });
  const end = (e) => {
    if (!drag || drag.id !== e.pointerId) return;
    drag = null; el.classList.remove('drag'); document.body.classList.remove('resizing', 'resizing-x', 'resizing-y'); laySave();
  };
  el.addEventListener('pointerup', end); el.addEventListener('pointercancel', end);
  el.addEventListener('dblclick', () => laySet(k, L.def, true));
  el.addEventListener('keydown', (e) => {
    const step = e.shiftKey ? 48 : 16;
    let nextValue = null;
    if (e.key === 'ArrowRight') nextValue = lay[k] + step * L.dir; else if (e.key === 'ArrowLeft') nextValue = lay[k] - step * L.dir;
    else if (e.key === 'Home') nextValue = L.min; else if (e.key === 'End') nextValue = layLimit(k); else if (e.key === 'Enter') nextValue = L.def;
    if (nextValue == null) return;
    e.preventDefault(); laySet(k, nextValue, true);
  });
}

/** One-time setup on the DOM of the first render (the Panel never re-creates #sep-global). */
export function initLayout() {
  layApply();
  for (const k of Object.keys(LAYOUT)) initSep(k);
  window.addEventListener('resize', () => { for (const k of Object.keys(LAYOUT)) lay[k] = layClamp(k, lay[k]); layApply(); });
  document.addEventListener('pointerdown', (e) => { if (lay.drawer && !e.target.closest('.z-global, .z-global-toggle')) closeDrawer(); });
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && lay.drawer && !document.querySelector('dialog[open]')) closeDrawer(); });
}

layLoad(); // the saved width and the collapsed flag are known before the first render
