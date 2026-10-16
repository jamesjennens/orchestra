// The undo wording follows the operation actually happening (kittrial-5bb.295 item 4):
// a late withdrawal withdraws the later accepted revision, a late supersession supersedes it.
import './web_dom_shim.mjs';
import assert from 'node:assert/strict';
const [apiUrl, viewUrl] = process.argv.slice(2);
const view = await import(viewUrl);
const ctx = { api: {}, href: (path) => '#' + path, go() {}, setDirty() {} };
const data = (operation) => ({
  kind: 'requirement', can_edit: true, requirement_state: 'withdrawn', governance: { mode: 'simple' },
  current: { title: 'Intent', description: 'Statement.', revision: 3, acceptance_state: 'accepted', sha256: 'a'.repeat(64) },
  accepted: null, history: [], supersedes: [],
  pending_transition: { operation_id: 'op-1', expected_receipt_sha256: 'b'.repeat(64),
    can_finish: true, can_abandon: false, state: { state: 'withdrawn', at: '2026-01-01T00:00:00Z' },
    reason: { reason: 'No longer needed.' }, message: 'A requirement transition is pending.',
    undoes: { revision: 4, sha256: 'c'.repeat(64), accepted_at: '2026-01-02T00:00:00Z', operation } },
});
const withdraw = await view.requirement(ctx, { pid: 'pp', rid: 'req-1' }, {}, data('withdraw'));
const supersede = await view.requirement(ctx, { pid: 'pp', rid: 'req-1' }, {}, data('supersede'));
const withdrawLabel = 'I understand finishing also withdraws the later accepted revision named above.';
const supersedeLabel = 'I understand finishing also supersedes the later accepted revision named above.';
assert.ok(withdraw.textContent.includes(withdrawLabel), withdraw.textContent);
assert.ok(supersede.textContent.includes(supersedeLabel), supersede.textContent);
assert.ok(!supersede.textContent.includes(withdrawLabel));
console.log(JSON.stringify({ withdraw: true, supersede: true }));
