/* Workspace dialog (planning mode, WS-02/WS-03): everything the old WORKSPACE band and the "Writing the plan in <path>" line showed, in one
   place, opened by the Workspace button of the navbar. GET workspace (read again every few seconds while open and after every action):
   per repository the name, folder, role tag (edits | reference, with the hover of RP-02), why the chat chose that role (`reason`), the current
   branch, "starts from <base> (<sha>)", what the loop will do with it, the setup state (Set up / Map open the per-repository installer,
   panel/repo_setup_dialog.js), the uncommitted setup files with "Commit setup files and push" (POST workspace/repos/<name>/commit-setup
   {confirm_branch}, behind a confirmation that names the branch) and the other changes that will NOT be carried. This dialog is the ONLY place to
   manage repositories by hand: per non-primary repository a role switch edits | reference (PATCH workspace/repos/<name> {role}) and Remove
   (DELETE workspace/repos/<name>, confirmation, no file is deleted); both locked while a loop or a job runs; the primary is always edits. Top: integration branch
   ("PR to developer") and the plan folder. "Add repository…" opens the folder picker, adds the repository (POST workspace/repos, or the old
   projects/<id>/repos route when the new one is not served yet) and opens its setup dialog.
   The back end decides every state and refusal; the payload is read tolerantly (an old payload lacks the new keys). */
import { html, useState, useEffect, useRef } from '../../util/preact.js';
import { list, txt, midPath } from '../../util/format.js';
import { appApi, requestJson } from '../../api/api_client.js';
import { toast } from '../../components/toast.js';
import { openDialog, confirmDialog } from '../../components/dialog.js';
import { ctx, status } from './context.js';
import { renderAll, schedulePoll } from '../project_panel.js';
import { reloadProgress } from './progress_trail.js';
import { ROLE_TEXT, roleKey, chooseFolder } from '../repo_scope.js';
import { openRepoSetupDialog } from './repo_setup_dialog.js';

const REFRESH_MS = 5000;
const LIST_LIMIT = 12;
const SETUP_STATE = {
  not_set_up: ['not set up', 'warn'], set_up: ['set up', 'ok'], map_missing: ['map missing', 'warn'], mapping: ['mapping…', 'muted'],
};
const NO_COMMIT_BRANCHES = ['main', 'master'];

const shortSha = (sha) => String(sha || '').slice(0, 7);
const names = (items) => {
  const all = list(items).map((x) => (x && typeof x === 'object' ? x.path || x.name || '' : String(x))).filter(Boolean);
  return all.slice(0, LIST_LIMIT).join(', ') + (all.length > LIST_LIMIT ? ` and ${all.length - LIST_LIMIT} more` : '');
};
const pathsOf = (items) => list(items).map((x) => (x && typeof x === 'object' ? x.path || x.name || '' : String(x))).filter(Boolean);

/** Role tag in plain words with its hover (RP-02). Also used by the Workspace line and the Start dialog. */
export function RoleTag({ repo }) {
  const role = ROLE_TEXT[roleKey(repo)];
  return html`<span class="rp-tag mono ws-role-tag" title=${role.hover} tabIndex="0">${role.label}</span>`;
}

/** Why "Commit setup files and push" is not possible right now (the server repeats every refusal); '' = possible. */
function commitBlock(repo, busy, loopRunning) {
  const branch = String(repo.current_branch || '');
  if (!pathsOf(repo.setup_files).length) return 'Nothing to commit.';
  if (loopRunning) return 'A loop is running: wait until it stops.';
  if (busy) return 'Another action is running.';
  if (!branch || branch === 'HEAD') return 'The folder is on a detached HEAD: check out a branch first.';
  if (NO_COMMIT_BRANCHES.includes(branch)) return `Never commits on ${branch}: switch to a working branch (developer) first.`;
  if (!repo.upstream) return `${branch} has no upstream: run "git push -u origin ${branch}" in the folder once, then try again.`;
  return '';
}

const LOCK_REASON = 'A loop or a job is running: wait until it stops.';

