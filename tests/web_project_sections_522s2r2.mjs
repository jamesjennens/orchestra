// kittrial-5bb.522 slice 2 r2: the pre-review's shoulds and notes 1 and 3 (review-522s2, 2026-11-08).
// Runs web/js/app.js and web/js/views/project.js under Node with tests/web_dom_shim.mjs, a fake location,
// history and storage, against a stubbed transport that records every request.
//
// argv: shim URL, app.js URL, project.js URL, routes.js URL. Prints one JSON object of observations;
// tests/test_project_sections_522s2r2.py asserts on it.
const [shimUrl, appUrl, projectUrl, routesUrl] = process.argv.slice(2);
await import(shimUrl);

globalThis.location = { hash: '' };
globalThis.history = { replaceState(_s, _t, url) { location.hash = url; } };
const listeners = {};
globalThis.window = {
  isSecureContext: false,
  addEventListener(type, fn) { (listeners[type] = listeners[type] || []).push(fn); },
  scrollTo() {},
};
const settle = () => new Promise((resolve) => setTimeout(resolve, 20));
async function navigate(route) {
  location.hash = '#' + route;
  for (const fn of listeners.hashchange || []) await fn();
  await settle();
}
const hashNow = () => String(location.hash).replace(/^#/, '');
class Storage {
  constructor() { this.map = new Map(); }
  getItem(k) { return this.map.has(k) ? this.map.get(k) : null; }
  setItem(k, v) { this.map.set(k, String(v)); }
  removeItem(k) { this.map.delete(k); }
}

// alpha: owned by the signed-in person. beta: a contributor's. gamma: a coordinator who is not an owner.
// delta: an owner's project the server marks unusable.
const PROJECTS = [
  { id: 'alpha', name: 'Alpha', role: 'owner', can_coordinate: true, usable: true, archived: false },
  { id: 'beta', name: 'Beta', role: 'contributor', can_coordinate: false, usable: true, archived: false },
  { id: 'gamma', name: 'Gamma', role: 'coordinator', can_coordinate: true, usable: true, archived: false },
  { id: 'delta', name: 'Delta', role: 'owner', can_coordinate: true, usable: false, archived: false,
    unusable_reason: 'The project record is from an older kit.' },
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
  else if (/\/projects\/[a-z]+\/agents$/.test(p)) data = { items: [] };
  else if (/\/coordination$/.test(p)) data = { people: [], agents: [], web_rules: [], host_rules: { state: 'unknown', rules: [] } };
  else if (/\/switches$/.test(p)) data = { switches: [], items: [] };
  else if (/\/delegations$/.test(p)) data = { items: [], delegations: [] };
  else if (/\/claiming$/.test(p)) data = { claiming: 'open' };
  else data = { items: [] };
  if (data === null) return Promise.resolve({ status: 404, data: { error: { code: 'not_found', message: 'Not found' } } });
  return Promise.resolve({ status: 200, data });
}

const app = await import(appUrl);
const project = await import(projectUrl);
const routes = await import(routesUrl);
const { createApi } = await import(new URL('./api.js', appUrl).href);
const out = {};
const hasClass = (e, name) => String(e.className || '').split(/\s+/).includes(name);
const title = (page) => (page.all((e) => e.tagName === 'H1')[0] || { textContent: null }).textContent;
const ctxFor = () => ({ api: createApi(transport), href: (path) => '#' + path, go() {}, query: {}, render() {}, refreshProjects: async () => {}, me: { id: 'usr_alex' } });
// The projects a set of requests read: the id after /v1/projects/ (the list itself names none).
const projectsRead = (made) => [...new Set(made.map((c) => (c.match(/^GET \/v1\/projects\/([^/?]+)/) || [])[1]).filter(Boolean))].sort();

// -- Should 1: an unusable project's links --------------------------------------------------------------------
{
  const page = await project.overview(ctxFor(), { pid: 'delta' });
  await settle();
  out.unusableLinks = page.all((e) => e.tagName === 'A').map((a) => [a.attributes.href, a.textContent]);
}

// -- Should 2: the Who-coordinates line on People and on Agents -------------------------------------------------
// -- Should 3: the Settings lede for each role ------------------------------------------------------------------
// -- note 3: the walkthrough link on People, Agents and Settings ------------------------------------------------
out.coordinationLine = {};
out.walkthrough = {};
for (const [name, view] of [['people', project.people], ['agents', project.projectAgents], ['settings', project.settings]]) {
  const page = await view(ctxFor(), { pid: 'alpha' });
  await settle();
  const panel = page.all((e) => e.attributes && e.attributes['data-panel'] === 'coordination')[0];
  if (panel) out.coordinationLine[name] = panel.all((e) => e.tagName === 'SPAN' && hasClass(e, 'hide-narrow')).map((s) => s.textContent);
  out.walkthrough[name] = page.all((e) => e.tagName === 'A' && e.attributes['data-link'] === 'handover').map((a) => a.attributes.href);
}
out.settingsLede = {};
for (const pid of ['alpha', 'gamma', 'beta']) {
  const page = await project.settings(ctxFor(), { pid });
  await settle();
  const lede = page.all((e) => e.tagName === 'P' && hasClass(e, 'lede'))[0];
  const saves = page.all((e) => e.attributes && e.attributes['data-panel'] === 'claiming')
    .flatMap((panel) => panel.all((e) => e.tagName === 'BUTTON' && e.textContent === 'Save'));
  out.settingsLede[pid] = { lede: lede ? lede.textContent : null, claimingSave: saves.length };
}

// -- Should 4: a switch from each of Alpha's three sections to Beta and back, with Beta remembered first ----------
const storage = new Storage();
const KEY = routes.LAST_PROJECT_PREFIX + 'usr_alex';
const root = document.createElement('div');
document.body.appendChild(root);
// Started on a page without a project, so each section below is a fresh render.
location.hash = '#/agents';
const ctx = await app.start(root, { transport, storage });
await settle();
const toasts = () => document.body.all((e) => hasClass(e, 'toast')).map((t) => t.textContent);
const switchTo = async (pid) => {
  const s = root.querySelector('#project-switch');
  s.value = pid; await s.dispatch('change'); await settle();
};
out.switchRound = {};
for (const section of ['people', 'agents', 'settings']) {
  const steps = [];
  // Beta remembered first: what Alpha's page shows must come from the route, never from the remembered project.
  storage.setItem(KEY, 'beta');
  const toastsBefore = toasts().length;
  calls.length = 0;
  await navigate(`/p/alpha/${section}`);
  steps.push({ at: hashNow(), title: title(root.querySelector('#main')), read: projectsRead(calls) });
  calls.length = 0;
  await switchTo('beta');
  steps.push({ at: hashNow(), title: title(root.querySelector('#main')), read: projectsRead(calls) });
  calls.length = 0;
  await switchTo('alpha');
  steps.push({ at: hashNow(), title: title(root.querySelector('#main')), read: projectsRead(calls) });
  out.switchRound[section] = { steps, toasts: toasts().slice(toastsBefore) };
}

// -- note 1: the lost-session half of n2 --------------------------------------------------------------------------
// Alex's session is lost on Alex's page, and a second 401 arrives while nobody is signed in (another request of
// the page left). Alex signing in again lands back on that page; anyone else lands at home.
await navigate('/p/alpha/people');
ctx.sessionLost();
ctx.sessionLost();
await ctx.signedIn({ id: 'usr_alex', username: 'alex', display_name: 'Alex', superuser: false });
await settle();
out.lostTwiceThenSame = hashNow();
await navigate('/p/alpha/people');
ctx.sessionLost();
ctx.sessionLost();
await ctx.signedIn({ id: 'usr_bea', username: 'bea', display_name: 'Bea', superuser: false });
await settle();
out.lostTwiceThenOther = hashNow();

console.log(JSON.stringify(out));
