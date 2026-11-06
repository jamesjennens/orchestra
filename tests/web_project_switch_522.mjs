// kittrial-5bb.522 slice 1: the project switch is orthogonal to the other menu entries. Runs web/js/app.js under
// Node with tests/web_dom_shim.mjs, a fake location/history and a fake storage, against a stubbed transport.
//
// argv: shim URL, app.js URL, routes.js URL, project.js URL. Prints one JSON object of observations; the Python
// test (tests/test_project_switch_522.py) asserts on it.
const [shimUrl, appUrl, routesUrl, projectUrl] = process.argv.slice(2);
await import(shimUrl);

// -- a browser's location, history and window, as far as app.js uses them ------------------------------------
const hist = { replaced: 0, pushed: 0 };
globalThis.location = { hash: '' };
globalThis.history = { replaceState(_s, _t, url) { hist.replaced += 1; location.hash = url; } };
const listeners = {};
globalThis.window = {
  isSecureContext: false,
  addEventListener(type, fn) { (listeners[type] = listeners[type] || []).push(fn); },
  scrollTo() {},
};
// A hash navigation as the browser makes it: the hash changes, then hashchange fires.
async function navigate(route) {
  hist.pushed += 1;
  location.hash = '#' + route;
  for (const fn of listeners.hashchange || []) await fn();
  await settle();
}
const settle = () => new Promise((resolve) => setTimeout(resolve, 5));

class Storage {
  constructor() { this.map = new Map(); }
  getItem(k) { return this.map.has(k) ? this.map.get(k) : null; }
  setItem(k, v) { this.map.set(k, String(v)); }
  removeItem(k) { this.map.delete(k); }
}

const PROJECTS = [
  { id: 'alpha', name: 'Alpha', role: 'owner', usable: true, archived: false },
  { id: 'beta', name: 'Beta', role: 'contributor', usable: true, archived: false },
  { id: 'old', name: 'Old', role: 'owner', usable: true, archived: true },
];
const calls = [];
let signedIn = true;
function transport(method, path) {
  calls.push(method + ' ' + path);
  const [p] = path.split('?');
  let data = null;
  let status = 200;
  if (p === '/v1/sessions/current') {
    if (!signedIn) return Promise.resolve({ status: 401, data: { error: { code: 'unauthenticated', message: 'Sign in' } } });
    data = { user: { id: 'usr_alex', username: 'alex', display_name: 'Alex', superuser: false }, csrf_token: 'tok' };
  } else if (p === '/v1/sessions' && method === 'DELETE') { status = 204; }
  else if (p === '/v1/projects') data = { items: PROJECTS };
  else if (/^\/v1\/projects\/[a-z]+$/.test(p)) data = PROJECTS.find((x) => x.id === p.split('/')[3]) || null;
  else if (/\/tasks$/.test(p)) data = { items: [], next_cursor: null };
  else if (/\/queue$/.test(p)) data = { items: [
    { id: 'alpha-1', title: 'One', review_state: 'awaiting-review', updated_at: '2026-01-01T00:00:00Z' },
    { id: 'alpha-2', title: 'Two', review_state: 'changes-requested', updated_at: '2026-01-01T00:00:00Z' }] };
  else if (/\/proposals$/.test(p)) data = { items: [], next_cursor: null };
  else if (p === '/v1/agents') data = { items: [], total: 0 };
  if (data === null && status === 200) return Promise.resolve({ status: 404, data: { error: { code: 'not_found', message: 'Not found' } } });
  return Promise.resolve({ status, data });
}

const app = await import(appUrl);
const routes = await import(routesUrl);
const out = {};
const storage = new Storage();
const KEY = routes.LAST_PROJECT_PREFIX + 'usr_alex';

// -- the pure helpers ----------------------------------------------------------------------------------------
out.switchTargets = {
  reviewsWithFilter: routes.switchTarget('/p/alpha/reviews?review=changes-requested', 'beta', ['reviews']),
  recordKeyDropped: routes.switchTarget('/p/alpha/reviews?review=changes-requested&proposal=k1', 'beta', ['reviews']),
  tasksFilters: routes.switchTarget('/p/alpha?status=closed&q=wish&requirements=1', 'beta', []),
  missingSection: routes.switchTarget('/p/alpha/setup?status=closed', 'beta', ['reviews']),
  taskPage: routes.switchTarget('/p/alpha/t/alpha-1?status=closed', 'beta', ['reviews']),
  globalPage: routes.switchTarget('/agents?status=closed', 'beta', []),
};
{
  const s = new Storage();
  routes.rememberProject(s, 'usr_a', 'alpha');
  routes.rememberProject(s, 'usr_b', 'beta');
  out.twoPeople = [routes.rememberedProject(s, 'usr_a', PROJECTS), routes.rememberedProject(s, 'usr_b', PROJECTS)];
  routes.rememberProject(s, 'usr_a', 'gone');
  out.staleForgotten = [routes.rememberedProject(s, 'usr_a', PROJECTS), s.getItem(routes.LAST_PROJECT_PREFIX + 'usr_a')];
  routes.rememberProject(s, 'usr_a', 'old');
  out.archivedNotShown = routes.rememberedProject(s, 'usr_a', PROJECTS);
  out.noStorage = routes.rememberedProject(null, 'usr_a', PROJECTS);
}

