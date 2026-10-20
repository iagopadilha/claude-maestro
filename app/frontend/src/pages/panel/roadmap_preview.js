/* Roadmap preview (planning screen, ROADMAP tab): the SAME FeatureGraph component of the loop (components/graph.js), fed by
   GET roadmap: pan, zoom, fit, keyboard and node selection. Clicking a node shows a details card under the graph. Read-only.
   Its pan/zoom view lives here (module level), apart from the loop panel's. */
import { html, useState, useEffect } from '../../util/preact.js';
import { computeGraphLayout, FeatureGraph, newGraphView } from '../../components/graph.js';
import { Markdown } from '../../components/markdown.js';
import { list, txt, fmtRelative } from '../../util/format.js';
import { ctx, ui, pal, features, lastN } from './context.js';
import { renderAll } from '../project_panel.js';
import { RoadmapChat } from './roadmap_chat.js';
import { parseValidate } from './roadmap_sidebar.js';
import { openFeatureSpec, openProjectFile } from './files.js';
import { FileNote, statusMeaning, KIND_TEXT } from './file_explanations.js';

const NW = 184, NH = 66; // node size of computeGraphLayout
const stageNumber = (s) => { const n = Number(s); return s !== '' && s != null && Number.isFinite(n) ? n : null; };

/** Roadmap endpoint payload -> the shape computeGraphLayout reads (id, status, stage as number, deps as slugs). */
export function previewModel(data) {
  const fs = list(data && data.features).map((f) => ({
    id: f.slug, status: f.status || 'pending', stage: stageNumber(f.stage), kind: f.kind,
    deps: String(f.deps || '').split(',').map((d) => d.trim()).filter((d) => d && !d.startsWith('dir:')),
    regression: !!f.regression && f.regression !== 'no',
  }));
  const stages = list(data && data.stages).map((s) => ({ n: stageNumber(s.n), title: s.title }));
  if (!fs.length) return null;
  const L = computeGraphLayout(fs, stages);
  const pos = Object.fromEntries(L.nodes.map((n) => [n.f.id, n]));
  const edges = L.edges.map((a) => {
    const p1 = pos[a.from], p2 = pos[a.to];
    const x1 = p1.x + NW, y1 = p1.y + NH / 2, x2 = p2.x - 4, y2 = p2.y + NH / 2, dx = Math.max(40, Math.abs(x2 - x1) / 2);
    return { ...a, d: `M${x1} ${y1} C${x1 + dx} ${y1},${x2 - dx} ${y2},${x2} ${y2}` };
  });
  return { L, edges };
}

// kept across re-renders and tab switches of the planning screen: view = pan/zoom, ctl = reached by the zoom buttons
const rmView = newGraphView(), rmCtl = {};

/** Roadmap payload -> the features FeatureGraph reads (id, status, stage number, kind, deps = slugs) + the raw fields for the card. */
function graphFeatures(data) {
  return list(data.features).map((f) => ({
    id: f.slug, status: f.status || 'pending', stage: stageNumber(f.stage), kind: f.kind,
    deps: String(f.deps || '').split(',').map((d) => d.trim()).filter(Boolean), raw: f,
  }));
}
const csv = (v) => String(v || '').split(',').map((x) => x.trim()).filter(Boolean);
/** `paths=<repo>:<path>` prefixes (a Windows drive letter is not a repo prefix). */
function reposOfPaths(paths) {
  const out = [];
  for (const p of csv(paths)) { const m = /^([\w.-]{2,}):(?![\\/])/.exec(p); if (m && !out.includes(m[1])) out.push(m[1]); }
  return out;
}

function Row({ k, children }) {
  return html`<div class="plan-rm-row"><span class="plan-rm-k">${k}</span><span class="plan-rm-v">${children}</span></div>`;
}

