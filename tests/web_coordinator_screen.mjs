// Runs the settings page of web/js/views/project.js under Node against a real http_service
// (started by tests/test_coordinator_grant.py). No browser: tests/web_dom_shim.mjs gives dom.js
// and ui.js what they use. Prints one JSON object of observations; the Python test asserts on it.
//
// argv: shim URL, api.js URL, project.js URL, base URL, project id, then name=token pairs.
const [shimUrl, apiUrl, projectUrl, base, project, ...people] = process.argv.slice(2);
await import(shimUrl);
const { createApi } = await import(apiUrl);
const view = await import(projectUrl);

function person(spec) {
  const [name, token] = spec.split('=');
  const transport = async (method, path, headers, body) => {
    const response = await fetch(base + path, { method, headers: { ...headers, Authorization: 'Bearer ' + token }, body });
    const text = await response.text();
    let data = null;
    try { data = text ? JSON.parse(text) : null; } catch { data = null; }
    return { status: response.status, data };
  };
  return [name, { api: createApi(transport), href: (path) => '#' + path, go() {}, query: {}, render() {}, refreshProjects: async () => {} }];
}
const who = Object.fromEntries(people.map(person));
const out = {};
const settle = () => new Promise((resolve) => setTimeout(resolve, 400));
const panel = (page) => page.all((e) => e.tagName === 'SECTION' && e.attributes['data-panel'] === 'project-agents')[0];
const rows = (page) => panel(page).all((e) => e.tagName === 'TR' && e.attributes['data-agent']).map((tr) => ({
  agent: tr.attributes['data-agent'], coordinator: tr.attributes['data-coordinator'], text: tr.textContent,
  buttons: tr.all((e) => e.tagName === 'BUTTON').map((b) => b.textContent),
}));
const press = async (page, agent, label) => {
  const row = panel(page).all((e) => e.tagName === 'TR' && e.attributes['data-agent'] === agent)[0];
  await row.all((e) => e.tagName === 'BUTTON' && e.textContent === label)[0].dispatch('click');
  await settle();
};

// 1. An owner: the members list offers the role, and the agents of the project are listed.
{
  const page = await view.settings(who.owner, { pid: project });
  await settle();
  out.roles = [...new Set(page.all((e) => e.tagName === 'OPTION').map((o) => o.textContent))];
  out.head = panel(page).textContent.slice(0, 160);
  out.before = rows(page);
  const [coordinating, working] = [out.before.find((r) => r.text.includes('Heron')).agent, out.before.find((r) => r.text.includes('Wren')).agent];
  // 2. Granted: the row says so and offers the removal.
  await press(page, coordinating, 'Make coordinator');
  out.granted = rows(page).find((r) => r.agent === coordinating);
  // 3. An agent whose account is only a contributor: refused by the server, and the row is as it was.
  await press(page, working, 'Make coordinator');
  out.refused = rows(page).find((r) => r.agent === working);
  // 4. Removed again.
  await press(page, coordinating, 'Remove grant');
  out.removed = rows(page).find((r) => r.agent === coordinating);
}
// 5. A coordinator who is not an owner sees no such panel (the server would answer 403).
{
  const page = await view.settings(who.coordinator, { pid: project });
  await settle();
  out.coordinatorSeesPanel = Boolean(panel(page));
}
out.can = { approveOwner: view.canApprove({ role: 'owner' }), approveCoordinator: view.canApprove({ role: 'coordinator' }),
  approveContributor: view.canApprove({ role: 'contributor' }), ownerCoordinator: view.isOwner({ role: 'coordinator' }),
  writeCoordinator: view.canWrite({ role: 'coordinator' }) };
console.log(JSON.stringify(out));