// -- the app -------------------------------------------------------------------------------------------------
const root = document.createElement('div');
document.body.appendChild(root);
location.hash = '#/agents';
const ctx = await app.start(root, { transport, storage });
await settle();
// The shim matches #id, tag and tag[attr=value] only; everything else goes through its all(predicate).
const hasClass = (e, name) => String(e.className || '').split(/\s+/).includes(name);
const switcher = () => root.querySelector('#project-switch');
const railLinks = () => root.all((e) => e.tagName === 'A' && hasClass(e, 'nav-link')).map((a) => a.attributes.href);
const toasts = () => document.body.all((e) => hasClass(e, 'toast')).map((t) => t.textContent);

// Before any project was opened the switcher says choose; no project sections.
out.freshGlobal = { selected: switcher().value, sections: railLinks().filter((h) => h.startsWith('#/p/')) };

// Opening a project page remembers it.
await navigate('/p/alpha/reviews');
out.afterProjectPage = storage.getItem(KEY);
// A page without a project shows the remembered one in the switcher and its sections; it stores nothing new.
await navigate('/agents');
out.globalShowsRemembered = { selected: switcher().value, sections: railLinks().filter((h) => h.startsWith('#/p/')),
  stored: storage.getItem(KEY) };

// (a) the section and the filters survive the switch; a key naming a record of alpha does not.
await navigate('/p/alpha/reviews?review=changes-requested&proposal=k1');
let s = switcher(); s.value = 'beta'; await s.dispatch('change'); await settle();
out.switchKeeps = { hash: location.hash, stored: storage.getItem(KEY) };
// (b) a section beta does not give this person (Set up for a contributor): beta's Tasks, the filters kept, said once.
const toastsBefore = toasts().length;
await navigate('/p/alpha/setup?status=closed');
s = switcher(); s.value = 'beta'; await s.dispatch('change'); await settle();
out.switchFallsBack = { hash: location.hash, toasts: toasts().slice(toastsBefore) };
// (c) a task page: beta's Tasks list, nothing of the task's query.
const toastsBeforeTask = toasts().length;
await navigate('/p/alpha/t/alpha-1?status=closed');
s = switcher(); s.value = 'beta'; await s.dispatch('change'); await settle();
out.switchFromTask = { hash: location.hash, toasts: toasts().slice(toastsBeforeTask) };

// (f) Tasks: the filters come from the URL and are written back without a new history entry or a re-render.
calls.length = 0;
await navigate('/p/alpha?status=closed&q=wish&review=approved&requirements=1');
const taskCall = calls.find((c) => c.startsWith('GET /v1/projects/alpha/tasks'));
const page = root.querySelector('#main');
out.tasksRead = { call: taskCall, search: page.querySelector('#task-search').value,
  requirements: page.querySelector('#show-requirements').checked, review: page.querySelector('#review-filter').value,
  pressed: page.all((e) => e.tagName === 'BUTTON' && e.attributes['aria-pressed'] === 'true').map((b) => b.textContent) };
const rendersBefore = calls.filter((c) => c === 'GET /v1/projects/alpha').length;
const replacedBefore = hist.replaced;
const pushedBefore = hist.pushed;
const review = page.querySelector('#review-filter'); review.value = 'changes-requested'; await review.dispatch('change'); await settle();
out.tasksWritten = { hash: location.hash, replaced: hist.replaced - replacedBefore, pushed: hist.pushed - pushedBefore,
  rerendered: calls.filter((c) => c === 'GET /v1/projects/alpha').length - rendersBefore };
// A filtered Tasks list switches to beta with its filters.
s = switcher(); s.value = 'beta'; await s.dispatch('change'); await settle();
out.tasksSwitch = location.hash;

// (f) Reviews: one group from the URL, written back on change.
await navigate('/p/alpha/reviews?review=changes-requested');
const groupsHost = () => root.all((e) => e.attributes['data-review-groups'] !== undefined)[0];
const groupsShown = () => groupsHost().all((e) => e.tagName === 'H2').map((h) => h.attributes.id);
out.reviewsRead = { shown: groupsShown(), select: root.querySelector('#reviews-filter').value,
  lines: groupsHost().all((e) => hasClass(e, 'empty-line')).map((p) => p.textContent) };
const rf = root.querySelector('#reviews-filter'); rf.value = ''; await rf.dispatch('change'); await settle();
out.reviewsWritten = { hash: location.hash, shown: groupsShown() };

// The URL wins on a project page: a remembered beta never changes what /p/alpha shows.
storage.setItem(KEY, 'beta');
await navigate('/p/alpha');
out.urlWins = { selected: switcher().value, stored: storage.getItem(KEY) };

// Sign-out forgets the project and sends the next person home.
await navigate('/p/beta/reviews');
await ctx.signOut();
out.signOut = { stored: storage.getItem(KEY), hash: location.hash };

// (e) A deep link opened signed out is where the person lands after signing in.
location.hash = '#/p/alpha/reviews?review=changes-requested';
await ctx.signedIn({ id: 'usr_alex', username: 'alex', display_name: 'Alex', superuser: false });
await settle();
out.deepLink = location.hash;
// And with no deep link, home as before.
location.hash = '#/';
await ctx.signedIn({ id: 'usr_alex', username: 'alex', display_name: 'Alex', superuser: false });
await settle();
out.noDeepLink = location.hash;

console.log(JSON.stringify(out));
