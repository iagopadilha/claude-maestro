/* Table of the subprojects of a monorepo (presentational): the screen state and the API calls live in
   pages/panel/subprojects.js. Every action is a callback in `actions`; nothing here talks to the server. */
import { html, useState, useEffect } from '../util/preact.js';
import { dur } from '../util/format.js';

export const MAP_DOCUMENT_TOTAL = 7;
const NOT_CONFIRMED_HINT = 'Confirm the list of subprojects first.';
export const DAMAGED_TEXT = 'context block damaged';
const ROOT_MAP_LEFTOVER_TEXT = 'The root docs/codebase has maps from an earlier version: the maps now live in each subproject; remove the root ones yourself when you want.';

export const isDamaged = (text) => /context block damaged/i.test(String(text || ''));
export const elapsedText = (job, now) => {
  const started = job && Date.parse(job.started_at);
  return Number.isFinite(started) ? dur(Math.max(0, (now - started) / 1000)) : '';
};

/** Re-renders every second while `active` so the elapsed time moves. */
export function useNow(active) {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    if (!active) return undefined;
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [active]);
  return now;
}

export const Chip = ({ text, kind = '', title }) => html`<span class=${'sp-chip ' + kind} title=${title || undefined}>${text}</span>`;

/** The map queue of the payload: `map_queue` (items by path) of the parallel back end, or the old `queue` of an older one. */
export const mapQueueOf = (data) => (data && (data.map_queue || data.queue)) || null;

/** State of one row in the queue: running / queued / queued_stopped / failed / null. */
export function mapQueueState(row, queue) {
  if (row.map && row.map.running) return { kind: 'running' };
  if (!queue) return null;
  const item = queue.items && queue.items[row.path];
  if (item) {
    if (item.state === 'running') return { kind: 'running' };
    if (item.state === 'failed') return { kind: 'failed', reason: item.error || 'the map failed' };
    if (item.state === 'queued') return { kind: queue.state === 'stopped' ? 'queued_stopped' : 'queued' };
    return null;
  }
  const failedReason = queue.failed && queue.failed[row.path];
  if (failedReason) return { kind: 'failed', reason: failedReason };
  if (queue.current === row.path) return { kind: 'running' };
  if ((queue.queue || []).includes(row.path)) return { kind: queue.state === 'stopped' ? 'queued_stopped' : 'queued' };
  return null;
}

/** Number of map documents of a row: the back end sends `map.files` as the list of document names. */
export const mapFileCount = (row) => {
  const files = row && row.map && row.map.files;
  return Array.isArray(files) ? files.length : (Number(files) || 0);
};

function MapCell({ row, queue, confirmed, busy, now, actions }) {
  const files = mapFileCount(row);
  const state = mapQueueState(row, queue);
  const job = row.map && row.map.job;
  if (state && state.kind === 'running') {
    return html`<div class="sp-map-run" role="status"><span class="spinner sm" aria-hidden="true"></span>
      <span>${`Mapping… ${elapsedText(job, now)} · ${files} of ${MAP_DOCUMENT_TOTAL}`}</span></div>`;
  }
  const disabled = !confirmed || !row.exists || busy || !row.has_code;
  const title = !confirmed ? NOT_CONFIRMED_HINT : !row.exists ? 'The folder is missing.' : !row.has_code ? 'No code in this folder.' : undefined;
  return html`<div class="sp-map">
    ${files > 0 ? html`<span class="sp-map-n">${`${files} of ${MAP_DOCUMENT_TOTAL} documents`}${row.map.commit ? ` · mapped at ${row.map.commit}` : ''}</span>` : html`<span class="sp-muted">not mapped</span>`}
    ${state && state.kind === 'queued' ? html`<${Chip} text="queued" kind="sp-chip-wait" />` : null}
    ${state && state.kind === 'queued_stopped' ? html`<${Chip} text="queued (stopped)" kind="sp-chip-warn" title="The map queue stopped; map again to resume it." />` : null}
    ${state && state.kind === 'failed' ? html`<span class="sp-bad" role="alert">${'Failed: ' + state.reason}</span>` : null}
    <button type="button" class="btn btn-sm" disabled=${disabled} title=${title} onClick=${() => actions.mapOne(row)}>${files > 0 ? 'Map again' : 'Map codebase'}</button>
  </div>`;
}

