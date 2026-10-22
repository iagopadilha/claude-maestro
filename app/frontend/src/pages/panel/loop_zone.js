/* LOOP zone (amber, main area): everything here depends on the selected turn. From top to bottom: the wrap-up (loop done or stopped
   with a worktree), the Claude account chip (always visible), the pause banner, the turn
   strip, the header of the selected turn, the tabs (Pipeline | Decision & events | Files | Errors) and a row with the tab
   content plus the inspector. The zone scrolls vertically, so the pipeline gets height when the user scrolls down. */
import { html } from '../../util/preact.js';
import { isNum, cost, dur, firstLine } from '../../util/format.js';
import { inlineNodes } from '../../components/markdown.js';
import { renderAll } from '../project_panel.js';
import { pipe, turnsList, curN, emptyReason } from './pipeline_model.js';
import { errorCountAt } from './problems.js';
import { PipelineView } from './pipeline_view.js';
import { TurnLog, TurnFiles, TurnErrors } from './turn_tabs.js';
import { openDepsDialog } from './dialogs/index.js';
import { PauseBanner } from './pause_banner.js';
import { TurnStrip, stepsInTurn } from './turn_strip.js';
import { Inspector } from './inspector.js';
import { TabBar } from './global_zone.js';
import { WrapUp, wrapUpVisible } from './wrap_up.js';
import { ClaudeAccountChip, MethodTag, methodOf } from './claude_account_chip.js';

const TURN_STATUS_LABEL = { done: 'DONE', error: 'ERROR', running: 'RUNNING', awaiting_user: 'WAITING FOR YOU', continue: 'CONTINUING' };
const FALLBACK_DOT = 'var(--line4)';
const DECISION_CLIP = 260;

/** `claude`, `cc`...: the command of the turn without its folder and extension ('—' for turns logged before it was recorded). */
function commandName(path) {
  const name = String(path || '').split(/[\\/]/).pop().replace(/\.(cmd|bat|exe|py)$/i, '');
  return name || '—';
}

function pickTab(tab) { pipe.tab = tab; renderAll(); }
function pickView(view) { pipe.view = view; renderAll(); }

/** "LOOP > Turn N", the title with status badge, the decision of the orchestrator and the three figures of the turn. */
function TurnHeader({ turn }) {
  if (!turn) {
    return html`<div class="z-turn-head"><div class="z-turn-head-main"><div class="z-crumb mono">LOOP</div>
      <div class="z-turn-title">${pipe.loading ? 'Reading the turns…' : 'No turn yet'}</div>
      <div class="z-turn-decision">The turns appear here when the loop runs its first turn.</div></div></div>`;
  }
  const color = turn.dot || FALLBACK_DOT, steps = stepsInTurn(turn);
  const decision = turn.decision ? firstLine(turn.decision) : '';
  return html`<div class="z-turn-head">
    <div class="z-turn-head-main">
      <div class="z-crumb mono">LOOP › <span class="z-crumb-turn">${'Turn ' + turn.n}</span></div>
      <div class="z-turn-title"><span>${`Turn ${turn.n}${turn.label ? ' · ' + turn.label : ''}`}</span>
        <span class="z-status mono" style=${{ color, borderColor: color }}>${TURN_STATUS_LABEL[turn.status] || String(turn.status || '').toUpperCase()}</span></div>
      ${decision ? html`<div class="z-turn-decision" title=${String(turn.decision)}>${inlineNodes(decision.length > DECISION_CLIP ? decision.slice(0, DECISION_CLIP - 1) + '…' : decision)}</div>` : null}
    </div>
    <div class="z-figures mono">
      <div><div class="z-figure-k">DURATION</div><div>${isNum(turn.duration_s) ? dur(turn.duration_s) : '—'}</div></div>
      <div><div class="z-figure-k">COST</div><div>${isNum(turn.cost_usd) ? cost(turn.cost_usd) : '—'}</div></div>
      <div><div class="z-figure-k">STEPS IN TURN</div><div>${steps == null ? '—' : steps}</div></div>
      <div title=${turn.claude_account || ''}><div class="z-figure-k">CLAUDE</div><div>${commandName(turn.claude)}<${MethodTag} method=${methodOf(turn.claude_account)} /></div>
        ${turn.claude_account ? html`<div class="z-figure-k">${turn.claude_account}</div>` : null}</div>
    </div>
  </div>`;
}

/** Content of the selected loop tab (the pipeline agent owns each view). */
function TabContent() {
  const unavailable = emptyReason();
  if (unavailable && !pipe.data) return html`<div class="z-note" role="alert">${unavailable}</div>`;
  if (pipe.tab === 'log') return html`<${TurnLog} />`;
  if (pipe.tab === 'files') return html`<${TurnFiles} />`;
  if (pipe.tab === 'errors') return html`<${TurnErrors} />`;
  return html`<${PipelineView} />`;
}

/** The whole <main>. `on` = the loop panel shows; before that (and in planning mode) only the composer frame stays in the DOM. */
export function LoopZone({ on, planning }) {
  const turn = on ? turnsList().find((item) => item.n === curN()) || null : null;
  const failed = on && turn ? errorCountAt(turn.n) : 0; // real errors of the turn; guard blocks are never counted as problems
  const tab = ['pipeline', 'log', 'files', 'errors'].includes(pipe.tab) ? pipe.tab : 'pipeline';
  const tabs = [['pipeline', 'Pipeline'], ['log', 'Decision & events'], ['files', 'Files'], ['errors', failed ? `Errors (${failed})` : 'Errors']];
  return html`<main class="z-loop loop" id="loop-zone" aria-label="Loop" hidden=${planning}>
    ${on && wrapUpVisible() ? html`<${WrapUp} />` : null}
    ${on ? html`<${ClaudeAccountChip} /><${PauseBanner} /><${TurnStrip} />
      <div class="z-loop-head"><${TurnHeader} turn=${turn} />
        <div class="z-loop-tabs">
          <${TabBar} tabs=${tabs} current=${tab} label="Loop views" className="z-ltabs" onPick=${pickTab} />
          <span class="z-spacer"></span>
          <button type="button" class="z-btn-global z-btn-inline" title="the dependency diagram of the whole roadmap" onClick=${openDepsDialog}>⤢ dependencies</button>
          ${tab === 'pipeline' ? html`<div class="z-toggle" role="group" aria-label="Pipeline view">${[['diagram', 'Diagram'], ['list', 'List']].map(([view, label]) => html`<button key=${view} type="button"
            class=${'z-toggle-btn mono' + (pipe.view === view ? ' is-on' : '')} aria-pressed=${String(pipe.view === view)} onClick=${() => pickView(view)}>${label}</button>`)}</div>` : null}
        </div></div>` : null}
    <div class="z-row">
      <div class="z-content" role="tabpanel">${on ? html`<${TabContent} />` : null}</div>
      <${Inspector} on=${on} />
    </div>
  </main>`;
}
