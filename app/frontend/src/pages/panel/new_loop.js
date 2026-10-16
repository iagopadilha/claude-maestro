/* "New loop" panel: approve the roadmap, check (loop/check) and start (loop/start). Without an approved
   roadmap the Start button stays locked: run_loop refuses a roadmap that is not approved. */
import { $, h, fill } from '../../util/dom.js';
import { list } from '../../util/format.js';
import { toast } from '../../components/toast.js';
import { copyText } from '../../components/copy.js';
import { ctx, ui, NL_STATES, status, approval } from './context.js';
import { schedulePoll } from '../project_panel.js';
import { reloadProgress } from './progress_trail.js';

const START_WAIT_MS = 40000;
const NL_DEFAULTS = { wave: '1', e2e: false, push: true, setup: '', budget: '5', max_cost: '30', timeout: '60', branch: '', continue_on_blocked: false, pause_on_blocked: false };
export const nl = { form: { ...NL_DEFAULTS }, check: null, checking: false, checkErr: null, starting: null, startErr: null, built: false, approving: false, apprErr: null, els: { err: {} } };
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

function nlField(key, label, control, hint, inline) {
  const err = h('div', { class: 'nl-err', role: 'alert', id: 'nl-err-' + key });
  nl.els.err[key] = err;
  control.setAttribute('aria-describedby', 'nl-err-' + key);
  return h('div', { class: 'nl-field' },
    inline ? h('label', { class: 'nl-check', for: 'nl-' + key }, control, h('span', { text: label })) : h('label', { class: 'nl-label', for: 'nl-' + key, text: label }),
    inline ? null : control,
    h('div', { class: 'nl-hint', text: hint }), err);
}
function nlInput(key, type, extra) {
  if (type === 'checkbox') {
    const el = h('input', { id: 'nl-' + key, type: 'checkbox', onchange: () => { nl.form[key] = el.checked; nlTouched(); } });
    el.checked = !!nl.form[key]; return el;
  }
  const el = h('input', { id: 'nl-' + key, type, autocomplete: 'off', spellcheck: 'false', ...(extra || {}), oninput: () => { nl.form[key] = el.value; nlTouched(); } });
  el.value = nl.form[key]; return el;
}
function nlTouched() { nl.startErr = null; nl.checkErr = null; nlUpdate(); }

function buildNewLoop() {
  const N = nlInput, e = nl.els;
  e.btnCheck = h('button', { type: 'button', class: 'btn', id: 'btn-check', onclick: nlCheck }, 'Check');
  e.btnStart = h('button', { type: 'button', class: 'btn btn-primary', id: 'btn-start', onclick: nlStart }, 'Start loop');
  e.hint = h('span', { class: 'hint' });
  e.status = h('div', { class: 'nl-status', 'aria-live': 'polite' });
  e.appr = h('div', { class: 'nl-appr', 'aria-live': 'polite' });
  const adv = h('details', { class: 'nl-adv' },
    h('summary', { text: 'Advanced limits' }),
    h('div', { class: 'nl-grid' },
      nlField('budget', 'Max spend per turn ($)', N('budget', 'number', { min: 0, step: 'any', inputmode: 'decimal' }), 'If a turn goes over this, it stops. Default: 5.'),
      nlField('max_cost', 'Max spend for the whole loop ($)', N('max_cost', 'number', { min: 0, step: 'any', inputmode: 'decimal' }), 'Summing every turn. When reached, the loop stops. Default: 30.'),
      nlField('timeout', 'Max time per turn (min)', N('timeout', 'number', { min: 1, max: 600, step: 1 }), 'If a turn takes longer, it is cut. Default: 60.'),
      nlField('branch', 'Branch name (optional)', N('branch', 'text', { placeholder: 'leave empty for the loop to choose' }), 'Working branch of the loop. Empty = automatic name.'),
      nlField('continue_on_blocked', 'If a feature gets stuck, carry on with the others', N('continue_on_blocked', 'checkbox'), 'The stuck feature goes to quarantine and the loop continues.', true),
      nlField('pause_on_blocked', 'If a feature gets stuck, pause and call me', N('pause_on_blocked', 'checkbox'), 'The loop stops and waits for your decision.', true)));
  fill($('newloop-zone'), h('section', { class: 'card card-new', 'aria-labelledby': 'nl-title' },
    h('div', { class: 'card-kicker', text: 'new loop' }),
    h('h2', { class: 'card-title', id: 'nl-title', text: 'Start a loop' }),
    e.appr,
    h('div', { class: 'hint', text: 'Fill it in, click Check and, with everything ready, Start loop. Check creates nothing and spends nothing. If this project already has a loop worktree, the loop continues from it.' }),
    h('div', { class: 'nl-grid' },
      nlField('wave', 'Wave (1 to 8)', N('wave', 'number', { min: 1, max: 8, step: 1 }), 'How many features the loop may work on at the same time.'),
      nlField('setup', 'Setup (optional)', N('setup', 'text', { placeholder: 'e.g. install dependencies' }), 'Setup instruction passed to the loop. Up to 200 characters.'),
      nlField('e2e', 'Test the flow end to end (E2E)', N('e2e', 'checkbox'), 'Runs the real app to validate. Takes longer.', true),
      nlField('push', 'push the branch + PR at the end', N('push', 'checkbox'), 'When it finishes, sends only the feature branch and opens the PR. Off, nothing leaves your machine.', true)),
    adv,
    h('div', { class: 'card-row' }, e.btnCheck, e.btnStart, e.hint),
    e.status));
  nl.built = true;
  nlUpdate();
}

