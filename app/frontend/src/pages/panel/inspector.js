/* Inspector (right): agent (log, scope, chat), feature, stage, snapshot or summary. */
import { $, h, fill, setColors } from '../../util/dom.js';
import { DASH, isNum, num, cost, dur, tok, txt, list } from '../../util/format.js';
import { ctx, ui, region, pal, ROLE_DESC, LOG_KIND, LIFECYCLE, FEATURE_STATUSES, live, features, agents, featById, agentById } from './context.js';
import { selectFeature, selectAgent } from './roadmap_sidebar.js';
import { retColor } from './turn_diagram.js';
import { depInfo, readiness } from './dependencies.js';
import { mountChat } from './chat.js';
import { openFeatureSpec } from './files.js';

export async function fetchAgentLog(id) {
  const mine = () => ui.sel && ui.sel.kind === 'agent' && ui.sel.id === id;
  try {
    const r = await ctx.api.get('agent/' + encodeURIComponent(id) + '/log', 20000);
    if (mine()) { ui.agentLog = { id, log: list(r.log), truncated: !!r.truncated }; ui.logLoading = null; renderInspector(); }
  } catch (e) {
    if (mine()) { ui.agentLog = { id, log: [], err: e.message }; ui.logLoading = null; renderInspector(); }
  }
}
function logEntry(m, agentName) {
  const k = LOG_KIND[m.type] || LOG_KIND.sys;
  const label = m.type === 'guard' ? `blocked by the guard${m.rule ? ' · ' + m.rule : ''} · exit 2` : k[0] || agentName;
  const box = h('div', { class: 'msg' },
    h('div', { class: 'msg-h' }, h('span', { text: label }), h('span', { class: 't', text: txt(m.t) })),
    h('div', { class: 'msg-b' + (k[4] ? ' m' : ''), text: txt(m.text) }),
    m.result ? h('div', { class: 'msg-r', text: String(m.result) }) : null);
  box.style.background = k[2]; box.style.border = '1px solid ' + k[3];
  box.querySelector('.msg-h').style.color = k[1];
  return box;
}
export function irow(a, b, c, onclick) {
  const left = onclick ? h('button', { type: 'button', class: 'a', onclick, text: a }) : h('span', { class: 'a', text: a });
  const right = h('span', { class: 'b', text: b || '' }); if (c) right.style.color = c;
  return h('div', { class: 'irow' }, left, right);
}
export function iblock(title, rows) { return h('div', { class: 'iblock' }, h('div', { class: 'small-label', text: title }), h('div', { class: 'irows' }, rows)); }
function summaryBlock(f) {
  const r = f.summary;
  if (!r || (!r.goal && !r.size && r.requirements == null)) return h('div', { class: 'iblock' }, h('div', { class: 'small-label', text: 'What it does' }), h('div', { class: 'empty-note', text: 'no summary available' }));
  const chips = [r.size ? 'size ' + r.size : null, r.requirements != null ? r.requirements + ' requirement' + (r.requirements === 1 ? '' : 's') : null, r.source && r.source !== 'none' ? 'source: ' + r.source : null].filter(Boolean);
  return h('div', { class: 'iblock' }, h('div', { class: 'small-label', text: 'What it does' }),
    h('div', { class: 'res-box' }, r.goal ? h('div', { class: 'res-txt', text: String(r.goal) }) : null, chips.length ? h('div', { class: 'chips' }, chips.map((c) => h('span', { text: c }))) : null));
}
function waveBlock(f, info) {
  const o = f.wave;
  let line;
  if (!o) line = 'no wave recorded yet';
  else if (o.upcoming) { const w = list(f.waiting_on).length ? list(f.waiting_on) : info.waiting; line = 'next wave' + (w.length ? ', waiting on ' + w.join(', ') : '') + (o.reason ? '; ' + o.reason : ''); }
  else line = `turn ${txt(o.turn)}${list(o.together_with).length ? ', together with ' + list(o.together_with).join(', ') : ', alone'}${o.reason ? '; ' + o.reason : ''}`;
  return h('div', { class: 'iblock' }, h('div', { class: 'small-label', text: 'Wave' }),
    h('div', { class: 'res-box' }, h('div', { class: 'res-txt', text: line }),
      h('div', { class: 'res-hint', text: 'Wave = features that run together because they edit different files and do not depend on each other.' })));
}
function waveShort(f) {
  const o = f.wave; if (!o) return '';
  return o.upcoming ? 'next wave' : isNum(o.turn) ? 'turn ' + o.turn : '';
}
function stageBlocks(n) {
  const e = list(ctx.state && ctx.state.stages).find((x) => x.n === n);
  if (!e) return [h('div', { class: 'empty-note', text: 'Stage not found.' })];
  const r = e.summary || {}, byId = featById(), pr = r.progress || {};
  const tot = isNum(pr.total) && pr.total > 0 ? pr.total : list(e.features).length, ver = isNum(pr.verified) ? pr.verified : 0, blk = isNum(pr.blocked) ? pr.blocked : 0;
  const bar = h('div', { class: 'st-bar', role: 'img', 'aria-label': `${ver} verified, ${blk} blocked, of ${tot}` }, h('div', { class: 'st-ok' }), h('div', { class: 'st-bad' }));
  bar.firstChild.style.width = (tot ? (ver / tot) * 100 : 0) + '%'; bar.lastChild.style.width = (tot ? (blk / tot) * 100 : 0) + '%';
  const out = [
    h('div', { class: 'iblock' }, h('div', { class: 'small-label', text: 'Description' }), h('div', { class: 'res-box' }, h('div', { class: 'res-txt', text: r.description ? String(r.description) : 'no description in the ROADMAP' }))),
    h('div', { class: 'iblock' }, h('div', { class: 'small-label', text: 'Progress' }), bar,
      h('div', { class: 'st-nums mono', text: `${ver} verified · ${blk} blocked · ${tot} in total` }),
      r.current_feature ? h('div', { class: 'st-now' }, 'now: ', h('button', { type: 'button', class: 'link-btn', onclick: () => selectFeature(r.current_feature), text: String(r.current_feature) })) : null),
    iblock('Features', list(e.features).length ? list(e.features).map((id) => { const x = byId[id]; return x ? irow(`${x.name || id}${waveShort(x) ? ' · ' + waveShort(x) : ''}`, x.status, pal(x.status)[0], () => selectFeature(id)) : irow(id, '', '#7D838C'); }) : [irow('none', '', '#7D838C')]),
  ];
  if (list(r.waves).length) out.push(iblock('Waves', list(r.waves).map((w) => irow(`turn ${txt(w.turn)}`, list(w.features).join(', '), '#8B9098'))));
  const f = r.closing;
  if (!f) out.push(h('div', { class: 'iblock' }, h('div', { class: 'empty-note', text: 'stage in progress' })));
  else {
    const secs = [['delivered', 'Delivered'], ['decisions', 'User decisions'], ['lessons', 'Lessons'], ['unverified', 'Unverified'], ['blocked', 'Blocked'], ['next_stage', 'For the next stage']];
    const kids = secs.filter(([k]) => f[k]).map(([k, label], i) => h('details', { class: 'st-sec', open: i === 0 ? true : null }, h('summary', { text: label }), h('div', { class: 'st-sec-b', text: String(f[k]) })));
    out.push(h('div', { class: 'iblock' }, h('div', { class: 'small-label', text: 'Stage closing' }), kids.length ? kids : h('div', { class: 'empty-note', text: 'closing without sections' })));
  }
  return out;
}
function featureBlocks(f) {
  const byId = featById(), info = depInfo(f);
  const reached = f.status === 'blocked' ? 4 : LIFECYCLE.indexOf(f.status);
  const ag = agents().find((a) => a.feature === f.id);
  const [lab, labColor] = readiness(f, info);
  const stRow = (id) => { const x = byId[id]; return x ? irow(x.name || id, x.status, pal(x.status)[0], () => selectFeature(id)) : irow(id, 'external', '#7D838C'); };
  const out = [
    summaryBlock(f), waveBlock(f, info),
    iblock('Status', [irow(lab, '', labColor)]),
    iblock('Depends on', info.dep.length ? info.dep.map((d) => stRow(d.id)) : [irow('none', '', '#7D838C')]),
    info.dirs.length ? iblock('Imports folders', info.dirs.map((p) => irow(p, 'folder', '#7D838C'))) : null,
    iblock('Unblocks', info.unblocks.length ? info.unblocks.map(stRow) : [irow('none — quarantine does not pause the loop', '', '#7D838C')]),
    iblock('Lifecycle', LIFECYCLE.map((s, i) => {
      const isBlk = f.status === 'blocked' && i === reached;
      return irow(s, isBlk ? 'blocked' : i < reached ? 'ok' : i === reached ? 'now' : '', isBlk ? '#F06A5E' : i <= reached ? pal(s)[0] : '#4A4F57');
    })),
    iblock('Details', [irow('tries', txt(f.tries)), irow('commit', txt(f.commit)), irow('regression', txt(f.regression)), irow('e2e', txt(f.e2e)), f.note ? irow('note', String(f.note)) : null]),
    iblock('Paths', list(f.paths).length ? list(f.paths).map((p) => irow(p, '')) : [irow('none declared', '', '#7D838C')]),
  ];
  if (ag) out.push(iblock('Agent in this turn', [irow(ag.role, 'open log →', '#F2B544', () => selectAgent(ag.id))]));
  return out;
}
function agentHighlight(a) {
  const ret = a.return_status ? String(a.return_status) : null, ua = a.last_action;
  const retEl = ret ? h('span', { class: 'rbadge', text: ret }) : h('span', { class: 'dim-mono', text: 'no return yet' });
  if (ret) { const rc = retColor(ret); retEl.style.color = rc; retEl.style.borderColor = rc; }
  return h('div', { class: 'hl' },
    h('div', { class: 'small-label', text: 'Last action · decision · return' }),
    h('div', { class: 'hl-row' }, h('span', { class: 'hl-k', text: 'return' }), retEl),
    h('div', { class: 'hl-row' }, h('span', { class: 'hl-k', text: 'decision' }), h('span', { class: 'hl-v', text: a.decision ? String(a.decision) : a.summary ? String(a.summary) : DASH })),
    h('div', { class: 'hl-row' }, h('span', { class: 'hl-k', text: 'last action' }),
      h('span', { class: 'hl-v mono' }, ua && ua.text ? [h('span', { class: 'dim-mono', text: `${txt(ua.type)}${ua.t ? ' · ' + ua.t : ''}  ` }), String(ua.text)] : DASH)));
}
function pastBlocks() {
  const p = ui.past, r = (p && p.turnLog) || {};
  const rows = [irow('cost', cost(r.total_cost_usd)), irow('duration', dur(isNum(r.duration_ms) ? r.duration_ms / 1000 : null)), irow('model turns', num(r.num_turns)),
    irow('exit code', txt(r.exit_code)), irow('timeout', r.timeout == null ? DASH : String(r.timeout)), irow('permission denials', Array.isArray(r.permission_denials) ? String(r.permission_denials.length) : DASH)];
  const out = [iblock('Turn result', rows)];
  if (r.result) out.push(h('div', { class: 'iblock' }, h('div', { class: 'small-label', text: 'Loop reply' }), h('div', { class: 'msg', style: 'background:#15171A;border:1px solid #24272C' }, h('div', { class: 'msg-b', text: String(r.result) }))));
  if (p && p.roadmap) out.push(h('details', {}, h('summary', { class: 'small-label', style: 'cursor:pointer', text: 'ROADMAP in the snapshot' }), h('pre', { class: 'msg-b m', style: 'white-space:pre-wrap', text: String(p.roadmap) })));
  return out;
}
function tabBtn(k, label, selected) {
  return h('button', { type: 'button', role: 'tab', id: 'insp-tab-' + k, class: 'insp-tab', 'aria-selected': String(selected), tabindex: selected ? '0' : '-1', onclick: () => { ui.tab = k; renderInspector(); } }, label);
}
function plainTabs(tabs, first) {
  fill(tabs, [['info', first || 'Details'], ['chat', 'Chat']].map(([k, label]) => tabBtn(k, label, (ui.tab === 'chat') === (k === 'chat'))));
}
export function renderInspector() {
  const state = ctx.state;
  if (!state) return;
  const sel = ui.sel;
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
  const data = { eff, a, f, st, logArr, logNote, tr: al && al.truncated, tab: ui.tab, past: ui.pastN, pr: ui.past && ui.past.turnLog, fsAll: f || (a && a.feature) ? features().map((x) => [x.id, x.status, x.deps, x.waiting_on, x.unblocks, x.blocked_by, x.dep_dirs]) : null, ag: f ? agents().map((x) => [x.id, x.feature]) : null, c: state.counts };
  region('insp', data, () => {
    const head = $('insp-head'), tabs = $('insp-tabs'), body = $('insp-body');
    body.classList.toggle('is-chat', ui.tab === 'chat');
    if (a) {
      const [c, bg] = pal(a.state);
      const chip = h('span', { class: 'chip', text: txt(a.state) }); setColors(chip, c, bg);
      const dot = h('span', { class: 'dot' }); dot.style.background = c;
      fill(head, h('div', { class: 'insp-row' }, dot, h('div', { class: 'insp-title', text: txt(a.role) }), h('div', { class: 'spacer' }), chip),
        h('div', { class: 'insp-role', text: ROLE_DESC[a.role] || '' }),
        h('div', { class: 'chips' }, [a.feature, dur(a.duration_s), tok(a.tokens) + ' tok', cost(a.cost_usd)].filter((x) => x).map((x) => h('span', { text: String(x) }))));
      fill(tabs, [['log', 'Log'], ['info', 'Scope'], ['chat', 'Chat']].map(([k, label]) => tabBtn(k, label, ui.tab === k)));
      if (ui.tab === 'chat') mountChat(body);
      else if (ui.tab === 'info') {
        const af = a.feature ? featById()[a.feature] : null;
        fill(body, af ? featureBlocks(af) : [h('div', { class: 'empty-note', text: 'This agent is not tied to a feature.' })]);
      } else {
        fill(body, [agentHighlight(a), h('div', { class: 'small-label', text: 'Full log' }), ...(logArr.length ? [logArr.map((m) => logEntry(m, a.role)), al && al.truncated ? h('div', { class: 'logend', text: 'log truncated (first 400 items)' }) : null] : [h('div', { class: 'empty-note', text: logNote || 'No log recorded for this agent (transcript unavailable or empty).' })])]);
      }
    } else if (st) {
      fill(head, h('div', { class: 'insp-row' }, h('div', { class: 'insp-title', text: `Stage ${String(st.n).padStart(2, '0')}` }), h('div', { class: 'spacer' }), h('span', { class: 'chip', text: `${num(st.features && st.features.length)} features` })),
        h('div', { class: 'insp-role', text: txt(st.title) }));
      plainTabs(tabs, 'Stage'); if (ui.tab === 'chat') mountChat(body); else fill(body, stageBlocks(st.n));
    } else if (f) {
      const [c, bg] = pal(f.status);
      const chip = h('span', { class: 'chip', text: txt(f.status) }); setColors(chip, c, bg);
      const dot = h('span', { class: 'dot' }); dot.style.background = c;
      fill(head, h('div', { class: 'insp-row' }, dot, h('div', { class: 'insp-title', text: txt(f.id) }), h('div', { class: 'spacer' }), chip),
        h('div', { class: 'insp-role', text: txt(f.name) }),
        h('div', { class: 'chips' }, [`stage ${txt(f.stage)}`, txt(f.kind), f.commit].filter((x) => x).map((x) => h('span', { text: String(x) })),
          h('button', { type: 'button', class: 'link-btn view-spec', title: 'opens the spec of this feature in the Files tab', onclick: () => openFeatureSpec(f.id) }, 'view spec')));
      plainTabs(tabs); if (ui.tab === 'chat') mountChat(body); else fill(body, featureBlocks(f));
    } else if (ui.past) {
      fill(head, h('div', { class: 'insp-row' }, h('div', { class: 'insp-title', text: `turn ${ui.pastN}` })), h('div', { class: 'insp-role', text: 'snapshot · read-only' }));
      plainTabs(tabs); if (ui.tab === 'chat') mountChat(body); else fill(body, pastBlocks());
    } else {
      const c = state.counts || {};
      fill(head, h('div', { class: 'insp-row' }, h('div', { class: 'insp-title', text: 'Summary' })), h('div', { class: 'insp-role', text: 'click a feature or an agent to see its details' }));
      plainTabs(tabs); if (ui.tab === 'chat') mountChat(body); else fill(body, iblock('Features', FEATURE_STATUSES.map((k) => irow(k, num(c[k]), pal(k)[0]))));
    }
  });
}
