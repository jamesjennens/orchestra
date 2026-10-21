// Runs the task detail page of web/js/views/task.js under Node against a stubbed
// transport, for kittrial-5bb.354: the 'changes since this checkpoint' block lists the
// entries (state, yours, author, clipped body) and the changed fields (now, before, or
// that the previous value is not recorded), puts hostile text in as text, says when
// nothing changed, and shows nothing without a checkpoint.
// tests/web_dom_shim.mjs gives dom.js and ui.js what they use. Prints one JSON object of
// observations; tests/test_changes_since_checkpoint.py asserts on it.
//
// argv: shim URL, api.js URL, task.js URL.
const [shimUrl, apiUrl, taskUrl] = process.argv.slice(2);
await import(shimUrl);
const { createApi } = await import(apiUrl);
const view = await import(taskUrl);

const PROJECT = { id: 'alpha', name: 'Alpha', role: 'viewer', usable: true, archived: false };
const TASK = {
  id: 'alpha-1', project_id: 'alpha', title: 'A task', status: 'in_progress', review_state: 'none',
  assignee: 'someone-else', assignee_name: 'Someone', priority: 2, version: 1, description: 'desc',
  claimable: false, next_action: null,
};
const HOSTILE = '<img src=x onerror="window.PWNED=1">';
const CHECKPOINT = { id: 'cp-1', at: '2026-10-20T00:00:00Z', author: 'a', author_name: 'A',
  summary: 'Where it stands', next_action: 'Next', open_items: [], open_items_shown: 0, open_items_total: 0 };

function brief(section, checkpoint = CHECKPOINT) {
  return { task: TASK, review: { state: 'none', requests: [] }, checkpoint, carry_open_items: [],
    changes_since_checkpoint: section };
}
const LISTED = {
  checkpoint: { comment_id: 'cp-1', author: { text: 'a', omitted_chars: 0 }, timestamp: '2026-10-20T00:00:00Z' },
  coverage: 'snapshot', total: 3, own_count: 1, other_count: 2, unverified_count: 0, omitted: 1,
  entries: [
    { entry_id: 'alpha-1-c3', kind: 'comment', author: { text: 'me', omitted_chars: 0 }, timestamp: '2026-10-20T03:00:00Z',
      own: true, state: 'new', body: { text: 'Working on it', omitted_chars: 0 } },
    { entry_id: 'alpha-1-c2', kind: 'comment', author: { text: 'coord', omitted_chars: 0 }, timestamp: '2026-10-20T02:00:00Z',
      own: false, state: 'edited', body: { text: HOSTILE, omitted_chars: 40 } },
  ],
  fields: [
    { field: 'description', author: null, changed_at: '2026-10-20T01:00:00Z', edits: 2,
      current: { text: 'New intent', omitted_chars: 0 }, previous: { text: 'Old intent', omitted_chars: 0 },
      previous_recorded: true, note: 'who made the change is not recorded' },
  ],
  fields_note: null, history: 'history alpha-1 --since-checkpoint', note: 'Newest first.',
};
const NOTHING = { checkpoint: LISTED.checkpoint, coverage: 'current', total: 0, own_count: 0, other_count: 0,
  unverified_count: 0, omitted: 0, entries: [], fields: [], fields_note: null, history: 'h', note: 'n' };

function makeCtx(data) {
  const transport = (method, path) => {
    let value = null;
    if (path === '/v1/projects/alpha') value = PROJECT;
    else if (path === '/v1/projects/alpha/tasks/alpha-1/brief') value = data;
    else if (path.indexOf('/v1/projects/alpha/tasks/alpha-1/history') === 0) value = { items: [], next_cursor: null };
    return Promise.resolve({ status: value === null ? 404 : 200, data: value });
  };
  return { api: createApi(transport), href: (p) => '#' + p, go() {}, query: {},
    me: { id: 'someone-else', superuser: false, display_name: 'Viewer' }, render() {}, setDirty() {} };
}
const settle = async () => { for (let i = 0; i < 40; i += 1) await new Promise((resolve) => setTimeout(resolve, 5)); };
const textOf = (page, cls) => page.all((e) => e.parentNode && e.parentNode.className === cls).map((e) => e.textContent);
const marked = (page, value) => page.all((e) => e.getAttribute && e.getAttribute('data-changes') === value).length;
const imgs = (page) => page.all((e) => e.tagName === 'IMG').length;

const listed = await view.detail(makeCtx(brief(LISTED)), { pid: 'alpha', tid: 'alpha-1' });
await settle();
const nothing = await view.detail(makeCtx(brief(NOTHING)), { pid: 'alpha', tid: 'alpha-1' });
await settle();
const none = await view.detail(makeCtx(brief(null, null)), { pid: 'alpha', tid: 'alpha-1' });
await settle();

console.log(JSON.stringify({
  entries: textOf(listed, 'changes-entries'),
  fields: textOf(listed, 'changes-fields'),
  heading: listed.textContent.indexOf('Changes since this checkpoint 3') !== -1,
  omitted: listed.textContent.indexOf('1 more are not shown') !== -1,
  hostileAsText: listed.textContent.indexOf(HOSTILE) !== -1,
  images: imgs(listed),
  pwned: Boolean(globalThis.PWNED),
  listedMarked: marked(listed, 'listed'),
  nothingMarked: marked(nothing, 'none'),
  nothingText: nothing.textContent.indexOf('Nothing has changed since this checkpoint.') !== -1,
  noCheckpoint: marked(none, 'listed') + marked(none, 'none'),
}));
