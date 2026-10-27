// The trace read on the web (kittrial-5bb.425 part 2; TRACEABILITY_DESIGN §5.2): the Coverage panel on the
// project page, the Designs list under Records, and one page per view. Every row is the server's data,
// built with h() as text: nothing in a title, a token or a comment can become markup, a link the server did
// not name, or a script. A count a bound cut short says so ("partial"), as the server reports it.
import { h } from '../dom.js';
import { pageHead, errorState, describe } from '../ui.js';

export const VIEWS = ['summary', 'requirements', 'tasks', 'tokens', 'decisions', 'designs', 'round'];
const NAMES = { requirements: 'Requirements', tasks: 'Tasks', tokens: 'Tokens', decisions: 'Decisions',
  designs: 'Designs', round: 'Design rounds' };

const href = (ctx, path) => (ctx && typeof ctx.href === 'function' ? ctx.href(path) : '#' + path);
const text = (value) => (value && typeof value === 'object' ? String(value.text || '') : String(value == null ? '' : value));
const flags = (row) => (Array.isArray(row.flags) && row.flags.length ? h('span', { class: 'small', 'data-flags': row.flags.join(' ') }, ' — ', row.flags.join(', ')) : null);

// One sentence per summary line: the total, each count, and "partial" when a bound cut the count short.
export function summaryLine(line) {
  const counts = Object.entries(line.counts || {}).map(([flag, count]) => `${count} ${flag}`);
  return `${line.total} ${(NAMES[line.view] || line.view).toLowerCase()}` + (counts.length ? ': ' + counts.join(', ') : '')
    + (line.partial ? ' (partial)' : '');
}

// The Coverage panel: one line per view, each opening that view. Silent on a server without the route.
export function coveragePanel(ctx, pid) {
  const body = h('div', { class: 'small muted' }, 'Reading coverage…');
  const panel = h('section', { class: 'panel', 'aria-labelledby': 'coverage-heading', 'data-coverage-panel': '1' },
    h('div', { class: 'panel-head' }, h('h2', { class: 'small', id: 'coverage-heading' }, 'Coverage')), body);
  Promise.resolve().then(() => ctx.api.trace(pid, { view: 'summary' })).then((answer) => {
    // replaceChildren() writes a null as the text "null" (h() skips it), so only real nodes are passed.
    body.replaceChildren(...[
      answer.note ? h('p', { class: 'small muted' }, answer.note) : null,
      h('ul', { class: 'coverage-lines' }, (answer.rows || []).map((line) => h('li', { 'data-coverage-view': line.view },
        h('a', { href: href(ctx, `/p/${pid}/trace/${line.view}`) }, summaryLine(line))))),
      h('p', { class: 'small muted', 'data-coverage-text': '1' }, text((answer.coverage || {}).text))].filter(Boolean));
  }).catch((error) => {
    if (error && error.status === 404) panel.hidden = true;
    else body.replaceChildren(h('p', { class: 'small muted' }, 'Coverage could not be read: ', describe(error)));
  });
  return panel;
}

// The Designs list under Records: one row per design.<scope>, its state and coverage, each opening its page.
export function designsPanel(ctx, pid) {
  const body = h('div', { class: 'small muted' }, 'Reading designs…');
  const panel = h('section', { class: 'panel', 'data-designs-list': '1' },
    h('div', { class: 'panel-head' }, h('h2', null, 'Designs')), body);
  Promise.resolve().then(() => ctx.api.trace(pid, { view: 'designs' })).then((answer) => {
    const rows = answer.rows || [];
    body.replaceChildren(rows.length
      ? h('ul', null, rows.map((row) => h('li', { 'data-design-row': row.scope },
        h('a', { href: href(ctx, `/p/${pid}/designs/${encodeURIComponent(row.scope)}`) }, 'design.' + row.scope),
        ' — ', text(row.title), ' — ', row.selected ? (row.state || 'selected') : 'not selected',
        ` — requirements ${(row.requirements || {}).cited || 0} cited, ${(row.requirements || {}).planned || 0} in a plan phase`,
        flags(row))))
      : h('p', { class: 'small muted' }, answer.note || 'No designs in this project.'));
  }).catch((error) => {
    if (error && error.status === 404) panel.hidden = true;
    else body.replaceChildren(h('p', { class: 'small muted' }, 'Designs could not be read: ', describe(error)));
  });
  return panel;
}

