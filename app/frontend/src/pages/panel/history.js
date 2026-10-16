/* "History" view: every turn and its agents (GET history, never in the 2 s polling). */
import { h } from '../../util/dom.js';
import { cost, dur, tok, txt, list } from '../../util/format.js';
import { ctx, ui, pal, TURN_COLOR } from './context.js';
import { renderAll } from '../project_panel.js';
import { renderCenter } from './center.js';
import { plainCanvas, emptyMsg, retColor, actText } from './turn_diagram.js';
import { selectAgent } from './roadmap_sidebar.js';
import { pickTurn } from './timeline.js';

export async function loadHist(force) {
  const state = ctx.state;
  const key = String(state && state.meta ? state.meta.turns : null), hs = ui.hist;
  if (hs.loading) return;
  if (!force && hs.key === key) return;
  hs.loading = true; hs.key = key; hs.err = null; if (!hs.data) renderCenter();
  try {
    const r = await ctx.api.get('history', 30000);
    hs.data = list(r && r.turns);
  } catch (e) {
    hs.err = e.status === 404 ? 'The server does not offer the history yet.' : e.message;
  } finally { hs.loading = false; hs.ver += 1; renderAll(); }
}
const histOpenFor = (v, i) => (ui.histOpen[v.n] != null ? ui.histOpen[v.n] : i < 2 || v.n === ui.pastN);
function histRow(a) {
  const [c] = pal(a.state), on = ui.sel && ui.sel.kind === 'agent' && ui.sel.id === a.id;
  const dot = h('span', { class: 'dot' }); dot.style.background = c;
  const st = h('span', { class: 'arow-state', text: txt(a.state) }); st.style.color = c;
  const ret = a.return_status ? h('span', { class: 'rbadge', text: String(a.return_status) }) : null;
  if (ret) { const rc = retColor(a.return_status); ret.style.color = rc; ret.style.borderColor = rc; }
  const act = actText(a), dec = a.decision || a.summary;
  return h('button', { type: 'button', class: 'hrow' + (on ? ' sel' : ''), title: a.id, 'aria-pressed': on ? 'true' : 'false', onclick: () => selectAgent(a.id) },
    h('span', { class: 'hrow-top' }, dot, h('span', { class: 'arow-role', text: txt(a.role) }), h('span', { class: 'arow-feat', text: a.feature || '' }), ret, st,
      h('span', { class: 'arow-meta', text: `${tok(a.tokens)} tok · ${dur(a.duration_s)}` })),
    h('span', { class: 'hrow-bot' }, h('span', { class: 'hrow-dec', text: dec ? String(dec) : 'no decision recorded' })),
    act ? h('span', { class: 'hrow-bot' }, h('span', { class: 'hrow-res', text: act })) : null);
}
export function histView() {
  const hs = ui.hist, data = list(hs.data);
  if (!hs.data) { plainCanvas(emptyMsg(hs.loading || hs.key === undefined ? 'Loading the history…' : `History unavailable: ${hs.err || 'no response'}`)); return; }
  if (!data.length) { plainCanvas(emptyMsg('No turn in the history yet. When the loop runs its first turn, it shows up here.')); return; }
  const items = data.map((v, i) => {
    const open = histOpenFor(v, i), ags = list(v.agents), c = TURN_COLOR[v.status] || '#4A4F57';
    const dot = h('span', { class: 'dot' }); dot.style.background = c;
    const stt = h('span', { class: 'hmeta', text: txt(v.status) }); stt.style.color = c;
    const toggle = h('button', { type: 'button', class: 'hhead', 'aria-expanded': String(open), onclick: () => { ui.histOpen[v.n] = !open; renderCenter(); } },
      h('span', { class: 'hchev', text: open ? '▾' : '▸' }), dot, h('span', { class: 'hn', text: 'turn ' + v.n }), stt,
      h('span', { class: 'hmeta', text: `${cost(v.cost_usd)} · ${dur(v.duration_s)} · ${ags.length} agent${ags.length === 1 ? '' : 's'}` }));
    const sv_ = list(ctx.state && ctx.state.turns).find((x) => x.n === v.n);
    const head = h('div', { class: 'hhead-row' }, toggle,
      h('button', { type: 'button', class: 'btn btn-sm btn-ghost', title: 'opens the pipeline diagram of this turn', disabled: !!(sv_ && sv_.has_snapshot === false), onclick: () => { ui.view = 'turn'; if (sv_) pickTurn(sv_); else renderCenter(); } }, 'view diagram'));
    const kids = [head];
    if (open) {
      const rows = [];
      if (v.transcript === false) rows.push(h('div', { class: 'hnote', text: 'Transcript unavailable: only the orchestrator of this turn (data from the turn result).' }));
      if (!ags.length) rows.push(h('div', { class: 'hnote', text: 'No agents recorded in this turn.' }));
      ags.forEach((a) => rows.push(histRow(a)));
      kids.push(h('div', { class: 'hrows' }, rows));
    }
    return h('div', { class: 'hturn' + (v.n === ui.pastN ? ' sel' : '') }, kids);
  });
  plainCanvas(h('div', { class: 'hist' }, hs.err ? h('div', { class: 'hnote', text: 'Could not refresh: ' + hs.err }) : null, items));
}
export function openHist() {
  ui.view = 'hist'; renderCenter();
  loadHist(!!ui.hist.err);
}
