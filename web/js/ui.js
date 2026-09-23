// Shared UI pieces: state chips, lifecycle strip, page scaffolding and error states.
import { h, toast } from './dom.js';

export const FACTS = [
  ['implemented', 'Implemented'], ['tested', 'Tested'], ['reviewed', 'Reviewed'],
  ['integrated', 'Integrated'], ['deployed', 'Deployed'], ['live-verified', 'Live-verified'],
];

const REVIEW = {
  'none': ['No contribution', ''],
  'awaiting-review': ['Awaiting review', 'accent'],
  'changes-requested': ['Changes requested', 'warn'],
  'approved': ['Approved · not integrated', 'ok'],
  'awaiting-integration': ['Awaiting integration', 'ok'],
  'integrated': ['Integrated', 'ok'],
  'error': ['Needs operator attention', 'crit'],
};
const STATUS = { open: ['Open', ''], in_progress: ['In progress', 'accent'], blocked: ['Blocked', 'crit'], closed: ['Closed', 'plain'] };
const PRIORITY = { 0: 'P0', 1: 'P1', 2: 'P2', 3: 'P3', 4: 'P4' };

export function reviewChip(state) {
  const [label, tone] = REVIEW[state] || [state || 'Unknown', ''];
  return h('span', { class: 'chip ' + tone }, label);
}
export function statusChip(status) {
  const [label, tone] = STATUS[status] || [status, ''];
  return h('span', { class: 'chip ' + tone }, label);
}
export function priority(p) {
  return h('span', { class: 'mono small muted', title: 'Priority' }, PRIORITY[p] ?? 'P?');
}
export function roleTag(role) {
  return role ? h('span', { class: 'role' }, role) : null;
}

// The six completion facts, kept distinct: "reviewed" never implies "deployed".
export function lifecycleStrip(lifecycle) {
  return h('div', { class: 'lifecycle', role: 'list', 'aria-label': 'Completion facts' },
    FACTS.map(([key, label]) => {
      const fact = (lifecycle && lifecycle[key]) || { value: 'unknown' };
      const value = fact.value === 'yes' ? 'Yes' : fact.value === 'no' ? 'Not yet' : 'Unknown';
      return h('div', { class: 'fact ' + (fact.value || 'unknown'), role: 'listitem' },
        h('span', { class: 'fact-name' }, label),
        h('span', { class: 'fact-value' }, value),
        fact.note ? h('span', { class: 'fact-note' }, fact.note) : null);
    }));
}

export function pageHead({ crumbs, title, lede, actions }) {
  return h('header', { class: 'page-head' },
    h('div', null,
      crumbs && crumbs.length ? h('nav', { class: 'crumbs', 'aria-label': 'Breadcrumb' },
        crumbs.flatMap((c, i) => [i ? h('span', { 'aria-hidden': 'true' }, '/') : null, c.href ? h('a', { href: c.href }, c.label) : h('span', null, c.label)])) : null,
      h('h1', { tabindex: '-1', id: 'page-title' }, title),
      lede ? h('p', { class: 'lede' }, lede) : null),
    actions ? h('div', { class: 'actions' }, actions) : null);
}

export function empty(title, body, action) {
  return h('div', { class: 'empty' }, h('strong', null, title), body ? h('p', null, body) : null, action || null);
}

export function loading(label = 'Loading…') {
  return h('div', { class: 'loading', role: 'status' }, label);
}

// Explains an API failure in the user's terms, with the server request id for support.
export function describe(error) {
  if (!error) return 'Something went wrong.';
  switch (error.status) {
    case 0: return 'The server could not be reached. Your change may not have been saved — retry to check.';
    case 401: return 'Your session has ended. Sign in again to continue.';
    case 403: return error.message || 'You do not have permission to do that.';
    case 404: return 'This item does not exist, or you do not have access to it.';
    case 409: return error.message || 'This was changed by someone else. Reload to see the current version.';
    case 413: return 'That is too large to send.';
    case 422: return error.message || 'Some details are not valid.';
    case 429: return 'Too many attempts. Wait a few minutes and try again.';
    default: return error.status >= 500 ? 'The server could not confirm whether this was saved. Retry — it is safe and will not create a duplicate.' : (error.message || 'Request failed.');
  }
}

export function errorState(error, retry) {
  return h('div', { class: 'panel' }, h('div', { class: 'empty', role: 'alert' },
    h('strong', null, error && error.status === 404 ? 'Not found' : error && error.status === 403 ? 'No access' : 'Could not load this page'),
    h('p', null, describe(error)),
    error && error.requestId ? h('p', { class: 'small mono' }, 'Request ' + error.requestId) : null,
    retry ? h('button', { type: 'button', onclick: retry }, 'Try again') : null));
}

// Runs a mutation with a busy button, a clear failure message and — when the outcome is
// uncertain — a retry that reuses the same idempotency key.
export async function act(button, work, { success, onError } = {}) {
  if (button) button.disabled = true;
  try {
    const result = await work();
    if (success) toast(success);
    return result;
  } catch (error) {
    if (onError && onError(error)) return undefined;
    if (error && error.retry) {
      toast(describe(error), 'crit');
    } else {
      toast(describe(error), 'crit');
    }
    throw error;
  } finally {
    if (button) button.disabled = false;
  }
}

export function field({ id, label, type = 'text', value = '', hint, required, autocomplete, rows, options, maxlength, placeholder }) {
  let control;
  if (type === 'textarea') control = h('textarea', { id, name: id, rows: rows || 4, required, maxlength, placeholder }, value);
  else if (type === 'select') control = h('select', { id, name: id, required }, options.map(([v, l]) => h('option', { value: v, selected: v === value }, l)));
  else control = h('input', { id, name: id, type, value, required, autocomplete, maxlength, placeholder });
  const errorId = id + '-error';
  const error = h('div', { class: 'error', id: errorId, hidden: true });
  if (hint) control.setAttribute('aria-describedby', id + '-hint ' + errorId);
  else control.setAttribute('aria-describedby', errorId);
  return h('div', { class: 'field' }, h('label', { for: id }, label), control, hint ? h('div', { class: 'hint', id: id + '-hint' }, hint) : null, error);
}

export function setFieldError(form, id, message) {
  const control = form.querySelector('#' + id);
  const error = form.querySelector('#' + id + '-error');
  if (!control || !error) return;
  control.setAttribute('aria-invalid', message ? 'true' : 'false');
  error.textContent = message || '';
  error.hidden = !message;
  if (message) control.focus();
}

export function formValues(form) {
  return Object.fromEntries(new FormData(form).entries());
}

export function avatarName(name) {
  return name || 'Unassigned';
}
