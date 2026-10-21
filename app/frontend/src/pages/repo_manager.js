/* Repositories of one project (hub dialog, panel Workspace area and planning tab): the "This project uses several repositories"
   checkbox, the repos with a checkbox each (project repos first, then the known repos of the catalog), "Add folder" (native window)
   and, per repo, the git-ignored config files to copy into the worktree. Everything goes through
   /api/app/projects/<id>/repos (list + suggested, add, multi, remove, copy, setup, copy-suggestions) and GET /api/app/catalog.
   Unchecked = a single-repo project: only the primary repo and its copy list show. Never touches a folder on disk.
   Also the helpers that tell which repos a ROADMAP feature writes to (`paths=<repo>:<path>`), used by the roadmap review. */
import { html, useState, useEffect, useRef } from '../util/preact.js';
import { list, txt } from '../util/format.js';
import { appApi, requestJson } from '../api/api_client.js';
import { toast } from '../components/toast.js';
import { pickFolderNativeFirst } from '../components/folder_browser.js';
import { openDialog, confirmDialog } from '../components/dialog.js';
import { Conformance, conformanceDefault } from '../components/conformance.js';
import { openBigDialog } from '../components/big_dialog.js';
import { SubprojectsPanel, loadSubprojects } from './panel/subprojects.js';

const READINESS_POLL_MS = 4000; // while a map job runs in some repo
const WORKTREE_HELP = 'The loop never works in your folders. For each repository it creates its own separate folder (a git worktree) next to yours, on a new loop/<date> branch from the branch your folder is on now. Nothing is cloned or pushed.';
const reposRoute =(id) => 'projects/' + encodeURIComponent(id) + '/repos';
export const normPath = (p) => String(p == null ? '' : p).replace(/\\/g, '/').replace(/\/+$/, '').toLowerCase();
const parentFolder = (p) => { const t = String(p || '').replace(/[\\/]+$/, ''); const k = Math.max(t.lastIndexOf('\\'), t.lastIndexOf('/')); return k > 0 ? t.slice(0, k) : ''; };
const focusId = (id) => setTimeout(() => { const el = document.getElementById(id); if (el) el.focus(); }, 0);
const fmtSize = (n) => (typeof n === 'number' ? (n >= 1024 ? (n / 1024).toFixed(1) + ' KB' : n + ' B') : '');

// ---------- reading ----------
/** GET repos of the project: { multi, repos[], suggested[] } (single-repo project: multi false, one repo).
    `suggested` = what the planning AI proposed from the catalog (REPOS-SUGGESTED.json, validated by the back end): { name, path, reason, evidence }. */
export async function fetchProjectRepos(projectId) {
  const r = await appApi.get(reposRoute(projectId), 15000);
  return { multi: !!r.multi, repos: list(r.repos), suggested: list(r.suggested) };
}
/** GET repos-readiness: { multi, ready, repos[] } (per extra repo: conformance, map {files, running}, missing[], ready, detail). A back end without it: null. */
export async function fetchRepoReadiness(projectId) {
  try {
    const r = await appApi.get('projects/' + encodeURIComponent(projectId) + '/repos-readiness', 15000);
    return { multi: !!r.multi, ready: r.ready !== false, repos: list(r.repos) };
  } catch (_) { return null; }
}
/** GET catalog index (name, path, purpose, weak, capability_ids). A back end without the catalog gives []. */
export async function fetchCatalog() {
  try { return list((await appApi.get('catalog', 15000)).repos); } catch (_) { return []; }
}

