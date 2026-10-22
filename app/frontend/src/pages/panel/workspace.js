/* Workspace line (under the header): ONE line, "N repositories · edit". The repositories are defined BEFORE the loop, outside the roadmap/loop
   screen, in the repository manager (dialog opened by "Repositories"/"Define repositories": by-hand list with a role per row; the planning chat
   fills the list; by hand only in the Workspace dialog). Every repository shows an `edit` or `reference` chip (GET workspace per repo: role, reason,
   changed_by_feature; a missing role reads as edit) and the last rejected suggestions of the chat show as warning chips
   (d.rejected_suggestions: [{path, reason}], read tolerantly). The worktrees are created by the loop itself when it starts (idempotent), so there is no manual create button; the
   details (rows, steps, Close loop) sit behind "details" and open by themselves only while a creation runs or failed.
   - GET workspace: per repo the folder, the worktree (existing or planned), loop branch, base branch and a state; the plain-language
     explanation and the warnings are shown BEFORE anything is created.
   - The steps of the creation at Start (GET workspace/job/<id>, append-only; the job comes back in GET workspace) are drawn live, so
     creation is never silent.
   - A reference repository shows "read only - no worktree" and is left out of every worktree / uncommitted list. The legacy
     commit-push route only acts on a loop worktree (the user's folder is saved with Commit setup files and push), so no button calls it.
   - "Change the roadmap" (inside the details) reopens planning (panel/reopen_planning.js).
   - Close loop: GET close-loop/check (reasons per repo while disabled), POST close-loop (per-repo result).
   The back end decides every state and refusal; this screen only shows them. */
import { html, useEffect, useLayoutEffect, useRef } from '../../util/preact.js';
import { list, txt, httpUrl, midPath } from '../../util/format.js';
import { readStored, writeStored } from '../../util/storage.js';
import { toast } from '../../components/toast.js';
import { confirmDialog } from '../../components/dialog.js';
import { ctx, status } from './context.js';
import { renderAll, schedulePoll } from '../project_panel.js';
import { reloadProgress } from './progress_trail.js';
import { openReopenPlanning } from './reopen_planning.js';
import { ROLE_TEXT } from '../repo_scope.js';

const OPEN_KEY = 'harness.workspace.open';
const SLOW_MS = 6000, FAST_MS = 1000, CHECK_MS = 15000, WATCH_MS = 90000;
const STATE = {
  not_created: ['not created', 'muted'], ready: ['ready', 'ok'], loop_running: ['loop running', 'warn'],
  dirty: ['uncommitted changes', 'warn'], missing: ['folder missing', 'bad'], read_only: ['read only — no worktree', 'muted'],
};
const REASON_FALLBACK = {
  loop_running: 'The loop is still running.', push_unconfirmed: 'The branch was not confirmed on the remote.', pr_missing: 'No pull request exists yet.',
  tree_dirty: 'The worktree has uncommitted changes.', copied_changed: 'A copied file changed.', worktree_missing: 'The worktree folder is missing.',
};
const FINISHED = ['done', 'stopped', 'error'];

/** Screen state (survives renders). job = the latest create job seen (from workspace or from the job route). */
const ws = {
  data: null, error: null, loading: false, again: false, loadedAt: 0, job: null, following: null, dismissed: null, watchUntil: 0,
  check: null, checkErr: null, checking: false, checkedAt: 0, closing: false, result: null, saving: null,
};

const running = () => status() === 'running';
const jobRunning = () => !!(ws.job && ws.job.state === 'running');
const repoList = () => list(ws.data && ws.data.repos);
const clock = (iso) => { const m = /T(\d{2}:\d{2}:\d{2})/.exec(String(iso || '')); return m ? m[1] : ''; };

