/* What needs the user, by loop status. Since the navbar (NAV-01..03) there is no band at the top: the status indicator of the navbar
   opens the StatusDialog (mounted by app.js) whose body is one of the components here: Ask, ErrorBand, Stopped, Paused, CostCap, Done.
   Also the answer / resume senders, the Start dialog opener and the idle note. */
import { html } from '../../util/preact.js';
import { DASH, isNum, num, cost, dur, txt, list } from '../../util/format.js';
import { toast } from '../../components/toast.js';
import { Dialog } from '../../components/dialog.js';
import { ctx, ui, RESUME_WAIT_MS, status, loop, askKey, resumingNow, isPauseAsk, isRecommended } from './context.js';
import { renderAll, schedulePoll } from '../project_panel.js';
import { openErrors } from './errors.js';
import { openStartLoopDialog } from './start_loop_dialog.js';

/** Opens the Start dialog (the old "New loop" card). Other screens call it as "go to Start loop". */
export function openNewLoop() {
  if (!ctx.state) return;
  openStartLoopDialog({
    state: ctx.state, project: ctx.projectId,
    onStarted: () => { renderAll(); schedulePoll(0); },
  });
}

export async function sendAnswer(body, label) {
  if (ui.busy.answer) return;
  ui.busy.answer = true; renderAll();
  try { await ctx.api.post('answer', body); ui.resuming = { key: askKey(), since: Date.now(), label }; ui.freeText = ''; setTimeout(renderAll, RESUME_WAIT_MS + 300); }
  catch (e) { toast('Answer not sent: ' + e.message, 'err'); }
  finally { ui.busy.answer = false; renderAll(); }
}
export async function sendResume(label, body = {}) {
  if (ui.busy.answer) return;
  ui.busy.answer = true; renderAll();
  try { await ctx.api.post('resume', body); ui.resuming = { key: askKey(), since: Date.now(), label }; setTimeout(renderAll, RESUME_WAIT_MS + 300); }
  catch (e) { toast('Could not resume: ' + e.message, 'err'); }
  finally { ui.busy.answer = false; renderAll(); }
}

// ---------- cost cap (COST-02..04) ----------
const COST_CAP_REASON = /^cost cap reached/i, OLD_COST_CAP_REASON = /total cost cap exceeded/i;
const RAISE_STEP_USD = 25;
/** The loop stopped because the spend cap was hit: status `paused` with "cost cap reached", or the old `error` + "total cost cap exceeded". */
export function costCapPause() {
  const s = status(), reason = String(loop().reason || '');
  return (s === 'paused' && COST_CAP_REASON.test(reason)) || (s === 'error' && OLD_COST_CAP_REASON.test(reason));
}
/** Spent total and effective cap of the run, from /state: meta.max_cost_usd (raised in the panel) wins over the cost limit of the command. */
export function costNumbers() {
  const state = ctx.state || {}, m = state.meta || {};
  const limit = list(state.limits).find((x) => x.key === 'cost') || {};
  const spent = isNum(m.cost_usd) ? m.cost_usd : isNum(limit.value) ? limit.value : null;
  const cap = isNum(m.max_cost_usd) ? m.max_cost_usd : isNum(limit.max) ? limit.max : null;
  return { spent, cap };
}
const raise = { value: null, error: null, busy: false };
const usd = (v) => (isNum(v) ? v.toFixed(2) : DASH);
const defaultRaise = () => { const { cap, spent } = costNumbers(); return String(Math.ceil(((isNum(cap) ? cap : isNum(spent) ? spent : 0) + RAISE_STEP_USD) * 100) / 100); };
function raiseProblem(raw) {
  const { spent } = costNumbers(), v = Number(raw);
  if (raw === '' || !Number.isFinite(v)) return 'Type the new limit in US$.';
  return isNum(spent) && v <= spent ? `the new limit must be above US$ ${usd(spent)}` : null;
}
async function raiseLimit() {
  const value = raise.value == null ? defaultRaise() : raise.value, problem = raiseProblem(value);
  if (problem) { raise.error = problem; renderAll(); return; }
  raise.busy = true; raise.error = null; renderAll();
  try {
    await ctx.api.post('loop/raise-limit', { max_cost_usd: Number(value) });
    ui.resuming = { key: askKey(), since: Date.now(), label: 'new limit' }; raise.value = null;
    setTimeout(renderAll, RESUME_WAIT_MS + 300); schedulePoll(0);
    toast('Limit raised: the loop continues with the spent total kept.', 'ok');
    closeStatusDialog();
  } catch (e) { raise.error = e.message || 'The limit was not raised.'; }
  finally { raise.busy = false; renderAll(); }
}

