/* Footer: timeline of the turns; clicking one opens the snapshot of that turn (read-only). */
import { $ } from '../../util/dom.js';
import { html, useEffect } from '../../util/preact.js';
import { DASH, isNum, cost, dur, list, featLabel } from '../../util/format.js';
import { toast } from '../../components/toast.js';
import { ctx, ui, TURN_COLOR, lastN } from './context.js';
import { renderAll } from '../project_panel.js';
import { loadHist } from './history.js';

function TurnButton({ v, isLast, on, maxC }) {
  const barH = isNum(v.cost_usd) && maxC > 0 ? Math.max(10, (v.cost_usd / maxC) * 100) : 10;
  const opacity = !isNum(v.cost_usd) ? '0.35' : on ? '1' : '0.75';
  const tip = `turn ${v.n}${isLast ? ' (latest · live)' : ''} · ${cost(v.cost_usd)} · ${dur(v.duration_s)} · ${v.status || DASH}${featLabel(v.feature) ? ' · ' + featLabel(v.feature) : ''}${isNum(v.commits) ? ' · ' + v.commits + ' commit(s)' : ''}`;
  return html`<button type="button" class=${'v' + (on ? ' on' : '') + (isLast ? ' last' : '')} title=${tip} aria-label=${tip}
      aria-pressed=${on ? 'true' : 'false'} onClick=${() => pickTurn(v)}>
    <span class="v-bar"><div style=${{ height: barH + '%', background: TURN_COLOR[v.status] || '#4A4F57', opacity }} /></span>
    <span class="v-lab">${String(v.n) + (isLast ? ' ●' : '')}</span>
  </button>`;
}

/** Content of #timeline: one button per turn (bar height = relative cost). */
export function Timeline() {
  const vs = list(ctx.state && ctx.state.turns), last = lastN();
  // a new turn scrolls the strip to the end (the latest turn stays visible)
  useEffect(() => { const t = $('timeline'); if (t) t.scrollLeft = t.scrollWidth; }, [vs.length]);
  if (!vs.length) return html`<span class="empty-note">no turn recorded yet</span>`;
  const maxC = Math.max(0, ...vs.map((v) => (isNum(v.cost_usd) ? v.cost_usd : 0)));
  const shown = ui.loadingN != null ? ui.loadingN : ui.pastN;
  return vs.map((v) => {
    const isLast = v.n === last;
    return html`<${TurnButton} key=${v.n} v=${v} isLast=${isLast} on=${shown != null ? shown === v.n : isLast} maxC=${maxC} />`;
  });
}

// Switching turns without a blank screen: the current content stays until the new data arrives;
// answers to old clicks are dropped (pickSeq) and snapshots already read stay in a cache.
const turnCache = new Map();
let pickSeq = 0;
function commitPast(r) {
  ui.past = { n: r.n, turnLog: r.turnLog || {}, roadmap: r.roadmap, features: list(r.features) };
  ui.pastN = r.n; ui.loadingN = null; ui.sel = null;
  renderAll();
  const state = ctx.state;
  if (ui.hist.key !== String(state && state.meta ? state.meta.turns : null)) loadHist();
}
export async function pickTurn(v) {
  if (v.n === lastN()) { backToLive(); return; }
  if (v.has_snapshot === false) { toast(`Turn ${v.n} has no snapshot.`, 'err'); return; }
  if (ui.pastN === v.n) { pickSeq += 1; ui.loadingN = null; renderAll(); return; }
  if (ui.pastN == null) ui.pinLast = lastN();
  const seq = ++pickSeq;
  if (turnCache.has(v.n)) { commitPast(turnCache.get(v.n)); return; }
  ui.loadingN = v.n; renderAll();
  try {
    const r = await ctx.api.get('turn/' + encodeURIComponent(v.n));
    if (seq !== pickSeq) return;
    const snap = { n: r.n, turnLog: r.turn_log, roadmap: r.roadmap, features: r.features };
    turnCache.set(v.n, snap); commitPast(snap);
  } catch (e) {
    if (seq !== pickSeq) return;
    toast(e.status === 404 ? `Turn ${v.n} has no snapshot.` : 'Could not open the turn: ' + e.message, 'err');
    ui.loadingN = null; if (ui.pastN == null) ui.pinLast = null; renderAll();
  }
}
export function backToLive() { pickSeq += 1; ui.pastN = null; ui.past = null; ui.loadingN = null; ui.pinLast = null; ui.sel = null; renderAll(); }
