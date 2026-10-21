/* Presentational pieces of the panel navbar: the status indicator (one table of texts for every loop status) and the compact meters.
   No fetch and no panel state here: the page (pages/panel/header.js) decides the status key, the detail and what a click does. */
import { html } from '../util/preact.js';
import { DASH, isNum, num } from '../util/format.js';

/** Percent of the cap from which a meter takes the attention color (cost meter, COST-04). */
export const METER_ATTENTION_PCT = 80;

/** The only table of indicator texts. tone picks the colors (css .nb-tone-*); attention = the user has something to do. */
export const NAVBAR_STATUS = {
  no_loop: { label: 'no loop', tone: 'muted', attention: false, tip: 'Nothing is running. Start a loop when ready.' },
  running: { label: 'running', tone: 'amber', attention: false, pulse: true, tip: 'The loop is running. You can pause it after the current turn.' },
  paused: { label: 'paused', tone: 'amber', attention: true, tip: 'The loop is paused and can continue. Click to see why and what to do.' },
  awaiting_user: { label: 'waiting for you', tone: 'amber', attention: true, pulse: true, tip: 'The loop has a question for you. Click to answer it.' },
  stopped: { label: 'stopped', tone: 'muted', attention: true, tip: 'The loop process is no longer running, but there was still work to do. Click to resume it.' },
  error: { label: 'error', tone: 'red', attention: true, tip: 'The loop stopped with an error. Click to see it and try again.' },
  done: { label: 'done', tone: 'green', attention: false, tip: 'The loop finished. Click for the summary, or use Finish to wrap up.' },
};

/** Loop status (as in /state) -> key of NAVBAR_STATUS. `continue` is a loop that goes on, shown as running. */
export function navbarStatusKey(loopStatus) {
  if (loopStatus === 'continue') return 'running';
  return NAVBAR_STATUS[loopStatus] ? loopStatus : 'no_loop';
}

/** Status indicator. statusKey: key of NAVBAR_STATUS; detail: extra sentence (the reason) appended to the tooltip;
    turn: current turn or null; tip: replaces the table sentence; onClick: given means the indicator is a button. */
export function NavbarStatus({ statusKey, turn, detail, tip, onClick }) {
  const st = NAVBAR_STATUS[statusKey] || NAVBAR_STATUS.no_loop;
  const title = [tip || st.tip, detail].filter(Boolean).join(' ');
  const cls = `nb-status nb-tone-${st.tone}${st.attention ? ' is-attention' : ''}${onClick ? ' is-click' : ''}`;
  const inner = html`<span class=${'dot' + (st.pulse ? ' pulse' : '')} aria-hidden="true"></span>
    <span class="mono nb-status-text">${st.label + (isNum(turn) ? ` · turn ${turn}` : '')}</span>`;
  return onClick
    ? html`<button type="button" class=${cls} id="navbar-status" title=${title} aria-label=${`Loop status: ${st.label}. ${title}`} onClick=${onClick}>${inner}</button>`
    : html`<div class=${cls} id="navbar-status" role="status" aria-live="polite" title=${title}>${inner}</div>`;
}

/** Compact meter: label, value / max and a thin bar. unit '$' puts the sign before the numbers; attention colors it. */
export function NavbarMeter({ label, value, max, unit = '', attention = false, danger = false, title }) {
  const pct = isNum(value) && isNum(max) && max > 0 ? Math.max(0, Math.min(100, (value / max) * 100)) : 0;
  const show = (v) => (isNum(v) ? unit + num(v) : DASH);
  return html`<div class=${'nb-meter' + (danger ? ' is-danger' : attention ? ' is-attention' : '')} title=${title || null}>
    <div class="nb-meter-label">${label}</div>
    <div class="nb-meter-val mono"><span>${show(value)}</span><span class="of">${' / ' + show(max)}</span></div>
    <div class="nb-meter-bar"><div style=${{ width: pct + '%' }}></div></div>
  </div>`;
}
