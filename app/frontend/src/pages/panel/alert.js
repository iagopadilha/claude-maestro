/* Top strip by loop status: waiting for you, error, stopped, done, no loop, resuming. */
import { $ } from '../../util/dom.js';
import { html, useEffect, useRef } from '../../util/preact.js';
import { DASH, isNum, num, cost, dur, txt, list, featLabel, firstLine } from '../../util/format.js';
import { toast } from '../../components/toast.js';
import { ctx, ui, STATUS, NL_STATES, RESUME_WAIT_MS, status, loop, askKey, resumingNow, isPauseAsk, isRecommended } from './context.js';
import { renderAll } from '../project_panel.js';

const detOpen = (s) => (ui.det[s] != null ? ui.det[s] : s === 'awaiting_user');
function toggleDet(s) { ui.det[s] = !detOpen(s); renderAll(); }
function DetBtn({ s }) {
  return html`<button type="button" class="btn btn-sm btn-ghost" id="btn-det" aria-expanded=${String(detOpen(s))} onClick=${() => toggleDet(s)}>${detOpen(s) ? 'details ▴' : 'details ▾'}</button>`;
}
export function openNewLoop() { ui.showNew = true; chrome.open = true; renderAll(); const z = $('newloop-zone'); if (z && !z.hidden) z.scrollIntoView({ block: 'nearest' }); }
function NewLoopBtn({ s }) {
  if (!NL_STATES.includes(s)) return null;
  return html`<button type="button" class="btn btn-sm" id="btn-newloop" aria-expanded=${ui.showNew ? 'true' : 'false'}
    title=${s === 'awaiting_user' ? 'There is a pending question; starting another loop does not answer it.' : 'Starts a new loop in this project (or continues the existing worktree).'}
    onClick=${() => { ui.showNew = !ui.showNew; renderAll(); }}>${ui.showNew ? 'Hide new loop' : 'New loop'}</button>`;
}
function Band({ kind, kick, children, actions }) {
  return html`<section class=${'band band-' + kind}><span class="band-kick">${kick}</span>
    <span class="band-text">${children}</span><div class="band-actions">${actions}</div></section>`;
}
export async function sendAnswer(body, label) {
  if (ui.busy.answer) return;
  ui.busy.answer = true; renderAll();
  try { await ctx.api.post('answer', body); ui.resuming = { key: askKey(), since: Date.now(), label }; ui.freeText = ''; setTimeout(renderAll, RESUME_WAIT_MS + 300); }
  catch (e) { toast('Answer not sent: ' + e.message, 'err'); }
  finally { ui.busy.answer = false; renderAll(); }
}
export async function sendResume(label) {
  if (ui.busy.answer) return;
  ui.busy.answer = true; renderAll();
  try { await ctx.api.post('resume', {}); ui.resuming = { key: askKey(), since: Date.now(), label }; setTimeout(renderAll, RESUME_WAIT_MS + 300); }
  catch (e) { toast('Could not resume: ' + e.message, 'err'); }
  finally { ui.busy.answer = false; renderAll(); }
}
function Resuming() {
  return html`<section class="band band-resume" role="status" aria-live="polite">
    <div class="spinner" aria-hidden="true"></div>
    <span class="band-text">${`Resuming the loop… your ${ui.resuming.label || 'answer'} was received; the screen goes live again when it runs.`}</span>
  </section>`;
}
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
function Ask() {
  const s = 'awaiting_user', l = loop(), opts = list(l.options), pauseAsk = isPauseAsk(), busy = !!ui.busy.answer;
  const band = html`<${Band} kind="ask" kick=${pauseAsk ? 'paused' : 'waiting for you'} actions=${[html`<${DetBtn} key="d" s=${s} />`, html`<${NewLoopBtn} key="n" s=${s} />`]}>${firstLine(l.reason)}<//>`;
  if (!detOpen(s)) return band;
  return html`${band}<div class="band-detail">
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
function ErrorBand() {
  const s = 'error', l = loop(), busy = !!ui.busy.answer;
  const actions = [
    html`<button key="r" type="button" class="btn btn-danger btn-sm" disabled=${busy} title="relaunches the loop from the point where it fell" onClick=${() => sendResume('retry')}>Try again</button>`,
    html`<${DetBtn} key="d" s=${s} />`, html`<${NewLoopBtn} key="n" s=${s} />`];
  return html`<${Band} kind="error" kick="error" actions=${actions}>${firstLine(l.reason)}<//>
    ${detOpen(s) ? html`<div class="band-detail"><div class="cause">${txt(l.reason)}</div>
      ${l.next ? html`<div class="hint">${'Planned next step: ' + l.next}</div>` : null}<${VerifyBlock} /></div>` : null}`;
}
function Stopped() {
  const s = 'stopped', l = loop(), busy = !!ui.busy.answer;
  const actions = [
    html`<button key="r" type="button" class="btn btn-primary btn-sm" disabled=${busy} title="relaunches the loop with the same command" onClick=${() => sendResume('resume request')}>Resume</button>`,
    html`<${DetBtn} key="d" s=${s} />`, html`<${NewLoopBtn} key="n" s=${s} />`];
  return html`<${Band} kind="stopped" kick="stopped" actions=${actions}>The loop process is no longer running, but there was still work to do.<//>
    ${detOpen(s) ? html`<div class="band-detail"><div class="question">The loop was probably closed or crashed. Resume relaunches the same command.</div>
      ${l.reason ? html`<div class="cause">${String(l.reason)}</div>` : null}</div>` : null}`;
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
function Stat({ k, v }) { return html`<div><div class="stat-k">${k}</div><div class="stat-v">${v}</div></div>`; }
function Done() {
  const state = ctx.state;
  const s = 'done', m = state.meta || {}, c = state.counts || {}, vals = list(state.validate);
  const actions = [html`<${DetBtn} key="d" s=${s} />`, html`<${NewLoopBtn} key="n" s=${s} />`];
  return html`<${Band} kind="done" kick="done" actions=${actions}>${`${num(m.turns)} turns · ${cost(m.cost_usd)} · ${num(c.verified)}/${num(c.total)} verified · ${num(c.blocked)} blocked · PR `}<${PrLink} pr=${m.pr} /><//>
    ${detOpen(s) ? html`<div class="band-detail">
      <div class="stats-row"><${Stat} k="turns" v=${num(m.turns)} /><${Stat} k="cost" v=${cost(m.cost_usd)} /><${Stat} k="verified features" v=${`${num(c.verified)} / ${num(c.total)}`} /><${Stat} k="blocked" v=${num(c.blocked)} /></div>
      <div><div class="stat-k">pull request</div><div class="stat-v"><${PrLink} pr=${m.pr} />${m.pr_status ? html`<span class="dim-mono">${'  ' + m.pr_status}</span>` : null}</div></div>
      ${vals.length ? html`<div class="check"><div class="check-title">${`For you to validate (${vals.length}) · see the list on the left`}</div></div>` : null}
      <div class="hint">${idleText() + ' To shut it down now, use the Shut down app button at the top.'}</div>
    </div>` : null}`;
}
function Empty() {
  return html`<${Band} kind="empty" kick="no loop" actions=${[]}>${`No loop found in ${txt(ctx.state && ctx.state.work)}. Follow the trail above; with the roadmap approved, set it up below and click Start loop.`}<//>`;
}
function InfoStrip({ s }) {
  const l = loop();
  if ((s !== 'running' && s !== 'continue') || (!l.next && !(s === 'running' && l.feature))) return null;
  const parts = [];
  if (s === 'running' && featLabel(l.feature)) parts.push(['f', 'in progress: ', featLabel(l.feature)]);
  if (l.next) parts.push(['n', 'next: ', String(l.next)]);
  return html`<div class="strip">${parts.map(([k, lab, val], i) => html`
    ${i ? html`<span key=${k + '-sep'} class="mono">·</span>` : null}<span key=${k}>${lab}<span class="mono">${val}</span></span>`)}</div>`;
}

