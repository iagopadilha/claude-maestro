/* Accessible file tree (role=tree): arrows move, Right/Left open/close a folder, Enter opens the file.
   Accepts the back-end tree in close formats (groups[] or one key per group). */
import { h, fill } from '../util/dom.js';

const STATUS_COLOR = {
  pending: '#7D838C', speccing: '#8AA4FF', specced: '#A58BFF', approved: '#5CC8D6',
  building: '#F2B544', verified: '#5BD38A', blocked: '#F06A5E',
};
const GROUP_TITLES = { project: 'Project', codebase: 'Codebase', features: 'Features', loop: 'Loop', other: 'Other' };
const GROUP_ORDER = ['project', 'codebase', 'features', 'loop', 'other'];

const baseName = (p) => String(p == null ? '' : p).replace(/\\/g, '/').split('/').filter(Boolean).pop() || String(p || '');
const childrenOf = (x) => (Array.isArray(x.files) ? x.files : Array.isArray(x.children) ? x.children : Array.isArray(x.items) ? x.items : null);

function normItem(x, key) {
  if (x == null) return null;
  if (typeof x === 'string') return { kind: 'file', name: baseName(x), path: x };
  const children = childrenOf(x);
  if (children || x.kind === 'dir' || x.kind === 'feature') {
    return { kind: 'dir', name: String(x.name || x.title || x.slug || x.id || key || 'folder'), slug: x.slug || x.id || key || null, status: x.status || null,
      outsideRoadmap: x.in_roadmap === false, path: x.path || null, items: normItems(children || []) };
  }
  const path = x.path || null;
  if (!path) return null;
  return { kind: 'file', name: String(x.name || baseName(path)), path: String(path), editable: typeof x.editable === 'boolean' ? x.editable : null,
    reason: x.not_editable_reason || null };
}
function normItems(v) {
  if (Array.isArray(v)) return v.map((x) => normItem(x)).filter(Boolean);
  if (v && typeof v === 'object') return Object.entries(v).map(([k, x]) => normItem(Array.isArray(x) ? { slug: k, files: x } : typeof x === 'string' ? x : { slug: k, ...x }, k)).filter(Boolean);
  return [];
}

/** Normalizes the GET specs/tree response into [{id, title, items}]. */
export function normalizeTree(r) {
  if (!r) return [];
  const base = Array.isArray(r) ? r : (r.groups || (Array.isArray(r.tree) ? r.tree : null));
  if (Array.isArray(base)) {
    return base.map((g, i) => ({ id: String(g.id || g.key || i), title: String(g.title || GROUP_TITLES[g.id] || g.name || g.id || 'Group'), items: normItems(childrenOf(g) || g.features || []) }));
  }
  const obj = r.tree && typeof r.tree === 'object' ? r.tree : r;
  const keys = Object.keys(obj).filter((k) => GROUP_TITLES[k]).sort((a, b) => GROUP_ORDER.indexOf(a) - GROUP_ORDER.indexOf(b));
  return keys.map((k) => ({ id: k, title: GROUP_TITLES[k], items: normItems(obj[k]) }));
}

/** Walks every node (for lookups such as "spec.md of feature X"). */
export function walkTree(groups, fn) {
  const go = (items, parent, group, anc) => { for (const n of items) { fn(n, parent, group, anc); if (n.kind === 'dir') go(n.items, n, group, [...anc, n]); } };
  for (const g of groups) go(g.items, null, g, []);
}

