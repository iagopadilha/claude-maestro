/* One way to show a feature everywhere in the panel: the readable name as the main label and the slug small and dim beside it.
   When the name is the slug (or missing) the slug shows once. The full text goes in a tooltip because the label may be clipped. */
import { html } from '../../util/preact.js';
import { featureName } from './pipeline_model.js';

/** {name, slug, same, tip} of a feature slug. */
export function featureParts(slug) {
  const id = String(slug == null ? '' : slug), name = featureName(id), same = name === id;
  return { name, slug: id, same, tip: same ? id : `${name} (${id})` };
}

/** Inline label: <name> <slug>. `stacked` puts the slug under the name; `block` lets both wrap instead of clipping. */
export function FeatureLabel({ slug, stacked = false, block = false, className = '' }) {
  const parts = featureParts(slug);
  const cls = 'fl' + (stacked ? ' is-stacked' : '') + (block ? ' is-block' : '') + (className ? ' ' + className : '');
  return html`<span class=${cls} title=${parts.tip}><span class="fl-name">${parts.name}</span>${parts.same ? null : html`<span class="fl-slug mono">${parts.slug}</span>`}</span>`;
}
