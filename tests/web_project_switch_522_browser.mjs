// kittrial-5bb.522 slice 1 (r2): the project switch under a stricter browser. Runs web/js/app.js on the Node DOM shim
// with a location and session history built on WHATWG URL (the hash setter parses as a browser does, a same-hash set
// is no navigation, history is a real stack, a cross-origin location.href is recorded instead of ignored), against
// a stubbed transport. The first four tests (b522-t1..t10 below) come from the review's own tests.
// argv: shim URL, app.js URL, routes.js URL, project.js URL. Prints one line per test: "ok   name" or "FAIL name: why",
// then ALL OK / FAILED n; tests/test_project_switch_522.py has one named test per line.
const [shimUrl, appUrl, routesUrl, projectUrl] = process.argv.slice(2);
await import(shimUrl);

const ORIGIN = 'https://orchestra.example.test';
const listeners = {};
const settle = (ms = 5) => new Promise((resolve) => setTimeout(resolve, ms));
const browser = {
  entries: [new URL(ORIGIN + '/')], index: 0, away: [],
  now() { return this.entries[this.index]; },
  async fire() { for (const fn of listeners.hashchange || []) await fn(); },
  push(url) {
    this.entries.length = this.index + 1;
    this.entries.push(url);
    this.index += 1;
    setTimeout(() => this.fire());
  },
  async traverse(delta) {
    const target = this.index + delta;
    if (target < 0 || target >= this.entries.length) return;
    const changed = this.entries[target].hash !== this.now().hash;
    this.index = target;
    if (changed) await this.fire();
    await settle();
  },
  reset(url) { this.entries = [new URL(url)]; this.index = 0; this.away = []; },
};
globalThis.location = {
  get hash() { return browser.now().hash; },
  set hash(value) {
    const next = new URL(browser.now());
    next.hash = value;
    if (next.href !== browser.now().href) browser.push(next);
  },
  get href() { return browser.now().href; },
  set href(value) {
    const next = new URL(value, browser.now());
    if (next.origin !== ORIGIN) browser.away.push(next.href);
    else browser.push(next);
  },
};
globalThis.history = {
  replaceState(_s, _t, url) {
    const next = new URL(url, browser.now());
    if (next.origin !== ORIGIN) throw new Error('SecurityError');
    browser.entries[browser.index] = next;
  },
  get length() { return browser.entries.length; },
};
globalThis.window = {
  isSecureContext: true,
  addEventListener(type, fn) { (listeners[type] = listeners[type] || []).push(fn); },
  scrollTo() {},
};

class Storage {
  constructor() { this.map = new Map(); }
  getItem(k) { return this.map.has(k) ? this.map.get(k) : null; }
  setItem(k, v) { this.map.set(k, String(v)); }
  removeItem(k) { this.map.delete(k); }
}
const PROJECTS = [
  { id: 'alpha', name: 'Alpha', role: 'owner', usable: true, archived: false },
  { id: 'beta', name: 'Beta', role: 'contributor', usable: true, archived: false },
];
const USERS = {
  a: { id: 'usr_a', username: 'a', display_name: 'A', superuser: false },
  b: { id: 'usr_b', username: 'b', display_name: 'B', superuser: false },
};
const ARCHIVED = { id: 'eta', name: 'Eta', role: 'owner', usable: true, archived: true };
const TASKS = { alpha: [{ id: 'alpha-1', title: 'One', status: 'open', updated_at: '2026-01-01T00:00:00Z' }], beta: [] };
const calls = [];
let projectList = () => ({ status: 200, data: { items: PROJECTS } });
let dead = false;   // every read answers 401: the session ended in another tab
const unauthenticated = { status: 401, data: { error: { code: 'unauthenticated', message: 'Sign in' } } };
function transport(method, path) {
  calls.push(method + ' ' + path);
  const [p] = path.split('?');
  const notFound = { status: 404, data: { error: { code: 'not_found', message: 'Not found' } } };
  if (p === '/v1/sessions' && method === 'DELETE') return Promise.resolve({ status: 204, data: null });
  if (dead) return Promise.resolve(unauthenticated);
  if (p === '/v1/sessions/current') return Promise.resolve({ status: 200, data: { user: USERS.a, csrf_token: 'tok' } });
  if (p === '/v1/projects') return Promise.resolve(projectList());
  const project = /^\/v1\/projects\/([a-z]+)$/.exec(p);
  if (project) return Promise.resolve({ status: 200, data: PROJECTS.concat([ARCHIVED]).find((x) => x.id === project[1]) });
  const tasks = /^\/v1\/projects\/([a-z]+)\/tasks$/.exec(p);
  if (tasks) return Promise.resolve({ status: 200, data: { items: TASKS[tasks[1]] || [], next_cursor: null } });
  if (/\/queue$/.test(p)) return Promise.resolve({ status: 200, data: { items: [] } });
  if (/\/proposals$/.test(p)) return Promise.resolve({ status: 200, data: { items: [], next_cursor: null } });
  return Promise.resolve(notFound);
}

