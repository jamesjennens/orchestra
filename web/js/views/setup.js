// "Set up this project" (kittrial-5bb.118): what is done and what is left before people
// and agents can work in a project, read from GET /v1/projects/{id}/setup. Each step
// says who can do it and either links to where it is done or shows the exact command
// for the operator. The page changes one thing itself: where the repository is.
import { h, copyButton } from '../dom.js';
import { pageHead, field, setFieldError, formValues, act, errorState } from '../ui.js';

// What each state reads as. `optional` and the three "cannot say" states are not left to do.
export const STATE_LABELS = {
  done: ['Done', 'ok'], todo: ['To do', 'warn'], optional: ['Optional', 'plain'],
  unknown: ['Could not check', 'plain'], unavailable: ['Not available on this server', 'plain'],
  'not-applicable': ['Not applicable here', 'plain'],
};

// Where a step is done in the web interface, by step id. The operator steps have none.
export function stepRoute(id, pid) {
  return { members: `/p/${pid}/settings`, 'first-task': `/p/${pid}/new`, agent: '/agents' }[id] || null;
}

export function summary(data) {
  const left = data.remaining;
  // A step the server could not check is neither done nor left to do: say so, so that
  // "nothing is left" is never shown over a step nobody could read.
  const unchecked = data.unchecked || 0;
  const could = unchecked === 1 ? '1 step could not be checked.' : `${unchecked} steps could not be checked.`;
  if (left === 0) return unchecked ? could : 'Nothing is left to do here.';
  const steps = left === 1 ? '1 step is left.' : `${left} steps are left.`;
  return unchecked ? `${steps} ${could}` : steps;
}

function repositoryForm(ctx, pid, current, redraw) {
  const form = h('form', { class: 'stack', novalidate: true },
    field({ id: 'repository', label: 'Repository location', value: current || '', maxlength: 300,
      placeholder: 'git@host:team/project.git or https://host/team/project.git',
      hint: 'Where the code lives, for example the address you would give to git clone: https://host/path, ssh://user@host/path, user@host:path, or a path beginning with /. No password or token: give the location only.' }),
    h('div', { class: 'actions' },
      h('button', { type: 'submit', class: 'primary' }, current ? 'Change' : 'Record'),
      current ? h('button', { type: 'button', class: 'ghost', onclick: async (event) => {
        await act(event.currentTarget, () => ctx.api.setRepository(pid, null), { success: 'Repository location cleared' });
        await redraw();
      } }, 'Clear') : null));
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const value = (formValues(form).repository || '').trim();
    if (!value) return setFieldError(form, 'repository', 'Enter where the repository is.');
    setFieldError(form, 'repository', '');
    const saved = await act(form.querySelector('button'), () => ctx.api.setRepository(pid, value), {
      success: 'Repository location recorded',
      onError: (error) => { if (error.status === 422) { setFieldError(form, 'repository', error.message); return true; } return false; },
    });
    if (saved) await redraw();
  });
  return form;
}

// An owner writes the project's onboarding text here. The server stores it under a line
// that says an owner wrote it; this form shows and edits the owner's own text only.
function onboardingForm(ctx, pid, redraw) {
  const host = h('div', { class: 'stack', id: 'onboarding-form' });
  (async () => {
    let current;
    try { current = await ctx.api.onboarding(pid); } catch { return; }
    const byOperator = current.source === 'operator';
    const form = h('form', { class: 'stack', novalidate: true },
      byOperator ? h('p', { class: 'small muted' }, 'An operator set the onboarding text on the server. Saving text here replaces it with yours.') : null,
      field({ id: 'onboarding', label: 'Onboarding text', type: 'textarea', rows: 8, value: current.text || '',
        hint: 'What a new worker should read first about this project. Plain text. Workers see it as information written by a project owner, not as an instruction from the server’s operator.' }),
      h('div', { class: 'actions' },
        h('button', { type: 'submit', class: 'primary' }, current.source === 'web' ? 'Save' : 'Set onboarding text'),
        current.source === 'web' ? h('button', { type: 'button', class: 'ghost', onclick: async (event) => {
          if (await act(event.currentTarget, () => ctx.api.clearOnboarding(pid), { success: 'Onboarding text removed' })) await redraw();
        } }, 'Remove') : null));
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      const value = formValues(form).onboarding || '';
      if (!value.trim()) return setFieldError(form, 'onboarding', 'Enter the onboarding text.');
      setFieldError(form, 'onboarding', '');
      const saved = await act(form.querySelector('button'), () => ctx.api.setOnboarding(pid, value), {
        success: 'Onboarding text saved',
        onError: (error) => { if (error.status === 422) { setFieldError(form, 'onboarding', error.message); return true; } return false; },
      });
      if (saved) await redraw();
    });
    host.replaceChildren(form);
  })();
  return host;
}

