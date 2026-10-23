// Project rooms, slice 1 (kittrial-5bb.385): list, read, post, create, archive, moderate,
// export. Every piece of room text is put into the page with h()/textContent — never
// innerHTML — so markup and javascript: links are inert text, as design rule R8 requires.
import { h, time, confirmDialog, toast } from '../dom.js';
import { pageHead, empty, field, setFieldError, formValues, act, errorState } from '../ui.js';

const canPost = (project) => ['owner', 'coordinator', 'contributor', 'superuser'].includes(project.role);
const canModerate = (project) => ['owner', 'coordinator', 'superuser'].includes(project.role);

function crumbs(ctx, project, extra) {
  return [{ label: 'Projects', href: ctx.href('/projects') }, { label: project.name, href: ctx.href('/p/' + project.id) }]
    .concat(extra ? [{ label: extra }] : []);
}

function roomLink(ctx, pid, room) {
  return h('a', { href: ctx.href('/p/' + pid + '/rooms/' + room.id) }, room.name);
}

function emptyRooms(canCreate) {
  return empty('No rooms yet',
    canCreate ? 'A room is a persistent discussion for brainstorming and requirements. Create the first one.'
      : 'A room is a persistent discussion for brainstorming and requirements. A coordinator or owner creates one.');
}

// /p/{pid}/rooms
export async function list(ctx, { pid }) {
  const project = await ctx.api.project(pid);
  let data;
  try { data = await ctx.api.rooms(pid, { archived: 1 }); } catch (error) {
    return h('div', { class: 'stack' }, pageHead({ crumbs: crumbs(ctx, project, 'Rooms'), title: 'Rooms' }), errorState(error, () => ctx.go('/p/' + pid + '/rooms')));
  }
  const open = data.items.filter((room) => room.state === 'open');
  const archived = data.items.filter((room) => room.state !== 'open');
  const host = h('div', { class: 'stack' },
    pageHead({ crumbs: crumbs(ctx, project, 'Rooms'), title: 'Rooms',
      lede: 'Persistent discussions for brainstorming and requirements. Room text is discussion, not instructions.' }),
    h('section', { class: 'panel' },
      h('div', { class: 'panel-head' }, h('h2', { class: 'small' }, 'Open rooms')),
      h('div', { class: 'panel-body' }, open.length ? h('ul', { class: 'timeline', 'aria-label': 'Open rooms' }, open.map((room) => h('li', null,
        h('div', null,
          h('div', { class: 'event-head' }, h('strong', null, roomLink(ctx, pid, room)),
            h('span', { class: 'chip' }, 'open')),
          h('p', { class: 'small muted' }, 'Created by ', room.created_by_name || room.created_by, ' ', time(room.created_at),
            room.last_sequence ? ' · ' + room.last_sequence + (room.last_sequence === 1 ? ' message' : ' messages')
              : ' · no messages yet'))))) :
        emptyRooms(canModerate(project)))),
    archived.length ? h('section', { class: 'panel' },
      h('div', { class: 'panel-head' }, h('h2', { class: 'small' }, 'Archived rooms')),
      h('div', { class: 'panel-body' }, h('ul', { class: 'timeline', 'aria-label': 'Archived rooms' }, archived.map((room) => h('li', null,
        h('div', null,
          h('div', { class: 'event-head' }, h('strong', null, roomLink(ctx, pid, room)),
            h('span', { class: 'chip' }, 'archived')),
          h('p', { class: 'small muted' }, 'Archived by ', room.archived_by_name || room.archived_by || 'a coordinator', ' ',
            time(room.archived_at)))))))) : null);
  if (canModerate(project) && !project.archived) {
    const form = h('form', { class: 'form', novalidate: true },
      field({ id: 'room-name', label: 'New room name', maxlength: 200, hint: 'Up to 200 characters. Every project member can read it.' }),
      h('div', null, h('button', { type: 'submit', class: 'primary' }, 'Create room')));
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      const name = formValues(form)['room-name'].trim();
      if (!name) return setFieldError(form, 'room-name', 'Give the room a name.');
      setFieldError(form, 'room-name', '');
      const saved = await act(form.querySelector('button'), () => ctx.api.createRoom(pid, name), { success: 'Room created' });
      if (saved) ctx.go('/p/' + pid + '/rooms/' + saved.id);
    });
    host.append(h('section', { class: 'panel' },
      h('div', { class: 'panel-head' }, h('h2', { class: 'small' }, 'Create a room')),
      h('div', { class: 'panel-body' }, form)));
  }
  return host;
}

