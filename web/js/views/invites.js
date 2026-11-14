// Invite links (kittrial-5bb.578). Two parts:
//  * invitePanel: on a project's People page, for a person who may invite to at least one
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

// kittrial-5bb.581: the roles an invite to an existing account may name (Owner only for a superuser), and the
// agent roles an invite may pre-approve.
export const INVITE_ROLES = Object.freeze([['viewer', 'Viewer'], ['contributor', 'Contributor'], ['coordinator', 'Coordinator'], ['owner', 'Owner']]);
export const AGENT_ROLES = Object.freeze([['worker', 'Worker'], ['reviewer', 'Reviewer']]);
export const PANEL_TITLE = 'Invite someone or add an agent';
export const AGENT_NOTE = 'A worker or reviewer only: a coordinator grant is given on the project’s Agents page after they sign in.';
export const ROLE_KEPT_HINT = 'A higher role they already have is kept.';
const ROLE_RANK = Object.freeze({ viewer: 1, contributor: 2, coordinator: 3, owner: 4 });

// What a used invite to an existing account did, one line per project: added, raised or kept.
export function membershipLines(item) {
  const names = new Map((item.projects || []).map((p) => [p.id, p.name]));
  return Object.entries(item.memberships || {}).map(([pid, change]) => {
    const name = names.get(pid) || pid;
    const before = change && change.before;
    const after = change && change.after;
    if (!before) return `added as ${after} in ${name}`;
    if (before === after) return `kept ${after} in ${name}`;
    return `raised from ${before} to ${after} in ${name}`;
  });
}

const inputsNamed = (node, name) => Array.from(node.querySelectorAll('input')).filter((input) => input.getAttribute('name') === name);