const app = await import(appUrl);
const routes = await import(routesUrl);
const projectJs = await import(projectUrl);
const route = () => decodeURIComponent(location.hash.slice(1));
let failed = 0;
async function test(name, body) {
  Object.keys(listeners).forEach((k) => { delete listeners[k]; });
  dead = false;
  projectList = () => ({ status: 200, data: { items: PROJECTS } });
  try {
    await body();
    console.log('ok   ' + name);
  } catch (error) {
    failed += 1;
    console.log('FAIL ' + name + ': ' + error.message);
  }
}
const same = (got, want, what) => {
  const g = JSON.stringify(got); const w = JSON.stringify(want);
  if (g !== w) throw new Error(`${what}: got ${g}, want ${w}`);
};
async function boot(url, storage = new Storage()) {
  browser.reset(ORIGIN + '/' + url);
  const root = document.createElement('div');
  document.body.appendChild(root);
  const ctx = await app.start(root, { transport, storage });
  await settle();
  return { ctx, root, storage };
}
const page = (root) => root.querySelector('#main');
const KEY = (who) => routes.LAST_PROJECT_PREFIX + USERS[who].id;
const hasClass = (e, name) => String(e.className || '').split(/\s+/).includes(name);
const toasts = () => document.body.all((e) => hasClass(e, 'toast')).map((t) => t.textContent);
const goTo = async (r) => { location.hash = r; await settle(20); };
async function chooseProject(root, id) {
  const s = root.querySelector('#project-switch');
  s.value = id; await s.dispatch('change'); await settle(20);
}
async function typeInSearch(root, text) {
  const search = page(root).querySelector('#task-search');
  search.value = text;
  await search.dispatch('input');
}
const buttons = (root) => Array.from(page(root).querySelectorAll('button'));
const pressed = (root) => buttons(root).filter((b) => b.attributes['aria-pressed'] === 'true').map((b) => b.textContent);
const SECTIONS = ['reviews', 'releases', 'requirements', 'records', 'rooms', 'lanes', 'feedback', 'settings', 'how'];

// ---- the review's own tests, T1-T10 (kept under their own numbers) --------------------------------------------
await test('t1_an_id_of_the_old_project_in_a_filter_value_does_not_cross', async () => {
  same(routes.switchTarget('/p/alpha?q=alpha-1', 'beta', []).path, '/p/beta', 'task id in q');
  same(routes.switchTarget('/p/alpha/rooms?q=room_7f3a', 'beta', ['rooms']).path, '/p/beta/rooms', 'room id in q');
  same(routes.switchTarget('/p/alpha/reviews?review=alpha-1', 'beta', ['reviews']).path, '/p/beta/reviews', 'task id in review');
  same(routes.switchTarget('/p/alpha?status=alpha-1', 'beta', []).path, '/p/beta', 'task id in status');
  same(routes.switchTarget('/p/alpha?q=wish', 'beta', []).path, '/p/beta?q=wish', 'a plain word');
});

await test('t2_back_restores_a_filtered_list_and_a_link_without_the_filter_resets_it', async () => {
  const { root } = await boot('#/projects');
  location.hash = '/p/alpha';
  await settle(20);
  const review = page(root).querySelector('#review-filter');
  review.value = 'approved';
  await review.dispatch('change');
  await settle();
  same(route(), '/p/alpha?review=approved', 'written back');
  same(browser.entries.length, 2, 'no history entry for the filter');
  location.hash = '/p/alpha/t/alpha-1';
  await settle(20);
  await browser.traverse(-1);
  await settle(20);
  same(route(), '/p/alpha?review=approved', 'Back');
  same(page(root).querySelector('#review-filter').value, 'approved', 'the select after Back');
  location.hash = '/p/alpha';
  await settle(20);
  same(page(root).querySelector('#review-filter').value, '', 'the select after the rail link');
  const again = page(root).querySelector('#review-filter');
  again.value = 'approved';
  await again.dispatch('change');
  await settle();
  location.hash = '/p/alpha';
  await settle(20);
  same([route(), page(root).querySelector('#review-filter').value], ['/p/alpha', ''], 'the URL and the select straight after a filter change');
});