function ConformanceCell({ row, confirmed, busy, actions }) {
  const [choice, setChoice] = useState(row.conformance || 'STANDARD');
  useEffect(() => { setChoice(row.conformance || 'STANDARD'); }, [row.conformance, row.path]);
  if (row.conformance) return html`<${Chip} text=${row.conformance} kind=${row.conformance === 'STANDARD' ? 'sp-chip-ok' : 'sp-chip-info'} />`;
  const disabled = !confirmed || !row.exists || busy;
  const title = !confirmed ? NOT_CONFIRMED_HINT : !row.exists ? 'The folder is missing.' : undefined;
  const suggestion = row.suggestion;
  const group = 'sp-conf-' + row.path;
  return html`<div class="sp-conf">
    <div class="sp-conf-radios" role="radiogroup" aria-label=${'Conformance of ' + row.path}>
      ${['STANDARD', 'LEGACY'].map((value) => html`<label key=${value} class="sp-radio" title=${title}>
        <input type="radio" name=${group} value=${value} checked=${choice === value} disabled=${disabled} onChange=${() => setChoice(value)} />${value}</label>`)}
      <button type="button" class="btn btn-sm" disabled=${disabled} title=${title} onClick=${() => actions.setConformance(row, choice)}>Set</button>
    </div>
    ${suggestion ? html`<button type="button" class="btn btn-sm btn-ghost sp-suggest" disabled=${disabled} title=${title || suggestion.evidence || undefined}
      onClick=${() => actions.setConformance(row, suggestion.suggestion)}>${'Use the suggestion: ' + suggestion.suggestion}</button>` : null}
  </div>`;
}

export function Warnings({ row }) {
  const giant = row.giant_files || 0;
  const giantTitle = (row.giant || []).map((g) => `${g.path} (${g.lines} lines)`).join('\n');
  return html`<div class="sp-flags">
    ${giant > 0 ? html`<${Chip} text=${`${giant} giant file${giant === 1 ? '' : 's'}`} kind="sp-chip-warn" title=${giantTitle || 'Giant files: an agent only reads parts of them.'} />` : null}
    ${row.warning ? html`<${Chip} text=${isDamaged(row.warning) ? DAMAGED_TEXT : row.warning} kind="sp-chip-warn" title=${row.warning} />` : null}
    ${row.exists === false ? html`<${Chip} text="folder missing" kind="sp-chip-bad" />` : null}
  </div>`;
}

function Row({ row, confirmed, queue, busy, now, actions }) {
  const missingFolder = row.exists === false;
  const editable = !confirmed;
  return html`<tr class=${'sp-row' + (missingFolder ? ' sp-row-missing' : '') + (row.skip ? ' sp-row-skipped' : '')} data-path=${row.path}>
    <td class="sp-c-name">
      ${editable
        ? html`<input class="sp-input" type="text" value=${row.name} aria-label=${'Name of ' + row.path} onInput=${(e) => actions.rename(row.path, e.currentTarget.value)} />`
        : html`<strong>${row.name}</strong>`}
      <div class="sp-path">${row.path}</div>
      ${row.reason ? html`<div class="sp-muted">${row.reason}</div>` : null}
      <${Warnings} row=${row} />
    </td>
    <td><${Chip} text=${row.stack || 'unknown'} /> ${row.skill ? html`<${Chip} text=${row.skill} kind="sp-chip-info" title="Skill the agents use here" />` : null}</td>
    <td class="sp-c-skip">
      <label class="sp-radio" title=${editable ? 'Skip: leave this folder out of the loop.' : 'The list is confirmed; detect again to change it.'}>
        <input type="checkbox" checked=${!!row.skip} disabled=${!editable} onChange=${(e) => actions.toggleSkip(row.path, e.currentTarget.checked)} />skip</label>
      ${editable ? html`<button type="button" class="btn btn-sm btn-ghost" onClick=${() => actions.removeRow(row.path)} aria-label=${'Remove ' + row.path}>Remove</button>` : null}
    </td>
    <td>${row.skip ? html`<span class="sp-muted">skipped</span>` : html`<${ConformanceCell} row=${row} confirmed=${confirmed} busy=${busy} actions=${actions} />`}</td>
    <td>${row.skip ? html`<span class="sp-muted">skipped</span>` : missingFolder ? html`<span class="sp-muted">folder missing</span>` : html`<${MapCell} row=${row} queue=${queue} confirmed=${confirmed} busy=${busy} now=${now} actions=${actions} />`}</td>
  </tr>`;
}

