/* Inspector (right): agent (log, scope, chat), feature, stage, snapshot or summary. */
import { html } from '../../util/preact.js';
import { DASH, isNum, num, cost, dur, tok, txt, list } from '../../util/format.js';
import { ctx, ui, pal, ROLE_DESC, LOG_KIND, LIFECYCLE, FEATURE_STATUSES, live, features, agents, featById, agentById } from './context.js';
import { selectFeature, selectAgent, selectStage, openFeatureDetail } from './roadmap_sidebar.js';
import { retColor } from './turn_diagram.js';
import { depInfo, readiness } from './dependencies.js';
import { Chat } from './chat.js';
import { openFeatureSpec } from './files.js';
import { renderAll } from '../project_panel.js';

export async function fetchAgentLog(id) {
  const mine = () => ui.sel && ui.sel.kind === 'agent' && ui.sel.id === id;
  try {
    const r = await ctx.api.get('agent/' + encodeURIComponent(id) + '/log', 20000);
    if (mine()) { ui.agentLog = { id, log: list(r.log), truncated: !!r.truncated }; ui.logLoading = null; renderAll(); }
  } catch (e) {
    if (mine()) { ui.agentLog = { id, log: [], err: e.message }; ui.logLoading = null; renderAll(); }
  }
}
function LogEntry({ m, agentName }) {
  const k = LOG_KIND[m.type] || LOG_KIND.sys;
  const label = m.type === 'guard' ? `blocked by the guard${m.rule ? ' · ' + m.rule : ''} · exit 2` : k[0] || agentName;
  return html`<div class="msg" style=${{ background: k[2], border: '1px solid ' + k[3] }}>
    <div class="msg-h" style=${{ color: k[1] }}><span>${label}</span><span class="t">${txt(m.t)}</span></div>
    <div class=${'msg-b' + (k[4] ? ' m' : '')}>${txt(m.text)}</div>
    ${m.result ? html`<div class="msg-r">${String(m.result)}</div>` : null}
  </div>`;
}
export function IRow({ a, b, c, onClick }) {
  return html`<div class="irow">${onClick ? html`<button type="button" class="a" onClick=${onClick}>${a}</button>` : html`<span class="a">${a}</span>`}<span class="b" style=${c ? { color: c } : null}>${b || ''}</span></div>`;
}
export function IBlock({ title, children }) {
  return html`<div class="iblock"><div class="small-label">${title}</div><div class="irows">${children}</div></div>`;
}
const row = (a, b, c, onClick) => html`<${IRow} a=${a} b=${b} c=${c} onClick=${onClick} />`;
const block = (title, rows) => html`<${IBlock} title=${title}>${rows}<//>`;
const NONE = '#7D838C';

