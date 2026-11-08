// The page header (kittrial-5bb.525 slice b): per page, the title (h1), the key number with its
// source, and the next action. Runs the real views under Node (tests/web_dom_shim.mjs) against
// the prototype's own mock service (web/js/mock.js), signed in as its project owner. Each
// expected number is read from the service here, independently of the view, so the header
// is compared with the server's own figure (and with the rows on one page, which it must not be).
// Prints one JSON object; tests/test_page_head_key.py asserts on it.
//
// argv: shim URL, web/js directory URL (with a trailing slash).
const [shimUrl, js] = process.argv.slice(2);
await import(shimUrl);
globalThis.window = globalThis.window || { isSecureContext: false };
globalThis.location = globalThis.location || { origin: 'http://page.origin.example', hash: '' };
const { createApi } = await import(js + 'api.js');
const { createMock } = await import(js + 'mock.js');
const { durationSince } = await import(js + 'ui.js');
const work = await import(js + 'views/work.js');
const project = await import(js + 'views/project.js');
const task = await import(js + 'views/task.js');
const agents = await import(js + 'views/agents.js');

const settle = async () => { for (let i = 0; i < 20; i += 1) await new Promise((r) => setTimeout(r, 5)); };
const api = createApi(createMock({ extra: null, latency: 0 }));
const me = await api.login('priya', 'demo');
const projects = (await api.projects()).items;
const ctx = { api, me, projects, href: (p) => '#' + p, go() {}, render() {}, query: new URLSearchParams(),
  features: { requirements: true }, options: {}, refreshProjects: async () => {} };
const PID = 'proj_portal';

function header(page) {
  const head = page.all((e) => e.tagName === 'HEADER' && String(e.className).includes('page-head'))[0];
  if (!head) return { found: false };
  const h1s = head.all((e) => e.tagName === 'H1');
  const key = head.all((e) => e.attributes && e.attributes['data-page-key'] !== undefined)[0];
  const actions = head.all((e) => (e.tagName === 'A' || e.tagName === 'BUTTON') && !e.parentNode.className.includes('crumbs')
    && e.parentNode.tagName !== 'NAV');
  return {
    found: true,
    headers: page.all((e) => e.tagName === 'HEADER' && String(e.className).includes('page-head')).length,
    h1: h1s.map((e) => e.textContent),
    h1Id: h1s[0] && h1s[0].attributes.id,
    tabTitle: (h1s[0] && h1s[0].attributes['data-tab-title']) || null,
    key: key ? { shown: !key.hidden, num: key.children[0].textContent, label: key.children[1].textContent,
      source: key.attributes['data-key-source'] || null } : null,
    actions: actions.map((e) => ({ text: e.textContent, primary: String(e.className).includes('primary'),
      next: e.attributes['data-next-action'] || null, href: e.attributes.href || null })),
  };
}
const ids = (page) => page.all((e) => e.attributes && e.attributes.id).map((e) => e.attributes.id);
const out = {};

