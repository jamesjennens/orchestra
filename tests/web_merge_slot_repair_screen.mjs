import assert from 'node:assert/strict';
const [shimUrl, apiUrl, viewUrl, base, project, token] = process.argv.slice(2);
const { Element } = await import(shimUrl);
Element.prototype.close = function () {};
Element.prototype.showModal = function () {
  queueMicrotask(() => this.querySelectorAll('button').at(-1).dispatch('click'));
};
const { createApi } = await import(apiUrl);
const { mergeSlotPanel } = await import(viewUrl);
const keys = [];
let losses = 2;
const api = createApi(async (method, path, headers, body) => {
  const response = await fetch(base + path, { method, headers: { ...headers, Authorization: 'Bearer ' + token }, body });
  const data = await response.json();
  if (method === 'POST' && path.endsWith('/create')) {
    keys.push(headers['Idempotency-Key']);
    assert.equal(response.status, 200);
    if (losses-- > 0) return { status: 503, data: { error: { message: 'Synthetic lost receipt' } } };
  }
  return { status: response.status, data };
});
const panel = mergeSlotPanel({ api }, { id: project, role: 'superuser', archived: false });
document.body.append(panel);
const button = (label) => panel.querySelectorAll('button').find((node) => node.textContent === label);
for (let i = 0; i < 1000 && !button('Create or repair slot'); i++) await new Promise((resolve) => setTimeout(resolve, 20));
assert.ok(button('Create or repair slot'), 'Missing slot has an explicit owner repair');
await button('Create or repair slot').dispatch('click');
assert.ok(button('Retry the same request'), 'An uncertain repair keeps an actionable retry');
await button('Retry the same request').dispatch('click');
assert.ok(button('Retry the same request'), 'A second lost answer keeps its retry');
await button('Retry the same request').dispatch('click');
assert.equal(keys.length, 3);
assert.ok(keys[0] && keys.every((key) => key === keys[0]), 'Repair retries retain the exact identity');
assert.ok(panel.textContent.includes('Held by ops'));

// A newer refresh wins even if an older response arrives last.
const waiting = [];
let read = 0;
const races = mergeSlotPanel({ api: { mergeSlot: async () => {
  if (!read++) return { available: true };
  return new Promise((resolve) => waiting.push(resolve));
} } }, { id: project });
document.body.append(races);await new Promise((resolve) => setTimeout(resolve, 0));
const refresh = () => races.querySelectorAll('button').find((node) => node.textContent === 'Refresh');
const first = refresh().dispatch('click');const second = refresh().dispatch('click');
waiting[1]({ available: false, holder: 'newer-holder' });await second;
waiting[0]({ available: true });await first;
assert.ok(races.textContent.includes('newer-holder'), 'Late response cannot replace the current holder');
console.log(JSON.stringify({ repairRequests: keys.length, stableKey: true, repeatedRetry: true, refreshRace: true }));
