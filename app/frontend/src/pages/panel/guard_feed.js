/* Guard feed: the tool calls the PreToolUse guard blocked (state.guard_events, newest first) and the per-rule counts
   (state.guard_counts). The header badge is the only entry point; clicking it opens the feed in a dialog.
   Data only from /state. */
import { html, useState } from '../../util/preact.js';
import { DASH, list } from '../../util/format.js';
import { openDialog } from '../../components/dialog.js';
import { ctx } from './context.js';

const TIME_PATTERN = /T?(\d{2}:\d{2}:\d{2})/;
const ALL_PROFILES = 'all';
const GROUP_BY_TIME = 'time';
const GROUP_BY_RULE = 'rule';
const UNKNOWN_RULE = 'unknown';
const UNKNOWN_PROFILE = 'unknown';

const events = () => list(ctx.state && ctx.state.guard_events);
const counts = () => (ctx.state && ctx.state.guard_counts && typeof ctx.state.guard_counts === 'object' ? ctx.state.guard_counts : {});
/** Total blocks: drives the header badge (hidden without blocks). */
export const totalBlocks = () => Object.values(counts()).reduce((sum, n) => sum + (Number.isFinite(n) ? n : 0), 0);

function clockOf(t) {
  const found = TIME_PATTERN.exec(String(t || ''));
  return found ? found[1] : DASH;
}
const ruleOf = (e) => String(e.rule || UNKNOWN_RULE);
const profileOf = (e) => (e.profile ? String(e.profile) : UNKNOWN_PROFILE);

// what each guard rule protects, in plain words (shown in the badge tooltip; an unknown rule shows only its name)
const RULE_REASON = {
  'specs-only': 'an agent tried to write outside .specs/ (planning and spec agents only write there; the codebase map goes to docs/codebase)',
  'state-write': 'an agent tried to write the loop state, which only the orchestrator and the engine own',
  'approval-write': 'something tried to write the roadmap approval, which only your click on Approve roadmap can write',
  'git-write': 'an agent tried a git write command (the engine owns commits)',
  'git-push': 'an agent tried git push (delivery is done by harness.py deliver, at the end)',
  'push-protected': 'an agent tried to push the protected branch',
  'pr-release': 'an agent tried to open a pull request or a release',
  'prod-write': 'an agent tried to write in production (never allowed)',
  secrets: 'an agent tried to read a credential file',
  'outbound-msg': 'an agent tried to send a message outside the machine',
  'panel-trigger': 'an agent tried to operate the panel itself',
};
/** Content of #guard-badge: the chip plus a styled tooltip (what the guard is, why it blocked per rule, that it is history). */
export function GuardBadge({ total }) {
  const rows = Object.entries(counts()).filter(([, n]) => n > 0).sort((x, y) => y[1] - x[1]).slice(0, 4);
  return html`<span class="gb-dot"></span>${`guard · ${total} blocked`}
    <span class="gb-tip" id="guard-tip" role="tooltip">
      <b class="gb-tip-t">Why something was blocked</b>
      <span class="gb-tip-p">The guard stops agent actions that break a project rule. The agent is told and tries another way: nothing to fix unless the same block keeps repeating.</span>
      <span class="gb-tip-l">${rows.map(([rule, n]) => html`<span class="gb-tip-r" key=${rule}><span class="gb-tip-n mono">${n}×</span><span><b class="mono">${rule}</b> ${RULE_REASON[rule] || 'open the feed for details'}</span></span>`)}</span>
      <span class="gb-tip-f">Click to open the feed</span>
    </span>`;
}

function EventRow({ e }) {
  return html`<li class="gf-item">
    <div class="gf-top">
      <span class="gf-time mono">${clockOf(e.t)}</span>
      <span class="gf-profile mono">${profileOf(e)}</span>
      <span class="gf-rule mono">${ruleOf(e)}</span>
      <span class="gf-exit mono">exit 2</span>
    </div>
    ${e.detail ? html`<div class="gf-detail">${String(e.detail)}</div>` : null}
  </li>`;
}

