// Hash-route table for the web interface. Pure data and string handling only (no DOM),
// so it can be checked outside a browser (tests/test_http_web.py runs it under node
// when available and re-implements the same compile step in Python).
//
// ID is the server's identifier pattern (http_service.ID): canonical ids contain dots
// and hyphens, e.g. kittrial-5bb.20, so a narrower pattern would hide real tasks.
export const ID = '[A-Za-z0-9][A-Za-z0-9_.-]{0,127}';

// name -> path template; {param} matches one ID.
export const ROUTE_PATTERNS = [
  ['home', '/'],
  ['welcome', '/welcome'],
  ['projects', '/projects'],
  ['agents', '/agents'],
  ['agentSetup', '/setup'],
  ['project', '/p/{pid}'],
  ['reviews', '/p/{pid}/reviews'],
  ['releases', '/p/{pid}/releases'],
  ['feedback', '/p/{pid}/feedback'],
  ['rooms', '/p/{pid}/rooms'],
  ['room', '/p/{pid}/rooms/{rid}'],
  ['lanes', '/p/{pid}/lanes'],
  ['people', '/p/{pid}/people'],
  ['projectAgents', '/p/{pid}/agents'],
  ['settings', '/p/{pid}/settings'],
  ['setup', '/p/{pid}/setup'],
  ['handover', '/p/{pid}/handover'],
  ['notes', '/p/{pid}/notes'],
  ['program', '/p/{pid}/program'],
  ['how', '/p/{pid}/how'],
  ['newTask', '/p/{pid}/new'],
  ['task', '/p/{pid}/t/{tid}'],
  ['requirements', '/p/{pid}/requirements'],
  ['requirement', '/p/{pid}/requirements/{rid}'],
  ['records', '/p/{pid}/records'],
  ['referenceRecord', '/p/{pid}/records/references/{key}'],
  ['referenceRecord', '/p/{pid}/references/{key}'],
  ['designPage', '/p/{pid}/designs/{scope}'],
  ['tracePage', '/p/{pid}/trace/{view}'],
  ['capabilityRecord', '/p/{pid}/records/capabilities/{key}'],
  ['capabilityRecord', '/p/{pid}/capabilities/{key}'],
  ['decisions', '/p/{pid}/decisions'],
  ['decision', '/p/{pid}/decisions/{did}'],
  ['record', '/p/{pid}/records/{id}'],
  ['users', '/admin/users'],
  ['account', '/account'],
];

const escape = (text) => text.replace(/[.*+?^$()|[\]\\/]/g, '\\$&');

export function compile(template) {
  const source = template.split(/(\{[a-z]+\})/).map((part) => {
    const param = /^\{([a-z]+)\}$/.exec(part);
    return param ? `(?<${param[1]}>${ID})` : escape(part);
  }).join('');
  return new RegExp('^' + source + '$');
}

export const COMPILED = ROUTE_PATTERNS.map(([name, template]) => [name, compile(template)]);

// The first route that matches, as { name, params }, or null.
export function matchRoute(route) {
  for (const [name, pattern] of COMPILED) {
    const match = pattern.exec(route);
    if (match) return { name, params: match.groups || {} };
  }
  return null;
}

// The project id a route is scoped to, if any.
export const PROJECT_PREFIX = new RegExp(`^/p/(${ID})(?:/|$)`);
export function projectOf(route) {
  const match = PROJECT_PREFIX.exec(route);
  return match ? match[1] : null;
}

// Views whose server routes are not part of every deployment yet.
export const RECORD_ROUTES = new Set(['requirements', 'requirement', 'decisions', 'decision', 'record']);

// What each record page is called and which list of the same project it belongs to (kittrial-5bb.522 slice 1,
// r2): a room page switches to the target's Rooms, a requirement page to its Requirements, and so on.
const LIST_OF_RECORD = { room: ['rooms', 'room'], requirement: ['requirements', 'requirement'], decision: ['decisions', 'decision'],
  record: ['records', 'record'], referenceRecord: ['records', 'record'], capabilityRecord: ['records', 'record'] };
// The name of each section, for the toast when the page the person left does not show it in the rail.
export const SECTION_LABELS = { reviews: 'Reviews', releases: 'Releases', requirements: 'Requirements', records: 'Records',
  decisions: 'Decisions', rooms: 'Rooms', lanes: 'Lanes', program: 'Program', feedback: 'Orchestra feedback',
  people: 'People', agents: 'Agents', settings: 'Settings', setup: 'Set up', how: 'How work is found', handover: 'Handover', notes: 'Notes' };