export function createFileTree({ onOpen, label = '.specs files' }) {
  const st = { groups: [], open: new Set(['g:project', 'g:features']), selected: null, filter: '', focus: null };
  const list = h('div', { class: 'tree', role: 'tree', 'aria-label': label });
  const filter = h('input', { type: 'search', class: 'tree-filter', placeholder: 'filter files…', 'aria-label': 'Filter files', autocomplete: 'off', spellcheck: 'false' });
  filter.addEventListener('input', () => { st.filter = filter.value.trim().toLowerCase(); draw(); });
  const element = h('div', { class: 'tree-wrap' }, filter, list);
  let visible = [];

  const keyOf = (n, group) => (n.kind === 'file' ? 'f:' + n.path : 'd:' + group.id + ':' + (n.slug || n.name));
  const matches = (n) => !st.filter || (n.kind === 'file' ? (n.path + ' ' + n.name).toLowerCase().includes(st.filter) : n.items.some(matches) || String(n.name).toLowerCase().includes(st.filter));

  function row(n, level, group, key, parent) {
    const isDir = n.kind !== 'file';
    const isOpen = isDir && (st.filter ? true : st.open.has(key));
    const sel = !isDir && st.selected === n.path;
    const kids = [h('span', { class: 'tree-chev', 'aria-hidden': 'true', text: isDir ? (isOpen ? '▾' : '▸') : '' })];
    if (n.status) {
      const c = STATUS_COLOR[n.status] || '#7D838C';
      const dot = h('span', { class: 'dot', 'aria-hidden': 'true' }); dot.style.background = c;
      kids.push(dot);
    }
    kids.push(h('span', { class: 'tree-name', text: n.name }));
    if (n.status) { const chip = h('span', { class: 'tree-st', text: n.status }); chip.style.color = STATUS_COLOR[n.status] || '#7D838C'; kids.push(chip); }
    if (n.outsideRoadmap) kids.push(h('span', { class: 'tree-n', title: 'feature folder that is not in the ROADMAP', text: 'not in roadmap' }));
    if (!isDir && n.editable === false) kids.push(h('span', { class: 'tree-ro', title: n.reason || 'read-only', 'aria-label': 'read-only', text: 'read' }));
    if (isDir && !n.status && group.id !== 'root' && level === 1) kids.push(h('span', { class: 'tree-n', text: String(n.items.length) }));
    const el = h('div', { class: 'tree-row' + (isDir ? ' dir' : ' file') + (sel ? ' sel' : '') + (level === 0 ? ' group' : ''), role: 'treeitem', 'aria-level': String(level + 1),
      'aria-expanded': isDir ? String(isOpen) : null, 'aria-selected': isDir ? null : String(sel), tabindex: '-1', title: n.path || n.name }, kids);
    el.style.paddingLeft = (6 + level * 14) + 'px';
    el.addEventListener('click', () => activate(key));
    visible.push({ key, n, el, isDir, isOpen, parent, group });
    return el;
  }
  function activate(key) {
    const v = visible.find((x) => x.key === key); if (!v) return;
    st.focus = key;
    if (v.isDir) { if (st.open.has(key)) st.open.delete(key); else st.open.add(key); draw(); return; }
    st.selected = v.n.path; draw();
    if (onOpen) onOpen(v.n);
  }
  function draw() {
    visible = [];
    const rows = [];
    const go = (items, level, group, parent) => {
      for (const n of items) {
        if (!matches(n)) continue;
        const key = keyOf(n, group);
        rows.push(row(n, level, group, key, parent));
        const isOpen = n.kind !== 'file' && (st.filter ? true : st.open.has(key));
        if (isOpen) go(n.items, level + 1, group, key);
      }
    };
    for (const g of st.groups) {
      const node = { kind: 'dir', name: g.title, items: g.items, slug: 'g' };
      if (!matches(node) && st.filter) continue;
      const key = 'g:' + g.id;
      rows.push(row(node, 0, { id: 'root' }, key, null));
      if (st.filter || st.open.has(key)) {
        if (!g.items.length) rows.push(h('div', { class: 'tree-empty', text: 'empty' }));
        go(g.items, 1, g, key);
      }
    }
    if (!rows.length) rows.push(h('div', { class: 'tree-empty', text: st.filter ? 'no file with that name' : 'no file in .specs' }));
    const hadFocus = list.contains(document.activeElement);
    fill(list, rows);
    const target = visible.find((v) => v.key === st.focus) || visible.find((v) => !v.isDir && v.n.path === st.selected) || visible[0];
    if (target) { target.el.tabIndex = 0; if (hadFocus) target.el.focus(); }
  }
  list.addEventListener('keydown', (e) => {
    const i = visible.findIndex((v) => v.el === document.activeElement);
    if (i < 0) return;
    const v = visible[i];
    const go = (j) => { const target = visible[Math.max(0, Math.min(visible.length - 1, j))]; if (!target) return; for (const x of visible) x.el.tabIndex = -1; target.el.tabIndex = 0; target.el.focus(); st.focus = target.key; };
    if (e.key === 'ArrowDown') go(i + 1);
    else if (e.key === 'ArrowUp') go(i - 1);
    else if (e.key === 'Home') go(0);
    else if (e.key === 'End') go(visible.length - 1);
    else if (e.key === 'ArrowRight') { if (v.isDir && !v.isOpen) { st.open.add(v.key); st.focus = v.key; draw(); } else if (v.isDir) go(i + 1); }
    else if (e.key === 'ArrowLeft') { if (v.isDir && v.isOpen && !st.filter) { st.open.delete(v.key); st.focus = v.key; draw(); } else if (v.parent) { const j = visible.findIndex((x) => x.key === v.parent); if (j >= 0) go(j); } }
    else if (e.key === 'Enter' || e.key === ' ') activate(v.key);
    else return;
    e.preventDefault();
  });

  return {
    element,
    set(groups) { st.groups = groups || []; draw(); },
    /** Marks a file as selected and opens the folders down to it. */
    select(path) {
      st.selected = path;
      walkTree(st.groups, (n, parent, group, anc) => {
        if (n.kind === 'file' && n.path === path) {
          st.open.add('g:' + group.id);
          for (const a of anc) st.open.add('d:' + group.id + ':' + (a.slug || a.name));
          st.focus = 'f:' + path;
        }
      });
      draw();
    },
    groups: () => st.groups,
  };
}
