/* "Set up <repo>" dialog (SU-01, SU-03): the same installer as the main repository, bound to one repository of the project.
   Step 0 says what was detected (monorepo or single project) with the "Not a monorepo" switch; then
   - monorepo: components/subprojects_installer.js through <SubprojectsInstallerPanel repo=...> (subprojects -> Conformance -> maps),
   - single repository: components/project_installer.js (Conformance -> map) driven by GET repos-readiness and
     PUT/POST repos/<name>/conformance|map (the routes the repositories dialog already uses).
   Role `reference`: no Conformance step (the installer opens on the maps) and the map is optional: "Skip — read the raw code".
   openRepoSetupDialog({ repo, role, onDone }): repo = name or { name, path }; role = 'edit' (default) | 'reference'; onDone({ skipped })
   runs when the user finishes (Done, the last installer button or Skip) and after the dialog is closed. */
import { html, useState, useEffect } from '../../util/preact.js';
import { list } from '../../util/format.js';
import { appApi } from '../../api/api_client.js';
import { toast } from '../../components/toast.js';
import { confirmDialog } from '../../components/dialog.js';
import { openBigDialog } from '../../components/big_dialog.js';
import { ProjectInstaller, MAP_CONFIRM } from '../../components/project_installer.js';
import { fetchRepoReadiness } from '../repo_manager.js';
import { ctx } from './context.js';
import { SubprojectsInstallerPanel, loadSubprojects, subprojectsData, onSubprojectsChanged } from './subprojects.js';

const POLL_MS = 3000;
const REFERENCE_NOTE = 'This is a reference repository: the loop only reads it. There is no Conformance to choose and the map is optional.';
const SKIP_TITLE = 'Nothing is written; the agents read the raw code of this repository when they need it.';
const reposRoute = (name) => 'projects/' + encodeURIComponent(ctx.projectId) + '/repos/' + encodeURIComponent(name);
const isTarget = (row) => !row.skip && row.exists !== false;

// ---------- step 0: what was detected ----------
function DetectionStep({ name, data, busy, onContinue, onNotMonorepo, onDetectAgain }) {
  if (!data) return html`<div class="sp-muted" role="status"><span class="spinner sm" aria-hidden="true"></span> Looking at ${name}…</div>`;
  const projects = list(data.projects);
  return html`<div class="sp-inst" id="rs-detect">
    <p class="sp-inst-d">${data.monorepo
      ? `${name} looks like a monorepo with ${projects.length} subproject${projects.length === 1 ? '' : 's'}. Each one gets its own conformance and its own code map.`
      : `${name} looks like a single project: one Conformance line and one code map for the whole repository.`}</p>
    ${data.reason ? html`<div class="sp-note">${'Detection: ' + data.reason}</div>` : null}
    ${data.error ? html`<div class="sp-note sp-note-bad" role="alert">${data.error}</div>` : null}
    ${data.monorepo && projects.length ? html`<ul class="sp-inst-list" aria-label="Detected subprojects">${projects.map((row) => html`<li key=${row.path} class="sp-inst-row"><div class="sp-inst-main"><strong class="sp-inst-name">${row.name}</strong><div class="sp-path">${row.path}</div></div></li>`)}</ul>` : null}
    <div class="sp-inst-foot">
      ${data.monorepo
        ? html`<button type="button" class="btn" id="rs-not-monorepo" disabled=${busy} onClick=${onNotMonorepo}>Not a monorepo</button>`
        : html`<button type="button" class="btn" id="rs-detect-again" disabled=${busy} onClick=${onDetectAgain}>It is a monorepo: detect again</button>`}
      <span class="sp-top-gap"></span>
      <button type="button" class="btn btn-primary" id="rs-continue" disabled=${busy} onClick=${onContinue}>Continue</button>
    </div>
  </div>`;
}

// ---------- single repository ----------
/** The ProjectInstaller fed by repos-readiness of `name`. */
function SingleRepoInstaller({ name, isReference, onFinish }) {
  const [info, setInfo] = useState(null);
  const [busy, setBusy] = useState(false);
  const [mapError, setMapError] = useState('');
  const reload = async () => {
    const readiness = await fetchRepoReadiness(ctx.projectId);
    const found = readiness && list(readiness.repos).find((repoInfo) => repoInfo.name === name);
    if (found) setInfo(found);
    return found || null;
  };
  const mapping = !!(info && info.map && info.map.running);
  useEffect(() => { reload(); }, [name]);
  useEffect(() => {
    if (!mapping) return undefined;
    const timer = setInterval(reload, POLL_MS);
    return () => clearInterval(timer);
  }, [mapping]);
  if (!info) return html`<div class="sp-muted" role="status"><span class="spinner sm" aria-hidden="true"></span> Reading the state of ${name}…</div>`;
  const documents = list(info.map && info.map.files).length;
  const failed = !mapping && !!mapError;
  const map = {
    state: mapping ? 'running' : failed ? 'failed' : documents > 0 ? 'done' : 'idle', documents, error: mapError,
    hasCode: info.has_code !== false, needed: true,
  };
  const startMapRequest = async () => {
    setMapError('');
    try { await appApi.post(reposRoute(name) + '/map', {}, 30000); toast('Mapping ' + name + ' started.', 'ok'); await reload(); }
    catch (error) { setMapError((error.status === 409 ? 'Cannot map right now: ' : 'Mapping not started: ') + error.message); }
  };
  const actions = {
    async saveConformance(value) {
      setBusy(true);
      try { await appApi.put(reposRoute(name) + '/conformance', { value }); toast('Conformance saved for ' + name + '.', 'ok'); await reload(); return true; }
      catch (error) { toast('Not saved: ' + error.message, 'err'); return false; }
      finally { setBusy(false); }
    },
    async startMap() {
      const ok = await confirmDialog({ title: 'Map ' + name + '?', text: MAP_CONFIRM, yesText: 'Map now' });
      if (ok) await startMapRequest();
    },
    retryMap: startMapRequest,
  };
  const conformance = { written: info.conformance || '', suggestion: info.suggestion || null };
  return html`<${ProjectInstaller} conformance=${conformance} map=${map} busy=${busy} actions=${actions} onContinue=${() => onFinish(false)} skipConformance=${isReference} finishLabel="Done" />`;
}

