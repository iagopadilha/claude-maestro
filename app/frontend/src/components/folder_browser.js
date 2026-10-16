/* In-app folder browser (fallback for the native dialog): GET /api/app/system/folders?path= lists only
   subfolders and marks the ones with .specs / git. Pasting a path also works. */
import { h, fill } from '../util/dom.js';
import { appApi } from '../api/api_client.js';
import { openDialog } from './dialog.js';

const baseName = (p) => String(p || '').replace(/[\\/]+$/, '').split(/[\\/]/).pop() || String(p || '');
function join(base, name) {
  if (!base) return name;
  const sep = base.includes('\\') || /^[A-Za-z]:/.test(base) ? '\\' : '/';
  return base.endsWith('\\') || base.endsWith('/') ? base + name : base + sep + name;
}
function parentOf(p) {
  const s = String(p || '').replace(/[\\/]+$/, '');
  if (!s || /^[A-Za-z]:$/.test(s)) return '';
  const i = Math.max(s.lastIndexOf('\\'), s.lastIndexOf('/'));
  if (i < 0) return '';
  const parent = s.slice(0, i);
  return /^[A-Za-z]:$/.test(parent) ? parent + '\\' : parent || '/';
}
function normalizeListing(r, requested) {
  const current = (r && (r.path != null ? r.path : requested)) || '';
  const raw = (r && (r.folders || (current ? null : r.roots))) || [];
  const items = raw.map((x) => {
    if (typeof x === 'string') return { name: baseName(x) || x, path: /[\\/]/.test(x) ? x : join(current, x), hasSpecs: false, hasGit: false };
    const name = x.name || baseName(x.path) || '?';
    return { name, path: x.path || join(current, name), hasSpecs: !!x.has_specs, hasGit: !!x.has_git };
  });
  const shortcuts = (r && (r.shortcuts || (current ? r.roots : null))) || [];
  return {
    current, items,
    parent: r && r.parent !== undefined ? r.parent : parentOf(current),
    currentHasSpecs: !!(r && r.has_specs), currentHasGit: !!(r && r.has_git),
    truncated: !!(r && r.truncated),
    shortcuts: shortcuts.map((a) => (typeof a === 'string' ? { name: a, path: a } : { name: a.name || a.path, path: a.path || a.name })),
  };
}

/** Opens the browser. Resolves with the chosen path or null. */
export function pickFolder({ title = 'Choose a folder', buttonText = 'Use this folder', initial = '', explanation = '' } = {}) {
  return new Promise((resolve) => {
    let chosen = null, seq = 0, current = '';
    const field = h('input', { type: 'text', class: 'fb-field mono', id: 'fb-field', 'aria-label': 'Folder path (you can paste one)', placeholder: 'paste a path, e.g. C:\\Users\\you\\projects\\app', autocomplete: 'off', spellcheck: 'false' });
    const btnGo = h('button', { type: 'button', class: 'btn btn-sm' }, 'Go');
    const btnUp = h('button', { type: 'button', class: 'btn btn-sm btn-ghost', title: 'parent folder' }, '↑ Up');
    const filter = h('input', { type: 'search', class: 'fb-filter', placeholder: 'filter…', 'aria-label': 'Filter subfolders', autocomplete: 'off' });
    const shortcuts = h('div', { class: 'fb-shortcuts' });
    const list = h('div', { class: 'fb-list', role: 'list', 'aria-label': 'Subfolders' });
    const state = h('div', { class: 'fb-state', 'aria-live': 'polite' });
    let last = [];

    function drawList() {
      const f = filter.value.trim().toLowerCase();
      const vis = last.filter((x) => !f || x.name.toLowerCase().includes(f));
      fill(list, vis.length ? vis.map((x) => h('div', { role: 'listitem' }, h('button', { type: 'button', class: 'fb-item', title: x.path, onclick: () => go(x.path) },
        h('span', { class: 'fb-ic', 'aria-hidden': 'true', text: '▸' }), h('span', { class: 'fb-name', text: x.name }),
        x.hasSpecs ? h('span', { class: 'fb-tag specs', text: '.specs' }) : null, x.hasGit ? h('span', { class: 'fb-tag git', text: 'git' }) : null)))
        : [h('div', { class: 'fb-empty', text: f ? 'no subfolder with that name' : 'no subfolders' })]);
    }
    async function go(path) {
      const mine = ++seq;
      state.textContent = 'loading…';
      try {
        const r = await appApi.get('system/folders?path=' + encodeURIComponent(path || ''), 15000);
        if (mine !== seq) return;
        const L = normalizeListing(r, path);
        current = L.current; field.value = L.current; last = L.items; filter.value = '';
        btnUp.disabled = !L.current;
        btnUp.onclick = () => go(L.parent || '');
        fill(shortcuts, L.shortcuts.map((a) => h('button', { type: 'button', class: 'btn btn-sm btn-ghost', title: a.path, onclick: () => go(a.path) }, a.name)));
        const marks = [L.currentHasSpecs ? 'has .specs (a loop project)' : '', L.currentHasGit ? 'is a git repository' : ''].filter(Boolean).join(' · ');
        state.textContent = (marks ? 'This folder ' + marks + '.' : '') + (L.truncated ? ' The list was truncated; filter or paste a path.' : '');
        drawList();
        d.setActions(actions());
      } catch (e) {
        if (mine !== seq) return;
        state.textContent = 'Could not open it: ' + e.message;
      }
    }
    const actions = () => [
      { text: 'Cancel' },
      { text: buttonText, cls: 'btn-primary', disabled: !(field.value.trim() || current), onClick: (api) => { chosen = field.value.trim() || current; api.close(); } },
    ];
    btnGo.addEventListener('click', () => go(field.value.trim()));
    field.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); go(field.value.trim()); } });
    field.addEventListener('input', () => d.setActions(actions()));
    filter.addEventListener('input', drawList);
    const d = openDialog({
      title, width: 640, cls: 'dlg-folders',
      content: [
        explanation ? h('p', { class: 'dlg-text', text: explanation }) : null,
        h('div', { class: 'fb-bar' }, btnUp, field, btnGo), shortcuts,
        h('div', { class: 'fb-bar' }, filter, h('span', { class: 'fb-legend' }, h('span', { class: 'fb-tag specs', text: '.specs' }), ' already has a loop · ', h('span', { class: 'fb-tag git', text: 'git' }), ' repository')),
        list, state,
      ],
      actions: actions(),
      onClose: () => resolve(chosen),
    });
    go(initial);
  });
}
