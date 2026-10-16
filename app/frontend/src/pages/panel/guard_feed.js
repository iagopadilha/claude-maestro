/* Guard feed (right column, under the inspector): the latest tool calls the PreToolUse guard blocked (state.guard_events,
   newest first) and the per-rule counts (state.guard_counts), plus the header badge. Data only from /state. */
import { $, h, fill, setAttr } from '../../util/dom.js';
import { DASH, list } from '../../util/format.js';
import { readStored, writeStored } from '../../util/storage.js';
import { ctx, region, sigs } from './context.js';

const OPEN_KEY = 'harness.guardFeed.open';
const TIME_PATTERN = /T?(\d{2}:\d{2}:\d{2})/;

const events = () => list(ctx.state && ctx.state.guard_events);
const counts = () => (ctx.state && ctx.state.guard_counts && typeof ctx.state.guard_counts === 'object' ? ctx.state.guard_counts : {});
const totalBlocks = () => Object.values(counts()).reduce((sum, n) => sum + (Number.isFinite(n) ? n : 0), 0);

function isOpen() {
  const stored = readStored(OPEN_KEY, null);
  return typeof stored === 'boolean' ? stored : events().length > 0;
}
function setOpen(open) { writeStored(OPEN_KEY, open); sigs['guard-feed'] = null; renderGuardFeed(); }

function clockOf(t) {
  const found = TIME_PATTERN.exec(String(t || ''));
  return found ? found[1] : DASH;
}

function eventRow(e) {
  return h('li', { class: 'gf-item' },
    h('div', { class: 'gf-top' },
      h('span', { class: 'gf-time mono', text: clockOf(e.t) }),
      e.profile ? h('span', { class: 'gf-profile mono', text: String(e.profile) }) : null,
      h('span', { class: 'gf-rule mono', text: String(e.rule || 'unknown') }),
      h('span', { class: 'gf-exit mono', text: 'exit 2' })),
    e.detail ? h('div', { class: 'gf-detail', title: String(e.detail), text: String(e.detail) }) : null);
}

/** Header badge: number of blocks; hidden without blocks. Clicking opens the feed and scrolls to it. */
function renderBadge(total) {
  const badge = $('guard-badge');
  if (!badge) return;
  badge.hidden = total === 0;
  if (!total) return;
  fill(badge, h('span', { class: 'gb-dot' }), `guard · ${total} blocked`);
  setAttr(badge, 'title', 'tool calls the PreToolUse guard blocked (exit 2): click to open the feed');
  badge.onclick = () => { setOpen(true); const feed = $('guard-feed'); if (feed) feed.scrollIntoView({ block: 'nearest' }); };
}

export function renderGuardFeed() {
  const feed = $('guard-feed');
  if (!feed || !ctx.state) return;
  const evs = events(), byRule = counts(), total = totalBlocks(), open = isOpen();
  renderBadge(total);
  region('guard-feed', { evs, byRule, open }, () => {
    feed.classList.toggle('has-blocks', total > 0);
    const head = h('button', { type: 'button', class: 'gf-head', 'aria-expanded': String(open), 'aria-controls': 'guard-feed-body', onclick: () => setOpen(!open) },
      h('span', { class: 'gf-chev', text: open ? '▾' : '▸' }),
      h('span', { class: 'gf-title', text: 'Guard · PreToolUse' }),
      h('span', { class: 'gf-count mono' + (total ? ' on' : ''), text: total ? `${total} blocked` : 'no blocks' }));
    const rules = Object.entries(byRule).sort((a, b) => b[1] - a[1]);
    const body = !open ? null : h('div', { class: 'gf-body', id: 'guard-feed-body' },
      rules.length ? h('div', { class: 'gf-rules' }, rules.map(([rule, n]) => h('span', { class: 'gf-rule-chip mono', text: `${rule} ${n}` }))) : null,
      evs.length
        ? h('ul', { class: 'gf-list', 'aria-label': 'Latest blocks, newest first' }, evs.map(eventRow))
        : h('div', { class: 'empty-note', text: 'The guard has not blocked any tool call in this project.' }));
    fill(feed, head, body);
  });
}
