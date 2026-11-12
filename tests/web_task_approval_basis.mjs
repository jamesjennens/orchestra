// kittrial-5bb.515: the task page shows WHY the server let an approval through as its own line, from the
// review block's approval_basis field, and never from any summary text. Runs web/js/views/task.js under Node
// (tests/web_dom_shim.mjs) against a stubbed transport; prints one JSON object of observations.
//
// argv: shim URL, api.js URL, task.js URL.
const [shimUrl, apiUrl, taskUrl] = process.argv.slice(2);
await import(shimUrl);
const { createApi } = await import(apiUrl);
const view = await import(taskUrl);

const SENTENCE = 'Approved by a coordinator agent of the same person as the agent that delivered it '
  + '(project setting agent_approval: by-agent).';
const PROJECT = { id: 'alpha', name: 'Alpha', role: 'owner', usable: true, archived: false };
const TASK = { id: 'alpha-1', project_id: 'alpha', title: 'A row', status: 'open', review_state: 'approved',
  assignee: 'worker', priority: 1, version: 1, description: null, claimable: false,
  next_action: { who: 'owner', text: 'Integrate' } };
const contribution = (summary) => ({ id: 'c1', revision: 1, commit: 'a'.repeat(40), base_commit: 'b'.repeat(40),
  branch: 'work', summary, author: 'worker', at: '2026-01-01T00:00:00Z' });

function page(review, history) {
  const transport = (_method, path) => {
    let data = null;
    if (path === '/v1/projects/alpha') data = PROJECT;
    else if (path === '/v1/projects/alpha/tasks/alpha-1/brief') data = { task: TASK, review, checkpoint: null };
    else if (path.indexOf('/v1/projects/alpha/tasks/alpha-1/history') === 0) data = { items: history, next_cursor: null };
    return Promise.resolve({ status: data === null ? 404 : 200, data });
  };
  const ctx = { api: createApi(transport), href: (p) => '#' + p, go() {}, query: {},
    me: { id: 'someone-else', superuser: false, display_name: 'Owner' }, render() {}, setDirty() {} };
  return view.detail(ctx, { pid: 'alpha', tid: 'alpha-1' });
}
const settle = async () => { for (let i = 0; i < 30; i += 1) await new Promise((resolve) => setTimeout(resolve, 5)); };
const classes = (e) => String(e.className || e.attributes.class || '').split(/\s+/);
const lines = (root) => root.all((e) => classes(e).includes('approval-basis'));
const proses = (root) => root.all((e) => classes(e).includes('prose'));
const observe = (root) => ({
  lines: lines(root).map((e) => ({ text: e.textContent, basis: e.attributes['data-approval-basis'] || null })),
  // The words in the summary paragraphs, which a basis line must never be part of.
  prose: proses(root).map((e) => e.textContent),
  text: root.textContent,
});

const BASIS = { basis: 'same-person-agent', text: 'Allowed by: coordinator agent of the same person as the delivering agent',
  author: 'heron', at: '2026-01-02T00:00:00Z' };
const withField = await page({ state: 'awaiting-integration', requests: [], contribution: contribution('Delivered the change'),
  approval_basis: BASIS }, []);
await settle();
// An approval written before the field: the sentence is in the approver's summary (and a delivery summary may
// also quote it). Neither is a basis; the audit stays the authority for these.
const oldForm = await page({ state: 'awaiting-integration', requests: [], contribution: contribution(SENTENCE) },
  [{ action: 'approve', actor: 'heron', time: '2026-01-02T00:00:00Z', detail: 'accepted ' + SENTENCE }]);
await settle();
const CREDENTIAL = { basis: 'same-person-worker-credential',
  text: 'Allowed by: coordinator agent of the same person as the delivering worker credential (project setting '
    + 'agent_approval: by-agent; the audit entry holds who set it and when)',
  author: 'heron', at: '2026-01-02T00:00:00Z' };
const withCredential = await page({ state: 'awaiting-integration', requests: [],
  contribution: contribution('Delivered under lane-c'), approval_basis: CREDENTIAL }, []);
await settle();
console.log(JSON.stringify({ withField: observe(withField), oldForm: observe(oldForm),
  withCredential: observe(withCredential), sentence: SENTENCE }));
