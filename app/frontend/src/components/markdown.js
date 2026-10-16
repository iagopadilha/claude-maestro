/* Small, safe markdown renderer: builds Preact vnodes from the parsed tokens (text goes in as text children),
   never raw HTML strings. Covers headings, paragraphs, lists (with [ ] / [x]), tables, block and inline code, quotes,
   horizontal rules, bold, italic and http(s) links. Any HTML in the text shows up as text. */
import { html, Fragment, useMemo } from '../util/preact.js';

// underscore only counts outside a word: names like loop_state and pause_on_blocked stay intact
const RE_INLINE = /(`[^`\n]+`|\*\*[^*\n]+\*\*|(?<![\w])__[^_\n]+__(?![\w])|\*[^*\s][^*\n]*\*|(?<![\w])_[^_\s][^_\n]*_(?![\w])|\[[^\]\n]+\]\([^)\s]+\)|https?:\/\/[^\s<>()]+)/g;

function safeLink(href) {
  return /^https?:\/\//i.test(href) ? href : null;
}

/** Inline: returns a list of vnodes/strings. */
export function inlineNodes(text) {
  const out = [];
  const s = String(text == null ? '' : text);
  let last = 0, m;
  const re = new RegExp(RE_INLINE.source, 'g'); // one per call: the function is recursive (bold/italic/link)
  while ((m = re.exec(s))) {
    if (m.index > last) out.push(s.slice(last, m.index));
    const tk = m[0];
    if (tk[0] === '`') out.push(html`<code class="md-code">${tk.slice(1, -1)}</code>`);
    else if (tk.startsWith('**') || tk.startsWith('__')) out.push(html`<strong>${inlineNodes(tk.slice(2, -2))}</strong>`);
    else if (tk[0] === '*' || tk[0] === '_') out.push(html`<em>${inlineNodes(tk.slice(1, -1))}</em>`);
    else if (tk[0] === '[') {
      const mm = /^\[([^\]]+)\]\(([^)\s]+)\)$/.exec(tk);
      const href = mm && safeLink(mm[2]);
      out.push(href ? html`<a href=${href} target="_blank" rel="noopener noreferrer">${inlineNodes(mm[1])}</a>` : html`<span>${inlineNodes(mm ? mm[1] : tk)}</span>`);
    } else {
      const href = safeLink(tk);
      out.push(href ? html`<a href=${href} target="_blank" rel="noopener noreferrer">${tk}</a>` : tk);
    }
    last = m.index + tk.length;
  }
  if (last < s.length) out.push(s.slice(last));
  return out;
}