// Where the project switcher sends the current page. A project page keeps its section when the target project
// has it (`sections` lists the segments the target serves to this person, '' for Tasks); a record page (room,
// requirement, decision, record) opens the target's list of that kind; anything else opens the target's Tasks
// list and says so via fellBack. `page` names the reason when it is not "the target lacks the section":
// 'new', 'other' or the record's noun (fallbackMessage turns it into a sentence).
export function switchTarget(route, targetPid, sections) {
  const [path, search] = route.split('?');
  const pid = projectOf(path);
  if (!pid) return { path: `/p/${targetPid}`, fellBack: false, section: '' };
  const tasks = (page) => ({ path: `/p/${targetPid}`, fellBack: true, section: '', ...(page ? { page } : {}) });
  const match = new RegExp(`^/p/${ID}(?:/([a-z]+))?$`).exec(path);
  if (!match) {
    // A record of the old project: its own list in the target when served, else Tasks. Nothing of the old query.
    const found = matchRoute(path);
    if (found && found.name === 'task') return tasks();
    const kind = found && LIST_OF_RECORD[found.name];
    if (!kind) return tasks('other');
    const [list, noun] = kind;
    return sections.includes(list)
      ? { path: `/p/${targetPid}/${list}`, fellBack: true, section: list, page: noun }
      : { path: `/p/${targetPid}`, fellBack: true, section: list };
  }
  const section = match[1] || '';
  // A new task is written in one project: Tasks, nothing of the query.
  if (section === 'new') return tasks('new');
  // kittrial-5bb.522 slice 1: the view's filters survive the switch (they mean the same in every project).
  const kept = switchQuery(search);
  // A search that stays behind is said so (qLeft), alone or with a fallback sentence.
  const qLeft = new URLSearchParams(search || '').get('q');
  const dropped = qLeft && !mayCross('q', qLeft) ? { qDropped: true } : {};
  if (section === '' || sections.includes(section)) {
    return { path: (section ? `/p/${targetPid}/${section}` : `/p/${targetPid}`) + kept, fellBack: false, section, ...dropped };
  }
  return { path: `/p/${targetPid}` + kept, fellBack: true, section, ...dropped };
}

// The query keys that are view filters, the same in every project (kittrial-5bb.522 slice 1). Every other key
// names something of one project (`proposal=<key>` on Reviews) or a mode of one page, and is not carried.
// The Records page's `state` is not here: no page writes it, so no switch carries it.
export const SWITCH_FILTER_KEYS = ['status', 'q', 'review', 'requirements'];
// A value crosses only when it means the same in every project (r2): status, review and requirements when it is a
// word of their vocabulary; q when no word in it has the shape of an id. A search for an id (task_18cb2f,
// alpha-7k2, kittrial-5bb.522, room_7f3a, r-9d4487e9c7da, REQ-7) names one record of one project, so it stays
// behind. The ONE pattern, tested once on the whole value (no split, no trim, so an id next to any other
// character is still found and the test is linear): a letter or digit, one or more of '-', '_' or '.', then a
// letter or digit. Plain words cross; a hyphenated or dotted word such as set-up has the shape too and stops
// crossing, which is accepted.
export const ID_SHAPED = /[A-Za-z0-9][-_.]+[A-Za-z0-9]/;
const SWITCH_WORDS = new Map([
  ['status', ['active', 'closed', 'all']],
  ['review', ['awaiting-review', 'legacy-review-ready', 'changes-requested', 'approved', 'awaiting-integration', 'error', 'withdrawn', 'superseded', 'none']],
  ['requirements', ['1']],
]);
const namesARecord = (text) => ID_SHAPED.test(text);
export function mayCross(key, value) {
  if (key === 'q') return !namesARecord(value);
  const words = SWITCH_WORDS.get(key);
  return !!words && words.includes(value);
}
export function switchQuery(search) {
  const from = new URLSearchParams(search || '');
  const kept = new URLSearchParams();
  for (const key of SWITCH_FILTER_KEYS) {
    const value = from.get(key);
    if (value !== null && value !== '' && mayCross(key, value)) kept.set(key, value);
  }
  const text = kept.toString();
  return text ? '?' + text : '';
}

// The last project a person opened, kept in their own browser (kittrial-5bb.522 slice 1). Keyed by the person,
// so two people signed in on one browser never share it; cleared at sign-out. A view preference only: no
// route reads it and it decides nothing.
export const LAST_PROJECT_PREFIX = 'orchestra.lastProject.';
export function rememberProject(storage, userId, pid) {
  if (!storage || !userId || !pid) return;
  try { storage.setItem(LAST_PROJECT_PREFIX + userId, pid); } catch { /* storage unavailable: nothing is remembered */ }
}
// The remembered project when the person can still open it (listed and not archived), else null; a project
// omitted by a SUCCESSFUL list is forgotten. `projects` is null when the list could not be read: that says
// nothing about access, so nothing is shown and nothing is forgotten.
export function rememberedProject(storage, userId, projects) {
  if (!storage || !userId || !projects) return null;
  let pid = null;
  try { pid = storage.getItem(LAST_PROJECT_PREFIX + userId); } catch { return null; }
  if (!pid) return null;
  const found = projects.find((p) => p.id === pid && !p.archived);
  if (found) return pid;
  try { storage.removeItem(LAST_PROJECT_PREFIX + userId); } catch { /* ignore */ }
  return null;
}
export function forgetProject(storage, userId) {
  if (!storage || !userId) return;
  try { storage.removeItem(LAST_PROJECT_PREFIX + userId); } catch { /* ignore */ }
}

// The line the toast adds when a switch leaves the search behind (switchTarget's qDropped).
export function searchLeftMessage(oldName) {
  return `The search was not carried over, because it names something in ${oldName}.`;
}

// The sentence the switcher's toast shows after a fallback, one true reason per case. `label` is the section's
// name in the project the user left (the target lacks the section, so it cannot supply the name); `page` is
// switchTarget's reason when it is not a missing section.
export function fallbackMessage(name, label, page) {
  if (page === 'new') return `A new task is written in one project, so ${name} opened on Tasks.`;
  if (page === 'other') return `That page belongs to one project, so ${name} opened on Tasks.`;
  if (page) return `A ${page} belongs to one project, so ${name} opened on its ${label} list.`;
  return label
    ? `${name} has no ${label} page, so it opened on Tasks.`
    : `Task pages belong to one project, so ${name} opened on Tasks.`;
}
