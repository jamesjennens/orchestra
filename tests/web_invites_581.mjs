// kittrial-5bb.581: invites for people who already have an account, the invite panel's off state and its way in
// from the page head, and the "Orchestra was upgraded" banner. Runs web/js/views/invites.js, project.js, api.js and
// app.js under Node with tests/web_dom_shim.mjs against stubbed APIs that record every request.
//
// argv: shim URL, api.js URL, invites.js URL, project.js URL, app.js URL.
// Prints one JSON object of observations; tests/test_web_invites_581.py asserts on it.
const [shimUrl, apiUrl, invitesUrl, projectUrl, appUrl] = process.argv.slice(2);
await import(shimUrl);

globalThis.location = { hash: '', origin: 'https://here', reload() { out.reloads = (out.reloads || 0) + 1; } };
globalThis.history = { replaceState(_s, _t, url) { location.hash = url; } };
const windowListeners = {};
globalThis.window = {
  isSecureContext: false,
  addEventListener(type, fn) { (windowListeners[type] = windowListeners[type] || []).push(fn); },
  scrollTo() {},
};
const documentListeners = {};
document.addEventListener = (type, fn) => { (documentListeners[type] = documentListeners[type] || []).push(fn); };
document.visibilityState = 'visible';

const settle = (ms = 20) => new Promise((resolve) => setTimeout(resolve, ms));
// Times relative to now (no fixed dates): days from this run's clock, as the service would write them.
const daysFromNow = (days) => new Date(Date.now() + days * 86400000).toISOString().replace(/\.\d{3}Z$/, 'Z');
async function navigate(route) {
  location.hash = '#' + route;
  for (const fn of windowListeners.hashchange || []) await fn();
  await settle();
}
class Storage {
  constructor() { this.map = new Map(); }
  getItem(k) { return this.map.has(k) ? this.map.get(k) : null; }
  setItem(k, v) { this.map.set(k, String(v)); }
  removeItem(k) { this.map.delete(k); }
}

const { createApi, ApiError } = await import(apiUrl);
const invites = await import(invitesUrl);
const project = await import(projectUrl);
const app = await import(appUrl);
const out = {};

const byId = (node, id) => node.querySelector('#' + id);
const text = (node) => node.textContent.replace(/\s+/g, ' ').trim();
const withAttr = (node, name, value) => node.all((e) => e.attributes && (value === undefined ? name in e.attributes : e.attributes[name] === value));
const selectOptions = (select) => select.all((e) => e.tagName === 'OPTION').map((o) => o.value);

// -- the invite panel ---------------------------------------------------------------------------------------------
const PROJECTS = [{ id: 'north', name: 'North' }, { id: 'south', name: 'South' }];
const ON = { enabled: true, may_invite: true, items: [], projects: PROJECTS, default_hours: 72, max_hours: 168, off_sentence: null };

async function panel({ data = ON, me = { id: 'usr_o', username: 'olga', superuser: false }, query = '', archived = false } = {}) {
  const made = [];
  const ctx = {
    me, query: new URLSearchParams(query), href: (path) => '#' + path,
    api: { invites: async () => data, createInvite: async (body) => { made.push(body); return {}; }, revokeInvite: async () => ({}) },
  };
  document.activeElement = document.body;
  const host = invites.invitePanel(ctx, { id: 'north', name: 'North', role: 'owner', archived });
  document.body.appendChild(host);
  await settle();
  return { host, made };
}
async function check(box, on) { box.checked = on; await box.dispatch('change'); }
const form = (host) => withAttr(host, 'data-form', 'invite')[0];