/** Groups the events by rule (rules ordered by how many blocks, events keep their newest-first order). */
function groupByRule(visible) {
  const groups = new Map();
  visible.forEach((e) => { const rule = ruleOf(e); if (!groups.has(rule)) groups.set(rule, []); groups.get(rule).push(e); });
  return [...groups.entries()].sort((a, b) => b[1].length - a[1].length);
}

/** Dialog body: warning, per-rule chips, profile filter, grouping toggle and the list. */
function GuardFeedBody() {
  const [profile, setProfile] = useState(ALL_PROFILES);
  const [groupBy, setGroupBy] = useState(GROUP_BY_TIME);
  const all = events(), byRule = counts();
  const profiles = [...new Set(all.map(profileOf))].sort();
  const visible = profile === ALL_PROFILES ? all : all.filter((e) => profileOf(e) === profile);
  const rules = Object.entries(byRule).filter(([, n]) => n > 0).sort((a, b) => b[1] - a[1]);
  const shownRules = rules.filter(([rule]) => profile === ALL_PROFILES || visible.some((e) => ruleOf(e) === rule));
  return html`<div class="gf-dialog">
    <p class="gf-note">A block means the guard worked: the agent was told and tries another way. Nothing to do here unless the same block keeps repeating.</p>
    ${shownRules.length ? html`<div class="gf-rules" aria-label="Blocks per rule">${shownRules.map(([rule, n]) => html`<span key=${rule} class="gf-rule-chip mono" title=${RULE_REASON[rule] || ''}>${`${rule} ${profile === ALL_PROFILES ? n : visible.filter((e) => ruleOf(e) === rule).length}`}</span>`)}</div>` : null}
    ${all.length ? html`<div class="gf-controls">
      <label class="gf-field"><span>agent profile</span>
        <select value=${profile} onChange=${(ev) => setProfile(ev.target.value)}>
          <option value=${ALL_PROFILES}>all profiles</option>
          ${profiles.map((p) => html`<option key=${p} value=${p}>${p}</option>`)}
        </select></label>
      <div class="gf-seg" role="group" aria-label="Group the list">
        <button type="button" class=${'btn btn-sm' + (groupBy === GROUP_BY_TIME ? ' btn-primary' : '')} aria-pressed=${String(groupBy === GROUP_BY_TIME)} onClick=${() => setGroupBy(GROUP_BY_TIME)}>by time</button>
        <button type="button" class=${'btn btn-sm' + (groupBy === GROUP_BY_RULE ? ' btn-primary' : '')} aria-pressed=${String(groupBy === GROUP_BY_RULE)} onClick=${() => setGroupBy(GROUP_BY_RULE)}>by rule</button>
      </div>
    </div>` : null}
    ${!all.length
      ? html`<div class="empty-note">The guard has not blocked any tool call in this project.</div>`
      : !visible.length
        ? html`<div class="empty-note">No blocks for this profile.</div>`
        : groupBy === GROUP_BY_RULE
          ? groupByRule(visible).map(([rule, group]) => html`<section key=${rule} class="gf-group">
              <h3 class="gf-group-title mono">${rule}<span class="gf-group-n">${group.length}</span></h3>
              ${RULE_REASON[rule] ? html`<p class="gf-group-why">${RULE_REASON[rule]}</p>` : null}
              <ul class="gf-list">${group.map((e, i) => html`<${EventRow} key=${i} e=${e} />`)}</ul>
            </section>`)
          : html`<ul class="gf-list" aria-label="Blocks, newest first">${visible.map((e, i) => html`<${EventRow} key=${i} e=${e} />`)}</ul>`}
  </div>`;
}

let openFeed = null;
/** Click on the header badge: opens the feed dialog (a second click while it is open does nothing). */
export function openGuardFeed() {
  if (openFeed) return;
  openFeed = openDialog({
    title: `Guard · PreToolUse · ${totalBlocks()} blocked`,
    content: html`<${GuardFeedBody} />`,
    width: 720,
    cls: 'gf-dlg',
    actions: [{ text: 'Close', onClick: (api) => api.close() }],
    onClose: () => { openFeed = null; },
  });
}
