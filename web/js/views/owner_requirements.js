// Direct owner editing. Content is always text; the BRD is generated read-only.
import { h } from '../dom.js';
import { pageHead, describe } from '../ui.js';

const expected = (record) => ({ expected_revision: record.revision, expected_sha256: record.sha256 });
const state = (record) => h('span', { class: 'chip ' + (record.acceptance_state === 'accepted' ? 'ok' : 'warn') },
  record.acceptance_state === 'accepted' ? 'Accepted' : 'Draft');

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
  const card = (item) => h('section', { class: 'panel' },
    h('div', { class: 'panel-head' }, h('h2', { class: 'small' },
      h('a', { href: ctx.href(`/p/${pid}/requirements/${item.id}`) }, item.current.title)), state(item.current)),
    h('div', { class: 'panel-body', style: 'white-space:pre-wrap' }, item.current.description));
  const modeMessage = h('p', { role: 'status', class: 'small' });
  const mode = owner ? h('button', { type: 'button', onclick: async (event) => {
    const button = event.currentTarget || event.target; button.disabled = true;
    try {
      await ctx.api.setRequirementsGovernance(pid, { ...expected(data.governance), mode: simple ? 'governed' : 'simple' });
      ctx.go(`/p/${pid}/requirements`);
    } catch (error) { modeMessage.textContent = describe(error); button.disabled = false; }
  } }, simple ? 'Use governed requirements' : 'Enable simple editing') : null;
  // Parent is an existing canonical job, never a guessed id. The owner selects
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
        h('div', { class: 'panel-body stack' }, jobs.length > 1 ? h('label', { for: 'requirement-parent' }, 'Job', parent) : null, holder));
    } else add = h('p', { class: 'muted' }, 'Create a job in this project before adding requirements.');
  }
  const document = await ctx.api.brd(pid);
  return h('div', { class: 'stack' }, pageHead({ title: 'Business requirements',
    lede: 'The current document, generated from the project’s requirement records.' }),
  h('div', { class: 'actions' }, mode, modeMessage),
  !simple ? h('p', { class: 'banner' }, 'This project uses governed requirements.') : null,
  data.items.filter((item) => item.kind === 'brd-section').map(card),
  data.items.filter((item) => item.kind === 'requirement').map(card),
  !data.items.length ? h('p', null, 'No requirements yet.') : null, add,
  h('section', { class: 'panel' }, h('h2', null, 'Open questions'), (document.questions || []).map((q) => h('p', null, q.title, '\n', q.description))),
  h('section', { class: 'panel' }, h('h2', null, 'Decisions'), (document.decisions || []).map((d) => h('p', null, d.title))));
}

export async function requirement(ctx, { pid, rid }, project, data) {
  const record = data.current;
  const message = h('p', { role: 'status' });
  const accept = data.can_edit && data.governance.mode === 'simple' && record.acceptance_state !== 'accepted'
    ? h('button', { type: 'button', class: 'primary', onclick: async (event) => {
      const button = event.currentTarget || event.target; button.disabled = true;
      try { await ctx.api.acceptRequirement(pid, rid, expected(record)); ctx.go(`/p/${pid}/requirements/${rid}`); }
      catch (error) { message.textContent = describe(error); button.disabled = false; }
    } }, 'Accept') : null;
  return h('div', { class: 'stack' }, pageHead({ title: record.title,
    crumbs: [{ label: 'Requirements', href: ctx.href(`/p/${pid}/requirements`) }] }),
  h('div', { class: 'actions' }, state(record), accept, message),
  data.can_edit && data.governance.mode === 'simple'
    ? editor(ctx, pid, data, null, async () => ctx.go(`/p/${pid}/requirements/${rid}`))
    : h('div', { class: 'panel-body', style: 'white-space:pre-wrap' }, record.description));
}
