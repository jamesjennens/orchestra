// Project Waiting page (/p/{pid}/waiting), lazy Wait times panel (/p/{pid}),
// and My work "Waiting on you" panel (kittrial-5bb.598 Slice 2, §§2.2, 2.3, 6).
//
// Everything shown here comes straight from the server's canonical waits.py read:
// ages use server-supplied whole seconds (ageText), never recomputed from the browser clock,
// and untrusted text is always placed via textContent (h()).
import { h, time } from '../dom.js';
import { pageHead, empty, loading, errorState, describe } from '../ui.js';

export const SILENT_HOURS_DEFAULT = 4;
export const SILENT_HOURS_MIN = 1;
export const SILENT_HOURS_MAX = 168;
export const METRICS_DAYS_DEFAULT = 14;
export const METRICS_DAYS_CHOICES = [7, 14, 30, 90];

// Whole server seconds as a short span (matches project.js ageText without circular import).
export function ageText(seconds) {
  if (seconds === null || seconds === undefined) return '—';
  if (seconds < 60) return 'under a minute';
  if (seconds < 3600) return Math.floor(seconds / 60) + ' min';
  if (seconds < 86400) return Math.floor(seconds / 3600) + ' h';
  const days = Math.floor(seconds / 86400);
  return days + (days === 1 ? ' day' : ' days');
}

export function stageChips(row) {
  const chips = [];
  const stage = row.escalation_stage || 'none';
  if (stage === 'project-visible') {
    chips.push(h('span', { class: 'chip crit', 'data-stage': 'project-visible' }, '+24h Project-wide'));
  } else if (stage === 'owner-not-reached') {
    chips.push(h('span', { class: 'chip crit', 'data-stage': 'owner-not-reached' }, '+4h Owner NOT reached'));
  } else if (stage === 'owner-notified') {
    chips.push(h('span', { class: 'chip warn', 'data-stage': 'owner-notified' }, '+4h Owner told'));
  } else if (row.overdue || stage === 'overdue') {
    chips.push(h('span', { class: 'chip warn', 'data-stage': 'overdue' }, 'Overdue'));
  }
  if (row.kind === 'silent-lane' || row.silent) {
    chips.push(h('span', { class: 'chip warn', 'data-flag': 'silent' }, 'Silent'));
  }
  if (row.role_has_no_members) {
    chips.push(h('span', { class: 'chip crit', 'data-flag': 'role-empty' }, 'No role members'));
  }
  if (row.target_churn) {
    chips.push(h('span', { class: 'chip warn', 'data-flag': 'target-churn' }, 'Target churn'));
  }
  if (row.snoozed && row.snooze) {
    chips.push(h('span', { class: 'chip', 'data-flag': 'snoozed' }, 'Snoozed'));
  }
  return chips;
}

function snoozeNote(row) {
  if (!row.snoozed || !row.snooze) return null;
  const sn = row.snooze;
  const who = sn.actor_display || sn.actor || 'a coordinator';
  const count = row.snooze_count || 1;
  const selfWarn = (sn.waited_on_party || sn.suppresses_owner_notice === false)
    ? h('div', { class: 'small', 'data-snooze-anti-suppress': 'true' },
      'Waited-on party snooze — +4h owner notice still fires.')
    : null;
  return h('div', { class: 'small muted', 'data-snooze-detail': row.episode_id || '' },
    'Snoozed until ', sn.until ? time(sn.until) : '—',
    ' by ', who, ` (${count}/2)`,
    sn.reason ? [' · ', sn.reason] : null,
    selfWarn);
}

