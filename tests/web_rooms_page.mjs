// Runs the rooms views of web/js/views/rooms.js under Node with the DOM shim.
// argv: shim URL, rooms.js URL.
const [shimUrl, roomsUrl] = process.argv.slice(2);
await import(shimUrl);
const rooms = await import(roomsUrl);

function ctxFor(role, data) {
  return {
    href: (path) => '#' + path,
    go() {},
    api: {
      project: async () => ({ id: 'alpha', name: 'Alpha', role, archived: false }),
      rooms: async () => ({ items: [{ id: 'r-0123456789ab', name: 'Planning', state: 'open',
        created_by: 'usr_1', created_by_name: 'Carol', created_at: '2026-10-23T04:00:00Z',
        last_sequence: 1 }], total: 1 }),
      room: async () => data,
      createRoom: async () => { throw new Error('not used'); },
      postRoomMessage: async () => { throw new Error('not used'); },
      archiveRoom: async () => { throw new Error('not used'); },
      moderateRoomMessage: async () => { throw new Error('not used'); },
      exportRoom: async () => ({ text: '# Planning\n' }),
    },
  };
}

const data = {
  room: { id: 'r-0123456789ab', name: 'Planning', state: 'open',
    created_by: 'usr_1', created_by_name: 'Carol', created_at: '2026-10-23T04:00:00Z',
    archived_by: null, archived_by_name: null, archived_at: null, archive_reason: null },
  messages: [{ sequence: 1, kind: 'message',
    author: { id: 'usr_2', name: 'Alice', role: 'contributor' },
    created_at: '2026-10-23T04:01:00Z',
    text: '<img src=x onerror=alert(1)> and [x](javascript:alert(1))' }],
  moderations: [], since: 0, last_sequence: 1, next_since: 1, has_more: false,
};

async function detail(role) {
  const root = await rooms.detail(ctxFor(role, data), { pid: 'alpha', rid: 'r-0123456789ab' });
  await new Promise((resolve) => setTimeout(resolve, 30));
  const all = root.all(() => true);
  return {
    text: root.textContent,
    forms: root.all((e) => e.tagName === 'FORM').length,
    images: root.all((e) => e.tagName === 'IMG').length,
    js_links: all.filter((e) => (e.attributes.href || '').startsWith('javascript:'))
      .map((e) => e.attributes.href),
  };
}

const listCtx = ctxFor('coordinator', data);
const listRoot = await rooms.list(listCtx, { pid: 'alpha' });
await new Promise((resolve) => setTimeout(resolve, 30));
console.log(JSON.stringify({
  list: { text: listRoot.textContent, forms: listRoot.all((e) => e.tagName === 'FORM').length },
  viewer: await detail('viewer'),
  contributor: await detail('contributor'),
  coordinator: await detail('coordinator'),
}));
