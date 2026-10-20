import assert from 'node:assert/strict';
const [shimUrl, apiUrl, viewUrl, base, pid, token] = process.argv.slice(2);
const { Element } = await import(shimUrl);
// DOM event currentTarget is real browser behavior; this local shim adapter supplies it.
Element.prototype.dispatch = async function (type) {
  const event = { type, target: this, currentTarget: this, preventDefault() {} };
  for (const handler of this.listeners[type] || []) await handler(event);
};
Element.prototype.close = function () {};
Element.prototype.showModal = function () { queueMicrotask(() => Array.from(this.querySelectorAll('button')).at(-1).dispatch('click')); };
const { createApi } = await import(apiUrl);
const { guidancePanel } = await import(viewUrl);
const calls = []; let inFlight = 0, loseAck = true;
const api = createApi(async (method, path, headers, body) => {
  inFlight++; calls.push({ method, path, headers: { ...headers }, body });
  try {
    const response = await fetch(base + path, { method, headers: { ...headers, Authorization: 'Bearer ' + token }, body });
    const data = await response.json();
    if (loseAck && method === 'POST' && path.endsWith('/guidance/ack')) {
      loseAck = false; return { status: 0, data: { error: { code: 'network', message: 'Test loses the committed acknowledgement response' } } };
    }
    return { status: response.status, data };
  } finally { inFlight--; }
});
async function settle() {
  let quiet = 0;
  for (let i = 0; i < 1000; i++) {
    await new Promise(resolve => setTimeout(resolve, 20));
    quiet = inFlight ? 0 : quiet + 1; if (quiet >= 3) return;
  }
  throw new Error('Guidance page did not settle');
}
const panel = guidancePanel({ api }, { id: pid, role: 'credential', archived: false });
document.body.append(panel); await settle();
const initial = panel.textContent;
const initialWrites = calls.filter(x => x.method !== 'GET').length;
assert.equal(initialWrites, 0, 'Opening guidance must not write or acknowledge');
const textareas = panel.querySelectorAll('textarea');
if (textareas.length !== 2) throw new Error('Server-authorized coordinator controls missing');
textareas[0].value = 'Unsaved guidance draft'; textareas[1].value = 'Unsaved onboarding draft';
const button = name => Array.from(panel.querySelectorAll('button')).find(node => node.textContent === name);
await button('Acknowledge displayed version').dispatch('click'); await settle();
const uncertain = panel.textContent;
await button('Retry the same request').dispatch('click'); await settle();
const afterRetry = panel.textContent;
const draftsRetained = textareas[0].value === 'Unsaved guidance draft' && textareas[1].value === 'Unsaved onboarding draft'
  && panel.querySelectorAll('textarea')[0] === textareas[0] && panel.querySelectorAll('textarea')[1] === textareas[1];
await button('Who acknowledged guidance?').dispatch('click'); await settle();
const ackCalls = calls.filter(x => x.method === 'POST' && x.path.endsWith('/guidance/ack'));
console.log(JSON.stringify({ initial, initialWrites, uncertain, afterRetry, roster: panel.textContent,
  draftsRetained, escaped: !panel.querySelector('img') && !panel.querySelector('script'), ackCalls,
  state: await api.guidance(pid) }));
