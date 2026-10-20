/* "Where this project stands" trail (GET progress): folder, git, structure, conformance, map, roadmap, flows,
   approval, test environment and loop, with the button for the next action. Also the claude jobs (map, generate and
   refine the roadmap) followed through GET jobs/<id>. Without an approved roadmap the loop does not start.
   In planning mode (state.planning_required) the roadmap and flows are written in the planning chat and the approval
   goes through the planning checklist: the trail points there instead of the one-shot roadmap dialog. */
import { html, useState, useEffect } from '../../util/preact.js';
import { dur, list, txt } from '../../util/format.js';
import { followJob } from '../../api/api_client.js';
import { toast } from '../../components/toast.js';
import { openDialog, confirmDialog } from '../../components/dialog.js';
import { Checklist } from '../../components/checklist.js';
import { Conformance, conformanceDefault } from '../../components/conformance.js';
import { Markdown, extractSection } from '../../components/markdown.js';
import { ctx, status, approval } from './context.js';
import { renderAll, schedulePoll } from '../project_panel.js';
import { openNewLoop } from './alert.js';
import { approveRoadmap, APPROVAL_NOTICE, nlExpand } from './new_loop.js';
import { openProjectFile, loadTree } from './files.js';
import { planningRequired, approvePlan } from './planning.js';
import { repoConfirmContent, RepoManager } from '../repo_manager.js';

const DEFAULT_STEPS = [
  ['folder', 'Folder'], ['git', 'Git'], ['structure', '.specs structure'], ['conformance', 'Conformance'], ['map', 'Code map'],
  ['roadmap', 'Roadmap'], ['flows', 'Flows'], ['approval', 'Approval'], ['test_environment', 'Test environment'], ['loop', 'Loop'],
];
const STEP_OF_ACTION = { structure: 'structure', conformance: 'conformance', map: 'map', repos: 'repos', roadmap: 'roadmap', flows: 'flows', approve: 'approval', test_environment: 'test_environment', check: 'loop', start_loop: 'loop', follow: 'loop' };
const TYPE_ROADMAP = /roadmap/i, TYPE_MAP = /^map$|codebase/i;

const tr = { trailOpen: false, loading: false, error: null, jobs: [], following: new Map(), dialog: null, lastRead: 0, roadmap: null };
const DEFAULT_BUDGET_USD = 5, MAX_BUDGET_USD = 50;

// ---------- reading ----------
export async function reloadProgress() {
  if (tr.loading) return;
  tr.loading = true;
  try {
    ctx.progress = await ctx.api.get('progress', 15000);
    tr.error = null;
  } catch (e) {
    tr.error = e.status === 404 && !/project/i.test(e.message) ? 'The server does not report the project progress yet.' : e.message;
  } finally {
    tr.loading = false; tr.lastRead = Date.now();
    renderAll();
  }
  const rj = ctx.progress && ctx.progress.running_job;
  if (rj) { const id = typeof rj === 'string' ? rj : rj.id; if (id && !tr.following.has(id)) follow(id, typeof rj === 'object' ? rj.type : null); }
  loadJobs();
}
/** Called by the panel polling: reads the progress every ~10 s (not every 2 s). */
export function maybeReloadProgress() { if (Date.now() - tr.lastRead > 10000) reloadProgress(); }

