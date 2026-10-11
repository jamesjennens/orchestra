// The actual page and HTTP adapter against a disposable loopback service.
const [shim, apiUrl, viewUrl, base, token, owner] = process.argv.slice(2);
await import(shim);
const { createApi } = await import(apiUrl);
const view = await import(viewUrl);
const reads = [];
const requestedLimits = [];
let pending = 0;
const api = createApi(async (method, path, headers, body) => {
  reads.push(path);
  pending += 1;
  try {
    const response = await fetch(base + path, { method, headers: { ...headers, Authorization: 'Bearer ' + token }, body });
    return { status: response.status, data: await response.json() };
  } finally { pending -= 1; }
});
const ctx = { api: { ...api, agents: (params) => {
  requestedLimits.push(params.limit);
  return api.agents({ ...params, limit: 2 });
} },
  me: { id: owner, superuser: true }, projects: [], href: (path) => '#' + path, go() {} };
const page = await view.list(ctx);
async function settle() {
  let quiet = 0;
  for (let i = 0; i < 600 && quiet < 6; i += 1) {
    await new Promise((resolve) => setTimeout(resolve, 25));
    quiet = pending ? 0 : quiet + 1;
  }
}
const ids = () => page.all((element) => element.attributes['data-agent']).map((element) => element.attributes['data-agent']);
async function click(label) {
  const control = page.all((element) => element.tagName === 'BUTTON' && element.textContent === label)[0];
  if (!control) throw new Error('Missing page control: ' + label);
  await control.dispatch('click');
  await settle();
}
await settle();
const first = ids();
await click('Next agents');
const second = ids();
await click('Previous agents');
const back = ids();
await click('Next agents');
const filter = page.all((element) => element.attributes['data-filter'] === 'unconfirmed-scopes')[0];
filter.checked = true;
await filter.dispatch('change');
await settle();
const filteredFirst = ids();
await click('Next agents');
const filteredSecond = ids();
console.log(JSON.stringify({ first, second, back, filteredFirst, filteredSecond, reads, requestedLimits }));
