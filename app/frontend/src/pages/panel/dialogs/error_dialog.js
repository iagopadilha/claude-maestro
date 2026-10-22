/* Error dialog: one item of `GET errors` with what happened, the turns it happened in (chips that select the turn), what the user can do, the recovery buttons the Errors list already had,
   and a chat to ask the AI about it (scope `error`, the server builds the context from the item and the events of its turn). */
import { html } from '../../../util/preact.js';
import { txt, list } from '../../../util/format.js';
import { confirmDialog } from '../../../components/dialog.js';
import { openBigDialog } from '../../../components/big_dialog.js';
import { ctx, ui } from '../context.js';
import { sendResume } from '../alert.js';
import { selectTurn } from '../pipeline_model.js';
import { openPauseDialog } from './pause_dialog.js';

const SEVERITY_COLOR = { error: '#f85149', warning: '#f5b82e', info: '#7fb2ff' };
const KIND_LABEL = { loop: 'loop stopped', turn: 'turn', guard: 'guard', feature: 'feature', launch: 'launch' };
const RECOVER_LABEL = { retry: 'Try again', commit_leftovers: 'Commit leftovers and try again', discard_leftovers: 'Discard leftovers and try again' };
const RECOVER_PRIMARY = ['retry', 'commit_leftovers'];
const FEATURE_ITEM_ID = /^feature-(.+)$/;
const MAX_TURN_CHIPS = 30;
const GREETING = 'I read this error and the events of its turn. Ask what caused it, how to fix it, or whether it is safe to try again.';
const SUGGESTIONS = ['What caused this error?', 'How do I fix it?', 'Is it safe to retry?'];

/** Slug of the quarantined feature behind an item of kind feature (`feature-<slug>`), or null. */
const quarantinedSlug = (item) => {
  if (item.kind !== 'feature') return null;
  const match = FEATURE_ITEM_ID.exec(String(item.id || ''));
  return match ? match[1] : null;
};

/** Turns as ranges: [3,4,5,6,9] -> "T3–T6, T9" (a run of two stays "T3, T4"). */
export function formatTurnRanges(turns) {
  const parts = [];
  for (let index = 0; index < turns.length;) {
    let end = index;
    while (end + 1 < turns.length && turns[end + 1] === turns[end] + 1) end += 1;
    if (end - index >= 2) parts.push('T' + turns[index] + '–T' + turns[end]);
    else for (let step = index; step <= end; step += 1) parts.push('T' + turns[step]);
    index = end + 1;
  }
  return parts.join(', ');
}

/** The TURN fact: one clickable chip per turn (selects it in the loop zone and closes the dialog), the count of events under it when the item has them. */
function TurnsFact({ item, closeDialog }) {
  const turns = list(item.turns).filter((number) => Number.isInteger(number)).sort((a, b) => a - b);
  if (!turns.length) return '—';
  const counts = item.turn_counts || {};
  const shown = turns.slice(-MAX_TURN_CHIPS);
  return html`<div class="bd-turn-chips">${shown.map((number) => html`<button key=${number} type="button" class="bd-turn-chip"
      title=${counts[number] ? counts[number] + ' x in turn ' + number + ' (click to select the turn)' : 'select turn ' + number}
      onClick=${() => { selectTurn(number); closeDialog(); }}>T${number}${counts[number] > 1 ? html`<span class="bd-turn-chip-count">×${counts[number]}</span>` : null}</button>`)}
    ${turns.length > shown.length ? html`<span class="bd-turn-range">+${turns.length - shown.length} earlier</span>` : null}</div>
    ${turns.length > 1 ? html`<div class="bd-turn-range">${formatTurnRanges(turns)}</div>` : null}`;
}

