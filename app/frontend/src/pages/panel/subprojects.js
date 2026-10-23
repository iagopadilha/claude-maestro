/* Subprojects of a monorepo: screen state, polling and the calls to /p/<id>/api/subprojects.
   <SubprojectsPanel repo?> renders components/subprojects_table.js; the trail, the planning wizard and the
   repositories dialog only mount it (and may call loadSubprojects() to refresh). One store per repo name
   ('' = the project's own repo), shared by every mounted panel, so two mounts never disagree. */
import { html, useState, useEffect } from '../../util/preact.js';
import { POLLING_INTERVAL_MS } from '../../api/api_client.js';
import { toast } from '../../components/toast.js';
import { confirmDialog } from '../../components/dialog.js';
import { pickFolderNativeFirst } from '../../components/folder_browser.js';
import { SubprojectsTable, mapFileCount, mapQueueOf } from '../../components/subprojects_table.js';
import { SubprojectsInstaller } from '../../components/subprojects_installer.js';
import { ctx } from './context.js';

const stores = new Map();
function storeFor(repo = '') {
  const key = repo || '';
  if (!stores.has(key)) stores.set(key, { key, data: null, draft: null, busy: false, mapError: '', loadError: '', listeners: new Set(), timer: null, client: null, rowStarts: new Map(), rootPath: '', wasBusy: false });
  return stores.get(key);
}
const notify = (store) => store.listeners.forEach((listener) => listener());
const routeFor = (store, rel) => 'subprojects' + (rel ? '/' + rel : '') + (store.key ? '?repo=' + encodeURIComponent(store.key) : '');
const apiOf = (store) => store.client || ctx.api;
const errorText = (error) => (error && error.message) || 'request failed';

/** True while something is still moving: the AI validation, the map queue (or its system map) or a map job of a row. */
export function isSubprojectsBusy(data) {
  if (!data) return false;
  const job = data.validation_job;
  if (job && job.state === 'running') return true;
  const queue = mapQueueOf(data);
  if (queue && queue.state === 'running') return true;
  if (queue && queue.system && (queue.system.state === 'running' || queue.system.state === 'waiting') && queue.state !== 'stopped') return true;
  return (data.projects || []).some((row) => row.map && row.map.running);
}

function schedulePoll(store) {
  if (store.timer || !store.listeners.size || !isSubprojectsBusy(store.data)) return;
  store.timer = setTimeout(async () => { store.timer = null; await loadSubprojects(store.key); }, POLLING_INTERVAL_MS);
}

const queueCurrentPath = (queue) => { const current = queue && queue.current; return current && typeof current === 'object' ? current.path : current || ''; };
/** The old queue sends no start time per row: remember when this panel first saw the row running (fallback: queue.started_at)
    and put it in row.map.job.started_at, which the table reads. The parallel queue carries started_at per item. */
function stampRowStarts(store, data) {
  const queue = mapQueueOf(data), running = new Set();
  for (const row of data.projects || []) {
    const job = row.map && row.map.job;
    const item = queue && queue.items && queue.items[row.path];
    const isRunning = !!(row.map && row.map.running) || !!(job && job.state === 'running') || !!(item && item.state === 'running') || (!!queue && queue.state === 'running' && queueCurrentPath(queue) === row.path);
    if (!isRunning) continue;
    running.add(row.path);
    if (!store.rowStarts.has(row.path)) store.rowStarts.set(row.path, new Date().toISOString());
    const started = (job && job.started_at) || (item && item.started_at) || store.rowStarts.get(row.path) || (queue && queue.started_at);
    row.map = { ...(row.map || {}), job: { ...(job || {}), started_at: started } };
  }
  for (const path of [...store.rowStarts.keys()]) if (!running.has(path)) store.rowStarts.delete(path);
}

/** GET subprojects (optionally for another repo). Keeps the last data on a failure and polls every 2 s only while a job runs. */
export async function loadSubprojects(repo = '', client = null) {
  const store = storeFor(repo);
  if (client) store.client = client;
  try {
    const data = await apiOf(store).get(routeFor(store, ''));
    stampRowStarts(store, data);
    store.data = data; store.loadError = '';
    // A draft only lives while the list is unconfirmed; once confirmed (or replaced by the server) it is dropped.
    if (data.confirmed) store.draft = null;
    // the maps finished while this panel was looking: the wizard steps (gates) may have changed, so tell the listeners
    const busyNow = isSubprojectsBusy(data);
    if (store.wasBusy && !busyNow) notifyChanged();
    store.wasBusy = busyNow;
  } catch (error) {
    store.loadError = errorText(error);
    if (store.data) toast('Could not refresh the subprojects: ' + store.loadError, 'err');
  }
  notify(store); schedulePoll(store);
  return store.data;
}

