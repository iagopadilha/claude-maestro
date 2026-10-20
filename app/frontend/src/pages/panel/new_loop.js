/* "New loop" panel: approve the roadmap, check (loop/check) and start (loop/start). Without an approved
   roadmap the Start button stays locked: run_loop refuses a roadmap that is not approved. */
import { html } from '../../util/preact.js';
import { list } from '../../util/format.js';
import { toast } from '../../components/toast.js';
import { copyText } from '../../components/copy.js';
import { ctx, ui, NL_STATES, status, approval } from './context.js';
import { renderAll, schedulePoll } from '../project_panel.js';
import { reloadProgress } from './progress_trail.js';
import { WorkspaceNotice, watchWorkspace } from './workspace.js';

const START_WAIT_MS = 40000;
const NL_DEFAULTS = { dispatch: 'auto', wave: '1', push: true, setup: '', budget: '5', max_cost: '30', timeout: '60', branch: '', continue_on_blocked: false, pause_on_blocked: true };
export const nl = { form: { ...NL_DEFAULTS }, check: null, checking: false, checkErr: null, starting: null, startErr: null, built: false, approving: false, apprErr: null, open: null, details: false, dispatch: null };
const nlNum = (v) => (String(v).trim() === '' ? NaN : Number(v));
export const APPROVAL_NOTICE = 'The approval covers today\'s features and stages. If you later change features or stages in ROADMAP.md, the approval stops being valid and you need to approve again.';

function nlErrors(f) {
  const e = {};
  const w = nlNum(f.wave); if (f.dispatch === 'custom' && (!Number.isInteger(w) || w < 1 || w > 8)) e.wave = 'Use a whole number from 1 to 8.';
  const b = nlNum(f.budget); if (!Number.isFinite(b) || b < 0) e.budget = 'Use a number greater than or equal to 0.';
  const m = nlNum(f.max_cost); if (!Number.isFinite(m) || m < 0) e.max_cost = 'Use a number greater than or equal to 0.';
  const t = nlNum(f.timeout); if (!Number.isInteger(t) || t < 1 || t > 600) e.timeout = 'Use a whole number from 1 to 600.';
  if (f.setup.trim().length > 200) e.setup = 'At most 200 characters.';
  const br = f.branch.trim();
  if (br && (!/^[A-Za-z0-9._/-]{1,80}$/.test(br) || br.includes('..') || br.startsWith('-'))) e.branch = 'Only letters, digits, dot, dash, underscore and slash (up to 80, no "..", not starting with "-").';
  return e;
}
// e2e is no longer asked here (the roadmap defines it): the field is omitted and the back end default applies.
// Auto sends no wave: the back end computes it from the approved roadmap. Custom sends the number.
const nlCheckBody = (f) => ({ ...(f.dispatch === 'custom' ? { wave: Number(f.wave) } : {}), push: !!f.push, setup: f.setup.trim() });
const nlStartBody = (f) => ({ ...nlCheckBody(f), budget: Number(f.budget), max_cost: Number(f.max_cost), timeout: Number(f.timeout), branch: f.branch.trim(), continue_on_blocked: !!f.continue_on_blocked, pause_on_blocked: !!f.pause_on_blocked });
export const nlStartingNow = () => !!(nl.starting && Date.now() - nl.starting.since < START_WAIT_MS);

/** Effective dispatch for the bar: Auto shows the computed wave of the last check; Custom shows the typed number. */
function dispatchSummary(f) {
  const d = nl.dispatch;
  if (f.dispatch === 'custom') return 'Dispatch: ' + (f.wave || '?') + ' (custom)';
  return 'Dispatch: Auto' + (d && d.auto ? ' (' + d.wave + ')' : '') + (d && d.auto && d.reason ? ' · ' + d.reason : '');
}

function nlTouched() { nl.startErr = null; nl.checkErr = null; renderAll(); }

/** One form field: label, control (text/number input or inline checkbox), hint and its error line. */
function Field({ k, label, type, extra, hint, err }) {
  const id = 'nl-' + k, errId = 'nl-err-' + k;
  const control = type === 'checkbox'
    ? html`<input id=${id} type="checkbox" aria-describedby=${errId} checked=${!!nl.form[k]} onChange=${(e) => { nl.form[k] = e.currentTarget.checked; if (e.currentTarget.checked && k === 'continue_on_blocked') nl.form.pause_on_blocked = false; if (e.currentTarget.checked && k === 'pause_on_blocked') nl.form.continue_on_blocked = false; nlTouched(); }} />`
    : html`<input id=${id} type=${type} autocomplete="off" spellcheck=${false} ...${extra} aria-describedby=${errId}
        value=${nl.form[k]} onInput=${(e) => { nl.form[k] = e.currentTarget.value; nlTouched(); }} />`;
  return html`<div class="nl-field">
    ${type === 'checkbox' ? html`<label class="nl-check" for=${id}>${control}<span>${label}</span></label>` : html`<label class="nl-label" for=${id}>${label}</label>`}
    ${type === 'checkbox' ? null : control}
    <div class="nl-hint">${hint}</div>
    <div class="nl-err" role="alert" id=${errId}>${err || ''}</div>
  </div>`;
}

