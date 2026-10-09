// Actual API calls to a loopback service; text and controls rendered with the DOM shim.
import './web_dom_shim.mjs';
import assert from 'node:assert/strict';
const [apiUrl, viewUrl, base, pid, ownerToken, viewerToken] = process.argv.slice(2);
const { createApi } = await import(apiUrl);
const view = await import(viewUrl);
const transport = (token) => async (method, path, headers, body) => {
  const response = await fetch(base + path, { method, headers: { ...headers, Authorization: 'Bearer ' + token }, body });
  return { status: response.status, data: response.status === 204 ? null : await response.json() };
};
function context(token) {
  return { api: createApi(transport(token)), href: (p) => '#' + p, go(p) { this.destination = p; }, setDirty() {} };
}
const owner = context(ownerToken);
const viewer = context(viewerToken);
const data = await owner.api.requirements(pid);
let page = await view.brd(owner, { pid }, {}, data);
assert.ok(page.textContent.includes('Add requirement or narrative'));
assert.ok(!page.textContent.includes(data.governance.sha256));
const form = page.querySelector('form');
form.querySelector('#requirement-title').value = '<img src=x onerror=alert(1)>';
form.querySelector('#requirement-text').value = '<script>evil()</script>\nOwner intent\u2028as text';
await form.dispatch('submit');
assert.ok(owner.destination, page.textContent);
const id = owner.destination.split('/').at(-1);
let item = await owner.api.requirement(pid, id);
page = await view.requirement(owner, { pid, rid: id }, {}, item);
assert.equal(page.querySelectorAll('script').length, 0);
assert.equal(page.querySelectorAll('img').length, 0);
assert.ok(page.textContent.includes('<img src=x onerror=alert(1)>'));
assert.ok(!page.textContent.includes(item.current.sha256));
let accept = page.querySelectorAll('button').find((b) => b.textContent === 'Accept');
assert.ok(accept);
await accept.dispatch('click');
item = await owner.api.requirement(pid, id);
assert.equal(item.current.acceptance_state, 'accepted');
const acceptedHash = item.current.sha256;
page = await view.requirement(owner, { pid, rid: id }, {}, item);
assert.ok(!page.querySelectorAll('button').some((b) => b.textContent === 'Accept'));
page.querySelector('#requirement-text').value = 'Changed intent';
await page.querySelector('form').dispatch('submit');
item = await owner.api.requirement(pid, id);
assert.equal(item.current.acceptance_state, 'draft');
assert.equal(item.history[1].sha256, acceptedHash);
const readOnly = await view.requirement(viewer, { pid, rid: id }, {}, await viewer.api.requirement(pid, id));
assert.equal(readOnly.querySelectorAll('form').length, 0);
assert.equal(readOnly.querySelectorAll('button').length, 0);
assert.ok(readOnly.textContent.includes('Changed intent'));
const viewerDocument = await view.brd(viewer, { pid }, {}, await viewer.api.requirements(pid));
assert.equal(viewerDocument.querySelectorAll('form').length, 0);
assert.equal(viewerDocument.querySelectorAll('button').length, 0);
page = await view.brd(owner, { pid }, {}, await owner.api.requirements(pid));
await page.querySelectorAll('button').find((b) => b.textContent === 'Use governed requirements').dispatch('click');
page = await view.requirement(owner, { pid, rid: id }, {}, await owner.api.requirement(pid, id));
assert.equal(page.querySelectorAll('form').length, 0);
assert.equal(page.querySelectorAll('button').length, 0);
console.log(JSON.stringify({ ownerCreated: true, accepted: true, editedDraft: true,
  historicalHashRetained: true, viewerReadOnly: true, governedReadOnly: true, hostileContentIsText: true }));
