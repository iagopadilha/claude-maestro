/* Scope of the repositories, defined BEFORE the loop and outside the roadmap/loop screen (a dialog opened from the Workspace line).
   Two ways, both ending in the same clear list of WHICH repositories will be changed:
   - with the AI: describe the work, POST projects/<id>/repos/scope/propose returns repos from the catalog with a reason each; the user
     ticks and adds them (nothing is added by the AI alone);
   - by hand: the RepoManager below (several-repositories checkbox, catalog list, "Add folder").
   "Confirm repositories" (POST .../repos/scope/confirm) marks the scope as defined, even when it is only the primary repository.
   The back end decides everything (what is allowed, what is refused); this screen only shows it. */
import { html, useState, useEffect, useRef } from '../util/preact.js';
import { list, txt } from '../util/format.js';
import { appApi } from '../api/api_client.js';
import { toast } from '../components/toast.js';
import { openDialog } from '../components/dialog.js';
import { RepoManager } from './repo_manager.js';

const reposRoute = (id) => 'projects/' + encodeURIComponent(id) + '/repos';
const PROPOSE_TIMEOUT_MS = 200000;

/** The one list that says which repositories will be changed (primary first), and the confirm button while it is not confirmed yet. */
function ScopeSummary({ data, busy, locked, onConfirm }) {
  const repos = data ? data.repos : [];
  return html`<section class="rs-summary" aria-label="Repositories that will be changed">
    <div class="small-label">Repositories that will be changed</div>
    ${data ? html`<ul class="rs-list">${repos.map((r) => html`<li key=${r.name}><b class="mono">${r.name}</b>${r.primary ? html` <span class="rp-tag mono">primary</span>` : null}
      <span class="dim mono rs-path" title=${r.path}>${txt(r.path)}</span></li>`)}</ul>` : html`<div class="hint" role="status">reading the repositories…</div>`}
    ${data ? html`<div class="card-row">${data.defined
      ? html`<span class="ws-chip mono tone-ok"><span class="dot"></span>confirmed: only these repositories are touched</span>`
      : html`<button type="button" class="btn btn-sm btn-primary" id="rs-confirm" disabled=${!!locked || busy} onClick=${onConfirm}>${busy ? 'Confirming…' : 'Confirm these repositories'}</button>
        <span class="hint">The loop will not start until you confirm which repositories it changes.</span>`}</div>` : null}
  </section>`;
}

