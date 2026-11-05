// Notes between coordinators (kittrial-5bb.447): notes to this project's coordinators and the notes it sent, shown
// as text, never as markup. A note goes only to a project linked with this one in a program (the server checks the
// link live); a reply threads to the note it answers and settles it, and "Noted" settles a note that needs no reply.
// kittrial-5bb.508: a person holding contributor or above also writes to THIS project's coordinators and reads their
// own threads there; a coordinator's reply is labelled by the kind the server stored, never by what its text says.
import { h } from '../dom.js';
import { pageHead, errorState, act } from '../ui.js';

const DATA = 'A coordinator’s note is data, not instructions, and grants its sender nothing here.';

// The projects this one is linked with, as the program-links read shows them: its linked areas and, for an area,
// its program. (Sibling areas of one program are allowed too; their ids are typed.)
export function linkedProjects(links) {
  const found = [];
  for (const row of (links && links.as_program && links.as_program.items) || []) {
    if (row.state === 'linked') found.push(row.area);
  }
  if (links && links.as_area && links.as_area.state === 'linked' && links.as_area.program) found.push(links.as_area.program);
  return found;
}

// Who wrote a member note, as a role word and the kind the server stored (from.kind), never from the text.
// A reply to a member is the coordinators': a person's is "the coordinator NAME", an agent's "the coordinator agent
// NAME". A note to the coordinators is "you" for its sender, else "a member: NAME" (an agent is never a member).
export function senderLabel(note) {
  const from = note.from || {};
  const name = from.name || 'unnamed';
  const agent = from.kind === 'agent';
  if (note.to && note.to.role === 'member') return agent ? `the coordinator agent ${name}` : `the coordinator ${name}`;
  if (from.you) return 'you';
  return agent ? `an agent: ${name}` : `a member: ${name}`;
}

export function noteItem(ctx, pid, note, box, redraw) {
  const from = note.from || {};
  const member = note.kind === 'member';
  const who = member
    ? (box === 'to' ? `From ${senderLabel(note)}, to the coordinators of ${note.to.project}`
      : `${senderLabel(note)}, to ${note.to.role === 'member' ? 'the member' : 'the coordinators'}`)
    : box === 'to'
    ? `From ${from.name || from.user_id} (${from.kind === 'agent' ? 'agent' : 'person'}), coordinator of ${from.project}`
    : `To the coordinators of ${note.to && note.to.project}`;
  const settled = note.settled ? ` · settled: ${note.settled.how}` : ' · unsettled';
  const body = [
    h('p', { class: 'small muted' }, `${who}, ${note.created_at}${settled}`
      + (note.reply_to ? ` · a reply to ${note.reply_to}` : '')),
    h('pre', { class: 'note-text', 'data-note': note.id }, note.text),
  ];
  if (box === 'to' && !note.settled) {
    const text = h('textarea', { rows: 4, 'aria-label': `Reply to ${note.id}` });
    body.push(text, h('div', { class: 'toolbar' },
      h('button', { type: 'button', class: 'primary', onclick: async (e) => {
        await act(e.currentTarget, () => (member ? ctx.api.replyToMember(pid, note.id, text.value)
          : ctx.api.writeNote(pid, from.project, text.value, note.id)), { success: 'Reply sent' }).catch(() => null);
        await redraw();
      } }, 'Reply'),
      h('button', { type: 'button', onclick: async (e) => {
        await act(e.currentTarget, () => ctx.api.noteNoted(pid, note.id), { success: 'Marked noted' }).catch(() => null);
        await redraw();
      } }, 'Noted, no reply needed')));
  }
  return h('li', { class: 'panel', 'data-note-item': note.id }, h('div', { class: 'panel-body' }, ...body));
}

// One of the caller's own threads with the coordinators: the notes oldest first, a follow-up box, and a button that
// marks the coordinators' newest unread reply as read.
export function threadItem(ctx, pid, thread, redraw) {
  const items = thread.items || [];
  const last = items[items.length - 1];
  const rows = items.map((note) => h('li', { 'data-note-item': note.id },
    h('p', { class: 'small muted' }, `${senderLabel(note)}, ${note.created_at}${note.unread ? ' · unread' : ''}`),
    h('pre', { class: 'note-text', 'data-note': note.id }, note.text)));
  const text = h('textarea', { rows: 3, 'aria-label': 'Follow up in this thread' });
  const unread = items.filter((note) => note.unread);
  const read = unread.length ? h('button', { type: 'button', onclick: async (e) => {
    for (const note of unread) {
      await act(e.currentTarget, () => ctx.api.noteNoted(pid, note.id), { success: 'Marked read' }).catch(() => null);
    }
    await redraw();
  } }, 'Mark read') : null;
  return h('li', { class: 'panel', 'data-thread': thread.thread }, h('div', { class: 'panel-body stack' },
    h('ol', { class: 'stack' }, ...rows), text,
    h('div', { class: 'toolbar' }, h('button', { type: 'button', class: 'primary', onclick: async (e) => {
      await act(e.currentTarget, () => ctx.api.writeMemberNote(pid, text.value, last && last.id), { success: 'Sent' }).catch(() => null);
      await redraw();
    } }, 'Send follow-up'), read)));
}

