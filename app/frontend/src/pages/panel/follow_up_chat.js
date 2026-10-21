/* Follow-up chat of a finished loop (FUC-01..12): a new round of work on the same worktree and branch. The back end decides what is allowed
   (can_send, can_continue, caps); this module only shows it.
   Routes: GET follow-up {round, thread{messages, spent_usd, summary_note}, stage, last_job, caps, can_send, can_continue, finished, draft},
   POST follow-up/send {text, decisions}, POST follow-up/run {} (Continue loop). */
import { html, useState, useEffect, useRef } from '../../util/preact.js';
import { list, txt, cost, isNum } from '../../util/format.js';
import { toast } from '../../components/toast.js';
import { ctx } from './context.js';
import { renderAll, schedulePoll } from '../project_panel.js';

export const FOLLOW_UP_EXPLANATION = 'A new round of work on the SAME worktree and branch as the loop, before Finish: you ask for fixes or new things, the AI appends a new stage at the end of the ROADMAP, you review it and then run it.';

/** "Changed: +added, ~edited, -removed" or "No change" for an assistant message. */
function changeLine(changed) {
  const parts = [];
  const add = (mark, items) => { const names = list(items).map((item) => txt(typeof item === 'string' ? item : item && (item.slug || item.id || item.name))).filter(Boolean); if (names.length) parts.push(mark + names.join(`, ${mark}`)); };
  if (changed) { add('+', changed.added); add('~', changed.edited); add('-', changed.removed); }
  return parts.length ? 'Changed: ' + parts.join(', ') : 'No change';
}

function Message({ message }) {
  const mine = message.role === 'user';
  const failed = !mine && (message.state === 'error' || !!message.error);
  const decisions = list(message.decisions);
  return html`<li class=${'wu-msg ' + (mine ? 'is-user' : 'is-ai') + (failed ? ' is-error' : '')}>
    <div class="wu-msg-who">${mine ? 'You' : 'AI'}${!mine && isNum(message.cost_usd) ? html` <span class="mono">${cost(message.cost_usd)}</span>` : null}</div>
    ${message.text ? html`<div class="wu-msg-text">${message.text}</div>` : null}
    ${mine && decisions.length ? html`<div class="wu-msg-decisions mono">${'Change: ' + decisions.join(', ')}</div>` : null}
    ${failed ? html`<div class="wu-msg-error" role="alert"><strong>It failed: </strong>${txt(message.error) || 'unknown error'}</div>`
      : !mine && message.state !== 'running' ? html`<div class="wu-msg-changed mono">${changeLine(message.changed)}</div>` : null}
    ${!mine && message.state === 'running' ? html`<div class="nl-msg" role="status"><div class="spinner" aria-hidden="true"></div><span>The AI is writing the follow-up stage…</span></div>` : null}
  </li>`;
}

/** The stage appended to ROADMAP.md: name plus each feature with status, dependencies and paths. */
export function AddedStage({ stage }) {
  if (!stage) return null;
  if (typeof stage === 'string') return html`<div class="wu-stage"><div class="pl-eyebrow">ADDED TO THE ROADMAP</div><pre class="wu-stage-text">${stage}</pre></div>`;
  const features = list(stage.features);
  return html`<div class="wu-stage"><div class="pl-eyebrow">ADDED TO THE ROADMAP</div><div class="wu-stage-name">${txt(stage.name || stage.title || stage.stage)}</div>
    ${features.length ? html`<ul class="wu-stage-features">${features.map((feature, index) => {
      if (typeof feature === 'string') return html`<li key=${index} class="mono">${feature}</li>`;
      const deps = list(feature.deps), paths = list(feature.paths);
      return html`<li key=${index} class="wu-stage-feature"><div><span class="mono">${txt(feature.slug || feature.id)}</span>${feature.title ? html` <span>${feature.title}</span>` : null}${feature.status ? html` <span class="wu-stage-status">${feature.status}</span>` : null}</div>
        ${deps.length ? html`<div class="wu-stage-meta">${'depends on: '}<span class="mono">${deps.join(', ')}</span></div>` : null}
        ${paths.length ? html`<div class="wu-stage-meta">${'paths: '}<span class="mono">${paths.join(', ')}</span></div>` : null}</li>`;
    })}</ul>` : null}</div>`;
}

