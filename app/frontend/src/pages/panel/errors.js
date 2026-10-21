/* "Errors" view: a global page with three sections (GET errors, never in the 2 s polling): Problems (what stops the loop, with what
   to do and the recovery buttons), Notes (info that is not guard) and Guard (blocks: the guard worked, nothing to fix). */
import { html } from '../../util/preact.js';
import { txt, list } from '../../util/format.js';
import { ctx, ui } from './context.js';
import { renderAll } from '../project_panel.js';
import { sendResume } from './alert.js';

const SEVERITY_COLOR = { error: '#F06A5E', warning: '#F2B544', info: '#7D838C' };
const RECOVER_LABEL = { retry: 'Try again', commit_leftovers: 'Commit leftovers and try again', discard_leftovers: 'Discard leftovers and try again' };
const KIND_LABEL = { loop: 'loop stopped', turn: 'turn', guard: 'guard', feature: 'feature', launch: 'launch' };

/** Real problems only (errors + warnings); guard blocks and notes never count. */
export const errorCount = () => { const d = ui.errors.data || {}, c = d.counts || {}; return d.problems != null ? d.problems : (c.error || 0) + (c.warning || 0); };

/** Reloads when the loop moved on (turn count, status, guard total) or when forced. */
export async function loadErrors(force) {
  const state = ctx.state, es = ui.errors;
  const key = [state && state.meta ? state.meta.turns : '', state && state.loop ? state.loop.status : '', Object.values((state && state.guard_counts) || {}).reduce((a, b) => a + b, 0)].join('|');
  if (es.loading || (!force && es.key === key)) return;
  es.loading = true; es.key = key; es.err = null; if (!es.data) renderAll();
  try { es.data = await ctx.api.get('errors', 30000); }
  catch (e) { es.err = e.status === 404 ? 'The server does not offer the error list yet.' : e.message; }
  finally { es.loading = false; renderAll(); }
}

export function openErrors() { ui.view = 'errors'; renderAll(); loadErrors(true); }

/** Recovery button: try again, or settle the leftover files first (discard asks twice: it deletes work). */
function recover(code) {
  if (code === 'discard_leftovers' && ui.errors.confirmDiscard !== true) { ui.errors.confirmDiscard = true; renderAll(); return; }
  ui.errors.confirmDiscard = false;
  const leftovers = code === 'commit_leftovers' ? 'commit' : code === 'discard_leftovers' ? 'discard' : undefined;
  sendResume(RECOVER_LABEL[code].toLowerCase(), leftovers ? { leftovers } : {});
}

function RecoverButtons({ it }) {
  const busy = !!ui.busy.answer, running = ctx.state && ctx.state.loop && ctx.state.loop.running;
  if (!it.recover.length || running) return null;
  return html`<div class="err-actions">${it.recover.map((code) => {
    const confirm = code === 'discard_leftovers' && ui.errors.confirmDiscard === true;
    return html`<button key=${code} type="button" class=${'btn btn-sm' + (code === 'retry' || code === 'commit_leftovers' ? ' btn-primary' : ' btn-danger')} disabled=${busy} onClick=${() => recover(code)}>${confirm ? 'Click again: this deletes those files' : RECOVER_LABEL[code]}</button>`;
  })}</div>`;
}

function ErrorItem({ it, neutral }) {
  const color = neutral ? '#7D838C' : SEVERITY_COLOR[it.severity] || '#7D838C', open = ui.errors.open[it.id] != null ? ui.errors.open[it.id] : it.kind === 'loop';
  return html`<div class="err-item" style=${{ borderLeftColor: color }}>
    <button type="button" class="err-head" aria-expanded=${String(open)} onClick=${() => { ui.errors.open[it.id] = !open; renderAll(); }}>
      <span class="hchev">${open ? '▾' : '▸'}</span>
      <span class="err-kind" style=${{ color }}>${KIND_LABEL[it.kind] || it.kind}</span>
      <span class="err-title">${txt(it.title)}</span>
      <span class="hmeta">${[it.turn != null ? 'turn ' + it.turn : '', it.when ? String(it.when).replace('T', ' ').slice(0, 16) : ''].filter(Boolean).join(' · ')}</span>
    </button>
    ${open ? html`<div class="err-body">
      ${it.hint ? html`<div class="err-hint"><b>What it is / how to fix: </b>${it.hint}</div>` : null}
      ${it.detail ? html`<pre class="err-detail">${it.detail}</pre>` : null}
      <${RecoverButtons} it=${it} />
    </div>` : null}
  </div>`;
}