function snoozeForm(ctx, pid, row, onSaved) {
  const untilInput = h('input', {
    type: 'text', name: 'until', placeholder: 'YYYY-MM-DDTHH:MM:SSZ (required, <= 7 days)',
    'aria-label': 'Snooze until (ISO-8601 with offset)',
  });
  const reasonInput = h('input', {
    type: 'text', name: 'reason', maxlength: 300, placeholder: 'Why this wait is snoozed (required)',
    'aria-label': 'Snooze reason',
  });
  const errBox = h('div', { class: 'small crit', role: 'alert', 'data-snooze-error': 'true' });
  const submit = h('button', { type: 'submit', class: 'btn' }, 'Save snooze');

  const form = h('form', {
    class: 'stack', 'data-snooze-form': row.episode_id || '',
    onsubmit: async (e) => {
      e.preventDefault();
      errBox.textContent = '';
      submit.disabled = true;
      try {
        await ctx.api.snoozeWait(pid, {
          task: row.task,
          episode_id: row.episode_id,
          until: untilInput.value.trim(),
          reason: reasonInput.value.trim(),
        });
        untilInput.value = '';
        reasonInput.value = '';
        if (onSaved) onSaved();
      } catch (error) {
        errBox.textContent = describe(error);
      } finally {
        submit.disabled = false;
      }
    },
  },
    h('div', { class: 'toolbar' }, untilInput, reasonInput, submit),
    errBox);

  return h('details', { class: 'fold', 'data-snooze-box': row.episode_id || '' },
    h('summary', null, 'Snooze'),
    h('div', { class: 'fold-body' },
      h('p', { class: 'small muted' },
        'Transparent and end-timed (max 7 days, max 2 renewals). Never hides this wait; a snooze by the waited-on party still alerts the owner at +4h.'),
      form));
}

function waitRowCard(ctx, pid, row, { canSnooze = false, onSnoozed = null, showProject = false } = {}) {
  const targetPid = row.project || pid;
  const href = ctx.href(`/p/${targetPid}/t/${row.task}`);
  const laneLabel = row.display_name || row.lane || row.actor || 'unknown lane';
  const waitingOn = row.waiting_on_name || row.waiting_on || null;
  const chips = stageChips(row);
  const metaBits = [
    laneLabel,
    waitingOn ? ` → ${waitingOn}` : null,
    ' · waiting ', ageText(row.age_seconds),
  ].filter(Boolean);
  if (row.expect_by) {
    metaBits.push(' · expect by ', time(row.expect_by));
    if (row.expect_by_defaulted) metaBits.push(' (defaulted)');
    if (row.expect_by_clamped) metaBits.push(' (clamped 72h)');
    if (row.expect_by_before_checkpoint) metaBits.push(' (before checkpoint)');
  }
  if (showProject && (row.project_name || row.project)) {
    metaBits.unshift(`${row.project_name || row.project} · `);
  }
  return h('article', {
    class: 'panel-body stack', 'data-wait-row': row.episode_id || row.task || '',
    'data-task': row.task || '', 'data-kind': row.kind || 'wait',
  },
    h('div', { class: 'toolbar' },
      h('a', { class: 'title', href }, row.title || row.task),
      h('span', { class: 'mono small muted' }, row.task),
      chips.length ? h('span', { class: 'chips' }, ...chips) : null),
    h('div', { class: 'small muted' }, ...metaBits),
    snoozeNote(row),
    canSnooze ? snoozeForm(ctx, targetPid, row, onSnoozed) : null);
}

