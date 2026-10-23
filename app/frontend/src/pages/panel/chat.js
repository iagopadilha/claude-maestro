/* Chat (read-only) and the unified composer: Instruct the loop | Chat.
   Chat = questions to the AI (read-only) + the immediate actions (collapsible "Actions", always with confirmation).
   Old version (snapshot or agent of a past turn) = Chat only, without actions. */
import { $ } from '../../util/dom.js';
import { html, useState, useEffect, useLayoutEffect, useRef } from '../../util/preact.js';
import { isNum, num, cost, txt, list, fmtHour, fmtDay } from '../../util/format.js';
import { toast } from '../../components/toast.js';
import { Markdown } from '../../components/markdown.js';
import { ctx, ui, status, live, features, agentById, selAgentId, verInfo } from './context.js';
import { renderAll, requestRender, schedulePoll } from '../project_panel.js';
import { openNewLoop } from './alert.js';
import { actState, loadActions, proposeAction } from './act.js';

const chats = new Map(); // scope key -> thread (thread, messages, job, error, list of threads)
const cp = { mode: 'instruct', manualFeat: false, feat: '', selKey: null, status: null, sending: false };
const CHAT_POLL_MS = 1500, CHAT_GIVEUP_MS = 200000;
const INSTRUCTION_MESSAGES = { note: 'Noted for the next turn.', answer: 'Answered, resuming the loop.', 'note+resume': 'Noted and loop resumed.' };
let shownKey = null; // scope key of the mounted <Chat>; null while the Chat tab is closed

function chatScope() {
  const v = verInfo();
  let agent = null;
  if (!ui.chatGeneral) { const id = selAgentId() || (live() && !ui.sel && agentById('orch') ? 'orch' : null); if (id) agent = agentById(id) || null; }
  if (agent) return { key: agent.id, type: 'agent', agent_id: agent.id, turn: v.n, agent, label: `${txt(agent.role)}${agent.feature ? ' · ' + agent.feature : ''}`, v };
  return { key: 'general:' + (v.old ? v.n : 'current'), type: 'general', agent_id: null, turn: v.old ? v.n : null, agent: null, label: 'project', v };
}
function getChat(key) {
  let c = chats.get(key);
  if (!c) { c = { thread: null, msgs: [], rev: 0, job: null, err: null, lastQ: null, scope: null, threads: null, loadingThreads: false, threadsErr: null }; chats.set(key, c); }
  return c;
}

function ChatMsg({ m, c }) {
  const ai = m.role === 'ai';
  const meta = [fmtHour(m.t), ai && isNum(m.cost_usd) ? cost(m.cost_usd) : ''].filter(Boolean).join(' · ');
  return html`<div class=${'cmsg ' + (ai ? 'ai' : 'user')}>
    <div class="cmsg-h"><span>${ai ? 'AI · read-only' : 'you'}</span><span class="t">${meta}</span></div>
    ${ai ? html`<div class="cmsg-b md"><${Markdown} source=${m.text} /></div>` : html`<div class="cmsg-b">${String(m.text == null ? '' : m.text)}</div>`}
    ${ai && !canAct() ? html`<button type="button" class="btn btn-sm btn-ghost cmsg-take" title="attaches this question and answer to the context of the next instruction to the current orchestrator"
      onClick=${() => takeToCurrent(c, m)}>Bring to the current version</button>` : null}
  </div>`;
}
// own 1 s tick while an answer is pending: the rest of the inspector does not re-render (also used by the planning chat)
export function Think({ job }) {
  const [, tick] = useState(0);
  useEffect(() => {
    if (!job) return undefined;
    const t = setInterval(() => tick((x) => x + 1), 1000);
    return () => clearInterval(t);
  }, [job]);
  return html`<div class="ch-think" role="status" hidden=${!job}>${job ? `thinking… ${Math.floor((Date.now() - job.start) / 1000)} s` : ''}</div>`;
}
function Threads({ sc, c }) {
  const ts = list(c.threads);
  return html`<details class="ch-thr">
    <summary class="ch-thr-sum">${c.loadingThreads ? 'Earlier chats…' : c.threadsErr ? 'Earlier chats (unavailable)' : `Earlier chats (${ts.length})`}</summary>
    <div class="ch-thr-list">${ts.length ? ts.map((t) => html`<button key=${t.thread} type="button" class=${'ch-thr-item' + (t.thread === c.thread ? ' on' : '')} title=${t.thread} onClick=${() => chatOpenThread(sc.key, t.thread)}>
        <span class="ch-thr-t">${txt(t.title)}</span><span class="ch-thr-m">${`${fmtDay(t.updated_at)} · ${num(t.message_count)} msg${isNum(t.cost_usd) ? ' · ' + cost(t.cost_usd) : ''}`}</span>
      </button>`) : html`<div class="empty-note">${c.threadsErr ? String(c.threadsErr) : 'No earlier chat in this scope.'}</div>`}</div>
  </details>`;
}

