import { h, time, copyButton, confirmDialog } from '../dom.js';
import { pageHead, reviewChip, statusChip, priority, empty, field, setFieldError, formValues, act, roleTag } from '../ui.js';
import { agentCard, attentionOf } from './agents.js';
import { myContributionsPanel } from './proposals.js';

function workTable(ctx, rows, { showProject = true, emptyTitle, emptyBody }) {
  if (!rows.length) return empty(emptyTitle, emptyBody);
  return h('div', { class: 'table-wrap' }, h('table', null,
    h('thead', null, h('tr', null,
      h('th', { scope: 'col' }, 'Task'), showProject ? h('th', { scope: 'col', class: 'hide-narrow' }, 'Project') : null,
      h('th', { scope: 'col' }, 'State'), h('th', { scope: 'col' }, 'Next action'), h('th', { scope: 'col', class: 'hide-narrow' }, 'Updated'))),
    h('tbody', null, rows.map((t) => {
      const href = ctx.href(`/p/${t.project_id}/t/${t.id}`);
      return h('tr', { class: 'row-link', onclick: (e) => { if (e.target.tagName !== 'A') ctx.go(`/p/${t.project_id}/t/${t.id}`); } },
        h('td', null, h('a', { class: 'title', href }, t.title), h('div', { class: 'sub' }, t.priority != null ? [priority(t.priority), ' · '] : null, h('span', { class: 'mono' }, t.id))),
        showProject ? h('td', { class: 'hide-narrow' }, t.project_name || '') : null,
        h('td', null, t.review_state && t.review_state !== 'none' ? reviewChip(t.review_state) : statusChip(t.status)),
        h('td', null, t.next_action ? t.next_action.text : h('span', { class: 'muted' }, '—'),
          t.blocked ? [' ', h('span', { class: 'chip crit', title: 'The latest checkpoint lists unresolved items' }, 'Blocked')] : null,
          t.recommended ? [' ', h('span', { class: 'chip ok', title: 'A reviewer recommends approving this revision. An owner still decides.' }, 'Recommended')] : null,
          t.pending_request_ids && t.pending_request_ids.length ? h('div', { class: 'sub' }, 'Open requests: ', t.pending_request_ids.join(', ')) : null),
        // Canonical queue rows carry no update time; the review wait start is shown instead.
        h('td', { class: 'hide-narrow muted' }, t.updated_at ? time(t.updated_at) : t.waiting_since ? time(t.waiting_since) : ''));
    }))));
}

export async function home(ctx) {
  if (!ctx.projects.length) return welcome(ctx);
  const [data, contributions] = await Promise.all([ctx.api.myWork(), myContributionsPanel(ctx)]);
  const incomplete = data.truncated || (data.unavailable && data.unavailable.length);
  const revisions = data.assigned.filter((t) => t.review_state === 'changes-requested');
  const assigned = data.assigned.filter((t) => t.review_state !== 'changes-requested');
  const panel = (title, count, body, note) => h('section', { class: 'panel', 'aria-labelledby': 'h-' + title.replace(/\W+/g, '') },
    h('div', { class: 'panel-head' }, h('h2', { id: 'h-' + title.replace(/\W+/g, ''), class: 'small' }, title, ' ', h('span', { class: 'nav-count' }, count)), note ? h('span', { class: 'small muted' }, note) : null),
    body);
  const myAgents = data.agents || [];
  const needs = myAgents.filter((a) => ['feedback', 'idle'].includes(attentionOf(a))).length;
  const agentsPanel = myAgents.length ? h('section', { class: 'panel', 'aria-labelledby': 'h-agents' },
    h('div', { class: 'panel-head' }, h('h2', { class: 'small', id: 'h-agents' }, 'Your agents ', h('span', { class: 'nav-count' }, needs ? `${needs} need you` : 'all busy')),
      h('span', { class: 'small muted' }, 'As of ', new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }), ' · ', h('button', { type: 'button', class: 'link', onclick: () => ctx.render() }, 'Refresh'))),
    h('div', { class: 'panel-body agent-grid' }, myAgents.map((a) => agentCard(ctx, a, { compact: false })))) : null;
  return h('div', { class: 'stack' },
    pageHead({ title: 'My work', lede: `Everything waiting on you across ${ctx.projects.filter((p) => !p.archived).length} active project(s). Opening a task never marks it done.` }),
    incomplete ? h('div', { class: 'banner' }, 'Some projects could not be read just now, or there is more work than one page shows. Open a project to see all of its tasks.') : null,
    agentPromptPanel(ctx, data),
    contributions,
    agentsPanel,
    panel('Revisions requested', revisions.length, workTable(ctx, revisions, { emptyTitle: 'No revisions requested', emptyBody: 'When a reviewer asks for changes to your work, it appears here first.' })),
    panel('Waiting for your review', data.to_review.length, workTable(ctx, data.to_review, { emptyTitle: 'Nothing to review', emptyBody: 'Contributions to projects you own appear here until you approve them or request changes.' }), 'Stays here until you act — no reminder needed'),
    panel('Assigned to you', assigned.length, workTable(ctx, assigned, { emptyTitle: 'Nothing assigned', emptyBody: 'Claim a task from a project to start work on it.' })));
}