/** Same actions as the Errors list: resume the loop, settling the leftover files first (discard deletes work, so it asks). */
async function recoverFrom(code, closeDialog) {
  if (code === 'discard_leftovers') {
    const confirmed = await confirmDialog({ title: 'Discard the leftover files?', text: 'This deletes the files the loop left behind, then tries again. It cannot be undone.', yesText: 'Discard and try again', danger: true });
    if (!confirmed) return;
  }
  const leftovers = code === 'commit_leftovers' ? 'commit' : code === 'discard_leftovers' ? 'discard' : undefined;
  const startedAt = Date.now();
  await sendResume(RECOVER_LABEL[code].toLowerCase(), leftovers ? { leftovers } : {});
  if (ui.resuming && ui.resuming.since >= startedAt) closeDialog(); // sendResume reports its own failure with a toast and leaves the dialog open
}

function ErrorBody({ item, closeDialog }) {
  const recover = list(item.recover), isRunning = !!(ctx.state && ctx.state.loop && ctx.state.loop.running), isBusy = !!ui.busy.answer;
  const slug = quarantinedSlug(item), attempts = list(item.attempts).map((attempt) => txt(attempt));
  const facts = [
    { label: 'WHEN', value: item.when ? String(item.when).replace('T', ' ').slice(0, 16) : '—' },
    { label: list(item.turns).length > 1 ? 'TURNS' : 'TURN', value: html`<${TurnsFact} item=${item} closeDialog=${closeDialog} />`, wide: list(item.turns).length > 1 },
    { label: 'KIND', value: KIND_LABEL[item.kind] || txt(item.kind) },
    item.count > 1 ? { label: 'TIMES', value: String(item.count) } : null,
  ].filter(Boolean);
  return html`<div class="bd-error">
    <div class="bd-facts-grid">${facts.map((fact) => html`<div key=${fact.label} class="bd-fact" style=${fact.wide ? { gridColumn: '1 / -1' } : null}><div class="bd-label">${fact.label}</div><div class="bd-fact-value">${fact.value}</div></div>`)}</div>
    ${item.happened ? html`<div class="bd-hint bd-hint-blue"><div class="bd-label bd-label-blue">WHAT HAPPENED</div><div class="bd-hint-text">${item.happened}</div></div>` : null}
    ${item.hint ? html`<div class="bd-hint"><div class="bd-label bd-label-amber">WHAT YOU CAN DO</div><div class="bd-hint-text">${item.hint}</div></div>` : null}
    ${attempts.length ? html`<div class="bd-section-label">WHAT WAS TRIED (distinct commands)</div><pre class="bd-detail">${attempts.join(String.fromCharCode(10))}</pre>` : null}
    ${item.detail ? html`<div class="bd-section-label">DETAIL</div><pre class="bd-detail">${item.detail}</pre>` : null}
    ${slug ? html`<div class="bd-actions"><button type="button" class="bd-button bd-button-primary"
      onClick=${() => { closeDialog(); openPauseDialog(slug); }}>Understand and resolve</button></div>` : null}
    ${recover.length ? html`<div class="bd-section-label">RECOVER</div>
      ${isRunning ? html`<div class="bd-dim-note">The loop is running; the recovery buttons are available once it stops.</div>`
        : html`<div class="bd-actions">${recover.filter((code) => RECOVER_LABEL[code]).map((code) => html`<button key=${code} type="button"
            class=${'bd-button' + (RECOVER_PRIMARY.includes(code) ? ' bd-button-primary' : ' bd-button-danger')} disabled=${isBusy}
            onClick=${() => recoverFrom(code, closeDialog)}>${RECOVER_LABEL[code]}</button>`)}</div>`}` : null}
  </div>`;
}

/** Opens the dialog of one item of GET errors. */
export function openErrorDialog(item) {
  const color = SEVERITY_COLOR[item.severity] || SEVERITY_COLOR.info;
  let dialog = null;
  dialog = openBigDialog({
    badge: String(item.severity || 'info').toUpperCase(), badgeColor: color, title: txt(item.title),
    body: () => html`<${ErrorBody} item=${item} closeDialog=${() => dialog.close()} />`,
    chat: {
      title: 'Ask about this error', placeholder: 'Ask about this error…',
      scope: { type: 'error', error_id: item.id }, greeting: GREETING, suggestions: SUGGESTIONS,
    },
  });
  return dialog;
}
