/* Chat (read-only) and the unified composer: Instruct the loop | Act | Ask.
   Old version (snapshot or agent of a past turn) = Ask only. */
import { $, h, fill, setText } from '../../util/dom.js';
import { isNum, num, cost, txt, list, fmtHour, fmtDay } from '../../util/format.js';
import { toast } from '../../components/toast.js';
import { renderMarkdown } from '../../components/markdown.js';
import { ctx, ui, region, status, live, features, agentById, selAgentId, verInfo, lastN } from './context.js';
import { schedulePoll } from '../project_panel.js';
import { renderInspector } from './inspector.js';
import { openNewLoop } from './alert.js';
import { actState, actSignature, actPanel, loadActions, proposeAction } from './act.js';

const chats = new Map(); // scope key -> thread (thread, messages, job, error, list of threads)
const cp = { mode: 'instruct', manualFeat: false, feat: '', selKey: null, status: null, sending: false };
const CHAT_POLL_MS = 1500, CHAT_GIVEUP_MS = 200000;
const INSTRUCTION_MESSAGES = { note: 'Noted for the next turn.', answer: 'Answered, resuming the loop.', 'note+resume': 'Noted and loop resumed.' };
const chEl = { root: null, title: null, btnNew: null, btnScope: null, thr: null, thrSum: null, thrList: null, list: null, empty: null, think: null, err: null, key: null, shown: 0, rev: -1, thrSig: '' };

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

