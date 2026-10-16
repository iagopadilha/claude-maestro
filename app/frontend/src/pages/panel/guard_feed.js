/* Guard feed (right column, under the inspector): the latest tool calls the PreToolUse guard blocked (state.guard_events,
   newest first) and the per-rule counts (state.guard_counts), plus the header badge. Data only from /state. */
import { $ } from '../../util/dom.js';
import { html } from '../../util/preact.js';
import { DASH, list } from '../../util/format.js';
import { readStored, writeStored } from '../../util/storage.js';
import { ctx } from './context.js';
import { renderAll } from '../project_panel.js';

const OPEN_KEY = 'harness.guardFeed.open';
const TIME_PATTERN = /T?(\d{2}:\d{2}:\d{2})/;

const events = () => list(ctx.state && ctx.state.guard_events);
const counts = () => (ctx.state && ctx.state.guard_counts && typeof ctx.state.guard_counts === 'object' ? ctx.state.guard_counts : {});
/** Total blocks: drives the header badge (hidden without blocks) and the has-blocks flag of #guard-feed. */
export const totalBlocks = () => Object.values(counts()).reduce((sum, n) => sum + (Number.isFinite(n) ? n : 0), 0);

function isOpen() {
  const stored = readStored(OPEN_KEY, null);
  return typeof stored === 'boolean' ? stored : events().length > 0;
}
function setOpen(open) { writeStored(OPEN_KEY, open); renderAll(); }

function clockOf(t) {
  const found = TIME_PATTERN.exec(String(t || ''));
  return found ? found[1] : DASH;
}

function EventRow({ e }) {
  return html`<li class="gf-item">
    <div class="gf-top">
      <span class="gf-time mono">${clockOf(e.t)}</span>
      ${e.profile ? html`<span class="gf-profile mono">${String(e.profile)}</span>` : null}
      <span class="gf-rule mono">${String(e.rule || 'unknown')}</span>
      <span class="gf-exit mono">exit 2</span>
    </div>
    ${e.detail ? html`<div class="gf-detail" title=${String(e.detail)}>${String(e.detail)}</div>` : null}
  </li>`;
}

export const GUARD_BADGE_TITLE = 'tool calls the PreToolUse guard blocked (exit 2): click to open the feed';
/** Content of #guard-badge. */
export function GuardBadge({ total }) {
  return html`<span class="gb-dot"></span>${`guard · ${total} blocked`}`;
}
/** Click on the header badge: opens the feed and scrolls to it. */
export function openGuardFeed() { setOpen(true); const feed = $('guard-feed'); if (feed) feed.scrollIntoView({ block: 'nearest' }); }

/** Content of #guard-feed: collapsible head, per-rule chips and the latest blocks. */
export function GuardFeed() {
  const evs = events(), byRule = counts(), total = totalBlocks(), open = isOpen();
  const rules = Object.entries(byRule).sort((a, b) => b[1] - a[1]);
  return html`
    <button type="button" class="gf-head" aria-expanded=${String(open)} aria-controls="guard-feed-body" onClick=${() => setOpen(!open)}>
      <span class="gf-chev">${open ? '▾' : '▸'}</span>
      <span class="gf-title">Guard · PreToolUse</span>
      <span class=${'gf-count mono' + (total ? ' on' : '')}>${total ? `${total} blocked` : 'no blocks'}</span>
    </button>
    ${open ? html`<div class="gf-body" id="guard-feed-body">
      ${rules.length ? html`<div class="gf-rules">${rules.map(([rule, n]) => html`<span key=${rule} class="gf-rule-chip mono">${`${rule} ${n}`}</span>`)}</div>` : null}
      ${evs.length
        ? html`<ul class="gf-list" aria-label="Latest blocks, newest first">${evs.map((e, i) => html`<${EventRow} key=${i} e=${e} />`)}</ul>`
        : html`<div class="empty-note">The guard has not blocked any tool call in this project.</div>`}
    </div>` : null}`;
}