// My work (kittrial-5bb.525 r2): a fixture whose facts are written here, row by row. The reader
// (usr_owen) owns Alpha and contributes to Beta. test_page_head_key.py states, from these facts
// alone, which rows wait on the reader; nothing here applies the view's rule.
const HOUR = 3600 * 1000;
const ago = (hours) => new Date(Date.now() - hours * HOUR).toISOString();
const row = (pid, id, extra) => ({ project_id: pid, id, title: id, status: 'open', project_name: pid === 'alpha' ? 'Alpha' : 'Beta', ...extra });
const FIXTURE_ROWS = {
  // the reader's own delivered task in a project the reader owns: in assigned AND in to_review
  a1: row('alpha', 'a1', { assignee: 'usr_owen', review_state: 'awaiting-review', waiting_since: ago(3), next_action: { who: 'owner', text: 'Review the delivered contribution' } }),
  // the reader delivered this in Beta, where the reader cannot approve: it waits on Beta's owner
  b1: row('beta', 'b1', { assignee: 'usr_owen', review_state: 'awaiting-review', waiting_since: ago(5), next_action: { who: 'owner', text: 'Review the delivered contribution' } }),
  // the reader's task to deliver
  b2: row('beta', 'b2', { assignee: 'usr_owen', review_state: 'none', updated_at: ago(30), next_action: { who: 'assignee', text: 'Deliver a contribution' } }),
  // sent back to the reader
  b3: row('beta', 'b3', { assignee: 'usr_owen', review_state: 'changes-requested', updated_at: ago(20), next_action: { who: 'assignee', text: 'Address the requested changes' } }),
  // someone else's delivery and someone else's approved work in Alpha, which the reader owns
  a2: row('alpha', 'a2', { assignee: 'usr_sam', review_state: 'awaiting-review', waiting_since: ago(48), next_action: { who: 'owner', text: 'Review the delivered contribution' } }),
  a3: row('alpha', 'a3', { assignee: 'usr_sam', review_state: 'approved', waiting_since: ago(24), next_action: { who: 'owner', text: 'Integrate the approved contribution' } }),
  // a requirement waiting for acceptance, and a requirement record that only says where it is edited
  a5: row('alpha', 'a5', { assignee: 'usr_owen', review_state: 'none', requirement_record: true, requirement_state: 'awaiting-acceptance', next_action: { who: 'owner', text: "Requirement waiting for acceptance: accept it on the project's Requirements page" } }),
  a6: row('alpha', 'a6', { assignee: 'usr_owen', review_state: 'none', requirement_record: true, requirement_state: 'draft', next_action: { who: 'owner', text: "Requirement record: edit it on the project's Requirements page" } }),
};
const F = FIXTURE_ROWS;
const fixture = (extra) => ({ assigned: [F.a1, F.b1, F.b2, F.b3, F.a5, F.a6], to_review: [F.a1, F.a2, F.a3], agents: [], agent_prompts: [], ...extra });
const owen = { ...ctx, me: { id: 'usr_owen', display_name: 'Owen' },
  projects: [{ id: 'alpha', name: 'Alpha', role: 'owner' }, { id: 'beta', name: 'Beta', role: 'contributor' }] };
{
  const page = await work.home({ ...owen, api: { ...api, myWork: async () => fixture({}) } });
  await settle();
  out.myWork = { head: header(page), fixture: { assigned: fixture({}).assigned.map((t) => t.id), to_review: fixture({}).to_review.map((t) => t.id) } };
  // the same lists in a read the server marks truncated: the count is not the whole
  const cut = await work.home({ ...owen, api: { ...api, myWork: async () => fixture({ truncated: true }) } });
  await settle();
  out.myWorkTruncated = { head: header(cut) };
}

// Tasks: the server's total for the filtered list, not the rows on the page; then the Closed filter.
{
  const page = await project.overview(ctx, { pid: PID });
  await settle();
  const active = await api.tasks(PID, { limit: 10, status: 'active', q: '', review_state: '' });
  const closed = await api.tasks(PID, { limit: 10, status: 'closed', q: '', review_state: '' });
  const first = header(page);
  const closedButton = page.all((e) => e.tagName === 'BUTTON' && e.textContent === 'Closed')[0];
  await closedButton.dispatch('click');
  await settle();
  out.tasks = { head: first, afterClosed: header(page).key,
    expected: { active: String(active.total), activeRows: active.items.length, closed: String(closed.total), project: projects.find((p) => p.id === PID).name },
    crumbs: page.all((e) => e.tagName === 'NAV' && e.attributes['aria-label'] === 'Breadcrumb')[0].textContent };
}

// A task waiting for review: how long since its contribution was delivered; the review is the next action.
{
  const list = await api.tasks(PID, { limit: 50, status: '', q: '', review_state: 'awaiting-review' });
  const tid = list.items[0].id;
  const brief = await api.brief(PID, tid);
  const page = await task.detail(ctx, { pid: PID, tid });
  await settle();
  out.taskWaiting = { head: header(page), ids: ids(page),
    expected: { title: brief.task.title, num: durationSince(brief.review.contribution.at), next: brief.task.next_action && brief.task.next_action.text } };
}

// A task with no contribution waiting: no key at all.
{
  const list = await api.tasks(PID, { limit: 50, status: 'active', q: '', review_state: '' });
  const plain = list.items.find((t) => !t.review_state || t.review_state === 'none');
  const page = await task.detail(ctx, { pid: PID, tid: plain.id });
  await settle();
  out.taskPlain = { head: header(page), expected: { title: plain.title } };
}

// My agents: the server's total of agents.
{
  const data = await api.agents({ limit: 20, cursor: null, unconfirmed: 'false' });
  const page = await agents.list(ctx);
  await settle();
  out.agents = { head: header(page), ids: ids(page), expected: { num: String(data.total) } };
}