async function loadJobs() {
  try {
    const r = await ctx.api.get('jobs', 10000);
    tr.jobs = list(Array.isArray(r) ? r : r && r.jobs);
    for (const j of tr.jobs) if (j.state === 'running' && !tr.following.has(j.id)) follow(j.id, j.type);
    renderAll();
  } catch (_) { /* no jobs route: the trail carries on without them */ }
}
function keepJob(j) {
  if (!j || !j.id) return;
  const i = tr.jobs.findIndex((x) => x.id === j.id);
  if (i >= 0) tr.jobs[i] = { ...tr.jobs[i], ...j }; else tr.jobs.unshift(j);
}
function follow(id, type) {
  if (tr.following.has(id)) return tr.following.get(id).promise;
  keepJob({ id, type, state: 'running', started_at: new Date().toISOString() });
  const a = followJob(ctx.api, 'jobs/' + encodeURIComponent(id), { onUpdate: (r) => { keepJob({ ...r, id, type: r.type || type }); renderAll(); refreshRoadmapDialog(); } });
  tr.following.set(id, a);
  a.promise.then((r) => {
    toast(r.state === 'ok' ? `${jobName(type)}: done.` : `${jobName(type)}: failed${r.error ? ' (' + r.error + ')' : ''}.`, r.state === 'ok' ? 'ok' : 'err');
  }).catch((e) => { keepJob({ id, state: 'error', error: e.message }); })
    .finally(() => { tr.following.delete(id); renderAll(); loadRoadmap(); reloadProgress(); schedulePoll(300); loadTree(true); });
  return a.promise;
}
const jobName = (type) => (TYPE_ROADMAP.test(type || '') ? 'Roadmap' : TYPE_MAP.test(type || '') ? 'Code map' : 'Job');
const jobsOf = (re) => tr.jobs.filter((j) => re.test(j.type || ''));
/** True while a code map job runs. */
export const mapRunning = () => jobsOf(TYPE_MAP).some((j) => j.state === 'running');
/** Under the "Map codebase" step: a spinner with the elapsed time while the job runs, and the last failure kept on screen. */
export function MapStatus({ written = 0, total = 0 }) {
  const [, tick] = useState(0);
  const j = jobsOf(TYPE_MAP)[0], running = !!j && j.state === 'running';
  useEffect(() => {
    if (!running) return undefined;
    const t = setInterval(() => tick((n) => n + 1), 1000);
    return () => clearInterval(t);
  }, [running]);
  if (!j) return null;
  if (running) return html`<div class="nl-msg" role="status"><div class="spinner" aria-hidden="true"></div><span>${`Mapping the code… ${elapsed(j)}. It takes 1 to 5 minutes; this screen updates by itself.${total ? ` ${written} of ${total} documents written.` : ''}`}</span></div>`;
  if (j.state === 'error') return html`<div class="nl-box nl-bad" role="alert"><strong>The last map did not work</strong> (${elapsed(j)}). ${j.error || 'No details were returned.'}</div>`;
  return null;
}
const elapsed = (j) => { const t0 = Date.parse(j.started_at), t1 = j.finished_at ? Date.parse(j.finished_at) : Date.now(); return Number.isFinite(t0) ? dur(Math.max(0, (t1 - t0) / 1000)) : ''; };

// ---------- actions ----------
/** Conformance radios for a dialog: the vnode plus a getter of the current choice. */
function conformanceInput(allowLater) {
  let value = conformanceDefault(null, allowLater);
  return { node: html`<${Conformance} allowLater=${allowLater} onChange=${(v) => { value = v; }} />`, value: () => value };
}
/** Trail action (also used by the planning checklist): dialog that creates the .specs structure. */
export async function initializeStructure() {
  const field = conformanceInput(true);
  openDialog({
    title: 'Initialize the .specs structure',
    content: [
      html`<p class="dlg-text">${'Creates .specs/project (PROJECT.md with the context for the subagents, an empty ROADMAP.md, STATE.md, FLOWS.md, RULES.md, TEST-ENVIRONMENT.md and .env.test), .specs/features and the loop folder, adds .specs/ to .gitignore and writes a generated "How to run" block in CLAUDE.md (https profile first). Never overwrites a file that already exists.'}</p>`,
      field.node,
    ],
    actions: [
      { text: 'Cancel' },
      { text: 'Initialize', cls: 'btn-primary', onClick: async (d) => {
        d.busy(true);
        try { await ctx.api.post('structure/initialize', { conformance: field.value() }); toast('Structure created.', 'ok'); d.close(); reloadProgress(); loadTree(true); schedulePoll(300); }
        catch (e) { toast('Not initialized: ' + e.message, 'err'); d.busy(false); }
      } },
    ],
  });
}
/** Trail action (also used by the planning checklist): writes the Conformance line. */
export function chooseConformance() {
  const field = conformanceInput(false);
  openDialog({
    title: 'Code conformance',
    content: [html`<p class="dlg-text">${'Creates (or updates) the "Conformance:" line in the project CLAUDE.md. You can edit that file later to match the pattern your team uses. The loop reads it to decide how much of the existing code it may change.'}</p>`, field.node],
    actions: [
      { text: 'Cancel' },
      { text: 'Save', cls: 'btn-primary', onClick: async (d) => {
        d.busy(true);
        try { await ctx.api.post('conformance', { value: field.value() }); toast('Conformance saved.', 'ok'); d.close(); reloadProgress(); }
        catch (e) { toast('Not saved: ' + e.message, 'err'); d.busy(false); }
      } },
    ],
  });
}
/** Trail action (also used by the planning checklist): starts the code map job. */
export async function mapCodebase() {
  const ok = await confirmDialog({ title: 'Map the code?', text: 'Runs claude with the /orchestrator skill in map mode: it reads the project and writes 7 short documents in docs/codebase/ at the root (stack, architecture, conventions...), committed with the code so the team can reuse them. They are a guide, not the source of truth: the code wins. Spends tokens and may take a few minutes. It does not touch the code.', yesText: 'Map now' });
  if (!ok) return;
  try {
    const r = await ctx.api.post('codebase/map', {});
    if (r.job) follow(r.job, 'map');
    toast('Mapping started' + (r.detail ? ': ' + r.detail : '.'), 'ok'); renderAll();
  } catch (e) { toast((e.status === 409 ? 'Cannot map right now: ' : 'Mapping not started: ') + e.message, 'err'); }
}
/** Trail action: the Repositories manager in a dialog (conformance and map of the other repos). */
function openReposSetup() {
  openDialog({
    title: 'Other repositories',
    width: 720,
    content: html`<${RepoManager} projectId=${ctx.projectId} locked=${status() === 'running' ? 'A loop is running: the repository list is locked until it stops.' : ''} onChange=${() => { reloadProgress(); schedulePoll(300); }} />`,
    actions: [{ text: 'Close' }],
  });
}
async function approveWithNotice() {
  const extraContent = await repoConfirmContent(ctx.api, ctx.projectId);
  const ok = await confirmDialog({ title: 'Approve the roadmap?', text: 'Unlocks the loop to run the features and stages that are in ROADMAP.md now. ' + APPROVAL_NOTICE, yesText: 'Approve roadmap', extraContent });
  if (ok) approveRoadmap(false);
}

