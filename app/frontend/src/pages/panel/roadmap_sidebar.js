/* Left column: roadmap by stage, quarantine, VALIDATE.md and ANSWERS.md. Also the selection. */
import { html, useState, useEffect } from '../../util/preact.js';
import { openDialog } from '../../components/dialog.js';
import { Markdown } from '../../components/markdown.js';
import { normalizeTree, walkTree } from '../../components/file_tree.js';
import { isNum, num, txt, list } from '../../util/format.js';
import { toast } from '../../components/toast.js';
import { ctx, ui, pal, status, live, features, focusFeatureId } from './context.js';
import { renderAll } from '../project_panel.js';
import { fetchAgentLog } from './inspector.js';
import { openRoadmapView } from './roadmap_preview.js';

/** One VALIDATE.md line: `- [<slug|general>] <doubt> | chose: <what was done> | revert: <how>`. */
export function parseValidate(raw) {
  const t = String(raw).replace(/^\s*-\s*/, '');
  const m = t.match(/^\[([^\]]+)\]\s*(.*)$/);
  const slug = m ? m[1] : null, rest = m ? m[2] : t;
  const parts = rest.split(' | ').map((x) => x.trim());
  const out = { slug, text: parts[0] || '', chose: null, revert: null };
  for (const p of parts.slice(1)) {
    if (/^chose:/i.test(p)) out.chose = p.replace(/^chose:\s*/i, '');
    else if (/^revert:/i.test(p)) out.revert = p.replace(/^revert:\s*/i, '');
    else out.text += ' · ' + p;
  }
  return out;
}

export function groupStages() {
  const fs = features(), titles = {};
  for (const e of list(ctx.state && ctx.state.stages)) titles[e.n] = e.title;
  const groups = new Map();
  for (const f of fs) { const n = isNum(f.stage) ? f.stage : null; if (!groups.has(n)) groups.set(n, []); groups.get(n).push(f); }
  return [...groups.entries()].sort((a, b) => (a[0] == null) - (b[0] == null) || a[0] - b[0])
    .map(([n, items]) => ({ n, title: n == null ? 'No stage' : titles[n] || `Stage ${String(n).padStart(2, '0')}`, items }));
}

export function selectStage(n) { ui.sel = { kind: 'stage', n }; ui.tab = 'info'; ui.chatGeneral = false; renderAll(); }
export function selectFeature(id) { ui.sel = { kind: 'feature', id }; ui.tab = 'info'; ui.chatGeneral = false; renderAll(); }
export function selectAgent(id) { ui.sel = { kind: 'agent', id }; ui.tab = 'chat'; ui.chatGeneral = false; ui.agentLog = null; ui.logLoading = id; renderAll(); fetchAgentLog(id); }

async function sendQuarantine(q, opt) {
  const s = status(), text = `Quarantine ${q.id}: ${opt}`;
  const k = q.id + '|' + opt;
  if (ui.busy[k]) return;
  ui.busy[k] = true; renderAll();
  try {
    if (s === 'awaiting_user') await ctx.api.post('answer', { text });
    else await ctx.api.post('note', { text });
    ui.qSent[q.id] = opt; toast('Sent: ' + text, 'ok');
  } catch (e) { toast('Not sent: ' + e.message, 'err'); }
  finally { ui.busy[k] = false; renderAll(); }
}

/** Short reader-facing status of a feature: [label, legend]. */
const FEATURE_STATUS = {
  pending: ['pending', 'waiting its turn'], speccing: ['spec', 'spec being written'], specced: ['spec ok', 'spec written, awaiting approval'],
  approved: ['ready', 'spec approved, ready to run'], building: ['running', 'being implemented'], verified: ['done', 'implemented and verified'],
  blocked: ['failed', 'blocked or failed, needs attention'],
};
/** Status of a feature for the sidebar: a building feature with a running verifier agent shows as "verifying". */
export function featureStatusInfo(feature) {
  const verifying = feature.status === 'building' && list(ctx.state && ctx.state.agents).some((a) => a.feature === feature.id && /verifier/.test(a.role || '') && a.state === 'running');
  if (verifying) return { label: 'verifying', legend: 'being verified (tests, build)', key: 'queued' };
  const [label, legend] = FEATURE_STATUS[feature.status] || [txt(feature.status), ''];
  return { label, legend, key: feature.status };
}