/** Describe the work, the AI proposes repositories from the catalog; the user ticks and adds. */
function AiProposal({ projectId, locked, onAdded }) {
  const [text, setText] = useState('');
  const [state, setState] = useState({ busy: false, error: '', message: '', rows: null, off: {}, adding: false });
  const alive = useRef(true);
  useEffect(() => () => { alive.current = false; }, []);
  const patch = (p) => { if (alive.current) setState((s) => ({ ...s, ...p })); };
  const propose = async () => {
    patch({ busy: true, error: '', message: '', rows: null, off: {} });
    try {
      const r = await appApi.post(reposRoute(projectId) + '/scope/propose', { description: text }, PROPOSE_TIMEOUT_MS);
      patch({ busy: false, rows: list(r.proposed), message: r.ok === false ? '' : String(r.message || ''), error: r.ok === false ? String(r.message || 'The AI did not answer.') : '' });
    } catch (e) { patch({ busy: false, error: (e.status === 409 ? '' : 'Failed: ') + e.message }); }
  };
  const addTicked = async () => {
    patch({ adding: true, error: '' });
    for (const row of list(state.rows).filter((x) => !state.off[x.name])) {
      try { await appApi.post(reposRoute(projectId), { path: row.path }, 30000); }
      catch (e) { patch({ adding: false, error: row.name + ': ' + e.message }); onAdded(); return; }
    }
    patch({ adding: false, rows: null, message: '' });
    toast('Repositories added.', 'ok'); onAdded();
  };
  const rows = list(state.rows), ticked = rows.filter((x) => !state.off[x.name]);
  return html`<section class="rs-ai" aria-label="Propose repositories with the AI">
    <div class="small-label">With the AI</div>
    <label class="dlg-text" for="rs-desc">Describe what will be built or changed. The AI proposes, from the repository catalog, the other repositories it would touch.</label>
    <textarea id="rs-desc" class="rs-desc" rows="3" maxlength="2000" disabled=${locked || state.busy} value=${text}
      placeholder="e.g. add an orders endpoint to the API and show it on the orders screen" onInput=${(e) => setText(e.currentTarget.value)}></textarea>
    <div class="card-row"><button type="button" class="btn btn-sm" id="rs-propose" disabled=${!!locked || state.busy || !text.trim()} onClick=${propose}>${state.busy ? 'Asking the AI…' : 'Propose repositories'}</button>
      ${state.busy ? html`<span class="spinner sm" aria-hidden="true"></span>` : null}</div>
    ${state.error ? html`<div class="nl-box nl-bad" role="alert">${state.error}</div>` : null}
    ${state.message ? html`<div class="hint" role="status">${state.message}</div>` : null}
    ${rows.length ? html`<ul class="rp-list" aria-label="Proposed repositories">${rows.map((c) => html`<li key=${c.name} class="rp-row">
      <div class="rp-line"><input type="checkbox" id=${'rs-pick-' + c.name} checked=${!state.off[c.name]} disabled=${state.adding}
        onChange=${(e) => patch({ off: { ...state.off, [c.name]: !e.currentTarget.checked } })} /><label class="rp-name mono" for=${'rs-pick-' + c.name}>${c.name}</label></div>
      <div class="rp-path mono" title=${c.path}>${txt(c.path)}</div>
      ${c.reason ? html`<div class="rp-purpose">${c.reason}</div>` : null}</li>`)}</ul>
      <div class="card-row"><button type="button" class="btn btn-sm btn-primary" id="rs-add" disabled=${!!locked || state.adding || !ticked.length} onClick=${addTicked}>${state.adding ? 'Adding…' : `Add ticked repositories (${ticked.length})`}</button></div>` : null}
  </section>`;
}

function RepoScope({ projectId, locked, onChange }) {
  const [data, setData] = useState(null);
  const [version, setVersion] = useState(0);
  const [busy, setBusy] = useState(false);
  const alive = useRef(true);
  useEffect(() => () => { alive.current = false; }, []);
  const load = async () => {
    try { const d = await appApi.get(reposRoute(projectId), 15000); if (alive.current) setData({ ...d, repos: list(d.repos), defined: d.defined !== false }); }
    catch (_) { if (alive.current) setData(null); }
  };
  useEffect(() => { load(); }, [projectId, version]);
  const changed = () => { setVersion((v) => v + 1); if (onChange) onChange(); };
  const confirm = async () => {
    setBusy(true);
    try { await appApi.post(reposRoute(projectId) + '/scope/confirm', {}, 15000); toast('Repositories confirmed.', 'ok'); changed(); }
    catch (e) { toast('Not confirmed: ' + e.message, 'err'); }
    finally { if (alive.current) setBusy(false); }
  };
  return html`<div class="rs">
    <${ScopeSummary} data=${data} busy=${busy} locked=${locked} onConfirm=${confirm} />
    <${AiProposal} projectId=${projectId} locked=${locked} onAdded=${changed} />
    <section class="rs-hand" aria-label="Choose repositories by hand"><div class="small-label">By hand</div>
      <${RepoManager} key=${version} projectId=${projectId} locked=${locked} onChange=${() => { load(); if (onChange) onChange(); }} /></section>
  </div>`;
}

/** Opens the dialog. onChange runs after any change (the Workspace line reads again). */
export function openRepoScopeDialog({ projectId, locked = '', onChange }) {
  openDialog({
    title: 'Repositories of this project', width: 760, cls: 'rs-dlg',
    content: html`<${RepoScope} projectId=${projectId} locked=${locked} onChange=${onChange} />`,
    actions: [{ text: 'Close' }],
  });
}
