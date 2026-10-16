/* "Files" tab: .specs explorer (GET specs/tree) grouped into Project, Codebase, Features (with status)
   and Loop, with a markdown reader and editing (PUT specs/file). */
import { h } from '../../util/dom.js';
import { confirmDialog } from '../../components/dialog.js';
import { createFileTree, normalizeTree, walkTree } from '../../components/file_tree.js';
import { createMarkdownEditor } from '../../components/markdown_editor.js';
import { ctx, ui, sigs } from './context.js';
import { renderCenter } from './center.js';
import { reloadProgress } from './progress_trail.js';

const TREE_STALE_MS = 10000;
const fv = { root: null, tree: null, editor: null, status: null, loading: false, loadedAt: 0, error: null, pending: null, promise: null };

function build() {
  fv.editor = createMarkdownEditor({ api: ctx.api, onSave: () => { loadTree(true); reloadProgress(); } });
  fv.tree = createFileTree({ onOpen: (node) => openCarefully(node.path, { editable: node.editable, reason: node.reason }) });
  fv.status = h('div', { class: 'files-status', 'aria-live': 'polite' });
  const btn = h('button', { type: 'button', class: 'btn btn-sm btn-ghost', title: 'reads the tree again', onclick: () => loadTree(true) }, 'Refresh');
  fv.root = h('div', { class: 'files' },
    h('aside', { class: 'files-side', 'aria-label': '.specs tree' }, h('div', { class: 'files-side-h' }, h('span', { class: 'small-label', text: '.specs' }), btn), fv.status, fv.tree.element),
    fv.editor.element);
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
  if (!force && Date.now() - fv.loadedAt < TREE_STALE_MS) return null;
  fv.loading = true; fv.error = null; refreshStatus();
  fv.promise = (async () => {
    try {
      const r = await ctx.api.get('specs/tree', 15000);
      fv.tree.set(normalizeTree(r));
      if (r && r.exists === false) fv.error = 'This project has no .specs yet. Use the trail at the top to initialize the structure.';
      fv.loadedAt = Date.now();
    } catch (e) {
      fv.error = e.status === 404 ? (/project/i.test(e.message) ? e.message : 'There is no .specs in this project yet (or the server does not offer the tree). Use the trail at the top to initialize the structure.') : e.message;
    } finally { fv.loading = false; refreshStatus(); }
  })();
  return fv.promise;
}
function refreshStatus() {
  if (!fv.status) return;
  fv.status.textContent = fv.loading ? 'loading…' : fv.error || '';
  fv.status.className = 'files-status' + (fv.error ? ' err' : '');
}

/** Element of the tab (the center shows it full size). Loads the tree when it is stale. */
export function filesView() {
  if (!fv.root) build();
  loadTree(false).then(() => {
    if (fv.pending) { const p = fv.pending; fv.pending = null; p(); }
  });
  return fv.root;
}

function goToFiles() {
  if (!fv.root) build();
  ui.view = 'files'; sigs.center = null; renderCenter();
}

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
    else fv.editor.showMessage(`Feature ${slug} has no spec yet (.specs/features/${slug}/spec.md). The spec-writer creates that file when the loop reaches it.`);
  };
  loadTree(true).then(() => { if (fv.pending) { const p = fv.pending; fv.pending = null; p(); } });
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
  loadTree(true).then(() => { if (fv.pending) { const p = fv.pending; fv.pending = null; p(); } });
}
