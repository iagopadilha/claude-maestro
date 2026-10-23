/* "Roadmap" tab of the loop view: the stages and features defined earlier (status of each) and, for the selected feature
   (or the whole plan), a chat that CHANGES a definition already written (POST spec-change/<scope>/send, reply followed
   through GET spec-change-job/<id>). The AI only edits the files of that scope; the loop is not touched: after a change the
   panel offers to send the existing "instruction" to the loop so it redoes the affected work.
   The back end enforces every rule; this view only shows them. Same chat look as the planning screen (plan-*, cmsg classes). */
import { html, useEffect, useLayoutEffect, useRef } from '../../util/preact.js';
import { isNum, cost, list, txt, fmtHour } from '../../util/format.js';
import { toast } from '../../components/toast.js';
import { confirmDialog } from '../../components/dialog.js';
import { Markdown } from '../../components/markdown.js';
import { ctx, pal, status, focusFeatureId, features } from './context.js';
import { renderAll } from '../project_panel.js';
import { Think } from './chat.js';
import { groupStages } from './roadmap_sidebar.js';

const PROJECT_SCOPE = '_project';
const JOB_POLL_MS = 1500, JOB_GIVEUP_MS = 660 * 1000, MAX_TEXT = 8000;
const SEND_ERRORS = {
  spec_change_busy: 'A reply is still being written: wait for it before sending another message.',
  loop_running: 'The loop is running: pause it first (the orchestrator writes these specs), change the definition, then resume.',
};
const SAFE_TEXT = 'The AI only edits the files listed above. A hook and the app undo any other change: your code is never touched, and the loop only learns about the change when you tell it.';

/** Screen state per scope (survives renders and tab switches; the textarea is uncontrolled so typing survives the polling). */
const rc = { scope: null, by: {} };
const st = (scope) => rc.by[scope] || (rc.by[scope] = { data: null, error: null, loading: false, job: null, sending: false, pending: null, err: null, lastText: null, draft: '', redone: '' });

function currentScope() {
  if (rc.scope) return rc.scope;
  return focusFeatureId() || (features()[0] && features()[0].id) || PROJECT_SCOPE;
}
function pickScope(scope) { rc.scope = scope; renderAll(); }

async function load(scope) {
  const s = st(scope);
  if (s.loading) return;
  s.loading = true;
  try {
    s.data = await ctx.api.get('spec-change/' + encodeURIComponent(scope), 10000); s.error = null;
    if (s.data.thread.busy && s.data.thread.job_id) followJob(scope, s.data.thread.job_id, Date.now());
  } catch (e) { s.error = e.message; }
  finally { s.loading = false; renderAll(); }
}

function followJob(scope, id, since) {
  const s = st(scope);
  if (s.job && s.job.id === id) return;
  const job = { id, start: since || Date.now(), timer: null };
  s.job = job;
  const done = (err) => {
    if (s.job !== job) return;
    s.job = null; s.err = err || null;
    load(scope).then(() => { if (s.job !== job) s.pending = null; renderAll(); });
  };
  const step = async () => {
    if (s.job !== job) return;
    if (Date.now() - job.start > JOB_GIVEUP_MS) { done('No reply after 11 minutes: the job was given up on this screen. Reload the page to check again.'); return; }
    try {
      const r = await ctx.api.get('spec-change-job/' + encodeURIComponent(id), 10000);
      if (s.job !== job) return;
      if (r.state === 'ok') { done(null); return; }
      if (r.state === 'error') { done(SEND_ERRORS[r.code] || r.error || 'The AI failed while writing the reply.'); return; } // a coded error = the refusal the 4xx used to give
    } catch (e) {
      if (s.job !== job) return;
      if (e.status === 404) { done('The reply job was not found on the server.'); return; }
    }
    job.timer = setTimeout(step, JOB_POLL_MS);
    renderAll();
  };
  job.timer = setTimeout(step, JOB_POLL_MS);
  renderAll();
}

