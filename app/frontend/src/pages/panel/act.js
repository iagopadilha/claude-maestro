/* Actions (inside the composer's Chat mode): concrete actions on the current version (back-end whitelist), always with an explicit confirmation. */
import { html, useState } from '../../util/preact.js';
import { num, txt, list, httpUrl, shortPath, fmtHour } from '../../util/format.js';
import { toast } from '../../components/toast.js';
import { copyText } from '../../components/copy.js';
import { ctx } from './context.js';
import { renderAll, schedulePoll } from '../project_panel.js';

const ACTION_TIMEOUT_MS = 15 * 60 * 1000; // cleaning worktrees backs up first: it can take a while
export const actState = { actions: null, loading: false, err: null, card: null, seq: 0 };
const FALLBACK_TITLES = { deliver: 'Deliver: push the branch + PR to the integration branch', summary: 'Generate the loop summary', push: 'Push the feature branches (no PR)', clean_worktrees: 'Clean loop worktrees (backup first)' };
const WORKTREE_ACTIONS = ['push', 'clean_worktrees'];
const KNOWN_ACTIONS = ['deliver', 'summary', 'push', 'clean_worktrees'];
const INSTRUCTION_MESSAGES = { note: 'Noted for the next turn.', answer: 'Answered, resuming the loop.', 'note+resume': 'Noted and loop resumed.' };

export async function loadActions() {
  if (actState.loading) return;
  actState.loading = true; actState.err = null; renderAll();
  try { const r = await ctx.api.get('actions', 70000); actState.actions = list(r && r.actions); }
  catch (e) { actState.err = e.status === 404 ? 'The server does not offer the action list yet.' : e.message; }
  finally { actState.loading = false; renderAll(); }
}
const findAction = (id) => list(actState.actions).find((a) => a.id === id) || { id, title: FALLBACK_TITLES[id] || id, available: true, preview: {} };
function openAction(action, extra) {
  actState.seq += 1;
  const wts = list(action.preview && action.preview.worktrees), want = list(extra && extra.targets);
  let sel = [];
  if (WORKTREE_ACTIONS.includes(action.id)) sel = want.length ? wts.filter((w) => want.some((x) => x === w.path || shortPath(x) === shortPath(w.path) || x === w.branch)).map((w) => w.path) : action.id === 'push' ? wts.map((w) => w.path) : [];
  actState.card = { kind: 'confirm', action, sel, typed: {}, ...(extra || {}) }; renderAll();
}
function cancelAction() { actState.seq += 1; actState.card = null; renderAll(); }
export async function proposeAction(text) {
  const seq = ++actState.seq;
  actState.card = { kind: 'proposing', text }; renderAll();
  try {
    let r = await ctx.api.post('action/propose', { text });
    let guard = 0;
    while (!r.proposal && r.job && guard < 60) {
      guard += 1;
      await new Promise((res) => setTimeout(res, 1500));
      if (seq !== actState.seq) return;
      let j;
      try { j = await ctx.api.get('action/job/' + encodeURIComponent(r.job), 10000); }
      catch (e) { if (e.status === 404) j = await ctx.api.get('chat/job/' + encodeURIComponent(r.job), 10000); else throw e; }
      if (j.state === 'error') throw new Error(j.error || 'could not understand the request');
      if (j.state === 'ok') {
        let p = j.proposal || null;
        if (!p && typeof j.reply === 'string') { try { p = JSON.parse(j.reply); } catch (_) { p = null; } }
        r = { proposal: p, cost_usd: j.cost_usd };
      }
    }
    if (seq !== actState.seq) return;
    const p = r.proposal || {};
    if (KNOWN_ACTIONS.includes(p.action)) openAction(findAction(p.action), { text, explanation: p.explanation, confidence: p.confidence, cost: r.cost_usd, targets: list(p.targets) });
    else { actState.card = { kind: 'none', text, explanation: p.explanation }; renderAll(); }
  } catch (e) { if (seq === actState.seq) { actState.card = { kind: 'result', action: { id: 'propose', title: 'Understand the request' }, ok: false, error: e.message }; renderAll(); } }
}
async function sendAsInstruction() {
  const c = actState.card; if (!c || c.kind !== 'none') return;
  try {
    const r = await ctx.api.post('instruction', { text: c.text, feature: null, attachments: [] });
    const msg = INSTRUCTION_MESSAGES[r.action] || 'Instruction sent.';
    toast(msg, 'ok'); actState.card = null; schedulePoll(800);
  } catch (e) { toast('Instruction not sent: ' + e.message, 'err'); }
  renderAll();
}
const dirtySel = (c) => list(c.action.preview && c.action.preview.worktrees).filter((w) => c.sel.includes(w.path) && w.dirty);
const typedOk = (c) => dirtySel(c).every((w) => (c.typed[w.path] || '').trim() === w.branch);
// only called by the click on "Confirm and run" / "Delete selected worktrees"
async function runAction() {
  const c = actState.card; if (!c || c.kind !== 'confirm' || c.running) return;
  const action = c.action, wt = WORKTREE_ACTIONS.includes(action.id);
  if (wt && (!c.sel.length || (action.id === 'clean_worktrees' && !typedOk(c)))) return;
  const body = { action: action.id, confirm: true };
  if (wt) { body.targets = c.sel.slice(); if (action.id === 'clean_worktrees' && dirtySel(c).length) body.force = true; }
  const seq = ++actState.seq;
  actState.card = { kind: 'running', action }; renderAll();
  try {
    const r = await ctx.api.post('action/run', body, ACTION_TIMEOUT_MS);
    if (seq === actState.seq) actState.card = { kind: 'result', action, ok: true, res: r };
    toast(`${action.title || action.id}: done.`, 'ok');
  } catch (e) {
    if (seq === actState.seq) actState.card = { kind: 'result', action, ok: false, error: e.message, output: e.body && e.body.output ? String(e.body.output) : '', push: e.body && e.body.push ? String(e.body.push) : '', rows: list(e.body && e.body.results) };
  }
  schedulePoll(400);
  renderAll(); loadActions();
}