// ---------- monorepo ----------
/** The SubprojectsInstaller for `name`. Reference: its Conformance step is skipped by remounting on the maps step once the list is confirmed. */
function MonorepoInstaller({ name, path, isReference, onFinish }) {
  const [, redraw] = useState(0);
  useEffect(() => onSubprojectsChanged(() => redraw((n) => n + 1)), []);
  const data = subprojectsData(name);
  const rows = list(data && data.projects).filter(isTarget);
  const gates = {
    subprojects: !!(data && data.confirmed),
    conformance: isReference || (rows.length > 0 && rows.every((row) => !!row.conformance)),
    map: false,
  };
  return html`<${SubprojectsInstallerPanel} key=${isReference && (gates.subprojects) ? 'maps' : 'list'} repo=${name} repoPath=${path} gates=${gates} onContinue=${() => onFinish(false)} skipConformance=${isReference} finishLabel="Done" />`;
}

// ---------- the dialog ----------
function RepoSetup({ name, path, role, onFinish }) {
  const isReference = role === 'reference';
  const [phase, setPhase] = useState('detect');
  const [, redraw] = useState(0);
  const [busy, setBusy] = useState(false);
  const data = subprojectsData(name);
  useEffect(() => { loadSubprojects(name).then(() => redraw((n) => n + 1)); }, [name]);
  const run = async (work) => {
    setBusy(true);
    try { await work(); } catch (error) { toast(error.message || 'request failed', 'err'); }
    finally { await loadSubprojects(name); setBusy(false); }
  };
  const notMonorepo = async () => {
    const ok = await confirmDialog({ title: 'This is not a monorepo?', text: `${name} goes back to a single code map and a single Conformance line at its root.`, yesText: 'Not a monorepo' });
    if (ok) await run(() => ctx.api.put('subprojects?repo=' + encodeURIComponent(name), { monorepo: false, repo: name }));
  };
  const detectAgain = () => run(() => ctx.api.post('subprojects/detect?repo=' + encodeURIComponent(name), { force: true, repo: name }));
  return html`<div class="rs-dialog" data-role=${role}>
    ${isReference ? html`<div class="sp-note sp-note-info" id="rs-reference-note">${REFERENCE_NOTE}</div>` : null}
    ${phase === 'detect'
      ? html`<${DetectionStep} name=${name} data=${data} busy=${busy} onContinue=${() => setPhase('install')} onNotMonorepo=${notMonorepo} onDetectAgain=${detectAgain} />`
      : data && data.monorepo
        ? html`<${MonorepoInstaller} name=${name} path=${path} isReference=${isReference} onFinish=${onFinish} />`
        : html`<${SingleRepoInstaller} name=${name} isReference=${isReference} onFinish=${onFinish} />`}
    ${phase === 'install' && isReference ? html`<div class="sp-inst-foot" id="rs-skip-row">
      <span class="sp-muted">The map is optional for a reference repository.</span>
      <span class="sp-top-gap"></span>
      <button type="button" class="btn" id="rs-skip" title=${SKIP_TITLE} onClick=${() => onFinish(true)}>Skip — read the raw code</button>
    </div>` : null}
  </div>`;
}

export function openRepoSetupDialog({ repo, role = 'edit', onDone } = {}) {
  const name = typeof repo === 'string' ? repo : (repo && repo.name) || '';
  const path = typeof repo === 'object' && repo ? repo.path || '' : '';
  if (!name) return null;
  let handle = null;
  let skipped = false;
  const finish = (wasSkipped) => { skipped = !!wasSkipped; if (handle) handle.close(); };
  handle = openBigDialog({
    badge: role === 'reference' ? 'reference' : 'edit',
    title: 'Set up ' + name,
    body: () => html`<${RepoSetup} name=${name} path=${path} role=${role} onFinish=${finish} />`,
    onClose: () => { if (onDone) onDone({ skipped }); },
  });
  return handle;
}
