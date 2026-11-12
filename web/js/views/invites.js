// Invite links (kittrial-5bb.578). Two parts:
//  * invitePanel: on a project's Members & settings page, for a person who may invite to at least one
//    project (an owner of it, or a superuser) while the installation's switch is on. It makes an
//    invite, shows its link ONCE, and lists the person's invites with their status and a Withdraw button.
//  * invitePage: the signed-out page the link opens (#/invite/TOKEN). It shows who invited whom, the
//    projects and the pre-approved agent, takes the password, and then offers the agent's files once.
// The page decides nothing: the server checks who may invite, the switch, the token and the password.
// Every string from the server is put in as text.
import { h, time, confirmDialog, secretDialog, brandMark } from '../dom.js';
import { field, setFieldError, formValues, act, describe, focusFirstEmpty,
         passwordHint, passwordTooShort, PASSWORD_MISMATCH } from '../ui.js';
import { inviteSetupBlock } from './agents.js';

export const STATUS_WORDS = Object.freeze({ pending: 'Pending', used: 'Used', expired: 'Expired', revoked: 'Withdrawn' });
export const LINK_WARNING = 'This link is a key: anyone who holds it can claim the account it names. Send it to that person only, by a private channel. It is shown only now.';

// The link the inviter sends: the service's own public address when it has one, else this page's.
export function inviteLink(result, origin) {
  if (result && typeof result.link === 'string' && result.link) return result.link;
  return String(origin || '') + '/#/invite/' + String((result && result.token) || '');
}

function checkbox(name, value, label, checked) {
  return h('label', { class: 'small', 'data-choice': name + ':' + value },
    h('input', { type: 'checkbox', name, value, checked }), ' ', label);
}

