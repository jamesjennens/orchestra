// kittrial-5bb.268 items 2, 4 and 5 on the real merge-slot page, with a fake API.
import assert from 'node:assert/strict';
const [shimUrl, viewUrl] = process.argv.slice(2);
const { Element } = await import(shimUrl);
Element.prototype.close = function () {};
Element.prototype.showModal = function () {
  queueMicrotask(() => Array.from(this.querySelectorAll('button')).at(-1).dispatch('click'));
};
const { mergeSlotPanel } = await import(viewUrl);
const settle = () => new Promise((resolve) => setTimeout(resolve, 0));
const failing = (status, message) => async () => {
  const error = new Error(message); error.status = status; throw error;
};
const damaged = 'The merge slot is damaged. A signed-in owner or superuser must use the explicit create/repair action.';
async function readFailure(role, message) {
  const panel = mergeSlotPanel({ api: { mergeSlot: failing(503, message) } }, { id: 'pp', role });
  document.body.append(panel); await settle(); await settle();
  const alert = Array.from(panel.querySelectorAll('p')).find((node) => node.getAttribute && node.getAttribute('role') === 'alert');
  const repair = Array.from(panel.querySelectorAll('button')).some((node) => node.textContent === 'Create or repair slot');
  return { text: alert.textContent, repair };
}
const contributor = await readFailure('contributor', damaged);
const owner = await readFailure('owner', damaged);
const operator = await readFailure('owner', 'Merge-slot context cannot be read; an installation operator must inspect it before retrying.');

// An uncertain force release keeps its retry and says the page does not retry by itself.
let forced = null;
const uncertain = mergeSlotPanel({ api: {
  mergeSlot: async () => ({ available: false, holder: 'ops', context: { holder: 'ops', task: 'pp-7', target: 'main' },
                            current_actor: 'usr_x', can_coordinate: true, can_force_release: true }),
  forceReleaseMergeSlot: async (pid, holder, reason, task) => {
    forced = { holder, reason, task };
    const error = new Error('lost'); error.status = 503; error.retry = async () => ({}); throw error;
  },
} }, { id: 'pp', role: 'owner' });
document.body.append(uncertain); await settle(); await settle();
const reason = uncertain.querySelectorAll('textarea')[0]; reason.value = 'Gone';
await Array.from(uncertain.querySelectorAll('button')).find((node) => node.textContent === 'Force release').dispatch('click');
await settle();
const status = Array.from(uncertain.querySelectorAll('p')).find((node) => node.getAttribute && node.getAttribute('role') === 'status');
assert.ok(Array.from(uncertain.querySelectorAll('button')).some((node) => node.textContent === 'Retry the same request'));
console.log(JSON.stringify({ contributor: contributor.text, owner: owner.text, operator: operator.text,
  ownerHasRepair: owner.repair, contributorHasRepair: contributor.repair, uncertain: status.textContent, forced }));
