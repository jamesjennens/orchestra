import { h, confirmDialog } from '../dom.js';
import { describe } from '../ui.js';

export function mergeSlotPanel(ctx, project) {
  const panel = h('section', { class: 'panel', 'aria-label': 'Merge slot' });
  let revision = 0;
  let writing = false;
  function showError(message, error) {
    message.textContent = describe(error);
    if (!error.retry) return;
    // kittrial-5bb.268 item 5: the audit entry is written after the slot changes. If the
    // server stopped between the two, only the same request, retried, records it.
    message.append(' This page does not retry by itself: if the change was made, the web audit records it only '
      + 'when the same request is retried, so retry it here before you leave the page. ');
    const retry = h('button', { type: 'button', onclick: async () => {
      if (writing) return;
      writing = true; retry.disabled = true;
      try { await error.retry(); writing = false; await draw(); }
      catch (again) { showError(message, again); }
      finally { writing = false; retry.disabled = false; }
    } }, 'Retry the same request');
    message.append(retry);
  }
  async function draw() {
    const version = ++revision;
    let slot;
    try {
      slot = await ctx.api.mergeSlot(project.id);
    } catch (error) {
      if (version !== revision) return;
      // Older services have no slot route. Keep the existing project page usable.
      if ([404, 501].includes(error.status)) { panel.replaceChildren(); return; }
      panel.replaceChildren(h('div', { class: 'stack' }, h('h2', null, 'Merge slot'),
        h('p', { role: 'alert' }, error.status === 503 ? unusable(error) : describe(error)),
        h('button', { type: 'button', onclick: draw }, 'Read again'),
        ['owner', 'superuser'].includes(project.role) && error.status === 503
          ? repairButton('Create or repair slot', () => ctx.api.createMergeSlot(project.id), true) : null));
      return;
    }
    if (version !== revision) return;
    const task = h('input', { maxlength: 96, required: true, 'aria-label': 'Task id' });
    const target = h('input', { maxlength: 160, required: true, 'aria-label': 'Integration target' });
    const reason = h('textarea', { maxlength: 1000, required: true, 'aria-label': 'Reason for forced release' });
    const message = h('p', { role: 'status' });
    function button(label, work, confirm = false) {
      const node = h('button', { type: 'button', disabled: writing, onclick: async () => {
        if (writing) return;
        if (confirm && !await confirmDialog({ title: label, body: 'Confirm this merge-slot change.', confirmLabel: label, danger: true })) return;
        writing = true; node.disabled = true;
        try { await work(); message.textContent = 'Saved.'; writing = false; await draw(); }
        catch (error) { showError(message, error); }
        finally { writing = false; node.disabled = false; }
      } }, label);
      return node;
    }
    const own = slot.holder === slot.current_actor;
    panel.replaceChildren(h('div', { class: 'stack' }, h('h2', null, 'Merge slot'),
      // The holder as people know it, and the exact name a forced release compares (kittrial-5bb.198).
      h('p', null, slot.available ? 'Available' : 'Held by ' + (slot.holder_name && slot.holder_name !== slot.holder
        ? slot.holder_name + ' (' + slot.holder + ')' : slot.holder)),
      slot.context ? h('p', null, 'Task: ' + slot.context.task + ' · Target: ' + slot.context.target
        + (slot.context.target_omitted_chars ? '…' : '')) : null,
      h('button', { type: 'button', onclick: draw }, 'Refresh'),
      slot.can_coordinate && slot.available && !project.archived ? h('div', { class: 'stack' },
        h('label', null, 'Task id', task), h('label', null, 'Integration target', target),
        button('Take slot', () => ctx.api.acquireMergeSlot(project.id, task.value, target.value))) : null,
      slot.can_coordinate && own && !slot.available ? button('Release slot', () => ctx.api.releaseMergeSlot(project.id)) : null,
      slot.can_force_release && !slot.available ? h('div', { class: 'stack' }, h('label', null, 'Reason for forced release', reason),
        // The task the page shows travels with the holder (kittrial-5bb.268 item 4): a holder
        // who released and took the slot again for another task is not released by this page.
        button('Force release', () => ctx.api.forceReleaseMergeSlot(project.id, slot.holder, reason.value,
          slot.context ? slot.context.task : undefined), true)) : null,
      message));
  }
  // kittrial-5bb.268 item 2: a 503 on the slot read is not a lost save. Say what the server
  // found and who can put it right, instead of the generic uncertain-outcome sentence.
  function unusable(error) {
    const found = error.message && error.message !== 'Request failed' ? error.message : 'The merge slot cannot be read.';
    const owner = ['owner', 'superuser'].includes(project.role);
    const who = /installation operator/i.test(found)
      ? ' An installation operator repairs this on the host.'
      : owner ? ' You can repair it with Create or repair slot below.'
        : ' A project owner or superuser can repair it from this page.';
    return 'The merge slot cannot be used: ' + found + who;
  }
  // The unavailable-state controls use the same explicit confirmation and retry flow.
  function repairButton(label, work, confirm) {
    const node = h('button', { type: 'button', onclick: async () => {
      if (writing || (confirm && !await confirmDialog({ title: label, body: 'Create the missing slot or repair its native shape?', confirmLabel: label }))) return;
      writing = true; node.disabled = true;
      try { await work(); writing = false; await draw(); }
      catch (error) {
        const message = h('p', { role: 'alert' });
        panel.append(message); showError(message, error);
      }
      finally { writing = false; node.disabled = false; }
    } }, label);
    return node;
  }
  draw();
  return panel;
}
