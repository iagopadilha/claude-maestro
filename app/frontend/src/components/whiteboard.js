/* Whiteboard: free drawing board persisted on the server (GET/POST whiteboard with a version; 409 = another tab saved).
   Drawn in plain SVG. The scene follows the Excalidraw scene format (type/elements/appState, elements
   rectangle/ellipse/arrow/line/freedraw/text/diamond with x, y, width, height, points), so a JSON exported
   from Excalidraw opens here and one from here opens there. Autosave with a 1.5 s debounce, only when it changed. */
import { h, sv, fill } from '../util/dom.js';
import { fmtHour } from '../util/format.js';
import { toast } from './toast.js';
import { confirmDialog } from './dialog.js';

const AUTOSAVE_MS = 1500;
const MAX_BYTES = 7.5 * 1024 * 1024; // the route accepts 8 MB
const COLORS = ['#E6E6E3', '#F2B544', '#5BD38A', '#8AA4FF', '#F06A5E', '#A58BFF'];
const BACKGROUND = '#0E0F11';
const TOOLS = [
  { id: 'select', label: 'Select and move', key: 'V', icon: '➤' },
  { id: 'pen', label: 'Pen', key: 'P', icon: '✎' },
  { id: 'rectangle', label: 'Rectangle', key: 'R', icon: '▭' },
  { id: 'ellipse', label: 'Ellipse', key: 'O', icon: '◯' },
  { id: 'arrow', label: 'Arrow', key: 'A', icon: '→' },
  { id: 'text', label: 'Text', key: 'T', icon: 'T' },
  { id: 'eraser', label: 'Eraser (click the drawing)', key: 'E', icon: '⌫' },
];
let idSeq = 0;
const newId = () => 'el' + Date.now().toString(36) + (idSeq++).toString(36);
const r1 = (v) => Math.round(v * 10) / 10;

function emptyScene() { return { type: 'excalidraw', version: 2, source: 'loop-harness', elements: [], appState: { viewBackgroundColor: BACKGROUND } }; }
function normalizeScene(d) {
  const c = d && typeof d === 'object' ? (d.data && Array.isArray(d.data.elements) ? d.data : d) : null;
  if (!c || !Array.isArray(c.elements)) return emptyScene();
  return { type: 'excalidraw', version: 2, source: c.source || 'loop-harness', elements: c.elements.filter((e) => e && !e.isDeleted && typeof e.x === 'number' && typeof e.y === 'number'), appState: { ...(c.appState || {}), viewBackgroundColor: (c.appState && c.appState.viewBackgroundColor) || BACKGROUND } };
}

