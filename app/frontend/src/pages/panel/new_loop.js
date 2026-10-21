/* Request helpers of the loop start (the card that used to live here is now the Start dialog, start_loop_dialog.js).
   - GET loop/settings: what planning decided (ROADMAP header) + problems + integration branch + workspace + models.
   - POST loop/check: pre-flight only (creates nothing, spends nothing).
   - POST loop/start: only the two run choices (push, remove_worktree_on_finish); every limit comes from the ROADMAP header.
   - POST roadmap/approve: without an approved roadmap run_loop refuses to start.
   The dialog owns its own screen state; `nl` keeps only what must survive the dialog being closed (a start in flight, an approval in flight). */
import { list } from '../../util/format.js';
import { toast } from '../../components/toast.js';
import { ctx, ui, approval } from './context.js';
import { renderAll, schedulePoll } from '../project_panel.js';
import { reloadProgress } from './progress_trail.js';
import { watchWorkspace } from './workspace.js';
import { openStartLoopDialog } from './start_loop_dialog.js';

export const START_WAIT_MS = 40000;
export const nl = { starting: null, approving: false, apprErr: null, onRunning: null };
export const APPROVAL_NOTICE = 'The approval covers today\'s features and stages. If you later change features or stages in ROADMAP.md, the approval stops being valid and you need to approve again.';

export const nlStartingNow = () => !!(nl.starting && Date.now() - nl.starting.since < START_WAIT_MS);

/** GET loop/settings -> { settings, problems, defaults, integration_branch, workspace, models } with the lists made safe. */
export async function requestLoopSettings() {
  const data = await ctx.api.get('loop/settings', 15000);
  return {
    settings: (data && data.settings) || {}, problems: list(data && data.problems), defaults: (data && data.defaults) || {}, missing: list(data && data.missing),
    integrationBranch: (data && data.integration_branch) || '', workspace: (data && data.workspace) || null, models: (data && data.models) || {},
  };
}

/** POST loop/check -> { ready, problems, warnings, command, dispatch }. The body carries only the push choice. */
export async function requestLoopCheck({ push }) {
  const res = await ctx.api.post('loop/check', { push: !!push }, 70000);
  return { ready: !!res.ready, problems: list(res.problems), warnings: list(res.warnings), command: res.command || '' };
}

/** POST loop/start with the two choices of the dialog; marks the start as in flight and asks the panel to look at /state soon. */
export async function requestLoopStart({ push, removeWorktree }) {
  nl.starting = { since: Date.now() };
  try {
    await ctx.api.post('loop/start', { push: !!push, remove_worktree_on_finish: !!removeWorktree }, 70000);
  } catch (e) {
    nl.starting = null;
    throw e;
  }
  watchWorkspace();
  setTimeout(renderAll, START_WAIT_MS + 200);
  schedulePoll(1000);
}

/** Approves the roadmap (POST roadmap/approve with confirm:true). Called by the user's click; `thenStart` opens the Start dialog after. */
export async function approveRoadmap(thenStart) {
  if (nl.approving) return false;
  nl.approving = true; nl.apprErr = null; renderAll();
  let ok = false;
  try {
    await ctx.api.post('roadmap/approve', { confirm: true });
    toast('Roadmap approved.', 'ok');
    ok = true;
    ctx.state = { ...ctx.state, roadmap_approval: { ...approval(), approved: true, reason: null } };
    nl.approving = false; renderAll();
    schedulePoll(500); reloadProgress();
    if (thenStart) openStartLoopDialog();
  } catch (e) { nl.apprErr = e.message; toast('Roadmap not approved: ' + e.message, 'err'); }
  finally { nl.approving = false; renderAll(); }
  return ok;
}

/** The old "expand the card" entry point (the trail's "Go to Start loop" button): opens the Start dialog. */
export function nlExpand() { openStartLoopDialog(); }

/** When /state shows the loop running: clears what the panel kept from the start (the caller renders). */
export function nlOnLoopRunning() {
  if (nl.starting) { nl.starting = null; toast('Loop started.', 'ok'); }
  ui.showNew = false;
  if (nl.onRunning) { const done = nl.onRunning; nl.onRunning = null; done(); }
}