out.panel = {};
{
  const { host, made } = await panel();
  out.panel.title = text(host.querySelector('h2'));
  out.panel.legendTexts = host.all((e) => e.tagName === 'LEGEND').map(text);
  out.panel.agentNote = host.all((e) => e.tagName === 'P' && e.className.includes('muted')).map(text);
  out.panel.newFieldsHiddenAtFirst = withAttr(host, 'data-person-fields', 'new')[0].hidden;
  out.panel.existingFieldsHiddenAtFirst = withAttr(host, 'data-person-fields', 'existing')[0].hidden;
  out.panel.personChoices = withAttr(host, 'data-choice').filter((e) => e.attributes['data-choice'].startsWith('i-person:')).map(text);
  byId(host, 'i-name').value = 'Lena Park';
  byId(host, 'i-username').value = '@lena';
  await form(host).dispatch('submit');
  out.panel.newBody = made[0] || null;
}
{
  const { host, made } = await panel();
  byId(host, 'i-name').value = 'Not sent';
  await check(byId(host, 'i-person-new'), false);
  await check(byId(host, 'i-person-existing'), true);
  out.panel.existingShowsRoleHint = text(withAttr(host, 'data-person-fields', 'existing')[0]);
  out.panel.newFieldsHiddenForExisting = withAttr(host, 'data-person-fields', 'new')[0].hidden;
  out.panel.existingFieldsHiddenForExisting = withAttr(host, 'data-person-fields', 'existing')[0].hidden;
  byId(host, 'i-existing-username').value = '@ivy';
  byId(host, 'i-role').value = 'coordinator';
  await form(host).dispatch('submit');
  out.panel.existingBody = made[0] || null;
  out.panel.roleOptionsOwner = selectOptions(byId(host, 'i-role'));
  out.panel.roleDefault = (() => { const s = byId(host, 'i-role'); s.chosen = undefined; return s.value; })();
}
{
  const { host, made } = await panel();
  await check(byId(host, 'i-person-existing'), true);
  byId(host, 'i-existing-username').value = 'ivy';
  await form(host).dispatch('submit');
  out.panel.existingDefaultBody = made[0] || null;
}
{
  const { host } = await panel({ me: { id: 'usr_s', username: 'root', superuser: true } });
  out.panel.roleOptionsSuperuser = selectOptions(byId(host, 'i-role'));
}
{
  const { host, made } = await panel();
  byId(host, 'i-name').value = 'Lena Park';
  byId(host, 'i-username').value = 'lena';
  await check(byId(host, 'i-agent'), true);
  byId(host, 'i-agent-name').value = 'Lena review bot';
  out.panel.agentRoleOptions = selectOptions(byId(host, 'i-agent-role'));
  byId(host, 'i-agent-role').value = 'reviewer';
  await form(host).dispatch('submit');
  out.panel.agentReviewerBody = made[0] || null;
}
{
  const { host, made } = await panel();
  byId(host, 'i-name').value = 'Lena Park';
  byId(host, 'i-username').value = 'lena';
  await check(byId(host, 'i-agent'), true);
  byId(host, 'i-agent-name').value = 'Lena build bot';
  await form(host).dispatch('submit');
  out.panel.agentDefaultBody = made[0] || null;
}
// The off state, and who sees the panel at all.
{
  const sentence = 'Invites are switched off on this installation; a superuser can switch them on.';
  const { host } = await panel({ data: { ...ON, enabled: false, off_sentence: sentence } });
  out.off = { mayInvite: { hidden: host.hidden, text: text(host), forms: withAttr(host, 'data-form', 'invite').length,
    offNote: withAttr(host, 'data-invites-off').map(text) } };
}
{
  const { host } = await panel({ data: { ...ON, enabled: false, may_invite: false, items: [], off_sentence: 'Off.' } });
  out.off.mayNotInvite = { hidden: host.hidden, children: host.childNodes.length };
}
{
  const { host } = await panel({ data: { ...ON, enabled: true, may_invite: false, items: [] } });
  out.off.mayNotInviteSwitchOn = { hidden: host.hidden, children: host.childNodes.length };
}
// The list: an invite to an existing account, used, says what it did per project.
{
  const items = [{
    id: 'inv_1', person: 'existing', username: 'ivy', user_id: 'usr_ivy', role: 'contributor', status: 'used',
    projects: [{ id: 'north', name: 'North' }, { id: 'south', name: 'South' }, { id: 'east', name: 'East' }],
    memberships: { north: { before: null, after: 'contributor' }, south: { before: 'owner', after: 'owner' }, east: { before: 'viewer', after: 'contributor' } },
    invited_by_name: 'Olga', created_at: daysFromNow(-3), used_at: daysFromNow(-2), used_by_name: 'Ivy',
  }, {
    id: 'inv_2', person: 'new', username: 'lena', display_name: 'Lena Park', status: 'pending', projects: [{ id: 'north', name: 'North' }],
    invited_by_name: 'Olga', created_at: daysFromNow(-10), expires_at: daysFromNow(-7),
  }];
  const { host } = await panel({ data: { ...ON, items } });
  const row = (id) => withAttr(host, 'data-invite', id)[0];
  out.list = {
    existingTitle: text(row('inv_1').querySelector('div')),
    membershipLines: withAttr(row('inv_1'), 'data-membership').map(text),
    newTitle: text(row('inv_2').querySelector('div')),
    newMembershipLines: withAttr(row('inv_2'), 'data-membership').length,
  };
}
// #/p/{pid}/people?invite=1 focuses the panel's first field once drawn.
{
  const { host } = await panel({ query: 'invite=1' });
  await settle();
  out.focus = { asked: document.activeElement === byId(host, 'i-person-new') };
  const plain = await panel();
  await settle();
  out.focus.notAsked = document.activeElement === byId(plain.host, 'i-person-new') || document.activeElement === document.body;
  out.focus.notAskedStaysOnBody = document.activeElement === document.body;
}

