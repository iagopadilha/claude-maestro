/* Plan & files dialog (PLAN-02..05, BACK-02): the plan and the files of the project, READ-ONLY, in one roomy dialog.
   Left: one merged tree: the plan (stages -> features with a plain title, the slug in small text and the status in plain words -> their
   spec / design / tasks / contract), the project files, the codebase maps (docs/codebase and, in a monorepo, each subproject's map)
   and the loop files worth reading. Right: the selected file as Markdown with its purpose in one plain sentence above it.
   There is no Edit, Refresh or Change with AI here: the plan only changes through Back to planning. The data is read again when the
   dialog opens and whenever the loop state changes. A side chat ("Ask about the plan", scope `plan`) answers questions read-only.
   Reads: GET roadmap, GET specs/tree, GET codebase/tree and GET specs/file?path= (a map file outside .specs is also tried on codebase/file). */
import { html, useEffect } from '../../util/preact.js';
import { list, txt } from '../../util/format.js';
import { openBigDialog } from '../../components/big_dialog.js';
import { Markdown } from '../../components/markdown.js';
import { ctx, status, loop, lastN } from './context.js';
import { renderAll, requestRender } from '../project_panel.js';
import { featureName } from './pipeline_model.js';
import { explainFile } from './file_explanations.js';

const STALE_MS = 8000;
const BADGE_COLOR = '#7fb2ff';
const FOOTER_NOTE = 'To change the plan, use Back to planning (no loop may be running).';
const CHAT = {
  title: 'Ask about the plan', scope: { type: 'plan' }, placeholder: 'Ask about the plan, the specs, the maps or the decisions…',
  greeting: 'I can answer questions about the plan, the specs, the code maps, the AI decisions and the progress of the loop. I only read: I never change anything.',
  suggestions: ['What is each stage for?', 'Which features are not done yet?', 'What did the AI decide on its own?'],
};

/** Plain labels of the files (PLAN-03): what the file is, then its name. */
export const FILE_LABELS = {
  'spec.md': 'Spec — what the feature must do', 'design.md': 'Design — how it is built', 'tasks.md': 'Tasks — the steps',
  'contract.md': 'Contract — what the other side can rely on', 'FLOWS.md': 'Flows — what must keep working',
  'PROJECT.md': 'Project — what is being built', 'ROADMAP.md': 'Roadmap — the plan in stages', 'RULES.md': 'Rules — the limits',
  'STATE.md': 'State — the memory of the loop', 'TEST-ENVIRONMENT.md': 'Test environment — where tests may run',
  'VALIDATE.md': 'AI decisions — what the AI chose alone', 'ANSWERS.md': 'Answers — what you decided',
  'STACK.md': 'Stack — languages and frameworks', 'ARCHITECTURE.md': 'Architecture — layers and data flow', 'CONVENTIONS.md': 'Conventions — naming and style',
  'STRUCTURE.md': 'Structure — where code lives', 'TESTING.md': 'Testing — how tests run', 'INTEGRATIONS.md': 'Integrations — what the code talks to',
  'CONCERNS.md': 'Concerns — debt and risks', 'SYSTEM.md': 'System — how the subprojects fit together',
};
const FEATURE_FILE_ORDER = ['spec.md', 'design.md', 'tasks.md', 'contract.md'];
const PROJECT_FILE_ORDER = ['PROJECT.md', 'ROADMAP.md', 'FLOWS.md', 'RULES.md', 'STATE.md', 'TEST-ENVIRONMENT.md', 'VALIDATE.md', 'ANSWERS.md'];
const PLAIN_STATUS = {
  pending: 'waiting its turn', speccing: 'spec being written', specced: 'spec written', approved: 'ready to build',
  building: 'being built', verified: 'done', blocked: 'needs attention',
};

const pf = { roadmap: null, specs: null, codebase: null, files: {}, loading: false, error: null, loadedAt: 0, loopKey: null, selected: null, open: {}, handle: null };