const busyNow = (s) => !!(s.job || (s.data && s.data.thread.busy));
/** Why the input is locked (null = open). */
function inputLock(s) {
  if (!s.data) return s.error ? 'This scope could not be read: ' + s.error : 'Reading…';
  if (s.data.loop_running || status() === 'running') return SEND_ERRORS.loop_running;
  return null;
}

/** `input` (optional, from submit): `clear()` empties the textarea and draft when the message is accepted (before the POST), `back()` puts
 *  the text back after a failed POST only if the user typed nothing new meanwhile. */
async function send(scope, text, input) {
  const s = st(scope), lock = inputLock(s);
  if (lock) { toast(lock, 'err'); return false; }
  if (busyNow(s) || s.sending) { toast(SEND_ERRORS.spec_change_busy, 'err'); return false; }
  if (text.length > MAX_TEXT) { toast(`The message has ${text.length} characters; the limit is ${MAX_TEXT}.`, 'err'); return false; }
  s.sending = true; s.err = null; s.lastText = text; s.pending = { role: 'user', text, at: new Date().toISOString() };
  if (input) input.clear();
  renderAll();
  let ok = false;
  try {
    const r = await ctx.api.post('spec-change/' + encodeURIComponent(scope) + '/send', { text }, 30000);
    ok = true;
    if (r.job_id) followJob(scope, r.job_id, Date.now()); else load(scope);
  } catch (e) {
    const code = e.body && e.body.code;
    s.pending = null; if (input) input.back();
    s.err = SEND_ERRORS[code] || 'Not sent: ' + e.message;
    toast(s.err, 'err');
    if (code) load(scope);
  } finally { s.sending = false; renderAll(); }
  return ok;
}
async function submit(ev, scope) {
  ev.preventDefault();
  const ta = document.getElementById('rc-text'), s = st(scope);
  const text = ta ? ta.value.trim() : '';
  if (!text) { if (ta) ta.focus(); return; }
  await send(scope, text, {
    clear: () => { if (ta) ta.value = ''; s.draft = ''; },
    back: () => { if (s.draft) return; s.draft = text; const now = document.getElementById('rc-text'); if (now) now.value = text; },
  });
}
async function resetConversation(scope) {
  const s = st(scope);
  if (busyNow(s)) return;
  const ok = await confirmDialog({ title: 'Start a new conversation?', text: 'Clears this chat; the next message starts a new AI session. The spec files stay as they are.', yesText: 'New conversation', danger: true });
  if (!ok) return;
  try { await ctx.api.post('spec-change/' + encodeURIComponent(scope) + '/reset', { confirm: true }); s.err = null; s.pending = null; s.lastText = null; toast('New conversation: the files were kept.', 'ok'); }
  catch (e) { toast('Not cleared: ' + e.message, 'err'); }
  finally { load(scope); }
}

/** Sends the existing loop instruction: the loop reads the changed files and redoes the affected work. */
async function tellLoop(scope, changed) {
  const s = st(scope), feature = scope === PROJECT_SCOPE ? null : scope;
  const files = changed.join(', ');
  const text = feature
    ? `The definition of ${feature} was changed by the user (${files}). Read it again and redo the affected work: send the feature back through the graph as far as needed (spec, gate, build, verify) and keep what the change does not touch.`
    : `The project plan was changed by the user (${files}). Read it again, find the features it affects and redo their affected work (spec, gate, build, verify). Do not change the features, stages or dependencies in the ROADMAP.`;
  const ok = await confirmDialog({ title: 'Tell the loop to redo the affected work?', text: `Sends an instruction to the loop about ${feature || 'the plan'}. A stopped loop is resumed; a running one reads it on its next turn.`, yesText: 'Tell the loop' });
  if (!ok) return;
  try {
    const body = feature ? { text, feature } : { text };
    await ctx.api.post('instruction', body, 30000);
    s.redone = (s.data && s.data.thread.messages.length) + '|' + files; toast('Instruction sent to the loop.', 'ok');
  } catch (e) { toast('Not sent: ' + e.message, 'err'); }
  finally { renderAll(); }
}

