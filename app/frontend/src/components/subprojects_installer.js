/* Installer of a monorepo's setup (presentational): three steps, "1 Subprojects · 2 Conformance · 3 Maps", with Back / Next.
   The screen state and the API calls live in pages/panel/subprojects.js (SubprojectsInstallerPanel); nothing here talks to the server.
   The current step follows the back end gates (props.gates); Back lets the user look at an earlier step. */
import { html, useState, useEffect } from '../util/preact.js';
import { dur } from '../util/format.js';
import { Chip, Warnings, useNow, mapFileCount, mapQueueOf, MAP_DOCUMENT_TOTAL, DAMAGED_TEXT, isDamaged } from './subprojects_table.js';

const STEPS = [[1, 'Subprojects'], [2, 'Conformance'], [3, 'Maps']];
const CONFORMANCE = ['STANDARD', 'LEGACY'];
const STEP_TEXT = {
  1: 'This repository holds several projects. Check the list below: each one gets its own conformance and its own code map. Choose Skip for a folder the loop should leave alone.',
  2: "Choose STANDARD or LEGACY for each subproject. Each choice is saved in that subproject's own CLAUDE.md.",
  3: 'The AI reads the code of every subproject at once and writes up to 7 short documents in its docs/codebase/, then one system map says who talks to whom.',
};

// what each step writes, said plainly under the footer (INS-12: every step has its own sentence)
const STEP_SAFE = {
  1: 'Writes only .specs/project/SUBPROJECTS.json. No code is touched.',
  2: "Writes one Conformance block in each subproject's CLAUDE.md. No code is touched.",
  3: 'Writes only docs/codebase of each subproject and docs/codebase/SYSTEM.md. No code is touched.',
};

const isTarget = (row) => !row.skip && row.exists !== false;
const needsMap = (row) => isTarget(row) && row.has_code && !mapFileCount(row);
const suggestionOf = (row) => (row.suggestion && row.suggestion.suggestion) || '';
const itemOf = (queue, path) => (queue && queue.items && queue.items[path]) || null;
const seconds = (from, to) => { const a = Date.parse(from), b = Date.parse(to); return Number.isFinite(a) && Number.isFinite(b) ? Math.max(0, (b - a) / 1000) : null; };

/** Step indicator, also used by the single-repository installer (components/project_installer.js) with its own `steps`.
    allDone (WS-04): the current (last) step is finished too, so it shows the check like the earlier ones. */
export function StepIndicator({ step, steps = STEPS, allDone = false }) {
  return html`<ol class="sp-inst-steps" aria-label="Setup steps">${steps.map(([n, label]) => {
    const done = n < step || (allDone && n === step);
    return html`<li key=${n} class=${'sp-inst-step' + (done ? ' done' : n === step ? ' current' : '')} aria-current=${n === step ? 'step' : undefined}>
    <span class="sp-inst-n" aria-hidden="true">${done ? '✓' : n}</span><span>${label}</span></li>`;
  })}</ol>`;
}

// ---------- step 1: the list ----------
function ListRow({ row, editable, actions }) {
  const missing = row.exists === false;
  return html`<li class=${'sp-inst-row' + (missing ? ' sp-row-missing' : '') + (row.skip ? ' sp-row-skipped' : '')} data-path=${row.path}>
    <div class="sp-inst-main">
      ${editable
        ? html`<input class="sp-input" type="text" value=${row.name} aria-label=${'Name of ' + row.path} onInput=${(e) => actions.rename(row.path, e.currentTarget.value)} />`
        : html`<strong class="sp-inst-name">${row.name}</strong>`}
      <div class="sp-path">${row.path}</div>
      ${row.reason ? html`<div class="sp-muted">${row.reason}</div>` : null}
      <${Warnings} row=${row} />
    </div>
    <div class="sp-inst-chips"><${Chip} text=${row.stack || 'unknown'} />${row.skill ? html`<${Chip} text=${row.skill} kind="sp-chip-info" title="Skill the agents use here" />` : null}</div>
    <div class="sp-inst-toggle" role="group" aria-label=${'Map or skip ' + row.path}>
      <button type="button" class="sp-inst-opt" aria-pressed=${String(!row.skip)} disabled=${!editable} onClick=${() => actions.toggleSkip(row.path, false)}>Map</button>
      <button type="button" class="sp-inst-opt" aria-pressed=${String(!!row.skip)} disabled=${!editable} onClick=${() => actions.toggleSkip(row.path, true)}>Skip</button>
    </div>
    <div class="sp-inst-end">${editable ? html`<button type="button" class="btn btn-sm btn-ghost" onClick=${() => actions.removeRow(row.path)} aria-label=${'Remove ' + row.path}>Remove</button>` : null}</div>
  </li>`;
}

