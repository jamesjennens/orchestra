// "Add a child task" on the task page (kittrial-5bb.434): a coordinator files a child task or a decision
// under this task. Shown only when the server says the reader may coordinate (`project.can_coordinate`); the
// server re-checks everything (POST .../tasks/{tid}/children runs the host's own create-child). Text only.
import { h } from '../dom.js';
import { field, formValues, act } from '../ui.js';

// create-child's type list (coordination.CHILD_TYPES) and a decision's required headings
// (coordination.REQUIRED_SECTIONS); tests/test_web_coordinator_gaps.py holds both equal to the Python.
export const CHILD_TYPES = ['task', 'bug', 'feature', 'chore', 'decision'];
export const DECISION_HEADINGS = ['Decision', 'Rationale', 'Alternatives Considered'];

export function childForm(ctx, project, task, onCreated) {
  if (!project || !project.can_coordinate || project.archived) return null;
  const hint = h('p', { class: 'small muted', 'data-decision-headings': '1', hidden: true },
    'A decision needs these headings in its description: ', DECISION_HEADINGS.map((name) => '## ' + name).join(', '), '.');
  const form = h('form', { class: 'panel-body', novalidate: true, 'data-child-form': '1' },
    h('div', { class: 'form-row' },
      field({ id: 'c-title', label: 'Title', required: true }),
      field({ id: 'c-type', label: 'Type', type: 'select', value: 'task', options: CHILD_TYPES.map((t) => [t, t]) })),
    field({ id: 'c-description', label: 'Description', type: 'textarea', rows: 4 }),
    hint,
    h('div', { class: 'form-row' },
      field({ id: 'c-assignee', label: 'Assignee (optional)' }),
      field({ id: 'c-priority', label: 'Priority', type: 'select', value: '2', options: [0, 1, 2, 3, 4].map((p) => [String(p), 'P' + p]) })),
    h('div', null, h('button', { type: 'submit', class: 'primary' }, 'Add child task')));
  const type = form.querySelector('#c-type');
  const showHint = () => { hint.hidden = !type || type.value !== 'decision'; };
  if (type) type.addEventListener('change', showHint);
  showHint();
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const v = formValues(form);
    const body = { title: String(v['c-title'] || '').trim(), type: v['c-type'], description: String(v['c-description'] || ''),
      priority: Number(v['c-priority']) };
    if (String(v['c-assignee'] || '').trim()) body.assignee = String(v['c-assignee']).trim();
    const made = await act(form.querySelector('button[type=submit]'),
      () => ctx.api.createChild(project.id, task.id, body), { success: 'Child task added' }).catch(() => null);
    if (made && onCreated) onCreated(made);
  });
  return h('section', { class: 'panel', 'aria-label': 'Add a child task', 'data-child-panel': '1' },
    h('div', { class: 'panel-head' }, h('h2', { class: 'small' }, 'Add a child task')), form);
}
