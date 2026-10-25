// The Lanes panel of the project page (kittrial-5bb.263, docs/LANE_BOARD_DESIGN.md sec. 4).
// Runs web/js/views/project.js under Node against a stubbed transport: no http_service is
// needed, because everything the panel shows is what the ONE board read answered -- this
// page computes no age and no flag of its own. Prints one JSON object of observations;
// tests/test_web_lanes.py asserts on it.
//
// argv: shim URL, api.js URL, project.js URL.
const [shimUrl, apiUrl, projectUrl] = process.argv.slice(2);
await import(shimUrl);
const { createApi } = await import(apiUrl);
const view = await import(projectUrl);

const PROJECT = { id: 'alpha', name: 'Alpha', role: 'owner', usable: true, archived: false, description: null };

// Three lanes as the read answers them: one holding a task it DECLARES a wait on, one
// holding a task whose state is only GUESSED from the checkpoint's prose, and one free.
const WAITING_LANE = {
  lane: 'lane-a', actor: 'session-a', idle: false, held: [{
    task: 'alpha-1', title: 'Ship the lanes panel', review_state: 'changes-requested',
    work_state: 'changes-requested', whose_move: 'you', next_actor: 'assignee',
    next: 'Deliver a revision', state: 'waiting-on-coordinator', waiting_on: 'coordinator',
    expect_by: '2026-10-12T00:00:00+00:00', declared: true, inferred: false,
    checkpoint: 'cp-1', state_since: '2026-10-12T00:00:00Z', state_age_seconds: 172800,
    no_checkpoint: false, held_age_seconds: 200000, holder_last_write_seconds: 90000,
    flags: ['waiting_too_long', 'expect_by_passed'], pending_review_items: 1,
  }],
  state: null, waiting_on: null, expect_by: null, inferred: false, state_since: null,
  state_age_seconds: null, last_write: '2026-10-15T10:00:00Z', last_write_age_seconds: 7200,
  waiting_too_long: true, expect_by_passed: true, your_move_idle: false,
  flag_tasks: { waiting_too_long: ['alpha-1'], expect_by_passed: ['alpha-1'] },
};
const GUESSED_LANE = {
  lane: 'lane-c', actor: 'session-c', idle: false, held: [{
    task: 'alpha-3', title: 'An older checkpoint', review_state: 'none', work_state: 'none',
    whose_move: 'you', next_actor: 'assignee', next: 'Read the review', state: 'waiting-on-coordinator',
    waiting_on: 'coordinator', expect_by: null, declared: false, inferred: true,
    checkpoint: 'cp-3', state_since: '2026-10-16T00:00:00Z', state_age_seconds: 3600,
    no_checkpoint: false, held_age_seconds: 40000, holder_last_write_seconds: 2000,
    flags: ['your_move_idle'], pending_review_items: 0,
  }],
  state: null, waiting_on: null, expect_by: null, inferred: false, state_since: null,
  state_age_seconds: null, last_write: '2026-10-17T19:30:00Z', last_write_age_seconds: 1800,
  waiting_too_long: false, expect_by_passed: false, your_move_idle: true,
  flag_tasks: { your_move_idle: ['alpha-3'] },
};
const FREE_LANE = {
  lane: 'lane-b', actor: 'session-b', idle: true, held: [], state: 'idle', waiting_on: null,
  expect_by: null, inferred: false, state_since: null, state_age_seconds: null,
  last_write: '2026-10-17T20:00:00Z', last_write_age_seconds: 300,
  waiting_too_long: false, expect_by_passed: false, your_move_idle: false, flag_tasks: {},
  needs_investigation: { record: 'c-9', task: 'alpha-4', about_task: 'alpha-5', reason: 'the <b>merge</b> slot is stuck',
    since: '2026-10-17T19:00:00Z', age_seconds: 3900,
    earlier: [{ record: 'c-8', task: 'alpha-4', about_task: null, reason: 'credentials leaked', since: '2026-10-17T18:00:00Z' }], earlier_omitted: 3 },
};

function board(idleMinutes, extra) {
  // A wider threshold is a different answer from the same read: 15 minutes flags the wait,
  // 45 does not. The panel must show what it was told, never recompute it from a clock.
  const flagged = idleMinutes <= 15;
  const waiting = JSON.parse(JSON.stringify(WAITING_LANE));
  if (!flagged) {
    waiting.held[0].flags = [];
    waiting.waiting_too_long = false; waiting.expect_by_passed = false; waiting.flag_tasks = {};
  }
  return Object.assign({
    schema: 'lanes-v1', generated_at: '2026-10-17T20:05:00Z', idle_minutes: idleMinutes,
    scope: 'all', total: 3, items: [waiting, GUESSED_LANE, FREE_LANE],
    next_offset: null,
    flags: { waiting_too_long: flagged ? 1 : 0, expect_by_passed: flagged ? 1 : 0, your_move_idle: 1 },
    coverage: 'One lane per registered session name; inferred true marks a row whose state is not declared.',
  }, extra);
}

let scopeExtra = {};
const calls = [];
function transport(method, path) {
  calls.push(method + ' ' + path);
  if (path === '/v1/projects/alpha') return Promise.resolve({ status: 200, data: PROJECT });
  if (path.startsWith('/v1/projects/alpha/tasks')) {
    return Promise.resolve({ status: 200,
      data: { items: [], total: 0, next_cursor: null, review_states_complete: true } });
  }
  if (path.startsWith('/v1/projects/alpha/lanes')) {
    const query = new URLSearchParams(path.split('?')[1] || '');
    const idle = Number(query.get('idle_minutes') || 15);
    if (idle === 7) {
      return Promise.resolve({ status: 403,
        data: { error: { code: 'forbidden', message: 'Your access to this project has ended.' } } });
    }
    // The first read offers a next page; a read after a threshold change does not.
    const extra = calls.filter((c) => c.startsWith('GET /v1/projects/alpha/lanes')).length === 1
      ? { next_offset: 10, next_cursor: 'cursor-page-2' } : {};
    return Promise.resolve({ status: 200, data: board(idle, Object.assign({}, extra, scopeExtra)) });
  }
  return Promise.resolve({ status: 404,
    data: { error: { code: 'not_found', message: 'No such operation' } } });
}

