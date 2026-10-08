// Runs web/js/views/setup.js under Node against a real http_service (started by
// tests/test_project_setup.py SetupScreenTests). No browser: tests/web_dom_shim.mjs gives
// dom.js and ui.js what they use. Prints one JSON object of observations; the Python
// test asserts on it.
//
// argv: shim URL, api.js URL, setup.js URL, base URL, project id, then name=token pairs.
const [shimUrl, apiUrl, setupUrl, base, project, ...people] = process.argv.slice(2);
await import(shimUrl);
const { createApi } = await import(apiUrl);
const setup = await import(setupUrl);

function person(spec) {
  const [name, token] = spec.split('=');
  const transport = async (method, path, headers, body) => {
    const response = await fetch(base + path, { method, headers: { ...headers, Authorization: 'Bearer ' + token }, body });
    const text = await response.text();
    let data = null;
    try { data = text ? JSON.parse(text) : null; } catch { data = null; }
    return { status: response.status, data };
  };
  return [name, { api: createApi(transport), href: (path) => '#' + path, go() {} }];
}
const who = Object.fromEntries(people.map(person));
const out = {};
const steps = (root) => root.all((e) => e.tagName === 'LI').map((li) => ({
  id: li.attributes['data-step'], state: li.attributes['data-state'],
  chip: li.all((e) => e.tagName === 'SPAN' && e.className.startsWith('chip'))[0].textContent,
  text: li.textContent,
  links: li.all((e) => e.tagName === 'A').map((a) => a.attributes.href),
  linkTexts: li.all((e) => e.tagName === 'A').map((a) => a.textContent),
  commands: li.all((e) => e.tagName === 'PRE').map((pre) => pre.textContent),
}));

// 1. The owner's page: seven steps, their states, where each is done.
{
  const ctx = who.owner;
  const page = await setup.page(ctx, { pid: project });
  out.first = { steps: steps(page), head: page.textContent.slice(0, 400) };

  // 2. The repository form: a refusal from the server is shown at the field; a good
  //    value is recorded and the step redraws as done.
  const form = () => page.all((e) => e.tagName === 'FORM')[0];
  form().querySelector('#repository').value = 'https://user:secret@git.example/team/alpha.git';
  await form().dispatch('submit');
  out.refused = { error: form().querySelector('#repository-error').textContent,
    state: steps(page).find((s) => s.id === 'repository').state };
  form().querySelector('#repository').value = '';
  await form().dispatch('submit');
  out.blank = form().querySelector('#repository-error').textContent;
  form().querySelector('#repository').value = 'git@git.example:team/alpha.git';
  await form().dispatch('submit');
  const after = steps(page).find((s) => s.id === 'repository');
  out.recorded = { state: after.state, text: after.text, buttons: page.all((e) => e.tagName === 'BUTTON').map((b) => b.textContent) };
  out.project = (await ctx.api.project(project)).repository;

  // 3. The line on the project page while steps are left.
  const hint = await setup.setupHint(ctx, { id: project, role: 'owner' });
  out.hint = hint ? { text: hint.textContent, link: hint.all((e) => e.tagName === 'A')[0].attributes.href } : null;
  // A contributor gets no line, and the page does not even ask the server for one.
  let asked = 0;
  const counting = { ...who.contributor, api: { ...who.contributor.api, projectSetup: (...args) => { asked += 1; return who.contributor.api.projectSetup(...args); } } };
  out.noHintForContributor = await setup.setupHint(counting, { id: project, role: 'contributor' });
  out.contributorAsked = asked;
  out.noHintWhenArchived = await setup.setupHint(ctx, { id: project, role: 'owner', archived: true });
}

// 4. A contributor who opens the address: one sentence, no steps.
{
  const page = await setup.page(who.contributor, { pid: project });
  out.contributor = { text: page.textContent, steps: steps(page).length };
}

out.routes = { members: setup.stepRoute('members', 'p1'), task: setup.stepRoute('first-task', 'p1'),
  agent: setup.stepRoute('agent', 'p1'), guidance: setup.stepRoute('guidance', 'p1') };
