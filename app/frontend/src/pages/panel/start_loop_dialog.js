/* Start dialog (START-01..04, MODEL-03): opened by "Start loop" in the navbar. Shows what planning decided (read-only, from the ROADMAP
   header through GET loop/settings), the models, the two run choices, the workspace state and the notices that used to sit in the
   Start card. Buttons: Check only / Start loop. The back end decides every value; this screen only shows them.
   Start is disabled only by a readiness PROBLEM of the last check (warnings never block); a click checks first when the last check is stale. */
import { html, useState, useEffect } from '../../util/preact.js';
import { list } from '../../util/format.js';
import { openDialog } from '../../components/dialog.js';
import { copyText } from '../../components/copy.js';
import { ctx, approval } from './context.js';
import { registerRenderer } from '../project_panel.js';
import { UncommittedContextNotice, WorkspaceNotice, WorkspaceState, loadWorkspace } from './workspace.js';
import { BranchAndParallelInfo, requestBranchAndParallel } from './roadmap_preview.js';
import { APPROVAL_NOTICE, approveRoadmap, nl, nlStartingNow, requestLoopSettings, requestLoopCheck, requestLoopStart } from './new_loop.js';

// ROADMAP header name of each setting (the names of design.md "Data shapes"), used to match the `problems` of GET loop/settings
const HEADER_NAME = {
  dispatch: 'Dispatch', e2e: 'E2E', budget_turn: 'Max spend per turn', budget_loop: 'Max spend for the loop', minutes_turn: 'Max minutes per turn',
  branch: 'Branch', base_branch: 'Branch base', setup: 'Setup', when_stuck: 'When a feature gets stuck', push_pr: 'Push and PR',
};
const SETTING_ROWS = [
  ['dispatch', 'Dispatch'], ['e2e', 'E2E tests'], ['budget_turn', 'Max spend per turn'], ['budget_loop', 'Max spend for the whole loop'],
  ['minutes_turn', 'Max minutes per turn'], ['branch', 'Branch'], ['base_branch', 'Branch base'], ['setup', 'Extra setup instruction'], ['when_stuck', 'When a feature gets stuck'],
];
const BUILT_IN_DEFAULTS = { dispatch: 'auto', e2e: false, budget_turn: 5, budget_loop: 50, minutes_turn: 60, branch: '', base_branch: 'developer', setup: '', when_stuck: 'pause', push_pr: true };

/** Plain words of one setting value. */
function settingText(key, value) {
  if (key === 'dispatch') return value === 'auto' || value == null ? 'Auto (the loop picks how many features run at once from the roadmap)' : `${value} ${Number(value) === 1 ? 'feature' : 'features'} at a time`;
  if (key === 'e2e') return value ? 'Yes' : 'No';
  if (key === 'budget_turn' || key === 'budget_loop') return 'US$ ' + value;
  if (key === 'minutes_turn') return value + ' min';
  if (key === 'branch') return value ? String(value) : 'automatic';
  if (key === 'setup') return value ? String(value) : 'none';
  if (key === 'when_stuck') return value === 'continue' ? 'Quarantine it and carry on with the others' : 'Pause and notify me';
  return String(value);
}

/** The rows of "Decided in planning": value + note. A missing key shows the default "(default)"; an invalid one the default and what the ROADMAP says. */
export function decidedRows(info) {
  const problems = list(info && info.problems), settings = (info && info.settings) || {}, defaults = (info && info.defaults) || {};
  const missing = list(info && info.missing);  // the back end fills `settings` with the defaults: `missing` names the keys the ROADMAP header does not carry
  return SETTING_ROWS.map(([key, label]) => {
    const problem = problems.find((p) => p && String(p.name || '').toLowerCase() === HEADER_NAME[key].toLowerCase());
    const fallback = key === 'base_branch' && info && info.integration_branch ? info.integration_branch : key in defaults ? defaults[key] : BUILT_IN_DEFAULTS[key];  // the base branch default is what the back end resolved (environment, then developer)
    if (problem) return { key, label, text: settingText(key, fallback), note: `(invalid in the ROADMAP: ${problem.value})`, bad: true };
    if (missing.includes(key) || settings[key] === undefined || settings[key] === null) return { key, label, text: settingText(key, fallback), note: '(default)', bad: false };
    return { key, label, text: settingText(key, settings[key]), note: '', bad: false };
  });
}

const capital = (word) => { const text = String(word || ''); return text ? text.charAt(0).toUpperCase() + text.slice(1) : text; };

const dialogState = { handle: null };

/** Re-renders the component when the panel renders (the workspace store and the loop state are shared stores). */
function useFollowPanel() {
  const [, tick] = useState(0);
  useEffect(() => registerRenderer(() => tick((count) => count + 1)), []);
}

const Items = ({ items }) => html`<ul>${items.map((item, index) => html`<li key=${index}>${String(item)}</li>`)}</ul>`;