// People (Members & settings before kittrial-5bb.522 slice 2): the member count. Settings has no key.
{
  const data = await api.members(PID);
  const page = await project.people(ctx, { pid: PID });
  await settle();
  out.people = { head: header(page), ids: ids(page),
    expected: { num: String(typeof data.total === 'number' ? data.total : data.items.length) } };
  const settingsPage = await project.settings(ctx, { pid: PID });
  await settle();
  out.settings = { head: header(settingsPage) };
}

// Tasks, a failed re-read after a filter (kittrial-5bb.525 r2): the first read is the service's; the
// Closed read fails; Try again then reads Closed from the service.
const failure = () => Object.assign(new Error('busy'), { status: 503, code: 'busy', requestId: 'req_r2' });
const pressed = (page, label) => page.all((e) => e.tagName === 'BUTTON' && e.textContent === label)[0];
{
  let failNext = false;
  const flaky = { ...api, tasks: async (pid, params) => { if (failNext) { failNext = false; throw failure(); } return api.tasks(pid, params); } };
  const page = await project.overview({ ...ctx, api: flaky }, { pid: PID });
  await settle();
  const before = header(page).key;
  failNext = true;
  await pressed(page, 'Closed').dispatch('click');
  await settle();
  const failed = { key: header(page).key, error: page.all((e) => e.attributes && e.attributes['data-state'] === 'error').length,
    retry: Boolean(pressed(page, 'Try again')) };
  if (failed.retry) await pressed(page, 'Try again').dispatch('click');
  await settle();
  const closed = await api.tasks(PID, { limit: 10, status: 'closed', q: '', review_state: '' });
  out.tasksFailedReread = { before, failed, retried: header(page).key, expected: { closed: String(closed.total) } };
}

// Tasks, answers out of order: Closed's read is held while All's is answered; Closed's answer lands
// last and must be dropped, for the key and the table together.
{
  let release;
  const held = { ...api, tasks: async (pid, params) => {
    if (params.status === 'closed') { await new Promise((r) => { release = r; }); }
    return api.tasks(pid, params);
  } };
  const page = await project.overview({ ...ctx, api: held }, { pid: PID });
  await settle();
  pressed(page, 'Closed').dispatch('click');
  await settle();
  await pressed(page, 'All').dispatch('click');
  await settle();
  release();
  await settle();
  const all = await api.tasks(PID, { limit: 10, status: '', q: '', review_state: '' });
  const closed = await api.tasks(PID, { limit: 10, status: 'closed', q: '', review_state: '' });
  const rowIds = page.all((e) => e.tagName === 'TR' && String(e.className).includes('task-row'))
    .map((tr) => tr.all((e) => e.tagName === 'A' && String(e.className).includes('title'))[0].attributes.href.split('/t/')[1]);
  out.tasksOutOfOrder = { key: header(page).key, rowIds,
    expected: { all: String(all.total), allIds: all.items.filter((t) => !t.requirement_record).map((t) => t.id), closed: String(closed.total) } };
}

// My agents, a failed re-read (the reviewer's MB2): a superuser's filter reads again, and that read fails.
{
  const first = await api.agents({ limit: 20, cursor: null, unconfirmed: 'false' });
  let calls = 0;
  const flaky = { ...api, accounts: async () => ({ items: [] }),
    agents: async () => { calls += 1; if (calls > 1) throw failure(); return first; } };
  const page = await agents.list({ ...ctx, me: { ...ctx.me, superuser: true }, api: flaky });
  await settle();
  const before = header(page).key;
  const filter = page.all((e) => e.tagName === 'INPUT' && e.attributes['data-filter'] === 'unconfirmed-scopes')[0];
  await filter.dispatch('change');
  await settle();
  out.agentsFailedReread = { before, after: header(page).key, error: page.all((e) => e.attributes && e.attributes['data-state'] === 'error').length,
    expected: { num: String(first.total) } };
}