// ---------- drawing ----------
function Msg({ m }) {
  const ai = m.role === 'ai', changed = list(m.changed);
  const meta = [fmtHour(m.at), ai && isNum(m.cost_usd) ? cost(m.cost_usd) : ''].filter(Boolean).join(' · ');
  return html`<div class=${'cmsg ' + (ai ? 'ai' : 'user') + (m.pending ? ' plan-pending' : '')}>
    <div class="cmsg-h"><span>${ai ? 'AI · edits the spec' : 'you'}</span><span class="t">${meta}</span></div>
    ${ai ? html`<div class="cmsg-b md"><${Markdown} source=${m.text == null ? '' : String(m.text)} /></div>` : html`<div class="cmsg-b">${String(m.text == null ? '' : m.text)}</div>`}
    ${ai ? html`<div class="rc-changed" aria-label="Files changed">${changed.length ? [html`<span key="l" class="small-label">changed</span>`, ...changed.map((f) => html`<code key=${f}>${f}</code>`)]
      : html`<span class="small-label">no file changed</span>`}</div>` : null}
  </div>`;
}
function messagesNow(s) {
  const ms = list(s.data && s.data.thread.messages), p = s.pending, lastUser = [...ms].reverse().find((m) => m.role === 'user');
  if (!p || (lastUser && lastUser.text === p.text)) return ms;
  return [...ms, { ...p, pending: true }];
}

function Side({ scope, groups }) {
  return html`<aside class="rc-side" aria-label="Stages and features">
    <div class="files-side-h"><span class="small-label">Plan</span></div>
    <button type="button" class=${'frow rc-plan' + (scope === PROJECT_SCOPE ? ' sel' : '')} aria-pressed=${String(scope === PROJECT_SCOPE)} onClick=${() => pickScope(PROJECT_SCOPE)}
      title="PROJECT.md, ROADMAP.md, FLOWS.md and RULES.md"><span class="nm">Whole plan</span><span class="chip rc-chip-plain">ROADMAP · FLOWS</span></button>
    ${groups.length ? groups.map((g) => html`<div key=${String(g.n)} class="rc-stage">
      <div class="small-label">${g.title}</div>
      <div class="frows">${g.items.map((f) => {
        const [col, bg] = pal(f.status), sel = scope === f.id;
        return html`<button key=${f.id} type="button" class=${'frow' + (sel ? ' sel' : '')} title=${f.id} aria-pressed=${String(sel)} onClick=${() => pickScope(f.id)}>
          <span class="nm">${txt(f.name || f.id)}</span><span class="chip" style=${{ color: col || null, background: bg || null }}>${txt(f.status)}</span></button>`;
      })}</div></div>`)
      : html`<div class="empty-note">No feature in the roadmap yet.</div>`}
  </aside>`;
}

