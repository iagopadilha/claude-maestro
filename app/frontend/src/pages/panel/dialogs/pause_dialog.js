/* Pause dialog: a feature in quarantine stopped the loop and waits for the user. Reading order = summary -> facts -> attempts ->
   evidence -> impact -> decision (guidance + the two buttons that act on the loop).
   What is AI and what is deterministic (important to trust the screen):
     AI (read-only claude -p, GET/POST quarantine/<slug>/summary): the SUMMARY box on top and the answers of the chat column.
     DETERMINISTIC (event record, verifier output, git status, dependency graph): facts, attempts, failed tests with
     expected x got, uncommitted files and the "if skipped" impact. The AI only summarizes them, it never produces them. */
import { html, useState, useEffect, useRef } from '../../../util/preact.js';
import { followJob } from '../../../api/api_client.js';
import { list, txt, fmtDay } from '../../../util/format.js';
import { toast } from '../../../components/toast.js';
import { confirmDialog } from '../../../components/dialog.js';
import { openBigDialog } from '../../../components/big_dialog.js';
import { ctx } from '../context.js';
import { schedulePoll } from '../../project_panel.js';
import { FeatureLabel } from '../feature_label.js';
import { featureName } from '../pipeline_model.js';

const QUARANTINE_FAILURE_LIMIT = 2; // the engine quarantines a feature at its second failure
const SUMMARY_POLL_INTERVAL_MS = 1500;
const SUMMARY_GIVE_UP_MS = 240000;
const BADGE_COLOR = '#f5b82e';
const FAILURE_COLOR = '#f85149';
const ATTEMPT_COLOR = { DONE: '#3fb950', RUNNING: '#f5b82e', QUEUED: '#7fb2ff', FAIL: FAILURE_COLOR, BLOCKED: FAILURE_COLOR };

const GREETING = 'I read the recorded facts of this pause. Ask why the feature failed, what each option risks, or ask me to draft the guidance for the new attempt.';
const SUGGESTIONS = ['Why did this feature fail?', 'What is the risk of trying again?', 'Write the guidance for the new attempt'];

function SummaryBox({ detail, summaryState, onRegenerate }) {
  const summary = (detail && detail.summary) || {};
  const isAi = summary.source === 'ai';
  const tag = summaryState.busy ? 'writing…' : isAi ? '✦ AI-generated' + (summary.at ? ' · ' + fmtDay(summary.at) : '') : 'from the recorded facts';
  return html`<div class="bd-summary">
    <div class="bd-summary-head">
      <span class="bd-label bd-label-blue">SUMMARY</span>
      <span class="bd-tag">${tag}</span>
      <span class="bd-spacer"></span>
      <button type="button" class="bd-link" disabled=${summaryState.busy} onClick=${onRegenerate}>↻ regenerate</button>
    </div>
    <div class="bd-summary-text">${summary.text ? summary.text : summaryState.busy ? 'Writing the summary…' : 'No summary yet.'}</div>
    ${summaryState.error ? html`<div class="bd-inline-error" role="alert">${'Could not write the summary: ' + summaryState.error}</div>` : null}
  </div>`;
}

