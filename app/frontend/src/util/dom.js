/* DOM helpers without innerHTML: every external text goes in through textContent. */

export const $ = (id) => document.getElementById(id);
export const SVGNS = 'http://www.w3.org/2000/svg';

function applyProps(el, props) {
  for (const [k, v] of Object.entries(props || {})) {
    if (v == null || v === false) continue;
    if (k === 'class') el.setAttribute('class', v);
    else if (k === 'text') el.textContent = v;
    else if (k === 'value') el.value = v;
    else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? '' : String(v));
  }
}

export function addKids(el, kids) {
  for (const k of kids.flat(Infinity)) {
    if (k == null || k === false) continue;
    el.appendChild(typeof k === 'object' ? k : document.createTextNode(String(k)));
  }
}

/** Creates an HTML element: h('div', {class, text, onclick}, ...children). */
export const h = (tag, props, ...kids) => { const el = document.createElement(tag); applyProps(el, props); addKids(el, kids); return el; };
/** Creates an SVG element. */
export const sv = (tag, props, ...kids) => { const el = document.createElementNS(SVGNS, tag); applyProps(el, props); addKids(el, kids); return el; };

export const setColors = (el, c, bg) => { if (c) el.style.color = c; if (bg) el.style.background = bg; };
export function fill(container, ...kids) { container.replaceChildren(); addKids(container, kids); }

export function setAttr(el, k, v) {
  if (v == null || v === false) { if (el.hasAttribute(k)) el.removeAttribute(k); return; }
  const s = String(v);
  if (el.getAttribute(k) !== s) el.setAttribute(k, s);
}
export function setText(el, t) { t = t == null ? '' : String(t); if (el.textContent !== t) el.textContent = t; }
export function setStyle(el, k, v) { if (el.style[k] !== v) el.style[k] = v; }

/** Rebuilds the content keeping the focus (and the caret) of the element with an id. */
export function keepFocus(fn) {
  const a = document.activeElement;
  const id = a && a.id;
  const start = a && typeof a.selectionStart === 'number' ? a.selectionStart : null;
  fn();
  if (id) {
    const n = document.getElementById(id);
    if (n && n !== document.activeElement) {
      n.focus();
      if (start != null && n.setSelectionRange) try { n.setSelectionRange(start, start); } catch (_) { /* ok */ }
    }
  }
}

/** Binds arrows/Home/End to a role=tablist (roving focus between the role=tab elements). */
export function bindTabKeys(tablist) {
  tablist.addEventListener('keydown', (e) => {
    const tabs = [...tablist.querySelectorAll('[role="tab"]:not([disabled]):not([hidden])')];
    const i = tabs.indexOf(document.activeElement);
    if (i < 0) return;
    let j = null;
    if (e.key === 'ArrowRight' || e.key === 'ArrowDown') j = (i + 1) % tabs.length;
    else if (e.key === 'ArrowLeft' || e.key === 'ArrowUp') j = (i - 1 + tabs.length) % tabs.length;
    else if (e.key === 'Home') j = 0;
    else if (e.key === 'End') j = tabs.length - 1;
    if (j == null) return;
    e.preventDefault();
    const id = tabs[j].id;
    tabs[j].focus();
    tabs[j].click();
    // tabs that redraw on click: give the focus back by id
    const fresh = id && document.getElementById(id);
    if (fresh && fresh !== document.activeElement) fresh.focus();
  });
}