function SystemRow({ system, confirmed, busy, actions }) {
  if (!system) return null;
  const title = !confirmed ? NOT_CONFIRMED_HINT : system.enabled ? undefined : system.reason || 'Not available yet.';
  return html`<tr class="sp-row sp-row-system" data-path="__system__">
    <td colspan="3"><strong>System map</strong>
      <div class="sp-path">SYSTEM.md: how the subprojects talk to each other</div>
      ${system.exists && system.older_than_list ? html`<div><${Chip} text="older than the subproject list" kind="sp-chip-warn" title="The list changed after the system map was written." /></div>` : null}
      ${!system.enabled && system.reason ? html`<div class="sp-muted">${system.reason}</div>` : null}</td>
    <td colspan="2"><div class="sp-map">
      ${system.exists ? html`<span class="sp-map-n">${'mapped' + (system.mapped_at ? ' at ' + system.mapped_at : '')}</span>` : html`<span class="sp-muted">not written</span>`}
      <button type="button" class="btn btn-sm" disabled=${!confirmed || !system.enabled || busy} title=${title} onClick=${() => actions.systemMap()}>${system.exists ? 'Map the system again' : 'Map the system'}</button>
    </div></td>
  </tr>`;
}

/**
 * <SubprojectsTable data draft busy mapError actions />
 * data: the GET subprojects description; draft: edited projects (unconfirmed list) or null; busy: an action is in flight.
 * actions: validate, confirmList, detectAgain, notMonorepo, useSuggestionForAll, mapAll, mapOne(row), systemMap,
 *          setConformance(row, value), rename(path, name), toggleSkip(path, bool), removeRow(path), addFolder.
 */
