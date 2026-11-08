// Shared UI pieces: state chips, lifecycle strip, page scaffolding and error states.
import { h, toast } from './dom.js';

// An account id (usr_...) is never shown as text; agent and session ids stay visible so
// an actor can still be told apart. The caller passes the fallback word: 'Someone' where
// it starts a sentence, 'someone' mid-sentence.
export const isAccountId = (value) => typeof value === 'string' && value.startsWith('usr_');
export function actorWords(name, id, fallback) {
  if (typeof name === 'string' && name) return name;
  const raw = typeof id === 'string' ? id : (id && typeof id.text === 'string' ? id.text : '');
  return raw && !isAccountId(raw) ? raw : fallback;
}

export const FACTS = [
  ['implemented', 'Implemented'], ['tested', 'Tested'], ['reviewed', 'Reviewed'],
  ['integrated', 'Integrated'], ['deployed', 'Deployed'], ['live-verified', 'Live-verified'],
];

const REVIEW = {
  'none': ['No contribution', ''],
  'awaiting-review': ['Awaiting review', 'warn'],  // kittrial-5bb.525: the accent is not a state
  'changes-requested': ['Changes requested', 'crit'],  // kittrial-5bb.525 (c): sent back
  'approved': ['Approved · not integrated', 'ok'],
  'awaiting-integration': ['Awaiting integration', 'ok'],
  'integrated': ['Integrated', 'ok'],
  // Additive review-workflow states (kittrial-5bb.94): a withdrawn or superseded
  // revision is final and needs a new contribution to continue review.
  'withdrawn': ['Withdrawn', 'plain'],
  'superseded': ['Superseded', 'plain'],
  'error': ['Needs operator attention', 'crit'],
};
const STATUS = { open: ['Open', ''], in_progress: ['In progress', 'info'], blocked: ['Blocked', 'crit'], closed: ['Closed', 'plain'] };
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
  if (p === undefined || p === null) return null;
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

// The page header (kittrial-5bb.525 slice b): what this page is (its h1), the page's one key
// number, and its next action. The key is { num, label, source } when the page already knows
// it, or 'pending' when the page's own read fills it later through setPageKey. A page with no
// honest number passes no key and shows none: never a count of the rows on one page, never a
// guess. `source` names where the number comes from (data-key-source), for the tests.
export function pageHead({ crumbs, title, lede, actions, key, tabTitle }) {
  const keyNum = h('span', { class: 'key-num' });
  const keyLabel = h('span', { class: 'key-label' });
  const keyNode = key ? h('div', { class: 'page-key', 'data-page-key': '', hidden: true }, keyNum, keyLabel) : null;
  const head = h('header', { class: 'page-head' },
    h('div', { class: 'ph-title' },
      crumbs && crumbs.length ? h('nav', { class: 'crumbs', 'aria-label': 'Breadcrumb' },
        crumbs.flatMap((c, i) => [i ? h('span', { 'aria-hidden': 'true' }, '/') : null, c.href ? h('a', { href: c.href }, c.label) : h('span', null, c.label)])) : null,
      // The tab names the page; a page whose h1 alone would not say where it is (Tasks) names its place too.
      h('h1', { tabindex: '-1', id: 'page-title', 'data-tab-title': tabTitle || null }, title),
      lede ? h('p', { class: 'lede' }, lede) : null),
    keyNode,
    actions ? h('div', { class: 'actions' }, actions) : null);
  head.pageKey = keyNode && { node: keyNode, num: keyNum, label: keyLabel };
  if (key && key !== 'pending') setPageKey(head, key);
  return head;
}

// What waits on the reader (kittrial-5bb.525 slice b, r2): a task whose next step is theirs.
// - The assignee's step, for the assignee.
// - An owner step, for an owner or coordinator of the task's project, and only the steps that ask
//   someone to act: review, integrate, close an integrated task, or accept a requirement. An epic,
//   a record, a decision or a dependency-blocked row also carries an owner step, but it only says
//   what the row is, so it waits on no one.
// A delivered task is in its assignee's `assigned` list but waits on its owner; an owner's own
// delivered task is in both of the owner's lists. Each is counted once, for whoever acts.
const OWNER_ACTS = new Set(['awaiting-review', 'legacy-review-ready', 'approved', 'awaiting-integration', 'integrated']);
export function waitsOnReader(ctx, t, approvesIn) {
  const step = t && t.next_action;
  if (!step || t.status === 'closed') return false;
  if (step.who === 'assignee') return Boolean(ctx.me) && t.assignee === ctx.me.id;
  if (step.who !== 'owner' || !approvesIn(t.project_id)) return false;
  return ownerStepAsks(t);
}

