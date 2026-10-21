// Runs the Agents page of web/js/views/agents.js under Node against a real http_service (started by
// tests/test_http_agents.py) for kittrial-5bb.355: an agent the viewer does NOT own lists only the
// projects its OWNER may be granted, and greys out the rest with the sentence that says why.
// No browser: tests/web_dom_shim.mjs gives dom.js and ui.js what they use.
// Prints one JSON object of observations; the Python test asserts on it.
//
// argv: shim URL, api.js URL, agents.js URL, base URL, Alpha id, Beta id, agent id,
//       then name=userId=token triples (alex the owner, admin the superuser).
const [shimUrl, apiUrl, agentsUrl, base, alpha, beta, agentId, ...people] = process.argv.slice(2);
const { Element } = await import(shimUrl);
Element.prototype.showModal = function showModal() { this.open = true; };
Element.prototype.close = function close() { this.open = false; return this.dispatch('close'); };
globalThis.window = globalThis.window || { isSecureContext: false };
globalThis.location = globalThis.location || { origin: base };
const { createApi } = await import(apiUrl);
const view = await import(agentsUrl);

const sent = [];
let inFlight = 0;
const specs = Object.fromEntries(people.map((spec) => {
  const [name, id, token] = spec.split('=');
  return [name, { id, token }];
}));
function person(name, projects) {
  const { id, token } = specs[name];
  const transport = async (method, path, headers, body) => {
    if (method !== 'GET') sent.push([name, method, path.replace(/agent_[0-9a-f]+/, 'AGENT').replace(/cred_[0-9a-f]+/, 'CRED'), body ? JSON.parse(body) : null]);
    inFlight += 1;
    let response, text;
    try {
      response = await fetch(base + path, { method, headers: { ...headers, Authorization: 'Bearer ' + token }, body });
      text = await response.text();
    } finally { inFlight -= 1; }
    let data = null;
    try { data = text ? JSON.parse(text) : null; } catch { data = null; }
    return { status: response.status, data };
  };
  return { api: createApi(transport), href: (path) => '#' + path, go() {}, query: {}, render() {}, refreshProjects: async () => {},
    me: { id, superuser: name === 'admin' }, projects };
}
const who = {
  // Alex is a contributor on Alpha and cannot see Beta at all.
  alex: person('alex', [{ id: alpha, name: 'Alpha', role: 'contributor' }]),
  // The administrator sees both projects and may manage their members.
  admin: person('admin', [{ id: alpha, name: 'Alpha', role: 'owner' }, { id: beta, name: 'Beta', role: 'owner' }]),
};
const out = { sent };
const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
// Until no request has been in flight for a moment (at most 30 seconds).
async function settle() {
  let quiet = 0;
  for (let i = 0; i < 1200 && quiet < 6; i += 1) { await pause(25); quiet = inFlight === 0 ? quiet + 1 : 0; }
}
const block = (page, name) => page.all((e) => e.tagName === 'DIV' && e.attributes['data-agent'])
  .find((e) => e.all((x) => x.tagName === 'H3')[0].textContent === name);
const projectForm = (page) => block(page, 'Kestrel').all((e) => e.tagName === 'FORM')
  .find((f) => f.all((x) => x.tagName === 'BUTTON' && x.textContent === 'Save projects')[0]);
function projectsSeen(page) {
  const form = projectForm(page);
  const root = block(page, 'Kestrel');
  return {
    tickable: form.all((e) => e.tagName === 'INPUT' && e.attributes.name === 'project')
      .map((i) => [i.value, Boolean(i.checked), Boolean(i.disabled)]),
    blocked: root.all((e) => e.attributes['data-owner-not-member'] !== undefined).map((e) => {
      const input = e.all((x) => x.tagName === 'INPUT')[0];
      return { id: e.attributes['data-owner-not-member'], text: e.textContent, disabled: Boolean(input && input.disabled),
        links: e.all((x) => x.tagName === 'A').map((a) => a.attributes.href) };
    }),
  };
}
async function open(ctx) { const page = await view.list(ctx); await settle(); return page; }
async function addMember(project, userId, role) {
  const response = await fetch(base + '/v1/projects/' + project + '/members/' + userId, {
    method: 'PUT', headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + specs.admin.token },
    body: JSON.stringify({ role }) });
  return response.status;
}

// 1. The administrator opens Alex's agent. Alpha is tickable; Beta is greyed with the sentence.
{
  const page = await open(who.admin);
  out.adminBefore = projectsSeen(page);
  out.adminBefore.ownerGrantable = (await who.admin.api.agent(agentId)).owner_grantable_projects;
}
// 2. The owner's own page offers Alpha and knows nothing of Beta: the owner is a member of Alpha only.
{
  const page = await open(who.alex);
  out.ownerBefore = projectsSeen(page);
  const mine = await who.alex.api.agent(agentId);
  out.ownerBefore.ownerGrantable = mine.owner_grantable_projects;
  out.ownerBefore.namesBeta = JSON.stringify(mine).includes(beta);
}
// 3. Add the owner to Beta (as the project's owner does under Members), then grant Beta from the page.
out.addedMember = await addMember(beta, specs.alex.id, 'contributor');
{
  const page = await open(who.admin);
  out.adminAfter = projectsSeen(page);
  const form = projectForm(page);
  const checkbox = form.all((e) => e.tagName === 'INPUT' && e.attributes.name === 'project' && e.value === beta)[0];
  checkbox.checked = true;
  await form.dispatch('submit');
  await settle();
  out.savedProjects = (await who.admin.api.agent(agentId)).projects;
  out.patch = sent.filter((row) => row[1] === 'PATCH' && row[2].includes('AGENT')).map((row) => row[3]);
}

console.log(JSON.stringify(out).replace(/[^\x00-\x7f]/g, (c) => '\\u' + c.charCodeAt(0).toString(16).padStart(4, '0')));