// ---------- which repos a feature writes to ----------
/** `paths=` text -> [{ repo, path }]; repo = '' when the entry has no prefix (= the primary repo). */
export function parsePaths(text) {
  return String(text || '').split(',').map((s) => s.trim()).filter(Boolean).map((entry) => {
    const i = entry.indexOf(':');
    const drive = i === 1 && /^[A-Za-z]$/.test(entry[0]) && /[\\/]/.test(entry[2] || ''); // C:/x is a path, not a repo prefix
    return i > 0 && !drive ? { repo: entry.slice(0, i), path: entry.slice(i + 1) } : { repo: '', path: entry };
  });
}
/** Per feature: the repos its paths touch, with the paths of each. `repos` = the project list (primary first). */
export function featureRepoRows(features, repos) {
  const known = new Set(list(repos).map((r) => r.name)), primary = (list(repos).find((r) => r.primary) || list(repos)[0] || {}).name || 'primary';
  return list(features).map((f) => {
    const by = new Map();
    for (const { repo, path } of parsePaths(f.paths)) {
      const name = repo || primary;
      if (!by.has(name)) by.set(name, []);
      by.get(name).push(path);
    }
    const order = String(f['repo-order'] || '').split(',').map((s) => s.trim()).filter(Boolean);
    return { slug: f.slug, repos: [...by.entries()].map(([name, paths]) => ({ name, paths, unknown: !known.has(name) })), order };
  });
}
/** The list inside the "approve" confirmation and the Repositories tab of planning: feature -> repos touched. */
export function RepoConfirm({ rows }) {
  return html`<div class="rp-confirm"><div class="small-label">Repositories each feature writes to</div>
    <ul class="rp-confirm-list">${rows.map((row) => html`<li key=${row.slug}><span class="mono rp-confirm-slug">${row.slug}</span>
      <span class="rp-confirm-repos">${row.repos.length ? row.repos.map((r) => html`<span key=${r.name} class=${'rp-tag mono' + (r.unknown ? ' bad' : '')}
        title=${r.paths.join(', ') + (r.unknown ? ' (not in the project list)' : '')}>${r.name}</span>`) : html`<span class="dim">no paths</span>`}
        ${row.order.length > 1 ? html`<span class="dim mono">${'order: ' + row.order.join(' → ')}</span>` : null}</span></li>`)}</ul>
    <p class="hint">Check that every path belongs to the right repository. The guard only allows a feature to write inside its own paths of its own repository.</p></div>`;
}
/** extraContent for an approval dialog: the per-feature repos of a multi-repo project, or null (single repo or any read failure). */
export async function repoConfirmContent(api, projectId) {
  try {
    const [rd, roadmap] = await Promise.all([fetchProjectRepos(projectId), api.get('roadmap', 15000)]);
    if (!rd.multi) return null;
    const rows = featureRepoRows(roadmap && roadmap.features, rd.repos);
    return rows.length ? html`<${RepoConfirm} rows=${rows} />` : null;
  } catch (_) { return null; }
}

// ---------- add folder ----------
async function chooseFolder(initial = '') {
  return pickFolderNativeFirst({ title: 'Repository folder', buttonText: 'Add this folder', initial }); // null = the window was cancelled
}

