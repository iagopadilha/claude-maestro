/* "Where this project stands" trail (GET progress): folder, git, structure, conformance, map, roadmap, approval,
   test environment and loop, with the button for the next action. Also the claude jobs (map, generate and refine
   the roadmap) followed through GET jobs/<id>. Without an approved roadmap the loop does not start. */
import { $, h, fill, keepFocus } from '../../util/dom.js';
import { dur, list, txt } from '../../util/format.js';
import { followJob } from '../../api/api_client.js';
import { toast } from '../../components/toast.js';
import { openDialog, confirmDialog } from '../../components/dialog.js';
import { createChecklist } from '../../components/checklist.js';
import { conformanceField } from '../../components/conformance.js';
import { renderMarkdown, extractSection } from '../../components/markdown.js';
import { ctx, status, approval } from './context.js';
import { schedulePoll } from '../project_panel.js';
import { openNewLoop } from './alert.js';
import { approveRoadmap, APPROVAL_NOTICE } from './new_loop.js';
import { openProjectFile, loadTree } from './files.js';

const DEFAULT_STEPS = [
  ['folder', 'Folder'], ['git', 'Git'], ['structure', '.specs structure'], ['conformance', 'Conformance'], ['map', 'Code map'],
  ['roadmap', 'Roadmap'], ['approval', 'Approval'], ['test_environment', 'Test environment'], ['loop', 'Loop'],
];
const STEP_OF_ACTION = { structure: 'structure', conformance: 'conformance', map: 'map', roadmap: 'roadmap', approve: 'approval', test_environment: 'test_environment', check: 'loop', start_loop: 'loop', follow: 'loop' };
const TYPE_ROADMAP = /roadmap/i, TYPE_MAP = /^map$|codebase/i;

const tr = { checklist: null, loading: false, error: null, jobs: [], following: new Map(), dialog: null, lastRead: 0, roadmap: null };
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
    renderTrail();
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
    renderTrail();
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
  const a = followJob(ctx.api, 'jobs/' + encodeURIComponent(id), { onUpdate: (r) => { keepJob({ ...r, id, type: r.type || type }); renderTrail(); refreshRoadmapDialog(); } });
  tr.following.set(id, a);
  a.promise.then((r) => {
    toast(r.state === 'ok' ? `${jobName(type)}: done.` : `${jobName(type)}: failed${r.error ? ' (' + r.error + ')' : ''}.`, r.state === 'ok' ? 'ok' : 'err');
  }).catch((e) => { keepJob({ id, state: 'error', error: e.message }); })
    .finally(() => { tr.following.delete(id); renderTrail(); loadRoadmap(); reloadProgress(); schedulePoll(300); loadTree(true); });
  return a.promise;
}
const jobName = (type) => (TYPE_ROADMAP.test(type || '') ? 'Roadmap' : TYPE_MAP.test(type || '') ? 'Code map' : 'Job');
const jobsOf = (re) => tr.jobs.filter((j) => re.test(j.type || ''));
const elapsed = (j) => { const t0 = Date.parse(j.started_at), t1 = j.finished_at ? Date.parse(j.finished_at) : Date.now(); return Number.isFinite(t0) ? dur(Math.max(0, (t1 - t0) / 1000)) : ''; };

