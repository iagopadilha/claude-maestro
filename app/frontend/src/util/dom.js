/* DOM helpers that never parse HTML strings: every external text goes in through textContent. The panel itself is Preact;
   these serve the imperative widgets (pan/zoom transforms) and the tablist keys. */

export const $ = (id) => document.getElementById(id);
const SVGNS = 'http://www.w3.org/2000/svg';

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

function addKids(el, kids) {
  for (const k of kids.flat(Infinity)) {
    if (k == null || k === false) continue;
    el.appendChild(typeof k === 'object' ? k : document.createTextNode(String(k)));
  }
}

/** Creates an SVG element: sv('path', {d, class}, ...children). */
export const sv = (tag, props, ...kids) => { const el = document.createElementNS(SVGNS, tag); applyProps(el, props); addKids(el, kids); return el; };

export function setStyle(el, k, v) { if (el.style[k] !== v) el.style[k] = v; }

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