// ---------- roadmap dialog (generate / refine) ----------
/** GET roadmap: text, approved, approved_at, errors and open_questions. */
async function loadRoadmap() {
  try { tr.roadmap = await ctx.api.get('roadmap', 15000); } catch (_) { tr.roadmap = null; }
  refreshRoadmapDialog();
}
function listOrText(v) {
  if (Array.isArray(v)) return v.filter((x) => x != null && String(x).trim()).map((x) => (typeof x === 'string' ? x : x.text || x.question || JSON.stringify(x)));
  if (typeof v === 'string' && v.trim()) return [v];
  return [];
}
// the dialog fields are controlled by tr.dialog.form: every refresh of the dialog keeps what was typed
function GenerateBlock({ form }) {
  return html`<div class="rm-block">
    <label class="nl-label" for="rm-goal">Project goal (free text)</label>
    <textarea id="rm-goal" class="dlg-area" rows="5" placeholder=${'E.g. appointment system for clinics, with patient signup, a schedule per professional, e-mail reminders and a billing dashboard. Back end in .NET, front end in React.'}
      value=${form.goal} onInput=${(e) => { form.goal = e.currentTarget.value; }}></textarea>
    <label class="nl-check" for="rm-map-first"><input type="checkbox" id="rm-map-first" checked=${form.mapFirst} onChange=${(e) => { form.mapFirst = e.currentTarget.checked; }} /><span>Map the code first (project that already has code)</span></label>
    <div class="rm-budget-l"><label class="nl-label" for="rm-budget">Max spend for this job ($)</label>
      <input type="number" id="rm-budget" class="dlg-input mono rm-budget" min="0.5" max=${MAX_BUDGET_USD} step="0.5" inputmode="decimal"
        value=${form.budget} onInput=${(e) => { form.budget = e.currentTarget.value; }} />
      <span class="hint">${`default ${DEFAULT_BUDGET_USD}, max ${MAX_BUDGET_USD}`}</span></div>
    <div class="hint">${'Runs claude with the /orchestrator skill in roadmap mode: reads the project, writes .specs/project/ROADMAP.md and returns the open questions. Spends tokens; does not start the loop.'}</div>
  </div>`;
}
function RefineBlock({ form, hidden }) {
  return html`<div class="rm-block" hidden=${hidden}><label class="nl-label" for="rm-refine">Refine the roadmap</label>
    <textarea id="rm-refine" class="dlg-area" rows="3" placeholder=${'Answer the open questions or ask for changes: "merge the login features", "stage 2 comes first", "no admin screen needed"…'}
      value=${form.refine} onInput=${(e) => { form.refine = e.currentTarget.value; }}></textarea>
    <div class="hint">Continues the same conversation as the last generated roadmap.</div>
  </div>`;
}
function openRoadmapDialog() {
  if (tr.dialog) return;
  const pr = ctx.progress || {};
  const mapStep = list(pr.steps).find((e) => e.id === 'map');
  const form = { goal: '', mapFirst: !!(mapStep && !mapStep.ok), budget: String(DEFAULT_BUDGET_USD), refine: '' };
  const t = { d: null, form };
  tr.dialog = t;
  t.d = openDialog({ title: 'AI roadmap', width: 760, cls: 'dlg-roadmap', content: roadmapBody(t).node, onClose: () => { tr.dialog = null; } });
  refreshRoadmapDialog();
  loadRoadmap();
}
function lastRoadmapJob() { return jobsOf(TYPE_ROADMAP)[0] || null; }
/** Questions: from GET roadmap (open_questions); without them, the "Open questions" section of the job reply. */
function roadmapQuestions(j) {
  const fromBack = listOrText(tr.roadmap && tr.roadmap.open_questions);
  if (fromBack.length) return fromBack;
  const sec = j && j.reply ? extractSection(j.reply, /open questions/i) : '';
  return sec ? [sec] : [];
}
/** Content of .rm-res in the roadmap dialog (empty = hidden by CSS). */
function RoadmapResult({ t, j, running, loopRunning, questions }) {
  const errors = listOrText(tr.roadmap && tr.roadmap.errors);
  const rm = tr.roadmap;
  return html`
    ${rm && rm.approved ? html`<div class="nl-box nl-ok"><span class="chip">ROADMAP APPROVED</span><span>${rm.approved_at ? ' on ' + String(rm.approved_at).replace('T', ' ').slice(0, 16) : ''}</span></div>` : null}
    ${errors.length ? html`<div class="nl-box nl-bad"><strong>ROADMAP.md has problems that block the approval:</strong><ul>${errors.map((e) => html`<li>${e}</li>`)}</ul></div>` : null}
    ${questions.length ? html`<div class="rm-questions"><div class="small-label">${`Open questions (${questions.length})`}</div>
      <div class="md-doc">${questions.length === 1 && /\n/.test(questions[0]) ? html`<${Markdown} source=${questions[0]} />`
        : html`<ol class="md-list">${questions.map((q) => html`<li><${Markdown} source=${q} /></li>`)}</ol>`}</div>
      <div class="hint">Answer in the Refine field below; claude adjusts the roadmap.</div></div>` : null}
    ${loopRunning ? html`<div class="nl-box nl-warn">The loop is running: generate or refine the roadmap after it stops.</div>` : null}
    ${j && running ? html`<div class="nl-msg" role="status"><div class="spinner" aria-hidden="true"></div><span>${`claude working on the roadmap… ${elapsed(j)}. You can close this window; the result stays here and in the trail.`}</span></div>` : null}
    ${j && !running && j.state === 'error' ? html`<div class="nl-box nl-bad" role="alert"><strong>The job failed: </strong>${txt(j.error)}</div>` : null}
    ${j && !running && j.reply ? html`<details class="rm-reply" open=${!roadmapQuestions(j).length}><summary>${`claude reply${typeof j.cost_usd === 'number' ? ' · $' + j.cost_usd.toFixed(2) : ''}`}</summary>
      <div class="md-doc"><${Markdown} source=${j.reply} /></div></details>` : null}
    ${j && !running && j.state === 'ok' ? html`<div class="card-row">
      <button type="button" class="btn btn-sm" onClick=${() => { t.d.close(); openProjectFile('ROADMAP.md'); }}>View ROADMAP.md</button>
      ${!approval().approved ? html`<button type="button" class="btn btn-sm" onClick=${() => { t.d.close(); approveWithNotice(); }}>Approve roadmap</button>` : null}
    </div>` : null}`;
}
/** Body of the roadmap dialog: generate block, result (.rm-res) and refine block. */
function roadmapBody(t) {
  const j = lastRoadmapJob(), running = !!(j && j.state === 'running'), hasSession = jobsOf(TYPE_ROADMAP).some((x) => x.state === 'ok');
  const loopRunning = status() === 'running';
  const questions = roadmapQuestions(j && !running ? j : null);
  const canRefine = hasSession || questions.length > 0;
  return { canRefine, running, loopRunning, hasSession, node: html`
    <${GenerateBlock} form=${t.form} />
    <div class="rm-res" aria-live="polite"><${RoadmapResult} t=${t} j=${j} running=${running} loopRunning=${loopRunning} questions=${questions} /></div>
    <${RefineBlock} form=${t.form} hidden=${!canRefine} />` };
}
function refreshRoadmapDialog() {
  const t = tr.dialog; if (!t || !t.d) return;
  const b = roadmapBody(t);
  t.d.setContent(b.node);
  t.d.setActions([
    { text: 'Close' },
    b.canRefine ? { text: 'Refine', cls: '', disabled: b.running || b.loopRunning, onClick: () => sendRoadmap('refine') } : null,
    { text: b.hasSession ? 'Generate again' : 'Generate roadmap', cls: 'btn-primary', disabled: b.running || b.loopRunning, onClick: () => sendRoadmap('generate') },
  ].filter(Boolean));
}
function validBudget(v) {
  const n = Number(v);
  return Number.isFinite(n) && n > 0 ? Math.min(MAX_BUDGET_USD, n) : DEFAULT_BUDGET_USD;
}
async function sendRoadmap(kind) {
  const t = tr.dialog; if (!t) return;
  const text = (kind === 'generate' ? t.form.goal : t.form.refine).trim();
  if (!text) {
    toast(kind === 'generate' ? 'Write the project goal.' : 'Write what to refine.', 'err');
    const field = document.getElementById(kind === 'generate' ? 'rm-goal' : 'rm-refine'); if (field) field.focus();
    return;
  }
  if (kind === 'generate' && approval().approved) {
    const ok = await confirmDialog({ title: 'Generate a new roadmap?', text: 'There is already an approved roadmap. A new roadmap with different features or stages drops the approval; you review it and approve again.', yesText: 'Generate anyway' });
    if (!ok) return;
  }
  t.d.busy(true);
  try {
    const r = kind === 'generate'
      ? await ctx.api.post('roadmap/generate', { goal: text, map_first: !!t.form.mapFirst, budget_usd: validBudget(t.form.budget) })
      : await ctx.api.post('roadmap/refine', { text });
    if (r.job) follow(r.job, kind === 'generate' ? 'roadmap' : 'roadmap_refine');
    if (kind === 'refine') t.form.refine = '';
  } catch (e) { toast((e.status === 409 ? 'Not right now: ' : 'Not sent: ') + e.message, 'err'); }
  finally { if (tr.dialog) { t.d.busy(false); refreshRoadmapDialog(); } }
}

