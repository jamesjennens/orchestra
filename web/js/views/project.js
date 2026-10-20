import { h, time, confirmDialog, secretDialog, toast, shortSha } from '../dom.js';
import { pageHead, reviewChip, statusChip, priority, empty, field, setFieldError, formValues, act, errorState, describe } from '../ui.js';
import { queuePanel, detailPanel, proposeForm, myStrip } from './proposals.js';
import { setupHint } from './setup.js';
import { mergeSlotPanel } from './merge_slot.js';
import { guidancePanel } from './guidance.js';
import { coordinationPanel } from './coordination.js';
import { expiryLine, extensionButton } from '../credentialLifetime.js';

const PAGE = 10;
export const canWrite = (p) => ['owner', 'coordinator', 'contributor', 'superuser'].includes(p.role);
export const isOwner = (p) => ['owner', 'superuser'].includes(p.role);
// Who decides on a contribution: an owner, or a coordinator (kittrial-5bb.209). Not who administers the project.
export const canApprove = (p) => ['owner', 'coordinator', 'superuser'].includes(p.role);
// Why a grant on record has no effect now, as the server says it (`coordinator.no_effect_because`).
export const NO_EFFECT = {
  'agent-disabled': ' — the agent is disabled',
  'account-disabled': ' — its account is disabled',
  'account-role': ' — its account is no longer a coordinator or owner here',
};
export const ROLE_NAMES = [['viewer', 'Viewer'], ['contributor', 'Contributor'], ['coordinator', 'Coordinator'], ['owner', 'Owner']];

async function load(ctx, pid) {
  const project = await ctx.api.project(pid);
  return project;
}

function crumbs(ctx, project, extra) {
  return [{ label: 'Projects', href: ctx.href('/projects') }, { label: project.name, href: extra ? ctx.href('/p/' + project.id) : null }].concat(extra ? [{ label: extra }] : []);
}

function archivedBanner(project) {
  return project.archived ? h('div', { class: 'banner' }, 'This project is archived. Its records are kept and readable; new work cannot be added.') : null;
}

// A record this server will not serve (project.usable === false): its task pages answer
// 409, so no page offers them. Only reading, removing access and archiving are left.
export function unusableBanner(project) {
  return project.usable === false ? h('div', { class: 'banner crit', role: 'alert' }, project.unusable_reason || 'This project cannot be used on this server.',
    ' Nothing that grants access is accepted on it; removing a member, revoking a credential and archiving still work.') : null;
}

// -- the lanes board (kittrial-5bb.263, docs/LANE_BOARD_DESIGN.md section 4) -------------
// ONE read answers this panel: the canonical `work lanes` read, through
// GET /v1/projects/<pid>/lanes. No age and no flag is computed here -- not the state age,
// not the last-write age, not waiting_too_long/expect_by_passed/your_move_idle -- because
// a second implementation of either on this side is exactly what the design forbids. The
// threshold below is an argument of THIS page: it travels with every read, and it is not a
// setting the page could inherit.
export const LANES_PAGE = 10;
export const LANES_DEFAULT_IDLE_MINUTES = 15;
export const LANES_IDLE_MINUTES_MIN = 1;
export const LANES_IDLE_MINUTES_MAX = 10080;
const LANE_FLAGS = [
  ['waiting_too_long', 'Waiting too long'],
  ['expect_by_passed', 'Expect-by passed'],
  ['your_move_idle', "Holder's move idle"],
];

// The server's ages are whole seconds; said as a rough span, never re-derived from a clock.
export function ageText(seconds) {
  if (seconds === null || seconds === undefined) return 'unknown';
  if (seconds < 60) return 'under a minute';
  if (seconds < 3600) return Math.floor(seconds / 60) + ' min';
  if (seconds < 86400) return Math.floor(seconds / 3600) + ' h';
  const days = Math.floor(seconds / 86400);
  return days + (days === 1 ? ' day' : ' days');
}