export function invitePanel(ctx, project) {
  const host = h('section', { class: 'panel', 'data-panel': 'invites', hidden: true });

  function form(data) {
    const projects = data.projects || [];
    const agentProject = h('select', { id: 'i-agent-project', name: 'i-agent-project' },
      projects.map((p) => h('option', { value: p.id, selected: p.id === project.id }, p.name)));
    const withAgent = h('input', { type: 'checkbox', id: 'i-agent', name: 'i-agent' });
    const node = h('form', { class: 'panel-body', novalidate: true, 'data-form': 'invite' },
      h('div', { class: 'form-row' },
        field({ id: 'i-name', label: 'Their name', placeholder: 'e.g. Lena Park', required: true, maxlength: 100 }),
        field({ id: 'i-username', label: 'Their username', placeholder: 'e.g. lena', required: true, maxlength: 64 })),
      h('fieldset', { class: 'stack', 'data-field': 'i-projects' },
        h('legend', { class: 'small' }, 'Add them to (as a contributor)'),
        h('div', { class: 'copy-row' }, projects.map((p) => checkbox('i-projects', p.id, p.name, p.id === project.id))),
        h('p', { class: 'small error-text', 'data-error': 'i-projects', hidden: true })),
      h('div', { class: 'form-row' },
        field({ id: 'i-hours', label: 'Expires after (hours)', value: String(data.default_hours || 72),
          hint: `At most ${data.max_hours || 168} hours (7 days).`, required: true })),
      h('fieldset', { class: 'stack' },
        h('legend', { class: 'small' }, h('label', null, withAgent, ' Also pre-approve one worker agent for them')),
        h('div', { class: 'form-row' },
          field({ id: 'i-agent-name', label: 'Agent name', placeholder: 'e.g. Lena build bot', maxlength: 64 }),
          h('label', { class: 'small' }, 'Project ', agentProject),
          field({ id: 'i-agent-limit', label: 'Claim limit', value: '1' })),
        h('p', { class: 'small muted' }, 'A worker only: a coordinator grant is given on Members & settings after they sign in.')),
      h('div', null, h('button', { type: 'submit', class: 'primary' }, 'Make invite link')));
    node.addEventListener('submit', async (event) => {
      event.preventDefault();
      const v = formValues(node);
      const chosen = [...node.querySelectorAll('input[name="i-projects"]:checked')].map((box) => box.value);
      const projectError = node.querySelector('[data-error="i-projects"]');
      if (!v['i-name'].trim()) return setFieldError(node, 'i-name', 'Enter their name.');
      setFieldError(node, 'i-name', '');
      const username = v['i-username'].trim().replace(/^@/, '');
      if (!username) return setFieldError(node, 'i-username', 'Enter a username.');
      setFieldError(node, 'i-username', '');
      projectError.hidden = chosen.length > 0;
      projectError.textContent = chosen.length ? '' : 'Choose at least one project.';
      if (!chosen.length) return;
      const hours = Number(v['i-hours']);
      if (!Number.isInteger(hours) || hours < 1 || hours > (data.max_hours || 168)) return setFieldError(node, 'i-hours', `A whole number from 1 to ${data.max_hours || 168}.`);
      setFieldError(node, 'i-hours', '');
      const body = { display_name: v['i-name'].trim(), username, projects: chosen, expires_hours: hours };
      if (withAgent.checked) {
        const limit = Number(v['i-agent-limit']);
        if (!v['i-agent-name'].trim()) return setFieldError(node, 'i-agent-name', 'Name the agent, or untick the box.');
        setFieldError(node, 'i-agent-name', '');
        if (!chosen.includes(agentProject.value)) return setFieldError(node, 'i-agent-name', 'The agent’s project must be one of the ticked projects.');
        body.agent = { name: v['i-agent-name'].trim(), project: agentProject.value, claim_limit: Number.isInteger(limit) ? limit : v['i-agent-limit'] };
      }
      let result;
      try { result = await act(node.querySelector('button[type=submit]'), () => ctx.api.createInvite(body), { success: 'Invite made' }); } catch { return; }
      if (result && result.token) {
        secretDialog({ title: `Invite link for ${body.display_name}`, body: result.note || LINK_WARNING, secret: inviteLink(result, location.origin) });
      }
      draw();
    });
    return node;
  }

  function list(items) {
    if (!items.length) return [];
    return [h('div', { class: 'table-wrap' }, h('table', null,
      h('thead', null, h('tr', null, h('th', { scope: 'col' }, 'Person'), h('th', { scope: 'col' }, 'Projects'),
        h('th', { scope: 'col' }, 'Status'), h('th', { scope: 'col', class: 'hide-narrow' }, 'Detail'),
        h('th', { scope: 'col' }, h('span', { class: 'visually-hidden' }, 'Actions')))),
      h('tbody', null, items.map((item) => h('tr', { 'data-invite': item.id, 'data-status': item.status },
        h('td', null, h('div', { class: 'title' }, item.display_name), h('div', { class: 'sub' }, '@' + item.username,
          item.agent ? ` · agent ${item.agent.name} in ${item.agent.project}` : '')),
        h('td', null, (item.projects || []).map((p) => p.name).join(', ')),
        h('td', null, h('span', { class: 'chip ' + (item.status === 'pending' ? 'accent' : 'plain') }, STATUS_WORDS[item.status] || String(item.status))),
        h('td', { class: 'hide-narrow small' },
          'Made by ', item.invited_by_name, ' ', time(item.created_at),
          item.status === 'used' ? [' · used ', time(item.used_at), ' by ', item.used_by_name || item.used_by, ' from ', item.used_from || 'an unknown address'] :
            item.status === 'revoked' ? [' · withdrawn ', time(item.revoked_at), ' by ', item.revoked_by_name || item.revoked_by] :
              [' · expires ', time(item.expires_at)]),
        h('td', null, item.status === 'pending' ? h('button', { type: 'button', class: 'danger', onclick: async (e) => {
          if (!(await confirmDialog({ title: `Withdraw the invite for ${item.display_name}?`, body: 'The link stops working at once.', confirmLabel: 'Withdraw', danger: true }))) return;
          try { await act(e.currentTarget, () => ctx.api.revokeInvite(item.id), { success: 'Invite withdrawn' }); } catch { /* shown */ }
          draw();
        } }, 'Withdraw') : ''))))))];
  }

  async function draw() {
    let data;
    try { data = await ctx.api.invites(); } catch { host.hidden = true; host.replaceChildren(); return; }
    const items = data.items || [];
    const canCreate = data.enabled === true && (data.projects || []).length > 0 && !project.archived;
    if (!canCreate && !items.length) { host.hidden = true; host.replaceChildren(); return; }
    host.hidden = false;
    host.replaceChildren(
      h('div', { class: 'panel-head' }, h('h2', { class: 'small' }, 'Invite someone'),
        h('span', { class: 'small muted hide-narrow' }, 'A single-use link makes their account; send it privately')),
      ...(canCreate ? [form(data)] : [h('p', { class: 'panel-body small muted' }, data.off_sentence || 'New invites cannot be made here.')]),
      ...list(items));
  }
  draw();
  return host;
}

