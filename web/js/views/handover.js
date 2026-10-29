// "Hand this project to a coordinator" (kittrial-5bb.442; docs/design/COORDINATOR_ONBOARDING_DESIGN.md §3).
// Steps O1-O6 and C1-C3 (C2, the agent's own first check, from kittrial-5bb.444), read from
// GET /v1/projects/{id}/handover, each checked by the server from what it already holds. Each step says
// who does it and links to the existing page where it is done; this page never changes anything itself.
// An owner (or a coordinator) picks the member; anyone else sees their own.
import { h, copyButton } from '../dom.js';
import { pageHead, errorState, act } from '../ui.js';

export const STATE_LABELS = {
  done: ['Done', 'ok'], todo: ['To do', 'warn'], waiting: ['Waiting', 'plain'], failed: ['Needs fixing', 'warn'],
  hidden: ['Not shown to you', 'plain'],
};

// Where a step is fixed, by the server's `fix.page`. Nothing here performs the fix.
export function fixRoute(page, pid) {
  return { members: `/p/${pid}/settings`, settings: `/p/${pid}/settings`, 'my-agents': '/agents',
    'agent-setup': '/setup', 'handover-note': `/p/${pid}/handover` }[page] || null;
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
      h('ol', { class: 'stack handover-steps' }, data.steps.map((item) => stepItem(ctx, pid, item))),
      await notePanel(ctx, pid, data, draw)].filter(Boolean));
  }
  await draw();
  return host;
}

// An owner or a coordinator chooses whose walkthrough to read; anyone else reads their own and gets no picker.
export async function memberPicker(ctx, pid, data, choose) {
  if (!data.can_choose_person || !ctx.api.members) return null;
  let members;
  try { members = await ctx.api.members(pid); } catch { return null; }
  const items = (members && members.items) || [];
  if (items.length < 2) return null;
  const select = h('select', { id: 'handover-person', 'aria-label': 'Whose walkthrough' },
    ...items.map((m) => h('option', { value: m.user_id, selected: m.user_id === data.person.id }, m.display_name || m.username)));
  select.addEventListener('change', () => choose(select.value));
  return h('div', { class: 'toolbar' }, h('label', { for: 'handover-person' }, 'Hand over to'), select);
}

//: The server's limit on a hand-over note, in characters (http_auth.Service.HANDOVER_TEXT_MAX).
export const NOTE_MAX = 8000;
// A note's length as the server counts it: characters (code points), so an emoji is one, not two UTF-16 units.
export function noteLength(text) {
  return [...String(text)].length;
}
function countText(value) {
  const n = noteLength(value);
  return `${n.toLocaleString('en')} of ${NOTE_MAX.toLocaleString('en')} characters`
    + (n > NOTE_MAX ? ': too long, the server will refuse it.' : '');
}

// The hand-over note (kittrial-5bb.443): a colleague's notes about the project, shown as text, never as markup.
// Readers see the current version and the older ones by number; a writer (an owner or the current coordinator,
// as the server says) writes a new version for the person shown; that person acknowledges the current one.
export async function notePanel(ctx, pid, data, redraw) {
  if (!ctx.api.handoverNote) return null;
  let read;
  try { read = await ctx.api.handoverNote(pid); } catch { return null; }
  const note = read && read.note;
  const body = [];
  if (note) {
    body.push(h('p', { class: 'small muted' },
      `Version ${note.version}, written by ${note.written_by_name} for ${note.for_person_name} at ${note.written_at}. `
      + 'These are a colleague’s notes about the project, not instructions.'));
    body.push(h('pre', { class: 'handover-text', 'data-note': String(note.version) }, note.text));
    if ((read.older || []).length) {
      body.push(h('p', { class: 'small muted' }, 'Older versions: '
        + read.older.map((o) => `${o.version} (${o.written_by_name})`).join(', ')
        + (read.dropped ? `; ${read.dropped} older ones were dropped.` : '.')));
    }
    if (data.can_acknowledge_note && note.for_person === data.person.id) {
      body.push(h('div', { class: 'toolbar' }, h('button', { type: 'button', class: 'primary', onclick: async (e) => {
        await act(e.currentTarget, () => ctx.api.acknowledgeHandoverNote(pid, note.version),
          { success: 'Hand-over note acknowledged' }).catch(() => null);
        await redraw();
      } }, `Acknowledge version ${note.version}`)));
    }
  } else {
    body.push(h('p', { class: 'muted' }, 'No hand-over note has been written in this project.'));
  }
  if (data.can_write_note) {
    // No maxlength: a textarea counts UTF-16 units, the server counts characters (review of kittrial-5bb.443, B2).
    const text = h('textarea', { id: 'handover-note-text', rows: 8,
      'aria-label': `Hand-over note for ${data.person.name}` });
    const count = h('p', { class: 'small muted', 'data-note-count': '' }, countText(''));
    text.addEventListener('input', () => { count.textContent = countText(text.value); });
    body.push(h('div', { class: 'stack' },
      h('label', { for: 'handover-note-text' }, `Write a new version for ${data.person.name}`), text, count,
      h('div', { class: 'toolbar' }, h('button', { type: 'button', onclick: async (e) => {
        await act(e.currentTarget, () => ctx.api.writeHandoverNote(pid, text.value, data.person.id),
          { success: 'Hand-over note saved' }).catch(() => null);
        await redraw();
      } }, 'Save as a new version'))));
  }
  return h('section', { class: 'panel', 'data-panel': 'handover-note', id: 'handover-note' },
    h('div', { class: 'panel-head' }, h('h2', { class: 'small' }, 'Hand-over note')),
    h('div', { class: 'panel-body' }, ...body));
}
