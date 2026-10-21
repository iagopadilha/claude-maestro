/* Panel of ONE project (/p/<id>/): port of the loop monitor with URLs relative to the project (/p/<id>/api/...),
   with a GLOBAL zone (trail, roadmap, dependencies, errors, .specs files) and a LOOP zone (turns, pipeline, inspector).
   The whole page is one Preact tree (<Panel>, panel/app.js): handlers mutate ctx/ui and call renderAll(). */
import { $, bindTabKeys } from '../util/dom.js';
import { html, render } from '../util/preact.js';
import { createProjectClient, createPolling } from '../api/api_client.js';
import { toast } from '../components/toast.js';
import { openWhyOnFirstVisit } from '../components/why_panel.js';
import { ctx, ui, askKey, agentById } from './panel/context.js';
import { Panel } from './panel/app.js';
import { updateTitle, cancelShutdown } from './panel/header.js';
import { nlOnLoopRunning } from './panel/new_loop.js';
import { fetchAgentLog } from './panel/inspector.js';
import { loadErrors } from './panel/errors.js';
import { initLayout } from './panel/layout.js';
import { maybeReloadPipeline } from './panel/pipeline_model.js';
import { reloadProgress, maybeReloadProgress } from './panel/progress_trail.js';

let polling = null, pollCount = 0, prevStatus = null, root = null, queued = false;
const extraRenderers = new Set();

/** Registers a function renderAll() calls after the panel is drawn (a big dialog is a second Preact root that must follow the
    shared stores). Returns the function that unregisters it. */
export function registerRenderer(renderer) {
  extraRenderers.add(renderer);
  return () => extraRenderers.delete(renderer);
}

/** Renders the whole panel (Preact only touches what changed). */
export function renderAll() {
  if (!root) return;
  render(html`<${Panel} />`, root);
  updateTitle();
  for (const renderer of [...extraRenderers]) {
    try { renderer(); } catch (_) { extraRenderers.delete(renderer); }
  }
}

/** renderAll() after the current task (coalesced): for callers that may run inside a component effect. */
export function requestRender() {
  if (queued) return;
  queued = true;
  queueMicrotask(() => { queued = false; renderAll(); });
}

/** Stops the polling (app shut down or unknown project). */
export function stopPanel() { if (polling) polling.stop(); }

/** Requests the next /state in `ms` (after a user action). */
export function schedulePoll(ms = 0) { if (polling) polling.now(ms); }

function applyState(s) {
  const prev = prevStatus;
  ctx.state = s;
  const cur = (s.loop && s.loop.status) || 'no_loop';
  if (cur !== 'running') ui.pausePending = false;
  if (cur === 'running') nlOnLoopRunning();
  if (ui.resuming && cur === 'running') { ui.resuming = null; toast('Loop resumed.', 'ok'); }
  else if (ui.resuming && ui.resuming.key !== askKey()) ui.resuming = null;
  if (prev && prev !== cur && cur === 'awaiting_user') toast('The loop needs an answer from you.', 'ok');
  if (prev && prev !== cur) reloadProgress();
  prevStatus = cur;
  loadErrors(); // cheap: re-reads only when the turn count, the status or the guard total changed
  maybeReloadPipeline(s); // re-reads GET pipeline when the turns, the status or the turn changed (and every ~10 s while running)
  renderAll();
  if (ui.sel && ui.sel.kind === 'agent' && !ui.past) {
    const a = agentById(ui.sel.id);
    if (a && a.state === 'running' && pollCount % 3 === 0) fetchAgentLog(a.id);
  }
  maybeReloadProgress();
  pollCount += 1;
}

function showNotFound(message) {
  ui.closed = { kind: 'notfound', message };
  renderAll();
}

export function startPanel(rootEl, projectId) {
  root = rootEl;
  root.replaceChildren();
  ctx.projectId = projectId;
  ctx.api = createProjectClient(projectId);
  renderAll();
  // one-time setup on the DOM of the first render (the Panel never re-creates these elements)
  const intentTabs = $('cp-seg'); // the tab strips of the zones are components that bind their own keys
  if (intentTabs) bindTabKeys(intentTabs);
  initLayout();
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && ui.confirmShutdown && !document.querySelector('dialog[open]')) cancelShutdown(); });

  polling = createPolling({
    task: async () => {
      try {
        const s = await ctx.api.get('state?visible=1');
        applyState(s);
      } catch (e) {
        if (e.status === 404 && /project/i.test(e.message)) { showNotFound(); e.stopPolling = true; }
        throw e;
      }
    },
    onConnectionChange: (disconnected, msg) => {
      ctx.offline = disconnected; ctx.offlineMsg = msg;
      renderAll();
    },
  });
  polling.now(0);
  reloadProgress();
  openWhyOnFirstVisit();
}