function SubprojectsStep({ data, rows, confirmed, locked, mapping, validating, now, mapError, actions }) {
  const validation = data.validation_job;
  const damaged = [data.error, data.validation_error, mapError].some(isDamaged);
  return html`<div class="sp-inst-body">
    <div class="sp-inst-tools">
      <button type="button" class="btn btn-sm" disabled=${locked || mapping} onClick=${actions.detectAgain} title=${confirmed ? 'Replaces the confirmed list (asks first).' : undefined}>Detect again</button>
      <button type="button" class="btn btn-sm" disabled=${locked || mapping} onClick=${actions.validate} title="An AI reviews the detected list (reads folders only).">Validate with AI</button>
      ${confirmed ? null : html`<button type="button" class="btn btn-sm" disabled=${locked} onClick=${actions.addFolder}>Add a folder…</button>`}
      <span class="sp-top-gap"></span>
      <button type="button" class="btn btn-sm btn-ghost" disabled=${locked || mapping} onClick=${actions.notMonorepo}>Not a monorepo</button>
    </div>
    ${validating ? html`<div class="sp-note" role="status"><span class="spinner sm" aria-hidden="true"></span> ${`Validating the list with AI… ${validation.started_at ? dur(seconds(validation.started_at, new Date(now).toISOString()) || 0) : ''}`}</div>` : null}
    ${data.reason && !confirmed ? html`<div class="sp-note">${'Detection: ' + data.reason}</div>` : null}
    ${data.validation_error ? html`<div class="sp-note sp-note-bad" role="alert">${'The AI validation did not work: ' + data.validation_error}</div>` : null}
    ${data.error ? html`<div class="sp-note sp-note-bad" role="alert">${data.error}</div>` : null}
    ${damaged ? html`<div class="sp-note sp-note-warn" role="alert"><strong>${DAMAGED_TEXT[0].toUpperCase() + DAMAGED_TEXT.slice(1)}.</strong> The block between the maestro:context markers was edited by hand; repair or remove the markers and the block is written again.</div>` : null}
    ${(data.rejected || []).length ? html`<div class="sp-rejected">${data.rejected.map((r) => html`<${Chip} key=${r.path} text=${`rejected: ${r.path}`} kind="sp-chip-warn" title=${r.reason} />`)}</div>` : null}
    ${(data.differences || []).length ? html`<div class="sp-diffs"><span class="sp-muted">AI changes:</span>${data.differences.map((d) => html`<${Chip} key=${d.path + d.change} text=${`${d.path}: ${d.change}`} kind="sp-chip-info" />`)}</div>` : null}
    ${(data.notes || []).length ? html`<ul class="sp-notes" aria-label="AI notes">${data.notes.map((n, i) => html`<li key=${i}>${n}</li>`)}</ul>` : null}
    ${data.root_map_leftover ? html`<div class="sp-note sp-note-info">The root docs/codebase has maps from an earlier version: the maps now live in each subproject; remove the root ones yourself when you want.</div>` : null}
    ${confirmed ? html`<div class="sp-note sp-note-info">The list is confirmed. To change it, use Detect again.</div>` : null}
    ${rows.length ? html`<ul class="sp-inst-list" aria-label="Subprojects">${rows.map((row) => html`<${ListRow} key=${row.path} row=${row} editable=${!confirmed && !locked} actions=${actions} />`)}</ul>`
      : html`<div class="sp-muted sp-inst-empty">No subprojects were found. Add a folder or detect again.</div>`}
  </div>`;
}