function Approval() {
  const approved = approval();
  if (approved.approved) return null;
  return html`<div class="nl-box nl-warn">
    <strong>Roadmap not approved yet: without approval the loop does not start. </strong>${approved.reason || 'Review the roadmap and approve it to unlock the loop.'}
    <div class="hint">${APPROVAL_NOTICE}</div>
    <div class="card-row"><button type="button" class="btn btn-primary" id="btn-approve" disabled=${nl.approving} onClick=${() => approveRoadmap(false)}>Approve roadmap</button></div>
    ${nl.apprErr ? html`<div class="nl-bad nl-box" role="alert">${nl.apprErr}</div>` : null}
  </div>`;
}

function DecidedInPlanning({ info }) {
  const rows = decidedRows(info);
  return html`<section class="sl-sec" aria-labelledby="sl-decided">
    <h3 class="sl-h" id="sl-decided">Decided in planning</h3>
    <dl class="sl-kv">${rows.map((row) => html`<${KeyValue} key=${row.key} row=${row} />`)}</dl>
    <div class="sl-muted">These come from the ROADMAP header. To change one, use Back to planning (no loop may be running).</div>
  </section>`;
}
const KeyValue = ({ row }) => html`<dt>${row.label}</dt><dd><span class=${row.bad ? 'sl-bad' : ''}>${row.text}</span>${row.note ? html` <span class=${'sl-note' + (row.bad ? ' sl-bad' : '')}>${row.note}</span>` : null}</dd>`;

function Models({ info }) {
  const models = (info && info.models) || {};
  const orchestrator = capital(models.orchestrator || 'opus'), agents = capital(models.agents || 'sonnet');
  return html`<div class="sl-models" id="sl-models">${`Orchestrator: ${orchestrator}; agents: ${agents}`}</div>`;
}

function Choices({ state, set, integrationBranch }) {
  const target = integrationBranch || 'the integration branch';
  return html`<section class="sl-sec" aria-labelledby="sl-choices">
    <h3 class="sl-h" id="sl-choices">For this run</h3>
    <label class="sl-choice" for="sl-push"><input id="sl-push" type="checkbox" checked=${state.push} onChange=${(e) => set({ push: e.currentTarget.checked })} />
      <span><b>${'Push the branch and open a PR to ' + target + ' at the end'}</b>
        <span class="sl-explain">Pushes only the loop/* branch and opens a pull request. It never merges and never deletes a branch. Off: nothing leaves your machine.</span></span></label>
    <label class="sl-choice" for="sl-remove"><input id="sl-remove" type="checkbox" checked=${state.removeWorktree} onChange=${(e) => set({ removeWorktree: e.currentTarget.checked })} />
      <span><b>Remove the loop worktree when I click Finish</b>
        <span class="sl-explain">Removing the worktree keeps the branch, the commits, the PR and the plan copied back to the repository. It never touches your original repository.</span></span></label>
  </section>`;
}

function CheckResult({ state }) {
  const result = state.check && state.check.result;
  return html`
    ${state.checking ? html`<div class="nl-msg"><div class="spinner" aria-hidden="true"></div><span>checking…</span></div>` : null}
    ${state.checkErr ? html`<div class="nl-box nl-bad" role="alert"><strong>Could not check: </strong>${state.checkErr}</div>` : null}
    ${state.startErr ? html`<div class="nl-box nl-bad" role="alert"><strong>The loop did not start: </strong>${state.startErr.message}${state.startErr.problems.length ? html`<${Items} items=${state.startErr.problems} />` : null}</div>` : null}
    ${!nl.starting ? null : nlStartingNow()
      ? html`<div class="nl-msg" role="status"><div class="spinner" aria-hidden="true"></div><span>starting the loop…</span></div>`
      : html`<div class="nl-box nl-warn" role="alert">The loop has not shown up as running after 40 s. It may have failed to start: see the file .specs/project/loop/internal/monitor-relaunch.log in the worktree. You can click Start loop to try again.</div>`}
    ${result && !state.checking ? html`<div class=${'nl-box ' + (result.ready ? 'nl-ok' : 'nl-bad')}>
      <div class="nl-verdict"><span class="chip">${result.ready ? 'READY' : 'PROBLEMS'}</span><span>${result.ready ? 'You can start the loop.' : 'Fix what is below and check again.'}</span></div>
      ${result.problems.length ? html`<div><div class="small-label">Problems</div><${Items} items=${result.problems} /></div>` : null}
      ${result.warnings.length ? html`<div><div class="small-label">Warnings</div><${Items} items=${result.warnings} /></div>` : null}
      ${result.command ? html`<div><div class="small-label">Command</div><div class="cmd-row"><pre class="cmd">${result.command}</pre>
        <button type="button" class="btn btn-sm" onClick=${() => copyText(result.command, 'Command copied.')}>Copy</button></div></div>` : null}
    </div>` : null}`;
}

