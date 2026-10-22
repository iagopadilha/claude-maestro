/* Wrap-up of a finished loop (FUP-01..05): the view in the LOOP column when the loop is done (or stopped) and still has a worktree,
   and the Finish dialog. The back end decides what is safe (GET wrap-up, POST wrap-up/finish); this module only shows it.
   Routes: GET wrap-up, POST wrap-up/finish {mode: safe|keep_worktree|remove_anyway, confirm_text?, accept_unreviewed?},
   GET follow-up, POST follow-up/send {text, decisions}, POST follow-up/run, GET decisions. */
import { html, useState, useEffect, useCallback, useRef } from '../../util/preact.js';
import { list, txt, cost, isNum, httpUrl } from '../../util/format.js';
import { toast } from '../../components/toast.js';
import { openDialog } from '../../components/dialog.js';
import { ctx, status } from './context.js';
import { renderAll, schedulePoll } from '../project_panel.js';
import { repoInfo } from './header.js';
import { DecisionsCount, openDecisionsDialog } from './decisions.js';
import { openReopenPlanning } from './reopen_planning.js';
import { FollowUpChat } from './follow_up_chat.js';

const POLL_MS = 4000;
const COMMIT_FIRST_MESSAGE = 'chore(loop): commit the pending work before finishing';
const FINISH_MODES = { safe: 'safe', keep: 'keep_worktree', remove: 'remove_anyway' };

/** True when the LOOP column shows the wrap-up: the loop is done (or stopped) and its worktree still exists. */
export function wrapUpVisible() {
  return ['done', 'stopped'].includes(status()) && !!repoInfo().worktree;
}

const Link = ({ url, text }) => (httpUrl(url) ? html`<a href=${url} target="_blank" rel="noopener noreferrer">${text}</a>` : null);
const countOf = (value) => (Array.isArray(value) ? value.length : Number(value) || 0);

// ---------- Finish dialog ----------
/** Per-repository line of the close check: pushed or not, link, uncommitted or unpushed work. Keys are read defensively.
 * The headline comes from the same verdict as the body: the repository is safe only when the back end says it can close (`can_close`, with
 * its `reasons`) and the refused Finish (`blocked`: the repo names the 409 listed) does not name it. */
function RepoCheck({ repo, blocked = [] }) {
  const dirty = countOf(repo.uncommitted != null ? repo.uncommitted : repo.dirty), unpushed = countOf(repo.unpushed);
  const reasons = list(repo.reasons).map((reason) => txt(reason && (reason.text || reason.code))).filter(Boolean);
  const name = repo.repo || repo.name;
  const safe = !dirty && !unpushed && repo.pushed !== false && repo.can_close !== false && !reasons.length && !blocked.includes(name);
  const pushed = repo.pushed === true || (repo.pushed == null && typeof repo.push === 'string' && repo.push.startsWith('done'));
  return html`<li class=${'wu-repo' + (safe ? ' is-safe' : ' is-risk')}>
    <span class="wu-repo-name mono">${txt(name)}</span>
    <span class="wu-repo-facts">${[
      pushed ? 'pushed' : repo.pushed === false ? 'not pushed' : null,
      dirty ? `${dirty} uncommitted file${dirty === 1 ? '' : 's'}` : null,
      unpushed ? `${unpushed} unpushed commit${unpushed === 1 ? '' : 's'}` : null,
      ...reasons,
      !safe && !dirty && !unpushed && !reasons.length ? 'not safe to remove yet' : null,
      safe ? 'nothing to lose' : null,
    ].filter(Boolean).join(' · ')}</span>
    <span class="wu-repo-links"><${Link} url=${repo.pr_url} text="PR" /><${Link} url=${repo.compare_url} text="Compare" /></span></li>`;
}

/** What a refused Finish (409) found: files and commits per repository. */
function Reasons({ reasons }) {
  return html`<ul class="wu-reasons" aria-label="What would be lost">${reasons.map((reason, index) => html`<li key=${index} class="wu-reason">
    <div><b class="mono">${txt(reason.repo)}</b> ${reason.message ? html`<span>${reason.message}</span>` : null}</div>
    ${list(reason.files).length ? html`<div class="wu-lost"><span class="wu-lost-k">Uncommitted files</span><ul>${list(reason.files).map((file) => html`<li class="mono">${typeof file === 'string' ? file : file.path || JSON.stringify(file)}</li>`)}</ul></div>` : null}
    ${list(reason.commits).length ? html`<div class="wu-lost"><span class="wu-lost-k">Unpushed commits</span><ul>${list(reason.commits).map((commit) => html`<li class="mono">${typeof commit === 'string' ? commit : [commit.sha && String(commit.sha).slice(0, 8), commit.subject || commit.message].filter(Boolean).join(' ')}</li>`)}</ul></div>` : null}
  </li>`)}</ul>`;
}