function metricsTable(data) {
  const byTarget = data.by_target || [];
  const byActor = data.by_actor || [];
  const targetBlock = byTarget.length
    ? h('div', { class: 'table-wrap' }, h('table', { 'data-table': 'metrics-by-target' },
      h('thead', null, h('tr', null,
        h('th', { scope: 'col' }, 'Day (UTC)'),
        h('th', { scope: 'col' }, 'Waiting on'),
        h('th', { scope: 'col', class: 'num' }, 'Answered'),
        h('th', { scope: 'col', class: 'num' }, 'Median'),
        h('th', { scope: 'col', class: 'num' }, 'Max'),
        h('th', { scope: 'col', class: 'num' }, 'Closed unans.'),
        h('th', { scope: 'col', class: 'num' }, 'Superseded'),
        h('th', { scope: 'col', class: 'num' }, 'Open now'))),
      h('tbody', null, byTarget.map((r) => {
        const members = (r.resolved_members || []).map((m) => m.name || m.id).filter(Boolean);
        return h('tr', null,
          h('td', { class: 'mono' }, r.day || ''),
          h('td', null, r.waiting_on_name || r.waiting_on || '—',
            members.length ? h('div', { class: 'sub' }, members.join(', ')) : null),
          h('td', { class: 'num' }, String((r.answered && r.answered.count) || 0)),
          h('td', { class: 'num' }, ageText(r.answered && r.answered.median_seconds)),
          h('td', { class: 'num' }, ageText(r.answered && r.answered.max_seconds)),
          h('td', { class: 'num' }, String((r.closed_unanswered && r.closed_unanswered.count) || 0)),
          h('td', { class: 'num' }, String((r.superseded && r.superseded.count) || 0)),
          h('td', { class: 'num' },
            r.open_now && r.open_now.open_count
              ? `${r.open_now.open_count} (max ${ageText(r.open_now.open_max_seconds)})`
              : '0'));
      }))))
    : empty('No wait history in this window', 'Answered, closed, superseded, and open waits appear here once recorded.');

  const actorBlock = byActor.length
    ? h('div', { class: 'table-wrap' }, h('table', { 'data-table': 'metrics-by-actor' },
      h('thead', null, h('tr', null,
        h('th', { scope: 'col' }, 'Day (UTC)'),
        h('th', { scope: 'col' }, 'Answered by'),
        h('th', { scope: 'col', class: 'num' }, 'Answered'),
        h('th', { scope: 'col', class: 'num' }, 'Median'),
        h('th', { scope: 'col', class: 'num' }, 'Max'))),
      h('tbody', null, byActor.map((r) => h('tr', null,
        h('td', { class: 'mono' }, r.day || ''),
        h('td', null, r.actor_name || r.actor || '—'),
        h('td', { class: 'num' }, String((r.answered && r.answered.count) || 0)),
        h('td', { class: 'num' }, ageText(r.answered && r.answered.median_seconds)),
        h('td', { class: 'num' }, ageText(r.answered && r.answered.max_seconds)))))))
    : h('p', { class: 'small muted' }, 'No answered waits by actor in this window.');

  return h('div', { class: 'stack' },
    h('h3', { class: 'small' }, 'By target'),
    targetBlock,
    h('h3', { class: 'small' }, 'By answering actor'),
    actorBlock);
}

