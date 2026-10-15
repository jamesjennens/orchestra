// Actual pinned-native HTTP backend; DOM and tab storage are modeled.
import './web_dom_shim.mjs';
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
const [apiUrl, panelUrl, base, pid, ownerToken, viewerToken, requirementId] = process.argv.slice(2);
const { createApi } = await import(apiUrl);
const { baselinePanel } = await import(panelUrl);
const storage = new Map();
globalThis.sessionStorage = { getItem: k => storage.get(k) ?? null, setItem: (k, v) => storage.set(k, v), removeItem: k => storage.delete(k) };
const transport = token => async (method, path, headers, body) => {
  const result = await fetch(base + path, { method, headers: { ...headers, Authorization: 'Bearer ' + token }, body });
  return { status: result.status, data: result.status === 204 ? null : await result.json() };
};
const context = token => ({ api: createApi(transport(token)), href: p => '#' + p, go() {}, setDirty() {} });
const owner = context(ownerToken), viewer = context(viewerToken);
const until = async test => {
  for (let i = 0; i < 1000; i++) { if (test()) return; await new Promise(resolve => setTimeout(resolve, 10)); }
  assert.fail('Native baseline panel did not reach its bounded ready state');
};
const page = baselinePanel(owner, pid, true);
await until(() => page.querySelector('select').disabled === false);
const form = page.querySelector('form'); assert.ok(form);
form.querySelector('#baseline-name').value = 'Node-1';
form.querySelectorAll('input').find(input => input.attributes.type === 'checkbox').checked = true;
await form.dispatch('submit');
assert.ok(page.textContent.includes('Baseline cut.'), page.textContent);
assert.equal((await owner.api.baseline(pid, 'node-1')).name, 'node-1');
const read = baselinePanel(viewer, pid, false);
await until(() => read.querySelector('select').disabled === false);
assert.equal(read.querySelectorAll('form').length, 0);
assert.equal(read.querySelectorAll('button').some(button => button.textContent === 'Cut baseline'), false);
read.querySelector('select').value = 'native-1'; await read.querySelector('select').dispatch('change');
const exportButton = read.querySelectorAll('button').find(button => button.textContent === 'Export Markdown');
await exportButton.dispatch('click');
assert.ok(read.textContent.includes('Pending - not accepted'));
assert.ok(read.textContent.includes('&lt;script&gt;'));
assert.equal(read.querySelectorAll('script').length, 0);
const document = await viewer.api.exportBrd(pid, 'native-1');
assert.equal(createHash('sha256').update(document.markdown, 'utf8').digest('hex'), document.sha256);
await read.querySelectorAll('button').find(button => button.textContent === 'Changes since baseline').dispatch('click');
assert.ok(read.textContent.includes('edited: ' + requirementId), read.textContent);
read.querySelector('select').value = 'does-not-exist'; await read.querySelector('select').dispatch('change');
await exportButton.dispatch('click');
assert.equal(read.querySelectorAll('pre').length, 0);
assert.ok(!read.textContent.includes('Original <script>wording</script>'));
console.log(JSON.stringify({ actualNativeOwnerCut: true, actualNativeViewerExport: true,
  exactUtf8Hash: true, actualNativeComparison: true, noSelectedFallback: true, escapedText: true }));