// ---------- actions ----------
async function initializeStructure() {
  const field = conformanceField({ allowLater: true });
  openDialog({
    title: 'Initialize the .specs structure',
    content: [
      h('p', { class: 'dlg-text', text: 'Creates .specs/project (PROJECT.md, an empty ROADMAP.md, STATE.md, RULES.md), .specs/codebase, .specs/features and the loop folder, and adds .specs/ to .gitignore. Never overwrites a file that already exists.' }),
      field.element,
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
function chooseConformance() {
  const field = conformanceField({ allowLater: false });
  openDialog({
    title: 'Code conformance',
    content: [h('p', { class: 'dlg-text', text: 'Writes the "Conformance:" line in the project CLAUDE.md. The loop uses it to decide how much it may touch the existing code.' }), field.element],
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
async function mapCodebase() {
  const ok = await confirmDialog({ title: 'Map the code?', text: 'Runs claude with the /orchestrator skill in map mode: it reads the project and writes .specs/codebase (stack, architecture, conventions). Spends tokens and may take a few minutes. It does not touch the code.', yesText: 'Map now' });
  if (!ok) return;
  try {
    const r = await ctx.api.post('codebase/map', {});
    if (r.job) follow(r.job, 'map');
    toast('Mapping started' + (r.detail ? ': ' + r.detail : '.'), 'ok'); renderTrail();
  } catch (e) { toast((e.status === 409 ? 'Cannot map right now: ' : 'Mapping not started: ') + e.message, 'err'); }
}
async function approveWithNotice() {
  const ok = await confirmDialog({ title: 'Approve the roadmap?', text: 'Unlocks the loop to run the features and stages that are in ROADMAP.md now. ' + APPROVAL_NOTICE, yesText: 'Approve roadmap' });
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
function openRoadmapDialog() {
  if (tr.dialog) return;
  const pr = ctx.progress || {};
  const mapStep = list(pr.steps).find((e) => e.id === 'map');
  const goal = h('textarea', { id: 'rm-goal', class: 'dlg-area', rows: 5, placeholder: 'E.g. appointment system for clinics, with patient signup, a schedule per professional, e-mail reminders and a billing dashboard. Back end in .NET, front end in React.' });
  const mapFirst = h('input', { type: 'checkbox', id: 'rm-map-first' });
  mapFirst.checked = !!(mapStep && !mapStep.ok);
  const budget = h('input', { type: 'number', id: 'rm-budget', class: 'dlg-input mono rm-budget', min: 0.5, max: MAX_BUDGET_USD, step: 0.5, value: String(DEFAULT_BUDGET_USD), inputmode: 'decimal' });
  const refine = h('textarea', { id: 'rm-refine', class: 'dlg-area', rows: 3, placeholder: 'Answer the open questions or ask for changes: "merge the login features", "stage 2 comes first", "no admin screen needed"…' });
  const result = h('div', { class: 'rm-res', 'aria-live': 'polite' });
  const generateBlock = h('div', { class: 'rm-block' },
    h('label', { class: 'nl-label', for: 'rm-goal', text: 'Project goal (free text)' }), goal,
    h('label', { class: 'nl-check', for: 'rm-map-first' }, mapFirst, h('span', { text: 'Map the code first (project that already has code)' })),
    h('div', { class: 'rm-budget-l' }, h('label', { class: 'nl-label', for: 'rm-budget', text: 'Max spend for this job ($)' }), budget,
      h('span', { class: 'hint', text: `default ${DEFAULT_BUDGET_USD}, max ${MAX_BUDGET_USD}` })),
    h('div', { class: 'hint', text: 'Runs claude with the /orchestrator skill in roadmap mode: reads the project, writes .specs/project/ROADMAP.md and returns the open questions. Spends tokens; does not start the loop.' }));
  const refineBlock = h('div', { class: 'rm-block' }, h('label', { class: 'nl-label', for: 'rm-refine', text: 'Refine the roadmap' }), refine,
    h('div', { class: 'hint', text: 'Continues the same conversation as the last generated roadmap.' }));
  const d = openDialog({
    title: 'AI roadmap', width: 760, cls: 'dlg-roadmap',
    content: [generateBlock, result, refineBlock],
    onClose: () => { tr.dialog = null; },
  });
  tr.dialog = { d, goal, mapFirst, budget, refine, result, generateBlock, refineBlock };
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
function refreshRoadmapDialog() {
  const t = tr.dialog; if (!t) return;
  const j = lastRoadmapJob(), running = j && j.state === 'running', hasSession = jobsOf(TYPE_ROADMAP).some((x) => x.state === 'ok');
  const loopRunning = status() === 'running';
  const kids = [];
  if (loopRunning) kids.push(h('div', { class: 'nl-box nl-warn', text: 'The loop is running: generate or refine the roadmap after it stops.' }));
  if (j) {
    if (running) kids.push(h('div', { class: 'nl-msg', role: 'status' }, h('div', { class: 'spinner', 'aria-hidden': 'true' }), h('span', { text: `claude working on the roadmap… ${elapsed(j)}. You can close this window; the result stays here and in the trail.` })));
    else if (j.state === 'error') kids.push(h('div', { class: 'nl-box nl-bad', role: 'alert' }, h('strong', { text: 'The job failed: ' }), txt(j.error)));
    if (!running && j.reply) {
      kids.push(h('details', { class: 'rm-reply', open: roadmapQuestions(j).length ? null : true }, h('summary', { text: `claude reply${typeof j.cost_usd === 'number' ? ' · $' + j.cost_usd.toFixed(2) : ''}` }), h('div', { class: 'md-doc' }, renderMarkdown(j.reply))));
    }
    if (!running && j.state === 'ok') {
      kids.push(h('div', { class: 'card-row' },
        h('button', { type: 'button', class: 'btn btn-sm', onclick: () => { t.d.close(); openProjectFile('ROADMAP.md'); } }, 'View ROADMAP.md'),
        !approval().approved ? h('button', { type: 'button', class: 'btn btn-sm', onclick: () => { t.d.close(); approveWithNotice(); } }, 'Approve roadmap') : null));
    }
  }
  const questions = roadmapQuestions(j && !running ? j : null);
  if (questions.length) kids.unshift(h('div', { class: 'rm-questions' }, h('div', { class: 'small-label', text: `Open questions (${questions.length})` }),
    h('div', { class: 'md-doc' }, questions.length === 1 && /\n/.test(questions[0]) ? renderMarkdown(questions[0]) : h('ol', { class: 'md-list' }, questions.map((q) => h('li', {}, renderMarkdown(q))))),
    h('div', { class: 'hint', text: 'Answer in the Refine field below; claude adjusts the roadmap.' })));
  const errors = listOrText(tr.roadmap && tr.roadmap.errors);
  if (errors.length) kids.unshift(h('div', { class: 'nl-box nl-bad' }, h('strong', { text: 'ROADMAP.md has problems that block the approval:' }), h('ul', {}, errors.map((e) => h('li', { text: e })))));
  if (tr.roadmap && tr.roadmap.approved) kids.unshift(h('div', { class: 'nl-box nl-ok' }, h('span', { class: 'chip', text: 'ROADMAP APPROVED' }), h('span', { text: tr.roadmap.approved_at ? ' on ' + String(tr.roadmap.approved_at).replace('T', ' ').slice(0, 16) : '' })));
  fill(t.result, kids);
  const canRefine = hasSession || questions.length > 0;
  t.refineBlock.hidden = !canRefine;
  t.d.setActions([
    { text: 'Close' },
    canRefine ? { text: 'Refine', cls: '', disabled: running || loopRunning, onClick: () => sendRoadmap('refine') } : null,
    { text: hasSession ? 'Generate again' : 'Generate roadmap', cls: 'btn-primary', disabled: running || loopRunning, onClick: () => sendRoadmap('generate') },
  ].filter(Boolean));
}
function validBudget(v) {
  const n = Number(v);
  return Number.isFinite(n) && n > 0 ? Math.min(MAX_BUDGET_USD, n) : DEFAULT_BUDGET_USD;
}
async function sendRoadmap(kind) {
  const t = tr.dialog; if (!t) return;
  const field = kind === 'generate' ? t.goal : t.refine;
  const text = field.value.trim();
  if (!text) { toast(kind === 'generate' ? 'Write the project goal.' : 'Write what to refine.', 'err'); field.focus(); return; }
  if (kind === 'generate' && approval().approved) {
    const ok = await confirmDialog({ title: 'Generate a new roadmap?', text: 'There is already an approved roadmap. A new roadmap with different features or stages drops the approval; you review it and approve again.', yesText: 'Generate anyway' });
    if (!ok) return;
  }
  t.d.busy(true);
  try {
    const r = kind === 'generate'
      ? await ctx.api.post('roadmap/generate', { goal: text, map_first: !!t.mapFirst.checked, budget_usd: validBudget(t.budget.value) })
      : await ctx.api.post('roadmap/refine', { text });
    if (r.job) follow(r.job, kind === 'generate' ? 'roadmap' : 'roadmap_refine');
    if (kind === 'refine') t.refine.value = '';
  } catch (e) { toast((e.status === 409 ? 'Not right now: ' : 'Not sent: ') + e.message, 'err'); }
  finally { if (tr.dialog) { t.d.busy(false); refreshRoadmapDialog(); } }
}

// ---------- drawing ----------
function actionButton(action) {
  const B = (text, fn, cls = 'btn-primary') => h('button', { type: 'button', id: 'tr-main-action', class: 'btn btn-sm ' + cls, onclick: fn, disabled: !fn }, text);
  const mapRunning = jobsOf(TYPE_MAP).some((j) => j.state === 'running');
  switch (action) {
    case 'structure': return B('Initialize structure', initializeStructure);
    case 'conformance': return B('Choose conformance', chooseConformance);
    case 'map': return mapRunning ? B('Mapping…', null, 'btn-pending') : B('Map codebase', mapCodebase);
    case 'roadmap': return B('Generate roadmap', openRoadmapDialog);
    case 'approve': return B('Approve roadmap', approveWithNotice);
    case 'test_environment': return B('Describe the test environment', () => openProjectFile('TEST-ENVIRONMENT.md'));
    case 'check': case 'start_loop': return B('Check and start loop', openNewLoop);
    default: return null;
  }
}
const ACTION_TEXT = {
  structure: 'Create the .specs folder for the loop to keep its state.', conformance: 'Say whether the code follows the skill pattern or is legacy.',
  map: 'Map the existing code so the roadmap starts from what already exists.', roadmap: 'Generate the roadmap from the project goal.',
  approve: 'Review and approve the roadmap: without approval the loop does not start.', test_environment: 'Optional: say how to start the app for flow tests.',
  check: 'All set: check and start the loop.', start_loop: 'All set: check and start the loop.', follow: 'Loop in progress: follow it below.',
};

export function renderTrail() {
  const root = $('trail'); if (!root) return;
  if (!tr.checklist) tr.checklist = createChecklist({ label: 'Where this project stands' });
  const pr = ctx.progress;
  if (!pr) {
    fill(root, h('div', { class: 'tr-line' }, h('span', { class: 'tr-title', text: 'Where this project stands' }),
      h('span', { class: 'hint', text: tr.error ? 'Progress unavailable: ' + tr.error : 'reading the project progress…' })));
    return;
  }
  const byId = Object.fromEntries(list(pr.steps).map((e) => [e.id, e]));
  const ids = list(pr.steps).length ? list(pr.steps).map((e) => e.id) : DEFAULT_STEPS.map(([id]) => id);
  const titles = Object.fromEntries(DEFAULT_STEPS);
  const steps = ids.map((id) => {
    const e = byId[id] || {};
    return { id, title: e.title || titles[id] || id, state: e.ok ? 'ok' : e.optional ? 'optional' : 'pending', detail: e.detail || '' };
  });
  tr.checklist.update(steps, STEP_OF_ACTION[pr.next_action] || null);
  const runningJobs = tr.jobs.filter((j) => j.state === 'running');
  const approved = approval().approved || (byId.approval && byId.approval.ok);
  const actionKids = [
    h('span', { class: 'tr-next', text: ACTION_TEXT[pr.next_action] || '' }),
    actionButton(pr.next_action),
    pr.next_action !== 'roadmap' && status() !== 'running' && byId.structure && byId.structure.ok ? h('button', { type: 'button', id: 'tr-ai-roadmap', class: 'btn btn-sm btn-ghost', onclick: openRoadmapDialog, title: 'generate or refine the roadmap with claude' }, 'AI roadmap') : null,
    runningJobs.map((j) => h('button', { type: 'button', id: 'tr-job-' + j.id, class: 'tr-job', onclick: TYPE_ROADMAP.test(j.type || '') ? openRoadmapDialog : null, title: 'claude job running' },
      h('span', { class: 'spinner sm', 'aria-hidden': 'true' }), h('span', { class: 'tr-job-t', 'data-job': j.id, text: `${jobName(j.type)} · ${elapsed(j)}` }))),
    !approved && ['roadmap', 'approve', 'structure', 'conformance', 'map'].includes(pr.next_action) ? h('span', { class: 'tr-note', text: 'Without an approved roadmap the loop does not start.' }) : null,
  ];
  if (!root.firstChild || root.firstChild.className !== 'tr-line full-line') {
    fill(root, h('div', { class: 'tr-line full-line' }, h('span', { class: 'tr-title', text: 'Where this project stands' }), tr.checklist.element, h('div', { class: 'tr-action' })));
  }
  keepFocus(() => fill(root.querySelector('.tr-action'), actionKids));
}

/** Refreshes the job timers every second while one is running. */
export function initTrail() {
  setInterval(() => {
    for (const el of document.querySelectorAll('.tr-job-t[data-job]')) {
      const j = tr.jobs.find((x) => x.id === el.dataset.job);
      if (j) el.textContent = `${jobName(j.type)} · ${elapsed(j)}`;
    }
  }, 1000);
}