/** Approves the roadmap (POST roadmap/approve with confirm:true). Called by the user's click. */
export async function approveRoadmap(thenStart) {
  if (nl.approving) return false;
  nl.approving = true; nl.apprErr = null; nlUpdate();
  let ok = false;
  try {
    await ctx.api.post('roadmap/approve', { confirm: true });
    toast('Roadmap approved.', 'ok');
    ok = true;
    ctx.state = { ...ctx.state, roadmap_approval: { ...approval(), approved: true, reason: null } };
    nl.approving = false; nl.check = null; nlUpdate();
    schedulePoll(500); reloadProgress();
    if (thenStart && Object.keys(nlErrors(nl.form)).length === 0) {
      await nlCheck();
      if (nl.check && nl.check.res.ready) await nlStart();
    }
  } catch (e) { nl.apprErr = e.message; toast('Roadmap not approved: ' + e.message, 'err'); }
  finally { nl.approving = false; nlUpdate(); }
  return ok;
}
function nlApprovalNodes() {
  const a = approval();
  if (a.approved) return [h('div', { class: 'nl-box nl-ok' }, h('span', { class: 'chip', text: 'ROADMAP APPROVED' }), h('span', { text: ` ${a.features} features${a.at ? ', on ' + String(a.at).replace('T', ' ').slice(0, 16) : ''}.` }))];
  const out = [h('div', { class: 'nl-box nl-warn' },
    h('strong', { text: 'Roadmap not approved yet: without approval the loop does not start. ' }), a.reason || 'Review the roadmap on screen and approve it to unlock the loop.',
    h('div', { class: 'hint', text: APPROVAL_NOTICE }),
    h('div', { class: 'card-row' },
      h('button', { type: 'button', class: 'btn', id: 'btn-approve', disabled: nl.approving, onclick: () => approveRoadmap(false) }, 'Approve roadmap'),
      h('button', { type: 'button', class: 'btn btn-primary', id: 'btn-approve-start', disabled: nl.approving, onclick: () => approveRoadmap(true) }, 'Approve and start loop')))];
  if (nl.apprErr) out.push(h('div', { class: 'nl-box nl-bad', role: 'alert' }, nl.apprErr));
  return out;
}
async function nlCheck() {
  const f = nl.form, sig = JSON.stringify(f);
  if (nl.checking || Object.keys(nlErrors(f)).length) return;
  nl.checking = true; nl.checkErr = null; nl.startErr = null; nlUpdate();
  try {
    const res = await ctx.api.post('loop/check', nlCheckBody(f), 70000);
    nl.check = { sig, res: { ready: !!res.ready, problems: list(res.problems), warnings: list(res.warnings), command: res.command || '' } };
  } catch (e) { nl.check = null; nl.checkErr = e.message; }
  finally { nl.checking = false; nlUpdate(); }
}
async function nlStart() {
  const f = nl.form;
  if (nlStartingNow() || Object.keys(nlErrors(f)).length || !(nl.check && nl.check.sig === JSON.stringify(f) && nl.check.res.ready)) return;
  nl.startErr = null; nl.starting = { since: Date.now() }; nlUpdate();
  try {
    await ctx.api.post('loop/start', nlStartBody(f), 70000);
    setTimeout(nlUpdate, START_WAIT_MS + 200);
    schedulePoll(1000);
  } catch (e) {
    nl.starting = null; nl.check = null;
    nl.startErr = { msg: e.message, problems: list(e.body && e.body.problems) };
    nlUpdate();
  }
}
function nlStatusNodes(fresh) {
  const out = [];
  if (nl.checking) out.push(h('div', { class: 'nl-msg' }, h('div', { class: 'spinner', 'aria-hidden': 'true' }), h('span', { text: 'checking…' })));
  if (nl.checkErr) out.push(h('div', { class: 'nl-box nl-bad', role: 'alert' }, h('strong', { text: 'Could not check: ' }), nl.checkErr));
  if (nl.startErr) {
    out.push(h('div', { class: 'nl-box nl-bad', role: 'alert' }, h('strong', { text: 'The loop did not start: ' }), nl.startErr.msg,
      nl.startErr.problems.length ? h('ul', {}, nl.startErr.problems.map((p) => h('li', { text: String(p) }))) : null));
  }
  if (nl.starting) {
    if (nlStartingNow()) out.push(h('div', { class: 'nl-msg', role: 'status' }, h('div', { class: 'spinner', 'aria-hidden': 'true' }), h('span', { text: 'starting the loop…' })));
    else out.push(h('div', { class: 'nl-box nl-warn', role: 'alert' }, 'The loop has not shown up as running after 40 s. It may have failed to start: see the file .specs/project/loop/internal/monitor-relaunch.log in the worktree. If you want, check again and retry.'));
  }
  const c = nl.check;
  if (c && !fresh) out.push(h('div', { class: 'nl-box nl-warn' }, 'You changed something after the last check. Check again to unlock the Start loop button.'));
  else if (c && !nl.checking) {
    const r = c.res;
    out.push(h('div', { class: 'nl-box ' + (r.ready ? 'nl-ok' : 'nl-bad') },
      h('div', { class: 'nl-verdict' }, h('span', { class: 'chip', text: r.ready ? 'READY' : 'PROBLEMS' }), h('span', { text: r.ready ? 'You can start the loop.' : 'Fix what is below and check again.' })),
      r.problems.length ? h('div', {}, h('div', { class: 'small-label', text: 'Problems' }), h('ul', {}, r.problems.map((p) => h('li', { text: String(p) })))) : null,
      r.warnings.length ? h('div', {}, h('div', { class: 'small-label', text: 'Warnings' }), h('ul', {}, r.warnings.map((p) => h('li', { text: String(p) })))) : null,
      r.command ? h('div', {}, h('div', { class: 'small-label', text: 'Command' }),
        h('div', { class: 'cmd-row' }, h('pre', { class: 'cmd', text: r.command }),
          h('button', { type: 'button', class: 'btn btn-sm', onclick: () => copyText(r.command, 'Command copied.') }, 'Copy'))) : null));
  }
  return out;
}
export function nlUpdate() {
  if (!nl.built) return;
  const f = nl.form, errs = nlErrors(f), valid = !Object.keys(errs).length;
  const fresh = !!(nl.check && nl.check.sig === JSON.stringify(f));
  const busy = nlStartingNow();
  for (const k of Object.keys(nl.els.err)) nl.els.err[k].textContent = errs[k] || '';
  nl.els.btnCheck.disabled = !valid || nl.checking || busy;
  nl.els.btnStart.disabled = !(valid && fresh && nl.check.res.ready && approval().approved) || nl.checking || busy;
  fill(nl.els.appr, nlApprovalNodes());
  nl.els.hint.textContent = !valid ? 'Fix the marked fields.' : !approval().approved ? 'Approve the roadmap to unlock the start.' : !fresh ? 'Check before starting.' : !nl.check.res.ready ? 'There are problems: the loop cannot start.' : '';
  fill(nl.els.status, nlStatusNodes(fresh));
}
export function nlVisibility() {
  const s = status();
  const show = !!ctx.state && (s === 'no_loop' || (NL_STATES.includes(s) && ui.showNew));
  if (show && !nl.built) buildNewLoop();
  else if (show && !nl.approving) nlUpdate();
  $('newloop-zone').hidden = !show;
}

/** When /state shows the loop running: clears what the panel kept from the start. */
export function nlOnLoopRunning() {
  if (nl.starting) { nl.starting = null; toast('Loop started.', 'ok'); }
  ui.showNew = false; nl.check = null; nl.startErr = null; nlUpdate();
}
