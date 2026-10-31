// The Program page, the program manager's dashboard (kittrial-5bb.453; PROGRAM_DESIGN.md sections 5 and 9.4). It
// shows the server's program view as text: the coverage block first, then the areas, the interfaces, the program
// requirements' coverage, and the decisions and suggestions. Every name, number and sentence is the server's; an
// area the reader may not read is one line, "not visible to you", with no number. The page itself always reads
// the view, so a stale menu flag only shows or hides the link.
import { h } from '../dom.js';
import { pageHead, errorState } from '../ui.js';

// Whether the project menu shows "Program": the server's cached flag on the project (Q1 at VERIFIED).
export function showsProgram(project) {
  return Boolean(project && project.program === true);
}

function coordinatorsText(found) {
  if (!found) return '';
  const people = (found.people || []).map((p) => `${p.display_name} (${p.role})`);
  const agents = (found.agents || []).map((a) => `${a.name} (agent of ${a.owner_display_name})`);
  return people.concat(agents).join(', ') || 'none';
}

export function areaRow(row) {
  if (!row.visible) {
    return h('tr', { 'data-area': row.area },
      h('td', null, row.area), h('td', null, row.state), h('td', { colspan: '4', class: 'muted' }, row.note));
  }
  const lanes = row.lanes || {};
  return h('tr', { 'data-area': row.area },
    h('td', null, row.area), h('td', null, row.state), h('td', null, String(row.open_tasks)),
    h('td', null, String(lanes.waiting_too_long)), h('td', null, String(lanes.expect_by_passed)),
    h('td', null, coordinatorsText(row.coordinators)));
}

export function areasTable(areas) {
  const items = (areas && areas.items) || [];
  if (!items.length) return h('p', { class: 'muted', 'data-empty': 'areas' }, 'This project lists no areas.');
  return h('table', { class: 'table', 'data-table': 'areas' },
    h('thead', null, h('tr', null, ...['Area', 'Link', 'Open tasks', 'Lanes waiting long', 'Lanes past expect-by',
      'Coordinators'].map((label) => h('th', null, label)))),
    h('tbody', null, ...items.map(areaRow)));
}

export function requirementsTable(requirements) {
  const items = (requirements && requirements.items) || [];
  const rows = items.map((row) => h('tr', { 'data-requirement': row.key },
    h('td', null, row.key),
    h('td', null, row.flags && row.flags.includes('cites-superseded-revision')
      ? `${row.area}: ${row.design}#${row.heading} cites ${row.token}` : (row.title || '')),
    h('td', null, (row.flags || []).join(', '))));
  return h('div', { class: 'stack' },
    h('p', { class: 'small muted' }, (requirements && requirements.scope) || ''),
    items.length ? h('table', { class: 'table', 'data-table': 'requirements' },
      h('thead', null, h('tr', null, ...['Requirement', 'Detail', 'Flag'].map((label) => h('th', null, label)))),
      h('tbody', null, ...rows))
      : h('p', { class: 'muted', 'data-empty': 'requirements' }, 'Nothing to show for the areas read.'));
}

// kittrial-5bb.454: the program's binding decisions, each with its bound areas' adoption states, as text.
export function decisionsTable(decisions) {
  const items = (decisions && decisions.items) || [];
  const rows = items.map((d) => {
    const areas = Object.entries(d.areas || {}).map(([area, entry]) =>
      `${area}: ${entry.state}${entry.reason ? ` (${entry.reason})` : ''}`).join('; ');
    const flags = (d.flags || []).concat(d.superseded ? ['superseded'] : []);
    return h('tr', { 'data-decision': d.id }, h('td', null, d.id), h('td', null, d.title || ''),
      h('td', null, d.owner_only ? 'owner-only' : ''), h('td', null, [flags.join(', '), areas].filter(Boolean).join('; ')));
  });
  return h('div', { class: 'stack' },
    h('p', { class: 'small muted' }, (decisions && decisions.scope) || ''),
    items.length ? h('table', { class: 'table', 'data-table': 'decisions' },
      h('thead', null, h('tr', null, ...['Decision', 'Title', 'Kind', 'Areas'].map((label) => h('th', null, label)))),
      h('tbody', null, ...rows))
      : h('p', { class: 'muted', 'data-empty': 'decisions' }, 'No program decision binds an area.'));
}

// The open suggestions from areas, oldest first, with the From: line each cites, as written.
export function suggestionsTable(suggestions) {
  const items = (suggestions && suggestions.items) || [];
  if (!items.length) return h('p', { class: 'muted', 'data-empty': 'suggestions' }, 'No open suggestion.');
  return h('table', { class: 'table', 'data-table': 'suggestions' },
    h('thead', null, h('tr', null, ...['Suggestion', 'Title', 'From', 'Raised'].map((label) => h('th', null, label)))),
    h('tbody', null, ...items.map((s) => h('tr', { 'data-suggestion': s.id }, h('td', null, s.id),
      h('td', null, s.title || ''), h('td', null, s.from || ''), h('td', null, s.created_at || '')))));
}

function section(name, title, body) {
  return h('section', { class: 'panel', 'data-panel': name },
    h('div', { class: 'panel-head' }, h('h2', { class: 'small' }, title)),
    h('div', { class: 'panel-body' }, body));
}

export function view(read) {
  const note = (part) => h('p', { class: 'muted', 'data-state': part && part.state }, (part && part.note) || '');
  return [
    h('p', { class: 'coverage', 'data-coverage': '' }, (read.coverage && read.coverage.text) || ''),
    section('areas', 'Areas', areasTable(read.areas)),
    section('interfaces', 'Interfaces', note(read.interfaces)),
    section('requirements', 'Program requirements', requirementsTable(read.requirements)),
    section('decisions', 'Decisions', decisionsTable(read.decisions)),
    section('suggestions', 'Suggestions', suggestionsTable(read.suggestions)),
  ];
}

export async function page(ctx, { pid }) {
  const host = h('div', { class: 'stack' });
  const head = () => pageHead({
    crumbs: [{ label: 'Projects', href: ctx.href('/projects') }, { label: pid, href: ctx.href('/p/' + pid) }, { label: 'Program' }],
    title: 'Program', lede: 'The areas linked with this program, read under your own permissions. A link grants nothing.' });
  async function draw(cursor) {
    let read;
    try { read = await ctx.api.programView(pid, cursor); } catch (error) {
      host.replaceChildren(head(), errorState(error, () => draw(cursor)));
      return;
    }
    const next = read.areas && read.areas.next_cursor;
    host.replaceChildren(head(), ...view(read),
      next ? h('div', { class: 'toolbar' }, h('button', { type: 'button', onclick: () => draw(next) }, 'Next areas')) : null);
  }
  await draw(null);
  return host;
}