// ---------- files to copy ----------
function CopyList({ projectId, repo, locked, onSaved }) {
  const [sug, setSug] = useState(null), [err, setErr] = useState(''), [picked, setPicked] = useState(null), [saving, setSaving] = useState(false);
  useEffect(() => {
    let live = true;
    appApi.get(reposRoute(projectId) + '/' + encodeURIComponent(repo.name) + '/copy-suggestions', 20000)
      .then((r) => {
        if (!live) return;
        const rows = list(r.suggestions);
        for (const p of list(repo.copy)) if (!rows.some((x) => x.path === p)) rows.push({ path: p, ticked: true, secret: false, size: null });
        setSug(rows); setPicked(new Set(rows.filter((x) => x.ticked).map((x) => x.path)));
      }).catch((e) => { if (live) setErr(e.message); });
    return () => { live = false; };
  }, [projectId, repo.name, repo.copy_configured]);
  const same = sug && picked && sug.filter((x) => x.ticked).length === picked.size && sug.every((x) => !!x.ticked === picked.has(x.path));
  const useAutomatic = async () => { // `copy: null` = back to the automatic copy of the suggested files
    setSaving(true); setErr('');
    try {
      await requestJson('PUT', '/api/app/' + reposRoute(projectId) + '/' + encodeURIComponent(repo.name) + '/copy', { copy: null });
      toast(`Files to copy of ${repo.name} are automatic again.`, 'ok');
      if (onSaved) onSaved();
    } catch (e) { setErr('Not changed: ' + e.message); } finally { setSaving(false); }
  };
  const toggle = (p, on) => { const n = new Set(picked); if (on) n.add(p); else n.delete(p); setPicked(n); };
  const save = async () => {
    setSaving(true); setErr('');
    try {
      const r = await requestJson('PUT', '/api/app/' + reposRoute(projectId) + '/' + encodeURIComponent(repo.name) + '/copy', { copy: [...picked] });
      setSug(sug.map((x) => ({ ...x, ticked: list(r.copy).includes(x.path) })));
      toast(`Files to copy saved for ${repo.name}.`, 'ok');
      if (onSaved) onSaved();
    } catch (e) { setErr('Not saved: ' + e.message); } finally { setSaving(false); }
  };
  const id = 'rp-copy-' + repo.name;
  return html`<div class="rp-copy" id=${id}>
    <p class="rp-help">A new worktree only gets the files git tracks. Tick the git-ignored files the build or the app needs here (local configuration). Folders such as node_modules, bin, obj and dist are never copied: the setup command rebuilds them.</p>
    ${err ? html`<div class="nl-box nl-bad" role="alert">${err}</div>` : null}
    ${!sug && !err ? html`<div class="nl-msg" role="status"><div class="spinner sm" aria-hidden="true"></div><span>looking for local configuration files…</span></div>` : null}
    ${sug && !sug.length ? html`<div class="rp-empty">No git-ignored configuration files were found in this repository.</div>` : null}
    ${sug && sug.length ? html`<ul class="rp-files" aria-label=${'Files to copy for ' + repo.name}>${sug.map((x, i) => html`<li key=${x.path} class="rp-file">
      <label class="nl-check" for=${id + '-' + i}><input type="checkbox" id=${id + '-' + i} checked=${picked.has(x.path)} disabled=${locked || saving}
        onChange=${(e) => toggle(x.path, e.currentTarget.checked)} /><span class="mono">${x.path}</span></label>
      <span class="dim mono">${fmtSize(x.size)}</span>
      ${x.secret ? html`<span class="rp-tag mono" title="Agents cannot read this file inside the worktree. The build and the app still can.">hidden from agents</span>` : null}
    </li>`)}</ul>
      <div class="card-row"><button type="button" class="btn btn-sm" disabled=${locked || saving || same} onClick=${save}>${saving ? 'Saving…' : 'Save files to copy'}</button>
        ${isCopyConfigured(repo) ? html`<button type="button" class="btn btn-sm btn-ghost" disabled=${locked || saving} onClick=${useAutomatic}>Use automatic</button>` : null}</div>` : null}
  </div>`;
}

/** Status of one repo's two overrides, computed from the fields the back end sends (a missing field reads as "not configured"). */
const isCopyConfigured = (repo) => (repo.copy_configured === undefined ? list(repo.copy).length > 0 : !!repo.copy_configured);
const isSetupConfigured = (repo) => (repo.setup_configured === undefined ? !!repo.setup : !!repo.setup_configured);
function AutomaticStatus({ repo }) {
  const setup = isSetupConfigured(repo) ? `Custom: "${repo.setup}"`
    : repo.detected_setup ? `Automatic: will run "${repo.detected_setup}"` : 'Automatic: nothing detected, no setup runs';
  const files = isCopyConfigured(repo) ? 'Custom list' : 'Automatic: git-ignored local config files (.env, appsettings.*.json…) are copied';
  return html`<div class="rp-auto" role="note">
    <p class="rp-help">Everything here works by itself. Change it only to override.</p>
    <div class="rp-help"><b>Setup:</b> <span class="mono">${setup}</span></div>
    <div class="rp-help"><b>Files:</b> <span class="mono">${files}</span></div>
  </div>`;
}

