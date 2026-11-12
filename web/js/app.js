// Orchestra web interface: boot, session, shell and hash router.
import { h, mount, brandMark, initials, confirmDialog, toast } from './dom.js';
import { createApi, fetchTransport } from './api.js';
import { describe } from './ui.js';
import { matchRoute, projectOf, RECORD_ROUTES, switchTarget, fallbackMessage, SECTION_LABELS, searchLeftMessage, rememberProject, rememberedProject, forgetProject } from './routes.js';
import * as auth from './views/auth.js';
import * as work from './views/work.js';
import * as project from './views/project.js';
import * as task from './views/task.js';
import * as admin from './views/admin.js';
import * as reqs from './views/requirements.js';
import * as records from './views/records.js';
import * as agents from './views/agents.js';
import * as setup from './views/setup.js';
import * as handover from './views/handover.js';
import * as notes from './views/notes.js';
import * as program from './views/program.js';
import * as how from './views/how.js';
import * as rooms from './views/rooms.js';
import * as releases from './views/releases.js';
import * as designs from './views/designs.js';
import * as trace from './views/trace.js';
import * as invites from './views/invites.js';

// Route name -> view. Patterns live in routes.js (shared ID pattern, testable).
const VIEWS = {
  home: work.home, welcome: work.welcome, projects: work.directory, agents: agents.list, agentSetup: agents.setupWizardPage,
  project: project.overview, reviews: project.reviews, releases: releases.page, feedback: project.feedback, people: project.people, projectAgents: project.projectAgents, settings: project.settings, setup: setup.page, handover: handover.page, notes: notes.page, program: program.page, how: how.page,
  newTask: task.create, task: task.detail,
  lanes: project.lanesPage,
  requirements: reqs.brd, requirement: reqs.requirement, decisions: reqs.decisions, decision: reqs.decision, record: reqs.record,
  records: records.page, referenceRecord: records.referencePage, capabilityRecord: records.capabilityPage,
  designPage: designs.page, tracePage: trace.page,
  rooms: rooms.list, room: rooms.detail,
  users: admin.users, account: admin.account,
};