const baseName = (path) => String(path || '').replace(/\\/g, '/').split('/').pop();
const fileLabel = (name) => FILE_LABELS[name] || FILE_LABELS[Object.keys(FILE_LABELS).find((key) => key.toLowerCase() === String(name).toLowerCase())] || '';
const rank = (order, name) => { const at = order.findIndex((key) => key.toLowerCase() === String(name).toLowerCase()); return at < 0 ? order.length : at; };
const loopKey = () => `${status()}|${lastN()}|${loop().at || ''}`;

// ---------- reading ----------
async function loadAll(force) {
  if (pf.loading || (!force && Date.now() - pf.loadedAt < STALE_MS)) return;
  pf.loading = true; pf.error = null; requestRender();
  try {
    const [roadmap, specs, codebase] = await Promise.all([
      ctx.api.get('roadmap', 15000).catch(() => null),
      ctx.api.get('specs/tree', 15000).catch((e) => { pf.error = e.message; return null; }),
      ctx.api.get('codebase/tree', 15000).catch(() => null),
    ]);
    pf.roadmap = roadmap; pf.specs = specs; pf.codebase = codebase; pf.loadedAt = Date.now();
    if (force) pf.files = {};
  } finally { pf.loading = false; renderAll(); }
}

/** Reads the text of a file node: .specs files on specs/file; a map outside .specs falls back to codebase/file. */
async function readNodeText(node) {
  const routes = node.source === 'map' ? ['specs/file', 'codebase/file'] : ['specs/file'];
  let firstError = null;
  for (const route of routes) {
    try {
      const response = await ctx.api.get(route + '?path=' + encodeURIComponent(node.path), 15000);
      return response.text != null ? String(response.text) : String(response.content || '');
    } catch (e) { firstError = firstError || e; }
  }
  throw firstError;
}

async function loadFile(node) {
  if (!node || pf.files[node.path]) return;
  pf.files[node.path] = { loading: true };
  try { pf.files[node.path] = { text: await readNodeText(node) }; }
  catch (e) { pf.files[node.path] = { error: e.message }; }
  renderAll();
}

// ---------- the merged tree (pure: data in, sections out) ----------
const fileNode = (item, source) => ({ kind: 'file', path: item.path, name: item.name || baseName(item.path), label: fileLabel(item.name || baseName(item.path)), source });

/** Plan section: stages -> features (title, slug, status in plain words, files). `liveStatus(slug)` overrides the ROADMAP status. */
export function planSection(roadmap, specs, liveStatus, titleOf) {
  const filesOf = new Map(list(specs && specs.features).map((feature) => [feature.slug, list(feature.files)]));
  const features = list(roadmap && roadmap.features).map((feature) => ({
    kind: 'feature', slug: feature.slug, stage: feature.stage == null || feature.stage === '' ? null : String(feature.stage),
    title: titleOf(feature.slug), status: PLAIN_STATUS[liveStatus(feature.slug) || feature.status || 'pending'] || txt(feature.status),
    files: list(filesOf.get(feature.slug)).map((item) => fileNode(item, 'specs')).sort((a, b) => rank(FEATURE_FILE_ORDER, a.name) - rank(FEATURE_FILE_ORDER, b.name)),
  }));
  const stages = list(roadmap && roadmap.stages).map((stage) => ({ kind: 'stage', n: String(stage.n), title: stage.title || `Stage ${stage.n}`, features: features.filter((f) => f.stage === String(stage.n)) }));
  const loose = features.filter((feature) => !stages.some((stage) => stage.n === feature.stage));
  if (loose.length) stages.push({ kind: 'stage', n: 'none', title: 'No stage', features: loose });
  return stages;
}

/** Project files (PROJECT, ROADMAP, FLOWS, RULES, STATE, TEST-ENVIRONMENT, then the decisions and answers of the loop folder). */
export function projectSection(specs) {
  const items = [...list(specs && specs.project), ...list(specs && specs.loop).filter((item) => /^(VALIDATE|ANSWERS)\.md$/i.test(item.name || baseName(item.path)))];
  return items.map((item) => fileNode(item, 'specs')).sort((a, b) => rank(PROJECT_FILE_ORDER, a.name) - rank(PROJECT_FILE_ORDER, b.name));
}

