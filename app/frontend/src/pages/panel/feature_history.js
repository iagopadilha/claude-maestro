/* Inspector, feature view: every agent call that worked on the feature, turn by turn (oldest first), with what each one returned and
   decided. A new iteration adds an entry; nothing is replaced. Past turns come from GET history, the running turn from the live state. */
import { html } from '../../util/preact.js';
import { tok, dur, txt, list } from '../../util/format.js';
import { ctx, ui, pal } from './context.js';
import { selectAgent } from './roadmap_sidebar.js';
import { retColor } from './turn_diagram.js';
import { openFeatureDialog } from './dialogs/index.js';
import { ClaudeBadge } from './claude_badge.js';

const sameFeature = (a, id) => String(a.feature || '').split(',').map((s) => s.trim()).includes(id);

/** [{ n, live, agents }] for the feature, oldest turn first. */
export function featureCalls(id) {
  const state = ctx.state || {}, running = !!(state.loop && state.loop.running), liveTurn = running && state.loop ? state.loop.turn : null;
  const turns = list(ui.hist.data).filter((t) => t.n !== liveTurn).map((t) => ({ n: t.n, live: false, agents: list(t.agents).filter((a) => sameFeature(a, id)) }));
  if (liveTurn != null) turns.push({ n: liveTurn, live: true, agents: list(state.agents).filter((a) => sameFeature(a, id)) });
  return turns.filter((t) => t.agents.length).sort((a, b) => a.n - b.n);
}

function Call({ a }) {
  const [color] = pal(a.state), rc = a.return_status ? retColor(a.return_status) : null, said = a.decision || a.summary || (a.last_action && a.last_action.text);
  return html`<button type="button" class="fh-call" title=${a.id} onClick=${() => selectAgent(a.id)}>
    <span class="fh-top"><span class="dot" style=${{ background: color }}></span><span class="arow-role">${txt(a.role)}</span>
      ${a.return_status ? html`<span class="rbadge" style=${{ color: rc, borderColor: rc }}>${String(a.return_status)}</span>` : null}
      <span class="arow-state" style=${{ color }}>${txt(a.state)}</span><span class="arow-meta">${`${tok(a.tokens)} tok · ${dur(a.duration_s)}`}</span><${ClaudeBadge} source=${a} inherited /></span>
    <span class="fh-said">${said ? String(said) : 'no decision recorded yet'}</span>
  </button>`;
}

export function FeatureHistory({ id }) {
  const turns = featureCalls(id), total = turns.reduce((n, t) => n + t.agents.length, 0);
  return html`<div class="iblock"><div class="small-label">${`Agent calls (${total})`}</div>
    <button type="button" class="z-btn-wide" onClick=${() => openFeatureDialog({ feature: id })}>Open feature view (steps, tasks, agents)</button>
    ${!turns.length ? html`<div class="empty-note">${ui.hist.loading ? 'loading…' : 'no agent has worked on this feature yet'}</div>` : turns.map((t) => html`<div key=${t.n} class="fh-turn">
      <div class="fh-head mono">${`turn ${t.n}${t.live ? ' · running' : ''} · ${t.agents.length} call${t.agents.length === 1 ? '' : 's'}`}</div>
      ${t.agents.map((a, i) => html`<${Call} key=${a.id || i} a=${a} />`)}</div>`)}
  </div>`;
}