export async function waitingPage(ctx, { pid }) {
  let project;
  try {
    project = await ctx.api.project(pid);
  } catch {
    project = (ctx.projects || []).find((p) => p.id === pid) || { id: pid, name: pid, usable: true };
  }
  const crumbs = [
    { label: 'Projects', href: ctx.href('/projects') },
    { label: project.name || pid, href: ctx.href(`/p/${pid}`) },
    { label: 'Waiting' },
  ];
  if (project.usable === false) {
    return h('div', { class: 'stack' },
      pageHead({ crumbs, title: 'Waiting' }),
      h('div', { class: 'banner' }, 'This project is not usable right now.'));
  }

  const state = {
    tab: 'open',
    filter: 'all',
    silent_hours: SILENT_HOURS_DEFAULT,
    cursor: null,
    stack: [],
    days: METRICS_DAYS_DEFAULT,
  };

  const openPanel = h('section', { class: 'panel', 'data-panel': 'waiting', 'aria-label': 'Open waits' });
  const metricsPanel = h('section', { class: 'panel', 'data-panel': 'waiting-metrics', 'aria-label': 'Wait metrics', hidden: true });
  const bodyHost = h('div', { class: 'stack' }, openPanel);

  let openReads = 0;
  async function drawOpen() {
    const seq = ++openReads;
    openPanel.setAttribute('aria-busy', 'true');
    openPanel.replaceChildren(loading('Reading open waits…'));
    const params = { filter: state.filter, silent_hours: state.silent_hours };
    if (state.cursor) params.cursor = state.cursor;
    let data;
    try {
      data = await ctx.api.waiting(pid, params);
    } catch (error) {
      if (seq !== openReads) return;
      openPanel.removeAttribute('aria-busy');
      openPanel.replaceChildren(errorState(error, drawOpen));
      return;
    }
    if (seq !== openReads) return;
    openPanel.removeAttribute('aria-busy');

    const items = data.items || [];
    const silent = data.silent || [];
    const canSnooze = Boolean(data.can_snooze);

    const filterSeg = h('div', { class: 'seg', role: 'group', 'aria-label': 'Wait filter' },
      [['all', 'All waits'], ['on-me', 'Waiting on me']].map(([val, label]) =>
        h('button', {
          type: 'button', 'aria-pressed': String(state.filter === val),
          onclick: () => {
            if (state.filter === val) return;
            state.filter = val;
            state.cursor = null;
            state.stack = [];
            drawOpen();
          },
        }, label)));

    const silentInput = h('input', {
      type: 'number', id: 'waiting-silent-hours', min: SILENT_HOURS_MIN, max: SILENT_HOURS_MAX,
      value: String(state.silent_hours), 'aria-label': 'Silent hours threshold (1 to 168)',
    });
    const silentMsg = h('span', { class: 'small muted', role: 'status' });
    const silentApply = h('button', {
      type: 'button',
      onclick: () => {
        const chosen = Number(silentInput.value);
        if (!Number.isInteger(chosen) || chosen < SILENT_HOURS_MIN || chosen > SILENT_HOURS_MAX) {
          silentMsg.textContent = `Silent hours must be a whole number from ${SILENT_HOURS_MIN} to ${SILENT_HOURS_MAX}.`;
          return;
        }
        silentMsg.textContent = '';
        state.silent_hours = chosen;
        state.cursor = null;
        state.stack = [];
        drawOpen();
      },
    }, 'Apply');

    const summaryChips = h('div', { class: 'chips', 'data-waiting-summary': 'true' },
      h('span', { class: 'chip' }, `${data.total || 0} open`),
      data.overdue_count ? h('span', { class: 'chip warn' }, `${data.overdue_count} overdue`) : null,
      data.unreached_count ? h('span', { class: 'chip crit' }, `${data.unreached_count} owner NOT reached`) : null,
      data.silent_count ? h('span', { class: 'chip warn' }, `${data.silent_count} silent`) : null,
      data.snoozed_count ? h('span', { class: 'chip' }, `${data.snoozed_count} snoozed`) : null);

    const waitsList = items.length
      ? h('div', { class: 'stack', 'data-section': 'open-waits' },
        items.map((row) => waitRowCard(ctx, pid, row, { canSnooze, onSnoozed: drawOpen })))
      : empty('No open waits', state.filter === 'on-me'
        ? 'No lane is currently waiting on you in this project.'
        : 'No lane has an open wait right now.');

    const silentSection = silent.length
      ? h('div', { class: 'stack', 'data-section': 'silent-lanes' },
        h('div', { class: 'panel-head' },
          h('h3', { class: 'small' }, 'Silent lanes ', h('span', { class: 'nav-count' }, String(silent.length))),
          h('span', { class: 'small muted' }, `Held tasks with no holder activity for >= ${data.silent_hours || state.silent_hours} h`)),
        silent.map((row) => waitRowCard(ctx, pid, row, { canSnooze, onSnoozed: drawOpen })))
      : null;

    const pager = (state.stack.length || data.next_cursor)
      ? h('div', { class: 'pager' },
        h('span', { class: 'num' }, `${items.length} of ${data.total || items.length}`),
        h('div', { class: 'actions' },
          h('button', {
            type: 'button', disabled: !state.stack.length,
            onclick: () => { state.cursor = state.stack.pop() || null; drawOpen(); },
          }, 'Previous'),
          h('button', {
            type: 'button', disabled: !data.next_cursor,
            onclick: () => { state.stack.push(state.cursor); state.cursor = data.next_cursor; drawOpen(); },
          }, 'Next')))
      : null;

    openPanel.replaceChildren(
      h('div', { class: 'panel-head' },
        h('h2', { class: 'small' }, 'Open waits'),
        filterSeg),
      h('div', { class: 'panel-body stack' },
        summaryChips,
        h('details', { class: 'fold' },
          h('summary', null, 'Silent threshold ', h('span', { class: 'fold-hint' }, `${state.silent_hours} h`)),
          h('div', { class: 'fold-body toolbar' }, silentInput, silentApply, silentMsg))),
      waitsList,
      silentSection,
      pager);
  }

  let metricsReads = 0;
  async function drawMetrics() {
    const seq = ++metricsReads;
    metricsPanel.setAttribute('aria-busy', 'true');
    metricsPanel.replaceChildren(loading('Computing wait metrics…'));
    let data;
    try {
      data = await ctx.api.waitingMetrics(pid, { days: state.days });
    } catch (error) {
      if (seq !== metricsReads) return;
      metricsPanel.removeAttribute('aria-busy');
      metricsPanel.replaceChildren(errorState(error, drawMetrics));
      return;
    }
    if (seq !== metricsReads) return;
    metricsPanel.removeAttribute('aria-busy');

    const daySeg = h('div', { class: 'seg', role: 'group', 'aria-label': 'Days' },
      METRICS_DAYS_CHOICES.map((d) => h('button', {
        type: 'button', 'aria-pressed': String(state.days === d),
        onclick: () => { if (state.days !== d) { state.days = d; drawMetrics(); } },
      }, `${d}d`)));

    metricsPanel.replaceChildren(
      h('div', { class: 'panel-head' },
        h('h2', { class: 'small' }, `Wait metrics (last ${data.days || state.days} days)`),
        daySeg),
      h('div', { class: 'panel-body stack' }, metricsTable(data)));
  }

  const openTabBtn = h('button', {
    type: 'button', 'data-tab': 'open', 'aria-pressed': 'true',
    onclick: () => {
      state.tab = 'open';
      openTabBtn.setAttribute('aria-pressed', 'true');
      metricsTabBtn.setAttribute('aria-pressed', 'false');
      openPanel.hidden = false;
      metricsPanel.hidden = true;
      bodyHost.replaceChildren(openPanel);
    },
  }, 'Open waits');
  const metricsTabBtn = h('button', {
    type: 'button', 'data-tab': 'metrics', 'aria-pressed': 'false',
    onclick: () => {
      state.tab = 'metrics';
      openTabBtn.setAttribute('aria-pressed', 'false');
      metricsTabBtn.setAttribute('aria-pressed', 'true');
      openPanel.hidden = true;
      metricsPanel.hidden = false;
      bodyHost.replaceChildren(metricsPanel);
      drawMetrics();
    },
  }, 'Metrics');

  // Only open waits are fetched on default page load; metrics stay lazy (SHOULD-9).
  drawOpen();

  return h('div', { class: 'stack' },
    pageHead({
      crumbs,
      title: 'Waiting',
      lede: 'Oldest-first open waits, silent lanes, and escalation status — all from the server.',
    }),
    h('div', { class: 'seg', role: 'group', 'aria-label': 'Waiting view' }, openTabBtn, metricsTabBtn),
    bodyHost);
}

