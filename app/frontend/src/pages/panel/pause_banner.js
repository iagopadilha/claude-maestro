/* Pause banner of the LOOP zone: one line and ONE action. It shows while a feature is in quarantine or the loop waits for the
   user. The decision (try again or skip) is never taken here: deciding needs context, and the context lives in the pause dialog.
   A loop that waits for an answer without any quarantine keeps the existing answer UI (the Alert band at the top of the page). */
import { html } from '../../util/preact.js';
import { list, firstLine, DASH } from '../../util/format.js';
import { ctx, status, loop } from './context.js';
import { pipe } from './pipeline_model.js';
import { openPauseDialog } from './dialogs/index.js';
import { openStatusDialog } from './alert.js';
import { FeatureLabel } from './feature_label.js';

const REASON_CLIP = 140;
const clip = (text, max) => { const flat = String(text == null ? '' : text).replace(/\s+/g, ' ').trim(); return flat.length > max ? flat.slice(0, max - 1) + '…' : flat; };

/** True when the banner has something to say. */
export function pauseBannerVisible() {
  return !!ctx.state && (list(ctx.state.quarantine).length > 0 || status() === 'awaiting_user');
}

/** Opens the question dialog (the same one the "waiting for you" chip opens); it used to flip a flag nobody read and scroll to an element that does not exist. */
const showAnswer = openStatusDialog;

export function PauseBanner() {
  if (!pauseBannerVisible()) return null;
  const quarantined = list(ctx.state.quarantine), first = quarantined[0], awaiting = status() === 'awaiting_user';
  const paused = awaiting || !!(pipe.data && pipe.data.paused);
  if (!first) {
    return html`<div class="z-pause" role="status">
      <span class="z-pause-kick mono">NEEDS YOU</span>
      <div class="z-pause-text"><span>${clip(firstLine(loop().reason), REASON_CLIP)}</span><span class="z-pause-dim"> · loop paused</span></div>
      <button type="button" class="z-pause-btn" onClick=${showAnswer}>Answer</button></div>`;
  }
  const more = quarantined.length - 1;
  return html`<div class="z-pause" role="status">
    <span class="z-pause-kick mono">NEEDS YOU</span>
    <div class="z-pause-text"><${FeatureLabel} slug=${first.id} className="z-pause-slug" /> in quarantine<span class="z-pause-dim">${' · ' + (clip(first.reason, REASON_CLIP) || DASH)}${paused ? ' · loop paused' : ''}${more > 0 ? ` · +${more} more` : ''}</span></div>
    <button type="button" class="z-pause-btn" onClick=${() => openPauseDialog(first.id)}>Understand and resolve</button>
  </div>`;
}
