/* Panel header. Loop mode = the navbar: left project + repo chip, status indicator (click opens the status dialog), compact cost and
   progress meters, then the actions in this order: PR, Start loop (Pause while running), Back to planning, Finish, Shut down app; help
   is added by app.js. Planning mode uses the same frame (WS-01): the Planning pill on the left next to the project block, a Workspace button (dialog, WS-03), Pause, Shut down.
   Shutting down belongs to the app: the button calls POST /api/app/shutdown (the project shutdown is a no-op).
   The header frame lives in the Panel (app.js); each dynamic part is a small component here, each action its own component. */
import { $ } from '../../util/dom.js';
import { html } from '../../util/preact.js';
import { DASH, isNum, txt, firstLine, httpUrl } from '../../util/format.js';
import { appApi } from '../../api/api_client.js';
import { toast } from '../../components/toast.js';
import { copyText } from '../../components/copy.js';
import { NavbarStatus, NavbarMeter, navbarStatusKey, METER_ATTENTION_PCT } from '../../components/navbar_status.js';
import { ctx, ui, STATUS, status, loop, pauseRequested, projectName } from './context.js';
import { renderAll, requestRender, schedulePoll, stopPanel } from '../project_panel.js';
import { costCapPause, costNumbers, openNewLoop, openStatusDialog, statusHasDialog } from './alert.js';
import { openReopenPlanning } from './reopen_planning.js';
import { openFinishDialog } from './wrap_up.js';
import { openWorkspaceDialog } from './workspace_dialog.js';

const meta = () => (ctx.state && ctx.state.meta) || {};
const PAUSED_STATUS = { label: 'Paused', c: '#F2B544', bg: '#2A2416', line: '#4A3D1C' }; // context.js STATUS has no `paused` yet; used by the planning pill and the title
const statusInfo = () => (ctx.state && ctx.state.planning_required ? STATUS.planning : STATUS[status()] || (status() === 'paused' ? PAUSED_STATUS : STATUS.no_loop));
const turnNow = () => { const t = loop().turn; return isNum(t) ? t : null; };
const folderName = (p) => String(p).replace(/[\\/]+$/, '').split(/[\\/]/).pop() || p;

/** Content of #brand-sub: project · branch. */
export function BrandSub() {
  const state = ctx.state;
  return `${txt(projectName())} · ${txt((state && state.branch) || meta().branch)}`;
}

/** Repo folder the panel works on and, when a loop worktree exists, that folder + the branch (both from /state). */
export function repoInfo() {
  const state = ctx.state || {}, ref = state.project_ref || {};
  const repo = ref.repo_path || state.repo || '', work = state.work || '';
  const norm = (v) => String(v).replace(/\\/g, '/').replace(/\/+$/, '').toLowerCase();
  const same = (a, b) => norm(a) === norm(b);
  return { repo, worktree: work && repo && !same(work, repo) ? work : '', branch: state.branch || meta().branch || '' };
}

/** Planning header, line under the brand: "repo <path> [copy]" and, with a loop worktree, "worktree <path> · branch". */
export function RepoLine() {
  const r = repoInfo();
  if (!r.repo) return null;
  return html`<div class="repo-line mono" id="repo-line">
    <span class="repo-item"><span class="repo-tag">repo</span><span class="repo-path" title=${r.repo}>${folderName(r.repo)}</span>
      <button type="button" class="repo-copy" title=${'copy the repo path: ' + r.repo} aria-label="copy the repo path" onClick=${() => copyText(r.repo, 'Repo path copied.')}>copy</button></span>
    ${r.worktree ? html`<span class="repo-item"><span class="repo-tag">worktree</span><span class="repo-path" title=${r.worktree}>${folderName(r.worktree)}</span>
      <button type="button" class="repo-copy" title=${'copy the worktree path: ' + r.worktree} aria-label="copy the worktree path" onClick=${() => copyText(r.worktree, 'Worktree path copied.')}>copy</button></span>` : null}
    ${r.worktree && r.branch ? html`<span class="repo-item"><span class="repo-tag">branch</span><span class="repo-path" title=${r.branch}>${r.branch}</span></span>` : null}
  </div>`;
}

/** Navbar repo chip (NAV-02): short name (the worktree folder when the loop works in one, else the repo folder); hover = full path + branch; copy = full path. */
export function RepoChip() {
  const r = repoInfo(), path = r.worktree || r.repo;
  if (!path) return null;
  const title = r.branch ? `${path}\nbranch: ${r.branch}` : path;
  return html`<span class="nb-repo mono" id="repo-chip" title=${title}>
    <span class="nb-repo-name">${folderName(path)}</span>
    <button type="button" class="nb-repo-copy" title=${'copy the full path: ' + path} aria-label="copy the full path" onClick=${() => copyText(path, 'Path copied.')}>copy</button>
  </span>`;
}