function SummaryBlock({ f }) {
  const r = f.summary;
  if (!r || (!r.goal && !r.size && r.requirements == null)) return html`<div class="iblock"><div class="small-label">What it does</div><div class="empty-note">no summary available</div></div>`;
  const chips = [r.size ? 'size ' + r.size : null, r.requirements != null ? r.requirements + ' requirement' + (r.requirements === 1 ? '' : 's') : null, r.source && r.source !== 'none' ? 'source: ' + r.source : null].filter(Boolean);
  return html`<div class="iblock"><div class="small-label">What it does</div>
    <div class="res-box">${r.goal ? html`<div class="res-txt">${String(r.goal)}</div>` : null}${chips.length ? html`<div class="chips">${chips.map((c) => html`<span>${c}</span>`)}</div>` : null}</div></div>`;
}
function WaveBlock({ f, info }) {
  const o = f.wave;
  let line;
  if (!o) line = 'no wave recorded yet';
  else if (o.upcoming) { const w = list(f.waiting_on).length ? list(f.waiting_on) : info.waiting; line = 'next wave' + (w.length ? ', waiting on ' + w.join(', ') : '') + (o.reason ? '; ' + o.reason : ''); }
  else line = `turn ${txt(o.turn)}${list(o.together_with).length ? ', together with ' + list(o.together_with).join(', ') : ', alone'}${o.reason ? '; ' + o.reason : ''}`;
  return html`<div class="iblock"><div class="small-label">Wave</div>
    <div class="res-box"><div class="res-txt">${line}</div>
      <div class="res-hint">Wave = features that run together because they edit different files and do not depend on each other.</div></div></div>`;
}
function waveShort(f) {
  const o = f.wave; if (!o) return '';
  return o.upcoming ? 'next wave' : isNum(o.turn) ? 'turn ' + o.turn : '';
}
function StageBlocks({ n }) {
  const e = list(ctx.state && ctx.state.stages).find((x) => x.n === n);
  if (!e) return html`<div class="empty-note">Stage not found.</div>`;
  const r = e.summary || {}, byId = featById(), pr = r.progress || {};
  const tot = isNum(pr.total) && pr.total > 0 ? pr.total : list(e.features).length, ver = isNum(pr.verified) ? pr.verified : 0, blk = isNum(pr.blocked) ? pr.blocked : 0;
  const f = r.closing;
  const secs = [['delivered', 'Delivered'], ['decisions', 'User decisions'], ['lessons', 'Lessons'], ['unverified', 'Unverified'], ['blocked', 'Blocked'], ['next_stage', 'For the next stage']].filter(([k]) => f && f[k]);
  return html`
    <div class="iblock"><div class="small-label">Description</div><div class="res-box"><div class="res-txt">${r.description ? String(r.description) : 'no description in the ROADMAP'}</div></div></div>
    <div class="iblock"><div class="small-label">Progress</div>
      <div class="st-bar" role="img" aria-label=${`${ver} verified, ${blk} blocked, of ${tot}`}>
        <div class="st-ok" style=${{ width: (tot ? (ver / tot) * 100 : 0) + '%' }}></div><div class="st-bad" style=${{ width: (tot ? (blk / tot) * 100 : 0) + '%' }}></div>
      </div>
      <div class="st-nums mono">${`${ver} verified · ${blk} blocked · ${tot} in total`}</div>
      ${r.current_feature ? html`<div class="st-now">now: <button type="button" class="link-btn" onClick=${() => selectFeature(r.current_feature)}>${String(r.current_feature)}</button></div>` : null}
    </div>
    ${block('Features', list(e.features).length ? list(e.features).map((id) => { const x = byId[id]; return x ? row(`${x.name || id}${waveShort(x) ? ' · ' + waveShort(x) : ''}`, x.status, pal(x.status)[0], () => selectFeature(id)) : row(id, '', NONE); }) : row('none', '', NONE))}
    ${list(r.waves).length ? block('Waves', list(r.waves).map((w) => row(`turn ${txt(w.turn)}`, list(w.features).join(', '), '#8B9098'))) : null}
    ${!f ? html`<div class="iblock"><div class="empty-note">stage in progress</div></div>`
      : html`<div class="iblock"><div class="small-label">Stage closing</div>${secs.length ? secs.map(([k, label], i) => html`<details key=${k} class="st-sec" open=${i === 0}><summary>${label}</summary><div class="st-sec-b">${String(f[k])}</div></details>`)
        : html`<div class="empty-note">closing without sections</div>`}</div>`}`;
}
function FeatureBlocks({ f, compact }) {
  const byId = featById(), info = depInfo(f);
  const reached = f.status === 'blocked' ? 4 : LIFECYCLE.indexOf(f.status);
  const ag = agents().find((a) => a.feature === f.id);
  const [lab, labColor] = readiness(f, info);
  const stRow = (id) => { const x = byId[id]; return x ? row(x.name || id, x.status, pal(x.status)[0], () => selectFeature(id)) : row(id, 'external', NONE); };
  return html`
    ${compact ? null : html`<${SummaryBlock} f=${f} />`}<${WaveBlock} f=${f} info=${info} />
    ${block('Status', row(lab, '', labColor))}
    ${block('Depends on', info.dep.length ? info.dep.map((d) => stRow(d.id)) : row('none', '', NONE))}
    ${info.dirs.length ? block('Imports folders', info.dirs.map((p) => row(p, 'folder', NONE))) : null}
    ${block('Unblocks', info.unblocks.length ? info.unblocks.map(stRow) : row('none — quarantine does not pause the loop', '', NONE))}
    ${block('Lifecycle', LIFECYCLE.map((s, i) => {
      const isBlk = f.status === 'blocked' && i === reached;
      return row(s, isBlk ? 'blocked' : i < reached ? 'ok' : i === reached ? 'now' : '', isBlk ? '#F06A5E' : i <= reached ? pal(s)[0] : '#4A4F57');
    }))}
    ${block('Details', [row('tries', txt(f.tries)), row('commit', txt(f.commit)), row('regression', txt(f.regression)), row('e2e', txt(f.e2e)), f.note ? row('note', String(f.note)) : null])}
    ${block('Paths (scope)', list(f.paths).length ? list(f.paths).map((p) => row(p, '')) : row('none declared', '', NONE))}
    ${ag && !compact ? block('Agent in this turn', row(ag.role, 'open log →', '#F2B544', () => selectAgent(ag.id))) : null}`;
}
function AgentHighlight({ a }) {
  const ret = a.return_status ? String(a.return_status) : null, ua = a.last_action, rc = ret ? retColor(ret) : null;
  return html`<div class="hl">
    <div class="small-label">Last action · decision · return</div>
    <div class="hl-row"><span class="hl-k">return</span>${ret ? html`<span class="rbadge" style=${{ color: rc, borderColor: rc }}>${ret}</span>` : html`<span class="dim-mono">no return yet</span>`}</div>
    <div class="hl-row"><span class="hl-k">decision</span><span class="hl-v">${a.decision ? String(a.decision) : a.summary ? String(a.summary) : DASH}</span></div>
    <div class="hl-row"><span class="hl-k">last action</span>
      <span class="hl-v mono">${ua && ua.text ? [html`<span class="dim-mono">${`${txt(ua.type)}${ua.t ? ' · ' + ua.t : ''}  `}</span>`, String(ua.text)] : DASH}</span></div>
  </div>`;
}
function PastBlocks() {
  const p = ui.past, r = (p && p.turnLog) || {};
  return html`
    ${block('Turn result', [row('cost', cost(r.total_cost_usd)), row('duration', dur(isNum(r.duration_ms) ? r.duration_ms / 1000 : null)), row('model turns', num(r.num_turns)),
      row('exit code', txt(r.exit_code)), row('timeout', r.timeout == null ? DASH : String(r.timeout)), row('permission denials', Array.isArray(r.permission_denials) ? String(r.permission_denials.length) : DASH)])}
    ${r.result ? html`<div class="iblock"><div class="small-label">Loop reply</div><div class="msg" style="background:#15171A;border:1px solid #24272C"><div class="msg-b">${String(r.result)}</div></div></div>` : null}
    ${p && p.roadmap ? html`<details><summary class="small-label" style="cursor:pointer">ROADMAP in the snapshot</summary><pre class="msg-b m" style="white-space:pre-wrap">${String(p.roadmap)}</pre></details>` : null}`;
}