/** Approves the roadmap (POST roadmap/approve with confirm:true). Called by the user's click. */
export async function approveRoadmap(thenStart) {
  if (nl.approving) return false;
  nl.approving = true; nl.apprErr = null; renderAll();
  let ok = false;
  try {
    await ctx.api.post('roadmap/approve', { confirm: true });
    toast('Roadmap approved.', 'ok');
    ok = true;
    ctx.state = { ...ctx.state, roadmap_approval: { ...approval(), approved: true, reason: null } };
    nl.approving = false; nl.check = null; renderAll();
    schedulePoll(500); reloadProgress();
    if (thenStart && Object.keys(nlErrors(nl.form)).length === 0) {
      await nlRun();
    }
  } catch (e) { nl.apprErr = e.message; toast('Roadmap not approved: ' + e.message, 'err'); }
  finally { nl.approving = false; renderAll(); }
  return ok;
}
function Approval() {
  const a = approval();
  if (a.approved) return html`<div class="nl-box nl-ok"><span class="chip">ROADMAP APPROVED</span><span>${` ${a.features} features${a.at ? ', on ' + String(a.at).replace('T', ' ').slice(0, 16) : ''}.`}</span></div>`;
  return html`<div class="nl-box nl-warn">
      <strong>Roadmap not approved yet: without approval the loop does not start. </strong>${a.reason || 'Review the roadmap on screen and approve it to unlock the loop.'}
      <div class="hint">${APPROVAL_NOTICE}</div>
      <div class="card-row">
        <button type="button" class="btn" id="btn-approve" disabled=${nl.approving} onClick=${() => approveRoadmap(false)}>Approve roadmap</button>
        <button type="button" class="btn btn-primary" id="btn-approve-start" disabled=${nl.approving} onClick=${() => approveRoadmap(true)}>Approve and start loop</button>
      </div>
    </div>
    ${nl.apprErr ? html`<div class="nl-box nl-bad" role="alert">${nl.apprErr}</div>` : null}`;
}
async function nlCheck() {
  const f = nl.form, sig = JSON.stringify(f);
  if (nl.checking || Object.keys(nlErrors(f)).length) return;
  nl.checking = true; nl.checkErr = null; nl.startErr = null; renderAll();
  try {
    const res = await ctx.api.post('loop/check', nlCheckBody(f), 70000);
    nl.check = { sig, res: { ready: !!res.ready, problems: list(res.problems), warnings: list(res.warnings), command: res.command || '' } };
    if (res.dispatch && Number.isInteger(res.dispatch.wave)) nl.dispatch = { wave: res.dispatch.wave, auto: !!res.dispatch.auto, reason: String(res.dispatch.reason || '') };
  } catch (e) { nl.check = null; nl.checkErr = e.message; }
  finally { nl.checking = false; renderAll(); }
}
/** The one primary action: checks first (result shown inline) and starts only when the check is READY. */
async function nlRun() {
  if (nlStartingNow() || nl.checking || Object.keys(nlErrors(nl.form)).length || !approval().approved) return;
  if (!(nl.check && nl.check.sig === JSON.stringify(nl.form) && nl.check.res.ready)) { nl.open = true; await nlCheck(); }
  await nlStart();
}
/** Expands the card (used by the trail button). */
export function nlExpand() { nl.open = true; }
async function nlStart() {
  const f = nl.form;
  if (nlStartingNow() || Object.keys(nlErrors(f)).length || !(nl.check && nl.check.sig === JSON.stringify(f) && nl.check.res.ready)) return;
  nl.startErr = null; nl.starting = { since: Date.now() }; renderAll();
  try {
    await ctx.api.post('loop/start', nlStartBody(f), 70000);
    watchWorkspace();
    setTimeout(renderAll, START_WAIT_MS + 200);
    schedulePoll(1000);
  } catch (e) {
    nl.starting = null; nl.check = null;
    nl.startErr = { msg: e.message, problems: list(e.body && e.body.problems) };
    renderAll();
  }
}
const Items = ({ items }) => html`<ul>${items.map((p, i) => html`<li key=${i}>${String(p)}</li>`)}</ul>`;
function CheckStatus({ fresh }) {
  const c = nl.check, r = c && c.res;
  return html`
    ${nl.checking ? html`<div class="nl-msg"><div class="spinner" aria-hidden="true"></div><span>checking…</span></div>` : null}
    ${nl.checkErr ? html`<div class="nl-box nl-bad" role="alert"><strong>Could not check: </strong>${nl.checkErr}</div>` : null}
    ${nl.startErr ? html`<div class="nl-box nl-bad" role="alert"><strong>The loop did not start: </strong>${nl.startErr.msg}${nl.startErr.problems.length ? html`<${Items} items=${nl.startErr.problems} />` : null}</div>` : null}
    ${!nl.starting ? null : nlStartingNow()
      ? html`<div class="nl-msg" role="status"><div class="spinner" aria-hidden="true"></div><span>starting the loop…</span></div>`
      : html`<div class="nl-box nl-warn" role="alert">The loop has not shown up as running after 40 s. It may have failed to start: see the file .specs/project/loop/internal/monitor-relaunch.log in the worktree. If you want, click Start loop to retry.</div>`}
    ${c && !fresh ? html`<div class="nl-box nl-warn">You changed something after the last check. Click Start loop to check again.</div>`
      : c && !nl.checking ? html`<div class=${'nl-box ' + (r.ready ? 'nl-ok' : 'nl-bad')}>
          <div class="nl-verdict"><span class="chip">${r.ready ? 'READY' : 'PROBLEMS'}</span><span>${r.ready ? 'You can start the loop.' : 'Fix what is below and check again.'}</span></div>
          ${r.problems.length ? html`<div><div class="small-label">Problems</div><${Items} items=${r.problems} /></div>` : null}
          ${r.warnings.length ? html`<div><div class="small-label">Warnings</div><${Items} items=${r.warnings} /></div>` : null}
          ${r.command ? html`<div><div class="small-label">Command</div>
            <div class="cmd-row"><pre class="cmd">${r.command}</pre>
              <button type="button" class="btn btn-sm" onClick=${() => copyText(r.command, 'Command copied.')}>Copy</button></div></div>` : null}
        </div>` : null}`;
}