export function lanesPanel(ctx, project) {
  const panel = h('section', { class: 'panel', 'data-panel': 'lanes', 'aria-label': 'Lanes' });
  const state = { idle_minutes: LANES_DEFAULT_IDLE_MINUTES, cursor: null, stack: [] };
  const minutes = h('input', { type: 'number', id: 'lanes-idle-minutes', min: LANES_IDLE_MINUTES_MIN,
    max: LANES_IDLE_MINUTES_MAX, value: String(LANES_DEFAULT_IDLE_MINUTES),
    'aria-label': 'Idle minutes (1 to 10080)' });
  const message = h('span', { class: 'small muted', role: 'status' });
  // The threshold is the page's argument: changing it reads the board again from its first
  // page, because a cursor belongs to the query (limit and threshold) that minted it.
  function readAgain(value) {
    const chosen = Number(value);
    if (!Number.isInteger(chosen) || chosen < LANES_IDLE_MINUTES_MIN || chosen > LANES_IDLE_MINUTES_MAX) {
      message.textContent = `Idle minutes must be a whole number from ${LANES_IDLE_MINUTES_MIN} to ${LANES_IDLE_MINUTES_MAX}.`;
      return;
    }
    message.textContent = '';
    state.idle_minutes = chosen; state.cursor = null; state.stack = [];
    draw();
  }
  const apply = h('button', { type: 'button', onclick: () => readAgain(minutes.value) }, 'Read with this threshold');
  const head = () => h('div', { class: 'panel-head' },
    h('h2', { class: 'small' }, 'Lanes'),
    h('span', { class: 'small muted hide-narrow' }, 'Who holds what, who is free, how long each has been that way, and the three flags: all of it from one board read on the server.'));
  const threshold = () => h('div', { class: 'toolbar' },
    h('label', { class: 'check', for: 'lanes-idle-minutes' }, 'Idle minutes'), minutes, apply, message);

  function taskLine(entry) {
    const flags = LANE_FLAGS.filter(([key]) => (entry.flags || []).includes(key));
    return h('li', { class: 'lane-task', 'data-task': entry.task, 'data-inferred': entry.inferred ? 'true' : null },
      h('a', { class: 'title', href: ctx.href(`/p/${project.id}/t/${entry.task}`) }, entry.title || entry.task),
      h('div', { class: 'sub' }, h('span', { class: 'mono' }, entry.task),
        ' · review ', h('span', { class: 'mono' }, entry.review_state || 'none'),
        ' · whose move ', h('span', { class: 'mono' }, entry.whose_move || 'unknown'),
        ' · next ', h('span', { class: 'mono' }, entry.next_actor || 'unknown')),
      h('div', { class: 'sub' },
        'State ', entry.state ? h('span', { class: 'mono' }, entry.state) : h('span', { class: 'muted' }, 'not declared'),
        entry.waiting_on ? [' · waiting on ', h('span', { class: 'mono' }, entry.waiting_on)] : null,
        entry.expect_by ? [' · expect by ', h('span', { class: 'mono' }, entry.expect_by)] : null,
        ' · in that state ', ageText(entry.state_age_seconds),
        ' · holder last wrote ', ageText(entry.holder_last_write_seconds)),
      // A row the read could not declare is GUESSED from the checkpoint text, and says so.
      entry.inferred ? h('div', { class: 'small', 'data-inferred': 'true' },
        h('span', { class: 'chip plain', title: 'The state is guessed from the checkpoint text, not declared by a lane-state record' }, 'Guessed')) : null,
      flags.length ? h('div', { class: 'small' }, flags.map(([key, label]) =>
        h('span', { class: 'chip warn', 'data-flag': key }, label))) : null);
  }

  function laneBlock(lane) {
    const held = lane.held || [];
    const raised = LANE_FLAGS.filter(([key]) => lane[key]);
    return h('div', { class: 'lane', 'data-lane': lane.lane, 'data-actor': lane.actor,
      'data-inferred': lane.inferred ? 'true' : null },
      h('div', { class: 'event-head' },
        h('strong', null, lane.lane), ' ', h('span', { class: 'mono small muted' }, lane.actor),
        ' ',
        lane.idle ? h('span', { class: 'chip', 'data-idle': 'true', title: 'This lane holds nothing' }, 'Free')
          : h('span', { class: 'chip plain' }, held.length === 1 ? '1 task held' : held.length + ' tasks held'),
        lane.inferred ? h('span', { class: 'chip plain', 'data-inferred': 'true', title: 'Guessed, not declared' }, 'Guessed') : null,
        h('span', { class: 'small muted' }, ' last write ', ageText(lane.last_write_age_seconds))),
      // Raised flags, each naming the tasks the read put under it (`flag_tasks`).
      raised.length ? h('div', { class: 'small' }, raised.map(([key, label]) => h('span', { class: 'chip warn', 'data-flag': key },
        label, (lane.flag_tasks && lane.flag_tasks[key] ? ': ' + lane.flag_tasks[key].join(', ') : '')))) : null,
      held.length ? h('ul', { class: 'lane-tasks' }, held.map((entry) => taskLine(entry))) : null,
      lane.idle ? h('p', { class: 'small muted' }, 'Holds nothing; free to take work. Last write ', ageText(lane.last_write_age_seconds), ' ago.') : null);
  }

  function pager(data) {
    const shown = (data.items || []).length;
    return h('div', { class: 'pager' },
      h('span', { class: 'num' }, data.total ? `${shown} of ${data.total} lane${data.total === 1 ? '' : 's'} on this page` : ''),
      h('div', { class: 'actions' },
        h('button', { type: 'button', disabled: !state.stack.length, onclick: () => { state.cursor = state.stack.pop() || null; draw(); } }, 'Previous'),
        h('button', { type: 'button', disabled: !data.next_cursor, onclick: () => { state.stack.push(state.cursor); state.cursor = data.next_cursor; draw(); } }, 'Next')));
  }

  async function draw() {
    panel.setAttribute('aria-busy', 'true');
    let data;
    try {
      data = await ctx.api.lanes(project.id, { idle_minutes: state.idle_minutes, limit: LANES_PAGE, cursor: state.cursor });
    } catch (error) {
      // A refused read says why and offers to read again, as the neighbouring panels do.
      // A server older than this route answers 404/501: the panel then simply is not there.
      if ([404, 501].includes(error.status)) { panel.replaceChildren(); return; }
      panel.replaceChildren(head(), threshold(), errorState(error, draw));
      return;
    } finally { panel.removeAttribute('aria-busy'); }
    const lanes = data.items || [];
    const counts = data.flags || {};
    panel.replaceChildren(head(), threshold(),
      h('p', { class: 'small muted', role: 'note' },
        'Reading with ', String(data.idle_minutes), ' idle minutes: ',
        LANE_FLAGS.map(([key, label]) => `${counts[key] || 0} ${label.toLowerCase()}`).join(' · '),
        data.scope === 'own' ? '. You see your own lanes; owners and coordinators see every lane.' : '.'),
      lanes.length ? h('div', { class: 'stack' }, lanes.map(laneBlock))
        : empty('No lanes to show', 'No session holds work in this project right now.'),
      pager(data));
  }
  draw();
  return panel;
}

