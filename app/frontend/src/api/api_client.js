/* App HTTP client: token on every mutating method, uniform errors, polling only while the tab is visible
   and a disconnected banner with a retry every 5 s. The hub uses /api/app/; the panel uses /p/<id>/api/. */

const DEFAULT_TIMEOUT_MS = 8000;
const WRITE_TIMEOUT_MS = 120000; // POST/PUT may spawn a subprocess (check, relaunch, folder dialog)
export const POLLING_INTERVAL_MS = 2000;
export const RECONNECT_INTERVAL_MS = 5000;

export class ApiError extends Error {
  constructor(message, status, body) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.body = body;
  }
}

/** Token injected into the page by the server; ignores the raw "__MONITOR_TOKEN__" placeholder. */
export function getToken() {
  for (const m of document.querySelectorAll('meta[name="monitor-token"]')) {
    const c = m.getAttribute('content') || '';
    if (c && !c.startsWith('__')) return c;
  }
  return '';
}

/** 409 code:"planning_required" (the project is still in planning mode): one plain message for every caller. */
export const PLANNING_REQUIRED_MESSAGE = 'This project is still being planned: finish the plan and approve the roadmap on the planning screen first.';

async function readJson(res) { try { return await res.json(); } catch (_) { return null; } }

/** JSON request. GET without token; POST/PUT/DELETE with X-Monitor-Token. HTTP error or {ok:false} becomes ApiError. */
export async function requestJson(method, url, body, options = {}) {
  const timeoutMs = options.timeoutMs || (method === 'GET' ? DEFAULT_TIMEOUT_MS : WRITE_TIMEOUT_MS);
  const headers = {};
  if (method !== 'GET') {
    const token = getToken();
    if (!token) throw new ApiError('app token missing from the page; reload it from the app address', 0, null);
    headers['Content-Type'] = 'application/json';
    headers['X-Monitor-Token'] = token;
  }
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), timeoutMs);
  try {
    const res = await fetch(url, {
      method, cache: 'no-store', headers, signal: ctl.signal,
      body: method === 'GET' ? undefined : JSON.stringify(body || {}),
    });
    const data = await readJson(res);
    if (!res.ok || (method !== 'GET' && data && data.ok === false)) {
      const msg = data && data.code === 'planning_required' ? PLANNING_REQUIRED_MESSAGE : (data && data.error) || 'HTTP ' + res.status;
      throw new ApiError(msg, res.status, data);
    }
    return data == null ? {} : data;
  } catch (e) {
    if (e instanceof ApiError) throw e;
    if (e && e.name === 'AbortError') throw new ApiError('no response (timed out)', 0, null);
    throw new ApiError((e && e.message) || 'network failure', 0, null);
  } finally {
    clearTimeout(timer);
  }
}

function buildClient(base) {
  const url = (rel) => base + String(rel).replace(/^\/+/, '');
  return {
    base,
    url,
    get: (rel, timeoutMs) => requestJson('GET', url(rel), null, { timeoutMs }),
    post: (rel, body, timeoutMs) => requestJson('POST', url(rel), body, { timeoutMs }),
    put: (rel, body, timeoutMs) => requestJson('PUT', url(rel), body, { timeoutMs }),
  };
}

/** Hub client: routes /api/app/... */
export const appApi = buildClient('/api/app/');

/** Project id from the URL /p/<id>/ (null outside the panel). */
export function projectIdFromUrl(pathname = location.pathname) {
  const m = /^\/p\/([^/]+)\/?/.exec(pathname);
  return m ? m[1] : null;
}

/** Panel client: routes relative to the project, /p/<id>/api/... */
export function createProjectClient(projectId) {
  return buildClient(`/p/${projectId}/api/`);
}

/** Detail line of the "app disconnected" banner. */
export const disconnectedDetail = (message) => `retrying every 5 s${message ? ' (' + message + ')' : ''}. The screen below is the last one received.`;
/** "Disconnected" banner (hub): shows/hides it and writes the detail, without losing what is on screen. */
export function showDisconnectedBanner(banner, detail, disconnected, message) {
  if (!banner) return;
  banner.hidden = !disconnected;
  if (detail) detail.textContent = disconnected ? disconnectedDetail(message) : '';
}

/**
 * Polling that only runs while the tab is visible. `task` is async; success schedules in `intervalMs`, error in 5 s.
 * Returns { now(ms), stop(), isDisconnected() }.
 */
export function createPolling({ task, intervalMs = POLLING_INTERVAL_MS, onConnectionChange }) {
  let timer = null, running = false, stopped = false, disconnected = false;
  async function cycle() {
    if (stopped || running) return;
    if (document.visibilityState !== 'visible') return; // resumes on visibilitychange
    running = true;
    try {
      await task();
      if (disconnected) { disconnected = false; if (onConnectionChange) onConnectionChange(false, ''); }
      schedule(intervalMs);
    } catch (e) {
      const stopForGood = e && e.stopPolling;
      disconnected = !stopForGood;
      if (onConnectionChange) onConnectionChange(!stopForGood, (e && e.message) || '', e);
      if (!stopForGood) schedule(RECONNECT_INTERVAL_MS);
    } finally { running = false; }
  }
  function schedule(ms) { clearTimeout(timer); if (!stopped) timer = setTimeout(cycle, ms); }
  document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'visible') { clearTimeout(timer); cycle(); } });
  return {
    now: (ms = 0) => schedule(ms),
    stop: () => { stopped = true; clearTimeout(timer); },
    isDisconnected: () => disconnected,
  };
}

/** Follows a job (`GET jobs/<id>` or similar) until state ok/error. `onUpdate` receives every read. */
export function followJob(client, route, { intervalMs = 2000, giveUpMs = 30 * 60 * 1000, onUpdate }) {
  let cancelled = false;
  const start = Date.now();
  const promise = new Promise((resolve, reject) => {
    async function step() {
      if (cancelled) return;
      if (Date.now() - start > giveUpMs) { reject(new ApiError('timed out waiting for the job', 0, null)); return; }
      try {
        const r = await client.get(route, 10000);
        if (cancelled) return;
        if (onUpdate) onUpdate(r);
        if (r && (r.state === 'ok' || r.state === 'error')) { resolve(r); return; }
      } catch (e) {
        if (e.status === 404) { reject(e); return; }
      }
      setTimeout(step, document.visibilityState === 'visible' ? intervalMs : intervalMs * 3);
    }
    step();
  });
  return { promise, cancel: () => { cancelled = true; } };
}
