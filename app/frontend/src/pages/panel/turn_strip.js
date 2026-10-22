/* Turn strip of the LOOP zone: the LOOP badge, the button that shows or hides the GLOBAL zone, "go to latest" and one card per
   turn (T<n>, label, dot = worst status of its events, "<k> steps", cost). Each turn is a snapshot of the loop: picking one
   makes the whole LOOP zone show the state accumulated up to that turn. Data and selection come from pipeline_model.js. */
import { html, useEffect, useRef } from '../../util/preact.js';
import { isNum, cost } from '../../util/format.js';
import { pipe, turnsList, liveN, curN, selectTurn } from './pipeline_model.js';
import { toggleGlobalZone, globalZoneHidden, globalToggleArrow } from './layout.js';
import { ClaudeBadge, describeClaude } from './claude_badge.js';

const FALLBACK_DOT = '#4a4f57';

/** Number of steps of a turn: `changes` when the model gives a number, else the events it has. */
export function stepsInTurn(turn) {
  if (isNum(turn.changes)) return turn.changes;
  return Array.isArray(turn.events) ? turn.events.length : null;
}
const stepsText = (turn) => { const k = stepsInTurn(turn); return k == null ? '' : `${k} step${k === 1 ? '' : 's'}`; };

function TurnCard({ turn, on, isLive }) {
  const claudeTip = (describeClaude(turn) || {}).title;
  const tip = `Turn ${turn.n}${isLive ? ' (latest)' : ''} · ${turn.label || turn.status || ''}${claudeTip ? '
' + claudeTip : ''}`;
  return html`<button type="button" class=${'z-turn' + (on ? ' is-on' : '') + (isLive ? ' is-live' : '')} title=${tip} aria-pressed=${String(on)}
    onClick=${() => selectTurn(isLive ? null : turn.n)}>
    <span class="z-turn-top"><span class="z-turn-n mono">${'T' + turn.n}</span><span class="z-turn-dot" style=${{ background: turn.dot || FALLBACK_DOT }}></span></span>
    <span class="z-turn-label">${turn.label || turn.status || '—'}</span>
    <span class="z-turn-foot mono"><span>${stepsText(turn)}</span><span>${isNum(turn.cost_usd) ? cost(turn.cost_usd) : ''}</span></span>
    <span class="z-turn-claude"><${ClaudeBadge} source=${turn} /></span>
  </button>`;
}

/** The whole strip (the caller shows it only when the panel is in loop mode). */
export function TurnStrip() {
  const turns = turnsList(), latest = liveN(), current = curN();
  const cards = useRef(null);
  // a new turn scrolls the strip to the end so the latest card stays visible
  useEffect(() => { if (cards.current) cards.current.scrollLeft = cards.current.scrollWidth; }, [turns.length]);
  return html`<section class="z-strip" aria-label="Loop turns">
    <div class="z-strip-top">
      <button type="button" class="z-global-toggle" title=${globalZoneHidden() ? 'Show the global context' : 'Hide the global context'}
        aria-label="Show or hide the global context" aria-expanded=${String(!globalZoneHidden())} aria-controls="global-zone" onClick=${toggleGlobalZone}>${globalToggleArrow()}</button>
      <span class="z-badge z-badge-loop">LOOP</span>
      <span class="z-head-note">each turn shows the state accumulated up to it</span>
      <span class="z-spacer"></span>
      <button type="button" class="z-btn-mono" disabled=${pipe.sel == null} title="back to the latest turn (live)" onClick=${() => selectTurn(null)}>● go to latest</button>
    </div>
    <div class="z-turns" ref=${cards}>${turns.length
      ? turns.map((turn) => html`<${TurnCard} key=${turn.n} turn=${turn} on=${turn.n === current} isLive=${turn.n === latest} />`)
      : html`<span class="empty-note">${pipe.loading ? 'Reading the turns…' : 'no turn recorded yet'}</span>`}</div>
  </section>`;
}
