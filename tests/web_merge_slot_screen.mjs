const [shimUrl, apiUrl, viewUrl, base, project, token, task] = process.argv.slice(2);
const { Element } = await import(shimUrl);
Element.prototype.close = function () {};
Element.prototype.showModal = function () {
  queueMicrotask(() => this.querySelectorAll('button').at(-1).dispatch('click'));
};
const { createApi } = await import(apiUrl);
const { mergeSlotPanel } = await import(viewUrl);
let inFlight = 0;
const api = createApi(async (method, path, headers, body) => {
  inFlight++;
  try {
    const response = await fetch(base + path, { method, headers: { ...headers, Authorization: 'Bearer ' + token }, body });
    return { status: response.status, data: await response.json() };
  } finally { inFlight--; }
});
async function settle() {
  let quiet = 0;
  for (let i = 0; i < 1000; i++) {
    await new Promise((resolve) => setTimeout(resolve, 20));
    quiet = inFlight ? 0 : quiet + 1;
    if (quiet >= 3) return;
  }
  throw new Error('The real slot page did not settle');
}
const panel = mergeSlotPanel({ api }, { id: project, role: 'superuser', archived: false });
document.body.append(panel);await settle();
const before = panel.textContent;
const inputs = panel.querySelectorAll('input');inputs[0].value = task;inputs[1].value = '<img src=x onerror=throw-new-Error()>';
const button = (name) => panel.querySelectorAll('button').find((node) => node.textContent === name);
await button('Take slot').dispatch('click');await settle();
const held = panel.textContent;
const escaped = !panel.querySelector('img');
const releaseEnabled = !button('Release slot').disabled;
await button('Release slot').dispatch('click');await settle();
console.log(JSON.stringify({ before, held, escaped, releaseEnabled, after: panel.textContent,
  slot: await api.mergeSlot(project) }));
