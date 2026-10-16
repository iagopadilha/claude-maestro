/* Panel header: project/branch, status pill, limits, PR, pause and Shut down app.
   Shutting down belongs to the app: the button calls POST /api/app/shutdown (the project shutdown is a no-op). */
import { $, h, fill, keepFocus } from '../../util/dom.js';
import { DASH, isNum, num, txt, list, httpUrl } from '../../util/format.js';
import { appApi } from '../../api/api_client.js';
import { toast } from '../../components/toast.js';
import { ctx, ui, sigs, region, STATUS, status, loop, pauseRequested, projectName } from './context.js';
import { schedulePoll, stopPanel } from '../project_panel.js';
import { renderComposer } from './chat.js';

const DOLLAR_UNITS = ['US$', 'USD', '$'];

function limitColor(l) {
  if (isNum(l.pct) && l.pct >= 85) return '#F06A5E';
  return { cost: '#F2B544', turns_without_progress: '#5BD38A', turn_minutes: '#8AA4FF' }[l.key] || '#A58BFF';
}

export function renderHeader() {
  const state = ctx.state;
  const s = status(), st = STATUS[s] || STATUS.no_loop, l = loop();
  const turn = isNum(l.turn) ? l.turn : null;
  const m = (state && state.meta) || {};
  region('header', { s, turn, p: projectName(), b: state && state.branch, lim: state && state.limits, pr: [m.pr, m.pr_status], pp: pauseRequested(), busy: [ui.busy.pause, ui.busy.shutdown], cs: ui.confirmShutdown, cu: m.cost_usd }, () => {
    $('brand-sub').textContent = `${txt(projectName())} · ${txt((state && state.branch) || m.branch)}`;
    $('brand-sub').title = (state && state.project_ref && state.project_ref.repo_path) || (state && state.repo) || '';
    const pill = $('status-pill');
    pill.style.background = st.bg; pill.style.borderColor = st.line;
    $('status-text').textContent = st.label + (turn != null ? ` · turn ${turn}` : '');
    $('status-text').style.color = st.c;
    const dot = $('status-dot'); dot.style.background = st.c; dot.className = 'dot' + (st.pulse ? ' pulse' : '');

    const limits = list(state && state.limits).map((x) => {
      const c = limitColor(x);
      const pctW = isNum(x.pct) ? Math.max(0, Math.min(100, x.pct)) : 0;
      const val = isNum(x.value) ? (DOLLAR_UNITS.includes(x.unit) ? '$' + num(x.value) : num(x.value) + (x.unit ? ' ' + x.unit : '')) : DASH;
      const mx = isNum(x.max) ? num(x.max) : DASH;
      const bar = h('div', { class: 'bar' }, h('div', {}));
      bar.firstChild.style.width = pctW + '%'; bar.firstChild.style.background = c;
      return h('div', { class: 'limit' },
        h('div', { class: 'limit-label', text: x.label || x.key }),
        h('div', { class: 'limit-val' }, h('span', { text: val }), h('span', { class: 'of', text: ' / ' + mx })),
        bar);
    });
    fill($('limits'), limits);

    const acts = [];
    const prU = httpUrl(m.pr);
    if (prU) {
      const n = prU.match(/\/pull\/(\d+)/);
      acts.push(h('a', { class: 'btn btn-sm pr-chip', href: prU, target: '_blank', rel: 'noopener noreferrer', title: 'open pull request' + (m.pr_status ? ' · ' + m.pr_status : '') },
        'PR ' + (n ? '#' + n[1] : '↗') + (m.pr_status ? ' · ' + m.pr_status : '')));
    }
    if (s === 'running') {
      acts.push(pauseRequested()
        ? h('button', { type: 'button', class: 'btn btn-pending', disabled: true, id: 'btn-pause' }, 'Pausing after the turn…')
        : h('button', { type: 'button', class: 'btn', id: 'btn-pause', disabled: !!ui.busy.pause, onclick: doPause }, 'Pause after the turn'));
    }
    if (ui.confirmShutdown) {
      acts.push(h('div', { class: 'confirm-inline', role: 'group', 'aria-label': 'Confirm shutting down the app' },
        h('span', { text: 'Shut down the app? Loops already started keep running as separate processes.' }),
        h('button', { type: 'button', class: 'btn btn-danger btn-sm', id: 'btn-shutdown-yes', disabled: !!ui.busy.shutdown, onclick: doShutdown }, 'Yes, shut down'),
        h('button', { type: 'button', class: 'btn btn-sm', id: 'btn-shutdown-no', onclick: cancelShutdown }, 'Cancel')));
    } else {
      acts.push(h('button', { type: 'button', class: 'btn btn-ghost', id: 'btn-shutdown', title: 'shuts down the whole app (every project)', onclick: () => { ui.confirmShutdown = true; renderHeader(); const b = $('btn-shutdown-no'); if (b) b.focus(); } }, 'Shut down app'));
    }
    keepFocus(() => fill($('header-actions'), acts));
  });
  updateTitle();
  renderComposer();
}

export function cancelShutdown() {
  if (!ui.confirmShutdown) return;
  ui.confirmShutdown = false; sigs.header = null; renderHeader();
  const b = $('btn-shutdown'); if (b) b.focus();
}

export function updateTitle() {
  const state = ctx.state;
  if (!state) { document.title = (ctx.offline ? 'disconnected' : 'loading') + ' · Loop Harness'; return; }
  const st = STATUS[status()] || STATUS.no_loop, v = loop().turn;
  document.title = `${ctx.offline ? 'disconnected · ' : ''}${st.label}${isNum(v) ? ' · turn ' + v : ''} · ${projectName() || 'project'}`;
}

async function doPause() {
  ui.busy.pause = true; renderHeader();
  try { await ctx.api.post('pause', {}); ui.pausePending = true; toast('Pause requested: the loop stops at the end of this turn.', 'ok'); schedulePoll(0); }
  catch (e) { toast('Could not pause: ' + e.message, 'err'); }
  finally { ui.busy.pause = false; renderHeader(); }
}

async function doShutdown() {
  ui.busy.shutdown = true; renderHeader();
  try {
    await appApi.post('shutdown', {}, 15000);
    stopPanel();
    const screen = $('closed');
    screen.replaceChildren(h('div', { class: 'closed-card', role: 'alert' }, h('div', { class: 'closed-title', text: 'App shut down' }),
      h('div', { class: 'muted', text: 'The server was shut down. You can close this tab. Loops already started keep running.' })));
    screen.hidden = false;
    document.title = 'app shut down · Loop Harness';
  } catch (e) { toast('Could not shut down: ' + e.message, 'err'); ui.confirmShutdown = false; }
  finally { ui.busy.shutdown = false; renderHeader(); }
}
