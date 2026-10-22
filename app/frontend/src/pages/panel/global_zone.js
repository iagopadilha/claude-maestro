/* GLOBAL zone (blue, left aside): everything that is valid for the whole loop, whatever turn is selected.
   Tabs (registry GLOBAL_TABS): Context (the project trail as a vertical checklist) | Plan & files | Dependencies | Errors (N).
   The plan and its files are read in the Plan & files dialog (plan_files_dialog.js); there is no Files tab.
   The width (resize handle on the right edge) and the show/hide state live in layout.js; the open tab is remembered here. */
import { html, useRef, useEffect } from '../../util/preact.js';
import { bindTabKeys } from '../../util/dom.js';
import { readStored, writeStored } from '../../util/storage.js';
import { list } from '../../util/format.js';
import { ctx, ui } from './context.js';
import { renderAll } from '../project_panel.js';
import { globalZoneHidden, globalDrawerOpen, showGlobalZone } from './layout.js';
import { ProgressTrail } from './progress_trail.js';
import { RoadmapSidebar } from './roadmap_sidebar.js';
import { openPlanFilesDialog } from './plan_files_dialog.js';
import { errorCount } from './errors.js';
import { repoInfo } from './header.js';
import { featuresWithWaves, featureStatusAt, liveN, featureName, FEATURE_COLOR } from './pipeline_model.js';
import { FeatureLabel } from './feature_label.js';
import { openDepsDialog } from './dialogs/index.js';
import { ProblemsView } from './problems.js';

const TAB_KEY = 'harness.global.tab.v1';
const NEUTRAL_COLOR = '#6b727c';
const LEGACY_TAB = { roadmap: 'plan' }; // a tab remembered under its old id

/** Opens one tab of the GLOBAL zone (and the zone itself when it is hidden). `render: false` = the caller is drawing right now. */
export function openGlobalTab(tab, { render = true } = {}) {
  if (!isTab(tab)) return;
  gz.tab = tab; writeStored(TAB_KEY, tab);
  showGlobalZone({ render });
  if (render) renderAll();
}

/** Opens the Plan & files dialog (the plan, the specs, the maps and the decisions, read-only, with the "Ask about the plan" chat). */
export function openRoadmapSpecsDialog() { openPlanFilesDialog(); }

/** Old entry points of the center views (ui.view = 'files' | 'errors' | 'roadmap' | 'deps' | 'hist') now mean "open that tab of the
    GLOBAL zone": Panel() calls this before drawing, so openProjectFile(), openErrors() and the roadmap title keep working.
    'files' and 'roadmap' open the Plan & files dialog (there is no Files tab in loop mode). */
export function consumeViewRequest() {
  const view = ui.view;
  if (view === 'turn') return;
  ui.view = 'turn';
  const planning = !!(ctx.state && ctx.state.planning_required);
  if (view === 'errors') { openGlobalTab(view, { render: false }); return; }
  if (view === 'files' || view === 'roadmap') { if (!planning) { queueMicrotask(openPlanFilesDialog); openGlobalTab('plan', { render: false }); } return; }
  if (view === 'deps') queueMicrotask(openDepsDialog);
}

const DRAG_THRESHOLD_PX = 4;

/** Drag-to-scroll for a strip narrower than its tabs: hold the mouse and drag sideways (no scrollbar, no arrows).
    A drag longer than DRAG_THRESHOLD_PX swallows the click that follows, so it never switches tabs by accident. */
function bindDragScroll(strip) {
  let drag = null, swallowClick = false;
  strip.addEventListener('pointerdown', (event) => {
    if (event.button !== 0 || strip.scrollWidth <= strip.clientWidth) return;
    drag = { x: event.clientX, scroll: strip.scrollLeft, moved: false, id: event.pointerId };
  });
  strip.addEventListener('pointermove', (event) => {
    if (!drag || drag.id !== event.pointerId) return;
    const distance = event.clientX - drag.x;
    if (!drag.moved && Math.abs(distance) < DRAG_THRESHOLD_PX) return;
    if (!drag.moved) { drag.moved = true; strip.setPointerCapture(event.pointerId); strip.classList.add('is-dragging'); }
    strip.scrollLeft = drag.scroll - distance;
  });
  const end = () => { if (drag && drag.moved) swallowClick = true; drag = null; strip.classList.remove('is-dragging'); };
  strip.addEventListener('pointerup', end); strip.addEventListener('pointercancel', end);
  strip.addEventListener('click', (event) => { if (swallowClick) { event.stopPropagation(); event.preventDefault(); swallowClick = false; } }, true);
  strip.addEventListener('wheel', (event) => { // a plain wheel over the strip also scrolls it sideways
    if (strip.scrollWidth <= strip.clientWidth || Math.abs(event.deltaX) > Math.abs(event.deltaY)) return;
    strip.scrollLeft += event.deltaY; event.preventDefault();
  }, { passive: false });
}