// ---------- reading ----------
/** GET workspace (overlapping calls collapse into one more read). Also follows a create job started by the loop itself. */
export async function loadWorkspace() {
  if (ws.loading) { ws.again = true; return; }
  ws.loading = true;
  try {
    const d = await ctx.api.get('workspace', 15000);
    ws.data = d; ws.error = null;
    if (d.job && d.job.id) {
      if (!ws.job || ws.job.id !== d.job.id || (d.job.steps || []).length >= list(ws.job.steps).length) ws.job = d.job;
      if (d.job.state === 'running') follow(d.job.id);
    }
    if (repoList().some((r) => r.worktree_exists) && !running() && Date.now() - ws.checkedAt > CHECK_MS) loadCheck();
  } catch (e) {
    ws.error = e.status === 404 && !/project/i.test(e.message) ? 'The server does not offer the workspace yet.' : e.message;
  } finally {
    ws.loading = false; ws.loadedAt = Date.now();
    renderAll();
    if (ws.again) { ws.again = false; loadWorkspace(); }
  }
}
/** Called right after a loop start: reads the workspace every second for a while so the steps of a creation at start show up. */
export function watchWorkspace() { ws.watchUntil = Date.now() + WATCH_MS; loadWorkspace(); }

/** Follows a create job (GET workspace/job/<id> every second): steps are append-only, the job ends with state ok or error. */
function follow(id) {
  if (ws.following === id) return;
  ws.following = id;
  const step = async () => {
    if (ws.following !== id) return;
    try {
      const r = await ctx.api.get('workspace/job/' + encodeURIComponent(id), 10000);
      if (ws.following !== id) return;
      if (r.job) {
        ws.job = r.job; renderAll();
        if (r.job.state !== 'running') { ws.following = null; finished(r.job); return; }
      }
    } catch (e) {
      if (e.status === 404) { ws.following = null; loadWorkspace(); return; }
    }
    setTimeout(step, FAST_MS);
  };
  step();
}
function finished(job) {
  toast(job.state === 'ok' ? 'Workspace ready.' : 'Workspace not created' + (job.error ? ': ' + job.error : '.'), job.state === 'ok' ? 'ok' : 'err');
  ws.checkedAt = 0;
  loadWorkspace(); reloadProgress(); schedulePoll(300);
}

async function loadCheck() {
  if (ws.checking) return;
  ws.checking = true; ws.checkedAt = Date.now();
  try { ws.check = await ctx.api.get('close-loop/check', 30000); ws.checkErr = null; }
  catch (e) { ws.check = null; ws.checkErr = e.status === 404 ? null : e.message; }
  finally { ws.checking = false; renderAll(); }
}

// ---------- actions ----------
async function closeLoop() {
  const rows = list(ws.check && ws.check.repos), confirmable = rows.filter((r) => r.needs_confirm);
  const ok = await confirmDialog({
    title: 'Close the loop?', yesText: confirmable.length ? 'Close anyway' : 'Close loop',
    text: 'Takes a verified backup, copies the plan (.specs) back to the primary repository, writes last-run.json and removes the worktree folders. Branches and commits stay. A worktree that is not clean is kept.',
    extraContent: confirmable.length ? html`<div class="nl-box nl-warn" role="alert">${'No pull request exists in ' + confirmable.map((r) => r.name).join(', ') + ': only a compare link. Closing anyway means you open that PR by hand.'}</div>` : null,
  });
  if (!ok) return;
  ws.closing = true; ws.result = null; renderAll();
  try {
    ws.result = await ctx.api.post('close-loop', { confirm_compare_link: confirmable.length > 0 }, 120000);
    toast(list(ws.result.closed).length ? `Closed: ${ws.result.closed.join(', ')}.` : 'Nothing was closed: see the reasons below.', list(ws.result.closed).length ? 'ok' : 'err');
  } catch (e) { toast((e.status === 409 ? 'Cannot close now: ' : 'Not closed: ') + e.message, 'err'); }
  finally { ws.closing = false; ws.checkedAt = 0; renderAll(); loadWorkspace(); loadCheck(); reloadProgress(); schedulePoll(300); }
}

/** Role of a repository: a missing value reads as edit. */
const roleOf = (r) => (r && r.role === 'reference' ? 'reference' : 'edit');
/** A reference repository is read by the agents only: no worktree, no carried files, nothing to commit for the loop. */
const READ_ONLY_TEXT = 'read only — no worktree';
const isReadOnly = (r) => !!r && (r.read_only === true || r.state === 'read_only' || roleOf(r) === 'reference');
/** Chip text of one repository: name + role (an edit repository no feature changes has no worktree yet). */
function roleText(r) {
  if (roleOf(r) === 'reference') return `${r.name} · reference`;
  return r.changed_by_feature === false ? `${r.name} · edits · no feature changes it yet (no worktree)` : `${r.name} · edits`;
}
const rejectedList = () => list(ws.data && (ws.data.rejected_suggestions || ws.data.rejected)).filter((x) => x && (x.path || x.reason));
const noteId = (r, i) => `ws-note-${r.name}-${i}`;
const notesIds = (r) => list(r.notes).map((n, i) => noteId(r, i)).join(' ') || undefined;