/** Container flags of the planning header: #status-pill colors and the #brand-sub title (repo path). */
export function headerModel() {
  const state = ctx.state, st = statusInfo();
  return {
    pillStyle: { background: st.bg, borderColor: st.line },
    repo: (state && state.project_ref && state.project_ref.repo_path) || (state && state.repo) || '',
  };
}

/** Content of #status-pill (planning header): dot + label (the pill colors are set on the container). */
export function StatusPill() {
  const st = statusInfo(), turn = turnNow();
  return html`
    <span class=${'dot' + (st.pulse ? ' pulse' : '')} id="status-dot" style=${{ background: st.c }}></span>
    <span class="mono" id="status-text" style=${{ color: st.c }}>${st.label + (turn != null ? ` · turn ${turn}` : '')}</span>`;
}

// ---------- navbar: status and meters ----------
/** Navbar status indicator: a cost-cap pause (or the old error with that reason) is `paused`; attention states open the status dialog. */
export function NavbarLoopStatus() {
  const key = costCapPause() ? 'paused' : navbarStatusKey(status()), l = loop();
  const clickable = statusHasDialog();
  const detail = key === 'awaiting_user' && !clickable ? 'Answer it in the LOOP panel.' : ['paused', 'error', 'awaiting_user', 'stopped'].includes(key) && l.reason ? firstLine(l.reason) : '';
  return html`<${NavbarStatus} statusKey=${key} turn=${turnNow()} detail=${detail} onClick=${clickable ? openStatusDialog : null} />`;
}

/** Cost meter `spent / cap` of the current run (COST-04): attention color from 80 % of the cap, red once the cap is passed. */
const planned = { cap: null, loading: false, wasPlanning: null };
/** Before a run exists /state only carries the default cap: the planned one is `settings.budget_loop` of GET loop/settings, read once
    and read again when the project leaves planning (the ROADMAP header may have changed). */
function plannedCap() {
  const planning = !!(ctx.state && ctx.state.planning_required);
  const leftPlanning = planned.wasPlanning === true && !planning;
  planned.wasPlanning = planning;
  if (leftPlanning) planned.cap = null;
  if (planned.cap === null && !planned.loading && ctx.api) {
    planned.loading = true;
    ctx.api.get('loop/settings', 15000)
      .then((data) => { const value = data && data.settings && data.settings.budget_loop; planned.cap = isNum(value) ? value : undefined; })
      .catch(() => { planned.cap = undefined; })
      .finally(() => { planned.loading = false; requestRender(); });
  }
  return isNum(planned.cap) ? planned.cap : null;
}
function CostMeter() {
  const noRun = status() === 'no_loop' && !isNum(meta().cost_usd);
  const planCap = noRun ? plannedCap() : null;
  const { spent, cap: stateCap } = costNumbers();
  const cap = noRun ? (isNum(planCap) ? planCap : stateCap) : stateCap;
  if (!isNum(spent) && !isNum(cap)) return null;
  const pct = isNum(spent) && isNum(cap) && cap > 0 ? (spent / cap) * 100 : 0;
  const title = isNum(cap) ? `Spent US$ ${isNum(spent) ? spent.toFixed(2) : DASH} of the US$ ${cap.toFixed(2)} limit of this run.` : 'Spent in this run.';
  return html`<${NavbarMeter} label="cost" value=${spent} max=${cap} unit="$" attention=${pct >= METER_ATTENTION_PCT} danger=${pct >= 100} title=${title} />`;
}
/** Progress meter: verified features of the roadmap. */
function ProgressMeter() {
  const c = (ctx.state && ctx.state.counts) || {};
  if (!isNum(c.total) || !c.total) return null;
  return html`<${NavbarMeter} label="verified" value=${c.verified || 0} max=${c.total} title=${`${c.verified || 0} of ${c.total} features verified`} />`;
}
/** Content of #limits (navbar): compact cost and progress meters. */
export function Meters() {
  return html`<${CostMeter} /><${ProgressMeter} />`;
}

// ---------- actions, one component each ----------
function PrChip() {
  const m = meta(), prU = httpUrl(m.pr), n = prU && prU.match(/\/pull\/(\d+)/);
  if (!prU) return null;
  return html`<a class="btn btn-sm pr-chip" href=${prU} target="_blank" rel="noopener noreferrer"
    title=${'open pull request' + (m.pr_status ? ' · ' + m.pr_status : '')}>${'PR ' + (n ? '#' + n[1] : '↗') + (m.pr_status ? ' · ' + m.pr_status : '')}</a>`;
}
/** Planning mode (WS-03): opens the Workspace dialog (repositories, roles, branches, setup, commit of the setup files). */
function WorkspaceButton() {
  return html`<button type="button" class="btn btn-ghost" id="btn-workspace" title="repositories, branches, setup state and the plan folder of this project"
    onClick=${openWorkspaceDialog}>Workspace</button>`;
}
function PauseButton() {
  if (status() !== 'running') return null;
  return pauseRequested()
    ? html`<button type="button" class="btn btn-pending" disabled id="btn-pause">Pausing after the turn…</button>`
    : html`<button type="button" class="btn" id="btn-pause" disabled=${!!ui.busy.pause} onClick=${doPause}>Pause after the turn</button>`;
}
function StartLoopButton() {
  if (status() === 'running') return null;
  return html`<button type="button" class="btn btn-primary" id="btn-start-loop" title="Opens the Start dialog: what will run and how to start it." onClick=${openNewLoop}>Start loop</button>`;
}

