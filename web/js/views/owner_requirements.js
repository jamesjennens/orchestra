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
const active = (item) => !item.requirement_state || item.requirement_state === 'active';
function terminalStatus(ctx, pid, item) {
  if (active(item)) return null;
  const successor = item.state?.superseded_by;
  return h('section', { class: 'banner' }, h('strong', null,
    item.requirement_state === 'unknown' ? 'Status unknown' : item.requirement_state === 'withdrawn' ? 'Withdrawn' : 'Superseded'),
  h('p', null, item.state_message || displayText(item.reason?.reason)),
  successor ? h('p', null, 'Replaced by ', h('a', { href: ctx.href(`/p/${pid}/requirements/${successor.id}`) }, successor.id),
    ` · revision ${successor.revision}`, h('code', null, successor.sha256)) : null,
  item.state ? h('p', { class: 'small' }, `Recorded by ${item.state.account_id} · ${item.state.at}`) : null);
}

function terminalForm(ctx, pid, item, candidates) {
  const reason = h('textarea', { rows: 3, maxlength: 1000, required: true });
  const target = h('select', null, h('option', { value: '' }, 'Withdraw without replacement'),
    candidates.map((candidate, index) => h('option', { value: String(index + 1) },
      `${displayText(candidate.current.title)} · ${candidate.id} · revision ${candidate.current.revision}`)));
  const reference = h('p', { class: 'small' });
  const confirm = h('input', { type: 'checkbox', required: true });
  const button = h('button', { type: 'submit' }, 'Confirm withdrawal or supersession');
  const message = h('p', { role: 'status' });
  target.addEventListener('change', () => {
    const candidate = candidates[Number(target.value) - 1];
    reference.textContent = candidate ? `${candidate.id} · revision ${candidate.current.revision} · ${candidate.current.sha256}` : '';
    confirm.checked = false;
  });
  return h('details', { class: 'panel' }, h('summary', null, 'Withdraw or supersede'),
    h('form', { class: 'panel-body stack', onsubmit: async (event) => {
      event.preventDefault(); if (button.disabled || !confirm.checked || !reason.value.trim()) return;
      button.disabled = true;
      const body = { ...expected(item.current), expected_state_sha256: item.state?.sha256 || null, reason: reason.value };
      try {
        const candidate = candidates[Number(target.value) - 1];
        if (target.value && !candidate) throw new Error('Reload before selecting a replacement.');
        if (candidate) await ctx.api.supersedeRequirement(pid, item.id, { ...body, successor: {
          id: candidate.id, revision: candidate.current.revision, sha256: candidate.current.sha256 } });
        else await ctx.api.withdrawRequirement(pid, item.id, body);
        ctx.go(`/p/${pid}/requirements/${item.id}`);
      } catch (error) { message.textContent = describe(error); button.disabled = false; }
    } }, h('p', null, 'This cannot be undone in the browser. If this is a mistake, create a new requirement. The old wording and history remain available.'),
    h('label', null, 'Replacement', target), reference, h('label', null, 'Reason', reason),
    h('label', null, confirm, ' I understand this cannot be undone in the browser.'), button, message));
}

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
    terminalStatus(ctx, pid, item),
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
    try {
      const failed = await ctx.api.requirementRecoveries(pid);
      recovery = (failed.items || []).length ? h('section', { class: 'panel stack' },
        h('h2', null, 'Failed creations'),
        h('p', null, 'Clear a failed creation only when no requirement was written. Then start a new creation.'),
        h('p', { class: 'small' }, `${failed.listed ?? failed.items.length} of ${failed.total ?? failed.items.length} failed creations listed.`),
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
      }), failed.truncated ? h('p', null, 'More failed creations remain. Refresh after clearing these.') : null) : null;
    } catch (error) {
      recovery = h('section', { class: 'panel' }, h('h2', null, 'Failed creations'),
        h('p', { role: 'status' }, 'Failed creations could not be read. Reload; if it still fails, ask the host operator.'));
    }
  }
  return h('div', { class: 'stack' }, pageHead({ title: 'Business requirements',
    lede: 'The current document, generated from the project’s requirement records.' }),
  h('div', { class: 'actions' }, mode, modeMessage),
  (data.governance.warnings || []).map((warning) => h('p', { class: 'banner', role: 'status' }, warning.message)),
  !simple ? h('p', { class: 'banner' }, 'This project uses governed requirements.') : null,
  data.items.filter((item) => item.kind === 'brd-section').map(card),
  data.items.filter((item) => item.kind === 'requirement' && active(item)).map(card),
  data.items.some((item) => item.kind === 'requirement' && !active(item)) ? h('section', { class: 'stack' },
    h('h2', null, 'Withdrawn, superseded or unverified requirements'),
    data.items.filter((item) => item.kind === 'requirement' && !active(item)).map(card)) : null,
  data.items.filter((item) => item.unreadable).map(card),
  !data.items.length ? h('p', null, 'No requirements yet.') : null, add, recovery,
  h('section', { class: 'panel' }, h('h2', null, 'Decisions'), (document.decisions || []).map((d) => h('p', null, `${displayText(d.title)} · revision ${d.revision}`))));
}