// ---------- bodies of the status dialog ----------
function VerifyBlock() {
  const v = list(loop().to_check);
  if (!v.length) return null;
  return html`<div class="check"><div class="check-title">What to check before answering</div><ul>${v.map((x, i) => html`<li key=${i}>${String(x)}</li>`)}</ul></div>`;
}
function sendFree() {
  const t = ui.freeText.trim();
  if (!t) { toast('Write the answer before sending.', 'err'); return; }
  sendAnswer({ text: t }, 'answer');
}
function Stat({ k, v }) { return html`<div><div class="stat-k">${k}</div><div class="stat-v">${v}</div></div>`; }
function Resuming() {
  return html`<section class="band band-resume" role="status" aria-live="polite">
    <div class="spinner" aria-hidden="true"></div>
    <span class="band-text">${`Resuming the loop… your ${(ui.resuming && ui.resuming.label) || 'answer'} was received; the screen goes live again when it runs.`}</span>
  </section>`;
}
export function Ask() {
  const l = loop(), opts = list(l.options), pauseAsk = isPauseAsk(), busy = !!ui.busy.answer;
  return html`<div class="band-detail">
    <div class="question">${txt(l.reason)}</div>
    ${l.next ? html`<div class="hint">${'After the answer: ' + l.next}</div>` : null}
    ${opts.length ? html`<div class="opts" role="group" aria-label="Answer options">${opts.map((o, i) => html`
      <button key=${i} type="button" class=${'opt' + (isRecommended(o) ? ' opt-rec' : '')} disabled=${busy} onClick=${() => sendAnswer({ option: i + 1 }, `option ${i + 1}`)}>
        <span class="opt-n">${String(i + 1)}</span><span class="opt-label">${String(o)}</span>${isRecommended(o) ? html`<span class="tag">recommended</span>` : null}
      </button>`)}</div>` : null}
    <div><label class="field-label" for="free-text">${opts.length ? 'Or answer in your own words' : 'Your answer'}</label>
      <div class="free">
        <textarea id="free-text" rows="2" placeholder=${opts.length ? 'None fits? Write your own answer here.' : 'Write your answer here.'} disabled=${busy}
          value=${ui.freeText} onInput=${(e) => { ui.freeText = e.currentTarget.value; }}></textarea>
        <button type="button" class="btn btn-primary" disabled=${busy} onClick=${sendFree}>Send answer</button>
      </div></div>
    <${VerifyBlock} />
    ${!opts.length || pauseAsk ? html`<div class="card-row">
      <button type="button" class=${'btn' + (pauseAsk ? ' btn-primary' : '')} disabled=${busy} onClick=${() => sendResume('resume request')}>${pauseAsk ? 'Resume the loop' : 'Resume without answering'}</button>
      <span class="hint">the loop carries on from where it stopped</span></div>` : null}
  </div>`;
}
export function ErrorBand() {
  const l = loop(), busy = !!ui.busy.answer;
  return html`<div class="band-detail"><div class="cause">${txt(l.reason)}</div>
    ${l.next ? html`<div class="hint">${'Planned next step: ' + l.next}</div>` : null}<${VerifyBlock} />
    <div class="card-row">
      <button type="button" class="btn btn-danger" disabled=${busy} title="relaunches the loop from the point where it fell" onClick=${() => sendResume('retry')}>Try again</button>
      <button type="button" class="btn" title="every error of this loop in one place, with what to do" onClick=${() => { closeStatusDialog(); openErrors(); }}>Errors</button>
      <span class="hint">to change the roadmap, use Back to planning in the top bar</span>
    </div></div>`;
}
export function Stopped() {
  const l = loop(), busy = !!ui.busy.answer;
  return html`<div class="band-detail"><div class="question">The loop process is no longer running, but there was still work to do. It was probably closed or crashed. Resume relaunches the same command.</div>
    ${l.reason ? html`<div class="cause">${String(l.reason)}</div>` : null}
    <div class="card-row"><button type="button" class="btn btn-primary" disabled=${busy} title="relaunches the loop with the same command" onClick=${() => sendResume('resume request')}>Resume</button></div></div>`;
}
export function Paused() {
  const l = loop(), busy = !!ui.busy.answer;
  return html`<div class="band-detail"><div class="question">The loop is paused.</div>
    ${l.reason ? html`<div class="cause">${String(l.reason)}</div>` : null}
    ${l.next ? html`<div class="hint">${'After resuming: ' + l.next}</div>` : null}
    <div class="card-row"><button type="button" class="btn btn-primary" disabled=${busy} onClick=${() => sendResume('resume request')}>Resume the loop</button>
      <span class="hint">the loop carries on from where it stopped</span></div></div>`;
}
/** Cost cap hit: raise the limit and continue (new limit above the spent total) or stop here (the loop stays paused). */
export function CostCap() {
  const { spent, cap } = costNumbers(), value = raise.value == null ? defaultRaise() : raise.value;
  const problem = raise.error || raiseProblem(value), showProblem = !!raise.error || (raise.value != null && !!problem);
  return html`<div class="band-detail">
    <div class="question">The loop reached its spending limit and paused. Nothing was lost: the spent total is kept and the work continues from where it stopped.</div>
    <div class="stats-row"><${Stat} k="spent" v=${'US$ ' + usd(spent)} /><${Stat} k="limit" v=${'US$ ' + usd(cap)} /></div>
    <div><label class="field-label" for="raise-limit">New limit (US$)</label>
      <div class="free">
        <input id="raise-limit" type="number" min="0" step="1" value=${value} disabled=${raise.busy} aria-invalid=${showProblem ? 'true' : 'false'}
          onInput=${(e) => { raise.value = e.currentTarget.value; raise.error = null; renderAll(); }} />
        <button type="button" class="btn btn-primary" id="btn-raise-limit" disabled=${raise.busy || !!problem} onClick=${raiseLimit}>Raise the limit and continue</button>
      </div>
      ${showProblem ? html`<div class="nl-box nl-bad" role="alert">${problem}</div>` : html`<div class="hint">${`Suggested: the current limit plus US$ ${RAISE_STEP_USD}.`}</div>`}</div>
    <div class="card-row"><button type="button" class="btn" id="btn-stop-here" onClick=${() => { closeStatusDialog(); toast('The loop stays paused. Use Finish or Back to planning when you are ready.', 'ok'); }}>Stop here</button>
      <span class="hint">the loop stays paused; nothing runs and nothing more is spent</span></div>
  </div>`;
}
/** Link to the PR (GitHub URL shortened), or the raw value as text. */
export function PrLink({ pr }) {
  if (pr == null || pr === '') return html`<span>${DASH}</span>`;
  const s = String(pr);
  if (/^https?:\/\//i.test(s)) return html`<a href=${s} target="_blank" rel="noopener noreferrer">${s.replace(/^https?:\/\/(www\.)?github\.com\//i, '')}</a>`;
  return html`<span>${s}</span>`;
}
export function idleText() {
  const mon = (ctx.state && ctx.state.monitor) || {};
  return isNum(mon.shutdown_in_s)
    ? `The app shuts down by itself in ${dur(mon.shutdown_in_s)} (idle, with no loop running).`
    : 'The app shuts down by itself after a while without use, if no loop is running.';
}
export function Done() {
  const state = ctx.state, m = state.meta || {}, c = state.counts || {}, vals = list(state.validate);
  return html`<div class="band-detail">
    <div class="stats-row"><${Stat} k="turns" v=${num(m.turns)} /><${Stat} k="cost" v=${cost(m.cost_usd)} /><${Stat} k="verified features" v=${`${num(c.verified)} / ${num(c.total)}`} /><${Stat} k="blocked" v=${num(c.blocked)} /></div>
    <div><div class="stat-k">pull request</div><div class="stat-v"><${PrLink} pr=${m.pr} />${m.pr_status ? html`<span class="dim-mono">${'  ' + m.pr_status}</span>` : null}</div></div>
    ${vals.length ? html`<div class="check"><div class="check-title">${`For you to validate (${vals.length}) · see the Plan & files tab on the left`}</div></div>` : null}
    <div class="hint">${idleText() + ' To change the roadmap, use Back to planning in the top bar; to wrap up, use Finish.'}</div>
  </div>`;
}

// ---------- the status dialog ----------
/** The loop waits for a quarantine decision: the pause banner of the LOOP zone and its dialog hold this decision, not the navbar. */
export const quarantineAsk = () => status() === 'awaiting_user' && list(ctx.state && ctx.state.quarantine).length > 0;
const statusDialog = { open: false, autoKey: null };
const BODY_BY_STATUS = {
  awaiting_user: ['The loop has a question for you', Ask], error: ['The loop stopped with an error', ErrorBand], stopped: ['The loop stopped', Stopped],
  paused: ['The loop is paused', Paused], done: ['The loop is done', Done],
};
function currentBody() {
  if (resumingNow()) return ['Resuming the loop', Resuming];
  if (costCapPause()) return ['The loop hit its spending limit', CostCap];
  if (quarantineAsk()) return null;
  return BODY_BY_STATUS[status()] || null;
}
/** True when clicking the indicator has something to show (an attention state, or done). */
export const statusHasDialog = () => !!currentBody();
export function openStatusDialog() { statusDialog.open = true; renderAll(); }
export function closeStatusDialog() { statusDialog.open = false; renderAll(); }
/** Mounted by Panel in loop mode. Opens by itself ONCE per new question (awaiting_user event); closes by itself when nothing needs the user. */
export function StatusDialog() {
  if (status() === 'awaiting_user' && !quarantineAsk() && !resumingNow() && statusDialog.autoKey !== askKey()) { statusDialog.autoKey = askKey(); statusDialog.open = true; }
  const body = statusDialog.open ? currentBody() : null;
  if (!body) { statusDialog.open = false; return null; }
  const [title, Body] = body;
  return html`<${Dialog} title=${title} width=${640} onClose=${closeStatusDialog} actions=${[{ text: 'Close', onClick: (a) => a.close() }]}><${Body} /><//>`;
}