function buildChat() {
  chEl.title = h('span', { class: 'ch-title' });
  chEl.btnNew = h('button', { type: 'button', class: 'btn btn-sm btn-ghost', onclick: chatNew }, 'New chat');
  chEl.btnScope = h('button', { type: 'button', class: 'btn btn-sm btn-ghost', onclick: () => { ui.chatGeneral = !ui.chatGeneral; chatSync(); renderComposer(); } });
  chEl.thrSum = h('summary', { class: 'ch-thr-sum' });
  chEl.thrList = h('div', { class: 'ch-thr-list' });
  chEl.thr = h('details', { class: 'ch-thr' }, chEl.thrSum, chEl.thrList);
  chEl.list = h('div', { class: 'ch-list', role: 'log', 'aria-live': 'polite' });
  chEl.empty = h('div', { class: 'ch-empty' });
  chEl.think = h('div', { class: 'ch-think', role: 'status' });
  chEl.err = h('div', { class: 'ch-err', role: 'alert' });
  chEl.root = h('div', { class: 'chat' }, h('div', { class: 'ch-head' }, chEl.title, h('span', { class: 'spacer' }), chEl.btnScope, chEl.btnNew), chEl.thr, chEl.empty, chEl.list, chEl.think, chEl.err);
}
export function mountChat(body) {
  if (!chEl.root) buildChat();
  if (body.firstChild !== chEl.root || body.childNodes.length !== 1) fill(body, chEl.root);
  chatSync();
}
function msgEl(m, c) {
  const ai = m.role === 'ai';
  const meta = [fmtHour(m.t), ai && isNum(m.cost_usd) ? cost(m.cost_usd) : ''].filter(Boolean).join(' · ');
  const el = h('div', { class: 'cmsg ' + (ai ? 'ai' : 'user') },
    h('div', { class: 'cmsg-h' }, h('span', { text: ai ? 'AI · read-only' : 'you' }), h('span', { class: 't', text: meta })),
    ai ? h('div', { class: 'cmsg-b md' }, renderMarkdown(m.text)) : h('div', { class: 'cmsg-b', text: String(m.text == null ? '' : m.text) }));
  if (ai && !canAct()) el.appendChild(h('button', { type: 'button', class: 'btn btn-sm btn-ghost cmsg-take', title: 'attaches this question and answer to the context of the next instruction to the current orchestrator', onclick: () => takeToCurrent(c, m) }, 'Bring to the current version'));
  return el;
}
function chatSync() {
  if (!chEl.root) buildChat();
  const sc = chatScope(), c = getChat(sc.key);
  if (chEl.key !== sc.key) { chEl.key = sc.key; chEl.shown = 0; chEl.rev = c.rev; chEl.list.replaceChildren(); chEl.thrSig = ''; }
  if (chEl.rev !== c.rev || c.msgs.length < chEl.shown) { chEl.rev = c.rev; chEl.shown = 0; chEl.list.replaceChildren(); }
  setText(chEl.title, sc.agent ? `Chat with ${sc.label}` : 'Chat about the project');
  setText(chEl.btnScope, ui.chatGeneral ? 'Back to the agent' : 'About the project');
  chEl.btnScope.hidden = !(ui.chatGeneral || selAgentId() || (live() && agentById('orch')));
  chEl.btnNew.disabled = !!c.job;
  const stick = chEl.list.scrollHeight - chEl.list.scrollTop - chEl.list.clientHeight < 60;
  let added = false;
  while (chEl.shown < c.msgs.length) { chEl.list.appendChild(msgEl(c.msgs[chEl.shown], c)); chEl.shown += 1; added = true; }
  if (added && (stick || c.msgs[c.msgs.length - 1].role === 'user')) chEl.list.scrollTop = chEl.list.scrollHeight;
  setText(chEl.empty, c.msgs.length || c.job ? '' : sc.agent ? `Ask something about ${sc.label}. The AI only reads; nothing is changed.` : 'Ask something about the project. The AI only reads; nothing is changed.');
  chEl.empty.hidden = !!(c.msgs.length || c.job);
  tickThink(c);
  chEl.err.replaceChildren();
  if (c.err) chEl.err.append(h('span', { text: c.err }), c.lastQ ? h('button', { type: 'button', class: 'btn btn-sm', onclick: () => chatRetry(sc.key) }, 'try again') : '');
  chEl.err.hidden = !c.err;
  // earlier threads
  const sig = JSON.stringify([c.threads, c.thread, c.loadingThreads, c.threadsErr]);
  if (sig !== chEl.thrSig) {
    chEl.thrSig = sig;
    const n = list(c.threads).length;
    setText(chEl.thrSum, c.loadingThreads ? 'Earlier chats…' : c.threadsErr ? 'Earlier chats (unavailable)' : `Earlier chats (${n})`);
    chEl.thrList.replaceChildren(...(n ? list(c.threads).map((t) => h('button', { type: 'button', class: 'ch-thr-item' + (t.thread === c.thread ? ' on' : ''), title: t.thread, onclick: () => chatOpenThread(sc.key, t.thread) },
      h('span', { class: 'ch-thr-t', text: txt(t.title) }), h('span', { class: 'ch-thr-m', text: `${fmtDay(t.updated_at)} · ${num(t.message_count)} msg${isNum(t.cost_usd) ? ' · ' + cost(t.cost_usd) : ''}` })))
      : [h('div', { class: 'empty-note', text: c.threadsErr ? String(c.threadsErr) : 'No earlier chat in this scope.' })]));
  }
  if (c.threads == null && !c.loadingThreads && !c.threadsErr) chatLoadThreads(sc);
}
function tickThink(c) {
  if (!chEl.root) return;
  const sc = chatScope(), cc = c || getChat(sc.key);
  if (cc.job) { chEl.think.hidden = false; setText(chEl.think, `thinking… ${Math.floor((Date.now() - cc.job.start) / 1000)} s`); } else { chEl.think.hidden = true; }
}
const chatVisible = (key) => ui.tab === 'chat' && chEl.root && chEl.root.isConnected && chEl.key === key;
function chatRefresh(key) { if (chatVisible(key)) chatSync(); renderComposer(); }
async function chatLoadThreads(sc) {
  const c = getChat(sc.key); c.loadingThreads = true; c.threadsErr = null;
  try {
    const q = (sc.agent_id ? 'agent_id=' + encodeURIComponent(sc.agent_id) + '&' : '') + 'turn=' + encodeURIComponent(sc.turn == null ? '' : sc.turn);
    const r = await ctx.api.get('chat/threads?' + q, 10000);
    c.threads = list(r && r.threads);
  } catch (e) { c.threads = []; c.threadsErr = e.status === 404 ? 'The server does not list chats yet.' : e.message; }
  finally { c.loadingThreads = false; if (chatVisible(sc.key)) chatSync(); }
}
async function chatOpenThread(key, thread) {
  const c = getChat(key);
  if (c.job) { toast('Wait for the current answer.', 'err'); return; }
  try {
    const r = await ctx.api.get('chat/thread/' + encodeURIComponent(thread), 10000);
    c.thread = thread; c.msgs = list(r && r.messages).map((m) => ({ role: m.role === 'ai' ? 'ai' : 'user', text: m.text, t: m.t, cost_usd: m.cost_usd, mode: m.mode })); c.rev += 1; c.err = null; c.lastQ = null;
    chatRefresh(key);
  } catch (e) { toast('Could not open the chat: ' + e.message, 'err'); }
}
function chatNew() {
  const sc = chatScope(), c = getChat(sc.key);
  if (c.job) return;
  c.thread = null; c.msgs = []; c.rev += 1; c.err = null; c.lastQ = null; c.threads = null; chatRefresh(sc.key);
}
async function chatSend(text, again) {
  const sc = chatScope(), c = getChat(sc.key);
  if (c.job) { toast('Wait for the current answer.', 'err'); return; }
  c.err = null; c.lastQ = text; c.scope = sc;
  if (!again) c.msgs.push({ role: 'user', text, t: new Date().toISOString(), mode: 'question' });
  const job = { id: null, start: Date.now(), timer: null }; c.job = job;
  chatRefresh(sc.key);
  try {
    const r = await ctx.api.post('chat/send', { thread: c.thread, scope: { type: sc.type, agent_id: sc.agent_id, turn: sc.turn }, text });
    if (c.job !== job) return;
    c.thread = r.thread || c.thread; job.id = r.job; chatPoll(sc.key, job);
  } catch (e) { if (c.job === job) { c.job = null; c.err = e.message; chatRefresh(sc.key); } }
}
function chatRetry(key) { const c = getChat(key); if (c.lastQ) chatSend(c.lastQ, true); }
function chatPoll(key, job) {
  const c = chats.get(key);
  job.timer = setTimeout(async () => {
    if (!c || c.job !== job) return;
    if (Date.now() - job.start > CHAT_GIVEUP_MS) { c.job = null; c.err = 'timed out'; chatRefresh(key); return; }
    try {
      const r = await ctx.api.get('chat/job/' + encodeURIComponent(job.id), 10000);
      if (c.job !== job) return;
      if (r.state === 'ok') {
        c.job = null; c.thread = r.thread || c.thread; c.threads = null;
        c.msgs.push({ role: 'ai', text: r.reply == null ? '' : String(r.reply), t: new Date().toISOString(), cost_usd: r.cost_usd, mode: 'question' });
        if (!chatVisible(key)) toast('The AI answered your question (Chat tab).', 'ok');
      } else if (r.state === 'error') { c.job = null; c.threads = null; c.err = r.error || 'error while answering'; }
      else { chatPoll(key, job); chatRefresh(key); return; }
      chatRefresh(key);
    } catch (e) {
      if (c.job !== job) return;
      if (e.status === 404) { c.job = null; c.err = 'the chat was not found on the server'; chatRefresh(key); } else chatPoll(key, job);
    }
  }, CHAT_POLL_MS);
}
function takeToCurrent(c, m) {
  const i = c.msgs.indexOf(m);
  let q = ''; for (let k = i - 1; k >= 0; k -= 1) if (c.msgs[k].role === 'user') { q = c.msgs[k].text; break; }
  if (ui.attach.length >= 5) { toast('At most 5 attached items.', 'err'); return; }
  const sc = chatScope();
  ui.attach.push({ id: Date.now() + '-' + ui.attach.length, label: q || 'answer', text: `Context from turn ${sc.v.n} (${sc.label}). Question: ${q}\nAnswer: ${m.text}`.slice(0, 2000) });
  toast('Attached: it goes along with the next instruction to the loop.', 'ok'); renderComposer();
}

