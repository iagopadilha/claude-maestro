/* "Back to planning" (navbar button): reopens the planning of a project that already left it.
   - GET planning/reopen-check lists the worktrees that going back removes, with what is at risk (uncommitted changes, unpushed commits).
   - POST planning/reopen {confirm_remove_worktrees} does it; 409 loop_running / needs_confirm are shown, never guessed.
   The back end decides everything; this dialog only shows it. */
import { html } from '../../util/preact.js';
import { list, txt } from '../../util/format.js';
import { toast } from '../../components/toast.js';
import { openDialog } from '../../components/dialog.js';
import { ctx } from './context.js';
import { renderAll, schedulePoll } from '../project_panel.js';
import { reloadProgress } from './progress_trail.js';
import { openReposTab } from './planning.js';

const REASON_FALLBACK = { dirty: 'Uncommitted changes will be lost.', unpushed: 'Commits that were not pushed. The branch is kept.' };
const reasons = (w) => list(w.reasons).map((x) => x.text || REASON_FALLBACK[x.code] || x.code);
const atRisk = (w) => !!(w.dirty || w.unpushed || list(w.reasons).length);
const errText = (e) => (e.status === 404 ? 'The server does not offer "reopen planning" yet.' : e.message);

/** Opens the dialog. Returns nothing; on success the screen switches to planning on the Repositories tab. */
export function openReopenPlanning() {
  const s = { check: null, error: null, confirm: false, busy: false, api: null };
  const blocked = () => !!(s.check && s.check.blocked_by === 'loop_running');
  const trees = () => list(s.check && s.check.worktrees);
  const needsConfirm = () => trees().some(atRisk);

  const body = () => html`<div class="rp-dlg">
    <p class="dlg-text">Going back to planning removes these worktrees; your own folders and the specs stay.</p>
    <p class="dlg-text">Repositories are set in planning: you will land on the Repositories tab.</p>
    ${s.error ? html`<div class="nl-box nl-bad" role="alert">${s.error}</div>`
      : !s.check ? html`<div class="hint" role="status">Checking the worktrees…</div>`
      : html`
      ${blocked() ? html`<div class="nl-box nl-warn" role="alert">The loop is running: stop the loop first.</div>` : null}
      ${trees().length ? html`<ul class="ws-dlg-list ro-list" aria-label="Worktrees that will be removed">${trees().map((w, i) => html`<li key=${i} class="ro-item">
        <b class="mono">${txt(w.repo)}</b>${w.branch ? html` <span class="mono dim">${w.branch}</span>` : null}
        <div class="mono dim ro-path">${txt(w.path)}</div>
        ${reasons(w).map((t, j) => html`<div key=${j} class="ws-dlg-warn ro-warn">${t}</div>`)}
        ${w.unpushed ? html`<div class="ro-warn dim">The branch is kept so the commits are not lost.</div>` : null}</li>`)}</ul>`
        : html`<div class="hint">No worktree exists: nothing will be removed.</div>`}
      ${needsConfirm() && !blocked() ? html`<label class="ro-confirm"><input type="checkbox" id="ro-confirm" checked=${s.confirm} disabled=${s.busy}
        onChange=${(e) => { s.confirm = e.currentTarget.checked; draw(); }} /> <span>Remove these worktrees anyway</span></label>` : null}`}
  </div>`;
  const actions = () => {
    const ready = !!s.check && !s.error && !blocked() && s.check.can_reopen !== false && (!needsConfirm() || s.confirm) && !s.busy;
    return [
      { text: 'Cancel', id: 'ro-cancel', onClick: (a) => a.close() },
      { text: s.busy ? 'Going back…' : 'Go back to planning', id: 'ro-go', cls: 'btn-primary', disabled: !ready, onClick: go },
    ];
  };
  const draw = () => { if (s.api) { s.api.setContent(body()); s.api.setActions(actions()); } };

  async function go() {
    s.busy = true; draw();
    try {
      const r = await ctx.api.post('planning/reopen', { confirm_remove_worktrees: needsConfirm() && s.confirm }, 120000);
      const n = list(r.removed).length;
      s.api.close();
      toast(n ? `Back to planning. ${n} worktree${n === 1 ? '' : 's'} removed.` : 'Back to planning.', 'ok');
      ctx.state = { ...ctx.state, planning_required: true };
      openReposTab();
      renderAll(); schedulePoll(0); reloadProgress();
    } catch (e) {
      s.busy = false;
      const b = e.body || {};
      if (e.status === 409 && b.code === 'needs_confirm') {
        s.check = { ...(s.check || {}), worktrees: list(b.worktrees).length ? b.worktrees : trees(), can_reopen: true, blocked_by: null };
        s.confirm = false; toast('Check the worktrees and confirm to continue.', 'err');
      } else if (e.status === 409 && b.code === 'loop_running') {
        s.check = { ...(s.check || {}), blocked_by: 'loop_running' }; toast('The loop is running: stop it first.', 'err');
      } else toast('Not reopened: ' + errText(e), 'err');
      draw();
    }
  }

  s.api = openDialog({ title: 'Back to planning?', content: body(), actions: actions(), width: 560 });
  ctx.api.get('planning/reopen-check', 30000).then((c) => { s.check = c || {}; })
    .catch((e) => { s.error = 'Could not check: ' + errText(e); toast('Could not check: ' + errText(e), 'err'); })
    .finally(draw);
}