function drawElement(e) {
  const color = e.strokeColor || '#E6E6E3', width = e.strokeWidth || 2;
  const common = { stroke: color, 'stroke-width': width, fill: 'none', 'stroke-linecap': 'round', 'stroke-linejoin': 'round', 'data-id': e.id };
  const w = e.width || 0, hh = e.height || 0;
  if (e.type === 'rectangle') return sv('rect', { ...common, x: Math.min(e.x, e.x + w), y: Math.min(e.y, e.y + hh), width: Math.abs(w), height: Math.abs(hh), rx: 6 });
  if (e.type === 'diamond') { const cx = e.x + w / 2, cy = e.y + hh / 2; return sv('path', { ...common, d: `M${cx} ${e.y} L${e.x + w} ${cy} L${cx} ${e.y + hh} L${e.x} ${cy} Z` }); }
  if (e.type === 'ellipse') return sv('ellipse', { ...common, cx: e.x + w / 2, cy: e.y + hh / 2, rx: Math.abs(w / 2), ry: Math.abs(hh / 2) });
  if (e.type === 'text') {
    const fs = e.fontSize || 20;
    const t = sv('text', { x: e.x, y: e.y + fs, fill: color, 'font-size': fs, 'font-family': 'IBM Plex Sans, system-ui, sans-serif', 'data-id': e.id });
    String(e.text || '').split('\n').forEach((ln, i) => t.appendChild(sv('tspan', { x: e.x, dy: i ? fs * 1.25 : 0, text: ln || ' ' })));
    return t;
  }
  const pts = Array.isArray(e.points) && e.points.length ? e.points : [[0, 0], [w, hh]];
  const d = pts.map((p, i) => `${i ? 'L' : 'M'}${r1(e.x + p[0])} ${r1(e.y + p[1])}`).join(' ');
  const el = sv('path', { ...common, d });
  if (e.type === 'arrow') el.setAttribute('marker-end', 'url(#wb-arrow-' + COLORS.indexOf(color) + ')');
  return el;
}
function bounds(e) {
  if (e.type === 'text') { const fs = e.fontSize || 20, lines = String(e.text || '').split('\n'); return { x: e.x, y: e.y, w: Math.max(...lines.map((l) => l.length)) * fs * 0.55, h: lines.length * fs * 1.25 }; }
  if (Array.isArray(e.points) && e.points.length) {
    const xs = e.points.map((p) => e.x + p[0]), ys = e.points.map((p) => e.y + p[1]);
    return { x: Math.min(...xs), y: Math.min(...ys), w: Math.max(...xs) - Math.min(...xs), h: Math.max(...ys) - Math.min(...ys) };
  }
  return { x: Math.min(e.x, e.x + (e.width || 0)), y: Math.min(e.y, e.y + (e.height || 0)), w: Math.abs(e.width || 0), h: Math.abs(e.height || 0) };
}
function download(name, blob) {
  const url = URL.createObjectURL(blob);
  const a = h('a', { href: url, download: name });
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 2000);
}

