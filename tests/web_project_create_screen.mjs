// Runs the pages of kittrial-5bb.118 part 2 under Node against a real http_service on the
// endpoint backend (started by tests/test_http_project_create.py ScreenTests). No browser:
// tests/web_dom_shim.mjs gives dom.js and ui.js what they use. Prints one JSON object of
// observations; the Python test asserts on it.
//
// argv: shim URL, api.js URL, work.js URL, admin.js URL, setup.js URL, base URL, phase
// ("create" or "stopped"), then name=token=userId triples (admin, olive, carl).
const [shimUrl, apiUrl, workUrl, adminUrl, setupUrl, base, phase, ...people] = process.argv.slice(2);
await import(shimUrl);
const { createApi } = await import(apiUrl);
const work = await import(workUrl);
const admin = await import(adminUrl);
const setup = await import(setupUrl);

const settle = () => new Promise((resolve) => setTimeout(resolve, 400));
function person(spec) {
  const [name, token, id] = spec.split('=');
  const transport = async (method, path, headers, body) => {
    const response = await fetch(base + path, { method, headers: { ...headers, Authorization: 'Bearer ' + token }, body });
    const text = await response.text();
    let data = null;
    try { data = text ? JSON.parse(text) : null; } catch { data = null; }
    return { status: response.status, data };
  };
  const went = [];
  return [name, { api: createApi(transport), me: { id, superuser: name === 'admin', display_name: name, username: name },
    href: (path) => '#' + path, go: (path) => went.push(path), went, refreshProjects: async () => {}, projects: [] }];
}
const who = Object.fromEntries(people.map(person));
const out = {};
const texts = (root, tag) => root.all((e) => e.tagName === tag).map((e) => e.textContent);
const fieldError = (form, id) => { const e = form.querySelector('#' + id + '-error'); return e && !e.hidden ? e.textContent : ''; };

if (phase === 'create') {
  // 1. The superuser's page: nobody may create yet; allow olive two projects.
  const page = await admin.users(who.admin);
  await settle();
  const grants = () => page.querySelector('#project-grants');
  out.before = grants().textContent.includes('Nobody else may create projects yet.');
  const add = grants().querySelector('#grant-add');
  out.choices = add.all((e) => e.tagName === 'OPTION').map((o) => o.textContent).sort();
  add.querySelector('#g-account').value = who.olive.me.id;
  add.querySelector('#g-limit').value = '0';
  await add.dispatch('submit');
  out.badLimit = fieldError(add, 'g-limit');
  add.querySelector('#g-limit').value = '2';
  await add.dispatch('submit');
  await settle();
  out.holders = grants().all((e) => e.tagName === 'FORM' && e.attributes['data-grant']).map((f) => f.textContent);
  out.debug = grants().textContent.slice(0, 400);

  // 2. Olive now sees "Create a project"; carl sees nothing.
  const session = await who.olive.api.current();
  out.session = session.project_host_create;
  out.carlPanel = work.hostCreatePanel(who.carl, (await who.carl.api.current()).project_host_create);
  const panel = work.hostCreatePanel(who.olive, session.project_host_create);
  const form = panel.querySelector('form');
  out.panelText = panel.textContent.slice(0, 260);
  form.querySelector('#new_project_id').value = 'Not A Name';
  await form.dispatch('submit');
  out.badName = fieldError(form, 'new_project_id');
  form.querySelector('#new_project_id').value = 'alpha';
  form.querySelector('#new_project_name').value = 'Alpha project';
  await form.dispatch('submit');
  out.went = who.olive.went.slice();
  out.projects = (await who.olive.api.projects()).items.map((p) => [p.id, p.name, p.role]);

  // 3. The setup page: the owner writes the onboarding text there.
  const page2 = await setup.page(who.olive, { pid: 'alpha' });
  await settle();
  const step = () => page2.all((e) => e.tagName === 'LI').find((li) => li.attributes['data-step'] === 'onboarding');
  out.onboardingBefore = { state: step().attributes['data-state'], who: step().textContent.includes('A project owner, on this page') };
  const editor = () => step().querySelector('#onboarding-form').querySelector('form');
  editor().querySelector('#onboarding').value = '   ';
  await editor().dispatch('submit');
  out.emptyText = fieldError(editor(), 'onboarding');
  editor().querySelector('#onboarding').value = 'bad‮text';
  await editor().dispatch('submit');
  out.refusedText = fieldError(editor(), 'onboarding');
  editor().querySelector('#onboarding').value = 'Start with docs/README.md.';
  await editor().dispatch('submit');
  await settle();
  out.onboardingAfter = { state: step().attributes['data-state'], value: editor().querySelector('#onboarding').value,
    buttons: texts(editor(), 'BUTTON') };
  const guidance = page2.all((e) => e.tagName === 'LI').find((li) => li.attributes['data-step'] === 'guidance');
  out.guidanceHasNoForm = guidance.all((e) => e.tagName === 'FORM').length === 0;
} else {
  // The server stops half way through creating "beta".
  const session = await who.olive.api.current();
  const panel = work.hostCreatePanel(who.olive, session.project_host_create);
  const form = panel.querySelector('form');
  form.querySelector('#new_project_id').value = 'beta';
  await form.dispatch('submit');
  const banner = form.all((e) => e.tagName === 'DIV' && e.attributes.role === 'alert')[0];
  out.stopped = { shown: !banner.hidden, text: banner.textContent, went: who.olive.went.slice() };
  out.projects = (await who.olive.api.projects()).items.map((p) => p.id);
  // The superuser's Projects page lists it; olive's does not.
  const list = await work.incompleteCreations(who.admin);
  out.list = list ? { text: list.textContent, commands: texts(list, 'PRE'), cards: list.all((e) => e.attributes['data-creation']).map((e) => e.attributes['data-creation']) } : null;
  out.oliveList = await work.incompleteCreations(who.olive);
  // The held name counts: olive's limit of two is used up.
  form.querySelector('#new_project_id').value = 'gamma';
  await form.dispatch('submit');
  out.limit = fieldError(form, 'new_project_id');
  out.after = (await who.olive.api.current()).project_host_create;
}
// ASCII only on the way out: the page texts carry typographic quotes, and the reader of
// this output decodes it with the platform's default encoding.
console.log(JSON.stringify(out).replace(/[\u007f-￿]/g, (c) => '\\u' + c.charCodeAt(0).toString(16).padStart(4, '0')));
