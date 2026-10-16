/* "New loop" panel: approve the roadmap, check (loop/check) and start (loop/start). Without an approved
   roadmap the Start button stays locked: run_loop refuses a roadmap that is not approved. */
import { html } from '../../util/preact.js';
import { list } from '../../util/format.js';
import { toast } from '../../components/toast.js';
import { copyText } from '../../components/copy.js';
import { ctx, ui, NL_STATES, status, approval } from './context.js';
import { renderAll, schedulePoll } from '../project_panel.js';
import { reloadProgress } from './progress_trail.js';

const START_WAIT_MS = 40000;
const NL_DEFAULTS = { wave: '1', e2e: false, push: true, setup: '', budget: '5', max_cost: '30', timeout: '60', branch: '', continue_on_blocked: false, pause_on_blocked: false };
export const nl = { form: { ...NL_DEFAULTS }, check: null, checking: false, checkErr: null, starting: null, startErr: null, built: false, approving: false, apprErr: null };
const nlNum = (v) => (String(v).trim() === '' ? NaN : Number(v));
export const APPROVAL_NOTICE = 'The approval covers today\'s features and stages. If you later change features or stages in ROADMAP.md, the approval stops being valid and you need to approve again.';

function nlErrors(f) {
  const e = {};
  const w = nlNum(f.wave); if (!Number.isInteger(w) || w < 1 || w > 8) e.wave = 'Use a whole number from 1 to 8.';
  const b = nlNum(f.budget); if (!Number.isFinite(b) || b < 0) e.budget = 'Use a number greater than or equal to 0.';
  const m = nlNum(f.max_cost); if (!Number.isFinite(m) || m < 0) e.max_cost = 'Use a number greater than or equal to 0.';
  const t = nlNum(f.timeout); if (!Number.isInteger(t) || t < 1 || t > 600) e.timeout = 'Use a whole number from 1 to 600.';
  if (f.setup.trim().length > 200) e.setup = 'At most 200 characters.';
  const br = f.branch.trim();
  if (br && (!/^[A-Za-z0-9._/-]{1,80}$/.test(br) || br.includes('..') || br.startsWith('-'))) e.branch = 'Only letters, digits, dot, dash, underscore and slash (up to 80, no "..", not starting with "-").';
  return e;
}
const nlCheckBody = (f) => ({ wave: Number(f.wave), e2e: !!f.e2e, push: !!f.push, setup: f.setup.trim() });
const nlStartBody = (f) => ({ ...nlCheckBody(f), budget: Number(f.budget), max_cost: Number(f.max_cost), timeout: Number(f.timeout), branch: f.branch.trim(), continue_on_blocked: !!f.continue_on_blocked, pause_on_blocked: !!f.pause_on_blocked });
export const nlStartingNow = () => !!(nl.starting && Date.now() - nl.starting.since < START_WAIT_MS);

function nlTouched() { nl.startErr = null; nl.checkErr = null; renderAll(); }