export async function overview(ctx, { pid }) {
  const project = await load(ctx, pid);
  if (project.usable === false) {
    return h('div', { class: 'stack' },
      pageHead({ crumbs: crumbs(ctx, project), title: project.name }),
      archivedBanner(project),
      unusableBanner(project),
      h('p', null, h('a', { href: ctx.href(`/p/${pid}/settings`) }, 'Members, credentials and archive')));
  }
  const state = { status: 'active', q: '', review: '', cursor: null, stack: [], requirements: false };
  const tableHost = h('div', { class: 'panel' });

  const search = h('input', { type: 'search', id: 'task-search', placeholder: 'Search title, description or ID', 'aria-label': 'Search tasks' });
  // A requirement record or BRD section is a visible row the server never calls claimable
  // work (kittrial-5bb.277): it is left out of the default task view, and this filter shows
  // it again marked as a requirement. Off by default, so the Tasks page reads as tasks.
  const requirementFilter = h('input', { type: 'checkbox', id: 'show-requirements' });
  const requirementToggle = h('label', { class: 'check', for: 'show-requirements' }, requirementFilter, 'Show requirements');
  requirementFilter.addEventListener('change', () => {
    state.requirements = requirementFilter.checked; state.cursor = null; state.stack = []; draw();
  });
  const statusSeg = h('div', { class: 'seg', role: 'group', 'aria-label': 'Status' },
    [['active', 'Active'], ['closed', 'Closed'], ['', 'All']].map(([value, label]) => h('button', { type: 'button', 'aria-pressed': String(state.status === value), onclick: (e) => {
      state.status = value; state.cursor = null; state.stack = [];
      statusSeg.querySelectorAll('button').forEach((b) => b.setAttribute('aria-pressed', String(b === e.currentTarget)));
      draw();
    } }, label)));
  const reviewSel = h('select', { id: 'review-filter', 'aria-label': 'Review state', onchange: (e) => { state.review = e.target.value; state.cursor = null; state.stack = []; draw(); } },
    [['', 'Any review state'], ['awaiting-review', 'Awaiting review'], ['changes-requested', 'Changes requested'], ['approved', 'Approved · not integrated'], ['withdrawn', 'Withdrawn'], ['superseded', 'Superseded'], ['none', 'No contribution']].map(([v, l]) => h('option', { value: v }, l)));
  let debounce;
  search.addEventListener('input', () => { clearTimeout(debounce); debounce = setTimeout(() => { state.q = search.value.trim(); state.cursor = null; state.stack = []; draw(); }, 250); });

  async function draw() {
    tableHost.setAttribute('aria-busy', 'true');
    let page;
    try {
      page = await ctx.api.tasks(pid, { limit: PAGE, cursor: state.cursor, status: state.status, q: state.q, review_state: state.review });
    } catch (error) {
      tableHost.replaceChildren(errorState(error, draw));
      return;
    } finally { tableHost.removeAttribute('aria-busy'); }
    const allRows = page.items;
    // Requirement records are marked by the server and, unless the filter asks for them,
    // left out of the task view (kittrial-5bb.277). The count is kept so the page can say
    // that rows were left out instead of reading as if the tracker held no work at all.
    const hiddenRequirements = allRows.filter((t) => t.requirement_record).length;
    const rows = state.requirements ? allRows : allRows.filter((t) => !t.requirement_record);
    const start = state.stack.length * PAGE;
    // The server says when some rows' review state is unknown (closed tasks with a
    // finished review, or rows past its bounded read); those show their status only.
    const partial = page.review_states_complete === false
      ? h('p', { class: 'small muted', role: 'note' }, 'Review state is not known for some tasks shown here (closed tasks, or more tasks than one read covers); they show their status only.')
      : null;
    const unread = (page.unreadable || []).length;
    const unreadable = unread
      ? h('p', { class: 'small', role: 'note' }, unread === 1 ? 'One task on the server cannot be read' : unread + ' tasks on the server cannot be read',
          ' (' + (page.unreadable || []).join(', ') + ')',
          '. Everything else is shown. Ask an operator of the server to repair ', unread === 1 ? 'it.' : 'them.',
          page.review_states_unavailable ? ' Until then review states are not shown here.' : '')
      : null;
    // Rows left out of the default view (kittrial-5bb.277): say how many and how to see
    // them, so an empty list is never mistaken for a project with no work. Said only for
    // the unfiltered view: a person who filtered for a state asked for that answer. The
    // count covers the rows the current page fetched, not the whole project.
    const leftOut = !state.requirements && hiddenRequirements && !state.q && !state.review
      ? h('p', { class: 'small muted', role: 'note' }, hiddenRequirements === 1
        ? '1 requirement record is not shown here. Turn on “Show requirements” to see it. The count covers the rows on this page.'
        : hiddenRequirements + ' requirement records are not shown here. Turn on “Show requirements” to see them. The count covers the rows on this page.')
      : null;
    tableHost.replaceChildren(
      ...(unreadable ? [unreadable] : []),
      leftOut,
      rows.length ? h('div', { class: 'table-wrap' }, h('table', null,
        h('caption', { class: 'visually-hidden' }, 'Tasks in ' + project.name),
        h('thead', null, h('tr', null, h('th', { scope: 'col' }, 'Task'), h('th', { scope: 'col' }, 'State'), h('th', { scope: 'col', class: 'hide-narrow' }, 'Assignee'), h('th', { scope: 'col', class: 'hide-narrow' }, 'Next action'), h('th', { scope: 'col', class: 'hide-narrow' }, 'Updated'))),
        h('tbody', null, rows.map((t) => h('tr', { class: 'row-link', 'data-requirement': t.requirement_record ? 'true' : null, onclick: (e) => { if (e.target.tagName !== 'A') ctx.go(`/p/${pid}/t/${t.id}`); } },
          h('td', null,
            // A requirement record is marked plainly and points at the Requirements page,
            // never offered as ordinary work (kittrial-5bb.277).
            t.requirement_record ? h('span', { class: 'chip', title: 'A requirement record, not claimable work' }, 'Requirement') : null,
            h('a', { class: 'title', href: ctx.href(`/p/${pid}/t/${t.id}`) }, t.title || t.id),
            h('div', { class: 'sub' }, t.priority != null ? [priority(t.priority), ' · '] : null, h('span', { class: 'mono' }, t.id),
              t.requirement_record ? [' · ', h('a', { href: ctx.href(`/p/${pid}/requirements`) }, 'Edit on Requirements')] : null)),
          // A row the server could not read is listed, and says so (kittrial-5bb.169).
          h('td', null, t.unreadable ? h('span', { class: 'chip crit' }, 'Cannot be read') : t.review_state && t.review_state !== 'none' ? reviewChip(t.review_state) : statusChip(t.status)),
          h('td', { class: 'hide-narrow' }, t.assignee_name || h('span', { class: 'muted' }, 'Unclaimed')),
          h('td', { class: 'hide-narrow' }, t.next_action ? t.next_action.text : h('span', { class: 'muted' }, '—')),
          h('td', { class: 'hide-narrow muted' }, t.updated_at ? time(t.updated_at) : '')))))) :
        empty(state.q || state.review ? 'No matching tasks' : 'No tasks yet', state.q || state.review ? 'Try a different search or filter.' : canWrite(project) && !project.archived ? 'Define the first task for this project.' : null),
      partial,
      h('div', { class: 'pager' },
        h('span', { class: 'num' }, page.total ? `${start + 1}–${start + rows.length} of ${page.total}` : ''),
        h('div', { class: 'actions' },
          h('button', { type: 'button', disabled: !state.stack.length, onclick: () => { state.cursor = state.stack.pop() || null; draw(); } }, 'Previous'),
          h('button', { type: 'button', disabled: !page.next_cursor, onclick: () => { state.stack.push(state.cursor); state.cursor = page.next_cursor; draw(); } }, 'Next'))));
  }
  draw();

  // "My contributions" strip: filled after the page is shown, so the task list never
  // waits for it; it stays empty on a server without the proposal routes.
  const strip = h('div');
  myStrip(ctx, pid).then((node) => { if (node) strip.replaceChildren(node); }).catch(() => {});
  // For owners, while setup steps are left: one line linking to them. Filled after the
  // page is shown, and silent when the server has no setup route.
  const setupLine = h('div');
  setupHint(ctx, project).then((node) => { if (node) setupLine.replaceChildren(node); }).catch(() => {});

  return h('div', { class: 'stack' },
    pageHead({ crumbs: crumbs(ctx, project), title: project.name, lede: project.description || null,
      actions: canWrite(project) && !project.archived ? h('a', { class: 'btn primary', href: ctx.href(`/p/${pid}/new`) }, 'New task') : null }),
    archivedBanner(project),
    unusableBanner(project),
    setupLine,
    mergeSlotPanel(ctx, project),
    guidancePanel(ctx, project),
    // The lane board (kittrial-5bb.263). Every member sees it: the route scopes a viewer
    // who is not an owner or coordinator to their own agents' lanes, so a lane always sees
    // its own row here, and the cross-lane view is never inherited from the transport.
    lanesPanel(ctx, project),
    strip,
    h('div', { class: 'toolbar' }, search, statusSeg, reviewSel, requirementToggle),
    tableHost);
}

