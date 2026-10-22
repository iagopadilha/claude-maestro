/* Pure helpers of the Feature dialog and of the compact task waves in the pipeline lanes: no DOM, no Preact, no stores.
   Input is the `tasks` object of a feature (GET feature/<slug> or /state `features[].tasks`):
   { tasks: [{ id, title, status, wave, deps?, executors?, ... }], max_parallel, waves?: [{ n, task_ids, split_by_limit }] }.
   Older states have no deps / waves / executors: waves are then derived from `task.wave`, no arrow is drawn and no agent chip claims
   an agent that the data does not show. Unknown values stay null, never invented. */

const GRAPH = { NODE_W: 214, NODE_H: 104, COLUMN_GAP: 70, ROW_GAP: 14, HEAD_H: 48, PAD: 10 };
const DEFAULT_WAVE = 1;
const SECONDS_PER_MINUTE = 60;
const TASK_STATUS_ORDER = ['running', 'failed', 'pending', 'done'];

const asList = (value) => (Array.isArray(value) ? value : []);

/** Task as the views use it: every optional field present, deps and executors always lists. */
function normalizeTask(task) {
  return {
    id: String(task.id), title: task.title == null ? '' : String(task.title), status: String(task.status || 'pending'),
    wave: Number.isFinite(task.wave) ? task.wave : DEFAULT_WAVE, deps: asList(task.deps).map(String), files: asList(task.files).map(String),
    parallel: !!task.parallel, note: task.note == null ? '' : String(task.note), executors: asList(task.executors),
  };
}

/** One wave: its tasks (in the order of `task_ids`, else of the task list), the parallel flag and the split-by-limit flag. */
function buildWave(waveNumber, tasks, serverWave) {
  const byId = Object.fromEntries(tasks.map((task) => [task.id, task]));
  const fromServer = serverWave ? asList(serverWave.task_ids).map((id) => byId[String(id)]).filter(Boolean) : [];
  const members = fromServer.length ? fromServer : tasks.filter((task) => task.wave === waveNumber);
  return { n: waveNumber, tasks: members, parallel: members.length > 1, splitByLimit: !!(serverWave && serverWave.split_by_limit) };
}

/** { tasks, waves, maxParallel, hasDeps } of a `tasks` payload; an empty or missing payload gives no tasks and no waves. */
export function taskGraphOf(payload) {
  const tasks = asList(payload && payload.tasks).filter((task) => task && task.id != null).map(normalizeTask);
  const serverWaves = asList(payload && payload.waves);
  const numbers = [...new Set(tasks.map((task) => task.wave).concat(serverWaves.map((wave) => wave.n).filter(Number.isFinite)))].sort((a, b) => a - b);
  const waves = numbers.map((waveNumber) => buildWave(waveNumber, tasks, serverWaves.find((wave) => wave.n === waveNumber)));
  const maxParallel = Number.isFinite(payload && payload.max_parallel) ? payload.max_parallel : null;
  return { tasks, waves: waves.filter((wave) => wave.tasks.length), maxParallel, hasDeps: tasks.some((task) => task.deps.length > 0) };
}

/** Label of a wave: "sequential" (one task) or "parallel ×N". */
export const waveModeLabel = (wave) => (wave.parallel ? `parallel ×${wave.tasks.length}` : 'sequential');

/** Note shown under a wave that the parallel limit cut short (null when it was not cut). */
export const splitNoteOf = (wave, maxParallel) => (wave.splitByLimit ? `split by the limit of ${maxParallel == null ? '?' : maxParallel}` : null);

/** Short duration: "42s", "3m 05s", "1h 02m". null/NaN gives ''. */
export function shortDuration(seconds) {
  if (!Number.isFinite(seconds)) return '';
  const whole = Math.floor(seconds);
  if (whole < SECONDS_PER_MINUTE) return `${whole}s`;
  const minutes = Math.floor(whole / SECONDS_PER_MINUTE);
  if (minutes < SECONDS_PER_MINUTE) return `${minutes}m ${String(whole % SECONDS_PER_MINUTE).padStart(2, '0')}s`;
  return `${Math.floor(minutes / SECONDS_PER_MINUTE)}h ${String(minutes % SECONDS_PER_MINUTE).padStart(2, '0')}m`;
}

/** What the agent chip of a task shows. `placeholder` says why there is no agent yet (no agent is claimed without data). */
export function agentChipOf(task) {
  const executors = asList(task.executors);
  if (!executors.length) {
    const placeholder = task.status === 'running' ? 'agent starting…' : task.status === 'pending' ? 'not spawned yet' : 'no agent recorded';
    return { placeholder, agent: null, attempts: 0 };
  }
  const latest = executors[executors.length - 1];
  const running = latest.state === 'running' || (!latest.return_status && !['done', 'failed', 'interrupted'].includes(latest.state) && task.status === 'running');
  return {
    placeholder: null, attempts: executors.length, running, agent: latest, role: latest.role || 'executor',
    result: running ? null : (latest.return_status || latest.state || null), duration: shortDuration(latest.duration_s),
  };
}

