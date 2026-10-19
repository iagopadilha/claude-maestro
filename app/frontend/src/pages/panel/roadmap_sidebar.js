/* Left column: roadmap by stage, quarantine, VALIDATE.md and ANSWERS.md. Also the selection. */
import { html } from '../../util/preact.js';
import { isNum, num, txt, list } from '../../util/format.js';
import { toast } from '../../components/toast.js';
import { ctx, ui, pal, status, live, features, focusFeatureId } from './context.js';
import { renderAll } from '../project_panel.js';
import { fetchAgentLog } from './inspector.js';

/** One VALIDATE.md line: `- [<slug|general>] <doubt> | chose: <what was done> | revert: <how>`. */
export function parseValidate(raw) {
  const t = String(raw).replace(/^\s*-\s*/, '');
  const m = t.match(/^\[([^\]]+)\]\s*(.*)$/);
  const slug = m ? m[1] : null, rest = m ? m[2] : t;
  const parts = rest.split(' | ').map((x) => x.trim());
  const out = { slug, text: parts[0] || '', chose: null, revert: null };
  for (const p of parts.slice(1)) {
    if (/^chose:/i.test(p)) out.chose = p.replace(/^chose:\s*/i, '');
    else if (/^revert:/i.test(p)) out.revert = p.replace(/^revert:\s*/i, '');
    else out.text += ' · ' + p;
  }
  return out;
}

export function groupStages() {
  const fs = features(), titles = {};
  for (const e of list(ctx.state && ctx.state.stages)) titles[e.n] = e.title;
  const groups = new Map();
  for (const f of fs) { const n = isNum(f.stage) ? f.stage : null; if (!groups.has(n)) groups.set(n, []); groups.get(n).push(f); }
  return [...groups.entries()].sort((a, b) => (a[0] == null) - (b[0] == null) || a[0] - b[0])
    .map(([n, items]) => ({ n, title: n == null ? 'No stage' : titles[n] || `Stage ${String(n).padStart(2, '0')}`, items }));
}

export function selectStage(n) { ui.sel = { kind: 'stage', n }; ui.tab = 'info'; ui.chatGeneral = false; renderAll(); }
export function selectFeature(id) { ui.sel = { kind: 'feature', id }; ui.tab = 'info'; ui.chatGeneral = false; renderAll(); }
export function selectAgent(id) { ui.sel = { kind: 'agent', id }; ui.tab = 'chat'; ui.chatGeneral = false; ui.agentLog = null; ui.logLoading = id; renderAll(); fetchAgentLog(id); }

async function sendQuarantine(q, opt) {
  const s = status(), text = `Quarantine ${q.id}: ${opt}`;
  const k = q.id + '|' + opt;
  if (ui.busy[k]) return;
  ui.busy[k] = true; renderAll();
  try {
    if (s === 'awaiting_user') await ctx.api.post('answer', { text });
    else await ctx.api.post('note', { text });
    ui.qSent[q.id] = opt; toast('Sent: ' + text, 'ok');
  } catch (e) { toast('Not sent: ' + e.message, 'err'); }
  finally { ui.busy[k] = false; renderAll(); }
}