/** Chat of the current scope (agent or project): the Chat tab content of the inspector. */
export function Chat() {
  const sc = chatScope(), c = getChat(sc.key);
  const listRef = useRef(null), stick = useRef(true), seen = useRef(null);
  useLayoutEffect(() => { shownKey = sc.key; });
  useEffect(() => () => { shownKey = null; }, []);
  // new scope/thread opens at the bottom; new messages follow only if the reader was at the bottom (or just asked)
  useLayoutEffect(() => {
    const el = listRef.current, id = sc.key + '|' + c.rev, n = c.msgs.length, prev = seen.current;
    seen.current = { id, n };
    if (!prev || prev.id !== id) { el.scrollTop = el.scrollHeight; stick.current = true; return; }
    if (n > prev.n && (stick.current || c.msgs[n - 1].role === 'user')) el.scrollTop = el.scrollHeight;
  });
  useEffect(() => { if (c.threads == null && !c.loadingThreads && !c.threadsErr) chatLoadThreads(sc); });
  const onScroll = (e) => { const el = e.currentTarget; stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 60; };
  const hasContent = !!(c.msgs.length || c.job);
  return html`<div class="chat">
    <div class="ch-head"><span class="ch-title">${sc.agent ? `Chat with ${sc.label}` : 'Chat about the project'}</span><span class="spacer"></span>
      <button type="button" class="btn btn-sm btn-ghost" hidden=${!(ui.chatGeneral || selAgentId() || (live() && agentById('orch')))}
        onClick=${() => { ui.chatGeneral = !ui.chatGeneral; renderAll(); }}>${ui.chatGeneral ? 'Back to the agent' : 'About the project'}</button>
      <button type="button" class="btn btn-sm btn-ghost" disabled=${!!c.job} onClick=${chatNew}>New chat</button>
    </div>
    <${Threads} sc=${sc} c=${c} />
    <div class="ch-empty" hidden=${hasContent}>${hasContent ? '' : sc.agent ? `Ask something about ${sc.label}. The AI only reads; nothing is changed.` : 'Ask something about the project. The AI only reads; nothing is changed.'}</div>
    <div class="ch-list" role="log" aria-live="polite" ref=${listRef} onScroll=${onScroll}>${c.msgs.map((m, i) => html`<${ChatMsg} key=${c.rev + ':' + i} m=${m} c=${c} />`)}</div>
    <${Think} job=${c.job} />
    <div class="ch-err" role="alert" hidden=${!c.err}>${c.err ? [html`<span>${c.err}</span>`, c.lastQ ? html`<button type="button" class="btn btn-sm" onClick=${() => chatRetry(sc.key)}>try again</button>` : ''] : null}</div>
  </div>`;
}