/** "2 done · 1 running · 3 pending" for the header of a stage. */
function stageCounts(items) {
  const tally = new Map();
  for (const f of items) { const label = featureStatusInfo(f).label; tally.set(label, (tally.get(label) || 0) + 1); }
  return [...tally.entries()].map(([label, n]) => `${n} ${label}`).join(' · ');
}

/** spec.md of a feature read through the existing specs/tree and specs/file endpoints; null = no spec yet. */
async function loadFeatureSpec(slug) {
  const tree = normalizeTree(await ctx.api.get('specs/tree', 15000));
  let dir = null;
  walkTree(tree, (node, parent, group) => { if (!dir && node.kind === 'dir' && (node.slug === slug || node.name === slug) && /feature/i.test(group.id + group.title)) dir = node; });
  if (!dir) walkTree(tree, (node) => { if (!dir && node.kind === 'dir' && (node.slug === slug || node.name === slug)) dir = node; });
  const files = dir ? dir.items.filter((x) => x.kind === 'file') : [];
  const specFile = files.find((x) => /(^|\/)spec\.md$/i.test(x.path));
  if (!specFile) return null;
  const response = await ctx.api.get('specs/file?path=' + encodeURIComponent(specFile.path), 15000);
  return { path: specFile.path, text: response.text != null ? String(response.text) : String(response.content || '') };
}

function FeatureDetail({ feature }) {
  const [spec, setSpec] = useState({ loading: true, data: null, error: null });
  useEffect(() => {
    let alive = true;
    loadFeatureSpec(feature.id).then((data) => alive && setSpec({ loading: false, data, error: null }))
      .catch((e) => alive && setSpec({ loading: false, data: null, error: e.message }));
    return () => { alive = false; };
  }, [feature.id]);
  const info = featureStatusInfo(feature), [col, bg] = pal(info.key);
  const summary = feature.summary || {};
  const deps = list(feature.depends_on).length ? list(feature.depends_on).map((d) => `${d.id} (${d.status})`) : list(feature.deps).filter((d) => !String(d).startsWith('dir:'));
  const paths = list(feature.paths);
  const row = (k, v) => html`<div class="fd-row"><span class="fd-k">${k}</span><span class="fd-v">${v}</span></div>`;
  return html`<div class="fd">
    <div class="fd-meta">
      ${row('status', html`<span class="chip" style=${{ color: col, background: bg }}>${info.label}</span> <span class="dim-mono">${info.legend}</span>`)}
      ${row('stage', feature.stage == null ? 'none' : String(feature.stage))}
      ${row('depends on', deps.length ? html`<span class="mono">${deps.join(', ')}</span>` : 'nothing')}
      ${row('paths', paths.length ? html`<span class="mono">${paths.join('  ')}</span>` : '—')}
      ${summary.size ? row('size', summary.size) : null}
    </div>
    ${spec.loading ? html`<div class="empty-note">Reading the spec…</div>`
      : spec.error ? html`<div class="empty-note" role="alert">${'Could not read the spec: ' + spec.error}</div>`
      : spec.data ? html`<div class="fd-src dim-mono">${spec.data.path}</div><div class="md-doc"><${Markdown} source=${spec.data.text} /></div>`
      : html`<div class="fd-nospec"><b>Spec not written yet.</b>
          ${summary.goal ? html`<div class="fd-roadline"><span class="dim-mono">ROADMAP</span> ${txt(summary.goal)}</div>` : null}</div>`}
  </div>`;
}

/** Definition of a feature (what was or will be done) in a dialog. */
export function openFeatureDetail(feature) {
  openDialog({ title: txt(feature.name || feature.id), content: html`<${FeatureDetail} feature=${feature} />`, width: 760, cls: 'fd-dlg',
    actions: [{ text: 'Close', cls: 'btn-ghost' }] });
}

