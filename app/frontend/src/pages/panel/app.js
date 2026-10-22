/* Root of the project panel (/p/<id>/): the whole page as one Preact tree. Before the first /state only the frame
   shows (as the old static template did); each region renders its content once ctx.state arrives.
   Two modes in a table (MODES), each with its header and the zones above the content:
   - loop (roadmap approved): the navbar and, right below it, the content (.z-body): the GLOBAL zone (blue aside, valid for the whole
     loop) and the LOOP zone (amber main, depends on the selected turn). No alert band, no New loop card, no Workspace zone: the status
     dialog (from the navbar indicator) and the Start dialog replaced them.
   - planning (state.planning_required): the PlanningScreen takes the place of both zones; the header is the navbar frame with the
     Planning pill on the left and a Workspace button (dialog) instead of the WORKSPACE band.
   The zones stay in the DOM (hidden in planning) because the one-time setup of startPanel is bound to them. */
import { html } from '../../util/preact.js';
import { disconnectedDetail } from '../../api/api_client.js';
import { WhyButton } from '../../components/why_panel.js';
import { ctx, ui } from './context.js';
import { BrandSub, RepoLine, RepoChip, StatusPill, NavbarLoopStatus, Meters, HeaderActions, headerModel } from './header.js';
import { errorCount, openErrors } from './errors.js';
import { idleText, StatusDialog } from './alert.js';
import { PlanningScreen, planningRequired } from './planning.js';
import { GlobalZone, consumeViewRequest } from './global_zone.js';
import { LoopZone } from './loop_zone.js';

/** Content of #closed: "App shut down" (header) or "Project not found" (polling 404). */
function ClosedCard({ c }) {
  if (c.kind === 'shutdown') {
    return html`<div class="closed-card" role="alert"><div class="closed-title">App shut down</div>
      <div class="muted">The server was shut down. You can close this tab. Loops already started keep running.</div></div>`;
  }
  return html`<div class="closed-card" role="alert">
    <div class="closed-title">Project not found</div>
    <div class="muted">${c.message || 'This address does not match a project registered in the app (it may have been forgotten).'}</div>
    <a class="btn btn-primary" href="/">Back to the projects</a>
  </div>`;
}

function Brand({ has, withRepoLine }) {
  const hd = headerModel();
  return html`<div class="brand">
    <a class="logo logo-link" href="/" title="back to the projects" aria-label="back to the projects">M</a>
    <div>
      <div class="brand-name"><a href="/" class="brand-link">Claude Maestro</a></div>
      <div class="brand-sub mono" id="brand-sub" title=${has ? hd.repo : null}>${has ? html`<${BrandSub} />` : '—'}</div>
      ${has && withRepoLine ? html`<${RepoLine} />` : null}
    </div>
  </div>`;
}
function GuardBadge() {
  const blocks = errorCount();
  return html`<button type="button" class="guard-badge mono" id="guard-badge" hidden=${!blocks} title="open the list of errors and warnings" onClick=${openErrors}>
    <span class="gb-dot"></span>${blocks ? `${blocks} problem${blocks === 1 ? '' : 's'}` : ''}</button>`;
}
function HelpActions() { return html`<div class="header-actions" id="header-extra"><${WhyButton} /></div>`; }

/** Loop mode header: the navbar (NAV-01..04). */
function Navbar({ has }) {
  return html`<header id="header" class="header navbar">
    <${Brand} has=${has} withRepoLine=${false} />
    ${has ? html`<${RepoChip} />` : null}
    ${has
      ? html`<${NavbarLoopStatus} />`
      : html`<div class="nb-status nb-tone-muted" id="navbar-status" role="status"><span class="dot"></span><span class="mono nb-status-text">connecting…</span></div>`}
    <div class="limits nb-meters" id="limits" aria-label="Cost and progress">${has ? html`<${Meters} />` : null}</div>
    <${GuardBadge} />
    <div class="spacer"></div>
    <div class="header-actions" id="header-actions">${has ? html`<${HeaderActions} mode="loop" />` : null}</div>
    <${HelpActions} />
  </header>`;
}
/** Planning mode header (WS-01): the same navbar frame as the loop mode, with the "Planning" pill on the LEFT right after the project block
    and the repo chip, never centered; the Workspace button (next to Shut down app) opens the Workspace dialog. */
function PlanningHeader({ has }) {
  const hd = headerModel();
  return html`<header id="header" class="header navbar nb-planning">
    <${Brand} has=${has} withRepoLine=${false} />
    ${has ? html`<${RepoChip} />` : null}
    <div class="pill nb-pill" id="status-pill" role="status" aria-live="polite" style=${has ? hd.pillStyle : null}>
      ${has ? html`<${StatusPill} />` : html`<span class="dot" id="status-dot"></span><span class="mono" id="status-text">connecting…</span>`}
    </div>
    <div class="limits" id="limits" aria-label="Loop limits"></div>
    <${GuardBadge} />
    <div class="spacer"></div>
    <div class="header-actions" id="header-actions">${has ? html`<${HeaderActions} mode="planning" />` : null}</div>
    <${HelpActions} />
  </header>`;
}
/** Loop mode: no zones above the content; only the status dialog (mounted here, opens from the navbar). */
function LoopTop({ has }) { return has ? html`<${StatusDialog} />` : null; }
/** Planning mode: only the PlanningScreen. The WORKSPACE band and the "Writing the plan in <path>" line are gone (WS-02): the
    Workspace dialog (navbar button) shows that content. The zone element stays, empty and hidden, for the one-time panel setup. */
function PlanningTop({ has }) {
  return html`<div id="chrome-zones" class="chrome-zones is-open" hidden>
      <div id="workspace-zone" class="workspace-zone" hidden></div>
    </div>
    <section id="planning" class="planning-zone" aria-label="Planning"><${PlanningScreen} /></section>`;
}

const MODES = {
  loop: { Header: Navbar, Top: LoopTop },
  planning: { Header: PlanningHeader, Top: PlanningTop },
};

export function Panel() {
  consumeViewRequest(); // old entry points of the removed center views (openProjectFile, openErrors...) open a tab of the global zone
  const has = !!ctx.state, planning = has && planningRequired(), on = has && !planning; // on = the loop panel shows
  const { Header, Top } = MODES[planning ? 'planning' : 'loop'];
  return html`<div id="app" data-mode=${planning ? 'planning' : null}>
    <div id="banner-offline" class="banner banner-offline" role="alert" hidden=${!ctx.offline}>
      <span class="dot dot-red"></span>
      <strong>app disconnected</strong>
      <span id="banner-offline-detail">${ctx.offline ? disconnectedDetail(ctx.offlineMsg) : ''}</span>
    </div>

    <${Header} has=${has} />
    <${Top} has=${has} />

    <div class="z-body" hidden=${planning}>
      <${GlobalZone} on=${on} />
      <${LoopZone} on=${on} planning=${planning} />
    </div>

    <div id="idle-note" class="idle-note mono">${on ? idleText() : ''}</div>
    <div id="closed" class="closed" hidden=${!ui.closed}>${ui.closed ? html`<${ClosedCard} c=${ui.closed} />` : null}</div>
  </div>`;
}
