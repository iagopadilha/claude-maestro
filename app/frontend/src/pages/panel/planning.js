/* Planning mode (state.planning_required): a project with no approved roadmap that never ran a loop opens here
   instead of the loop panel. Left: prerequisites + gate checklist (GET planning) and "Approve roadmap"; center: the
   live /orchestrator plan chat that writes PROJECT.md, ROADMAP.md and FLOWS.md (POST planning/send, reply followed
   through GET planning/job/<id>); right: the three documents, read-only, refreshed after every reply.
   The back end enforces every gate; this screen only shows them. */
import { html, useEffect, useLayoutEffect, useRef } from '../../util/preact.js';
import { isNum, cost, list, fmtHour } from '../../util/format.js';
import { toast } from '../../components/toast.js';
import { confirmDialog } from '../../components/dialog.js';
import { Markdown } from '../../components/markdown.js';
import { ctx, approval } from './context.js';
import { renderAll, schedulePoll } from '../project_panel.js';
import { Think } from './chat.js';
import { APPROVAL_NOTICE } from './new_loop.js';
import { reloadProgress, initializeStructure, chooseConformance, mapCodebase, mapRunning } from './progress_trail.js';

const JOB_POLL_MS = 1500, JOB_GIVEUP_MS = 660 * 1000, REFRESH_MS = 6000, MAX_TEXT = 8000;
const PREREQUISITES = ['git', 'structure', 'conformance', 'map'];
const DOCS = [['project', 'PROJECT', 'PROJECT.md'], ['roadmap', 'ROADMAP', 'ROADMAP.md'], ['flows', 'FLOWS', 'FLOWS.md']];
const SEND_ERRORS = {
  planning_busy: 'A reply is still being written: wait for it before sending another message.',
  planning_prerequisites: 'Finish the prerequisites on the left before chatting.',
  loop_running: 'A loop is running in this project: planning is closed while it runs.',
};
// actions of the failing prerequisites: the same ones the trail uses (dialogs and jobs live in progress_trail.js)
const PREREQ_ACTION = {
  structure: { text: 'Initialize structure', run: () => initializeStructure() },
  conformance: { text: 'Choose conformance', run: () => chooseConformance() },
  map: { text: 'Map codebase', run: () => mapCodebase(), busyText: 'Mapping…', busy: () => mapRunning() },
};
const GATE_HELP = { git: 'Run "git init" in the project folder, then come back.' };

/** Screen state (survives renders; the textarea itself is uncontrolled so typing and the caret survive the polling). */
const pl = { data: null, loading: false, again: false, error: null, loadedAt: 0, job: null, sending: false, pending: null, err: null, lastText: null, tab: 'roadmap', approving: false, resetting: false, draft: '', busyJob: null };

/** True when /state says the project is in planning mode. */
export const planningRequired = () => !!(ctx.state && ctx.state.planning_required);

function normalize(d) {
  const t = (d && d.thread) || {};
  return {
    required: !!(d && d.required),
    thread: { messages: list(t.messages), busy: !!t.busy, job_id: t.job_id || null, session: !!t.session },
    docs: (d && d.docs) || {},
    gates: list(d && d.gates),
    can_approve: !!(d && d.can_approve),
    approved: !!(d && d.approved),
  };
}

// ---------- reading ----------
/** GET planning. Overlapping calls collapse into one more read at the end. */
export async function loadPlanning() {
  if (pl.loading) { pl.again = true; return; }
  pl.loading = true;
  try {
    const d = normalize(await ctx.api.get('planning', 15000));
    pl.data = d; pl.error = null;
    if (d.thread.busy && d.thread.job_id && !pl.job) followJob(d.thread.job_id, startOfLastUser(d));
    // busy without a job id (or between reads): a stable start so the "thinking… N s" timer keeps counting
    pl.busyJob = d.thread.busy ? pl.busyJob || { start: startOfLastUser(d) || Date.now() } : null;
    if (!d.required || d.approved) schedulePoll(0); // the planning is over: /state flips the panel back
  } catch (e) {
    pl.error = e.status === 404 && !/project/i.test(e.message) ? 'The server does not offer planning mode yet.' : e.message;
  } finally {
    pl.loading = false; pl.loadedAt = Date.now();
    renderAll();
    if (pl.again) { pl.again = false; loadPlanning(); }
  }
}
function startOfLastUser(d) {
  const ms = d.thread.messages;
  for (let i = ms.length - 1; i >= 0; i -= 1) if (ms[i].role === 'user') { const t = Date.parse(ms[i].at); return Number.isFinite(t) ? t : null; }
  return null;
}

