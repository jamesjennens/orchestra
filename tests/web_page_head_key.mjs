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
    key: key ? { shown: !key.hidden, num: key.children[0].textContent, label: key.children[1].textContent,
      source: key.attributes['data-key-source'] || null } : null,
    actions: actions.map((e) => ({ text: e.textContent, primary: String(e.className).includes('primary'),
      next: e.attributes['data-next-action'] || null, href: e.attributes.href || null })),
  };
}
const ids = (page) => page.all((e) => e.attributes && e.attributes.id).map((e) => e.attributes.id);
const out = {};

// My work: everything waiting on you, from the three lists the page shows.
{
  const data = await api.myWork();
  const page = await work.home(ctx);
  await settle();
  const since = (t) => Date.parse(t.waiting_since || t.updated_at || '') || Infinity;
  const oldest = data.to_review.slice().sort((a, b) => since(a) - since(b))[0];
  out.myWork = { head: header(page),
    expected: { num: String(data.assigned.length + data.to_review.length), incomplete: Boolean(data.truncated || (data.unavailable && data.unavailable.length)),
      oldestReview: oldest ? oldest.id : null } };
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

// Members & settings: the member count.
{
  const data = await api.members(PID);
  const page = await project.settings(ctx, { pid: PID });
  await settle();
  out.settings = { head: header(page), ids: ids(page),
    expected: { num: String(typeof data.total === 'number' ? data.total : data.items.length) } };
}

console.log(JSON.stringify(out));