export async function reviews(ctx, { pid }) {
  const project = await load(ctx, pid);
  // One proposal, or the propose form, replaces the lists on the same page
  // (/p/{pid}/reviews?proposal=<key> and ?propose=1): no separate route.
  const key = ctx.query.get('proposal');
  if (key || ctx.query.get('propose')) {
    const reviewsCrumb = [{ label: 'Projects', href: ctx.href('/projects') }, { label: project.name, href: ctx.href('/p/' + project.id) },
      { label: 'Reviews', href: ctx.href(`/p/${pid}/reviews`) }, { label: key ? 'Proposal' : 'Propose a requirement' }];
    return h('div', { class: 'stack' },
      pageHead({ crumbs: reviewsCrumb, title: key ? 'Requirement proposal' : 'Propose a requirement' }),
      archivedBanner(project),
      key ? await detailPanel(ctx, pid, key) : proposeForm(ctx, pid));
  }
  const [data, proposals] = await Promise.all([ctx.api.queue(pid), queuePanel(ctx, pid)]);
  // The disposable server calls an approved contribution "approved"; the canonical
  // review projection calls it "awaiting-integration". Both land in one group.
  const groups = [
    [['awaiting-review', 'legacy-review-ready'], 'Awaiting review', 'An owner needs to approve or request changes. These stay listed until someone acts, however old.'],
    [['changes-requested'], 'Changes requested', 'Waiting on the contributor to deliver a revision.'],
    [['approved', 'awaiting-integration'], 'Approved · not yet integrated', 'Accepted work that still needs integrating. Holds and dependencies are shown on each task.'],
    [['error'], 'Needs operator attention', 'The review record could not be read cleanly; an operator has to repair it.'],
  ];
  const openRequests = (t) => (t.open_requests ?? (t.requests || []).filter((r) => r.status === 'open').length);
  return h('div', { class: 'stack' },
    pageHead({ crumbs: crumbs(ctx, project, 'Reviews'), title: 'Reviews', lede: 'Requirement proposals and every contribution in flight, grouped by who has to act next.' }),
    proposals,
    data.complete === false ? h('div', { class: 'banner' }, 'This list is incomplete: the project has more contributions in flight than one read covers.') : null,
    groups.map(([keys, title, note]) => {
      const key = keys[0];
      const rows = data.items.filter((t) => keys.includes(t.review_state));
      if (key === 'error' && !rows.length) return null;
      return h('section', { class: 'panel', 'aria-labelledby': 'g-' + key },
        h('div', { class: 'panel-head' }, h('h2', { class: 'small', id: 'g-' + key }, title, ' ', h('span', { class: 'nav-count' }, rows.length)), h('span', { class: 'small muted hide-narrow' }, note)),
        rows.length ? h('div', { class: 'table-wrap' }, h('table', null,
          h('thead', null, h('tr', null, h('th', { scope: 'col' }, 'Task'), h('th', { scope: 'col' }, 'Contribution'), h('th', { scope: 'col', class: 'hide-narrow' }, 'Open requests'), h('th', { scope: 'col', class: 'hide-narrow' }, 'Waiting since'))),
          h('tbody', null, rows.map((t) => h('tr', { class: 'row-link', onclick: (e) => { if (e.target.tagName !== 'A') ctx.go(`/p/${pid}/t/${t.id}`); } },
            h('td', null, h('a', { class: 'title', href: ctx.href(`/p/${pid}/t/${t.id}`) }, t.title), h('div', { class: 'sub' }, t.assignee_name || 'Unassigned'),
              t.recommended ? h('span', { class: 'chip ok', title: 'A reviewer recommends approving this revision. An owner still decides.' }, 'Recommended') : null),
            h('td', null, t.contribution ? [t.contribution.revision ? h('span', null, 'Revision ', t.contribution.revision, ' · ') : null, h('code', null, shortSha(t.contribution.commit))] : '—'),
            h('td', { class: 'hide-narrow num' }, String(openRequests(t))),
            h('td', { class: 'hide-narrow muted' }, (t.contribution && t.contribution.at) || t.updated_at ? time((t.contribution && t.contribution.at) || t.updated_at) : '')))))) :
          empty('Nothing here', null));
    }));
}