/** Tab strip (role=tablist, arrows through bindTabKeys). `tabs` = [[key, label]]; used by the GLOBAL and the LOOP zones. */
export function TabBar({ tabs, current, onPick, label, className = 'z-tabs' }) {
  const bar = useRef(null);
  useEffect(() => { if (bar.current) { bindTabKeys(bar.current); bindDragScroll(bar.current); } }, []);
  return html`<div class=${className} role="tablist" aria-label=${label} ref=${bar}>${tabs.map(([key, text]) => html`<button key=${key} type="button" role="tab" id=${`${className}-${key}`}
    class="z-tab" aria-selected=${String(current === key)} tabIndex=${current === key ? 0 : -1} onClick=${() => onPick(key)}>${text}</button>`)}</div>`;
}

// ---------- tab contents ----------
function ContextTab() {
  const repo = repoInfo();
  const row = (key, value) => (value ? html`<div class="z-kv"><span class="z-kv-k">${key}</span><span class="z-kv-v mono" title=${value}>${value}</span></div>` : null);
  return html`<div class="z-trail"><${ProgressTrail} vertical=${true} /></div>
    ${repo.repo ? html`<div class="z-section"><div class="z-label">WORKSPACE</div>
      ${row('repo', repo.repo)}${row('worktree', repo.worktree)}${row('branch', repo.branch)}</div>` : null}`;
}

function PlanFilesTab() {
  return html`<button type="button" class="z-btn-global" id="btn-open-plan" onClick=${openPlanFilesDialog}>Open plan & files</button>
    <${RoadmapSidebar} />`;
}

function dependenciesByWave() {
  const waves = new Map();
  for (const feature of featuresWithWaves()) {
    const wave = Number.isFinite(feature.wave) ? feature.wave : 1;
    if (!waves.has(wave)) waves.set(wave, []);
    waves.get(wave).push(feature);
  }
  return [...waves.entries()].sort((a, b) => a[0] - b[0]);
}
function DependenciesTab() {
  const waves = dependenciesByWave();
  return html`<button type="button" class="z-btn-global" onClick=${openDepsDialog}>⤢ Open dependency diagram</button>
    ${waves.length ? waves.map(([wave, features]) => html`<div class="z-section" key=${wave}><div class="z-label">${'WAVE ' + wave}</div>
      ${features.map((feature) => {
        const needs = list(feature.deps).filter((dep) => !String(dep).startsWith('dir:'));
        return html`<div class="z-dep" key=${feature.id}>
          <div class="z-dep-id"><span class="z-dot" style=${{ background: FEATURE_COLOR[featureStatusAt(feature.id, liveN())] || NEUTRAL_COLOR }}></span><${FeatureLabel} slug=${feature.id} stacked=${true} block=${true} /></div>
          <div class="z-dep-needs">${needs.length ? 'after ' + needs.map(featureName).join(', ') : 'no dependencies'}</div></div>`;
      })}</div>`) : html`<div class="empty-note">No feature in the roadmap yet.</div>`}`;
}

// every turn of the loop, with the same Errors | Guard blocks split the LOOP zone uses for one turn (problems.js)
const ErrorsTab = () => html`<${ProblemsView} turn=${null} />`;

/** Tab registry: id -> label and component. The order here is the order of the strip. */
const GLOBAL_TABS = [
  { id: 'context', label: 'Context', Body: ContextTab },
  { id: 'plan', label: 'Plan & files', Body: PlanFilesTab },
  { id: 'deps', label: 'Dependencies', Body: DependenciesTab },
  { id: 'errors', label: 'Errors', Body: ErrorsTab, count: errorCount },
];
const isTab = (id) => GLOBAL_TABS.some((tab) => tab.id === id);
const savedTab = readStored(TAB_KEY, null);
const gz = { tab: isTab(savedTab) ? savedTab : (LEGACY_TAB[savedTab] || 'context') };

/** The whole <aside>: always in the DOM (the resize handle #sep-global is bound once), collapsed or drawer-closed through a class. */
export function GlobalZone({ on }) {
  const Body = (GLOBAL_TABS.find((tab) => tab.id === gz.tab) || GLOBAL_TABS[0]).Body;
  const tabs = GLOBAL_TABS.map(({ id, label, count }) => { const n = count ? count() : 0; return [id, n ? `${label} (${n})` : label]; });
  const className = 'z-global' + (globalZoneHidden() ? ' is-collapsed' : '') + (globalDrawerOpen() ? ' is-drawer' : '');
  return html`<aside class=${className} id="global-zone" aria-label="Global context (valid for the whole loop)">
    <div class="z-global-head"><span class="z-badge z-badge-global">GLOBAL</span><span class="z-head-note">valid for the whole loop</span></div>
    <${TabBar} tabs=${tabs} current=${gz.tab} label="Global zone" className="z-gtabs" onPick=${openGlobalTab} />
    <div class="z-global-body" role="tabpanel">${on ? html`<${Body} />` : null}</div>
    <div class="z-resize" id="sep-global" role="separator" aria-orientation="vertical" tabindex="0" aria-label="Global zone width" title="drag, use the arrows or double-click to restore"></div>
  </aside>`;
}