// ---------- step 2: one conformance line per subproject ----------
function ConformanceStep({ rows, choices, setChoice, locked }) {
  if (!rows.length) return html`<div class="sp-muted sp-inst-empty">Every subproject is skipped: nothing to choose.</div>`;
  return html`<ul class="sp-inst-list" aria-label="Conformance of each subproject">${rows.map((row) => {
    const suggested = suggestionOf(row), written = !!row.conformance;
    const value = choices[row.path] || row.conformance || suggested || 'STANDARD';
    const group = 'sp-inst-conf-' + row.path;
    return html`<li key=${row.path} class="sp-inst-row sp-inst-conf-row" data-path=${row.path}>
      <div class="sp-inst-main"><strong class="sp-inst-name">${row.name}</strong><div class="sp-path">${row.path}</div></div>
      <div class="sp-inst-chips"><${Chip} text=${row.stack || 'unknown'} /></div>
      <div class="sp-conf-radios" role="radiogroup" aria-label=${'Conformance of ' + row.path}>
        ${CONFORMANCE.map((option) => html`<label key=${option} class="sp-radio sp-inst-radio">
          <input type="radio" name=${group} value=${option} checked=${value === option} disabled=${locked || written} onChange=${() => setChoice(row.path, option)} />${option}
          ${!written && suggested === option ? html`<span class="sp-inst-tag" title=${(row.suggestion && row.suggestion.evidence) || 'Suggested from the code'}>suggested</span>` : null}</label>`)}
      </div>
      <div class="sp-inst-end">${written ? html`<span class="sp-muted">saved</span>` : null}</div>
    </li>`;
  })}</ul>`;
}

// ---------- step 3: the maps ----------
function MapLine({ row, item, now, locked, actions }) {
  const files = mapFileCount(row);
  const state = row.map && row.map.running ? 'running' : item ? item.state : files > 0 ? 'done' : 'waiting';
  const started = (item && item.started_at) || (row.map && row.map.job && row.map.job.started_at);
  const documents = Math.max(files, Number(item && item.documents) || 0);
  let status;
  if (state === 'running') status = html`<span class="sp-map-run" role="status"><span class="spinner sm" aria-hidden="true"></span><span>${`Mapping… ${started ? dur(seconds(started, new Date(now).toISOString()) || 0) : ''} · ${documents} of ${MAP_DOCUMENT_TOTAL} documents`}</span></span>`;
  else if (state === 'done') {
    const took = item && item.started_at && item.finished_at ? seconds(item.started_at, item.finished_at) : null;
    status = html`<span class="sp-inst-ok"><span aria-hidden="true">✓</span> ${`Done · ${documents} of ${MAP_DOCUMENT_TOTAL} documents${took != null ? ' · ' + dur(took) : ''}`}</span>`;
  } else if (state === 'failed') status = html`<span class="sp-bad" role="alert">${'Failed: ' + ((item && item.error) || 'the map failed')}</span>`;
  else status = html`<span class="sp-muted">${state === 'queued' ? 'Queued' : 'Waiting'}</span>`;
  return html`<li class="sp-inst-row sp-inst-map-row" data-path=${row.path} data-state=${state}>
    <div class="sp-inst-main"><strong class="sp-inst-name">${row.name}</strong><div class="sp-path">${row.path}</div></div>
    <div class="sp-inst-status">${status}</div>
    <div class="sp-inst-end">${state === 'failed' ? html`<button type="button" class="btn btn-sm" disabled=${locked} onClick=${() => actions.mapRetry(row)}>Retry</button>` : null}</div>
  </li>`;
}

/** State of the system map line: idle / waiting / running / done / failed (+ error). */
function systemState(data, queue) {
  const own = (queue && queue.system) || {};
  const state = own.state && own.state !== 'idle' ? own.state : data.system && data.system.exists ? 'done' : 'idle';
  return { state, error: own.error || '' };
}

