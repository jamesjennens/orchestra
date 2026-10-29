// "Hand this project to a coordinator" (kittrial-5bb.442; docs/design/COORDINATOR_ONBOARDING_DESIGN.md §3).
// Steps O1-O5 and C1, read from GET /v1/projects/{id}/handover, each checked by the server from what it
// already holds. Each step says who does it and links to the existing page where it is done; this page
// never changes anything itself. An owner (or a coordinator) picks the member; anyone else sees their own.
import { h, copyButton } from '../dom.js';
import { pageHead, errorState } from '../ui.js';

export const STATE_LABELS = {
  done: ['Done', 'ok'], todo: ['To do', 'warn'], waiting: ['Waiting', 'plain'], failed: ['Needs fixing', 'warn'],
  hidden: ['Not shown to you', 'plain'],
};

// Where a step is fixed, by the server's `fix.page`. Nothing here performs the fix.
export function fixRoute(page, pid) {
  return { members: `/p/${pid}/settings`, settings: `/p/${pid}/settings`, 'my-agents': '/agents',
    'agent-setup': '/setup' }[page] || null;
}

export function stepItem(ctx, pid, item) {
  const [label, tone] = STATE_LABELS[item.state] || [item.state, 'plain'];
  const fix = item.fix || {};
  const route = fixRoute(fix.page, pid);
  const commands = Array.isArray(fix.commands) ? fix.commands.filter((c) => typeof c === 'string' && c) : [];
  return h('li', { class: 'panel handover-step', 'data-step': item.id, 'data-state': item.state },
    h('div', { class: 'panel-head' },
      h('h2', { class: 'small' }, item.title),
      h('span', { class: 'chip ' + tone }, label)),
    h('div', { class: 'panel-body' },
      h('p', null, item.detail),
      route && item.state !== 'done' && item.state !== 'hidden'
        ? h('p', null, h('a', { class: 'button', href: ctx.href(route) }, 'Open where this is done')) : null,
      ...commands.map((command) => h('div', { class: 'toolbar' },
        h('code', { class: 'mono' }, command), copyButton('Copy command', command, { what: 'Command' })))));
}

export async function page(ctx, { pid }) {
  const host = h('div', { class: 'stack' });
  let person = null;
  async function draw() {
    let data;
    try { data = await ctx.api.projectHandover(pid, person); } catch (error) {
      host.replaceChildren(
        pageHead({ crumbs: [{ label: 'Projects', href: ctx.href('/projects') }, { label: pid, href: ctx.href('/p/' + pid) }, { label: 'Hand over' }],
          title: 'Hand this project to a coordinator' }),
        errorState(error, draw));
      return;
    }
    const picker = await memberPicker(ctx, pid, data, (value) => { person = value; draw(); });
    host.replaceChildren(...[
      pageHead({
        crumbs: [{ label: 'Projects', href: ctx.href('/projects') }, { label: pid, href: ctx.href('/p/' + pid) }, { label: 'Hand over' }],
        title: 'Hand this project to a coordinator',
        lede: (data.viewer === 'self' ? 'Your steps.' : `The steps for ${data.person.name}.`)
          + ' Each step is checked by the server every time this page is read, and says who does it. This page changes nothing itself.',
      }),
      picker,
      h('ol', { class: 'stack handover-steps' }, data.steps.map((item) => stepItem(ctx, pid, item)))].filter(Boolean));
  }
  await draw();
  return host;
}

// An owner or a coordinator chooses whose walkthrough to read; anyone else reads their own and gets no picker.
async function memberPicker(ctx, pid, data, choose) {
  if (data.viewer === 'agent' || !ctx.api.members) return null;
  let members;
  try { members = await ctx.api.members(pid); } catch { return null; }
  const items = (members && members.items) || [];
  if (items.length < 2) return null;
  const select = h('select', { id: 'handover-person', 'aria-label': 'Whose walkthrough' },
    ...items.map((m) => h('option', { value: m.user_id, selected: m.user_id === data.person.id }, m.display_name || m.username)));
  select.addEventListener('change', () => choose(select.value));
  return h('div', { class: 'toolbar' }, h('label', { for: 'handover-person' }, 'Hand over to'), select);
}