// An owner step that asks someone to act (review, integrate, close an integrated task, accept a
// requirement), as opposed to one that only says what the row is (an epic, a record, a decision, a
// blocked or non-claimable row). My work and Tasks both take it from here (kittrial-5bb.525 c r2).
export function ownerStepAsks(t) {
  return OWNER_ACTS.has(t.review_state) || (t.requirement_record === true && t.requirement_state === 'awaiting-acceptance');
}

// Fills (or, with no key, hides) the key number of a header made by pageHead.
export function setPageKey(head, key) {
  const parts = head && head.pageKey;
  if (!parts) return;
  const { node } = parts;
  const known = Boolean(key) && key.num !== null && key.num !== undefined && key.num !== '';
  node.hidden = !known;
  parts.num.textContent = known ? String(key.num) : '';
  parts.label.textContent = known ? key.label : '';
  if (known && key.source) node.setAttribute('data-key-source', key.source); else node.removeAttribute('data-key-source');
}

// "3 hours", "2 days": how long ago an ISO time was, for a key number. Null when unknown.
export function durationSince(iso, now = Date.now()) {
  const at = Date.parse(iso);
  if (!iso || Number.isNaN(at)) return null;
  const minutes = Math.max(0, Math.floor((now - at) / 60000));
  const unit = (n, word) => `${n} ${word}${n === 1 ? '' : 's'}`;
  if (minutes < 60) return unit(minutes, 'minute');
  const hours = Math.floor(minutes / 60);
  if (hours < 48) return unit(hours, 'hour');
  return unit(Math.floor(hours / 24), 'day');
}

