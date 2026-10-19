/* Panel header: project/branch, status pill, limits, PR, pause and Shut down app.
   Shutting down belongs to the app: the button calls POST /api/app/shutdown (the project shutdown is a no-op).
   The header frame lives in the Panel (app.js); each dynamic part is a small component here. */
import { $ } from '../../util/dom.js';
import { html } from '../../util/preact.js';
import { DASH, isNum, num, txt, list, httpUrl, midPath } from '../../util/format.js';
import { appApi } from '../../api/api_client.js';
import { toast } from '../../components/toast.js';
import { copyText } from '../../components/copy.js';
import { ctx, ui, STATUS, status, loop, pauseRequested, projectName } from './context.js';
import { renderAll, schedulePoll, stopPanel } from '../project_panel.js';

const DOLLAR_UNITS = ['US$', 'USD', '$'];

function limitColor(l) {
  if (isNum(l.pct) && l.pct >= 85) return '#F06A5E';
  return { cost: '#F2B544', turns_without_progress: '#5BD38A', turn_minutes: '#8AA4FF' }[l.key] || '#A58BFF';
}

const meta = () => (ctx.state && ctx.state.meta) || {};
const statusInfo = () => (ctx.state && ctx.state.planning_required ? STATUS.planning : STATUS[status()] || STATUS.no_loop);
const turnNow = () => { const t = loop().turn; return isNum(t) ? t : null; };

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

/** Header line under the brand: "repo <path> [copy]" and, with a loop worktree, "worktree <path> · branch". */
export function RepoLine() {
  const r = repoInfo();
  if (!r.repo) return null;
  return html`<div class="repo-line mono" id="repo-line">
    <span class="repo-tag">repo</span><span class="repo-path" title=${r.repo}>${midPath(r.repo)}</span>
    <button type="button" class="repo-copy" title="copy the repo path" aria-label="copy the repo path" onClick=${() => copyText(r.repo, 'Repo path copied.')}>copy</button>
    ${r.worktree ? html`<span class="repo-tag">worktree</span><span class="repo-path" title=${r.worktree}>${midPath(r.worktree)}</span>${r.branch ? html`<span class="repo-tag">branch</span><span class="repo-path" title=${r.branch}>${r.branch}</span>` : null}` : null}
  </div>`;
}

/** Container flags of the header: #status-pill colors and the #brand-sub title (repo path). */
export function headerModel() {
  const state = ctx.state, st = statusInfo();
  return {
    pillStyle: { background: st.bg, borderColor: st.line },
    repo: (state && state.project_ref && state.project_ref.repo_path) || (state && state.repo) || '',
  };
}

/** Content of #status-pill: dot + label (the pill colors are set on the container). */
export function StatusPill() {
  const st = statusInfo(), turn = turnNow();
  return html`
    <span class=${'dot' + (st.pulse ? ' pulse' : '')} id="status-dot" style=${{ background: st.c }}></span>
    <span class="mono" id="status-text" style=${{ color: st.c }}>${st.label + (turn != null ? ` · turn ${turn}` : '')}</span>`;
}

function Limit({ x }) {
  const c = limitColor(x);
  const pctW = isNum(x.pct) ? Math.max(0, Math.min(100, x.pct)) : 0;
  const val = isNum(x.value) ? (DOLLAR_UNITS.includes(x.unit) ? '$' + num(x.value) : num(x.value) + (x.unit ? ' ' + x.unit : '')) : DASH;
  const mx = isNum(x.max) ? num(x.max) : DASH;
  return html`<div class="limit">
    <div class="limit-label">${x.label || x.key}</div>
    <div class="limit-val"><span>${val}</span><span class="of">${' / ' + mx}</span></div>
    <div class="bar"><div style=${{ width: pctW + '%', background: c }}></div></div>
  </div>`;
}

/** Content of #limits: one bar per loop limit. */
export function Limits() {
  return list(ctx.state && ctx.state.limits).map((x, i) => html`<${Limit} key=${x.key || i} x=${x} />`);
}

function askShutdown() {
  ui.confirmShutdown = true; renderAll();
  const b = $('btn-shutdown-no'); if (b) b.focus();
}

/** Content of #header-actions: PR chip, pause, shut down (with inline confirmation). */
export function HeaderActions() {
  const m = meta(), prU = httpUrl(m.pr), n = prU && prU.match(/\/pull\/(\d+)/);
  return html`
    ${prU ? html`<a class="btn btn-sm pr-chip" href=${prU} target="_blank" rel="noopener noreferrer"
        title=${'open pull request' + (m.pr_status ? ' · ' + m.pr_status : '')}>${'PR ' + (n ? '#' + n[1] : '↗') + (m.pr_status ? ' · ' + m.pr_status : '')}</a>` : null}
    ${status() !== 'running' ? null : pauseRequested()
      ? html`<button type="button" class="btn btn-pending" disabled id="btn-pause">Pausing after the turn…</button>`
      : html`<button type="button" class="btn" id="btn-pause" disabled=${!!ui.busy.pause} onClick=${doPause}>Pause after the turn</button>`}
    ${ui.confirmShutdown
      ? html`<div class="confirm-inline" role="group" aria-label="Confirm shutting down the app">
          <span>Shut down the app? Loops already started keep running as separate processes.</span>
          <button type="button" class="btn btn-danger btn-sm" id="btn-shutdown-yes" disabled=${!!ui.busy.shutdown} onClick=${doShutdown}>Yes, shut down</button>
          <button type="button" class="btn btn-sm" id="btn-shutdown-no" onClick=${cancelShutdown}>Cancel</button>
        </div>`
      : html`<button type="button" class="btn btn-ghost" id="btn-shutdown" title="shuts down the whole app (every project)" onClick=${askShutdown}>Shut down app</button>`}`;
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