// ---------- one-time setup command ----------
function SetupField({ projectId, repo, locked, onSaved }) {
  const [value, setValue] = useState(repo.setup || ''), [saving, setSaving] = useState(false), [err, setErr] = useState('');
  const same = value.trim() === (repo.setup || '');
  const id = 'rp-setup-' + repo.name;
  const save = async () => {
    setSaving(true); setErr('');
    try {
      const r = await requestJson('PUT', '/api/app/' + reposRoute(projectId) + '/' + encodeURIComponent(repo.name) + '/setup', { setup: value.trim() });
      setValue(r.setup || '');
      toast(r.setup ? `Setup saved for ${repo.name}.` : `Setup removed for ${repo.name}.`, 'ok');
      if (onSaved) onSaved();
    } catch (e) { setErr('Not saved: ' + e.message); } finally { setSaving(false); }
  };
  return html`<div class="rp-setup">
    <label class="small-label" for=${id}>Setup command</label>
    <p class="rp-help">Runs once in this repository's worktree, right after it is created and its files are copied (for example <span class="mono">npm ci</span>). Saving it empty goes back to automatic. A failure stops the start and names this repository.</p>
    ${err ? html`<div class="nl-box nl-bad" role="alert">${err}</div>` : null}
    <div class="card-row"><input type="text" id=${id} class="rp-setup-input mono" value=${value} maxlength="200" placeholder=${repo.detected_setup || 'npm ci'} disabled=${locked || saving}
      onInput=${(e) => setValue(e.currentTarget.value)} onKeyDown=${(e) => { if (e.key === 'Enter' && !same && !locked && !saving) save(); }} />
      <button type="button" class="btn btn-sm" disabled=${locked || saving || same} onClick=${save}>${saving ? 'Saving…' : 'Save setup'}</button></div>
  </div>`;
}

// ---------- repositories suggested by the planning AI ----------
function SuggestedRepos({ rows, locked, busy, onAdd }) {
  const [off, setOff] = useState({});
  const on = rows.filter((c) => !off[c.name]);
  return html`<section class="rp-suggested" aria-label="Repositories suggested from the catalog"><div class="small-label">Suggested from the catalog</div>
    <p class="rp-help">The planning assistant proposed these. Nothing is added until you confirm.</p>
    <ul class="rp-list">${rows.map((c) => html`<li key=${c.name} class="rp-row">
      <div class="rp-line"><input type="checkbox" id=${'rp-sug-' + c.name} checked=${!off[c.name]} disabled=${locked || busy}
        onChange=${(e) => setOff({ ...off, [c.name]: !e.currentTarget.checked })} /><label class="rp-name mono" for=${'rp-sug-' + c.name}>${c.name}</label></div>
      ${c.reason ? html`<div class="rp-purpose">${c.reason}</div>` : null}
      ${c.evidence ? html`<div class="rp-evidence"><span>Evidence:</span><span class="mono">${c.evidence}</span></div>` : null}
    </li>`)}</ul>
    <div class="card-row"><button type="button" class="btn btn-sm btn-primary" disabled=${locked || busy || !on.length} onClick=${() => onAdd(on)}>${busy ? 'Adding…' : `Add ticked repositories (${on.length})`}</button></div>
  </section>`;
}

