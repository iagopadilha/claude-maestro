/* Installer of a single repository's setup (presentational): two steps, "1 Conformance · 2 Map", with Back / Next, the same
   look as the monorepo installer (components/subprojects_installer.js: step indicator, rows, footer, CSS).
   The state and the API calls live in pages/panel/project_installer.js; nothing here talks to the server. */
import { html, useState, useEffect } from '../util/preact.js';
import { dur } from '../util/format.js';
import { StepIndicator } from './subprojects_installer.js';
import { StandardsBlock } from './standards_block.js';
import { Chip, useNow, MAP_DOCUMENT_TOTAL } from './subprojects_table.js';

const STEPS = [[1, 'Conformance'], [2, 'Map']];
const OPTIONS = ['STANDARD', 'LEGACY'];
const STEP_TEXT = {
  1: 'Say how the loop should treat this code: STANDARD (it refactors toward the pattern as it goes) or LEGACY (it avoids touching what it should not). The choice is saved in the CLAUDE.md of the project.',
  2: 'The AI reads the code once and writes up to 7 short documents in docs/codebase/ (stack, architecture, conventions...). They are a guide, not the source of truth: the code wins.',
};
const STEP_SAFE = {
  1: 'Writes one Conformance block in the CLAUDE.md of the project. No code is touched.',
  2: 'Writes only docs/codebase. No code is touched.',
};
export const MAP_CONFIRM = 'Runs claude (Sonnet) on this repository. It writes only docs/codebase; it never touches the code. No spend cap.';
const seconds = (from, to) => { const a = Date.parse(from), b = Date.parse(to); return Number.isFinite(a) && Number.isFinite(b) ? Math.max(0, (b - a) / 1000) : null; };
const evidenceOf = (suggestion) => {
  const evidence = suggestion && suggestion.evidence;
  return (Array.isArray(evidence) ? evidence.join('; ') : String(evidence || '')) || 'Suggested from the code';
};

function ConformanceStep({ written, suggestion, choice, setChoice, locked }) {
  const suggested = (suggestion && suggestion.suggestion) || '';
  const value = written || choice || suggested || 'STANDARD';
  return html`<ul class="sp-inst-list" aria-label="Conformance">
    <li class="sp-inst-row sp-inst-conf-row" data-state=${written ? 'saved' : 'open'}>
      <div class="sp-inst-main"><strong class="sp-inst-name">Conformance</strong><div class="sp-path">CLAUDE.md</div></div>
      <div class="sp-inst-chips">${suggestion && suggestion.stack ? html`<${Chip} text=${suggestion.stack} />` : null}</div>
      <div class="sp-conf-radios" role="radiogroup" aria-label="Conformance of the project">
        ${OPTIONS.map((option) => html`<label key=${option} class="sp-radio sp-inst-radio">
          <input type="radio" name="pi-conformance" value=${option} checked=${value === option} disabled=${locked || !!written} onChange=${() => setChoice(option)} />${option}
          ${!written && suggested === option ? html`<span class="sp-inst-tag" title=${evidenceOf(suggestion)}>suggested</span>` : null}</label>`)}
      </div>
      <div class="sp-inst-end">${written ? html`<span class="sp-muted">saved</span>` : null}</div>
    </li>
  </ul>`;
}

/** map = { state: 'idle'|'running'|'done'|'failed', startedAt, finishedAt, documents, error, hasCode } */
function MapStep({ map, now, retry }) {
  let status;
  if (map.state === 'running') status = html`<span class="sp-map-run" role="status"><span class="spinner sm" aria-hidden="true"></span><span>${`Mapping… ${map.startedAt ? dur(seconds(map.startedAt, new Date(now).toISOString()) || 0) : ''} · ${map.documents} of ${MAP_DOCUMENT_TOTAL} documents`}</span></span>`;
  else if (map.state === 'done') {
    const took = map.startedAt && map.finishedAt ? seconds(map.startedAt, map.finishedAt) : null;
    status = html`<span class="sp-inst-ok"><span aria-hidden="true">✓</span> ${`Done · ${map.documents} of ${MAP_DOCUMENT_TOTAL} documents${took != null ? ' · ' + dur(took) : ''}`}</span>`;
  } else if (map.state === 'failed') status = html`<span class="sp-bad" role="alert">${'Failed: ' + (map.error || 'the map failed')}</span>`;
  else status = html`<span class="sp-muted">${map.hasCode ? 'Not mapped yet' : 'No code yet: nothing to map'}</span>`;
  return html`<div class="sp-inst-body"><ul class="sp-inst-list" aria-label="Map">
    <li class="sp-inst-row sp-inst-map-row" id="pi-map-row" data-state=${map.state}>
      <div class="sp-inst-main"><strong class="sp-inst-name">Codebase map — docs/codebase</strong></div>
      <div class="sp-inst-status">${status}</div>
      <div class="sp-inst-end">${map.state === 'failed' ? html`<button type="button" class="btn btn-sm" id="pi-retry" onClick=${retry}>Retry</button>` : null}</div>
    </li>
  </ul></div>`;
}

