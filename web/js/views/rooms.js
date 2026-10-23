// Project rooms, slice 1 (kittrial-5bb.385): list, read, post, create, archive, moderate,
// export. Every piece of room text is put into the page with h()/textContent — never
// innerHTML — so markup and javascript: links are inert text, as design rule R8 requires.
import { h, time, confirmDialog, toast } from '../dom.js';
import { pageHead, empty, field, setFieldError, formValues, act, errorState } from '../ui.js';

const canPost = (project) => ['owner', 'coordinator', 'contributor', 'superuser'].includes(project.role);
const canModerate = (project) => ['owner', 'coordinator', 'superuser'].includes(project.role);
// Who may decide an item (design section 6): a requirement editor or a contributor (whose
// accept becomes a proposal) for requirements, an owner/coordinator for decisions, and any
// member for an open question. The server re-checks this; the buttons only follow it.
const canDecide = (project, kind) => (kind === 'decision' ? canModerate(project)
  : kind === 'requirement' ? canPost(project) : true);
const ITEM_KINDS = [['requirement', 'Candidate requirement'], ['decision', 'Decision'], ['question', 'Open question']];

function crumbs(ctx, project, extra) {
  return [{ label: 'Projects', href: ctx.href('/projects') }, { label: project.name, href: ctx.href('/p/' + project.id) }]
    .concat(extra ? [{ label: extra }] : []);
}