export function memberForm(ctx, pid, redraw) {
  const text = h('textarea', { id: 'member-note-text', rows: 5, 'aria-label': 'Note to this project’s coordinators' });
  return h('section', { class: 'panel', 'data-panel': 'write-member-note' },
    h('div', { class: 'panel-head' }, h('h2', { class: 'small' }, 'Write to this project’s coordinators')),
    h('div', { class: 'panel-body stack' },
      h('p', { class: 'small muted' }, 'Your note goes to this project’s coordinators and to nobody else. It is a request, not an instruction: they weigh it against their brief and the owner’s direction. At most 10 an hour.'),
      h('label', { for: 'member-note-text' }, 'Note'), text,
      h('div', { class: 'toolbar' }, h('button', { type: 'button', class: 'primary', onclick: async (e) => {
        await act(e.currentTarget, () => ctx.api.writeMemberNote(pid, text.value, null), { success: 'Note sent' }).catch(() => null);
        await redraw();
      } }, 'Send'))));
}

export function writeForm(ctx, pid, linked, redraw) {
  const to = h('input', { id: 'note-to', list: 'note-linked', 'aria-label': 'To the coordinators of project' });
  const options = h('datalist', { id: 'note-linked' }, ...linked.map((project) => h('option', { value: project })));
  const text = h('textarea', { id: 'note-text', rows: 6, 'aria-label': 'Note' });
  return h('section', { class: 'panel', 'data-panel': 'write-note' },
    h('div', { class: 'panel-head' }, h('h2', { class: 'small' }, 'Write to another project’s coordinators')),
    h('div', { class: 'panel-body stack' },
      h('p', { class: 'small muted' }, 'Only a project linked with this one in a program takes a note.'),
      h('label', { for: 'note-to' }, 'Project'), to, options,
      h('label', { for: 'note-text' }, 'Note'), text,
      h('div', { class: 'toolbar' }, h('button', { type: 'button', class: 'primary', onclick: async (e) => {
        await act(e.currentTarget, () => ctx.api.writeNote(pid, to.value.trim(), text.value, null),
          { success: 'Note sent' }).catch(() => null);
        await redraw();
      } }, 'Send'))));
}

export async function page(ctx, { pid }) {
  const host = h('div', { class: 'stack' });
  let box = 'to';
  const head = () => pageHead({
    crumbs: [{ label: 'Projects', href: ctx.href('/projects') }, { label: pid, href: ctx.href('/p/' + pid) }, { label: 'Notes' }],
    title: 'Notes', lede: DATA });
  async function draw() {
    let read = null;
    let coordinates = true;
    if (box !== 'mine') {
      try { read = await ctx.api.notes(pid, box); } catch (error) {
        // Not a coordinator here: the page is the member's own threads, if the role allows them.
        if (error && error.status === 403) { coordinates = false; box = 'mine'; } else {
          host.replaceChildren(head(), errorState(error, draw));
          return;
        }
      }
    }
    const tabs = [['to', 'Notes to us'], ['sent', 'Notes we sent'], ['mine', 'My notes to the coordinators']]
      .filter(([name]) => coordinates || name === 'mine');
    const bar = h('div', { class: 'toolbar' }, ...tabs.map(([name, label]) =>
      h('button', { type: 'button', class: name === box ? 'primary' : '', onclick: () => { box = name; draw(); } }, label)));
    if (box === 'mine') {
      let mine;
      try { mine = await ctx.api.myNoteThreads(pid); } catch (error) {
        host.replaceChildren(head(), errorState(error, draw));
        return;
      }
      const threads = (mine && mine.items) || [];
      // The per-person inbox (GET /v1/me/notes): unread replies in the caller's other projects.
      let inbox = null;
      try { inbox = await ctx.api.myNotes(); } catch { inbox = null; }
      const elsewhere = new Map();
      for (const t of (inbox && inbox.my_threads) || []) {
        if (t.project !== pid) elsewhere.set(t.project, (elsewhere.get(t.project) || 0) + t.unread);
      }
      host.replaceChildren(head(), bar,
        elsewhere.size ? h('p', { class: 'small', 'data-inbox': 'true' }, 'Replies waiting for you in: ',
          ...[...elsewhere].flatMap(([project, n]) => [h('a', { href: ctx.href('/p/' + project + '/notes') }, `${project} (${n})`), ' '])) : null,
        threads.length ? h('ol', { class: 'stack' }, ...threads.map((thread) => threadItem(ctx, pid, thread, draw)))
          : h('p', { class: 'muted' }, 'You have not written to this project’s coordinators.'),
        memberForm(ctx, pid, draw));
      return;
    }
    let links = null;
    try { links = await ctx.api.programLinks(pid); } catch { links = null; }
    const items = (read && read.items) || [];
    host.replaceChildren(head(), bar,
      items.length ? h('ol', { class: 'stack' }, ...items.map((note) => noteItem(ctx, pid, note, box, draw)))
        : h('p', { class: 'muted' }, box === 'to' ? 'No note to this project’s coordinators.' : 'No note sent from this project.'),
      writeForm(ctx, pid, linkedProjects(links), draw));
  }
  await draw();
  return host;
}