function stepItem(ctx, pid, item, redraw) {
  const [label, tone] = STATE_LABELS[item.state] || [item.state, 'plain'];
  const route = stepRoute(item.id, pid);
  const body = [h('p', null, item.detail)];
  // The value is accepted, and part of it looks like an access token: say so plainly.
  if (item.warning) body.push(h('div', { class: 'banner crit', role: 'alert', 'data-warning': item.id }, item.warning));
  if (item.id === 'onboarding' && item.who === 'owner-or-operator') body.push(onboardingForm(ctx, pid, redraw));
  if (item.id === 'repository') body.push(repositoryForm(ctx, pid, (ctx.setupProject || {}).repository, redraw));
  else if (route && item.state !== 'done') body.push(h('p', null, h('a', { class: 'btn', href: ctx.href(route) }, 'Go there')));
  else if (route) body.push(h('p', { class: 'small' }, h('a', { href: ctx.href(route) }, 'Open')));
  // Text to copy; nothing on this page runs it. Each entry says what it is: a shell command, a shell
  // command with words to replace, or a line of a unit file, which is not a command (kittrial-5bb.200).
  const entries = Array.isArray(item.commands) && item.commands.length ? item.commands
    : (item.command ? [{ kind: 'shell', label: 'Command', text: item.command, note: null }] : []);
  for (const one of entries) {
    const line = one.kind === 'unit-line';
    body.push(h('div', { class: 'stack', 'data-command': one.kind },
      h('p', { class: 'small' }, h('strong', null, one.label)),
      h('pre', { class: 'json' }, one.text),
      h('div', null, copyButton(line ? 'Copy line' : 'Copy command', one.text, { what: line ? 'Line' : 'Command' })),
      one.note ? h('p', { class: 'small muted' }, one.note) : null));
  }
  if (item.note) body.push(h('p', { class: 'small muted' }, item.note));
  return h('li', { class: 'panel', 'data-step': item.id, 'data-state': item.state },
    h('div', { class: 'panel-head' },
      h('h2', { class: 'small' }, item.title),
      h('span', { class: 'chip ' + tone }, label)),
    h('div', { class: 'panel-body stack' },
      h('p', { class: 'small muted' }, 'Who: ' + item.who_text + '.'),
      body));
}

export async function page(ctx, { pid }) {
  const host = h('div', { class: 'stack' });
  async function draw() {
    let data;
    try { data = await ctx.api.projectSetup(pid); } catch (error) {
      host.replaceChildren(
        pageHead({ crumbs: [{ label: 'Projects', href: ctx.href('/projects') }, { label: pid, href: ctx.href('/p/' + pid) }, { label: 'Set up' }], title: 'Set up this project' }),
        error.status === 403 ? h('div', { class: 'banner' }, 'Only an owner of this project, or a superuser, can see its setup steps.') : errorState(error, draw));
      return;
    }
    ctx.setupProject = data.project;
    host.replaceChildren(
      pageHead({
        crumbs: [{ label: 'Projects', href: ctx.href('/projects') }, { label: data.project.name, href: ctx.href('/p/' + pid) }, { label: 'Set up' }],
        title: 'Set up this project',
        lede: summary(data) + ' Each step says who can do it. Steps done on the server show the command for an operator; this page does not run anything there.',
      }),
      h('ol', { class: 'stack setup-steps' }, data.steps.map((item) => stepItem(ctx, pid, item, draw))));
  }
  await draw();
  return host;
}

// The line a project page shows its owners while steps are left. Null when nothing is
// left, when the caller is not an owner, or when the setup read fails (it is a hint).
export async function setupHint(ctx, project) {
  if (!['owner', 'superuser'].includes(project.role) || project.archived || project.usable === false) return null;
  let data;
  try { data = await ctx.api.projectSetup(project.id); } catch { return null; }
  if (!data.remaining && !data.unchecked) return null;
  const unchecked = data.unchecked || 0;
  const parts = [];
  if (data.remaining) parts.push(summary({ remaining: data.remaining }).replace(/\.$/, '') + ' to set this project up.');
  if (unchecked) parts.push(unchecked === 1 ? '1 setup step could not be checked.' : `${unchecked} setup steps could not be checked.`);
  return h('div', { class: 'banner' }, parts.join(' ') + ' ',
    h('a', { href: ctx.href(`/p/${project.id}/setup`) }, 'See the setup steps'));
}
