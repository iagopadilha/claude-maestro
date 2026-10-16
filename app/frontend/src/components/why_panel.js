/* "Why use this panel?" dialog: opens from the "?" button and by itself on the first visit (remembered in localStorage). */
import { html } from '../util/preact.js';
import { openDialog } from './dialog.js';
import { readStored, writeStored } from '../util/storage.js';

const SEEN_KEY = 'harness.why.seen.v1';

const GLOSSARY = [
  ['turn', 'one run of claude -p with the /orchestrator skill. The loop is a sequence of turns; each one moves the roadmap forward and writes the state.'],
  ['wave', 'features that run together in the same turn because they touch different files and do not depend on each other.'],
  ['gate', 'quality gate: a spec only becomes code after the spec-reviewer approves it; code only becomes verified after the verifier passes it.'],
  ['quarantine', 'where a feature that got stuck (blocked) waits. The loop carries on with what does not depend on it; you decide to try again or skip it.'],
  ['VALIDATE', 'VALIDATE.md: decisions the loop took on its own that you should check (with what it chose and how to revert it).'],
  ['ANSWERS', 'ANSWERS.md: your answers and instructions to the loop; the next turn reads and applies them.'],
  ['worktree', 'a working copy of the repository in a sibling folder (…-worktree-loop) on the loop branch. The loop works there, not in your folder.'],
];

const PLEX_SANS = 'IBM Plex Sans, sans-serif', PLEX_MONO = 'IBM Plex Mono, monospace';
const Box = ({ x, y, w, title, sub, color }) => html`<g>
  <rect x=${x} y=${y} width=${w} height="58" rx="10" fill="#15171A" stroke=${color} stroke-width="1.5" />
  <text x=${x + w / 2} y=${y + 24} text-anchor="middle" fill="#E6E6E3" font-size="13" font-weight="600" font-family=${PLEX_SANS}>${title}</text>
  <text x=${x + w / 2} y=${y + 42} text-anchor="middle" fill="#8B9098" font-size="10.5" font-family=${PLEX_MONO}>${sub}</text>
</g>`;
const Arrow = ({ x1, x2, y, label }) => html`<g>
  <path d=${`M${x1} ${y} L${x2 - 4} ${y}`} stroke="#4A4F57" stroke-width="1.5" marker-end="url(#why-arrow)" />
  <text x=${(x1 + x2) / 2} y=${y - 7} text-anchor="middle" fill="#7D838C" font-size="9.5" font-family=${PLEX_MONO}>${label}</text>
</g>`;

function FlowDiagram() {
  return html`<svg class="why-diagram" viewBox="0 0 660 92" role="img" aria-label="Flow: the /orchestrator skill is the brain; the app engine runs the turns; the state lives in .specs files; the panel reads all of it and sends your clicks back to the engine.">
    <defs><marker id="why-arrow" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto"><path d="M0 0 L8 4 L0 8 z" fill="#4A4F57" /></marker></defs>
    <${Box} x=${4} y=${18} w=${140} title="/orchestrator skill" sub="brain (prompt)" color="#A58BFF" />
    <${Arrow} x1=${144} x2=${186} y=${47} label="each turn" />
    <${Box} x=${186} y=${18} w=${140} title="app · engine" sub="run_loop, state…" color="#F2B544" />
    <${Arrow} x1=${326} x2=${368} y=${47} label="writes" />
    <${Box} x=${368} y=${18} w=${120} title=".specs/" sub="dozens of .md/.json" color="#5CC8D6" />
    <${Arrow} x1=${488} x2=${530} y=${47} label="reads all" />
    <${Box} x=${530} y=${18} w=${126} title="panel" sub="one single view" color="#5BD38A" />
    <path d="M593 76 C593 90, 256 90, 256 78" fill="none" stroke="#F2B544" stroke-width="1.2" stroke-dasharray="4 4" marker-end="url(#why-arrow)" />
    <text x="424" y="88" text-anchor="middle" fill="#F2B544" font-size="9.5" font-family=${PLEX_MONO}>your clicks (with confirmation)</text>
  </svg>`;
}

/** Body of the "Why use this panel?" dialog. */
export function WhyContent() {
  const p = (text) => html`<p class="dlg-text">${text}</p>`;
  return html`
    ${p('Loop Harness is a local control panel for loop engineering and graph engineering on top of Claude Code. It runs on your machine and uses your own Claude subscription through the claude CLI: no API token needed.')}
    ${p('The loop produces dozens of files: ROADMAP, STATE, VALIDATE, ANSWERS, LOOP.json, one spec per feature, turns, snapshots and the transcript of every agent. Opening .md files one by one does not show the whole. The panel gathers them in one view: the roadmap as a graph, the turns and agents, the specs, the questions from the loop, what to validate, the cost and the limits.')}
    <${FlowDiagram} />
    <ul class="why-list">
      <li><strong>Act on a specific point. </strong>Open a node of the graph (a feature, a turn, an agent) and work on exactly that point: inspect it, chat about it, instruct the loop or answer its question.</li>
      <li><strong>Commands become buttons. </strong>harness.py loop --check, --answer, --option and --rewind, the loop state and the delivery become buttons with confirmation: fewer commands to remember, fewer copy-and-paste mistakes.</li>
      <li><strong>Any starting point. </strong>A new project (creates the folder and .specs), an existing repository without .specs (maps it and generates the roadmap) or a loop stopped midway (opens it and shows where it stopped).</li>
      <li><strong>Who does what. </strong>The loop runs in the app engine. The brain is the /orchestrator skill of the plugin: the prompt every turn runs. The panel only shows and triggers what you click.</li>
    </ul>
    <div class="small-label">Mini glossary</div>
    <dl class="why-glossary">${GLOSSARY.map(([t, d]) => [html`<dt key=${'t-' + t}>${t}</dt>`, html`<dd key=${'d-' + t}>${d}</dd>`])}</dl>`;
}

export function openWhy() {
  writeStored(SEEN_KEY, true);
  openDialog({
    title: 'Why use this panel?', width: 720, cls: 'dlg-why',
    content: html`<${WhyContent} />`,
    actions: [{ text: 'Got it', cls: 'btn-primary' }],
  });
}

/** Opens by itself only on the first visit (without storage it opens every time it cannot remember: acceptable). */
export function openWhyOnFirstVisit() {
  if (!readStored(SEEN_KEY, false)) openWhy();
}

/** The "?" button that opens the dialog. */
export function WhyButton() {
  return html`<button type="button" class="btn btn-ghost btn-help" aria-label="Why use this panel?" title="Why use this panel?" onClick=${openWhy}>?</button>`;
}