/** One form field: label, control (text/number input or inline checkbox), hint and its error line. */
function Field({ k, label, type, extra, hint, err }) {
  const id = 'nl-' + k, errId = 'nl-err-' + k;
  const control = type === 'checkbox'
    ? html`<input id=${id} type="checkbox" aria-describedby=${errId} checked=${!!nl.form[k]} onChange=${(e) => { nl.form[k] = e.currentTarget.checked; nlTouched(); }} />`
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
      await nlCheck();
      if (nl.check && nl.check.res.ready) await nlStart();
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
  } catch (e) { nl.check = null; nl.checkErr = e.message; }
  finally { nl.checking = false; renderAll(); }
}
async function nlStart() {
  const f = nl.form;
  if (nlStartingNow() || Object.keys(nlErrors(f)).length || !(nl.check && nl.check.sig === JSON.stringify(f) && nl.check.res.ready)) return;
  nl.startErr = null; nl.starting = { since: Date.now() }; renderAll();
  try {
    await ctx.api.post('loop/start', nlStartBody(f), 70000);
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
      : html`<div class="nl-box nl-warn" role="alert">The loop has not shown up as running after 40 s. It may have failed to start: see the file .specs/project/loop/internal/monitor-relaunch.log in the worktree. If you want, check again and retry.</div>`}
    ${c && !fresh ? html`<div class="nl-box nl-warn">You changed something after the last check. Check again to unlock the Start loop button.</div>`
      : c && !nl.checking ? html`<div class=${'nl-box ' + (r.ready ? 'nl-ok' : 'nl-bad')}>
          <div class="nl-verdict"><span class="chip">${r.ready ? 'READY' : 'PROBLEMS'}</span><span>${r.ready ? 'You can start the loop.' : 'Fix what is below and check again.'}</span></div>
          ${r.problems.length ? html`<div><div class="small-label">Problems</div><${Items} items=${r.problems} /></div>` : null}
          ${r.warnings.length ? html`<div><div class="small-label">Warnings</div><${Items} items=${r.warnings} /></div>` : null}
          ${r.command ? html`<div><div class="small-label">Command</div>
            <div class="cmd-row"><pre class="cmd">${r.command}</pre>
              <button type="button" class="btn btn-sm" onClick=${() => copyText(r.command, 'Command copied.')}>Copy</button></div></div>` : null}
        </div>` : null}`;
}

/** Content of #newloop-zone: the whole "Start a loop" card. */
export function NewLoop() {
  const f = nl.form, errs = nlErrors(f), valid = !Object.keys(errs).length;
  const fresh = !!(nl.check && nl.check.sig === JSON.stringify(f));
  const busy = nlStartingNow(), approved = approval().approved;
  const F = (k, label, type, extra, hint) => html`<${Field} k=${k} label=${label} type=${type} extra=${extra} hint=${hint} err=${errs[k]} />`;
  const hint = !valid ? 'Fix the marked fields.' : !approved ? 'Approve the roadmap to unlock the start.' : !fresh ? 'Check before starting.' : !nl.check.res.ready ? 'There are problems: the loop cannot start.' : '';
  return html`<section class="card card-new" aria-labelledby="nl-title">
    <div class="card-kicker">new loop</div>
    <h2 class="card-title" id="nl-title">Start a loop</h2>
    <div class="nl-appr" aria-live="polite"><${Approval} /></div>
    <div class="hint">Fill it in, click Check and, with everything ready, Start loop. Check creates nothing and spends nothing. If this project already has a loop worktree, the loop continues from it.</div>
    <div class="nl-grid">
      ${F('wave', 'Wave (1 to 8)', 'number', { min: 1, max: 8, step: 1 }, 'How many features the loop may work on at the same time.')}
      ${F('setup', 'Setup (optional)', 'text', { placeholder: 'e.g. install dependencies' }, 'Setup instruction passed to the loop. Up to 200 characters.')}
      ${F('e2e', 'Test the flow end to end (E2E)', 'checkbox', null, 'Runs the real app to validate. Takes longer.')}
      ${F('push', 'push the branch + PR at the end', 'checkbox', null, 'When it finishes, sends only the feature branch and opens the PR. Off, nothing leaves your machine.')}
    </div>
    <details class="nl-adv">
      <summary>Advanced limits</summary>
      <div class="nl-grid">
        ${F('budget', 'Max spend per turn ($)', 'number', { min: 0, step: 'any', inputmode: 'decimal' }, 'If a turn goes over this, it stops. Default: 5.')}
        ${F('max_cost', 'Max spend for the whole loop ($)', 'number', { min: 0, step: 'any', inputmode: 'decimal' }, 'Summing every turn. When reached, the loop stops. Default: 30.')}
        ${F('timeout', 'Max time per turn (min)', 'number', { min: 1, max: 600, step: 1 }, 'If a turn takes longer, it is cut. Default: 60.')}
        ${F('branch', 'Branch name (optional)', 'text', { placeholder: 'leave empty for the loop to choose' }, 'Working branch of the loop. Empty = automatic name.')}
        ${F('continue_on_blocked', 'If a feature gets stuck, carry on with the others', 'checkbox', null, 'The stuck feature goes to quarantine and the loop continues.')}
        ${F('pause_on_blocked', 'If a feature gets stuck, pause and call me', 'checkbox', null, 'The loop stops and waits for your decision.')}
      </div>
    </details>
    <div class="card-row">
      <button type="button" class="btn" id="btn-check" disabled=${!valid || nl.checking || busy} onClick=${nlCheck}>Check</button>
      <button type="button" class="btn btn-primary" id="btn-start" disabled=${!(valid && fresh && nl.check.res.ready && approved) || nl.checking || busy} onClick=${nlStart}>Start loop</button>
      <span class="hint">${hint}</span>
    </div>
    <div class="nl-status" aria-live="polite"><${CheckStatus} fresh=${fresh} /></div>
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
