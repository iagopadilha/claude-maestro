/* Pipeline model of the LOOP zone: loads GET pipeline and answers every question the views ask about it.
   Event sourcing on the client: the state of the loop at turn N is the replay of the events of turns 1..N, so
   selecting an old turn shows the loop as it was and "changed in this turn" is just the events of turn N.
   Nothing is invented: what the server does not send stays null/empty and the views say so. Never throws on
   missing, partial or failed data (the views read the message from emptyReason()). */
import { ctx } from './context.js';
import { list, isNum } from '../../util/format.js';
import { renderAll, requestRender } from '../project_panel.js';

// ---------- visual constants (design palette) ----------
export const STEP_COLOR = { DONE: '#3fb950', RUNNING: '#f5b82e', QUEUED: '#7fb2ff', FAIL: '#f85149', BLOCKED: '#f85149', PENDING: '#4b525c' };
export const FEATURE_COLOR = { DONE: '#3fb950', RUNNING: '#f5b82e', QUARANTINE: '#f85149', ATTENTION: '#f5b82e', 'IN PROGRESS': '#7fb2ff', 'NOT DISPATCHED': '#4b525c' };
const COLOR_AMBER = '#f5b82e', COLOR_GREEN = '#3fb950', COLOR_RED = '#f85149', COLOR_IDLE = '#4b525c';
const PENDING = 'PENDING';
/** The fixed flow shown for every feature when the server sends no steps. */
const DEFAULT_STEPS = [
  { key: 'spec', agent: 'spec-writer', role: 'writes the spec (PATHS + criteria)' },
  { key: 'review', agent: 'spec-reviewer', role: 'reviews the spec' },
  { key: 'gate', agent: 'gate', role: 'approves for execution' },
  { key: 'exec', agent: 'executor', role: 'implements' },
  { key: 'verify', agent: 'verifier', role: 'build · types · lint · tests' },
  { key: 'commit', agent: 'commit', role: 'git checkpoint' },
];
const RUNNING_REFRESH_MS = 10000;
const REQUEST_TIMEOUT_MS = 30000;

/** Screen state: data (normalized GET pipeline), sel = selected turn n (null = live), node = {f, s} | {orch:true} | null. */
export const pipe = { data: null, loading: false, err: null, key: undefined, readAt: 0, queued: null, sel: null, node: null, view: 'diagram', tab: 'pipeline' };

// ---------- loading ----------
const stateKey = (state) => `${state && state.meta ? state.meta.turns : null}|${state && state.loop ? state.loop.status : null}|${state && state.loop ? state.loop.turn : null}`;

function normalizeEvent(raw) {
  const event = raw && typeof raw === 'object' ? raw : {};
  return { feature: event.feature || null, step: isNum(event.step) ? event.step : -1, status: String(event.status || 'DONE').toUpperCase(),
    out: event.out == null ? '' : String(event.out), agent_id: event.agent_id || null, agent: event.agent || null };
}
function normalizeTurn(raw) {
  const turn = raw && typeof raw === 'object' ? raw : {};
  return { n: turn.n, label: turn.label || '', status: turn.status || null, cost_usd: isNum(turn.cost_usd) ? turn.cost_usd : null,
    duration_s: isNum(turn.duration_s) ? turn.duration_s : null, decision: turn.decision || null,
    events: list(turn.events).map(normalizeEvent), files: list(turn.files).filter((file) => file && file.path) };
}
function normalizePayload(raw) {
  const payload = raw && typeof raw === 'object' ? raw : {};
  return { steps: list(payload.steps), features: list(payload.features).filter((feature) => feature && feature.id), stages: list(payload.stages),
    turns: list(payload.turns).map(normalizeTurn).filter((turn) => isNum(turn.n)).sort((a, b) => a.n - b.n),
    live_turn: isNum(payload.live_turn) ? payload.live_turn : null, paused: !!payload.paused, quarantine: list(payload.quarantine) };
}

