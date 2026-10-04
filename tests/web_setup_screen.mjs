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
  out.noHintForContributor = await setup.setupHint(who.contributor, { id: project, role: 'contributor' });
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
console.log(JSON.stringify(out));