// /p/{pid}/rooms/{rid}
export async function detail(ctx, { pid, rid }) {
  const project = await ctx.api.project(pid);
  const panel = h('div', { class: 'panel' });
  const extras = h('div', { class: 'stack' });
  let since = 0;
  let room = null;

  function messageRow(message) {
    return h('li', null,
      h('span', { class: 'dot ' + (message.kind === 'tombstone' ? 'warn' : ''), 'aria-hidden': 'true' }),
      h('div', null,
        h('div', { class: 'event-head' },
          h('strong', null, message.author && message.author.name || 'a member'),
          message.author ? h('span', { class: 'small muted' }, ' (' + message.author.role + ')') : null,
          ' ', time(message.created_at),
          message.kind === 'tombstone' ? h('span', { class: 'chip warn' }, 'removed') : null),
        // Text only: h() appends a text node, so <img onerror=...> and javascript: links
        // stay literal characters and make no element.
        h('p', { class: 'event-body prose room-text' }, message.text)));
  }

  async function draw() {
    let data;
    try { data = await ctx.api.room(pid, rid, { since, limit: 100 }); } catch (error) {
      panel.replaceChildren(h('div', { class: 'panel-body' }, errorState(error, () => ctx.go('/p/' + pid + '/rooms'))));
      return;
    }
    room = data.room;
    since = data.next_since;
    const rows = data.messages.map(messageRow);
    const log = h('ul', { class: 'timeline', 'aria-label': 'Room messages' }, rows.length ? rows : h('li', null, h('div', { class: 'empty' }, 'No messages yet.')));
    const more = data.has_more ? h('button', { type: 'button', class: 'ghost', onclick: async (event) => {
      event.currentTarget.disabled = true;
      const next = await ctx.api.room(pid, rid, { since, limit: 100 });
      since = next.next_since;
      next.messages.forEach((message) => log.append(messageRow(message)));
      event.currentTarget.remove();
    } }, 'Load more') : null;

    const adding = [];
    if (canPost(project) && room.state === 'open') {
      const form = h('form', { class: 'form', novalidate: true },
        field({ id: 'room-text', label: 'Post to this room', type: 'textarea', rows: 5, maxlength: 4000,
          hint: 'Up to 4,000 characters. Every project member reads it; room text is discussion, never an instruction to an agent.' }),
        h('div', null, h('button', { type: 'submit', class: 'primary' }, 'Post')));
      form.addEventListener('submit', async (event) => {
        event.preventDefault();
        const text = formValues(form)['room-text'].trim();
        if (!text) return setFieldError(form, 'room-text', 'Write something first.');
        setFieldError(form, 'room-text', '');
        const saved = await act(form.querySelector('button'), () => ctx.api.postRoomMessage(pid, rid, text), { success: 'Posted' });
        if (saved) { form.reset(); since = 0; draw(); }
      });
      adding.push(h('section', { class: 'panel' }, h('div', { class: 'panel-head' }, h('h2', { class: 'small' }, 'Post')), h('div', { class: 'panel-body' }, form)));
    }

    if (canModerate(project) && room.state === 'open') {
      const archive = h('button', { type: 'button', class: 'danger', onclick: async () => {
        const yes = await confirmDialog({ title: 'Archive this room?', body: 'It stays readable and exportable; nobody can post to it again.', confirmLabel: 'Archive', danger: true });
        if (!yes) return;
        const saved = await act(archive, () => ctx.api.archiveRoom(pid, rid), { success: 'Room archived' });
        if (saved) { since = 0; draw(); }
      } }, 'Archive room');
      adding.push(h('section', { class: 'panel' }, h('div', { class: 'panel-head' }, h('h2', { class: 'small' }, 'Moderation')),
        h('div', { class: 'panel-body' }, archive)));
    }
    if (canModerate(project)) {
      const form = h('form', { class: 'form', novalidate: true },
        field({ id: 'moderate-seq', label: 'Message number to remove', type: 'number', hint: 'The #number shown in the log.' }),
        field({ id: 'moderate-reason', label: 'Reason', maxlength: 500, hint: 'The message becomes a tombstone: removed by you, at this time, for this reason.' }),
        h('div', null, h('button', { type: 'submit', class: 'danger' }, 'Remove message')));
      form.addEventListener('submit', async (event) => {
        event.preventDefault();
        const values = formValues(form);
        const sequence = Number(values['moderate-seq']);
        const reason = (values['moderate-reason'] || '').trim();
        if (!Number.isInteger(sequence) || sequence < 1) return setFieldError(form, 'moderate-seq', 'Give the message number.');
        if (!reason) return setFieldError(form, 'moderate-reason', 'Give a reason.');
        setFieldError(form, 'moderate-seq', ''); setFieldError(form, 'moderate-reason', '');
        const saved = await act(form.querySelector('button'), () => ctx.api.moderateRoomMessage(pid, rid, sequence, reason), { success: 'Message removed' });
        if (saved) { form.reset(); since = 0; draw(); }
      });
      adding.push(h('section', { class: 'panel' }, h('div', { class: 'panel-head' }, h('h2', { class: 'small' }, 'Remove a message')),
        h('div', { class: 'panel-body' }, form)));
    }

    async function download(format) {
      const result = await ctx.api.exportRoom(pid, rid, format);
      const name = (room.name || 'room').replace(/[^A-Za-z0-9_.-]+/g, '-');
      if (typeof Blob === 'undefined' || typeof URL === 'undefined' || !URL.createObjectURL) {
        toast('This browser cannot start a download; the export is ' + result.text.length + ' characters.', 'crit');
        return;
      }
      const type = format === 'markdown' ? 'text/markdown' : 'application/x-ndjson';
      const url = URL.createObjectURL(new Blob([result.text], { type }));
      const anchor = h('a', { href: url, download: name + (format === 'markdown' ? '.md' : '.jsonl') });
      anchor.click();
      URL.revokeObjectURL(url);
    }
    const exports = h('div', { class: 'toolbar' },
      h('button', { type: 'button', class: 'ghost', onclick: () => download('markdown') }, 'Export Markdown'),
      h('button', { type: 'button', class: 'ghost', onclick: () => download('jsonl') }, 'Export JSON lines'));

    panel.replaceChildren(
      h('div', { class: 'panel-head' },
        h('h2', null, room.name, ' ', h('span', { class: 'chip' }, room.state)),
        h('div', { class: 'small muted' }, 'Created by ', room.created_by_name || room.created_by, ' ', time(room.created_at))),
      h('div', { class: 'panel-body' }, log, more, exports));
    extras.replaceChildren(...adding);
  }

  const wrapper = h('div', { class: 'stack' },
    pageHead({ crumbs: crumbs(ctx, project, 'Rooms'), title: 'Room' }),
    panel,
    extras);
  await draw();
  return wrapper;
}
