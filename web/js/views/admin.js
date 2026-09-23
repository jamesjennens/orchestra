import { h, time, confirmDialog, secretDialog, initials } from '../dom.js';
import { pageHead, empty, field, setFieldError, formValues, act, errorState } from '../ui.js';

export async function users(ctx) {
  if (!ctx.me.superuser) {
    return h('div', { class: 'stack' }, pageHead({ title: 'People & access' }), h('div', { class: 'banner crit', role: 'alert' }, 'Only a superuser can manage accounts.'));
  }
  const host = h('div', { class: 'panel' });
  const filter = h('input', { type: 'search', id: 'user-filter', placeholder: 'Filter by name or username', 'aria-label': 'Filter people' });
  let all = [];
  async function load() {
    try { all = (await ctx.api.accounts()).items; } catch (error) { host.replaceChildren(errorState(error, load)); return; }
    draw();
  }
  function draw() {
    const q = filter.value.trim().toLowerCase();
    const rows = all.filter((u) => !q || (u.display_name + ' ' + u.username).toLowerCase().includes(q))
      .sort((a, b) => Number(a.disabled) - Number(b.disabled) || a.display_name.localeCompare(b.display_name));
    host.replaceChildren(rows.length ? h('div', { class: 'table-wrap' }, h('table', null,
      h('thead', null, h('tr', null, h('th', { scope: 'col' }, 'Person'), h('th', { scope: 'col' }, 'Access'), h('th', { scope: 'col', class: 'hide-narrow' }, 'Created'), h('th', { scope: 'col' }, h('span', { class: 'visually-hidden' }, 'Actions')))),
      h('tbody', null, rows.map((u) => h('tr', null,
        h('td', null, h('div', { class: 'toolbar' }, h('span', { class: 'avatar', 'aria-hidden': 'true' }, initials(u.display_name)), h('div', null, h('div', { class: 'title' }, u.display_name), h('div', { class: 'sub' }, '@' + u.username, ' · ', h('code', null, u.id))))),
        h('td', null, u.disabled ? h('span', { class: 'chip crit' }, 'Disabled') : u.superuser ? h('span', { class: 'chip accent' }, 'Superuser') : h('span', { class: 'chip ok' }, 'Active')),
        h('td', { class: 'hide-narrow muted' }, time(u.created_at)),
        h('td', null, u.disabled ? null : h('div', { class: 'actions' },
          h('button', { type: 'button', onclick: async (e) => {
            if (!(await confirmDialog({ title: `Issue a password reset for ${u.display_name}?`, body: 'You will see a single-use code, valid for 30 minutes, to pass to them through a trusted channel. Their current password keeps working until they use it.', confirmLabel: 'Issue reset code' }))) return;
            const r = await act(e.currentTarget, () => ctx.api.issueReset(u.id)).catch(() => null);
            if (r) secretDialog({ title: 'Reset code for ' + u.display_name, body: `Account ID ${u.id}. They choose “Set a new password” on the sign-in page and enter the account ID and this code. Shown once.`, secret: r.reset_value });
          } }, 'Reset password'),
          u.id === ctx.me.id ? null : h('button', { type: 'button', class: 'danger', onclick: async (e) => {
            if (!(await confirmDialog({ title: `Disable ${u.display_name}?`, body: 'They are signed out everywhere and their worker credentials stop working immediately. Their records and past work are kept.', confirmLabel: 'Disable account', danger: true }))) return;
            try { await act(e.currentTarget, () => ctx.api.disableAccount(u.id), { success: 'Account disabled' }); } catch { return; }
            load();
          } }, 'Disable')))))))) : empty('No matching people', null));
  }
  filter.addEventListener('input', draw);
  load();

  const form = h('form', { class: 'form', novalidate: true },
    h('div', { class: 'form-row' },
      field({ id: 'u-username', label: 'Username', hint: 'Lowercase letters, digits, dot, dash or underscore.', required: true }),
      field({ id: 'u-name', label: 'Display name', hint: 'How colleagues will see them.' })),
    h('p', { class: 'small muted' }, 'New accounts have no password. After creating one, issue a reset code so they can set their own.'),
    h('div', null, h('button', { type: 'submit', class: 'primary' }, 'Create account')));
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const v = formValues(form);
    const username = v['u-username'].trim().toLowerCase();
    if (!/^[a-z0-9][a-z0-9._-]{1,63}$/.test(username)) return setFieldError(form, 'u-username', 'Use 2–64 characters: letters, digits, dot, dash or underscore.');
    setFieldError(form, 'u-username', '');
    const created = await act(form.querySelector('button'), () => ctx.api.createAccount(username, v['u-name'].trim() || username), {
      success: 'Account created',
      onError: (e) => { if (e.status === 409 || e.status === 422) { setFieldError(form, 'u-username', e.message); return true; } return false; },
    }).catch(() => null);
    if (created) { form.reset(); load(); }
  });

  return h('div', { class: 'stack' },
    pageHead({ title: 'People & access', lede: 'Create accounts, reset passwords and disable access. Project membership is managed in each project’s settings.' }),
    h('div', { class: 'toolbar' }, filter),
    host,
    h('section', { class: 'panel' }, h('div', { class: 'panel-head' }, h('h2', { class: 'small' }, 'New account')), h('div', { class: 'panel-body' }, form)));
}

export async function account(ctx) {
  const status = h('div', { class: 'banner info', role: 'status', hidden: true });
  const form = h('form', { class: 'form', novalidate: true },
    status,
    field({ id: 'current', label: 'Current password', type: 'password', autocomplete: 'current-password', required: true }),
    field({ id: 'next', label: 'New password', type: 'password', autocomplete: 'new-password', hint: 'At least 12 characters. A short sentence is easier to remember.', required: true }),
    field({ id: 'confirm', label: 'Repeat new password', type: 'password', autocomplete: 'new-password', required: true }),
    h('div', null, h('button', { type: 'submit', class: 'primary' }, 'Change password')));
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const v = formValues(form);
    if (v.next.length < 12) return setFieldError(form, 'next', 'Use at least 12 characters.');
    setFieldError(form, 'next', '');
    if (v.next !== v.confirm) return setFieldError(form, 'confirm', 'The two new passwords do not match.');
    setFieldError(form, 'confirm', '');
    const done = await act(form.querySelector('button'), () => ctx.api.changePassword(ctx.me.id, v.current, v.next), {
      onError: (e) => { if (e.status === 401) { setFieldError(form, 'current', 'That is not your current password.'); return true; } if (e.status === 422) { setFieldError(form, 'next', e.message); return true; } return false; },
    }).catch(() => null);
    if (done) { form.reset(); status.textContent = 'Password changed. Other sessions stay signed in until they expire.'; status.hidden = false; }
  });
  return h('div', { class: 'stack' },
    pageHead({ title: 'Account', lede: `Signed in as @${ctx.me.username || ctx.me.id}${ctx.me.superuser ? ' · superuser' : ''}.` }),
    h('section', { class: 'panel' }, h('div', { class: 'panel-head' }, h('h2', { class: 'small' }, 'Change password')), h('div', { class: 'panel-body' }, form)));
}
