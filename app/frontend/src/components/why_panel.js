/* "Why use this panel?" dialog: opens from the "?" button and by itself on the first visit (remembered in localStorage). */
import { h, sv } from '../util/dom.js';
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

function flowDiagram() {
  const box = (x, y, w, title, sub, color) => sv('g', {},
    sv('rect', { x, y, width: w, height: 58, rx: 10, fill: '#15171A', stroke: color, 'stroke-width': 1.5 }),
    sv('text', { x: x + w / 2, y: y + 24, 'text-anchor': 'middle', fill: '#E6E6E3', 'font-size': 13, 'font-weight': 600, 'font-family': 'IBM Plex Sans, sans-serif', text: title }),
    sv('text', { x: x + w / 2, y: y + 42, 'text-anchor': 'middle', fill: '#8B9098', 'font-size': 10.5, 'font-family': 'IBM Plex Mono, monospace', text: sub }));
  const arrow = (x1, x2, y, label) => sv('g', {},
    sv('path', { d: `M${x1} ${y} L${x2 - 4} ${y}`, stroke: '#4A4F57', 'stroke-width': 1.5, 'marker-end': 'url(#why-arrow)' }),
    sv('text', { x: (x1 + x2) / 2, y: y - 7, 'text-anchor': 'middle', fill: '#7D838C', 'font-size': 9.5, 'font-family': 'IBM Plex Mono, monospace', text: label }));
  return sv('svg', { class: 'why-diagram', viewBox: '0 0 660 92', role: 'img', 'aria-label': 'Flow: the /orchestrator skill is the brain; the app engine runs the turns; the state lives in .specs files; the panel reads all of it and sends your clicks back to the engine.' },
    sv('defs', {}, sv('marker', { id: 'why-arrow', viewBox: '0 0 8 8', refX: 7, refY: 4, markerWidth: 7, markerHeight: 7, orient: 'auto' }, sv('path', { d: 'M0 0 L8 4 L0 8 z', fill: '#4A4F57' }))),
    box(4, 18, 140, '/orchestrator skill', 'brain (prompt)', '#A58BFF'),
    arrow(144, 186, 47, 'each turn'),
    box(186, 18, 140, 'app · engine', 'run_loop, state…', '#F2B544'),
    arrow(326, 368, 47, 'writes'),
    box(368, 18, 120, '.specs/', 'dozens of .md/.json', '#5CC8D6'),
    arrow(488, 530, 47, 'reads all'),
    box(530, 18, 126, 'panel', 'one single view', '#5BD38A'),
    sv('path', { d: 'M593 76 C593 90, 256 90, 256 78', fill: 'none', stroke: '#F2B544', 'stroke-width': 1.2, 'stroke-dasharray': '4 4', 'marker-end': 'url(#why-arrow)' }),
    sv('text', { x: 424, y: 88, 'text-anchor': 'middle', fill: '#F2B544', 'font-size': 9.5, 'font-family': 'IBM Plex Mono, monospace', text: 'your clicks (with confirmation)' }));
}

export function openWhy() {
  writeStored(SEEN_KEY, true);
  const p = (text) => h('p', { class: 'dlg-text', text });
  openDialog({
    title: 'Why use this panel?', width: 720, cls: 'dlg-why',
    content: [
      p('Loop Harness is a local control panel for loop engineering and graph engineering on top of Claude Code. It runs on your machine and uses your own Claude subscription through the claude CLI: no API token needed.'),
      p('The loop produces dozens of files: ROADMAP, STATE, VALIDATE, ANSWERS, LOOP.json, one spec per feature, turns, snapshots and the transcript of every agent. Opening .md files one by one does not show the whole. The panel gathers them in one view: the roadmap as a graph, the turns and agents, the specs, the questions from the loop, what to validate, the cost and the limits.'),
      flowDiagram(),
      h('ul', { class: 'why-list' },
        h('li', {}, h('strong', { text: 'Act on a specific point. ' }), 'Open a node of the graph (a feature, a turn, an agent) and work on exactly that point: inspect it, chat about it, instruct the loop or answer its question.'),
        h('li', {}, h('strong', { text: 'Commands become buttons. ' }), 'harness.py loop --check, --answer, --option and --rewind, the loop state and the delivery become buttons with confirmation: fewer commands to remember, fewer copy-and-paste mistakes.'),
        h('li', {}, h('strong', { text: 'Any starting point. ' }), 'A new project (creates the folder and .specs), an existing repository without .specs (maps it and generates the roadmap) or a loop stopped midway (opens it and shows where it stopped).'),
        h('li', {}, h('strong', { text: 'Who does what. ' }), 'The loop runs in the app engine. The brain is the /orchestrator skill of the plugin: the prompt every turn runs. The panel only shows and triggers what you click.')),
      h('div', { class: 'small-label', text: 'Mini glossary' }),
      h('dl', { class: 'why-glossary' }, GLOSSARY.map(([t, d]) => [h('dt', { text: t }), h('dd', { text: d })])),
    ],
    actions: [{ text: 'Got it', cls: 'btn-primary' }],
  });
}

/** Opens by itself only on the first visit (without storage it opens every time it cannot remember: acceptable). */
export function openWhyOnFirstVisit() {
  if (!readStored(SEEN_KEY, false)) openWhy();
}

export function whyButton() {
  return h('button', { type: 'button', class: 'btn btn-ghost btn-help', 'aria-label': 'Why use this panel?', title: 'Why use this panel?', onclick: openWhy }, '?');
}
