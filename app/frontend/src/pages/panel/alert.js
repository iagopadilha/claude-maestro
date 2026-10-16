/* Top strip by loop status: waiting for you, error, stopped, done, no loop, resuming. */
import { $, h, fill, keepFocus } from '../../util/dom.js';
import { DASH, isNum, num, cost, dur, txt, list, featLabel, firstLine } from '../../util/format.js';
import { toast } from '../../components/toast.js';
import { ctx, ui, sigs, region, NL_STATES, RESUME_WAIT_MS, status, loop, askKey, resumingNow, isPauseAsk, isRecommended } from './context.js';
import { nlVisibility } from './new_loop.js';

const detOpen = (s) => (ui.det[s] != null ? ui.det[s] : s === 'awaiting_user');
function toggleDet(s) { ui.det[s] = !detOpen(s); sigs.alert = null; renderAlert(); }
function detBtn(s) {
  return h('button', { type: 'button', class: 'btn btn-sm btn-ghost', id: 'btn-det', 'aria-expanded': String(detOpen(s)), onclick: () => toggleDet(s) }, detOpen(s) ? 'details ▴' : 'details ▾');
}
export function openNewLoop() { ui.showNew = true; sigs.alert = null; renderAlert(); nlVisibility(); const z = $('newloop-zone'); if (z && !z.hidden) z.scrollIntoView({ block: 'nearest' }); }
function newLoopBtn(s) {
  if (!NL_STATES.includes(s)) return null;
  return h('button', { type: 'button', class: 'btn btn-sm', id: 'btn-newloop', 'aria-expanded': ui.showNew ? 'true' : 'false',
    title: s === 'awaiting_user' ? 'There is a pending question; starting another loop does not answer it.' : 'Starts a new loop in this project (or continues the existing worktree).',
    onclick: () => { ui.showNew = !ui.showNew; sigs.alert = null; renderAlert(); nlVisibility(); } }, ui.showNew ? 'Hide new loop' : 'New loop');
}
function band(kind, kick, textKids, actions) {
  return h('section', { class: 'band band-' + kind }, h('span', { class: 'band-kick', text: kick }),
    h('span', { class: 'band-text' }, textKids), h('div', { class: 'band-actions' }, actions));
}
export async function sendAnswer(body, label) {
  if (ui.busy.answer) return;
  ui.busy.answer = true; sigs.alert = null; renderAlert();
  try { await ctx.api.post('answer', body); ui.resuming = { key: askKey(), since: Date.now(), label }; ui.freeText = ''; setTimeout(renderAlert, RESUME_WAIT_MS + 300); }
  catch (e) { toast('Answer not sent: ' + e.message, 'err'); }
  finally { ui.busy.answer = false; sigs.alert = null; renderAlert(); }
}
export async function sendResume(label) {
  if (ui.busy.answer) return;
  ui.busy.answer = true; sigs.alert = null; renderAlert();
  try { await ctx.api.post('resume', {}); ui.resuming = { key: askKey(), since: Date.now(), label }; setTimeout(renderAlert, RESUME_WAIT_MS + 300); }
  catch (e) { toast('Could not resume: ' + e.message, 'err'); }
  finally { ui.busy.answer = false; sigs.alert = null; renderAlert(); }
}
function resumingNodes() {
  return [h('section', { class: 'band band-resume', role: 'status', 'aria-live': 'polite' },
    h('div', { class: 'spinner', 'aria-hidden': 'true' }),
    h('span', { class: 'band-text', text: `Resuming the loop… your ${ui.resuming.label || 'answer'} was received; the screen goes live again when it runs.` }))];
}
function verifyBlock() {
  const v = list(loop().to_check);
  if (!v.length) return null;
  return h('div', { class: 'check' }, h('div', { class: 'check-title', text: 'What to check before answering' }), h('ul', {}, v.map((x) => h('li', { text: String(x) }))));
}
function askNodes() {
  const s = 'awaiting_user', l = loop(), opts = list(l.options), pauseAsk = isPauseAsk(), busy = !!ui.busy.answer;
  const out = [band('ask', pauseAsk ? 'paused' : 'waiting for you', firstLine(l.reason), [detBtn(s), newLoopBtn(s)])];
  if (!detOpen(s)) return out;
  const kids = [h('div', { class: 'question', text: txt(l.reason) })];
  if (l.next) kids.push(h('div', { class: 'hint', text: 'After the answer: ' + l.next }));
  if (opts.length) {
    kids.push(h('div', { class: 'opts', role: 'group', 'aria-label': 'Answer options' }, opts.map((o, i) =>
      h('button', { type: 'button', class: 'opt' + (isRecommended(o) ? ' opt-rec' : ''), disabled: busy, onclick: () => sendAnswer({ option: i + 1 }, `option ${i + 1}`) },
        h('span', { class: 'opt-n', text: String(i + 1) }),
        h('span', { class: 'opt-label', text: String(o) }),
        isRecommended(o) ? h('span', { class: 'tag', text: 'recommended' }) : null))));
  }
  const ta = h('textarea', { id: 'free-text', rows: 2, placeholder: opts.length ? 'None fits? Write your own answer here.' : 'Write your answer here.', disabled: busy, value: ui.freeText });
  ta.addEventListener('input', () => { ui.freeText = ta.value; });
  kids.push(h('div', {}, h('label', { class: 'field-label', for: 'free-text', text: opts.length ? 'Or answer in your own words' : 'Your answer' }),
    h('div', { class: 'free' }, ta,
      h('button', { type: 'button', class: 'btn btn-primary', disabled: busy, onclick: () => { const t = ui.freeText.trim(); if (!t) { toast('Write the answer before sending.', 'err'); return; } sendAnswer({ text: t }, 'answer'); } }, 'Send answer'))));
  const vb = verifyBlock(); if (vb) kids.push(vb);
  if (!opts.length || pauseAsk) {
    kids.push(h('div', { class: 'card-row' },
      h('button', { type: 'button', class: 'btn' + (pauseAsk ? ' btn-primary' : ''), disabled: busy, onclick: () => sendResume('resume request') }, pauseAsk ? 'Resume the loop' : 'Resume without answering'),
      h('span', { class: 'hint', text: 'the loop carries on from where it stopped' })));
  }
  out.push(h('div', { class: 'band-detail' }, kids));
  return out;
}
function errorNodes() {
  const s = 'error', l = loop(), busy = !!ui.busy.answer;
  const out = [band('error', 'error', firstLine(l.reason), [
    h('button', { type: 'button', class: 'btn btn-danger btn-sm', disabled: busy, title: 'relaunches the loop from the point where it fell', onclick: () => sendResume('retry') }, 'Try again'), detBtn(s), newLoopBtn(s)])];
  if (detOpen(s)) out.push(h('div', { class: 'band-detail' }, h('div', { class: 'cause', text: txt(l.reason) }),
    l.next ? h('div', { class: 'hint', text: 'Planned next step: ' + l.next }) : null, verifyBlock()));
  return out;
}
function stoppedNodes() {
  const s = 'stopped', l = loop(), busy = !!ui.busy.answer;
  const out = [band('stopped', 'stopped', 'The loop process is no longer running, but there was still work to do.', [
    h('button', { type: 'button', class: 'btn btn-primary btn-sm', disabled: busy, title: 'relaunches the loop with the same command', onclick: () => sendResume('resume request') }, 'Resume'), detBtn(s), newLoopBtn(s)])];
  if (detOpen(s)) out.push(h('div', { class: 'band-detail' }, h('div', { class: 'question', text: 'The loop was probably closed or crashed. Resume relaunches the same command.' }),
    l.reason ? h('div', { class: 'cause', text: String(l.reason) }) : null));
  return out;
}
export function prLink(pr) {
  if (pr == null || pr === '') return h('span', { text: DASH });
  const s = String(pr);
  if (/^https?:\/\//i.test(s)) return h('a', { href: s, target: '_blank', rel: 'noopener noreferrer', text: s.replace(/^https?:\/\/(www\.)?github\.com\//i, '') });
  return h('span', { text: s });
}
export function idleText() {
  const mon = (ctx.state && ctx.state.monitor) || {};
  return isNum(mon.shutdown_in_s)
    ? `The app shuts down by itself in ${dur(mon.shutdown_in_s)} (idle, with no loop running).`
    : 'The app shuts down by itself after a while without use, if no loop is running.';
}
function doneNodes() {
  const state = ctx.state;
  const s = 'done', m = state.meta || {}, c = state.counts || {}, vals = list(state.validate);
  const stat = (k, v) => h('div', {}, h('div', { class: 'stat-k', text: k }), h('div', { class: 'stat-v', text: v }));
  const out = [band('done', 'done', [`${num(m.turns)} turns · ${cost(m.cost_usd)} · ${num(c.verified)}/${num(c.total)} verified · ${num(c.blocked)} blocked · PR `, prLink(m.pr)], [detBtn(s), newLoopBtn(s)])];
  if (detOpen(s)) out.push(h('div', { class: 'band-detail' },
    h('div', { class: 'stats-row' }, stat('turns', num(m.turns)), stat('cost', cost(m.cost_usd)), stat('verified features', `${num(c.verified)} / ${num(c.total)}`), stat('blocked', num(c.blocked))),
    h('div', {}, h('div', { class: 'stat-k', text: 'pull request' }), h('div', { class: 'stat-v' }, prLink(m.pr), m.pr_status ? h('span', { class: 'dim-mono', text: '  ' + m.pr_status }) : null)),
    vals.length ? h('div', { class: 'check' }, h('div', { class: 'check-title', text: `For you to validate (${vals.length}) · see the list on the left` })) : null,
    h('div', { class: 'hint', text: idleText() + ' To shut it down now, use the Shut down app button at the top.' })));
  return out;
}
function emptyNodes() {
  return [band('empty', 'no loop', `No loop found in ${txt(ctx.state && ctx.state.work)}. Follow the trail above; with the roadmap approved, set it up below and click Start loop.`, [])];
}
function infoStrip(s) {
  const l = loop();
  if ((s !== 'running' && s !== 'continue') || (!l.next && !(s === 'running' && l.feature))) return null;
  const parts = [];
  if (s === 'running' && featLabel(l.feature)) parts.push(h('span', {}, 'in progress: ', h('span', { class: 'mono', text: featLabel(l.feature) })));
  if (l.next) parts.push(h('span', {}, 'next: ', h('span', { class: 'mono', text: String(l.next) })));
  return h('div', { class: 'strip' }, parts.map((p, i) => (i ? [h('span', { class: 'mono', text: '·' }), p] : p)).flat());
}
export function renderAlert() {
  const state = ctx.state;
  if (!state) return;
  const s = status(), l = loop(), resuming = resumingNow();
  const data = { s, resuming, sn: ui.showNew, det: ui.det, busy: !!ui.busy.answer, m: l.reason, o: l.options, v: l.to_check, p: l.next, f: l.feature, vo: l.turn, at: l.at, work: state.work,
    done: s === 'done' ? [state.meta, state.counts, state.validate, state.monitor && state.monitor.shutdown_in_s] : null };
  region('alert', data, () => {
    let nodes;
    if (resuming) nodes = resumingNodes();
    else if (s === 'awaiting_user') nodes = askNodes();
    else if (s === 'error') nodes = errorNodes();
    else if (s === 'stopped') nodes = stoppedNodes();
    else if (s === 'done') nodes = doneNodes();
    else if (s === 'no_loop') nodes = emptyNodes();
    else nodes = [infoStrip(s)];
    keepFocus(() => fill($('alert-zone'), nodes));
  });
}