function Roadmap({ fs, groups, c }) {
  const focusF = focusFeatureId();
  const verified = ui.past ? fs.filter((f) => f.status === 'verified').length : c.verified;
  const total = ui.past ? fs.length : c.total;
  const stageSel = (g) => !!(ui.sel && ui.sel.kind === 'stage' && ui.sel.n === g.n);
  return html`<div>
    <div class="sec-head"><h2 class="sec-title"><button type="button" class="sec-title-link" title="opens the Roadmap & specs tab: read the plan and the specs" onClick=${openRoadmapView}>Roadmap</button></h2><div class="dim-mono">${`${num(verified)}/${num(total)} verified`}</div></div>
    ${groups.length ? html`<div class="stages">${groups.map((g) => html`<div key=${String(g.n)}>
      ${g.n == null ? html`<div class="small-label">${g.title}</div>`
        : html`<button type="button" class=${'stage-h rm-stage' + (stageSel(g) ? ' sel' : '')} aria-expanded=${String(stageSel(g))} title="opens the summary of this stage in the inspector"
            onClick=${() => selectStage(g.n)}><span class="rm-stage-t"><span class="rm-stage-name">${g.title}</span>
              <span class="rm-stage-n">${stageCounts(g.items)}</span></span><span class="stage-chev" aria-hidden="true">›</span></button>`}
      <div class="frows">${g.items.map((f) => {
        const info = featureStatusInfo(f), [col, bg] = pal(info.key);
        return html`<button key=${f.id} type="button" class=${'frow' + (focusF === f.id ? ' sel' : '')} title=${`${f.id}: ${info.legend} (click for the definition)`} aria-pressed=${focusF === f.id ? 'true' : 'false'}
            onClick=${() => { selectFeature(f.id); openFeatureDetail(f); }}>
          <span class="nm">${txt(f.name || f.id)}</span><span class="chip" style=${{ color: col || null, background: bg || null }}>${info.label}</span></button>`;
      })}</div>
    </div>`)}</div>`
      : html`<div class="empty-note">No feature in the roadmap. Use the trail at the top to generate the roadmap.</div>`}
  </div>`;
}

function QuarantineCard({ qq, canQ }) {
  const sent = ui.qSent[qq.id];
  return html`<div class="qcard">
    <div class="qtitle"><span class="dot dot-red"></span><span>${`${qq.id} in quarantine`}</span></div>
    <div class="qtext">${txt(qq.reason)}</div>
    ${list(qq.stuck).length ? html`<div class="qtext">Holds back: <span class="mono">${list(qq.stuck).join(', ')}</span></div>` : html`<div class="qtext">Nothing depends on it, the loop carries on.</div>`}
    <div class="qopts">${list(qq.options).map((o, i) => html`<button key=${i} type="button" class="qopt" disabled=${!canQ || !!ui.busy[qq.id + '|' + o]}
      title=${canQ ? '' : 'only with the loop running or waiting for an answer'} onClick=${() => sendQuarantine(qq, o)}><span class="n">${String(i + 1)}</span><span>${String(o)}</span></button>`)}</div>
    ${sent ? html`<div class="qsent">${`→ sent: ${sent} · the next turn applies it`}</div>` : null}
  </div>`;
}

function Validate({ vals }) {
  return html`<div>
    <div class="sec-title" style="margin-bottom:8px">For you to validate <span class="dim-mono">VALIDATE.md</span></div>
    ${vals.length ? html`<div class="vlist">${vals.map((v) => {
      const p = parseValidate(v);
      return html`<div class="vitem">
        <div>${p.slug ? html`<span class="dim-mono">${p.slug + ' '}</span>` : null}${p.text}</div>
        ${p.chose ? html`<div class="vrev">${'chose: ' + p.chose}</div>` : null}
        ${p.revert ? html`<div class="vrev">${'revert: ' + p.revert}</div>` : null}
      </div>`;
    })}</div>` : html`<div class="empty-note">Nothing to validate for now.</div>`}
  </div>`;
}

function Answers({ answers }) {
  return html`<div><div class="sec-title" style="margin-bottom:8px">Answers <span class="dim-mono">ANSWERS.md</span></div>
    <div class="vlist">${answers.map((r) => html`<div class="vitem">
      <div class="dim-mono">${`${txt(r.date)} · ${txt(r.state)}`}</div>
      <div>${txt(r.question)}</div><div class="vrev">${'→ ' + txt(r.answer)}</div>
    </div>`)}</div></div>`;
}

/** Content of #left. */
export function RoadmapSidebar() {
  const state = ctx.state;
  if (!state) return null;
  const fs = features(), groups = groupStages(), s = status();
  const q = live() ? list(state.quarantine) : [];
  const canQ = s === 'running' || s === 'awaiting_user';
  const answers = list(state.answers).slice(-5).reverse();
  return html`
    <${Roadmap} fs=${fs} groups=${groups} c=${state.counts || {}} />
    ${q.map((qq) => html`<${QuarantineCard} key=${'q:' + qq.id} qq=${qq} canQ=${canQ} />`)}
    <${Validate} vals=${list(state.validate)} />
    ${answers.length ? html`<${Answers} answers=${answers} />` : null}`;
}
