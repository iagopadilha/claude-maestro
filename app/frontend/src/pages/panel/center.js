/* Panel center: tabs Current turn | History | Dependencies | Files | Whiteboard. */
import { $, h, fill } from '../../util/dom.js';
import { isNum, list } from '../../util/format.js';
import { createWhiteboard } from '../../components/whiteboard.js';
import { ctx, ui, region, pal, lastN, FEATURE_STATUSES } from './context.js';
import { agentsModel, dgRender, dgFit, zoomAt, plainCanvas, fullCanvas, emptyMsg } from './turn_diagram.js';
import { histView, loadHist, openHist } from './history.js';
import { dependenciesView, zoomDependencies, fitDependencies, dependenciesScale } from './dependencies.js';
import { filesView } from './files.js';
import { backToLive } from './timeline.js';

const VIEWS = [['turn', 'tab-turn'], ['hist', 'tab-hist'], ['deps', 'tab-deps'], ['files', 'tab-files'], ['board', 'tab-board']];
const TURN_LEGEND = [['running', '#F2B544'], ['done', '#5BD38A'], ['queued', '#8AA4FF'], ['idle', '#7D838C'], ['blocked/failed', '#F06A5E']];
let board = null;

function legendItems(view) {
  const dotI = (label, color) => ({ label, color });
  const lineI = (label, color, dashed) => ({ label, color, line: true, dashed });
  if (view === 'turn') return [...TURN_LEGEND.map(([l, c]) => dotI(l, c)), lineI('in progress', '#F2B544', true), lineI('done', '#4A4F57', false), lineI('failure', '#F06A5E', true)];
  if (view === 'deps') return [lineI('dependency met (verified)', '#5BD38A', false), lineI('waiting', '#F2B544', true), lineI('blocked', '#F06A5E', true),
    ...FEATURE_STATUSES.map((k) => dotI(k, pal(k)[0]))];
  return [];
}
const CAPTIONS = {
  turn: () => (ui.pastN != null ? `pipeline of turn ${ui.pastN}: one lane per feature` : 'pipeline of this turn: one lane per feature, left to right'),
  hist: () => 'every turn and its agents · click an agent to see its full log',
  deps: () => 'one column per stage; inside a stage, each feature sits to the right of what it waits on · hover to see the path, click to open',
  files: () => 'everything the loop writes in .specs, in one place · read or edit what the app allows',
  board: () => 'free project board · saves itself on the server',
};

export function getBoard() {
  if (!board) board = createWhiteboard({ api: ctx.api });
  return board;
}

export function renderCenter() {
  const state = ctx.state;
  if (!state) return;
  const view = ui.view, hs = ui.hist;
  const model = view === 'turn' ? agentsModel() : null;
  const depsSig = view === 'deps' ? [list(ui.past ? ui.past.features : state.features).map((f) => [f.id, f.status, f.stage, f.deps, f.name]), state.stages && state.stages.map((e) => [e.n, e.title]), state.counts, ui.sel] : null;
  const data = { view, past: ui.pastN, sel: view === 'turn' ? ui.sel : null, ln: lastN(), pl: ui.pinLast, m: model, d: depsSig,
    hv: view === 'hist' ? [hs.ver, ui.histOpen, !hs.data && hs.loading, state.turns && state.turns.length] : null };
  region('center', data, () => {
    const wrap = document.querySelector('.canvas-wrap'), top = wrap ? wrap.scrollTop : 0;
    for (const [k, id] of VIEWS) { const b = $(id); b.setAttribute('aria-selected', String(view === k)); b.tabIndex = view === k ? 0 : -1; }
    $('caption').textContent = (CAPTIONS[view] || (() => ''))();
    $('zoom-ctl').hidden = !(view === 'deps' || (view === 'turn' && model && model.mode !== 'plain'));
    const pb = $('past-bar');
    if (ui.pastN != null && (view === 'turn' || view === 'deps' || view === 'hist')) {
      pb.hidden = false;
      const last = lastN(), fresh = isNum(last) && isNum(ui.pinLast) && last > ui.pinLast;
      fill(pb, h('span', { text: `Snapshot of turn ${ui.pastN} · read-only` }),
        h('button', { type: 'button', class: 'btn btn-sm btn-ghost', onclick: backToLive }, 'back to the latest turn'),
        fresh ? h('button', { type: 'button', class: 'btn btn-sm past-new', onclick: backToLive }, `new turn ${last} available`) : null);
    } else { pb.hidden = true; fill(pb); }
    if (view === 'hist') histView();
    else if (view === 'deps') fullCanvas(dependenciesView());
    else if (view === 'files') fullCanvas(filesView());
    else if (view === 'board') { const q = getBoard(); fullCanvas(q.element); q.open(false); }
    else if (model.mode === 'plain') plainCanvas(emptyMsg(model.msg));
    else dgRender(model);
    fill($('legend'), legendItems(view).map((it) => {
      const mark = it.line ? h('span', { class: 'sw-line' + (it.dashed ? ' dashed' : '') }) : h('span', { class: 'dot' });
      if (it.line) mark.style.color = it.color; else mark.style.background = it.color;
      return h('div', {}, mark, it.label);
    }));
    $('legend').hidden = !legendItems(view).length;
    if (wrap && view === 'hist') wrap.scrollTop = top;
  });
}

export function initCenter() {
  $('tab-turn').addEventListener('click', () => { ui.view = 'turn'; renderCenter(); if (ui.pastN != null) loadHist(); });
  $('tab-deps').addEventListener('click', () => { ui.view = 'deps'; renderCenter(); });
  $('tab-hist').addEventListener('click', openHist);
  $('tab-files').addEventListener('click', () => { ui.view = 'files'; renderCenter(); });
  $('tab-board').addEventListener('click', () => { ui.view = 'board'; renderCenter(); });
  const showScale = () => { if (ui.view === 'deps') $('zoom-val').textContent = Math.round(dependenciesScale() * 100) + '%'; };
  $('zoom-in').addEventListener('click', () => { if (ui.view === 'deps') zoomDependencies(1.2); else zoomAt(1.2); showScale(); });
  $('zoom-out').addEventListener('click', () => { if (ui.view === 'deps') zoomDependencies(1 / 1.2); else zoomAt(1 / 1.2); showScale(); });
  $('zoom-fit').addEventListener('click', () => { if (ui.view === 'deps') fitDependencies(); else dgFit(); showScale(); });
}

/** Light polling of the board: only with the Whiteboard tab open (and the page visible). */
export function checkRemoteBoard() {
  if (ui.view === 'board' && board) board.checkRemote();
}