// unified composer (instruct the loop | act | ask | read-only old version)
function autoFeature() {
  const a = selAgentId() ? agentById(selAgentId()) : null;
  if (a && a.feature) return a.feature;
  return ui.sel && ui.sel.kind === 'feature' ? ui.sel.id : '';
}
// instruct/act only exist on the orchestrator of the CURRENT version; everything else is chat only (the other options do not even show)
export function canAct() {
  if (verInfo().old) return false;
  const id = selAgentId();
  if (!id) return true; // no agent: the inspector shows the current orchestrator
  const a = agentById(id);
  return !!a && a.role === 'orchestrator';
}
export function renderComposer() {
  if (!ctx.state) return;
  const v = verInfo(), act = canAct(), mode = act ? cp.mode : 'ask', st = status();
  const selKey = ui.sel ? ui.sel.kind + ':' + (ui.sel.id != null ? ui.sel.id : ui.sel.n) : '';
  if (selKey !== cp.selKey) { cp.selKey = selKey; cp.manualFeat = false; }
  const auto = autoFeature(), featVal = cp.manualFeat ? cp.feat : auto;
  const sc = chatScope(), asking = !!getChat(sc.key).job;
  const fs = features().map((f) => f.id);
  const who = sc.agent ? txt(sc.agent.role) : 'project';
  region('composer', { act, n: v.n, mode, who, att: act && mode === 'instruct' ? ui.attach.map((a) => a.id) : null, featVal, fs, st, sending: cp.sending, status: cp.status, asking, last: lastN(), ag: act && mode === 'act' ? actSignature() : null }, () => {
    $('composer').className = 'composer m-' + (act ? mode : 'chat');
    fill($('cp-band'), act ? [h('strong', { text: 'CURRENT VERSION' }), h('span', { text: ` · turn ${num(v.n)}` })]
      : [h('span', { class: 'cp-who', text: `Chat with ${who} · turn ${num(v.n)}` })]);
    const segBtn = (key, label) => h('button', { type: 'button', role: 'tab', class: 'cp-seg-btn', id: 'cp-seg-' + key, 'aria-selected': String(mode === key), tabindex: mode === key ? '0' : '-1',
      onclick: () => { cp.mode = key; cp.status = null; renderComposer(); $('cp-text').focus(); if (key === 'act') loadActions(); } }, label);
    $('cp-seg').hidden = !act;
    fill($('cp-seg'), act ? [segBtn('instruct', 'Instruct the loop'), segBtn('act', 'Act'), segBtn('ask', 'Ask')] : []);
    $('cp-why').hidden = true;
    $('cp-feat').hidden = !(act && mode === 'instruct');
    const actEl = $('cp-act'); actEl.hidden = !(act && mode === 'act'); if (act && mode === 'act') fill(actEl, actPanel()); else fill(actEl);
    const sel = $('cp-feature');
    sel.replaceChildren(h('option', { value: '', text: '(none)' }), ...fs.map((id) => h('option', { value: id, text: id })));
    sel.value = fs.includes(featVal) ? featVal : '';
    const showTray = act && mode === 'instruct' && ui.attach.length;
    fill($('cp-tray'), showTray ? [h('span', { class: 'cp-tray-l', text: 'attached context:' }), ui.attach.map((a) =>
      h('span', { class: 'cp-chip', title: a.text }, h('span', { class: 'cp-chip-t', text: a.label.length > 40 ? a.label.slice(0, 40) + '…' : a.label }),
        h('button', { type: 'button', class: 'cp-chip-x', 'aria-label': 'remove attachment', onclick: () => { ui.attach = ui.attach.filter((x) => x.id !== a.id); renderComposer(); } }, '×')))] : []);
    $('cp-tray').hidden = !showTray;
    const ta = $('cp-text');
    ta.placeholder = !act ? `Ask about ${who}…` : mode === 'act' ? 'e.g. open the PR to the integration branch and push' : mode === 'instruct' ? 'Instruct the loop: the next turn applies it…' : 'Ask the AI (it only reads the project)…';
    const send = $('cp-send'); send.textContent = !act || mode === 'ask' ? 'Ask' : mode === 'act' ? 'Propose action' : 'Instruct the loop';
    send.disabled = cp.sending || (mode === 'ask' && asking) || (mode === 'act' && !!actState.card && (actState.card.kind === 'proposing' || actState.card.kind === 'running'));
    setText($('cp-hint'), '');
    const cs = $('cp-status'); cs.replaceChildren(); cs.hidden = !(act && cp.status);
    if (act && cp.status) { cs.className = 'cp-status ' + cp.status.kind; cs.append(h('span', { text: cp.status.text }), cp.status.openNew ? h('button', { type: 'button', class: 'btn btn-sm', onclick: openNewLoop }, 'Open New loop') : ''); }
  });
}
async function composerSubmit(ev) {
  ev.preventDefault();
  const ta = $('cp-text'), text = ta.value.trim();
  if (!text) return;
  const mode = canAct() ? cp.mode : 'ask';
  if (mode === 'ask') { ta.value = ''; ui.tab = 'chat'; renderInspector(); chatSend(text); return; }
  if (mode === 'act') { if (actState.card && (actState.card.kind === 'proposing' || actState.card.kind === 'running')) return; ta.value = ''; proposeAction(text); return; }
  if (cp.sending) return;
  cp.sending = true; cp.status = null; renderComposer();
  try {
    const feature = $('cp-feature').value || null;
    const r = await ctx.api.post('instruction', { text, feature, attachments: ui.attach.slice(0, 5).map((a) => a.text.slice(0, 2000)) });
    const msg = INSTRUCTION_MESSAGES[r.action] || 'Instruction sent.';
    ta.value = ''; ui.attach = []; cp.status = { kind: 'ok', text: msg }; toast(msg, 'ok'); schedulePoll(800);
  } catch (e) {
    cp.status = { kind: 'err', text: e.message, openNew: e.status === 409 && (status() === 'no_loop' || /new loop/i.test(e.message)) };
  } finally { cp.sending = false; renderComposer(); }
}
export function initComposer() {
  $('composer').addEventListener('submit', composerSubmit);
  $('cp-text').addEventListener('keydown', (e) => { if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); $('composer').requestSubmit(); } });
  $('cp-feature').addEventListener('change', (e) => { cp.manualFeat = true; cp.feat = e.target.value; });
  setInterval(() => { if (chEl.root && chEl.root.isConnected) tickThink(); }, 1000);
}