// ---------- drawing ----------
function actionButton(action) {
  const B = (text, fn, cls = 'btn-primary') => html`<button type="button" id="tr-main-action" class=${'btn btn-sm ' + cls} onClick=${fn} disabled=${!fn}>${text}</button>`;
  const planning = planningRequired();
  switch (action) {
    case 'structure': return B('Initialize structure', initializeStructure);
    case 'conformance': return B('Choose conformance', chooseConformance);
    case 'map': return mapRunning() ? B('Mapping…', null, 'btn-pending') : B('Map codebase', mapCodebase);
    case 'repos': return B('Set up repositories', openReposSetup);
    case 'roadmap': return planning ? null : B('Generate roadmap', openRoadmapDialog);
    case 'flows': return planning ? null : B('Open FLOWS.md', () => openProjectFile('FLOWS.md'));
    case 'approve': return B('Approve roadmap', planning ? approvePlan : approveWithNotice);
    case 'test_environment': return B('Describe the test environment', () => openProjectFile('TEST-ENVIRONMENT.md'));
    case 'check': case 'start_loop': return B('Go to Start loop', () => { nlExpand(); openNewLoop(); setTimeout(() => { const b = document.getElementById('btn-start'); if (b) b.focus(); }, 60); });
    default: return null;
  }
}
const PLANNING_TEXT = {
  roadmap: 'Plan the roadmap in the chat below.', flows: 'Ask the planning chat for the missing flows (FLOWS.md).',
  approve: 'Every gate passes: approve the roadmap.',
};
const ACTION_TEXT = {
  structure: 'Create the .specs folder for the loop to keep its state.', conformance: 'Say whether the code follows the skill pattern or is legacy.',
  repos: 'Give every other repository a Conformance line and a code map.',
  map: 'Map the existing code so the roadmap starts from what already exists.', roadmap: 'Generate the roadmap from the project goal.',
  flows: 'Write one flow per feature in FLOWS.md (what the verifier checks).',
  approve: 'Review and approve the roadmap: without approval the loop does not start.', test_environment: 'Optional: say how to start the app for flow tests.',
  check: 'All set: check and start the loop.', start_loop: 'All set: check and start the loop.', follow: 'Loop in progress: follow it below.',
};