// Compact lazy-loaded Wait times panel for /p/{pid} (Section 6, SHOULD-9).
// Fetches /waiting/metrics?days=14 after the page renders; stays quiet on 404/unsupported servers.
export function waitTimesPanel(ctx, pid) {
  const host = h('section', { class: 'panel', 'data-panel': 'wait-times', 'aria-label': 'Wait times' });
  if (!ctx || !ctx.api || typeof ctx.api.waitingMetrics !== 'function') return host;
  Promise.resolve().then(() => ctx.api.waitingMetrics(pid, { days: METRICS_DAYS_DEFAULT })).then((data) => {
    if (!data || typeof data !== 'object') return;
    const byTarget = data.by_target || [];
    let answered = 0;
    let maxAnswered = null;
    let openNow = 0;
    let maxOpen = null;
    const medians = [];
    for (const row of byTarget) {
      const a = row.answered || {};
      answered += Number(a.count || 0);
      if (a.max_seconds != null) maxAnswered = maxAnswered == null ? a.max_seconds : Math.max(maxAnswered, a.max_seconds);
      if (a.median_seconds != null && a.count) medians.push(a.median_seconds);
      const o = row.open_now || {};
      openNow += Number(o.open_count || 0);
      if (o.open_max_seconds != null) maxOpen = maxOpen == null ? o.open_max_seconds : Math.max(maxOpen, o.open_max_seconds);
    }
    medians.sort((x, y) => x - y);
    const approxMedian = medians.length ? medians[Math.floor((medians.length - 1) / 2)] : null;
    host.replaceChildren(
      h('div', { class: 'panel-head' },
        h('h2', { class: 'small' }, 'Wait times (14 days)'),
        h('a', { class: 'small', href: ctx.href(`/p/${pid}/waiting`) }, 'Open Waiting')),
      h('div', { class: 'panel-body chips' },
        h('span', { class: 'chip' }, `${answered} answered`),
        approxMedian != null ? h('span', { class: 'chip plain' }, `median ${ageText(approxMedian)}`) : null,
        maxAnswered != null ? h('span', { class: 'chip plain' }, `max ${ageText(maxAnswered)}`) : null,
        openNow ? h('span', { class: 'chip warn' }, `${openNow} open (max ${ageText(maxOpen)})`) : h('span', { class: 'chip ok' }, '0 open')));
  }).catch(() => { /* optional summary panel: keep quiet if unavailable */ });
  return host;
}

