/* Panel of ONE project (/p/<id>/): port of the loop monitor with URLs relative to the project (/p/<id>/api/...),
   plus the "Where this project stands" trail, the .specs explorer, the graph and the whiteboard. */
import { $, h, bindTabKeys } from '../util/dom.js';
import { createProjectClient, createPolling, showDisconnectedBanner } from '../api/api_client.js';
import { toast } from '../components/toast.js';
import { openWhyOnFirstVisit, whyButton } from '../components/why_panel.js';
import { ctx, ui, askKey, agentById } from './panel/context.js';
import { renderHeader, updateTitle, cancelShutdown } from './panel/header.js';
import { renderAlert, idleText } from './panel/alert.js';
import { nlVisibility, nlOnLoopRunning } from './panel/new_loop.js';
import { renderComposer, initComposer } from './panel/chat.js';
import { renderLeft } from './panel/roadmap_sidebar.js';
import { renderCenter, initCenter, checkRemoteBoard } from './panel/center.js';
import { renderInspector, fetchAgentLog } from './panel/inspector.js';
import { renderTimeline } from './panel/timeline.js';
import { loadHist } from './panel/history.js';
import { initLayout } from './panel/layout.js';
import { dgScale } from './panel/turn_diagram.js';
import { renderTrail, reloadProgress, maybeReloadProgress, initTrail } from './panel/progress_trail.js';

let polling = null, pollCount = 0, prevStatus = null;

export function renderAll() {
  if (!ctx.state) return;
  renderHeader(); renderAlert(); nlVisibility(); renderComposer(); $('idle-note').textContent = idleText(); renderLeft(); renderCenter(); renderInspector(); renderTimeline();
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
  if ((ui.view === 'hist' || ui.pastN != null) && ui.hist.key !== String(s.meta ? s.meta.turns : null)) loadHist();
  renderAll();
  if (ui.sel && ui.sel.kind === 'agent' && !ui.past) {
    const a = agentById(ui.sel.id);
    if (a && a.state === 'running' && pollCount % 3 === 0) fetchAgentLog(a.id);
  }
  if (pollCount % 3 === 0) checkRemoteBoard();
  maybeReloadProgress();
  pollCount += 1;
}

function showNotFound(message) {
  const screen = $('closed');
  screen.replaceChildren(h('div', { class: 'closed-card', role: 'alert' },
    h('div', { class: 'closed-title', text: 'Project not found' }),
    h('div', { class: 'muted', text: message || 'This address does not match a project registered in the app (it may have been forgotten).' }),
    h('a', { class: 'btn btn-primary', href: '/' }, 'Back to the projects')));
  screen.hidden = false;
  document.title = 'project not found · Loop Harness';
}

export function startPanel(root, projectId) {
  const tpl = document.getElementById('tpl-panel');
  root.replaceChildren(tpl.content.cloneNode(true));
  ctx.projectId = projectId;
  ctx.api = createProjectClient(projectId);
  $('header-extra').append(whyButton());
  for (const id of ['center-tabs', 'insp-tabs', 'cp-seg']) bindTabKeys($(id));
  initCenter();
  initLayout();
  const cwrap = document.querySelector('.canvas-wrap');
  if (cwrap && typeof ResizeObserver === 'function') new ResizeObserver(() => dgScale()).observe(cwrap);
  initComposer();
  initTrail();
  renderTrail();
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && ui.confirmShutdown && !document.querySelector('dialog[open]')) cancelShutdown(); });
  updateTitle();

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
      ctx.offline = disconnected;
      showDisconnectedBanner($('banner-offline'), $('banner-offline-detail'), disconnected, msg);
      updateTitle();
    },
  });
  polling.now(0);
  reloadProgress();
  openWhyOnFirstVisit();
}