// "Copy prompt for my agent": one button per agent the person owns. The server builds
// each prompt from this same My work data (ids, states and actions it derives; titles
// only as sanitised labels; never a secret). A person who can only view gets a
// read-only status summary instead, and an agent with no projects gets only a short
// "no projects yet" note (kind "empty") pointing its owner at My agents.
const PROMPT_WHAT = { status: 'Status summary', empty: 'Note' };
export function agentPromptPanel(ctx, data) {
  const prompts = data.agent_prompts || [];
  const idle = prompts.filter((p) => p.kind === 'empty');
  const body = prompts.length
    ? h('div', { class: 'panel-body stack' },
      h('p', { class: 'small muted' }, 'Paste into the agent’s chat in its folder. The agent first checks its own list in Orchestra and reports any difference to you before acting.'),
      h('div', { class: 'copy-row' }, prompts.map((p) => copyButton(p.label, p.text, { ariaLabel: p.label, what: PROMPT_WHAT[p.kind] || 'Prompt' }))),
      prompts.length > idle.length ? h('p', { class: 'small muted' }, prompts.filter((p) => p.kind !== 'empty').map((p) => `${p.agent_name}: ${p.items} item(s)${p.omitted ? ` (+${p.omitted} more)` : ''}`).join(' · '),
        data.generated_at ? [' · snapshot ', time(data.generated_at)] : null) : null,
      idle.length ? h('p', { class: 'small muted' }, `${idle.map((p) => p.agent_name).join(', ')}: no projects yet. `,
        h('a', { href: ctx.href('/agents') }, 'Grant one on My agents'), ' to give it work.') : null)
    : h('div', { class: 'panel-body' }, h('p', { class: 'small muted' }, 'You have no agents yet. ',
      h('a', { href: ctx.href('/agents') }, 'Add one on My agents'), ' to copy a prompt with this work for it.'));
  return h('section', { class: 'panel', 'aria-labelledby': 'h-agent-prompts' },
    h('div', { class: 'panel-head' }, h('h2', { class: 'small', id: 'h-agent-prompts' }, 'Copy prompt for my agent')), body);
}