function StartLoopBody({ api, onStarted }) {
  useFollowPanel();
  const [info, setInfo] = useState({ loading: true, error: null, data: null });
  const [state, setState] = useState({ push: true, removeWorktree: true, check: null, checking: false, checkErr: null, startErr: null, pushTouched: false });
  const [extra, setExtra] = useState(null);  // read-only base branch (with source) and max parallel tasks
  const set = (patch) => setState((current) => ({ ...current, ...patch, check: null, startErr: null, checkErr: null, pushTouched: current.pushTouched || 'push' in patch }));
  useEffect(() => {
    let alive = true;
    loadWorkspace();
    requestLoopSettings().then((data) => {
      if (!alive) return;
      setInfo({ loading: false, error: null, data });
      setState((current) => (current.pushTouched ? current : { ...current, push: data.settings.push_pr !== false }));
    }).catch((e) => alive && setInfo({ loading: false, error: e.message, data: null }));
    requestBranchAndParallel().then((value) => alive && setExtra(value)).catch(() => {});
    nl.onRunning = () => api.close();
    return () => { alive = false; if (nl.onRunning) nl.onRunning = null; };
  }, []);

  const approved = approval().approved, starting = nlStartingNow();
  const signature = JSON.stringify({ push: state.push });
  const fresh = !!(state.check && state.check.signature === signature);
  const blocked = !!(fresh && !state.check.result.ready);
  const working = state.checking || starting;
  const canAct = approved && !info.loading && !info.error && !working;

  const runCheck = async () => {
    setState((current) => ({ ...current, checking: true, checkErr: null, startErr: null }));
    try {
      const result = await requestLoopCheck({ push: state.push });
      setState((current) => ({ ...current, checking: false, check: { signature, result } }));
      return result;
    } catch (e) {
      setState((current) => ({ ...current, checking: false, check: null, checkErr: e.message }));
      return null;
    }
  };
  const start = async () => {
    if (!canAct) return;
    const result = fresh ? state.check.result : await runCheck();
    if (!result || !result.ready) return;
    try {
      await requestLoopStart({ push: state.push, removeWorktree: state.removeWorktree });
      if (onStarted) onStarted();
      setState((current) => ({ ...current }));
    } catch (e) {
      setState((current) => ({ ...current, check: null, startErr: { message: e.message, problems: list(e.body && e.body.problems) } }));
    }
  };

  if (info.loading) return html`<div class="nl-msg"><div class="spinner" aria-hidden="true"></div><span>reading what planning decided…</span></div>`;
  if (info.error) return html`<div class="nl-box nl-bad" role="alert"><strong>Could not read the loop settings: </strong>${info.error}</div>`;
  const reattachBranch = (info.data.workspace && info.data.workspace.reattach_branch) || '';
  const hint = !approved ? 'Approve the roadmap to unlock the start.' : blocked ? 'Fix the problems above and check again.' : '';
  return html`<div class="sl">
    <${Approval} />
    <${DecidedInPlanning} info=${info.data} />
    <${BranchAndParallelInfo} info=${extra} explain=${false} />
    <${Models} info=${info.data} />
    <${Choices} state=${state} set=${set} integrationBranch=${info.data.integrationBranch} />
    <section class="sl-sec" aria-labelledby="sl-workspace"><h3 class="sl-h" id="sl-workspace">Workspace</h3>
      <${WorkspaceState} reattachBranch=${reattachBranch} /><${WorkspaceNotice} /><${UncommittedContextNotice} />
      <div class="sl-muted">Start creates a separate worktree; your working copy is not touched. If this project already has a loop worktree, the loop continues from it.${reattachBranch ? html` It continues the branch <span class="mono">${reattachBranch}</span> (not finished).` : ' A finished or first run starts on a new loop branch.'}</div>
    </section>
    <div class="sl-status" aria-live="polite"><${CheckResult} state=${state} /></div>
    <div class="sl-actions">
      <button type="button" class="btn btn-sm" id="btn-check" title="Pre-flight check only: creates nothing, spends nothing, shows the command" disabled=${!canAct} onClick=${runCheck}>Check only</button>
      <button type="button" class="btn btn-primary btn-sm" id="btn-start" title="Runs the checks first and starts the loop only if everything is ready" disabled=${!canAct || blocked} onClick=${start}>${state.checking ? 'Checking…' : starting ? 'Starting…' : 'Start loop'}</button>
      <span class="hint">${hint}</span>
    </div>
  </div>`;
}

/** Opens the Start dialog. `state` and `project` default to the panel's own; `onStarted` runs once the back end accepted the start. */
export function openStartLoopDialog({ state = ctx.state, project = ctx.projectId, onStarted } = {}) {
  if (dialogState.handle) return dialogState.handle;
  const api = { close: () => dialogState.handle && dialogState.handle.close() };
  const handle = openDialog({
    title: 'Start loop', width: 720, cls: 'sl-dlg',
    content: html`<${StartLoopBody} api=${api} onStarted=${onStarted} />`, actions: [],
    onClose: () => { dialogState.handle = null; },
  });
  dialogState.handle = handle;
  return handle;
}