async function fetchPipeline(key, force) {
  if (!ctx.api) return;
  if (pipe.loading) { if (force || key !== pipe.key) pipe.queued = { key, force }; return; }
  if (!force && pipe.key === key) return;
  pipe.loading = true; pipe.key = key; pipe.err = null;
  if (!pipe.data) renderAll();
  try {
    pipe.data = normalizePayload(await ctx.api.get('pipeline', REQUEST_TIMEOUT_MS));
  } catch (error) {
    pipe.err = error && error.status === 404 ? 'The server does not offer the pipeline yet.' : (error && error.message) || 'Could not load the pipeline.';
  } finally {
    pipe.loading = false; pipe.readAt = Date.now();
    renderAll();
    const next = pipe.queued; pipe.queued = null;
    if (next) fetchPipeline(next.key, next.force);
  }
}
/** Reads GET pipeline (skips when already read for the current state, unless forced). */
export async function loadPipeline(force = false) { await fetchPipeline(stateKey(ctx.state), force); }
/** Call on every /state: reloads when meta.turns, loop.status or loop.turn changed, and every ~10 s while the loop runs. */
export function maybeReloadPipeline(state) {
  const key = stateKey(state);
  if (key !== pipe.key) { fetchPipeline(key, false); return; }
  if (state && state.loop && state.loop.status === 'running' && !pipe.loading && Date.now() - pipe.readAt > RUNNING_REFRESH_MS) fetchPipeline(key, true);
}

// ---------- derived data (memoized per payload: every load replaces pipe.data) ----------
let memo = { data: null, features: null, states: new Map(), dependents: new Map() };
function memoFor() {
  if (memo.data !== pipe.data) memo = { data: pipe.data, features: null, states: new Map(), dependents: new Map() };
  return memo;
}
const turnsData = () => (pipe.data ? pipe.data.turns : []);

/** The steps of the fixed flow (server list, else the default). */
export function steps() { return pipe.data && pipe.data.steps.length ? pipe.data.steps : DEFAULT_STEPS; }
/** n of the last turn, or null with no turns. */
export function liveN() { const all = turnsData(); return all.length ? all[all.length - 1].n : null; }
/** Turn being shown: the selected one (when it still exists) or the live one. */
export function curN() {
  if (pipe.sel != null && turnsData().some((turn) => turn.n === pipe.sel)) return pipe.sel;
  return liveN();
}
/** Selects a turn (null = live), going back to the default node of that turn. */
export function selectTurn(n) { pipe.sel = n; pipe.node = null; requestRender(); }
/** Selects a node: {f: featureId, s: stepIndex} or {orch: true}. */
export function selectNode(node) { pipe.node = node; requestRender(); }

/** Events of turn n, as recorded (empty when the turn does not exist). */
export function turnEvents(n) { const turn = turnsData().find((item) => item.n === n); return turn ? turn.events : []; }
/** The turn n as recorded, or null. */
export function turnAt(n) { return turnsData().find((item) => item.n === n) || null; }
/** FAIL/BLOCKED events of turn n. */
export function turnFailures(n) { return turnEvents(n).filter((event) => event.status === 'FAIL' || event.status === 'BLOCKED'); }
/** Set of "feature:step" touched by the events of turn n ("changed in this turn"). */
export function changedStepsAt(n) { return new Set(turnEvents(n).filter((event) => event.feature).map((event) => `${event.feature}:${event.step}`)); }

function turnDot(turn, isLiveTurn) {
  const statuses = turn.events.map((event) => event.status);
  if (isLiveTurn && pipe.data && (pipe.data.paused || turn.status === 'running' || turn.status === 'awaiting_user')) return COLOR_AMBER;
  if (statuses.includes('RUNNING')) return COLOR_AMBER;
  if (statuses.includes('FAIL') || statuses.includes('BLOCKED') || turn.status === 'error') return COLOR_RED;
  if (!statuses.length && (turn.status === 'running' || turn.status === 'awaiting_user')) return COLOR_AMBER;
  if (!statuses.length && turn.status === 'continue') return COLOR_IDLE;
  return COLOR_GREEN;
}
/** Turn strip data: dot = worst status of the events (amber for the live paused/running turn); changes = number of events. */
export function turnsList() {
  const last = liveN();
  return turnsData().map((turn) => ({ n: turn.n, label: turn.label, status: turn.status, cost_usd: turn.cost_usd, duration_s: turn.duration_s,
    decision: turn.decision, changes: turn.events.length, dot: turnDot(turn, turn.n === last), events: turn.events, files: turn.files,
    claude: turn.claude, claude_account: turn.claude_account }));
}

/** Readable name of a feature (GET pipeline features[].name, else ctx.state.features[].name); the slug itself when there is none. */
export function featureName(id) {
  const slug = String(id == null ? '' : id);
  const piped = pipe.data ? pipe.data.features.find((feature) => feature.id === slug) : null;
  const stated = list(ctx.state && ctx.state.features).find((feature) => feature && feature.id === slug);
  const name = String((piped && piped.name) || (stated && stated.name) || '').trim();
  return name || slug;
}