// ---------- drawing ----------
function StateChip({ state }) {
  const [text, tone] = STATE[state] || [String(state || ''), 'muted'];
  return html`<span class=${'ws-chip mono tone-' + tone}><span class="dot"></span>${text}</span>`;
}

function RepoRow({ r }) {
  const planned = !r.worktree_exists;
  return html`<li class="ws-repo">
    <div class="ws-repo-h"><span class="ws-repo-name mono">${r.name}</span>${r.primary ? html`<span class="rp-tag mono">primary</span>` : null}<span class="rp-tag mono" title=${ROLE_TEXT[roleOf(r)].hover} tabIndex="0">${ROLE_TEXT[roleOf(r)].label}</span><${StateChip} state=${r.state} /></div>
    <dl class="ws-kv">
      <dt>Your folder</dt><dd class="mono" title=${r.folder}>${txt(r.folder)}</dd>
      ${isReadOnly(r) ? html`<dt>Worktree</dt><dd class="mono">${READ_ONLY_TEXT}</dd>` : html`
      <dt>${planned ? 'Worktree (planned)' : 'Worktree'}</dt><dd class="mono" title=${r.worktree}>${txt(r.worktree)}</dd>
      <dt>Branch</dt><dd class="mono">${r.branch ? `${r.branch}  (from ${txt(r.base)})` : `a new loop branch, from ${txt(r.base)}`}</dd>`}
      ${list(r.copy).length ? html`<dt>Files copied</dt><dd class="mono">${r.copy.join(', ')}</dd>` : null}
      ${r.setup_command ? html`<dt>Setup</dt><dd class="mono">${r.setup_command}</dd>` : null}
    </dl>
    ${list(r.notes).map((n, i) => html`<div key=${i} id=${noteId(r, i)} class="ws-note" role="note">${n}</div>`)}
    ${r.state === 'not_created' && r.dirty ? html`<div class="ws-note" role="note">Save setup files with Commit setup files and push in Workspace; other changed files are never committed by the app.</div>` : null}
  </li>`;
}

/** The steps of the create job, live. Appends only; follows the end of the list unless the reader scrolled up. */
function Steps({ job }) {
  const box = useRef(null), stick = useRef(true);
  const steps = list(job.steps);
  useLayoutEffect(() => { const el = box.current; if (el && stick.current) el.scrollTop = el.scrollHeight; }, [steps.length]);
  const onScroll = (e) => { const el = e.currentTarget; stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 40; };
  const res = job.result && job.result.repos ? Object.entries(job.result.repos) : [];
  const live = job.state === 'running';
  return html`<section class="ws-job" aria-label="Workspace creation">
    <div class="ws-job-h">
      ${live ? html`<span class="spinner sm" aria-hidden="true"></span>` : html`<span class=${'ws-job-ic ' + (job.state === 'ok' ? 'ok' : 'bad')} aria-hidden="true">${job.state === 'ok' ? '✓' : '✗'}</span>`}
      <strong>${live ? 'Creating the workspace…' : job.state === 'ok' ? 'Workspace created' : 'The workspace was not created'}</strong>
      <span class="spacer"></span>
      ${!live ? html`<button type="button" class="btn btn-sm btn-ghost" onClick=${() => { ws.dismissed = job.id; renderAll(); }}>Dismiss</button>` : null}
    </div>
    <ol class="ws-steps mono" role="log" aria-live="polite" tabIndex="0" ref=${box} onScroll=${onScroll}>
      ${steps.length ? steps.map((s, i) => html`<li key=${i} class=${'ws-step k-' + (s.kind || 'check')}>
        <span class="ws-step-t">${clock(s.at)}</span>${s.repo ? html`<span class="ws-step-r">${s.repo}</span>` : html`<span class="ws-step-r"></span>`}
        <span class="ws-step-k">${s.kind}</span><span class="ws-step-x">${s.text}</span></li>`)
        : html`<li class="ws-step"><span class="ws-step-x dim">waiting for the first step…</span></li>`}
    </ol>
    ${job.state === 'error' ? html`<div class="nl-box nl-bad" role="alert">${job.error || 'The creation failed.'}</div>` : null}
    ${job.state === 'ok' && res.length ? html`<ul class="ws-done">${res.map(([name, x]) => html`<li key=${name}><b class="mono">${name}</b> ${x.reattached ? ' re-attached to ' : ' on '}<span class="mono">${x.branch}</span>
      ${' in '}<span class="mono" title=${x.worktree}>${midPath(x.worktree, 64)}</span></li>`)}</ul>` : null}
  </section>`;
}