/** Code maps: `.specs/codebase` plus the groups of GET codebase/tree (root docs/codebase and one per subproject). */
export function mapSection(specs, codebase) {
  const groups = [];
  const inSpecs = list(specs && specs.codebase);
  if (inSpecs.length) groups.push({ id: 'specs-codebase', label: 'Code map in .specs', files: inSpecs.map((item) => fileNode(item, 'specs')) });
  for (const group of list(codebase && codebase.groups)) {
    const files = list(group.files).map((item) => fileNode(item, 'map'));
    if (files.length) groups.push({ id: 'map:' + (group.folder || group.label), label: group.label || group.folder || 'Code map', files });
  }
  return groups;
}

/** Loop files worth reading (the summary); VALIDATE/ANSWERS are already under the project files. */
export function loopSection(specs) {
  return list(specs && specs.loop).filter((item) => !/^(VALIDATE|ANSWERS)\.md$/i.test(item.name || baseName(item.path))).map((item) => fileNode(item, 'specs'));
}

// ---------- drawing ----------
const isOpen = (key, fallback = true) => (key in pf.open ? pf.open[key] : fallback);
const toggle = (key, fallback = true) => { pf.open[key] = !isOpen(key, fallback); renderAll(); };
const select = (node) => { pf.selected = node; loadFile(node); renderAll(); };

function FileRow({ node }) {
  const selected = pf.selected && pf.selected.path === node.path;
  return html`<button type="button" class=${'pf-file' + (selected ? ' is-sel' : '')} aria-current=${selected ? 'true' : null} title=${node.path} onClick=${() => select(node)}>
    ${node.label ? html`<span class="pf-file-label">${node.label}</span>` : null}<span class="pf-file-name">${node.name}</span></button>`;
}

function Feature({ feature }) {
  const key = 'f:' + feature.slug, open = isOpen(key, false);
  return html`<div class="pf-feature">
    <button type="button" class="pf-feat-h" aria-expanded=${String(open)} onClick=${() => toggle(key, false)}>
      <span class="pf-caret" aria-hidden="true">${open ? '▾' : '▸'}</span><span class="pf-feat-title">${feature.title}</span>
      ${feature.title === feature.slug ? null : html`<span class="pf-slug">${feature.slug}</span>`}<span class="pf-feat-status">${feature.status}</span></button>
    ${open ? html`<div class="pf-files">${feature.files.length ? feature.files.map((node) => html`<${FileRow} key=${node.path} node=${node} />`)
      : html`<div class="pf-empty">No spec file yet: the spec-writer creates it when the loop reaches this feature.</div>`}</div>` : null}
  </div>`;
}

function Stage({ stage, first }) {
  const key = 's:' + stage.n, open = isOpen(key, first);
  const done = stage.features.filter((feature) => feature.status === PLAIN_STATUS.verified).length;
  return html`<div class="pf-stage">
    <button type="button" class="pf-stage-h" aria-expanded=${String(open)} onClick=${() => toggle(key, first)}>
      <span class="pf-caret" aria-hidden="true">${open ? '▾' : '▸'}</span><span>${stage.n === 'none' ? stage.title : `${stage.n} · ${stage.title}`}</span>
      <span class="pf-stage-n">${`${done}/${stage.features.length} done`}</span></button>
    ${open ? html`<div class="pf-feats">${stage.features.map((feature) => html`<${Feature} key=${feature.slug} feature=${feature} />`)}</div>` : null}
  </div>`;
}