// ---------- the reply job ----------
function followJob(id, since) {
  if (pl.job && pl.job.id === id) return;
  const job = { id, start: since || Date.now(), timer: null };
  pl.job = job;
  const done = (err) => {
    if (pl.job !== job) return;
    pl.job = null; pl.err = err || null;
    loadPlanning().then(() => { if (pl.job !== job) pl.pending = null; renderAll(); });
    reloadProgress();
  };
  const step = async () => {
    if (pl.job !== job) return;
    if (Date.now() - job.start > JOB_GIVEUP_MS) { done('No reply after 11 minutes: the job was given up on this screen. Reload the page to check again.'); return; }
    try {
      const r = await ctx.api.get('planning/job/' + encodeURIComponent(id), 10000);
      if (pl.job !== job) return;
      if (r.state === 'ok') { done(null); return; }
      if (r.state === 'error') { done(r.error || 'The AI failed while writing the reply.'); return; }
    } catch (e) {
      if (pl.job !== job) return;
      if (e.status === 404) { done('The reply job was not found on the server.'); return; }
    }
    job.timer = setTimeout(step, JOB_POLL_MS);
    renderAll();
  };
  job.timer = setTimeout(step, JOB_POLL_MS);
  renderAll();
}

// ---------- actions ----------
const busyNow = () => !!(pl.job || (pl.data && pl.data.thread.busy));
const failingPrereqs = () => list(pl.data && pl.data.gates).filter((g) => PREREQUISITES.includes(g.id) && !g.ok && !g.optional);
/** Why the chat input is locked (null = it is open). */
function inputLock() {
  if (!pl.data) return pl.error ? 'The plan could not be read.' : 'Reading the plan…';
  const f = failingPrereqs();
  return f.length ? `Finish the prerequisites first: ${f.map((g) => g.label || g.id).join(', ')}.` : null;
}

async function send(text) {
  const lock = inputLock();
  if (lock) { toast(lock, 'err'); return false; }
  if (busyNow() || pl.sending) { toast(SEND_ERRORS.planning_busy, 'err'); return false; }
  if (text.length > MAX_TEXT) { toast(`The message has ${text.length} characters; the limit is ${MAX_TEXT}.`, 'err'); return false; }
  pl.sending = true; pl.err = null; pl.lastText = text; pl.pending = { role: 'user', text, at: new Date().toISOString() };
  renderAll();
  let ok = false;
  try {
    const r = await ctx.api.post('planning/send', { text }, 30000);
    ok = true;
    if (r.job_id) followJob(r.job_id, Date.now()); else loadPlanning();
  } catch (e) {
    const code = e.body && e.body.code;
    pl.pending = null;
    pl.err = SEND_ERRORS[code] || (e.status === 400 ? 'Not sent: ' + e.message : 'Not sent: ' + e.message);
    toast(pl.err, 'err');
    if (code) loadPlanning();
  } finally { pl.sending = false; renderAll(); }
  return ok;
}
async function submit(ev) {
  ev.preventDefault();
  const ta = document.getElementById('plan-text');
  const text = ta ? ta.value.trim() : '';
  if (!text) { if (ta) ta.focus(); return; }
  if (await send(text)) { if (ta) ta.value = ''; pl.draft = ''; renderAll(); }
}
function keyDown(e) {
  if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); const f = document.getElementById('plan-form'); if (f) f.requestSubmit(); }
}
async function resetConversation() {
  if (busyNow() || pl.resetting) return;
  const ok = await confirmDialog({ title: 'Start a new conversation?', text: 'Clears this chat; the next message starts a new AI session. PROJECT.md, ROADMAP.md and FLOWS.md stay as they are.', yesText: 'New conversation', danger: true });
  if (!ok) return;
  pl.resetting = true; renderAll();
  try { await ctx.api.post('planning/reset', { confirm: true }); pl.err = null; pl.pending = null; pl.lastText = null; toast('New conversation: the documents were kept.', 'ok'); }
  catch (e) { toast('Not cleared: ' + e.message, 'err'); }
  finally { pl.resetting = false; loadPlanning(); }
}
/** Approve roadmap (end of planning): confirm, POST roadmap/approve; the back end checks every gate again. */
export async function approvePlan() {
  if (pl.approving) return;
  if (pl.data && !pl.data.can_approve) { toast('Not yet: every required item of the checklist must pass.', 'err'); return; }
  const ok = await confirmDialog({ title: 'Approve the roadmap?', text: 'Ends the planning and unlocks the loop for the features and stages in ROADMAP.md now. ' + APPROVAL_NOTICE, yesText: 'Approve roadmap' });
  if (!ok) return;
  pl.approving = true; renderAll();
  try {
    await ctx.api.post('roadmap/approve', { confirm: true });
    toast('Roadmap approved: the planning is done. You can start a loop now.', 'ok');
    ctx.state = { ...ctx.state, planning_required: false, roadmap_approval: { ...approval(), approved: true, reason: null } };
    schedulePoll(0); reloadProgress();
  } catch (e) {
    if (e.body && Array.isArray(e.body.gates) && pl.data) pl.data = { ...pl.data, gates: e.body.gates, can_approve: false };
    toast('Roadmap not approved: ' + e.message, 'err');
    loadPlanning();
  } finally { pl.approving = false; renderAll(); }
}

