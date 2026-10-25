import { h, time } from '../dom.js';
import { errorState, field, setFieldError, formValues, act, describe } from '../ui.js';

// Delegated decision rights (kittrial-5bb.407). A person owner delegates record acceptance to the
// project's coordinator agents (or to one named agent) on a decision issue; an agent then accepts
// or verifies records only within a delegation in force. Every member reads the list; only a
// person owner's own session records, narrows or revokes one, and the server checks that again.
const SCOPE = { references: 'references', capabilities: 'capabilities' };
const GRANTEE = (d, names) => (d.grantee === 'coordinator-grant'
  ? 'every agent holding this project\'s coordinator grant'
  : (names[d.grantee] || d.grantee));

function line(item, names) {
  const d = item.delegation;
  if (!item.in_force || !d) {
    return h('li', { 'data-delegation': item.decision, 'data-in-force': 'false' },
      h('span', { class: 'chip plain' }, 'Not in force'), ' ', item.decision, item.title ? ' · ' + item.title : '',
      h('div', { class: 'small muted' }, item.reason || ''));
  }
  return h('li', { 'data-delegation': item.decision, 'data-in-force': 'true' },
    h('span', { class: 'chip ok' }, 'In force'), ' ', item.decision, item.title ? ' · ' + item.title : '',
    h('div', { class: 'small' }, 'Record acceptance by ', GRANTEE(d, names), ' for ',
      d.scope.map((s) => SCOPE[s] || s).join(' and ')),
    h('div', { class: 'small' }, 'Escalate: ', d.escalate),
    h('div', { class: 'small muted' }, 'Recorded by ', d.via === 'session' ? (names[d.account] || d.account) : d.recorded_by + ' (operator)',
      ' ', time(d.at), d.changes && d.changes.length ? ' · narrowed ' + d.changes.length + ' time(s)' : ''));
}

export function delegationsPanel(ctx, project, { owner = false } = {}) {
  const host = h('div');
  async function draw() {
    let data;
    try { data = await ctx.api.delegations(project.id); } catch (error) {
      // A server older than this read has no route: the page stays as it was.
      if ([404, 501].includes(error.status)) { host.replaceChildren(); return; }
      host.replaceChildren(errorState(error, draw));
      return;
    }
    const names = data.names || {};
    const items = data.items || [];
    const agents = data.granted_agents || [];
    const body = h('div', { class: 'panel-body stack' },
      h('p', { class: 'small muted' },
        'An agent holding the coordinator grant accepts or verifies a reference or capability record only within a ',
        'delegation in force, recorded by a person owner. Anything outside it is escalated to the owner.'),
      h('div', null, h('h3', { class: 'small' }, 'Delegations'),
        items.length ? h('ul', { 'data-delegations': String(items.length) }, items.map((item) => line(item, names)))
          : h('p', { class: 'muted' }, 'No delegation is recorded in this project.')),
      h('div', null, h('h3', { class: 'small' }, 'Coordinator agents'),
        agents.length ? h('ul', null, agents.map((a) => h('li', { 'data-granted-agent': a.agent, 'data-covered': String(a.covered) },
          names[a.agent] || a.agent, ' · ',
          a.covered ? 'may accept records (decision ' + a.by.join(', ') + ')'
            : 'holds the grant but no delegation: it cannot accept records')))
          : h('p', { class: 'muted' }, 'No agent holds this project\'s coordinator grant.')));
    if (owner) body.append(ownerForm(ctx, project, agents, names, draw));
    host.replaceChildren(h('section', { class: 'panel', 'data-panel': 'delegations' },
      h('div', { class: 'panel-head' }, h('h2', { class: 'small' }, 'Delegated decisions')), body));
  }
  draw();
  return { node: host, draw };
}

function ownerForm(ctx, project, agents, names, draw) {
  const grantees = [['coordinator-grant', 'Every agent holding the coordinator grant']]
    .concat(agents.map((a) => [a.agent, names[a.agent] || a.agent]));
  const form = h('form', { class: 'stack', novalidate: true, 'data-form': 'delegation' },
    h('h3', { class: 'small' }, 'Record a delegation'),
    h('p', { class: 'small muted' }, 'A grant either names an existing decision issue or creates one from the title ',
      'below (the reason becomes its description). A narrowing names a smaller scope; a revocation ends it. ',
      'Only your own signed-in session can do this.'),
    h('div', { class: 'form-row' },
      field({ id: 'd-action', label: 'Action', type: 'select', value: 'grant',
        options: [['grant', 'Grant'], ['narrow', 'Narrow'], ['revoke', 'Revoke']] }),
      field({ id: 'd-decision', label: 'Decision issue id (empty: create one)', placeholder: 'e.g. ' + project.id + '-abc' })),
    field({ id: 'd-title', label: 'Title of a new decision', maxlength: 200,
      hint: 'Only for a grant with no decision issue id.' }),
    field({ id: 'd-grantee', label: 'Granted to', type: 'select', value: 'coordinator-grant', options: grantees }),
    h('fieldset', null, h('legend', { class: 'small' }, 'Scope'),
      h('label', null, h('input', { type: 'checkbox', name: 'd-scope-references', checked: true }), ' references'),
      h('label', null, h('input', { type: 'checkbox', name: 'd-scope-capabilities', checked: true }), ' capabilities')),
    field({ id: 'd-escalate', label: 'Escalate (what the agent must not decide)', maxlength: 300,
      hint: 'One line. Empty for a revocation.' }),
    field({ id: 'd-reason', label: 'Reason', type: 'textarea', rows: 2, maxlength: 1000, required: true }),
    h('div', null, h('button', { type: 'submit', class: 'primary' }, 'Record')));
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const v = formValues(form);
    const action = v['d-action'];
    const decision = (v['d-decision'] || '').trim();
    const title = (v['d-title'] || '').trim();
    if (!decision && action !== 'grant') return setFieldError(form, 'd-decision', 'Enter the decision issue id.');
    if (!decision && !title) return setFieldError(form, 'd-title', 'Enter a decision issue id, or a title for a new decision.');
    if (decision && title) return setFieldError(form, 'd-title', 'Give a title only to create a new decision.');
    setFieldError(form, 'd-decision', '');
    setFieldError(form, 'd-title', '');
    const scope = action === 'revoke' ? []
      : ['capabilities', 'references'].filter((s) => form.querySelector('[name=d-scope-' + s + ']').checked);
    const body = { action, decision: decision || null, title: decision ? null : title,
      right: 'record-acceptance', grantee: v['d-grantee'], scope,
      escalate: action === 'revoke' ? '' : (v['d-escalate'] || '').trim(), reason: (v['d-reason'] || '').trim() };
    if (action !== 'revoke' && !body.escalate) return setFieldError(form, 'd-escalate', 'Say what must be escalated.');
    if (!body.reason) return setFieldError(form, 'd-reason', 'Give a reason.');
    try {
      await act(form.querySelector('button[type=submit]'), () => ctx.api.recordDelegation(project.id, body),
        { success: action === 'grant' ? 'Delegation recorded' : action === 'narrow' ? 'Delegation narrowed' : 'Delegation revoked',
          onError: (error) => setFieldError(form, 'd-decision', describe(error)) });
    } catch { return; }
    draw();
  });
  return form;
}
