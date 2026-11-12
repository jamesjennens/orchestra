// Orchestra web interface: boot, session, shell and hash router.
import { h, mount, brandMark, initials, confirmDialog, toast } from './dom.js';
import { createApi, fetchTransport } from './api.js';
import { describe } from './ui.js';
import { matchRoute, projectOf, RECORD_ROUTES, switchTarget, fallbackMessage } from './routes.js';
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
  project: project.overview, reviews: project.reviews, releases: releases.page, feedback: project.feedback, settings: project.settings, setup: setup.page, handover: handover.page, notes: notes.page, program: program.page, how: how.page,
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
  ctx.setDirty = (value) => { ctx.dirty = value; };

  ctx.refreshProjects = async () => {
    try { ctx.projects = (await api.projects()).items || []; } catch { ctx.projects = []; }
  };

  ctx.signedIn = async (user) => {
    ctx.me = user;
    await ctx.refreshProjects();
    ctx.go(ctx.projects.length ? '/' : '/welcome');
  };
  ctx.signOut = async () => {
    try { await api.logout(); } catch { /* the session is gone either way */ }
    agents.clearWizardStorage();
    ctx.me = null; ctx.projects = [];
    showLogin();
  };
  ctx.sessionLost = () => {
    agents.clearWizardStorage();
    ctx.me = null;
    showLogin('Your session has ended. Sign in again to continue.');
  };

  function showLogin(message) {
    mount(root, auth.login(ctx, message));
  }

  // kittrial-5bb.578: an invite link (#/invite/TOKEN) opens its own signed-out page, whoever is signed in.
  const INVITE_PREFIX = '/invite/';
  function showInvite() {
    const route = currentRoute();
    if (!route.startsWith(INVITE_PREFIX)) return false;
    rendered = route;
    mount(root, invites.invitePage(ctx, route.slice(INVITE_PREFIX.length)));
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
      ['settings', 'Members & settings'],
      ['owner', 'superuser'].includes(proj.role) && ['setup', 'Set up'],
      ['how', 'How work is found'],
    ].filter(Boolean);
  }

  // Switching project keeps the page when the target has it; otherwise the target's Tasks
  // list opens and a toast says why (the toast layer leaves the page layout alone).
  function switchProject(targetPid) {
    // Read the current route and its project's sections before ctx.go changes location.hash.
    const route = currentRoute();
    const here = ctx.projects.find((p) => p.id === projectOf(route));
    const hereSections = here ? projectSections(here) : [];
    const target = ctx.projects.find((p) => p.id === targetPid);
    const sections = target ? projectSections(target) : [];
    const t = switchTarget(route, targetPid, sections.map(([seg]) => seg));
    ctx.go(t.path);
    if (t.fellBack) {
      const name = target ? target.name : 'that project';
      const label = t.section && (hereSections.find(([seg]) => seg === t.section) || [])[1];
      toast(fallbackMessage(name, label));
    }
  }

  function buildShell(route) {
    route = route.split('?')[0];
    const pid = projectOf(route);
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
    mount(root, ctx.options.banner ? [ctx.options.banner(ctx), shell] : shell);
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
      document.title = (title ? title.textContent + ' · ' : '') + 'Orchestra';
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