/** Runs one call with the busy flag; the error goes to a toast and, for map calls, stays in the table (mapError). */
// ---------- listeners: the wizard and the trail re-read their steps after a change made in the table ----------
const changeListeners = new Set();
/** Registers `listener()` to run after every action of the table that succeeded (the steps may change: confirm, conformance, map...). */
export function onSubprojectsChanged(listener) { changeListeners.add(listener); return () => changeListeners.delete(listener); }
const notifyChanged = () => changeListeners.forEach((listener) => { try { listener(); } catch (error) { /* a listener never breaks the table */ } });

async function perform(store, work, { keepError = false, okText = '' } = {}) {
  store.busy = true; if (!keepError) store.mapError = ''; notify(store);
  try {
    const result = await work();
    if (okText) toast(okText, 'ok');
    notifyChanged();
    return result;
  } catch (error) {
    const message = errorText(error);
    if (keepError) store.mapError = message;
    toast(message, 'err');
    return null;
  } finally {
    store.busy = false; notify(store);
    await loadSubprojects(store.key);
  }
}

// ---------- folder picked by the user -> path relative to the repository root ----------
const slashed = (path) => String(path || '').replace(/\\/g, '/').replace(/\/+$/, '');
/** Root of the repo this table is about: the repo given by the dialog (secondary) or the project's own repo (state.project_ref.repo_path). */
const repositoryRoot = (store) => slashed(store.rootPath || (!store.key && ctx.state && ((ctx.state.project_ref && ctx.state.project_ref.repo_path) || ctx.state.repo)) || '');
/** '' = the root itself, null = outside the root (or root unknown). */
export function relativeToRoot(picked, root) {
  const folder = slashed(picked), base = slashed(root);
  if (!base) return null;
  if (folder.toLowerCase() === base.toLowerCase()) return '';
  return folder.toLowerCase().startsWith(base.toLowerCase() + '/') ? folder.slice(base.length + 1) : null;
}

const draftRows = (store) => store.draft || (store.data && store.data.projects ? store.data.projects.map((row) => ({ ...row })) : []);
function editDraft(store, change) { store.draft = change(draftRows(store)); notify(store); }
const projectsPayload = (rows) => rows.map((row) => ({ path: row.path, name: row.name, stack: row.stack, skill: row.skill, skip: !!row.skip, reason: row.reason || '' }));

// ---------- actions (the setup jobs run without a spend cap: no budget is asked or sent) ----------
const rowsMissingMap = (store) => (store.data.projects || []).filter((row) => !row.skip && row.exists !== false && row.has_code && !mapFileCount(row));
const rowsWithSuggestion = (store) => (store.data.projects || []).filter((row) => !row.skip && row.exists !== false && !row.conformance && row.suggestion);

