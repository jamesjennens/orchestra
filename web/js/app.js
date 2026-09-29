// Orchestra web interface: boot, session, shell and hash router.
import { h, mount, brandMark, initials, confirmDialog, toast } from './dom.js';
import { createApi, fetchTransport } from './api.js';
import { describe } from './ui.js';
import * as auth from './views/auth.js';
import * as work from './views/work.js';
import * as project from './views/project.js';
import * as task from './views/task.js';
import * as admin from './views/admin.js';
import * as reqs from './views/requirements.js';
import * as agents from './views/agents.js';

const ROUTES = [
  [/^\/$/, work.home],
  [/^\/welcome$/, work.welcome],
  [/^\/projects$/, work.directory],
  [/^\/agents$/, agents.list],
  [/^\/p\/(?<pid>[\w-]+)$/, project.overview],
  [/^\/p\/(?<pid>[\w-]+)\/reviews$/, project.reviews],
  [/^\/p\/(?<pid>[\w-]+)\/feedback$/, project.feedback],
  [/^\/p\/(?<pid>[\w-]+)\/settings$/, project.settings],
  [/^\/p\/(?<pid>[\w-]+)\/new$/, task.create],
  [/^\/p\/(?<pid>[\w-]+)\/t\/(?<tid>[\w-]+)$/, task.detail],
  [/^\/p\/(?<pid>[\w-]+)\/requirements$/, reqs.brd],
  [/^\/p\/(?<pid>[\w-]+)\/requirements\/(?<rid>[\w.-]+)$/, reqs.requirement],
  [/^\/p\/(?<pid>[\w-]+)\/decisions$/, reqs.decisions],
  [/^\/p\/(?<pid>[\w-]+)\/decisions\/(?<did>[\w.-]+)$/, reqs.decision],
  [/^\/p\/(?<pid>[\w-]+)\/records\/(?<id>[\w.-]+)$/, reqs.record],
  [/^\/admin\/users$/, admin.users],
  [/^\/account$/, admin.account],
];

// Views whose server routes are not part of every deployment yet.
const RECORD_ROUTES = /^\/p\/[\w-]+\/(requirements|decisions|records)(\/|$)/;

export async function start(root, options = {}) {
  const transport = options.transport || fetchTransport;
  const api = createApi(transport);
  // Requirements, decisions and records are not served by the HTTP service yet
  // (slice 1): the production entry hides them; the prototype's mock turns them on.
  const features = { requirements: false, ...(options.features || {}) };
  const ctx = { api, me: null, projects: [], root, dirty: false, options, features };
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
    ctx.me = null; ctx.projects = [];
    showLogin();
  };
  ctx.sessionLost = () => { ctx.me = null; showLogin('Your session has ended. Sign in again to continue.'); };

  function showLogin(message) {
    mount(root, auth.login(ctx, message));
  }

  let shell;
  function buildShell(route) {
    const pidMatch = /^\/p\/([\w-]+)/.exec(route);
    const pid = pidMatch ? pidMatch[1] : null;
    const active = ctx.projects.filter((p) => !p.archived);
    const current = pid && ctx.projects.find((p) => p.id === pid);
    const link = (href, label, count) => h('a', { class: 'nav-link', href: ctx.href(href), 'aria-current': route === href ? 'page' : null },
      h('span', null, label), count ? h('span', { class: 'nav-count' }, count) : null);

    const switcher = h('select', { id: 'project-switch', class: 'project-switch', 'aria-label': 'Current project', onchange: (e) => e.target.value && ctx.go('/p/' + e.target.value) },
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
          current ? [
            link('/p/' + pid, 'Tasks'),
            link('/p/' + pid + '/reviews', 'Reviews'),
            ctx.features.requirements ? link('/p/' + pid + '/requirements', 'Requirements') : null,
            ctx.features.requirements ? link('/p/' + pid + '/decisions', 'Decisions') : null,
            link('/p/' + pid + '/feedback', 'Feedback'),
            link('/p/' + pid + '/settings', 'Members & settings'),
          ] : null),
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
    if (!ctx.me) return showLogin();
    const route = currentRoute();
    rendered = route;
    const mine = ++token;
    const main = buildShell(route);
    mount(root, ctx.options.banner ? [ctx.options.banner(ctx), shell] : shell);
    for (const [pattern, view] of ROUTES) {
      const match = pattern.exec(route);
      if (!match) continue;
      try {
        const node = await view(ctx, match.groups || {});
        if (mine !== token) return;
        mount(main, node);
      } catch (error) {
        if (mine !== token) return;
        if (error && error.status === 401) return ctx.sessionLost();
        if (!ctx.features.requirements && error && [404, 501].includes(error.status) && RECORD_ROUTES.test(route)) {
          mount(main, h('div', { class: 'panel' }, h('div', { class: 'empty', role: 'status' }, h('strong', null, 'Not available on this server'),
            h('p', null, 'Requirements, decisions and records are not served by this Orchestra server yet. Tasks, reviews and feedback work as usual.'))));
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
    showLogin(error && error.status && error.status !== 401 ? describe(error) : null);
  }
  return ctx;
}
