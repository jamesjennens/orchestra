// Node DOM-shim test for /p/{pid}/waiting, Wait times panel (/p/{pid}), and My work
// "Waiting on you" panel (kittrial-5bb.598 Slice 2).
//
// argv: shim URL, api.js URL, routes.js URL, waiting.js URL, work.js URL, project.js URL.
const [shimUrl, apiUrl, routesUrl, waitingUrl, workUrl, projectUrl] = process.argv.slice(2);
await import(shimUrl);
const { createApi } = await import(apiUrl);
const routes = await import(routesUrl);
const waitingView = await import(waitingUrl);
const workView = await import(workUrl);
const projectView = await import(projectUrl);

const PROJECT = {
  id: 'alpha',
  name: 'Alpha',
  role: 'coordinator',
  usable: true,
  archived: false,
  description: null,
  overdue_escalations: {
    total: 3,
    overdue_count: 2,
    unreached_count: 1,
    project_visible_count: 1,
    silent_count: 1,
    snoozed_count: 1,
  },
};

const WAIT_ITEMS = [
  {
    kind: 'wait',
    episode_id: 'ep-project-wide-1',
    task: 'alpha-1',
    title: 'Oldest <script>wait</script>',
    status: 'in_progress',
    lane: 'lane-a',
    actor: 'session-a',
    display_name: 'Lane A',
    waiting_on: 'coordinator',
    waiting_on_name: 'coordinator',
    started_at: '2026-11-22T00:00:00Z',
    age_seconds: 172800,
    expect_by: '2026-11-22T04:00:00Z',
    overdue: true,
    overdue_seconds: 158400,
    escalation_stage: 'project-visible',
    snoozed: true,
    snooze_count: 1,
    snooze: {
      until: '2026-11-25T12:00:00Z',
      reason: 'Waiting for <b>batch</b>',
      actor: 'usr_coord',
      actor_display: 'Coord Sam',
      waited_on_party: true,
      suppresses_owner_notice: false,
    },
  },
  {
    kind: 'wait',
    episode_id: 'ep-unreached-2',
    task: 'alpha-2',
    title: 'Second wait unreached',
    status: 'open',
    lane: 'lane-b',
    actor: 'session-b',
    display_name: 'Lane B',
    waiting_on: 'reviewer',
    waiting_on_name: 'reviewer',
    started_at: '2026-11-23T08:00:00Z',
    age_seconds: 28800,
    expect_by: '2026-11-23T12:00:00Z',
    overdue: true,
    overdue_seconds: 14400,
    escalation_stage: 'owner-not-reached',
    snoozed: false,
    snooze_count: 0,
    snooze: null,
  },
  {
    kind: 'wait',
    episode_id: 'ep-told-3',
    task: 'alpha-3',
    title: 'Third wait owner told',
    status: 'blocked',
    lane: 'lane-c',
    actor: 'session-c',
    display_name: 'Lane C',
    waiting_on: 'user',
    waiting_on_name: 'user',
    started_at: '2026-11-23T10:00:00Z',
    age_seconds: 21600,
    expect_by: '2026-11-23T12:00:00Z',
    overdue: true,
    overdue_seconds: 14400,
    escalation_stage: 'owner-notified',
    snoozed: false,
    snooze_count: 0,
    snooze: null,
  },
];

const SILENT_ITEMS = [
  {
    kind: 'silent-lane',
    silent: true,
    episode_id: 'ep-silent-4',
    task: 'alpha-4',
    title: 'Silent worker task',
    status: 'in_progress',
    lane: 'lane-d',
    actor: 'session-d',
    display_name: 'Lane D',
    waiting_on: null,
    waiting_on_name: null,
    started_at: '2026-11-23T06:00:00Z',
    age_seconds: 36000,
    expect_by: '2026-11-23T10:00:00Z',
    overdue: true,
    overdue_seconds: 21600,
    escalation_stage: 'overdue',
    snoozed: false,
    snooze_count: 0,
    snooze: null,
  },
];

const METRICS_PAYLOAD = {
  schema_version: 1,
  scope: 'all',
  days: 14,
  computed_at: '2026-11-24T16:00:00Z',
  by_target: [
    {
      day: '2026-11-24',
      waiting_on: 'coordinator',
      waiting_on_name: 'coordinator',
      resolved_members: [{ id: 'usr_coord', name: 'Coord Sam' }],
      answered: { count: 3, median_seconds: 1800, max_seconds: 7200 },
      closed_unanswered: { count: 1, max_seconds: 3600 },
      superseded: { count: 0, max_seconds: null },
      open_now: { open_count: 1, open_max_seconds: 172800 },
    },
  ],
  by_actor: [
    {
      day: '2026-11-24',
      actor: 'usr_coord',
      actor_name: 'Coord Sam',
      answered: { count: 3, median_seconds: 1800, max_seconds: 7200 },
    },
  ],
};