// ---------- drawing ----------
const ICON = { ok: '✓', bad: '✗', opt: '◌' };
function Gate({ g }) {
  const kind = g.ok ? 'ok' : g.optional ? 'opt' : 'bad';
  const act = !g.ok && !g.optional ? PREREQ_ACTION[g.id] : null, running = !!(act && act.busy && act.busy());
  const items = list(g.items).filter((x) => x != null && String(x).trim());
  const help = !g.ok && !g.optional && !g.detail ? GATE_HELP[g.id] : null;
  return html`<li class=${'plan-gate ' + kind} id=${'plan-gate-' + g.id}>
    <span class="plan-gate-ic" aria-hidden="true">${ICON[kind]}</span>
    <div class="plan-gate-b">
      <div class="plan-gate-t">${g.label || g.id}<span class="sr-only">${g.ok ? ' (done)' : g.optional ? ' (optional)' : ' (pending)'}</span>
        ${g.optional && !g.ok ? html`<span class="plan-opt">optional</span>` : null}</div>
      ${g.detail || help ? html`<div class="plan-gate-d">${g.detail || help}</div>` : null}
      ${items.length ? html`<ul class="plan-gate-items">${items.map((x, i) => html`<li key=${i}>${String(x)}</li>`)}</ul>` : null}
      ${act ? html`<button type="button" class=${'btn btn-sm ' + (running ? 'btn-pending' : 'btn-primary')} disabled=${running} onClick=${act.run}>${running ? act.busyText : act.text}</button>` : null}
    </div>
  </li>`;
}
function Gates({ d }) {
  const pre = d.gates.filter((g) => PREREQUISITES.includes(g.id)), plan = d.gates.filter((g) => !PREREQUISITES.includes(g.id));
  const missing = d.gates.filter((g) => !g.ok && !g.optional).length;
  return html`
    <div class="small-label">Prerequisites</div>
    <ul class="plan-gates" aria-label="Prerequisites">${pre.map((g) => html`<${Gate} key=${g.id} g=${g} />`)}</ul>
    <div class="small-label">Before approving</div>
    <ul class="plan-gates" aria-label="Gates before approving">${plan.map((g) => html`<${Gate} key=${g.id} g=${g} />`)}</ul>
    <div class="plan-approve">
      ${d.approved ? html`<div class="nl-box nl-ok"><span class="chip">ROADMAP APPROVED</span></div>` : null}
      <button type="button" class="btn btn-primary" id="plan-approve" disabled=${!d.can_approve || pl.approving || d.approved}
        onClick=${approvePlan}>${pl.approving ? 'Approving…' : 'Approve roadmap'}</button>
      <div class="hint" id="plan-approve-hint">${d.can_approve ? 'Everything required passes: approving ends the planning and unlocks the loop.'
        : `${missing} required item${missing === 1 ? '' : 's'} still open. The approval unlocks when every required item passes.`}</div>
    </div>`;
}
function PlanChecklist() {
  const d = pl.data;
  return html`<aside class="plan-col plan-left" aria-label="Planning checklist">
    <div class="plan-h"><span class="plan-title">Checklist</span></div>
    <div class="plan-scroll">
      ${d ? html`<${Gates} d=${d} />`
        : html`<div class=${pl.error ? 'nl-box nl-bad' : 'hint'} role=${pl.error ? 'alert' : null}>${pl.error ? 'Planning unavailable: ' + pl.error : 'reading the plan…'}
          ${pl.error ? html`<div><button type="button" class="btn btn-sm" onClick=${() => loadPlanning()}>Try again</button></div>` : null}</div>`}
    </div>
  </aside>`;
}