/** Receipt after a successful Finish. */
function Receipt({ receipt }) {
  const r = receipt || {};
  const row = (key, value) => (value ? html`<div class="z-kv"><span class="z-kv-k wu-kv-k">${key}</span><span class="z-kv-v wu-kv-v mono" title=${String(value)}>${value}</span></div>` : null);
  const url = r.pr_url || r.compare_url;
  return html`<div class="wu-receipt" role="status">
    <p class="dlg-text">The loop was finished. The plan is back in the repository.</p>
    ${url ? html`<div class="z-kv"><span class="z-kv-k wu-kv-k">PR</span><span class="z-kv-v wu-kv-v"><${Link} url=${url} text=${r.pr_url ? 'Open the pull request' : 'Open the compare page'} /></span></div>` : null}
    ${row('branch kept', r.branch)}${row('backup', r.backup_dir || r.backup_folder || r.backup)}
    ${row('worktree', r.worktree_removed === true ? 'removed' : r.worktree_removed === false ? 'kept' : '')}
    ${row('total cost', isNum(r.cost_usd) ? cost(r.cost_usd) + (isNum(r.max_cost_usd) ? ' of ' + cost(r.max_cost_usd) + ' cap' : '') : '')}
    ${row('turns', isNum(r.turns) ? String(r.turns) : '')}</div>`;
}

/** Opens the Finish dialog: runs GET wrap-up and shows the check per repository, then the three outcomes when it is not safe.
    `onDone()` runs once after the dialog closes following a successful Finish. */
