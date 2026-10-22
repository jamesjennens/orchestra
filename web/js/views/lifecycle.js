import { h } from '../dom.js';
import { describe } from '../ui.js';

const DIMENSIONS = ['implemented', 'tested', 'reviewed', 'integrated', 'deployed', 'live-verified'];
const VALUES = ['unknown', 'pending', 'passed', 'failed', 'not-applicable'];
const SCOPE = ['source_commit', 'integration_commit', 'release_id', 'environment'];

function integrationCheck(ctx, project, task) {
  if (!ctx.api.integrationCheck) return null;
  const target = h('input', { maxlength: 160, 'aria-label': 'Integration target (optional)' });
  const button = h('button', { type: 'button' }, 'Read integration check');
  const output = h('div', { class: 'stack', role: 'status' });
  let reading = false;
  function render(state) {
    const contribution = state.contribution;
    output.replaceChildren(
      h('p', null, 'Contribution: ', contribution ? (contribution.commit || 'no commit') : 'none',
        contribution && contribution.author ? ' by ' + contribution.author : '',
        state.review_state ? ' · review: ' + state.review_state : ''),
      h('h4', null, 'Checks the server can make'),
      h('ul', null, (state.checks || []).map(row => h('li', null,
        h('strong', null, row.check + ': '), row.allowed ? 'allowed' : 'refused',
        row.reason ? h('span', { class: 'muted' }, ' — ' + row.reason) : null,
        row.approver ? h('span', { class: 'muted' }, ' — approver ' + row.approver) : null,
        row.holder !== undefined && row.holder !== null
          ? h('span', { class: 'muted' }, ' — holder ' + row.holder) : null,
        row.target ? h('span', { class: 'muted' }, ' — target ' + row.target) : null))),
      h('h4', null, 'Not checked by the server'),
      h('ul', null, (state.unchecked || []).map(row => h('li', null,
        h('strong', null, row.check + ': '), 'not checked — ' + (row.reason || 'unknown')))),
      h('h4', null, 'Writes still owed'),
      h('ol', null, (state.next || []).map(step => h('li', null,
        h('strong', null, step.step + ': '), step.state,
        h('div', { class: 'muted' }, step.route,
          step.operation_id ? ' · operation ' + step.operation_id : ' · new operation identity'),
        step.operation_id_note ? h('div', { class: 'muted' }, step.operation_id_note) : null,
        step.reason ? h('div', { class: 'muted' }, step.reason) : null))),
      h('p', { class: 'muted' }, 'The server never performs the merge, the test run or the push; those stay a person\'s step.'));
  }
  button.addEventListener('click', async () => {
    if (reading) return;
    reading = true; button.disabled = true;
    output.replaceChildren(h('p', { class: 'muted' }, 'Reading…'));
    try {
      render(await ctx.api.integrationCheck(project.id, task.id, target.value.trim() || undefined));
    } catch (failure) {
      // A definite "this task does not exist" answer must not be reported as an
      // older server without the route (kittrial-5bb.374 review item 3a). Both
      // carry status 404, so the server's own sentence is the discriminator.
      if (failure.status === 404 && /Lifecycle task does not exist/i.test(failure.message || '')) {
        output.replaceChildren(h('p', { role: 'alert' }, failure.message));
      } else if ([404, 501].includes(failure.status)) {
        output.replaceChildren(h('p', { class: 'muted' }, 'This server has no integration check.'));
      } else {
        output.replaceChildren(h('p', { role: 'alert' }, failure.message || describe(failure)),
          failure.status === 409
            ? h('p', { class: 'muted' }, 'Read the current state again before changing anything.') : null);
      }
    } finally { reading = false; button.disabled = false; }
  });
  return h('details', null, h('summary', null, 'Integration check (read-only)'),
    h('p', { class: 'muted' }, 'Reports the merge preconditions the server can see, and the ones it cannot. It writes nothing.'),
    h('label', { class: 'stack' }, 'Integration target (optional)', target), button, output);
}

