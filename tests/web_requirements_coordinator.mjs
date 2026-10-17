// A project's coordinator uses the same page as its owner, including accepting many
// requirements in one action (kittrial-5bb.278, WEB_COORDINATOR_DESIGN slice 10).
import './web_dom_shim.mjs';
import assert from 'node:assert/strict';
const [apiUrl, viewUrl, base, pid, coordinatorToken] = process.argv.slice(2);
const { createApi } = await import(apiUrl);
const view = await import(viewUrl);
const transport = (token) => async (method, path, headers, body) => {
  const response = await fetch(base + path, { method, headers: { ...headers, Authorization: 'Bearer ' + token }, body });
  return { status: response.status, data: response.status === 204 ? null : await response.json() };
};
const context = (token) => ({ api: createApi(transport(token)), href: (p) => '#' + p,
  go(p) { this.destination = p; }, setDirty() {} });
const coordinator = context(coordinatorToken);

let data = await coordinator.api.requirements(pid);
assert.equal(data.can_edit, true, 'a coordinator may edit this page');
let page = await view.brd(coordinator, { pid }, {}, data);
assert.ok(page.textContent.includes('Accept several at once'), page.textContent);
const panel = page.querySelectorAll('details').find((d) => d.textContent.includes('Accept several at once'));
const boxes = panel.querySelectorAll('input');
assert.equal(boxes.length, 2, panel.textContent);
for (const box of boxes) { box.checked = true; await box.dispatch('change'); }
await panel.querySelectorAll('button').find((b) => b.textContent === 'Accept selected').dispatch('click');
assert.equal(coordinator.destination, `/p/${pid}/requirements`, panel.textContent);
const after = await coordinator.api.requirements(pid);
assert.equal(after.items.filter((item) => item.current.acceptance_state === 'accepted').length, 2,
  JSON.stringify(after.items.map((item) => item.current.acceptance_state)));

// The same panel shows what was NOT accepted, with the reason, and undoes nothing.
await coordinator.api.createRequirement(pid, { kind: 'requirement', parent: data.jobs[0].id,
  title: 'Third requirement', description: 'Created for the batch refusal check.' });
data = await coordinator.api.requirements(pid);
const third = data.items.find((item) => item.current.title === 'Third requirement');
const stale = data.items.find((item) => item.current.acceptance_state === 'accepted');
const body = { items: [
  { id: third.id, expected_revision: third.current.revision, expected_sha256: third.current.sha256 },
  { id: stale.id, expected_revision: 1, expected_sha256: '0'.repeat(64) }] };
const answer = await coordinator.api.acceptRequirements(pid, body);
assert.equal(answer.summary.accepted, 1, JSON.stringify(answer));
assert.equal(answer.summary.refused, 1, JSON.stringify(answer));
assert.ok(answer.refused[0].reason, JSON.stringify(answer));
page = await view.brd(coordinator, { pid }, {}, await coordinator.api.requirements(pid));
assert.ok(!page.textContent.includes('Accept several at once'),
  'the accepted ones are no longer offered for a second acceptance');
console.log(JSON.stringify({ coordinatorEdits: true, batchAccepted: true, batchRefusalShown: true }));