/** Label of a running job; ticks every second on its own. */
function JobTimer({ j }) {
  const [, tick] = useState(0);
  useEffect(() => { const t = setInterval(() => tick((n) => n + 1), 1000); return () => clearInterval(t); }, []);
  return html`<span class="tr-job-t" data-job=${j.id}>${`${jobName(j.type)} · ${elapsed(j)}`}</span>`;
}

/** Content of #trail. */
export function ProgressTrail() {
  const pr = ctx.progress;
  if (!pr) {
    return html`<div class="tr-line"><span class="tr-title">Where this project stands</span>
      <span class="hint">${tr.error ? 'Progress unavailable: ' + tr.error : 'reading the project progress…'}</span></div>`;
  }
  const byId = Object.fromEntries(list(pr.steps).map((e) => [e.id, e]));
  const ids = list(pr.steps).length ? list(pr.steps).map((e) => e.id) : DEFAULT_STEPS.map(([id]) => id);
  const titles = Object.fromEntries(DEFAULT_STEPS);
  const steps = ids.map((id) => {
    const e = byId[id] || {};
    return { id, title: e.title || titles[id] || id, state: e.ok ? 'ok' : e.optional ? 'optional' : 'pending', detail: e.detail || '' };
  });
  const runningJobs = tr.jobs.filter((j) => j.state === 'running');
  const approved = approval().approved || (byId.approval && byId.approval.ok);
  const planning = planningRequired();
  const aiRoadmap = !planning && pr.next_action !== 'roadmap' && status() !== 'running' && byId.structure && byId.structure.ok;
  const toggleTrail = () => { tr.trailOpen = !tr.trailOpen; renderAll(); };
  return html`<div class=${'tr-line full-line' + (tr.trailOpen ? ' is-open' : ' is-compact')}><span class="tr-title">Where this project stands</span>
    <${Checklist} label="Where this project stands" steps=${steps} currentId=${STEP_OF_ACTION[pr.next_action] || null} />
    <div class="tr-action">
      <span class="tr-next">${(planning && PLANNING_TEXT[pr.next_action]) || ACTION_TEXT[pr.next_action] || ''}</span>
      ${actionButton(pr.next_action)}
      ${aiRoadmap ? html`<button type="button" id="tr-ai-roadmap" class="btn btn-sm btn-ghost tr-ai-sep" onClick=${openRoadmapDialog} title="Refine the roadmap with Claude. Does not start the loop.">AI roadmap</button>` : null}
      ${runningJobs.map((j) => html`<button key=${j.id} type="button" id=${'tr-job-' + j.id} class="tr-job" onClick=${TYPE_ROADMAP.test(j.type || '') ? openRoadmapDialog : null} title="claude job running">
        <span class="spinner sm" aria-hidden="true"></span><${JobTimer} j=${j} /></button>`)}
      ${!approved && ['roadmap', 'flows', 'approve', 'structure', 'conformance', 'map', 'repos'].includes(pr.next_action) ? html`<span class="tr-note">Without an approved roadmap the loop does not start.</span>` : null}
    </div>
    <button type="button" id="tr-toggle" class="tr-toggle" aria-expanded=${String(tr.trailOpen)} title=${tr.trailOpen ? 'Collapse the trail to one line' : 'Show the trail descriptions and notes'}
      onClick=${toggleTrail}>${tr.trailOpen ? '▴' : '▾'}</button>
  </div>`;
}