const Spinner = () => html`<div class="spinner" aria-hidden="true"></div>`;
const Pre = ({ text }) => (text ? html`<pre class="ac-pre">${String(text)}</pre>` : null);
const Row = ({ k, v }) => (v == null || v === '' ? null : html`<div class="ac-row"><span class="ac-k">${k}</span><span class="ac-v">${String(v)}</span></div>`);
function PreviewRows({ a }) {
  const p = a.preview || {};
  if (a.id === 'deliver') {
    const pr = httpUrl(p.existing_pr), of = list(p.open_features);
    return html`<${Row} k="branch" v=${p.branch} /><${Row} k="base" v=${p.base || 'integration branch'} />
      <div class="ac-row"><span class="ac-k">PR</span>${pr ? html`<a href=${pr} target="_blank" rel="noopener noreferrer">${pr.replace(/^https?:\/\/(www\.)?github\.com\//i, '')}</a>`
        : html`<span class="ac-v">${p.existing_pr ? String(p.existing_pr) : 'none yet (will be created)'}</span>`}</div>
      <${Row} k="open features" v=${of.length ? of.join(', ') : 'none'} />
      ${p.push ? html`<div class="ac-k">what delivery.py will do (dry-run)</div><${Pre} text=${p.push} />` : null}`;
  }
  if (a.id === 'summary') return html`<${Row} k="destination" v=${p.destination} />`;
  if (WORKTREE_ACTIONS.includes(a.id)) return null;
  return Object.entries(p).map(([k, v]) => html`<${Row} key=${k} k=${k} v=${typeof v === 'object' ? JSON.stringify(v) : v} />`);
}
// worktree table with checkboxes; a change re-renders only the confirmation card
function WorktreeTable({ c, onChange }) {
  const wts = list(c.action.preview && c.action.preview.worktrees);
  if (!wts.length) return html`<div class="ac-expl ac-off">No loop worktree found.</div>`;
  const toggle = (w, on) => { c.sel = on ? [...c.sel.filter((x) => x !== w.path), w.path] : c.sel.filter((x) => x !== w.path); onChange(); };
  return html`<div class="ac-tw"><table class="ac-table">
    <thead><tr><th></th><th>worktree</th><th>branch</th><th>changes</th><th>unpushed</th><th>specs</th></tr></thead>
    <tbody>${wts.map((w) => html`<tr key=${w.path}>
      <td><input type="checkbox" aria-label=${'select ' + shortPath(w.path)} checked=${c.sel.includes(w.path)} onChange=${(e) => toggle(w, e.currentTarget.checked)} /></td>
      <td class="ac-td-p" title=${w.path}>${shortPath(w.path).split('/').pop()}</td><td class="ac-td-b">${txt(w.branch)}</td>
      <td>${w.dirty ? html`<span class="ac-tag bad">dirty</span>` : html`<span class="ac-tag">clean</span>`}</td>
      <td class="ac-td-n">${num(w.unpushed_commits)}</td><td>${w.has_specs ? html`<span class="ac-tag">yes</span>` : html`<span class="ac-tag">no</span>`}</td>
    </tr>`)}</tbody></table></div>`;
}
function ResultRows({ r, c }) {
  const rows = list((r && r.results) || (c && c.rows));
  if (!rows.length) return null;
  return html`<div class="ac-res">${rows.map((x, i) => {
    const okv = !(!!x.error || x.ok === false || x.deleted === false);
    return html`<div key=${i} class=${'ac-res-row ' + (okv ? 'ok' : 'bad')}>
      <span class="ac-res-i">${okv ? '✓' : '✗'}</span><span class="ac-res-p" title=${x.path}>${shortPath(x.path) || txt(x.branch)}</span>
      <span class="ac-res-s">${x.error ? 'error: ' + x.error : x.deleted ? 'deleted' : x.ok ? 'ok' : x.deleted === false ? 'not deleted' : 'ok'}</span>
      ${x.output ? html`<span class="ac-res-o">${String(x.output)}</span>` : null}
    </div>`;
  })}</div>`;
}
function ConfirmCard({ c }) {
  const [, bump] = useState(0);
  const changed = () => bump((x) => x + 1);
  const a = c.action, off = a.available === false, wt = WORKTREE_ACTIONS.includes(a.id), danger = a.id === 'clean_worktrees';
  const ds = danger ? dirtySel(c) : [];
  const ok = !off && !(wt && !c.sel.length) && !(danger && !typedOk(c));
  const backupDir = a.preview && a.preview.backup_dir;
  return html`<div class=${'ac-card ' + (danger ? 'ac-danger' : 'ac-confirm')}>
    <div class="ac-kick">${danger ? 'DESTRUCTIVE ACTION · CONFIRM CAREFULLY' : 'CONFIRM BEFORE RUNNING'}</div>
    <div class="ac-title">${a.title || a.id}</div>
    ${c.explanation ? html`<div class="ac-expl">${`The AI understood: ${c.explanation}${c.confidence ? ' (confidence ' + c.confidence + ')' : ''}`}</div>` : null}
    <div class="ac-prev"><${PreviewRows} a=${a} /></div>
    ${danger ? html`<ul class="ac-warnlist">
      <li>${`Before deleting, the system saves a backup (summary + specs + loops) in ${backupDir ? String(backupDir) : 'the app output folder, as loop-<project>-<date>'}.`}</li>
      <li>The branch and its commits are NOT deleted; only the worktree folder.</li>
      <li>No worktree comes selected: choose what to delete.</li>
    </ul>` : null}
    ${wt ? html`<${WorktreeTable} c=${c} onChange=${changed} />` : null}
    ${danger ? html`<div class="ac-dirty" hidden=${!ds.length}>${ds.length ? [
      html`<div class="ac-expl">These worktrees have uncommitted changes, which will be lost. To delete them anyway, type the branch name of each one:</div>`,
      ...ds.map((w) => html`<label key=${w.path} class="ac-type-l"><span class="mono">${w.branch}</span>
        <input type="text" class="ac-type" placeholder=${w.branch} aria-label=${'type ' + w.branch} autocomplete="off" spellcheck=${false}
          value=${c.typed[w.path] || ''} onInput=${(e) => { c.typed[w.path] = e.currentTarget.value; changed(); }} /></label>`),
    ] : null}</div>` : null}
    ${off ? html`<div class="ac-expl ac-off">${'Unavailable now: ' + txt(a.unavailable_reason)}</div>` : html`<div class="ac-expl">Runs now, only on your click.</div>`}
    <div class="ac-btns">
      <button type="button" class=${'btn btn-sm ' + (danger ? 'btn-danger' : 'btn-go')} id="ac-go" disabled=${!ok} onClick=${runAction}>${danger ? 'Delete selected worktrees' : a.id === 'push' ? 'Push selected branches' : 'Confirm and run'}</button>
      <button type="button" class="btn btn-sm" onClick=${cancelAction}>Cancel</button>
    </div>
  </div>`;
}
function ActionCard({ c }) {
  if (!c) return null;
  const close = html`<button type="button" class="btn btn-sm btn-ghost" onClick=${cancelAction}>Close</button>`;
  if (c.kind === 'proposing') return html`<div class="ac-card"><div class="ac-msg"><${Spinner} /><span>understanding the request…</span></div><div class="ac-quote">${c.text}</div></div>`;
  if (c.kind === 'running') return html`<div class=${'ac-card' + (c.action.id === 'clean_worktrees' ? ' ac-danger' : '')}><div class="ac-title">${c.action.title || c.action.id}</div>
    <div class="ac-msg"><${Spinner} /><span>${c.action.id === 'clean_worktrees' ? 'backing up and deleting… (may take a few minutes)' : 'running… (may take up to 3 min)'}</span></div></div>`;
  if (c.kind === 'none') return html`<div class="ac-card ac-warn"><div class="ac-title">This is not a ready-made action.</div>
    ${c.explanation ? html`<div class="ac-expl">${String(c.explanation)}</div>` : null}<div class="ac-quote">${c.text}</div>
    <div class="ac-btns"><button type="button" class="btn btn-sm btn-primary" onClick=${sendAsInstruction}>Send as an instruction to the loop</button>${close}</div></div>`;
  if (c.kind === 'result') {
    const ok = c.ok, r = c.res || {}, pr = httpUrl(r.pr);
    const backup = (list(r.results)[0] || {}).backup;
    const out = (k, t) => (t ? html`<div><div class="ac-k">${k}</div><${Pre} text=${t} /></div>` : null);
    return html`<div class=${'ac-card ' + (ok ? 'ac-ok' : 'ac-bad')}>
      <div class="ac-title">${`${ok ? 'Done' : 'Did not run'} · ${c.action.title || c.action.id}`}</div>
      ${ok ? null : html`<div class="ac-expl">${String(c.error || 'unknown error')}</div>`}
      <${ResultRows} r=${r} c=${c} />
      ${backup ? html`<div class="ac-bk"><div class="ac-k">backup saved in</div><div class="ac-bk-row"><code class="ac-bk-p">${String(backup)}</code>
        <button type="button" class="btn btn-sm" onClick=${() => copyText(String(backup), 'Backup path copied.')}>Copy path</button></div></div>` : null}
      ${pr ? html`<div class="ac-row"><span class="ac-k">PR</span><a href=${pr} target="_blank" rel="noopener noreferrer">${pr}</a></div>` : r.pr ? html`<${Row} k="PR" v=${r.pr} />` : null}
      <${Row} k="PR status" v=${r.pr_status} />
      ${out('push', r.push)}${out('push', c.push)}${out('output', r.output)}${out('output', c.output)}
      <div class="ac-btns">${close}</div>
    </div>`;
  }
  return html`<${ConfirmCard} c=${c} />`;
}

/** Content of #cp-act: quick actions, the current card and the recent actions. */
export function ActPanel({ onPropose }) {
  const acts = list(actState.actions), rec = list(ctx.state && ctx.state.recent_actions).slice(0, 5), available = acts.filter((a) => a.available !== false);
  return html`<details class="ac-wrap" open=${actState.card ? true : undefined}>
    <summary class="ac-sum">${`Actions${actState.actions ? ` (${available.length})` : ''}`}</summary>
    <div class="ac-quick">
      ${actState.loading && !actState.actions ? html`<div class="ac-msg"><${Spinner} /><span>checking what can be done…</span></div>` : null}
      ${actState.err ? html`<div class="ac-expl ac-off">${actState.err}</div>` : null}
      ${available.map((a) => html`<div key=${a.id} class="ac-act"><button type="button" class=${'btn btn-sm ac-btn' + (a.id === 'clean_worktrees' ? ' danger' : '')}
          title="opens the confirmation card" onClick=${() => openAction(a)}>${a.title || a.id}</button></div>`)}
      ${onPropose ? html`<button type="button" class="btn btn-sm btn-ghost ac-btn" title="the AI turns the text of the message box into an action, which you confirm" onClick=${onPropose}>Propose from message</button>` : null}
      ${acts.some((a) => a.available === false) ? html`<details class="ac-off-list"><summary>${acts.filter((a) => a.available === false).length} unavailable now</summary>
        ${acts.filter((a) => a.available === false).map((a) => html`<div key=${a.id} class="ac-why"><strong>${a.title || a.id}</strong>: ${txt(a.unavailable_reason)}</div>`)}</details>` : null}
      ${actState.actions && !actState.actions.length ? html`<div class="empty-note">No action available.</div>` : null}
    </div>
    <${ActionCard} key=${actState.seq} c=${actState.card} />
    ${rec.length ? html`<div class="ac-rec"><div class="ac-k">Recent actions</div>${rec.map((x, i) => html`<div key=${i} class="ac-rec-row">
      <span class=${'ac-rec-ok ' + (x.ok ? 'ok' : 'bad')}>${x.ok ? '✓' : '✗'}</span><span class="ac-rec-a">${txt(x.action)}</span>
      <span class="ac-rec-t">${fmtHour(x.t)}</span><span class="ac-rec-r">${txt(x.summary)}</span></div>`)}</div>` : null}
  </details>`;
}