// -- the page-head action -------------------------------------------------------------------------------------------
const HEAD_PROJECTS = {
  alpha: { id: 'alpha', name: 'Alpha', role: 'owner', can_coordinate: true, usable: true, archived: false },
  beta: { id: 'beta', name: 'Beta', role: 'contributor', can_coordinate: false, usable: true, archived: false },
  gamma: { id: 'gamma', name: 'Gamma', role: 'owner', can_coordinate: true, usable: true, archived: true },
  delta: { id: 'delta', name: 'Delta', role: 'viewer', can_coordinate: false, usable: true, archived: false },
};
function headTransport(method, path) {
  const [p] = path.split('?');
  let data = { items: [] };
  if (/^\/v1\/projects\/[a-z]+$/.test(p)) data = HEAD_PROJECTS[p.split('/')[3]] || null;
  else if (/\/coordination$/.test(p)) data = { people: [], agents: [], web_rules: [], host_rules: { state: 'unknown', rules: [] } };
  else if (/\/members$/.test(p)) data = { items: [] };
  else if (p === '/v1/invites') data = { ...ON, items: [] };
  return Promise.resolve({ status: 200, data });
}
async function headAction(view, pid, me) {
  const ctx = { api: createApi(headTransport), href: (path) => '#' + path, go() {}, query: new URLSearchParams(''), setQuery() {},
    currentView: () => 1, render() {}, refreshProjects: async () => {}, me, projects: Object.values(HEAD_PROJECTS), features: { requirements: true } };
  const page = await view(ctx, { pid });
  await settle();
  const head = page.all((e) => e.tagName === 'HEADER')[0];
  const actions = head.all((e) => e.attributes['data-action'] === 'invite');
  return { count: actions.length, href: actions[0] ? actions[0].attributes.href : null, label: actions[0] ? text(actions[0]) : null,
    newTask: head.all((e) => e.tagName === 'A' && text(e) === 'New task').length };
}
const PLAIN = { id: 'usr_alex', username: 'alex', display_name: 'Alex', superuser: false };
const SUPER = { id: 'usr_root', username: 'root', display_name: 'Root', superuser: true };
out.head = {};
for (const [name, view] of [['overview', project.overview], ['people', project.people]]) {
  out.head[name] = {
    owner: await headAction(view, 'alpha', PLAIN),
    contributor: await headAction(view, 'beta', PLAIN),
    superuser: await headAction(view, 'delta', SUPER),
    archived: await headAction(view, 'gamma', PLAIN),
  };
}

// -- the invite page, for an existing account -----------------------------------------------------------------------
const TOKEN = 'tok-581-abcdefghijklmnopqrstuvwxyz';
const EXISTING = { person: 'existing', username: 'ivy', display_name: null, invited_by_name: 'Olga',
  projects: [{ id: 'north', name: 'North' }, { id: 'south', name: 'South' }, { id: 'west', name: 'West' }],
  agent: null, expires_at: daysFromNow(3), password_min_length: null };
