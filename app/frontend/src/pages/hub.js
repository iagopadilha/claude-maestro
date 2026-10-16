/* Hub (/): recent projects, open a folder (native dialog or in-app browser), create a project from scratch,
   forget a project, app diagnostics (GET /api/app/info), test notification and shut down the app. */
import { $, h, fill, keepFocus } from '../util/dom.js';
import { DASH, list, txt, fmtRelative } from '../util/format.js';
import { appApi, createPolling, showDisconnectedBanner } from '../api/api_client.js';
import { toast } from '../components/toast.js';
import { openDialog } from '../components/dialog.js';
import { pickFolder } from '../components/folder_browser.js';
import { conformanceField } from '../components/conformance.js';
import { openWhyOnFirstVisit, whyButton } from '../components/why_panel.js';

const NATIVE_DIALOG_TIMEOUT_MS = 310000; // the back end waits up to 300 s for the system window
const MODE = {
  new: ['new project', '#8AA4FF'], no_specs: ['no .specs', '#8B9098'], planning: ['planning', '#A58BFF'],
  ready_for_loop: ['ready for the loop', '#5CC8D6'], in_loop: ['in loop', '#F2B544'], done: ['done', '#5BD38A'],
};
const NEXT_ACTION = {
  structure: 'initialize .specs', conformance: 'choose conformance', map: 'map the code', roadmap: 'generate the roadmap',
  approve: 'approve the roadmap', test_environment: 'describe the test environment', check: 'check the loop', start_loop: 'start the loop', follow: 'follow the loop',
};
const LOOP = {
  running: ['running', '#F2B544', true], awaiting_user: ['waiting for you', '#F2B544', true], continue: ['between turns', '#8AA4FF'],
  done: ['done', '#5BD38A'], error: ['error', '#F06A5E'], stopped: ['stopped', '#C9C4BE'], no_loop: ['no loop', '#8B9098'],
};
const INVALID_NAME = /[\\/:*?"<>|]/;

const hb = { projects: null, error: null, info: null, infoError: null, forgetting: null, busy: {}, opening: null, confirmShutdown: false, native: null };

// ---------- projects ----------
async function loadProjects() {
  const r = await appApi.get('projects', 10000);
  hb.projects = list(r && r.projects); hb.error = null;
  drawProjects();
}
function chip(text, color, pulse) {
  const el = h('span', { class: 'hub-chip' }, h('span', { class: 'dot' + (pulse ? ' pulse' : '') }), h('span', { text }));
  el.style.color = color; el.style.borderColor = color + '55'; el.firstChild.style.background = color;
  return el;
}
function projectCard(p) {
  const ps = p.progress_summary || {};
  const mode = MODE[ps.mode], loop = LOOP[ps.loop_status], c = ps.counts || {};
  const url = p.url || `/p/${encodeURIComponent(p.id)}/`;
  const forgetting = hb.forgetting === p.id;
  const counts = typeof c.total === 'number' && c.total > 0 ? `${c.verified != null ? c.verified : 0}/${c.total} verified${c.blocked ? ` · ${c.blocked} blocked` : ''}` : 'no features yet';
  return h('li', { class: 'hub-project' },
    h('div', { class: 'hub-project-top' },
      p.favorite ? h('span', { class: 'hub-fav', title: 'favorite', 'aria-label': 'favorite', text: '★' }) : null,
      h('a', { class: 'hub-project-name', href: url, text: txt(p.name || p.id) }),
      loop ? chip(loop[0], loop[1], loop[2]) : null,
      mode ? chip(mode[0], mode[1]) : null,
      p.folder_exists === false ? chip('folder missing', '#F06A5E') : null),
    h('div', { class: 'hub-project-path mono', title: p.repo_path || '', text: txt(p.repo_path) }),
    h('div', { class: 'hub-project-meta' },
      h('span', {}, h('span', { class: 'hub-k', text: 'next action ' }), h('strong', { text: NEXT_ACTION[ps.next_action] || txt(ps.next_action) })),
      h('span', { class: 'mono dim', text: counts }),
      h('span', { class: 'dim', text: 'opened ' + fmtRelative(p.last_opened_at) })),
    forgetting
      ? h('div', { class: 'confirm-inline', role: 'group', 'aria-label': 'Confirm forgetting the project' },
        h('span', { text: 'Remove it from the list? No file is deleted.' }),
        h('button', { type: 'button', class: 'btn btn-danger btn-sm', id: 'forget-yes-' + p.id, disabled: !!hb.busy[p.id], onclick: () => forget(p) }, 'Yes, forget'),
        h('button', { type: 'button', class: 'btn btn-sm', id: 'forget-no-' + p.id, onclick: () => { hb.forgetting = null; drawProjects(); focusId('forget-' + p.id); } }, 'Cancel'))
      : h('div', { class: 'hub-project-actions' },
        h('a', { class: 'btn btn-primary btn-sm', href: url }, 'Open panel'),
        h('button', { type: 'button', class: 'btn btn-ghost btn-sm', id: 'forget-' + p.id, onclick: () => { hb.forgetting = p.id; drawProjects(); focusId('forget-no-' + p.id); } }, 'Forget')));
}
const focusId = (id) => { const el = document.getElementById(id); if (el) el.focus(); };
let listSignature = null;
function drawProjects() {
  const target = $('hub-list');
  // the 5 s polling only redraws when something changed (does not steal the focus of keyboard users)
  const signature = JSON.stringify([hb.projects, hb.error, hb.forgetting, hb.busy]);
  if (signature === listSignature) return;
  listSignature = signature;
  let kids;
  if (hb.projects == null) kids = [h('div', { class: 'empty-note', text: hb.error ? 'Could not read the projects: ' + hb.error : 'loading projects…' })];
  else if (!hb.projects.length) kids = [h('div', { class: 'hub-empty' }, h('div', { class: 'hub-empty-t', text: 'No projects yet' }),
    h('div', { class: 'muted', text: 'Open the folder of an existing project (with or without .specs) or create one from scratch. The app remembers the projects you open.' }))];
  else {
    const ord = [...hb.projects].sort((a, b) => (b.favorite ? 1 : 0) - (a.favorite ? 1 : 0) || String(b.last_opened_at || '').localeCompare(String(a.last_opened_at || '')));
    kids = [h('ul', { class: 'hub-projects' }, ord.map(projectCard))];
  }
  keepFocus(() => fill(target, kids));
  $('hub-total').textContent = hb.projects && hb.projects.length ? String(hb.projects.length) : '';
}
async function forget(p) {
  hb.busy[p.id] = true; drawProjects();
  try {
    await appApi.post('projects/' + encodeURIComponent(p.id) + '/forget', {});
    hb.projects = hb.projects.filter((x) => x.id !== p.id); hb.forgetting = null;
    toast(`${p.name || p.id} left the list (the files stay where they were).`, 'ok');
  } catch (e) { toast('Not forgotten: ' + e.message, 'err'); }
  finally { delete hb.busy[p.id]; drawProjects(); }
}

// ---------- open / create ----------
function goToProject(r) {
  const url = (r && r.url) || (r && r.project && r.project.url) || (r && r.project && r.project.id ? `/p/${encodeURIComponent(r.project.id)}/` : null);
  if (!url) { toast('The app opened the project but did not return the panel address.', 'err'); loadProjects().catch(() => {}); return; }
  location.href = url;
}
async function openPath(path) {
  if (!path) return;
  setOpening({ text: 'opening ' + path + '…' });
  try { goToProject(await appApi.post('projects/open', { path }, 60000)); }
  catch (e) { setOpening({ error: e.status === 400 ? `Not opened: ${e.message}. Check that the path is a folder.` : 'Not opened: ' + e.message }); }
}
async function openBrowser() {
  const path = await pickFolder({ title: 'Open a project folder', buttonText: 'Open this folder', explanation: 'Browse to the project folder (or paste the path) and click Open this folder. Folders with .specs already have a loop.' });
  if (path) openPath(path); else setOpening(null);
}
async function openFolder() {
  if (hb.opening && hb.opening.native) return;
  const mine = {};
  hb.native = mine;
  setOpening({ native: true, text: 'Pick the folder in the system window that opened (it may be behind the browser).' });
  try {
    const r = await appApi.post('projects/pick-folder', {}, NATIVE_DIALOG_TIMEOUT_MS);
    if (hb.native !== mine) return; // the user already went through the in-app browser
    hb.native = null;
    if (r && r.path) openPath(r.path);
    else { setOpening(null); openBrowser(); }
  } catch (e) {
    if (hb.native !== mine) return;
    hb.native = null;
    setOpening(null);
    toast('The system window did not open (' + e.message + '); use the in-app folder browser.', 'err');
    openBrowser();
  }
}
function setOpening(v) {
  hb.opening = v;
  const target = $('hub-opening');
  if (!v) { fill(target); target.hidden = true; return; }
  target.hidden = false;
  fill(target, v.error ? h('div', { class: 'nl-box nl-bad', role: 'alert', text: v.error })
    : h('div', { class: 'nl-msg', role: 'status' }, h('div', { class: 'spinner', 'aria-hidden': 'true' }), h('span', { text: v.text }),
      v.native ? h('button', { type: 'button', class: 'btn btn-sm', onclick: () => { hb.native = null; setOpening(null); openBrowser(); } }, 'Use the in-app browser') : null));
}
function createDialog() {
  const parent = h('input', { type: 'text', id: 'cr-parent', class: 'dlg-input mono', autocomplete: 'off', spellcheck: 'false', placeholder: 'C:\\Users\\you\\projects' });
  const name = h('input', { type: 'text', id: 'cr-name', class: 'dlg-input mono', autocomplete: 'off', spellcheck: 'false', placeholder: 'my-app' });
  const git = h('input', { type: 'checkbox', id: 'cr-git' }); git.checked = true;
  const conf = conformanceField({ allowLater: true });
  const preview = h('div', { class: 'hint mono', 'aria-live': 'polite' });
  const error = h('div', { class: 'nl-err', role: 'alert' });
  const refresh = () => {
    const p = parent.value.trim(), n = name.value.trim();
    const sep = p.includes('/') && !p.includes('\\') ? '/' : '\\';
    preview.textContent = p && n ? 'Will create: ' + p.replace(/[\\/]+$/, '') + sep + n : '';
    error.textContent = n && INVALID_NAME.test(n) ? 'The name cannot contain \\ / : * ? " < > |' : '';
  };
  parent.addEventListener('input', refresh); name.addEventListener('input', refresh);
  const choose = h('button', { type: 'button', class: 'btn btn-sm', onclick: async () => { const c = await pickFolder({ title: 'Parent folder of the new project', buttonText: 'Use this folder', initial: parent.value.trim() }); if (c) { parent.value = c; refresh(); } name.focus(); } }, 'Choose…');
  openDialog({
    title: 'Create a project from scratch', width: 600,
    content: [
      h('p', { class: 'dlg-text', text: 'Creates the project folder, the template .specs and (if checked) git with an initial commit. Refuses if the folder already exists with content.' }),
      h('div', { class: 'dlg-field' }, h('label', { class: 'nl-label', for: 'cr-parent', text: 'Parent folder' }), h('div', { class: 'fb-bar' }, parent, choose)),
      h('div', { class: 'dlg-field' }, h('label', { class: 'nl-label', for: 'cr-name', text: 'Project name (becomes the folder name)' }), name, error),
      preview,
      h('label', { class: 'nl-check', for: 'cr-git' }, git, h('span', { text: 'Initialize git (git init + initial commit)' })),
      conf.element,
    ],
    actions: [
      { text: 'Cancel' },
      { text: 'Create project', cls: 'btn-primary', onClick: async (d) => {
        const p = parent.value.trim(), n = name.value.trim();
        if (!p) { error.textContent = 'Choose the parent folder.'; parent.focus(); return; }
        if (!n || INVALID_NAME.test(n)) { error.textContent = n ? 'The name cannot contain \\ / : * ? " < > |' : 'Give the project a name.'; name.focus(); return; }
        d.busy(true);
        try { const r = await appApi.post('projects/create', { parent_path: p, name: n, init_git: !!git.checked, conformance: conf.value() }, 60000); d.close(); toast('Project created.', 'ok'); goToProject(r); }
        catch (e) { error.textContent = 'Not created: ' + e.message; d.busy(false); }
      } },
    ],
  });
}

// ---------- diagnostics ----------
function diagRow(label, ok, detail) {
  const state = ok === true ? ['ok', 'ok'] : ok === false ? ['missing', 'bad'] : [DASH, 'na'];
  return h('div', { class: 'diag-item ' + state[1] }, h('span', { class: 'diag-ic', 'aria-hidden': 'true', text: ok === true ? '✓' : ok === false ? '✗' : '·' }),
    h('span', { class: 'diag-k', text: label }), h('span', { class: 'sr-only', text: ': ' + state[0] + '. ' }), h('span', { class: 'diag-v mono', title: detail || '', text: detail || '' }));
}
const bool = (v) => (typeof v === 'boolean' ? v : v == null ? null : !!v);
function enabledChannels(v) {
  if (Array.isArray(v)) return v.filter((x) => typeof x === 'string' || (x && x.enabled !== false)).map((x) => (typeof x === 'string' ? x : x && x.name)).filter(Boolean);
  if (v && typeof v === 'object') return Object.entries(v).filter(([, on]) => on).map(([k]) => k);
  return [];
}
function drawDiagnostics() {
  const target = $('hub-diag');
  const r = hb.info;
  if (!r) { fill(target, h('span', { class: 'hint', text: hb.infoError ? 'Diagnostics unavailable: ' + hb.infoError : 'reading diagnostics…' })); return; }
  const guard = r.guard || {}, claude = r.claude || {};
  const skillDir = r.skill_dir;
  const guardOk = bool(guard.blocks_git_push != null ? guard.blocks_git_push : guard.found);
  const claudeOk = bool(claude.on_path);
  const claudeDetail = claude.binary_configured;
  const channels = enabledChannels(r.notify_channels);
  const problems = list(r.problems);
  const kids = [
    diagRow('/orchestrator skill', skillDir ? true : null, skillDir ? String(skillDir) : ''),
    diagRow('guard blocking git push', guardOk, guard.path ? String(guard.path) : ''),
    diagRow('claude on PATH', claudeOk, claudeDetail ? String(claudeDetail) : ''),
    diagRow('notification channels', channels.length ? true : null, channels.length ? channels.join(', ') : 'none configured'),
    r.data_dir ? diagRow('data folder', null, String(r.data_dir)) : null,
    ...problems.map((p) => diagRow('problem', false, txt(p))),
  ];
  const known = new Set(['skill_dir', 'guard', 'claude', 'notify_channels', 'data_dir', 'version', 'seconds_until_shutdown', 'problems', 'ok', 'pid', 'url']);
  const rest = Object.entries(r).filter(([k, v]) => !known.has(k) && (v == null || typeof v !== 'object'));
  const shutdownIn = r.seconds_until_shutdown;
  fill(target,
    h('div', { class: 'diag-grid' }, kids),
    h('div', { class: 'diag-foot' },
      h('span', { class: 'dim mono', text: [r.version ? 'version ' + r.version : '', typeof shutdownIn === 'number' ? `shuts down by itself in ${Math.round(shutdownIn / 60)} min without use` : ''].filter(Boolean).join(' · ') }),
      rest.length ? h('details', { class: 'diag-more' }, h('summary', { text: 'more details' }), h('div', { class: 'diag-grid' }, rest.map(([k, v]) => diagRow(k, typeof v === 'boolean' ? v : null, typeof v === 'boolean' ? '' : txt(v))))) : null));
}
async function loadInfo() {
  try { hb.info = await appApi.get('info', 15000); hb.infoError = null; }
  catch (e) { hb.infoError = e.message; }
  drawDiagnostics();
}
async function testNotification() {
  const b = $('hub-test'); b.disabled = true; b.textContent = 'sending…';
  try {
    const r = await appApi.post('notify/test', {}, 90000);
    const results = r.results && typeof r.results === 'object' ? Object.entries(r.results).map(([k, v]) => `${k}: ${v}`).join(' · ') : '';
    toast('Test notification sent' + (results ? ': ' + results.slice(0, 160) : '.'), 'ok');
  } catch (e) { toast('The test notification failed: ' + e.message, 'err'); }
  finally { b.disabled = false; b.textContent = 'Test notification'; }
}
function drawShutdown() {
  const target = $('hub-shutdown');
  if (hb.confirmShutdown) {
    fill(target, h('div', { class: 'confirm-inline', role: 'group', 'aria-label': 'Confirm shutting down the app' },
      h('span', { text: 'Shut down the app? Open panels stop updating. Loops already started keep running as separate processes.' }),
      h('button', { type: 'button', class: 'btn btn-danger btn-sm', id: 'shut-yes', onclick: shutDown }, 'Yes, shut down'),
      h('button', { type: 'button', class: 'btn btn-sm', id: 'shut-no', onclick: () => { hb.confirmShutdown = false; drawShutdown(); focusId('shut-open'); } }, 'Cancel')));
    focusId('shut-no');
  } else {
    fill(target, h('button', { type: 'button', class: 'btn btn-ghost', id: 'shut-open', onclick: () => { hb.confirmShutdown = true; drawShutdown(); } }, 'Shut down app'));
  }
}
let hubPolling = null;
async function shutDown() {
  const b = $('shut-yes'); if (b) b.disabled = true;
  try {
    await appApi.post('shutdown', {}, 15000);
    if (hubPolling) hubPolling.stop();
    $('closed').hidden = false;
    document.title = 'app shut down · Loop Harness';
  } catch (e) { toast('Could not shut down: ' + e.message, 'err'); hb.confirmShutdown = false; drawShutdown(); }
}

export function startHub(root) {
  const tpl = document.getElementById('tpl-hub');
  root.replaceChildren(tpl.content.cloneNode(true));
  document.title = 'projects · Loop Harness';
  $('hub-help').replaceWith(whyButton());
  $('hub-open').addEventListener('click', openFolder);
  $('hub-browse').addEventListener('click', openBrowser);
  $('hub-create').addEventListener('click', createDialog);
  $('hub-test').addEventListener('click', testNotification);
  $('hub-paste').addEventListener('submit', (e) => { e.preventDefault(); const c = $('hub-paste-field').value.trim(); if (c) openPath(c); });
  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape') return;
    if (hb.confirmShutdown) { hb.confirmShutdown = false; drawShutdown(); focusId('shut-open'); }
    else if (hb.forgetting) { const id = hb.forgetting; hb.forgetting = null; drawProjects(); focusId('forget-' + id); }
  });
  drawProjects(); drawDiagnostics(); drawShutdown();
  hubPolling = createPolling({
    intervalMs: 5000,
    task: async () => {
      try { await loadProjects(); } catch (e) { hb.error = e.message; if (hb.projects == null) drawProjects(); throw e; }
    },
    onConnectionChange: (disc, msg) => showDisconnectedBanner($('banner-offline'), $('banner-offline-detail'), disc, msg),
  });
  hubPolling.now(0);
  loadInfo();
  openWhyOnFirstVisit();
}