// ---------- minimum setup of a non-primary repo (Conformance line + codebase map) ----------
/** Dialog: pick STANDARD or LEGACY for one repo, PUT repos/<name>/conformance. */
function chooseRepoConformance(projectId, repo, onDone) {
  const sug = repo.suggestion, initial = repo.conformance || (sug && sug.suggestion) || null;
  let value = conformanceDefault(initial, false);
  openDialog({
    title: 'Code conformance of ' + repo.name,
    content: [
      html`<p class="dlg-text">Writes the "Conformance:" line in the CLAUDE.md of this repository (a new CLAUDE.md if it has none). The loop reads it to decide how much of the existing code it may change there.</p>`,
      sug && sug.suggestion ? html`<p class="hint">${'script suggestion: ' + sug.suggestion + (sug.stack ? ' (stack ' + sug.stack + ')' : '')}</p>` : null,
      html`<${Conformance} initial=${initial} allowLater=${false} onChange=${(v) => { value = v; }} />`,
    ],
    actions: [
      { text: 'Cancel' },
      { text: 'Save', cls: 'btn-primary', onClick: async (d) => {
        d.busy(true);
        try {
          await requestJson('PUT', '/api/app/' + reposRoute(projectId) + '/' + encodeURIComponent(repo.name) + '/conformance', { value });
          toast('Conformance saved for ' + repo.name + '.', 'ok'); d.close(); onDone();
        } catch (e) { toast('Not saved: ' + e.message, 'err'); d.busy(false); }
      } },
    ],
  });
}
/** Confirm, then POST repos/<name>/map (a claude job: minutes). */
async function mapRepoCodebase(projectId, repo, onDone) {
  const ok = await confirmDialog({ title: 'Map ' + repo.name + '?', text: 'Runs claude in this repository and writes only docs/codebase/ and CLAUDE.md. Your code is not touched. Spends tokens and may take a few minutes.', yesText: 'Map now' });
  if (!ok) return;
  try {
    await appApi.post(reposRoute(projectId) + '/' + encodeURIComponent(repo.name) + '/map', {}, 30000);
    toast('Mapping ' + repo.name + ' started.', 'ok'); onDone();
  } catch (e) { toast((e.status === 409 ? 'Cannot map right now: ' : 'Mapping not started: ') + e.message, 'err'); }
}
/** A secondary repo that is a monorepo (readiness says so, or its unready items talk about subprojects) shows the subprojects table instead of the two buttons. */
const isMonorepoRepo = (info) => !!info.monorepo || list(info.missing).includes('subprojects') || /subproject/i.test(String(info.detail || ''));
/** Dialog with the same table as the panel, for the repo `info.name` (its folder = `repoPath`, so "Add folder" is made relative to it). */
function openSubprojectsDialog(info, repoPath, onDone) {
  openBigDialog({
    badge: 'Monorepo', title: 'Subprojects of ' + info.name,
    body: () => html`<${SubprojectsPanel} repo=${info.name} repoPath=${repoPath} />`,
    onClose: () => { loadSubprojects(info.name); if (onDone) onDone(); },
  });
}
/** Under a non-primary repo row: its tags, why it is not ready and the two setup actions. `info` = the repos-readiness row. */
function RepoReadiness({ projectId, info, repoPath, locked, onDone }) {
  if (isMonorepoRepo(info)) return html`<${MonorepoReadiness} info=${info} repoPath=${repoPath} onDone=${onDone} />`;
  const running = !!(info.map && info.map.running), docs = list(info.map && info.map.files).length, miss = list(info.missing);
  return html`<div class="rp-ready" role="group" aria-label=${'Setup of ' + info.name}>
    <div class="rp-line">
      <span class=${'rp-tag mono' + (info.conformance ? '' : ' bad')}>${info.conformance ? 'Conformance: ' + info.conformance : 'no Conformance'}</span>
      ${running ? html`<span class="rp-tag mono"><span class="spinner sm" aria-hidden="true"></span> mapping…</span>`
        : info.has_code === false ? html`<span class="rp-tag mono" title="no code here: a map is not required">no code</span>`
        : html`<span class=${'rp-tag mono' + (docs ? '' : ' bad')}>${docs ? `mapped (${docs} docs)` : 'not mapped'}</span>`}
      <span class="spacer"></span>
      <button type="button" class="btn btn-sm" disabled=${!!locked || running} onClick=${() => chooseRepoConformance(projectId, info, onDone)}>Set conformance</button>
      ${info.has_code === false ? null : html`<button type="button" class="btn btn-sm" disabled=${!!locked || running} onClick=${() => mapRepoCodebase(projectId, info, onDone)}>${running ? 'Mapping…' : 'Map codebase'}</button>`}
    </div>
    ${!info.ready ? html`<div class="nl-box nl-warn" role="note">${'Not ready: ' + (info.detail || (miss.length ? 'missing ' + miss.join(' and ') : 'setup incomplete'))}</div>` : null}
    <p class="rp-help">Setting conformance or mapping writes CLAUDE.md (and docs/codebase) in that repository, so its folder shows as changed: the Workspace area then offers Commit and push.</p>
  </div>`;
}

function MonorepoReadiness({ info, repoPath, onDone }) {
  const confirmed = !!(info.monorepo && info.monorepo.confirmed), miss = list(info.missing);
  return html`<div class="rp-ready" role="group" aria-label=${'Subprojects of ' + info.name}>
    <div class="rp-line">
      <span class=${'rp-tag mono' + (confirmed ? '' : ' bad')}>${confirmed ? 'monorepo: list confirmed' : 'monorepo: list not confirmed'}</span>
      <span class="spacer"></span>
      <button type="button" class="btn btn-sm" onClick=${() => openSubprojectsDialog(info, repoPath, onDone)}>Subprojects</button>
    </div>
    ${!info.ready ? html`<div class="nl-box nl-warn" role="note">${'Not ready: ' + (info.detail || (miss.length ? 'missing ' + miss.join(' and ') : 'setup incomplete'))}</div>` : null}
    <p class="rp-help">The Conformance line and the code map of a monorepo live in each subproject. Open Subprojects to confirm the list, set conformance and map them.</p>
  </div>`;
}

