// The two web places a password is set, run under Node with tests/web_dom_shim.mjs:
// web/js/views/admin.js `account` (change own password) and web/js/views/auth.js `redeem`
// (set a new password from a reset code). kittrial-5bb.363.
//
// argv: shim URL, admin.js URL, auth.js URL. Prints one JSON object of observations; the
// Python test asserts on it. The fake api records every call, so "sends nothing" is a
// counted fact, not an absence of a visible message.
const [shimUrl, adminUrl, authUrl] = process.argv.slice(2);
await import(shimUrl);
globalThis.window = globalThis.window || { isSecureContext: false };
const admin = await import(adminUrl);
const auth = await import(authUrl);

const calls = [];
const PASSWORD_MISMATCH = 'The two new passwords do not match.';

function context(minimum) {
  return {
    me: { id: 'usr_alex', username: 'alex', display_name: 'Alex', superuser: false,
      password_min_length: minimum },
    options: {},
    api: {
      changePassword: async (...args) => { calls.push(['change', ...args]); return { id: 'usr_alex', changed: true }; },
      redeemReset: async (...args) => { calls.push(['redeem', ...args]); return { id: 'usr_alex', password_set: true }; },
    },
    root: document.createElement('div'),
    href: (path) => '#' + path,
    go() {},
  };
}

const formOf = (page) => page.querySelector('form');
const errorOf = (form, id) => {
  const error = form.querySelector('#' + id + '-error');
  return error && !error.hidden ? error.textContent : null;
};
const hintOf = (form, id) => {
  const hint = form.querySelector('#' + id + '-hint');
  return hint ? hint.textContent : null;
};
function fill(form, values) {
  for (const [id, value] of Object.entries(values)) form.querySelector('#' + id).value = value;
}

// -- the account page: change own password -------------------------------------
async function accountPage(minimum) {
  const before = calls.length;
  const ctx = context(minimum);
  const page = await admin.account(ctx);
  const form = formOf(page);
  await form.dispatch('submit');
  return { page, form, ctx, before };
}

const out = {};
{
  const { form, before } = await accountPage(1);
  out.accountHintDefault = hintOf(form, 'next');
  fill(form, { current: 'old', next: '', confirm: '' });
  await form.dispatch('submit');
  out.accountEmpty = errorOf(form, 'next');
  out.accountEmptyCalls = calls.length - before;
  fill(form, { current: 'old', next: 'abc', confirm: 'abd' });
  await form.dispatch('submit');
  out.accountMismatch = errorOf(form, 'confirm');
  out.accountMismatchCalls = calls.length - before;
  fill(form, { current: 'old', next: 'x', confirm: 'x' });
  await form.dispatch('submit');
  out.accountOneCharCalls = calls.length - before;
}
{
  const { form, before } = await accountPage(8);
  out.accountHintRaised = hintOf(form, 'next');
  fill(form, { current: 'old', next: 'short', confirm: 'short' });
  await form.dispatch('submit');
  out.accountTooShort = errorOf(form, 'next');
  out.accountTooShortCalls = calls.length - before;
  fill(form, { current: 'old', next: 'longenough', confirm: 'longenough' });
  await form.dispatch('submit');
  out.accountAtMinimumCalls = calls.length - before;
}

// -- the reset page: set a new password from the single-use code ----------------
async function redeemPage(minimum) {
  const before = calls.length;
  const ctx = context(minimum);
  const page = auth.redeem(ctx);
  const form = formOf(page);
  return { page, form, before };
}

{
  const { form, before } = await redeemPage(1);
  out.redeemHintDefault = hintOf(form, 'new_password');
  fill(form, { account: 'usr_alex', code: 'code', new_password: '', confirm_password: '' });
  await form.dispatch('submit');
  out.redeemEmpty = errorOf(form, 'new_password');
  out.redeemEmptyCalls = calls.length - before;
  fill(form, { account: 'usr_alex', code: 'code', new_password: 'abc', confirm_password: 'abd' });
  await form.dispatch('submit');
  out.redeemMismatch = errorOf(form, 'confirm_password');
  out.redeemMismatchCalls = calls.length - before;
  fill(form, { account: 'usr_alex', code: 'code', new_password: 'x', confirm_password: 'x' });
  await form.dispatch('submit');
  out.redeemOneCharCalls = calls.length - before;
}
{
  const { form, before } = await redeemPage(8);
  out.redeemHintRaised = hintOf(form, 'new_password');
  fill(form, { account: 'usr_alex', code: 'code', new_password: 'short', confirm_password: 'short' });
  await form.dispatch('submit');
  out.redeemTooShort = errorOf(form, 'new_password');
  out.redeemTooShortCalls = calls.length - before;
}

console.log(JSON.stringify(out).replace(/[^\x00-\x7f]/g, (c) => '\\u' + c.charCodeAt(0).toString(16).padStart(4, '0')));