/**
 * <ProjectInstaller conformance map busy actions onContinue />
 * conformance: { written: 'STANDARD'|'LEGACY'|'', suggestion: {suggestion, stack, evidence}|null }
 * map: see MapStep (+ `needed`: false when the map is optional, e.g. a repository without code)
 * actions: saveConformance(value) (resolves true when saved), startMap() (asks the one-sentence confirmation), retryMap().
 * standards: optional { scope, get(), put(bindings) } of the Standards slots (see standards_block.js); shown under the Conformance, never
 * hidden behind it and never a condition of Next / Done. Left out for a reference repository (it has no Conformance step).
 */
export function ProjectInstaller({ conformance, map, busy = false, actions, onContinue, skipConformance = false, finishLabel = 'Continue to planning', standards = null }) {
  const gateStep = conformance.written || skipConformance ? 2 : 1;
  const [view, setView] = useState(null); // a step the user went back to; null = follow what is written
  const [choice, setChoice] = useState('');
  useEffect(() => { setView(null); }, [gateStep]);
  const mapping = map.state === 'running';
  const now = useNow(mapping);
  const step = view || gateStep;
  const suggested = (conformance.suggestion && conformance.suggestion.suggestion) || '';
  const next = async () => {
    if (conformance.written) { setView(2); return; }
    if (await actions.saveConformance(choice || suggested || 'STANDARD')) setView(2);
  };
  const backBlocked = step === 1 ? 'This is the first step.' : mapping ? 'The map is running.' : map.state === 'done' ? 'The map already ran.' : '';
  const finished = map.state === 'done' || (map.state === 'idle' && map.needed === false);
  return html`<div class="sp-inst" id="pi-installer" data-step=${step}>
    <${StepIndicator} step=${skipConformance ? 1 : step} steps=${skipConformance ? [[1, 'Map']] : STEPS} allDone=${step === STEPS.length && finished} />
    <p class="sp-inst-d">${STEP_TEXT[step]}</p>
    ${step === 1 ? html`<${ConformanceStep} written=${conformance.written} suggestion=${conformance.suggestion} choice=${choice} setChoice=${setChoice} locked=${busy} />
        ${standards && !skipConformance ? html`<${StandardsBlock} api=${standards} scope=${standards.scope} />` : null}`
      : html`<${MapStep} map=${map} now=${now} retry=${actions.retryMap} />`}
    <div class="sp-inst-foot">
      ${skipConformance ? null : html`<button type="button" class="btn" id="sp-inst-back" disabled=${!!backBlocked || busy} title=${backBlocked || undefined} onClick=${() => setView(step - 1)}>Back</button>`}
      <span class="sp-top-gap"></span>
      ${step === 1 ? html`<button type="button" class="btn btn-primary" id="sp-inst-next" disabled=${busy} onClick=${next}>${busy ? 'Saving…' : 'Next'}</button>`
        : finished ? html`<button type="button" class="btn btn-primary" id="sp-inst-continue" onClick=${onContinue}>${finishLabel}</button>`
        : html`<button type="button" class="btn btn-primary" id="sp-inst-map" disabled=${mapping || !map.hasCode || busy} onClick=${actions.startMap}>${mapping ? 'Mapping…' : 'Map the project'}</button>`}
    </div>
    <p class="plan-safe" id="sp-inst-safe">${STEP_SAFE[step]}</p>
  </div>`;
}
