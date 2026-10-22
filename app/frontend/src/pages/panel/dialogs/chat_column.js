/* Chat column of the big dialogs (pause, agent, error): greeting, messages, "thinking…", suggestion chips and the input.
   Questions go to `POST chat/send` and the answer is followed with `GET chat/job/<job>` (read-only AI, same safety as the Chat tab).
   The conversation of each scope lives in a module map, so closing and reopening the dialog continues the same thread, and an
   answer that arrives while the dialog is closed is still there on reopening. Errors are shown inline, never with alert(). */
import { html, useState, useEffect, useLayoutEffect, useRef } from '../../../util/preact.js';
import { followJob } from '../../../api/api_client.js';
import { Markdown } from '../../../components/markdown.js';
import { list } from '../../../util/format.js';
import { ctx } from '../context.js';

const CHAT_POLL_INTERVAL_MS = 1500;
const CHAT_GIVE_UP_MS = 200000;

// scope key (JSON of the scope) -> { thread, messages, pending, error, lastText, listeners }
const conversations = new Map();

function conversationFor(scopeKey) {
  let conversation = conversations.get(scopeKey);
  if (!conversation) {
    conversation = { thread: null, messages: [], pending: null, error: null, lastText: null, listeners: new Set() };
    conversations.set(scopeKey, conversation);
  }
  return conversation;
}

const notifyListeners = (conversation) => conversation.listeners.forEach((listener) => listener());

/** Sends one question and waits for the answer. Lives at module level so it survives the dialog being closed meanwhile. */
async function sendQuestion(conversation, scope, text, isRetry) {
  if (conversation.pending) return;
  conversation.error = null;
  conversation.lastText = text;
  if (!isRetry) conversation.messages.push({ role: 'user', text });
  conversation.pending = { startedAt: Date.now() };
  notifyListeners(conversation);
  try {
    const sent = await ctx.api.post('chat/send', { thread: conversation.thread, scope, text });
    conversation.thread = sent.thread || conversation.thread;
    const finished = await followJob(ctx.api, 'chat/job/' + encodeURIComponent(sent.job), { intervalMs: CHAT_POLL_INTERVAL_MS, giveUpMs: CHAT_GIVE_UP_MS }).promise;
    if (finished.state === 'ok') {
      conversation.thread = finished.thread || conversation.thread;
      conversation.messages.push({ role: 'ai', text: finished.reply == null ? '' : String(finished.reply) });
    } else {
      conversation.error = finished.error || 'the AI could not answer';
    }
  } catch (error) {
    conversation.error = error.message;
  } finally {
    conversation.pending = null;
    notifyListeners(conversation);
  }
}

function ChatMessage({ message, onUse }) {
  const isAi = message.role === 'ai';
  return html`<div class=${'bd-msg ' + (isAi ? 'bd-msg-ai' : 'bd-msg-user')}>
    ${isAi ? html`<div class="bd-msg-text md"><${Markdown} source=${message.text} /></div>` : html`<div class="bd-msg-text">${message.text}</div>`}
    ${isAi && onUse ? html`<button type="button" class="bd-link bd-msg-use" onClick=${() => onUse(message.text)}>↳ use as guidance</button>` : null}
  </div>`;
}

/**
 * chat = { title, placeholder, scope, greeting, suggestions, onUse?(text) }.
 * The parent keys this component by the scope, so another scope starts from its own conversation.
 */
export function ChatColumn({ chat }) {
  const scopeKey = JSON.stringify(chat.scope);
  const conversation = conversationFor(scopeKey);
  const [, refresh] = useState(0);
  const listRef = useRef(null), inputRef = useRef(null);

  useEffect(() => {
    const listener = () => refresh((count) => count + 1);
    conversation.listeners.add(listener);
    return () => conversation.listeners.delete(listener);
  }, [conversation]);

  const messageCount = conversation.messages.length, isPending = !!conversation.pending;
  useLayoutEffect(() => {
    const element = listRef.current;
    if (element) element.scrollTop = element.scrollHeight;
  }, [messageCount, isPending, conversation.error]);

  const ask = (text) => {
    const question = String(text == null ? '' : text).trim();
    if (!question || conversation.pending) return false;
    sendQuestion(conversation, chat.scope, question, false);
    return true;
  };
  const sendFromInput = () => {
    const input = inputRef.current;
    if (input && ask(input.value)) input.value = '';
  };
  const onInputKeyDown = (event) => {
    if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) { event.preventDefault(); sendFromInput(); }
  };

  return html`<aside class="bd-chat" aria-label="Chat">
    <div class="bd-chat-head">
      <div class="bd-label">CHAT</div>
      <div class="bd-chat-title">${chat.title}</div>
    </div>
    <div class="bd-chat-list" role="log" aria-live="polite" ref=${listRef}>
      ${chat.greeting ? html`<${ChatMessage} message=${{ role: 'ai', text: chat.greeting }} onUse=${null} />` : null}
      ${conversation.messages.map((message, index) => html`<${ChatMessage} key=${index} message=${message} onUse=${chat.onUse} />`)}
      ${isPending ? html`<div class="bd-thinking" role="status">thinking…</div>` : null}
      ${conversation.error ? html`<div class="bd-chat-error" role="alert"><span>${conversation.error}</span>
        ${conversation.lastText ? html`<button type="button" class="bd-link" disabled=${isPending} onClick=${() => sendQuestion(conversation, chat.scope, conversation.lastText, true)}>try again</button>` : null}</div>` : null}
    </div>
    <div class="bd-chips">${list(chat.suggestions).map((suggestion) => html`<button key=${suggestion} type="button" class="bd-chip" disabled=${isPending} onClick=${() => ask(suggestion)}>${suggestion}</button>`)}</div>
    <div class="bd-chat-input">
      <input type="text" ref=${inputRef} class="bd-input" placeholder=${chat.placeholder || 'Ask the AI (read-only)…'} aria-label="Question for the AI" onKeyDown=${onInputKeyDown} />
      <button type="button" class="bd-button bd-button-primary" disabled=${isPending} onClick=${sendFromInput}>Send</button>
    </div>
  </aside>`;
}
