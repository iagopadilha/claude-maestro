/* Planning mode (state.planning_required): a project with no approved roadmap that never ran a loop opens here
   instead of the loop panel. Left (SidePanel): prerequisites, gate checklist (GET planning), the documents (read-only, refreshed after
   every reply) and "Approve roadmap"; right (ChatCol): the live /orchestrator plan chat that writes ROADMAP.md and FLOWS.md, the project
   context (PROJECT.md) and the rules (RULES.md) (POST planning/send, reply followed through GET planning/job/<id>). Layout: styles/planning_layout.css.
   The back end enforces every gate; this screen only shows them. */
import { html, useEffect, useLayoutEffect, useRef, useState } from '../../util/preact.js';
import { isNum, cost, list, fmtHour } from '../../util/format.js';
import { appApi } from '../../api/api_client.js';
import { toast } from '../../components/toast.js';
import { confirmDialog, openDialog } from '../../components/dialog.js';
import { pickFolder } from '../../components/folder_browser.js';
import { Markdown } from '../../components/markdown.js';
import { ctx, approval } from './context.js';
import { renderAll, schedulePoll } from '../project_panel.js';
import { Think } from './chat.js';
import { RoadmapPreview } from './roadmap_preview.js';
import { APPROVAL_NOTICE } from './new_loop.js';
import { RepoManager, RepoConfirm, featureRepoRows, fetchProjectRepos, fetchCatalog, normPath, repoConfirmContent } from '../repo_manager.js';
import { loadWorkspace } from './workspace.js';
import { reloadProgress, initializeStructure, chooseConformance, mapCodebase, mapRunning, MapStatus, monorepoMode } from './progress_trail.js';
import { SubprojectsInstallerPanel, loadSubprojects, subprojectsData, onSubprojectsChanged } from './subprojects.js';
import { ProjectInstallerPanel } from './project_installer.js';

const NATIVE_DIALOG_TIMEOUT_MS = 310000; // the back end waits up to 300 s for the system window
const JOB_POLL_MS = 1500, JOB_GIVEUP_MS = 660 * 1000, REFRESH_MS = 6000, MAX_TEXT = 8000;
const ENV_GATE = 'test_environment'; // gate id, also the id of its side-panel tab
const QUESTIONS_GATE = 'open_questions'; // gate id, also the id of its side-panel tab
const READY_GATE = 'ready'; // gate id: the readiness check (the loop can run), last item of the checklist
const REPOS_GATE = 'repos'; // gate id of the other repositories' setup, also the id of the Repositories tab (not a prerequisite of the chat)
const PREREQUISITES = ['git', 'structure', 'subprojects', 'conformance', 'map'];
const SUBPROJECT_GATES = ['subprojects', 'conformance', 'map']; // gates the installer (components/subprojects_installer.js) answers in a monorepo
const INSTALLER_GATES = ['conformance', 'map']; // gates the single-repository installer (components/project_installer.js) answers
const DOCS = [['project', 'Project', 'PROJECT.md'], ['roadmap', 'Roadmap', 'ROADMAP.md'], ['flows', 'Flows', 'FLOWS.md'], ['environment', 'Environment', 'TEST-ENVIRONMENT.md']];
const SEND_ERRORS = {
  planning_busy: 'A reply is still being written: wait for it before sending another message.',
  planning_prerequisites: 'Finish the prerequisites on the left before chatting.',
  loop_running: 'A loop is running in this project: planning is closed while it runs.',
};
// actions of the failing prerequisites: the same ones the trail uses (dialogs and jobs live in progress_trail.js)
const PREREQ_ACTION = {
  structure: { text: 'Initialize structure', run: () => initializeStructure() },
  conformance: { text: 'Choose conformance', run: () => chooseConformance() },
  map: { text: 'Map codebase', run: () => mapCodebase(), busyText: 'Mapping…', busy: () => mapRunning() },
};
// a monorepo never opens the root-level dialogs: conformance and map are per subproject, in the installer (no wizard button)
const MONOREPO_PREREQ_ACTION = { conformance: null, map: null, subprojects: null };
/** True when this planning read belongs to a monorepo: the `subprojects` gate exists, or progress says so. */
const planningIsMonorepo = (d) => !!(d && list(d.gates).some((g) => g.id === 'subprojects')) || monorepoMode();
const prereqActionFor = (d, gateId) => (planningIsMonorepo(d) && gateId in MONOREPO_PREREQ_ACTION ? MONOREPO_PREREQ_ACTION[gateId] : PREREQ_ACTION[gateId]);

/** Screen state (survives renders; the textarea itself is uncontrolled so typing and the caret survive the polling). */
const pl = { repos: null, catalog: [], sugOff: {}, sugBusy: false, repoVer: 0, data: null, loading: false, again: false, error: null, loadedAt: 0, job: null, sending: false, pending: null, err: null, lastText: null, tab: 'checklist', approving: false, resetting: false, draft: '', busyJob: null, codeDoc: null, installerFor: null };

/** Structured ROADMAP for the preview diagram (GET roadmap): read again only when the ROADMAP text changes, never in parallel. */
const rm = { data: null, error: null, forText: null, loading: false, again: false };
async function loadRoadmapPreview() {
  const text = typeof (pl.data && pl.data.docs.roadmap) === 'string' ? pl.data.docs.roadmap : '';
  if (text === rm.forText) return;
  if (rm.loading) { rm.again = true; return; }
  rm.loading = true; rm.forText = text;
  try {
    rm.data = text.trim() ? await ctx.api.get('roadmap', 15000) : null; rm.error = null;
  } catch (e) { rm.error = e.message; rm.forText = null; } // forText null: the next planning read tries again
  finally {
    rm.loading = false; renderAll();
    if (rm.again) { rm.again = false; loadRoadmapPreview(); }
  }
}

/** Opens the Repositories tab (used right after planning is reopened). */
export function openReposTab() { pl.tab = REPOS_GATE; }

/** True when /state says the project is in planning mode. */
export const planningRequired = () => !!(ctx.state && ctx.state.planning_required);

function normalize(d) {
  const t = (d && d.thread) || {};
  return {
    required: !!(d && d.required),
    thread: { messages: list(t.messages), busy: !!t.busy, job_id: t.job_id || null, session: !!t.session },
    docs: (d && d.docs) || {},
    codebase: (d && d.codebase) || { folder: 'docs/codebase', commit: null, docs: {} },
    gates: list(d && d.gates),
    open_questions: list(d && d.open_questions).filter((q) => q && String(q.text || '').trim()),
    can_approve: !!(d && d.can_approve),
    approved: !!(d && d.approved),
  };
}

// ---------- reading ----------
/** GET planning. Overlapping calls collapse into one more read at the end. */
onSubprojectsChanged(() => { loadPlanning(); });  // a confirm / Not a monorepo / map changes the steps

export async function loadPlanning() {
  if (pl.loading) { pl.again = true; return; }
  pl.loading = true;
  try {
    const d = normalize(await ctx.api.get('planning', 15000));
    pl.data = d; pl.error = null;
    if (planningIsMonorepo(d)) loadSubprojects();
    loadRoadmapPreview(); loadRepoInfo(); loadEnv(); loadImports(); loadReadiness();
    if (d.thread.busy && d.thread.job_id && !pl.job) followJob(d.thread.job_id, startOfLastUser(d));
    // busy without a job id (or between reads): a stable start so the "thinking… N s" timer keeps counting
    pl.busyJob = d.thread.busy ? pl.busyJob || { start: startOfLastUser(d) || Date.now() } : null;
    if (!d.required || d.approved) schedulePoll(0); // the planning is over: /state flips the panel back
  } catch (e) {
    pl.error = e.status === 404 && !/project/i.test(e.message) ? 'The server does not offer planning mode yet.' : e.message;
  } finally {
    pl.loading = false; pl.loadedAt = Date.now();
    renderAll();
    if (pl.again) { pl.again = false; loadPlanning(); }
  }
}
/** The project's repositories and the catalog (read again with every planning read; failures keep the last data). */
let repoLoading = false;
async function loadRepoInfo() {
  if (repoLoading || !ctx.projectId) return;
  repoLoading = true;
  try {
    const [r, c] = await Promise.all([fetchProjectRepos(ctx.projectId), fetchCatalog()]);
    pl.repos = r; pl.catalog = c;
  } catch (_) { /* a back end without repositories: the screen stays as a single-repo project */ }
  finally { repoLoading = false; renderAll(); }
}
/** Repos the planning chat proposed from the catalog (REPOS-SUGGESTED.json, validated by the back end): shown and added by the RepoManager. */
const suggestedCount = () => (pl.repos && Array.isArray(pl.repos.suggested) ? pl.repos.suggested.length : 0);
function startOfLastUser(d) {
  const ms = d.thread.messages;
  for (let i = ms.length - 1; i >= 0; i -= 1) if (ms[i].role === 'user') { const t = Date.parse(ms[i].at); return Number.isFinite(t) ? t : null; }
  return null;
}