export async function requirement(ctx, { pid, rid }, project, data) {
  const record = data.current;
  const main = data.accepted || record;
  const message = h('p', { role: 'status' });
  const editable = data.can_edit && data.governance.mode === 'simple' && active(data);
  let terminal = null;
  if (editable && data.kind === 'requirement' && record.acceptance_state === 'accepted' && !data.pending_draft) {
    try {
      const listing = await ctx.api.requirements(pid);
      terminal = terminalForm(ctx, pid, data, listing.items.filter((item) => item.id !== rid && active(item)
        && item.kind === 'requirement' && item.current?.acceptance_state === 'accepted' && !item.pending_draft));
    } catch (error) { terminal = h('p', { role: 'status' }, 'Replacement choices could not be read. Reload before withdrawing or superseding.'); }
  }
  const accept = editable && record.acceptance_state !== 'accepted'
    ? h('button', { type: 'button', class: 'primary', onclick: async (event) => {
      const button = event.currentTarget || event.target; button.disabled = true;
      try { await ctx.api.acceptRequirement(pid, rid, expected(record)); ctx.go(`/p/${pid}/requirements/${rid}`); }
      catch (error) { message.textContent = describe(error); button.disabled = false; }
    } }, data.accepted && data.pending_draft ? 'Accept pending edit' : 'Accept draft') : null;
  return h('div', { class: 'stack' }, pageHead({ title: displayText(main.title),
    crumbs: [{ label: 'Requirements', href: ctx.href(`/p/${pid}/requirements`) }] }),
  h('div', { class: 'actions' }, state(main), accept, message),
  terminalStatus(ctx, pid, data),
  h('p', { class: 'small' }, revision(main)),
  h('div', { class: 'panel-body', style: 'white-space:pre-wrap;unicode-bidi:plaintext' }, displayText(main.description)),
  data.accepted && data.pending_draft ? h('h2', null, `Pending edit · revision ${record.revision}`) : null,
  editable
    ? editor(ctx, pid, data, null, async () => ctx.go(`/p/${pid}/requirements/${rid}`))
    : data.accepted && data.pending_draft ? h('section', null,
      h('h3', null, displayText(record.title)),
      h('div', { class: 'panel-body', style: 'white-space:pre-wrap;unicode-bidi:plaintext' }, displayText(record.description))) : null,
  terminal,
  h('details', null, h('summary', null, 'Revision history'), (data.history || []).map((entry) => h('section', null,
    h('h3', null, revision(entry)), h('p', null, displayText(entry.title)),
    h('p', { style: 'white-space:pre-wrap;unicode-bidi:plaintext' }, displayText(entry.description))))),
  (data.supersedes || []).length ? h('p', null, 'Supersedes ', data.supersedes.map((old) =>
    h('a', { href: ctx.href(`/p/${pid}/requirements/${old.id}`) }, `${old.id} · revision ${old.revision}`))) : null);
}