// The signed-out page an invite link opens. `token` is the part after #/invite/.
export function invitePage(ctx, token) {
  // The token leaves the address bar (and so the browser history) at once; it lives in this closure only.
  try { history.replaceState(null, '', '#/'); } catch { /* ignore */ }
  const status = h('div', { class: 'banner crit', role: 'alert', hidden: true });
  const body = h('div', { class: 'stack' }, h('p', { class: 'muted' }, 'Reading the invite…'));
  const title = h('h1', { id: 'page-title', tabindex: '-1' }, 'You are invited');
  const card = h('div', { class: 'auth-card', 'data-invite-page': 'true' },
    h('div', { class: 'brand' }, brandMark(), h('span', null, 'Orchestra')), title, status, body);
  const fail = (error) => {
    status.textContent = error && error.message && [404, 403, 409, 422, 429].includes(error.status) ? error.message : describe(error);
    status.hidden = false;
  };
  const toSignIn = h('button', { type: 'button', class: 'link', onclick: async () => {
    try { history.replaceState(null, '', '#/'); } catch { /* ignore */ }
    if (ctx.me) await ctx.signOut(); else ctx.go('/');
  } }, 'Go to sign in');

  function show(invite) {
    const rule = { password_min_length: invite.password_min_length };
    const agent = invite.agent;
    const projectName = (id) => ((invite.projects || []).find((p) => p.id === id) || {}).name || id;
    const form = h('form', { class: 'form', novalidate: true },
      field({ id: 'new_password', label: 'Choose a password', type: 'password', autocomplete: 'new-password', hint: passwordHint(rule), required: true }),
      field({ id: 'confirm_password', label: 'Repeat the password', type: 'password', autocomplete: 'new-password', required: true }),
      h('button', { type: 'submit', class: 'primary' }, 'Create my account'));
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      const v = formValues(form);
      const short = passwordTooShort(rule, v.new_password);
      if (short) return setFieldError(form, 'new_password', short);
      setFieldError(form, 'new_password', '');
      if (v.new_password !== v.confirm_password) return setFieldError(form, 'confirm_password', PASSWORD_MISMATCH);
      setFieldError(form, 'confirm_password', '');
      const button = form.querySelector('button[type=submit]');
      button.disabled = true; status.hidden = true;
      let done;
      try { done = await ctx.api.redeemInvite(token, v.new_password); } catch (error) { fail(error); button.disabled = false; return; }
      // The link is spent: it leaves the address bar, and the secret below lives in this page only.
      try { history.replaceState(null, '', '#/'); } catch { /* ignore */ }
      title.textContent = 'Your account is ready';
      const parts = [h('p', null, `You are @${done.user.username}, a contributor in ${(done.projects || []).map((p) => p.name).join(', ')}. Sign in with your username and the password you just chose.`)];
      if (done.agent && done.credential && done.credential.secret) {
        try { parts.push(await inviteSetupBlock(ctx, done, done.credential.secret)); } catch (error) {
          parts.push(h('p', { class: 'small error-text' }, 'The agent was made, but its files could not be shown: ', describe(error), '. Sign in and use “Set up folder” on the agent’s card to issue a new secret.'));
        }
      }
      parts.push(h('p', { class: 'auth-foot' }, toSignIn));
      body.replaceChildren(...parts);
    });
    body.replaceChildren(
      h('p', null, `${invite.invited_by_name} invites ${invite.display_name} to Orchestra, with the username @${invite.username}.`),
      h('p', { class: 'small' }, 'Projects (as a contributor): ', (invite.projects || []).map((p) => p.name).join(', ')),
      agent ? h('p', { class: 'small', 'data-invite-agent': 'true' }, `A worker agent, “${agent.name}”, is made for you in ${projectName(agent.project)} (claim limit ${agent.claim_limit}), with its credential. Its secret is shown once, on the next screen.`) : '',
      h('p', { class: 'small muted' }, 'This link works once and expires ', time(invite.expires_at), '.'),
      form,
      h('p', { class: 'auth-foot' }, 'Already have an account? ', toSignIn));
    setTimeout(() => focusFirstEmpty(form));
  }

  try { document.title = 'Invitation · Orchestra'; } catch { /* no document */ }
  Promise.resolve().then(() => ctx.api.viewInvite(token)).then(show).catch((error) => {
    body.replaceChildren(h('p', { class: 'auth-foot' }, toSignIn));
    fail(error);
  });
  return h('div', { class: 'auth' }, card);
}