// ---------- imported specs (attachments of the conversation) ----------
const imp = { list: [], loading: false, gone: false };
async function loadImports() {
  if (imp.loading || imp.gone) return;
  imp.loading = true;
  try { const r = await ctx.api.get('planning/imports', 10000); imp.list = list(r && r.imports); }
  catch (e) { if (e.status === 404) imp.gone = true; }
  finally { imp.loading = false; renderAll(); }
}
/** Name of an imported spec feature: the feature folder name (the import id without its `YYYYMMDD-HHMMSS-` stamp). */
const importName = (x) => String(x.import_id || '').replace(/^\d{8}-\d{6}-/, '') || x.main || 'spec feature';
/** Dialog content: the folder of a spec feature (tlc-spec-driven: spec.md, design.md, tasks.md), Preview, then the files found
 *  and Confirm (sends them to the AI). A single file or pasted text is not imported. */
function ImportForm({ api }) {
  const [path, setPath] = useState(''), [prev, setPrev] = useState(null), [err, setErr] = useState(''), [busy, setBusy] = useState('');
  // the Windows folder window first (POST projects/pick-folder); the in-app browser only when the system window is unavailable
  const browse = async () => {
    const fallback = () => pickFolder({ title: 'Choose the spec feature folder (the one with spec.md)', buttonText: 'Use this folder', initial: path });
    setBusy('browse'); setErr('');
    let v = null;
    try {
      const r = await appApi.post('projects/pick-folder', {}, NATIVE_DIALOG_TIMEOUT_MS);
      v = r && r.path ? r.path : r && /folder browser/i.test(String(r.reason || '')) ? await fallback() : null; // null = cancelled
    } catch (_) { v = await fallback(); }
    finally { setBusy(''); }
    if (v) { setPath(v); setPrev(null); }
  };
  const preview = async () => {
    if (!path.trim()) { setErr('Type or choose the spec feature folder first.'); return; }
    setBusy('preview'); setErr(''); setPrev(null);
    try { setPrev(await ctx.api.post('planning/import', { path: path.trim() }, 30000)); }
    catch (e) { setErr(e.message); }
    finally { setBusy(''); }
  };
  const confirm = async () => {
    setBusy('send'); setErr('');
    try {
      const r = await ctx.api.post('planning/import/' + encodeURIComponent(prev.import_id) + '/send', {}, 30000);
      api.close();
      pl.err = null; pl.pending = null;
      if (r.job_id) followJob(r.job_id, Date.now()); else loadPlanning();
      loadImports();
    } catch (e) { setErr('Not sent: ' + e.message); setBusy(''); }
  };
  const files = list(prev && prev.files);
  return html`<div class="plan-imp">
    <p class="plan-env-note">Pick the folder of one spec feature (tlc-spec-driven: <code>spec.md</code>, and <code>design.md</code>, <code>tasks.md</code>, <code>context.md</code> when they exist), for example <code>${'.specs/features/<feature>/'}</code>. The AI reads it and writes this project's roadmap, flows and context from it (it still only edits the files in .specs/project). Nothing is sent until you confirm.</p>
    <div class="plan-imp-row"><label class="sr-only" for="plan-imp-path">Spec feature folder</label>
      <input id="plan-imp-path" type="text" class="plan-imp-in" spellcheck="false" placeholder="Spec feature folder (with spec.md)" value=${path} onInput=${(e) => { setPath(e.currentTarget.value); setPrev(null); }} />
      <button type="button" class="btn btn-sm" id="plan-imp-browse" disabled=${!!busy} onClick=${browse}>${busy === 'browse' ? 'Waiting for the window…' : 'Browse folders…'}</button></div>
    ${err ? html`<div class="nl-box nl-bad" role="alert" id="plan-imp-err">${err}</div>` : null}
    ${prev ? html`<div class="plan-imp-prev" id="plan-imp-prev" role="status"><b>Spec feature ${importName(prev)}: ${files.length} file${files.length === 1 ? '' : 's'}</b>
      <ul>${files.map((f, i) => { const n = typeof f === 'string' ? f : f.name || f.path || ''; return html`<li key=${i}><code>${n}</code>${n === prev.main ? html` <span class="plan-imp-main">main</span>` : null}</li>`; })}</ul></div>` : null}
    <div class="plan-imp-act">
      <button type="button" class="btn btn-sm" id="plan-imp-cancel" disabled=${busy === 'send'} onClick=${() => api.close()}>Cancel</button>
      <button type="button" class="btn btn-sm" id="plan-imp-preview" disabled=${!!busy} onClick=${preview}>${busy === 'preview' ? 'Reading…' : 'Preview'}</button>
      <button type="button" class="btn btn-sm btn-primary" id="plan-imp-confirm" disabled=${!prev || !!busy || busyNow()} onClick=${confirm}>${busy === 'send' ? 'Sending…' : 'Confirm and send to the AI'}</button>
    </div></div>`;
}
function openImport() {
  if (busyNow() || pl.sending || inputLock()) { toast(inputLock() || SEND_ERRORS.planning_busy, 'err'); return; }
  const d = openDialog({ title: 'Import a spec feature', width: 640, cls: 'dlg-import', content: html`<${ImportForm} api=${{ close: () => d.close() }} />`, actions: [] });
}
async function resendImport(x) {
  if (busyNow() || pl.sending) { toast(SEND_ERRORS.planning_busy, 'err'); return; }
  try { const r = await ctx.api.post('planning/import/' + encodeURIComponent(x.import_id) + '/send', {}, 30000); if (r.job_id) followJob(r.job_id, Date.now()); loadImports(); }
  catch (e) { toast('Not sent: ' + e.message, 'err'); }
}