function Msg({ m }) {
  const ai = m.role === 'ai';
  const meta = [fmtHour(m.at), ai && isNum(m.cost_usd) ? cost(m.cost_usd) : ''].filter(Boolean).join(' · ');
  return html`<div class=${'cmsg ' + (ai ? 'ai' : 'user') + (m.pending ? ' plan-pending' : '')}>
    <div class="cmsg-h"><span>${ai ? 'AI · writes the plan' : 'you'}</span><span class="t">${meta}</span></div>
    ${ai ? html`<div class="cmsg-b md"><${Markdown} source=${m.text == null ? '' : String(m.text)} /></div>` : html`<div class="cmsg-b">${String(m.text == null ? '' : m.text)}</div>`}
  </div>`;
}
function messagesNow() {
  const ms = list(pl.data && pl.data.thread.messages);
  const p = pl.pending, lastUser = [...ms].reverse().find((m) => m.role === 'user');
  if (!p || (lastUser && lastUser.text === p.text)) return ms; // the server already has it
  return [...ms, { ...p, pending: true }];
}
function ChatCol() {
  const listRef = useRef(null), taRef = useRef(null), stick = useRef(true), seen = useRef(0);
  const ms = messagesNow(), busy = busyNow(), lock = inputLock();
  useLayoutEffect(() => { if (taRef.current && pl.draft) taRef.current.value = pl.draft; }, []);
  // new messages follow only if the reader was at the bottom (or just sent)
  useLayoutEffect(() => {
    const el = listRef.current, n = ms.length, prev = seen.current;
    seen.current = n;
    if (el && n > prev && (stick.current || !prev || ms[n - 1].role === 'user')) el.scrollTop = el.scrollHeight;
  });
  const onScroll = (e) => { const el = e.currentTarget; stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 60; };
  const job = pl.job || pl.busyJob;
  return html`<section class="plan-col plan-chat" aria-label="Planning chat">
    <div class="plan-h"><span class="plan-title">Plan with the AI</span><span class="spacer"></span>
      <button type="button" class="btn btn-sm btn-ghost" id="plan-reset" disabled=${busy || pl.sending || pl.resetting || !ms.length}
        title="clears the chat; the documents stay" onClick=${resetConversation}>New conversation</button></div>
    <div class="ch-empty plan-empty" hidden=${!!ms.length}>${ms.length ? '' : 'Describe the project: what it is for, who uses it, the main screens or APIs. The AI writes PROJECT.md, ROADMAP.md and FLOWS.md and asks what is still missing. Answer its questions here until the checklist passes.'}</div>
    <div class="ch-list plan-list" role="log" aria-live="polite" ref=${listRef} onScroll=${onScroll}>${ms.map((m, i) => html`<${Msg} key=${i} m=${m} />`)}</div>
    <${Think} job=${busy ? job : null} />
    <div class="ch-err" role="alert" hidden=${!pl.err}>${pl.err ? [html`<span key="t">${pl.err}</span>`,
      pl.lastText && !busy ? html`<button key="r" type="button" class="btn btn-sm" onClick=${() => send(pl.lastText)}>Send again</button>` : null,
      html`<button key="x" type="button" class="btn btn-sm btn-ghost" onClick=${() => { pl.err = null; renderAll(); }}>Dismiss</button>`] : null}</div>
    <form class="plan-form" id="plan-form" aria-label="Message to the planning AI" onSubmit=${submit}>
      <div class="plan-lock" role="status" hidden=${!lock}>${lock || ''}</div>
      <div class="cp-box plan-box">
        <textarea id="plan-text" ref=${taRef} rows="3" aria-label="Message" disabled=${!!lock} maxlength=${MAX_TEXT}
          placeholder=${lock ? '' : 'Answer the questions or describe what the project needs…'}
          onInput=${(e) => { pl.draft = e.currentTarget.value; }} onKeyDown=${keyDown}></textarea>
        <button type="submit" class="btn btn-primary" id="plan-send" disabled=${!!lock || busy || pl.sending}>${busy ? 'Writing…' : 'Send'}</button>
      </div>
      <div class="cp-hint mono">Enter sends · Shift+Enter new line · the AI only writes PROJECT.md, ROADMAP.md and FLOWS.md and never approves.</div>
    </form>
  </section>`;
}