/** Edits | reference switch of a repository (same words and hovers as RP-02); the primary is always edits. */
function RoleSwitch({ repo, disabled, onPick }) {
  const current = roleKey(repo);
  return html`<div class="seg ws-role-switch" role="group" aria-label=${'Role of ' + repo.name}>${['edit', 'reference'].map((key) => {
    const text = ROLE_TEXT[key];
    return html`<button key=${key} type="button" class="seg-btn" data-role=${key} aria-pressed=${String(current === key)} disabled=${disabled || !!repo.primary}
      title=${repo.primary ? 'The primary repository is always edited. ' + text.hover : disabled ? LOCK_REASON : text.hover}
      onClick=${() => { if (current !== key) onPick(repo, key); }}>${text.label}</button>`;
  })}</div>`;
}

function RepoCard({ repo, integration, busyKey, locked, onSetup, onCommit, onRole, onRemove }) {
  const role = roleKey(repo), editing = role === 'edit';
  const [stateText, tone] = SETUP_STATE[(repo.setup && repo.setup.state) || ''] || ['', 'muted'];
  const setupState = repo.setup && repo.setup.state;
  const setupFiles = pathsOf(repo.setup_files), otherChanges = pathsOf(repo.other_changes);
  const block = commitBlock(repo, !!busyKey, locked);
  const base = repo.base_branch, target = integration || base;
  const worktreeName = `${repo.name}-worktree-loop`;
  return html`<li class="ws-repo wsd-repo" data-repo=${repo.name}>
    <div class="ws-repo-h"><span class="ws-repo-name mono">${repo.name}</span>${repo.primary ? html`<span class="rp-tag mono">primary</span>` : null}
      ${repo.primary ? html`<${RoleTag} repo=${repo} />` : null}
      <${RoleSwitch} repo=${repo} disabled=${locked || !!busyKey} onPick=${onRole} />
      ${stateText ? html`<span class=${'ws-chip mono tone-' + tone}><span class="dot"></span>${stateText}</span>` : null}
      ${setupState === 'not_set_up' || setupState === 'map_missing'
        ? html`<button type="button" class="btn btn-sm btn-primary" data-act="setup" disabled=${!!busyKey} onClick=${() => onSetup(repo)}>${setupState === 'map_missing' ? 'Map' : 'Set up'}</button>`
        : setupState === 'mapping' ? html`<button type="button" class="btn btn-sm" data-act="setup" onClick=${() => onSetup(repo)}>Show progress</button>` : null}
      ${repo.primary ? null : html`<span class="spacer"></span><button type="button" class="btn btn-sm btn-ghost" data-act="remove" disabled=${locked || !!busyKey}
        title=${locked ? LOCK_REASON : 'Removes it from this project; no file is deleted.'} aria-label=${'Remove ' + repo.name} onClick=${() => onRemove(repo)}>${busyKey === 'rm:' + repo.name ? 'Removing…' : 'Remove'}</button>`}</div>
    ${repo.reason ? html`<div class="rp-purpose wsd-reason">${repo.reason}</div>` : null}
    <dl class="ws-kv">
      <dt>Folder</dt><dd class="mono" title=${repo.path || repo.folder}>${txt(repo.path || repo.folder)}</dd>
      <dt>Current branch</dt><dd class="mono">${txt(repo.current_branch)}${repo.upstream ? html` <span class="dim">· upstream ${repo.upstream}</span>` : null}</dd>
      ${editing ? html`<dt>Starts from</dt><dd class="mono">${base ? `${base}${repo.base_sha ? ` (${shortSha(repo.base_sha)})` : ''}` : 'the current branch'}</dd>` : null}
      <dt>The loop will</dt><dd class="wsd-plain">${editing
        ? html`create the worktree <span class="mono">${worktreeName}</span> on a new <span class="mono">loop/*</span> branch${base ? html` from <span class="mono">${base}</span>` : null}, then open a PR to <span class="mono">${txt(target)}</span>.`
        : 'only read it: no worktree, no branch, nothing is changed.'}</dd>
    </dl>
    ${setupFiles.length ? html`<div class="wsd-files" role="note"><strong>${setupFiles.length} setup file${setupFiles.length === 1 ? '' : 's'} not committed.</strong>
      They are carried into the worktree when the loop starts, or you can commit them here.
      <div class="mono wsd-list">${names(setupFiles)}</div>
      <div class="card-row"><button type="button" class="btn btn-sm" data-act="commit-setup" disabled=${!!block} aria-describedby=${'wsd-why-' + repo.name} onClick=${() => onCommit(repo, setupFiles)}>${busyKey === 'commit:' + repo.name ? 'Committing…' : 'Commit setup files and push'}</button>
        <span class="hint" id=${'wsd-why-' + repo.name}>${block || `Commits only these files on ${repo.current_branch} and pushes it to ${repo.upstream}.`}</span></div></div>` : null}
    ${otherChanges.length ? html`<div class="wsd-files wsd-other" role="note"><strong>${otherChanges.length} other changed file${otherChanges.length === 1 ? '' : 's'} will NOT be carried.</strong>
      The loop works from a commit: these stay in your folder only.<div class="mono wsd-list">${names(otherChanges)}</div></div>` : null}
  </li>`;
}

function WorkspaceDialog() {
  const [data, setData] = useState(null), [err, setErr] = useState(''), [busyKey, setBusyKey] = useState('');
  const alive = useRef(true), loading = useRef(false), busyRef = useRef('');
  useEffect(() => () => { alive.current = false; }, []);
  const load = async () => {
    if (loading.current) return;
    loading.current = true;
    try { const d = await ctx.api.get('workspace', 15000); if (alive.current) { setData(d); setErr(''); } }
    catch (e) { if (alive.current) setErr('Could not read the workspace: ' + e.message); }
    finally { loading.current = false; }
  };
  useEffect(() => {
    load();
    const timer = setInterval(() => { if (document.visibilityState === 'visible' && !busyRef.current) load(); }, REFRESH_MS);
    return () => clearInterval(timer);
  }, []);
  const busy = (key) => { busyRef.current = key; if (alive.current) setBusyKey(key); };
  const afterChange = () => { reloadProgress(); schedulePoll(300); renderAll(); };

  const running = status() === 'running';
  const locked = running || !!(data && data.job && data.job.state === 'running');
  const openSetup = (repo) => openRepoSetupDialog({ repo: repo.name, role: roleKey(repo) });

  const commitSetup = async (repo, files) => {
    const branch = String(repo.current_branch || '');
    const ok = await confirmDialog({
      title: `Commit setup files and push ${branch}?`, yesText: `Commit and push ${branch}`,
      text: `This commits only the ${files.length} setup file${files.length === 1 ? '' : 's'} listed below on the branch "${branch}" of ${repo.name} (message "chore(setup): codebase maps and agent context") and pushes "${branch}" to ${repo.upstream || 'its upstream'}. Other changes in the folder are not staged. Nothing is forced.`,
      extraContent: html`<div class="mono wsd-list">${names(files)}</div>`,
    });
    if (!ok) return;
    busy('commit:' + repo.name); setErr('');
    try {
      const r = await ctx.api.post('workspace/repos/' + encodeURIComponent(repo.name) + '/commit-setup', { confirm_branch: branch }, 90000);
      toast(`Committed${r && r.commit ? ' ' + shortSha(r.commit) : ''} and pushed to ${(r && r.pushed_to) || branch}.`, 'ok');
    } catch (e) {
      const reason = (e.body && e.body.reason) || e.message;
      setErr((e.status === 409 ? 'Not committed: ' : 'Commit failed: ') + reason);
    } finally { busy(''); await load(); afterChange(); }
  };

  const repoRoute = (repo) => ctx.api.url('workspace/repos/' + encodeURIComponent(repo.name));
  const changeRepo = async (key, request, okText) => {
    busy(key); setErr('');
    try { await request(); toast(okText, 'ok'); }
    catch (e) { setErr(((e.body && e.body.reason) || e.message) + (e.status === 409 ? ' (nothing was changed)' : '')); } // the server's own sentence (e.g. a loop is running)
    finally { busy(''); await load(); afterChange(); }
  };
  const setRole = (repo, role) => changeRepo('role:' + repo.name, () => requestJson('PATCH', repoRoute(repo), { role }), `${repo.name} is now ${ROLE_TEXT[role].label}.`);
  const removeRepository = async (repo) => {
    const ok = await confirmDialog({
      title: `Remove ${repo.name} from this project?`, yesText: 'Remove',
      text: `Removes it from this project; no file is deleted. The folder, its branches and any worktree stay where they are.`,
    });
    if (ok) changeRepo('rm:' + repo.name, () => requestJson('DELETE', repoRoute(repo), null), `${repo.name} left the project (no file was deleted).`);
  };

  const addRepository = async () => {
    const path = await chooseFolder();
    if (!path) return;
    busy('add'); setErr('');
    try {
      let result;
      try { result = await ctx.api.post('workspace/repos', { path, role: 'edit' }, 30000); }
      catch (e) { if (e.status !== 404 && e.status !== 405) throw e; result = await appApi.post('projects/' + encodeURIComponent(ctx.projectId) + '/repos', { path }, 30000); }
      await load();
      afterChange();
      toast('Repository added.', 'ok');
      const added = (result && (result.repo || result.added)) || null;
      const addedName = (added && added.name) || (result && result.name)
        || (list(result && result.repos).find((r) => r && String(r.path).replace(/[\\/]+$/, '') === path.replace(/[\\/]+$/, '')) || {}).name
        || String(path).replace(/[\\/]+$/, '').split(/[\\/]/).pop();
      openRepoSetupDialog({ repo: addedName, role: roleKey(added || result) });
    } catch (e) { setErr(e.message); }
    finally { busy(''); }
  };

  if (!data) return html`<div class=${err ? 'nl-box nl-bad' : 'hint'} role=${err ? 'alert' : 'status'}>${err || 'reading the workspace…'}</div>`;
  const repos = list(data.repos);
  const integration = data.integration_branch;
  return html`<div class="wsd">
    ${err ? html`<div class="nl-box nl-bad" role="alert">${err}</div>` : null}
    ${locked ? html`<div class="nl-box nl-warn" role="note">A loop or a job is running: the repository list and the commits are locked until it stops.</div>` : null}
    <dl class="ws-kv wsd-top">
      <dt>Integration branch</dt><dd class="mono">${integration ? `PR to ${integration}` : 'not detected: the PR targets each repository\'s base branch'}</dd>
      <dt>Plan folder</dt><dd class="mono" title=${data.plan_folder || ''}>${data.plan_folder ? midPath(data.plan_folder, 72) : '—'}</dd>
    </dl>
    ${repos.length ? html`<ul class="ws-repos wsd-list-repos" aria-label="Repositories of the project">${repos.map((repo) => html`<${RepoCard} key=${repo.name} repo=${repo} integration=${integration}
      busyKey=${busyKey} locked=${locked} onSetup=${openSetup} onCommit=${commitSetup} onRole=${setRole} onRemove=${removeRepository} />`)}</ul>`
      : html`<div class="hint">No repository is defined yet: the planning chat adds them, or use Add repository.</div>`}
    <div class="card-row"><button type="button" class="btn btn-sm" id="wsd-add" disabled=${locked || !!busyKey} onClick=${addRepository}>${busyKey === 'add' ? 'Adding…' : 'Add repository…'}</button>
      <span class="hint">Opens the folder picker. The folder must be a git repository on a branch. It is added as <b>edits</b>; switch it to <b>reference</b> in its card.</span></div>
  </div>`;
}

/** Opens the Workspace dialog (the navbar button of planning mode). */
export function openWorkspaceDialog() {
  openDialog({ title: 'Workspace', width: 860, cls: 'wsd-dlg', content: html`<${WorkspaceDialog} />`, actions: [{ text: 'Close' }] });
}
