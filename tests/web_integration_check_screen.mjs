import assert from 'node:assert/strict';
const [shimUrl, apiUrl, viewUrl, base, pid, token, tid] = process.argv.slice(2);
await import(shimUrl);
const storage = new Map();
globalThis.sessionStorage = { getItem: k => storage.get(k) ?? null,
  setItem: (k, v) => storage.set(k, String(v)), removeItem: k => storage.delete(k) };
const { createApi } = await import(apiUrl);
const { lifecyclePanel } = await import(viewUrl);
let inFlight = 0;
const calls = [];
// One-shot server behaviours for the two panel refusal/rendering cases below.
// forcedResponse returns a whole reply instead of fetching; transform rewrites a
// real reply. Both are null during the ordinary read.
let forcedResponse = null;
let transform = null;
const transport = async (method, path, headers, body) => {
  inFlight++;
  calls.push({ method, path });
  try {
    const forced = forcedResponse ? forcedResponse(method, path) : null;
    if (forced) return forced;
    const response = await fetch(base + path, { method,
      headers: { ...headers, Authorization: 'Bearer ' + token }, body });
    let data = await response.json();
    if (transform) data = transform(path, data);
    return { status: response.status, data };
  } finally { inFlight--; }
};
async function settle() {
  for (let i = 0; i < 200; i++) {
    await new Promise(r => setTimeout(r, 20));
    if (!inFlight) { await new Promise(r => setTimeout(r, 30)); if (!inFlight) return; }
  }
  throw new Error('Lifecycle page did not settle');
}
const project = { id: pid, role: 'superuser', archived: false };
const task = { id: tid };
const api = createApi(transport);
const panel = lifecyclePanel({ api, href: path => path }, project, task);
document.body.append(panel);
await settle();
const button = Array.from(panel.querySelectorAll('button'))
  .find(node => node.textContent === 'Read integration check');
assert(button, 'the task page has no integration check control');
const target = Array.from(panel.querySelectorAll('input'))
  .find(node => node.getAttribute('aria-label') === 'Integration target (optional)');
assert(target, 'the integration check has no target input');
target.value = 'main';
await button.dispatch('click');
await settle();
const text = panel.textContent;
assert(text.includes('Checks the server can make'));
assert(text.includes('Not checked by the server'));
assert(text.includes('the server has no code-host access'));
assert(text.includes('Writes still owed'));
assert(text.includes('the server never performs the merge') || text.includes('The server never performs the merge'));
assert(!calls.some(call => call.method !== 'GET'), JSON.stringify(calls));
assert(calls.some(call => call.path.endsWith('/integration-check?target=main')), JSON.stringify(calls));

// A missing task is a definite 404 with the server's sentence, not the generic
// "this server has no integration check" text (kittrial-5bb.374 review item 3a).
forcedResponse = (method, path) => path.includes('/integration-check')
  ? { status: 404, data: { error: { code: 'not_found', message: 'Lifecycle task does not exist' } } }
  : null;
await button.dispatch('click');
await settle();
const missingTask = panel.textContent;
assert(!missingTask.includes('This server has no integration check'), missingTask);
assert(missingTask.includes('Lifecycle task does not exist'), missingTask);
forcedResponse = null;

// A server string carrying markup renders as text, never as DOM (review item 3b).
const markup = '<b>server markup</b><img src=x onerror=alert(1)>';
transform = (path, data) => {
  if (!path.includes('/integration-check')) return data;
  const copy = JSON.parse(JSON.stringify(data));
  copy.checks[0].reason = markup;
  return copy;
};
await button.dispatch('click');
await settle();
assert(!panel.querySelector('b'), 'server markup became a DOM element');
assert(!panel.querySelector('img'), 'server markup became an image');
assert(panel.textContent.includes(markup), panel.textContent);
transform = null;

console.log(JSON.stringify({ checked: true, unchecked: true, readOnly: true, target: true,
  missingTask: true, markupText: true }));