/** Counts of tasks by status, in a stable reading order: [['running', 2], ['pending', 3]]. */
export function statusCounts(tasks) {
  const counts = {};
  tasks.forEach((task) => { counts[task.status] = (counts[task.status] || 0) + 1; });
  const rank = (status) => (TASK_STATUS_ORDER.indexOf(status) + 1) || 9;
  return Object.entries(counts).sort((a, b) => rank(a[0]) - rank(b[0]));
}

const edgePath = (x1, y1, x2, y2) => { const pull = Math.max(24, (x2 - x1) / 2); return `M${x1},${y1} C${x1 + pull},${y1} ${x2 - pull},${y2} ${x2 - 2},${y2}`; };
/** An edge that skips a wave goes up to the lane above the nodes, across, and down into its target: drawn straight it would pass behind
    a task of the skipped wave and look like a dependency on that task. */
const skipEdgePath = (x1, y1, x2, y2, lane) => `M${x1},${y1} C${x1 + 18},${y1} ${x1 + 18},${lane} ${x1 + 36},${lane} L${x2 - 36},${lane} C${x2 - 18},${lane} ${x2 - 18},${y2} ${x2 - 2},${y2}`;

/** Positions for the dialog graph: one column per wave, nodes stacked in the column, an SVG edge for each dep that exists.
    { width, height, columns: [{ wave, x }], nodes: [{ task, x, y }], edges: [{ key, d, from, to, status }], geometry }. */
export function layoutTaskGraph(graph) {
  const { NODE_W, NODE_H, COLUMN_GAP, ROW_GAP, HEAD_H, PAD } = GRAPH;
  const columns = [], nodes = [], position = {};
  graph.waves.forEach((wave, index) => {
    const x = PAD + index * (NODE_W + COLUMN_GAP);
    columns.push({ wave, x });
    wave.tasks.forEach((task, row) => { const y = PAD + HEAD_H + row * (NODE_H + ROW_GAP); position[task.id] = { x, y }; nodes.push({ task, x, y }); });
  });
  const edges = [];
  graph.tasks.forEach((task) => task.deps.forEach((dep) => {
    const from = position[dep], to = position[task.id], source = graph.tasks.find((candidate) => candidate.id === dep);
    if (!from || !to || from.x >= to.x) return;
    const skips = to.x - from.x > NODE_W + COLUMN_GAP, lane = PAD + HEAD_H - ROW_GAP / 2;
    const d = skips ? skipEdgePath(from.x + NODE_W, from.y + NODE_H / 2, to.x, to.y + NODE_H / 2, lane) : edgePath(from.x + NODE_W, from.y + NODE_H / 2, to.x, to.y + NODE_H / 2);
    edges.push({ key: `${dep}>${task.id}`, d, from: dep, to: task.id, skips, status: source ? source.status : 'pending' });
  }));
  const rows = Math.max(1, ...graph.waves.map((wave) => wave.tasks.length));
  return { width: PAD * 2 + graph.waves.length * NODE_W + Math.max(0, graph.waves.length - 1) * COLUMN_GAP,
    height: PAD * 2 + HEAD_H + rows * NODE_H + (rows - 1) * ROW_GAP, columns, nodes, edges, geometry: GRAPH };
}

/** Rows of agent calls of a feature for the dialog list: the grouped calls of `featureCalls` flattened with their turn, else the
    agents of the feature endpoint (older panels without history). */
export function agentRowsOf(turns, detailAgents) {
  const fromTurns = asList(turns).flatMap((turn) => asList(turn.agents).map((agent) => ({
    id: agent.id, role: agent.role, turn: turn.n, state: agent.state, return_status: agent.return_status, duration_s: agent.duration_s,
    summary: agent.decision || agent.summary || null,
  })));
  if (fromTurns.length) return fromTurns;
  return asList(detailAgents).map((agent) => ({ id: agent.agent_id, role: agent.role, turn: agent.turn, state: agent.state,
    return_status: agent.return_status, duration_s: agent.duration_s, summary: agent.summary || null }));
}

/** The five steps of the timeline when the endpoint did not send them (older back end): derived from the roadmap status. */
export function fallbackSteps(status) {
  const order = ['spec', 'gate', 'build', 'verify', 'commit'];
  const reached = { pending: 0, speccing: 0, specced: 1, approved: 1, building: 2, verified: 5, blocked: 2 }[status];
  if (reached == null) return order.map((step) => ({ step, state: 'pending' }));
  return order.map((step, index) => ({ step, state: index < reached ? 'done' : index === reached ? (status === 'blocked' ? 'failed' : 'running') : 'pending' }));
}
