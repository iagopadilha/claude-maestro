/* Viewer/editor for a .specs .md file: GET specs/file (text + version), PUT specs/file with the version
   that was read. 409 = another process changed the file: offers to reload or overwrite. If the back end refuses
   the write, the file becomes read-only with the reason. Ctrl+S saves. */
import { h, fill } from '../util/dom.js';
import { renderMarkdown } from './markdown.js';
import { toast } from './toast.js';
import { copyText } from './copy.js';

export function createMarkdownEditor({ api, onSave }) {
  const st = { path: null, text: '', original: '', version: null, mode: 'read', readOnly: false, readOnlyReason: null, loading: false, saving: false, conflict: null, error: null, isNew: false, seq: 0 };
  const title = h('span', { class: 'ed-path mono' });
  const badge = h('span', { class: 'ed-badge', 'aria-live': 'polite' });
  const btnRead = h('button', { type: 'button', role: 'tab', class: 'seg-btn', 'aria-selected': 'true' }, 'Read');
  const btnEdit = h('button', { type: 'button', role: 'tab', class: 'seg-btn', 'aria-selected': 'false' }, 'Edit');
  const tabs = h('div', { class: 'seg', role: 'tablist', 'aria-label': 'File mode' }, btnRead, btnEdit);
  const btnSave = h('button', { type: 'button', class: 'btn btn-sm btn-primary', title: 'Ctrl+S' }, 'Save');
  const btnDiscard = h('button', { type: 'button', class: 'btn btn-sm btn-ghost' }, 'Discard');
  const btnReload = h('button', { type: 'button', class: 'btn btn-sm btn-ghost', title: 'reads the file again from disk' }, 'Reload');
  const btnCopy = h('button', { type: 'button', class: 'btn btn-sm btn-ghost', title: 'copies the file path' }, 'Copy path');
  const bar = h('div', { class: 'ed-bar' }, title, badge, h('span', { class: 'spacer' }), tabs, btnDiscard, btnSave, btnReload, btnCopy);
  const notice = h('div', { class: 'ed-notice', role: 'alert', hidden: true });
  const area = h('textarea', { class: 'ed-area', spellcheck: 'false', 'aria-label': 'File content (markdown)' });
  const preview = h('div', { class: 'md-doc ed-preview', 'aria-label': 'Preview' });
  const reader = h('article', { class: 'md-doc ed-read', tabindex: '0' });
  const body = h('div', { class: 'ed-body' });
  const empty = h('div', { class: 'ed-empty', text: 'Pick a file in the tree on the left to read it. Feature files, PROJECT, RULES, TEST-ENVIRONMENT and codebase files can be edited here.' });
  const element = h('section', { class: 'ed', 'aria-label': 'File' }, bar, notice, body);

  const changed = () => st.text !== st.original;
  let previewTimer = null;
  function refreshPreview() { clearTimeout(previewTimer); previewTimer = setTimeout(() => fill(preview, renderMarkdown(st.text)), 120); }

  function draw() {
    const hasFile = !!st.path;
    bar.hidden = !hasFile;
    title.textContent = st.path || '';
    const label = st.loading ? 'loading…' : st.saving ? 'saving…' : st.readOnly ? 'read-only' : changed() ? 'changed, not saved' : st.isNew ? 'new file' : st.version ? 'saved' : '';
    badge.textContent = label;
    badge.className = 'ed-badge' + (st.readOnly ? ' ro' : changed() ? ' dirty' : '');
    btnRead.setAttribute('aria-selected', String(st.mode === 'read')); btnEdit.setAttribute('aria-selected', String(st.mode === 'edit'));
    btnRead.tabIndex = st.mode === 'read' ? 0 : -1; btnEdit.tabIndex = st.mode === 'edit' ? 0 : -1;
    btnEdit.disabled = st.readOnly || st.loading;
    btnEdit.title = st.readOnly ? (st.readOnlyReason || 'read-only') : '';
    btnSave.hidden = st.mode !== 'edit'; btnDiscard.hidden = st.mode !== 'edit';
    btnSave.disabled = !changed() || st.saving || st.readOnly;
    btnDiscard.disabled = !changed() || st.saving;
    area.readOnly = st.readOnly;
    // notice
    const notices = [];
    if (st.error) notices.push(h('div', { class: 'ed-av ed-av-bad' }, h('strong', { text: 'Could not do it: ' }), st.error));
    if (st.readOnly && st.readOnlyReason) notices.push(h('div', { class: 'ed-av ed-av-ro' }, h('strong', { text: 'Read-only. ' }), st.readOnlyReason));
    if (st.conflict) {
      notices.push(h('div', { class: 'ed-av ed-av-warn' },
        h('div', {}, h('strong', { text: 'The file changed on disk after you opened it. ' }), 'Another process (the loop, another tab) wrote a new version.'),
        h('div', { class: 'card-row' },
          h('button', { type: 'button', class: 'btn btn-sm', onclick: () => load(st.path, { force: true }) }, 'Reload (discards my changes)'),
          h('button', { type: 'button', class: 'btn btn-sm btn-danger', onclick: overwrite }, 'Overwrite with my version'),
          h('button', { type: 'button', class: 'btn btn-sm btn-ghost', onclick: () => copyText(st.text, 'Your text was copied.') }, 'Copy my text'))));
    }
    fill(notice, notices); notice.hidden = !notices.length;
    // body
    if (!hasFile) { if (body.firstChild !== empty) fill(body, empty); return; }
    if (st.mode === 'edit') {
      if (!body.querySelector('.ed-split')) fill(body, h('div', { class: 'ed-split' }, area, preview));
      if (area.value !== st.text) area.value = st.text;
      refreshPreview();
    } else {
      if (body.firstChild !== reader) fill(body, reader);
      fill(reader, st.text.trim() ? renderMarkdown(st.text) : [h('p', { class: 'md-p dim', text: st.loading ? 'loading…' : '(empty file)' })]);
    }
  }

  async function load(path, options = {}) {
    // reloading the same file keeps what the tree said (editable / reason)
    const openOptions = { ...(path === st.path && st.lastOptions ? st.lastOptions : {}), ...options };
    st.lastOptions = { editable: openOptions.editable, reason: openOptions.reason };
    const { force = false, isNew = false } = openOptions;
    const seq = ++st.seq;
    st.path = path; st.loading = true; st.error = null; st.conflict = null; st.isNew = false;
    if (force || path !== st.loadedPath) { st.readOnly = false; st.readOnlyReason = null; }
    draw();
    try {
      const r = await api.get('specs/file?path=' + encodeURIComponent(path), 15000);
      if (seq !== st.seq) return;
      const text = r.text != null ? String(r.text) : r.content != null ? String(r.content) : '';
      st.text = text; st.original = text; st.version = r.version == null ? null : r.version; st.loadedPath = path;
      const ro = r.editable === false || openOptions.editable === false;
      st.readOnly = ro; st.readOnlyReason = ro ? (r.not_editable_reason || openOptions.reason || 'the app does not allow editing this file from here') : null;
      if (ro && st.mode === 'edit') st.mode = 'read';
    } catch (e) {
      if (seq !== st.seq) return;
      if (e.status === 404 && isNew) {
        st.text = ''; st.original = ''; st.version = null; st.isNew = true; st.loadedPath = path; st.mode = 'edit';
      } else {
        st.text = ''; st.original = ''; st.version = null; st.error = e.status === 404 ? 'file not found' : e.message;
      }
    } finally {
      if (seq === st.seq) { st.loading = false; draw(); }
    }
  }
  async function write(version) {
    st.saving = true; st.error = null; draw();
    try {
      // empty version = new file (the back end creates it)
      const r = await api.put('specs/file', { path: st.path, content: st.text, version: version == null ? '' : version }, 30000);
      st.original = st.text; st.version = r.version != null ? r.version : st.version; st.conflict = null; st.isNew = false;
      toast('File saved.', 'ok');
      if (onSave) onSave(st.path);
    } catch (e) {
      if (e.status === 409) st.conflict = { version: e.body ? (e.body.current_version != null ? e.body.current_version : e.body.version != null ? e.body.version : null) : null };
      else if ([400, 403, 405, 423].includes(e.status)) { st.readOnly = true; st.readOnlyReason = e.message; }
      else st.error = e.message;
    } finally { st.saving = false; draw(); }
  }
  async function overwrite() {
    let version = st.conflict && st.conflict.version;
    if (version == null) {
      try { const r = await api.get('specs/file?path=' + encodeURIComponent(st.path), 15000); version = r.version; } catch (e) { st.error = e.message; draw(); return; }
    }
    st.conflict = null;
    await write(version);
  }
  function save() { if (st.mode === 'edit' && changed() && !st.saving && !st.readOnly) write(st.version); }

  btnRead.addEventListener('click', () => { st.mode = 'read'; draw(); });
  btnEdit.addEventListener('click', () => { if (st.readOnly) return; st.mode = 'edit'; draw(); area.focus(); });
  tabs.addEventListener('keydown', (e) => { if (e.key === 'ArrowRight' || e.key === 'ArrowLeft') { e.preventDefault(); (st.mode === 'read' ? btnEdit : btnRead).click(); (st.mode === 'read' ? btnRead : btnEdit).focus(); } });
  btnSave.addEventListener('click', save);
  btnDiscard.addEventListener('click', () => { st.text = st.original; st.conflict = null; draw(); });
  btnReload.addEventListener('click', () => { if (st.path) load(st.path, { force: true }); });
  btnCopy.addEventListener('click', () => { if (st.path) copyText(st.path, 'Path copied.'); });
  area.addEventListener('input', () => { st.text = area.value; draw(); });
  element.addEventListener('keydown', (e) => { if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 's') { e.preventDefault(); save(); } });
  draw();

  return {
    element,
    open: (path, options) => load(path, options),
    path: () => st.path,
    hasChanges: () => changed(),
    showMessage(text) { st.path = null; st.text = ''; st.original = ''; draw(); empty.textContent = text; fill(body, empty); },
  };
}
