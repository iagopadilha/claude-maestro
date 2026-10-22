/* Plan & files tab of the GLOBAL zone (the summary part): stages with their counts by status, the count of AI decisions to review,
   quarantine pointers and ANSWERS.md. Also the selection helpers (selectStage / selectFeature / selectAgent) other views use.
   The full plan and its files are read in plan_files_dialog.js; the decisions in decisions.js. */
import { html } from '../../util/preact.js';
import { isNum, num, txt, list } from '../../util/format.js';
import { ctx, ui, live, features } from './context.js';
import { renderAll } from '../project_panel.js';
import { fetchAgentLog } from './inspector.js';
import { DecisionsCount } from './decisions.js';
import { openPauseDialog } from './dialogs/index.js';
import { loadHist } from './history.js';

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
export function selectFeature(id) { ui.sel = { kind: 'feature', id }; ui.tab = 'info'; ui.chatGeneral = false; renderAll(); loadHist(); }
export function selectAgent(id) { ui.sel = { kind: 'agent', id }; ui.tab = 'chat'; ui.chatGeneral = false; ui.agentLog = null; ui.logLoading = id; renderAll(); fetchAgentLog(id); }

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

/** Short summary of the plan for the Plan & files tab (PLAN-01): feature counts by status, one row per stage and the button that opens the dialog. */
function Roadmap({ fs, groups, c }) {
  const verified = ui.past ? fs.filter((f) => f.status === 'verified').length : c.verified;
  const total = ui.past ? fs.length : c.total;
  const stageSel = (g) => !!(ui.sel && ui.sel.kind === 'stage' && ui.sel.n === g.n);
  return html`<div>
    <div class="sec-head"><h2 class="sec-title">Plan</h2><div class="dim-mono">${`${num(verified)}/${num(total)} verified`}</div></div>
    ${groups.length ? html`<div class="stages">${groups.map((g) => g.n == null
      ? html`<div class="small-label" key="none">${g.title}<span class="rm-stage-n">${' ' + stageCounts(g.items)}</span></div>`
      : html`<button key=${String(g.n)} type="button" class=${'stage-h rm-stage' + (stageSel(g) ? ' sel' : '')} aria-expanded=${String(stageSel(g))} title="opens the summary of this stage in the inspector"
          onClick=${() => selectStage(g.n)}><span class="rm-stage-t"><span class="rm-stage-name">${g.title}</span>
            <span class="rm-stage-n">${stageCounts(g.items)}</span></span><span class="stage-chev" aria-hidden="true">›</span></button>`)}</div>`
      : html`<div class="empty-note">No feature in the plan yet.</div>`}
  </div>`;
}

/** A feature in quarantine: a pointer to the pause dialog (the decision and its context live there, not here). */
function QuarantineCard({ qq }) {
  return html`<div class="qcard">
    <div class="qtitle"><span class="dot dot-red"></span><span>${`${qq.id} in quarantine`}</span></div>
    <div class="qtext">${txt(qq.reason)}</div>
    ${list(qq.stuck).length ? html`<div class="qtext">Holds back: <span class="mono">${list(qq.stuck).join(', ')}</span></div>` : html`<div class="qtext">Nothing depends on it, the loop carries on.</div>`}
    <button type="button" class="btn btn-sm" onClick=${() => openPauseDialog(qq.id)}>Understand and resolve</button>
  </div>`;
}

/** Decisions the AI took alone that wait for your review: the count and the link to the dialog (decisions.js). */
function DecisionsToReview() {
  return html`<div class="pf-sum-row" id="plan-decisions"><${DecisionsCount} project=${ctx.projectId} state=${ctx.state} /></div>`;
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
  const fs = features(), groups = groupStages();
  const q = live() ? list(state.quarantine) : [];
  const answers = list(state.answers).slice(-5).reverse();
  return html`
    <${Roadmap} fs=${fs} groups=${groups} c=${state.counts || {}} />
    ${q.map((qq) => html`<${QuarantineCard} key=${'q:' + qq.id} qq=${qq} />`)}
    <${DecisionsToReview} />
    ${answers.length ? html`<${Answers} answers=${answers} />` : null}`;
}