await test('t3_a_deep_link_spelling_an_external_url_stays_on_this_origin_after_sign_in', async () => {
  for (const fragment of ['#https://evil.example/', '#//evil.example/', '#/\\evil.example', '#javascript:alert(1)']) {
    browser.reset(ORIGIN + '/');
    const root = document.createElement('div');
    document.body.appendChild(root);
    const ctx = await app.start(root, { transport, storage: new Storage() });
    await settle();
    ctx.me = null;
    location.hash = fragment;
    await settle(20);
    await ctx.signedIn(USERS.a);
    await settle(20);
    same(browser.away, [], 'navigated away for ' + fragment);
    same(browser.now().origin, ORIGIN, 'origin for ' + fragment);
    Object.keys(listeners).forEach((k) => { delete listeners[k]; });
  }
});

await test('t4_the_search_timer_does_not_rewrite_the_url_of_the_page_opened_meanwhile', async () => {
  const { root } = await boot('#/p/alpha');
  await typeInSearch(root, 'zzz');
  location.hash = '/p/alpha/reviews?review=changes-requested';
  await settle(400);
  same(route(), '/p/alpha/reviews?review=changes-requested', 'the URL of the page opened');
});

await test('t5_a_failed_read_of_the_project_list_does_not_forget_the_project', async () => {
  const storage = new Storage();
  storage.setItem(KEY('a'), 'alpha');
  projectList = () => ({ status: 503, data: { error: { code: 'busy', message: 'busy' } } });
  await boot('#/agents', storage);
  same(storage.getItem(KEY('a')), 'alpha', 'stored');
});

await test('t6_a_room_page_or_a_requirement_page_keeps_nothing_of_its_record', async () => {
  for (const pg of ['rooms/room_7f3a', 'requirements/REQ-1', 'decisions/DEC-1', 'records/references/k1']) {
    const got = routes.switchTarget(`/p/alpha/${pg}?q=room_7f3a`, 'beta', ['rooms', 'requirements', 'decisions', 'records']).path;
    const id = pg.split('/').pop();
    if (got.includes(id) || got.includes('q=')) throw new Error(`${pg}: ${got}`);
  }
});

await test('t7_after_sign_in_a_deep_link_shows_its_page', async () => {
  browser.reset(ORIGIN + '/');
  const root = document.createElement('div');
  document.body.appendChild(root);
  const ctx = await app.start(root, { transport, storage: new Storage() });
  await settle();
  ctx.me = null;
  location.hash = '#/p/alpha/reviews?review=changes-requested';
  await settle(20);
  same(root.querySelector('#reviews-filter'), null, 'signed out: the login page, not the reviews');
  await ctx.signedIn(USERS.a);
  await settle(20);
  same(root.querySelector('#reviews-filter') && root.querySelector('#reviews-filter').value, 'changes-requested', 'the page after sign-in');
  same(route(), '/p/alpha/reviews?review=changes-requested', 'the URL');
});

await test('t8_each_tasks_control_writes_the_url_and_all_round_trips', async () => {
  const { root } = await boot('#/p/alpha');
  const press = async (word) => { await buttons(root).find((b) => b.textContent === word).dispatch('click'); await settle(); };
  await press('Closed');
  same(route(), '/p/alpha?status=closed', 'the Closed button');
  await press('All');
  same(route(), '/p/alpha?status=all', 'the All button');
  const box = page(root).querySelector('#show-requirements');
  box.checked = true;
  await box.dispatch('change');
  await settle();
  same(route(), '/p/alpha?status=all&requirements=1', 'the requirements box');
  await typeInSearch(root, 'wish');
  await settle(350);
  same(route(), '/p/alpha?status=all&q=wish&requirements=1', 'the search box');
  calls.length = 0;
  location.hash = '/p/alpha?status=all';
  await settle(20);
  same(calls.find((c) => c.includes('/tasks')), 'GET /v1/projects/alpha/tasks?limit=10', 'the list read for All');
  same(pressed(root), ['All'], 'the pressed button');
});

