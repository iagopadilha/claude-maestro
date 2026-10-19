/* "History" view: every turn and its agents (GET history, never in the 2 s polling). */
import { html } from '../../util/preact.js';
import { cost, dur, tok, txt, list } from '../../util/format.js';
import { ctx, ui, pal, TURN_COLOR } from './context.js';
import { renderAll } from '../project_panel.js';
import { retColor, actText } from './turn_diagram.js';
import { selectAgent } from './roadmap_sidebar.js';
import { pickTurn } from './timeline.js';

export async function loadHist(force) {
  const state = ctx.state;
  const key = String(state && state.meta ? state.meta.turns : null), hs = ui.hist;
  if (hs.loading) return;
  if (!force && hs.key === key) return;
  hs.loading = true; hs.key = key; hs.err = null; if (!hs.data) renderAll();
  try {
    const r = await ctx.api.get('history', 30000);
    hs.data = list(r && r.turns);
  } catch (e) {
    hs.err = e.status === 404 ? 'The server does not offer the history yet.' : e.message;
  } finally { hs.loading = false; hs.ver += 1; renderAll(); }
}
const histOpenFor = (v, i) => (ui.histOpen[v.n] != null ? ui.histOpen[v.n] : i < 2 || v.n === ui.pastN);

function HistRow({ a }) {
  const [c] = pal(a.state), on = ui.sel && ui.sel.kind === 'agent' && ui.sel.id === a.id;
  const rc = a.return_status ? retColor(a.return_status) : null;
  const act = actText(a), dec = a.decision || a.summary;
  return html`<button type="button" class=${'hrow' + (on ? ' sel' : '')} title=${a.id} aria-pressed=${on ? 'true' : 'false'} onClick=${() => selectAgent(a.id)}>
    <span class="hrow-top"><span class="dot" style=${{ background: c }}></span><span class="arow-role">${txt(a.role)}</span><span class="arow-feat">${a.feature || ''}</span>${
      a.return_status ? html`<span class="rbadge" style=${{ color: rc, borderColor: rc }}>${String(a.return_status)}</span>` : null
    }<span class="arow-state" style=${{ color: c }}>${txt(a.state)}</span><span class="arow-meta">${`${tok(a.tokens)} tok · ${dur(a.duration_s)}`}</span></span>
    <span class="hrow-bot"><span class="hrow-dec">${dec ? String(dec) : 'no decision recorded'}</span></span>
    ${act ? html`<span class="hrow-bot"><span class="hrow-res">${act}</span></span>` : null}
  </button>`;
}

function HistTurn({ v, open }) {
  const ags = list(v.agents), c = TURN_COLOR[v.status] || '#4A4F57';
  const sv_ = list(ctx.state && ctx.state.turns).find((x) => x.n === v.n);
  return html`<div class=${'hturn' + (v.n === ui.pastN ? ' sel' : '')}>
    <div class="hhead-row">
      <button type="button" class="hhead" aria-expanded=${String(open)} onClick=${() => { ui.histOpen[v.n] = !open; renderAll(); }}>
        <span class="hchev">${open ? '▾' : '▸'}</span><span class="dot" style=${{ background: c }}></span><span class="hn">${'turn ' + v.n}</span><span class="hmeta" style=${{ color: c }}>${txt(v.status)}</span><span class="hmeta">${`${cost(v.cost_usd)} · ${dur(v.duration_s)} · ${ags.length} agent${ags.length === 1 ? '' : 's'}`}</span>
      </button>
      <button type="button" class="btn btn-sm btn-ghost" title="opens the pipeline diagram of this turn" disabled=${!!(sv_ && sv_.has_snapshot === false)}
        onClick=${() => { ui.view = 'turn'; if (sv_) pickTurn(sv_); else renderAll(); }}>view diagram</button>
    </div>
    ${open ? html`<div class="hrows">
      ${v.transcript === false ? html`<div class="hnote">Transcript unavailable: only the orchestrator of this turn (data from the turn result).</div>` : null}
      ${!ags.length ? html`<div class="hnote">No agents recorded in this turn.</div>` : null}
      ${ags.map((a, i) => html`<${HistRow} key=${a.id || i} a=${a} />`)}
    </div>` : null}
  </div>`;
}

/** Content of #canvas in the "History" view (the canvas is in plain mode). */
export function History() {
  const hs = ui.hist, data = list(hs.data);
  if (!hs.data) return html`<div class="empty-canvas">${hs.loading || hs.key === undefined ? 'Loading the history…' : `History unavailable: ${hs.err || 'no response'}`}</div>`;
  if (!data.length) return html`<div class="empty-canvas">No turn in the history yet. When the loop runs its first turn, it shows up here.</div>`;
  return html`<div class="hist">
    ${hs.err ? html`<div class="hnote">${'Could not refresh: ' + hs.err}</div>` : null}
    ${data.map((v, i) => html`<${HistTurn} key=${v.n} v=${v} open=${histOpenFor(v, i)} />`)}
  </div>`;
}

export function openHist() {
  ui.view = 'hist'; renderAll();
  loadHist(!!ui.hist.err);
}