/** Content of #alert-zone, by loop status. */
export function Alert() {
  const s = status();
  if (resumingNow()) return html`<${Resuming} />`;
  if (s === 'awaiting_user') return html`<${Ask} />`;
  if (s === 'error') return html`<${ErrorBand} />`;
  if (s === 'stopped') return html`<${Stopped} />`;
  if (s === 'done') return html`<${Done} />`;
  if (s === 'no_loop') return html`<${Empty} />`;
  return html`<${InfoStrip} s=${s} />`;
}

// ---------- chrome summary bar: the alert, new loop and workspace zones behind one ~34px bar ----------
/** Open/closed of the bar, kept in memory only. open === null follows the loop: it opens by itself on a question or an error. */
const chrome = { open: null, key: '' };
const NEEDS_YOU = ['awaiting_user', 'error'];
export function chromeOpen() {
  const key = askKey();
  if (chrome.key !== key) { chrome.key = key; chrome.open = null; } // a new ask/error drops the manual choice
  return chrome.open === null ? NEEDS_YOU.includes(status()) : chrome.open;
}
export function ChromeBar({ nlShown }) {
  const open = chromeOpen(), s = status(), l = loop(), wsRef = useRef(null);
  // the workspace line owns its text (#ws-sum); the bar mirrors it without reaching into that module
  useEffect(() => {
    const copy = () => { const src = document.getElementById('ws-sum'), el = wsRef.current; if (src && el && el.textContent !== src.textContent) el.textContent = src.textContent; };
    copy(); const t = setInterval(copy, 1000); return () => clearInterval(t);
  });
  const label = (STATUS[s] || STATUS.no_loop).label;
  const reason = NEEDS_YOU.includes(s) && l.reason ? firstLine(l.reason) : '';
  const toggle = () => { chrome.open = !open; renderAll(); };
  return html`<button type="button" id="chrome-toggle" class=${'chrome-bar chrome-' + s + (open ? ' is-open' : '')} aria-expanded=${String(open)} aria-controls="chrome-zones" onClick=${toggle}>
    <span class="chrome-caret" aria-hidden="true">${open ? '▾' : '▸'}</span>
    <span class="chrome-status">${label}</span>
    ${reason ? html`<span class="chrome-reason">${reason}</span>` : null}
    ${nlShown ? html`<span class="chrome-part">Start a loop</span>` : null}
    <span class="chrome-part">Workspace <span class="mono" ref=${wsRef}></span></span>
    <span class="chrome-hint">${open ? 'collapse' : 'expand'}</span>
  </button>`;
}
