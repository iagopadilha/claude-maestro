/* List of AI decisions (presentational): grouped by turn, each one with its level, feature, doubt, choice, how to revert it and
   the two actions (Mark as reviewed / Ask for a change). The screen state and the API calls live in pages/panel/decisions.js. */
import { html } from '../util/preact.js';

export const EMPTY_DECISIONS_TEXT = 'No decision of the AI needs your review';
export const TURN_UNKNOWN_TEXT = 'Turn unknown';
const LEVEL_LABEL = { critical: 'CRITICAL', important: 'IMPORTANT' };

/** Groups items by turn: [{ turn: number|null, items }] with known turns ascending and "unknown" last. */
export function groupByTurn(items) {
  const groups = new Map();
  for (const item of items || []) {
    const key = Number.isFinite(item.turn) ? item.turn : null;
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(item);
  }
  return [...groups.entries()].sort(([a], [b]) => (a === null) - (b === null) || (a || 0) - (b || 0)).map(([turn, group]) => ({ turn, items: group }));
}

/** One decision: `busy` disables its buttons while a call runs; `renderFeature(slug)` may return a nicer label than the slug. */
function DecisionItem({ item, busy, onReview, onChange, renderFeature }) {
  const level = item.level === 'critical' ? 'critical' : 'important';
  return html`<li class=${'dl-item is-' + level + (item.reviewed ? ' is-reviewed' : '')}>
    <div class="dl-top">
      <span class=${'dl-level mono is-' + level}>${LEVEL_LABEL[level]}</span>
      <span class="dl-feature">${renderFeature ? renderFeature(item.slug) : item.slug}</span>
      ${item.reviewed ? html`<span class="dl-state mono">reviewed</span>` : null}
      ${item.change_requested ? html`<span class="dl-state is-change mono">change asked</span>` : null}
    </div>
    <dl class="dl-fields">
      <dt>Doubt</dt><dd>${item.doubt}</dd>
      <dt>Choice</dt><dd>${item.chose}</dd>
      <dt>How to revert</dt><dd>${item.revert}</dd>
    </dl>
    <div class="dl-actions">
      <button type="button" class="btn btn-sm" disabled=${busy || item.reviewed} onClick=${() => onReview(item)}>${item.reviewed ? 'Reviewed' : 'Mark as reviewed'}</button>
      <button type="button" class="btn btn-sm" disabled=${busy || item.change_requested} onClick=${() => onChange(item)}>${item.change_requested ? 'Change asked' : 'Ask for a change'}</button>
    </div>
  </li>`;
}

/** <DecisionList items unparsed busyIds onReview onChange renderFeature showTurnTitles emptyText /> */
export function DecisionList({ items = [], unparsed = [], busyIds = {}, onReview, onChange, renderFeature, showTurnTitles = true, emptyText = EMPTY_DECISIONS_TEXT }) {
  if (!items.length && !unparsed.length) return html`<div class="dl-empty">${emptyText}</div>`;
  return html`<div class="dl">
    ${groupByTurn(items).map((group) => html`<section key=${String(group.turn)} class="dl-group">
      ${showTurnTitles ? html`<h3 class="dl-turn mono">${group.turn == null ? TURN_UNKNOWN_TEXT : 'Turn ' + group.turn}</h3>` : null}
      <ul class="dl-list">${group.items.map((item) => html`<${DecisionItem} key=${item.id} item=${item} busy=${!!busyIds[item.id]}
        onReview=${onReview} onChange=${onChange} renderFeature=${renderFeature} />`)}</ul>
    </section>`)}
    ${unparsed.length ? html`<section class="dl-group dl-unparsed"><h3 class="dl-turn mono">Unparsed lines</h3>
      <p class="dl-hint">These lines of VALIDATE.md did not match the decision format; they are shown as written.</p>
      <ul class="dl-list">${unparsed.map((line) => html`<li key=${line.line_no} class="dl-item dl-raw"><span class="dl-raw-no mono">${'line ' + line.line_no}</span><code class="dl-raw-text">${line.raw}</code></li>`)}</ul></section>` : null}
  </div>`;
}
