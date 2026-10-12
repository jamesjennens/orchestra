import { h, time } from '../dom.js';
import { errorState } from '../ui.js';

// Who coordinates a project (kittrial-5bb.198): the members whose role coordinates, the agents whose grant has
// effect and the rules that bind them. For every member, and read only: an owner changes roles and grants on the
// panels of this page, and an operator changes the installation's rules on the server.
const ROLE = { coordinator: 'Coordinator', owner: 'Owner' };
const HOST = {
  unknown: 'The server could not say just now which coordinator rules this installation enforces. Reload the page to try again.',
  unavailable: 'This server runs a version that cannot report the installation\'s coordinator rules. Ask an operator which are on.',
  'not-applicable': 'This server has no host project behind it, so the installation\'s coordinator rules do not apply here.',
};
// The installation's operator list, read from the host (kittrial-5bb.198); why it is not shown otherwise.
const OPERATORS = {
  unknown: 'The server could not say just now who its operators are. Reload the page to try again.',
  unavailable: 'This server runs a version that cannot list its operators. Ask an operator of the server.',
  'not-applicable': 'This server has no host project behind it, so it has no operators.',
};

export function coordinationPanel(ctx, project) {
  const host = h('div');
  async function draw() {
    let data;
    try { data = await ctx.api.coordination(project.id); } catch (error) {
      // A server older than this read has no route: the page stays as it was.
      if ([404, 501].includes(error.status)) { host.replaceChildren(); return; }
      host.replaceChildren(errorState(error, draw));
      return;
    }
    const people = data.people || [];
    const agents = data.agents || [];
    const rule = (r) => h('li', { 'data-rule': r.rule, 'data-on': String(r.on) },
      h('span', { class: 'chip ' + (r.on ? 'ok' : 'plain') }, r.on ? 'On' : 'Off'), ' ', r.text);
    const hostRules = data.host_rules || { state: 'unknown', rules: [] };
    // A service older than this read sends no operators: shown as a server that cannot list them.
    const operators = data.host_operators || { state: 'unavailable', operators: [] };
    host.replaceChildren(h('section', { class: 'panel', 'data-panel': 'coordination' },
      h('div', { class: 'panel-head' }, h('h2', { class: 'small' }, 'Who coordinates'),
        h('span', { class: 'small muted hide-narrow' }, 'Read only: owners change roles and grants on this page; operators change the installation\'s rules on the server')),
      h('div', { class: 'panel-body stack' },
        h('div', null, h('h3', { class: 'small' }, 'People'),
          people.length ? h('ul', null, people.map((p) => h('li', { 'data-person': p.user_id },
            p.display_name, ' · ', ROLE[p.role] || p.role, p.disabled ? ' · account disabled, so does not coordinate now' : '')))
            : h('p', { class: 'muted' }, 'No member coordinates this project.')),
        h('div', null, h('h3', { class: 'small' }, 'Agents'),
          agents.length ? h('ul', null, agents.map((a) => h('li', { 'data-agent-name': a.name },
            a.name, ', agent of ', a.owner_display_name, h('span', { class: 'muted' }, ' · granted by ', a.granted_by_display_name, ' ', time(a.granted_at)))))
            : h('p', { class: 'muted' }, 'No agent coordinates this project.')),
        h('div', null, h('h3', { class: 'small' }, 'Operators of the server'),
          operators.state === 'available'
            ? (operators.operators.length
              ? h('ul', { 'data-operators': 'available' }, operators.operators.map((name) => h('li', { 'data-operator': name }, name)))
              : h('p', { class: 'muted', 'data-operators': 'available' }, 'The server lists no operator.'))
            : h('p', { class: 'small', 'data-operators': operators.state }, OPERATORS[operators.state] || OPERATORS.unknown),
          h('p', { class: 'small muted' }, 'Operators act on the server: they run its host commands (the guidance, the merge-slot repair, the backup schedule note) and change the rules below. The list is kept on the server and changed there.')),
        h('p', { class: 'small muted' }, 'A superuser of this server may also act as a coordinator here.'),
        h('div', null, h('h3', { class: 'small' }, 'Rules'),
          h('ul', { 'data-rules': 'web' }, (data.web_rules || []).map(rule)),
          hostRules.state === 'available' ? h('ul', { 'data-rules': 'host' }, hostRules.rules.map(rule))
            : h('p', { class: 'small', 'data-rules': 'host', 'data-state': hostRules.state }, HOST[hostRules.state] || HOST.unknown)))));
  }
  draw();
  return { node: host, draw };
}