function MapsStep({ data, rows, queue, now, locked, mapping, needsSystem, systemRetry, actions }) {
  const sys = systemState(data, queue);
  const lines = rows.filter((row) => row.has_code);
  const skippedNoCode = rows.filter((row) => !row.has_code);
  let sysStatus;
  if (sys.state === 'running') sysStatus = html`<span class="sp-map-run" role="status"><span class="spinner sm" aria-hidden="true"></span><span>Writing SYSTEM.md…</span></span>`;
  else if (sys.state === 'done') sysStatus = html`<span class="sp-inst-ok"><span aria-hidden="true">✓</span> Done · SYSTEM.md</span>`;
  else if (sys.state === 'failed') sysStatus = html`<span class="sp-bad" role="alert">${'Failed: ' + (sys.error || 'the system map failed')}</span>`;
  else if (sys.state === 'waiting') sysStatus = html`<span class="sp-muted">Waiting for the subprojects</span>`;
  else sysStatus = html`<span class="sp-muted">Waiting</span>`;
  return html`<div class="sp-inst-body">
    ${data.error ? html`<div class="sp-note sp-note-bad" role="alert">${data.error}</div>` : null}
    <ul class="sp-inst-list" aria-label="Maps">
      ${lines.map((row) => html`<${MapLine} key=${row.path} row=${row} item=${itemOf(queue, row.path)} now=${now} locked=${locked || mapping} actions=${actions} />`)}
      ${needsSystem ? html`<li class="sp-inst-row sp-inst-map-row sp-inst-system" data-path="__system__" data-state=${sys.state}>
        <div class="sp-inst-main"><strong class="sp-inst-name">System map</strong><div class="sp-path">Who talks to whom: SYSTEM.md</div></div>
        <div class="sp-inst-status">${sysStatus}</div>
        <div class="sp-inst-end">${sys.state === 'failed' ? html`<button type="button" class="btn btn-sm" disabled=${locked || mapping} onClick=${systemRetry}>Retry</button>` : null}</div>
      </li>` : null}
    </ul>
    ${skippedNoCode.length ? html`<div class="sp-muted">${'No code, nothing to map: ' + skippedNoCode.map((row) => row.path).join(', ')}</div>` : null}
  </div>`;
}

/**
 * <SubprojectsInstaller data draft busy mapError actions gates onContinue />
 * data: GET subprojects (map_queue = { state, items: {path: {state, started_at, finished_at, error, documents}}, system: {state, error} });
 * gates: { subprojects, conformance, map } booleans from the back end; onContinue(): the last button, after the maps are done.
 * actions: validate, confirmList (resolves true when saved), detectAgain, notMonorepo, addFolder, rename, toggleSkip, removeRow,
 *          saveConformances([{ path, value }]) (resolves true when saved), mapAll, mapRetry(row), systemMap.
 */