// Dedicated "Waiting on you" panel for My work (Section 2.2).
export function waitingOnYouPanel(ctx, waiting) {
  if (!waiting || typeof waiting !== 'object') return null;
  const items = waiting.items || [];
  const silent = waiting.silent || [];
  const total = Number(waiting.total || 0);
  const silentCount = Number(waiting.silent_lanes_count || silent.length || 0);
  if (!total && !silentCount && !items.length && !silent.length) return null;
  const chips = [
    h('span', { class: 'nav-count' }, String(total + silentCount)),
    waiting.overdue_count ? h('span', { class: 'chip warn' }, `${waiting.overdue_count} overdue`) : null,
    waiting.unreached_count ? h('span', { class: 'chip crit' }, `${waiting.unreached_count} owner NOT reached`) : null,
    silentCount ? h('span', { class: 'chip warn' }, `${silentCount} silent`) : null,
    waiting.snoozed_count ? h('span', { class: 'chip' }, `${waiting.snoozed_count} snoozed`) : null,
  ].filter(Boolean);
  return h('section', { class: 'panel', 'data-panel': 'waiting-on-you', 'aria-labelledby': 'h-waiting-on-you' },
    h('div', { class: 'panel-head' },
      h('h2', { id: 'h-waiting-on-you', class: 'small' }, 'Waiting on you ', ...chips),
      h('span', { class: 'small muted' }, 'Oldest first across your projects')),
    items.map((row) => waitRowCard(ctx, row.project, row, { canSnooze: false, showProject: true })),
    silent.map((row) => waitRowCard(ctx, row.project, row, { canSnooze: false, showProject: true })));
}