// One sentence, said on the page and again before the form: whose feedback this is
// (kittrial-5bb.279). The project's own work goes into tasks, comments and requirements.
const FEEDBACK_LEDE = "Orchestra feedback is about Orchestra itself - the tools, the process and the coordination - not about this project's own work, which goes into tasks, comments and requirements.";

export async function feedback(ctx, { pid }) {
  const project = await load(ctx, pid);
  const list = h('div', { class: 'panel' });
  const adding = h('div');
  async function draw() {
    let data;
    try { data = await ctx.api.feedback(pid); } catch (error) {
      if (error && error.status === 501) {
        // A server that keeps no feedback answers 501 to reading and to sending it. That is
        // not a fault to try again: say that it is not there, and offer no form that can only fail.
        adding.replaceChildren();
        list.replaceChildren(h('div', { role: 'status' }, empty('Feedback is not available on this server',
          'This Orchestra server does not keep feedback yet, so none can be read or sent from this page. Until it does, write what you found on the task it concerns, or tell the people who run the project.')));
        return;
      }
      list.replaceChildren(errorState(error, draw)); return;
    }
    list.replaceChildren(data.items.length ? h('ul', { class: 'timeline panel-body', 'aria-label': 'Feedback' }, data.items.map((f) => h('li', null,
      h('span', { class: 'dot ' + (f.status === 'resolved' ? 'ok' : 'warn'), 'aria-hidden': 'true' }),
      h('div', null,
        h('div', { class: 'event-head' }, h('strong', null, f.author_name || f.actor || ''), time(f.at || f.created_at),
          f.status ? h('span', { class: 'chip ' + (f.status === 'open' ? 'warn' : 'ok') }, f.status === 'open' ? 'Open' : 'Resolved') : null),
        h('p', { class: 'event-body prose' }, f.text),
        f.triage ? h('p', { class: 'small muted' }, 'Triage: ', f.triage) : null)))) :
      empty('No feedback yet', 'Problems, ideas and friction reported by the team appear here.'));
  }
  const form = h('form', { class: 'form', novalidate: true },
    field({ id: 'fb-text', label: 'What happened, or what would help?', type: 'textarea', maxlength: 2000, hint: 'Up to 2,000 characters. Visible to project members.' }),
    h('div', null, h('button', { type: 'submit', class: 'primary' }, 'Submit feedback')));
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const text = formValues(form)['fb-text'].trim();
    if (!text) return setFieldError(form, 'fb-text', 'Write something first.');
    setFieldError(form, 'fb-text', '');
    const saved = await act(form.querySelector('button'), () => ctx.api.addFeedback(pid, { text }), { success: 'Feedback submitted' });
    if (saved) { form.reset(); draw(); }
  });
  if (canWrite(project) && !project.archived) adding.replaceChildren(h('section', { class: 'panel' }, h('div', { class: 'panel-head' }, h('h2', { class: 'small' }, 'Add feedback')), h('div', { class: 'panel-body' }, h('p', { class: 'small muted' }, FEEDBACK_LEDE), form)));
  draw();
  return h('div', { class: 'stack' },
    pageHead({ crumbs: crumbs(ctx, project, 'Orchestra feedback'), title: 'Orchestra feedback', lede: FEEDBACK_LEDE }),
    adding,
    list);
}

