import { h, brandMark } from '../dom.js';
import { field, setFieldError, formValues, describe, focusFirstEmpty,
         passwordHint, passwordTooShort, PASSWORD_MISMATCH } from '../ui.js';

const SVG_NS = 'http://www.w3.org/2000/svg';

// The brand side's "score": grey bars entering in turn, one in the accent, drawn from the
// mark's own idea (kittrial-5bb.525 slice d). Decorative only: aria-hidden, no text in it.
function score() {
  const svg = document.createElementNS(SVG_NS, 'svg');
  svg.setAttribute('viewBox', '0 0 420 170');
  svg.setAttribute('class', 'auth-score');
  svg.setAttribute('aria-hidden', 'true');
  [[0, 120], [60, 170], [150, 90], [30, 210], [210, 130], [90, 150], [260, 110], [20, 80]].forEach(([x, width], i) => {
    const rect = document.createElementNS(SVG_NS, 'rect');
    rect.setAttribute('x', x); rect.setAttribute('y', i * 22);
    rect.setAttribute('width', width); rect.setAttribute('height', 8); rect.setAttribute('rx', 4);
    if (i === 3) rect.setAttribute('class', 'lead');
    svg.appendChild(rect);
  });
  return svg;
}

// The sign-in frame: the brand side (the mark, one sentence on what Orchestra is, the score)
// and the form side, which holds the page's only h1. On a phone the brand side folds away and
// the mark sits above the form instead (CSS). No external asset: the mark and score are SVG.
function frame(...card) {
  return h('div', { class: 'auth' },
    h('aside', { class: 'auth-side', 'aria-label': 'About Orchestra' },
      h('div', { class: 'brand' }, brandMark(), h('span', null, 'Orchestra')),
      h('div', { class: 'auth-pitch' },
        h('p', { class: 'auth-headline' }, 'Every part, in its turn.'),
        h('p', null, 'One shared record of tasks, contributions and reviews, for the people on a team and the agents working with them.')),
      score(),
      h('p', { class: 'auth-fine' }, 'Signed-in pages are private to the members of each project.')),
    h('div', { class: 'auth-main' }, h('div', { class: 'auth-card' },
      h('div', { class: 'brand auth-card-brand' }, brandMark(), h('span', null, 'Orchestra')), ...card)));
}

export function login(ctx, message) {
  const status = h('div', { class: 'banner crit', role: 'alert', hidden: !message }, message || '');
  const submit = h('button', { type: 'submit', class: 'primary' }, 'Sign in');
  const form = h('form', { class: 'form', novalidate: true },
    field({ id: 'username', label: 'Username', autocomplete: 'username', required: true }),
    passwordField(ctx),
    submit);
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const { username, password } = formValues(form);
    setFieldError(form, 'username', username.trim() ? '' : 'Enter your username.');
    if (!username.trim()) return;
    setFieldError(form, 'password', password ? '' : 'Enter your password.');
    if (!password) return;
    submit.disabled = true; status.hidden = true;
    try {
      const user = await ctx.api.login(username.trim(), password);
      await ctx.signedIn(user);
    } catch (error) {
      // Too many log-ins at this moment (kittrial-5bb.170): nothing was tried, so the server's own
      // sentence, not "could not confirm whether this was saved".
      const busy = error.status === 503 && error.code === 'busy' && error.message;
      status.textContent = error.status === 401 ? 'That username and password do not match an active account.' : (busy || describe(error));
      status.hidden = false;
      form.querySelector('#password').value = '';
      form.querySelector('#password').focus();
    } finally {
      submit.disabled = false;
    }
  });

  const page = frame(
    h('div', null, h('h1', { id: 'page-title', tabindex: '-1' }, 'Sign in'), h('p', { class: 'muted' }, 'Coordinate tasks, contributions and reviews with your team.')),
    status, form,
    h('p', { class: 'auth-foot' }, 'No account yet? Ask your Orchestra administrator to create one.'),
    ctx.options.loginHint ? ctx.options.loginHint() : null);
  // Once the card is in the page (kittrial-5bb.503).
  setTimeout(() => focusFirstEmpty(form));
  return page;
}

// The password field, with the way to a reset code beside its label: the one place a person
// who cannot sign in looks (kittrial-5bb.525 slice d; it was a sentence under the form).
function passwordField(ctx) {
  // The link follows the input in the document, so Tab goes Username, Password, then the link;
  // the grid in app.css (.field.with-aside) draws it on the label's line.
  const box = field({ id: 'password', label: 'Password', type: 'password', autocomplete: 'current-password', required: true });
  box.setAttribute('class', 'field with-aside');
  const link = h('button', { type: 'button', class: 'link field-aside', 'data-reset-code': '', onclick: () => ctx.root.replaceChildren(redeem(ctx)) }, 'Have a reset code?');
  const parts = Array.from(box.childNodes);
  parts.splice(parts.indexOf(box.querySelector('#password')) + 1, 0, link);
  box.replaceChildren(...parts);
  return box;
}

export function redeem(ctx) {
  const status = h('div', { class: 'banner crit', role: 'alert', hidden: true });
  const form = h('form', { class: 'form', novalidate: true },
    field({ id: 'account', label: 'Account ID', hint: 'Shown next to the reset code your administrator gave you, e.g. usr_…', required: true }),
    field({ id: 'code', label: 'Reset code', autocomplete: 'one-time-code', required: true }),
    field({ id: 'new_password', label: 'New password', type: 'password', autocomplete: 'new-password', hint: passwordHint(ctx.me), required: true }),
    field({ id: 'confirm_password', label: 'Repeat new password', type: 'password', autocomplete: 'new-password', required: true }),
    h('button', { type: 'submit', class: 'primary' }, 'Set password'));
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const v = formValues(form);
    const tooShort = passwordTooShort(ctx.me, v.new_password);
    if (tooShort) return setFieldError(form, 'new_password', tooShort);
    setFieldError(form, 'new_password', '');
    if (v.new_password !== v.confirm_password) return setFieldError(form, 'confirm_password', PASSWORD_MISMATCH);
    setFieldError(form, 'confirm_password', '');
    try {
      await ctx.api.redeemReset(v.account.trim(), v.code.trim(), v.new_password);
      ctx.root.replaceChildren(login(ctx));
    } catch (error) {
      status.textContent = error.status === 401 || error.status === 404 ? 'That reset code is not valid or has expired. Ask your administrator for a new one.' : describe(error);
      status.hidden = false;
    }
  });
  // Once the card is in the page (kittrial-5bb.503).
  setTimeout(() => focusFirstEmpty(form));
  return frame(
    h('h1', { id: 'page-title', tabindex: '-1' }, 'Set a new password'),
    status, form,
    h('p', { class: 'auth-foot' }, h('button', { type: 'button', class: 'link', onclick: () => ctx.root.replaceChildren(login(ctx)) }, 'Back to sign in')));
}