export async function welcome(ctx) {
  // What "start a project" means depends on the server (session.project_create).
  const session = await ctx.api.current().catch(() => null);
  const mode = (session && session.project_create) || 'create';
  const start = {
    create: ['Create a project for a piece of work. You become its owner and can invite colleagues and agents with a role: viewer, contributor or owner.', 'Create a project'],
    register: ['On this server an operator creates a project on the coordination host (admin.py add-project NAME). You then register it here: you become its owner and add colleagues and agents with a role.', 'Register a project'],
    'operator-only': ['On this server an operator creates a project on the coordination host and a superuser registers it. Ask an operator or a superuser; they then add you as a member.', null],
  }[mode] || [];
  const create = start[1] ? h('a', { class: 'btn primary', href: ctx.href('/projects') }, start[1]) : null;
  return h('div', { class: 'stack' },
    pageHead({ title: `Welcome, ${ctx.me.display_name}`, lede: 'Orchestra keeps a team’s tasks, contributions and reviews in one shared record — for people and agents alike.' }),
    h('div', { class: 'steps' },
      h('section', { class: 'step' }, h('h2', { class: 'small' }, 'Start a project'),
        h('p', { class: 'muted' }, start[0]), create ? h('div', null, create) : null),
      h('section', { class: 'step' }, h('h2', { class: 'small' }, 'Join an existing project'),
        h('p', { class: 'muted' }, 'Projects are private. Ask a project owner to add you — tell them your username:'),
        h('p', null, h('code', null, '@' + (ctx.me.username || ctx.me.id)))),
      h('section', { class: 'step' }, h('h2', { class: 'small' }, 'How work flows'),
        h('p', { class: 'muted' }, 'Define a task → someone claims it → they deliver a contribution → an owner reviews it, requesting changes or approving → it is integrated and deployed. Each of the six completion facts is recorded separately.'))));
}

// "Create a project" for an account a superuser granted that, and for a superuser. The
// project is made on the server and registered in one step; the creator is its owner.
// `creation` is the session's project_host_create: null where the server has no host.
export function hostCreatePanel(ctx, creation) {
  if (!creation || !(creation.allowed || creation.reason === 'limit' || creation.reason === 'server-limit')) return null;
  const numbers = creation.limit == null ? null : `You have created ${creation.used} of the ${creation.limit} projects you may have at one time. Archiving one frees a place.`;
  if (!creation.allowed) {
    // Two different limits: this account's own, and the server's (an operator's setting).
    const why = creation.reason === 'server-limit'
      ? 'This server is at its limit of projects, so no new one can be created. Ask an operator of the server to raise the limit or to make room.'
      : [numbers, ' Ask a superuser to raise the limit.'];
    return h('section', { class: 'panel', id: 'host-create' }, h('div', { class: 'panel-head' }, h('h2', { class: 'small' }, 'Create a project')),
      h('div', { class: 'panel-body' }, h('p', { class: 'small muted' }, why)));
  }
  const status = h('div', { class: 'banner crit', role: 'alert', hidden: true });
  const form = h('form', { class: 'form', novalidate: true },
    h('p', { class: 'small muted' }, 'This creates the project on the server and makes you its owner. It can take from a few seconds to a few minutes: the more projects the server holds, the longer. Keep this page open. You then set it up step by step.', numbers ? ' ' + numbers : ''),
    status,
    field({ id: 'new_project_id', label: 'Project name', hint: '2–24 lowercase letters or digits, beginning with a letter. It becomes the start of every task id and cannot be changed.', required: true, maxlength: 24 }),
    field({ id: 'new_project_name', label: 'Display name (optional)', hint: 'Defaults to the project name.', maxlength: 64 }),
    h('div', null, h('button', { type: 'submit', class: 'primary' }, 'Create project')));
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const values = formValues(form);
    const projectId = values.new_project_id.trim();
    if (!/^[a-z][a-z0-9]{1,23}$/.test(projectId)) return setFieldError(form, 'new_project_id', 'Enter 2–24 lowercase letters or digits, beginning with a letter.');
    setFieldError(form, 'new_project_id', '');
    status.hidden = true;
    const created = await act(form.querySelector('button'), () => ctx.api.createHostProject(projectId, values.new_project_name.trim()), {
      success: 'Project created',
      onError: (e) => {
        // A creation that stopped half way: the server's sentence names the project and says who must act.
        if (e.status === 409 && e.detail && e.detail.state === 'incomplete') { status.replaceChildren(e.message); status.hidden = false; return true; }
        // Another project is being created: nothing was done, and the same request can be sent again.
        if (e.status === 503 && e.code === 'busy') { status.replaceChildren(e.message); status.hidden = false; return true; }
        if (e.status === 422 || e.status === 409 || e.status === 403) { setFieldError(form, 'new_project_id', e.message); return true; }
        return false;
      },
    });
    if (created) { await ctx.refreshProjects(); ctx.go('/p/' + created.id + '/setup'); }
  });
  return h('section', { class: 'panel', id: 'host-create' }, h('div', { class: 'panel-head' }, h('h2', { class: 'small' }, 'Create a project')), h('div', { class: 'panel-body' }, form));
}