const ACCEPTED = { operation: 'invites.accept', user: { id: 'usr_ivy', username: 'ivy', display_name: 'Ivy Tan' },
  projects: [{ id: 'north', name: 'North', role: 'contributor', role_before: null },
    { id: 'south', name: 'South', role: 'coordinator', role_before: 'viewer' },
    { id: 'west', name: 'West', role: 'owner', role_before: 'owner' }],
  invited_by_name: 'Olga', agent: null, credential: null, setup: null, secret_available: false };

function pageCtx({ me = null, invite = EXISTING, acceptFails = null } = {}) {
  const calls = [];
  const ctx = {
    me, projects: [], href: (path) => '#' + path,
    go(route) { calls.push(['go', route]); },
    signedIn() { calls.push(['signedIn']); },
    signOut() { calls.push(['signOut']); },
    refreshProjects: async () => { calls.push(['refreshProjects']); },
    api: {
      viewInvite: async (token) => { calls.push(['viewInvite', token]); return invite; },
      login: async (username, password) => { calls.push(['login', username, password]); return { id: 'usr_ivy', username: 'ivy', display_name: 'Ivy Tan' }; },
      logout: async () => { calls.push(['logout']); },
      acceptInvite: async (token) => {
        calls.push(['acceptInvite', token]);
        if (acceptFails) { const error = acceptFails; acceptFails = null; throw error; }
        return ACCEPTED;
      },
      redeemInvite: async (token, password) => { calls.push(['redeemInvite', token, password]); return { user: { username: 'lena' }, projects: [{ id: 'north', name: 'North' }] }; },
    },
  };
  return { ctx, calls };
}
async function openPage(options) {
  const { ctx, calls } = pageCtx(options);
  const page = invites.invitePage(ctx, TOKEN);
  await settle();
  return { page, ctx, calls };
}
const acceptButtons = (page) => withAttr(page, 'data-action', 'accept-invite');
const doneLines = (page) => withAttr(page, 'data-accepted-project').map(text);
const title = (page) => text(page.querySelector('h1'));
const MEMBERSHIP_WORDS = ['already', 'member', 'keep', 'Raised', 'Added as', 'role in'];