// One row of any view, as text. Links only to pages of this project the row names by id.
export function rowLine(ctx, pid, view, row) {
  if (row.oversized) return h('li', { 'data-oversized': '1' }, row.note);
  const task = (id) => h('a', { href: href(ctx, `/p/${pid}/t/${encodeURIComponent(id)}`) }, id);
  if (view === 'requirements') {
    return h('li', null, h('code', null, row.key), ' — ', text(row.title),
      ` — ${(row.tasks || []).length + (row.tasks_omitted || 0)} tasks, ${(row.designs || []).length + (row.designs_omitted || 0)} designs, `
      + `${(row.capabilities || []).length + (row.capabilities_omitted || 0)} capabilities`, flags(row));
  }
  if (view === 'tasks') {
    return h('li', null, task(row.id), ' — ', text(row.title), ' — ', String(row.status),
      (row.cites || []).length ? h('div', { class: 'small' }, 'Cites: ', (row.cites || []).map((c) => `${c.pointer} (${[c.state].concat(c.flags || []).join(', ')})`).join('; ')) : null,
      row.tokens_not_checked ? h('div', { class: 'small muted' }, `${row.tokens_not_checked} more tokens not checked`) : null,
      flags(row));
  }
  if (view === 'tokens') {
    const where = row.where || {};
    return h('li', null, row.pointer === null ? h('span', { class: 'muted' }, `${row.omitted} tokens not checked`) : h('code', null, row.pointer),
      ' — in ', where.task ? [task(where.task), where.field ? ' (' + where.field + ')' : null]
        : ['design.' + where.design, where.section ? ' #' + where.section : null], flags(row));
  }
  if (view === 'decisions') {
    return row.kind === 'decision'
      ? h('li', null, task(row.id), ' — ', text(row.title),
        (row.supersedes || []).length ? ' — supersedes ' + row.supersedes.join(', ') : null,
        (row.designs || []).length ? ' — cited by design.' + row.designs.join(', design.') : null, flags(row))
      : h('li', null, 'Possible decision (a heuristic) on ', task(row.task), ': ', text(row.text), flags(row));
  }
  if (view === 'designs') {
    return h('li', null, h('a', { href: href(ctx, `/p/${pid}/designs/${encodeURIComponent(row.scope)}`) }, 'design.' + row.scope),
      ' — ', text(row.title), ' — ', row.selected ? (row.state || 'selected') : 'not selected', flags(row));
  }
  if (view === 'round') {
    return h('li', null, task(row.epic), ' — ', text(row.title), ' — design-round:', row.scope, ' — ', row.published_at ? 'published ' + row.published_at : row.publication,
      h('ul', null, (row.lanes || []).map((lane) => h('li', null, task(lane.task), ' — ', text(lane.title),
        ` — plan ${lane.plan_at || 'none'}, delivered ${lane.delivered_at || 'not yet'}, ${(lane.cross_reviews || []).length + (lane.cross_reviews_omitted || 0)} cross-reviews`,
        flags(lane))),
      row.lanes_omitted ? h('li', { class: 'muted' }, `${row.lanes_omitted} more lanes`) : null), flags(row));
  }
  return h('li', null, summaryLine(row));
}

// A view's page: its rows, its coverage line, and "More" while the server has a next page.
export async function page(ctx, { pid, view }) {
  const title = NAMES[view] || 'Coverage';
  const crumbs = [{ label: 'Project', href: href(ctx, `/p/${pid}`) }];
  if (!VIEWS.includes(view)) return h('div', null, pageHead({ title, crumbs }), h('p', null, 'There is no such trace view.'));
  let answer;
  try {
    answer = await ctx.api.trace(pid, { view });
  } catch (error) {
    return h('div', null, pageHead({ title, crumbs }), errorState(error, () => ctx.render()));
  }
  const list = h('ul', { class: 'trace-rows', 'data-trace-view': view });
  const more = h('button', { type: 'button' }, 'More');
  let cursor = answer.next_cursor;
  const show = (rows) => list.append(...rows.map((row) => rowLine(ctx, pid, view, row)));
  show(answer.rows || []);
  more.hidden = !cursor;
  more.addEventListener('click', async () => {
    try {
      const next = await ctx.api.trace(pid, { view, cursor });
      show(next.rows || []);
      cursor = next.next_cursor;
      more.hidden = !cursor;
    } catch (error) {
      more.replaceWith(h('p', { class: 'small muted' }, describe(error)));
    }
  });
  return h('div', { class: 'stack' },
    pageHead({ title, lede: `${answer.total} rows`, crumbs }),
    answer.note ? h('p', { class: 'small muted' }, answer.note) : null,
    h('section', { class: 'panel' }, list, more),
    h('p', { class: 'small muted' }, text((answer.coverage || {}).text), ' (untrusted data)'));
}