function buildActions(store) {
  const post = (rel, body) => apiOf(store).post(routeFor(store, rel), body);
  const put = (body) => apiOf(store).put(routeFor(store, ''), body);
  return {
    async validate() {
      const ok = await confirmDialog({ title: 'Validate the list with AI?', text: 'Runs claude (Sonnet) read-only over the folders to check the detected list: it may add, skip, rename or flag subprojects. The result is only a proposal; you still confirm the list. No spend cap.', yesText: 'Validate' });
      if (!ok) return;
      await perform(store, () => post('validate', {}), { okText: 'Validation started.' });
    },
    async confirmList() {
      const rows = draftRows(store);
      return perform(store, async () => { await put({ projects: projectsPayload(rows), confirmed: true }); return true; }, { okText: 'List confirmed.' });
    },
    async detectAgain() {
      const confirmed = !!store.data.confirmed;
      if (confirmed) {
        const ok = await confirmDialog({ title: 'Detect the subprojects again?', text: 'The confirmed list is replaced by a new detection and has to be confirmed again. Conformance lines already written in the subprojects stay.', yesText: 'Detect again', danger: true });
        if (!ok) return;
      }
      store.draft = null;
      await perform(store, () => post('detect', confirmed ? { force: true } : {}), { okText: 'Detected again.' });
    },
    async notMonorepo() {
      const ok = await confirmDialog({ title: 'This is not a monorepo?', text: 'The project goes back to a single code map and a single Conformance line at the root.', yesText: 'Not a monorepo' });
      if (!ok) return;
      store.draft = null;
      await perform(store, () => put({ monorepo: false }), { okText: 'Marked as a single project.' });
    },
    async setConformance(row, value) {
      await perform(store, () => post('conformance', { path: row.path, value }), { okText: `${row.path}: ${value}` });
    },
    /** Installer step 2: writes every choice, one subproject after the other. `choices` = [{ path, value }]. True when all were written. */
    async saveConformances(choices) {
      return perform(store, async () => { for (const choice of choices) await post('conformance', { path: choice.path, value: choice.value }); return true; }, { okText: 'Conformance saved.' });
    },
    async useSuggestionForAll() {
      const rows = rowsWithSuggestion(store);
      if (!rows.length) return;
      const list = html`<ul class="sp-confirm-list">${rows.map((row) => html`<li key=${row.path}><code>${row.path}</code>: ${row.suggestion.suggestion}</li>`)}</ul>`;
      const ok = await confirmDialog({ title: 'Use the suggestion for all?', text: `Writes the suggested Conformance line in ${rows.length} subproject${rows.length === 1 ? '' : 's'}. Rows that already have one are kept.`, yesText: 'Write them', extraContent: list });
      if (!ok) return;
      await perform(store, () => post('conformance', { all: true }), { okText: 'Conformance written.' });
    },
    async mapOne(row) {
      const ok = await confirmDialog({ title: `Map ${row.path}?`, text: `Runs claude (Sonnet) in map mode on ${row.path}: it writes up to 7 short documents in ${row.path}/docs/codebase/. It never touches the code. No spend cap.`, yesText: 'Map now' });
      if (!ok) return;
      await perform(store, () => post('map', { paths: [row.path] }), { keepError: true, okText: `Mapping ${row.path} started.` });
    },
    /** Retry of one failed line of the installer (no confirmation: the user already confirmed the run). */
    async mapRetry(row) {
      await perform(store, () => post('map', { paths: [row.path] }), { keepError: true, okText: `Mapping ${row.path} started.` });
    },
    /** One sentence of confirmation, then every subproject without a map starts at once and the system map follows. */
    async mapAll() {
      const rows = rowsMissingMap(store);
      const withSystem = !!store.data.system && (store.data.projects || []).filter((row) => !row.skip && row.exists !== false && row.has_code).length >= 2;
      if (!rows.length && !withSystem) return;
      const text = rows.length
        ? `Runs claude (Sonnet) on ${rows.length} subproject${rows.length === 1 ? '' : 's'} in parallel${withSystem ? ', then the system map' : ''}. It writes only docs/codebase; it never touches the code. No spend cap.`
        : 'Runs claude (Sonnet) to write the system map. It writes only SYSTEM.md; it never touches the code. No spend cap.';
      const ok = await confirmDialog({ title: rows.length ? 'Start the maps?' : 'Map the system?', text, yesText: 'Start' });
      if (!ok) return;
      await perform(store, () => (rows.length ? post('map', {}) : post('system-map', {})), { keepError: true, okText: 'Mapping started.' });
    },
    async systemMap() {
      const ok = await confirmDialog({ title: 'Map the system?', text: 'Runs claude (Sonnet) to write SYSTEM.md at the root: who talks to whom. It reads the subproject maps; it never touches the code. No spend cap.', yesText: 'Map the system' });
      if (!ok) return;
      await perform(store, () => post('system-map', {}), { keepError: true, okText: 'System map started.' });
    },
    rename: (path, name) => editDraft(store, (rows) => rows.map((row) => (row.path === path ? { ...row, name } : row))),
    toggleSkip: (path, skip) => editDraft(store, (rows) => rows.map((row) => (row.path === path ? { ...row, skip } : row))),
    removeRow: (path) => editDraft(store, (rows) => rows.filter((row) => row.path !== path)),
    async addFolder() {
      const root = repositoryRoot(store);
      const picked = await pickFolderNativeFirst({ initial: root, title: 'Add a subproject folder', buttonText: 'Add this folder', explanation: 'Choose a folder inside the repository.' });
      if (!picked) return;
      const relative = relativeToRoot(picked, root);
      if (relative == null) { toast(root ? `Choose a folder inside the repository (${root}).` : 'Cannot tell the repository folder: reopen the project and try again.', 'err'); return; }
      if (!relative) { toast('Choose a folder inside the repository, not the repository itself.', 'err'); return; }
      if (draftRows(store).some((row) => row.path === relative)) { toast(`${relative} is already in the list.`, 'err'); return; }
      editDraft(store, (rows) => [...rows, { path: relative, name: relative.split('/').pop(), stack: 'unknown', skill: '', skip: false, reason: 'added by you', has_code: true, exists: true, conformance: null, suggestion: null, map: { files: 0 }, giant_files: 0 }]);
    },
  };
}

