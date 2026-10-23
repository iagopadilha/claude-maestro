/* Settings dialog (hub and project panel): every app setting of ~/.loop-harness/config.json in one place.
   GET /api/app/settings lists them (value, default, type, restart_required, caveman availability and cost comparison); POST /api/app/settings
   saves one `{key, value, confirm: true}` or resets it `{key, reset: true, confirm: true}` (the key leaves config.json). A bool saves when
   toggled; the other types show Save once the draft differs. Presentational parts here, the calls in SettingsBody. */
import { html, useState, useEffect } from '../util/preact.js';
import { appApi } from '../api/api_client.js';
import { toast } from './toast.js';
import { openDialog } from './dialog.js';

const SAVE_TIMEOUT_MS = 15000;
const RESTART_TEXT = 'Takes effect after restarting the app.';
const NEXT_RUN_TEXT = 'Applies to the next claude run.';
const sameValue = (a, b) => JSON.stringify(a) === JSON.stringify(b);
const toDraft = (item) => (item.type === 'list' ? (item.value || []).join('\n') : item.type === 'bool' ? !!item.value : String(item.value == null ? '' : item.value));
const money = (n) => (typeof n === 'number' ? '$' + n.toFixed(2) : '—');

/** The value to send for a draft, or an `{ error }` the user can fix before any call. */
function fromDraft(item, draft) {
  if (item.type === 'bool' || item.type === 'choice' || item.type === 'text') return { value: item.type === 'bool' ? !!draft : String(draft) };
  if (item.type === 'list') return { value: String(draft).split('\n').map((line) => line.trim()).filter(Boolean) };
  const text = String(draft).trim(), number = Number(text);
  if (!text || !Number.isFinite(number)) return { error: 'Type a number.' };
  if (item.type === 'int' && !Number.isInteger(number)) return { error: 'Type a whole number.' };
  return { value: number };
}

function SettingControl({ item, draft, disabled, onDraft, onToggle }) {
  const id = 'set-' + item.key;
  if (item.type === 'bool') {
    return html`<label class="nl-check set-toggle" for=${id}><input type="checkbox" id=${id} checked=${!!draft} disabled=${disabled}
      onChange=${(e) => onToggle(e.currentTarget.checked)} /><span>${draft ? 'On' : 'Off'}</span></label>`;
  }
  if (item.type === 'choice') {
    return html`<select id=${id} class="dlg-input set-input" value=${draft} disabled=${disabled} onChange=${(e) => onDraft(e.currentTarget.value)}>
      ${(item.choices || []).map((c) => html`<option key=${c.value} value=${c.value} selected=${c.value === draft}>${c.label}</option>`)}</select>`;
  }
  if (item.type === 'list') {
    return html`<textarea id=${id} class="dlg-area mono set-input" rows="3" spellcheck="false" disabled=${disabled} value=${draft}
      onInput=${(e) => onDraft(e.currentTarget.value)} />`;
  }
  const numeric = item.type === 'int' || item.type === 'number';
  return html`<input id=${id} class=${'dlg-input set-input' + (numeric ? '' : ' mono')} type=${numeric ? 'number' : 'text'} min=${numeric ? '0' : undefined}
    step=${item.type === 'number' ? 'any' : undefined} autocomplete="off" spellcheck="false" disabled=${disabled} value=${draft}
    placeholder=${numeric ? undefined : item.default === '' ? 'default' : String(item.default)} onInput=${(e) => onDraft(e.currentTarget.value)} />`;
}

/** Cost per turn with and without caveman over the recorded turns of every project (CAV-04). */
function CavemanComparison({ caveman }) {
  if (!caveman) return null;
  return html`<div class="set-caveman" id="set-caveman-info">
    <div class="hint">${caveman.recommendation}</div>
    ${caveman.enough_data
      ? html`<table class="set-compare"><caption class="sr-only">Average cost per turn with and without caveman</caption>
          <thead><tr><th scope="col"></th><th scope="col">Turns</th><th scope="col">Avg cost per turn</th></tr></thead>
          <tbody><tr><th scope="row">With caveman</th><td class="mono">${caveman.on.turns}</td><td class="mono">${money(caveman.on.average_cost_usd)}</td></tr>
            <tr><th scope="row">Without caveman</th><td class="mono">${caveman.off.turns}</td><td class="mono">${money(caveman.off.average_cost_usd)}</td></tr></tbody></table>`
      : html`<div class="set-nodata mono" id="set-caveman-nodata">${caveman.message}</div>`}
  </div>`;
}