out.page = {};
{
  const { page, ctx, calls } = await openPage();
  const before = text(page);
  const userBox = byId(page, 'invite_username');
  out.page.signedOut = {
    intro: withAttr(page, 'data-invite-intro').map(text),
    username: userBox ? userBox.value : null, readonly: userBox ? 'readonly' in userBox.attributes : false,
    hasPassword: Boolean(byId(page, 'invite_password')),
    acceptButtons: acceptButtons(page).length,
    membershipWordsBefore: MEMBERSHIP_WORDS.filter((w) => before.includes(w)),
  };
  byId(page, 'invite_password').value = 'correct horse';
  await withAttr(page, 'data-form', 'invite-sign-in')[0].dispatch('submit');
  await settle();
  out.page.signedOut.calls = calls;
  out.page.signedOut.me = ctx.me && ctx.me.username;
  out.page.signedOut.title = title(page);
  out.page.signedOut.lines = doneLines(page);
}
{
  const { page, calls } = await openPage({ me: { id: 'usr_ivy', username: 'IVY', display_name: 'Ivy Tan' } });
  const before = text(page);
  out.page.named = {
    acceptButtons: acceptButtons(page).map(text), signInForms: withAttr(page, 'data-form', 'invite-sign-in').length,
    membershipWordsBefore: MEMBERSHIP_WORDS.filter((w) => before.includes(w)),
  };
  await acceptButtons(page)[0].dispatch('click');
  await settle();
  out.page.named.title = title(page);
  out.page.named.lines = doneLines(page);
  const go = withAttr(page, 'data-link', 'invite-first-project')[0];
  out.page.named.goLabel = go ? text(go) : null;
  out.page.named.goHref = go ? go.attributes.href : null;
  if (go) { await go.dispatch('click'); await settle(); }
  out.page.named.calls = calls;
}
{
  const { page, ctx, calls } = await openPage({ me: { id: 'usr_max', username: 'max', display_name: 'Max' } });
  const other = withAttr(page, 'data-invite-other');
  const before = text(page);
  out.page.other = {
    hint: other.map(text), acceptButtons: acceptButtons(page).length,
    membershipWordsBefore: MEMBERSHIP_WORDS.filter((w) => before.includes(w)),
  };
  // A missing button or form is recorded, not thrown, so the test that names it is the one that fails.
  const signOut = withAttr(page, 'data-action', 'invite-sign-out')[0];
  out.page.other.signOutButton = Boolean(signOut);
  if (signOut) {
    await signOut.dispatch('click');
    await settle();
  }
  out.page.other.afterSignOut = { me: signOut ? ctx.me : 'no sign-out button', hasPassword: Boolean(byId(page, 'invite_password')),
    username: byId(page, 'invite_username') ? byId(page, 'invite_username').value : null };
  const signInForm = withAttr(page, 'data-form', 'invite-sign-in')[0];
  if (signOut && signInForm && byId(page, 'invite_password')) {
    byId(page, 'invite_password').value = 'correct horse';
    await signInForm.dispatch('submit');
    await settle();
  }
  out.page.other.calls = calls;
  out.page.other.title = title(page);
}
{
  const { page, ctx, calls } = await openPage({ me: { id: 'usr_ivy', username: 'ivy' }, acceptFails: new ApiError(401, 'unauthenticated', 'Sign in.') });
  await acceptButtons(page)[0].dispatch('click');
  await settle();
  out.page.sessionEnded = { me: ctx.me, hasPassword: Boolean(byId(page, 'invite_password')), title: title(page), calls };
}
{
  const { page } = await openPage({ me: { id: 'usr_ivy', username: 'ivy' }, invite: { ...EXISTING, agent: { name: 'Ivy review bot', project: 'north', claim_limit: 1, role: 'reviewer' } } });
  out.page.agentIntro = withAttr(page, 'data-invite-intro').map(text);
}
{
  const { page, calls } = await openPage({ me: { id: 'usr_ivy', username: 'ivy' }, acceptFails: new ApiError(404, 'invite_unusable', 'This invite link cannot be used.') });
  await acceptButtons(page)[0].dispatch('click');
  await settle();
  out.page.refused = { alert: withAttr(page, 'role', 'alert').map((e) => ({ hidden: e.hidden, text: text(e) })), title: title(page), calls };
}
// A new person's invite: unchanged (.578).
{
  const NEW = { person: 'new', username: 'lena', display_name: 'Lena Park', invited_by_name: 'Olga', projects: [{ id: 'north', name: 'North' }],
    agent: null, expires_at: daysFromNow(3), password_min_length: 12 };
  const { page, calls } = await openPage({ invite: NEW });
  out.page.newPerson = {
    hasNewPassword: Boolean(byId(page, 'new_password')), hasConfirm: Boolean(byId(page, 'confirm_password')),
    signInForms: withAttr(page, 'data-form', 'invite-sign-in').length, acceptButtons: acceptButtons(page).length,
    firstLine: text(page.all((e) => e.tagName === 'P')[0]),
  };
  byId(page, 'new_password').value = 'a good long password';
  byId(page, 'confirm_password').value = 'a good long password';
  await page.all((e) => e.tagName === 'FORM')[0].dispatch('submit');
  await settle();
  out.page.newPerson.calls = calls;
  out.page.newPerson.title = title(page);
}
// A new person's invite with a reviewer agent: the page names the agent's role (kittrial-5bb.581 r2, review N4).
{
  const agents = {};
  for (const role of ['reviewer', 'worker']) {
    const NEW = { person: 'new', username: 'lena', display_name: 'Lena Park', invited_by_name: 'Olga', projects: [{ id: 'north', name: 'North' }],
      agent: { name: 'Lena bot', project: 'north', role, claim_limit: 1 }, expires_at: daysFromNow(3), password_min_length: 12 };
    const { page } = await openPage({ invite: NEW });
    agents[role] = withAttr(page, 'data-invite-agent', 'true').map(text);
  }
  out.page.newPersonAgent = agents;
}