await test('t9_unknown_and_alias_values_on_reviews_and_tasks', async () => {
  const { root } = await boot('#/p/alpha/reviews?review=legacy-review-ready');
  same(page(root).querySelector('#reviews-filter').value, 'awaiting-review', 'an alias');
  location.hash = '/p/alpha/reviews?review=bogus';
  await settle(20);
  same(page(root).querySelector('#reviews-filter').value, '', 'an unknown value');
  calls.length = 0;
  location.hash = '/p/alpha?review=bogus';
  await settle(20);
  same(calls.find((c) => c.includes('/tasks')), 'GET /v1/projects/alpha/tasks?limit=10&status=active', 'the list read');
});

await test('t10_a_project_page_that_is_not_listed_does_not_overwrite_the_remembered_project', async () => {
  const storage = new Storage();
  await boot('#/p/alpha', storage);
  location.hash = '/p/gone';
  await settle(20);
  same(storage.getItem(KEY('a')), 'alpha', 'stored');
});

// ---- m1: a value crosses only when it means the same in every project ----------------------------------------
await test('m1_q_crosses_only_without_an_id_shaped_word', async () => {
  const q = (text) => routes.switchTarget('/p/alpha?q=' + encodeURIComponent(text), 'beta', []).path;
  same(q('wish'), '/p/beta?q=wish', 'plain word');
  same(q('login page'), '/p/beta?q=login+page', 'two plain words');
  for (const id of ['task_18cb2f', 'alpha-1', 'alpha-7k2', 'room_7f3a', 'room-9d4487', 'r-9d4487e9c7da', 'kittrial-5bb.522', 'REQ-7', 'see alpha-1 please', 'alpha-1,', '(alpha-1)']) {
    same(q(id), '/p/beta', 'an id: ' + id);
  }
  same(q('set-up'), '/p/beta', 'a hyphenated word has an id\'s shape and may stay behind');
  // The dot is pinned (approval of slice 1 r3): a dotted word has an id's shape too, and stays behind.
  for (const dotted of ['kit.guide', '5bb.522', 'v1.2', 'node.js']) same(q(dotted), '/p/beta', 'a dotted word: ' + dotted);
  // r3: an id next to any other character is still an id (the whole value is tested, no split, no trim).
  const ids = ['task_18913a1f7280', 'alpha-1', 'kittrial-5bb.522', 'room_7f3a', 'REQ-7'];
  const shapes = [(x) => '`' + x + '`', (x) => '#' + x, (x) => '<' + x + '>', (x) => 'id=' + x, (x) => x + '/', (x) => x + '..', (x) => x + '...',
    (x) => 'http://orchestra.example/#/p/alpha/t/' + x, (x) => 'ref.' + x + '@3', (x) => '\u201c' + x + '\u201d', (x) => x + '\u2026', (x) => '@' + x,
    (x) => '$' + x, (x) => '%' + x, (x) => '^' + x, (x) => '~' + x, (x) => '*' + x + '*', (x) => '\\' + x, (x) => '|' + x + '|', (x) => '&' + x,
    (x) => '+' + x, (x) => '\u00ab' + x + '\u00bb', (x) => '[' + x + ']', (x) => '{' + x + '}', (x) => x + '-', (x) => '-' + x, (x) => x.replace(/[_-]/, '__'),
    (x) => 'wish ' + x, (x) => x + ' wish', (x) => x + ';', (x) => x + '?'];
  let tried = 0;
  for (const id of ids) for (const shape of shapes) { const text = shape(id); tried += 1; same(q(text), '/p/beta', 'an id with a neighbour: ' + text); }
  if (tried < 52) throw new Error('only ' + tried + ' shapes');
  same(q('ref.a--b'), '/p/beta', 'a doubled separator');
  for (const word of ["don't", 'R&D', 'TCP/IP', '#tag', 'caf\u00e9', '\u65e5\u672c\u8a9e', 'wish list', 'k1', 'R01']) same(q(word), '/p/beta?q=' + new URLSearchParams({ q: word }).toString().slice(2), 'plain: ' + word);
  const started = Date.now();
  q('a' + '-'.repeat(100000));
  q('-'.repeat(100000) + ' wish');
  if (Date.now() - started > 1000) throw new Error('a long run of hyphens takes ' + (Date.now() - started) + ' ms');
});