function SettingRow({ item, caveman, onSaved }) {
  const [draft, setDraft] = useState(() => toDraft(item));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  useEffect(() => { setDraft(toDraft(item)); }, [item.value]);
  const blocked = item.available === false && !item.value; // caveman not installed: cannot be turned on
  const parsed = fromDraft(item, draft);
  const changed = item.type !== 'bool' && !parsed.error && !sameValue(parsed.value, item.value);
  const send = async (body, done) => {
    setBusy(true); setError('');
    try {
      const r = await appApi.post('settings', Object.assign({ key: item.key, confirm: true }, body), SAVE_TIMEOUT_MS);
      onSaved(r.setting); toast(done, 'ok');
    } catch (e) { setError(e.message); setDraft(toDraft(item)); }
    finally { setBusy(false); }
  };
  const save = (value) => send({ value }, item.label + ' saved.');
  const reset = () => send({ reset: true }, item.label + ' back to its default.');
  return html`<section class="set-row" aria-labelledby=${'set-l-' + item.key} data-key=${item.key}>
    <div class="set-head">
      <label class="set-label" id=${'set-l-' + item.key} for=${'set-' + item.key}>${item.label}</label>
      ${item.restart_required ? html`<span class="set-tag set-restart" title=${RESTART_TEXT}>restart</span>` : null}
      ${item.customized ? html`<span class="set-tag set-custom">changed</span>` : null}
    </div>
    <p class="set-desc">${item.description}</p>
    <div class="set-edit">
      <${SettingControl} item=${item} draft=${draft} disabled=${busy || blocked}
        onDraft=${(v) => { setDraft(v); setError(''); }} onToggle=${(v) => { setDraft(v); save(v); }} />
      ${item.type !== 'bool' ? html`<button type="button" class="btn btn-sm btn-primary" id=${'set-save-' + item.key}
        disabled=${busy || !changed} onClick=${() => save(parsed.value)}>Save</button>` : null}
      <button type="button" class="btn btn-sm btn-ghost" id=${'set-reset-' + item.key} disabled=${busy || !item.customized} onClick=${reset}
        title=${'Back to the default: ' + (Array.isArray(item.default) ? (item.default.join(', ') || 'none') : item.default === '' ? 'empty' : item.default)}>Reset to default</button>
    </div>
    ${parsed.error && item.type !== 'bool' && String(draft).trim() !== '' ? html`<div class="nl-err" role="alert">${parsed.error}</div>` : null}
    ${error ? html`<div class="nl-err" role="alert">${error}</div>` : null}
    ${item.note ? html`<div class="hint set-note" id=${'set-note-' + item.key}>${item.note}</div>` : null}
    <div class="hint set-when">${item.restart_required ? RESTART_TEXT : NEXT_RUN_TEXT}</div>
    ${item.key === 'loop_caveman' ? html`<${CavemanComparison} caveman=${caveman} />` : null}
  </section>`;
}

function InfoRows({ info }) {
  if (!info || !info.length) return null;
  return html`<section class="set-info" aria-label="Read-only information">
    <div class="small-label">About this installation (read only)</div>
    <dl class="set-info-list">${info.map((row) => [html`<dt key=${'k-' + row.key}>${row.label}</dt>`, html`<dd key=${'v-' + row.key} class="mono" title=${row.value}>${row.value}</dd>`])}</dl>
  </section>`;
}

/** Body of the dialog: loads the list, keeps it fresh after each save. */
export function SettingsBody() {
  const [data, setData] = useState(null);
  const [error, setError] = useState('');
  useEffect(() => {
    appApi.get('settings', SAVE_TIMEOUT_MS).then(setData).catch((e) => setError(e.message));
  }, []);
  if (error) return html`<div class="nl-box nl-bad" role="alert">Could not read the settings: ${error}</div>`;
  if (!data) return html`<div class="nl-msg" role="status"><div class="spinner" aria-hidden="true" /><span>loading settings…</span></div>`;
  const replace = (saved) => setData((old) => Object.assign({}, old, { settings: old.settings.map((s) => (s.key === saved.key ? saved : s)) }));
  return html`<p class="dlg-text">Saved in <span class="mono">config.json</span> of the app data folder. A setting you never change stays out of the file and keeps its default.</p>
    <div class="set-list">${data.settings.map((item) => html`<${SettingRow} key=${item.key} item=${item} caveman=${data.caveman} onSaved=${replace} />`)}</div>
    <${InfoRows} info=${data.info} />`;
}

export function openSettingsDialog() {
  return openDialog({ title: 'Settings', width: 760, cls: 'dlg-settings', content: html`<${SettingsBody} />`, actions: [{ text: 'Close', cls: 'btn-primary' }] });
}

/** The header button: gear icon + label. */
export function SettingsButton() {
  return html`<button type="button" class="btn btn-ghost set-btn" id="btn-settings" title="App settings" onClick=${openSettingsDialog}>
    <svg class="set-gear" viewBox="0 0 24 24" width="14" height="14" aria-hidden="true" focusable="false"><path fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"
      d="M12 15.2a3.2 3.2 0 1 0 0-6.4 3.2 3.2 0 0 0 0 6.4zM19.4 13.5a1.6 1.6 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.6 1.6 0 0 0-1.8-.3 1.6 1.6 0 0 0-1 1.5V19.4a2 2 0 1 1-4 0v-.1a1.6 1.6 0 0 0-1-1.5 1.6 1.6 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.6 1.6 0 0 0 .3-1.8 1.6 1.6 0 0 0-1.5-1H4.6a2 2 0 1 1 0-4h.1a1.6 1.6 0 0 0 1.5-1 1.6 1.6 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.6 1.6 0 0 0 1.8.3h.1a1.6 1.6 0 0 0 1-1.5V4.6a2 2 0 1 1 4 0v.1a1.6 1.6 0 0 0 1 1.5 1.6 1.6 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.6 1.6 0 0 0-.3 1.8v.1a1.6 1.6 0 0 0 1.5 1h.2a2 2 0 1 1 0 4h-.1a1.6 1.6 0 0 0-1.5 1z" /></svg>
    <span>Settings</span></button>`;
}
