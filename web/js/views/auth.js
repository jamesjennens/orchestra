import { h, brandMark } from '../dom.js';
import { field, setFieldError, formValues, describe } from '../ui.js';

export function login(ctx, message) {
  const status = h('div', { class: 'banner crit', role: 'alert', hidden: !message }, message || '');
  const submit = h('button', { type: 'submit', class: 'primary' }, 'Sign in');
  const form = h('form', { class: 'form', novalidate: true },
    field({ id: 'username', label: 'Username', autocomplete: 'username', required: true }),
    field({ id: 'password', label: 'Password', type: 'password', autocomplete: 'current-password', required: true }),
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

  const card = h('div', { class: 'auth-card' },
    h('div', { class: 'brand' }, brandMark(), h('span', null, 'Orchestra')),
    h('div', null, h('h1', { id: 'page-title', tabindex: '-1' }, 'Sign in'), h('p', { class: 'muted' }, 'Coordinate tasks, contributions and reviews with your team.')),
    status, form,
    h('p', { class: 'auth-foot' },
      'Given a reset code by an administrator? ',
      h('button', { type: 'button', class: 'link', onclick: () => ctx.root.replaceChildren(redeem(ctx)) }, 'Set a new password'),
      '. No account yet? Ask your Orchestra administrator to create one.'),
    ctx.options.loginHint ? ctx.options.loginHint() : null);
  return h('div', { class: 'auth' }, card);
}

export function redeem(ctx) {
  const status = h('div', { class: 'banner crit', role: 'alert', hidden: true });
  const form = h('form', { class: 'form', novalidate: true },
    field({ id: 'account', label: 'Account ID', hint: 'Shown next to the reset code your administrator gave you, e.g. usr_…', required: true }),
    field({ id: 'code', label: 'Reset code', autocomplete: 'one-time-code', required: true }),
    field({ id: 'new_password', label: 'New password', type: 'password', autocomplete: 'new-password', hint: 'At least 12 characters.', required: true }),
    h('button', { type: 'submit', class: 'primary' }, 'Set password'));
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const v = formValues(form);
    if (String(v.new_password).length < 12) return setFieldError(form, 'new_password', 'Use at least 12 characters.');
    setFieldError(form, 'new_password', '');
    try {
      await ctx.api.redeemReset(v.account.trim(), v.code.trim(), v.new_password);
      ctx.root.replaceChildren(login(ctx));
    } catch (error) {
      status.textContent = error.status === 401 || error.status === 404 ? 'That reset code is not valid or has expired. Ask your administrator for a new one.' : describe(error);
      status.hidden = false;
    }
  });
  return h('div', { class: 'auth' }, h('div', { class: 'auth-card' },
    h('div', { class: 'brand' }, brandMark(), h('span', null, 'Orchestra')),
    h('h1', { id: 'page-title', tabindex: '-1' }, 'Set a new password'),
    status, form,
    h('p', { class: 'auth-foot' }, h('button', { type: 'button', class: 'link', onclick: () => ctx.root.replaceChildren(login(ctx)) }, 'Back to sign in'))));
}