// Superuser only: project creations that run, stopped or finished without being registered,
// so that none is forgotten; and how full the server is.
const CREATION_CHIPS = { running: ['Being created', 'plain'], incomplete: ['Did not finish', 'warn'], stalled: ['Did not start', 'warn'],
  damaged: ['Record damaged', 'warn'], 'created-unregistered': ['Made, not registered', 'warn'] };
export async function incompleteCreations(ctx) {
  if (!ctx.me.superuser) return null;
  const found = await ctx.api.projectCreations().catch(() => null);
  const items = [...((found && found.items) || []), ...((found && found.unregistered) || [])];
  const server = found && found.server;
  if (!items.length && !server) return null;
  const needing = items.filter((item) => item.state !== 'running').length;
  return h('section', { class: 'panel', id: 'incomplete-creations' },
    h('div', { class: 'panel-head' }, h('h2', { class: 'small' }, 'Projects on the server ', needing ? h('span', { class: 'nav-count' }, needing) : null)),
    h('div', { class: 'panel-body stack' },
      server ? h('p', { class: 'small', id: 'server-usage' }, `This server holds ${server.used} of the ${server.limit} project databases its operator allows. `, h('span', { class: 'muted' }, server.note || '')) : null,
      items.length ? h('p', { class: 'small muted' }, 'Each of these was started from the web interface. Nothing is registered here for it and its name is held. An operator acts on the server; this page does not run anything.') : null,
      items.map((item) => {
        const [label, tone] = CREATION_CHIPS[item.state] || [item.state, 'warn'];
        return h('div', { class: 'card', 'data-creation': item.project, 'data-state': item.state },
          h('div', { class: 'toolbar' }, h('h2', { class: 'small mono' }, item.project), h('span', { class: 'chip ' + tone }, label)),
          h('p', { class: 'small' }, 'Started by ', item.by_name || item.by || 'unknown', item.started_at ? [' ', time(item.started_at)] : null, item.stage && item.state !== 'running' ? `; stopped at the step “${item.stage}”.` : '.'),
          item.what ? h('p', { class: 'small muted' }, item.what) : null,
          item.finish ? [h('p', { class: 'small muted' }, 'To finish it:'), h('pre', { class: 'json' }, item.finish)] : null,
          item.remove ? [h('p', { class: 'small muted' }, item.state === 'stalled' ? 'To remove it (nothing was made, so the name is free again):' : 'To remove it (the name stays retired):'), h('pre', { class: 'json' }, item.remove)] : null);
      })));
}