/** What the inspector shows now (chat = the is-chat flag of #insp-body); drops a selection that no longer exists. */
export function inspectorModel() {
  const state = ctx.state, sel = ui.sel;
  if (sel && sel.kind === 'agent' && !agentById(sel.id)) ui.sel = null;
  if (sel && sel.kind === 'feature' && !featById()[sel.id]) ui.sel = null;
  if (sel && sel.kind === 'stage' && !list(state.stages).some((x) => x.n === sel.n)) ui.sel = null;
  // default: the live orchestrator
  const eff = ui.sel || (live() && agentById('orch') ? { kind: 'agent', id: 'orch' } : null);
  const a = eff && eff.kind === 'agent' ? agentById(eff.id) : null;
  const f = eff && eff.kind === 'feature' ? featById()[eff.id] : null;
  const st = eff && eff.kind === 'stage' ? list(state.stages).find((x) => x.n === eff.n) : null;
  const al = a && ui.agentLog && ui.agentLog.id === a.id ? ui.agentLog : null;
  const logArr = a ? (al && (al.log.length || !list(a.log).length) ? al.log : list(a.log)) : [];
  const logNote = a ? (al && al.err ? 'Log unavailable: ' + al.err : !al && ui.logLoading === a.id ? 'loading the log…' : null) : null;
  return { a, f, st, logArr, logNote, truncated: !!(al && al.truncated), chat: ui.tab === 'chat' };
}

