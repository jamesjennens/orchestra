// Runs the rooms item panel under Node with the DOM shim (kittrial-5bb.388).
// argv: shim URL, rooms.js URL.  Marker and javascript: text must stay inert, and the
// Accept buttons must follow the caller's authority.
const [shimUrl, roomsUrl] = process.argv.slice(2);
await import(shimUrl);
const rooms = await import(roomsUrl);

function ctxFor(role) {
  const accepted = [];
  return {
    href: (path) => '#' + path,
    go() {},
    accepted,
    api: {
      project: async () => ({ id: 'alpha', name: 'Alpha', role, archived: false }),
      rooms: async () => ({ items: [{ id: 'r-0123456789ab', name: 'Planning', state: 'open',
        created_by: 'usr_1', created_by_name: 'Carol', created_at: '2026-10-23T04:00:00Z',
        last_sequence: 3 }], total: 1 }),
      room: async () => data,
      createRoom: async () => { throw new Error('not used'); },
      postRoomMessage: async () => { throw new Error('not used'); },
      createRoomItem: async () => { throw new Error('not used'); },
      acceptRoomItem: async (pid, rid, sequence) => {
        accepted.push(sequence);
        return { sequence, state: 'accepted' };
      },
      rejectRoomItem: async () => { throw new Error('not used'); },
      amendRoomItem: async () => { throw new Error('not used'); },
      answerRoomItem: async () => { throw new Error('not used'); },
      archiveRoom: async () => { throw new Error('not used'); },
      moderateRoomMessage: async () => { throw new Error('not used'); },
      exportRoom: async () => ({ text: '# Planning\n' }),
    },
  };
}

const MARKUP = '<img src=x onerror=alert(1)> and [x](javascript:alert(1))';
const data = {
  room: { id: 'r-0123456789ab', name: 'Planning', state: 'open',
    created_by: 'usr_1', created_by_name: 'Carol', created_at: '2026-10-23T04:00:00Z',
    archived_by: null, archived_by_name: null, archived_at: null, archive_reason: null },
  messages: [
    { sequence: 1, kind: 'item', item_kind: 'requirement', title: MARKUP, state: 'proposed',
      author: { id: 'usr_2', name: 'Alice', role: 'contributor' },
      created_at: '2026-10-23T04:01:00Z', text: MARKUP,
      evidence: 'orchestra:room/r-0123456789ab#1', moderated: false, history: [] },
    { sequence: 2, kind: 'item-event', target_sequence: 1, event: 'amend', state: 'amended',
      author: { id: 'usr_3', name: 'Carol', role: 'coordinator' },
      created_at: '2026-10-23T04:02:00Z', text: '<script>alert(1)</script>',
      record: null, title: MARKUP },
    { sequence: 3, kind: 'item', item_kind: 'question', title: 'Delimiter?',
      state: 'proposed', author: { id: 'usr_2', name: 'Alice', role: 'contributor' },
      created_at: '2026-10-23T04:03:00Z', text: 'Comma or semicolon?',
      evidence: 'orchestra:room/r-0123456789ab#3', moderated: false, history: [] },
  ],
  items: [
    { sequence: 1, kind: 'item', item_kind: 'requirement', title: MARKUP, state: 'amended',
      author: { id: 'usr_2', name: 'Alice', role: 'contributor' },
      created_at: '2026-10-23T04:01:00Z', text: MARKUP,
      evidence: 'orchestra:room/r-0123456789ab#1', moderated: false,
      record: null, history: [{ sequence: 2, event: 'amend', state: 'amended',
        author: { id: 'usr_3', name: 'Carol', role: 'coordinator' },
        created_at: '2026-10-23T04:02:00Z', text: '<script>alert(1)</script>', record: null }] },
    { sequence: 3, kind: 'item', item_kind: 'question', title: 'Delimiter?', state: 'proposed',
      author: { id: 'usr_2', name: 'Alice', role: 'contributor' },
      created_at: '2026-10-23T04:03:00Z', text: 'Comma or semicolon?',
      evidence: 'orchestra:room/r-0123456789ab#3', moderated: false, record: null, history: [] },
  ],
  moderations: [], since: 0, last_sequence: 3, next_since: 3, has_more: false,
};

async function detail(role) {
  const ctx = ctxFor(role);
  const root = await rooms.detail(ctx, { pid: 'alpha', rid: 'r-0123456789ab' });
  await new Promise((resolve) => setTimeout(resolve, 30));
  const all = root.all(() => true);
  return {
    text: root.textContent,
    images: root.all((e) => e.tagName === 'IMG').length,
    js_links: all.filter((e) => (e.attributes.href || '').startsWith('javascript:')).length,
    forms: root.all((e) => e.tagName === 'FORM').length,
    buttons: root.all((e) => e.tagName === 'BUTTON').map((e) => e.textContent),
    accepted: ctx.accepted,
  };
}

console.log(JSON.stringify({
  viewer: await detail('viewer'),
  contributor: await detail('contributor'),
  coordinator: await detail('coordinator'),
}));