export function invitePanel(ctx, project) {
  const host = h('section', { class: 'panel', 'data-panel': 'invites', tabindex: '-1', hidden: true });
  // #/p/{pid}/people?invite=1 (the "Invite someone or add an agent" action): focus the panel once it is drawn.
  let focusPending = Boolean(ctx.query && typeof ctx.query.get === 'function' && ctx.query.get('invite') === '1');
  const superuser = Boolean(ctx.me && ctx.me.superuser);

  function form(data) {
    const projects = data.projects || [];
    const agentProject = h('select', { id: 'i-agent-project', name: 'i-agent-project' },
      projects.map((p) => h('option', { value: p.id, selected: p.id === project.id }, p.name)));
    const withAgent = h('input', { type: 'checkbox', id: 'i-agent', name: 'i-agent' });
    const personNew = h('input', { type: 'radio', id: 'i-person-new', name: 'i-person', value: 'new', checked: true });
    const personExisting = h('input', { type: 'radio', id: 'i-person-existing', name: 'i-person', value: 'existing' });
    const newFields = h('div', { class: 'form-row', 'data-person-fields': 'new' },
      field({ id: 'i-name', label: 'Their name', placeholder: 'e.g. Lena Park', required: true, maxlength: 100 }),
      field({ id: 'i-username', label: 'Their username', placeholder: 'e.g. lena', required: true, maxlength: 64 }));
    const roles = INVITE_ROLES.filter(([value]) => value !== 'owner' || superuser);
    const existingFields = h('div', { class: 'form-row', 'data-person-fields': 'existing', hidden: true },
      field({ id: 'i-existing-username', label: 'Their username', placeholder: 'e.g. lena', required: true, maxlength: 64 }),
      field({ id: 'i-role', label: 'Their role', type: 'select', value: 'contributor', options: roles, hint: ROLE_KEPT_HINT }));
    const projectError = h('p', { class: 'small error-text', 'data-error': 'i-projects', hidden: true });
    const projectsLegend = h('legend', { class: 'small' }, 'Add them to (as a contributor)');
    const existing = () => personExisting.checked === true;
    const choose = () => {
      newFields.hidden = existing();
      existingFields.hidden = !existing();
      projectsLegend.textContent = existing() ? 'Add them to (in the role above)' : 'Add them to (as a contributor)';
    };
    personNew.addEventListener('change', choose);
    personExisting.addEventListener('change', choose);
    const node = h('form', { class: 'panel-body', novalidate: true, 'data-form': 'invite' },
      h('fieldset', { class: 'stack', 'data-field': 'i-person' },
        h('legend', { class: 'small' }, 'Who'),
        h('div', { class: 'copy-row' },
          h('label', { class: 'small', 'data-choice': 'i-person:new' }, personNew, ' Someone new'),
          h('label', { class: 'small', 'data-choice': 'i-person:existing' }, personExisting, ' Someone already on Orchestra'))),
      newFields,
      existingFields,
      h('fieldset', { class: 'stack', 'data-field': 'i-projects' },
        projectsLegend,
        h('div', { class: 'copy-row' }, projects.map((p) => checkbox('i-projects', p.id, p.name, p.id === project.id))),
        projectError),
      h('div', { class: 'form-row' },
        field({ id: 'i-hours', label: 'Expires after (hours)', value: String(data.default_hours || 72),
          hint: `At most ${data.max_hours || 168} hours (7 days).`, required: true })),
      h('fieldset', { class: 'stack' },
        h('legend', { class: 'small' }, h('label', null, withAgent, ' Also add an agent for them')),
        h('div', { class: 'form-row' },
          field({ id: 'i-agent-name', label: 'Agent name', placeholder: 'e.g. Lena build bot', maxlength: 64 }),
          h('label', { class: 'small' }, 'Project ', agentProject),
          field({ id: 'i-agent-role', label: 'Agent role', type: 'select', value: 'worker', options: AGENT_ROLES }),
          field({ id: 'i-agent-limit', label: 'Claim limit', value: '1' })),
        h('p', { class: 'small muted' }, AGENT_NOTE)),
      h('div', null, h('button', { type: 'submit', class: 'primary' }, 'Make invite link')));
    node.addEventListener('submit', async (event) => {
      event.preventDefault();
      const v = formValues(node);
      const chosen = inputsNamed(node, 'i-projects').filter((box) => box.checked).map((box) => box.value || box.getAttribute('value'));
      const forExisting = existing();
      let body;
      if (forExisting) {
        const username = (v['i-existing-username'] || '').trim().replace(/^@/, '');
        if (!username) return setFieldError(node, 'i-existing-username', 'Enter their username.');
        setFieldError(node, 'i-existing-username', '');
        body = { person: 'existing', username, role: v['i-role'] || 'contributor' };
      } else {
        if (!v['i-name'].trim()) return setFieldError(node, 'i-name', 'Enter their name.');
        setFieldError(node, 'i-name', '');
        const username = v['i-username'].trim().replace(/^@/, '');
        if (!username) return setFieldError(node, 'i-username', 'Enter a username.');
        setFieldError(node, 'i-username', '');
        body = { display_name: v['i-name'].trim(), username };
      }
      projectError.hidden = chosen.length > 0;
      projectError.textContent = chosen.length ? '' : 'Choose at least one project.';
      if (!chosen.length) return;
      const hours = Number(v['i-hours']);
      if (!Number.isInteger(hours) || hours < 1 || hours > (data.max_hours || 168)) return setFieldError(node, 'i-hours', `A whole number from 1 to ${data.max_hours || 168}.`);
      setFieldError(node, 'i-hours', '');
      body.projects = chosen;
      body.expires_hours = hours;
      if (withAgent.checked) {
        const limit = Number(v['i-agent-limit']);
        if (!v['i-agent-name'].trim()) return setFieldError(node, 'i-agent-name', 'Name the agent, or untick the box.');
        setFieldError(node, 'i-agent-name', '');
        if (!chosen.includes(agentProject.value)) return setFieldError(node, 'i-agent-name', 'The agent’s project must be one of the ticked projects.');
        body.agent = { name: v['i-agent-name'].trim(), project: agentProject.value, role: v['i-agent-role'] || 'worker',
          claim_limit: Number.isInteger(limit) ? limit : v['i-agent-limit'] };
      }
      let result;
      try { result = await act(node.querySelector('button[type=submit]'), () => ctx.api.createInvite(body), { success: 'Invite made' }); } catch { return; }
      if (result && result.token) {
        secretDialog({ title: `Invite link for ${forExisting ? '@' + body.username : body.display_name}`, body: result.note || LINK_WARNING, secret: inviteLink(result, location.origin) });
      }
      draw();
    });
    return node;
  }

  function person(item) {
    if (item.person === 'existing') {
      return h('td', null, h('div', { class: 'title' }, '@' + item.username + ' (existing account)'),
        h('div', { class: 'sub' }, item.role ? 'as ' + item.role : '', item.agent ? `${item.role ? ' · ' : ''}agent ${item.agent.name} in ${item.agent.project}` : ''));
    }
    return h('td', null, h('div', { class: 'title' }, item.display_name), h('div', { class: 'sub' }, '@' + item.username,
      item.agent ? ` · agent ${item.agent.name} in ${item.agent.project}` : ''));
  }

  function list(items) {
    if (!items.length) return [];
    return [h('div', { class: 'table-wrap' }, h('table', null,
      h('thead', null, h('tr', null, h('th', { scope: 'col' }, 'Person'), h('th', { scope: 'col' }, 'Projects'),
        h('th', { scope: 'col' }, 'Status'), h('th', { scope: 'col', class: 'hide-narrow' }, 'Detail'),
        h('th', { scope: 'col' }, h('span', { class: 'visually-hidden' }, 'Actions')))),
      h('tbody', null, items.map((item) => h('tr', { 'data-invite': item.id, 'data-status': item.status, 'data-person': item.person || 'new' },
        person(item),
        h('td', null, (item.projects || []).map((p) => p.name).join(', '),
          item.person === 'existing' && item.status === 'used' ? membershipLines(item).map((line) => h('div', { class: 'sub', 'data-membership': 'true' }, line)) : null),
        h('td', null, h('span', { class: 'chip ' + (item.status === 'pending' ? 'info' : 'plain') }, STATUS_WORDS[item.status] || String(item.status))),
        h('td', { class: 'hide-narrow small' },
          'Made by ', item.invited_by_name, ' ', time(item.created_at),
          item.status === 'used' ? [' · used ', time(item.used_at), ' by ', item.used_by_name || item.used_by, ' from ', item.used_from || 'an unknown address'] :
            item.status === 'revoked' ? [' · withdrawn ', time(item.revoked_at), ' by ', item.revoked_by_name || item.revoked_by] :
              [' · expires ', time(item.expires_at)]),
        h('td', null, item.status === 'pending' ? h('button', { type: 'button', class: 'danger', onclick: async (e) => {
          const who = item.person === 'existing' ? '@' + item.username : item.display_name;
          if (!(await confirmDialog({ title: `Withdraw the invite for ${who}?`, body: 'The link stops working at once.', confirmLabel: 'Withdraw', danger: true }))) return;
          try { await act(e.currentTarget, () => ctx.api.revokeInvite(item.id), { success: 'Invite withdrawn' }); } catch { /* shown */ }
          draw();
        } }, 'Withdraw') : ''))))))];
  }

  function focusPanel() {
    if (!focusPending) return;
    focusPending = false;
    // After the page has been placed (app.js focuses its title first).
    setTimeout(() => {
      try { if (typeof host.scrollIntoView === 'function') host.scrollIntoView({ block: 'start' }); } catch { /* ignore */ }
      const first = host.querySelector('input');
      (first || host).focus();
    });
  }

  async function draw() {
    let data;
    try { data = await ctx.api.invites(); } catch { host.hidden = true; host.replaceChildren(); return; }
    const items = data.items || [];
    // kittrial-5bb.581: a person who may invite sees the panel while the switch is off too, with the sentence
    // that says so; it is hidden only from a person who may not invite and has no invites of their own.
    const mayInvite = data.may_invite === true;
    const canCreate = data.enabled === true && mayInvite && (data.projects || []).length > 0 && !project.archived;
    if (!mayInvite && !items.length) { host.hidden = true; host.replaceChildren(); return; }
    host.hidden = false;
    const why = data.enabled !== true ? (data.off_sentence || 'New invites cannot be made here.')
      : project.archived ? 'This project is archived: no new invites are made from it.' : 'New invites cannot be made here.';
    host.replaceChildren(
      h('div', { class: 'panel-head' }, h('h2', { class: 'small' }, PANEL_TITLE),
        h('span', { class: 'small muted hide-narrow' }, 'A single-use link; send it privately')),
      ...(canCreate ? [form(data)] : [h('p', { class: 'panel-body small muted', 'data-invites-off': 'true' }, why)]),
      ...list(items));
    focusPanel();
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

  // kittrial-5bb.581: an invite to an account that already exists. Its holder signs in (or is signed in) as that
  // account and accepts. Nothing here says, or lets anyone infer, whether the account is already a member.
  function showExisting(invite) {
    const agent = invite.agent;
    const projectNames = (invite.projects || []).map((p) => p.name).join(', ');
    const named = String(invite.username || '');
    const slot = h('div', { class: 'stack', 'data-invite-action': 'true' });
    const isNamed = () => Boolean(ctx.me && String(ctx.me.username || '').toLowerCase() === named.toLowerCase());

    function signInForm(message) {
      const form = h('form', { class: 'form', novalidate: true, 'data-form': 'invite-sign-in' },
        h('div', { class: 'field' }, h('label', { for: 'invite_username' }, 'Username'),
          h('input', { id: 'invite_username', name: 'invite_username', type: 'text', value: named, readonly: true, autocomplete: 'username' })),
        field({ id: 'invite_password', label: 'Password', type: 'password', autocomplete: 'current-password', required: true }),
        h('button', { type: 'submit', class: 'primary' }, 'Sign in and accept'));
      form.addEventListener('submit', async (event) => {
        event.preventDefault();
        const v = formValues(form);
        if (!v.invite_password) return setFieldError(form, 'invite_password', 'Enter your password.');
        setFieldError(form, 'invite_password', '');
        const button = form.querySelector('button[type=submit]');
        button.disabled = true; status.hidden = true;
        let user;
        try { user = await ctx.api.login(named, v.invite_password); } catch (error) { fail(error); button.disabled = false; return; }
        // Signed in on this page: the invite is still open here (ctx.signedIn would navigate away from it).
        ctx.me = user;
        await accept(button);
      });
      const parts = [];
      if (message) parts.push(h('p', { class: 'small' }, message));
      parts.push(h('p', { class: 'small' }, `Sign in as @${named} to accept.`), form);
      slot.replaceChildren(...parts);
      setTimeout(() => focusFirstEmpty(form));
    }

    function redraw(message) {
      status.hidden = true;
      if (!ctx.me) return signInForm(message);
      if (isNamed()) {
        const button = h('button', { type: 'button', class: 'primary', 'data-action': 'accept-invite', onclick: () => accept(button) }, 'Accept');
        slot.replaceChildren(button);
        return undefined;
      }
      slot.replaceChildren(
        h('p', { 'data-invite-other': 'true' }, `This invite is for @${named}. Sign out and sign in as @${named} to accept it.`),
        h('button', { type: 'button', 'data-action': 'invite-sign-out', onclick: async () => {
          try { await ctx.api.logout(); } catch { /* the session is gone either way */ }
          ctx.me = null; ctx.projects = [];
          redraw();
        } }, 'Sign out'));
      return undefined;
    }

    async function accept(button) {
      if (button) button.disabled = true;
      status.hidden = true;
      let done;
      try { done = await ctx.api.acceptInvite(token); } catch (error) {
        if (button) button.disabled = false;
        if (error && error.status === 401) { ctx.me = null; return redraw('Your session has ended. Sign in again to accept.'); }
        return fail(error);
      }
      try { history.replaceState(null, '', '#/'); } catch { /* ignore */ }
      title.textContent = 'Done';
      const ranked = (role) => ROLE_RANK[role] || 0;
      const lines = (done.projects || []).map((p) => {
        const text = !p.role_before ? `Added as ${p.role} in ${p.name}`
          : ranked(p.role_before) < ranked(p.role) ? `Raised from ${p.role_before} to ${p.role} in ${p.name}`
            : `You keep your ${p.role} role in ${p.name}`;
        return h('li', { 'data-accepted-project': p.id }, text);
      });
      const parts = [h('ul', { class: 'stack', 'data-accepted': 'true' }, lines)];
      if (done.agent && done.credential && done.credential.secret) {
        try { parts.push(await inviteSetupBlock(ctx, done, done.credential.secret)); } catch (error) {
          parts.push(h('p', { class: 'small error-text' }, 'The agent was made, but its files could not be shown: ', describe(error), '. Use “Set up folder” on the agent’s card to issue a new secret.'));
        }
      }
      const first = (done.projects || [])[0];
      if (first) {
        const route = `/p/${first.id}`;
        parts.push(h('p', { class: 'auth-foot' }, h('a', { href: ctx.href ? ctx.href(route) : '#' + route, 'data-link': 'invite-first-project', onclick: async (e) => {
          e.preventDefault();
          if (ctx.refreshProjects) { try { await ctx.refreshProjects(); } catch { /* the page reads them again */ } }
          ctx.go(route);
        } }, `Go to ${first.name}`)));
      }
      body.replaceChildren(...parts);
      return done;
    }

    body.replaceChildren(
      h('p', { 'data-invite-intro': 'true' }, `${invite.invited_by_name} invites @${named} to ${projectNames}`,
        agent ? ` and adds a ${agent.role || 'worker'} agent “${agent.name}” for you` : '', '.'),
      h('p', { class: 'small muted' }, 'This link works once and expires ', time(invite.expires_at), '.'),
      slot);
    redraw();
  }

  function show(invite) {
    if (invite && invite.person === 'existing') return showExisting(invite);
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
      agent ? h('p', { class: 'small', 'data-invite-agent': 'true' }, `A ${agent.role || 'worker'} agent, “${agent.name}”, is made for you in ${projectName(agent.project)} (claim limit ${agent.claim_limit}), with its credential. Its secret is shown once, on the next screen.`) : '',
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