/** Collapsible section; `open` is kept in ui.errors.open (collapsed by default when `startOpen` is false). */
function Section({ id, title, startOpen, tone, children }) {
  const stored = ui.errors.open['sec:' + id], open = stored != null ? stored : startOpen;
  return html`<div class=${'err-section err-section-' + tone}>
    <button type="button" class="err-section-head" aria-expanded=${String(open)} onClick=${() => { ui.errors.open['sec:' + id] = !open; renderAll(); }}>
      <span class="hchev">${open ? '▾' : '▸'}</span><span class="err-section-title">${title}</span>
    </button>
    ${open ? html`<div class="err-section-body">${children}</div>` : null}
  </div>`;
}

function GuardLine({ it }) {
  const [reason] = String(it.hint || '').split(' A block means');
  return html`<div class="err-guard-line"><span class="err-guard-count mono">${it.kind === 'guard' && it.count > 1 ? it.count + ' x' : '·'}</span>
    <span class="err-guard-title">${txt(it.title)}</span>${reason ? html`<span class="err-guard-reason">${reason}</span>` : null}</div>`;
}

/** Content of #canvas in the "Errors" view (plain mode). */
export function Errors() {
  const es = ui.errors, data = es.data, items = list(data && data.items);
  if (!data) return html`<div class="empty-canvas">${es.loading || es.key === undefined ? 'Loading the errors…' : `Errors unavailable: ${es.err || 'no response'}`}</div>`;
  const problems = items.filter((it) => it.severity !== 'info'), notes = items.filter((it) => it.severity === 'info' && it.kind !== 'guard');
  const guard = items.filter((it) => it.kind === 'guard'), blocks = guard.reduce((sum, it) => sum + (it.id === 'guard-history' ? 0 : it.count || 0), 0);
  const history = guard.filter((it) => it.id === 'guard-history'), rules = guard.filter((it) => it.id !== 'guard-history');
  return html`<div class="hist err-page">
    <div class="hnote">${'Everything the loop leaves behind, in one place. Only real problems (the loop stops and notifies you) are errors.'}
      <button type="button" class="btn btn-sm btn-ghost" onClick=${() => loadErrors(true)}>refresh</button></div>
    ${es.err ? html`<div class="hnote">${'Could not refresh: ' + es.err}</div>` : null}
    <${Section} id="problems" title=${`Problems (${problems.length})`} startOpen=${true} tone=${problems.length ? 'problem' : 'ok'}>
      ${problems.length ? problems.map((it) => html`<${ErrorItem} key=${it.id} it=${it} />`)
        : html`<div class="err-ok">No problems. The loop only stops and notifies you for a real problem.</div>`}
    <//>
    ${notes.length ? html`<${Section} id="notes" title=${`Notes (${notes.length})`} startOpen=${false} tone="note">
      ${notes.map((it) => html`<${ErrorItem} key=${it.id} it=${it} neutral=${true} />`)}
    <//>` : null}
    ${guard.length ? html`<${Section} id="guard" title=${`Guard (${blocks} block${blocks === 1 ? '' : 's'}) — it worked, nothing to fix`} startOpen=${false} tone="guard">
      <div class="err-guard-note">A block means the guard stopped an agent from breaking a rule. It is not an error.</div>
      ${rules.map((it) => html`<${GuardLine} key=${it.id} it=${it} />`)}
      ${history.map((it) => html`<div key=${it.id} class="err-guard-line"><span class="err-guard-count mono">·</span><span class="err-guard-title">${txt(it.title)}</span></div>`)}
    <//>` : null}
  </div>`;
}