/** "Back to planning" state from GET planning/reopen-check, fetched again when the loop status changes (BACK-01). */
const reopenGate = { key: null, check: null };
function ensureReopenGate() {
  const key = `${status()}|${loop().at}`;
  if (reopenGate.key === key || !ctx.api) return;
  reopenGate.key = key;
  ctx.api.get('planning/reopen-check', 30000).then((c) => { reopenGate.check = c || null; }).catch(() => { reopenGate.check = null; }).finally(renderAll);
}
function reopenBlockReason() {
  const c = reopenGate.check;
  if (status() === 'running' || (c && c.blocked_by === 'loop_running')) return 'The loop is running: pause it and wait for it to stop before going back to planning.';
  return '';
}
function BackToPlanningButton() {
  ensureReopenGate();
  const why = reopenBlockReason();
  return html`<button type="button" class="btn" id="btn-back-planning" disabled=${!!why} aria-disabled=${why ? 'true' : 'false'}
    title=${why || 'Change the roadmap or the repositories: goes back to the planning screen.'} onClick=${openReopenPlanning}>Back to planning</button>`;
}
function FinishButton() {
  const s = status(), r = repoInfo();
  if (!(s === 'done' || s === 'stopped') || !r.worktree) return null;
  return html`<button type="button" class="btn" id="btn-finish" title="Wrap up this loop: deliver the branch, close it and remove the worktree."
    onClick=${() => openFinishDialog({ state: ctx.state, project: ctx.projectId, onDone: () => { renderAll(); schedulePoll(0); } })}>Finish</button>`;
}
function ShutdownControl() {
  return ui.confirmShutdown
    ? html`<div class="confirm-inline" role="group" aria-label="Confirm shutting down the app">
        <span>Shut down the app? Loops already started keep running as separate processes.</span>
        <button type="button" class="btn btn-danger btn-sm" id="btn-shutdown-yes" disabled=${!!ui.busy.shutdown} onClick=${doShutdown}>Yes, shut down</button>
        <button type="button" class="btn btn-sm" id="btn-shutdown-no" onClick=${cancelShutdown}>Cancel</button>
      </div>`
    : html`<button type="button" class="btn btn-ghost" id="btn-shutdown" title="shuts down the whole app (every project)" onClick=${askShutdown}>Shut down app</button>`;
}

function askShutdown() {
  ui.confirmShutdown = true; renderAll();
  const b = $('btn-shutdown-no'); if (b) b.focus();
}

/** Content of #header-actions. mode 'loop' = the navbar order (NAV-04); 'planning' = as before. */
export function HeaderActions({ mode = 'loop' }) {
  return mode === 'planning'
    ? html`<${PrChip} /><${WorkspaceButton} /><${PauseButton} /><${ShutdownControl} />`
    : html`<${PrChip} /><${PauseButton} /><${StartLoopButton} /><${BackToPlanningButton} /><${FinishButton} /><${ShutdownControl} />`;
}

export function cancelShutdown() {
  if (!ui.confirmShutdown) return;
  ui.confirmShutdown = false; renderAll();
  const b = $('btn-shutdown'); if (b) b.focus();
}

const CLOSED_TITLE = { notfound: 'project not found · Claude Maestro', shutdown: 'app shut down · Claude Maestro' };
export function updateTitle() {
  const state = ctx.state;
  if (ui.closed) { document.title = CLOSED_TITLE[ui.closed.kind] || 'Claude Maestro'; return; }
  if (!state) { document.title = (ctx.offline ? 'disconnected' : 'loading') + ' · Claude Maestro'; return; }
  const st = statusInfo(), v = loop().turn;
  document.title = `${ctx.offline ? 'disconnected · ' : ''}${st.label}${isNum(v) ? ' · turn ' + v : ''} · ${projectName() || 'project'}`;
}

async function doPause() {
  ui.busy.pause = true; renderAll();
  try { await ctx.api.post('pause', {}); ui.pausePending = true; toast('Pause requested: the loop stops at the end of this turn.', 'ok'); schedulePoll(0); }
  catch (e) { toast('Could not pause: ' + e.message, 'err'); }
  finally { ui.busy.pause = false; renderAll(); }
}

async function doShutdown() {
  ui.busy.shutdown = true; renderAll();
  try {
    await appApi.post('shutdown', {}, 15000);
    stopPanel();
    ui.closed = { kind: 'shutdown' };
  } catch (e) { toast('Could not shut down: ' + e.message, 'err'); ui.confirmShutdown = false; }
  finally { ui.busy.shutdown = false; renderAll(); }
}