// The per-project claiming setting (kittrial-5bb.352). Owners and coordinators set it;
// every member sees the value and who set it, and the page explains what each value
// means. The server reads the value fresh from the project's own file, so the answer
// here is not a cached tracker read.
export function claimingPanel(ctx, project) {
  const host = h('section', { class: 'panel', 'data-panel': 'claiming' });
  const block = project.claiming || { claiming: 'open', set_by: null, set_at: null, set_via: null };
  const canSet = canApprove(project) && !project.archived && project.usable !== false;
  function draw(value) {
    const chosen = value || block.claiming || 'open';
    const warned = block.source === 'damaged' ? block.warning : null;
    const select = h('select', { id: 'claiming-value', 'aria-label': 'How work is handed out' },
      h('option', { value: 'open', selected: chosen === 'open' }, 'Workers may claim open tasks'),
      h('option', { value: 'assigned', selected: chosen === 'assigned' }, 'Only coordinators assign work'));
    const save = h('button', { type: 'button', class: 'primary', onclick: async (e) => {
      try {
        const result = await act(e.currentTarget, () => ctx.api.setProjectClaiming(project.id, select.value),
          { success: 'Claiming setting saved' });
        if (result && result.claiming) Object.assign(block, result);
        draw(select.value);
      } catch { /* the refusal is shown beside the control */ }
    } }, 'Save');
    host.replaceChildren(
      h('div', { class: 'panel-head' }, h('h2', { class: 'small' }, 'How work is handed out')),
      h('div', { class: 'panel-body' },
        h('p', { class: 'muted' }, chosen === 'assigned'
          ? 'Only this project’s coordinators assign work: unassigned tasks are not offered as claimable and a claim of one is refused. A task already assigned to you can still be started.'
          : 'Any member who may write tasks can claim an open, unassigned task. That is the default.'),
        canSet ? h('div', { class: 'toolbar' }, h('label', { class: 'check', for: 'claiming-value' }, 'Claiming'), select, save) : null,
        h('p', { class: 'small muted' },
          (chosen === 'assigned' ? 'Set to “only coordinators assign”' : 'Set to “workers may claim open tasks”'),
          block.set_by ? [' by ', h('span', { class: 'mono' }, block.set_by)] : null,
          block.set_at ? [' ', time(block.set_at)] : null,
          block.set_via ? [' (', block.set_via, ')'] : null,
          block.source === 'default' ? ' — the default; nobody has changed it.' : '.'),
        warned ? h('p', { class: 'small', role: 'status' }, warned) : null,
        h('p', { class: 'small muted' }, 'This setting is stored with the project and carried by its backup. An older kit that does not know it reads the project as open, so after a rollback to an older release claiming is open again until it is set here again.')));
  }
  draw();
  return host;
}