function Roadmap({ fs, groups, c }) {
  const focusF = focusFeatureId();
  const verified = ui.past ? fs.filter((f) => f.status === 'verified').length : c.verified;
  const total = ui.past ? fs.length : c.total;
  const stageSel = (g) => !!(ui.sel && ui.sel.kind === 'stage' && ui.sel.n === g.n);
  return html`<div>
    <div class="sec-head"><h2 class="sec-title">Roadmap</h2><div class="dim-mono">${`${num(verified)}/${num(total)} verified`}</div></div>
    ${groups.length ? html`<div class="stages">${groups.map((g) => html`<div key=${String(g.n)}>
      ${g.n == null ? html`<div class="small-label">${g.title}</div>`
        : html`<button type="button" class=${'stage-h' + (stageSel(g) ? ' sel' : '')} aria-expanded=${String(stageSel(g))} title="opens the summary of this stage in the inspector"
            onClick=${() => selectStage(g.n)}><span>${g.title}</span><span class="stage-chev" aria-hidden="true">›</span></button>`}
      <div class="frows">${g.items.map((f) => {
        const [col, bg] = pal(f.status);
        return html`<button key=${f.id} type="button" class=${'frow' + (focusF === f.id ? ' sel' : '')} title=${f.id} aria-pressed=${focusF === f.id ? 'true' : 'false'} onClick=${() => selectFeature(f.id)}>
          <span class="nm">${txt(f.name || f.id)}</span><span class="chip" style=${{ color: col || null, background: bg || null }}>${txt(f.status)}</span></button>`;
      })}</div>
    </div>`)}</div>`
      : html`<div class="empty-note">No feature in the roadmap. Use the trail at the top to generate the roadmap.</div>`}
  </div>`;
}

function QuarantineCard({ qq, canQ }) {
  const sent = ui.qSent[qq.id];
  return html`<div class="qcard">
    <div class="qtitle"><span class="dot dot-red"></span><span>${`${qq.id} in quarantine`}</span></div>
    <div class="qtext">${txt(qq.reason)}</div>
    ${list(qq.stuck).length ? html`<div class="qtext">Holds back: <span class="mono">${list(qq.stuck).join(', ')}</span></div>` : html`<div class="qtext">Nothing depends on it, the loop carries on.</div>`}
    <div class="qopts">${list(qq.options).map((o, i) => html`<button key=${i} type="button" class="qopt" disabled=${!canQ || !!ui.busy[qq.id + '|' + o]}
      title=${canQ ? '' : 'only with the loop running or waiting for an answer'} onClick=${() => sendQuarantine(qq, o)}><span class="n">${String(i + 1)}</span><span>${String(o)}</span></button>`)}</div>
    ${sent ? html`<div class="qsent">${`→ sent: ${sent} · the next turn applies it`}</div>` : null}
  </div>`;
}

function Validate({ vals }) {
  return html`<div>
    <div class="sec-title" style="margin-bottom:8px">For you to validate <span class="dim-mono">VALIDATE.md</span></div>
    ${vals.length ? html`<div class="vlist">${vals.map((v) => {
      const p = parseValidate(v);
      return html`<div class="vitem">
        <div>${p.slug ? html`<span class="dim-mono">${p.slug + ' '}</span>` : null}${p.text}</div>
        ${p.chose ? html`<div class="vrev">${'chose: ' + p.chose}</div>` : null}
        ${p.revert ? html`<div class="vrev">${'revert: ' + p.revert}</div>` : null}
      </div>`;
    })}</div>` : html`<div class="empty-note">Nothing to validate for now.</div>`}
  </div>`;
}

function Answers({ answers }) {
  return html`<div><div class="sec-title" style="margin-bottom:8px">Answers <span class="dim-mono">ANSWERS.md</span></div>
    <div class="vlist">${answers.map((r) => html`<div class="vitem">
      <div class="dim-mono">${`${txt(r.date)} · ${txt(r.state)}`}</div>
      <div>${txt(r.question)}</div><div class="vrev">${'→ ' + txt(r.answer)}</div>
    </div>`)}</div></div>`;
}

/** Content of #left. */
export function RoadmapSidebar() {
  const state = ctx.state;
  if (!state) return null;
  const fs = features(), groups = groupStages(), s = status();
  const q = live() ? list(state.quarantine) : [];
  const canQ = s === 'running' || s === 'awaiting_user';
  const answers = list(state.answers).slice(-5).reverse();
  return html`
    <${Roadmap} fs=${fs} groups=${groups} c=${state.counts || {}} />
    ${q.map((qq) => html`<${QuarantineCard} key=${'q:' + qq.id} qq=${qq} canQ=${canQ} />`)}
    <${Validate} vals=${list(state.validate)} />
    ${answers.length ? html`<${Answers} answers=${answers} />` : null}`;
}