export async function start(root, options = {}) {
  const transport = options.transport || fetchTransport;
  const api = createApi(transport);
  // Direct owner requirements are served by slice A. Other record pages remain
  // prototype-only until their own backend routes are released.
  const features = { requirements: true, records: false, ...(options.features || {}) };
  const ctx = { api, me: null, projects: [], root, dirty: false, options, features, query: new URLSearchParams('') };

  // kittrial-5bb.581: the service was upgraded under this open page. One banner, kept above every later page
  // (place() puts it first), until the person reloads.
  let upgradeBanner = null;
  const place = (...nodes) => mount(root, upgradeBanner ? [upgradeBanner, ...nodes] : nodes);
  function showUpgraded() {
    if (upgradeBanner) return;
    upgradeBanner = h('div', { class: 'banner info upgrade-banner', role: 'status', 'data-banner': 'upgraded' },
      h('span', null, 'Orchestra was upgraded. Reload.'), ' ',
      h('button', { type: 'button', 'data-action': 'reload', onclick: () => { try { location.reload(); } catch { /* ignore */ } } }, 'Reload'));
    // Put it on top of the page shown now without drawing that page again (a form being typed in is kept).
    if (typeof root.prepend === 'function') root.prepend(upgradeBanner);
    else root.replaceChildren(upgradeBanner, ...Array.from(root.childNodes));
  }
  if (typeof api.onBuildChange === 'function') api.onBuildChange(showUpgraded);
  // A tab that comes back into view asks the service its build, at most once a minute.
  const HEALTH_EVERY_MS = 60 * 1000;
  let lastHealth = -Infinity;
  try {
    if (typeof document !== 'undefined' && typeof document.addEventListener === 'function') {
      document.addEventListener('visibilitychange', () => {
        if (document.visibilityState !== 'visible' || upgradeBanner) return;
        const now = Date.now();
        if (now - lastHealth < HEALTH_EVERY_MS) return;
        lastHealth = now;
        Promise.resolve().then(() => api.checkBuild()).catch(() => { /* the next answer tells */ });
      });
    }
  } catch { /* no document events: the answers still tell */ }
  let memoryRoute = '/';

  const currentRoute = () => {
    try { return (location.hash || '').replace(/^#/, '') || memoryRoute; } catch { return memoryRoute; }
  };
  let rendered = null;
  ctx.go = (route) => {
    memoryRoute = route;
    try { if (location.hash !== '#' + route) location.hash = route; } catch { /* hash unavailable: render from memory */ }
    render();
  };
  ctx.href = (route) => '#' + route;
  // kittrial-5bb.522 slice 1: the browser storage that keeps the last project (options.storage in tests).
  const storage = (() => {
    if ('storage' in options) return options.storage;
    try { return window.localStorage; } catch { return null; }
  })();
  // A view writes its filters into the URL's query without a new history entry or a re-render, so a filtered
  // list is shareable, reload-safe and survives a project switch (kittrial-5bb.522 slice 1, .502 §1.9).
  // `view` is the render that asks (ctx.currentView(), taken when the view starts): a write from a render that
  // is no longer current, or while another page's navigation is pending, writes nothing, so a late timer of a
  // page already left (the search box's 250 ms) never puts its filters on the next page.
  ctx.currentView = () => token;
  ctx.setQuery = (params, view) => {
    if (view !== token || !rendered) return;
    const path = rendered.split('?')[0];
    if (currentRoute().split('?')[0] !== path) return;
    const text = new URLSearchParams(params).toString();
    const route = path + (text ? '?' + text : '');
    memoryRoute = route;
    rendered = route;
    try {
      history.replaceState(null, '', '#' + route);
      lastHash = location.hash;
    } catch { /* hash unavailable: the route stays in memory */ }
    ctx.query = new URLSearchParams(text);
  };
  ctx.setDirty = (value) => { ctx.dirty = value; };

  // projectsRead is false when the list could not be read: that says nothing about access (kittrial-5bb.522).
  ctx.refreshProjects = async () => {
    try { ctx.projects = (await api.projects()).items || []; ctx.projectsRead = true; } catch { ctx.projects = []; ctx.projectsRead = false; }
  };
  // Who was signed in when the page now in the address bar was opened, once that person is gone (sign-out or a
  // lost session): only they return to it. null: nobody was, so whoever signs in does (a deep link opened cold).
  let pendingUser = null;

  ctx.signedIn = async (user) => {
    ctx.me = user;
    await ctx.refreshProjects();
    // A deep link opened before signing in is where the person goes once signed in (kittrial-5bb.522 Q4).
    // Only for the person it was opened for: the next person to sign in does not land on the last one's page.
    const wanted = pendingUser === null || pendingUser === user.id ? currentRoute() : '/';
    pendingUser = null;
    ctx.go(wanted && wanted !== '/' && wanted !== '/welcome' ? wanted : (ctx.projects.length ? '/' : '/welcome'));
  };
  ctx.signOut = async () => {
    try { await api.logout(); } catch { /* the session is gone either way */ }
    agents.clearWizardStorage();
    forgetProject(storage, ctx.me && ctx.me.id);
    if (ctx.me) pendingUser = ctx.me.id;   // a second click finds nobody signed in and must not clear it
    token += 1;
    ctx.me = null; ctx.projects = [];
    // The next person to sign in on this browser starts at home, not on this person's last page.
    memoryRoute = '/';
    try { history.replaceState(null, '', '#/'); } catch { /* ignore */ }
    showLogin();
  };
  ctx.sessionLost = () => {
    agents.clearWizardStorage();
    // Another tab signed out: nothing of this person's is kept, and no late write comes from the page left.
    forgetProject(storage, ctx.me && ctx.me.id);
    if (ctx.me) pendingUser = ctx.me.id;
    token += 1;
    ctx.me = null;
    showLogin('Your session has ended. Sign in again to continue.');
  };

  function showLogin(message) {
    // Nothing is rendered from the route now, so opening any route (an invite link included) renders again.
    rendered = null;
    place(auth.login(ctx, message));
  }

  // kittrial-5bb.578: an invite link (#/invite/TOKEN) opens its own signed-out page, whoever is signed in.
  const INVITE_PREFIX = '/invite/';
  function showInvite() {
    const route = currentRoute();
    if (!route.startsWith(INVITE_PREFIX)) return false;
    rendered = route;
    place(invites.invitePage(ctx, route.slice(INVITE_PREFIX.length)));
    return true;
  }

  let shell;
  function projectSections(proj) {
    return [
      ['', 'Tasks'],
      ['reviews', 'Reviews'],
      ['releases', 'Releases'],
      ctx.features.requirements && ['requirements', 'Requirements'],
      ['records', 'Records'],
      ctx.features.records && ['decisions', 'Decisions'],
      ['rooms', 'Rooms'],
      ['lanes', 'Lanes'],
      // kittrial-5bb.453: only on a project with an accepted program.area side (the server's cached flag).
      program.showsProgram(proj) && ['program', 'Program'],
      ['feedback', 'Orchestra feedback'],
      ['people', 'People'],
      ['agents', 'Agents'],
      ['settings', 'Settings'],
      ['owner', 'superuser'].includes(proj.role) && ['setup', 'Set up'],
      ['how', 'How work is found'],
    ].filter(Boolean);
  }

  // Switching project keeps the page when the target has it; otherwise the target's Tasks
  // list opens and a toast says why (the toast layer leaves the page layout alone).
  function switchProject(targetPid) {
    // Read the current route and its project's sections before ctx.go changes location.hash.
    const route = currentRoute();
    const here = ctx.projects.find((p) => p.id === projectOf(route.split('?')[0]));
    const hereSections = here ? projectSections(here) : [];
    const target = ctx.projects.find((p) => p.id === targetPid);
    const sections = target ? projectSections(target) : [];
    // Handover and Notes are not rail entries; the target is kept on them. Handover opens for every member; Notes
    // can answer "No access" to a viewer, which its page says (the section is still the one asked for).
    const t = switchTarget(route, targetPid, sections.map(([seg]) => seg).concat(target ? ['handover', 'notes'] : []));
    ctx.go(t.path);
    const lines = [];
    if (t.fellBack) {
      const name = target ? target.name : 'that project';
      const label = t.section && ((hereSections.find(([seg]) => seg === t.section) || [])[1] || (Object.hasOwn(SECTION_LABELS, t.section) ? SECTION_LABELS[t.section] : ''));
      // An unknown section (a hand-written URL) has no name to give: the page-level sentence, not the task one.
      lines.push(t.section && !label ? fallbackMessage(name, '', 'other') : fallbackMessage(name, label, t.page));
    }
    if (t.qDropped) lines.push(searchLeftMessage());
    if (lines.length) toast(lines.join(' '));
  }

  function buildShell(route) {
    route = route.split('?')[0];
    // The project the rail shows: the URL's on a project page (the URL always wins), else the last project this
    // person opened (kittrial-5bb.522 slice 1), so the switcher is orthogonal to the other menu entries.
    const pid = projectOf(route) || rememberedProject(storage, ctx.me && ctx.me.id, ctx.projectsRead ? ctx.projects : null);
    const active = ctx.projects.filter((p) => !p.archived);
    const current = pid && ctx.projects.find((p) => p.id === pid);
    const link = (href, label, count) => h('a', { class: 'nav-link', href: ctx.href(href), 'aria-current': route === href ? 'page' : null },
      h('span', null, label), count ? h('span', { class: 'nav-count' }, count) : null);

    const switcher = h('select', { id: 'project-switch', class: 'project-switch', 'aria-label': 'Current project', onchange: (e) => e.target.value && switchProject(e.target.value) },
      h('option', { value: '' }, active.length ? 'Choose a project…' : 'No projects yet'),
      active.map((p) => h('option', { value: p.id, selected: p.id === pid }, p.name)));

    const main = h('main', { class: 'main', id: 'main', tabindex: '-1' });
    shell = h('div', { class: 'shell' },
      h('a', { class: 'skip', href: '#main', onclick: (e) => { e.preventDefault(); main.focus(); } }, 'Skip to content'),
      h('nav', { class: 'rail', id: 'rail', 'aria-label': 'Main' },
        h('a', { class: 'brand', href: ctx.href('/') }, brandMark(), h('span', null, 'Orchestra')),
        h('div', { class: 'rail-section' },
          link('/', 'My work'),
          link('/agents', 'My agents'),
          link('/projects', 'All projects')),
        h('div', { class: 'rail-section' },
          h('label', { class: 'rail-label', for: 'project-switch' }, 'Project'),
          switcher,
          current ? projectSections(current).map(([seg, label]) => link('/p/' + pid + (seg ? '/' + seg : ''), label)) : null),
        ctx.me.superuser ? h('div', { class: 'rail-section' },
          h('span', { class: 'rail-label' }, 'Administration'),
          link('/admin/users', 'People & access')) : null,
        h('div', { class: 'rail-foot' },
          h('div', { class: 'who' }, h('span', { class: 'avatar', 'aria-hidden': 'true' }, initials(ctx.me.display_name)),
            h('div', { style: null }, h('div', { class: 'who-name' }, ctx.me.display_name), h('div', { class: 'small muted' }, ctx.me.superuser ? 'Superuser' : '@' + (ctx.me.username || '')))),
          h('div', { class: 'actions' },
            h('a', { class: 'btn', href: ctx.href('/account') }, 'Account'),
            h('button', { type: 'button', onclick: ctx.signOut }, 'Sign out')))),
      h('div', { class: 'content' },
        h('div', { class: 'main-top' },
          h('button', { type: 'button', class: 'menu-toggle ghost', 'aria-expanded': 'false', 'aria-controls': 'rail', onclick: (e) => { const open = shell.classList.toggle('nav-open'); e.currentTarget.setAttribute('aria-expanded', String(open)); } }, 'Menu'),
          h('a', { class: 'brand', href: ctx.href('/') }, brandMark(), h('span', null, 'Orchestra'))),
        main));
    return main;
  }

  let token = 0;
  async function render() {
    if (showInvite()) return;
    if (!ctx.me) return showLogin();
    const route = currentRoute();
    rendered = route;
    const mine = ++token;
    const main = buildShell(route);
    place(...(ctx.options.banner ? [ctx.options.banner(ctx), shell] : [shell]));
    // A route may carry a query, e.g. /p/{pid}/reviews?proposal=<key>: the path selects
    // the view and the query is the view's own state.
    const [path, search] = route.split('?');
    ctx.query = new URLSearchParams(search || '');
    const match = matchRoute(path);
    const view = match && VIEWS[match.name];
    if (view) {
      try {
        const node = await view(ctx, match.params);
        if (mine !== token) return;
        // kittrial-5bb.522 slice 1: a project page that opened selects that project for the rail's other pages
        // (after the view answered, so a session that has ended writes nothing).
        const opened = projectOf(path);
        if (opened && ctx.projects.some((p) => p.id === opened && !p.archived)) rememberProject(storage, ctx.me.id, opened);
        mount(main, node);
      } catch (error) {
        if (mine !== token) return;
        if (error && error.status === 401) return ctx.sessionLost();
        if (error && [404, 501].includes(error.status) && RECORD_ROUTES.has(match.name)) {
          mount(main, h('div', { class: 'panel' }, h('div', { class: 'empty', role: 'status' }, h('strong', null, 'Not available on this server'),
            h('p', null, 'Requirements, decisions and records are not served by this Orchestra server yet. Tasks and reviews work as usual.'))));
          document.title = 'Not available · Orchestra';
          return;
        }
        mount(main, h('div', { class: 'panel' }, h('div', { class: 'empty', role: 'alert' }, h('strong', null, 'Could not load this page'), h('p', null, describe(error)))));
      }
      const title = main.querySelector('h1');
      // The h1 names the tab, or its data-tab-title when the h1 alone would not say where it is (Tasks · Alpha).
      const named = title ? (title.getAttribute('data-tab-title') || title.textContent) : '';
      document.title = (named ? named + ' · ' : '') + 'Orchestra';
      try { window.scrollTo(0, 0); } catch { /* ignore */ }
      if (title) title.focus({ preventScroll: true });
      return;
    }
    mount(main, h('div', { class: 'panel' }, h('div', { class: 'empty' }, h('strong', null, 'Page not found'), h('a', { href: ctx.href('/') }, 'Go to My work'))));
  }
  ctx.render = render;

  let lastHash = null;
  try { lastHash = location.hash; } catch { /* ignore */ }
  window.addEventListener('hashchange', async () => {
    if (ctx.dirty) {
      const leave = await confirmDialog({ title: 'Discard your changes?', body: 'You have unsaved edits on this page.', confirmLabel: 'Discard', danger: true });
      if (!leave) { try { history.replaceState(null, '', lastHash || '#/'); } catch { /* ignore */ } return; }
      ctx.dirty = false;
    }
    try { lastHash = location.hash; } catch { /* ignore */ }
    if (currentRoute() !== rendered) render();
  });
  window.addEventListener('beforeunload', (event) => { if (ctx.dirty) { event.preventDefault(); event.returnValue = ''; } });

  try {
    const session = await api.current();
    ctx.me = session.user;
    await ctx.refreshProjects();
    if (!api.hasCsrf()) toast('Signed in, but changes need a fresh sign-in (the server did not provide a CSRF token).', 'crit');
    render();
  } catch (error) {
    if (!showInvite()) showLogin(error && error.status && error.status !== 401 ? describe(error) : null);
  }
  return ctx;
}