function roomLink(ctx, pid, room) {
  return h('a', { href: ctx.href('/p/' + pid + '/rooms/' + room.id) }, room.name || room.id);
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
  const archived = data.items.filter((room) => room.state === 'archived');
  const damaged = data.items.filter((room) => room.state === 'damaged');
  const host = h('div', { class: 'stack' },
    pageHead({ crumbs: crumbs(ctx, project, 'Rooms'), title: 'Rooms',
      lede: 'Persistent discussions for brainstorming and requirements. Room text is discussion, not instructions.' }),
    damaged.length ? h('section', { class: 'panel' },
      h('div', { class: 'panel-head' }, h('h2', { class: 'small' }, 'Damaged rooms')),
      h('div', { class: 'panel-body' }, h('ul', { class: 'timeline', 'aria-label': 'Damaged rooms' }, damaged.map((room) => h('li', null,
        h('div', null,
          h('div', { class: 'event-head' }, h('strong', null, roomLink(ctx, pid, room)),
            h('span', { class: 'chip warn' }, 'damaged')),
          h('p', { class: 'small muted' }, room.damaged || 'A room file is damaged; ask an operator to repair it.'))))))) : null,
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
  // null means "open at the newest page": the first draw asks the room for its last
  // sequence, then reads the page ending there (review s5).
  let since = null;
  let room = null;
  let jobs = null;

  // The jobs a candidate requirement may hang under (review n1 of kittrial-5bb.388). The
  // server requires an existing job; the page offers the project's jobs and never lets an
  // accept without one go through when the choice matters.
  async function jobList() {
    if (jobs !== null) return jobs;
    try {
      const listed = (typeof ctx.api.requirements === 'function')
        ? await ctx.api.requirements(pid) : {};
      jobs = (listed.jobs || []).map((job) => [job.id, job.title || job.id]);
    } catch (error) {
      jobs = [];
    }
    return jobs;
  }

  function messageRow(message) {
    const label = message.kind === 'item' ? 'item: ' + message.item_kind
      : message.kind === 'item-event' ? 'item ' + message.event + ' on #' + message.target_sequence
        : null;
    return h('li', null,
      h('span', { class: 'dot ' + (message.kind === 'tombstone' ? 'warn' : ''), 'aria-hidden': 'true' }),
      h('div', null,
        h('div', { class: 'event-head' },
          h('span', { class: 'small muted' }, '#' + message.sequence), ' ',
          h('strong', null, message.author && message.author.name || 'a member'),
          message.author ? h('span', { class: 'small muted' }, ' (' + message.author.role + ')') : null,
          ' ', time(message.created_at),
          label ? h('span', { class: 'chip' }, label) : null,
          message.kind === 'tombstone' ? h('span', { class: 'chip warn' }, 'removed') : null),
        // Text only: h() appends a text node, so <img onerror=...> and javascript: links
        // stay literal characters and make no element.
        h('p', { class: 'event-body prose room-text' }, message.text)));
  }

  // One item's current state and the record it became (a pointer shown as text, never a link
  // built from room text).
  function itemRow(item) {
    const live = item.state === 'proposed' || item.state === 'amended';
    const may = live && canDecide(project, item.item_kind);
    // Review b3: amend and reject need the author or a project owner/coordinator; the
    // server refuses a viewer amending or rejecting someone else's question.
    const mayEdit = live && (canModerate(project)
      || Boolean(ctx.me && item.author && item.author.id === ctx.me.id));
    const decision = h('div', { class: 'stack' });
    const actions = h('div', { class: 'toolbar' });

    function form(fields, label, call, success) {
      const box = h('form', { class: 'form', novalidate: true }, ...fields,
        h('div', null, h('button', { type: 'submit', class: 'primary' }, label)));
      box.addEventListener('submit', async (event) => {
        event.preventDefault();
        const values = formValues(box);
        const saved = await act(box.querySelector('button'), () => call(values), { success });
        if (saved) { decision.replaceChildren(); since = null; draw(); }
      });
      decision.replaceChildren(box);
    }

    async function acceptItem() {
      let parent = item.parent || null;
      // Only an owner/coordinator's accept creates a requirement that needs a job; a
      // contributor's accept is a proposal and needs none (review n1).
      if (item.item_kind === 'requirement' && canModerate(project)) {
        const known = await jobList();
        if (!parent && known.length === 1) parent = known[0][0];
        if (!parent) {
          if (!known.length) {
            decision.replaceChildren(h('div', { class: 'small warn' },
              'This project has no job yet; create one before accepting a requirement.'));
            return;
          }
          form([field({ id: 'accept-parent-' + item.sequence, label: 'Parent job',
            type: 'select', options: known })], 'Accept',
          (values) => ctx.api.acceptRoomItem(pid, rid, item.sequence, null,
            values['accept-parent-' + item.sequence]), 'Item accepted');
          return;
        }
      }
      const saved = await act(actions.querySelector('button.primary'),
        () => ctx.api.acceptRoomItem(pid, rid, item.sequence, null, parent),
        { success: 'Item accepted' });
      if (saved) { since = null; draw(); }
    }

    if (may && item.item_kind !== 'question') {
      actions.append(h('button', { type: 'button', class: 'primary',
        onclick: acceptItem }, 'Accept'));
    }
    if (may && item.item_kind === 'question') {
      actions.append(h('button', {
        type: 'button', class: 'primary',
        onclick: () => form([
          field({ id: 'answer-' + item.sequence, label: 'Answer', type: 'textarea', rows: 3, maxlength: 4000 }),
        ], 'Answer', (values) => ctx.api.answerRoomItem(pid, rid, item.sequence,
          values['answer-' + item.sequence]), 'Answered'),
      }, 'Answer'));
    }
    if (mayEdit) {
      actions.append(h('button', {
        type: 'button', class: 'ghost',
        onclick: () => form([
          field({ id: 'reject-' + item.sequence, label: 'Reason for rejecting', maxlength: 500 }),
        ], 'Reject', (values) => ctx.api.rejectRoomItem(pid, rid, item.sequence,
          values['reject-' + item.sequence]), 'Item rejected'),
      }, 'Reject'));
      if (item.item_kind !== 'question') {
        actions.append(h('button', {
          type: 'button', class: 'ghost',
          onclick: () => form([
            field({ id: 'amend-title-' + item.sequence, label: 'New title', maxlength: 200, value: item.title || '' }),
            field({ id: 'amend-text-' + item.sequence, label: 'New text', type: 'textarea', rows: 4, maxlength: 4000, value: item.text || '' }),
          ], 'Amend', (values) => ctx.api.amendRoomItem(pid, rid, item.sequence,
            values['amend-title-' + item.sequence], values['amend-text-' + item.sequence]), 'Item amended'),
        }, 'Amend'));
      }
    }
    const history = (item.history || []).map((event) => h('div', { class: 'small muted' },
      '#' + event.sequence + ' ' + event.event + ' by ' + (event.author && event.author.name || 'a member') + ' ',
      time(event.created_at), event.text ? ' — ' + event.text : ''));
    const record = item.record ? h('div', { class: 'small muted' },
      'Record: ' + item.record.kind + ' ' + (item.record.id || '') +
      (item.record.decision_id ? ' (decision ' + item.record.decision_id + ')' : '')) : null;
    return h('li', null,
      h('div', { class: 'event-head' },
        h('span', { class: 'small muted' }, '#' + item.sequence), ' ',
        h('span', { class: 'chip' }, item.item_kind), ' ',
        h('span', { class: 'chip' + (item.state === 'accepted' ? '' : ' warn') }, item.state),
        item.moderated ? h('span', { class: 'chip warn' }, 'removed') : null),
      h('strong', null, item.moderated ? '(removed)' : (item.title || '')),
      h('p', { class: 'event-body prose room-text' }, item.text || ''),
      h('div', { class: 'small muted' }, (item.author && item.author.name || 'a member'), ' ', time(item.created_at),
        item.evidence ? ' · ' + item.evidence : '',
        item.parent ? ' · job ' + item.parent : ''),
      record,
      history.length ? h('div', { class: 'stack' }, history) : null,
      actions.childNodes.length ? actions : null,
      decision);
  }

  async function latestSince() {
    // The list read is the index only; asking the room for its last sequence would read
    // the whole room on the server.
    const listed = await ctx.api.rooms(pid, { archived: 1 });
    const found = (listed.items || []).find((item) => item.id === rid);
    const last = Number(found && found.last_sequence) || 0;
    return Math.max(0, last - 100);
  }

  async function draw() {
    let data;
    try {
      if (since === null) since = await latestSince();
      data = await ctx.api.room(pid, rid, { since, limit: 100 });
    } catch (error) {
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

    const items = data.items || [];
    const itemSection = h('section', { class: 'panel' },
      h('div', { class: 'panel-head' }, h('h2', { class: 'small' }, 'Items awaiting a person')),
      h('div', { class: 'panel-body' }, items.length
        ? h('ul', { class: 'timeline', 'aria-label': 'Room items' }, items.map(itemRow))
        : h('div', { class: 'empty' }, 'No items yet. A candidate requirement, decision or open question goes here until a person decides it.')));

    const adding = [];
    if (canPost(project) && room.state === 'open') {
      const itemForm = h('form', { class: 'form', novalidate: true },
        field({ id: 'item-kind', label: 'Kind', type: 'select', options: ITEM_KINDS }),
        field({ id: 'item-title', label: 'Title', maxlength: 200, hint: 'Up to 200 characters. Every project member reads it.' }),
        field({ id: 'item-text', label: 'Text', type: 'textarea', rows: 5, maxlength: 4000,
          hint: 'Up to 4,000 characters. Plain discussion; a Kind: prefix is refused.' }),
        field({ id: 'item-parent', label: 'Parent job (requirements only)', maxlength: 200,
          hint: 'The existing job id the requirement hangs under. Leave empty when the project has exactly one job.' }),
        h('div', null, h('button', { type: 'submit', class: 'primary' }, 'Add item')));
      itemForm.addEventListener('submit', async (event) => {
        event.preventDefault();
        const values = formValues(itemForm);
        const title = (values['item-title'] || '').trim();
        const text = (values['item-text'] || '').trim();
        if (!title) return setFieldError(itemForm, 'item-title', 'Give the item a title.');
        if (!text) return setFieldError(itemForm, 'item-text', 'Write the item first.');
        setFieldError(itemForm, 'item-title', ''); setFieldError(itemForm, 'item-text', '');
        const body = { item_kind: values['item-kind'], title, text };
        const parent = (values['item-parent'] || '').trim();
        if (parent) body.parent = parent;
        const saved = await act(itemForm.querySelector('button'), () => ctx.api.createRoomItem(pid, rid, body), { success: 'Item added' });
        if (saved) { itemForm.reset(); since = null; draw(); }
      });
      adding.push(h('section', { class: 'panel' }, h('div', { class: 'panel-head' },
        h('h2', { class: 'small' }, 'Add an item')), h('div', { class: 'panel-body' }, itemForm)));
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
        if (saved) { form.reset(); since = null; draw(); }
      });
      adding.push(h('section', { class: 'panel' }, h('div', { class: 'panel-head' }, h('h2', { class: 'small' }, 'Post')), h('div', { class: 'panel-body' }, form)));
    }

    if (canModerate(project) && room.state === 'open') {
      const archive = h('button', { type: 'button', class: 'danger', onclick: async () => {
        const yes = await confirmDialog({ title: 'Archive this room?', body: 'It stays readable and exportable; nobody can post to it again.', confirmLabel: 'Archive', danger: true });
        if (!yes) return;
        const saved = await act(archive, () => ctx.api.archiveRoom(pid, rid), { success: 'Room archived' });
        if (saved) { since = null; draw(); }
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
        if (saved) { form.reset(); since = null; draw(); }
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
      h('div', { class: 'panel-body' }, itemSection, log, more, exports));
    extras.replaceChildren(...adding);
  }

  const wrapper = h('div', { class: 'stack' },
    pageHead({ crumbs: crumbs(ctx, project, 'Rooms'), title: 'Room' }),
    panel,
    extras);
  await draw();
  return wrapper;
}
