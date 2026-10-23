// Frozen documents are displayed as text. No source Markdown is interpreted.
import { h } from '../dom.js';
import { describe } from '../ui.js';

const shown = value => String(value ?? '').replace(/[\u0000-\u001f\u007f-\u009f\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069]/g,
  c => `[U+${c.codePointAt(0).toString(16).toUpperCase().padStart(4, '0')}]`);

function readError(error) {
  if (error?.status === 0 || error?.status >= 500)
    return 'Baselines could not be read. Reload baselines; if it still fails, ask the host operator.';
  return describe(error);
}
function cutError(error) {
  if (error?.status === 0 || error?.status >= 500)
    return 'The cut may have completed. Retry the same named cut using its saved request. Reloading keeps the request key for this account.';
  return describe(error);
}

export function baselinePanel(ctx, pid, canCut) {
  const status = h('p', { role: 'status' }, 'Loading baselines...');
  const content = h('div', { class: 'stack' });
  const pending = h('div', { class: 'stack' });
  const select = h('select', { id: 'brd-baseline', disabled: true }, h('option', { value: '' }, 'Current requirements'));
  const exportButton = h('button', { type: 'button', disabled: true, onclick: exportDocument }, 'Export Markdown');
  const compareButton = h('button', { type: 'button', disabled: true, onclick: showChanges }, 'Changes since baseline');
  let generation = 0;
  const selection = () => select.value || undefined;
  function busy(value) { exportButton.disabled = value; compareButton.disabled = value; }
  async function exportDocument() {
    const selected = selection(), turn = ++generation; busy(true); content.textContent = 'Loading document...';
    try {
      const result = await ctx.api.exportBrd(pid, selected);
      if (turn !== generation) return;
      const preview = h('pre', { style: 'white-space:pre-wrap;unicode-bidi:plaintext' }, result.markdown);
      const save = h('button', { type: 'button', onclick: () => {
        const url = URL.createObjectURL(new Blob([result.markdown], { type: 'text/markdown;charset=utf-8' }));
        const link = h('a', { href: url, download: result.filename });
        document.body.appendChild(link); link.click(); link.remove();
        setTimeout(() => URL.revokeObjectURL(url), 1000);
      } }, 'Save ' + shown(result.filename));
      content.replaceChildren(h('p', null, result.baseline ? 'Frozen baseline: ' + shown(result.baseline) : 'Current requirements'),
        h('p', { class: 'small' }, 'SHA-256: ', h('code', null, result.sha256)), save, preview);
    } catch (error) { if (turn === generation) content.textContent = readError(error); }
    finally { if (turn === generation) busy(false); }
  }
  async function showChanges() {
    const selected = selection(), turn = ++generation; busy(true); content.textContent = 'Loading changes...';
    try {
      const result = await ctx.api.brdChanges(pid, selected);
      if (turn !== generation) return;
      if (!result.available) { content.textContent = result.message; return; }
      const groups = Object.entries(result.groups).filter(([, ids]) => ids.length);
      const rows = groups.map(([kind, ids]) => h('p', null, shown(kind) + ': ' + ids.map(shown).join(', ')));
      const refs = (result.changes || []).map(change => h('details', null,
        h('summary', null, shown(change.id) + ': exact references'),
        h('pre', { style: 'white-space:pre-wrap' }, JSON.stringify({ before: change.before, after: change.after,
          before_state: change.before_state, after_state: change.after_state }, null, 2))));
      const context = Object.entries(result.context || {}).flatMap(([kind, entries]) => entries.map(entry =>
        h('p', null, shown(kind) + ': ' + shown(entry.change) + ' ' + shown(entry.id))));
      content.replaceChildren(h('div', { class: 'stack' }, h('h3', null, 'Changes since ' + shown(result.baseline)),
        rows, refs, context, result.governance ? h('p', null, 'Requirements governance changed.') : null,
        !rows.length && !context.length && !result.governance ? h('p', null, 'No changes since this baseline.') : null));
    } catch (error) { if (turn === generation) content.textContent = readError(error); }
    finally { if (turn === generation) busy(false); }
  }
  select.addEventListener('change', () => { generation++; content.textContent = ''; busy(false); });
  async function retrySaved(item, button, message) {
    button.disabled = true;
    try { await item.retry(); await reload(); message.textContent = 'Baseline cut reconciled.'; }
    catch (error) { message.textContent = cutError(error); }
    finally { button.disabled = false; }
  }
  async function reload() {
    status.textContent = 'Loading baselines...'; select.disabled = true; busy(true);
    try {
      const result = await ctx.api.baselines(pid);
      select.replaceChildren(h('option', { value: '' }, 'Current requirements'),
        ...result.items.map(item => h('option', { value: item.name, disabled: !item.readable },
          shown(item.name) + (item.name === result.current?.name ? ' (last cut)' : '') + (!item.readable ? ' (unavailable)' : ''))));
      select.value = '';
      status.textContent = result.current_error || (result.total ? `${result.total} of ${result.limit} baselines.` : 'No baselines yet.');
      if (result.unfinished) status.textContent += ' An unfinished cut remains. Retry its saved request or ask the host operator to reconcile it.';
      select.disabled = false; busy(false);
    } catch (error) { status.textContent = readError(error); }
    pending.replaceChildren();
    if (canCut) {
      try {
        const intents = await ctx.api.pendingBaselineCuts(pid);
        for (const item of intents) {
          const message = h('p', { role: 'status' });
          const button = h('button', { type: 'button', onclick: () => retrySaved(item, button, message) }, 'Retry ' + shown(item.name));
          pending.appendChild(h('div', null, button, message));
        }
      } catch (error) { pending.textContent = readError(error); }
    }
  }
  const name = h('input', { id: 'baseline-name', required: true, maxlength: 64, autocomplete: 'off' });
  const confirm = h('input', { type: 'checkbox', required: true });
  const cutMessage = h('p', { role: 'status' });
  const cutButton = h('button', { type: 'submit' }, 'Cut baseline');
  const form = canCut ? h('form', { class: 'stack', onsubmit: async event => {
    event.preventDefault(); if (cutButton.disabled || !confirm.checked) return;
    cutButton.disabled = true;
    try {
      await ctx.api.cutBaseline(pid, { name: name.value });
      name.value = ''; confirm.checked = false; cutMessage.textContent = 'Baseline cut.';
      await reload();
    } catch (error) { cutMessage.textContent = cutError(error); await reload(); }
    finally { cutButton.disabled = false; }
  } }, h('label', { for: 'baseline-name' }, 'New baseline name', name),
    h('label', null, confirm, ' Freeze this view. Pending wording stays unaccepted.'), cutButton, cutMessage) : null;
  const panel = h('section', { class: 'panel stack' }, h('h2', null, 'Baselines and Markdown'),
    h('p', null, 'A baseline freezes this view. It does not accept pending requirements or narrative.'),
    status, h('label', { for: 'brd-baseline' }, 'Document', select),
    h('div', { class: 'actions' }, exportButton, compareButton,
      h('button', { type: 'button', onclick: reload }, 'Reload baselines')), form, pending, content);
  void reload();
  return panel;
}