const chatVisible = (key) => ui.tab === 'chat' && shownKey === key;
function chatRefresh() { renderAll(); }
async function chatLoadThreads(sc) {
  const c = getChat(sc.key); c.loadingThreads = true; c.threadsErr = null;
  if (chatVisible(sc.key)) requestRender(); // called from the Chat effect: render after the effects flush
  try {
    const q = (sc.agent_id ? 'agent_id=' + encodeURIComponent(sc.agent_id) + '&' : '') + 'turn=' + encodeURIComponent(sc.turn == null ? '' : sc.turn);
    const r = await ctx.api.get('chat/threads?' + q, 10000);
    c.threads = list(r && r.threads);
  } catch (e) { c.threads = []; c.threadsErr = e.status === 404 ? 'The server does not list chats yet.' : e.message; }
  finally { c.loadingThreads = false; if (chatVisible(sc.key)) renderAll(); }
}
async function chatOpenThread(key, thread) {
  const c = getChat(key);
  if (c.job) { toast('Wait for the current answer.', 'err'); return; }
  try {
    const r = await ctx.api.get('chat/thread/' + encodeURIComponent(thread), 10000);
    c.thread = thread; c.msgs = list(r && r.messages).map((m) => ({ role: m.role === 'ai' ? 'ai' : 'user', text: m.text, t: m.t, cost_usd: m.cost_usd, mode: m.mode })); c.rev += 1; c.err = null; c.lastQ = null;
    chatRefresh();
  } catch (e) { toast('Could not open the chat: ' + e.message, 'err'); }
}
function chatNew() {
  const sc = chatScope(), c = getChat(sc.key);
  if (c.job) return;
  c.thread = null; c.msgs = []; c.rev += 1; c.err = null; c.lastQ = null; c.threads = null; chatRefresh();
}
async function chatSend(text, again) {
  const sc = chatScope(), c = getChat(sc.key);
  if (c.job) { toast('Wait for the current answer.', 'err'); return; }
  c.err = null; c.lastQ = text; c.scope = sc;
  if (!again) c.msgs.push({ role: 'user', text, t: new Date().toISOString(), mode: 'question' });
  const job = { id: null, start: Date.now(), timer: null }; c.job = job;
  chatRefresh();
  try {
    const r = await ctx.api.post('chat/send', { thread: c.thread, scope: { type: sc.type, agent_id: sc.agent_id, turn: sc.turn }, text });
    if (c.job !== job) return;
    c.thread = r.thread || c.thread; job.id = r.job; chatPoll(sc.key, job);
  } catch (e) {
    if (c.job !== job) return;
    c.job = null; c.err = e.message;
    if (!again) { const box = $('cp-text'); if (box && !box.value) box.value = text; } // the text goes back only if the user typed nothing new
    chatRefresh();
  }
}
function chatRetry(key) { const c = getChat(key); if (c.lastQ) chatSend(c.lastQ, true); }
function chatPoll(key, job) {
  const c = chats.get(key);
  job.timer = setTimeout(async () => {
    if (!c || c.job !== job) return;
    if (Date.now() - job.start > CHAT_GIVEUP_MS) { c.job = null; c.err = 'timed out'; chatRefresh(); return; }
    try {
      const r = await ctx.api.get('chat/job/' + encodeURIComponent(job.id), 10000);
      if (c.job !== job) return;
      if (r.state === 'ok') {
        c.job = null; c.thread = r.thread || c.thread; c.threads = null;
        c.msgs.push({ role: 'ai', text: r.reply == null ? '' : String(r.reply), t: new Date().toISOString(), cost_usd: r.cost_usd, mode: 'question' });
        if (!chatVisible(key)) toast('The AI answered your question (Chat tab).', 'ok');
      } else if (r.state === 'error') { c.job = null; c.threads = null; c.err = r.error || 'error while answering'; }
      else { chatPoll(key, job); chatRefresh(); return; }
      chatRefresh();
    } catch (e) {
      if (c.job !== job) return;
      if (e.status === 404) { c.job = null; c.err = 'the chat was not found on the server'; chatRefresh(); } else chatPoll(key, job);
    }
  }, CHAT_POLL_MS);
}
function takeToCurrent(c, m) {
  const i = c.msgs.indexOf(m);
  let q = ''; for (let k = i - 1; k >= 0; k -= 1) if (c.msgs[k].role === 'user') { q = c.msgs[k].text; break; }
  if (ui.attach.length >= 5) { toast('At most 5 attached items.', 'err'); return; }
  const sc = chatScope();
  ui.attach.push({ id: Date.now() + '-' + ui.attach.length, label: q || 'answer', text: `Context from turn ${sc.v.n} (${sc.label}). Question: ${q}\nAnswer: ${m.text}`.slice(0, 2000) });
  toast('Attached: it goes along with the next instruction to the loop.', 'ok'); renderAll();
}

// unified composer (instruct the loop | chat | read-only old version)
function autoFeature() {
  const a = selAgentId() ? agentById(selAgentId()) : null;
  if (a && a.feature) return a.feature;
  return ui.sel && ui.sel.kind === 'feature' ? ui.sel.id : '';
}
// instruct/actions only exist on the orchestrator of the CURRENT version; everything else is chat only (the other options do not even show)
export function canAct() {
  if (verInfo().old) return false;
  const id = selAgentId();
  if (!id) return true; // no agent: the inspector shows the current orchestrator
  const a = agentById(id);
  return !!a && a.role === 'orchestrator';
}
/** What the composer shows now (also resets the manual feature choice when the selection changes). */
export function composerModel() {
  const v = verInfo(), act = canAct(), mode = act ? cp.mode : 'chat';
  const selKey = ui.sel ? ui.sel.kind + ':' + (ui.sel.id != null ? ui.sel.id : ui.sel.n) : '';
  if (selKey !== cp.selKey) { cp.selKey = selKey; cp.manualFeat = false; }
  const featVal = cp.manualFeat ? cp.feat : autoFeature();
  const sc = chatScope();
  return { v, act, mode, featVal, fs: features().map((f) => f.id), who: sc.agent ? txt(sc.agent.role) : 'project', asking: !!getChat(sc.key).job };
}
const pickMode = (key) => { cp.mode = key; cp.status = null; renderAll(); $('cp-text').focus(); if (key === 'chat') loadActions(); };