export function SubprojectsTable({ data, draft = null, busy = false, mapError = '', actions }) {
  const confirmed = !!(data && data.confirmed);
  const rows = draft || (data && data.projects) || [];
  const queue = mapQueueOf(data);
  const validation = (data && data.validation_job) || null;
  const validating = !!validation && validation.state === 'running';
  const mapping = !!(queue && queue.state === 'running') || rows.some((row) => row.map && row.map.running);
  const now = useNow(validating || mapping);
  if (!data) return html`<div class="sp-muted" role="status">Loading the subprojects…</div>`;

  const mappable = rows.filter((row) => !row.skip && row.exists !== false && row.has_code && !mapFileCount(row));
  const unset = rows.filter((row) => !row.skip && row.exists !== false && !row.conformance && row.suggestion);
  const locked = busy || validating;
  const confirmHint = confirmed ? undefined : NOT_CONFIRMED_HINT;
  const damaged = [data.error, data.validation_error, mapError].some(isDamaged);

  return html`<div class="sp-wrap">
    <div class="sp-top">
      <button type="button" class="btn btn-sm" disabled=${locked || mapping} onClick=${actions.validate}
        title="An AI reviews the detected list (reads folders only).">Validate with AI</button>
      <button type="button" class="btn btn-sm btn-primary" disabled=${locked || confirmed || !rows.length} onClick=${actions.confirmList}
        title=${confirmed ? 'The list is already confirmed.' : undefined}>Confirm list</button>
      <button type="button" class="btn btn-sm" disabled=${locked || mapping} onClick=${actions.detectAgain}
        title=${confirmed ? 'Replaces the confirmed list (asks first).' : undefined}>Detect again</button>
      <button type="button" class="btn btn-sm btn-ghost" disabled=${locked || mapping} onClick=${actions.notMonorepo}>Not a monorepo</button>
      <span class="sp-top-gap"></span>
      <button type="button" class="btn btn-sm" disabled=${!confirmed || locked || !unset.length} title=${confirmHint || (unset.length ? undefined : 'Every subproject already has a Conformance line or has no suggestion.')}
        onClick=${actions.useSuggestionForAll}>Use the suggestion for all</button>
      <button type="button" class="btn btn-sm btn-primary" disabled=${!confirmed || locked || mapping || !mappable.length} title=${confirmHint || (mappable.length ? undefined : 'Nothing is left to map.')}
        onClick=${actions.mapAll}>Map all that are missing</button>
    </div>
    ${validating ? html`<div class="sp-note" role="status"><span class="spinner sm" aria-hidden="true"></span> ${`Validating the list with AI… ${elapsedText(validation, now)}`}</div>` : null}
    ${data.reason && !confirmed ? html`<div class="sp-note">${'Detection: ' + data.reason}</div>` : null}
    ${!confirmed ? html`<div class="sp-note sp-note-info">The list is not confirmed yet. Review it (rename, skip, remove or add folders), then Confirm list. Conformance and maps are available after that.</div>` : null}
    ${data.validation_error ? html`<div class="sp-note sp-note-bad" role="alert">${'The AI validation did not work: ' + data.validation_error}</div>` : null}
    ${data.error ? html`<div class="sp-note sp-note-bad" role="alert">${data.error}</div>` : null}
    ${damaged ? html`<div class="sp-note sp-note-warn" role="alert"><strong>${DAMAGED_TEXT[0].toUpperCase() + DAMAGED_TEXT.slice(1)}.</strong> The block between the maestro:context markers was edited by hand; repair or remove the markers and the block is written again.</div>` : null}
    ${mapError ? html`<div class="sp-note sp-note-bad" role="alert">${mapError}</div>` : null}
    ${(data.rejected || []).length ? html`<div class="sp-rejected">${data.rejected.map((r) => html`<${Chip} key=${r.path} text=${`rejected: ${r.path}`} kind="sp-chip-warn" title=${r.reason} />`)}</div>` : null}
    ${(data.differences || []).length ? html`<div class="sp-diffs"><span class="sp-muted">AI changes:</span>${data.differences.map((d) => html`<${Chip} key=${d.path + d.change} text=${`${d.path}: ${d.change}`} kind="sp-chip-info" />`)}</div>` : null}
    ${(data.notes || []).length ? html`<ul class="sp-notes" aria-label="AI notes">${data.notes.map((n, i) => html`<li key=${i}>${n}</li>`)}</ul>` : null}
    ${data.root_map_leftover ? html`<div class="sp-note sp-note-info">${ROOT_MAP_LEFTOVER_TEXT}</div>` : null}
    <div class="sp-scroll"><table class="sp-table">
      <thead><tr><th>Subproject</th><th>Stack</th><th>Options</th><th>Conformance</th><th>Map</th></tr></thead>
      <tbody>
        ${rows.map((row) => html`<${Row} key=${row.path} row=${row} confirmed=${confirmed} queue=${queue} busy=${locked} now=${now} actions=${actions} />`)}
        ${!rows.length ? html`<tr><td colspan="5" class="sp-muted">No subprojects were found.</td></tr>` : null}
        <${SystemRow} system=${data.system} confirmed=${confirmed} busy=${locked || mapping} actions=${actions} />
      </tbody>
    </table></div>
    ${!confirmed ? html`<div class="sp-add"><button type="button" class="btn btn-sm btn-ghost" onClick=${actions.addFolder} disabled=${locked}>Add a folder…</button></div>` : null}
  </div>`;
}