await test('m1_status_review_requirements_cross_only_as_words_of_their_vocabulary', async () => {
  const path = (query) => routes.switchTarget('/p/alpha/reviews?' + query, 'beta', ['reviews']).path;
  for (const word of ['active', 'closed', 'all']) same(path('status=' + word), '/p/beta/reviews?status=' + word, 'status ' + word);
  for (const word of ['wish', 'alpha-1', 'constructor', 'Closed', 'toString', '__proto__']) same(path('status=' + word), '/p/beta/reviews', 'status ' + word);
  for (const [value] of projectJs.TASK_REVIEW_FILTERS.filter(([v]) => v)) same(path('review=' + value), '/p/beta/reviews?review=' + value, 'review ' + value);
  for (const word of ['legacy-review-ready', 'awaiting-integration', 'error']) same(path('review=' + word), '/p/beta/reviews?review=' + word, 'review ' + word);
  for (const word of ['alpha-1', 'bogus', 'constructor']) same(path('review=' + word), '/p/beta/reviews', 'review ' + word);
  same(path('requirements=1'), '/p/beta/reviews?requirements=1', 'requirements=1');
  for (const word of ['0', 'yes', 'true', 'alpha-1', '11']) same(path('requirements=' + word), '/p/beta/reviews', 'requirements ' + word);
});

await test('m1_the_switcher_applies_it_to_the_live_url', async () => {
  const { root } = await boot('#/p/alpha?status=closed&q=alpha-1');
  await chooseProject(root, 'beta');
  same(route(), '/p/beta?status=closed', 'the id left behind, the status kept');
});

// ---- m2: a write belongs to the render that asked for it ------------------------------------------------------
await test('m2_a_rail_link_within_the_search_window_keeps_its_own_url', async () => {
  const { root } = await boot('#/p/alpha');
  await typeInSearch(root, 'zzz');
  await settle(50);
  location.hash = '/p/alpha/reviews';
  await settle(400);
  same(route(), '/p/alpha/reviews', 'the URL');
});

await test('m2_reviews_keeps_its_review_and_proposal_keys', async () => {
  for (const target of ['/p/alpha/reviews?review=changes-requested', '/p/alpha/reviews?proposal=k1']) {
    const { root } = await boot('#/p/alpha');
    await typeInSearch(root, 'zzz');
    await settle(50);
    location.hash = target;
    await settle(400);
    same(route(), target, 'the URL');
  }
});

await test('m2_a_switch_within_the_search_window_writes_nothing_onto_the_new_project', async () => {
  const { root } = await boot('#/p/alpha');
  await typeInSearch(root, 'zzz');
  await settle(50);
  await chooseProject(root, 'beta');
  await settle(400);
  same(route(), '/p/beta', 'the URL');
});

await test('m2_a_task_page_opened_within_the_search_window_keeps_its_url', async () => {
  const { root } = await boot('#/p/alpha');
  await typeInSearch(root, 'zzz');
  await settle(50);
  location.hash = '/p/alpha/t/alpha-1';
  await settle(400);
  same(route(), '/p/alpha/t/alpha-1', 'the URL');
});

await test('m2_signing_out_within_the_search_window_leaves_home_alone', async () => {
  const { ctx, root } = await boot('#/p/alpha');
  await typeInSearch(root, 'zzz');
  await settle(50);
  await ctx.signOut();
  await settle(400);
  same(route(), '/', 'the URL after sign-out');
});

await test('m2_a_lost_session_within_the_search_window_writes_nothing', async () => {
  const { ctx, root } = await boot('#/p/alpha');
  await typeInSearch(root, 'zzz');
  await settle(50);
  ctx.sessionLost();
  await settle(400);
  same(route(), '/p/alpha', 'the URL');
});

await test('m2_a_search_left_alone_is_still_written', async () => {
  const { root } = await boot('#/p/alpha');
  await typeInSearch(root, 'zzz');
  await settle(400);
  same(route(), '/p/alpha?q=zzz', 'the URL');
});

// ---- s1: a failed read is unknown, not "not listed" -----------------------------------------------------------
await test('s1_a_503_keeps_the_key_and_a_successful_list_without_it_forgets', async () => {
  const storage = new Storage();
  storage.setItem(KEY('a'), 'alpha');
  projectList = () => ({ status: 503, data: { error: { code: 'busy', message: 'busy' } } });
  const { ctx } = await boot('#/agents', storage);
  same(storage.getItem(KEY('a')), 'alpha', 'kept after a 503');
  projectList = () => ({ status: 200, data: { items: [PROJECTS[1]] } });
  await ctx.refreshProjects();
  await ctx.render();
  same(storage.getItem(KEY('a')), null, 'forgotten when a successful list omits it');
});

