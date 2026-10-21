// Runs the task detail page of web/js/views/task.js under Node against a stubbed
// transport, for kittrial-5bb.356 item 6:
//  - the unresolved list shows every carry_open_items entry with the true total, and
//    says the list could not all be read (with a reload) when carry_open_items is null;
//  - one long comment split into fragments (shared entry_id, body_offset, continued) is
//    joined into one history item, fetching the next page while the last fragment is
//    continued;
//  - the history button names the NEWER entries it loads (history is oldest first).
// tests/web_dom_shim.mjs gives dom.js and ui.js what they use. Prints one JSON object of
// observations; tests/test_web_brief_open_items.py asserts on it.
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
const ITEMS = [1, 2, 3, 4, 5].map((n) => ({ id: 'i' + n, kind: 'blocker', text: 'Item ' + n, source: 'src ' + n }));

function brief(carry) {
  return {
    task: TASK, review: { state: 'none', requests: [] },
    checkpoint: {
      id: 'cp-1', at: '2026-10-20T00:00:00Z', author: 'a', author_name: 'A',
      summary: 'Where it stands', next_action: 'Next', open_items: ITEMS.slice(0, 2),
      open_items_shown: 2, open_items_total: 5,
      open_items_note: 'The first 2 of 5; carry_open_items has all 5, in order, to carry forward unchanged.',
    },
    carry_open_items: carry,
  };
}

// One long comment split across the page boundary: its first fragment is the last item
// of page 1 and is still continued, so the page must fetch page 2 itself.
const PAGES = {
  null: {
    items: [
      { kind: 'comment', author: 'a', timestamp: '2026-10-19T10:00:00Z', entry_id: 'e1',
        body: 'First half. ', body_offset: 0, body_total_chars: 23, continued: true },
    ],
    next_cursor: 'c1',
  },
  c1: {
    items: [
      { kind: 'comment', author: 'a', timestamp: '2026-10-19T10:00:00Z', entry_id: 'e1',
        body: 'Second half.', body_offset: 12, body_total_chars: 23, continued: false },
      { kind: 'comment', author: 'b', timestamp: '2026-10-19T11:00:00Z', entry_id: 'e2',
        body: 'A short one.', body_offset: 0, body_total_chars: 12, continued: false },
    ],
    next_cursor: null,
  },
};

function makeCtx(data, calls) {
  const transport = (method, path) => {
    calls.push(path);
    let value = null;
    if (path === '/v1/projects/alpha') value = PROJECT;
    else if (path === '/v1/projects/alpha/tasks/alpha-1/brief') value = data;
    else if (path.indexOf('/v1/projects/alpha/tasks/alpha-1/history') === 0) {
      const match = /[?&]cursor=([^&]+)/.exec(path);
      value = PAGES[match ? decodeURIComponent(match[1]) : 'null'];
    }
    return Promise.resolve({ status: value === null ? 404 : 200, data: value });
  };
  const ctx = { api: createApi(transport), href: (p) => '#' + p, go() {}, query: {},
    me: { id: 'someone-else', superuser: false, display_name: 'Viewer' }, render() {}, setDirty() {} };
  return ctx;
}
const settle = async () => { for (let i = 0; i < 40; i += 1) await new Promise((resolve) => setTimeout(resolve, 5)); };
const textOf = (page, cls) => page.all((e) => e.parentNode && e.parentNode.className === cls).map((e) => e.textContent);
const buttons = (page, text) => page.all((e) => e.tagName === 'BUTTON' && e.textContent === text).length;

const completeCalls = [];
const complete = await view.detail(makeCtx(brief(ITEMS), completeCalls), { pid: 'alpha', tid: 'alpha-1' });
await settle();
const historyText = textOf(complete, 'timeline');

const incompleteCalls = [];
const incomplete = await view.detail(makeCtx(brief(null), incompleteCalls), { pid: 'alpha', tid: 'alpha-1' });
await settle();

console.log(JSON.stringify({
  openItems: textOf(complete, 'open-items'),
  unresolvedHeading: complete.textContent.indexOf('Unresolved items 5') !== -1,
  completeHasReadAgain: complete.textContent.indexOf('could not all be read') !== -1,
  historyItems: historyText.length,
  historyText: historyText.join(' | '),
  historyCalls: completeCalls.filter((p) => p.indexOf('/history') !== -1).length,
  showNewer: buttons(complete, 'Show newer'),
  showOlder: buttons(complete, 'Show older'),
  incompleteNotice: incomplete.textContent.indexOf('could not all be read') !== -1,
  reloadButtons: buttons(incomplete, 'Reload the task'),
  incompleteItems: textOf(incomplete, 'open-items'),
}));