// ---------- readiness of the loop (GET planning/readiness; gate id "ready") ----------
const rdy = { data: null, loading: false, running: false, gone: false };
async function loadReadiness() {
  if (rdy.loading || rdy.gone) return;
  rdy.loading = true;
  try { rdy.data = await ctx.api.get('planning/readiness', 10000); }
  catch (e) { if (e.status === 404) rdy.gone = true; }
  finally { rdy.loading = false; renderAll(); }
}
async function runReadiness() {
  if (rdy.running) return;
  rdy.running = true; renderAll();
  try {
    const r = await ctx.api.post('planning/readiness/run', {}, 30000);
    rdy.data = r && r.state ? r : rdy.data;
    for (let i = 0; i < 300 && rdy.data && rdy.data.state === 'running'; i += 1) {
      await new Promise((ok) => setTimeout(ok, 2000));
      rdy.data = await ctx.api.get('planning/readiness', 10000);
      renderAll();
    }
  } catch (e) { toast('Readiness check not run: ' + e.message, 'err'); }
  finally { rdy.running = false; loadPlanning(); }
}
/** Why the "ready" gate is not ok, as one line (null = ok). */
function readyReason() {
  const r = rdy.data;
  if (!r || r.state === 'ready') return null;
  if (r.state === 'problems') return list(r.problems)[0] || 'the readiness check found problems';
  if (r.state === 'running') return 'the readiness check is running';
  if (r.state === 'stale') return 'the plan changed since the readiness check: run it again';
  return 'readiness check did not run: ' + (r.error || 'unknown error');
}
// RD-01: one block per repository about uncommitted changes: {repo, setup_files, other_changes}. The server sends them as items of kind
// `uncommitted` (r.uncommitted, r.items or among the warnings); an old payload only has the three text warnings, which still render as text.
const isUncommitted = (x) => !!x && typeof x === 'object' && (x.kind === 'uncommitted' || Array.isArray(x.setup_files) || Array.isArray(x.other_changes));
function uncommittedBlocks(r) {
  const fromKey = [...list(r.uncommitted), ...list(r.items).filter((x) => x && x.kind === 'uncommitted')];
  const fromWarnings = list(r.warnings).filter(isUncommitted);
  return [...fromKey, ...fromWarnings].filter(isUncommitted);
}
const warningTexts = (r) => list(r.warnings).filter((x) => !isUncommitted(x)).map((x) => String(x));
const fileNames = (items) => { const all = list(items).map(String); return all.slice(0, 12).join(', ') + (all.length > 12 ? ` and ${all.length - 12} more` : ''); };
function UncommittedBlock({ u }) {
  const setup = list(u.setup_files), other = list(u.other_changes);
  if (!setup.length && !other.length) return null;
  return html`<div class="plan-ready-w plan-ready-u" data-repo=${u.repo || ''}>
    <b>WARNING</b> ${u.repo ? html`<span class="mono">${u.repo}</span>: ` : null}uncommitted changes
    ${setup.length ? html`<div>${`${setup.length} setup file${setup.length === 1 ? '' : 's'} not committed: they will be carried into the worktree (or commit them from Workspace).`}
      <div class="mono plan-ready-files">${fileNames(setup)}</div></div>` : null}
    ${other.length ? html`<div>${`${other.length} other changed file${other.length === 1 ? '' : 's'} will NOT be carried.`}
      <div class="mono plan-ready-files">${fileNames(other)}</div></div>` : null}
  </div>`;
}
function ReadyBody() {
  const r = rdy.data;
  if (!r) return null;
  const when = r.at ? fmtHour(r.at) : '';
  const running = r.state === 'running' || rdy.running;
  return html`<div class="plan-ready" id="plan-ready-body">
    ${r.state === 'ready' ? html`<div class="plan-ready-ok"><b>READY</b>${when ? ' · ran at ' + when : ''}</div>` : null}
    ${r.state === 'stale' ? html`<div class="plan-ready-w">The plan changed since the last check${when ? ' (ran at ' + when + ')' : ''}.</div>` : null}
    ${r.state === 'running' ? html`<div class="plan-ready-w">Checking whether the loop can run…</div>` : null}
    ${r.state === 'error' ? html`<div class="plan-ready-p">${'readiness check did not run: ' + (r.error || 'unknown error')}</div>` : null}
    ${list(r.problems).map((x, i) => html`<div class="plan-ready-p" key=${'p' + i}><b>PROBLEM</b> ${String(x)}</div>`)}
    ${uncommittedBlocks(r).map((u, i) => html`<${UncommittedBlock} key=${'u' + i} u=${u} />`)}
    ${warningTexts(r).map((x, i) => html`<div class="plan-ready-w" key=${'w' + i}><b>WARNING</b> ${x}</div>`)}
    <button type="button" class="btn btn-sm" id="plan-ready-run" disabled=${running || busyNow()} onClick=${runReadiness}>${running ? 'Checking…' : 'Run again'}</button>
  </div>`;
}

// ---------- the reply job ----------
function followJob(id, since) {
  if (pl.job && pl.job.id === id) return;
  const job = { id, start: since || Date.now(), timer: null };
  pl.job = job;
  const done = (err) => {
    if (pl.job !== job) return;
    pl.job = null; pl.err = err || null;
    loadPlanning().then(() => { if (pl.job !== job) pl.pending = null; renderAll(); });
    loadWorkspace(); // the Workspace bar shows the repositories (and roles) the reply just applied
    reloadProgress();
  };
  const step = async () => {
    if (pl.job !== job) return;
    if (Date.now() - job.start > JOB_GIVEUP_MS) { done('No reply after 11 minutes: the job was given up on this screen. Reload the page to check again.'); return; }
    try {
      const r = await ctx.api.get('planning/job/' + encodeURIComponent(id), 10000);
      if (pl.job !== job) return;
      if (r.state === 'ok') { done(null); return; }
      if (r.state === 'error') { done(r.error || 'The AI failed while writing the reply.'); return; }
    } catch (e) {
      if (pl.job !== job) return;
      if (e.status === 404) { done('The reply job was not found on the server.'); return; }
    }
    job.timer = setTimeout(step, JOB_POLL_MS);
    renderAll();
  };
  job.timer = setTimeout(step, JOB_POLL_MS);
  renderAll();
}

// ---------- actions ----------
const busyNow = () => !!(pl.job || (pl.data && pl.data.thread.busy));
const failingPrereqs = () => list(pl.data && pl.data.gates).filter((g) => PREREQUISITES.includes(g.id) && !g.ok && !g.optional);
/** Why the chat input is locked (null = it is open). */
function inputLock() {
  if (!pl.data) return pl.error ? 'The plan could not be read.' : 'Reading the plan…';
  const f = failingPrereqs();
  return f.length ? `Finish the prerequisites first: ${f.map((g) => g.label || g.id).join(', ')}.` : null;
}

async function send(text) {
  const lock = inputLock();
  if (lock) { toast(lock, 'err'); return false; }
  if (busyNow() || pl.sending) { toast(SEND_ERRORS.planning_busy, 'err'); return false; }
  if (text.length > MAX_TEXT) { toast(`The message has ${text.length} characters; the limit is ${MAX_TEXT}.`, 'err'); return false; }
  pl.sending = true; pl.err = null; pl.lastText = text; pl.pending = { role: 'user', text, at: new Date().toISOString() };
  renderAll();
  let ok = false;
  try {
    const r = await ctx.api.post('planning/send', { text }, 30000);
    ok = true;
    if (r.job_id) followJob(r.job_id, Date.now()); else loadPlanning();
  } catch (e) {
    const code = e.body && e.body.code;
    pl.pending = null;
    pl.err = SEND_ERRORS[code] || (e.status === 400 ? 'Not sent: ' + e.message : 'Not sent: ' + e.message);
    toast(pl.err, 'err');
    if (code) loadPlanning();
  } finally { pl.sending = false; renderAll(); }
  return ok;
}
async function submit(ev) {
  ev.preventDefault();
  const ta = document.getElementById('plan-text');
  const text = ta ? ta.value.trim() : '';
  if (!text) { if (ta) ta.focus(); return; }
  if (await send(text)) { if (ta) ta.value = ''; pl.draft = ''; renderAll(); }
}
function keyDown(e) {
  if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); const f = document.getElementById('plan-form'); if (f) f.requestSubmit(); }
}
async function resetConversation() {
  if (busyNow() || pl.resetting) return;
  const ok = await confirmDialog({ title: 'Start a new conversation?', text: 'Clears this chat; the next message starts a new AI session. PROJECT.md, ROADMAP.md and FLOWS.md stay as they are.', yesText: 'New conversation', danger: true });
  if (!ok) return;
  pl.resetting = true; renderAll();
  try { await ctx.api.post('planning/reset', { confirm: true }); pl.err = null; pl.pending = null; pl.lastText = null; toast('New conversation: the documents were kept.', 'ok'); }
  catch (e) { toast('Not cleared: ' + e.message, 'err'); }
  finally { pl.resetting = false; loadPlanning(); }
}
/** Approve roadmap (end of planning): confirm, POST roadmap/approve; the back end checks every gate again. */
export async function approvePlan() {
  if (pl.approving) return;
  if (pl.data && !pl.data.can_approve) { toast('Not yet: every required item of the checklist must pass.', 'err'); return; }
  const extraContent = await repoConfirmContent(ctx.api, ctx.projectId);
  const ok = await confirmDialog({ title: 'Approve the roadmap?', text: 'Ends the planning and unlocks the loop for the features and stages in ROADMAP.md now. ' + APPROVAL_NOTICE, yesText: 'Approve roadmap', extraContent });
  if (!ok) return;
  pl.approving = true; renderAll();
  try {
    await ctx.api.post('roadmap/approve', { confirm: true });
    toast('Roadmap approved: the planning is done. You can start a loop now.', 'ok');
    ctx.state = { ...ctx.state, planning_required: false, roadmap_approval: { ...approval(), approved: true, reason: null } };
    schedulePoll(0); reloadProgress();
  } catch (e) {
    if (e.body && Array.isArray(e.body.gates) && pl.data) pl.data = { ...pl.data, gates: e.body.gates, can_approve: false };
    toast('Roadmap not approved: ' + e.message, 'err');
    loadPlanning();
  } finally { pl.approving = false; renderAll(); }
}