// ---- s2: sections and the true sentence -----------------------------------------------------------------------
await test('s2_handover_and_notes_keep_their_section_on_a_switch', async () => {
  for (const section of ['handover', 'notes']) {
    const { root } = await boot('#/p/alpha/' + section);
    await chooseProject(root, 'beta');
    same(route(), '/p/beta/' + section, section);
    same(toasts(), [], 'no fallback for ' + section);
  }
});

await test('s2_a_record_page_switches_to_its_list_and_says_why', async () => {
  const cases = [
    ['/p/alpha/rooms/room_7f3a', '/p/beta/rooms', 'A room belongs to one project, so Beta opened on its Rooms list.'],
    ['/p/alpha/requirements/REQ-1', '/p/beta/requirements', 'A requirement belongs to one project, so Beta opened on its Requirements list.'],
    ['/p/alpha/records/r-9d4487e9c7da', '/p/beta/records', 'A record belongs to one project, so Beta opened on its Records list.'],
    ['/p/alpha/records/references/k1', '/p/beta/records', 'A record belongs to one project, so Beta opened on its Records list.'],
    ['/p/alpha/decisions/DEC-1', '/p/beta', 'Beta has no Decisions page, so it opened on Tasks.'],
    ['/p/alpha/t/alpha-1', '/p/beta', 'Task pages belong to one project, so Beta opened on Tasks.'],
    ['/p/alpha/new', '/p/beta', 'A new task is written in one project, so Beta opened on Tasks.'],
    ['/p/alpha/setup', '/p/beta?q=zzz', 'Beta has no Set up page, so it opened on Tasks.'],
    ['/p/alpha/designs/some-scope', '/p/beta', 'That page belongs to one project, so Beta opened on Tasks.'],
  ];
  for (const [from, to, sentence] of cases) {
    const { root } = await boot('#' + from + '?q=zzz');
    await chooseProject(root, 'beta');
    same([route(), toasts()], [to, [sentence]], from);
    document.body.all((e) => hasClass(e, 'toast')).forEach((t) => t.remove && t.remove());
  }
});

await test('s2_program_without_a_program_in_the_target_says_so_by_name', async () => {
  const sections = ['reviews', 'rooms'];
  same(routes.switchTarget('/p/alpha/program?status=closed', 'beta', sections), { path: '/p/beta?status=closed', fellBack: true, section: 'program' }, 'switchTarget');
  same(routes.fallbackMessage('Beta', routes.SECTION_LABELS.program), 'Beta has no Program page, so it opened on Tasks.', 'sentence');
});

// ---- s3: Back and Forward, the controls, a switch of a driven page ---------------------------------------------
await test('s3_back_and_forward_across_a_switch', async () => {
  const { root } = await boot('#/projects');
  location.hash = '/p/alpha/reviews?review=changes-requested';
  await settle(20);
  await chooseProject(root, 'beta');
  same(route(), '/p/beta/reviews?review=changes-requested', 'the switch');
  await browser.traverse(-1);
  await settle(20);
  same([route(), page(root).querySelector('#reviews-filter').value], ['/p/alpha/reviews?review=changes-requested', 'changes-requested'], 'Back');
  await browser.traverse(1);
  await settle(20);
  same([route(), page(root).querySelector('#reviews-filter').value, root.querySelector('#project-switch').value], ['/p/beta/reviews?review=changes-requested', 'changes-requested', 'beta'], 'Forward');
});

await test('s3_the_controls_drive_the_url_and_the_switch_carries_what_they_wrote', async () => {
  const { root } = await boot('#/p/alpha');
  await buttons(root).find((b) => b.textContent === 'Closed').dispatch('click');
  await settle();
  const box = page(root).querySelector('#show-requirements');
  box.checked = true; await box.dispatch('change'); await settle();
  await typeInSearch(root, 'wish'); await settle(350);
  const review = page(root).querySelector('#review-filter');
  review.value = 'approved'; await review.dispatch('change'); await settle();
  same(route(), '/p/alpha?status=closed&q=wish&review=approved&requirements=1', 'written');
  await chooseProject(root, 'beta');
  same(route(), '/p/beta?status=closed&q=wish&review=approved&requirements=1', 'carried');
  calls.length = 0;
  await browser.traverse(-1);
  await settle(20);
  same(route(), '/p/alpha?status=closed&q=wish&review=approved&requirements=1', 'Back from the switch');
});

