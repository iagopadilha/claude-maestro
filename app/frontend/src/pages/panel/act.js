/* Act mode: concrete actions on the current version (back-end whitelist), always with an explicit confirmation. */
import { h } from '../../util/dom.js';
import { num, txt, list, httpUrl, shortPath, fmtHour } from '../../util/format.js';
import { toast } from '../../components/toast.js';
import { copyText } from '../../components/copy.js';
import { ctx } from './context.js';
import { schedulePoll } from '../project_panel.js';
import { renderComposer } from './chat.js';

const ACTION_TIMEOUT_MS = 15 * 60 * 1000; // cleaning worktrees backs up first: it can take a while
export const actState = { actions: null, loading: false, err: null, card: null, seq: 0 };
export const actSignature = () => [actState.actions, actState.loading, actState.err, actState.card && { ...actState.card, sel: null, typed: null }, ctx.state && ctx.state.recent_actions];
const FALLBACK_TITLES = { deliver: 'Deliver: push the branch + PR to the integration branch', summary: 'Generate the loop summary', push: 'Push the feature branches (no PR)', clean_worktrees: 'Clean loop worktrees (backup first)' };
const WORKTREE_ACTIONS = ['push', 'clean_worktrees'];
const KNOWN_ACTIONS = ['deliver', 'summary', 'push', 'clean_worktrees'];
const INSTRUCTION_MESSAGES = { note: 'Noted for the next turn.', answer: 'Answered, resuming the loop.', 'note+resume': 'Noted and loop resumed.' };

