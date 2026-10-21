/* Single repository: screen state and API calls of the setup installer (components/project_installer.js).
   Step 1 writes the root Conformance block (POST conformance, the route the old dialog used); step 2 starts the root map
   (POST codebase/map, no budget) and follows its job. The planning gates (conformance, map) decide the current step. */
import { html, useState, useEffect } from '../../util/preact.js';
import { list } from '../../util/format.js';
import { toast } from '../../components/toast.js';
import { confirmDialog } from '../../components/dialog.js';
import { ProjectInstaller, MAP_CONFIRM } from '../../components/project_installer.js';
import { ctx } from './context.js';
import { reloadProgress, startMapJob, mapRunning, lastMapJob, mapStartError } from './progress_trail.js';

const WRITTEN_PATTERN = /Conformance:\s*(STANDARD|LEGACY)/i;
const POLL_MS = 2000;
const progressStepOf = (id) => list(ctx.progress && ctx.progress.steps).find((step) => step.id === id) || null;

/** What the progress says about the root Conformance: the written value (from the step detail) and the script suggestion. */
function readConformance(conformanceGate) {
  const progressStep = progressStepOf('conformance');
  const isWritten = !!((progressStep && progressStep.ok) || (conformanceGate && conformanceGate.ok));
  const match = isWritten ? WRITTEN_PATTERN.exec(String((progressStep && progressStep.detail) || (conformanceGate && conformanceGate.detail) || '')) : null;
  return { written: match ? match[1].toUpperCase() : isWritten ? 'STANDARD' : '', suggestion: (progressStep && progressStep.suggestion) || null };
}

function readMap(mapGate) {
  const job = lastMapJob(), documents = list(mapGate && mapGate.items).length;
  const running = mapRunning();
  const failed = !running && ((job && job.state === 'error') || !!mapStartError());
  const done = !running && !failed && !!mapGate && !!mapGate.ok && documents > 0;
  const progressMap = progressStepOf('map');
  return {
    state: running ? 'running' : failed ? 'failed' : done ? 'done' : 'idle', documents,
    startedAt: job && job.started_at, finishedAt: job && job.finished_at,
    hasCode: !(progressMap && /without code/i.test(String(progressMap.detail || ''))), needed: !(mapGate && mapGate.optional),
    error: mapStartError() || (job && job.error) || '',
  };
}

/** <ProjectInstallerPanel conformanceGate mapGate reloadPlanning onContinue />: the gates are the planning gates `conformance` and `map`. */
export function ProjectInstallerPanel({ conformanceGate, mapGate, reloadPlanning, onContinue }) {
  const [busy, setBusy] = useState(false);
  const [, redraw] = useState(0);
  const map = readMap(mapGate);
  useEffect(() => {  // while the map job runs the gates are read again every 2 s (documents n/7, the end of the job)
    if (map.state !== 'running') return undefined;
    const timer = setInterval(() => { reloadPlanning(); redraw((n) => n + 1); }, POLL_MS);
    return () => clearInterval(timer);
  }, [map.state]);
  const actions = {
    async saveConformance(value) {
      setBusy(true);
      try {
        await ctx.api.post('conformance', { value });
        toast('Conformance saved.', 'ok');
        await reloadProgress(); reloadPlanning();
        return true;
      } catch (error) { toast('Not saved: ' + error.message, 'err'); return false; }
      finally { setBusy(false); }
    },
    async startMap() {
      const ok = await confirmDialog({ title: 'Map the project?', text: MAP_CONFIRM, yesText: 'Map now' });
      if (ok) { await startMapJob(); redraw((n) => n + 1); }
    },
    async retryMap() { await startMapJob(); redraw((n) => n + 1); }, // the user already confirmed this run
  };
  return html`<${ProjectInstaller} conformance=${readConformance(conformanceGate)} map=${map} busy=${busy} actions=${actions} onContinue=${onContinue} />`;
}