export function SubprojectsInstaller({ data, draft = null, busy = false, mapError = '', actions, gates = {}, onContinue, skipConformance = false, finishLabel = 'Continue to planning' }) {
  const gateStep = !gates.subprojects ? 1 : !gates.conformance && !skipConformance ? 2 : 3;
  const [view, setView] = useState(null); // a step the user went back to; null = follow the gates
  const [choices, setChoices] = useState({});
  useEffect(() => { setView(null); }, [gateStep]);
  const queue = mapQueueOf(data);
  const validation = data && data.validation_job;
  const validating = !!validation && validation.state === 'running';
  const queueRunning = !!queue && queue.state === 'running';
  const mapping = queueRunning || ((data && data.projects) || []).some((row) => row.map && row.map.running) || (!!queue && !!queue.system && ['running', 'waiting'].includes(queue.system.state) && queue.state !== 'stopped');
  const now = useNow(validating || mapping);
  if (!data) return html`<div class="sp-muted" role="status">Loading the subprojects…</div>`;

  const step = view || gateStep;
  const confirmed = !!data.confirmed;
  const allRows = draft || data.projects || [];
  const rows = step === 1 ? allRows : allRows.filter(isTarget);
  const locked = busy || validating;
  const mappable = rows.filter(needsMap);
  const eligibleWithCode = allRows.filter((row) => isTarget(row) && row.has_code);
  const needsSystem = !!data.system && eligibleWithCode.length >= 2;
  const sysState = systemState(data, queue).state;
  const systemPending = needsSystem && sysState !== 'done';
  const everMapped = allRows.some((row) => isTarget(row) && mapFileCount(row) > 0) || (!!queue && queue.state && queue.state !== 'idle');
  const allDone = !mapping && !mappable.length && !systemPending;

  const toConformance = rows.filter((row) => isTarget(row) && !row.conformance);
  const next = async () => {
    if (step === 1) {
      if (!confirmed && !(await actions.confirmList())) return;
      setView(skipConformance ? 3 : 2);
    } else if (step === 2) {
      const pending = toConformance.map((row) => ({ path: row.path, value: choices[row.path] || suggestionOf(row) || 'STANDARD' }));
      if (pending.length && !(await actions.saveConformances(pending))) return;
      setView(3);
    }
  };
  const backBlocked = step === 1 ? 'This is the first step.' : mapping ? 'The maps are running.' : step === 2 && everMapped ? 'The maps already ran: Detect again to change the list.' : '';
  const back = () => setView(skipConformance && step === 3 ? 1 : step - 1);
  const nextDisabled = locked || (step === 1 && (!allRows.length || !allRows.some(isTarget) || validating));
  const mapLabel = mappable.length
    ? `Map ${mappable.length} subproject${mappable.length === 1 ? '' : 's'}${needsSystem ? ' and the system' : ''}`
    : 'Map the system';

  return html`<div class="sp-inst" id="sp-installer" data-step=${step}>
    <${StepIndicator} step=${skipConformance && step === 3 ? 2 : step} steps=${skipConformance ? [[1, 'Subprojects'], [2, 'Maps']] : STEPS} allDone=${step === 3 && allDone && !!everMapped} />
    <p class="sp-inst-d">${STEP_TEXT[step]}</p>
    ${step === 1 ? html`<${SubprojectsStep} data=${data} rows=${rows} confirmed=${confirmed} locked=${locked} mapping=${mapping} validating=${validating} now=${now} mapError=${mapError} actions=${actions} />`
      : step === 2 ? html`<${ConformanceStep} rows=${rows} choices=${choices} setChoice=${(path, value) => setChoices((current) => ({ ...current, [path]: value }))} locked=${locked} />`
      : html`<${MapsStep} data=${data} rows=${rows} queue=${queue} now=${now} locked=${locked} mapping=${mapping} needsSystem=${needsSystem} systemRetry=${actions.systemMap} actions=${actions} />`}
    ${step === 3 && mapError ? html`<div class="sp-note sp-note-bad" role="alert">${mapError}</div>` : null}
    <div class="sp-inst-foot">
      <button type="button" class="btn" id="sp-inst-back" disabled=${!!backBlocked || locked} title=${backBlocked || undefined} onClick=${back}>Back</button>
      <span class="sp-top-gap"></span>
      ${step < 3 ? html`<button type="button" class="btn btn-primary" id="sp-inst-next" disabled=${nextDisabled} onClick=${next}>${busy ? 'Saving…' : 'Next'}</button>`
        : allDone ? html`<button type="button" class="btn btn-primary" id="sp-inst-continue" onClick=${onContinue}>${finishLabel}</button>`
        : html`<button type="button" class="btn btn-primary" id="sp-inst-map" disabled=${locked || mapping || (!mappable.length && !systemPending)} onClick=${actions.mapAll}>${mapping ? 'Mapping…' : mapLabel}</button>`}
    </div>
    <p class="plan-safe" id="sp-inst-safe">${STEP_SAFE[step]}</p>
  </div>`;
}
