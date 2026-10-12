// The superuser's Projects page (web/js/views/work.js directory) under Node with a fake API, run by
// tests/test_http_project_create.py UnregisteredScreenTests (kittrial-5bb.198). No browser: tests/web_dom_shim.mjs
// gives dom.js and ui.js what they use. Prints one JSON object of observations; the Python test asserts on it.
//
// argv: shim URL, work.js URL.
const [shimUrl, workUrl] = process.argv.slice(2);
await import(shimUrl);
const work = await import(workUrl);

const calls = [];
const api = {
  current: async () => ({ project_create: 'register' }),
  unconfirmedProjects: async () => ({ items: [] }),
  projectCreations: async () => ({
    items: [{ project: 'beta', state: 'incomplete', by: 'usr_0123456789abcdef', by_name: 'olive', stage: 'merge-slot',
      started_at: '2026-10-12T09:00:00Z', what: 'It stopped.', finish: 'admin.py finish-project beta', remove: null }],
    unregistered: [
      { project: 'gamma', state: 'created-unregistered', by: 'host', by_name: 'host', started_at: '2026-10-12T09:30:00Z',
        what: 'It is complete on the server and not registered here.' },
      { project: 'delta', state: 'created-unregistered', by: 'usr_0123456789abcdef', by_name: 'olive',
        started_at: '2026-10-12T10:00:00Z', what: 'It is complete on the server and not registered here.' }],
    server: null, host: 'available' }),
  registerProject: async (id, name) => { calls.push(['registerProject', id, name]); return { id }; },
};
const ctx = { api, me: { superuser: true }, projects: [], refreshProjects: async () => {}, href: (p) => '#' + p,
  went: [], go(path) { this.went.push(path); }, query: {}, render() {} };
const page = await work.directory(ctx);
const list = page.all((e) => e.attributes && e.attributes.id === 'incomplete-creations')[0];
const cards = Object.fromEntries(list.all((e) => e.attributes['data-creation']).map((card) => [card.attributes['data-creation'], {
  by: card.attributes['data-by'], text: card.textContent,
  buttons: card.all((e) => e.tagName === 'BUTTON').map((b) => b.textContent) }]));
const form = page.all((e) => e.tagName === 'FORM').find((f) => f.querySelector('#project_id'));
const out = { intro: list.textContent.slice(0, 400), cards, before: form.querySelector('#project_id').value };
const press = async (project) => {
  const card = list.all((e) => e.attributes['data-creation'] === project)[0];
  await card.all((e) => e.tagName === 'BUTTON')[0].dispatch('click');
};
// The button fills the form in and sends nothing.
await press('gamma');
out.filled = form.querySelector('#project_id').value;
out.callsAfterFill = calls.slice();
await press('delta');
out.refilled = form.querySelector('#project_id').value;
// The superuser registers it with the form's own button.
await form.dispatch('submit');
out.calls = calls.slice();
out.went = ctx.went.slice();
// Without a register form (no callback), the list offers no button.
const bare = await work.incompleteCreations(ctx);
out.bareButtons = bare.all((e) => e.tagName === 'BUTTON').length;
console.log(JSON.stringify(out));