// ---------- the repositories ----------
/** props: projectId, locked (a sentence: why changes are refused now; '' = allowed), onChange (after any change), noMultiBox. */
export function RepoManager({ projectId, locked = '', onChange }) {
  const [data, setData] = useState(null), [catalog, setCatalog] = useState([]), [err, setErr] = useState(''), [busy, setBusy] = useState(''),
    [removing, setRemoving] = useState(null), [copyOpen, setCopyOpen] = useState(null), [ready, setReady] = useState(null);
  const alive = useRef(true);
  useEffect(() => () => { alive.current = false; }, []);
  const load = async () => {
    try { const d = await fetchProjectRepos(projectId); if (alive.current) { setData(d); setErr(''); } }
    catch (e) { if (alive.current) setErr('Could not read the repositories: ' + e.message); return; }
    const rd = await fetchRepoReadiness(projectId);
    if (alive.current) setReady(rd && rd.multi ? rd : null);
  };
  const mapping = !!(ready && ready.repos.some((x) => x.map && x.map.running));
  useEffect(() => { // polls the readiness only while some map job runs
    if (!mapping) return undefined;
    const t = setInterval(async () => {
      const rd = await fetchRepoReadiness(projectId);
      if (!alive.current) return;
      setReady(rd && rd.multi ? rd : null);
      if (!(rd && rd.repos.some((x) => x.map && x.map.running)) && onChange) onChange(); // the map finished: the planning checklist reads again
    }, READINESS_POLL_MS);
    return () => clearInterval(t);
  }, [mapping, projectId]);
  useEffect(() => { load(); fetchCatalog().then((c) => { if (alive.current) setCatalog(c); }); }, [projectId]);
  const run = async (key, fn, okText) => {
    setBusy(key); setErr('');
    try { await fn(); await load(); if (okText) toast(okText, 'ok'); if (onChange) onChange(); return true; }
    catch (e) { setErr((e.status === 409 || e.status === 400 ? '' : 'Failed: ') + e.message); return false; }
    finally { if (alive.current) setBusy(''); }
  };
  if (!data) return html`<div class=${err ? 'nl-box nl-bad' : 'hint'} role=${err ? 'alert' : 'status'}>${err || 'reading the repositories…'}</div>`;

  const repos = data.repos;
  const inProject = new Set(repos.map((r) => normPath(r.path)));
  const extra = catalog.filter((c) => !inProject.has(normPath(c.path)));
  const lockMsg = locked || '';
  const addPath = (path) => run('add:' + path, () => appApi.post(reposRoute(projectId), { path }, 30000), 'Repository added.');
  const addSuggested = async (rows) => {
    for (const c of rows) if (!(await addPath(c.path))) break; // stops at the first refusal (the error is shown); the others stay suggested
  };
  const addFolder = async () => { const primary = repos.find((r) => r.primary) || repos[0]; const path = await chooseFolder(primary && primary.path ? parentFolder(primary.path) : ''); if (path) addPath(path); };
  const removeRepo = async (r) => {
    const ok = await run('rm:' + r.name, () => requestJson('DELETE', '/api/app/' + reposRoute(projectId) + '/' + encodeURIComponent(r.name), null), `${r.name} left the project (no folder or worktree was deleted).`);
    if (ok) { setRemoving(null); focusId('rp-add'); }
  };
  const idle = !busy;
  const readyOf = (name) => (ready ? ready.repos.find((x) => x.name === name) : null);

  return html`<div class="rp">
    <p class="rp-help" id="rp-help">${lockMsg || ('One loop works across the repositories listed here. ' + WORKTREE_HELP + ' The plan (.specs) stays in the primary repository.')}</p>
    ${err ? html`<div class="nl-box nl-bad" role="alert">${err}</div>` : null}
    <ul class="rp-list" aria-label="Repositories of the project">
      ${repos.map((r) => html`<li key=${r.name} class="rp-row">
        <div class="rp-line">
          <input type="checkbox" id=${'rp-chk-' + r.name} checked disabled=${r.primary || !!lockMsg || !idle} aria-label=${'Repository ' + r.name + (r.primary ? ' (primary, always part of the project)' : '')}
            onChange=${() => { setRemoving(r.name); focusId('rp-rm-no-' + r.name); }} />
          <span class="rp-name mono">${r.name}</span>
          ${r.primary ? html`<span class="rp-tag mono">primary</span>` : null}
          ${r.role ? html`<span class="rp-tag mono" title=${r.reason || 'role of this repository in the project'}>${r.role}</span>` : null}
          ${r.exists === false ? html`<span class="rp-tag bad mono">folder missing</span>` : null}
          ${r.branch ? html`<span class="rp-tag mono" title="current branch of your folder">${r.branch}</span>` : null}
          <span class="spacer"></span>
          <button type="button" class="btn btn-sm btn-ghost" aria-expanded=${String(copyOpen === r.name)} aria-controls=${'rp-copy-' + r.name}
            onClick=${() => setCopyOpen(copyOpen === r.name ? null : r.name)}>${'Advanced: files and setup' + (isCopyConfigured(r) && list(r.copy).length ? ` (${r.copy.length})` : '')}</button>
        </div>
        <div class="rp-path mono" title=${r.path}>${txt(r.path)}</div>
        ${r.worktree_path ? html`<div class="rp-path rp-worktree mono" title=${r.worktree_path}>${'Worktree: ' + r.worktree_path}</div>` : null}
        ${data.multi && !r.primary && readyOf(r.name) ? html`<${RepoReadiness} projectId=${projectId} info=${readyOf(r.name)} repoPath=${r.path} locked=${lockMsg} onDone=${() => { load(); if (onChange) onChange(); }} />` : null}
        ${removing === r.name ? html`<div class="confirm-inline" role="group" aria-label=${'Confirm removing ' + r.name}>
          <span>Remove it from the project? No folder or worktree is deleted.</span>
          <button type="button" class="btn btn-danger btn-sm" id=${'rp-rm-yes-' + r.name} disabled=${!idle} onClick=${() => removeRepo(r)}>Yes, remove</button>
          <button type="button" class="btn btn-sm" id=${'rp-rm-no-' + r.name} onClick=${() => { setRemoving(null); focusId('rp-chk-' + r.name); }}>Cancel</button>
        </div>` : null}
        ${copyOpen === r.name ? html`<${AutomaticStatus} repo=${r} />` : null}
        ${copyOpen === r.name ? html`<${SetupField} projectId=${projectId} repo=${r} locked=${!!lockMsg} onSaved=${() => load()} />` : null}
        ${copyOpen === r.name ? html`<${CopyList} projectId=${projectId} repo=${r} locked=${!!lockMsg} onSaved=${() => load()} />` : null}
      </li>`)}
      ${extra.map((c) => html`<li key=${'cat-' + c.path} class="rp-row">
        <div class="rp-line">
          <input type="checkbox" id=${'rp-cat-' + normPath(c.path)} checked=${false} disabled=${!!lockMsg || !idle} aria-label=${'Add repository ' + c.name}
            onChange=${(e) => { e.currentTarget.checked = false; addPath(c.path); }} />
          <label class="rp-name mono" for=${'rp-cat-' + normPath(c.path)}>${c.name}</label>
          ${c.weak ? html`<span class="rp-tag mono" title="No docs/codebase in this repository: the catalog has little to say about it">not mapped</span>` : null}
        </div>
        <div class="rp-path mono" title=${c.path}>${txt(c.path)}</div>
        ${c.purpose ? html`<div class="rp-purpose">${c.purpose}</div>` : null}
      </li>`)}
    </ul>
    ${data.suggested.length ? html`<${SuggestedRepos} rows=${data.suggested} locked=${!!lockMsg} busy=${busy.startsWith('add:')} onAdd=${addSuggested} />` : null}
    ${html`<div class="card-row"><button type="button" class="btn btn-sm" id="rp-add" disabled=${!!lockMsg || !idle} onClick=${addFolder}>${busy.startsWith('add:') ? 'Adding…' : 'Add folder…'}</button>
      <span class="hint">Opens the system folder window. The folder must be a git repository on a branch.</span></div>`}
  </div>`;
}