/** Content of #cp-band. */
export function ComposerBand({ m }) {
  return m.act ? html`<strong>CURRENT VERSION</strong><span>${` · turn ${num(m.v.n)}`}</span>` : html`<span class="cp-who">${`Chat with ${m.who} · turn ${num(m.v.n)}`}</span>`;
}
/** Content of #cp-seg (role=tablist; arrows handled by bindTabKeys). */
export function ComposerSeg({ m }) {
  if (!m.act) return null;
  return [['instruct', 'Instruct the loop'], ['chat', 'Chat']].map(([key, label]) => html`<button key=${key} type="button" role="tab" class="cp-seg-btn" id=${'cp-seg-' + key}
    aria-selected=${String(m.mode === key)} tabindex=${m.mode === key ? '0' : '-1'} onClick=${() => pickMode(key)}>${label}</button>`);
}
/** Options of #cp-feature. */
export function FeatureOptions({ fs }) {
  return [html`<option key="" value="">(none)</option>`, ...fs.map((id) => html`<option key=${id} value=${id}>${id}</option>`)];
}
/** Content of #cp-tray: context attached from old chats. */
export function ComposerTray() {
  const drop = (a) => { ui.attach = ui.attach.filter((x) => x.id !== a.id); renderAll(); };
  return html`<span class="cp-tray-l">attached context:</span>${ui.attach.map((a) => html`<span key=${a.id} class="cp-chip" title=${a.text}>
    <span class="cp-chip-t">${a.label.length > 40 ? a.label.slice(0, 40) + '…' : a.label}</span>
    <button type="button" class="cp-chip-x" aria-label="remove attachment" onClick=${() => drop(a)}>×</button></span>`)}`;
}
/** Content of #cp-status. */
export function ComposerStatus({ s }) {
  return html`<span>${s.text}</span>${s.openNew ? html`<button type="button" class="btn btn-sm" onClick=${openNewLoop}>Open New loop</button>` : ''}`;
}

/** Container flags of the composer form (class, hidden parts, select value, placeholder, send button, status). */
export function composerView() {
  const m = composerModel(), { act, mode } = m, on = (k) => act && mode === k, card = actState.card;
  const instructing = act && mode === 'instruct';
  return {
    m, on,
    cls: 'composer m-' + (instructing ? 'instruct' : 'chat'),
    featValue: m.fs.includes(m.featVal) ? m.featVal : '',
    showTray: instructing && ui.attach.length > 0,
    showActions: on('chat'),
    hint: !act ? 'Read-only: the AI only reads the project.' : instructing ? 'The next turn applies it.' : 'Ask the AI (read-only) or open Actions.',
    placeholder: !act ? `Ask about ${m.who}…` : instructing ? 'Instruct the loop…' : 'Ask the AI about the project…',
    sendText: instructing ? 'Instruct the loop' : 'Ask',
    sendDisabled: cp.sending || (!instructing && m.asking),
    proposing: !!card && (card.kind === 'proposing' || card.kind === 'running'),
    status: act && cp.status ? cp.status : null,
  };
}
/** The <textarea id="cp-text"> is uncontrolled (no value prop): typing and the caret survive every render. */
export function composerKeyDown(e) { if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); $('composer').requestSubmit(); } }
/** "Propose an action" button of the Actions panel: the text typed in the box becomes a proposal (still confirmed by the user). */
export function proposeFromComposer() {
  const ta = $('cp-text'), text = ta ? ta.value.trim() : '';
  if (!text) { toast('Type what to do in the message box first.', 'err'); return; }
  if (actState.card && (actState.card.kind === 'proposing' || actState.card.kind === 'running')) return;
  ta.value = ''; proposeAction(text);
}
export function pickFeature(e) { cp.manualFeat = true; cp.feat = e.currentTarget.value; }
export async function composerSubmit(ev) {
  ev.preventDefault();
  const ta = $('cp-text'), text = ta.value.trim();
  if (!text) return;
  const mode = canAct() ? cp.mode : 'chat';
  if (mode === 'chat') { ta.value = ''; ui.tab = 'chat'; renderAll(); chatSend(text); return; }
  if (cp.sending) return;
  cp.sending = true; cp.status = null; renderAll();
  try {
    const feature = $('cp-feature').value || null;
    const r = await ctx.api.post('instruction', { text, feature, attachments: ui.attach.slice(0, 5).map((a) => a.text.slice(0, 2000)) });
    const msg = INSTRUCTION_MESSAGES[r.action] || 'Instruction sent.';
    ta.value = ''; ui.attach = []; cp.status = { kind: 'ok', text: msg }; toast(msg, 'ok'); schedulePoll(800);
  } catch (e) {
    cp.status = { kind: 'err', text: e.message, openNew: e.status === 409 && (status() === 'no_loop' || /new loop/i.test(e.message)) };
  } finally { cp.sending = false; renderAll(); }
}