export async function settings(ctx, { pid }) {
  const project = await load(ctx, pid);
  const owner = isOwner(project);
  // Its own selector: the role options belong to the members list, never to another
  // panel's control (the claiming setting has data-panel="claiming" instead).
  const membersHost = h('div', { class: 'panel', 'data-panel': 'members' });
  const agentsHost = h('div');
  const credsHost = h('div');
  const auditHost = h('div');
  // Who coordinates (kittrial-5bb.198), read again after an owner changes a role or a grant here.
  const coordination = coordinationPanel(ctx, project);

  async function drawMembers() {
    let data;
    try { data = await ctx.api.members(pid); } catch (error) { membersHost.replaceChildren(errorState(error, drawMembers)); return; }
    const rows = data.items.slice().sort((a, b) => a.display_name.localeCompare(b.display_name));
    membersHost.replaceChildren(
      h('div', { class: 'panel-head' }, h('h2', { class: 'small' }, 'Members ', h('span', { class: 'nav-count' }, rows.length)), h('span', { class: 'small muted hide-narrow' }, 'Viewers read · contributors claim and deliver · coordinators also approve · owners also manage')),
      h('div', { class: 'table-wrap' }, h('table', null,
        h('thead', null, h('tr', null, h('th', { scope: 'col' }, 'Person'), h('th', { scope: 'col' }, 'Role'), owner ? h('th', { scope: 'col' }, h('span', { class: 'visually-hidden' }, 'Actions')) : null)),
        h('tbody', null, rows.map((m) => {
          const select = h('select', { 'aria-label': 'Role for ' + m.display_name, disabled: !owner || project.archived },
            ROLE_NAMES.map(([r, label]) => h('option', { value: r, selected: r === m.role }, label)));
          select.addEventListener('change', async () => {
            try { await act(select, () => ctx.api.setMember(pid, m.user_id, select.value), { success: `${m.display_name} is now ${select.value}` }); }
            catch { select.value = m.role; }
            drawMembers(); coordination.draw();
          });
          return h('tr', null,
            h('td', null, h('div', { class: 'title' }, m.display_name), h('div', { class: 'sub' }, '@' + m.username, m.agent_of_name ? ' · agent of ' + m.agent_of_name : '', m.disabled ? ' · account disabled' : '')),
            h('td', null, owner ? select : h('span', { class: 'role' }, m.role)),
            owner ? h('td', null, h('button', { type: 'button', class: 'danger', disabled: project.archived, onclick: async (e) => {
              const ok = await confirmDialog({ title: `Remove ${m.display_name}?`, body: 'They lose access to this project immediately, including any worker credentials they issued. Their past contributions stay in the record.', confirmLabel: 'Remove', danger: true });
              if (!ok) return;
              try { await act(e.currentTarget, () => ctx.api.removeMember(pid, m.user_id), { success: 'Removed' }); } catch { /* shown */ }
              drawMembers(); coordination.draw();
            } }, 'Remove')) : null);
        })))),
      // Not `null`: replaceChildren writes a null argument out as the word "null", which is what a member
      // who is not an owner saw under this table.
      ...(owner && !project.archived ? [addMemberForm()] : []));
  }

  function addMemberForm() {
    const form = h('form', { class: 'panel-body', novalidate: true },
      h('div', { class: 'form-row' },
        field({ id: 'm-username', label: 'Add by username', placeholder: 'e.g. lena', required: true }),
        field({ id: 'm-role', label: 'Role', type: 'select', value: 'contributor', options: ROLE_NAMES })),
      h('div', null, h('button', { type: 'submit', class: 'primary' }, 'Add member')));
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      const v = formValues(form);
      const username = v['m-username'].trim().replace(/^@/, '');
      if (!username) return setFieldError(form, 'm-username', 'Enter a username.');
      let account;
      try { account = await ctx.api.lookup(username, pid); } catch (error) {
        return setFieldError(form, 'm-username', error.status === 404 ? 'No active account has that username.' : describe(error));
      }
      setFieldError(form, 'm-username', '');
      try { await act(form.querySelector('button[type=submit]'), () => ctx.api.setMember(pid, account.id, v['m-role']), { success: `${account.display_name} added as ${v['m-role']}` }); } catch { return; }
      drawMembers(); coordination.draw();
    });
    return form;
  }

  // The agents that work in this project, and which of them coordinate it (kittrial-5bb.209). An owner
  // gives and removes the grant here; the server says why when a grant cannot be given.
  async function drawAgents() {
    if (!owner) { agentsHost.replaceChildren(); return; }
    let data;
    try { data = await ctx.api.projectAgents(pid); } catch (error) { agentsHost.replaceChildren(errorState(error, drawAgents)); return; }
    const rows = (data.items || []).slice().sort((a, b) => a.name.localeCompare(b.name));
    agentsHost.replaceChildren(h('section', { class: 'panel', 'data-panel': 'project-agents' },
      h('div', { class: 'panel-head' }, h('h2', { class: 'small' }, 'Agents in this project ', h('span', { class: 'nav-count' }, rows.length)),
        h('span', { class: 'small muted hide-narrow' }, 'A coordinating agent may approve other accounts’ work here; never its own account’s')),
      rows.length ? h('div', { class: 'table-wrap' }, h('table', null,
        h('thead', null, h('tr', null, h('th', { scope: 'col' }, 'Agent'), h('th', { scope: 'col' }, 'Account'), h('th', { scope: 'col' }, 'Coordinator'), h('th', { scope: 'col' }, 'Agent credentials'), h('th', { scope: 'col' }, h('span', { class: 'visually-hidden' }, 'Actions')))),
        h('tbody', null, rows.map((a) => {
          const grant = a.coordinator;
          const change = async (e, call, done) => {
            try { await act(e.currentTarget, call, { success: done }); } catch { /* the refusal is shown */ }
            drawAgents(); coordination.draw();
          };
          return h('tr', { 'data-agent': a.id, 'data-coordinator': grant ? String(grant.effective) : 'none' },
            h('td', null, a.name, a.enabled ? null : h('div', { class: 'sub' }, 'Disabled')),
            h('td', null, a.owner_display_name || a.owner),
            h('td', null, !grant ? h('span', { class: 'muted' }, 'No') :
              [h('span', { class: 'chip ' + (grant.effective ? 'ok' : 'plain') }, grant.effective ? 'Coordinates' : 'Granted, no effect'),
                h('div', { class: 'sub' }, 'by ', grant.granted_by_display_name || grant.granted_by, ' ', time(grant.granted_at),
                  grant.effective ? null : (NO_EFFECT[grant.no_effect_because] || NO_EFFECT['account-role']))]),
            h('td', null, (a.credentials || []).map((c) => h('div', { class: 'stack', 'data-credential': c.id },
              h('code', { class: 'small' }, c.id), expiryLine(c),
              !project.archived && a.enabled && c.working ? extensionButton(a, c,
                () => ctx.api.extendProjectAgentCredential(pid, a.id, c.id), drawAgents) : null))),
            h('td', null, project.archived ? null : grant ?
              h('button', { type: 'button', class: 'danger', 'aria-label': 'Remove the coordinator grant from ' + a.name,
                onclick: (e) => change(e, () => ctx.api.revokeCoordinator(pid, a.id), `${a.name} no longer coordinates`) }, 'Remove grant') :
              h('button', { type: 'button', 'aria-label': 'Let ' + a.name + ' coordinate this project',
                onclick: (e) => change(e, () => ctx.api.grantCoordinator(pid, a.id), `${a.name} coordinates this project`) }, 'Make coordinator')));
        })))) : h('div', { class: 'panel-body' }, empty('No agents here yet', 'A member grants their own agent this project on the Agents page.'))));
  }

  async function drawCreds() {
    if (!owner) { credsHost.replaceChildren(); return; }
    let data;
    try { data = await ctx.api.credentials(pid); } catch (error) { credsHost.replaceChildren(errorState(error, drawCreds)); return; }
    const issue = h('button', { type: 'button', disabled: project.archived, onclick: async (e) => {
      const result = await act(e.currentTarget, () => ctx.api.issueCredential(pid, ['tasks', 'checkpoints', 'reviews'], 'Worker credential')).catch(() => null);
      if (!result) return;
      secretDialog({ title: 'Worker credential issued', body: 'Give this to the agent or script that will work on this project. It is shown once and never again. It can do no more than you can, and stops working if you lose access.', secret: result.credential.secret });
      drawCreds();
    } }, 'Issue credential');
    credsHost.replaceChildren(h('section', { class: 'panel' },
      h('div', { class: 'panel-head' }, h('h2', { class: 'small' }, 'Worker credentials'), issue),
      data.items.length ? h('div', { class: 'table-wrap' }, h('table', null,
        h('thead', null, h('tr', null, h('th', { scope: 'col' }, 'Label'), h('th', { scope: 'col' }, 'Issued by'), h('th', { scope: 'col', class: 'hide-narrow' }, 'Scopes'), h('th', { scope: 'col', class: 'hide-narrow' }, 'Expires'), h('th', { scope: 'col' }, h('span', { class: 'visually-hidden' }, 'Actions')))),
        h('tbody', null, data.items.map((c) => h('tr', null,
          h('td', null, c.label, c.revoked ? h('div', { class: 'sub' }, 'Revoked') : null),
          h('td', null, c.user_name || c.user_id || ''),
          h('td', { class: 'hide-narrow mono small' }, c.scopes.join(', ')),
          h('td', { class: 'hide-narrow muted' }, time(c.expires_at)),
          h('td', null, c.revoked ? null : h('button', { type: 'button', class: 'danger', onclick: async (e) => {
            if (!(await confirmDialog({ title: 'Revoke this credential?', body: `“${c.label}” stops working immediately, including for requests already queued.`, confirmLabel: 'Revoke', danger: true }))) return;
            try { await act(e.currentTarget, () => ctx.api.revokeCredential(pid, c.id), { success: 'Credential revoked' }); } catch { /* shown */ }
            drawCreds();
          } }, 'Revoke'))))))) :
        empty('No worker credentials', 'Agents and scripts use a credential instead of a password. Issue one per worker.')));
  }

  async function drawAudit() {
    if (!owner) { auditHost.replaceChildren(); return; }
    let data;
    try { data = await ctx.api.audit(pid, { limit: 20 }); } catch (error) { auditHost.replaceChildren(errorState(error, drawAudit)); return; }
    auditHost.replaceChildren(h('section', { class: 'panel' },
      h('div', { class: 'panel-head' }, h('h2', { class: 'small' }, 'Recent activity (audit)')),
      data.items.length ? h('div', { class: 'table-wrap' }, h('table', null,
        h('thead', null, h('tr', null, h('th', { scope: 'col' }, 'When'), h('th', { scope: 'col' }, 'Who'), h('th', { scope: 'col' }, 'Action'), h('th', { scope: 'col', class: 'hide-narrow' }, 'Detail'))),
        h('tbody', null, data.items.map((a) => h('tr', null, h('td', { class: 'muted' }, time(a.time)), h('td', null, a.user_name || a.user_id || ''), h('td', { class: 'mono small' }, a.action, a.outcome && a.outcome !== 'committed' ? ' · ' + a.outcome : ''), h('td', { class: 'hide-narrow' }, a.detail || '')))))) :
        empty('No administrative activity yet', null)));
  }

  drawMembers(); drawAgents(); drawCreds(); drawAudit();

  const danger = owner && !project.archived ? h('section', { class: 'panel' },
    h('div', { class: 'panel-head' }, h('h2', { class: 'small' }, 'Archive project')),
    h('div', { class: 'panel-body' },
      h('p', { class: 'muted' }, 'Archiving stops new work and hides the project from active lists. Nothing is deleted: tasks, reviews and history stay readable to members.'),
      h('div', null, h('button', { type: 'button', class: 'danger', onclick: async (e) => {
        if (!(await confirmDialog({ title: `Archive “${project.name}”?`, body: 'Members keep read access to all records. New tasks, claims and contributions will be refused.', confirmLabel: 'Archive project', danger: true }))) return;
        try { await act(e.currentTarget, () => ctx.api.archiveProject(pid), { success: 'Project archived' }); } catch { return; }
        await ctx.refreshProjects(); ctx.render();
      } }, 'Archive project')))) : null;

  return h('div', { class: 'stack' },
    pageHead({ crumbs: crumbs(ctx, project, 'Members & settings'), title: 'Members & settings', lede: owner ? 'Manage who can see and change this project.' : 'Only project owners can change membership.' }),
    archivedBanner(project),
    unusableBanner(project),
    membersHost, claimingPanel(ctx, project), coordination.node, agentsHost, credsHost, auditHost, danger);
}