export async function directory(ctx) {
  await ctx.refreshProjects();
  const session = await ctx.api.current().catch(() => null);
  const mode = (session && session.project_create) || 'create';
  const showArchived = h('input', { type: 'checkbox', id: 'show-archived' });
  const archivedToggle = h('label', { class: 'check', for: 'show-archived' }, showArchived, 'Show archived projects');
  const list = h('div');
  const draw = () => {
    const items = ctx.projects.filter((p) => showArchived.checked || !p.archived);
    list.replaceChildren(items.length ? h('div', { class: 'cards' }, items.map((p) => p.usable === false
      // A project with no canonical Beads project behind it: never a link to task pages.
      ? h('div', { class: 'card' },
        h('div', { class: 'toolbar' }, h('h2', { class: 'small' }, p.name), roleTag(p.role), h('span', { class: 'chip warn' }, 'Not usable'), p.archived ? h('span', { class: 'chip plain' }, 'Archived') : null),
        h('p', { class: 'small muted' }, p.unusable_reason || 'This project cannot be used on this server.'),
        p.archived ? null : h('div', null, h('button', { type: 'button', onclick: async (event) => {
          if (await act(event.currentTarget, () => ctx.api.archiveProject(p.id), { success: 'Project archived' })) { await ctx.refreshProjects(); draw(); }
        } }, 'Archive')))
      : h('a', { class: 'card', href: ctx.href('/p/' + p.id) },
      h('div', { class: 'toolbar' }, h('h2', { class: 'small' }, p.name), roleTag(p.role), p.archived ? h('span', { class: 'chip plain' }, 'Archived') : null),
      p.description ? h('p', { class: 'small muted' }, p.description) : null,
      h('div', { class: 'card-stats' }, h('span', null, h('b', null, (p.members || []).length), ' members'), h('span', null, 'Created ', time(p.created_at)))))) :
      empty('No projects yet', 'Create one below, or ask a project owner to add you.'));
  };
  showArchived.addEventListener('change', draw);
  draw();

  const hostLine = 'On this server a project is created on the coordination host by an operator, with admin.py add-project NAME.';
  if (mode !== 'create') {
    let panel;
    // A superuser's upgrade check: records this server will not serve, with who created
    // each one and who its members are.
    const review = h('div');
    const drawReview = async () => {
      const found = mode === 'register' ? await ctx.api.unconfirmedProjects().catch(() => null) : null;
      const items = (found && found.items) || [];
      const who = (u) => '@' + (u.username || u.id);
      review.replaceChildren(items.length ? h('section', { class: 'panel' },
        h('div', { class: 'panel-head' }, h('h2', { class: 'small' }, 'Projects to confirm or archive ', h('span', { class: 'nav-count' }, items.length))),
        h('div', { class: 'panel-body stack' },
          h('p', { class: 'small muted' }, 'These project records were made by an older kit and cannot be used until you decide. Confirming keeps the current members; archiving retires the record. Removing a member or revoking a credential still works on them.'),
          items.map((p) => h('div', { class: 'card' },
            h('div', { class: 'toolbar' }, h('h2', { class: 'small' }, p.name), h('span', { class: 'mono' }, p.id), h('span', { class: 'chip warn' }, p.kind === 'unconfirmed' ? 'Needs confirmation' : 'Not usable')),
            h('p', { class: 'small muted' }, p.reason),
            h('p', { class: 'small' }, 'Created by ', who(p.created_by), p.created_at ? [' ', time(p.created_at)] : null, '. Members: ', p.members.length ? p.members.map((m) => `${who(m)} (${m.role})`).join(', ') : 'none', '.'),
            h('div', { class: 'actions' },
              p.kind === 'unconfirmed' ? h('button', { type: 'button', class: 'primary', onclick: async (event) => {
                const button = event.currentTarget;
                const ok = await confirmDialog({ title: `Confirm “${p.name}” (${p.id})?`, body: `It will use the canonical project ${p.id} again. Created by ${who(p.created_by)}. These members keep their access: ${p.members.length ? p.members.map((m) => `${who(m)} (${m.role})`).join(', ') : 'none'}. Your confirmation is recorded.`, confirmLabel: 'Confirm project' });
                if (ok && await act(button, () => ctx.api.confirmProject(p.id), { success: 'Project confirmed' })) { await ctx.refreshProjects(); draw(); drawReview(); }
              } }, 'Confirm') : null,
              h('button', { type: 'button', onclick: async (event) => {
                const button = event.currentTarget;
                const ok = await confirmDialog({ title: `Archive “${p.name}”?`, body: 'The record is retired. Members keep read access to it; nothing is deleted.', confirmLabel: 'Archive project', danger: true });
                if (ok && await act(button, () => ctx.api.archiveProject(p.id), { success: 'Project archived' })) { await ctx.refreshProjects(); draw(); drawReview(); }
              } }, 'Archive')))))) : '');
    };
    await drawReview();
    if (mode === 'register') {
      const register = h('form', { class: 'form', novalidate: true },
        h('p', { class: 'small muted' }, hostLine + ' Register it here with that NAME to use it in the web interface. Registering makes you its owner and gives nobody else access: add members afterwards.'),
        field({ id: 'project_id', label: 'Canonical project name', hint: 'The NAME given to admin.py add-project: 2–24 lowercase letters or digits, beginning with a letter.', required: true, maxlength: 24 }),
        field({ id: 'name', label: 'Display name (optional)', hint: 'Defaults to the canonical name.', maxlength: 64 }),
        h('div', null, h('button', { type: 'submit', class: 'primary' }, 'Register project')));
      register.addEventListener('submit', async (event) => {
        event.preventDefault();
        const { project_id: projectId, name } = formValues(register);
        if (!/^[a-z][a-z0-9]{1,23}$/.test(projectId.trim())) return setFieldError(register, 'project_id', 'Enter the canonical name: 2–24 lowercase letters or digits, beginning with a letter.');
        setFieldError(register, 'project_id', '');
        const created = await act(register.querySelector('button'), () => ctx.api.registerProject(projectId.trim(), name.trim()), { success: 'Project registered', onError: (e) => { if (e.status === 422 || e.status === 409) { setFieldError(register, 'project_id', e.message); return true; } return false; } });
        if (created) { await ctx.refreshProjects(); ctx.go('/p/' + created.id + '/setup'); }
      });
      panel = register;
    } else {
      panel = h('p', { class: 'small muted' }, hostLine + ' A superuser then registers it in the web interface and adds members. Ask an operator or a superuser.');
    }
    return h('div', { class: 'stack' },
      pageHead({ title: 'Projects', lede: ctx.me.superuser ? 'As a superuser you can see every project.' : 'Projects you are a member of.' }),
      h('div', { class: 'toolbar' }, archivedToggle),
      list,
      review,
      await incompleteCreations(ctx),
      hostCreatePanel(ctx, session && session.project_host_create),
      h('section', { class: 'panel' }, h('div', { class: 'panel-head' }, h('h2', { class: 'small' }, mode === 'register' ? 'Register a project' : 'New project')), h('div', { class: 'panel-body' }, panel)));
  }

  const form = h('form', { class: 'form', novalidate: true },
    field({ id: 'name', label: 'Project name', hint: '2–64 characters: letters, digits, spaces, dot, dash or underscore.', required: true, maxlength: 64 }),
    h('div', null, h('button', { type: 'submit', class: 'primary' }, 'Create project')));
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const { name } = formValues(form);
    if (name.trim().length < 2) return setFieldError(form, 'name', 'Enter a name of at least 2 characters.');
    setFieldError(form, 'name', '');
    const created = await act(form.querySelector('button'), () => ctx.api.createProject(name.trim()), { success: 'Project created', onError: (e) => { if (e.status === 422) { setFieldError(form, 'name', e.message); return true; } return false; } });
    if (created) { await ctx.refreshProjects(); ctx.go('/p/' + created.id + '/setup'); }
  });

  return h('div', { class: 'stack' },
    pageHead({ title: 'Projects', lede: ctx.me.superuser ? 'As a superuser you can see every project.' : 'Projects you are a member of.' }),
    h('div', { class: 'toolbar' }, archivedToggle),
    list,
    h('section', { class: 'panel' }, h('div', { class: 'panel-head' }, h('h2', { class: 'small' }, 'New project')), h('div', { class: 'panel-body' }, form)));
}
