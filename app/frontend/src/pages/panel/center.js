/* Panel center: tabs Current turn | History | Dependencies | Roadmap & specs | Files, and the #canvas content of each view. */
import { html } from '../../util/preact.js';
import { isNum } from '../../util/format.js';
import { ui, pal, lastN, FEATURE_STATUSES } from './context.js';
import { renderAll } from '../project_panel.js';
import { agentsModel, TurnView, EmptyMsg, dgFit, zoomAt, showZoom, resetNodeLayout } from './turn_diagram.js';
import { History, loadHist, openHist } from './history.js';
import { Dependencies, zoomDependencies, fitDependencies, dependenciesScale } from './dependencies.js';
import { Files } from './files.js';
import { RoadmapSpecsView } from './roadmap_preview.js';
import { backToLive } from './timeline.js';

const TABS = [['turn', 'tab-turn', 'Current turn'], ['hist', 'tab-hist', 'History'], ['deps', 'tab-deps', 'Dependencies'], ['roadmap', 'tab-roadmap', 'Roadmap & specs'], ['files', 'tab-files', 'Files']];
const TURN_LEGEND = [['running', '#F2B544'], ['done', '#5BD38A'], ['queued', '#8AA4FF'], ['idle', '#7D838C'], ['blocked/failed', '#F06A5E']];

function legendItems(view) {
  const dotI = (label, color) => ({ label, color });
  const lineI = (label, color, dashed) => ({ label, color, line: true, dashed });
  if (view === 'turn') return [...TURN_LEGEND.map(([l, c]) => dotI(l, c)), lineI('in progress', '#F2B544', true), lineI('done', '#4A4F57', false), lineI('failure', '#F06A5E', true)];
  if (view === 'deps') return [lineI('dependency met (verified)', '#5BD38A', false), lineI('waiting', '#F2B544', true), lineI('blocked', '#F06A5E', true),
    ...FEATURE_STATUSES.map((k) => dotI(k, pal(k)[0]))];
  return [];
}
/** [short text shown in the tab bar, full text as tooltip]. */
const CAPTIONS = {
  turn: () => (ui.pastN != null ? [`turn ${ui.pastN} pipeline`, `pipeline of turn ${ui.pastN}: one lane per feature`] : ['turn pipeline', 'pipeline of this turn: one lane per feature, left to right']),
  hist: () => ['turns and agents', 'every turn and its agents · click an agent to see its full log'],
  deps: () => ['stage dependencies', 'one column per stage; inside a stage, each feature sits to the right of what it waits on · hover to see the path, click to open'],
  roadmap: () => ['stages and features', 'stages and features defined earlier · change a spec with the AI, then tell the loop to redo the affected work'],
  files: () => ['.specs files', 'everything the loop writes in .specs, in one place · read or edit what the app allows'],
};
const pastVisible = (view) => ui.pastN != null && (view === 'turn' || view === 'deps' || view === 'hist');

function pickView(view) {
  if (view === 'hist') { openHist(); return; }
  ui.view = view; renderAll();
  if (view === 'turn' && ui.pastN != null) loadHist();
}

/** What the center shows now: the turn model and the container flags (#zoom-ctl, #past-bar, #legend, canvas mode).
    kind: 'plain' = text flow (history, empty message), 'full' = the view fills the pane, 'dg' = pan/zoom diagram. */
export function centerModel() {
  const view = ui.view, model = view === 'turn' ? agentsModel() : null;
  const kind = view === 'hist' || (model && model.mode === 'plain') ? 'plain' : view === 'turn' ? 'dg' : 'full';
  return { view, model, kind, zoom: view === 'deps' || kind === 'dg', past: pastVisible(view), legend: legendItems(view).length > 0 };
}

/** Content of #center-tabs (role=tablist; arrows handled by bindTabKeys in project_panel.js). */
export function CenterTabs({ view = ui.view }) {
  return TABS.map(([k, id, label]) => html`<button key=${k} type="button" role="tab" id=${id} class="seg-btn" aria-selected=${String(view === k)}
    tabIndex=${view === k ? 0 : -1} onClick=${() => pickView(k)}>${label}</button>`);
}
/** Content of #caption. */
export const Caption = ({ view = ui.view }) => { const [shortText, fullText] = (CAPTIONS[view] || (() => ['', '']))(); return html`<span title=${fullText}>${shortText}</span>`; };
/** Content of #past-bar (the container is hidden outside a snapshot). */
export function PastBar() {
  const last = lastN(), fresh = isNum(last) && isNum(ui.pinLast) && last > ui.pinLast;
  return html`<span>${`Snapshot of turn ${ui.pastN} · read-only`}</span>
    <button type="button" class="btn btn-sm btn-ghost" onClick=${() => backToLive()}>back to the latest turn</button>
    ${fresh ? html`<button type="button" class="btn btn-sm past-new" onClick=${() => backToLive()}>${`new turn ${last} available`}</button>` : null}`;
}
/** Content of #legend. */
export function Legend({ view = ui.view }) {
  return legendItems(view).map((it) => html`<div key=${(it.line ? 'line:' : 'dot:') + it.label}>${it.line
    ? html`<span class=${'sw-line' + (it.dashed ? ' dashed' : '')} style=${{ color: it.color }} />`
    : html`<span class="dot" style=${{ background: it.color }} />`}${it.label}</div>`);
}

/** Content of #canvas for the current view (cm = centerModel()). Each view mounts when shown and unmounts when
    left; its state (pan/zoom, open file and draft) lives in module stores, so coming back restores it. */
export function CenterCanvas({ cm }) {
  if (cm.view === 'hist') return html`<${History} />`;
  if (cm.view === 'deps') return html`<${Dependencies} />`;
  if (cm.view === 'roadmap') return html`<${RoadmapSpecsView} />`;
  if (cm.view === 'files') return html`<${Files} />`;
  if (cm.model.mode === 'plain') return html`<${EmptyMsg} text=${cm.model.msg} />`;
  return html`<${TurnView} model=${cm.model} />`;
}

/** Zoom buttons of #zoom-ctl: the dependency graph or the turn diagram. */
export function zoomCenter(f) {
  if (ui.view === 'deps') { zoomDependencies(f); showZoom(dependenciesScale()); } else zoomAt(f);
}
export function fitCenter() {
  if (ui.view === 'deps') { fitDependencies(); showZoom(dependenciesScale()); } else { resetNodeLayout(); dgFit(); }
}