await test('s3_the_status_words_and_inherited_names_in_the_url', async () => {
  const { root } = await boot('#/p/alpha?status=constructor');
  same(pressed(root), ['Active'], 'constructor is no status');
  location.hash = '/p/alpha?status=toString';
  await settle(20);
  same(pressed(root), ['Active'], 'toString is no status');
  location.hash = '/p/alpha?status=closed';
  await settle(20);
  same(pressed(root), ['Closed'], 'closed');
});

await test('n4_the_group_asked_for_says_none_even_when_it_is_needs_operator_attention', async () => {
  const { root } = await boot('#/p/alpha/reviews?review=error');
  const lines = root.all((e) => hasClass(e, 'empty-line')).map((p) => p.textContent).filter((t) => t.startsWith('Needs operator'));
  same(lines, ['Needs operator attention: none.'], 'the filtered empty group');
  location.hash = '/p/alpha/reviews';
  await settle(20);
  same(root.all((e) => hasClass(e, 'empty-line')).map((p) => p.textContent).some((t) => t.startsWith('Needs operator')), false, 'hidden when not asked for');
});

// ---- notes 1 and 2 ---------------------------------------------------------------------------------------------
await test('n1_a_session_lost_in_another_tab_forgets_the_project_and_nothing_is_written_back', async () => {
  const { root, storage } = await boot('#/p/alpha');
  same(storage.getItem(KEY('a')), 'alpha', 'remembered while signed in');
  dead = true;
  location.hash = '/p/alpha/reviews';
  await settle(20);
  same(storage.getItem(KEY('a')), null, 'forgotten at the 401');
  same(!!root.querySelector('#project-switch'), false, 'the login page');
  const second = await boot('#/agents');
  dead = true;
  location.hash = '/p/beta';
  await settle(20);
  same(second.storage.getItem(KEY('a')), null, 'not written by the page that got the 401');
});

// Note 1 (approval of slice 1 r3): section by section, a session that ended in another tab writes nothing back.
// Records, Set up, How work is found, Handover and Notes answer a 401 instead of throwing, so they keep the
// person's own key; that was accepted as it is (re-check of r2, n1) and is not asserted here.
await test('n1_every_throwing_section_writes_nothing_back_after_a_lost_session', async () => {
  for (const section of ['', 'reviews', 'releases', 'rooms', 'lanes', 'people', 'agents', 'settings', 'requirements', 'feedback']) {
    dead = false;
    const { root, storage } = await boot('#/p/beta');
    same(storage.getItem(KEY('a')), 'beta', 'remembered first: ' + section);
    dead = true;
    location.hash = '/p/alpha' + (section ? '/' + section : '');
    await settle(20);
    same([storage.getItem(KEY('a')), !!root.querySelector('#project-switch')], [null, false], 'after the 401 on ' + (section || 'tasks'));
  }
  dead = false;
});

await test('n2_the_next_person_does_not_land_on_the_last_ones_url_after_a_lost_session', async () => {
  const { ctx } = await boot('#/p/alpha/reviews?review=changes-requested');
  ctx.sessionLost();
  await ctx.signedIn(USERS.b);
  await settle(20);
  same(route(), '/', 'the next person');
});

await test('n2_the_same_person_returns_to_the_page_after_a_lost_session', async () => {
  const { ctx, root } = await boot('#/p/alpha/reviews?review=changes-requested');
  ctx.sessionLost();
  await ctx.signedIn(USERS.a);
  await settle(20);
  same([route(), root.querySelector('#reviews-filter').value], ['/p/alpha/reviews?review=changes-requested', 'changes-requested'], 'the same person');
});

await test('n2_back_on_the_login_page_after_sign_out_does_not_hand_the_next_person_the_last_url', async () => {
  const { ctx } = await boot('#/agents');
  location.hash = '/p/alpha/reviews?review=changes-requested';
  await settle(20);
  location.hash = '/p/alpha/rooms';
  await settle(20);
  await ctx.signOut();
  await browser.traverse(-1);
  await settle(20);
  same(route(), '/p/alpha/reviews?review=changes-requested', 'Back reaches the last person\'s URL');
  await ctx.signedIn(USERS.b);
  await settle(20);
  same(route(), '/', 'the next person');
});

await test('n2_a_deep_link_opened_with_nobody_signed_in_is_for_whoever_signs_in', async () => {
  browser.reset(ORIGIN + '/#/p/alpha/reviews?review=changes-requested');
  dead = true;
  const root = document.createElement('div');
  document.body.appendChild(root);
  const ctx = await app.start(root, { transport, storage: new Storage() });
  await settle();
  dead = false;
  await ctx.signedIn(USERS.b);
  await settle(20);
  same(route(), '/p/alpha/reviews?review=changes-requested', 'the deep link');
});

