/* AI decisions to review (DEC-05..07): the count, the dialog listing all of them, and the "Decisions" section of one turn.
   Data from GET decisions[?turn=N]; "Mark as reviewed" and "Ask for a change" are POST decisions/<id>/review|change. The
   engine file VALIDATE.md is never edited from here: the review state lives in the app. */
import { html, useState, useEffect, useCallback } from '../../util/preact.js';
import { toast } from '../../components/toast.js';
import { openDialog } from '../../components/dialog.js';
import { DecisionList, EMPTY_DECISIONS_TEXT } from '../../components/decision_list.js';
import { ctx } from './context.js';
import { renderAll, schedulePoll } from '../project_panel.js';
import { FeatureLabel } from './feature_label.js';

export const DECISIONS_EXPLANATION = 'Choices the AI made on its own while it worked (a doubt, what it chose and how to undo it). Mark the ones you agree with as reviewed, or ask for a change.';
const renderFeature = (slug) => html`<${FeatureLabel} slug=${slug} />`;

/** Open counts of the state payload: { critical, important } (0 when the server does not report them yet). */
export function decisionsCounts(state = ctx.state) {
  const summary = (state && state.decisions_summary) || {};
  return { critical: Number(summary.open_critical) || 0, important: Number(summary.open_important) || 0 };
}

/** Count of decisions waiting for a review; click opens the dialog. */
export function DecisionsCount({ project, state } = {}) {
  const { critical, important } = decisionsCounts(state || ctx.state);
  if (!critical && !important) return html`<span class="dc-none">No AI decision to review</span>`;
  return html`<button type="button" class="dc" onClick=${() => openDecisionsDialog({ project, state })} title="AI decisions to review">
    <span class="dc-n">AI decisions to review</span>
    ${critical ? html`<span class="dc-chip is-critical">${critical + ' critical'}</span>` : null}
    ${important ? html`<span class="dc-chip">${important + ' important'}</span>` : null}</button>`;
}

/** Loads the decisions (all, or of one turn) and exposes review/change actions that reload them. */
function useDecisions(turn) {
  const [data, setData] = useState(null);
  const [error, setError] = useState('');
  const [busyIds, setBusyIds] = useState({});
  const route = 'decisions' + (turn == null ? '' : '?turn=' + encodeURIComponent(turn));
  const load = useCallback(async () => {
    try { setData(await ctx.api.get(route, 15000)); setError(''); } catch (failure) { setError(failure.message); }
  }, [route]);
  useEffect(() => { setData(null); load(); }, [load]);
  const act = async (item, action, doneText) => {
    setBusyIds((current) => ({ ...current, [item.id]: true }));
    try {
      await ctx.api.post('decisions/' + encodeURIComponent(item.id) + '/' + action, {});
      toast(doneText, 'ok');
      await load(); schedulePoll(300); renderAll();
    } catch (failure) { toast('Not done: ' + failure.message, 'err'); } finally { setBusyIds((current) => ({ ...current, [item.id]: false })); }
  };
  return {
    data, error, busyIds, reload: load,
    review: (item) => act(item, 'review', 'Marked as reviewed.'),
    change: (item) => act(item, 'change', 'Added to the follow-up draft.'),
  };
}

function DecisionsBody({ turn, showTurnTitles }) {
  const decisions = useDecisions(turn);
  if (decisions.error && !decisions.data) return html`<div class="dl-error" role="alert">${'Could not read the decisions: ' + decisions.error}</div>`;
  if (!decisions.data) return html`<div class="dl-empty">Reading the decisions…</div>`;
  return html`<${DecisionList} items=${decisions.data.items || []} unparsed=${decisions.data.unparsed || []} busyIds=${decisions.busyIds}
    onReview=${decisions.review} onChange=${decisions.change} renderFeature=${renderFeature} showTurnTitles=${showTurnTitles} />`;
}

function DecisionsDialogContent() {
  return html`<p class="dl-explain">${DECISIONS_EXPLANATION}</p><${DecisionsBody} turn=${null} showTurnTitles=${true} />`;
}

/** Dialog "AI decisions to review": every decision grouped by turn, plus the unparsed lines as written. */
export function openDecisionsDialog() {
  openDialog({ title: 'AI decisions to review', width: 760, cls: 'dlg-decisions', content: html`<${DecisionsDialogContent} />`, actions: [{ text: 'Close' }] });
}

/** "Decisions" section of a turn (turn_tabs.js): only the decisions of that turn. */
export function TurnDecisions({ turn }) {
  return html`<section class="td" aria-label="Decisions of this turn">
    <div class="pl-eyebrow spaced">DECISIONS TO REVIEW</div>
    <p class="dl-explain">${DECISIONS_EXPLANATION}</p>
    <${DecisionsBody} key=${turn} turn=${turn} showTurnTitles=${false} /></section>`;
}

export { EMPTY_DECISIONS_TEXT };