const calls = [];
const snoozePayloads = [];
let snoozeMode = 'ok';

function transport(method, path, _headers, body) {
  calls.push(method + ' ' + path);
  if (method === 'GET' && path === '/v1/projects/alpha') {
    return Promise.resolve({ status: 200, data: PROJECT });
  }
  if (method === 'GET' && path.startsWith('/v1/projects/alpha/waiting/metrics')) {
    const query = new URLSearchParams(path.split('?')[1] || '');
    const days = Number(query.get('days') || 14);
    return Promise.resolve({ status: 200, data: Object.assign({}, METRICS_PAYLOAD, { days }) });
  }
  if (method === 'GET' && path.startsWith('/v1/projects/alpha/waiting')) {
    const query = new URLSearchParams(path.split('?')[1] || '');
    const flt = query.get('filter') || 'all';
    const silentHours = Number(query.get('silent_hours') || 4);
    return Promise.resolve({
      status: 200,
      data: {
        schema_version: 1,
        project: 'alpha',
        scope: 'all',
        filter: flt,
        silent_hours: silentHours,
        total: flt === 'on-me' ? 1 : WAIT_ITEMS.length,
        overdue_count: flt === 'on-me' ? 1 : 3,
        unreached_count: flt === 'on-me' ? 0 : 1,
        project_visible_count: 1,
        snoozed_count: 1,
        silent_count: SILENT_ITEMS.length,
        can_snooze: true,
        items: flt === 'on-me' ? [WAIT_ITEMS[0]] : WAIT_ITEMS,
        silent: SILENT_ITEMS,
        next_cursor: null,
      },
    });
  }
  if (method === 'POST' && path === '/v1/projects/alpha/waiting/snooze') {
    const parsed = body ? JSON.parse(body) : {};
    snoozePayloads.push(parsed);
    if (snoozeMode === '422' || !parsed.until) {
      return Promise.resolve({
        status: 422,
        data: { error: { code: 'invalid', message: "A snooze requires an explicit future 'until' timestamp. Nothing was written." } },
      });
    }
    if (snoozeMode === '409') {
      return Promise.resolve({
        status: 409,
        data: { error: { code: 'conflict', message: 'A wait episode may be snoozed at most 2 times. Nothing was written.' } },
      });
    }
    return Promise.resolve({
      status: 201,
      data: {
        task: parsed.task,
        episode_id: parsed.episode_id,
        comment_id: 'c-sn-1',
        snooze: { until: parsed.until, reason: parsed.reason, waited_on_party: true, suppresses_owner_notice: false },
        snooze_count: 2,
        waited_on_party: true,
        suppresses_owner_notice: false,
      },
    });
  }
  if (method === 'GET' && path === '/v1/me/work') {
    return Promise.resolve({
      status: 200,
      data: {
        assigned: [],
        to_review: [],
        waiting_on_you: {
          total: 1,
          overdue_count: 1,
          unreached_count: 1,
          snoozed_count: 0,
          silent_lanes_count: 1,
          items: [Object.assign({ project: 'alpha', project_name: 'Alpha' }, WAIT_ITEMS[1])],
          silent: [Object.assign({ project: 'alpha', project_name: 'Alpha' }, SILENT_ITEMS[0])],
        },
        agents: [],
        agent_prompts: [],
        truncated: false,
        unavailable: [],
        generated_at: '2026-11-24T16:00:00Z',
      },
    });
  }
  if (method === 'GET' && path.startsWith('/v1/me/contributions')) {
    return Promise.resolve({ status: 200, data: { items: [], total: 0 } });
  }
  if (method === 'GET' && path.startsWith('/v1/projects/alpha/tasks')) {
    return Promise.resolve({ status: 200, data: { items: [], total: 0, next_cursor: null, review_states_complete: true } });
  }
  return Promise.resolve({ status: 404, data: { error: { code: 'not_found', message: 'No such route' } } });
}

const ctx = {
  api: createApi(transport),
  href: (p) => '#' + p,
  go() {},
  query: new URLSearchParams(''),
  me: { id: 'usr_coord', superuser: false, display_name: 'Coord Sam' },
  render() {},
  refreshProjects: async () => {},
  projects: [PROJECT],
};

async function settle() {
  for (let i = 0; i < 40; i += 1) await new Promise((r) => setTimeout(r, 5));
}

function byAttr(root, tag, attr, val) {
  return Array.from(root.querySelectorAll(tag)).filter((el) =>
    val === undefined ? el.getAttribute(attr) != null : el.getAttribute(attr) === val);
}

