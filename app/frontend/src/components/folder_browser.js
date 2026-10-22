/* In-app folder browser (fallback for the native dialog): GET /api/app/system/folders?path= lists only
   subfolders and marks the ones with .specs / git. Pasting a path also works. */
import { html, useState, useEffect, useRef } from '../util/preact.js';
import { appApi } from '../api/api_client.js';
import { openDialog } from './dialog.js';

const NATIVE_DIALOG_TIMEOUT_MS = 310000; // the back end waits up to 300 s for the system window
const NATIVE_CANCELLED_REASON = 'cancelled'; // folder_picker_service.CANCELLED_REASON

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

/** Content of the folder dialog. onValue(path) runs whenever the path the main button would use changes. */
export function FolderBrowser({ initial = '', explanation = '', onValue }) {
  const [L, setL] = useState(null);
  const [field, setField] = useState('');
  const [filter, setFilter] = useState('');
  const [state, setState] = useState('');
  const seq = useRef(0);

  async function go(path) {
    const mine = ++seq.current;
    setState('loading…');
    try {
      const r = await appApi.get('system/folders?path=' + encodeURIComponent(path || ''), 15000);
      if (mine !== seq.current) return;
      const next = normalizeListing(r, path);
      setL(next); setField(next.current); setFilter('');
      const marks = [next.currentHasSpecs ? 'has .specs (a loop project)' : '', next.currentHasGit ? 'is a git repository' : ''].filter(Boolean).join(' · ');
      setState((marks ? 'This folder ' + marks + '.' : '') + (next.truncated ? ' The list was truncated; filter or paste a path.' : ''));
    } catch (e) {
      if (mine !== seq.current) return;
      setState('Could not open it: ' + e.message);
    }
  }
  useEffect(() => { go(initial); return () => { seq.current += 1; }; }, []);
  const value = field.trim() || (L ? L.current : '');
  useEffect(() => { if (onValue) onValue(value); }, [value]);

  const f = filter.trim().toLowerCase();
  const items = L ? L.items : [];
  const vis = items.filter((x) => !f || x.name.toLowerCase().includes(f));
  return html`
    ${explanation ? html`<p class="dlg-text">${explanation}</p>` : null}
    <div class="fb-bar">
      <button type="button" class="btn btn-sm btn-ghost" title="parent folder" disabled=${L ? !L.current : null} onClick=${() => go(L ? L.parent || '' : '')}>↑ Up</button>
      <input type="text" class="fb-field mono" id="fb-field" aria-label="Folder path (you can paste one)" placeholder=${'paste a path, e.g. C:\\Users\\you\\projects\\app'} autocomplete="off" spellcheck="false"
        value=${field} onInput=${(e) => setField(e.currentTarget.value)}
        onKeyDown=${(e) => { if (e.key === 'Enter') { e.preventDefault(); go(e.currentTarget.value.trim()); } }} />
      <button type="button" class="btn btn-sm" onClick=${() => go(field.trim())}>Go</button>
    </div>
    <div class="fb-shortcuts">${(L ? L.shortcuts : []).map((a) => html`<button key=${a.path} type="button" class="btn btn-sm btn-ghost" title=${a.path} onClick=${() => go(a.path)}>${a.name}</button>`)}</div>
    <div class="fb-bar">
      <input type="search" class="fb-filter" placeholder="filter…" aria-label="Filter subfolders" autocomplete="off" value=${filter} onInput=${(e) => setFilter(e.currentTarget.value)} />
      <span class="fb-legend"><span class="fb-tag specs">.specs</span> already has a loop · <span class="fb-tag git">git</span> repository</span>
    </div>
    <div class="fb-list" role="list" aria-label="Subfolders">
      ${!L ? null : vis.length
        ? vis.map((x) => html`<div key=${x.path} role="listitem"><button type="button" class="fb-item" title=${x.path} onClick=${() => go(x.path)}>
            <span class="fb-ic" aria-hidden="true">▸</span><span class="fb-name">${x.name}</span>
            ${x.hasSpecs ? html`<span class="fb-tag specs">.specs</span>` : null}${x.hasGit ? html`<span class="fb-tag git">git</span>` : null}
          </button></div>`)
        : html`<div class="fb-empty">${f ? 'no subfolder with that name' : 'no subfolders'}</div>`}
    </div>
    <div class="fb-state" aria-live="polite">${state}</div>`;
}

/** Opens the browser. Resolves with the chosen path or null. */
export function pickFolder({ title = 'Choose a folder', buttonText = 'Use this folder', initial = '', explanation = '' } = {}) {
  return new Promise((resolve) => {
    let chosen = null, d = null;
    const actions = (value) => [
      { text: 'Cancel' },
      { text: buttonText, cls: 'btn-primary', disabled: !value, onClick: (api) => { chosen = value; api.close(); } },
    ];
    d = openDialog({
      title, width: 640, cls: 'dlg-folders',
      content: html`<${FolderBrowser} initial=${initial} explanation=${explanation} onValue=${(v) => { if (d) d.setActions(actions(v)); }} />`,
      actions: actions(''),
      onClose: () => resolve(chosen),
    });
  });
}

/** The system folder window first (POST projects/pick-folder); the in-app browser only when the system window is unavailable or failed.
    Resolves with the chosen path or null (cancelled). */
export async function pickFolderNativeFirst({ initial = '', title, buttonText, explanation } = {}) {
  try {
    const r = await appApi.post('projects/pick-folder', { initial }, NATIVE_DIALOG_TIMEOUT_MS);
    if (r && r.path) return r.path;
    if (r && r.reason === NATIVE_CANCELLED_REASON) return null;
  } catch (_) { /* the system window failed: use the in-app browser */ }
  return pickFolder({ initial, title, buttonText, explanation });
}