/** Features of the roadmap with their wave (server wave, else 1 + max wave of the slug deps; "dir:" deps ignored). */
export function featuresWithWaves() {
  const cache = memoFor();
  if (cache.features) return cache.features;
  const features = pipe.data ? pipe.data.features : [], byId = Object.fromEntries(features.map((feature) => [feature.id, feature])), resolved = {};
  const waveOf = (id, trail) => {
    if (resolved[id] != null) return resolved[id];
    const feature = byId[id];
    if (feature && isNum(feature.wave)) return (resolved[id] = feature.wave);
    if (!feature || trail.includes(id)) return 1;
    const deps = list(feature.deps).filter((dep) => byId[dep]);
    return (resolved[id] = deps.length ? 1 + Math.max(...deps.map((dep) => waveOf(dep, [...trail, id]))) : 1);
  };
  cache.features = features.map((feature) => ({ ...feature, deps: list(feature.deps), wave: waveOf(feature.id, []) }));
  return cache.features;
}

/** Replays the events of turns 1..n (n null = live): { [featureId]: [ { st, runs: [{turn, st, out, agent_id, agent}] } per step ] }. */
export function stateAt(n) {
  const limit = n == null ? liveN() : n, cache = memoFor();
  if (limit == null) return {};
  if (cache.states.has(limit)) return cache.states.get(limit);
  const stepCount = steps().length, state = {};
  for (const turn of turnsData()) {
    if (turn.n > limit) break;
    for (const event of turn.events) {
      if (!event.feature || event.step < 0 || event.step >= stepCount) continue;
      if (!state[event.feature]) state[event.feature] = Array.from({ length: stepCount }, () => ({ st: PENDING, runs: [] }));
      const slot = state[event.feature][event.step];
      slot.st = event.status;
      slot.runs.push({ turn: turn.n, st: event.status, out: event.out, agent_id: event.agent_id, agent: event.agent });
    }
  }
  cache.states.set(limit, state);
  return state;
}

/** Whole-feature status at turn n, derived from the steps (never stored apart, so it cannot disagree with them). */
export function featureStatusAt(id, n) {
  const at = n == null ? curN() : n, stepsOfFeature = stateAt(at)[id];
  if (!stepsOfFeature) return 'NOT DISPATCHED';
  if (stepsOfFeature.every((step) => step.st === 'DONE')) return 'DONE';
  if (at === liveN() && pipe.data && pipe.data.quarantine.includes(id)) return 'QUARANTINE';
  if (stepsOfFeature.some((step) => step.st === 'RUNNING')) return 'RUNNING';
  if (stepsOfFeature.some((step) => step.st === 'FAIL' || step.st === 'BLOCKED')) return 'ATTENTION';
  return 'IN PROGRESS';
}

/** Every run of a step across all turns (oldest first). */
export function stepRuns(featureId, stepIndex) {
  const stepsOfFeature = stateAt(liveN())[featureId];
  return stepsOfFeature && stepsOfFeature[stepIndex] ? stepsOfFeature[stepIndex].runs : [];
}

/** Transitive dependents of a feature (slug deps only), each once. */
export function dependents(id) {
  const cache = memoFor();
  if (cache.dependents.has(id)) return cache.dependents.get(id);
  const features = featuresWithWaves(), found = [], seen = new Set([id]), queue = [id];
  while (queue.length) {
    const current = queue.shift();
    for (const feature of features) {
      if (!seen.has(feature.id) && feature.deps.includes(current)) { seen.add(feature.id); found.push(feature.id); queue.push(feature.id); }
    }
  }
  cache.dependents.set(id, found);
  return found;
}

/** Node selected in the pipeline: the clicked one when it exists at the shown turn, else the first event of the turn, else null. */
export function activeNode() {
  const at = curN();
  if (at == null) return null;
  const state = stateAt(at), node = pipe.node;
  if (node && (node.orch || (state[node.f] && node.s >= 0 && node.s < steps().length))) return node;
  const first = turnEvents(at).find((event) => event.feature && state[event.feature]);
  return first ? { f: first.feature, s: first.step } : null;
}

/** Why there is nothing to draw yet (loading, error, zero turns), or null when the data is there. */
export function emptyReason() {
  if (pipe.err && !pipe.data) return pipe.err;
  if (!pipe.data) return pipe.loading || pipe.key === undefined ? 'Loading the pipeline…' : 'The pipeline is not available.';
  if (!pipe.data.turns.length) return 'No turn recorded yet. The pipeline appears when the loop runs its first turn.';
  return null;
}