function CloseLoop() {
  const c = ws.check, rows = list(c && c.repos);
  const finishedLoop = FINISHED.includes(status()) || status() === 'no_loop';
  const blockers = rows.filter((r) => !r.can_close && !r.needs_confirm);
  const confirmable = rows.filter((r) => r.needs_confirm);
  const enabled = !!c && finishedLoop && !running() && !blockers.length && (rows.length > 0) && !ws.closing;
  const why = running() ? 'The loop is still running: close it after it stops.'
    : !c ? (ws.checkErr ? 'Could not check: ' + ws.checkErr : 'Checking each repository…')
    : blockers.length ? 'Blocked by ' + blockers.map((r) => r.name).join(', ') + ': ' + list(blockers[0].reasons).map((x) => x.text || REASON_FALLBACK[x.code] || x.code).join(' ')
    : confirmable.length ? 'Only a compare link exists in ' + confirmable.map((r) => r.name).join(', ') + ': you will be asked to confirm.'
    : 'Everything is pushed and has a pull request.';
  const res = ws.result;
  return html`<section class="ws-close" aria-label="Close loop">
    <div class="ws-close-h"><h3 class="ws-h3">Close loop</h3>
      <button type="button" class="btn btn-sm btn-ghost" disabled=${ws.checking} onClick=${loadCheck}>${ws.checking ? 'Checking…' : 'Check again'}</button></div>
    <p class="rp-help">Finishes the loop safely: nothing is removed until the branch is pushed and a pull request exists in every repository. The plan (.specs) goes back to the primary repository; branches and commits stay.</p>
    ${rows.length ? html`<ul class="ws-close-list">${rows.map((r) => html`<li key=${r.name} class=${'ws-close-row ' + (r.can_close ? 'ok' : r.needs_confirm ? 'warn' : 'bad')}>
      <div class="ws-close-top"><span class="ws-close-ic" aria-hidden="true">${r.can_close ? '✓' : r.needs_confirm ? '!' : '✗'}</span><b class="mono">${r.name}</b>
        <span class="sr-only">${r.can_close ? ' can close' : r.needs_confirm ? ' needs your confirmation' : ' blocked'}</span>
        <span class="dim mono">${r.branch ? `${r.branch} → ${txt(r.base)}` : ''}</span></div>
      <div class="ws-close-d"><span class="dim">push </span><span class="mono">${txt(r.push)}</span>
        <span class="dim">  pull request </span>${httpUrl(r.pr) ? html`<a href=${r.pr} target="_blank" rel="noopener noreferrer" class="mono">${r.pr_status || 'open'}</a>` : html`<span class="mono">${txt(r.pr_status)}</span>`}</div>
      ${list(r.reasons).map((x, i) => html`<div key=${i} class="ws-note" role="note">${x.text || REASON_FALLBACK[x.code] || x.code}</div>`)}
    </li>`)}</ul>` : null}
    <div class="card-row"><button type="button" class="btn btn-primary" id="ws-close" disabled=${!enabled} aria-describedby="ws-close-why" onClick=${closeLoop}>${ws.closing ? 'Closing…' : 'Close loop'}</button>
      <span class="hint" id="ws-close-why">${why}</span></div>
    ${res ? html`<div class="ws-result" role="status"><div class="ws-job-h"><strong>${list(res.closed).length ? 'Closed' : 'Nothing was closed'}</strong><span class="spacer"></span>
        <button type="button" class="btn btn-sm btn-ghost" onClick=${() => { ws.result = null; renderAll(); }}>Dismiss</button></div>
      <ul class="ws-close-list">${Object.entries(res.repos || {}).map(([name, x]) => html`<li key=${name} class=${'ws-close-row ' + (x.status === 'closed' ? 'ok' : x.status === 'kept' ? 'warn' : 'bad')}>
        <div class="ws-close-top"><span class="ws-close-ic" aria-hidden="true">${x.status === 'closed' ? '✓' : x.status === 'kept' ? '!' : '✗'}</span><b class="mono">${name}</b>
          <span class="ws-chip mono">${x.status}</span><span class="dim mono">${x.removed_worktree ? 'worktree removed' : 'worktree kept'}${x.branch_kept ? ' · branch ' + x.branch_kept + ' kept' : ''}</span></div>
        ${list(x.reasons).map((q, i) => html`<div key=${i} class="ws-note" role="note">${q.text || REASON_FALLBACK[q.code] || q.code}</div>`)}</li>`)}</ul>
      ${res.backup ? html`<div class="rp-help">Backup: <span class="mono">${res.backup}</span></div>` : null}
      ${list(res.durable_copied).length ? html`<div class="rp-help">${`${res.durable_copied.length} plan file${res.durable_copied.length === 1 ? '' : 's'} copied back to the primary repository.`}${res.last_run ? html` Last run saved in <span class="mono">${res.last_run}</span>.` : null}</div>` : null}
    </div>` : null}
  </section>`;
}

