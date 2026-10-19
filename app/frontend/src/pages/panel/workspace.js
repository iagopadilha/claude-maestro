/* Workspace area (under the header, both in planning and in the loop panel): where the loop works and why.
   - GET workspace: per repo the folder, the worktree (existing or planned), loop branch, base branch and a state; the plain-language
     explanation and the warnings are shown BEFORE anything is created.
   - POST workspace/create starts a job; its steps (GET workspace/job/<id>, append-only) are drawn live. The same steps show when the
     loop itself creates the worktrees at start (the job comes back in GET workspace), so creation is never silent.
   - POST workspace/repos/<name>/commit-push ("Commit and push" / "Commit only" on a not-created row whose folder is dirty): saves the
     user's folder so the new worktree carries the work.
   - The repositories are defined ONLY in planning (Repositories tab, pages/repo_manager.js); here they are a read-only list and
     "Change roadmap or repositories" reopens planning (panel/reopen_planning.js).
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

const OPEN_KEY = 'harness.workspace.open';
const SLOW_MS = 6000, FAST_MS = 1000, CHECK_MS = 15000, WATCH_MS = 90000;
const STATE = {
  not_created: ['not created', 'muted'], ready: ['ready', 'ok'], loop_running: ['loop running', 'warn'],
  dirty: ['uncommitted changes', 'warn'], missing: ['folder missing', 'bad'],
};
// what each worktree state means, in plain words (tooltip of the Workspace band)
const STATE_MEANING = {
  not_created: 'created when the loop starts, or now with "Create workspace"',
  ready: 'the separate folder exists and the loop can work in it',
  loop_running: 'the loop is working in it right now',
  dirty: 'it has changes that are not committed yet',
  missing: 'the repository folder was moved or deleted',
};
function WorkspaceTip({ repos }) {
  const counts = Object.keys(STATE_MEANING).map((k) => [k, repos.filter((r) => r.state === k).length]).filter(([, n]) => n > 0);
  return html`<span class="gb-tip ws-tip" id="ws-tip" role="tooltip">
    <b class="gb-tip-t">What is the workspace?</b>
    <span class="gb-tip-p">It is where the loop works. One separate folder (a git worktree) per repository, each next to yours, on a new <b class="mono">${"loop/<date>"}</b> branch from its last commit. Your own folders and branches are never touched, and nothing is cloned or pushed until you deliver.</span>
    ${counts.length ? html`<span class="gb-tip-l">${counts.map(([k, n]) => html`<span class="gb-tip-r" key=${k}><span class="gb-tip-n mono">${n}×</span><span><b class="mono">${STATE[k][0]}</b>${STATE_MEANING[k]}</span></span>`)}</span>` : null}
    <span class="gb-tip-f">Use the Workspace row to open the details</span>
  </span>`;
}
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
async function createWorkspace(names) {
  const targets = repoList().filter((r) => names.includes(r.name));
  const warnings = targets.flatMap((r) => list(r.notes).map((n) => `${r.name}: ${n}`));
  const ok = await confirmDialog({
    title: 'Create the workspace?', yesText: 'Create workspace',
    text: (ws.data && ws.data.explanation) || 'Creates one separate folder (a git worktree) per repository, each on a new loop branch from its last commit, so your folders and branches are never touched. Nothing is cloned or pushed.',
    extraContent: html`<ul class="ws-dlg-list">${targets.map((r) => html`<li key=${r.name}><b class="mono">${r.name}</b> ${' → '}<span class="mono">${r.worktree}</span></li>`)}
      ${warnings.map((w, i) => html`<li key=${'w' + i} class="ws-dlg-warn">${w}</li>`)}</ul>`,
  });
  if (!ok) return;
  try {
    const r = await ctx.api.post('workspace/create', { repos: names }, 30000);
    ws.dismissed = null; ws.job = { id: r.job_id, type: 'workspace-create', state: 'running', steps: [], result: null, error: '' };
    follow(r.job_id); renderAll();
  } catch (e) { toast((e.status === 409 ? 'Not right now: ' : 'Not created: ') + e.message, 'err'); loadWorkspace(); }
}

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

/** Saves the user's own folder (git add -A, commit, optionally push the current branch) so a new worktree carries the work. */
async function commitRepo(r, push) {
  if (ws.saving || running() || jobRunning()) return;
  const field = { el: null };
  const branch = txt(r.base);
  const ok = await confirmDialog({
    title: push ? 'Commit and push?' : 'Commit only?', yesText: push ? 'Commit and push' : 'Commit only',
    text: `Runs "git add -A" in ${r.folder}, commits everything and ${push ? `pushes the current branch (${branch}) to its remote` : 'keeps the commit local, nothing is pushed'}. Nothing is forced. The worktree is then created from that commit.`,
    extraContent: html`<label class="dlg-text">Commit message (optional)
      <input type="text" class="rp-setup-input mono" maxlength="200" placeholder="leave empty for the default message" ref=${(el) => { if (el) field.el = el; }} /></label>`,
  });
  if (!ok) return;
  const message = field.el ? field.el.value.trim() : '';
  ws.saving = r.name; renderAll();
  try {
    const res = await ctx.api.post('workspace/repos/' + encodeURIComponent(r.name) + '/commit-push', message ? { message, push } : { push }, 60000);
    toast(res.pushed ? `Committed and pushed to ${res.branch || branch}.` : 'Committed.', 'ok');
  } catch (e) {
    const committed = e.committed || (e.body && (e.body.committed || (e.body.extras && e.body.extras.committed)));
    toast((committed ? 'Committed, but not pushed: ' : 'Not saved: ') + e.message + (committed ? ' The commit stays in your folder.' : ''), 'err');
  } finally { ws.saving = null; ws.checkedAt = 0; renderAll(); loadWorkspace(); }
}
const noteId = (r, i) => `ws-note-${r.name}-${i}`;
const notesIds = (r) => list(r.notes).map((n, i) => noteId(r, i)).join(' ') || undefined;

