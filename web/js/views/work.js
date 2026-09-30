import { h, time, copyButton } from '../dom.js';
import { pageHead, reviewChip, statusChip, priority, empty, field, setFieldError, formValues, act, roleTag } from '../ui.js';
import { agentCard, attentionOf } from './agents.js';

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
        h('td', null, t.next_action ? t.next_action.text : h('span', { class: 'muted' }, '—')),
        h('td', { class: 'hide-narrow muted' }, t.updated_at ? time(t.updated_at) : ''));
    }))));
}

export async function home(ctx) {
  if (!ctx.projects.length) return welcome(ctx);
  const data = await ctx.api.myWork();
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
    agentsPanel,
    panel('Revisions requested', revisions.length, workTable(ctx, revisions, { emptyTitle: 'No revisions requested', emptyBody: 'When a reviewer asks for changes to your work, it appears here first.' })),
    panel('Waiting for your review', data.to_review.length, workTable(ctx, data.to_review, { emptyTitle: 'Nothing to review', emptyBody: 'Contributions to projects you own appear here until you approve them or request changes.' }), 'Stays here until you act — no reminder needed'),
    panel('Assigned to you', assigned.length, workTable(ctx, assigned, { emptyTitle: 'Nothing assigned', emptyBody: 'Claim a task from a project to start work on it.' })));
}

// "Copy prompt for my agent": one button per agent the person owns. The server builds
// each prompt from this same My work data (ids, states and actions it derives; titles
// only as sanitised labels; never a secret). A person who can only view gets a
// read-only status summary instead.
export function agentPromptPanel(ctx, data) {
  const prompts = data.agent_prompts || [];
  const body = prompts.length
    ? h('div', { class: 'panel-body stack' },
      h('p', { class: 'small muted' }, 'Paste into the agent’s chat in its folder. The agent first checks its own list in Orchestra and reports any difference to you before acting.'),
      h('div', { class: 'copy-row' }, prompts.map((p) => copyButton(p.label, p.text, { ariaLabel: p.label, what: p.kind === 'status' ? 'Status summary' : 'Prompt' }))),
      h('p', { class: 'small muted' }, prompts.map((p) => `${p.agent_name}: ${p.items} item(s)${p.omitted ? ` (+${p.omitted} more)` : ''}`).join(' · '),
        data.generated_at ? [' · snapshot ', time(data.generated_at)] : null))
    : h('div', { class: 'panel-body' }, h('p', { class: 'small muted' }, 'You have no agents yet. ',
      h('a', { href: ctx.href('/agents') }, 'Add one on My agents'), ' to copy a prompt with this work for it.'));
  return h('section', { class: 'panel', 'aria-labelledby': 'h-agent-prompts' },
    h('div', { class: 'panel-head' }, h('h2', { class: 'small', id: 'h-agent-prompts' }, 'Copy prompt for my agent')), body);
}

export async function welcome(ctx) {
  const create = h('a', { class: 'btn primary', href: ctx.href('/projects') }, 'Create a project');
  return h('div', { class: 'stack' },
    pageHead({ title: `Welcome, ${ctx.me.display_name}`, lede: 'Orchestra keeps a team’s tasks, contributions and reviews in one shared record — for people and agents alike.' }),
    h('div', { class: 'steps' },
      h('section', { class: 'step' }, h('h2', { class: 'small' }, 'Start a project'),
        h('p', { class: 'muted' }, 'Create a project for a piece of work. You become its owner and can invite colleagues and agents with a role: viewer, contributor or owner.'), h('div', null, create)),
      h('section', { class: 'step' }, h('h2', { class: 'small' }, 'Join an existing project'),
        h('p', { class: 'muted' }, 'Projects are private. Ask a project owner to add you — tell them your username:'),
        h('p', null, h('code', null, '@' + (ctx.me.username || ctx.me.id)))),
      h('section', { class: 'step' }, h('h2', { class: 'small' }, 'How work flows'),
        h('p', { class: 'muted' }, 'Define a task → someone claims it → they deliver a contribution → an owner reviews it, requesting changes or approving → it is integrated and deployed. Each of the six completion facts is recorded separately.'))));
}

export async function directory(ctx) {
  await ctx.refreshProjects();
  const showArchived = h('input', { type: 'checkbox', id: 'show-archived' });
  const archivedToggle = h('label', { class: 'check', for: 'show-archived' }, showArchived, 'Show archived projects');
  const list = h('div');
  const draw = () => {
    const items = ctx.projects.filter((p) => showArchived.checked || !p.archived);
    list.replaceChildren(items.length ? h('div', { class: 'cards' }, items.map((p) => h('a', { class: 'card', href: ctx.href('/p/' + p.id) },
      h('div', { class: 'toolbar' }, h('h2', { class: 'small' }, p.name), roleTag(p.role), p.archived ? h('span', { class: 'chip plain' }, 'Archived') : null),
      p.description ? h('p', { class: 'small muted' }, p.description) : null,
      h('div', { class: 'card-stats' }, h('span', null, h('b', null, (p.members || []).length), ' members'), h('span', null, 'Created ', time(p.created_at)))))) :
      empty('No projects yet', 'Create one below, or ask a project owner to add you.'));
  };
  showArchived.addEventListener('change', draw);
  draw();

  const form = h('form', { class: 'form', novalidate: true },
    field({ id: 'name', label: 'Project name', hint: '2–64 characters: letters, digits, spaces, dot, dash or underscore.', required: true, maxlength: 64 }),
    h('div', null, h('button', { type: 'submit', class: 'primary' }, 'Create project')));
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const { name } = formValues(form);
    if (name.trim().length < 2) return setFieldError(form, 'name', 'Enter a name of at least 2 characters.');
    setFieldError(form, 'name', '');
    const created = await act(form.querySelector('button'), () => ctx.api.createProject(name.trim()), { success: 'Project created', onError: (e) => { if (e.status === 422) { setFieldError(form, 'name', e.message); return true; } return false; } });
    if (created) { await ctx.refreshProjects(); ctx.go('/p/' + created.id + '/settings'); }
  });

  return h('div', { class: 'stack' },
    pageHead({ title: 'Projects', lede: ctx.me.superuser ? 'As a superuser you can see every project.' : 'Projects you are a member of.' }),
    h('div', { class: 'toolbar' }, archivedToggle),
    list,
    h('section', { class: 'panel' }, h('div', { class: 'panel-head' }, h('h2', { class: 'small' }, 'New project')), h('div', { class: 'panel-body' }, form)));
}
