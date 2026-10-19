/* Viewer/editor for a .specs .md file: GET specs/file (text + version), PUT specs/file with the version
   that was read. 409 = another process changed the file: offers to reload or overwrite. If the back end refuses
   the write, the file becomes read-only with the reason. Ctrl+S saves. */
import { html, useState, useEffect, useLayoutEffect, useMemo, useRef } from '../util/preact.js';
import { Markdown } from './markdown.js';
import { toast } from './toast.js';
import { copyText } from './copy.js';

const EMPTY_TEXT = 'Pick a file in the tree on the left to read it. Feature files, PROJECT, RULES, TEST-ENVIRONMENT and codebase files can be edited here.';

/** State and actions of one editor; `core.draw` is set by the mounted <MarkdownEditor>. */
function editorCore({ api, onSave }) {
  const st = { path: null, text: '', original: '', version: null, mode: 'read', readOnly: false, readOnlyReason: null, loading: false, saving: false, conflict: null, error: null, isNew: false, seq: 0,
    preview: '', emptyText: EMPTY_TEXT, focusArea: false };
  const core = { st, draw: () => {} };
  const draw = () => core.draw();
  const changed = () => st.text !== st.original;

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

  Object.assign(core, {
    changed, load, overwrite, save,
    setMode(mode, focus) {
      if (mode === 'edit' && st.readOnly) return;
      st.mode = mode; if (mode === 'edit' && focus) st.focusArea = true; draw();
    },
    setText(t) { st.text = t; draw(); },
    discard() { st.text = st.original; st.conflict = null; draw(); },
    onKeyDown(e) { if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 's') { e.preventDefault(); save(); } },
    handle: {
      open: (path, options) => load(path, options),
      path: () => st.path,
      hasChanges: () => changed(),
      showMessage(text) { st.path = null; st.text = ''; st.original = ''; st.emptyText = text; draw(); },
    },
  });
  return core;
}