function FileGroup({ id, label, files }) {
  const key = 'g:' + id, open = isOpen(key, true);
  return html`<div class="pf-group"><button type="button" class="pf-stage-h" aria-expanded=${String(open)} onClick=${() => toggle(key, true)}>
      <span class="pf-caret" aria-hidden="true">${open ? '▾' : '▸'}</span><span>${label}</span><span class="pf-stage-n">${files.length}</span></button>
    ${open ? html`<div class="pf-files">${files.map((node) => html`<${FileRow} key=${node.path} node=${node} />`)}</div>` : null}</div>`;
}

function Tree() {
  const live = Object.fromEntries(list(ctx.state && ctx.state.features).map((feature) => [feature.id, feature.status]));
  const stages = planSection(pf.roadmap, pf.specs, (slug) => live[slug], featureName);
  const project = projectSection(pf.specs), maps = mapSection(pf.specs, pf.codebase), loopFiles = loopSection(pf.specs);
  const firstOpen = stages.findIndex((stage) => stage.features.some((feature) => feature.status !== PLAIN_STATUS.verified));
  return html`<nav class="pf-side" aria-label="Plan and files">
    <div class=${'pf-status' + (pf.error ? ' is-bad' : '')} aria-live="polite">${pf.loading ? 'reading…' : pf.error || ''}</div>
    <div class="pf-sec">The plan</div>
    ${stages.length ? stages.map((stage, index) => html`<${Stage} key=${stage.n} stage=${stage} first=${index === (firstOpen < 0 ? 0 : firstOpen)} />`)
      : html`<div class="pf-empty">${pf.loading ? 'Reading…' : 'No roadmap yet.'}</div>`}
    ${project.length ? html`<div class="pf-sec">Project files</div><div class="pf-files">${project.map((node) => html`<${FileRow} key=${node.path} node=${node} />`)}</div>` : null}
    ${maps.length ? html`<div class="pf-sec">Code maps</div>${maps.map((group) => html`<${FileGroup} key=${group.id} ...${group} />`)}` : null}
    ${loopFiles.length ? html`<div class="pf-sec">Loop</div><div class="pf-files">${loopFiles.map((node) => html`<${FileRow} key=${node.path} node=${node} />`)}</div>` : null}
  </nav>`;
}

function Reader() {
  const node = pf.selected;
  if (!node) return html`<div class="pf-doc"><div class="pf-empty">Pick a file on the left to read it. Everything here is read-only.</div></div>`;
  const explanation = explainFile(node.path), entry = pf.files[node.path];
  return html`<div class="pf-doc">
    <div class="sv-note" role="note">${explanation ? html`<b>${node.label || node.name}</b> ${explanation.what + ' ' + explanation.purpose}` : html`<b>${node.label || node.name}</b> File kept by the loop or by planning.`}</div>
    <div class="dim-mono">${node.path}</div>
    ${!entry || entry.loading ? html`<div class="empty-note">Reading…</div>` : entry.error ? html`<div class="empty-note" role="alert">${'Could not read this file: ' + entry.error}</div>`
      : entry.text.trim() ? html`<div class="md-doc"><${Markdown} source=${entry.text} /></div>` : html`<div class="empty-note">The file is empty.</div>`}
  </div>`;
}

function PlanFilesBody() {
  const key = loopKey();
  // reads when the dialog opens (loopKey starts null) and again each time the loop state changes
  useEffect(() => { const changed = pf.loopKey !== key; pf.loopKey = key; loadAll(changed); }, [key]);
  return html`<div class="pf"><${Tree} /><div class="pf-main"><${Reader} /><div class="pf-note">${FOOTER_NOTE}</div></div></div>`;
}

/** Opens the Plan & files dialog (one at a time). */
export function openPlanFilesDialog() {
  if (pf.handle) return pf.handle;
  pf.loopKey = null; pf.loadedAt = 0;
  pf.handle = openBigDialog({
    badge: 'PLAN', badgeColor: BADGE_COLOR, title: 'Plan & files', body: () => html`<${PlanFilesBody} />`, chat: CHAT,
    onClose: () => { pf.handle = null; },
  });
  return pf.handle;
}
