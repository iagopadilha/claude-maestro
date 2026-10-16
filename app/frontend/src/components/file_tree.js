/* Accessible file tree (role=tree): arrows move, Right/Left open/close a folder, Enter opens the file.
   Accepts the back-end tree in close formats (groups[] or one key per group). */
import { html, useState, useLayoutEffect, useMemo } from '../util/preact.js';

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

/** State and actions of one tree; `core.draw` is set by the mounted <FileTree>. */
function treeCore({ onOpen }) {
  const st = { groups: [], open: new Set(['g:project', 'g:features']), selected: null, filter: '', filterText: '', focus: null };
  const core = { st, visible: [], els: new Map(), list: null, hadFocus: false, draw: () => {} };
  const redraw = () => { core.hadFocus = !!(core.list && core.list.contains(document.activeElement)); core.draw(); };
  const elOf = (v) => core.els.get(v.key);

  function activate(key) {
    const v = core.visible.find((x) => x.key === key); if (!v) return;
    st.focus = key;
    if (v.isDir) { if (st.open.has(key)) st.open.delete(key); else st.open.add(key); redraw(); return; }
    st.selected = v.n.path; redraw();
    if (onOpen) onOpen(v.n);
  }
  function onKeyDown(e) {
    const visible = core.visible;
    const i = visible.findIndex((v) => elOf(v) === document.activeElement);
    if (i < 0) return;
    const v = visible[i];
    const go = (j) => {
      const target = visible[Math.max(0, Math.min(visible.length - 1, j))]; if (!target) return;
      st.focus = target.key; redraw();
      const el = elOf(target); if (el && el !== document.activeElement) el.focus();
    };
    if (e.key === 'ArrowDown') go(i + 1);
    else if (e.key === 'ArrowUp') go(i - 1);
    else if (e.key === 'Home') go(0);
    else if (e.key === 'End') go(visible.length - 1);
    else if (e.key === 'ArrowRight') { if (v.isDir && !v.isOpen) { st.open.add(v.key); st.focus = v.key; redraw(); } else if (v.isDir) go(i + 1); }
    else if (e.key === 'ArrowLeft') { if (v.isDir && v.isOpen && !st.filter) { st.open.delete(v.key); st.focus = v.key; redraw(); } else if (v.parent) { const j = visible.findIndex((x) => x.key === v.parent); if (j >= 0) go(j); } }
    else if (e.key === 'Enter' || e.key === ' ') activate(v.key);
    else return;
    e.preventDefault();
  }
  Object.assign(core, {
    redraw, activate, onKeyDown,
    setFilter(value) { st.filterText = value; st.filter = value.trim().toLowerCase(); redraw(); },
    handle: {
      set(groups) { st.groups = groups || []; redraw(); },
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
        redraw();
      },
      groups: () => st.groups,
    },
  });
  return core;
}

const keyOf = (n, group) => (n.kind === 'file' ? 'f:' + n.path : 'd:' + group.id + ':' + (n.slug || n.name));

/** Flat list of the visible rows (and the "empty" notes between them), in order. */
function visibleRows(st) {
  const matches = (n) => !st.filter || (n.kind === 'file' ? (n.path + ' ' + n.name).toLowerCase().includes(st.filter) : n.items.some(matches) || String(n.name).toLowerCase().includes(st.filter));
  const rows = [];
  const push = (n, level, group, key, parent) => {
    const isDir = n.kind !== 'file';
    const isOpen = isDir && (st.filter ? true : st.open.has(key));
    rows.push({ key, n, level, group, parent, isDir, isOpen });
    return isOpen;
  };
  const go = (items, level, group, parent) => {
    for (const n of items) {
      if (!matches(n)) continue;
      const key = keyOf(n, group);
      if (push(n, level, group, key, parent)) go(n.items, level + 1, group, key);
    }
  };
  for (const g of st.groups) {
    const node = { kind: 'dir', name: g.title, items: g.items, slug: 'g' };
    if (!matches(node) && st.filter) continue;
    const key = 'g:' + g.id;
    if (push(node, 0, { id: 'root' }, key, null)) {
      if (!g.items.length) rows.push({ key: 'e:' + g.id, empty: 'empty' });
      go(g.items, 1, g, key);
    }
  }
  if (!rows.length) rows.push({ key: 'e:none', empty: st.filter ? 'no file with that name' : 'no file in .specs' });
  return rows;
}