// Tasks rows (kittrial-5bb.525 c r2): a fixture whose facts are written here. test_page_head_key.py states,
// for each reader, which rows are marked and what the Who acts next cell names, from these facts alone.
const step = (who, text) => ({ who, text });
const ROWS = [
  { id: 'r_rev', assignee: 'usr_sam', assignee_name: 'Sam Ito', review_state: 'awaiting-review', next_action: step('owner', 'Review the delivered contribution') },
  { id: 'r_int', assignee: 'usr_sam', assignee_name: 'Sam Ito', review_state: 'approved', next_action: step('owner', 'Integrate the approved contribution') },
  { id: 'r_epic', issue_type: 'epic', review_state: 'none', next_action: step('owner', 'This is an epic, not a unit of work: work its tasks') },
  { id: 'r_reqrec', requirement_record: true, requirement_state: 'draft', review_state: 'none', next_action: step('owner', "Requirement record: edit it on the project's Requirements page") },
  { id: 'r_reqacc', requirement_record: true, requirement_state: 'awaiting-acceptance', review_state: 'none', next_action: step('owner', "Requirement waiting for acceptance: accept it on the project's Requirements page") },
  { id: 'r_held', assignee: 'usr_sam', assignee_name: 'Sam Ito', review_state: 'held', next_action: step('coordinator', 'Release the integration hold or record a corrected verification') },
  // a held integration with no assignee: only the held state keeps it from looking like work to assign
  { id: 'r_held_open', review_state: 'held', next_action: step('coordinator', 'Release the integration hold or record a corrected verification') },
  { id: 'r_assign', review_state: 'none', next_action: step('coordinator', "Only the project's coordinators assign work here; nothing is assigned to you yet.") },
  { id: 'r_mine', assignee: 'usr_cara', assignee_name: 'Cara Diaz', review_state: 'none', next_action: step('assignee', 'Deliver a contribution') },
  { id: 'r_back', assignee: 'usr_cara', assignee_name: 'Cara Diaz', review_state: 'changes-requested', next_action: step('assignee', 'Address the requested changes') },
  { id: 'r_claim', review_state: 'none', next_action: step('anyone', 'Claim this task') },
  { id: 'r_blocked', review_state: 'none', open_dependencies: ['alpha-x'], next_action: step('owner', 'Blocked by alpha-x') },
  { id: 'r_error', review_state: 'error', next_action: step('owner', 'Needs operator attention') },
  { id: 'r_unknown', review_state: null, next_action: null },
].map((t) => ({ project_id: 'alpha', title: t.id, status: 'open', priority: 2, ...t }));
const READERS = { owner: { id: 'usr_olga', role: 'owner' }, coordinator: { id: 'usr_nia', role: 'coordinator' }, contributor: { id: 'usr_cara', role: 'contributor' } };
out.rows = {};
for (const [label, reader] of Object.entries(READERS)) {
  const alpha = { id: 'alpha', name: 'Alpha', role: reader.role, archived: false, usable: true, description: '' };
  const stub = { ...api, project: async () => alpha, tasks: async () => ({ items: ROWS, total: ROWS.length, next_cursor: null }) };
  const page = await project.overview({ ...ctx, api: stub, me: { id: reader.id, display_name: label }, projects: [alpha],
    query: new URLSearchParams('requirements=1') }, { pid: 'alpha' });
  await settle();
  out.rows[label] = Object.fromEntries(page.all((e) => e.tagName === 'TR' && String(e.className).includes('task-row')).map((tr) => {
    const link = tr.all((e) => e.tagName === 'A' && String(e.className).includes('title'))[0];
    const cell = tr.all((e) => e.tagName === 'TD' && String(e.className).includes('who-acts-cell'))[0];
    return [link.attributes.href.split('/t/')[1], { mine: String(tr.className).split(' ').includes('mine'),
      flag: tr.attributes['data-waits-on-you'] === 'true', hidden: tr.textContent.startsWith('Waits on you: '),
      name: cell.attributes['data-who-acts'], accent: cell.all((e) => e.tagName === 'B').map((b) => b.textContent), cell: cell.textContent }];
  }));
}

// e r2: aria-busy is on the region being loaded (the agents host), never on the status message; it is
// cleared when the read answers and when it fails.
{
  const busy = (page) => {
    const host = page.all((e) => e.attributes && e.attributes['data-agents-host'] !== undefined)[0];
    const status = page.all((e) => e.attributes && e.attributes.role === 'status' && String(e.className).includes('loading'));
    return { host: host.attributes['aria-busy'] || null, status: status.map((e) => e.attributes['aria-busy'] || null), loading: status.length };
  };
  const first = await api.agents({ limit: 20, cursor: null, unconfirmed: 'false' });
  let release;
  const held = { ...api, agents: () => new Promise((r) => { release = () => r(first); }) };
  const page = await agents.list({ ...ctx, api: held });
  const during = busy(page);
  release();
  await settle();
  const after = busy(page);
  const failing = await agents.list({ ...ctx, api: { ...api, agents: async () => { throw failure(); } } });
  await settle();
  out.agentsBusy = { during, after, afterFailure: busy(failing) };
}

console.log(JSON.stringify(out));
