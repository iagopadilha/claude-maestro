/* Root of the project panel (/p/<id>/): the whole page as one Preact tree. Before the first /state only the frame
   shows (as the old static template did); each region renders its content once ctx.state arrives. Container flags
   (hidden parts, classes, colors, titles) come from the models of the owner modules.
   Planning mode (state.planning_required): the PlanningScreen takes the place of the loop panel; header and trail
   stay. The loop regions stay in the DOM (hidden and empty) because the one-time setup of startPanel is bound to them. */
import { html } from '../../util/preact.js';
import { disconnectedDetail } from '../../api/api_client.js';
import { WhyButton } from '../../components/why_panel.js';
import { ctx, ui } from './context.js';
import { BrandSub, RepoLine, StatusPill, Limits, HeaderActions, headerModel } from './header.js';
import { GuardBadge, totalBlocks, openGuardFeed } from './guard_feed.js';
import { ProgressTrail } from './progress_trail.js';
import { Alert, idleText, ChromeBar, chromeOpen } from './alert.js';
import { NewLoop, nlZone } from './new_loop.js';
import { RoadmapSidebar } from './roadmap_sidebar.js';
import { CenterTabs, Caption, PastBar, Legend, CenterCanvas, centerModel, zoomCenter, fitCenter } from './center.js';
import { InspectorHead, InspectorTabs, InspectorBody, inspectorModel } from './inspector.js';
import { ComposerBand, ComposerSeg, FeatureOptions, ComposerTray, ComposerStatus, composerView, composerSubmit, composerKeyDown, pickFeature, proposeFromComposer } from './chat.js';
import { ActPanel } from './act.js';
import { PlanningScreen, planningRequired } from './planning.js';
import { Timeline } from './timeline.js';
import { WorkspaceZone } from './workspace.js';

const SEP_TITLE = 'drag, use the arrows or double-click to restore';

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

