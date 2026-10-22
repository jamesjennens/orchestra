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
const transport = async (method, path, headers, body) => {
  inFlight++;
  calls.push({ method, path });
  try {
    const response = await fetch(base + path, { method,
      headers: { ...headers, Authorization: 'Bearer ' + token }, body });
    return { status: response.status, data: await response.json() };
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
console.log(JSON.stringify({ checked: true, unchecked: true, readOnly: true, target: true }));