function Chat({ scope }) {
  const s = st(scope), listRef = useRef(null), taRef = useRef(null), stick = useRef(true), seen = useRef({ scope: null, n: 0 });
  const ms = messagesNow(s), busy = busyNow(s), lock = inputLock(s), files = list(s.data && s.data.files);
  useEffect(() => { if (!s.data && !s.loading) load(scope); }, [scope]);
  useLayoutEffect(() => { if (taRef.current) taRef.current.value = s.draft || ''; }, [scope]);
  useLayoutEffect(() => {
    const el = listRef.current, n = ms.length, prev = seen.current.scope === scope ? seen.current.n : 0;
    seen.current = { scope, n };
    if (el && n > prev && (stick.current || !prev || ms[n - 1].role === 'user')) el.scrollTop = el.scrollHeight;
  });
  const onScroll = (e) => { const el = e.currentTarget; stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 60; };
  const lastAi = [...ms].reverse().find((m) => m.role === 'ai');
  const changed = lastAi && !busy ? list(lastAi.changed) : [];
  const redoKey = (s.data && s.data.thread.messages.length) + '|' + changed.join(', ');
  const feat = scope === PROJECT_SCOPE ? null : features().find((f) => f.id === scope);
  const title = scope === PROJECT_SCOPE ? 'Change the plan with the AI' : 'Change a spec with the AI';
  return html`<section class="plan-col plan-chat rc-chat" aria-label="Spec change chat">
    <div class="plan-h"><span class="plan-title">${title}</span><span class="rc-scope mono">${scope === PROJECT_SCOPE ? 'whole plan' : scope}</span>
      ${feat ? html`<span class="chip" style=${{ color: pal(feat.status)[0], background: pal(feat.status)[1] }}>${txt(feat.status)}</span>` : null}<span class="spacer"></span>
      <button type="button" class="btn btn-sm btn-ghost" id="rc-reset" disabled=${busy || !ms.length} title="clears the chat; the files stay" onClick=${() => resetConversation(scope)}>New conversation</button></div>
    <div class="rc-files" aria-label="Files the AI may edit"><span class="small-label">the AI may edit</span>${files.length ? files.map((f) => html`<code key=${f}>${f}</code>`) : html`<span class="dim-mono">${s.data ? 'no file yet in this scope' : '…'}</span>`}</div>
    <div class="ch-empty plan-empty" hidden=${!!ms.length}>${ms.length ? '' : html`<p class="plan-empty-t">Found something new while the loop runs?</p>
      <p>Describe the change in this ${scope === PROJECT_SCOPE ? 'plan' : 'feature'} and the AI edits the spec files already written (it keeps the requirement IDs). Then you decide whether to tell the loop to redo the affected work. The loop must be paused while you edit.</p>`}</div>
    <div class="ch-list plan-list" role="log" aria-live="polite" ref=${listRef} onScroll=${onScroll}>${ms.map((m, i) => html`<${Msg} key=${i} m=${m} />`)}</div>
    <${Think} job=${busy ? s.job || { start: Date.now() } : null} />
    ${changed.length ? html`<div class="rc-redo" role="status"><span>${s.redone === redoKey ? 'The loop was told about this change.' : `${changed.length} file(s) changed: the work already done from them may be out of date.`}</span>
      <button type="button" class="btn btn-sm btn-primary" id="rc-redo" disabled=${s.redone === redoKey} onClick=${() => tellLoop(scope, changed)}>Tell the loop to redo it</button></div>` : null}
    <div class="ch-err" role="alert" hidden=${!s.err}>${s.err ? [html`<span key="t">${s.err}</span>`,
      s.lastText && !busy ? html`<button key="r" type="button" class="btn btn-sm" onClick=${() => send(scope, s.lastText)}>Send again</button>` : null,
      html`<button key="x" type="button" class="btn btn-sm btn-ghost" onClick=${() => { s.err = null; renderAll(); }}>Dismiss</button>`] : null}</div>
    <form class="plan-form" id="rc-form" aria-label="Message to the spec AI" onSubmit=${(e) => submit(e, scope)}>
      <div class="plan-lock" role="status" hidden=${!lock}>${lock || ''}</div>
      <div class="cp-box plan-box">
        <textarea id="rc-text" ref=${taRef} rows="3" aria-label="Message" disabled=${!!lock} maxlength=${MAX_TEXT}
          placeholder=${lock ? '' : 'What changed? (Enter sends, Shift+Enter new line)'}
          onInput=${(e) => { s.draft = e.currentTarget.value; }}
          onKeyDown=${(e) => { if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); const f = document.getElementById('rc-form'); if (f) f.requestSubmit(); } }}></textarea>
        <button type="submit" class="btn btn-primary" id="rc-send" disabled=${!!lock || busy || s.sending}>${busy ? 'Writing…' : 'Send'}</button>
      </div>
      <p class="plan-safe">${SAFE_TEXT}</p>
    </form>
  </section>`;
}

/** Content of #canvas in the "Roadmap" view (div.rc): stages and features on the side, the chat of the selected scope. */
export function RoadmapChat() {
  const scope = currentScope();
  return html`<div class="rc"><${Side} scope=${scope} groups=${groupStages()} /><${Chat} key=${scope} scope=${scope} /></div>`;
}