/** Content of section.ed: bar, notice and body. */
function EditorView({ core }) {
  const { st } = core;
  const area = useRef(null), btnRead = useRef(null), btnEdit = useRef(null), bodyRef = useRef(null), scrolls = useRef({});
  useLayoutEffect(() => { if (st.focusArea && area.current) { st.focusArea = false; area.current.focus(); } });
  // a hidden box may lose its scroll position: each mode gets back the scroll it had when the user left it
  useLayoutEffect(() => {
    const sc = scrolls.current[st.mode]; if (!sc) return;
    if (bodyRef.current) bodyRef.current.scrollTop = sc.body;
    if (st.mode === 'edit' && area.current) area.current.scrollTop = sc.area;
  }, [st.mode]);
  const setMode = (mode, focus) => {
    if (mode !== st.mode) scrolls.current[st.mode] = { body: bodyRef.current ? bodyRef.current.scrollTop : 0, area: area.current ? area.current.scrollTop : 0 };
    core.setMode(mode, focus);
  };
  // the preview follows the text with a small debounce
  useEffect(() => {
    if (st.mode !== 'edit' || st.preview === st.text) return undefined;
    const t = setTimeout(() => { st.preview = st.text; core.draw(); }, 120);
    return () => clearTimeout(t);
  }, [st.mode, st.text]);

  const hasFile = !!st.path, dirty = core.changed();
  const label = st.loading ? 'loading…' : st.saving ? 'saving…' : st.readOnly ? 'read-only' : dirty ? 'changed, not saved' : st.isNew ? 'new file' : st.version ? 'saved' : '';
  const tabsKey = (e) => {
    if (e.key !== 'ArrowRight' && e.key !== 'ArrowLeft') return;
    e.preventDefault();
    setMode(st.mode === 'read' ? 'edit' : 'read');
    (st.mode === 'read' ? btnRead : btnEdit).current.focus();
  };
  const notices = [];
  if (st.error) notices.push(html`<div class="ed-av ed-av-bad"><strong>Could not do it: </strong>${st.error}</div>`);
  if (st.readOnly && st.readOnlyReason) notices.push(html`<div class="ed-av ed-av-ro"><strong>Read-only. </strong>${st.readOnlyReason}</div>`);
  if (st.conflict) {
    notices.push(html`<div class="ed-av ed-av-warn">
      <div><strong>The file changed on disk after you opened it. </strong>Another process (the loop, another tab) wrote a new version.</div>
      <div class="card-row">
        <button type="button" class="btn btn-sm" onClick=${() => core.load(st.path, { force: true })}>Reload (discards my changes)</button>
        <button type="button" class="btn btn-sm btn-danger" onClick=${core.overwrite}>Overwrite with my version</button>
        <button type="button" class="btn btn-sm btn-ghost" onClick=${() => copyText(st.text, 'Your text was copied.')}>Copy my text</button>
      </div>
    </div>`);
  }
  // read view and edit split stay mounted (only hidden), so toggling keeps the textarea caret and both scrolls;
  // both render the debounced preview, which equals the text outside edit mode
  if (st.mode !== 'edit') st.preview = st.text;
  const body = !hasFile ? html`<div class="ed-empty">${st.emptyText}</div>` : html`
    <div class="ed-split" hidden=${st.mode !== 'edit'}>
      <textarea class="ed-area" spellcheck=${false} aria-label="File content (markdown)" ref=${area} value=${st.text} readOnly=${st.readOnly}
        onInput=${(e) => core.setText(e.currentTarget.value)} />
      <div class="md-doc ed-preview" aria-label="Preview"><${Markdown} source=${st.preview} /></div>
    </div>
    <article class="md-doc ed-read" tabindex="0" hidden=${st.mode === 'edit'}>${st.preview.trim() ? html`<${Markdown} source=${st.preview} />` : html`<p class="md-p dim">${st.loading ? 'loading…' : '(empty file)'}</p>`}</article>`;
  return html`
    <div class="ed-bar" hidden=${!hasFile}>
      <span class="ed-path mono">${st.path || ''}</span>
      <span class=${'ed-badge' + (st.readOnly ? ' ro' : dirty ? ' dirty' : '')} aria-live="polite">${label}</span>
      <span class="spacer"></span>
      <div class="seg" role="tablist" aria-label="File mode" onKeyDown=${tabsKey}>
        <button type="button" role="tab" class="seg-btn" ref=${btnRead} aria-selected=${String(st.mode === 'read')} tabIndex=${st.mode === 'read' ? 0 : -1}
          onClick=${() => setMode('read')}>Read</button>
        <button type="button" role="tab" class="seg-btn" ref=${btnEdit} aria-selected=${String(st.mode === 'edit')} tabIndex=${st.mode === 'edit' ? 0 : -1}
          disabled=${st.readOnly || st.loading} title=${st.readOnly ? (st.readOnlyReason || 'read-only') : ''} onClick=${() => setMode('edit', true)}>Edit</button>
      </div>
      <button type="button" class="btn btn-sm btn-ghost" hidden=${st.mode !== 'edit'} disabled=${!dirty || st.saving} onClick=${core.discard}>Discard</button>
      <button type="button" class="btn btn-sm btn-primary" title="Ctrl+S" hidden=${st.mode !== 'edit'} disabled=${!dirty || st.saving || st.readOnly} onClick=${core.save}>Save</button>
      <button type="button" class="btn btn-sm btn-ghost" title="reads the file again from disk" onClick=${() => { if (st.path) core.load(st.path, { force: true }); }}>Reload</button>
      <button type="button" class="btn btn-sm btn-ghost" title="copies the file path" onClick=${() => { if (st.path) copyText(st.path, 'Path copied.'); }}>Copy path</button>
    </div>
    <div class="ed-notice" role="alert" hidden=${!notices.length}>${notices}</div>
    <div class="ed-body" ref=${bodyRef}>${body}</div>`;
}

/** Editor state that outlives the component (e.g. across tab switches): <MarkdownEditor store=${s} />;
    s.open/path/hasChanges/showMessage. */
export function markdownEditorStore({ api, onSave }) {
  const core = editorCore({ api, onSave });
  return { ...core.handle, core };
}

/** <MarkdownEditor api onSave editorRef store /> -> editorRef.current = { open, path, hasChanges, showMessage }. */
export function MarkdownEditor({ api, onSave, editorRef, store }) {
  const [, tick] = useState(0);
  const core = useMemo(() => (store ? store.core : editorCore({ api, onSave })), []);
  core.draw = () => tick((n) => n + 1);
  if (editorRef) editorRef.current = core.handle;
  return html`<section class="ed" aria-label="File" onKeyDown=${core.onKeyDown}><${EditorView} core=${core} /></section>`;
}
