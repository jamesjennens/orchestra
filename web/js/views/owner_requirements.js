// Direct owner editing. Content is always text; the BRD is generated read-only.
import { h } from '../dom.js';
import { pageHead, describe } from '../ui.js';

const expected = (record) => ({ expected_revision: record.revision, expected_sha256: record.sha256 });
// Keep stored text unchanged; make invisible controls explicit in reading views.
const displayText = (text) => String(text || '').replace(/[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f-\u009f\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069]/g,
  (c) => `[U+${c.codePointAt(0).toString(16).toUpperCase().padStart(4, '0')}]`);
const state = (record) => h('span', { class: 'chip ' + (record.acceptance_state === 'accepted' ? 'ok' : 'warn') },
  record.acceptance_state === 'accepted' ? 'Accepted' : 'Draft');
const revision = (record) => `${record.acceptance_state === 'accepted' ? 'Accepted' : 'Draft'} revision ${record.revision}`;

function editor(ctx, pid, item, parent, done) {
  const record = item?.current;
  const title = h('input', { id: 'requirement-title', value: record?.title || '', maxlength: 200, required: true });
  const description = h('textarea', { id: 'requirement-text', rows: 12, maxlength: 20000, required: true }, record?.description || '');
  const kind = h('select', { id: 'requirement-kind' },
    h('option', { value: 'requirement' }, 'Requirement'), h('option', { value: 'brd-section' }, 'Purpose, users or scope'));
  const message = h('p', { role: 'status', class: 'small' });
  const save = h('button', { type: 'submit', class: 'primary' }, 'Save draft');
  let busy = false;
  const form = h('form', { class: 'stack', onsubmit: async (event) => {
    event.preventDefault(); if (busy) return; busy = true; save.disabled = true;
    try {
      const fields = { title: title.value, description: description.value };
      const result = item ? await ctx.api.reviseRequirement(pid, item.id, { ...fields, ...expected(record) })
        : await ctx.api.createRequirement(pid, { ...fields, kind: kind.value, parent });
      message.textContent = 'Draft saved.'; ctx.setDirty?.(false); await done(result);
    } catch (error) { message.textContent = describe(error); }
    finally { busy = false; save.disabled = false; }
  } }, !item ? h('label', { for: 'requirement-kind' }, 'What are you adding?', kind) : null,
  h('label', { for: 'requirement-title' }, 'Title', title),
  h('label', { for: 'requirement-text' }, 'Text', description), save, message);
  form.addEventListener('input', () => ctx.setDirty?.(true));
  return form;
}