// -- the upgraded banner --------------------------------------------------------------------------------------------
function appTransport(state) {
  return (method, path) => {
    const [p] = path.split('?');
    state.calls.push(method + ' ' + p);
    let data = { items: [] };
    if (p === '/v1/sessions/current') data = { user: { id: 'usr_alex', username: 'alex', display_name: 'Alex', superuser: false }, csrf_token: 'tok' };
    else if (p === '/v1/projects') data = { items: [HEAD_PROJECTS.alpha] };
    else if (/^\/v1\/projects\/[a-z]+$/.test(p)) data = HEAD_PROJECTS[p.split('/')[3]] || null;
    else if (/\/coordination$/.test(p)) data = { people: [], agents: [], web_rules: [], host_rules: { state: 'unknown', rules: [] } };
    else if (p === '/healthz') return Promise.resolve({ status: 200, data: { service: 'orchestra', version: '1', build_id: state.health, status: 'ok' } });
    return Promise.resolve({ status: 200, data, build: state.build });
  };
}
const banners = (root) => withAttr(root, 'data-banner', 'upgraded');
async function startApp(state) {
  const root = document.createElement('div');
  document.body.appendChild(root);
  location.hash = '#/p/alpha/people';
  await app.start(root, { transport: appTransport(state), storage: new Storage() });
  await settle();
  return root;
}
out.banner = {};
{
  const state = { build: 'build-1', health: 'build-1', calls: [] };
  const root = await startApp(state);
  await navigate('/p/alpha/agents');
  await navigate('/p/alpha/people');
  out.banner.same = { count: banners(root).length };
}
{
  const state = { build: 'build-1', health: 'build-1', calls: [] };
  const root = await startApp(state);
  const beforeChange = banners(root).length;
  state.build = 'build-2';
  await navigate('/p/alpha/agents');
  const shown = banners(root);
  out.banner.changed = { before: beforeChange, count: shown.length, role: shown[0] ? shown[0].attributes.role : null,
    text: shown[0] ? text(shown[0]) : null, reloadButtons: shown[0] ? withAttr(shown[0], 'data-action', 'reload').length : 0 };
  state.build = 'build-3';
  await navigate('/p/alpha/people');
  out.banner.changed.afterAnotherChange = banners(root).length;
  if (shown[0]) await withAttr(shown[0], 'data-action', 'reload')[0].dispatch('click');
  out.banner.changed.reloads = out.reloads || 0;
}
{
  // An empty header value is no build: no banner.
  const state = { build: 'build-1', health: 'build-1', calls: [] };
  const root = await startApp(state);
  state.build = '';
  await navigate('/p/alpha/agents');
  out.banner.emptyValue = { count: banners(root).length };
}
{
  // A tab that becomes visible asks /healthz, at most once a minute; another build_id shows the banner.
  const state = { build: 'build-1', health: 'build-1', calls: [] };
  const root = await startApp(state);
  const listeners = [...(documentListeners.visibilitychange || [])];
  const mine = listeners[listeners.length - 1];
  document.visibilityState = 'visible';
  mine(); await settle();
  mine(); await settle();
  const sameHealthCalls = state.calls.filter((c) => c === 'GET /healthz').length;
  const noBannerYet = banners(root).length;
  // A new app (no throttle spent yet) whose /healthz names another build.
  const state2 = { build: 'build-1', health: 'build-2', calls: [] };
  const root2 = await startApp(state2);
  const all = documentListeners.visibilitychange || [];
  const latest = all[all.length - 1];
  document.visibilityState = 'hidden';
  latest(); await settle();
  const hiddenCalls = state2.calls.filter((c) => c === 'GET /healthz').length;
  document.visibilityState = 'visible';
  latest(); await settle();
  out.banner.visible = { sameHealthCalls, noBannerYet, hiddenCalls,
    changedHealthCalls: state2.calls.filter((c) => c === 'GET /healthz').length, count: banners(root2).length };
}

console.log(JSON.stringify(out));
process.exit(0);