function TreeRow({ core, v, tab }) {
  const { n, level, group, isDir, isOpen } = v;
  const sel = !isDir && core.st.selected === n.path;
  const c = n.status ? STATUS_COLOR[n.status] || '#7D838C' : null;
  return html`<div ref=${(el) => { if (el) core.els.set(v.key, el); }} class=${'tree-row' + (isDir ? ' dir' : ' file') + (sel ? ' sel' : '') + (level === 0 ? ' group' : '')}
      role="treeitem" aria-level=${String(level + 1)} aria-expanded=${isDir ? String(isOpen) : null} aria-selected=${isDir ? null : String(sel)}
      tabindex=${tab ? '0' : '-1'} title=${n.path || n.name} style=${{ paddingLeft: (6 + level * 14) + 'px' }} onClick=${() => core.activate(v.key)}>
    <span class="tree-chev" aria-hidden="true">${isDir ? (isOpen ? '▾' : '▸') : ''}</span>
    ${n.status ? html`<span class="dot" aria-hidden="true" style=${{ background: c }}></span>` : null}
    <span class="tree-name">${n.name}</span>
    ${n.status ? html`<span class="tree-st" style=${{ color: c }}>${n.status}</span>` : null}
    ${n.outsideRoadmap ? html`<span class="tree-n" title="feature folder that is not in the ROADMAP">not in roadmap</span>` : null}
    ${!isDir && n.editable === false ? html`<span class="tree-ro" title=${n.reason || 'read-only'} aria-label="read-only">read</span>` : null}
    ${isDir && !n.status && group.id !== 'root' && level === 1 ? html`<span class="tree-n">${String(n.items.length)}</span>` : null}
  </div>`;
}

/** Content of div.tree-wrap: filter + role=tree list. */
function TreeView({ core, label }) {
  const { st } = core;
  const rows = visibleRows(st);
  const visible = rows.filter((r) => !r.empty);
  core.visible = visible;
  const target = visible.find((v) => v.key === st.focus) || visible.find((v) => !v.isDir && v.n.path === st.selected) || visible[0];
  // rows are kept by key: if the focus was in the list, it lands on the target row
  useLayoutEffect(() => {
    if (!core.hadFocus || !target) return;
    core.hadFocus = false;
    const el = core.els.get(target.key);
    if (el && el !== document.activeElement) el.focus();
  });
  return html`
    <input type="search" class="tree-filter" placeholder="filter files…" aria-label="Filter files" autocomplete="off" spellcheck=${false}
      value=${st.filterText} onInput=${(e) => core.setFilter(e.currentTarget.value)} />
    <div class="tree" role="tree" aria-label=${label} ref=${(el) => { core.list = el; }} onKeyDown=${core.onKeyDown}>
      ${rows.map((r) => (r.empty ? html`<div key=${r.key} class="tree-empty">${r.empty}</div>` : html`<${TreeRow} key=${r.key} core=${core} v=${r} tab=${r === target} />`))}
    </div>`;
}

/** Tree state that outlives the component (e.g. across tab switches): <FileTree store=${s} />; s.set/select/groups.
    While no <FileTree> is mounted, changes only update the state (draw is a no-op on an unmounted component). */
export function fileTreeStore({ onOpen }) {
  const core = treeCore({ onOpen });
  return { ...core.handle, core };
}

/** <FileTree onOpen label groups treeRef store /> -> treeRef.current = { set, select, groups }. */
export function FileTree({ onOpen, label = '.specs files', groups, treeRef, store }) {
  const [, tick] = useState(0);
  const core = useMemo(() => (store ? store.core : treeCore({ onOpen })), []);
  core.draw = () => tick((n) => n + 1);
  if (groups && groups !== core.st.groups) core.st.groups = groups;
  if (treeRef) treeRef.current = core.handle;
  return html`<div class="tree-wrap"><${TreeView} core=${core} label=${label} /></div>`;
}
