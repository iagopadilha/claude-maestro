/* Files explorer + Markdown editor (GET specs/tree, PUT specs/file): planning only. The loop screen has no Files tab and no editing: it reads through plan_files_dialog.js. Grouped into Project, Codebase, Features (with status) and Loop. */
import { html, useEffect } from '../../util/preact.js';
import { confirmDialog } from '../../components/dialog.js';
import { FileTree, fileTreeStore, normalizeTree, walkTree } from '../../components/file_tree.js';
import { MarkdownEditor, markdownEditorStore } from '../../components/markdown_editor.js';
import { ctx, ui, lastN } from './context.js';
import { renderAll, requestRender } from '../project_panel.js';
import { reloadProgress } from './progress_trail.js';
import { FileNote, explainFile, OUTSIDE_SPECS } from './file_explanations.js';

const TREE_STALE_MS = 10000;
// tree and editor stores outlive the tab: switching views keeps the open file, the draft and the open folders
const fv = { tree: null, editor: null, loading: false, loadedAt: 0, guide: false, error: null, pending: null, promise: null };

function parts() {
  if (fv.tree) return;
  fv.editor = markdownEditorStore({ api: ctx.api, onSave: () => { loadTree(true); reloadProgress(); } });
  fv.tree = fileTreeStore({ onOpen: (node) => openCarefully(node.path, { editable: node.editable, reason: node.reason }) });
}
function runPending() { if (fv.pending) { const p = fv.pending; fv.pending = null; p(); } }

/** Content of #canvas in the "Files" view (div.files): the .specs tree on the side and the markdown editor.
    Showing it (and a new turn while shown) reads the tree again when it is stale. */
export function Files() {
  parts();
  useEffect(() => { loadTree(false).then(runPending); }, [lastN(), ui.pastN, ui.pinLast]);
  const openPath = fv.editor.path();
  return html`<div class="files">
    <aside class="files-side" aria-label=".specs tree">
      <div class="files-side-h"><span class="small-label">.specs</span><span class="files-side-actions">
        <button type="button" class="btn btn-sm btn-ghost" aria-pressed=${String(fv.guide)} title="one line explaining each file" onClick=${() => { fv.guide = !fv.guide; renderAll(); }}>${fv.guide ? 'Tree' : 'Guide'}</button>
        <button type="button" class="btn btn-sm btn-ghost" title="reads the tree again" onClick=${() => loadTree(true)}>Refresh</button></span></div>
      <div class=${'files-status' + (fv.error ? ' err' : '')} aria-live="polite">${fv.loading ? 'loading…' : fv.error || null}</div>
      ${fv.guide ? html`<${FilesGuide} />` : html`<${FileTree} store=${fv.tree} />`}
    </aside>
    <div class="files-main">${openPath ? html`<${FileNote} path=${openPath} />` : null}<${MarkdownEditor} store=${fv.editor} /></div>
  </div>`;
}

/** "Guide" mode of the side: every file of the tree with one line saying what it is; click opens it. */
function FilesGuide() {
  const rows = [];
  walkTree(fv.tree.groups(), (n, parent, group) => { if (n.kind === 'file') rows.push({ n, group }); });
  return html`<div class="files-guide">
    ${rows.map(({ n }) => { const e = explainFile(n.path);
      return html`<button key=${n.path} type="button" class="files-guide-row" title=${n.path} onClick=${() => openCarefully(n.path, { editable: n.editable, reason: n.reason })}>
        <span class="files-guide-name mono">${n.path}</span><span class="files-guide-what">${e ? e.what : 'File written by the loop or by you.'}</span></button>`; })}
    ${rows.length ? null : html`<div class="empty-note">no file in .specs</div>`}
    <div class="small-label files-guide-out">Outside .specs</div>
    ${OUTSIDE_SPECS.map(([name, what]) => html`<div key=${name} class="files-guide-row static"><span class="files-guide-name mono">${name}</span><span class="files-guide-what">${what}</span></div>`)}
  </div>`;
}

async function openCarefully(path, options) {
  if (fv.editor.hasChanges() && fv.editor.path() !== path) {
    const ok = await confirmDialog({ title: 'Discard changes?', text: `You have unsaved changes in ${fv.editor.path()}. Opening another file discards them.`, yesText: 'Discard and open', danger: true });
    if (!ok) { fv.tree.select(fv.editor.path()); return; }
  }
  fv.tree.select(path);
  fv.editor.open(path, options);
}

export async function loadTree(force) {
  if (fv.loading) return fv.promise;
  parts();
  if (!force && Date.now() - fv.loadedAt < TREE_STALE_MS) return null;
  fv.loading = true; fv.error = null; requestRender(); // may run inside the Files effect
  fv.promise = (async () => {
    try {
      const r = await ctx.api.get('specs/tree', 15000);
      fv.tree.set(normalizeTree(r));
      if (r && r.exists === false) fv.error = 'This project has no .specs yet. Use the trail at the top to initialize the structure.';
      fv.loadedAt = Date.now();
    } catch (e) {
      fv.error = e.status === 404 ? (/project/i.test(e.message) ? e.message : 'There is no .specs in this project yet (or the server does not offer the tree). Use the trail at the top to initialize the structure.') : e.message;
    } finally { fv.loading = false; renderAll(); }
  })();
  return fv.promise;
}

function goToFiles() { ui.view = 'files'; renderAll(); }

/** "view spec": opens the Files tab on the feature spec (spec.md in the feature folder; otherwise the first file). */
export function openFeatureSpec(slug) {
  goToFiles();
  fv.pending = () => {
    let dir = null;
    walkTree(fv.tree.groups(), (n, parent, group) => { if (!dir && n.kind === 'dir' && (n.slug === slug || n.name === slug) && /feature/i.test(group.id + group.title)) dir = n; });
    if (!dir) walkTree(fv.tree.groups(), (n) => { if (!dir && n.kind === 'dir' && (n.slug === slug || n.name === slug)) dir = n; });
    const files = dir ? dir.items.filter((x) => x.kind === 'file') : [];
    const target = files.find((x) => /(^|\/)spec\.md$/i.test(x.path)) || files[0];
    if (target) openCarefully(target.path, { editable: target.editable, reason: target.reason });
    else fv.editor.showMessage(`Feature ${slug} has no spec yet (.specs/features/${slug.replace(/[<>:"/\|?*]/g, '-')}/spec.md). The spec-writer creates that file when the loop reaches it.`);
  };
  loadTree(true).then(runPending);
}

/** Opens (or prepares to create) a project file by name, e.g. TEST-ENVIRONMENT.md. */
export function openProjectFile(name) {
  goToFiles();
  fv.pending = () => {
    let found = null, sibling = null;
    walkTree(fv.tree.groups(), (n) => {
      if (n.kind !== 'file') return;
      if (!found && n.name.toLowerCase() === name.toLowerCase()) found = n;
      if (!sibling && /(^|\/)PROJECT\.md$/i.test(n.path)) sibling = n;
    });
    if (found) { openCarefully(found.path, { editable: found.editable, reason: found.reason }); return; }
    // does not exist yet: use the same folder as PROJECT.md (path format the back end returned)
    const path = sibling ? sibling.path.replace(/PROJECT\.md$/i, name) : 'project/' + name;
    openCarefully(path, { isNew: true });
  };
  loadTree(true).then(runPending);
}