/** createWhiteboard({ api }) -> { element, open(), checkRemote(), isDirty(), fit() } */
export function createWhiteboard({ api }) {
  const st = { scene: emptyScene(), version: 0, updatedAt: null, dirty: false, saving: false, error: null, conflict: null, loaded: false, loading: false,
    tool: 'pen', color: COLORS[1], sel: null, view: { x: 0, y: 0, s: 1 }, undoStack: [], redoStack: [], timer: null };
  const defs = sv('defs', {}, COLORS.map((c, i) => sv('marker', { id: 'wb-arrow-' + i, viewBox: '0 0 10 10', refX: 8, refY: 5, markerWidth: 6, markerHeight: 6, orient: 'auto-start-reverse' }, sv('path', { d: 'M0 0 L10 5 L0 10 z', fill: c }))));
  const layer = sv('g', { class: 'wb-scene' });
  const selLayer = sv('g', { class: 'wb-sel' });
  const vp = sv('g', {}, layer, selLayer);
  const svg = sv('svg', { class: 'wb-svg', role: 'img', 'aria-label': 'Free drawing board. Use the toolbar or the keys V, P, R, O, A, T, E.' }, defs, vp);
  const indicator = h('span', { class: 'wb-ind', role: 'status', 'aria-live': 'polite' });
  const notice = h('div', { class: 'wb-notice', role: 'alert', hidden: true });
  const fileInput = h('input', { type: 'file', accept: '.json,.excalidraw,application/json', hidden: true });
  const toolButtons = TOOLS.map((f) => h('button', { type: 'button', class: 'wb-btn', title: `${f.label} (${f.key})`, 'aria-label': f.label, 'aria-pressed': 'false', 'data-f': f.id, onclick: () => pickTool(f.id) }, f.icon));
  const colorButtons = COLORS.map((c) => { const b = h('button', { type: 'button', class: 'wb-color', title: 'color ' + c, 'aria-label': 'color ' + c, 'aria-pressed': 'false', onclick: () => { st.color = c; if (st.sel) recolorSelected(c); toolbar(); } }); b.style.background = c; return b; });
  const btn = (text, title, fn, cls = '') => h('button', { type: 'button', class: 'btn btn-sm btn-ghost ' + cls, title, onclick: fn }, text);
  const tools = h('div', { class: 'wb-bar', role: 'toolbar', 'aria-label': 'Board tools' },
    h('div', { class: 'wb-group' }, toolButtons), h('div', { class: 'wb-group' }, colorButtons),
    h('div', { class: 'wb-group' }, btn('Undo', 'Ctrl+Z', undo), btn('Redo', 'Ctrl+Y', redo), btn('Delete selection', 'Delete', deleteSelected)),
    h('span', { class: 'spacer' }), indicator,
    h('div', { class: 'wb-group' }, btn('PNG', 'export a PNG image', () => exportAs('png')), btn('SVG', 'export SVG', () => exportAs('svg')),
      btn('Download JSON', 'downloads the scene (Excalidraw format)', downloadJson), btn('Import JSON', 'opens a scene JSON (from this board or from Excalidraw)', () => fileInput.click()),
      btn('Clear', 'erases everything on the board', clear, 'wb-danger')));
  const area = h('div', { class: 'wb-area' }, svg);
  const element = h('section', { class: 'wb', 'aria-label': 'Whiteboard' }, tools, notice, area, fileInput);

  // ---------- drawing ----------
  function render() {
    layer.replaceChildren(...st.scene.elements.map(drawElement));
    selLayer.replaceChildren();
    const e = st.sel && st.scene.elements.find((x) => x.id === st.sel);
    if (e) { const b = bounds(e); selLayer.appendChild(sv('rect', { x: b.x - 6, y: b.y - 6, width: b.w + 12, height: b.h + 12, rx: 4, class: 'wb-sel-box' })); }
    vp.setAttribute('transform', `translate(${st.view.x} ${st.view.y}) scale(${st.view.s})`);
  }
  function toolbar() {
    for (const b of toolButtons) b.setAttribute('aria-pressed', String(b.dataset.f === st.tool));
    colorButtons.forEach((b, i) => b.setAttribute('aria-pressed', String(COLORS[i] === st.color)));
    area.dataset.tool = st.tool;
  }
  function indicatorText() {
    indicator.className = 'wb-ind' + (st.error ? ' err' : st.saving || st.dirty ? ' busy' : '');
    indicator.textContent = st.loading ? 'loading…' : st.error ? 'save failed' : st.saving ? 'saving…' : st.dirty ? 'changed…' : st.loaded ? `saved${st.updatedAt ? ' · ' + fmtHour(st.updatedAt) : ''}` : '';
    const notices = [];
    if (st.conflict) notices.push(h('div', { class: 'ed-av ed-av-warn' }, h('strong', { text: 'Another tab saved the board first. ' }), 'Choose which version stays.',
      h('div', { class: 'card-row' }, h('button', { type: 'button', class: 'btn btn-sm', onclick: () => open(true) }, 'Reload (lose my changes)'),
        h('button', { type: 'button', class: 'btn btn-sm btn-danger', onclick: overwrite }, 'Overwrite with mine'))));
    if (st.error && !st.conflict) notices.push(h('div', { class: 'ed-av ed-av-bad' }, h('strong', { text: 'Not saved: ' }), st.error, ' ', h('button', { type: 'button', class: 'btn btn-sm', onclick: () => save() }, 'try again')));
    fill(notice, notices); notice.hidden = !notices.length;
  }
  function pickTool(f) { st.tool = f; if (f !== 'select') st.sel = null; toolbar(); render(); }

  // ---------- history ----------
  function remember() { st.undoStack.push(JSON.stringify(st.scene.elements)); if (st.undoStack.length > 100) st.undoStack.shift(); st.redoStack = []; }
  function undo() { if (!st.undoStack.length) return; st.redoStack.push(JSON.stringify(st.scene.elements)); st.scene.elements = JSON.parse(st.undoStack.pop()); st.sel = null; changed(); }
  function redo() { if (!st.redoStack.length) return; st.undoStack.push(JSON.stringify(st.scene.elements)); st.scene.elements = JSON.parse(st.redoStack.pop()); st.sel = null; changed(); }
  function deleteSelected() { if (!st.sel) return; remember(); st.scene.elements = st.scene.elements.filter((e) => e.id !== st.sel); st.sel = null; changed(); }
  function recolorSelected(c) { const e = st.scene.elements.find((x) => x.id === st.sel); if (!e) return; remember(); e.strokeColor = c; changed(); }
  async function clear() {
    if (!st.scene.elements.length) return;
    const ok = await confirmDialog({ title: 'Clear the board?', text: 'Erases every drawing on the board (for every tab). You can undo it while this tab stays open.', yesText: 'Clear board', danger: true });
    if (!ok) return;
    remember(); st.scene.elements = []; st.sel = null; changed();
  }
  function changed() { st.dirty = true; render(); indicatorText(); clearTimeout(st.timer); st.timer = setTimeout(save, AUTOSAVE_MS); }

  // ---------- pointer ----------
  const toScene = (ev) => { const r = svg.getBoundingClientRect(); return { x: (ev.clientX - r.left - st.view.x) / st.view.s, y: (ev.clientY - r.top - st.view.y) / st.view.s }; };
  const idUnder = (ev) => { const el = ev.target && ev.target.closest && ev.target.closest('[data-id]'); return el ? el.getAttribute('data-id') : null; };
  let gesture = null;
  svg.addEventListener('pointerdown', (ev) => {
    if (ev.button === 1 || (ev.button === 0 && (ev.shiftKey && st.tool === 'select' || spacePressed))) {
      gesture = { kind: 'pan', x: ev.clientX, y: ev.clientY, vx: st.view.x, vy: st.view.y }; svg.setPointerCapture(ev.pointerId); return;
    }
    if (ev.button !== 0) return;
    const p = toScene(ev);
    if (st.tool === 'eraser') { const id = idUnder(ev); if (id) { remember(); st.scene.elements = st.scene.elements.filter((e) => e.id !== id); changed(); } gesture = { kind: 'eraser' }; svg.setPointerCapture(ev.pointerId); return; }
    if (st.tool === 'select') {
      const id = idUnder(ev); st.sel = id; render();
      if (id) { remember(); const e = st.scene.elements.find((x) => x.id === id); gesture = { kind: 'move', e, x0: p.x, y0: p.y, ex: e.x, ey: e.y, moved: false }; svg.setPointerCapture(ev.pointerId); }
      else { gesture = { kind: 'pan', x: ev.clientX, y: ev.clientY, vx: st.view.x, vy: st.view.y }; svg.setPointerCapture(ev.pointerId); }
      return;
    }
    if (st.tool === 'text') { ev.preventDefault(); askText(p, ev); return; }
    remember();
    const base = { id: newId(), x: r1(p.x), y: r1(p.y), width: 0, height: 0, strokeColor: st.color, strokeWidth: 2, isDeleted: false, version: 1 };
    const e = st.tool === 'pen' ? { ...base, type: 'freedraw', points: [[0, 0]] } : st.tool === 'arrow' ? { ...base, type: 'arrow', points: [[0, 0], [0, 0]] } : { ...base, type: st.tool };
    st.scene.elements.push(e);
    gesture = { kind: 'draw', e, x0: p.x, y0: p.y };
    svg.setPointerCapture(ev.pointerId);
    render();
  });
  svg.addEventListener('pointermove', (ev) => {
    if (!gesture) return;
    if (gesture.kind === 'pan') { st.view = { ...st.view, x: gesture.vx + ev.clientX - gesture.x, y: gesture.vy + ev.clientY - gesture.y }; render(); return; }
    if (gesture.kind === 'eraser') { const id = idUnder(ev); if (id) { st.scene.elements = st.scene.elements.filter((e) => e.id !== id); changed(); } return; }
    const p = toScene(ev);
    if (gesture.kind === 'move') { gesture.e.x = r1(gesture.ex + p.x - gesture.x0); gesture.e.y = r1(gesture.ey + p.y - gesture.y0); gesture.moved = true; render(); return; }
    const e = gesture.e;
    if (e.type === 'freedraw') {
      const last = e.points[e.points.length - 1], dx = p.x - e.x, dy = p.y - e.y;
      if (Math.hypot(dx - last[0], dy - last[1]) >= 2 / st.view.s) e.points.push([r1(dx), r1(dy)]);
    } else if (e.type === 'arrow') { e.points[1] = [r1(p.x - e.x), r1(p.y - e.y)]; e.width = e.points[1][0]; e.height = e.points[1][1]; }
    else { e.width = r1(p.x - gesture.x0); e.height = r1(p.y - gesture.y0); }
    render();
  });
  const release = () => {
    if (!gesture) return;
    const g = gesture; gesture = null;
    if (g.kind === 'draw') {
      const e = g.e, b = bounds(e);
      if (b.w < 2 && b.h < 2 && e.type !== 'freedraw') { st.scene.elements = st.scene.elements.filter((x) => x !== e); st.undoStack.pop(); render(); return; }
      if (e.type === 'rectangle' || e.type === 'ellipse') { if (e.width < 0) { e.x = r1(e.x + e.width); e.width = -e.width; } if (e.height < 0) { e.y = r1(e.y + e.height); e.height = -e.height; } }
      if (e.type === 'freedraw') { e.width = r1(b.w); e.height = r1(b.h); }
      changed();
    } else if (g.kind === 'move') { if (g.moved) changed(); else st.undoStack.pop(); }
  };
  svg.addEventListener('pointerup', release); svg.addEventListener('pointercancel', release);
  svg.addEventListener('wheel', (ev) => {
    ev.preventDefault();
    if (ev.ctrlKey || ev.metaKey) {
      const r = svg.getBoundingClientRect(), px = ev.clientX - r.left, py = ev.clientY - r.top, v = st.view;
      const s = Math.max(0.2, Math.min(4, v.s * (ev.deltaY < 0 ? 1.1 : 1 / 1.1))), k = s / v.s;
      st.view = { s, x: px - (px - v.x) * k, y: py - (py - v.y) * k };
    } else st.view = { ...st.view, x: st.view.x - ev.deltaX, y: st.view.y - ev.deltaY };
    render();
  }, { passive: false });

  function askText(p, ev) {
    const r = area.getBoundingClientRect();
    const ta = h('textarea', { class: 'wb-text', rows: 1, 'aria-label': 'Board text (Enter confirms, Shift+Enter breaks the line, Esc cancels)' });
    ta.style.left = (ev.clientX - r.left) + 'px'; ta.style.top = (ev.clientY - r.top - 10) + 'px'; ta.style.color = st.color;
    area.appendChild(ta);
    setTimeout(() => ta.focus(), 0);
    let done = false;
    const close = (keep) => {
      if (done) return; done = true;
      const t = ta.value.replace(/\s+$/, '');
      ta.remove();
      if (keep && t) { remember(); st.scene.elements.push({ id: newId(), type: 'text', x: r1(p.x), y: r1(p.y - 10), width: 0, height: 0, text: t, fontSize: 20, strokeColor: st.color, isDeleted: false, version: 1 }); changed(); }
      area.focus({ preventScroll: true });
    };
    ta.addEventListener('keydown', (e) => { e.stopPropagation(); if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); close(true); } else if (e.key === 'Escape') { e.preventDefault(); close(false); } });
    ta.addEventListener('blur', () => close(true));
  }

  let spacePressed = false;
  area.tabIndex = 0;
  area.addEventListener('keydown', (e) => {
    if (e.target !== area) return;
    const k = e.key.toLowerCase();
    if ((e.ctrlKey || e.metaKey) && k === 'z') { e.preventDefault(); if (e.shiftKey) redo(); else undo(); return; }
    if ((e.ctrlKey || e.metaKey) && k === 'y') { e.preventDefault(); redo(); return; }
    if (e.key === 'Delete' || e.key === 'Backspace') { e.preventDefault(); deleteSelected(); return; }
    if (e.key === ' ') { spacePressed = true; e.preventDefault(); return; }
    const f = TOOLS.find((x) => x.key.toLowerCase() === k);
    if (f && !e.ctrlKey && !e.metaKey && !e.altKey) { e.preventDefault(); pickTool(f.id); }
  });
  area.addEventListener('keyup', (e) => { if (e.key === ' ') spacePressed = false; });
  area.addEventListener('pointerdown', () => { if (document.activeElement !== area && !area.contains(document.activeElement)) area.focus({ preventScroll: true }); });

  // ---------- export / import ----------
  function exportableSvg() {
    const bs = st.scene.elements.map(bounds);
    const minX = bs.length ? Math.min(...bs.map((b) => b.x)) - 20 : 0, minY = bs.length ? Math.min(...bs.map((b) => b.y)) - 20 : 0;
    const maxX = bs.length ? Math.max(...bs.map((b) => b.x + b.w)) + 20 : 400, maxY = bs.length ? Math.max(...bs.map((b) => b.y + b.h)) + 20 : 300;
    const w = Math.max(40, maxX - minX), hh = Math.max(40, maxY - minY);
    const out = sv('svg', { xmlns: 'http://www.w3.org/2000/svg', viewBox: `${minX} ${minY} ${w} ${hh}`, width: Math.ceil(w), height: Math.ceil(hh) },
      defs.cloneNode(true), sv('rect', { x: minX, y: minY, width: w, height: hh, fill: st.scene.appState.viewBackgroundColor || BACKGROUND }),
      ...st.scene.elements.map(drawElement));
    return { text: new XMLSerializer().serializeToString(out), w, h: hh };
  }
  function exportAs(kind) {
    if (!st.scene.elements.length) { toast('The board is empty.', 'err'); return; }
    const { text, w, h: hh } = exportableSvg();
    if (kind === 'svg') { download('whiteboard.svg', new Blob([text], { type: 'image/svg+xml' })); return; }
    const img = new Image();
    img.onload = () => {
      const scale = 2, c = document.createElement('canvas');
      c.width = Math.ceil(w * scale); c.height = Math.ceil(hh * scale);
      const g = c.getContext('2d'); g.scale(scale, scale); g.drawImage(img, 0, 0, w, hh);
      c.toBlob((b) => { if (b) download('whiteboard.png', b); else toast('Could not generate the PNG.', 'err'); }, 'image/png');
    };
    img.onerror = () => toast('Could not generate the PNG.', 'err');
    img.src = 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(text);
  }
  function downloadJson() { download('whiteboard.excalidraw.json', new Blob([JSON.stringify(st.scene, null, 2)], { type: 'application/json' })); }
  fileInput.addEventListener('change', async () => {
    const f = fileInput.files && fileInput.files[0];
    fileInput.value = '';
    if (!f) return;
    try {
      const scene = normalizeScene(JSON.parse(await f.text()));
      const ok = !st.scene.elements.length || await confirmDialog({ title: 'Import board?', text: `Replaces the current board with the ${scene.elements.length} element(s) from ${f.name}. You can undo it while this tab stays open.`, yesText: 'Import' });
      if (!ok) return;
      remember(); st.scene = scene; st.sel = null; changed(); fitView();
    } catch (_) { toast('That file is not a valid scene JSON.', 'err'); }
  });

  // ---------- server ----------
  function fitView() {
    const r = svg.getBoundingClientRect();
    if (!st.scene.elements.length || !r.width) { st.view = { x: 0, y: 0, s: 1 }; render(); return; }
    const bs = st.scene.elements.map(bounds);
    const minX = Math.min(...bs.map((b) => b.x)), minY = Math.min(...bs.map((b) => b.y));
    const maxX = Math.max(...bs.map((b) => b.x + b.w)), maxY = Math.max(...bs.map((b) => b.y + b.h));
    const s = Math.max(0.2, Math.min(1.5, (r.width - 60) / Math.max(1, maxX - minX), (r.height - 60) / Math.max(1, maxY - minY)));
    st.view = { s, x: (r.width - (maxX - minX) * s) / 2 - minX * s, y: (r.height - (maxY - minY) * s) / 2 - minY * s };
    render();
  }
  async function open(force) {
    if (st.loading || (st.loaded && !force)) return;
    st.loading = true; indicatorText();
    try {
      const r = await api.get('whiteboard', 20000);
      st.scene = normalizeScene(r.data); st.version = typeof r.version === 'number' ? r.version : 0; st.updatedAt = r.updated_at || null;
      st.dirty = false; st.error = null; st.conflict = null; st.loaded = true; st.undoStack = []; st.redoStack = []; st.sel = null;
      clearTimeout(st.timer);
      requestAnimationFrame(fitView);
    } catch (e) { st.error = e.status === 404 ? 'the server does not offer the board yet' : e.message; }
    finally { st.loading = false; render(); toolbar(); indicatorText(); }
  }
  async function save(baseVersion) {
    if (st.saving || (!st.dirty && baseVersion == null)) return;
    const data = JSON.parse(JSON.stringify(st.scene));
    const body = JSON.stringify(data);
    if (body.length > MAX_BYTES) { st.error = 'the board went over 8 MB; delete large freehand drawings'; indicatorText(); return; }
    st.saving = true; st.error = null; indicatorText();
    const sent = JSON.stringify(st.scene.elements);
    try {
      const r = await api.post('whiteboard', { data, base_version: baseVersion != null ? baseVersion : st.version }, 30000);
      st.version = typeof r.version === 'number' ? r.version : st.version + 1; st.updatedAt = r.updated_at || new Date().toISOString();
      st.dirty = JSON.stringify(st.scene.elements) !== sent; st.conflict = null;
    } catch (e) {
      if (e.status === 409) { const b = e.body || {}; const v = typeof b.version === 'number' ? b.version : null; st.conflict = { version: v }; }
      else st.error = e.message;
    } finally {
      st.saving = false; indicatorText();
      if (st.dirty && !st.conflict && !st.error) { clearTimeout(st.timer); st.timer = setTimeout(save, AUTOSAVE_MS); }
    }
  }
  async function overwrite() {
    let v = st.conflict && st.conflict.version;
    if (v == null) { try { v = (await api.get('whiteboard', 20000)).version; } catch (e) { st.error = e.message; indicatorText(); return; } }
    st.conflict = null; st.dirty = true; await save(v);
  }
  /** Called by the panel while the board tab is visible: detects a newer version saved by another tab. */
  async function checkRemote() {
    if (!st.loaded || st.saving || st.loading || st.conflict) return;
    try {
      const r = await api.get('whiteboard', 15000);
      if (typeof r.version !== 'number' || r.version <= st.version) return;
      if (st.dirty) { st.conflict = { version: r.version }; indicatorText(); return; }
      st.scene = normalizeScene(r.data); st.version = r.version; st.updatedAt = r.updated_at || null; st.sel = null;
      render(); indicatorText();
      toast('The board was updated by another tab.', 'ok');
    } catch (_) { /* silent: the disconnected banner already covers network failures */ }
  }

  toolbar(); render(); indicatorText();
  return { element, open, checkRemote, isDirty: () => st.dirty, fit: fitView };
}