/** Standards slots of one subproject row: GET/PUT subprojects/standards (path in the query for GET and in the body for PUT; ?repo= like the rest). */
const standardsOf = (store) => (row) => ({
  get: () => apiOf(store).get(routeFor(store, 'standards') + (store.key ? '&' : '?') + 'path=' + encodeURIComponent(row.path)),
  put: (bindings) => apiOf(store).put(routeFor(store, 'standards'), { path: row.path, bindings }),
});

/** <SubprojectsPanel repo? client? />: the table with every action. `repo` = repo name and `repoPath` = its folder (repositories dialog; Add folder is made relative to it), `client` = project client when ctx.api is not the right one. */
export function SubprojectsPanel({ repo = '', client = null, repoPath = '' }) {
  const { store, actions } = useSubprojectsStore(repo, client, repoPath);
  if (!store.data && store.loadError) return html`<${LoadFailed} store=${store} repo=${repo} client=${client} />`;
  return html`<${SubprojectsTable} data=${store.data} draft=${store.draft} busy=${store.busy} mapError=${store.mapError} actions=${actions} />`;
}

/** <SubprojectsInstallerPanel gates onContinue repo? repoPath? client? />: the monorepo setup of the planning screen (3 steps, Back / Next).
 *  gates = { subprojects, conformance, map } booleans (true = the back end says the step passes); onContinue() runs after the last step.
 *  repo = name of another repository of the project ('' = the project's own repo), repoPath = its folder, client = project client. */
export function SubprojectsInstallerPanel({ gates, onContinue, repo = '', repoPath = '', client = null, skipConformance = false, finishLabel = undefined }) {
  const { store, actions } = useSubprojectsStore(repo, client, repoPath);
  if (!store.data && store.loadError) return html`<${LoadFailed} store=${store} repo=${repo} client=${client} />`;
  return html`<${SubprojectsInstaller} data=${store.data} draft=${store.draft} busy=${store.busy} mapError=${store.mapError} actions=${actions} gates=${gates} onContinue=${onContinue} skipConformance=${skipConformance} finishLabel=${finishLabel} standards=${standardsOf(store)} />`;
}

/** Shared by the table and the installer: subscribes to the store of `repo`, loads it and returns { store, actions }. */
function useSubprojectsStore(repo, client, repoPath) {
  const store = storeFor(repo);
  if (repoPath) store.rootPath = repoPath;
  const [, redraw] = useState(0);
  useEffect(() => {
    const listener = () => redraw((n) => n + 1);
    store.listeners.add(listener);
    loadSubprojects(repo, client);
    return () => { store.listeners.delete(listener); if (!store.listeners.size && store.timer) { clearTimeout(store.timer); store.timer = null; } };
  }, [repo]);
  return { store, actions: buildActions(store) };
}
const LoadFailed = ({ store, repo, client }) => html`<div class="sp-note sp-note-bad" role="alert">${'Could not read the subprojects: ' + store.loadError}
  <button type="button" class="btn btn-sm" onClick=${() => loadSubprojects(repo, client)}>Try again</button></div>`;

/** Snapshot of the last loaded description of a repo ('' = own repo), for trail/wizard summaries. */
export const subprojectsData = (repo = '') => storeFor(repo).data;
