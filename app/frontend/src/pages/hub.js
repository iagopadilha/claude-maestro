/* Hub (/): recent projects, open a folder (native dialog or in-app browser), create a project from scratch,
   forget a project, app diagnostics (GET /api/app/info), test notification and shut down the app. */
import { $ } from '../util/dom.js';
import { html, render, useState, useRef } from '../util/preact.js';
import { DASH, list, txt, fmtRelative } from '../util/format.js';
import { appApi, createPolling, showDisconnectedBanner } from '../api/api_client.js';
import { toast } from '../components/toast.js';
import { openDialog } from '../components/dialog.js';
import { pickFolder } from '../components/folder_browser.js';
import { Conformance, conformanceDefault } from '../components/conformance.js';
import { openWhyOnFirstVisit, WhyButton } from '../components/why_panel.js';

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
const INVALID_NAME_MSG = 'The name cannot contain \\ / : * ? " < > |';

// store of the page: handlers mutate it and call draw() (Preact only touches what changed, so the focus stays)
const hb = {
  projects: null, error: null, info: null, infoError: null, forgetting: null, busy: {}, opening: null, confirmShutdown: false, native: null,
  testing: false, shuttingDown: false, closed: false,
};
let hubRoot = null;
const draw = () => { if (hubRoot) render(html`<${Hub} />`, hubRoot); };
const focusId = (id) => { const el = document.getElementById(id); if (el) el.focus(); };