const Chips = ({ items, children }) => html`<div class="chips">${items.filter((x) => x).map((x) => html`<span>${String(x)}</span>`)}${children}</div>`;
function TitleRow({ title, color, bg, chip }) {
  return html`<div class="insp-row">${color ? html`<span class="dot" style=${{ background: color }}></span>` : null}<div class="insp-title">${title}</div>${chip != null ? html`<div class="spacer"></div><span class="chip" style=${color ? { color, background: bg } : null}>${chip}</span>` : null}</div>`;
}
const FOCUS_PATHS_SHOWN = 4;
/** Stage the orchestrator is on: the first stage not fully verified (or the last one). */
function currentStage() {
  const stages = list(ctx.state && ctx.state.stages);
  const open = stages.find((s) => { const p = (s.summary && s.summary.progress) || {}; return !(isNum(p.total) && p.total > 0 && p.verified >= p.total); });
  return open || stages[stages.length - 1] || null;
}
/** What the agent is working on: its feature (or roadmap point), scope paths, current task and the "view definition" button. */
function AgentFocusCard({ a }) {
  const feature = a.feature ? featById()[a.feature] : null;
  if (!feature) {
    const stage = a.role === 'orchestrator' ? currentStage() : null;
    return html`<div class="focus-card">
      <div class="focus-label">${a.role === 'orchestrator' ? 'Roadmap point' : 'Feature'}</div>
      ${a.role === 'orchestrator'
        ? html`<div class="focus-name">${stage ? `Stage ${String(stage.n).padStart(2, '0')}${stage.title ? ' · ' + txt(stage.title) : ''}` : 'Coordinates all features'}</div>
            <div class="focus-goal">${stage && stage.summary && stage.summary.description ? String(stage.summary.description) : 'Coordinates all features of the roadmap: dispatch, commit and checkpoint.'}</div>
            ${stage ? html`<button type="button" class="link-btn" onClick=${() => selectStage(stage.n)}>open the stage</button>` : null}`
        : html`<div class="focus-name">${a.feature ? txt(a.feature) : 'Not tied to a feature'}</div>`}
    </div>`;
  }
  const [color, background] = pal(feature.status);
  const goal = feature.summary && feature.summary.goal ? String(feature.summary.goal) : '';
  const paths = list(feature.paths);
  const task = a.decision || a.summary || (a.last_action && a.last_action.text) || '';
  return html`<div class="focus-card">
    <div class="focus-label">Working on</div>
    <div class="focus-top"><span class="focus-name">${txt(feature.name || feature.id)}</span>
      <span class="chip" style=${{ color, background }}>${txt(feature.status)}</span>${feature.stage != null ? html`<span class="chip">${'stage ' + txt(feature.stage)}</span>` : null}</div>
    ${goal ? html`<div class="focus-goal">${goal}</div>` : null}
    ${paths.length ? html`<div class="focus-paths" title=${paths.join('\n')}><span class="focus-k">scope</span>
      ${paths.slice(0, FOCUS_PATHS_SHOWN).map((p) => html`<span class="focus-path">${p}</span>`)}${paths.length > FOCUS_PATHS_SHOWN ? html`<span class="dim-mono">${`+${paths.length - FOCUS_PATHS_SHOWN} more`}</span>` : null}</div>` : null}
    ${task ? html`<div class="focus-task"><span class="focus-k">task</span><span>${String(task)}</span></div>` : null}
    <button type="button" class="link-btn" onClick=${() => openFeatureDetail(feature)}>View definition</button>
  </div>`;
}
/** Content of #insp-head. */
export function InspectorHead({ m = inspectorModel() }) {
  const { a, f, st } = m;
  if (a) {
    const [c, bg] = pal(a.state);
    return html`<${TitleRow} title=${txt(a.role)} color=${c} bg=${bg} chip=${txt(a.state)} /><div class="insp-role">${ROLE_DESC[a.role] || ''}</div>
      <${Chips} items=${[dur(a.duration_s), tok(a.tokens) + ' tok', cost(a.cost_usd)]} />
      <${AgentFocusCard} a=${a} />`;
  }
  if (st) return html`<${TitleRow} title=${`Stage ${String(st.n).padStart(2, '0')}`} chip=${`${num(st.features && st.features.length)} features`} /><div class="insp-role">${txt(st.title)}</div>`;
  if (f) {
    const [c, bg] = pal(f.status);
    return html`<${TitleRow} title=${txt(f.id)} color=${c} bg=${bg} chip=${txt(f.status)} /><div class="insp-role">${txt(f.name)}</div>
      <${Chips} items=${[`stage ${txt(f.stage)}`, txt(f.kind), f.commit]}><button type="button" class="link-btn view-spec" title="opens the spec of this feature in the Files tab" onClick=${() => openFeatureSpec(f.id)}>view spec</button><//>`;
  }
  if (ui.past) return html`<${TitleRow} title=${`turn ${ui.pastN}`} /><div class="insp-role">snapshot · read-only</div>`;
  return html`<${TitleRow} title="Summary" /><div class="insp-role">click a feature or an agent to see its details</div>`;
}
/** Content of #insp-tabs (role=tablist; arrows handled by bindTabKeys). */
export function InspectorTabs({ m = inspectorModel() }) {
  const tabs = m.a ? [['log', 'Log', ui.tab === 'log'], ['info', 'Scope', ui.tab === 'info'], ['chat', 'Chat', ui.tab === 'chat']]
    : [['info', m.st ? 'Stage' : 'Details', ui.tab !== 'chat'], ['chat', 'Chat', ui.tab === 'chat']];
  return tabs.map(([k, label, on]) => html`<button key=${k} type="button" role="tab" id=${'insp-tab-' + k} class="insp-tab" aria-selected=${String(on)} tabindex=${on ? '0' : '-1'}
    onClick=${() => { ui.tab = k; renderAll(); }}>${label}</button>`);
}
/** Content of #insp-body. */
export function InspectorBody({ m = inspectorModel() }) {
  const { a, f, st, logArr, logNote } = m;
  if (ui.tab === 'chat') return html`<${Chat} />`;
  if (a) {
    if (ui.tab === 'info') {
      const af = a.feature ? featById()[a.feature] : null;
      return af ? html`<${FeatureBlocks} f=${af} compact=${true} />` : html`<div class="empty-note">This agent is not tied to a feature.</div>`;
    }
    return html`<${AgentHighlight} a=${a} /><div class="small-label">Full log</div>
      ${logArr.length ? [logArr.map((x, i) => html`<${LogEntry} key=${i} m=${x} agentName=${a.role} />`), m.truncated ? html`<div class="logend">log truncated (first 400 items)</div>` : null]
        : html`<div class="empty-note">${logNote || 'No log recorded for this agent (transcript unavailable or empty).'}</div>`}`;
  }
  if (st) return html`<${StageBlocks} n=${st.n} />`;
  if (f) return html`<${FeatureBlocks} f=${f} />`;
  if (ui.past) return html`<${PastBlocks} />`;
  const c = ctx.state.counts || {};
  return block('Features', FEATURE_STATUSES.map((k) => row(k, num(c[k]), pal(k)[0])));
}
