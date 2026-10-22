/* History data of the loop (GET history, never in the 2 s polling). The "History" view is gone (the turn strip, the
   "Decision & events" tab and the agent dialog replaced it); what stays is the store ui.hist, which the feature history of
   the inspector and the agent lookup of old turns (context.js) read. */
import { ctx, ui } from './context.js';
import { renderAll } from '../project_panel.js';
import { list } from '../../util/format.js';

export async function loadHist(force) {
  const state = ctx.state;
  const key = String(state && state.meta ? state.meta.turns : null), hs = ui.hist;
  if (hs.loading) return;
  if (!force && hs.key === key) return;
  hs.loading = true; hs.key = key; hs.err = null; if (!hs.data) renderAll();
  try {
    const r = await ctx.api.get('history', 30000);
    hs.data = list(r && r.turns);
  } catch (e) {
    hs.err = e.status === 404 ? 'The server does not offer the history yet.' : e.message;
  } finally { hs.loading = false; hs.ver += 1; renderAll(); }
}