/** Chat + stage preview + Continue loop. `followUp` is the GET follow-up payload; `decisions` the decisions list; `locked` = the loop is running. */
export function FollowUpChat({ followUp, decisions, reload, locked }) {
  const data = followUp || {};
  const draft = data.draft || {};
  const thread = data.thread || {};
  const caps = data.caps || {};
  const messages = list(thread.messages);
  const [text, setText] = useState('');
  const [picked, setPicked] = useState([]);
  const [busy, setBusy] = useState(false);
  const touched = useRef(false);
  const logRef = useRef(null);
  const draftKey = followUp ? (draft.text || '') + '|' + list(draft.decisions).join(',') : null;
  useEffect(() => { if (followUp && !touched.current) { setText(draft.text || ''); setPicked(list(draft.decisions)); } }, [draftKey]);
  useEffect(() => { const log = logRef.current; if (log) log.scrollTop = log.scrollHeight; }, [messages.length]);

  const job = data.last_job, running = !!job && job.state === 'running';
  const choices = list(decisions).filter((item) => !item.reviewed || picked.includes(item.id) || item.change_requested);
  const toggle = (id) => { touched.current = true; setPicked((current) => (current.includes(id) ? current.filter((x) => x !== id) : [...current, id])); };
  const call = async (route, body, doneText) => {
    setBusy(true);
    try { await ctx.api.post(route, body, 30000); toast(doneText, 'ok'); if (route === 'follow-up/send') { touched.current = false; setText(''); setPicked([]); } await reload(); schedulePoll(300); renderAll(); }
    catch (failure) { toast((failure.status === 409 ? 'Not right now: ' : 'Not done: ') + failure.message, 'err'); await reload(); } finally { setBusy(false); }
  };

  const sendGate = data.can_send || {}, continueGate = data.can_continue || {};
  const sendReason = locked ? 'A loop is running.' : running ? 'The AI is working on the previous message.' : sendGate.ok === false ? txt(sendGate.reason) || 'Not available now.' : '';
  const continueReason = locked ? 'A loop is running.' : running ? 'The AI is still writing the stage.' : continueGate.ok === false ? txt(continueGate.reason) || 'Not available now.' : !data.stage ? 'Send a message first: it adds the stage to run.' : '';
  const inputOff = busy || !!sendReason;
  const canSend = !inputOff && !!(text.trim() || picked.length);
  const canContinue = !busy && !continueReason;
  const spent = isNum(thread.spent_usd) ? thread.spent_usd : 0;

  return html`<section class="wu-follow wu-chat" aria-label="Follow-up chat">
    <div class="z-label">FOLLOW-UP${isNum(data.round) ? html` <span class="mono">${'· round ' + data.round}</span>` : null}</div>
    <p class="wu-hint">${FOLLOW_UP_EXPLANATION}</p>
    ${thread.summary_note ? html`<div class="nl-box nl-warn wu-note" role="status">${thread.summary_note}</div>` : null}
    ${messages.length ? html`<ul class="wu-msgs" ref=${logRef} aria-label="Follow-up messages" aria-live="polite">${messages.map((message, index) => html`<${Message} key=${index} message=${message} />`)}</ul>` : null}
    <textarea class="dlg-area" rows="3" aria-label="Follow-up message" placeholder="E.g. rename the export button, and add a CSV option to the report."
      value=${text} disabled=${inputOff} onInput=${(e) => { touched.current = true; setText(e.currentTarget.value); }}></textarea>
    ${choices.length ? html`<div class="wu-chips" role="group" aria-label="Ask for a change"><span class="wu-chips-k">Ask for a change</span>${choices.map((item) => html`<button key=${item.id} type="button"
      class=${'wu-chip' + (picked.includes(item.id) ? ' is-on' : '')} aria-pressed=${picked.includes(item.id)} disabled=${inputOff} title=${txt(item.chose)}
      onClick=${() => toggle(item.id)}><span class="mono">${item.slug}</span> ${item.chose}</button>`)}</div>` : null}
    <div class="wu-follow-actions">
      <button type="button" class="btn btn-sm" disabled=${!canSend} title=${sendReason} onClick=${() => call('follow-up/send', { text: text.trim(), decisions: picked }, 'Message sent.')}>Send</button>
      <span class="wu-hint mono">${`US$ ${spent.toFixed(2)} of ${isNum(caps.round_usd) ? caps.round_usd : '—'} used in this round`}</span>
      ${sendReason ? html`<span class="wu-hint">${sendReason}</span>` : null}</div>
    <${AddedStage} stage=${data.stage} />
    <div class="wu-follow-actions">
      <button type="button" class="btn btn-sm btn-primary" disabled=${!canContinue} title=${continueReason} onClick=${() => call('follow-up/run', {}, 'Follow-up round started.')}>Continue loop</button>
      ${continueReason ? html`<span class="wu-hint">${continueReason}</span>` : null}</div>
  </section>`;
}