// ---------- projects ----------
async function loadProjects() {
  const r = await appApi.get('projects', 10000);
  hb.projects = list(r && r.projects); hb.error = null;
  draw();
}
function Chip({ text, color, pulse }) {
  return html`<span class="hub-chip" style=${{ color, borderColor: color + '55' }}><span class=${'dot' + (pulse ? ' pulse' : '')} style=${{ background: color }} /><span>${text}</span></span>`;
}
function ProjectCard({ p }) {
  const ps = p.progress_summary || {};
  const mode = MODE[ps.mode], loop = LOOP[ps.loop_status], c = ps.counts || {};
  const url = p.url || `/p/${encodeURIComponent(p.id)}/`;
  const counts = typeof c.total === 'number' && c.total > 0 ? `${c.verified != null ? c.verified : 0}/${c.total} verified${c.blocked ? ` · ${c.blocked} blocked` : ''}` : 'no features yet';
  const askForget = () => { hb.forgetting = p.id; draw(); focusId('forget-no-' + p.id); };
  const cancelForget = () => { hb.forgetting = null; draw(); focusId('forget-' + p.id); };
  return html`<li class="hub-project">
    <div class="hub-project-top">
      ${p.favorite ? html`<span class="hub-fav" title="favorite" aria-label="favorite">★</span>` : null}
      <a class="hub-project-name" href=${url}>${txt(p.name || p.id)}</a>
      ${loop ? html`<${Chip} text=${loop[0]} color=${loop[1]} pulse=${loop[2]} />` : null}
      ${mode ? html`<${Chip} text=${mode[0]} color=${mode[1]} />` : null}
      ${p.folder_exists === false ? html`<${Chip} text="folder missing" color="#F06A5E" />` : null}
    </div>
    <div class="hub-project-path mono" title=${p.repo_path || ''}>${txt(p.repo_path)}</div>
    <div class="hub-project-meta">
      <span><span class="hub-k">next action </span><strong>${NEXT_ACTION[ps.next_action] || txt(ps.next_action)}</strong></span>
      <span class="mono dim">${counts}</span>
      <span class="dim">${'opened ' + fmtRelative(p.last_opened_at)}</span>
    </div>
    ${hb.forgetting === p.id
      ? html`<div class="confirm-inline" role="group" aria-label="Confirm forgetting the project">
          <span>Remove it from the list? No file is deleted.</span>
          <button type="button" class="btn btn-danger btn-sm" id=${'forget-yes-' + p.id} disabled=${!!hb.busy[p.id]} onClick=${() => forget(p)}>Yes, forget</button>
          <button type="button" class="btn btn-sm" id=${'forget-no-' + p.id} onClick=${cancelForget}>Cancel</button>
        </div>`
      : html`<div class="hub-project-actions">
          <a class="btn btn-primary btn-sm" href=${url}>Open panel</a>
          <button type="button" class="btn btn-ghost btn-sm" id=${'forget-' + p.id} onClick=${askForget}>Forget</button>
        </div>`}
  </li>`;
}
/** Content of #hub-list. */
function ProjectList() {
  if (hb.projects == null) return html`<div class="empty-note">${hb.error ? 'Could not read the projects: ' + hb.error : 'loading projects…'}</div>`;
  if (!hb.projects.length) {
    return html`<div class="hub-empty"><div class="hub-empty-t">No projects yet</div>
      <div class="muted">Open the folder of an existing project (with or without .specs) or create one from scratch. The app remembers the projects you open.</div></div>`;
  }
  const ord = [...hb.projects].sort((a, b) => (b.favorite ? 1 : 0) - (a.favorite ? 1 : 0) || String(b.last_opened_at || '').localeCompare(String(a.last_opened_at || '')));
  return html`<ul class="hub-projects">${ord.map((p) => html`<${ProjectCard} key=${p.id} p=${p} />`)}</ul>`;
}
async function forget(p) {
  hb.busy[p.id] = true; draw();
  try {
    await appApi.post('projects/' + encodeURIComponent(p.id) + '/forget', {});
    hb.projects = hb.projects.filter((x) => x.id !== p.id); hb.forgetting = null;
    toast(`${p.name || p.id} left the list (the files stay where they were).`, 'ok');
  } catch (e) { toast('Not forgotten: ' + e.message, 'err'); }
  finally { delete hb.busy[p.id]; draw(); }
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
function setOpening(v) { hb.opening = v; draw(); }
/** Content of #hub-opening (hidden when nothing is opening). */
function Opening({ v }) {
  if (!v) return null;
  if (v.error) return html`<div class="nl-box nl-bad" role="alert">${v.error}</div>`;
  return html`<div class="nl-msg" role="status"><div class="spinner" aria-hidden="true" /><span>${v.text}</span>
    ${v.native ? html`<button type="button" class="btn btn-sm" onClick=${() => { hb.native = null; setOpening(null); openBrowser(); }}>Use the in-app browser</button>` : null}</div>`;
}

/** Body of the "Create a project from scratch" dialog; handle.submit(dialogApi) is the main action. */
function CreateProjectForm({ handle }) {
  const [parent, setParent] = useState('');
  const [name, setName] = useState('');
  const [git, setGit] = useState(true);
  const [conf, setConf] = useState(() => conformanceDefault(null, true));
  const [submitError, setSubmitError] = useState(''); // cleared by any edit, like the live name check
  const parentRef = useRef(null), nameRef = useRef(null);
  const p = parent.trim(), n = name.trim();
  const error = submitError || (n && INVALID_NAME.test(n) ? INVALID_NAME_MSG : '');
  const editParent = (v) => { setParent(v); setSubmitError(''); };
  const editName = (v) => { setName(v); setSubmitError(''); };
  const sep = p.includes('/') && !p.includes('\\') ? '/' : '\\';
  const choose = async () => {
    const c = await pickFolder({ title: 'Parent folder of the new project', buttonText: 'Use this folder', initial: p });
    if (c) editParent(c);
    if (nameRef.current) nameRef.current.focus();
  };
  handle.submit = async (d) => {
    if (!p) { setSubmitError('Choose the parent folder.'); parentRef.current.focus(); return; }
    if (!n || INVALID_NAME.test(n)) { setSubmitError(n ? INVALID_NAME_MSG : 'Give the project a name.'); nameRef.current.focus(); return; }
    d.busy(true);
    try { const r = await appApi.post('projects/create', { parent_path: p, name: n, init_git: !!git, conformance: conf }, 60000); d.close(); toast('Project created.', 'ok'); goToProject(r); }
    catch (e) { setSubmitError('Not created: ' + e.message); d.busy(false); }
  };
  return html`
    <p class="dlg-text">Creates the project folder, the template .specs and (if checked) git with an initial commit. Refuses if the folder already exists with content.</p>
    <div class="dlg-field"><label class="nl-label" for="cr-parent">Parent folder</label>
      <div class="fb-bar">
        <input type="text" id="cr-parent" class="dlg-input mono" autocomplete="off" spellcheck="false" placeholder=${'C:\\Users\\you\\projects'} ref=${parentRef}
          value=${parent} onInput=${(e) => editParent(e.currentTarget.value)} />
        <button type="button" class="btn btn-sm" onClick=${choose}>Choose…</button>
      </div>
    </div>
    <div class="dlg-field"><label class="nl-label" for="cr-name">Project name (becomes the folder name)</label>
      <input type="text" id="cr-name" class="dlg-input mono" autocomplete="off" spellcheck="false" placeholder="my-app" ref=${nameRef}
        value=${name} onInput=${(e) => editName(e.currentTarget.value)} />
      <div class="nl-err" role="alert">${error}</div>
    </div>
    <div class="hint mono" aria-live="polite">${p && n ? 'Will create: ' + p.replace(/[\\/]+$/, '') + sep + n : ''}</div>
    <label class="nl-check" for="cr-git"><input type="checkbox" id="cr-git" checked=${git} onChange=${(e) => setGit(e.currentTarget.checked)} /><span>Initialize git (git init + initial commit)</span></label>
    <${Conformance} allowLater=${true} onChange=${setConf} />`;
}
function createDialog() {
  const handle = {};
  openDialog({
    title: 'Create a project from scratch', width: 600,
    content: html`<${CreateProjectForm} handle=${handle} />`,
    actions: [
      { text: 'Cancel' },
      { text: 'Create project', cls: 'btn-primary', onClick: (d) => handle.submit(d) },
    ],
  });
}

// ---------- diagnostics ----------
function DiagRow({ label, ok, detail }) {
  const state = ok === true ? ['ok', 'ok'] : ok === false ? ['missing', 'bad'] : [DASH, 'na'];
  return html`<div class=${'diag-item ' + state[1]}><span class="diag-ic" aria-hidden="true">${ok === true ? '✓' : ok === false ? '✗' : '·'}</span>
    <span class="diag-k">${label}</span><span class="sr-only">${': ' + state[0] + '. '}</span><span class="diag-v mono" title=${detail || ''}>${detail || ''}</span></div>`;
}
const row = (label, ok, detail, key = label) => html`<${DiagRow} key=${key} label=${label} ok=${ok} detail=${detail} />`;
const bool = (v) => (typeof v === 'boolean' ? v : v == null ? null : !!v);
function enabledChannels(v) {
  if (Array.isArray(v)) return v.filter((x) => typeof x === 'string' || (x && x.enabled !== false)).map((x) => (typeof x === 'string' ? x : x && x.name)).filter(Boolean);
  if (v && typeof v === 'object') return Object.entries(v).filter(([, on]) => on).map(([k]) => k);
  return [];
}
const KNOWN_INFO = new Set(['skill_dir', 'guard', 'claude', 'notify_channels', 'data_dir', 'version', 'seconds_until_shutdown', 'problems', 'ok', 'pid', 'url']);
/** Content of #hub-diag. */
function Diagnostics({ r, error }) {
  if (!r) return html`<span class="hint">${error ? 'Diagnostics unavailable: ' + error : 'reading diagnostics…'}</span>`;
  const guard = r.guard || {}, claude = r.claude || {};
  const skillDir = r.skill_dir;
  const guardOk = bool(guard.blocks_git_push != null ? guard.blocks_git_push : guard.found);
  const channels = enabledChannels(r.notify_channels);
  const rest = Object.entries(r).filter(([k, v]) => !KNOWN_INFO.has(k) && (v == null || typeof v !== 'object'));
  const shutdownIn = r.seconds_until_shutdown;
  return html`
    <div class="diag-grid">
      ${row('/orchestrator skill', skillDir ? true : null, skillDir ? String(skillDir) : '')}
      ${row('guard blocking git push', guardOk, guard.path ? String(guard.path) : '')}
      ${row('claude on PATH', bool(claude.on_path), claude.binary_configured ? String(claude.binary_configured) : '')}
      ${row('notification channels', channels.length ? true : null, channels.length ? channels.join(', ') : 'none configured')}
      ${r.data_dir ? row('data folder', null, String(r.data_dir)) : null}
      ${list(r.problems).map((p, i) => row('problem', false, txt(p), 'problem-' + i))}
    </div>
    <div class="diag-foot">
      <span class="dim mono">${[r.version ? 'version ' + r.version : '', typeof shutdownIn === 'number' ? `shuts down by itself in ${Math.round(shutdownIn / 60)} min without use` : ''].filter(Boolean).join(' · ')}</span>
      ${rest.length ? html`<details class="diag-more"><summary>more details</summary>
        <div class="diag-grid">${rest.map(([k, v]) => row(k, typeof v === 'boolean' ? v : null, typeof v === 'boolean' ? '' : txt(v)))}</div></details>` : null}
    </div>`;
}
async function loadInfo() {
  try { hb.info = await appApi.get('info', 15000); hb.infoError = null; }
  catch (e) { hb.infoError = e.message; }
  draw();
}
async function testNotification() {
  hb.testing = true; draw();
  try {
    const r = await appApi.post('notify/test', {}, 90000);
    const results = r.results && typeof r.results === 'object' ? Object.entries(r.results).map(([k, v]) => `${k}: ${v}`).join(' · ') : '';
    toast('Test notification sent' + (results ? ': ' + results.slice(0, 160) : '.'), 'ok');
  } catch (e) { toast('The test notification failed: ' + e.message, 'err'); }
  finally { hb.testing = false; draw(); }
}
const askShutdown = () => { hb.confirmShutdown = true; draw(); focusId('shut-no'); };
const cancelShutdown = () => { hb.confirmShutdown = false; draw(); focusId('shut-open'); };
/** Content of #hub-shutdown. */
function Shutdown() {
  if (!hb.confirmShutdown) return html`<button type="button" class="btn btn-ghost" id="shut-open" onClick=${askShutdown}>Shut down app</button>`;
  return html`<div class="confirm-inline" role="group" aria-label="Confirm shutting down the app">
    <span>Shut down the app? Open panels stop updating. Loops already started keep running as separate processes.</span>
    <button type="button" class="btn btn-danger btn-sm" id="shut-yes" disabled=${hb.shuttingDown} onClick=${shutDown}>Yes, shut down</button>
    <button type="button" class="btn btn-sm" id="shut-no" onClick=${cancelShutdown}>Cancel</button>
  </div>`;
}
let hubPolling = null;
async function shutDown() {
  hb.shuttingDown = true; draw();
  try {
    await appApi.post('shutdown', {}, 15000);
    if (hubPolling) hubPolling.stop();
    hb.closed = true; draw();
    document.title = 'app shut down · Loop Harness';
  } catch (e) { toast('Could not shut down: ' + e.message, 'err'); hb.confirmShutdown = false; hb.shuttingDown = false; draw(); focusId('shut-open'); }
}

/** The whole hub page (#hub). The offline banner is driven by showDisconnectedBanner(); its props never change, so Preact leaves it alone. */
export function Hub() {
  const pasteRef = useRef(null);
  const onPaste = (e) => { e.preventDefault(); const c = pasteRef.current.value.trim(); if (c) openPath(c); };
  return html`<div id="hub" class="hub">
    <div id="banner-offline" class="banner banner-offline" role="alert" hidden>
      <span class="dot dot-red" />
      <strong>app disconnected</strong>
      <span id="banner-offline-detail" />
    </div>
    <header class="header hub-header">
      <div class="brand">
        <div class="logo" aria-hidden="true">L</div>
        <div>
          <div class="brand-name">Loop Harness</div>
          <div class="brand-sub mono">projects</div>
        </div>
      </div>
      <div class="spacer" />
      <${WhyButton} />
    </header>

    <main class="hub-main">
      <section class="hub-start" aria-labelledby="hub-title">
        <h1 id="hub-title" class="hub-h1">Projects</h1>
        <p class="muted hub-lead">Open a project folder (new, existing without .specs, or with a loop stopped midway) or create a new one. Each project panel shows where it stands and what the next step is.</p>
        <div class="hub-buttons">
          <button type="button" class="btn btn-primary hub-big" id="hub-open" onClick=${openFolder}>Open folder</button>
          <button type="button" class="btn hub-big" id="hub-create" onClick=${createDialog}>Create project from scratch</button>
          <button type="button" class="btn btn-ghost btn-sm" id="hub-browse" title="in-app folder browser, without the system window" onClick=${openBrowser}>Browse folders</button>
        </div>
        <form class="hub-paste" id="hub-paste" aria-label="Open by path" onSubmit=${onPaste}>
          <label for="hub-paste-field" class="hint">or paste the path:</label>
          <input type="text" id="hub-paste-field" class="mono" placeholder=${'C:\\Users\\you\\projects\\my-app'} autocomplete="off" spellcheck="false" ref=${pasteRef} />
          <button type="submit" class="btn btn-sm">Open</button>
        </form>
        <div id="hub-opening" class="hub-opening" hidden=${!hb.opening}><${Opening} v=${hb.opening} /></div>
      </section>

      <section class="hub-recent" aria-labelledby="hub-recent-t">
        <div class="sec-head"><h2 class="sec-title" id="hub-recent-t">Recent <span class="dim-mono" id="hub-total">${hb.projects && hb.projects.length ? String(hb.projects.length) : ''}</span></h2></div>
        <div id="hub-list"><${ProjectList} /></div>
      </section>
    </main>

    <footer class="hub-footer" aria-label="App diagnostics">
      <div class="hub-footer-t">
        <span class="small-label">Diagnostics</span>
        <button type="button" class="btn btn-sm" id="hub-test" disabled=${hb.testing} onClick=${testNotification}>${hb.testing ? 'sending…' : 'Test notification'}</button>
        <span class="spacer" />
        <div id="hub-shutdown"><${Shutdown} /></div>
      </div>
      <div id="hub-diag"><${Diagnostics} r=${hb.info} error=${hb.infoError} /></div>
    </footer>

    <div id="closed" class="closed" hidden=${!hb.closed}>
      <div class="closed-card" role="alert">
        <div class="closed-title">App shut down</div>
        <div class="muted">The server was shut down. You can close this tab. Loops already started keep running.</div>
      </div>
    </div>
  </div>`;
}

export function startHub(root) {
  hubRoot = root;
  root.replaceChildren();
  document.title = 'projects · Loop Harness';
  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape') return;
    if (hb.confirmShutdown) cancelShutdown();
    else if (hb.forgetting) { const id = hb.forgetting; hb.forgetting = null; draw(); focusId('forget-' + id); }
  });
  draw();
  hubPolling = createPolling({
    intervalMs: 5000,
    task: async () => {
      try { await loadProjects(); } catch (e) { hb.error = e.message; if (hb.projects == null) draw(); throw e; }
    },
    onConnectionChange: (disc, msg) => showDisconnectedBanner($('banner-offline'), $('banner-offline-detail'), disc, msg),
  });
  hubPolling.now(0);
  loadInfo();
  openWhyOnFirstVisit();
}
