// Runs the slice-5 moderation fold of web/js/views/rooms.js under Node with the
// DOM shim (review b2 r2: viewer/contributor see no Moderate fold; a coordinator
// does; never on tombstones, item-events or moderated items — the server answers
// 409 for an already-moderated line and 404 for other kinds, rooms.py:1666-1669).
// Prints one JSON object; tests/test_rooms_http.py asserts on it.
//
// argv: shim URL, rooms.js URL.
const [shimUrl, roomsUrl] = process.argv.slice(2);
await import(shimUrl);
const rooms = await import(roomsUrl);

function ctxFor(role, messages) {
  const data = { room: { id: 'r-0123456789ab', name: 'Planning', state: 'open',
      created_by: 'usr_1', created_by_name: 'Carol', created_at: '2026-10-26T04:00:00Z',
      archived_by: null, archived_by_name: null, archive_reason: null },
    messages, moderations: [], items: [], since: 0, last_sequence: 160, next_since: 160, has_more: false };
  return { href: (path) => '#' + path, go() {},
    api: { project: async () => ({ id: 'alpha', name: 'Alpha', role, archived: false }),
      rooms: async () => ({ items: [], total: 0 }),
      room: async () => data,
      createRoom: async () => { throw new Error('not used'); },
      postRoomMessage: async () => { throw new Error('not used'); },
      archiveRoom: async () => { throw new Error('not used'); },
      moderateRoomMessage: async () => { throw new Error('not used'); },
      createRoomItem: async () => { throw new Error('not used'); },
      acceptRoomItem: async () => { throw new Error('not used'); },
      rejectRoomItem: async () => { throw new Error('not used'); },
      amendRoomItem: async () => { throw new Error('not used'); },
      answerRoomItem: async () => { throw new Error('not used'); },
      exportRoom: async () => ({ text: '# Planning\n' }),
      requirements: async () => ({ jobs: [] }) } };
}

const LIVE = { sequence: 150, kind: 'message',
  author: { id: 'usr_2', name: 'Alice', role: 'contributor' },
  created_at: '2026-10-26T04:01:00Z', text: 'a live message' };
const TOMBSTONE = { sequence: 151, kind: 'tombstone', moderated: true,
  author: { id: 'usr_9', name: 'Rae', role: 'coordinator' }, created_at: '2026-10-26T04:02:00Z',
  text: 'removed by Rae at 2026-10-26T04:02:00Z: off topic', reason: 'off topic', target_sequence: 150 };
const EVENT = { sequence: 152, kind: 'item-event', event: 'accept', target_sequence: 12,
  author: { id: 'usr_2', name: 'Alice', role: 'contributor' }, created_at: '2026-10-26T04:03:00Z', text: '' };
const MODERATED_ITEM = { sequence: 153, kind: 'item', item_kind: 'requirement', title: null, text: 'removed',
  state: 'proposed', moderated: true,
  author: { id: 'usr_2', name: 'Alice', role: 'contributor' }, created_at: '2026-10-26T04:04:00Z' };

async function folds(role, messages) {
  const ctx = ctxFor(role, messages);
  const root = await rooms.detail(ctx, { pid: 'alpha', rid: 'r-0123456789ab' });
  await new Promise((resolve) => setTimeout(resolve, 30));
  return root.all((e) => e.tagName === 'SUMMARY' && e.textContent.includes('Moderate')).length;
}

console.log(JSON.stringify({
  viewerLive: await folds('viewer', [LIVE]),
  contributorLive: await folds('contributor', [LIVE]),
  coordinatorLive: await folds('coordinator', [LIVE]),
  coordinatorTombstone: await folds('coordinator', [TOMBSTONE]),
  coordinatorEvent: await folds('coordinator', [EVENT]),
  coordinatorModeratedItem: await folds('coordinator', [MODERATED_ITEM]),
}));