function DocsCol() {
  const docs = (pl.data && pl.data.docs) || {};
  const cur = DOCS.find(([k]) => k === pl.tab) || DOCS[1];
  const pick = (k) => { pl.tab = k; renderAll(); };
  const onKey = (e) => {
    const i = DOCS.findIndex(([k]) => k === pl.tab);
    const j = e.key === 'ArrowRight' ? (i + 1) % DOCS.length : e.key === 'ArrowLeft' ? (i - 1 + DOCS.length) % DOCS.length : e.key === 'Home' ? 0 : e.key === 'End' ? DOCS.length - 1 : null;
    if (j == null) return;
    e.preventDefault(); pick(DOCS[j][0]);
    const b = document.getElementById('plan-tab-' + DOCS[j][0]); if (b) b.focus();
  };
  const src = docs[cur[0]];
  return html`<section class="plan-col plan-docs" aria-label="Planning documents">
    <div class="plan-h"><div class="seg" role="tablist" aria-label="Document" onKeyDown=${onKey}>${DOCS.map(([k, label, file]) => html`<button key=${k} type="button" role="tab"
      id=${'plan-tab-' + k} class="seg-btn" aria-selected=${String(cur[0] === k)} aria-controls="plan-doc" tabIndex=${cur[0] === k ? 0 : -1} title=${'.specs/project/' + file}
      onClick=${() => pick(k)}>${label}</button>`)}</div>
      <span class="spacer"></span><span class="hint">read-only · updated after each reply</span></div>
    <div class="plan-scroll" id="plan-doc" role="tabpanel" aria-labelledby=${'plan-tab-' + cur[0]}>
      ${typeof src === 'string' && src.trim() ? html`<div class="md-doc"><${Markdown} source=${src} /></div>`
        : html`<div class="empty-note">${pl.data ? `${cur[2]} is not written yet. It fills in as you chat.` : 'reading…'}</div>`}
    </div>
  </section>`;
}

/** The whole planning screen (replaces the loop panel while state.planning_required is true). */
export function PlanningScreen() {
  // reads again when the trail progress changes (structure, conformance, map just done) and every few seconds
  useEffect(() => { loadPlanning(); }, [ctx.progress]);
  useEffect(() => {
    const t = setInterval(() => {
      if (!pl.job && document.visibilityState === 'visible' && Date.now() - pl.loadedAt > REFRESH_MS - 500) loadPlanning();
    }, REFRESH_MS);
    return () => clearInterval(t);
  }, []);
  return html`<div class="plan">
    <div class="plan-intro"><span class="plan-kick">planning mode</span>
      <span>${'Plan the project before the first loop: chat with the AI until every item of the checklist passes, then approve the roadmap. The loop views open after the approval.'}</span></div>
    <div class="plan-grid">
      <${PlanChecklist} />
      <${ChatCol} />
      <${DocsCol} />
    </div>
  </div>`;
}