// Empty, loading and error states (kittrial-5bb.525 slice e): a small round icon, a title that
// says what would be here, one sentence on how it gets there, and the action when there is one.
const SVG_NS = 'http://www.w3.org/2000/svg';
const STATE_ICONS = {
  empty: ['M4 13l2.5-7h11L20 13', 'M4 13v5h16v-5h-5a3 3 0 0 1-6 0z'],
  error: ['M12 4l9 16H3z', 'M12 10v4', 'M12 17.5v.01'],
};
function stateIcon(kind) {
  const svg = document.createElementNS(SVG_NS, 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('aria-hidden', 'true');
  svg.setAttribute('fill', 'none');
  svg.setAttribute('stroke', 'currentColor');
  svg.setAttribute('stroke-width', '1.8');
  svg.setAttribute('stroke-linecap', 'round');
  svg.setAttribute('stroke-linejoin', 'round');
  STATE_ICONS[kind].forEach((d) => {
    const path = document.createElementNS(SVG_NS, 'path');
    path.setAttribute('d', d);
    svg.appendChild(path);
  });
  return h('span', { class: 'state-icon' }, svg);
}

export function empty(title, body, action) {
  return h('div', { class: 'state', 'data-state': 'empty' }, stateIcon('empty'),
    h('div', { class: 'state-text' }, h('strong', null, title), body ? h('p', null, body) : null),
    action ? h('div', { class: 'state-act' }, action) : null);
}

// Loading: a skeleton in the shape of what will come, never a blank. Its words are for screen
// readers (role=status); the bars are decoration, and they stop shimmering under reduced motion.
// It is not itself aria-busy (a busy live region may be held back by assistive technology): the
// region being loaded carries aria-busy, set and cleared by the view that reads it.
const SKELETON = [['60', '30'], ['80', '40'], ['40', '30']];
export function loading(label = 'Loading…', rows = 3) {
  return h('div', { class: 'loading', role: 'status', 'data-state': 'loading' },
    h('span', { class: 'visually-hidden' }, label),
    h('div', { class: 'skeleton', 'aria-hidden': 'true' },
      Array.from({ length: rows }, (_, i) => h('div', { class: 'sk-row' }, h('span', { class: 'sk sk-dot' }),
        h('span', { class: 'sk-lines' }, SKELETON[i % SKELETON.length].map((w) => h('span', { class: 'sk sk-w' + w })))))));
}

// Explains an API failure in the user's terms, with the server request id for support.
// The rule a refusal names. The server puts it in `detail` (one line, as text); a stale
// read is told to reload, in the page's words rather than a command's.
const STALE = /re-read|has moved on|does not match revision|changed by someone else/i;
function reason(error, fallback) {
  const message = error.message || fallback;
  let detail = typeof error.detail === 'string' ? error.detail.replace(/^[A-Za-z]*Error: /, '').trim() : '';
  if (!detail) return message;
  if (STALE.test(detail)) return 'Someone else changed this while you had it open. Reload the page to see the current version, then try again. (' + detail + ')';
  return /^Canonical command rejected the request$/.test(message) ? detail : message + ': ' + detail;
}

// What to do after an uncertain outcome. The protection lives in this page (api.js keeps
// the request's Idempotency-Key in memory), so it ends with a reload, and the message says so.
const RETRY = 'Stay on this page and press the same button again without changing anything: the same request is sent again, so it cannot create a duplicate. Do not reload first; after a reload the next press is a new request, so check whether it was saved before sending it again.';

export function describe(error) {
  if (!error) return 'Something went wrong.';
  switch (error.status) {
    case 0: return 'The server could not be reached. Your change may not have been saved. ' + RETRY;
    case 401: return 'Your session has ended. Sign in again to continue.';
    case 403: return error.message || 'You do not have permission to do that.';
    case 404: return 'This item does not exist, or you do not have access to it.';
    case 409: return reason(error, 'This was changed by someone else. Reload to see the current version.');
    case 413: return 'That is too large to send.';
    case 422: return reason(error, 'Some details are not valid.');
    case 429: return 'Too many attempts. Wait a few minutes and try again.';
    case 501: return 'This server does not support that yet.';
    default: return error.status >= 500 ? 'The server could not confirm whether this was saved. ' + RETRY : (error.message || 'Request failed.');
  }
}

// A failed READ in the reader's words (kittrial-5bb.525 e r2). Nothing was being saved, so it never says a
// change "may not have been saved" or "do not reload" (describe() says that, rightly, for writes). It names
// what is true: the server did not answer, or could not be reached; nothing was changed; and the server's own
// sentence when its body sent one (a body without one leaves only the code, or 'Request failed').
export function describeRead(error) {
  if (!error) return 'The page could not be read. Nothing was changed.';
  if (error.status === 0) return 'The server could not be reached. Nothing was changed; check the connection, then try again.';
  if (error.status >= 500 && error.status !== 501) {
    const said = error.message && error.message !== error.code && error.message !== 'Request failed' ? String(error.message).trim() : '';
    return `The server did not answer (${error.status}). Nothing was changed.` + (said ? ' The server said: ' + said : '');
  }
  return describe(error);
}

// An error: inline where the read failed, on the crit tone with its rule and icon; what failed,
// the server's reason in the reader's words, the request id for support, and Try again.
export function errorState(error, retry) {
  return h('div', { class: 'state error', role: 'alert', 'data-state': 'error' }, stateIcon('error'),
    h('div', { class: 'state-text' },
      h('strong', null, error && error.status === 404 ? 'Not found' : error && error.status === 403 ? 'No access' : 'Could not load this page'),
      h('p', null, describeRead(error)),
      error && error.requestId ? h('p', { class: 'state-req mono' }, 'Request ' + error.requestId) : null),
    retry ? h('div', { class: 'state-act' }, h('button', { type: 'button', onclick: retry }, 'Try again')) : null);
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

// Put the cursor in the first empty field of a form just shown, but only while nothing else
// has the focus: the page title (app.js focuses it on every render) or the body. Once the
// person has moved into a field, or into anything else, the focus is theirs (kittrial-5bb.503).
export function focusFirstEmpty(form) {
  const active = document.activeElement;
  const untouched = !active || active === document.body || (active.getAttribute && active.getAttribute('id') === 'page-title');
  if (!untouched) return;
  const first = Array.from(form.querySelectorAll('input')).find((input) => !input.value);
  if (first) first.focus();
}

// The server's password rule, stated by the page (kittrial-5bb.363). The session read
// carries the installation's minimum. The reset page is used signed out, where there is
// no session answer to read: it states no number and lets the server's own 422 sentence
// name the real range, rather than claiming a minimum it has not been told. The maximum
// is a bound on work, not a rule the page repeats: the server still refuses a password
// over 1024 characters.
export const PASSWORD_MISMATCH = 'The two new passwords do not match.';
export const PASSWORD_WHITESPACE = 'A password cannot be only spaces.';

export function passwordMinimum(me) {
  const value = me && me.password_min_length;
  return Number.isInteger(value) && value >= 1 ? value : null;
}

export function passwordHint(me) {
  const minimum = passwordMinimum(me);
  if (minimum === null) return 'The server checks the password length.';
  return `At least ${minimum} character${minimum === 1 ? '' : 's'}.`;
}

export function passwordAdvice(me) {
  const minimum = passwordMinimum(me);
  return minimum !== null && minimum >= 8 ? ' A short sentence is easier to remember.' : '';
}

export function passwordTooShort(me, value) {
  const text = String(value);
  if (text && !text.trim()) return PASSWORD_WHITESPACE;
  const minimum = passwordMinimum(me);
  if (minimum === null) return text ? '' : 'Enter a new password.';
  if (text.length >= minimum) return '';
  return `Use at least ${minimum} character${minimum === 1 ? '' : 's'}.`;
}

export function avatarName(name) {
  return name || 'Unassigned';
}
