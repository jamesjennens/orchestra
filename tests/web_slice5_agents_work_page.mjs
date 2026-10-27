// Runs the slice-5 agent card and My work page under Node with the DOM shim
// (review b2/notes r2). No http_service is needed: agentCard is pure and the
// home myWork/myContributions reads are stubbed. Prints one JSON object;
// tests/test_rooms_http.py asserts on it.
//
// argv: shim URL, agents.js URL, work.js URL.
const [shimUrl, agentsUrl, workUrl] = process.argv.slice(2);
await import(shimUrl);
const agentViews = await import(agentsUrl);
const workViews = await import(workUrl);

const nextLines = (root) => root.all((e) => e.tagName === 'P'
  && String(e.className || e.attributes['class'] || '').split(' ').includes('next')).map((e) => e.textContent);
const whoActs = (root) => root.all((e) => e.tagName === 'SPAN'
  && String(e.className || e.attributes['class'] || '').includes('who-acts')).map((e) => e.textContent);
const emptyLines = (root) => root.all((e) => e.tagName === 'P'
  && String(e.className || e.attributes['class'] || '').includes('empty-line')).map((e) => e.textContent);

function ctx() {
  return { href: (path) => '#' + path, go() {}, me: { id: 'usr_1', superuser: true }, projects: [] };
}
// Server-shaped agents: attention is { state, summary, counts } with one item;
// normalize() (agents.js:27) reads summary/items off it.
const ownerAgent = { id: 'a-1', display_name: 'portal-agent', tool: 'Claude Code',
  working_directory: '/tmp/portal', last_seen_at: null, enabled: true,
  attention: { state: 'changes-requested', summary: 'Fix the failing test', counts: { claimable: 0 } },
  items: [{ project_id: 'alpha', task_id: 't-1', title: 'Fix it', text: 'Fix the failing test' },
    { project_id: 'alpha', task_id: 't-2', title: 'Then this', text: 'Then do the next thing' }],
  projects: [], credentials: [] };
const agentCaseAgent = { id: 'a-2', display_name: 'report-agent', tool: 'Claude Code',
  working_directory: '/tmp/report', last_seen_at: null, enabled: true,
  attention: { state: 'working', summary: 'nothing from you. It holds nightly report', counts: { claimable: 0 } },
  items: [], projects: [], credentials: [] };

const ownerCard = agentViews.agentCard(ctx(), ownerAgent);
const bareCard = agentViews.agentCard(ctx(), agentCaseAgent);

function workCtx(myWork) {
  return { href: (path) => '#' + path, go() {}, render() {}, me: { id: 'usr_1' },
    projects: [{ id: 'alpha', archived: false }],
    api: { myWork: async () => myWork,
      myContributions: async () => ({ items: [], total: 0, next_cursor: null }) } };
}
const fullWork = { truncated: false, unavailable: [],
  assigned: [{ id: 't-1', project_id: 'alpha', title: 'Do it', status: 'open', review_state: 'none' }],
  to_review: [{ id: 't-9', project_id: 'alpha', title: 'Review me', status: 'open', review_state: 'awaiting-review' }],
  agents: [ownerAgent] };
const emptyWork = { truncated: false, unavailable: [], assigned: [], to_review: [], agents: [] };
const full = await workViews.home(workCtx(fullWork));
const bare = await workViews.home(workCtx(emptyWork));
console.log(JSON.stringify({
  ownerNext: nextLines(ownerCard),
  ownerWho: whoActs(ownerCard),
  ownerRest: ownerCard.all((e) => e.tagName === 'UL'
    && String(e.className || e.attributes['class'] || '').includes('agent-items')).map((e) => e.textContent),
  agentNext: nextLines(bareCard),
  agentWho: whoActs(bareCard),
  workFull: { text: full.textContent, empty: emptyLines(full),
    agentSections: full.all((e) => e.attributes['data-agent']).length },
  workEmpty: { text: bare.textContent, empty: emptyLines(bare) },
}));