export function openFinishDialog({ state, project, onDone } = {}) {
  const s = { data: null, error: '', busy: false, reasons: null, receipt: null, accept: false, typed: '', removing: false, note: '', api: null, finished: false };
  const unreviewed = () => countOf(s.data && s.data.unreviewed_critical);
  const blockedByDecisions = () => unreviewed() > 0 && !s.accept;
  const confirmName = () => txt((list(s.reasons)[0] && list(s.reasons)[0].repo) || (list(s.data && s.data.check && s.data.check.repos)[0] || {}).repo || (state && state.project) || project);

  const finish = async (mode, extra = {}) => {
    s.busy = true; s.error = ''; draw();
    try {
      const body = { mode, ...extra };
      if (s.accept) body.accept_unreviewed = true;
      const res = await ctx.api.post('wrap-up/finish', body, 120000);
      s.receipt = res.receipt || {}; s.finished = true; s.reasons = null;
    } catch (failure) {
      const reasons = failure.status === 409 && failure.body && list(failure.body.reasons);
      if (reasons && reasons.length) { s.reasons = reasons; s.removing = false; s.typed = ''; } else s.error = failure.message;
    } finally { s.busy = false; draw(); }
  };

  /** "Commit and push them first": really commits and pushes the pending work of the loop worktree of each blocked repository (the guarded
      commit-push route acts on a loop/* worktree only), then re-runs the check and, when it is clean, the safe Finish. */
  const commitFirst = async () => {
    s.busy = true; s.error = ''; s.note = ''; draw();
    try {
      const names = [...new Set(list(s.reasons).filter((reason) => reason.repo && (list(reason.files).length || list(reason.commits).length)).map((reason) => reason.repo))];
      const done = [];
      for (const name of names) {
        const res = await ctx.api.post('workspace/repos/' + encodeURIComponent(name) + '/commit-push', { message: COMMIT_FIRST_MESSAGE }, 120000);
        done.push(`${name}: ${res.committed ? 'committed' : 'nothing new to commit'}${res.pushed ? ', pushed ' + txt(res.branch) : ''}`);
      }
      s.data = await ctx.api.get('wrap-up', 30000);
      // a missing PR (no gh / non-GitHub remote) is a note, not a blocker: the safe Finish lifts it (same rule as the back end's blocking_reasons)
      const clean = list(s.data.check && s.data.check.repos).every((repo) => !list(repo.reasons).some((reason) => reason.code !== 'pr_missing'));
      s.note = done.join(' · ');
      if (clean) { s.reasons = null; s.busy = false; await finish(FINISH_MODES.safe); return; }
      s.note += (s.note ? ' · ' : '') + 'something still blocks the safe Finish: see the check above';
    } catch (failure) { s.error = failure.message; } finally { s.busy = false; draw(); }
  };

  const body = () => {
    if (s.receipt) return html`<${Receipt} receipt=${s.receipt} />`;
    if (s.error && !s.data) return html`<div class="nl-box nl-bad" role="alert">${s.error}</div>`;
    if (!s.data) return html`<div class="hint" role="status">Checking every repository…</div>`;
    const repos = list(s.data.check && s.data.check.repos);
    const delivery = s.data.delivery || {};
    return html`<div class="wu-dlg">
      <p class="dlg-text">${delivery.remove_worktree_on_finish === false ? 'Finishing runs the delivery (push and pull request) and keeps the worktree.' : 'Finishing delivers what is missing, saves a backup, puts the plan back in the repository and removes the worktree. The branch stays.'}</p>
      ${repos.length ? html`<ul class="wu-repos" aria-label="Check per repository">${repos.map((repo, index) => html`<${RepoCheck} key=${index} repo=${repo} blocked=${list(s.reasons).map((reason) => reason.repo)} />`)}</ul>` : html`<div class="hint">No repository reported.</div>`}
      ${unreviewed() ? html`<div class="nl-box nl-warn" role="alert"><strong>${`${unreviewed()} critical decision${unreviewed() === 1 ? '' : 's'} not reviewed.`}</strong>
        <div><button type="button" class="btn btn-sm" onClick=${() => openDecisionsDialog({ project, state })}>Review them</button></div>
        <label class="nl-check"><input type="checkbox" checked=${s.accept} disabled=${s.busy} onChange=${(e) => { s.accept = e.currentTarget.checked; draw(); }} /><span>Finish without reviewing them</span></label></div>` : null}
      ${s.reasons ? html`<div class="nl-box nl-bad" role="alert"><strong>Not safe to remove yet: there is work that exists only in the worktree.</strong></div><${Reasons} reasons=${s.reasons} />
        <div class="wu-options">
          <div class="wu-option"><button type="button" class="btn btn-sm btn-primary" disabled=${s.busy || blockedByDecisions()} onClick=${commitFirst}>Commit and push them first</button>
            <span class="hint">Commits what is pending in the loop worktree and pushes the loop branch, then checks again and finishes if it is safe.</span></div>
          <div class="wu-option"><button type="button" class="btn btn-sm" disabled=${s.busy || blockedByDecisions()} onClick=${() => finish(FINISH_MODES.keep)}>Keep the worktree</button>
            <span class="hint">Finishes without removing anything.</span></div>
          <div class="wu-option"><button type="button" class="btn btn-sm btn-danger" disabled=${s.busy} onClick=${() => { s.removing = !s.removing; draw(); }}>Remove anyway</button>
            <span class="hint">Deletes the worktree with the files and commits above. The backup still runs and the branch stays.</span></div>
          ${s.removing ? html`<div class="wu-confirm"><label class="nl-label" for="wu-typed">${`Type ${confirmName()} to confirm that the work listed above is lost`}</label>
            <input id="wu-typed" class="dlg-input mono" type="text" autocomplete="off" value=${s.typed} onInput=${(e) => { s.typed = e.currentTarget.value; draw(); }} />
            <button type="button" class="btn btn-sm btn-danger" disabled=${s.busy || s.typed.trim() !== confirmName() || blockedByDecisions()}
              onClick=${() => finish(FINISH_MODES.remove, { confirm_text: s.typed.trim() })}>Remove the worktree and lose that work</button></div>` : null}
        </div>` : null}
      ${s.note ? html`<div class="nl-box nl-ok" role="status">${s.note}</div>` : null}
      ${s.error ? html`<div class="nl-box nl-bad" role="alert">${s.error}</div>` : null}
    </div>`;
  };
  const actions = () => {
    if (s.receipt) return [{ text: 'Close', onClick: (a) => a.close() }, { text: 'Back to planning', cls: 'btn-primary', onClick: (a) => { a.close(); openReopenPlanning(); } }];
    const canFinish = !!s.data && !s.busy && !s.reasons && !blockedByDecisions();
    return [{ text: 'Cancel', onClick: (a) => a.close() }, { text: s.busy ? 'Finishing…' : 'Finish', cls: 'btn-primary', disabled: !canFinish, onClick: () => finish(FINISH_MODES.safe) }];
  };
  const draw = () => { if (s.api) { s.api.setContent(body()); s.api.setActions(actions()); } };

  s.api = openDialog({ title: 'Finish the loop', width: 640, cls: 'dlg-finish', content: body(), actions: actions(),
    onClose: () => { if (s.finished && onDone) onDone(); } });
  ctx.api.get('wrap-up', 30000).then((data) => { s.data = data; draw(); }).catch((failure) => { s.error = failure.message; draw(); });
}

