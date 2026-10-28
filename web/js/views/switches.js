// The installation switches that refuse a web write (kittrial-5bb.434), on Members & settings: each switch's
// name, state, what it unlocks and the operator command that turns it on. Read only, for a reader the server
// says may coordinate (`project.can_coordinate`); the page decides nothing itself. Every string is text.
import { h } from '../dom.js';
import { describe } from '../ui.js';

const STATES = { on: 'on', off: 'off', unreadable: 'unreadable: an operator repairs the setting' };

export function switchesPanel(ctx, project) {
  if (!project || !project.can_coordinate) return h('div', { hidden: true, 'data-switches-panel': 'none' });
  const body = h('div', { class: 'small muted' }, 'Reading the installation switches…');
  const panel = h('section', { class: 'panel', 'aria-labelledby': 'switches-heading', 'data-switches-panel': '1' },
    h('div', { class: 'panel-head' }, h('h2', { class: 'small', id: 'switches-heading' }, 'Installation switches'),
      h('span', { class: 'small muted hide-narrow' }, 'A web write these refuse while off needs an operator on the server')),
    body);
  Promise.resolve().then(() => ctx.api.switches(project.id)).then((answer) => {
    body.replaceChildren(h('ul', { class: 'switches' }, (answer.items || []).map((item) =>
      h('li', { 'data-switch': item.name, 'data-state': item.state },
        h('code', null, item.name), ' — ', STATES[item.state] || String(item.state),
        h('div', { class: 'small' }, 'Unlocks: ', String(item.unlocks)),
        item.state === 'on' ? null : h('div', { class: 'small muted' }, 'Operator command: ', h('code', null, String(item.command)))))));
  }).catch((error) => {
    if (error && (error.status === 404 || error.status === 501 || error.status === 403)) panel.hidden = true;
    else body.replaceChildren(h('p', { class: 'small muted' }, 'The switches could not be read: ', describe(error)));
  });
  return panel;
}