function PauseBody({ slug, bridge, onFinished }) {
  const route = 'quarantine/' + encodeURIComponent(slug);
  const [detail, setDetail] = useState(null);
  const [loadError, setLoadError] = useState(null);
  const [summaryState, setSummaryState] = useState({ busy: false, error: null });
  const [guidance, setGuidance] = useState('');
  const [busyAction, setBusyAction] = useState(null);
  const isMounted = useRef(true), isSummaryBusy = useRef(false), autoSummaryRequested = useRef(false);

  async function loadDetail() {
    try {
      const loaded = await ctx.api.get(route, 20000);
      if (isMounted.current) { setDetail(loaded); setLoadError(null); }
      return loaded;
    } catch (error) {
      if (isMounted.current) setLoadError(error.status === 404 ? 'This feature is no longer blocked: there is nothing to resolve here.' : error.message);
      return null;
    }
  }

  async function generateSummary() {
    if (isSummaryBusy.current) return;
    isSummaryBusy.current = true;
    if (isMounted.current) setSummaryState({ busy: true, error: null });
    let failure = null;
    try {
      const sent = await ctx.api.post(route + '/summary', {});
      const finished = await followJob(ctx.api, 'chat/job/' + encodeURIComponent(sent.job), { intervalMs: SUMMARY_POLL_INTERVAL_MS, giveUpMs: SUMMARY_GIVE_UP_MS }).promise;
      if (finished.state === 'error') throw new Error(finished.error || 'the AI could not write the summary');
      await loadDetail();
    } catch (error) {
      failure = error.message;
    } finally {
      isSummaryBusy.current = false;
      if (isMounted.current) setSummaryState({ busy: false, error: failure });
    }
  }

  useEffect(() => {
    isMounted.current = true;
    bridge.setGuidance = setGuidance; // the chat column fills the textarea through this ("↳ use as guidance")
    (async () => {
      const loaded = await loadDetail();
      // no AI summary cached yet: ask for one once, as soon as the dialog opens
      if (loaded && !(loaded.summary && loaded.summary.source === 'ai') && !autoSummaryRequested.current) { autoSummaryRequested.current = true; generateSummary(); }
    })();
    return () => { isMounted.current = false; bridge.setGuidance = null; };
  }, []);

  /** An action that changes the loop: toast, close the dialog and ask for a fresh /state. */
  async function runLoopAction(name, request, doneMessage) {
    if (busyAction) return;
    setBusyAction(name);
    try {
      await request();
      toast(doneMessage, 'ok');
      onFinished();
      schedulePoll(0);
    } catch (error) {
      toast('Could not complete the action: ' + error.message, 'err');
      if (isMounted.current) setBusyAction(null);
    }
  }
  const retryWithGuidance = () => runLoopAction('retry', () => ctx.api.post(route + '/retry', { guidance: guidance.trim() }), 'Trying ' + slug + ' again with your guidance.');
  const skipFeature = () => runLoopAction('skip', () => ctx.api.post(route + '/skip', {}), slug + ' skipped; the loop carries on.');

  async function revertChanges() {
    const paths = list(detail && detail.uncommitted);
    if (!paths.length || busyAction) return;
    const confirmed = await confirmDialog({
      title: 'Revert the uncommitted changes?',
      text: `This restores the ${paths.length} listed file${paths.length === 1 ? '' : 's'} to the last commit and deletes the ones that were never committed. It cannot be undone.`,
      yesText: 'Revert changes', danger: true,
      extraContent: html`<ul class="bd-path-list">${paths.map((path) => html`<li key=${path}>${path}</li>`)}</ul>`,
    });
    if (!confirmed) return;
    setBusyAction('revert');
    try {
      await ctx.api.post(route + '/revert', { paths });
      toast('Uncommitted changes reverted.', 'ok');
      await loadDetail();
    } catch (error) {
      toast('Could not revert: ' + error.message, 'err');
    } finally {
      if (isMounted.current) setBusyAction(null);
    }
  }

  if (loadError) return html`<div class="bd-empty" role="alert">${loadError}</div>`;
  if (!detail) return html`<div class="bd-empty">Loading the pause…</div>`;

  const attempts = list(detail.attempts), failedTests = list(detail.failed_tests), uncommitted = list(detail.uncommitted), skipImpact = list(detail.skip_impact);
  const failures = Number.isFinite(detail.failures) ? detail.failures : 0;
  const isBusy = !!busyAction;
  return html`<div class="bd-pause">
    <${SummaryBox} detail=${detail} summaryState=${summaryState} onRegenerate=${generateSummary} />

    <div class="bd-facts-row">
      <span><span class="bd-dim">feature </span><${FeatureLabel} slug=${detail.id || slug} block=${true} /></span>
      <span><span class="bd-dim">step </span>${txt(detail.step_agent != null ? detail.step_agent : detail.step)}</span>
      <span><span class="bd-dim">failures </span><span style=${{ color: FAILURE_COLOR }}>${failures + '/' + Math.max(QUARANTINE_FAILURE_LIMIT, failures)}</span></span>
      <span><span class="bd-dim">turn </span>${detail.turn != null ? 'T' + detail.turn : '—'}</span>
    </div>
    ${detail.reason ? html`<div class="bd-reason">${detail.reason}</div>` : null}

    <div class="bd-section-label">ATTEMPT HISTORY</div>
    <div class="bd-attempts">${attempts.length ? attempts.map((attempt, index) => html`<div key=${index} class="bd-attempt">
        <span class="bd-turn">${'T' + attempt.turn}</span><span class="bd-mono">${txt(attempt.agent)}</span>
        <span class="bd-mono" style=${{ color: ATTEMPT_COLOR[attempt.status] || '#8b929c' }}>${txt(attempt.status)}</span>
        <span class="bd-attempt-out">${txt(attempt.out)}</span></div>`)
      : html`<div class="bd-dim-note">No recorded attempt.</div>`}</div>

    ${failedTests.length ? html`<div class="bd-section-label">FAILED TESTS · EXPECTED × GOT</div>
      <div class="bd-tests">${failedTests.map((failed, index) => html`<div key=${index} class="bd-test">
        <div class="bd-test-head"><span class="bd-test-name">${txt(failed.name)}</span>${failed.why ? html`<span class="bd-test-why">${failed.why}</span>` : null}</div>
        ${failed.expected != null || failed.got != null ? html`<div class="bd-test-pills">
          <span class="bd-pill bd-pill-expected">${'expected: ' + txt(failed.expected)}</span>
          <span class="bd-pill bd-pill-got">${'got: ' + txt(failed.got)}</span></div>` : null}
      </div>`)}</div>` : null}

    <div class="bd-pair">
      <div class="bd-box">
        <div class="bd-box-head"><span class="bd-label bd-box-title">UNCOMMITTED CODE</span>
          ${uncommitted.length ? html`<button type="button" class="bd-button bd-button-danger bd-button-small" disabled=${isBusy} onClick=${revertChanges}>Revert changes</button>` : null}</div>
        ${uncommitted.length ? uncommitted.map((path) => html`<div key=${path} class="bd-path">${path}</div>`) : html`<div class="bd-dim-note">Nothing uncommitted in the working folder.</div>`}
      </div>
      <div class="bd-box bd-box-danger">
        <div class="bd-label bd-label-red">IF SKIPPED, ALSO BLOCKS</div>
        <div class="bd-impact">${skipImpact.length ? skipImpact.map(featureName).join(' → ') : 'Nothing else depends on it.'}</div>
        ${skipImpact.length ? html`<div class="bd-dim-note">depend on it in the graph</div>` : null}
      </div>
    </div>

    <div class="bd-section-label">GUIDANCE FOR THE NEW ATTEMPT</div>
    <textarea class="bd-textarea" value=${guidance} placeholder="Write here, or use an answer of the chat →"
      onInput=${(event) => setGuidance(event.currentTarget.value)}></textarea>
    <div class="bd-actions">
      <button type="button" class="bd-button bd-button-primary" disabled=${isBusy} onClick=${retryWithGuidance}>↻ Try again with this guidance</button>
      <button type="button" class="bd-button" disabled=${isBusy} onClick=${skipFeature}>Skip and continue</button>
    </div>
  </div>`;
}

/** Opens the pause dialog of one quarantined feature (GET quarantine/<slug>). */
export function openPauseDialog(slug) {
  const bridge = { setGuidance: null };
  let dialog = null;
  dialog = openBigDialog({
    badge: 'NEEDS YOU', badgeColor: BADGE_COLOR, title: html`<${FeatureLabel} slug=${slug} /> in quarantine`,
    body: () => html`<${PauseBody} slug=${slug} bridge=${bridge} onFinished=${() => dialog.close()} />`,
    chat: {
      title: 'Resolve with the orchestrator', placeholder: 'Describe how you want the loop to resolve it…',
      scope: { type: 'quarantine', feature: slug }, greeting: GREETING, suggestions: SUGGESTIONS,
      onUse: (text) => { if (bridge.setGuidance) bridge.setGuidance(text); },
    },
  });
  return dialog;
}
