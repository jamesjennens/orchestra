// Runs the slice-5 reviews page of web/js/views/project.js under Node with the
// DOM shim (review b1/b2 r2: the empty-error-group crash, the one-line empty
// proposal groups and the folded propose form). No http_service is needed: the
// stubbed transport answers the project read, the queue and the proposals reads.
// Prints one JSON object; tests/test_rooms_http.py asserts on it.
//
// argv: shim URL, api.js URL, project.js URL.
const [shimUrl, apiUrl, projectUrl] = process.argv.slice(2);
await import(shimUrl);
const { createApi } = await import(apiUrl);
const view = await import(projectUrl);

const PROJECT = { id: 'alpha', name: 'Alpha', role: 'owner', usable: true, archived: false };
const TASK = (id, review_state) => ({ id, project_id: 'alpha', title: 'Task ' + id,
  status: 'open', review_state, assignee_name: 'Ann', updated_at: '2026-10-26T04:00:00Z' });

const calls = [];
function transportFor(items) {
  return (method, path) => {
    calls.push(method + ' ' + path);
    if (path === '/v1/projects/alpha') return Promise.resolve({ status: 200, data: PROJECT });
    if (path.startsWith('/v1/projects/alpha/queue')) {
      return Promise.resolve({ status: 200, data: { complete: true, items } });
    }
    if (path.startsWith('/v1/projects/alpha/proposals')) {
      return Promise.resolve({ status: 200,
        data: { items: [], total: 0, next_cursor: null, can_triage: false, can_propose: true } });
    }
    return Promise.resolve({ status: 404, data: null });
  };
}

const summaries = (root) => root.all((e) => e.tagName === 'SUMMARY').map((e) => e.textContent);
const emptyLines = (root) => root.all((e) => e.tagName === 'P')
  .filter((e) => String(e.className || e.attributes['class'] || '').includes('empty-line')).map((e) => e.textContent);

async function reviewsPage(items) {
  const ctx = { api: createApi(transportFor(items)), href: (p) => '#' + p, go() {},
    query: new Map(), me: { id: 'usr_1', superuser: false }, render() {}, projects: [] };
  const page = await view.reviews(ctx, { pid: 'alpha' });
  for (let i = 0; i < 40; i += 1) await new Promise((resolve) => setTimeout(resolve, 10));
  return page;
}

const withoutError = await reviewsPage([TASK('alpha-1', 'awaiting-review'), TASK('alpha-2', 'changes-requested')]);
const withError = await reviewsPage([TASK('alpha-1', 'awaiting-review'), TASK('alpha-e', 'error')]);
console.log(JSON.stringify({
  withoutError: { text: withoutError.textContent, empty: emptyLines(withoutError), summaries: summaries(withoutError) },
  withError: { text: withError.textContent, empty: emptyLines(withError), summaries: summaries(withError) },
}));