const isUl = (s) => /^\s*[-*+]\s+/.test(s);
const isOl = (s) => /^\s*\d+[.)]\s+/.test(s);
const isFence = (s) => /^\s*(```|~~~)/.test(s);
const isHeading = (s) => /^#{1,6}\s+/.test(s);
const isHr = (s) => /^\s*([-*_])(\s*\1){2,}\s*$/.test(s);
const isQuote = (s) => /^\s*>/.test(s);
const isTableRow = (s) => /^\s*\|.*\|\s*$/.test(s);
const isTableSep = (s) => /^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$/.test(s) && s.includes('-');

function cells(line) {
  return line.trim().replace(/^\|/, '').replace(/\|$/, '').split(/(?<!\\)\|/).map((c) => c.trim().replace(/\\\|/g, '|'));
}

function listItem(text, sub) {
  const m = /^\[( |x|X)\]\s+(.*)$/.exec(text);
  if (!m) return html`<li>${inlineNodes(text)}${sub}</li>`;
  const checked = m[1].toLowerCase() === 'x';
  return html`<li class=${'md-task' + (checked ? ' done' : '')}>
    <span class="md-box" aria-hidden="true">${checked ? '✓' : ''}</span><span class="sr-only">${checked ? 'done: ' : 'pending: '}</span><span>${inlineNodes(m[2])}</span>${sub}</li>`;
}

/** Block: returns a list of vnodes. `options.headingId(text)` may generate ids for the headings. */
export function markdownNodes(source, options = {}) {
  const lines = String(source == null ? '' : source).replace(/\r/g, '').split('\n');
  const out = [];
  let i = 0;
  while (i < lines.length) {
    const ln = lines[i];
    if (isFence(ln)) {
      const lang = ln.trim().replace(/^(```|~~~)/, '').trim();
      const buf = []; i += 1;
      while (i < lines.length && !isFence(lines[i])) buf.push(lines[i++]);
      i += 1;
      out.push(html`<pre class="md-pre" data-lang=${lang || null}><code>${buf.join('\n')}</code></pre>`);
      continue;
    }
    if (isHeading(ln)) {
      const n = /^#+/.exec(ln)[0].length, t = ln.replace(/^#+\s+/, '').replace(/\s+#+\s*$/, '');
      const Tag = 'h' + Math.min(6, n);
      out.push(html`<${Tag} class=${'md-h md-h' + n} id=${options.headingId ? options.headingId(t) : null}>${inlineNodes(t)}<//>`);
      i += 1; continue;
    }
    if (isHr(ln)) { out.push(html`<hr class="md-hr" />`); i += 1; continue; }
    if (isTableRow(ln) && i + 1 < lines.length && isTableSep(lines[i + 1])) {
      const head = cells(ln);
      const aligns = cells(lines[i + 1]).map((c) => (/^:-+:$/.test(c) ? 'center' : /-:$/.test(c) ? 'right' : null));
      i += 2;
      const rows = [];
      while (i < lines.length && isTableRow(lines[i])) rows.push(cells(lines[i++]));
      const style = (k) => (aligns[k] ? { textAlign: aligns[k] } : null);
      out.push(html`<div class="md-table-wrap"><table class="md-table">
        <thead><tr>${head.map((c, k) => html`<th style=${style(k)}>${inlineNodes(c)}</th>`)}</tr></thead>
        <tbody>${rows.map((r) => html`<tr>${head.map((_, k) => html`<td style=${style(k)}>${inlineNodes(r[k] == null ? '' : r[k])}</td>`)}</tr>`)}</tbody>
      </table></div>`);
      continue;
    }
    if (isQuote(ln)) {
      const buf = [];
      while (i < lines.length && isQuote(lines[i])) buf.push(lines[i++].replace(/^\s*>\s?/, ''));
      out.push(html`<blockquote class="md-quote">${markdownNodes(buf.join('\n'), options)}</blockquote>`);
      continue;
    }
    if (isUl(ln) || isOl(ln)) {
      const ord = isOl(ln), base = /^\s*/.exec(ln)[0].length, items = [];
      while (i < lines.length && (isUl(lines[i]) || isOl(lines[i]) || (/^\s+\S/.test(lines[i]) && items.length))) {
        const cur = lines[i], ind = /^\s*/.exec(cur)[0].length;
        if ((isUl(cur) || isOl(cur)) && ind <= base) {
          items.push({ text: cur.replace(/^\s*(?:[-*+]|\d+[.)])\s+/, ''), sub: [] });
        } else if (items.length) {
          items[items.length - 1].sub.push(cur.slice(Math.min(ind, base + 2)));
        }
        i += 1;
      }
      const Tag = ord ? 'ol' : 'ul';
      out.push(html`<${Tag} class="md-list">${items.map((it) => listItem(it.text, it.sub.length ? markdownNodes(it.sub.join('\n'), options) : null))}<//>`);
      continue;
    }
    if (!ln.trim()) { i += 1; continue; }
    const buf = [];
    while (i < lines.length && lines[i].trim() && !isFence(lines[i]) && !isUl(lines[i]) && !isOl(lines[i]) && !isHeading(lines[i]) && !isQuote(lines[i]) && !isHr(lines[i])
      && !(isTableRow(lines[i]) && i + 1 < lines.length && isTableSep(lines[i + 1]))) buf.push(lines[i++]);
    out.push(html`<p class="md-p">${inlineNodes(buf.join('\n'))}</p>`);
  }
  return out;
}

/** <Markdown source headingId /> renders the blocks without a wrapper (the caller provides the .md-doc).
    The vnodes are memoized by source: an unchanged document is not parsed nor diffed again. */
export function Markdown({ source, headingId }) {
  const nodes = useMemo(() => markdownNodes(source, { headingId }), [source, headingId]);
  return html`<${Fragment}>${nodes}<//>`;
}

/** Extracts the lines of a "## <title>" section (up to the next heading of the same level or above). */
export function extractSection(source, titleRe) {
  const lines = String(source == null ? '' : source).replace(/\r/g, '').split('\n');
  let level = 0;
  const buf = [];
  for (const ln of lines) {
    const m = /^(#{1,6})\s+(.*)$/.exec(ln);
    if (level) {
      if (m && m[1].length <= level) break;
      buf.push(ln);
    } else if (m && titleRe.test(m[2])) level = m[1].length;
    else if (!m && titleRe.test(ln) && /:\s*$/.test(ln.trim())) level = 7; // "Open questions:" without a heading
  }
  return level ? buf.join('\n').trim() : '';
}
