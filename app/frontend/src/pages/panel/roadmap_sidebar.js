/* Left column: roadmap by stage, quarantine, VALIDATE.md and ANSWERS.md. Also the selection. */
import { $, h, fill, setColors } from '../../util/dom.js';
import { isNum, num, txt, list } from '../../util/format.js';
import { toast } from '../../components/toast.js';
import { ctx, ui, sigs, region, pal, status, live, features, focusFeatureId } from './context.js';
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
  ui.busy[k] = true; renderLeft(true);
  try {
    if (s === 'awaiting_user') await ctx.api.post('answer', { text });
    else await ctx.api.post('note', { text });
    ui.qSent[q.id] = opt; toast('Sent: ' + text, 'ok');
  } catch (e) { toast('Not sent: ' + e.message, 'err'); }
  finally { ui.busy[k] = false; renderLeft(true); }
}

export function renderLeft(force) {
  const state = ctx.state;
  if (!state) return;
  const fs = features(), groups = groupStages(), c = state.counts || {}, s = status();
  const q = live() ? list(state.quarantine) : [];
  const canQ = s === 'running' || s === 'awaiting_user';
  const data = { fs: fs.map((f) => [f.id, f.name, f.status, f.stage]), g: groups.map((g) => g.n), sel: ui.sel, v: c.verified, t: c.total, q, qs: ui.qSent, canQ, bq: Object.keys(ui.busy).filter((k) => ui.busy[k] && k.includes('|')), val: state.validate, ans: state.answers, past: ui.pastN };
  if (force) sigs.left = null;
  region('left', data, () => {
    const focusF = focusFeatureId();
    const verified = ui.past ? fs.filter((f) => f.status === 'verified').length : c.verified;
    const total = ui.past ? fs.length : c.total;
    const road = h('div', {},
      h('div', { class: 'sec-head' }, h('h2', { class: 'sec-title', text: 'Roadmap' }), h('div', { class: 'dim-mono', text: `${num(verified)}/${num(total)} verified` })),
      groups.length ? h('div', { class: 'stages' }, groups.map((g) => h('div', {},
        g.n == null ? h('div', { class: 'small-label', text: g.title }) : h('button', { type: 'button', class: 'stage-h' + (ui.sel && ui.sel.kind === 'stage' && ui.sel.n === g.n ? ' sel' : ''), 'aria-expanded': String(!!(ui.sel && ui.sel.kind === 'stage' && ui.sel.n === g.n)), title: 'opens the summary of this stage in the inspector', onclick: () => selectStage(g.n) }, h('span', { text: g.title }), h('span', { class: 'stage-chev', 'aria-hidden': 'true', text: '›' })),
        h('div', { class: 'frows' }, g.items.map((f) => {
          const [col, bg] = pal(f.status);
          const chip = h('span', { class: 'chip', text: txt(f.status) }); setColors(chip, col, bg);
          return h('button', { type: 'button', class: 'frow' + (focusF === f.id ? ' sel' : ''), title: f.id, 'aria-pressed': focusF === f.id ? 'true' : 'false', onclick: () => selectFeature(f.id) },
            h('span', { class: 'nm', text: txt(f.name || f.id) }), chip);
        }))))) : h('div', { class: 'empty-note', text: 'No feature in the roadmap. Use the trail at the top to generate the roadmap.' }));
    const kids = [road];
    for (const qq of q) {
      const sent = ui.qSent[qq.id];
      kids.push(h('div', { class: 'qcard' },
        h('div', { class: 'qtitle' }, h('span', { class: 'dot dot-red' }), h('span', { text: `${qq.id} in quarantine` })),
        h('div', { class: 'qtext', text: txt(qq.reason) }),
        list(qq.stuck).length ? h('div', { class: 'qtext' }, 'Holds back: ', h('span', { class: 'mono', text: list(qq.stuck).join(', ') })) : h('div', { class: 'qtext', text: 'Nothing depends on it, the loop carries on.' }),
        h('div', { class: 'qopts' }, list(qq.options).map((o, i) =>
          h('button', { type: 'button', class: 'qopt', disabled: !canQ || !!ui.busy[qq.id + '|' + o], title: canQ ? '' : 'only with the loop running or waiting for an answer', onclick: () => sendQuarantine(qq, o) },
            h('span', { class: 'n', text: String(i + 1) }), h('span', { text: String(o) })))),
        sent ? h('div', { class: 'qsent', text: `→ sent: ${sent} · the next turn applies it` }) : null));
    }
    const vals = list(state.validate);
    kids.push(h('div', {},
      h('div', { class: 'sec-title', style: 'margin-bottom:8px' }, 'For you to validate ', h('span', { class: 'dim-mono', text: 'VALIDATE.md' })),
      vals.length ? h('div', { class: 'vlist' }, vals.map((v) => {
        const p = parseValidate(v);
        return h('div', { class: 'vitem' },
          h('div', {}, p.slug ? h('span', { class: 'dim-mono', text: p.slug + ' ' }) : null, p.text),
          p.chose ? h('div', { class: 'vrev', text: 'chose: ' + p.chose }) : null,
          p.revert ? h('div', { class: 'vrev', text: 'revert: ' + p.revert }) : null);
      }))
        : h('div', { class: 'empty-note', text: 'Nothing to validate for now.' })));
    const answers = list(state.answers).slice(-5).reverse();
    if (answers.length) {
      kids.push(h('div', {}, h('div', { class: 'sec-title', style: 'margin-bottom:8px' }, 'Answers ', h('span', { class: 'dim-mono', text: 'ANSWERS.md' })),
        h('div', { class: 'vlist' }, answers.map((r) => h('div', { class: 'vitem' },
          h('div', { class: 'dim-mono', text: `${txt(r.date)} · ${txt(r.state)}` }),
          h('div', { text: txt(r.question) }), h('div', { class: 'vrev', text: '→ ' + txt(r.answer) }))))));
    }
    fill($('left'), kids);
  });
}