// ---------- drawing ----------
const ICON = { ok: '✓', bad: '✗', opt: '◌' };
const STEP_TEXT = {
  git: 'The loop needs a git repository. Run "git init" in the project folder, then come back.',
  structure: 'Create the .specs folder where the loop keeps its state.',
  conformance: 'Say whether this is a STANDARD project (the loop refactors toward the pattern as it goes) or LEGACY (the agents avoid touching what they should not). This is saved in a CLAUDE.md in the project, which you can edit later.',
  map: 'The AI reads the code once and writes 7 short documents in docs/codebase/ at the root of your project. They are committed with the code so the team can reuse them. They are a guide, not the source of truth: the code always wins, and they are updated when something really changes. Required when the repo already has code; a repo without code skips this step.',
};
// what each step writes, said plainly (the planning job is confined to .specs/ and the map job to .specs/ + docs/codebase/ by a hook)
const SAFE_TEXT = {
  git: 'Nothing is written in this step.',
  subprojects: 'Writes .specs/project/SUBPROJECTS.json only. No code is touched.',
  structure: 'Creates the .specs/ folder. It also adds .specs/ to .gitignore and a few lines to CLAUDE.md. No code is touched.',
  conformance: 'Writes one block in CLAUDE.md. No code is touched.',
  map: 'The AI can only write inside docs/codebase/ and .specs/. A hook blocks any other file, and anything changed outside them is undone. Your code is never touched.',
};
const CHAT_SAFE_TEXT = 'The AI only edits PROJECT.md, ROADMAP.md, FLOWS.md, RULES.md and TEST-ENVIRONMENT.md in .specs/project. A hook blocks any other file: your code is never touched.';
const MAP_DOCS = [
  ['STACK.md', 'languages, frameworks, versions and main dependencies'],
  ['ARCHITECTURE.md', 'layers, patterns and how data flows'],
  ['CONVENTIONS.md', 'naming, code style and error handling in use'],
  ['STRUCTURE.md', 'folder layout and where each kind of code lives'],
  ['TESTING.md', 'how tests are written and run'],
  ['INTEGRATIONS.md', 'external APIs, databases, queues and authentication'],
  ['CONCERNS.md', 'tech debt, risks and fragile areas'],
];
const EXAMPLES = [
  ['Credit request', 'Feature: credit request. A customer asks for credit in the portal; finance approves or rejects it; the customer gets an e-mail on every status change. A rejected request must show the reason.'],
  ['E-mail send test', 'Feature: e-mail send test. Send a test e-mail to a list of recipients. Sends go out in batches with a pause between them; if a send fails, stop and alert the owner by e-mail with what failed and what was already sent.'],
];
/** A fill_in line is raw markdown (table row, quote, list item): show it as plain words. */
const tidyFill = (x) => String(x).replace(/`/g, '').replace(/^\s*[>|-]+\s*/, '').replace(/\s*\|\s*$/, '').replace(/\s*\|\s*/g, ' · ');
/** Test environment tab: always there. Status from the optional gate, what it is for, the two safety lists, then the document. */
function LegacyEnvironmentTab({ d }) {
  const g = d.gates.find((x) => x.id === ENV_GATE), doc = d.docs.environment;
  const items = list(g && g.items).filter((x) => x != null && String(x).trim() && !(g.detail && String(x).trim() === String(g.detail).trim()));
  return html`<div class="plan-env">
    ${g ? html`<div class=${'plan-env-st ' + (g.ok ? 'ok' : 'opt')}><span class="plan-gate-ic" aria-hidden="true">${g.ok ? ICON.ok : ICON.opt}</span>
      <div><b>${g.ok ? 'Ready' : 'Optional, not ready yet'}</b>${g.ok ? '' : (/(\d+) FILL_IN/.exec(items.concat(g.detail || '').join(' ')) || [])[1] ? `${/(\d+) FILL_IN/.exec(items.concat(g.detail || '').join(' '))[1]} values to fill in the document.` : 'Fill in the document.'}</div></div>` : null}
    <p class="plan-env-note">Tests run the real app on DEV or localhost. Values stay in <code>.specs/project/.env.test</code>, never in the document or in the AI.</p>
    <div class="plan-env-sec"><h3>TEST-ENVIRONMENT.md</h3>
      ${typeof doc === 'string' && doc.trim() ? html`<div class="md-doc"><${Markdown} source=${doc} /></div>`
        : html`<p>The file is created with your first message in the chat.</p>`}</div>
  </div>`;
}
// ---------- test environment (GET test-environment; an old back end answers 404 and the legacy tab stays) ----------
const SOURCE_TEXT = { project: 'project .env.test', machine: 'this machine', shared: 'shared .env', env: 'environment', missing: 'missing' };
// variable tables: login first (it yields the access token), then headers/tokens, then everything else
const ENV_GROUPS = [['login', 'Login'], ['api', 'API headers and tokens'], ['data', 'Test data']];
function groupOf(name) {
  if (/(^|_)(USER|EMAIL|LOGIN|PASSWORD|PASS)(_|$)/i.test(name) && !/TOKEN|HEADER/i.test(name)) return 'login';
  if (/TOKEN|JWT|BEARER|HEADER|API|KEY|URL|PREFIX/i.test(name)) return 'api';
  return 'data';
}
const env = { data: null, na: false, saving: {}, loading: false, again: false };
/** GET test-environment. 404 (or any failure before the first answer) = the old back end: the legacy tab is used. */
async function loadEnv() {
  if (env.gone) return; // the back end has no such route: asked once, not on every poll (a page reload asks again)
  if (env.loading) { env.again = true; return; }
  env.loading = true;
  try {
    const r = await ctx.api.get('test-environment', 10000);
    env.data = r && r.ok !== false ? r : null; env.na = !env.data;
  } catch (e) { if (e.status === 404) env.gone = true; if (e.status === 404 || !env.data) env.na = true; }
  finally { env.loading = false; renderAll(); if (env.again) { env.again = false; loadEnv(); } }
}
async function saveEnvValue(name) {
  const input = document.getElementById('env-in-' + name);
  const value = input ? input.value : '';
  if (!value.trim() || env.saving[name]) { if (input) input.focus(); return; }
  env.saving[name] = true; renderAll();
  try {
    const tg = document.getElementById('env-tg-' + name);
    await ctx.api.put('test-environment/values', { values: { [name]: value }, target: tg && tg.value === 'shared' ? 'shared' : 'project' }, 15000);
    toast(name + ' saved.', 'ok');
  } catch (e) { toast('Not saved: ' + e.message, 'err'); }
  finally {
    const el = document.getElementById('env-in-' + name); if (el) el.value = ''; // the value never stays in the page
    delete env.saving[name]; loadEnv(); loadPlanning();
  }
}
const LAYER_ORDER = { project: 0, machine: 1, shared: 2 };
/** Access card: where credentials come from (three layers, all always read) and which read tools the AI has (MCP). */
function AccessCard({ access }) {
  const layers = list(access && access.credential_layers).slice().sort((a, b) => (LAYER_ORDER[a.name] ?? 9) - (LAYER_ORDER[b.name] ?? 9));
  const mcp = list(access && access.mcp);
  return html`<div class="plan-env-sec plan-access" id="plan-access"><h3>Access</h3>
    <p class="plan-env-note">Credentials are read from three places, in this order: the project <code>.env.test</code> first, then this machine's <code>~/.loop-harness/.env</code>, then the shared <code>.env</code>. All three are always read; when a name is in more than one, the first one wins. Values are never shown.</p>
    <ol class="plan-access-layers" aria-label="Credential layers">${layers.map((l) => html`<li key=${l.name} class=${'plan-access-layer' + (l.exists ? '' : ' none')} data-layer=${l.name}>
      <div><b>${l.label || l.name}</b> <span class=${l.exists ? 'plan-env-yes' : 'plan-env-no'}>${l.exists ? 'found' : 'file not found'}</span></div>
      <div class="plan-env-note mono" title=${l.path}>${l.path}</div>
      ${list(l.names).length ? html`<div class="plan-access-names">${list(l.names).map((n) => html`<code key=${n.name} class=${n.winning ? 'plan-access-win' : 'plan-access-lose'} title=${n.winning ? 'this value is the one used' : 'another layer wins for this name'}>${n.name}${n.winning ? ' ✓' : ''}</code>`)}</div>`
        : html`<div class="plan-env-note">No names from this file.</div>`}</li>`)}</ol>
    <p class="plan-env-note">✓ = the value that is used.</p>
    <h4 class="plan-access-h">Read access through MCP</h4>
    <ul class="plan-access-mcp" aria-label="MCP read access">${mcp.map((m) => html`<li key=${m.server} data-server=${m.server}><b>${m.server}</b> ${m.configured ? html`<span class="plan-access-tools">${list(m.tools).join(', ') || 'read tools'}</span>` : html`<span class="plan-env-note">not configured on this machine</span>`}</li>`)}</ul>
    <p class="plan-env-note">MySQL is read only for the AI: any change goes out as a <code>.sql</code> script for a person to run.</p></div>`;
}
/** Test environment tab: one status line, the credentials table (names only, never values), the "never do" list, the Access card and the full document folded. */
function EnvironmentTab({ d }) {
  const e = env.data;
  if (!e) return html`<${LegacyEnvironmentTab} d=${d} />`;
  const doc = d.docs.environment, names = list(e.names), fill = list(e.fill_in).filter((x) => x != null && String(x).trim());
  const missing = names.filter((n) => !n.set).length;
  const ready = !!e.ready && !missing && !fill.length;
  const why = [missing ? `${missing} ${missing === 1 ? 'name is' : 'names are'} missing` : '', fill.length ? `${fill.length} ${fill.length === 1 ? 'field' : 'fields'} to complete in the document` : ''].filter(Boolean);
  const status = ready ? 'Ready: the loop can run the real app to test.' : why.length ? 'Not ready: ' + why.join('; ') + '.' : 'Not ready yet.';
  // one small table per kind of value, only the kinds that have names
  const groups = ENV_GROUPS.map(([id, title]) => [id, title, names.filter((n) => groupOf(n.name) === id)]).filter(([, , rows]) => rows.length);
  const row = (n) => html`<tr key=${n.name}>
    <td class="plan-env-name"><code>${n.name}</code>
      ${n.set ? null : html`<div class="plan-env-add"><label class="sr-only" for=${'env-tg-' + n.name}>Save ${n.name} to</label>
        <select id=${'env-tg-' + n.name} class="plan-env-tg" disabled=${!!env.saving[n.name]}><option value="project">Save to: This project</option><option value="shared">Save to: Shared (all projects)</option></select>
        <label class="sr-only" for=${'env-in-' + n.name}>Value for ${n.name}</label>
        <input id=${'env-in-' + n.name} type=${n.secret ? 'password' : 'text'} autocomplete="off" spellcheck="false" placeholder="Value" disabled=${!!env.saving[n.name]}
          onKeyDown=${(ev) => { if (ev.key === 'Enter') { ev.preventDefault(); saveEnvValue(n.name); } }} />
        <button type="button" class="btn btn-sm" disabled=${!!env.saving[n.name]} onClick=${() => saveEnvValue(n.name)}>${env.saving[n.name] ? 'Saving…' : 'Save'}</button></div>`}</td>
    <td>${SOURCE_TEXT[n.source] || n.source}${n.path_label && n.path_label !== SOURCE_TEXT[n.source] ? html`<div class="plan-env-note">${n.path_label}</div>` : null}</td>
    <td class=${n.set ? 'plan-env-yes' : 'plan-env-no'}><span aria-hidden="true">${n.set ? ICON.ok : ICON.bad}</span><span class="sr-only">${n.set ? 'Set' : 'Not set'}</span></td></tr>`;
  return html`<div class="plan-env plan-env2">
    <div class=${'plan-env-st ' + (ready ? 'ok' : 'opt')} role="status" aria-live="polite"><span class="plan-gate-ic" aria-hidden="true">${ready ? ICON.ok : ICON.opt}</span>
      <div><b>${status}</b>DEV and localhost only. Values stay in <code>.specs/project/.env.test</code>.</div></div>
    ${groups.length ? groups.map(([id, title, rows]) => html`<div class="plan-env-sec" key=${id}><h3 id=${'env-h-' + id}>${title}</h3>
      <table class="plan-env-tbl" aria-labelledby=${'env-h-' + id}><thead><tr><th scope="col">Name</th><th scope="col">From</th><th scope="col">Set</th></tr></thead><tbody>${rows.map(row)}</tbody></table></div>`)
      : html`<p class="plan-env-note">No variables listed yet. Ask the AI in the chat.</p>`}
    <${AccessCard} access=${e.access} />
    <div class="plan-env-sec"><h3>Document</h3>
      ${fill.length ? html`<p>${fill.length} ${fill.length === 1 ? 'field' : 'fields'} to complete:</p><ul>${fill.map((x, i) => html`<li key=${i}>${tidyFill(x)}</li>`)}</ul>` : null}
      <details class="plan-env-doc"><summary>Show the full document</summary>
        ${typeof doc === 'string' && doc.trim() ? html`<div class="md-doc"><${Markdown} source=${doc} /></div>` : html`<p>The file is created with the first message you send in the chat.</p>`}</details></div>
  </div>`;
}
/** Display only: hides the "Open questions" section (own tab) from the rendered ROADMAP; the file and the payload are untouched. */
function withoutOpenQuestions(text) {
  if (typeof text !== 'string') return text;
  return text.replace(/^##[ \t]+Open questions[ \t]*\r?\n[\s\S]*?(?=^##[ \t]|(?![\s\S]))/im, '');
}
/** Pre-fills the chat textarea (never sends) and focuses it with the caret at the end. */
function fillChat(text) {
  const ta = document.getElementById('plan-text');
  pl.draft = text;
  if (!ta) return;
  ta.value = text; ta.focus(); ta.setSelectionRange(text.length, text.length);
}
const quoteQuestion = (q) => '> ' + String(q.text).split('\n').join('\n> ');
/** Open questions tab: every open question of the ROADMAP in full, to answer in the chat. */
function QuestionsTab({ d }) {
  const qs = list(d.open_questions);
  if (!qs.length) return html`<div class="plan-oq"><p class="plan-oq-none">No open questions.</p></div>`;
  return html`<div class="plan-oq">
    <p class="plan-rm-cap">Each answer goes through the chat: the AI updates the ROADMAP and the question leaves this list. Nothing is sent until you press Send.</p>
    <ol class="plan-oq-list" aria-label="Open questions">${qs.map((q, i) => html`<li key=${q.id || i} class="plan-oq-card">
      <div class="plan-oq-n">Question ${i + 1}</div>
      <p class="plan-oq-q">${q.text}</p>
      ${q.recommended ? html`<div class="plan-oq-rec"><span class="plan-oq-rec-l">Recommended</span><p>${q.recommended}</p></div>` : null}
      <div class="plan-oq-act">
        <button type="button" class="btn btn-sm" onClick=${() => fillChat(quoteQuestion(q) + '\n\nAnswer: ')}>Answer in chat</button>
        ${q.recommended ? html`<button type="button" class="btn btn-sm btn-primary" onClick=${() => fillChat('For: ' + q.text + ' -> use the recommendation: ' + q.recommended)}>Use recommendation</button>` : null}
      </div></li>`)}</ol>
  </div>`;
}
/** Codebase tab: the documents the map wrote in docs/codebase, one at a time (a guide, not the source of truth). */
function CodebaseTab({ d }) {
  const cb = d.codebase || {}, docs = cb.docs || {}, names = Object.keys(docs);
  const cur = names.includes(pl.codeDoc) ? pl.codeDoc : names[0];
  const what = Object.fromEntries(MAP_DOCS);
  return html`<div class="plan-code">
    <p class="plan-rm-cap">Mapped from the existing code${cb.commit ? ' at ' + cb.commit : ''}, in <code>${cb.folder || 'docs/codebase'}/</code>. A guide, not the source of truth: the code wins.</p>
    <div class="seg plan-code-pick" role="tablist" aria-label="Codebase documents">${names.map((n) => html`<button key=${n} type="button" role="tab" class="seg-btn" aria-selected=${String(n === cur)}
      title=${what[n] || n} onClick=${() => { pl.codeDoc = n; renderAll(); }}>${n.replace(/\.md$/i, '').toLowerCase().replace(/^./, (c) => c.toUpperCase())}</button>`)}</div>
    ${what[cur] ? html`<p class="plan-rm-note">${what[cur]}</p>` : null}
    <div class="md-doc"><${Markdown} source=${docs[cur]} /></div>
  </div>`;
}
/** Giant files (spec GIANT-05): the count the gate items carry, or for a monorepo map gate the sum over the table rows. */
function giantNote(g, items) {
  let count = 0;
  const data = g.id === 'map' ? subprojectsData() : null;
  if (data && data.confirmed) count = list(data.projects).reduce((n, row) => n + (Number(row.giant_files) || 0), 0);
  else count = items.filter((x) => /giant/i.test(String(x))).length;
  return count ? html`<div class="plan-gate-d" data-giant=${count}>${`${count} giant file${count === 1 ? '' : 's'} (over the size limit): a feature that touches one needs a split or an accepted note.`}</div>` : null;
}
/** One checklist row; the (long) list of items stays folded. */
function Gate({ g }) {
  const kind = g.ok ? 'ok' : g.optional ? 'opt' : 'bad';
  const items = list(g.items).filter((x) => x != null && String(x).trim());
  return html`<li class=${'plan-gate ' + kind} id=${'plan-gate-' + g.id}>
    <span class="plan-gate-ic" aria-hidden="true">${ICON[kind]}</span>
    <div class="plan-gate-b">
      <div class="plan-gate-t">${g.id === ENV_GATE ? html`<button type="button" class="plan-gate-link" title="Open the Test environment tab" onClick=${() => { pl.tab = ENV_GATE; renderAll(); }}>${g.label || g.id}</button>`
        : g.id === REPOS_GATE && !g.ok ? html`<button type="button" class="plan-gate-link" title="Open the Repositories tab" onClick=${() => { pl.tab = REPOS_GATE; renderAll(); }}>${g.label || g.id}</button>`
        : g.id === QUESTIONS_GATE && list(pl.data && pl.data.open_questions).length ? html`<button type="button" class="plan-gate-link" title="Open the Open questions tab" onClick=${() => { pl.tab = QUESTIONS_GATE; renderAll(); }}>${g.label || g.id}</button>` : (g.label || g.id)}<span class="sr-only">${g.ok ? ' (done)' : g.optional ? ' (optional)' : ' (pending)'}</span>
        ${g.optional && !g.ok ? html`<span class="plan-opt">optional</span>` : null}</div>
      ${g.id === READY_GATE && rdy.data ? html`<${ReadyBody} />` : g.detail ? html`<div class="plan-gate-d">${g.detail}</div>` : null}
      ${giantNote(g, items)}
      ${g.id === READY_GATE && rdy.data ? null : items.length ? html`<details class="plan-gate-more"><summary>${items.length} item${items.length === 1 ? '' : 's'}</summary>
        <ul class="plan-gate-items">${items.map((x, i) => html`<li key=${i}>${String(x)}</li>`)}</ul></details>` : null}
    </div>
  </li>`;
}
const planGates = (d) => d.gates.filter((g) => !PREREQUISITES.includes(g.id));
const requiredPrereqs = (d) => d.gates.filter((g) => PREREQUISITES.includes(g.id) && !g.optional);

/** Extra body of the current prerequisite step, by gate id: renderer(cur, written) returns a vnode (or null).
    A monorepo registers its own renderers for `subprojects`, `conformance` and `map` through registerPrereqRenderer. */
const PREREQ_RENDERERS = {
  map: (cur, written) => [
    html`<${MapStatus} written=${written.size} total=${MAP_DOCS.length} />`,
    html`<ul class="plan-map-docs" aria-label="Documents the map writes in docs/codebase (a guide, not the source of truth)">${MAP_DOCS.map(([name, what]) => html`<li key=${name} class=${written.has(name) ? 'ok' : ''}><span aria-hidden="true">${written.has(name) ? '✓' : '·'}</span><b>${name}</b> ${what}</li>`)}</ul>`,
  ],
};
export function registerPrereqRenderer(gateId, renderer) { PREREQ_RENDERERS[gateId] = renderer; }
const renderPrereqBody = (cur, written) => { const render = PREREQ_RENDERERS[cur.id]; return render ? render(cur, written) : null; };

/** Monorepo: the three steps (subprojects, conformance, maps) are ONE installer card. It stays on screen after the last map
 *  until the user presses "Continue to planning", even though the gates already pass (installerFor = the project it is held for). */
const installerHeld = () => pl.installerFor === ctx.projectId;
function continueToPlanning() { pl.installerFor = null; loadPlanning(); loadSubprojects(); renderAll(); }
function InstallerCard({ d, done }) {
  const gateOk = (id) => { const g = d.gates.find((x) => x.id === id); return !g || g.ok || !!g.optional; };
  const gates = { subprojects: gateOk('subprojects'), conformance: gateOk('conformance'), map: gateOk('map') };
  return html`<div class="plan-wizard">
    <div class="plan-step plan-step-installer" id="plan-gate-installer">
      <h2 class="plan-step-t">Set up the subprojects</h2>
      <${SubprojectsInstallerPanel} gates=${gates} onContinue=${continueToPlanning} />
    </div>
    ${done.length ? html`<ul class="plan-done" aria-label="Done">${done.map((g) => html`<li key=${g.id}><span aria-hidden="true">✓</span> ${g.label || g.id}</li>`)}</ul>` : null}
  </div>`;
}

/** Single repository: conformance and map are ONE installer card too (same look, 2 steps). git and .specs structure stay the cards before it. */
function ProjectInstallerCard({ d, done }) {
  const gate = (id) => d.gates.find((x) => x.id === id) || null;
  return html`<div class="plan-wizard">
    <div class="plan-step plan-step-installer" id="plan-gate-installer">
      <h2 class="plan-step-t">Set up the project</h2>
      <${ProjectInstallerPanel} conformanceGate=${gate('conformance')} mapGate=${gate('map')} reloadPlanning=${loadPlanning} onContinue=${continueToPlanning} />
    </div>
    ${done.length ? html`<ul class="plan-done" aria-label="Done">${done.map((g) => html`<li key=${g.id}><span aria-hidden="true">✓</span> ${g.label || g.id}</li>`)}</ul>` : null}
  </div>`;
}

/** Stage 1: one prerequisite at a time (the chat only opens when all of them pass). */
function Prerequisites({ d }) {
  const req = requiredPrereqs(d), done = req.filter((g) => g.ok), cur = req.find((g) => !g.ok);
  if (planningIsMonorepo(d) && ((cur && SUBPROJECT_GATES.includes(cur.id)) || installerHeld())) {
    pl.installerFor = ctx.projectId;
    return html`<${InstallerCard} d=${d} done=${done.filter((g) => !SUBPROJECT_GATES.includes(g.id))} />`;
  }
  if (!planningIsMonorepo(d) && ((cur && INSTALLER_GATES.includes(cur.id)) || installerHeld())) {
    pl.installerFor = ctx.projectId;
    return html`<${ProjectInstallerCard} d=${d} done=${done.filter((g) => !INSTALLER_GATES.includes(g.id))} />`;
  }
  if (!cur) return null;
  const act = prereqActionFor(d, cur.id), running = !!(act && act.busy && act.busy());
  const written = new Set(list(cur.items).map((x) => String(x).split(':')[0]));
  return html`<div class="plan-wizard">
    <div class="plan-step" id=${'plan-gate-' + cur.id}>
      <div class="plan-step-n">${`Step ${done.length + 1} of ${req.length}`}</div>
      <div class="plan-bar" aria-hidden="true"><i style=${{ width: Math.round(100 * done.length / req.length) + '%' }}></i></div>
      <h2 class="plan-step-t">${cur.label || cur.id}</h2>
      <p class="plan-step-d">${STEP_TEXT[cur.id] || cur.detail || ''}</p>
      ${act ? html`<button type="button" class=${'btn ' + (running ? 'btn-pending' : 'btn-primary')} disabled=${running} onClick=${act.run}>${running ? act.busyText : act.text}</button>` : null}
      ${renderPrereqBody(cur, written)}
      <p class="plan-safe">${SAFE_TEXT[cur.id] || ''}</p>
    </div>
    ${done.length ? html`<ul class="plan-done" aria-label="Done">${done.map((g) => html`<li key=${g.id}><span aria-hidden="true">✓</span> ${g.label || g.id}</li>`)}</ul>` : null}
  </div>`;
}
function Approve({ d }) {
  const open = planGates(d).filter((g) => !g.ok && !g.optional), missing = open.length;
  const first = open[0], why = first ? (first.id === READY_GATE ? readyReason() || first.detail : first.detail) || first.label || first.id : '';
  const reason = d.can_approve ? '' : `${first && first.id === READY_GATE ? 'Not ready to run the loop: ' : 'Open: ' + (first && first.label ? first.label + ': ' : '')}${why}`.trim();
  return html`<div class="plan-approve">
    ${d.approved ? html`<div class="nl-box nl-ok"><span class="chip">ROADMAP APPROVED</span></div>` : null}
    <button type="button" class="btn btn-primary" id="plan-approve" disabled=${!d.can_approve || pl.approving || d.approved} title=${reason}
      onClick=${approvePlan}>${pl.approving ? 'Approving…' : 'Approve roadmap'}</button>
    <div class="hint" id="plan-approve-hint">${d.can_approve ? 'Everything required passes.'
      : `${missing} required item${missing === 1 ? '' : 's'} still open.${reason ? ' ' + reason : ''}`}</div>
  </div>`;
}
function Notice() {
  return html`<div class="plan-wizard"><div class=${pl.error ? 'nl-box nl-bad' : 'hint'} role=${pl.error ? 'alert' : null}>${pl.error ? 'Planning unavailable: ' + pl.error : 'reading the plan…'}
    ${pl.error ? html`<div><button type="button" class="btn btn-sm" onClick=${() => loadPlanning()}>Try again</button></div>` : null}</div></div>`;
}

function Msg({ m }) {
  const ai = m.role === 'ai';
  const meta = [fmtHour(m.at), ai && isNum(m.cost_usd) ? cost(m.cost_usd) : ''].filter(Boolean).join(' · ');
  return html`<div class=${'cmsg ' + (ai ? 'ai' : 'user') + (m.pending ? ' plan-pending' : '')}>
    <div class="cmsg-h"><span>${ai ? 'AI · writes the plan' : 'you'}</span><span class="t">${meta}</span></div>
    ${ai ? html`<div class="cmsg-b md"><${Markdown} source=${m.text == null ? '' : String(m.text)} /></div>` : html`<div class="cmsg-b">${String(m.text == null ? '' : m.text)}</div>`}
  </div>`;
}
function messagesNow() {
  const ms = list(pl.data && pl.data.thread.messages);
  const p = pl.pending, lastUser = [...ms].reverse().find((m) => m.role === 'user');
  if (!p || (lastUser && lastUser.text === p.text)) return ms; // the server already has it
  return [...ms, { ...p, pending: true }];
}
function ChatCol() {
  const listRef = useRef(null), taRef = useRef(null), stick = useRef(true), seen = useRef(0);
  const ms = messagesNow(), busy = busyNow(), lock = inputLock();
  useLayoutEffect(() => { if (taRef.current && pl.draft) taRef.current.value = pl.draft; }, []);
  // new messages follow only if the reader was at the bottom (or just sent)
  useLayoutEffect(() => {
    const el = listRef.current, n = ms.length, prev = seen.current;
    seen.current = n;
    if (el && n > prev && (stick.current || !prev || ms[n - 1].role === 'user')) el.scrollTop = el.scrollHeight;
  });
  const onScroll = (e) => { const el = e.currentTarget; stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 60; };
  const job = pl.job || pl.busyJob;
  const useExample = (text) => { const ta = taRef.current; if (!ta) return; ta.value = text; pl.draft = text; ta.focus(); };
  return html`<section class="plan-col plan-chat" aria-label="Planning chat">
    <div class="plan-h"><span class="plan-title">Plan with the AI</span><span class="spacer"></span>
      <button type="button" class="btn btn-sm btn-ghost" id="plan-import" disabled=${busy || pl.sending || pl.resetting || !!lock}
        title="send the folder of an existing spec feature (spec.md, design.md, tasks.md) to the AI" onClick=${openImport}>Import a spec feature</button>
      <button type="button" class="btn btn-sm btn-ghost" id="plan-reset" disabled=${busy || pl.sending || pl.resetting || !ms.length}
        title="clears the chat; the documents stay" onClick=${resetConversation}>New conversation</button></div>
    <div class="ch-empty plan-empty" hidden=${!!ms.length}>${ms.length ? '' : html`<p class="plan-empty-t">Describe what you want to build, one feature at a time.</p>
      <p>For each feature say what it does, who uses it, what can go wrong and what you will be able to see when it works. The AI turns that into the roadmap (ROADMAP.md) and the flows (FLOWS.md), fills the short project context for the subagents (PROJECT.md) and records what must never be done in RULES.md, then asks what is still missing. Examples (click to use as a starting point):</p>
      <div class="plan-examples">${EXAMPLES.map(([label, text]) => html`<button key=${label} type="button" class="plan-example" disabled=${!!lock} onClick=${() => useExample(text)}><strong>${label}</strong><span>${text}</span></button>`)}</div>`}</div>
    <div class="ch-list plan-list" role="log" aria-live="polite" ref=${listRef} onScroll=${onScroll}>${ms.map((m, i) => html`<${Msg} key=${i} m=${m} />`)}</div>
    ${imp.list.length ? html`<ul class="plan-imports" id="plan-imports" aria-label="Imported specs">${imp.list.map((x) => html`<li key=${x.import_id} class="plan-import">
      <span aria-hidden="true">📎</span> <b title=${list(x.files).map((f) => (typeof f === 'string' ? f : f.name || f.path)).join(', ')}>${importName(x)}</b>
      <span class="plan-env-note">${list(x.files).length} file${list(x.files).length === 1 ? '' : 's'}${x.at ? ' · imported ' + fmtHour(x.at) : ''}${x.sent_at ? ' · sent ' + fmtHour(x.sent_at) : ' · not sent yet'}</span>
      ${x.sent_at ? null : html`<button type="button" class="btn btn-sm" disabled=${busy || pl.sending} onClick=${() => resendImport(x)}>Send</button>`}</li>`)}</ul>` : null}
    <${Think} job=${busy ? job : null} />
    <div class="ch-err" role="alert" hidden=${!pl.err}>${pl.err ? [html`<span key="t">${pl.err}</span>`,
      pl.lastText && !busy ? html`<button key="r" type="button" class="btn btn-sm" onClick=${() => send(pl.lastText)}>Send again</button>` : null,
      html`<button key="x" type="button" class="btn btn-sm btn-ghost" onClick=${() => { pl.err = null; renderAll(); }}>Dismiss</button>`] : null}</div>
    <form class="plan-form" id="plan-form" aria-label="Message to the planning AI" onSubmit=${submit}>
      <div class="plan-lock" role="status" hidden=${!lock}>${lock || ''}</div>
      <div class="cp-box plan-box">
        <textarea id="plan-text" ref=${taRef} rows="3" aria-label="Message" disabled=${!!lock} maxlength=${MAX_TEXT}
          placeholder=${lock ? '' : 'Answer the questions or describe what the project needs… (Enter sends, Shift+Enter new line)'}
          onInput=${(e) => { pl.draft = e.currentTarget.value; }} onKeyDown=${keyDown}></textarea>
        <button type="submit" class="btn btn-primary" id="plan-send" disabled=${!!lock || busy || pl.sending}>${busy ? 'Writing…' : 'Send'}</button>
      </div>
      <p class="plan-safe">${CHAT_SAFE_TEXT}</p>
    </form>
  </section>`;
}

/** Top of the ROADMAP tab: the diagram, what it is, and the regression-proof note. */
function RoadmapPreviewBlock() {
  const e2e = rm.data ? !!rm.data.e2e : null;
  return html`<div class="plan-rm-sec">
    ${e2e == null ? null : html`<p class="plan-rm-cap"><b>E2E: ${e2e ? 'yes' : 'no'}</b>. ${e2e ? 'The loop runs the real app to validate the flows.' : 'The loop validates by build and tests only.'} Set in the ROADMAP: ask the chat to change it.</p>`}
    <${RoadmapPreview} rm=${rm} />
    <p class="plan-rm-cap">Preview: how the loop will see this roadmap (features by stage, arrows = dependencies). Drag to move, Ctrl + scroll to zoom, click a feature for its details. The real graph appears when the loop runs.</p>
    <p class="plan-rm-note">Refactoring features can ask for a regression proof: the loop captures how the flow behaves before the change and compares after. Mark the feature <code>regression=yes</code> in the ROADMAP (only for slugs that start with <code>refactor-</code>). It needs a flow in FLOWS.md and a test environment (credential names; values in <code>.specs/project/.env.test</code> or the machine's <code>~/.loop-harness/.env</code>). Without them the loop stops and sends an e-mail saying it cannot test.</p>
  </div>`;
}

/** Roadmap tab, multi-repo projects only: which repositories each feature writes to (from paths=<repo>:<path>). */
function RepoMapBlock() {
  const rows = pl.repos && (pl.repos.multi || null) && rm.data && !list(rm.data.errors).length ? featureRepoRows(rm.data.features, pl.repos.repos) : [];
  if (!rows.length) return null;
  return html`<div class="plan-rm-sec"><${RepoConfirm} rows=${rows} /></div>`;
}
/** Repositories tab: the project's repositories; the suggestions from the catalog are part of the RepoManager. */
function RepoTab() {
  return html`<div class="plan-repos">
    <${RepoManager} key=${pl.repoVer} projectId=${ctx.projectId} locked=${ctx.state && ctx.state.loop && ctx.state.loop.status === 'running' ? 'A loop is running: the repository list is locked until it stops.' : ''} onChange=${() => loadRepoInfo()} />
  </div>`;
}

function SidePanel() {
  const d = pl.data, docs = d.docs || {};
  // prerequisites included (the whole picture); done first, then what is still open, optional-not-ready last (stable inside each group)
  const rank = (g) => (g.ok ? 0 : g.optional ? 2 : 1);
  const gates = d.gates.map((g, i) => ({ g, i })).sort((a, b) => rank(a.g) - rank(b.g) || a.i - b.i).map((x) => x.g);
  const okCount = gates.filter((g) => g.ok || g.optional).length;
  const filled = DOCS.filter(([k]) => k !== 'environment' && typeof docs[k] === 'string' && docs[k].trim());
  const envGate = gates.find((g) => g.id === ENV_GATE);
  const mapped = Object.keys((d.codebase && d.codebase.docs) || {}).length;
  const openQuestions = list(d.open_questions).length;
  const tabs = [['checklist', `Checklist ${okCount}/${gates.length}`, 'Checklist before approving'],
    ...(openQuestions ? [[QUESTIONS_GATE, `Open questions ${openQuestions}`, 'Questions the ROADMAP still has open: answer them in the chat']] : []),
    ...(mapped ? [['codebase', `Codebase ${mapped}`, 'What the map found in the existing code: docs/codebase']] : []), ...filled.map(([k, label, file]) => [k, label, '.specs/project/' + file]),
    ...(pl.repos && (pl.repos.multi || suggestedCount()) ? [['repos', 'Repositories' + (suggestedCount() ? ` ${suggestedCount()} suggested` : ''), 'Which repositories take part in this project']] : []),
    [ENV_GATE, 'Test environment' + (envGate && envGate.ok ? ' ✓' : ''), 'How the loop runs the real app to test: .specs/project/TEST-ENVIRONMENT.md']];
  const cur = tabs.find(([k]) => k === pl.tab) || tabs[0];
  const pick = (k) => { pl.tab = k; renderAll(); };
  const onKey = (e) => {
    const i = tabs.findIndex(([k]) => k === cur[0]);
    const j = e.key === 'ArrowRight' ? (i + 1) % tabs.length : e.key === 'ArrowLeft' ? (i - 1 + tabs.length) % tabs.length : e.key === 'Home' ? 0 : e.key === 'End' ? tabs.length - 1 : null;
    if (j == null) return;
    e.preventDefault(); pick(tabs[j][0]);
    const b = document.getElementById('plan-tab-' + tabs[j][0]); if (b) b.focus();
  };
  return html`<aside class="plan-col plan-docs" aria-label="Plan status and documents">
    <div class="plan-h"><div class="seg" role="tablist" aria-label="Plan" onKeyDown=${onKey}>${tabs.map(([k, label, title]) => html`<button key=${k} type="button" role="tab"
      id=${'plan-tab-' + k} class="seg-btn" aria-selected=${String(cur[0] === k)} aria-controls="plan-doc" tabIndex=${cur[0] === k ? 0 : -1} title=${title}
      onClick=${() => pick(k)}>${label}</button>`)}</div></div>
    <div class="plan-scroll" id="plan-doc" role="tabpanel" aria-labelledby=${'plan-tab-' + cur[0]}>
      ${cur[0] === 'checklist' ? html`<ul class="plan-gates" aria-label="Gates before approving">${gates.map((g) => html`<${Gate} key=${g.id} g=${g} />`)}</ul>
        ${filled.length ? null : html`<div class="hint">The documents show up here as the AI writes them.</div>`}`
        : cur[0] === 'codebase' ? html`<${CodebaseTab} d=${d} />`
        : cur[0] === 'repos' ? html`<${RepoTab} />`
        : cur[0] === ENV_GATE ? html`<${EnvironmentTab} d=${d} />`
        : cur[0] === QUESTIONS_GATE ? html`<${QuestionsTab} d=${d} />`
        : html`${cur[0] === 'roadmap' ? html`<${RoadmapPreviewBlock} /><${RepoMapBlock} />` : null}<div class="md-doc"><${Markdown} source=${cur[0] === 'roadmap' ? withoutOpenQuestions(docs[cur[0]]) : docs[cur[0]]} /></div>`}
    </div>
    <${Approve} d=${d} />
  </aside>`;
}

/** The whole planning screen (replaces the loop panel while state.planning_required is true). */
export function PlanningScreen() {
  // reads again when the trail progress changes (structure, conformance, map just done) and every few seconds
  useEffect(() => { loadPlanning(); }, [ctx.progress]);
  useEffect(() => {
    const t = setInterval(() => {
      if (!pl.job && document.visibilityState === 'visible' && Date.now() - pl.loadedAt > REFRESH_MS - 500) loadPlanning();
    }, REFRESH_MS);
    return () => clearInterval(t);
  }, []);
  const d = pl.data, stage1 = d && (failingPrereqs().length > 0 || installerHeld());
  return html`<div class="plan">
    ${!d ? html`<${Notice} />` : stage1 ? html`<${Prerequisites} d=${d} />`
      : html`<div class="plan-grid"><${SidePanel} /><${ChatCol} /></div>`}
  </div>`;
}
