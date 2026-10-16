/* Resizable panes (persisted in localStorage, without depending on it) and the roadmap drawer on narrow screens. */
import { $ } from '../../util/dom.js';
import { readStored, writeStored } from '../../util/storage.js';

const LAYOUT = {
  left: { min: 180, max: 520, def: 240, v: '--left-w', id: 'sep-left', axis: 'x', dir: 1 },
  right: { min: 260, max: 640, def: 330, v: '--right-w', id: 'sep-right', axis: 'x', dir: -1 },
  foot: { min: 56, max: 320, def: 64, v: '--foot-h', id: 'sep-foot', axis: 'y', dir: -1 },
};
const LKEY = 'harness.layout.v1';
const lay = { left: 240, right: 330, foot: 64 };

function layLimit(k) {
  const L = LAYOUT[k], app = $('app');
  if (k === 'foot') return Math.max(L.min, Math.min(L.max, window.innerHeight - 420));
  const other = k === 'left' ? lay.right : lay.left;
  return Math.max(L.min, Math.min(L.max, app.clientWidth - other - 340));
}
const layClamp = (k, v) => Math.round(Math.max(LAYOUT[k].min, Math.min(layLimit(k), v)));
function layApply() {
  const app = $('app');
  for (const k of Object.keys(LAYOUT)) {
    const L = LAYOUT[k], el = $(L.id);
    app.style.setProperty(L.v, lay[k] + 'px');
    el.setAttribute('aria-valuemin', String(L.min)); el.setAttribute('aria-valuemax', String(layLimit(k))); el.setAttribute('aria-valuenow', String(lay[k]));
  }
}
function laySave() { writeStored(LKEY, lay); }
function layLoad() {
  const j = readStored(LKEY, {}) || {};
  for (const k of Object.keys(LAYOUT)) if (typeof j[k] === 'number' && Number.isFinite(j[k])) lay[k] = Math.max(LAYOUT[k].min, Math.min(LAYOUT[k].max, j[k]));
}
function laySet(k, v, persist) { lay[k] = layClamp(k, v); layApply(); if (persist) laySave(); }
function initSep(k) {
  const L = LAYOUT[k], el = $(L.id);
  let drag = null;
  el.addEventListener('pointerdown', (e) => {
    if (e.button !== 0) return;
    e.preventDefault(); el.setPointerCapture(e.pointerId);
    drag = { id: e.pointerId, p: L.axis === 'x' ? e.clientX : e.clientY, v: lay[k] };
    el.classList.add('drag'); document.body.classList.add('resizing', 'resizing-' + L.axis);
  });
  el.addEventListener('pointermove', (e) => {
    if (!drag || drag.id !== e.pointerId) return;
    const d = (L.axis === 'x' ? e.clientX : e.clientY) - drag.p;
    laySet(k, drag.v + d * L.dir, false);
  });
  const end = (e) => {
    if (!drag || drag.id !== e.pointerId) return;
    drag = null; el.classList.remove('drag'); document.body.classList.remove('resizing', 'resizing-x', 'resizing-y'); laySave();
  };
  el.addEventListener('pointerup', end); el.addEventListener('pointercancel', end);
  el.addEventListener('dblclick', () => laySet(k, L.def, true));
  el.addEventListener('keydown', (e) => {
    const step = e.shiftKey ? 48 : 16;
    let nv = null;
    if (L.axis === 'x') { if (e.key === 'ArrowRight') nv = lay[k] + step * L.dir; else if (e.key === 'ArrowLeft') nv = lay[k] - step * L.dir; }
    else if (e.key === 'ArrowUp') nv = lay[k] + step; else if (e.key === 'ArrowDown') nv = lay[k] - step;
    if (e.key === 'Home') nv = L.min; else if (e.key === 'End') nv = layLimit(k); else if (e.key === 'Enter') nv = L.def;
    if (nv == null) return;
    e.preventDefault(); laySet(k, nv, true);
  });
}
export function initLayout() {
  layLoad(); layApply();
  for (const k of Object.keys(LAYOUT)) initSep(k);
  window.addEventListener('resize', () => { for (const k of Object.keys(LAYOUT)) lay[k] = layClamp(k, lay[k]); layApply(); });
  const closeDrawer = () => { $('app').classList.remove('left-open'); $('left-toggle').setAttribute('aria-expanded', 'false'); };
  document.addEventListener('pointerdown', (e) => { if ($('app').classList.contains('left-open') && !e.target.closest('#left, #left-toggle')) closeDrawer(); });
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && $('app').classList.contains('left-open')) closeDrawer(); });
  $('left-toggle').addEventListener('click', () => { const open = $('app').classList.toggle('left-open'); $('left-toggle').setAttribute('aria-expanded', String(open)); });
}
