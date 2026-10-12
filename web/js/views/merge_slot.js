import { h, confirmDialog } from '../dom.js';
import { describe } from '../ui.js';

export function mergeSlotPanel(ctx, project) {
  const panel = h('section', { class: 'panel', 'aria-label': 'Merge slot' });
  let revision = 0;
  let writing = false;
  async function draw() {
    const version = ++revision;
    let slot;
    try {
      slot = await ctx.api.mergeSlot(project.id);
    } catch (error) {
      if (version !== revision) return;
      // Older services have no slot route. Keep the existing project page usable.
      if ([404, 501].includes(error.status)) { panel.replaceChildren(); return; }
      panel.replaceChildren(h('div', { class: 'stack' }, h('h2', null, 'Merge slot'), h('p', { role: 'alert' }, describe(error)),
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
        catch (error) { message.textContent = describe(error); if (error.retry) message.append(h('button', { type: 'button', onclick: async () => {
          try { await error.retry(); await draw(); } catch (again) { message.textContent = describe(again); }
        } }, 'Retry the same request')); }
        finally { writing = false; node.disabled = false; }
      } }, label);
      return node;
    }
    const own = slot.holder === slot.current_actor;
    panel.replaceChildren(h('div', { class: 'stack' }, h('h2', null, 'Merge slot'),
      h('p', null, slot.available ? 'Available' : 'Held by ' + slot.holder),
      slot.context ? h('p', null, 'Task: ' + slot.context.task + ' · Target: ' + slot.context.target
        + (slot.context.target_omitted_chars ? '…' : '')) : null,
      h('button', { type: 'button', onclick: draw }, 'Refresh'),
      slot.can_coordinate && slot.available && !project.archived ? h('div', { class: 'stack' },
        h('label', null, 'Task id', task), h('label', null, 'Integration target', target),
        button('Take slot', () => ctx.api.acquireMergeSlot(project.id, task.value, target.value))) : null,
      slot.can_coordinate && own && !slot.available ? button('Release slot', () => ctx.api.releaseMergeSlot(project.id)) : null,
      slot.can_force_release && !slot.available ? h('div', { class: 'stack' }, h('label', null, 'Reason for forced release', reason),
        button('Force release', () => ctx.api.forceReleaseMergeSlot(project.id, slot.holder, reason.value), true)) : null,
      message));
  }
  // The unavailable-state controls use the same explicit confirmation and retry flow.
  function repairButton(label, work, confirm) {
    const node = h('button', { type: 'button', onclick: async () => {
      if (writing || (confirm && !await confirmDialog({ title: label, body: 'Create the missing slot or repair its native shape?', confirmLabel: label }))) return;
      writing = true; node.disabled = true;
      try { await work(); writing = false; await draw(); }
      catch (error) { panel.append(h('p', { role: 'alert' }, describe(error))); }
      finally { writing = false; node.disabled = false; }
    } }, label);
    return node;
  }
  draw();
  return panel;
}