function FeatureCard({ f, onClose }) {
  const r = f.raw, paths = csv(r.paths), deps = csv(r.deps), repos = reposOfPaths(r.paths), order = csv(r['repo-order']);
  const reg = !!r.regression && r.regression !== 'no';
  return html`<div class="plan-rm-card" role="region" aria-label=${'Details of ' + f.id}>
    <div class="plan-rm-card-top"><b class="mono">${f.id}</b>
      <button type="button" class="btn btn-sm btn-ghost" onClick=${onClose} aria-label="close the details">Close</button></div>
    <${Row} k="stage">${r.stage == null || r.stage === '' ? 'none' : r.stage}<//>
    <${Row} k="kind">${r.kind || '—'}<//>
    <${Row} k="paths">${paths.length ? html`<span class="mono">${paths.join('  ')}</span>` : '—'}<//>
    <${Row} k="depends on">${deps.length ? html`<span class="mono">${deps.join(', ')}</span>` : 'nothing'}<//>
    ${repos.length ? html`<${Row} k="repos"><span class="mono">${repos.join(', ')}</span><//>` : null}
    ${order.length ? html`<${Row} k="repo-order"><span class="mono">${order.join(' → ')}</span><//>` : null}
    ${r.isolation ? html`<${Row} k="isolation">${r.isolation}<//>` : null}
    <${Row} k="regression">${reg ? 'proof requested (regression=' + r.regression + ')' : 'no'}<//>
  </div>`;
}

function RoadmapGraph({ data }) {
  const [sel, setSel] = useState(null);
  const fs = graphFeatures(data);
  const cur = fs.find((f) => f.id === sel) || null;
  const stages = list(data.stages).map((s) => ({ n: stageNumber(s.n), title: s.title }));
  return html`<div class="plan-rm-graph">
    <div class="plan-rm-bar" role="group" aria-label="Roadmap graph zoom">
      <button type="button" class="btn btn-sm btn-ghost" title="zoom out (Ctrl + scroll)" aria-label="zoom out" onClick=${() => rmCtl.zoom && rmCtl.zoom(1 / 1.2)}>−</button>
      <button type="button" class="btn btn-sm btn-ghost" title="zoom in (Ctrl + scroll)" aria-label="zoom in" onClick=${() => rmCtl.zoom && rmCtl.zoom(1.2)}>+</button>
      <button type="button" class="btn btn-sm btn-ghost" title="fits and centers the graph" onClick=${() => rmCtl.fit && rmCtl.fit()}>Center</button>
    </div>
    <div class="dep-graph plan-rm-box" style="flex:none;height:420px">
      <${FeatureGraph} features=${fs} stages=${stages} selected=${cur ? cur.id : null} onSelect=${(id) => setSel((c) => (c === id ? null : id))} view=${rmView} ctl=${rmCtl} />
    </div>
    ${cur ? html`<${FeatureCard} f=${cur} onClose=${() => setSel(null)} />` : null}
  </div>`;
}

/** The graph, or the parse errors instead of it, or nothing (empty roadmap). `rm` = { data, error }. */
export function RoadmapPreview({ rm }) {
  const data = rm && rm.data;
  if (!data) return null;
  const errors = list(data.errors);
  if (errors.length) {
    return html`<div class="plan-rm-err" role="alert"><b>The ROADMAP has errors, so there is no diagram yet:</b>
      <ul>${errors.map((e, i) => html`<li key=${i}>${String(e)}</li>`)}</ul></div>`;
  }
  if (!list(data.features).length) return null;
  return html`<${RoadmapGraph} data=${data} />`;
}

// ---------------------------------------------------------------------------------------------------------------------
// "Roadmap & specs" tab: READ and VALIDATE the plan (roadmap by stage, spec of each feature, VALIDATE/ANSWERS) with a short
// plain-language explanation of every file, plus the "change with the AI" chat. Reads GET roadmap, specs/tree and specs/file.
// ---------------------------------------------------------------------------------------------------------------------
const PLAN_SCOPE = '_plan';
const STALE_MS = 10000;
const sv = { rm: null, tree: null, files: {}, loading: false, error: null, loadedAt: 0, scope: null, fileOf: {}, mode: 'read' };

/** Shortcut used by the always-visible "Roadmap" title: opens this tab (works with the loop running). */
export function openRoadmapView() { ui.view = 'roadmap'; sv.mode = 'read'; renderAll(); }

async function loadOverview(force) {
  if (sv.loading || (!force && Date.now() - sv.loadedAt < STALE_MS)) return;
  sv.loading = true; sv.error = null; renderAll();
  try {
    const [rm, tree] = await Promise.all([ctx.api.get('roadmap', 15000).catch(() => null), ctx.api.get('specs/tree', 15000).catch((e) => { sv.error = e.message; return null; })]);
    sv.rm = rm; sv.tree = tree; sv.loadedAt = Date.now();
    if (force) sv.files = {};
  } finally { sv.loading = false; renderAll(); }
}

async function loadFile(path) {
  if (!path || sv.files[path]) return;
  sv.files[path] = { loading: true };
  try { const r = await ctx.api.get('specs/file?path=' + encodeURIComponent(path), 10000); sv.files[path] = { text: String(r.text == null ? '' : r.text) }; }
  catch (e) { sv.files[path] = { error: e.message }; }
  renderAll();
}

const treeFiles = (slug) => { const f = sv.tree && list(sv.tree.features).find((x) => x.slug === slug); return f ? list(f.files) : []; };
const projectFile = (name) => {
  const t = sv.tree || {};
  return [...list(t.project), ...list(t.loop), ...list(t.codebase), ...list(t.other)].find((x) => String(x.name).toLowerCase() === name.toLowerCase()) || null;
};
const newest = (files) => files.map((f) => f.modified_at).filter(Boolean).sort().pop() || null;
const statusChip = (status) => { const [c, bg] = pal(status); return html`<span class="chip" style=${{ color: c || null, background: bg || null }}>${txt(status)}</span>`; };

function Doc({ path, file }) {
  const f = sv.files[path];
  useEffect(() => { loadFile(path); }, [path]);
  return html`<div class="sv-doc"><${FileNote} path=${path} file=${file} />
    ${!f || f.loading ? html`<div class="empty-note">Reading…</div>` : f.error ? html`<div class="empty-note">${f.error}</div>`
      : f.text.trim() ? html`<div class="md-doc"><${Markdown} source=${f.text} /></div>` : html`<div class="empty-note">The file is empty.</div>`}</div>`;
}

/** One VALIDATE.md as cards (doubt / what the AI chose / how to revert), optionally only the lines of one feature. */
function ValidateList({ path, slug }) {
  const f = sv.files[path];
  useEffect(() => { loadFile(path); }, [path]);
  if (!f || f.loading) return html`<div class="empty-note">Reading…</div>`;
  if (f.error) return html`<div class="empty-note">${f.error}</div>`;
  const items = f.text.split('\n').filter((l) => /^\s*-\s+/.test(l)).map(parseValidate).filter((v) => !slug || v.slug === slug);
  if (!items.length) return html`<div class="empty-note">${slug ? 'Nothing to validate for this feature.' : 'Nothing to validate yet.'}</div>`;
  return html`<div class="sv-val">${items.map((v, i) => html`<div key=${i} class="sv-val-item">
    ${!slug && v.slug ? html`<span class="chip mono">${v.slug}</span>` : null}<div class="sv-val-doubt">${v.text}</div>
    ${v.chose ? html`<div><b>The AI chose</b> ${v.chose}</div>` : null}${v.revert ? html`<div><b>To revert</b> ${v.revert}</div>` : null}</div>`)}</div>`;
}

function Tabs({ items, value, onPick, label }) {
  return html`<div class="sv-tabs" role="tablist" aria-label=${label}>${items.map(([k, text]) => html`<button key=${k} type="button" role="tab" class="sv-tab"
    aria-selected=${String(value === k)} onClick=${() => onPick(k)}>${text}</button>`)}</div>`;
}

function AnswersFor({ path, slug }) {
  const f = sv.files[path];
  useEffect(() => { loadFile(path); }, [path]);
  if (!f || f.loading) return html`<div class="empty-note">Reading…</div>`;
  const lines = f.error ? [] : f.text.split('\n').filter((l) => l.includes('[' + slug + ']'));
  return lines.length ? html`<div class="md-doc"><${Markdown} source=${lines.join('\n')} /></div>` : html`<div class="empty-note">No answer recorded for this feature.</div>`;
}

function FeatureDetail({ slug }) {
  const rmFeature = list(sv.rm && sv.rm.features).find((f) => f.slug === slug), live = features().find((f) => f.id === slug);
  if (!rmFeature) return html`<div class="empty-note">Feature ${slug} is not in the roadmap.</div>`;
  const files = treeFiles(slug), status = rmFeature.status || 'pending';
  const stage = list(sv.rm.stages).find((s) => String(s.n) === String(rmFeature.stage));
  const deps = csv(rmFeature.deps), byStatus = Object.fromEntries(list(sv.rm.features).map((f) => [f.slug, f.status]));
  const goal = live && live.summary && live.summary.goal;
  const pick = sv.fileOf[slug] && files.some((x) => x.path === sv.fileOf[slug]) ? sv.fileOf[slug] : (files.find((x) => /spec\.md$/i.test(x.path)) || files[0] || {}).path;
  const valid = projectFile('VALIDATE.md'), answers = projectFile('ANSWERS.md'), last = newest(files);
  return html`<div class="sv-detail">
    <div class="sv-head"><b class="mono">${slug}</b>${statusChip(status)}<span class="dim-mono">${stage ? `stage ${stage.n} · ${stage.title}` : 'no stage'}</span><span class="spacer"></span>
      <button type="button" class="btn btn-sm btn-ghost" title="opens the spec in the Files tab (to edit)" onClick=${() => openFeatureSpec(slug)}>Edit in Files</button></div>
    <div class="sv-sum"><div class="small-label">In plain words</div>
      <p>${goal ? goal : files.length ? 'Open spec.md below: its first section states the goal.' : 'There is no spec yet: the spec-writer creates it when the loop reaches this feature.'}</p>
      <p><b>Where it stands</b> ${statusMeaning(status)}</p>
      <p><b>Waits for</b> ${deps.length ? deps.map((d) => html`<span key=${d} class="chip mono">${d.startsWith('dir:') ? d : d + ' · ' + (byStatus[d] || '?')}</span>`) : 'nothing'}</p>
      <p><b>Kind and size</b> ${KIND_TEXT[rmFeature.kind] || txt(rmFeature.kind)} · ${txt(rmFeature.scope)}${rmFeature.commit ? html` · commit <span class="mono">${rmFeature.commit}</span>` : null}</p>
      <p><b>Touches</b> ${csv(rmFeature.paths).map((p) => html`<code key=${p}>${p}</code>`)}</p>
      <p><b>What changed</b> ${last ? `its files were last written ${fmtRelative(last)}` : 'no spec file written yet'}${rmFeature.note ? ` · loop note: ${rmFeature.note}` : ''}</p></div>
    ${files.length ? html`<${Tabs} label="Files of the feature" items=${files.map((x) => [x.path, x.name])} value=${pick} onPick=${(p) => { sv.fileOf[slug] = p; renderAll(); }} />
      <${Doc} key=${pick} path=${pick} file=${files.find((x) => x.path === pick)} />` : null}
    <div class="sv-sec"><div class="small-label">To validate (VALIDATE.md)</div>${valid ? html`<${ValidateList} path=${valid.path} slug=${slug} />` : html`<div class="empty-note">No VALIDATE.md yet.</div>`}</div>
    ${answers ? html`<div class="sv-sec"><div class="small-label">Your answers (ANSWERS.md)</div><${AnswersFor} path=${answers.path} slug=${slug} /></div>` : null}
  </div>`;
}

const PLAN_TABS = [['roadmap', 'Roadmap', 'ROADMAP.md'], ['project', 'Project', 'PROJECT.md'], ['validate', 'Validate', 'VALIDATE.md'], ['answers', 'Answers', 'ANSWERS.md'],
  ['state', 'State', 'STATE.md'], ['rules', 'Rules', 'RULES.md'], ['flows', 'Flows', 'FLOWS.md']];

function PlanDetail() {
  const rm = sv.rm, fs = list(rm && rm.features), counts = {};
  for (const f of fs) counts[f.status || 'pending'] = (counts[f.status || 'pending'] || 0) + 1;
  const tabs = PLAN_TABS.filter(([, , name]) => name === 'ROADMAP.md' || projectFile(name));
  const cur = sv.fileOf[PLAN_SCOPE] && tabs.some(([k]) => k === sv.fileOf[PLAN_SCOPE]) ? sv.fileOf[PLAN_SCOPE] : 'roadmap';
  const name = tabs.find(([k]) => k === cur)[2], file = projectFile(name);
  const goal = /^Goal:\s*(.+)$/m.exec((rm && rm.text) || '');
  return html`<div class="sv-detail">
    <div class="sv-head"><b>Whole plan</b><span class="dim-mono">${fs.length} features · ${list(rm && rm.stages).length} stages</span>
      ${rm && rm.approved === false ? html`<span class="chip" style=${{ color: 'var(--amber)' }}>not approved</span>` : null}<span class="spacer"></span>
      <button type="button" class="btn btn-sm btn-ghost" title="opens the file in the Files tab (to edit)" onClick=${() => openProjectFile(name)}>Edit in Files</button></div>
    <div class="sv-sum"><div class="small-label">In plain words</div>
      <p>${goal ? goal[1] : 'The plan is split in stages; each stage groups features that are specified, built and verified together.'}</p>
      <p>${Object.entries(counts).map(([k, n]) => html`<span key=${k} class="sv-count">${statusChip(k)} ${n}</span>`)}</p></div>
    <${Tabs} label="Plan files" items=${tabs.map(([k, t]) => [k, t])} value=${cur} onPick=${(k) => { sv.fileOf[PLAN_SCOPE] = k; renderAll(); }} />
    ${cur === 'roadmap' ? html`<div class="sv-doc"><${FileNote} path="project/ROADMAP.md" file=${file} />${rm && rm.text ? html`<div class="md-doc"><${Markdown} source=${rm.text} /></div>` : html`<div class="empty-note">No ROADMAP yet.</div>`}</div>`
      : cur === 'validate' ? html`<div class="sv-doc"><${FileNote} path=${file.path} file=${file} /><${ValidateList} path=${file.path} /></div>`
      : html`<${Doc} key=${file.path} path=${file.path} file=${file} />`}
  </div>`;
}

function SideList({ scope }) {
  const stages = list(sv.rm && sv.rm.stages), fs = list(sv.rm && sv.rm.features), go = (s) => { sv.scope = s; renderAll(); };
  return html`<aside class="sv-side" aria-label="Plan and features">
    <div class="files-side-h"><span class="small-label">Plan</span><button type="button" class="btn btn-sm btn-ghost" title="reads roadmap and specs again" onClick=${() => loadOverview(true)}>Refresh</button></div>
    <button type="button" class=${'frow rc-plan' + (scope === PLAN_SCOPE ? ' sel' : '')} aria-pressed=${String(scope === PLAN_SCOPE)} onClick=${() => go(PLAN_SCOPE)}><span class="nm">Whole plan</span><span class="chip rc-chip-plain">files</span></button>
    ${stages.map((s) => html`<div key=${String(s.n)} class="rc-stage"><div class="small-label" title=${s.title}>${`Stage ${s.n} · ${s.title}`}</div>
      <div class="frows">${fs.filter((f) => String(f.stage) === String(s.n)).map((f) => html`<button key=${f.slug} type="button" class=${'frow' + (scope === f.slug ? ' sel' : '')}
        aria-pressed=${String(scope === f.slug)} title=${f.slug} onClick=${() => go(f.slug)}><span class="nm">${f.slug}</span>${statusChip(f.status || 'pending')}</button>`)}</div></div>`)}
    ${!stages.length ? html`<div class="empty-note">${sv.loading ? 'Reading…' : 'No roadmap yet.'}</div>` : null}
  </aside>`;
}

/** Content of #canvas in the "Roadmap & specs" view: read and validate (default) or change a spec with the AI. */
export function RoadmapSpecsView() {
  useEffect(() => { loadOverview(false); }, [lastN(), ui.pastN, ui.pinLast]);
  const scope = sv.scope || PLAN_SCOPE;
  const running = ctx.state && ctx.state.loop && ctx.state.loop.status === 'running';
  return html`<div class="sv">
    <div class="sv-bar"><${Tabs} label="Roadmap view" items=${[['read', 'Read and validate'], ['chat', 'Change with the AI']]} value=${sv.mode} onPick=${(m) => { sv.mode = m; renderAll(); }} />
      <span class="dim-mono sv-hint">${running ? 'loop running: reading is always allowed' : ''}</span></div>
    <div class="sv-body">${sv.mode === 'chat' ? html`<${RoadmapChat} />`
      : html`<${SideList} scope=${scope} /><div class="sv-main">${sv.error ? html`<div class="empty-note">${sv.error}</div>` : null}
        ${sv.rm ? (scope === PLAN_SCOPE ? html`<${PlanDetail} />` : html`<${FeatureDetail} key=${scope} slug=${scope} />`) : html`<div class="empty-note">${sv.loading ? 'Reading…' : 'No roadmap yet.'}</div>`}</div>`}</div>
  </div>`;
}