export function Panel() {
  const has = !!ctx.state, planning = has && planningRequired(), on = has && !planning; // on = the loop panel shows
  const im = on ? inspectorModel() : null; // first: it drops a selection that no longer exists
  const hd = headerModel(), blocks = totalBlocks(), nz = planning ? { show: false, built: false } : nlZone();
  const cm = on ? centerModel() : null, cv = on ? composerView() : null;
  const canvasKind = cm ? cm.kind : null;
  return html`<div id="app" data-mode=${planning ? 'planning' : null}>
    <div id="banner-offline" class="banner banner-offline" role="alert" hidden=${!ctx.offline}>
      <span class="dot dot-red"></span>
      <strong>app disconnected</strong>
      <span id="banner-offline-detail">${ctx.offline ? disconnectedDetail(ctx.offlineMsg) : ''}</span>
    </div>

    <header id="header" class="header">
      <div class="brand">
        <a class="logo logo-link" href="/" title="back to the projects" aria-label="back to the projects">M</a>
        <div>
          <div class="brand-name"><a href="/" class="brand-link">Claude Maestro</a></div>
          <div class="brand-sub mono" id="brand-sub" title=${has ? hd.repo : null}>${has ? html`<${BrandSub} />` : '—'}</div>
          ${has ? html`<${RepoLine} />` : null}
        </div>
      </div>
      <div class="pill" id="status-pill" role="status" aria-live="polite" style=${has ? hd.pillStyle : null}>
        ${has ? html`<${StatusPill} />` : html`<span class="dot" id="status-dot"></span><span class="mono" id="status-text">connecting…</span>`}
      </div>
      <div class="limits" id="limits" aria-label="Loop limits">${on ? html`<${Limits} />` : null}</div>
      <button type="button" class="guard-badge mono" id="guard-badge" hidden=${!blocks} aria-describedby=${blocks ? 'guard-tip' : null}
        onClick=${openGuardFeed}>${blocks ? html`<${GuardBadge} total=${blocks} />` : null}</button>
      <div class="spacer"></div>
      <div class="header-actions" id="header-actions">${has ? html`<${HeaderActions} />` : null}</div>
      <div class="header-actions" id="header-extra"><${WhyButton} /></div>
    </header>

    <div id="trail" class="trail" aria-label="Where this project stands" hidden=${planning}>${planning ? null : html`<${ProgressTrail} />`}</div>
    <div id="chrome-zones" class=${'chrome-zones' + (planning || chromeOpen() ? ' is-open' : '')} hidden=${!has}>
      ${has && !planning ? html`<${ChromeBar} nlShown=${nz.show} />` : null}
      <div id="alert-zone" class="alert-zone">${on ? html`<${Alert} />` : null}</div>
      <div id="newloop-zone" class="newloop-zone" hidden=${!nz.show}>${nz.built ? html`<${NewLoop} />` : null}</div>

      <div id="workspace-zone" class="workspace-zone" hidden=${!has}>${has ? html`<${WorkspaceZone} />` : null}</div>
    </div>

    ${planning ? html`<section id="planning" class="planning-zone" aria-label="Planning"><${PlanningScreen} /></section>` : null}

    <main class="main" hidden=${planning}>
      <aside class="left" id="left" aria-label="Roadmap">${on ? html`<${RoadmapSidebar} />` : null}</aside>
      <div class="sep sep-v" id="sep-left" role="separator" aria-orientation="vertical" tabindex="0" aria-label="Roadmap width" title=${SEP_TITLE}></div>

      <section class="center" id="center" aria-label="Loop view">
        <div class="center-bar">
          <button type="button" class="btn btn-sm left-toggle" id="left-toggle" aria-expanded="false" aria-controls="left">Roadmap</button>
          <div class="seg" role="tablist" aria-label="View" id="center-tabs"><${CenterTabs} view=${ui.view} /></div>
          <div class="caption" id="caption">${on ? html`<${Caption} view=${ui.view} />` : null}</div>
          <div class="zoom-ctl" id="zoom-ctl" role="group" aria-label="Diagram zoom" hidden=${cm ? !cm.zoom : false}>
            <button type="button" class="btn btn-sm btn-ghost" id="zoom-out" title="zoom out (Ctrl + scroll)" aria-label="zoom out" onClick=${() => zoomCenter(1 / 1.2)}>−</button>
            <span class="zoom-val mono" id="zoom-val">100%</span>
            <button type="button" class="btn btn-sm btn-ghost" id="zoom-in" title="zoom in (Ctrl + scroll)" aria-label="zoom in" onClick=${() => zoomCenter(1.2)}>+</button>
            <button type="button" class="btn btn-sm btn-ghost" id="zoom-fit" title="fits and centers the diagram and puts dragged nodes back" onClick=${fitCenter}>Center</button>
          </div>
        </div>
        <div id="past-bar" class="past-bar" hidden=${!(cm && cm.past)}>${cm && cm.past ? html`<${PastBar} />` : null}</div>
        <div class=${'canvas-wrap' + (canvasKind === 'dg' ? ' dgmode' : canvasKind === 'full' ? ' fullmode' : '')}>
          <div class=${'canvas' + (canvasKind ? ' ' + canvasKind : '')} id="canvas">${cm ? html`<${CenterCanvas} cm=${cm} />` : null}</div>
        </div>
        <div class="legend" id="legend" hidden=${cm ? !cm.legend : false}>${on ? html`<${Legend} view=${ui.view} />` : null}</div>
      </section>

      <div class="sep sep-v" id="sep-right" role="separator" aria-orientation="vertical" tabindex="0" aria-label="Inspector width" title=${SEP_TITLE}></div>
      <aside class="right" aria-label="Inspector">
        <div class="insp-head" id="insp-head">${im ? html`<${InspectorHead} m=${im} />` : null}</div>
        <div class="insp-tabs" id="insp-tabs" role="tablist" aria-label="Inspector">${im ? html`<${InspectorTabs} m=${im} />` : null}</div>
        <div class=${'insp-body' + (im && im.chat ? ' is-chat' : '')} id="insp-body">${im ? html`<${InspectorBody} m=${im} />` : null}</div>
        <form class=${cv ? cv.cls : 'composer m-instruct'} id="composer" aria-label="Composer: instruct the loop or chat" onSubmit=${composerSubmit}>
          <div class="cp-band" id="cp-band">${cv ? html`<${ComposerBand} m=${cv.m} />` : null}</div>
          <div class="cp-seg" id="cp-seg" role="tablist" aria-label="Intent" hidden=${cv ? !cv.m.act : false}>${cv ? html`<${ComposerSeg} m=${cv.m} />` : null}</div>
          <div class="cp-why" id="cp-why" hidden></div>
          <div class="cp-act" id="cp-act" hidden=${!(cv && cv.showActions)}>${cv && cv.showActions ? html`<${ActPanel} onPropose=${proposeFromComposer} />` : null}</div>
          <label class="cp-feat" id="cp-feat" for="cp-feature" hidden=${cv ? !cv.on('instruct') : false}><span>about the feature:</span><select id="cp-feature"
            value=${cv ? cv.featValue : ''} onChange=${pickFeature}>${cv ? html`<${FeatureOptions} fs=${cv.m.fs} />` : null}</select></label>
          <div class="cp-tray" id="cp-tray" hidden=${!(cv && cv.showTray)}>${cv && cv.showTray ? html`<${ComposerTray} />` : null}</div>
          <div class="cp-box">
            <textarea id="cp-text" rows="2" aria-label="Message" placeholder=${cv ? cv.placeholder : null} onKeyDown=${composerKeyDown}></textarea>
            <button type="submit" class="btn btn-primary" id="cp-send" disabled=${!!(cv && cv.sendDisabled)}>${cv ? cv.sendText : 'Instruct the loop'}</button>
          </div>
          <div class="cp-hint mono" id="cp-hint">${cv ? cv.hint : ''}</div>
          <div class=${'cp-status' + (cv && cv.status ? ' ' + cv.status.kind : '')} id="cp-status" role="status" hidden=${!(cv && cv.status)}>${cv && cv.status ? html`<${ComposerStatus} s=${cv.status} />` : null}</div>
        </form>
      </aside>
    </main>

    <div class="sep sep-h" id="sep-foot" hidden=${planning} role="separator" aria-orientation="horizontal" tabindex="0" aria-label="Timeline height" title=${SEP_TITLE}></div>
    <footer class="footer" id="footer" hidden=${planning}>
      <div class="footer-title">
        <div class="strong">Turns</div>
        <div class="mono dim-sm">● = latest turn (live)</div>
      </div>
      <div class="timeline" id="timeline" role="group" aria-label="Loop turns">${on ? html`<${Timeline} />` : null}</div>
    </footer>

    <div id="idle-note" class="idle-note mono">${on ? idleText() : ''}</div>
    <div id="closed" class="closed" hidden=${!ui.closed}>${ui.closed ? html`<${ClosedCard} c=${ui.closed} />` : null}</div>
  </div>`;
}