/** Content of #newloop-zone: the "Start a loop" card, one slim bar when collapsed. */
export function NewLoop() {
  const f = nl.form, errs = nlErrors(f), valid = !Object.keys(errs).length;
  const fresh = !!(nl.check && nl.check.sig === JSON.stringify(f));
  const busy = nlStartingNow(), approved = approval().approved;
  const open = nl.open === null ? !approved : nl.open;
  const F = (k, label, type, extra, hint) => html`<${Field} k=${k} label=${label} type=${type} extra=${extra} hint=${hint} err=${errs[k]} />`;
  const hint = !valid ? 'Fix the marked fields.' : !approved ? 'Approve the roadmap to unlock the start.' : '';
  const summary = `${dispatchSummary(f)} · ${f.push ? 'push + PR at the end' : 'no push'}`;
  const working = nl.checking || busy;
  const toggle = () => { nl.open = !open; renderAll(); };
  const startLabel = nl.checking ? 'Checking…' : busy ? 'Starting…' : 'Start loop';
  return html`<section class=${'card card-new nl-card' + (open ? ' is-open' : '')} aria-labelledby="nl-title">
    <div class="nl-bar">
      <button type="button" class="nl-toggle" id="nl-toggle" aria-expanded=${open} aria-controls="nl-body" onClick=${toggle}>
        <span class="nl-caret" aria-hidden="true">${open ? '▾' : '▸'}</span><span class="nl-bar-title" id="nl-title">Start a loop</span></button>
      <span class="nl-sum">${approved ? summary : 'Roadmap not approved yet'}</span>
      ${approved && fresh && nl.check && !nl.checking ? html`<span class=${'chip nl-chip ' + (nl.check.res.ready ? 'nl-chip-ok' : 'nl-chip-bad')}>${nl.check.res.ready ? 'READY' : 'PROBLEMS'}</span>` : null}
      <span class="nl-bar-actions">
        ${approved
          ? html`<button type="button" class="btn btn-primary btn-sm" id="btn-start" title="Runs the checks first and starts the loop only if everything is ready" disabled=${!valid || working} onClick=${nlRun}>${startLabel}</button>`
          : html`<button type="button" class="btn btn-primary btn-sm" id="btn-approve-start-bar" disabled=${nl.approving || !valid} onClick=${() => approveRoadmap(true)}>Approve and start loop</button>`}
      </span>
    </div>
    <div class="nl-body" id="nl-body" hidden=${!open}>
      ${approved ? null : html`<div class="nl-appr" aria-live="polite"><${Approval} /></div>`}
      <div class="nl-grid">
        <div class="nl-field nl-dispatch">
          <span class="nl-label" id="nl-dispatch-label">Dispatch</span>
          <div class="nl-seg" role="group" aria-labelledby="nl-dispatch-label">
            ${['auto', 'custom'].map((m) => html`<button type="button" key=${m} class=${'nl-seg-btn' + (f.dispatch === m ? ' is-on' : '')} id=${'nl-dispatch-' + m} aria-pressed=${f.dispatch === m}
              onClick=${() => { f.dispatch = m; nlTouched(); }}>${m === 'auto' ? 'Auto' : 'Custom'}</button>`)}
          </div>
          ${f.dispatch === 'auto'
            ? html`<div class="nl-hint" id="nl-dispatch-hint">${nl.dispatch && nl.dispatch.auto ? `Auto: ${nl.dispatch.wave} at a time (${nl.dispatch.reason}).` : 'Auto: the loop picks how many features run at once from the roadmap (independent features, up to 3). Run Check only to see the value.'}</div>`
            : html`<div class="nl-custom">${F('wave', 'Features at once (1 to 8)', 'number', { min: 1, max: 8, step: 1, inputmode: 'numeric' }, 'How many features the loop may work on at the same time.')}</div>`}
        </div>
        ${F('push', 'Push the branch + open the PR at the end', 'checkbox', null, 'Sends only the feature branch. Off: nothing leaves your machine.')}
      </div>
      <details class="nl-adv">
        <summary>Advanced limits</summary>
        <div class="nl-grid">
          ${F('budget', 'Max spend per turn ($)', 'number', { min: 0, step: 'any', inputmode: 'decimal' }, 'A turn that goes over this stops. Default 5.')}
          ${F('max_cost', 'Max spend for the whole loop ($)', 'number', { min: 0, step: 'any', inputmode: 'decimal' }, 'Sum of every turn. Default 30.')}
          ${F('timeout', 'Max time per turn (min)', 'number', { min: 1, max: 600, step: 1 }, 'A longer turn is cut. Default 60.')}
          ${F('branch', 'Branch name', 'text', { placeholder: 'empty = automatic' }, 'Working branch of the loop.')}
          ${F('setup', 'Extra setup instruction', 'text', { placeholder: 'e.g. dotnet restore' }, 'Optional command or hint run before the loop (up to 200 characters), e.g. a restore the worktree lacks. Empty is fine.')}
          ${F('continue_on_blocked', 'If a feature gets stuck, carry on with the others', 'checkbox', null, 'The stuck feature goes to quarantine.')}
          ${F('pause_on_blocked', 'If a feature gets stuck, pause and call me (recommended)', 'checkbox', null, 'The loop stops and notifies you; you decide to try again or skip it.')}
        </div>
      </details>
      <div class="nl-note"><span>Start creates a separate worktree; your working copy is not touched.</span>
        <button type="button" class="nl-link" aria-expanded=${nl.details} onClick=${() => { nl.details = !nl.details; renderAll(); }}>Details</button></div>
      ${nl.details ? html`<div class="nl-details"><div>If this project already has a loop worktree, the loop continues from it. Check only creates nothing and spends nothing: it shows the pre-flight result and the exact command.</div><div class="nl-status" aria-live="polite"><${WorkspaceNotice} /></div></div>` : null}
      <div class="nl-actions">
        <button type="button" class="btn btn-sm" id="btn-check" title="Pre-flight check only: creates nothing, spends nothing, shows the command" disabled=${!valid || working} onClick=${nlCheck}>Check only</button>
        <span class="hint">${hint}</span>
      </div>
    </div>
    <div class="nl-status nl-result" aria-live="polite"><${CheckStatus} fresh=${fresh} /></div>
  </section>`;
}

/** #newloop-zone: shown with no loop, or on demand (New loop) in a finished state; the card is built on its first show. */
export function nlZone() {
  const s = status();
  const show = !!ctx.state && (s === 'no_loop' || (NL_STATES.includes(s) && ui.showNew));
  if (show) nl.built = true;
  return { show, built: nl.built };
}

/** When /state shows the loop running: clears what the panel kept from the start (the caller renders). */
export function nlOnLoopRunning() {
  if (nl.starting) { nl.starting = null; toast('Loop started.', 'ok'); }
  ui.showNew = false; nl.check = null; nl.startErr = null;
}
