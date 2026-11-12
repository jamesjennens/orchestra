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
  ['standards', '/p/{pid}/standards'],
  ['releases', '/p/{pid}/releases'],
  ['feedback', '/p/{pid}/feedback'],
  ['rooms', '/p/{pid}/rooms'],
  ['room', '/p/{pid}/rooms/{rid}'],
  ['lanes', '/p/{pid}/lanes'],
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

// Where the project switcher sends the current page. A project page keeps its section
// when the target project has it (`sections` lists the segments, '' for Tasks); anything
// else, a task page included, opens the target's Tasks list and says so via fellBack.
export function switchTarget(route, targetPid, sections) {
  const path = route.split('?')[0];
  const pid = projectOf(path);
  if (!pid) return { path: `/p/${targetPid}`, fellBack: false, section: '' };
  const match = new RegExp(`^/p/${ID}(?:/([a-z]+))?$`).exec(path);
  if (!match) return { path: `/p/${targetPid}`, fellBack: true, section: '' };
  const section = match[1] || '';
  if (section === '' || sections.includes(section)) {
    return { path: section ? `/p/${targetPid}/${section}` : `/p/${targetPid}`, fellBack: false, section };
  }
  return { path: `/p/${targetPid}`, fellBack: true, section };
}

// The sentence the switcher's toast shows after a fallback. `label` is the section's name in
// the project the user left (the target lacks the section, so it cannot supply the name).
export function fallbackMessage(name, label) {
  return label
    ? `${name} has no ${label} page, so it opened on Tasks.`
    : `Task pages belong to one project, so ${name} opened on Tasks.`;
}