// 1. Route matching & switcher
const matched = routes.matchRoute('/p/alpha/waiting');
const switched = routes.switchTarget('/p/alpha/waiting', 'beta', ['lanes', 'waiting']);

// 2. Waiting page default render (MUST NOT call /waiting/metrics!)
const page = await waitingView.waitingPage(ctx, { pid: 'alpha' });
await settle();

const callsBeforeMetrics = calls.slice();
const waitRows = byAttr(page, 'article', 'data-wait-row').map((el) => ({
  id: el.getAttribute('data-wait-row'),
  task: el.getAttribute('data-task'),
  kind: el.getAttribute('data-kind'),
  text: el.textContent,
}));
const stageChipValues = byAttr(page, 'span', 'data-stage').map((el) => ({
  stage: el.getAttribute('data-stage'),
  text: el.textContent,
}));
const antiSuppressCount = byAttr(page, 'div', 'data-snooze-anti-suppress', 'true').length;

// 3. Filter toggle -> Waiting on me
const onMeBtn = Array.from(page.querySelectorAll('button')).find((b) => b.textContent === 'Waiting on me');
await onMeBtn.dispatch('click');
await settle();
const callsAfterFilter = calls.slice();

// Switch back to All waits so all rows are present
const allBtn = Array.from(page.querySelectorAll('button')).find((b) => b.textContent === 'All waits');
await allBtn.dispatch('click');
await settle();

// 4. Snooze form validation 422, conflict 409, and success 201
const form = byAttr(page, 'form', 'data-snooze-form', 'ep-unreached-2')[0];
const inputs = Array.from(form.querySelectorAll('input'));
const untilIn = inputs.find((i) => i.getAttribute('name') === 'until');
const reasonIn = inputs.find((i) => i.getAttribute('name') === 'reason');
const errEl = byAttr(form, 'div', 'data-snooze-error', 'true')[0];

snoozeMode = '422';
untilIn.value = '';
reasonIn.value = 'No end time';
await form.dispatch('submit');
const err422 = errEl.textContent;

snoozeMode = '409';
untilIn.value = '2026-11-25T12:00:00Z';
reasonIn.value = 'Third renewal';
await form.dispatch('submit');
const err409 = errEl.textContent;

snoozeMode = 'ok';
untilIn.value = '2026-11-25T12:00:00Z';
reasonIn.value = 'Wait for release window';
await form.dispatch('submit');
await settle();
const errAfterOk = errEl.textContent;

// 5. Metrics tab lazy-load
const metricsBtn = byAttr(page, 'button', 'data-tab', 'metrics')[0];
await metricsBtn.dispatch('click');
await settle();
const callsAfterMetrics = calls.slice();
const targetTableText = byAttr(page, 'table', 'data-table', 'metrics-by-target').map((t) => t.textContent);
const actorTableText = byAttr(page, 'table', 'data-table', 'metrics-by-actor').map((t) => t.textContent);

// 6. My work waiting_on_you panel + workKey
const homeNode = await workView.home(ctx);
await settle();
const waitingPanel = byAttr(homeNode, 'section', 'data-panel', 'waiting-on-you');
const homeText = homeNode.textContent;

// 7. Project overview Wait times panel + overdue banner
const overviewNode = await projectView.overview(ctx, { pid: 'alpha' });
await settle();
const waitTimesNode = byAttr(overviewNode, 'section', 'data-panel', 'wait-times');
const overdueBannerNode = byAttr(overviewNode, 'div', 'data-banner', 'overdue-escalations');

console.log(JSON.stringify({
  matched,
  sectionLabel: routes.SECTION_LABELS.waiting,
  switched,
  metricsCalledOnDefaultLoad: callsBeforeMetrics.some((c) => c.includes('/waiting/metrics')),
  waitRowOrder: waitRows.map((r) => r.task),
  waitRowKinds: waitRows.map((r) => r.kind),
  stageChipValues,
  antiSuppressCount,
  scriptTagCount: Array.from(page.querySelectorAll('script')).length,
  onMeCalled: callsAfterFilter.some((c) => c.includes('filter=on-me')),
  snoozePayloads,
  err422,
  err409,
  errAfterOk,
  metricsCalledOnTab: callsAfterMetrics.some((c) => c.includes('/waiting/metrics?days=14')),
  targetTableText,
  actorTableText,
  myWorkHasWaitingPanel: waitingPanel.length === 1,
  myWorkWaitingText: waitingPanel.length ? waitingPanel[0].textContent : '',
  myWorkKeyText: homeText,
  overviewWaitTimesText: waitTimesNode.length ? waitTimesNode[0].textContent : '',
  overviewBannerText: overdueBannerNode.length ? overdueBannerNode[0].textContent : '',
}));