export async function loadActions() {
  if (actState.loading) return;
  actState.loading = true; actState.err = null; renderComposer();
  try { const r = await ctx.api.get('actions', 70000); actState.actions = list(r && r.actions); }
  catch (e) { actState.err = e.status === 404 ? 'The server does not offer the action list yet.' : e.message; }
  finally { actState.loading = false; renderComposer(); }
}
const findAction = (id) => list(actState.actions).find((a) => a.id === id) || { id, title: FALLBACK_TITLES[id] || id, available: true, preview: {} };
function openAction(action, extra) {
  actState.seq += 1;
  const wts = list(action.preview && action.preview.worktrees), want = list(extra && extra.targets);
  let sel = [];
  if (WORKTREE_ACTIONS.includes(action.id)) sel = want.length ? wts.filter((w) => want.some((x) => x === w.path || shortPath(x) === shortPath(w.path) || x === w.branch)).map((w) => w.path) : action.id === 'push' ? wts.map((w) => w.path) : [];
  actState.card = { kind: 'confirm', action, sel, typed: {}, ...(extra || {}) }; renderComposer();
}
function cancelAction() { actState.seq += 1; actState.card = null; renderComposer(); }
export async function proposeAction(text) {
  const seq = ++actState.seq;
  actState.card = { kind: 'proposing', text }; renderComposer();
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
    else { actState.card = { kind: 'none', text, explanation: p.explanation }; renderComposer(); }
  } catch (e) { if (seq === actState.seq) { actState.card = { kind: 'result', action: { id: 'propose', title: 'Understand the request' }, ok: false, error: e.message }; renderComposer(); } }
}
async function sendAsInstruction() {
  const c = actState.card; if (!c || c.kind !== 'none') return;
  try {
    const r = await ctx.api.post('instruction', { text: c.text, feature: null, attachments: [] });
    const msg = INSTRUCTION_MESSAGES[r.action] || 'Instruction sent.';
    toast(msg, 'ok'); actState.card = null; schedulePoll(800);
  } catch (e) { toast('Instruction not sent: ' + e.message, 'err'); }
  renderComposer();
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
  actState.card = { kind: 'running', action }; renderComposer();
  try {
    const r = await ctx.api.post('action/run', body, ACTION_TIMEOUT_MS);
    if (seq === actState.seq) actState.card = { kind: 'result', action, ok: true, res: r };
    toast(`${action.title || action.id}: done.`, 'ok');
  } catch (e) {
    if (seq === actState.seq) actState.card = { kind: 'result', action, ok: false, error: e.message, output: e.body && e.body.output ? String(e.body.output) : '', push: e.body && e.body.push ? String(e.body.push) : '', rows: list(e.body && e.body.results) };
  }
  schedulePoll(400);
  renderComposer(); loadActions();
}
function pre(text) { return text ? h('pre', { class: 'ac-pre', text: String(text) }) : null; }
function row(k, v) { return v == null || v === '' ? null : h('div', { class: 'ac-row' }, h('span', { class: 'ac-k', text: k }), h('span', { class: 'ac-v', text: String(v) })); }
function previewRows(a) {
  const p = a.preview || {}, kids = [];
  if (a.id === 'deliver') {
    kids.push(row('branch', p.branch), row('base', p.base || 'integration branch'));
    const pr = httpUrl(p.existing_pr);
    kids.push(h('div', { class: 'ac-row' }, h('span', { class: 'ac-k', text: 'PR' }), pr ? h('a', { href: pr, target: '_blank', rel: 'noopener noreferrer', text: pr.replace(/^https?:\/\/(www\.)?github\.com\//i, '') }) : h('span', { class: 'ac-v', text: p.existing_pr ? String(p.existing_pr) : 'none yet (will be created)' })));
    const of = list(p.open_features);
    kids.push(row('open features', of.length ? of.join(', ') : 'none'));
    if (p.push) kids.push(h('div', { class: 'ac-k', text: 'what delivery.py will do (dry-run)' }), pre(p.push));
  } else if (a.id === 'summary') kids.push(row('destination', p.destination));
  else if (!WORKTREE_ACTIONS.includes(a.id)) for (const [k, v] of Object.entries(p)) kids.push(row(k, typeof v === 'object' ? JSON.stringify(v) : v));
  return kids;
}
// worktree table with checkboxes; updates the button and the confirmation block without rebuilding the card
function worktreeTable(c, onChange) {
  const wts = list(c.action.preview && c.action.preview.worktrees);
  if (!wts.length) return h('div', { class: 'ac-expl ac-off', text: 'No loop worktree found.' });
  const rows = wts.map((w) => {
    const cb = h('input', { type: 'checkbox', 'aria-label': 'select ' + shortPath(w.path) });
    cb.checked = c.sel.includes(w.path);
    cb.addEventListener('change', () => { c.sel = cb.checked ? [...c.sel.filter((x) => x !== w.path), w.path] : c.sel.filter((x) => x !== w.path); onChange(); });
    return h('tr', {}, h('td', {}, cb), h('td', { class: 'ac-td-p', title: w.path, text: shortPath(w.path).split('/').pop() }), h('td', { class: 'ac-td-b', text: txt(w.branch) }),
      h('td', {}, w.dirty ? h('span', { class: 'ac-tag bad', text: 'dirty' }) : h('span', { class: 'ac-tag', text: 'clean' })),
      h('td', { class: 'ac-td-n', text: num(w.unpushed_commits) }), h('td', {}, w.has_specs ? h('span', { class: 'ac-tag', text: 'yes' }) : h('span', { class: 'ac-tag', text: 'no' })));
  });
  return h('div', { class: 'ac-tw' }, h('table', { class: 'ac-table' },
    h('thead', {}, h('tr', {}, h('th', {}), h('th', { text: 'worktree' }), h('th', { text: 'branch' }), h('th', { text: 'changes' }), h('th', { text: 'unpushed' }), h('th', { text: 'specs' }))), h('tbody', {}, rows)));
}
function resultRows(r, c) {
  const rows = list((r && r.results) || (c && c.rows));
  if (!rows.length) return null;
  return h('div', { class: 'ac-res' }, rows.map((x) => {
    const bad = !!x.error || x.ok === false || x.deleted === false, okv = !bad;
    return h('div', { class: 'ac-res-row ' + (okv ? 'ok' : 'bad') },
      h('span', { class: 'ac-res-i', text: okv ? '✓' : '✗' }), h('span', { class: 'ac-res-p', title: x.path, text: shortPath(x.path) || txt(x.branch) }),
      h('span', { class: 'ac-res-s', text: x.error ? 'error: ' + x.error : x.deleted ? 'deleted' : x.ok ? 'ok' : x.deleted === false ? 'not deleted' : 'ok' }),
      x.output ? h('span', { class: 'ac-res-o', text: String(x.output) }) : null);
  }));
}
function actionCard() {
  const c = actState.card;
  if (!c) return null;
  const close = h('button', { type: 'button', class: 'btn btn-sm btn-ghost', onclick: cancelAction }, 'Close');
  if (c.kind === 'proposing') return h('div', { class: 'ac-card' }, h('div', { class: 'ac-msg' }, h('div', { class: 'spinner', 'aria-hidden': 'true' }), h('span', { text: 'understanding the request…' })), h('div', { class: 'ac-quote', text: c.text }));
  if (c.kind === 'running') return h('div', { class: 'ac-card' + (c.action.id === 'clean_worktrees' ? ' ac-danger' : '') }, h('div', { class: 'ac-title', text: c.action.title || c.action.id }),
    h('div', { class: 'ac-msg' }, h('div', { class: 'spinner', 'aria-hidden': 'true' }), h('span', { text: c.action.id === 'clean_worktrees' ? 'backing up and deleting… (may take a few minutes)' : 'running… (may take up to 3 min)' })));
  if (c.kind === 'none') return h('div', { class: 'ac-card ac-warn' }, h('div', { class: 'ac-title', text: 'This is not a ready-made action.' }),
    c.explanation ? h('div', { class: 'ac-expl', text: String(c.explanation) }) : null, h('div', { class: 'ac-quote', text: c.text }),
    h('div', { class: 'ac-btns' }, h('button', { type: 'button', class: 'btn btn-sm btn-primary', onclick: sendAsInstruction }, 'Send as an instruction to the loop'), close));
  if (c.kind === 'result') {
    const ok = c.ok, r = c.res || {}, pr = httpUrl(r.pr);
    const backup = (list(r.results)[0] || {}).backup;
    return h('div', { class: 'ac-card ' + (ok ? 'ac-ok' : 'ac-bad') },
      h('div', { class: 'ac-title', text: `${ok ? 'Done' : 'Did not run'} · ${c.action.title || c.action.id}` }),
      ok ? null : h('div', { class: 'ac-expl', text: String(c.error || 'unknown error') }),
      resultRows(r, c),
      backup ? h('div', { class: 'ac-bk' }, h('div', { class: 'ac-k', text: 'backup saved in' }), h('div', { class: 'ac-bk-row' }, h('code', { class: 'ac-bk-p', text: String(backup) }),
        h('button', { type: 'button', class: 'btn btn-sm', onclick: () => copyText(String(backup), 'Backup path copied.') }, 'Copy path'))) : null,
      pr ? h('div', { class: 'ac-row' }, h('span', { class: 'ac-k', text: 'PR' }), h('a', { href: pr, target: '_blank', rel: 'noopener noreferrer', text: pr })) : (r.pr ? row('PR', r.pr) : null),
      row('PR status', r.pr_status),
      r.push ? h('div', {}, h('div', { class: 'ac-k', text: 'push' }), pre(r.push)) : null, c.push ? h('div', {}, h('div', { class: 'ac-k', text: 'push' }), pre(c.push)) : null,
      r.output ? h('div', {}, h('div', { class: 'ac-k', text: 'output' }), pre(r.output)) : null, c.output ? h('div', {}, h('div', { class: 'ac-k', text: 'output' }), pre(c.output)) : null,
      h('div', { class: 'ac-btns' }, close));
  }
  const a = c.action, off = a.available === false, wt = WORKTREE_ACTIONS.includes(a.id), danger = a.id === 'clean_worktrees';
  const go = h('button', { type: 'button', class: 'btn btn-sm ' + (danger ? 'btn-danger' : 'btn-go'), id: 'ac-go', onclick: runAction }, danger ? 'Delete selected worktrees' : a.id === 'push' ? 'Push selected branches' : 'Confirm and run');
  const dirtyBox = h('div', { class: 'ac-dirty' });
  const refresh = () => {
    let ok = !off;
    if (wt && !c.sel.length) ok = false;
    if (danger) {
      const ds = dirtySel(c);
      dirtyBox.hidden = !ds.length;
      if (ds.length && dirtyBox.dataset.sig !== ds.map((w) => w.path).join('|')) {
        dirtyBox.dataset.sig = ds.map((w) => w.path).join('|');
        dirtyBox.replaceChildren(h('div', { class: 'ac-expl', text: 'These worktrees have uncommitted changes, which will be lost. To delete them anyway, type the branch name of each one:' }),
          ...ds.map((w) => {
            const inp = h('input', { type: 'text', class: 'ac-type', placeholder: w.branch, 'aria-label': 'type ' + w.branch, autocomplete: 'off', spellcheck: 'false' });
            inp.value = c.typed[w.path] || ''; inp.addEventListener('input', () => { c.typed[w.path] = inp.value; refresh(); });
            return h('label', { class: 'ac-type-l' }, h('span', { class: 'mono', text: w.branch }), inp);
          }));
      }
      if (!typedOk(c)) ok = false;
    }
    go.disabled = !ok;
  };
  const backupDir = a.preview && a.preview.backup_dir;
  const kids = [
    h('div', { class: 'ac-kick', text: danger ? 'DESTRUCTIVE ACTION · CONFIRM CAREFULLY' : 'CONFIRM BEFORE RUNNING' }),
    h('div', { class: 'ac-title', text: a.title || a.id }),
    c.explanation ? h('div', { class: 'ac-expl', text: `The AI understood: ${c.explanation}${c.confidence ? ' (confidence ' + c.confidence + ')' : ''}` }) : null,
    h('div', { class: 'ac-prev' }, previewRows(a)),
  ];
  if (danger) kids.push(h('ul', { class: 'ac-warnlist' },
    h('li', { text: `Before deleting, the system saves a backup (summary + specs + loops) in ${backupDir ? String(backupDir) : 'the app output folder, as loop-<project>-<date>'}.` }),
    h('li', { text: 'The branch and its commits are NOT deleted; only the worktree folder.' }),
    h('li', { text: 'No worktree comes selected: choose what to delete.' })));
  if (wt) kids.push(worktreeTable(c, refresh));
  if (danger) kids.push(dirtyBox);
  kids.push(off ? h('div', { class: 'ac-expl ac-off', text: 'Unavailable now: ' + txt(a.unavailable_reason) }) : h('div', { class: 'ac-expl', text: 'This runs now. Nothing happens before your click.' }),
    h('div', { class: 'ac-btns' }, go, h('button', { type: 'button', class: 'btn btn-sm', onclick: cancelAction }, 'Cancel')));
  refresh();
  return h('div', { class: 'ac-card ' + (danger ? 'ac-danger' : 'ac-confirm') }, kids);
}
export function actPanel() {
  const kids = [h('div', { class: 'ac-head', text: 'Immediate action: runs now, with confirmation' })];
  const q = [];
  if (actState.loading && !actState.actions) q.push(h('div', { class: 'ac-msg' }, h('div', { class: 'spinner', 'aria-hidden': 'true' }), h('span', { text: 'checking what can be done…' })));
  if (actState.err) q.push(h('div', { class: 'ac-expl ac-off', text: actState.err }));
  for (const a of list(actState.actions)) {
    const off = a.available === false;
    q.push(h('div', { class: 'ac-act' }, h('button', { type: 'button', class: 'btn btn-sm ac-btn' + (a.id === 'clean_worktrees' ? ' danger' : ''), disabled: off || null, title: off ? txt(a.unavailable_reason) : 'opens the confirmation card', onclick: () => openAction(a) }, a.title || a.id),
      off ? h('div', { class: 'ac-why', text: 'unavailable: ' + txt(a.unavailable_reason) }) : null));
  }
  if (actState.actions && !actState.actions.length) q.push(h('div', { class: 'empty-note', text: 'No action available.' }));
  kids.push(h('div', { class: 'ac-quick' }, q));
  const card = actionCard(); if (card) kids.push(card);
  const rec = list(ctx.state && ctx.state.recent_actions).slice(0, 5);
  if (rec.length) kids.push(h('div', { class: 'ac-rec' }, h('div', { class: 'ac-k', text: 'Recent actions' }),
    rec.map((x) => h('div', { class: 'ac-rec-row' }, h('span', { class: 'ac-rec-ok ' + (x.ok ? 'ok' : 'bad'), text: x.ok ? '✓' : '✗' }), h('span', { class: 'ac-rec-a', text: txt(x.action) }), h('span', { class: 'ac-rec-t', text: fmtHour(x.t) }), h('span', { class: 'ac-rec-r', text: txt(x.summary) })))));
  return kids;
}