out.summary = [setup.summary({ remaining: 0 }), setup.summary({ remaining: 1 }), setup.summary({ remaining: 4 })];
// A step the server could not check is said, never hidden behind "nothing is left".
out.unchecked = [setup.summary({ remaining: 0, unchecked: 1 }), setup.summary({ remaining: 0, unchecked: 2 }),
  setup.summary({ remaining: 2, unchecked: 1 })];
// The hint on the project page appears for an unchecked step too. The API is a stand-in here.
{
  const fake = (data) => ({ api: { projectSetup: async () => data }, href: (path) => '#' + path });
  const hint = await setup.setupHint(fake({ remaining: 0, unchecked: 1 }), { id: 'p1', role: 'owner' });
  out.uncheckedHint = hint ? hint.textContent : null;
  out.noHintWhenAllDone = await setup.setupHint(fake({ remaining: 0, unchecked: 0 }), { id: 'p1', role: 'owner' });
}
// A value that begins like a token is shown with its warning, on the repository step.
{
  const ctx = who.owner;
  await ctx.api.setRepository(project, 'ssh://ghp_0123456789abcdef@git.example/team/alpha.git');
  const page = await setup.page(ctx, { pid: project });
  const step = page.all((e) => e.tagName === 'LI').find((li) => li.attributes['data-step'] === 'repository');
  out.warning = step.all((e) => e.tagName === 'DIV' && e.attributes['data-warning'] === 'repository').map((e) => e.textContent);
  await ctx.api.setRepository(project, 'git@git.example:team/alpha.git');
  const again = await setup.page(ctx, { pid: project });
  out.noWarning = again.all((e) => e.tagName === 'DIV' && e.attributes['data-warning']).length;
}
// kittrial-5bb.200: what a step gives to copy, each entry under its own label. The API is a stand-in.
{
  const data = (step) => ({ project: { id: 'p1', name: 'P', repository: null }, remaining: 1, unchecked: 0, host: 'available',
    steps: [{ id: 'backup', title: 'Backup', state: 'todo', detail: 'd', who: 'operator', who_text: 'An operator', link: null,
      note: 'n', ...step }] });
  const fake = (step) => ({ api: { projectSetup: async () => data(step) }, href: (path) => '#' + path, go() {} });
  const shown = async (step) => {
    const page = await setup.page(fake(step), { pid: 'p1' });
    const li = page.all((e) => e.tagName === 'LI')[0];
    const blocks = li.all((e) => e.tagName === 'DIV' && e.attributes['data-command']);
    return { kinds: blocks.map((b) => b.attributes['data-command']),
      texts: li.all((e) => e.tagName === 'PRE').map((pre) => pre.textContent),
      labels: blocks.map((b) => b.all((e) => e.tagName === 'STRONG')[0].textContent),
      buttons: blocks.map((b) => b.all((e) => e.tagName === 'BUTTON')[0].textContent), text: li.textContent };
  };
  const run = '/opt/py/bin/python3 /opt/kit/admin.py --root /srv/rt backup --all';
  out.labelled = await shown({ command: run, commands: [
    { kind: 'shell', label: 'Run a backup now (a shell command)', text: run, note: 'It does not schedule anything.', replace: [] },
    { kind: 'unit-line', label: 'The line for a schedule (not a shell command)', text: 'ExecStart=' + run,
      note: 'It goes in the [Service] section of a unit.', replace: [] },
    { kind: 'shell', label: 'Check', text: '/opt/py/bin/python3 /opt/kit/admin.py --root /srv/rt backup-status --require-complete', note: null, replace: [] }] });
  const older = await shown({ command: 'admin.py set-guidance p1 --actor OPERATOR --file FILE' });
  out.older = { kinds: older.kinds, texts: older.texts, buttons: older.buttons };
  const none = await shown({ command: null, commands: [] });
  out.none = { kinds: none.kinds, texts: none.texts, buttons: none.buttons };
}
console.log(JSON.stringify(out));