export function lifecyclePanel(ctx, project, task) {
  const panel = h('section', { id: 'lifecycle-facts', class: 'panel', 'aria-label': 'Lifecycle facts', hidden: true });
  if (!ctx.api.lifecycle) return null;
  let revision = 0;
  let writing = false;
  const archived = project.archived || project.usable === false;
  function label(text, node) { return h('label', { class: 'stack' }, text, node); }
  function select(values, name) {
    return h('select', { 'aria-label': name }, values.map(value => h('option', { value }, value)));
  }
  function error(message, failure) {
    // Native held-contribution and writer remedies are shown literally as text.
    message.replaceChildren(h('span', null, failure.message || describe(failure)));
    if (failure.status === 409) message.append(' Read the current state before changing this request.',
      h('button', { type: 'button', onclick: draw }, 'Read current state'));
    if (failure.retry) message.append(' ', h('button', { type: 'button', onclick: async () => {
      if (writing) return;
      writing = true;
      try { await failure.retry(); writing = false; await draw(); }
      catch (again) { error(message, again); }
      finally { writing = false; }
    } }, 'Retry the identical request'));
  }
  async function draw() {
    const version = ++revision;
    let state;
    try { state = await ctx.api.lifecycle(project.id, task.id); }
    catch (failure) {
      if (version !== revision) return;
      if ([404, 501].includes(failure.status)) { panel.replaceChildren(); return; }
      panel.hidden = false;
      panel.replaceChildren(h('div', { class: 'panel-body stack' }, h('h2', null, 'Lifecycle facts'),
        h('p', { role: 'alert' }, failure.message || describe(failure)),
        h('button', { type: 'button', onclick: draw }, 'Read again')));
      return;
    }
    if (version !== revision) return;
    panel.hidden = false;
    const scope = state.scope || Object.fromEntries(SCOPE.map(key => [key, '']));
    const message = h('p', { role: 'status' });
    const evidence = h('textarea', { rows: 3, maxlength: 100000, 'aria-label': 'Evidence pointers' });
    const provenance = select(['performed', 'reported', 'imported'], 'Evidence provenance');
    const note = h('input', { maxlength: 500, 'aria-label': 'Lifecycle note' });
    const trigger = h('input', { maxlength: 500, 'aria-label': 'Pending trigger' });
    const dimension = select(DIMENSIONS, 'Lifecycle dimension');
    const value = select(VALUES, 'Lifecycle value');
    value.value = 'passed';
    const scopeInputs = Object.fromEntries(SCOPE.map(key => [key,
      h('input', { value: scope[key] || '', maxlength: key.endsWith('commit') ? 40 : 500, 'aria-label': key })]));
    function pointers() { return evidence.value.split('\n').map(text => text.trim()).filter(Boolean); }
    function extra() {
      const body = { evidence: pointers(), provenance: provenance.value };
      if (note.value.trim()) body.note = note.value.trim();
      return body;
    }
    async function write(work, button) {
      if (writing) return;
      writing = true; button.disabled = true;
      try { await work(); writing = false; await draw(); }
      catch (failure) {
        error(message, failure);
        if (version !== revision) { writing = false; await draw(); }
      }
      finally { writing = false; update(); }
    }
    const scopeButton = h('button', { type: 'button', onclick: () => write(() =>
      ctx.api.setLifecycleScope(project.id, task.id, { ...extra(),
        scope: Object.fromEntries(SCOPE.map(key => [key, scopeInputs[key].value.trim()])),
        expected_scope_token: state.scope_token }), scopeButton) }, 'Set scope');
    const factButton = h('button', { type: 'button', onclick: () => write(() => {
      const body = { ...extra(), scope: state.scope, value: value.value };
      if (value.value === 'pending' && trigger.value.trim()) body.trigger = trigger.value.trim();
      return ctx.api.recordLifecycleFact(project.id, task.id, dimension.value, body);
    }, factButton) }, 'Record fact');
    const reason = h('p', { class: 'muted', role: 'note' });
    function update() {
      const base = (state.affordances || {})[dimension.value] || {};
      const allowed = dimension.value === 'integrated' && value.value === 'passed' ? base.passed || base : base;
      scopeButton.disabled = writing || archived || !state.scope_write?.allowed;
      factButton.disabled = writing || archived || !allowed.allowed;
      reason.textContent = archived ? 'This project cannot be changed.' : allowed.reason || '';
      trigger.disabled = value.value !== 'pending';
    }
    dimension.addEventListener('change', update);value.addEventListener('change', update);
    const approval = state.standing_approval;
    const pending = ctx.api.pendingLifecycle?.(project.id, task.id) || [];
    function savedRetry(request) {
      const button = h('button', { type: 'button', disabled: archived || writing || !state.scope_write?.allowed,
        onclick: () => write(request.retry, button) }, 'Retry saved identical request');
      return button;
    }
    panel.replaceChildren(h('div', { class: 'panel-body stack' }, h('h2', null, 'Lifecycle facts'),
      h('p', null, 'Current source: ', h('code', null, state.contribution?.commit || 'No current contribution')),
      h('p', null, approval ? 'Independent approval: ' + approval.approver : (state.approval_reason || 'No standing independent approval')),
      h('dl', null, SCOPE.map(key => h('div', null, h('dt', null, key.replaceAll('_', ' ')), h('dd', null, scope[key] || '—')))),
      h('div', { class: 'stack' }, DIMENSIONS.map(dim => {
        const fact = state.facts?.[dim] || { value: 'unknown' };
        return h('div', null, h('strong', null, dim + ': '), fact.value,
          fact.reason ? h('p', { class: 'muted' }, fact.reason) : null,
          fact.trigger ? h('p', null, 'Waiting for: ', fact.trigger) : null,
          fact.note ? h('p', { class: 'muted' }, fact.note) : null,
          h('ul', null, (fact.evidence || []).map(pointer => h('li', null, pointer))));
      })),
      h('button', { type: 'button', onclick: draw }, 'Refresh facts'),
      integrationCheck(ctx, project, task),
      pending.length ? h('div', { class: 'banner warn stack' }, h('strong', null, 'Requests to reconcile'),
        pending.map(request => h('div', null,
          h('p', null, request.operation + ' · Source: ' + (request.body.scope?.source_commit || 'none')
            + ' · Value: ' + (request.body.value || 'scope')),
          savedRetry(request)))) : null,
      h('a', { href: ctx.href('/p/' + project.id) }, 'View the project merge slot'),
      h('details', null, h('summary', null, 'Record lifecycle evidence'),
        label('Evidence pointers, one per line', evidence), label('Provenance', provenance), label('Optional note', note),
        h('h3', null, 'Scope'), SCOPE.map(key => label(key.replaceAll('_', ' '), scopeInputs[key])),
        state.scope_write?.reason ? h('p', { class: 'muted' }, state.scope_write.reason) : null, scopeButton,
        h('h3', null, 'Fact'), label('Dimension', dimension), label('Value', value), label('Trigger for a pending fact', trigger),
        reason, factButton, message)));
    update();
  }
  void draw();
  return panel;
}