const UNCOMMITTED_EXPLANATION = 'These files are not committed. The loop worktree is created when the loop starts, from the last commit, so the agents will not see them. Commit them first.';
const UNCOMMITTED_LIST_LIMIT = 12;

/** The context files (maps, CLAUDE.md, SYSTEM.md) a not-yet-created worktree will miss; [] once the worktree exists. */
function uncommittedContextOf(r) {
  return r && r.state === 'not_created' && !isReadOnly(r) ? list(r.uncommitted_context).filter((item) => item && item.path) : [];
}

/** Short summary like "maps: 3 files, CLAUDE.md: backend, SYSTEM.md" (mirrors context_files.uncommitted_chip_summary). */
function uncommittedChipSummary(items) {
  const maps = items.filter((i) => i.kind === 'map'), claudes = items.filter((i) => i.kind === 'claude'), systems = items.filter((i) => i.kind === 'system');
  const parts = [];
  if (maps.length) parts.push(`maps: ${maps.length} ${maps.length === 1 ? 'file' : 'files'}`);
  if (claudes.length) {
    const folders = claudes.map((i) => { const path = txt(i.path).replace(/\\/g, '/'); const at = path.lastIndexOf('/'); return at < 0 ? 'root' : path.slice(0, at); });
    parts.push(folders.length === 1 && folders[0] === 'root' ? 'CLAUDE.md' : 'CLAUDE.md: ' + folders.join(', '));
  }
  if (systems.length) parts.push('SYSTEM.md');
  return parts.join(', ');
}

/** "a, b, ... and N more": up to 12 names. */
function uncommittedNames(items) {
  const names = items.map((i) => txt(i.path));
  return names.slice(0, UNCOMMITTED_LIST_LIMIT).join(', ') + (names.length > UNCOMMITTED_LIST_LIMIT ? ` and ${names.length - UNCOMMITTED_LIST_LIMIT} more` : '');
}

/** Warning for the Start dialog: uncommitted context files the new worktree will not carry. Never blocks Start. */
export function UncommittedContextNotice() {
  const items = repoList().flatMap(uncommittedContextOf);
  if (!items.length) return null;
  return html`<div class="nl-box nl-warn ws-note" role="note" id="nl-uncommitted"><strong>Setup files not committed: </strong>they are carried into the worktree when the loop starts (or commit them from Workspace).
    <div class="mono ws-notice-l">${uncommittedNames(items)}</div></div>`;
}