const newToasts = (before) => toasts().slice(before);

// ---- r3 m1b: a search that stays behind is said so ------------------------------------------------------------
await test('m1b_a_search_left_behind_is_said_once_and_alone_or_with_a_fallback_sentence', async () => {
  const LEFT = 'The search was not carried over, because it looks like an id, and ids belong to one project.';
  let before = toasts().length;
  const { root } = await boot('#/p/alpha?q=alpha-1');
  await chooseProject(root, 'beta');
  same([route(), newToasts(before)], ['/p/beta', [LEFT]], 'a search that names a task of Alpha');
  before = toasts().length;
  const second = await boot('#/p/alpha?q=wish');
  await chooseProject(second.root, 'beta');
  same(newToasts(before).filter((t) => t.includes('not carried')), [], 'a plain word crosses, no line');
});

await test('m1b_with_a_fallback_the_two_sentences_share_one_toast', async () => {
  const before = toasts().length;
  const { root } = await boot('#/p/alpha/setup?q=alpha-1&status=closed');
  await chooseProject(root, 'beta');
  same([route(), newToasts(before)], ['/p/beta?status=closed', ['Beta has no Set up page, so it opened on Tasks. The search was not carried over, because it looks like an id, and ids belong to one project.']], 'both');
});

await test('m1b_no_search_no_line', async () => {
  const before = toasts().length;
  const { root } = await boot('#/p/alpha?status=closed');
  await chooseProject(root, 'beta');
  same([route(), newToasts(before)], ['/p/beta?status=closed', []], 'nothing to say');
});

// ---- r3 s1: two cases that had no test --------------------------------------------------------------------------
await test('s1a_a_new_render_on_the_same_path_does_not_take_the_old_timers_write', async () => {
  const { root } = await boot('#/p/alpha');
  await typeInSearch(root, 'zzz');
  await settle(30);
  await chooseProject(root, 'beta');
  await chooseProject(root, 'alpha');
  await settle(400);
  same([route(), page(root).querySelector('#task-search').value], ['/p/alpha', ''], 'Alpha, Beta, Alpha inside the window');
});

await test('s1a_the_rail_tasks_link_from_a_closed_list_inside_the_window', async () => {
  const { root } = await boot('#/p/alpha?status=closed');
  await typeInSearch(root, 'zzz');
  await settle(30);
  location.hash = '/p/alpha';
  await settle(400);
  same([route(), pressed(root), page(root).querySelector('#task-search').value], ['/p/alpha', ['Active'], ''], 'the link\'s own URL');
});

await test('s1b_an_archived_projects_page_does_not_become_the_remembered_project', async () => {
  projectList = () => ({ status: 200, data: { items: PROJECTS.concat([ARCHIVED]) } });
  const { root, storage } = await boot('#/p/alpha');
  same(storage.getItem(KEY('a')), 'alpha', 'remembered');
  location.hash = '/p/eta';
  await settle(30);
  same(root.querySelector('h1') && root.querySelector('h1').textContent, 'Eta', 'the archived page itself opened');
  same(storage.getItem(KEY('a')), 'alpha', 'still alpha');
});

// ---- r3 n2 / n3 -----------------------------------------------------------------------------------------------
await test('n2_a_double_click_on_sign_out_still_sends_the_next_person_home', async () => {
  const { ctx } = await boot('#/agents');
  location.hash = '/p/alpha/reviews?review=changes-requested';
  await settle(20);
  location.hash = '/p/alpha/rooms';
  await settle(20);
  await ctx.signOut();
  await ctx.signOut();
  await browser.traverse(-1);
  await settle(20);
  await ctx.signedIn(USERS.b);
  await settle(20);
  same(route(), '/', 'the next person');
});

await test('n3_an_inherited_name_as_a_section_puts_no_code_in_the_toast', async () => {
  for (const name of ['constructor', 'toString', 'nosuchpage']) {
    const before = toasts().length;
    const { root } = await boot('#/p/alpha/' + name);
    await chooseProject(root, 'beta');
    same([route(), newToasts(before)], ['/p/beta', ['That page belongs to one project, so Beta opened on Tasks.']], name);
  }
});

console.log(failed ? `FAILED ${failed}` : 'ALL OK');
process.exit(failed ? 1 : 0);