export async function brd(ctx, { pid }, project, data) {
  const simple = data.governance.mode === 'simple';
  const owner = data.can_edit;
  const card = (item) => {
    if (item.unreadable) return h('section', { class: 'panel' }, h('h2', null, item.id), h('p', { role: 'status' }, item.message));
    const record = item.accepted || item.current;
    return h('section', { class: 'panel' },
    h('div', { class: 'panel-head' }, h('h2', { class: 'small' },
      h('a', { href: ctx.href(`/p/${pid}/requirements/${item.id}`) }, displayText(record.title))), state(record)),
    h('p', { class: 'small panel-body' }, revision(record)),
    h('div', { class: 'panel-body', style: 'white-space:pre-wrap;unicode-bidi:plaintext' }, displayText(record.description)),
    item.accepted && item.pending_draft ? h('details', { class: 'panel-body' }, h('summary', null, `Pending edit · revision ${item.pending_draft.revision}`),
      h('h3', null, displayText(item.pending_draft.title)), h('p', { style: 'white-space:pre-wrap;unicode-bidi:plaintext' }, displayText(item.pending_draft.description))) : null);
  };
  const modeMessage = h('p', { role: 'status', class: 'small' });
  const mode = owner ? h('button', { type: 'button', onclick: async (event) => {
    const button = event.currentTarget || event.target; button.disabled = true;
    try {
      await ctx.api.setRequirementsGovernance(pid, { ...expected(data.governance), mode: simple ? 'governed' : 'simple' });
      ctx.go(`/p/${pid}/requirements`);
    } catch (error) { modeMessage.textContent = describe(error); button.disabled = false; }
  } }, simple ? 'Use governed requirements' : 'Enable simple editing') : null;
  // Parent is an existing canonical task, never a guessed id. The owner selects
  // it only when adding new content; accepted text needs no setup dialogue.
  let add = null;
  if (owner && simple) {
    const jobs = data.jobs || [];
    const parent = h('select', { id: 'requirement-parent' }, jobs.map((job) => h('option', { value: job.id }, job.title)));
    const holder = h('div');
    const show = () => { holder.replaceChildren(editor(ctx, pid, null, parent.value,
      async (result) => ctx.go(`/p/${pid}/requirements/${result.id}`))); };
    if (jobs.length) {
      parent.addEventListener('change', show); show();
      add = h('details', { class: 'panel' }, h('summary', null, 'Add requirement or narrative'),
        h('div', { class: 'panel-body stack' }, jobs.length > 1 ? h('label', { for: 'requirement-parent' }, 'Task', parent) : null, holder));
    } else add = h('button', { type: 'button', onclick: async (event) => {
      const button = event.currentTarget || event.target; button.disabled = true;
      try {
        await ctx.api.createTask(pid, { title: 'Requirements', description: 'Requirement and narrative work for this project.' });
        ctx.go(`/p/${pid}/requirements`);
      } catch (error) { modeMessage.textContent = describe(error); button.disabled = false; }
    } }, 'Start requirements');
  }
  const document = await ctx.api.brd(pid);
  let recovery = null;
  if (owner && simple) {
    const failed = await ctx.api.requirementRecoveries(pid);
    recovery = (failed.items || []).length ? h('section', { class: 'panel stack' },
      h('h2', null, 'Failed creations'),
      h('p', null, 'Clear a failed creation only when no requirement was written. Then start a new creation.'),
      failed.items.map((item) => {
        const message = h('p', { role: 'status' }, item.message || 'No requirement was written.');
        if (!item.can_clear) return message;
        const reason = h('input', { maxlength: 1000, required: true, value: item.reason || '' });
        const button = h('button', { type: 'submit' }, 'Clear failed creation');
        return h('form', { class: 'stack', onsubmit: async (event) => {
          event.preventDefault(); if (button.disabled) return; button.disabled = true;
          try {
            await ctx.api.clearRequirementCreation(pid, { original_operation_id: item.operation_id,
              expected_receipt_sha256: item.expected_receipt_sha256, reason: reason.value });
            ctx.go(`/p/${pid}/requirements`);
          } catch (error) { message.textContent = describe(error); button.disabled = false; }
        } }, h('label', null, 'Why clear this failed creation?', reason), button, message);
      }), failed.truncated ? h('p', null, 'More failed creations may remain. Refresh after clearing these.') : null) : null;
  }
  return h('div', { class: 'stack' }, pageHead({ title: 'Business requirements',
    lede: 'The current document, generated from the project’s requirement records.' }),
  h('div', { class: 'actions' }, mode, modeMessage),
  (data.governance.warnings || []).map((warning) => h('p', { class: 'banner', role: 'status' }, warning.message)),
  !simple ? h('p', { class: 'banner' }, 'This project uses governed requirements.') : null,
  data.items.filter((item) => item.kind === 'brd-section').map(card),
  data.items.filter((item) => item.kind === 'requirement').map(card),
  data.items.filter((item) => item.unreadable).map(card),
  !data.items.length ? h('p', null, 'No requirements yet.') : null, add, recovery,
  h('section', { class: 'panel' }, h('h2', null, 'Decisions'), (document.decisions || []).map((d) => h('p', null, `${displayText(d.title)} · revision ${d.revision}`))));
}

export async function requirement(ctx, { pid, rid }, project, data) {
  const record = data.current;
  const main = data.accepted || record;
  const message = h('p', { role: 'status' });
  const accept = data.can_edit && data.governance.mode === 'simple' && record.acceptance_state !== 'accepted'
    ? h('button', { type: 'button', class: 'primary', onclick: async (event) => {
      const button = event.currentTarget || event.target; button.disabled = true;
      try { await ctx.api.acceptRequirement(pid, rid, expected(record)); ctx.go(`/p/${pid}/requirements/${rid}`); }
      catch (error) { message.textContent = describe(error); button.disabled = false; }
    } }, data.accepted && data.pending_draft ? 'Accept pending edit' : 'Accept draft') : null;
  return h('div', { class: 'stack' }, pageHead({ title: displayText(main.title),
    crumbs: [{ label: 'Requirements', href: ctx.href(`/p/${pid}/requirements`) }] }),
  h('div', { class: 'actions' }, state(main), accept, message),
  h('p', { class: 'small' }, revision(main)),
  h('div', { class: 'panel-body', style: 'white-space:pre-wrap;unicode-bidi:plaintext' }, displayText(main.description)),
  data.accepted && data.pending_draft ? h('h2', null, `Pending edit · revision ${record.revision}`) : null,
  data.can_edit && data.governance.mode === 'simple'
    ? editor(ctx, pid, data, null, async () => ctx.go(`/p/${pid}/requirements/${rid}`))
    : data.accepted && data.pending_draft ? h('section', null,
      h('h3', null, displayText(record.title)),
      h('div', { class: 'panel-body', style: 'white-space:pre-wrap;unicode-bidi:plaintext' }, displayText(record.description))) : null);
}