/** One short notice for the Start dialog: which repositories are in scope and which worktrees Start creates by itself. */
export function WorkspaceNotice() {
  const repos = repoList();
  const todo = repos.filter((r) => r.state === 'not_created' && !isReadOnly(r));
  // RD-01: the live `other_changes` of each repository (tracked files changed in the folder that are not setup files), listed by name; a payload
  // without that key falls back to the bare `dirty` flag.
  const othersOf = (r) => (Array.isArray(r.other_changes) ? list(r.other_changes) : null);
  const withOthers = todo.filter((r) => (othersOf(r) ? othersOf(r).length : r.dirty));
  if (!todo.length && !withOthers.length) return null;
  return html`<div class="nl-box nl-warn" role="note"><strong>${'Repositories in scope: ' + repos.map((r) => r.name).join(', ') + '. '}</strong>
    ${todo.length ? html`Start creates ${todo.length === 1 ? 'the missing worktree' : 'the ' + todo.length + ' missing worktrees'} by itself (each on a new loop branch from its last commit; your folder and branch are never touched).
      <div class="mono ws-notice-l">${todo.map((r) => `${r.name} → ${r.worktree}`).join(String.fromCharCode(10))}</div>` : null}
    ${withOthers.map((r) => { const files = othersOf(r); return html`<div class="ws-note" role="note" key=${r.name}>${files ? `${files.length} other changed file${files.length === 1 ? '' : 's'} in ${r.name} will NOT be carried: the worktree is born from a commit.` : `Other changed files in ${r.name} will NOT be carried: the worktree is born from a commit.`}
      ${files ? html`<div class="mono ws-notice-l">${uncommittedNames(files.map((path) => ({ path })))}</div>` : null}</div>`; })}</div>`;
}

/** Workspace state for the Start dialog (START-04): one row per repository with its role, whether the worktree exists and the branch. */
export function WorkspaceState({ reattachBranch = null } = {}) {
  const repos = repoList(), d = ws.data;
  if (!d) return html`<div class="sl-muted">${ws.error ? 'Workspace unavailable: ' + ws.error : 'Reading the workspace…'}</div>`;
  if (!repos.length) return html`<div class="sl-muted">No repository is defined yet.</div>`;
  return html`<ul class="sl-repos" aria-label="Repositories and worktrees">${repos.map((r) => {
    const [stateText, tone] = STATE[r.state] || [String(r.state || ''), 'muted'];
    if (isReadOnly(r)) {
      return html`<li key=${r.name} class="sl-repo"><span class="sl-repo-name mono">${r.name}</span><span class="rp-tag mono" title=${ROLE_TEXT.reference.hover} tabIndex="0">${ROLE_TEXT.reference.label}</span>
        <span class="ws-chip mono tone-muted"><span class="dot"></span>${READ_ONLY_TEXT}</span></li>`;
    }
    const worktree = r.worktree_exists ? 'worktree created' : 'worktree not created yet (Start creates it)';
    return html`<li key=${r.name} class="sl-repo"><span class="sl-repo-name mono">${r.name}</span><span class="rp-tag mono" title=${ROLE_TEXT[roleOf(r)].hover} tabIndex="0">${ROLE_TEXT[roleOf(r)].label}</span>
      <span class=${'ws-chip mono tone-' + tone}><span class="dot"></span>${stateText}</span>
      <span class="sl-muted">${worktree}${r.branch ? ' · branch ' : reattachBranch && r.primary ? ' · continues the branch ' : ' · a new loop branch from '}<span class="mono">${r.branch || (r.primary && reattachBranch) || txt(r.base_branch || r.base)}</span>
        ${!r.branch && !(reattachBranch && r.primary) && roleOf(r) === 'edit' ? html` · starts from <span class="mono">${txt(r.base_branch || r.base)}${r.base_sha ? ` (${String(r.base_sha).slice(0, 7)})` : ''}</span>${r.current_branch && r.current_branch !== (r.base_branch || r.base) ? html`, your folder is on <span class="mono">${r.current_branch}</span>` : null}` : null}
        ${roleOf(r) === 'edit' && d.integration_branch ? html` · PR to <span class="mono">${d.integration_branch}</span>` : null}</span></li>`;
  })}</ul>`;
}

