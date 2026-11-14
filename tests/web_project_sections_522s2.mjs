// kittrial-5bb.522 slice 2: "Members & settings" is split into the project's People, Agents and Settings
// sections. Runs web/js/views/project.js and web/js/app.js under Node with tests/web_dom_shim.mjs against a
// stubbed transport that records every request.
//
// argv: shim URL, app.js URL, project.js URL, handover.js URL, setup.js URL, routes.js URL, and optionally a
// project.js URL to measure instead (used once to measure the OLD settings page, see the Python test).
// Prints one JSON object of observations; tests/test_project_sections_522s2.py asserts on it.
const [shimUrl, appUrl, projectUrl, handoverUrl, setupUrl, routesUrl, measureUrl] = process.argv.slice(2);
await import(shimUrl);

globalThis.location = { hash: '' };
globalThis.history = { replaceState(_s, _t, url) { location.hash = url; } };
const listeners = {};
globalThis.window = {
  isSecureContext: false,
  addEventListener(type, fn) { (listeners[type] = listeners[type] || []).push(fn); },
  scrollTo() {},
};
async function navigate(route) {
  location.hash = '#' + route;
  for (const fn of listeners.hashchange || []) await fn();
  await settle();
}
const settle = () => new Promise((resolve) => setTimeout(resolve, 20));
class Storage {
  constructor() { this.map = new Map(); }
  getItem(k) { return this.map.has(k) ? this.map.get(k) : null; }
  setItem(k, v) { this.map.set(k, String(v)); }
  removeItem(k) { this.map.delete(k); }
}

// alpha: the signed-in person owns it. beta: a contributor.
const PROJECTS = [
  { id: 'alpha', name: 'Alpha', role: 'owner', can_coordinate: true, usable: true, archived: false },
  { id: 'beta', name: 'Beta', role: 'contributor', can_coordinate: false, usable: true, archived: false },
];
const calls = [];
function transport(method, path) {
  const [p] = path.split('?');
  calls.push(method + ' ' + p);
  let data = null;
  if (p === '/v1/sessions/current') data = { user: { id: 'usr_alex', username: 'alex', display_name: 'Alex', superuser: false }, csrf_token: 'tok' };
  else if (p === '/v1/projects') data = { items: PROJECTS };
  else if (/^\/v1\/projects\/[a-z]+$/.test(p)) data = PROJECTS.find((x) => x.id === p.split('/')[3]) || null;
  else if (/\/members$/.test(p)) data = { items: [{ user_id: 'usr_alex', username: 'alex', display_name: 'Alex', role: 'owner' }] };
  else if (/\/projects\/[a-z]+\/agents$/.test(p)) data = { items: [{ id: 'agt_1', name: 'Heron', owner: 'usr_alex', enabled: true, coordinator: null, release: null, credentials: [] }] };
  else if (/\/coordination$/.test(p)) data = { people: [], agents: [], web_rules: [], host_rules: { state: 'unknown', rules: [] } };
  else if (/\/credentials$/.test(p) || /\/audit$/.test(p)) data = { items: [] };
  else if (/\/switches$/.test(p)) data = { switches: [], items: [] };
  else if (/\/delegations$/.test(p)) data = { items: [], delegations: [] };
  else data = { items: [] };
  return Promise.resolve({ status: 200, data });
}

const { createApi } = await import(new URL('./api.js', appUrl).href);
const project = await import(measureUrl || projectUrl);
const app = await import(appUrl);
const handover = await import(handoverUrl);
const setup = await import(setupUrl);
const routes = await import(routesUrl);
const out = {};

const hasClass = (e, name) => String(e.className || '').split(/\s+/).includes(name);
const panels = (page) => [...new Set(page.all((e) => e.attributes && e.attributes['data-panel']).map((e) => e.attributes['data-panel']))].sort();
const headings = (page) => page.all((e) => e.tagName === 'H2').map((e) => e.textContent.trim());
const title = (page) => (page.all((e) => e.tagName === 'H1')[0] || { textContent: null }).textContent;
const ctxFor = () => ({ api: createApi(transport), href: (path) => '#' + path, go() {}, query: {}, render() {}, refreshProjects: async () => {}, me: { id: 'usr_alex' } });

async function sectionsOf(view, pid) {
  calls.length = 0;
  const page = await view(ctxFor(), { pid });
  await settle();
  return { page, calls: [...calls] };
}

// -- measuring the OLD page: the original settings view on its own, for the owner ------------------------------
if (measureUrl) {
  const old = await sectionsOf(project.settings, 'alpha');
  console.log(JSON.stringify({ oldSettingsCalls: [...new Set(old.calls)].sort() }));
  process.exit(0);
}

// -- each section for the owner --------------------------------------------------------------------------------
const owner = {};
for (const [name, view] of [['people', project.people], ['agents', project.projectAgents], ['settings', project.settings]]) {
  const { page, calls: made } = await sectionsOf(view, 'alpha');
  owner[name] = { title: title(page), panels: panels(page), headings: headings(page), calls: [...new Set(made)].sort() };
}
out.owner = owner;
out.ownerUnionCalls = [...new Set([...owner.people.calls, ...owner.agents.calls, ...owner.settings.calls])].sort();

// -- a person who is not an owner, on Agents -------------------------------------------------------------------
{
  const { page, calls: made } = await sectionsOf(project.projectAgents, 'beta');
  const note = page.all((e) => e.attributes && e.attributes['data-note'] === 'agents-owner-only')[0];
  out.nonOwnerAgents = {
    title: title(page), panels: panels(page), calls: made,
    sentence: note ? note.textContent : null,
    link: note ? note.all((e) => e.tagName === 'A').map((a) => a.attributes.href) : [],
  };
}

// -- the app: routes and the rail --------------------------------------------------------------------------------
const root = document.createElement('div');
document.body.appendChild(root);
location.hash = '#/p/alpha/people';
await app.start(root, { transport, storage: new Storage() });
await settle();
const navLinks = () => root.all((e) => e.tagName === 'A' && hasClass(e, 'nav-link')).map((a) => [a.attributes.href, a.textContent.trim()]);
out.rail = navLinks().filter(([href]) => href.startsWith('#/p/alpha'));
const mainTitle = () => title(root.querySelector('#main'));
out.routes = {};
for (const route of ['/p/alpha/people', '/p/alpha/agents', '/p/alpha/settings']) {
  await navigate(route);
  out.routes[route] = { title: mainTitle(), panels: panels(root.querySelector('#main')) };
}
out.routeNames = ['/p/alpha/people', '/p/alpha/agents', '/p/alpha/settings', '/agents'].map((r) => (routes.matchRoute(r) || {}).name);

// -- the links ---------------------------------------------------------------------------------------------------
out.fixRoutes = {
  members: handover.fixRoute('members', 'p1'), settings: handover.fixRoute('settings', 'p1'),
  myAgents: handover.fixRoute('my-agents', 'p1'), agentSetup: handover.fixRoute('agent-setup', 'p1'),
  note: handover.fixRoute('handover-note', 'p1'),
};
out.setupMembers = setup.stepRoute('members', 'p1');

console.log(JSON.stringify(out));