// ---------- Wrap-up view ----------
function useWrapUp() {
  const [wrap, setWrap] = useState(null);
  const [followUp, setFollowUp] = useState(null);
  const [decisions, setDecisions] = useState([]);
  const [error, setError] = useState('');
  const reload = useCallback(async () => {
    try {
      const [wrapData, followData, decisionData] = await Promise.all([ctx.api.get('wrap-up', 20000), ctx.api.get('follow-up', 20000), ctx.api.get('decisions', 20000).catch(() => ({ items: [] }))]);
      setWrap(wrapData); setFollowUp(followData); setDecisions(list(decisionData.items)); setError('');
    } catch (failure) { setError(failure.message); }
  }, []);
  useEffect(() => { reload(); const timer = setInterval(reload, POLL_MS); return () => clearInterval(timer); }, [reload]);
  return { wrap, followUp, decisions, error, reload };
}

function Summary({ summary, delivery }) {
  const s = summary || {};
  const fact = (key, value) => html`<div class="wu-fact"><div class="z-figure-k">${key}</div><div class="mono">${value}</div></div>`;
  return html`<div class="wu-facts">
    ${fact('FEATURES DONE', isNum(s.features_done) ? s.features_done : '—')}
    ${fact('TURNS', isNum(s.turns) ? s.turns : '—')}
    ${fact('COST', isNum(s.cost_usd) ? cost(s.cost_usd) + (isNum(s.max_cost_usd) ? ' / ' + cost(s.max_cost_usd) : '') : '—')}
    <div class="wu-fact"><div class="z-figure-k">DELIVERY</div><div>${httpUrl(s.pr_url) ? html`<${Link} url=${s.pr_url} text="Pull request" />` : httpUrl(s.compare_url) ? html`<${Link} url=${s.compare_url} text="Compare page" />` : delivery && delivery.done ? html`<span>pushed to origin (no PR link for this remote)</span>` : html`<span class="muted">not delivered yet</span>`}</div></div></div>`;
}

/** The view of the LOOP column when the loop is done or stopped with a worktree. */
export function WrapUp() {
  const { wrap, followUp, decisions, error, reload } = useWrapUp();
  const finished = !!wrap && ['finished', 'closed', 'no_loop'].includes(wrap.status);
  const finish = () => openFinishDialog({ state: ctx.state, project: ctx.projectId, onDone: () => { renderAll(); schedulePoll(300); } });
  return html`<section class="wu" aria-label="Wrap-up">
    <div class="wu-head"><span class="z-badge z-badge-loop">WRAP-UP</span><span class="wu-title">${finished ? 'Finished' : status() === 'stopped' ? 'The loop was stopped' : 'The loop is done'}</span>
      <span class="z-spacer"></span>
      ${finished ? html`<button type="button" class="btn btn-sm btn-primary" onClick=${openReopenPlanning}>Back to planning</button>` : html`<button type="button" class="btn btn-sm btn-primary" onClick=${finish}>Finish</button>`}</div>
    ${error && !wrap ? html`<div class="nl-box nl-bad" role="alert">${'Could not read the wrap-up: ' + error}</div>` : null}
    ${wrap ? html`<${Summary} summary=${wrap.summary} delivery=${wrap.delivery} />
      <div class="wu-decisions"><${DecisionsCount} project=${ctx.projectId} state=${ctx.state} /></div>` : html`<div class="hint">Reading the wrap-up…</div>`}
    ${finished ? html`<p class="wu-hint">The worktree is gone, so there is no follow-up for this run. Go back to planning for the next work.</p>`
      : html`<${FollowUpChat} followUp=${followUp} decisions=${decisions} reload=${reload} locked=${status() === 'running'} />`}
  </section>`;
}