const ctx = { api: createApi(transport), href: (p) => '#' + p, go() {}, query: {},
  me: { superuser: false, display_name: 'Owner' }, render() {}, refreshProjects: async () => {},
  projects: [] };

async function settle() {
  for (let i = 0; i < 60; i += 1) await new Promise((resolve) => setTimeout(resolve, 5));
}

const lanesCalls = () => calls.filter((c) => c.startsWith('GET /v1/projects/alpha/lanes'));
const button = (node, label) => Array.from(node.querySelectorAll('button')).find((b) => b.textContent === label);

// 1. The panel on its own, as the overview composes it.
const panel = view.lanesPanel(ctx, PROJECT);
document.body.append(panel);
await settle();
const out = {
  firstRead: lanesCalls()[0],
  text: panel.textContent,
  lanes: panel.all((e) => e.attributes && e.attributes['data-lane']).map((e) => e.attributes['data-lane']),
  taskIds: panel.all((e) => e.tagName === 'LI').map((e) => e.attributes['data-task']),
  tasks: panel.all((e) => e.tagName === 'LI').map((e) => e.textContent),
  inferredTasks: panel.all((e) => e.tagName === 'LI').map((e) => e.attributes['data-inferred'] || null),
  flagChips: panel.all((e) => e.tagName === 'SPAN' && e.attributes['data-flag'])
    .map((e) => e.attributes['data-flag'] + ': ' + e.textContent),
  guessedChips: panel.all((e) => e.tagName === 'SPAN' && e.textContent === 'Guessed')
    .map((e) => e.attributes['title']),
  freeText: panel.all((e) => e.tagName === 'P' && e.textContent.includes('Holds nothing'))
    .map((e) => e.textContent),
  countsLine: panel.all((e) => e.tagName === 'P' && e.textContent.includes('idle minutes'))
    .map((e) => e.textContent),
  threshold: panel.querySelector('#lanes-idle-minutes')
    ? panel.querySelector('#lanes-idle-minutes').value : null,
  fault: panel.all((e) => e.attributes && e.attributes['data-fault']).map((e) => e.textContent),
  faultEarlier: panel.all((e) => e.attributes && e.attributes['data-fault-earlier']).map((e) => e.textContent),
  faultOmitted: panel.all((e) => e.attributes && e.attributes['data-fault-omitted']).map((e) => e.textContent),
  nextEnabled: !button(panel, 'Next').disabled,
};
// 2. The overview composes it.
const overview = await view.overview(ctx, { pid: 'alpha' });
await settle();
out.overviewHasPanel = overview.all((e) => e.attributes && e.attributes['data-panel'] === 'lanes').length;
// 3. A paged answer: Next asks for the read's next page, with the SAME threshold.
await button(panel, 'Next').dispatch('click');
await settle();
out.pagedRead = lanesCalls().at(-1);
// 4. The threshold is the page's argument: a value outside the read's range is refused
//    here, before any read, and says what the range is.
const input = panel.querySelector('#lanes-idle-minutes');
const before = lanesCalls().length;
input.value = '0';
await button(panel, 'Read with this threshold').dispatch('click');
await settle();
out.refusedText = panel.textContent;
out.refusedIssuedNoRead = lanesCalls().length === before;
// 5. A new value re-reads with it, from the first page (no cursor), and the flags the read
//    answered with are what the panel shows.
input.value = '45';
await button(panel, 'Read with this threshold').dispatch('click');
await settle();
out.thresholdRead = lanesCalls().at(-1);
out.afterThreshold = panel.textContent;
out.thresholdChips = panel.all((e) => e.tagName === 'SPAN' && e.attributes['data-flag'])
  .map((e) => e.attributes['data-flag'] + ': ' + e.textContent);
// 6. A refused read says why and offers to read again instead of reading as an empty board.
input.value = '7';
await button(panel, 'Read with this threshold').dispatch('click');
await settle();
out.refusal = panel.textContent;
out.refusalHasRetry = Boolean(button(panel, 'Try again'));
// 7. The sentence about what this viewer sees is built from the server's scope and its basis.
out.scopeSentences = {};
const variants = {
  operator: { scope: 'all', scope_basis: 'operator', scope_role: null },
  owner: { scope: 'all', scope_basis: 'project-role', scope_role: 'owner' },
  coordinator: { scope: 'all', scope_basis: 'project-role', scope_role: 'coordinator' },
  grant: { scope: 'all', scope_basis: 'coordinator-grant', scope_role: null },
  superuser: { scope: 'all', scope_basis: 'superuser', scope_role: null },
  own: { scope: 'own', scope_basis: 'own', scope_role: null },
  unknown: { scope: 'all', scope_basis: 'something-new', scope_role: null },
};
for (const [name, extra] of Object.entries(variants)) {
  scopeExtra = extra;
  const fresh = view.lanesPanel(ctx, PROJECT);
  document.body.append(fresh);
  await settle();
  out.scopeSentences[name] = fresh.all((e) => e.tagName === 'P' && e.textContent.includes('idle minutes'))
    .map((e) => e.textContent);
}
out.calls = calls;
console.log(JSON.stringify(out));