// ---------- drawing ----------
function StateChip({ state }) {
  const [text, tone] = STATE[state] || [String(state || ''), 'muted'];
  return html`<span class=${'ws-chip mono tone-' + tone}><span class="dot"></span>${text}</span>`;
}

function RepoRow({ r }) {
  const planned = !r.worktree_exists;
  const busy = !!ws.saving || running() || jobRunning();
  return html`<li class="ws-repo">
    <div class="ws-repo-h"><span class="ws-repo-name mono">${r.name}</span>${r.primary ? html`<span class="rp-tag mono">primary</span>` : null}<${StateChip} state=${r.state} /></div>
    <dl class="ws-kv">
      <dt>Your folder</dt><dd class="mono" title=${r.folder}>${txt(r.folder)}</dd>
      <dt>${planned ? 'Worktree (planned)' : 'Worktree'}</dt><dd class="mono" title=${r.worktree}>${txt(r.worktree)}</dd>
      <dt>Branch</dt><dd class="mono">${r.branch ? `${r.branch}  (from ${txt(r.base)})` : `a new loop branch, from ${txt(r.base)}`}</dd>
      ${list(r.copy).length ? html`<dt>Files copied</dt><dd class="mono">${r.copy.join(', ')}</dd>` : null}
      ${r.setup ? html`<dt>Setup</dt><dd class="mono">${r.setup}</dd>` : null}
    </dl>
    ${list(r.notes).map((n, i) => html`<div key=${i} id=${noteId(r, i)} class="ws-note" role="note">${n}</div>`)}
    ${r.state === 'not_created' && r.dirty ? html`<div class="card-row ws-save">
      <button type="button" class="btn btn-sm btn-primary" disabled=${busy} aria-describedby=${notesIds(r)} onClick=${() => commitRepo(r, true)}>${ws.saving === r.name ? 'Saving…' : 'Commit and push'}</button>
      <button type="button" class="btn btn-sm btn-ghost" disabled=${busy} aria-describedby=${notesIds(r)} onClick=${() => commitRepo(r, false)}>Commit only</button>
      ${running() ? html`<span class="hint">A loop is running.</span>` : null}</div>` : null}
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

/** One short notice for the start-loop card: which worktrees the start will create, and why. Null when nothing will be created. */
export function WorkspaceNotice() {
  const todo = repoList().filter((r) => r.state === 'not_created');
  if (!todo.length) return null;
  return html`<div class="nl-box nl-warn" role="note"><strong>Start creates ${todo.length === 1 ? 'a separate worktree' : todo.length + ' separate worktrees'}: </strong>
    ${(ws.data && ws.data.explanation) || 'The loop works in a separate folder on a new loop branch; your folder and branch are never touched.'}
    <div class="mono ws-notice-l">${todo.map((r) => `${r.name} → ${r.worktree}`).join('\n')}</div>
    <div class="hint">The steps show in the Workspace area as they happen.</div></div>`;
}

/** Escape closes the tooltip even while the pointer is still over the icon (data-esc hides it until the next hover or focus). */
function tipEscape(e) { if (e.key === 'Escape') e.currentTarget.dataset.esc = '1'; }
function tipRearm(e) { delete e.currentTarget.dataset.esc; }

/** Content of #workspace-zone. */
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
  const todo = repos.filter((r) => r.state === 'not_created');
  const job = ws.job && ws.job.id !== ws.dismissed ? ws.job : null;
  const stored = readStored(OPEN_KEY, null);
  const open = typeof stored === 'boolean' ? stored : !!(jobRunning() || (job && job.state === 'error'));
  const toggle = () => { writeStored(OPEN_KEY, !open); renderAll(); };
  const anyWorktree = repos.some((r) => r.worktree_exists);
  const summary = !d ? (ws.error ? 'unavailable' : 'reading…')
    : [`${repos.length} ${repos.length === 1 ? 'repository' : 'repositories'}`, jobRunning() ? 'creating the worktree…'
      : todo.length ? `${todo.length} worktree${todo.length === 1 ? '' : 's'} not created` : repos.length && repos.every((r) => r.state === 'ready') ? 'ready' : ''].filter(Boolean).join(' · ');
  return html`<section class="ws" aria-label="Workspace">
    <div class="ws-head">
      <button type="button" class="ws-toggle" id="ws-toggle" aria-expanded=${String(open)} aria-controls="ws-body" onClick=${toggle}>
        <span class="ws-chev" aria-hidden="true">${open ? '▾' : '▸'}</span><span class="ws-title">Workspace</span>
        <span class="ws-sum mono">${summary}</span>
        ${jobRunning() ? html`<span class="spinner sm" aria-hidden="true"></span>` : null}
      </button>
      <button type="button" class="ws-info" id="ws-info" aria-label="What is the workspace?" aria-describedby="ws-tip" onKeyDown=${tipEscape} onMouseLeave=${tipRearm} onBlur=${tipRearm}>i</button>
      <${WorkspaceTip} repos=${repos} />
    </div>
    ${open ? html`<div class="ws-body" id="ws-body">
      ${!d ? html`<div class=${ws.error ? 'nl-box nl-bad' : 'hint'} role=${ws.error ? 'alert' : 'status'}>${ws.error ? 'Workspace unavailable: ' + ws.error : 'reading the workspace…'}
        ${ws.error ? html`<div><button type="button" class="btn btn-sm" onClick=${() => loadWorkspace()}>Try again</button></div>` : null}</div>` : html`
      ${todo.length || job ? html`<p class="ws-explain">${d.explanation}</p>` : null}
      <ul class="ws-repos" aria-label="Repositories and worktrees">${repos.map((r) => html`<${RepoRow} key=${r.name} r=${r} />`)}</ul>
      ${job ? html`<${Steps} job=${job} />` : null}
      ${todo.length && !jobRunning() ? html`<div class="card-row ws-create">
        <button type="button" class="btn btn-primary" id="ws-create" disabled=${running()} onClick=${() => createWorkspace(todo.map((r) => r.name))}>Create workspace</button>
        <span class="hint">${running() ? 'A loop is running.' : 'Creates one separate folder (git worktree) per repository above, each on a new loop branch from its last commit. Nothing is cloned or pushed. Optional: if you skip it, the loop creates the same ones when it starts.'}</span></div>` : null}
      ${anyWorktree ? html`<${CloseLoop} />` : null}
      ${ctx.state && ctx.state.planning_required ? null : html`<section class="ws-repos-mgr" aria-label="Roadmap and repositories"><h3 class="ws-h3">Roadmap and repositories</h3>
        <p class="rp-help">The repositories come from the roadmap and are set in planning. To add, remove or change one, go back to planning: the worktrees above are removed, your folders and the specs stay.</p>
        <div class="card-row"><button type="button" class="btn" id="ws-reopen" disabled=${running()} aria-describedby="ws-reopen-why" onClick=${openReopenPlanning}>Change roadmap or repositories</button>
          <span class="hint" id="ws-reopen-why">${running() ? 'The loop is running: stop it first.' : 'Reopens planning on the Repositories tab.'}</span></div></section>`}`}
    </div>` : null}
  </section>`;
}