/** Content of #workspace-zone: one line; the details open by themselves only while a creation runs or failed. */
export function WorkspaceZone() {
  const st = status();
  useEffect(() => { loadWorkspace(); }, [st]);
  useEffect(() => {
    const t = setInterval(() => {
      if (document.visibilityState !== 'visible') return;
      const fast = jobRunning() || Date.now() < ws.watchUntil;
      if (Date.now() - ws.loadedAt >= (fast ? FAST_MS : SLOW_MS) - 100) loadWorkspace();
    }, FAST_MS);
    return () => clearInterval(t);
  }, []);
  const repos = repoList(), d = ws.data;
  const job = ws.job && ws.job.id !== ws.dismissed ? ws.job : null;
  const stored = readStored(OPEN_KEY, null);
  const open = typeof stored === 'boolean' ? stored : !!(jobRunning() || (job && job.state === 'error'));
  const toggle = () => { writeStored(OPEN_KEY, !open); renderAll(); };
  const anyWorktree = repos.some((r) => r.worktree_exists);
  const contextItems = repos.flatMap(uncommittedContextOf);
  const uncommitted = repos.filter((r) => r.state === 'not_created' && !isReadOnly(r) && (r.dirty || uncommittedContextOf(r).length));
  const contextSummary = uncommittedChipSummary(contextItems);
  const uncommittedTitle = contextItems.length ? [UNCOMMITTED_EXPLANATION, ...contextItems.map((i) => txt(i.path))].join(String.fromCharCode(10)) : 'The worktree is born from the last commit and does not carry uncommitted changes.';
  const defined = !d || d.defined !== false;
  const rejected = rejectedList();
  const count = `${repos.length} ${repos.length === 1 ? 'repository' : 'repositories'}`;
  return html`<section class="ws ws-line" aria-label="Workspace">
    <div class="ws-head">
      <span class="ws-title">Workspace</span>
      <span class="ws-sum mono" id="ws-sum" title=${repos.map((r) => r.name).join(', ')}>${!d ? (ws.error ? 'unavailable' : 'reading…') : !defined ? 'no repositories defined yet' : [count, jobRunning() ? 'creating the worktrees…' : ''].filter(Boolean).join(' · ')}</span>
      ${uncommitted.length ? html`<span class="ws-chip mono tone-warn" role="note" title=${uncommittedTitle}><span class="dot"></span>${'uncommitted in ' + uncommitted.map((r) => r.name).join(', ') + ': not carried' + (contextSummary ? ` (${contextSummary})` : '')}</span>` : null}
      ${jobRunning() ? html`<span class="spinner sm" aria-hidden="true"></span>` : null}
      <span class="spacer"></span>
      ${d && defined ? html`<button type="button" class="btn btn-sm btn-ghost ws-link" id="ws-toggle" aria-expanded=${String(open)} aria-controls="ws-body" onClick=${toggle}>${open ? 'hide details' : 'details'}</button>` : null}
    </div>
    ${d && defined && (repos.length || rejected.length) ? html`<div class="ws-roles" aria-label="Repositories and their role">
      ${repos.map((r) => html`<span key=${r.name} class=${'ws-chip mono ' + (roleOf(r) === 'reference' ? 'tone-muted' : r.changed_by_feature === false ? 'tone-warn' : 'tone-ok')}
        title=${[r.reason, r.folder].filter(Boolean).join(' · ')}><span class="dot"></span>${roleText(r)}</span>`)}
      ${rejected.map((x, i) => html`<span key=${'rej' + i} class="ws-chip mono tone-warn" role="note" title=${txt(x.reason)}><span class="dot"></span>${'rejected: ' + midPath(txt(x.path), 40) + (x.reason ? ' · ' + x.reason : '')}</span>`)}</div>` : null}
    ${open && d && defined ? html`<div class="ws-body" id="ws-body">
      <ul class="ws-repos" aria-label="Repositories and worktrees">${repos.map((r) => html`<${RepoRow} key=${r.name} r=${r} />`)}</ul>
      ${job ? html`<${Steps} job=${job} />` : null}
      ${anyWorktree ? html`<${CloseLoop} />` : null}
      ${ctx.state && ctx.state.planning_required ? null : html`<section class="ws-repos-mgr" aria-label="Roadmap"><h3 class="ws-h3">Roadmap</h3>
        <div class="card-row"><button type="button" class="btn" id="ws-reopen" disabled=${running()} aria-describedby="ws-reopen-why" onClick=${openReopenPlanning}>Change the roadmap</button>
          <span class="hint" id="ws-reopen-why">${running() ? 'The loop is running: stop it first.' : 'Reopens planning: the worktrees are removed, your folders and the specs stay.'}</span></div></section>`}
    </div>` : null}
    ${open && !d && ws.error ? html`<div class="nl-box nl-bad" role="alert">${'Workspace unavailable: ' + ws.error}</div>` : null}
  </section>`;
}
