// Personal agents: each belongs to the person who runs it. Nothing polls; when the
// owner opens this page they see which agent has something to do and which folder to
// open in VS Code to resume it. Agents themselves read work over the REST API.
import { h, time, toast, copyText, copyButton, confirmDialog } from '../dom.js';
import { pageHead, empty, field, setFieldError, formValues, act, errorState } from '../ui.js';
import * as setupText from '../agentSetup.js';
import { expiryLine, extensionButton } from '../credentialLifetime.js';
import { SESSION_IDLE_WARNING_EVENT } from '../api.js';

export const ATTENTION = {
  feedback: ['Has feedback to act on', 'warn'],
  idle: ['Could take more work', 'accent'],
  working: ['Working', ''],
  waiting: ['Waiting for review', 'ok'],
  blocked: ['Blocked', 'crit'],
};

// The server reports attention as { state, summary, counts }; the prototype's mock
// uses a flat word. One card shape serves both.
const SERVER_ATTENTION = { 'changes-requested': 'feedback', blocked: 'blocked', 'waiting-review': 'waiting', 'waiting-integration': 'waiting', working: 'working', idle: 'idle' };
export function attentionOf(agent) {
  if (typeof agent.attention === 'string') return agent.attention;
  return SERVER_ATTENTION[agent.attention && agent.attention.state] || 'idle';
}

function normalize(ctx, agent) {
  if (typeof agent.attention === 'string') return agent;
  const byId = Object.fromEntries((ctx.projects || []).map((p) => [p.id, p]));
  const attention = agent.attention || {};
  return {
    ...agent,
    server: true,
    display_name: agent.display_name || agent.name,
    attention: attentionOf(agent),
    summary: attention.summary || null,
    claimable: (attention.counts && attention.counts.claimable) || 0,
    items: agent.items || [],
    projects: (agent.projects || []).map((p) => (typeof p === 'string' ? { id: p, name: (byId[p] || {}).name || p, role: 'granted' } : p)),
    disabled: agent.enabled === false,
  };
}

export function attentionChip(state) {
  const [label, tone] = ATTENTION[state] || [state, ''];
  return h('span', { class: 'chip ' + tone }, label);
}

async function copy(text, what) {
  if (await copyText(text)) toast(what + ' copied'); else toast('Select the text and press Ctrl+C to copy');
}

// The same instruction in both modes: the setup dialog writes .orchestra/AGENT.md and
// this prompt tells the agent to read exactly that file.
export function resumePrompt(agent) {
  return setupText.resumePrompt(agent.display_name || agent.name);
}

// Only the agent's owner (or a superuser) may set up its folder or issue it a secret;
// the server enforces the same rule (a non-owner gets 404 from /v1/agents/{id}).
function ownerId(raw) {
  return raw.owner || raw.owner_id || raw.agent_of;
}

function ownedBy(ctx, raw) {
  return Boolean(ctx.me && ownerId(raw) === ctx.me.id);
}

function manages(ctx, raw) {
  const owner = ownerId(raw);
  return Boolean(ctx.me && (ctx.me.superuser || owner === ctx.me.id));
}

// What an agent may do (kittrial-5bb.208). The server keeps it on the agent record: `scopes`, and in
// `scopes_source` where that is known from (`set`, `inferred` from its working credentials for an agent
// made before the record kept it, or `unknown`: scopes is null and a new secret needs a choice).
// The per-agent claim limit's editable bounds (kittrial-5bb.276), kept beside the
// service's own claim_limit.LIMIT_MIN / LIMIT_MAX so the field cannot offer a value
// the route would refuse.
export const CLAIM_LIMIT_MIN = 1;
export const CLAIM_LIMIT_MAX = 50;

export const SCOPES = [['read', 'Read (every agent can read)'], ['tasks', 'Claim and change tasks'], ['checkpoints', 'Write checkpoints'],
  ['reviews', 'Deliver and review (needed to deliver at all)'], ['feedback', 'Send feedback'], ['proposals', 'Propose requirements']];
const scopeNames = (scopes) => (Array.isArray(scopes) && scopes.length ? scopes.join(', ') : 'nothing beyond reading');
const sameScopes = (a, b) => Array.isArray(a) && Array.isArray(b) && a.length === b.length && a.every((s) => b.includes(s));
const credentialsOf = (raw) => (Array.isArray(raw.credentials) ? raw.credentials.filter((c) => c && typeof c === 'object') : []);
// An older service sends no `working`: then a credential that is not revoked counts.
const works = (c) => (c.working === undefined ? !c.revoked : Boolean(c.working));
// The working credentials that allow something else than the agent's scopes. With scopes unknown: every
// working credential when they do not all allow the same.
export function otherCredentials(raw) {
  const working = credentialsOf(raw).filter(works);
  if (Array.isArray(raw.scopes)) return working.filter((c) => !sameScopes(c.scopes, raw.scopes));
  return working.some((c) => !sameScopes(c.scopes, working[0].scopes)) ? working : [];
}
// The card's line. It says what is so and blames nobody: an account may have narrowed its agent on
// purpose and left the older credential working.
export function scopesLine(raw) {
  if (raw.scopes === undefined && raw.scopes_source === undefined) return null;         // a service that does not say
  const known = Array.isArray(raw.scopes);
  const other = otherCredentials(raw).length;
  return h('div', { class: 'small', 'data-scopes': known ? raw.scopes.join(' ') : 'unknown', 'data-scopes-source': raw.scopes_source || '' },
    known ? ['May: ', scopeNames(raw.scopes), raw.scopes_source === 'inferred' ? ' (inferred from working credentials, not confirmed: revocation or expiry may have left an unintended credential working; its own account should check these)' : null, '.'] :
      'Nothing says what this agent may do. Choose under “What it may do” on the Agents page before it gets a new secret.',
    other ? h('div', { class: 'banner', role: 'status', 'data-scopes-differ': String(other) },
      known ? `${other} working ${other === 1 ? 'credential allows' : 'credentials allow'} something else than that.` : 'Its working credentials do not all allow the same.',
      ' “What it may do” on the Agents page shows each one and can revoke it.') : null);
}

// One agent's card: status, what is waiting, and exactly where to go to resume it.
export function agentCard(ctx, raw, { compact = false } = {}) {
  const agent = normalize(ctx, raw);
  const folder = agent.working_directory;
  return h('article', { class: 'agent-card' + (agent.attention === 'feedback' ? ' needs' : '') },
    h('div', { class: 'agent-head' },
      h('div', null, h('h3', null, agent.display_name), h('div', { class: 'small muted' }, agent.tool || 'Agent', agent.last_seen_at ? [' · last active ', time(agent.last_seen_at)] : ' · not connected yet')),
      attentionChip(agent.attention)),
    agent.items && agent.items.length ? h('ul', { class: 'agent-items' }, agent.items.map((i) => h('li', null,
      h('a', { href: ctx.href(`/p/${i.project_id}/t/${i.task_id}`) }, i.title), h('span', { class: 'small muted' }, ' — ', i.text)))) :
      agent.summary ? h('p', { class: 'small muted' }, agent.summary) :
      agent.attention === 'idle' ? h('p', { class: 'small muted' }, `${agent.claimable} unclaimed task(s) in its projects.`) : null,
    h('div', { class: 'resume' },
      h('div', { class: 'resume-row' },
        h('span', { class: 'resume-label' }, 'Folder'),
        folder ? h('code', { class: 'path', title: 'Open this folder in VS Code' }, folder) : h('span', { class: 'muted small' }, 'Not recorded'),
        folder ? h('button', { type: 'button', class: 'ghost', onclick: () => copy(folder, 'Folder path') }, 'Copy') : null),
      compact ? null : h('div', { class: 'resume-row' },
        h('span', { class: 'resume-label' }, 'Resume prompt (after setup)'),
        h('span', { class: 'small prompt' }, resumePrompt(agent)),
        h('button', { type: 'button', class: 'ghost', 'aria-label': 'Copy the resume prompt', onclick: () => copy(resumePrompt(agent), 'Resume prompt') }, 'Copy')),
      compact ? null : h('p', { class: 'small muted resume-hint' }, 'Needs .orchestra/agent.json in the folder; use Set up folder first.')),
    raw.created_by && raw.created_by !== raw.owner ? h('p', { class: 'small muted', 'data-created-by': raw.created_by },
      'Created by ', raw.created_by_display_name || raw.created_by, '. That superuser saw the first secret. Issue a new secret if this matters to you.') : null,
    credentialsOf(raw).some((c) => c.expires_soon && works(c)) ? h('div', { class: 'banner warn', role: 'status' }, 'A credential expires within seven days. Extend it under What it may do; its existing secret keeps working.') : null,
    compact ? null : scopesLine(raw),
    manages(ctx, raw) ? h('div', { class: 'copy-row' },
      h('button', { type: 'button', 'aria-label': 'Set up the folder for ' + agent.display_name, onclick: (event) => reopenSetup(ctx, raw.id, event.currentTarget) }, 'Set up folder')) : null,
    compact ? null : h('div', { class: 'small muted' }, 'Projects: ', agent.projects.map((p) => `${p.name} (${p.role})`).join(', ') || 'none'));
}

export async function list(ctx) {
  const host = h('div', { class: 'stack' });
  let unconfirmedOnly = false;
  let cursor = null;
  const previous = [];
  const filter = ctx.me && ctx.me.superuser ? h('label', { class: 'check' },
    h('input', { type: 'checkbox', 'data-filter': 'unconfirmed-scopes', onchange: (event) => {
      unconfirmedOnly = event.target.checked;
      cursor = null;
      previous.length = 0;
      load();
    } }), 'Show agents with inferred or unknown scopes') : null;
  async function load() {
    let data;
    try { data = await ctx.api.agents({ limit: 20, cursor, unconfirmed: unconfirmedOnly ? 'true' : 'false' }); } catch (error) { host.replaceChildren(errorState(error, load)); return; }
    const items = data.items;
    host.replaceChildren(items.length ? h('div', { class: 'agent-grid' }, items.map((a) => h('div', { class: 'stack', 'data-agent': a.id }, agentCard(ctx, a), manages(ctx, a) ? editClaimLimit(a) : null, editFolder(a), manages(ctx, a) ? editProjects(a) : null, manages(ctx, a) ? editScopes(a) : null))) :
      h('div', { class: 'panel' }, empty('No agents yet', 'Add an agent for each assistant you run — for example a GitHub Copilot chat working in its own folder.')));
    host.append(h('div', { class: 'copy-row', 'data-agent-pages': '' },
      h('span', { class: 'small muted' }, `${items.length} shown of ${data.total} agents`),
      previous.length ? h('button', { type: 'button', onclick: () => { cursor = previous.pop(); load(); } }, 'Previous agents') : null,
      data.next_cursor ? h('button', { type: 'button', onclick: () => { previous.push(cursor); cursor = data.next_cursor; load(); } }, 'Next agents') : null));
  }
  // The per-agent limit on claimed, undelivered tasks (kittrial-5bb.276). The server
  // refuses a claim past it, so this is where an owner raises it; leaving the box empty
  // clears the setting back to the default. Only the owner (and an administrator) sees
  // this card at all, and the server judges it again on the write.
  function editClaimLimit(agent) {
    const details = h('details', { class: 'deliver' });
    const form = h('form', { class: 'form', novalidate: true },
      field({ id: 'cl-' + agent.id, label: 'Claims it may hold at once',
              type: 'number', value: String(agent.claim_limit == null ? '' : agent.claim_limit),
              hint: `Tasks it has claimed and not yet delivered. ${CLAIM_LIMIT_MIN} to ${CLAIM_LIMIT_MAX}; empty uses the default of 1.` }),
      h('div', null, h('button', { type: 'submit' }, 'Save limit')));
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      const raw = formValues(form)['cl-' + agent.id].trim();
      const clear = form.querySelector('#cl-' + agent.id + '-error');
      clear.hidden = true;
      if (raw !== '' && !/^\d+$/.test(raw)) {
        setFieldError(form, 'cl-' + agent.id, `Enter a whole number from ${CLAIM_LIMIT_MIN} to ${CLAIM_LIMIT_MAX}, or leave it empty.`);
        return;
      }
      const value = raw === '' ? null : Number(raw);
      if (value !== null && (value < CLAIM_LIMIT_MIN || value > CLAIM_LIMIT_MAX)) {
        setFieldError(form, 'cl-' + agent.id, `Enter a whole number from ${CLAIM_LIMIT_MIN} to ${CLAIM_LIMIT_MAX}, or leave it empty.`);
        return;
      }
      const saved = await act(form.querySelector('button'), () => ctx.api.updateAgent(agent.id, { claim_limit: value }), { success: 'Limit saved' }).catch(() => null);
      if (saved) load();
    });
    details.append(h('summary', null, 'Change claim limit'), form);
    return details;
  }

  function editFolder(agent) {
    const details = h('details', { class: 'deliver' });
    const form = h('form', { class: 'form', novalidate: true },
      field({ id: 'wd-' + agent.id, label: 'Folder on your computer', value: agent.working_directory || '', hint: 'Only you and administrators can see this.' }),
      h('div', null, h('button', { type: 'submit' }, 'Save folder')));
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      const value = formValues(form)['wd-' + agent.id].trim();
      const saved = await act(form.querySelector('button'), () => ctx.api.updateAgent(agent.id, { working_directory: value }), { success: 'Folder saved' }).catch(() => null);
      if (saved) load();
    });
    details.append(h('summary', null, 'Change folder'), form);
    return details;
  }
  // Grant or withdraw projects. An agent with none gets only a "no projects yet" note
  // on My work, so this is where that note sends its owner. The server applies the
  // whole change or none of it, and refuses a project the agent's owner cannot open.
  function editProjects(agent) {
    const granted = new Set((agent.projects || []).map((p) => (typeof p === 'string' ? p : p.id)));
    // For an agent this viewer does not own, the list is the OWNER's, not the viewer's
    // (kittrial-5bb.355): the server sends the projects the owner may be granted,
    // limited to the projects this viewer can already see. The rest are listed greyed
    // with the move that fixes it. An older service sends none: keep the old list.
    const ownerProjects = !ownedBy(ctx, agent) && Array.isArray(agent.owner_grantable_projects)
      ? new Set(agent.owner_grantable_projects) : null;
    const choices = ownerProjects ? grantable().filter((p) => ownerProjects.has(p.id)) : grantable();
    const blocked = ownerProjects ? grantable().filter((p) => !ownerProjects.has(p.id)) : [];
    const ownerName = agent.owner_display_name || ownerId(agent);
    const details = h('details', { class: 'deliver', open: granted.size ? null : true });
    const form = h('form', { class: 'form', novalidate: true },
      h('fieldset', { class: 'checks' }, h('legend', null, 'Projects it can work on'),
        choices.length ? choices.map((p) => h('label', { class: 'check', for: `gp-${agent.id}-${p.id}` },
          h('input', { type: 'checkbox', id: `gp-${agent.id}-${p.id}`, name: 'project', value: p.id, checked: granted.has(p.id) ? true : null }), p.name)) :
          (blocked.length ? null : h('p', { class: 'small muted' }, 'You need contributor access to a project first.')),
        blocked.map((p) => h('label', { class: 'check muted', 'data-owner-not-member': p.id },
          h('input', { type: 'checkbox', disabled: true, 'aria-label': `${p.name} (not available to ${ownerName})` }),
          h('span', null, `${ownerName} is not a member of ${p.name}: `,
            ctx.me && (ctx.me.superuser || p.role === 'owner')
              ? h('a', { href: ctx.href(`/p/${p.id}/settings`) }, "add them under the project's Members first")
              : "add them under the project's Members first")))),
      h('div', null, h('button', { type: 'submit' }, 'Save projects')));
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      // Keep grants this page cannot show (e.g. a project hidden from this list). A
      // blocked project that is already granted drops out: the server would refuse the
      // whole change if the page sent that grant again.
      const shown = new Set(choices.concat(blocked).map((p) => p.id));
      const chosen = [...granted].filter((id) => !shown.has(id))
        .concat([...form.querySelectorAll('input[name=project]')].filter((i) => i.checked && !i.disabled).map((i) => i.value));
      const saved = await act(form.querySelector('button'), () => ctx.api.updateAgent(agent.id, { projects: chosen }), { success: 'Projects saved' }).catch(() => null);
      if (saved) load();
    });
    details.append(h('summary', null, granted.size ? 'Change projects' : 'Grant a project (none yet)'), form);
    return details;
  }
  // What the agent may do, each of its credentials with what it allows, and the way to change either.
  // Setting the scopes issues a new secret that carries them (the server keeps the list on the agent);
  // a credential the agent already has keeps what it allows until it is revoked here.
  function editScopes(agent) {
    const own = Boolean(ctx.me && agent.owner === ctx.me.id);
    const known = Array.isArray(agent.scopes);
    const inferredWarning = agent.scopes_source === 'inferred' ? 'These scopes are inferred. Saving confirms the chosen list as this agent’s stored scopes. ' : '';
    const has = known ? agent.scopes : [];
    const other = otherCredentials(agent);
    const details = h('details', { class: 'deliver', 'data-panel': 'agent-scopes', open: !known || other.length ? true : null });
    const rows = credentialsOf(agent).slice().reverse().map((c) => {
      const working = works(c);
      const row = h('li', { class: 'copy-row', 'data-credential': c.id, 'data-working': String(working), 'data-differs': String(other.includes(c)) },
        h('span', null, c.label || c.id, ' ', h('span', { class: 'small muted' }, 'issued ', time(c.created_at), ' · allows ', scopeNames(c.scopes))),
        working ? (other.includes(c) ? h('span', { class: 'chip warn' }, known ? 'Allows something else' : 'Differs') : h('span', { class: 'chip ok' }, 'Works')) :
          h('span', { class: 'chip plain' }, c.revoked ? 'Revoked' : 'Expired'));
      row.append(expiryLine(c));
      if (working && agent.enabled !== false) row.append(extensionButton(
        agent, c, () => ctx.api.extendAgentCredential(agent.id, c.id), load));
      if (working && agent.enabled !== false) row.append(h('button', { type: 'button', class: 'danger', 'aria-label': 'Revoke credential ' + (c.label || c.id), onclick: async (event) => {
        if (!(await confirmDialog({ title: 'Revoke this credential?', body: 'Anything still using its secret stops working immediately.', confirmLabel: 'Revoke', danger: true }))) return;
        try { await act(event.currentTarget, () => ctx.api.revokeAgentCredential(agent.id, c.id), { success: 'Credential revoked' }); } catch { return; }
        load();
      } }, 'Revoke'));
      return row;
    });
    // Ticked: what it has; every agent can read. Only its own account may give it more.
    const ticked = new Set(known ? ['read', ...has] : ['read']);
    const form = h('form', { class: 'form', novalidate: true },
      h('fieldset', { class: 'checks' }, h('legend', null, 'What it may do'),
        SCOPES.map(([scope, label]) => h('label', { class: 'check', for: `sc-${agent.id}-${scope}` },
          h('input', { type: 'checkbox', id: `sc-${agent.id}-${scope}`, name: 'scope', value: scope, checked: ticked.has(scope) ? true : null,
            disabled: scope === 'read' || (!own && !has.includes(scope)) ? true : null }), label)),
        h('p', { class: 'small muted' }, own ? inferredWarning + 'Every agent can read. Keep “Deliver and review” ticked so the agent can deliver its own work (whether it also reviews other people’s deliveries is set by its worker/reviewer role, not by removing this scope). To remove a scope from an existing agent, untick it, click “Save and issue a new secret”, and click “Revoke” on the older credential above. Saving issues a new secret that carries exactly these, and a later new secret carries them too. A credential it already has keeps what it allows until you revoke it above.' :
          'Only the agent’s own account may give it more than it has; you may take something away (except Read, which every agent has). To remove a scope, untick it, click “Save and issue a new secret”, and click “Revoke” on the older credential above. Saving issues a new secret.')),
      h('p', { class: 'error', role: 'alert', hidden: true }),
      h('div', null, h('button', { type: 'submit' }, 'Save and issue a new secret')));
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      const problem = form.querySelector('p[role=alert]');
      const chosen = [...form.querySelectorAll('input')].filter((i) => i.getAttribute('name') === 'scope' && i.checked && (i.value === 'read' ? (own || !known || has.includes('read')) : !i.disabled)).map((i) => i.value);
      problem.hidden = chosen.length > 0;
      problem.textContent = chosen.length ? '' : 'Tick at least one.';
      if (!chosen.length) return;
      if (!(await confirmDialog({ title: 'Set what it may do and issue a new secret?',
        body: inferredWarning + `${agent.name} will have: ${chosen.join(', ')}. A new credential with exactly these is created and its secret is shown once.`, confirmLabel: 'Issue new secret' }))) return;
      let result;
      try { result = await act(form.querySelector('button'), () => ctx.api.issueAgentCredential(agent.id, chosen), { success: 'New secret issued' }); } catch { return; }
      const fresh = await ctx.api.agent(agent.id).catch(() => agent);
      setupDialog(ctx, { agent: fresh, setup: result && result.setup }, { secret: result && result.credential && result.credential.secret, certificate: await agentCertificate(ctx) });
      load();
    });
    details.append(h('summary', null, known ? 'What it may do' : 'What it may do (choose)'),
      rows.length ? h('ul', { class: 'open-items' }, rows) : h('p', { class: 'small muted' }, 'It has no credential on record.'),
      agent.enabled === false ? h('p', { class: 'small muted' }, 'This agent is disabled: enable it before it gets a new secret.') :
        (!known || agent.scopes_source === 'inferred') && !own ? h('p', { class: 'small muted', 'data-owner-choice': 'true' },
          'Only this agent’s own account may choose or confirm its scopes when they are unknown or inferred. Ask that account before issuing a new secret.') : form);
    return details;
  }
  load();

  function grantable() {
    return ctx.projects.filter((p) => !p.archived && ['owner', 'coordinator', 'contributor', 'superuser'].includes(p.role));
  }
  const projects = grantable();
  let accounts = [];
  let accountError = null;
  if (ctx.me && ctx.me.superuser) {
    try { accounts = (await ctx.api.accounts()).items.filter((u) => !u.disabled); } catch (error) { accountError = error; }
  }
  const ownerField = ctx.me && ctx.me.superuser ? field({ id: 'a-owner', type: 'select', label: 'Account that owns this agent', value: ctx.me.id,
    options: accounts.length ? accounts.map((u) => [u.id, (u.display_name || u.username || u.id) + (u.id === ctx.me.id ? ' (you)' : '')]) : [[ctx.me.id, 'Your account']],
    hint: 'A superuser may create for another account. You see its first secret; that account can issue a new one. Projects must be available to that account.' }) : null;
  const form = h('form', { class: 'form', novalidate: true },
    ownerField,
    accountError ? h('p', { class: 'banner', role: 'status' }, 'Could not load other accounts. Reload this page to choose another account; your draft stays here until creation succeeds.') : null,
    h('div', { class: 'form-row' },
      field({ id: 'a-name', label: 'Agent name', hint: 'How colleagues will see it, e.g. “Kestrel”.', required: true, maxlength: 40 }),
      field({ id: 'a-tool', label: 'Runs in', value: 'GitHub Copilot in VS Code' })),
    field({ id: 'a-dir', label: 'Folder on your computer', placeholder: 'C:\\Users\\you\\agents\\kestrel', hint: 'The folder you open in VS Code to run this agent. Its setup file goes here. Only you and administrators can see it.' }),
    h('fieldset', { class: 'checks' }, h('legend', null, 'Projects it can work on'),
      projects.length ? projects.map((p) => h('label', { class: 'check', for: 'ap-' + p.id }, h('input', { type: 'checkbox', id: 'ap-' + p.id, name: 'project', value: p.id }), p.name)) :
        h('p', { class: 'small muted' }, 'You need contributor access to a project first.'),
      h('p', { class: 'small muted' }, 'It works as a contributor and can never do more than its owning account can in that project.')),
    h('div', null, h('button', { type: 'submit', class: 'primary' }, 'Add agent')));
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const v = formValues(form);
    const name = v['a-name'].trim();
    if (name.length < 2) return setFieldError(form, 'a-name', 'Give the agent a name of at least 2 characters.');
    setFieldError(form, 'a-name', '');
    const chosen = [...form.querySelectorAll('input[name=project]:checked')].map((i) => i.value);
    // A name whose secret file would clash with another of your agents (409), or an
    // invalid name (422), is explained next to the name field in the server's words.
    const created = await act(form.querySelector('button[type=submit]'), () => ctx.api.createAgent({ name, tool: v['a-tool'].trim(), working_directory: v['a-dir'].trim(), projects: chosen, ...(ownerField ? { owner: v['a-owner'] } : {}) }), {
      success: 'Agent added',
      onError: (e) => { if (e.status === 409 || e.status === 422) { setFieldError(form, 'a-name', e.message); return true; } return false; },
    }).catch(() => null);
    if (!created) return;
    form.reset();
    setupDialog(ctx, created, { secret: created.credential && created.credential.secret, certificate: await agentCertificate(ctx) });
    load();
  });

  return h('div', { class: 'stack' },
    pageHead({ title: 'My agents', lede: 'Agents you run on your own computer. Nothing checks in automatically: look here, see which agent has something to do, then open its folder in VS Code and start it.' }),
    filter, host,
    h('section', { class: 'panel' }, h('div', { class: 'panel-head' }, h('h2', { class: 'small' }, 'Add an agent')), h('div', { class: 'panel-body' }, form)));
}

// ---- one-time setup dialog ---------------------------------------------------
//
// Shown once after creation. The secret appears only in its own box and its own copy
// button. Everything that can be saved to disk or pasted into the agent's chat is built
// by agentSetup.js from secretlessPayload(), which never reads the credential.

export const SETUP_INTRO = 'Set up the agent’s folder in one of two ways: Save to agent folder, or paste the setup prompt into the agent’s chat. Copy path is only for saving the files by hand.';
export const SETUP_PROMPT_LABEL = 'Copy setup prompt (creates the files)';
// What the prompt writes, in one line (kittrial-5bb.203 review 01a12408, item 2: no workspace
// settings and no certificate file any more), and the one thing left to the person.
export const SETUP_WRITES_LINE = 'The prompt writes .orchestra/agent.json and .orchestra/AGENT.md, adds .orchestra/ to an existing .gitignore, and puts the address (and, when the service has its own certificate, its fingerprint) as comment lines into your credential file, replacing any earlier copy. It changes nothing else, and it writes no workspace settings: the extension’s own “Orchestra Bridge: Set up” command writes those from that address line. No secret ever leaves the credential file.';
export const SETUP_ONCE_LINE = 'What is still yours, once: install the Orchestra Bridge extension in VS Code, run “Orchestra Bridge: Set up” in the agent’s folder (it writes the workspace settings from your credential file’s address line and shows the server certificate’s fingerprint when the service has one — compare it), reload the window, and click the first wake. Nothing is typed into a chat before you do.';

const canSaveToFolder = () => typeof window.showDirectoryPicker === 'function' && Boolean(window.isSecureContext);
let lastAgentFolderHandle = null;
const PAYLOAD_META = new WeakMap();
export const WIZARD_STORAGE_KEY = 'orchestra.agentSetup.v1';

// The service's own certificate, when it has one: the route is served only by an https service
// that was given a certificate, so a plain-http install or the first-install tunnel simply has
// none and the dialog says nothing about certificates. A failed read is not an error here.
async function agentCertificate(ctx) {
  try { const data = await ctx.api.certificate(); return data && data.certificate ? data : null; } catch { return null; }
}

// Saves that certificate as a file, in one click. The bytes are the ones the fingerprint above was
// computed from. This is for the person: the kit writes no setting that names it (review
// 01a12408, item 2), because the extension asks the person to trust the server on first contact.
function saveCertificate(certificate, file) {
  const blob = new Blob([certificate.certificate], { type: 'application/x-pem-file' });
  const url = URL.createObjectURL(blob);
  const link = h('a', { href: url, download: file.name });
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
  toast('Certificate saved as ' + file.name);
}

// Writes .orchestra/agent.json and .orchestra/AGENT.md into a folder the user picks and appends
// .orchestra/ to an EXISTING .gitignore that lacks it. Creates or rewrites nothing else: the VS
// Code workspace settings are the extension's own "Orchestra Bridge: Set up" command's work since
// extension 0.9.0 (kittrial-5bb.203 review 01a12408, item 2). `files` is [[name, text], ...] built
// from the secretless payload. Remembers the last picked folder handle so subsequent saves start
// in the same directory instead of resetting to Documents (kittrial-5bb.329).
async function saveSetupFiles(files) {
  let root;
  try {
    root = await window.showDirectoryPicker({
      id: 'orchestra-agent',
      mode: 'readwrite',
      ...(lastAgentFolderHandle ? { startIn: lastAgentFolderHandle } : {}),
    });
  } catch (error) {
    if (error && error.name === 'AbortError') return { cancelled: true };
    throw error;
  }
  lastAgentFolderHandle = root;
  if (root.queryPermission && (await root.queryPermission({ mode: 'readwrite' })) !== 'granted') {
    const granted = root.requestPermission ? await root.requestPermission({ mode: 'readwrite' }) : 'denied';
    if (granted !== 'granted') throw Object.assign(new Error('Write permission was not granted'), { name: 'NotAllowedError' });
  }
  const missing = (error) => error && (error.name === 'NotFoundError' || error.name === 'TypeMismatchError');
  let dir = null;
  try { dir = await root.getDirectoryHandle('.orchestra'); } catch (error) { if (!missing(error)) throw error; }
  const existing = [];
  if (dir) {
    for (const [name] of files) {
      try { await dir.getFileHandle(name); existing.push('.orchestra/' + name); } catch (error) { if (!missing(error)) throw error; }
    }
  }
  if (existing.length) {
    const replace = await confirmDialog({
      title: 'Replace the existing setup files?',
      body: `${root.name} already has ${existing.join(' and ')}. Replacing points this folder at the new agent. Nothing else in the folder changes.`,
      confirmLabel: 'Replace', danger: true,
    });
    if (!replace) return { cancelled: true };
  }
  dir = dir || await root.getDirectoryHandle('.orchestra', { create: true });
  const written = [];
  for (const [name, text] of files) {
    const handle = await dir.getFileHandle(name, { create: true });
    const out = await handle.createWritable();
    await out.write(text);
    await out.close();
    written.push('.orchestra/' + name);
  }
  let ignore = null;
  try { ignore = await root.getFileHandle('.gitignore'); } catch (error) { if (!missing(error)) throw error; }
  if (ignore) {
    const current = await ignore.getFile();
    const additionText = setupText.gitignoreAppend(await current.text());
    if (additionText) {
      const out = await ignore.createWritable({ keepExistingData: true });
      await out.seek(current.size);
      await out.write(additionText);
      await out.close();
      written.push('.gitignore (added .orchestra/)');
    }
  }
  return { written, folder: root.name };
}

function fileBlock(title, text, folder, relative) {
  const destination = setupText.destinationPath(folder, relative);
  const name = relative.split('/').pop();
  return h('section', { class: 'setup-block', 'aria-label': title },
    h('div', { class: 'copy-row' },
      h('h3', { class: 'small' }, title),
      copyButton('Copy', text, { ariaLabel: 'Copy ' + name + ' contents', what: name }),
      copyButton('Copy path', destination.path, { ariaLabel: 'Copy the path to save ' + name, what: 'Path' })),
    h('p', { class: 'small muted' }, 'Save as ', h('code', { class: 'path' }, destination.path),
      destination.relative ? ' (no folder is recorded for this agent, so this path is relative to its folder)' : null),
    h('pre', { class: 'json' }, text));
}

// A copyable one-liner: label, the text in <code>, and a Copy button.
function copyLine(label, text, { ariaLabel, what, note, os } = {}) {
  return h('div', { class: 'setup-line' + (os ? ' setup-os' : ''), ...(os ? { 'data-os': os } : {}) },
    h('div', { class: 'copy-row' }, h('span', { class: 'small' }, label), copyButton('Copy', text, { ariaLabel: ariaLabel || 'Copy: ' + label, what: what || label })),
    h('code', { class: 'path' }, text),
    note ? h('p', { class: 'small muted' }, note) : null);
}

// One line per curl error the person may see when the test fails (kittrial-5bb.275 & .328).
export const CURL_ERROR_LINES = Object.freeze({
  2: 'curl exit 2 / cacert does not exist: either <your-name> was left in the credential file (run the rewrite command above so the real path is written automatically) or the certificate is still in Downloads without its leading dot (run the move command above).',
  60: 'curl exit 60: the certificate is not in this machine’s trust store — save the service’s certificate and keep the cacert line in the credential file.',
  35: 'curl exit 35: the TLS handshake failed (wrong address or a proxy speaking plain HTTP to the https port).',
  26: 'curl exit 26: cannot read the config file — the file name is wrong or Notepad added .txt.',
  401: 'HTTP 401: the secret or the header line is wrong.',
});

// Step 1's numbered instructions. `secretValue` is the real secret (only from
// secretSection) or the placeholder.
//
// kittrial-5bb.328 & .329:
// - Each OS-specific block carries data-os="windows" or data-os="posix" so the wizard's OS
//   selector shows only the chosen system's commands and paths.
// - Primary flow has ZERO <your-name> placeholders: the person pastes the single header line
//   and runs the filled-in rewrite command (which writes # server = ..., # server certificate
//   sha256 = ..., cacert = <real expanded path>, and ssl-no-revoke automatically).
// - When hasCertificate is true, explains why browsers strip the leading dot when saving to
//   Downloads and provides the filled-in Move-Item / mv command to move the .crt into the user
//   profile with its leading dot restored.
// - The manual Notepad/editor full-file body (with <your-name>) is tucked inside a collapsed
//   <details data-fallback="manual-body"> for environments where the shell rewrite cannot run.
function secretSteps(payload, secretValue, { withSecret, hasCertificate = false }) {
  const file = payload.secretFile;
  const test = setupText.testCommands(payload);
  const check = setupText.checkCommands(payload, { hasCertificate });
  const meta = PAYLOAD_META.get(payload) || {};
  const address = meta.bridgeServer || payload.server;
  const fingerprint = (meta.certificate && meta.certificate.sha256) || null;
  const comments = setupText.credentialCommentLines(address, fingerprint);
  const rewrite = setupText.credentialFileCommand(file, comments, {
    certFile: payload.certificateFile,
    hasCertificate,
  });
  const moveCert = setupText.certificateMoveCommands(payload.certificateFile);
  const value = secretValue || setupText.SECRET_PLACEHOLDER;
  const singleHeader = setupText.headerLine(value);
  const bodyWindows = hasCertificate
    ? setupText.credentialFileBody(value, payload.certificateFile, { windows: true, hasCertificate: true }).trimEnd()
    : singleHeader;
  const bodyPosix = hasCertificate
    ? setupText.credentialFileBody(value, payload.certificateFile, { windows: false, hasCertificate: true }).trimEnd()
    : null;
  return h('ol', { class: 'setup-substeps' },
    hasCertificate ? h('li', null,
      'Save the service’s certificate FIRST (the “Save certificate” button above), as ',
      h('code', { class: 'path setup-os', 'data-os': 'windows' }, payload.certificateFile.windows),
      h('span', { class: 'setup-os', 'data-os': 'posix' }, ' (macOS/Linux ', h('code', { class: 'path' }, payload.certificateFile.posix), ')'),
      '. Every curl below needs that file.',
      h('p', { class: 'small muted' }, moveCert.note),
      copyLine('Move the downloaded certificate into your user profile (PowerShell):', moveCert.powershell,
        { ariaLabel: 'Copy the PowerShell certificate move command', what: 'Certificate move command', os: 'windows' }),
      copyLine('Move the downloaded certificate into your user profile (macOS/Linux):', moveCert.posix,
        { ariaLabel: 'Copy the macOS/Linux certificate move command', what: 'Certificate move command', os: 'posix' })) : null,
    h('li', null,
      h('span', { class: 'setup-os', 'data-os': 'windows' }, 'Open Notepad.'),
      h('span', { class: 'setup-os', 'data-os': 'posix' }, 'Open a text editor (on macOS or Linux).')),
    h('li', null,
      copyLine(withSecret ? 'Paste this line (it contains the secret):' : 'Paste this line, with your secret in place of <your secret>:', singleHeader,
        { ariaLabel: withSecret ? 'Copy the credential file text with the secret' : 'Copy the credential file template', what: 'Credential file text',
          note: 'No placeholders to edit: the rewrite command in the next step adds the server address and the real expanded certificate path automatically.' })),
    h('li', null,
      copyLine('File > Save As, set “Save as type” to “All files (*.*)”, and use this file name:', file.windows,
        { ariaLabel: 'Copy the Windows secret file name', what: 'File name', os: 'windows',
          note: 'Windows file dialogs understand %USERPROFILE%. Keep the name exactly, with no .txt at the end.' }),
      copyLine('On macOS or Linux, save it as this file instead, then restrict it:', file.posix,
        { ariaLabel: 'Copy the macOS/Linux secret file name', what: 'File name', os: 'posix' }),
      copyLine('…and run:', test.posixChmod,
        { ariaLabel: 'Copy the chmod command', what: 'Command', os: 'posix' })),
    h('li', null,
      copyLine('Write the server address and real certificate path into the credential file (PowerShell):', rewrite.powershell,
        { ariaLabel: 'Copy the PowerShell credential file rewrite command', what: 'Command', os: 'windows',
          note: 'Rewrites the file in place so cacert uses your real user profile path (no placeholder to edit) and adds the # server = line for Orchestra Bridge.' }),
      copyLine('Write the server address and real certificate path into the credential file (macOS/Linux):', rewrite.posix,
        { ariaLabel: 'Copy the macOS/Linux credential file rewrite command', what: 'Command', os: 'posix',
          note: 'Rewrites the file in place under mode 600 so cacert uses your real $HOME path (no placeholder to edit) and adds the # server = line for Orchestra Bridge.' }),
      hasCertificate ? h('details', { class: 'small', 'data-fallback': 'manual-body' },
        h('summary', null, 'Manual text-editor body (only if you cannot run the rewrite command above)'),
        h('p', { class: 'small muted' }, 'Type your own full path in place of <your-name> (PowerShell: echo $env:USERPROFILE; macOS/Linux: echo $HOME). Leaving <your-name> literally in the file causes curl exit 2 (cacert file does not exist).'),
        copyLine('Full Windows credential file body (replace <your-name>):', bodyWindows,
          { ariaLabel: 'Copy the manual Windows credential file text', what: 'Credential file text', os: 'windows' }),
        bodyPosix ? copyLine('On macOS or Linux the body is this instead:', bodyPosix,
          { ariaLabel: 'Copy the macOS/Linux credential file text', what: 'Credential file text', os: 'posix' }) : null) : null),
    h('li', null,
      copyLine('Self-check and test in PowerShell (checks for common mistakes, then calls Orchestra):', check.powershell,
        { ariaLabel: 'Copy the PowerShell self-check command', what: 'Command', os: 'windows' }),
      copyLine('Test it in PowerShell:', test.powershell,
        { ariaLabel: 'Copy the PowerShell test command', what: 'Command', os: 'windows' }),
      copyLine('Self-check and test on macOS or Linux (checks for common mistakes, then calls Orchestra):', check.posix,
        { ariaLabel: 'Copy the macOS/Linux self-check command', what: 'Command', os: 'posix' }),
      copyLine('On macOS or Linux:', test.posix,
        { ariaLabel: 'Copy the macOS/Linux test command', what: 'Command', os: 'posix' }),
      h('p', { class: 'small muted' }, 'Success prints the agent’s details as JSON (its id and name).'),
      ...Object.entries(CURL_ERROR_LINES).map(([code, text]) => (code === '26'
        ? h('p', { class: 'small muted', 'data-curl-error': code },
            h('span', { class: 'setup-os', 'data-os': 'windows' }, text),
            h('span', { class: 'setup-os', 'data-os': 'posix' }, 'curl exit 26: cannot read the config file — the file name is wrong or an extra .txt extension was added.'))
        : h('p', { class: 'small muted', 'data-curl-error': code }, text)))),
    h('li', null, 'Never put the secret on a command line, in the agent folder, in agent.json, in AGENT.md or in the setup prompt. Those only name the file above.'));
}

// The one-time secret, in its own box with its own copy button, and the step-1 line
// built from it. This is the ONLY place a secret is rendered; nothing else in the
// dialog receives it.
function secretSection(secret, heading, payload, { hasCertificate = false } = {}) {
  return h('div', { class: 'setup-block' },
    h('div', { class: 'copy-row' }, h('h4', { class: 'small' }, heading), copyButton('Copy', secret, { ariaLabel: 'Copy the secret', what: 'Secret' })),
    h('div', { class: 'secret' }, secret),
    h('p', { class: 'small muted' }, 'It is not shown again. Store it now:'),
    secretSteps(payload, secret, { withSecret: true, hasCertificate }));
}

// Reopened dialog: no secret. The secret was shown once when its credential was
// issued; if it is lost, issue a NEW credential (the old secret is never re-shown).
// ": read, tasks" for a list of scopes, or "" when the server sent none (an older service).
function scopeList(scopes) {
  return Array.isArray(scopes) && scopes.length ? ': ' + scopes.join(', ') : '';
}
function reissueSection(ctx, agent, payload, { hasCertificate = false } = {}) {
  const section = h('div', { class: 'setup-block' });
  if (agent.scopes === null || (agent.scopes_source === 'inferred' && agent.owner !== (ctx.me && ctx.me.id))) {
    // Nothing says what the agent may do, so the server refuses a plain renewal: the choice is on its card.
    section.append(h('p', { class: 'small', role: 'status', 'data-scopes-needed': 'true' },
      'The secret was shown once and cannot be shown again. A new one cannot be issued from here: its scopes are unknown or inferred and unconfirmed. Close this and choose under “What it may do” on its card; only its own account may give it more or confirm inferred scopes by plain renewal.'));
    return section;
  }
  const issue = h('button', { type: 'button', onclick: async () => {
    const ok = await confirmDialog({
      title: 'Issue a new secret?',
      body: `A new credential is created for ${agent.name || agent.display_name} and its secret is shown once. ${agent.scopes_source === 'inferred' ? `These scopes are inferred from working credentials and are not confirmed. Issuing this secret confirms them as this agent’s stored scopes${scopeList(agent.scopes)}.` : Array.isArray(agent.scopes) ? `It carries what the agent has now${scopeList(agent.scopes)}.` : 'This older service does not report the agent’s scopes. Check the new credential’s scopes before using it.'} The agent's current credential keeps working until you revoke it.`,
      confirmLabel: 'Issue new secret',
    });
    if (!ok) return;
    let result;
    try { result = await act(issue, () => ctx.api.issueAgentCredential(agent.id), { success: 'New secret issued' }); } catch { return; }
    const credential = (result && result.credential) || {};
    if (!credential.secret) {
      section.replaceChildren(h('p', { class: 'small', role: 'alert' }, 'The new credential was created, but its secret cannot be shown again (secrets are shown only once). Issue another one and revoke this one.'));
      return;
    }
    // What the new credential may do is said with it: a renewal used to change that without a word (kittrial-5bb.208).
    section.replaceChildren(secretSection(credential.secret, 'New secret (shown once)', payload, { hasCertificate }),
      h('p', { class: 'small', 'data-scopes': (credential.scopes || []).join(' ') }, 'This credential carries' + (scopeList(credential.scopes) || ': nothing beyond reading') + '.'),
      olderCredentials(ctx, agent, credential.id));
    if (typeof section.onReissued === 'function') section.onReissued();
  } }, 'Issue a new secret');
  section.append(
    h('p', { class: 'small muted' }, 'The secret was shown once, when the agent’s credential was issued, and cannot be shown again. If you stored it as below, nothing more is needed here. If it is lost, issue a new one.'),
    h('div', { class: 'copy-row' }, issue),
    h('details', null, h('summary', null, 'How the secret is stored'), secretSteps(payload, null, { withSecret: false, hasCertificate })));
  return section;
}
// The agent's other live credentials after a new one is issued: they still work
// until revoked, so offer the revoke (POST /v1/agents/{id}/credentials/{cid}/revoke).
function olderCredentials(ctx, agent, newId) {
  const live = credentialsOf(agent).filter((c) => c.id !== newId && works(c));
  if (!live.length) return h('p', { class: 'small muted' }, 'Any earlier credential for this agent keeps working until it is revoked.');
  return h('div', { class: 'setup-block' },
    h('p', { class: 'small' }, `The agent’s earlier ${live.length === 1 ? 'credential still works' : 'credentials still work'} until revoked. Revoke once the agent uses the new secret.`),
    h('ul', { class: 'open-items' }, live.map((c) => {
      const row = h('li', { class: 'copy-row' }, h('span', null, c.label || c.id, ' ', h('span', { class: 'small muted' }, 'issued ', time(c.created_at), ' · allows ', scopeNames(c.scopes))));
      row.append(h('button', { type: 'button', class: 'danger', 'aria-label': 'Revoke credential ' + (c.label || c.id), onclick: async (event) => {
        const button = event.currentTarget;
        if (!(await confirmDialog({ title: 'Revoke this credential?', body: 'Anything still using its secret stops working immediately.', confirmLabel: 'Revoke', danger: true }))) return;
        try { await act(button, () => ctx.api.revokeAgentCredential(agent.id, c.id), { success: 'Credential revoked' }); } catch { return; }
        button.replaceWith(h('span', { class: 'chip plain' }, 'Revoked'));
      } }, 'Revoke'));
      return row;
    })));
}

// Reopens the setup dialog from the card, from the agent's CURRENT secretless record.
async function reopenSetup(ctx, agentId, button) {
  let agent;
  try { agent = await act(button, () => ctx.api.agent(agentId)); } catch { return; }
  setupDialog(ctx, { agent }, { secret: null, certificate: await agentCertificate(ctx) });
}

function walkElements(node, fn) {
  if (!node || !node.childNodes) return;
  for (const child of node.childNodes) {
    if (child && child.tagName) {
      fn(child);
      walkElements(child, fn);
    }
  }
}

function readWizardStorage() {
  try {
    if (typeof sessionStorage === 'undefined') return null;
    const raw = sessionStorage.getItem(WIZARD_STORAGE_KEY);
    return raw ? JSON.parse(raw) : null;
  } catch { return null; }
}

function writeWizardStorage(state) {
  try {
    if (typeof sessionStorage === 'undefined') return;
    sessionStorage.setItem(WIZARD_STORAGE_KEY, JSON.stringify({
      agentId: state.agentId,
      step: state.step,
      os: state.os,
      role: state.role || 'worker',
    }));
  } catch { /* ignore storage errors */ }
}

export function clearWizardStorage() {
  try {
    if (typeof sessionStorage === 'undefined') return;
    sessionStorage.removeItem(WIZARD_STORAGE_KEY);
  } catch { /* ignore storage errors */ }
}

// Turns the three setup steps into a step-by-step wizard (kittrial-5bb.329):
// - One step screen visible at a time (Step 1 of 3, Step 2 of 3, Step 3 of 3) with Next and Back.
// - OS selector chosen first (auto-detected from navigator, changeable at any step) that hides
//   commands and paths for other operating systems.
// - A connection check before Next (GET /v1/service/certificate when hasCertificate, plus
//   GET /v1/agents/{id} checking last_seen_at newer than the step's start), with a one-sentence
//   remedy if not yet seen.
// - On Step 3, states plainly what Orchestra Bridge 0.14+ writes (.orchestra/agent.json and
//   .orchestra/AGENT.md into the open workspace folder from GET /v1/agents/me, plus comparing
//   working_directory) and shows the fallback for older extensions (Save to agent folder…
//   remembering the last folder, or let the agent do it).
// - Runs a 3-second online check ONLY while Step 3 is open and document.visibilityState === 'visible',
//   stopping immediately on success, when leaving Step 3, hiding the page, or closing the dialog.
function wireWizard(ctx, container, payload, { hasCertificate = false, pageMode = false, initialStep = 1, initialOs = null, initialLastSeenAt = null, keepAliveOptions = null } = {}) {
  const saved = readWizardStorage();
  const sameAgent = saved && saved.agentId === payload.agentId;
  let currentOs = initialOs || (sameAgent && saved.os) || setupText.detectOs();
  if (!setupText.OS_CHOICES.some(([v]) => v === currentOs)) currentOs = 'windows';
  let currentStep = initialStep || 1;
  let checkVerified = false;
  let lastKeepAliveAt = 0;
  let stepStartedAt = Date.now();
  let baselineLastSeen = initialLastSeenAt || null;

  const idleWarningBanner = h('div', {
    class: 'small banner warn copy-row',
    role: 'status',
    'data-wizard-idle-warning': 'true',
    hidden: true,
  });
  function showIdleWarning(detail) {
    const raw = typeof detail === 'number' ? detail : (detail && detail.remainingSeconds);
    const secs = typeof raw === 'number' && raw > 0 ? raw : 120;
    const mins = Math.max(1, Math.round(secs / 60));
    idleWarningBanner.replaceChildren(
      h('span', null, `Your session will expire in about ${mins} min (${secs}s) from inactivity — continue set-up to stay signed in.`),
      h('button', {
        type: 'button',
        'data-action': 'wizard-keep-alive',
        onclick: async () => {
          await touchKeepAlive({ force: true });
          idleWarningBanner.hidden = true;
        },
      }, 'Keep session open'),
    );
    idleWarningBanner.hidden = false;
  }
  const onIdleWarningEvent = (event) => showIdleWarning(event && event.detail);
  if (typeof document !== 'undefined' && typeof document.addEventListener === 'function') {
    document.addEventListener(SESSION_IDLE_WARNING_EVENT, onIdleWarningEvent);
  }
  let keepAliveHandle = null;
  if (ctx && ctx.api && typeof ctx.api.keepAliveWhileVisible === 'function') {
    keepAliveHandle = ctx.api.keepAliveWhileVisible({
      ...(keepAliveOptions || {}),
      onWarning: showIdleWarning,
    });
  }
  function stopKeepAlive() {
    if (keepAliveHandle && typeof keepAliveHandle.stop === 'function') {
      try { keepAliveHandle.stop(); } catch { /* ignore */ }
    }
    keepAliveHandle = null;
    if (typeof document !== 'undefined' && typeof document.removeEventListener === 'function') {
      document.removeEventListener(SESSION_IDLE_WARNING_EVENT, onIdleWarningEvent);
    }
  }

  async function touchKeepAlive({ force = false } = {}) {
    const now = Date.now();
    if (!force && now - lastKeepAliveAt < 60000) return;
    lastKeepAliveAt = now;
    try {
      if (ctx && typeof ctx.keepSessionAlive === 'function') await ctx.keepSessionAlive();
      else if (ctx && ctx.api && typeof ctx.api.keepAlive === 'function') await ctx.api.keepAlive().catch(() => {});
      else if (ctx && ctx.api && typeof ctx.api.current === 'function') await ctx.api.current().catch(() => {});
    } catch { /* ignore */ }
  }

  const stepNodes = [];
  let bodyNode = null;
  let footNode = null;
  let roleSelect = null;
  const cls = (el) => el.className || el.getAttribute('class') || '';
  walkElements(container, (el) => {
    if (cls(el) === 'dialog-body') bodyNode = el;
    if (cls(el) === 'dialog-foot') footNode = el;
    if (el.getAttribute('id') === 'setup-role') roleSelect = el;
    if (cls(el) === 'setup-step') stepNodes.push(el);
    if (typeof el.onReissued !== 'undefined' || (cls(el) === 'setup-block' && el.parentNode)) {
      el.onReissued = () => {
        stepStartedAt = Date.now();
        applyOsFilter();
      };
    }
  });

  const stepTitles = ['Store the secret', 'Set up the folder', 'Start the agent'];
  const whenWorkedTexts = [
    `What you will see when it worked: Running the test command prints this agent’s JSON ("id": "${payload.agentId}", "name": "${payload.name}") with no curl error, and Check & Next confirms the connection.`,
    'What you will see when it worked: With Orchestra Bridge 0.14 or later in VS Code, Step 3’s “Orchestra Bridge: Set up” writes .orchestra/agent.json and .orchestra/AGENT.md (and .gitignore) directly into the open workspace folder from GET /v1/agents/me. With an older extension, “Save to agent folder…” (or the setup prompt) writes both files into the folder.',
    `What you will see when it worked: In VS Code, “Orchestra Bridge: Set up” walks you through choosing the agent, confirming the server, trusting the certificate by fingerprint, testing the connection, writing .orchestra/agent.json and .orchestra/AGENT.md into the open workspace folder, checking working_directory, and turning watching on; the connection status below changes to Connected.`,
  ];

  // Add step-specific Bridge 0.14+ vs older extension guidance and "What you will see when it worked".
  stepNodes.forEach((stepNode, idx) => {
    const stepNum = idx + 1;
    stepNode.setAttribute('data-wizard-step', String(stepNum));
    if (stepNum === 1) {
      walkElements(stepNode, (el) => {
        if (el.tagName === 'CODE' && el.textContent === payload.secretFile.windows && !el.getAttribute('data-os')) {
          el.setAttribute('data-os-path', 'secret');
        }
        if (cls(el) === 'setup-block') {
          el.onReissued = () => {
            stepStartedAt = Date.now();
            applyOsFilter();
          };
        }
      });
    }
    if (stepNum === 2) {
      const bridgeStep2 = h('div', { class: 'banner info', role: 'note', 'data-bridge-note': 'step2' },
        'Orchestra Bridge 0.14 or later writes ', h('code', null, setupText.CONFIG_PATH), ' and ', h('code', null, setupText.GUIDE_PATH),
        ' (and ', h('code', null, setupText.GITIGNORE_LINE), ' in .gitignore) into the open VS Code workspace folder for you when you run “Orchestra Bridge: Set up” in Step 3. With an older Orchestra Bridge extension (before 0.14), use “Save to agent folder…” below (it remembers the last folder you picked) or “Or let the agent do it” to write those two files now.');
      stepNode.append(bridgeStep2);
    }
    if (stepNum === 3) {
      const bridgeStep3 = h('div', { class: 'setup-block', 'data-bridge-note': 'step3' },
        h('p', { class: 'small' },
          h('strong', null, 'Orchestra Bridge 0.14 or later: '),
          'open this folder in VS Code and run “Orchestra Bridge: Set up” (',
          h('kbd', { 'data-os-shortcut': 'palette' }, currentOs === 'macos' ? 'Cmd+Shift+P' : 'Ctrl+Shift+P'),
          '); it walks you through the rest — chooses the agent from the credential files found, confirms the server, trusts the certificate by fingerprint (and reads the certificate from the setup bundle), tests the connection, writes ',
          h('code', null, setupText.CONFIG_PATH), ' and ', h('code', null, setupText.GUIDE_PATH),
          ' (and adds ', h('code', null, setupText.GITIGNORE_LINE),
          ' to an existing .gitignore) directly into the open workspace folder from GET /v1/agents/me, compares the open workspace folder with this agent’s recorded working_directory (offering to record the open folder via PATCH /v1/agents/me if they differ), and turns watching on.'),
        h('p', { class: 'small muted' },
          h('strong', null, 'Older Orchestra Bridge extension (before 0.14): '),
          'update the extension first (this site serves it), or use the fallback: an older installed extension writes only the VS Code workspace settings and does not write ',
          h('code', null, setupText.CONFIG_PATH), ' or ', h('code', null, setupText.GUIDE_PATH),
          ' — click “Back” to return to Step 2 and click “Save to agent folder…” (which remembers the last folder you picked) or “Or let the agent do it” to write those two files.'));
      stepNode.append(bridgeStep3);
    }
    stepNode.append(h('p', { class: 'small banner info', 'data-when-worked': String(stepNum) }, whenWorkedTexts[idx]));
  });

  const onlineStatus = h('p', {
    class: 'small banner',
    role: 'status',
    'data-online-status': 'true',
    'data-online-state': 'waiting',
  }, `Waiting for ${payload.name} to connect from VS Code or curl…`);
  if (stepNodes[2]) {
    stepNodes[2].append(onlineStatus);
  }

  const progressBadge = h('span', { class: 'chip accent', 'data-wizard-progress': String(currentStep) },
    `Step ${currentStep} of 3: ${stepTitles[currentStep - 1]}`);
  const pills = h('div', { class: 'seg', 'data-wizard-pills': 'true' },
    stepTitles.map((title, idx) => {
      const n = idx + 1;
      return h('button', {
        type: 'button',
        'data-step-pill': String(n),
        'aria-pressed': String(n === currentStep),
        onclick: () => showStep(n),
      }, `${n}. ${title}`);
    }));
  const osSelect = h('select', {
    id: 'setup-os',
    'data-os-select': 'true',
    'aria-label': 'Your operating system',
    onchange: (event) => {
      currentOs = event.target.value;
      applyOsFilter();
      persistState();
    },
  }, setupText.OS_CHOICES.map(([val, label]) => h('option', { value: val, selected: val === currentOs ? true : null }, label)));
  osSelect.value = currentOs;

  const wizardBar = h('div', { class: 'copy-row', 'data-wizard-bar': 'true' },
    progressBadge,
    pills,
    h('label', { class: 'small', for: 'setup-os' }, 'Operating system: ', osSelect));

  if (bodyNode) {
    const existingChildren = Array.from(bodyNode.childNodes);
    // A browser inserts the text "null" for a null child, so keep the heading explicitly:
    // a null first child showed the word "null" when the body was empty
    // (kittrial-5bb.342 review item static-lists-and-null).
    bodyNode.replaceChildren(
      ...(existingChildren[0] ? [existingChildren[0]] : []),
      wizardBar,
      idleWarningBanner,
      ...existingChildren.slice(1),
    );
  }

  const checkStatus = h('p', {
    class: 'small banner',
    role: 'status',
    'data-wizard-check-status': 'true',
    'data-check-state': 'idle',
    hidden: true,
  });
  const skipBtn = h('button', {
    type: 'button',
    'data-action': 'skip-check',
    hidden: true,
    onclick: () => {
      checkStatus.hidden = true;
      skipBtn.hidden = true;
      if (currentStep < 3) showStep(currentStep + 1);
    },
  }, 'Continue without checking');
  const backBtn = h('button', {
    type: 'button',
    'data-action': 'wizard-back',
    hidden: currentStep === 1,
    onclick: () => {
      if (currentStep > 1) showStep(currentStep - 1);
    },
  }, 'Back');
  const checkBtn = h('button', {
    type: 'button',
    'data-action': 'wizard-check',
    onclick: () => runCheckBeforeNext({ advanceOnPass: false }),
  }, 'Check connection');
  const nextBtn = h('button', {
    type: 'button',
    class: 'primary',
    'data-action': 'wizard-next',
    onclick: () => {
      if (currentStep === 1) {
        return runCheckBeforeNext({ advanceOnPass: true });
      }
      if (currentStep === 2) {
        if (checkVerified) { showStep(3); return true; }
        return runCheckBeforeNext({ advanceOnPass: true });
      }
      return false;
    },
  }, 'Next');
  const doneBtn = h('button', {
    type: 'button',
    class: currentStep === 3 ? 'primary' : '',
    'data-action': 'wizard-done',
    onclick: () => {
      stopStep3Poll();
      stopKeepAlive();
      if (pageMode && ctx && typeof ctx.go === 'function') ctx.go('/agents');
      else { container.close(); container.remove(); }
    },
  }, 'Done');

  if (footNode) {
    footNode.replaceChildren(checkStatus, skipBtn, backBtn, checkBtn, nextBtn, doneBtn);
  }

  function persistState() {
    touchKeepAlive();
    writeWizardStorage({
      agentId: payload.agentId,
      step: currentStep,
      os: currentOs,
      role: (roleSelect && roleSelect.value) || 'worker',
    });
  }

  function applyOsFilter() {
    const kind = setupText.osKind(currentOs);
    walkElements(container, (el) => {
      const targetOs = el.getAttribute('data-os');
      if (targetOs) {
        el.hidden = !(targetOs === currentOs || targetOs === kind);
      }
      const pathKind = el.getAttribute('data-os-path');
      if (pathKind === 'secret') {
        el.textContent = setupText.osSecretFile(payload.secretFile, currentOs);
      } else if (pathKind === 'certificate') {
        el.textContent = setupText.osCertificateFile(payload.certificateFile, currentOs);
      }
      if (el.getAttribute('data-os-shortcut') === 'palette') {
        el.textContent = currentOs === 'macos' ? 'Cmd+Shift+P' : 'Ctrl+Shift+P';
      }
    });
  }

  function isNewerThanStart(lastSeen) {
    if (!lastSeen) return false;
    const seenStr = String(lastSeen);
    if (baselineLastSeen && seenStr === String(baselineLastSeen)) return false;
    const seenMs = Date.parse(seenStr);
    if (Number.isNaN(seenMs) || seenMs < stepStartedAt) return false;
    if (baselineLastSeen) {
      const baseMs = Date.parse(String(baselineLastSeen));
      if (!Number.isNaN(baseMs) && seenMs <= baseMs) return false;
    }
    return true;
  }

  async function runCheckBeforeNext({ advanceOnPass = false } = {}) {
    touchKeepAlive();
    if (hasCertificate && ctx && ctx.api && typeof ctx.api.certificate === 'function') {
      try { await ctx.api.certificate(); } catch { /* ignore */ }
    }
    let latest = null;
    if (ctx && ctx.api && typeof ctx.api.agent === 'function' && payload.agentId) {
      try { latest = await ctx.api.agent(payload.agentId); } catch { latest = null; }
    }
    const seen = latest && latest.last_seen_at ? String(latest.last_seen_at) : null;
    if (isNewerThanStart(seen)) {
      checkVerified = true;
      checkStatus.textContent = `Connected: ${payload.name} reached Orchestra during this step (last seen ${seen}).`;
      checkStatus.setAttribute('data-check-state', 'passed');
      checkStatus.hidden = false;
      skipBtn.hidden = true;
      onlineStatus.setAttribute('data-online-state', 'connected');
      onlineStatus.textContent = `Connected: ${payload.name} has reached Orchestra (last active ${seen}).`;
      if (advanceOnPass && currentStep < 3) {
        showStep(currentStep + 1);
      }
      return true;
    }
    checkStatus.textContent = seen
      ? `Orchestra has not seen a new request from ${payload.name} since this step started (last seen earlier at ${seen}). Run the self-check or test command in Step 1 now, then click Next again — or click Continue without checking.`
      : `Orchestra has not seen a request from ${payload.name} yet. Run the self-check or test command in Step 1 first, then click Next again — or click Continue without checking.`;
    checkStatus.setAttribute('data-check-state', 'pending');
    checkStatus.hidden = false;
    skipBtn.hidden = false;
    return false;
  }

  // 3-second online check: runs ONLY while Step 3 is open and document.visibilityState === 'visible',
  // and stops immediately on success, when leaving Step 3, hiding the page, or closing the dialog.
  let pollTimer = null;
  let pollStopped = false;
  const isVisible = () => typeof document === 'undefined' || document.visibilityState === undefined || document.visibilityState === 'visible';

  function stopStep3Poll() {
    if (pollTimer !== null) {
      clearInterval(pollTimer);
      pollTimer = null;
    }
  }

  async function pollStep3Once() {
    if (pollStopped || currentStep !== 3 || !isVisible() || (!pageMode && !container.open)) {
      stopStep3Poll();
      return false;
    }
    if (!ctx || !ctx.api || typeof ctx.api.agent !== 'function' || !payload.agentId) return false;
    try {
      const latest = await ctx.api.agent(payload.agentId);
      if (latest && latest.last_seen_at && isNewerThanStart(latest.last_seen_at)) {
        pollStopped = true;
        checkVerified = true;
        stopStep3Poll();
        onlineStatus.setAttribute('data-online-state', 'connected');
        onlineStatus.textContent = `Connected: ${payload.name} has reached Orchestra (last active ${latest.last_seen_at}).`;
        return true;
      }
    } catch { /* ignore transient error */ }
    return false;
  }

  function syncStep3Poll() {
    if (pollStopped || currentStep !== 3 || !isVisible() || (!pageMode && !container.open)) {
      stopStep3Poll();
      return;
    }
    if (pollTimer === null) {
      pollStep3Once();
      pollTimer = setInterval(pollStep3Once, 3000);
    }
  }

  const onVisibilityChange = () => syncStep3Poll();
  if (typeof document !== 'undefined' && typeof document.addEventListener === 'function') {
    document.addEventListener('visibilitychange', onVisibilityChange);
  }
  container.addEventListener('close', () => {
    stopStep3Poll();
    stopKeepAlive();
    if (typeof document !== 'undefined' && typeof document.removeEventListener === 'function') {
      document.removeEventListener('visibilitychange', onVisibilityChange);
    }
  });

  function showStep(stepNumber) {
    currentStep = Math.max(1, Math.min(3, Number(stepNumber) || 1));
    container.setAttribute('data-active-step', String(currentStep));
    progressBadge.setAttribute('data-wizard-progress', String(currentStep));
    progressBadge.textContent = `Step ${currentStep} of 3: ${stepTitles[currentStep - 1]}`;
    stepNodes.forEach((node, idx) => {
      const active = idx + 1 === currentStep;
      node.hidden = !active;
      node.setAttribute('data-active', String(active));
    });
    walkElements(pills, (btn) => {
      const n = Number(btn.getAttribute('data-step-pill'));
      if (n) btn.setAttribute('aria-pressed', String(n === currentStep));
    });
    backBtn.hidden = currentStep === 1;
    checkBtn.hidden = currentStep === 3;
    nextBtn.hidden = currentStep === 3;
    doneBtn.className = currentStep === 3 ? 'primary' : '';
    checkStatus.hidden = true;
    skipBtn.hidden = true;
    persistState();
    syncStep3Poll();
  }

  if (roleSelect) {
    roleSelect.addEventListener('change', () => persistState());
  }
  applyOsFilter();
  showStep(currentStep);
  return { showStep, applyOsFilter, stopStep3Poll, syncStep3Poll, stopKeepAlive };
}

// `source` is the create response ({agent, setup, ...}) or {agent} from GET
// /v1/agents/{id}; `secret` is present only straight after creation; `certificate` is the
// service's own certificate (GET /v1/service/certificate) or null when it has none.
function setupDialog(ctx, source, { secret = null, certificate = null } = {}) {
  const payload = setupText.secretlessPayload(source, location.origin);
  const hasCertificate = Boolean(certificate);
  const files = [['agent.json', setupText.agentJson(payload)], ['AGENT.md', setupText.agentGuide(payload, { hasCertificate })]];
  const folder = payload.workingDirectory;
  const where = folder ? h('code', { class: 'path' }, folder) : 'the agent’s folder';
  // The address the extension must use is the one this page is connected to, not the configured
  // public address, which may be a name that does not resolve from this person's network.
  const bridgeServer = location.origin;
  PAYLOAD_META.set(payload, { certificate, bridgeServer });
  let role = (source && source.agent && source.agent.role === 'reviewer') ? 'reviewer' : 'worker';
  const prompt = setupText.setupPrompt(payload, { role, certificate, bridgeServer });
  const promptText = (chosen) => setupText.setupPrompt(payload, { role: chosen, certificate, bridgeServer });

  const status = h('p', { class: 'small', role: 'status', hidden: true });
  let save;
  if (canSaveToFolder()) {
    save = h('div', { class: 'copy-row' },
      h('button', { type: 'button', class: 'primary', 'aria-describedby': 'setup-save-hint', onclick: async (event) => {
        const button = event.currentTarget || event.target;
        button.disabled = true;
        status.hidden = true;
        try {
          const result = await saveSetupFiles(files);
          if (!result.cancelled) {
            status.className = 'small';
            status.setAttribute('role', 'status');
            status.textContent = `Saved in ${result.folder}: ${result.written.join(', ')}.`;
            status.hidden = false;
            toast('Setup files saved');
          }
        } catch (error) {
          status.className = 'small error-text';
          status.setAttribute('role', 'alert');
          status.textContent = error && (error.name === 'NotAllowedError' || error.name === 'SecurityError')
            ? 'The browser did not allow writing to that folder. Choose a folder you can write to, or use the copy buttons below.'
            : 'Could not save the files (' + ((error && error.message) || 'unknown error') + '). Use the copy buttons below instead.';
          status.hidden = false;
        } finally {
          button.disabled = false;
        }
      } }, 'Save to agent folder…'),
      h('span', { class: 'small muted', id: 'setup-save-hint' }, 'Pick ', where, '. Writes .orchestra/agent.json and .orchestra/AGENT.md and adds .orchestra/ to an existing .gitignore. The VS Code settings are the extension’s own “Orchestra Bridge: Set up” command’s work. The secret is never saved.'));
  } else {
    save = h('p', { class: 'small muted', role: 'note' }, 'Saving directly needs Edge or Chrome on an https or localhost address. Use the copy buttons below instead.');
  }

  // The role this window's agent takes (the extension's worker or reviewer: the prompt names it so
  // the person chooses the same in the extension's own set-up), and — when the service has its own
  // certificate — its fingerprint with a Save button and what comparing it does and does not prove.
  const roleStatus = h('p', { class: 'small', role: 'status', 'data-role-status': 'true', hidden: true });
  const roleSelectEl = h('select', { id: 'setup-role', onchange: async (event) => {
    role = event.target.value;
    drawPrompt(promptText(role));
    roleStatus.hidden = true;
    if (ctx && ctx.api && typeof ctx.api.updateAgent === 'function' && payload.agentId) {
      try {
        await ctx.api.updateAgent(payload.agentId, { role });
        roleStatus.className = 'small muted';
        roleStatus.setAttribute('role', 'status');
        roleStatus.textContent = `Saved ${payload.name}’s stored role on Orchestra as ${role}.`;
        roleStatus.hidden = false;
      } catch (error) {
        roleStatus.className = 'small error-text';
        roleStatus.setAttribute('role', 'alert');
        roleStatus.textContent = `Could not save ${payload.name}’s stored role on Orchestra: ${(error && error.message) || 'request failed'}.`;
        roleStatus.hidden = false;
      }
    }
  } },
    h('option', { value: 'worker' }, 'works on tasks and answers reviews'),
    h('option', { value: 'reviewer' }, 'reviews other agents’ deliveries'));
  for (const opt of roleSelectEl.childNodes || []) {
    if (opt && opt.getAttribute && opt.getAttribute('value') === role) {
      opt.selected = true;
      opt.setAttribute('selected', '');
    }
  }
  roleSelectEl.value = role;
  const roleChoice = h('div', { class: 'stack' },
    h('label', { class: 'small', for: 'setup-role' }, 'What this agent does in this window: (also saves ', payload.name, '’s stored role on Orchestra) ',
      roleSelectEl),
    roleStatus,
    h('p', { class: 'small muted', 'data-role-note': 'true' },
      'Changing this picker updates both the setup prompt for this window and this agent’s stored role on Orchestra. A worker keeps the “Deliver and review” scope (needed to deliver its own work and answer review items on its own tasks), while its /v1/agents/me/next and wake omit other people’s deliveries awaiting review (to-review and review-recommended). Choose reviewer when this window should also be offered other people’s deliveries to review.'));
  const certificateBlock = certificate ? h('section', { class: 'setup-block', 'aria-label': 'The service’s own certificate' },
    h('div', { class: 'copy-row' }, h('h4', { class: 'small' }, 'The service’s own certificate'),
      copyButton('Copy', certificate.sha256, { ariaLabel: 'Copy the certificate fingerprint', what: 'Fingerprint' }),
      h('button', { type: 'button', class: 'primary', onclick: () => saveCertificate(certificate, payload.certificateFile) }, 'Save certificate')),
    // The button used to blend into the text, and the next command then failed with a raw
    // error because nothing had been downloaded (James, 2026-10-18; kittrial-5bb.342).
    h('p', { class: 'small', 'data-save-certificate-line': 'true' }, 'Save certificate downloads the certificate file to your Downloads folder; the move command in step 1 below puts it in your user profile.'),
    h('p', { class: 'small' }, 'SHA-256 fingerprint: ', h('code', { class: 'path', 'data-fingerprint': certificate.sha256 }, certificate.sha256)),
    h('p', { class: 'small muted' }, 'In VS Code run “Orchestra Bridge: Set up”: it will show this fingerprint when it first reaches the server — compare it before you trust it.'),
    h('p', { class: 'small muted' }, 'What that comparison proves: the extension reached the same endpoint this page did. What it does not: this page and this string came over the same connection, so anyone who can serve this page can serve his own certificate and his own fingerprint. The independent source is you, on the server: run openssl x509 -in <the certificate file> -noout -fingerprint -sha256 there and compare that value.')) : null;

  const promptBody = h('div', { class: 'stack' });
  const drawPrompt = (text) => promptBody.replaceChildren(
    h('div', { class: 'copy-row' }, h('h4', { class: 'small' }, 'Or let the agent do it'), copyButton(SETUP_PROMPT_LABEL, text, { what: 'Setup prompt' })),
    h('p', { class: 'small muted' }, 'Paste this into the agent’s own chat, opened in its folder. It creates both files, updates .gitignore in a Git repository, puts the address into your credential file, then continues. It writes no VS Code settings and does not contain the secret.'),
    h('details', null, h('summary', null, 'Show the prompt'), h('pre', { class: 'json' }, text)));
  drawPrompt(prompt);
  const promptSection = h('section', { class: 'setup-block', 'aria-label': 'Setup prompt' }, promptBody);

  const resume = setupText.resumePrompt(payload.name);
  // The wizard bar's progress badge is the step's title ("Step 1 of 3: Store the secret"). A
  // numbered heading here repeated "1. Store the secret" inside the step list, so James saw
  // "step 1" twice; leaving the badge as the one title (kittrial-5bb.342).
  const step = (number, title, ...body) => h('li', { class: 'setup-step', 'aria-label': `Step ${number}: ${title}` }, ...body);
  const dialog = h('dialog', { class: 'dialog-wide', 'aria-labelledby': 'setup-title' });
  dialog.append(
    h('div', { class: 'dialog-body' },
      h('h2', { id: 'setup-title' }, `Set up ${payload.name}`),
      h('p', { class: 'small muted' }, 'Three steps. Each has its own Copy buttons.'),
      h('ol', { class: 'setup-steps' },
        step(1, 'Store the secret',
          h('p', { class: 'small' }, 'The agent reads its secret from one small file in your user profile, never from its own folder: ', h('code', null, payload.secretFile.windows), '.'),
          // Save the certificate BEFORE the test (kittrial-5bb.275): the test curl and every
          // curl -K need the saved certificate to pass the certificate check.
          certificateBlock,
          secret ? secretSection(secret, 'Secret (shown once)', payload, { hasCertificate }) : reissueSection(ctx, source.agent || {}, payload, { hasCertificate })),
        step(2, 'Set up the folder',
          h('p', { class: 'setup-intro' }, SETUP_INTRO),
          h('p', { class: 'small' }, 'The folder gets two small files, ', h('code', null, setupText.CONFIG_PATH), ' and ', h('code', null, setupText.GUIDE_PATH),
            ', in ', where, '. Keep .orchestra/ out of Git. Neither file holds the secret; they only name the file from step 1.'),
          h('p', { class: 'small', 'data-writes': 'setup' }, SETUP_WRITES_LINE),
          roleChoice,
          h('section', { class: 'setup-block', 'aria-label': 'Save the setup files' }, save, status),
          promptSection,
          fileBlock(setupText.CONFIG_PATH, files[0][1], folder, setupText.CONFIG_PATH),
          fileBlock(setupText.GUIDE_PATH, files[1][1], folder, setupText.GUIDE_PATH),
          source.setup && source.setup.setup_snippet ? h('details', null, h('summary', null, 'Shell commands instead (optional)'), h('pre', { class: 'json' }, source.setup.setup_snippet)) : null),
        step(3, 'Start the agent',
          h('p', { class: 'small' }, 'Open ', where, ' in VS Code (File > Open Folder…) and run “Orchestra Bridge: Set up”; it walks you through the rest (chooses this agent from your credential file, confirms the server, trusts the certificate by fingerprint, tests the connection, writes .orchestra/agent.json and .orchestra/AGENT.md, and turns watching on). Then open the agent’s chat and paste the resume prompt.'),
          h('p', { class: 'small', 'data-once': 'true' }, SETUP_ONCE_LINE),
          h('p', { class: 'small muted', 'data-screen-reader-tip': 'true' },
            'Tip: if VS Code makes sounds while the agent works, click the screen reader button in the bottom right of VS Code to switch it off.'),
          folder ? copyLine('Folder:', folder, { ariaLabel: 'Copy the agent folder path', what: 'Folder path' }) : null,
          copyLine('VS Code command:', 'Orchestra Bridge: Set up', { ariaLabel: 'Copy the Orchestra Bridge Set up command', what: 'VS Code command' }),
          hasCertificate ? h('div', { class: 'setup-block', 'data-bridge-cafile': 'true' },
            h('p', { class: 'small' },
              'Certificate trust before prompting the agent: Orchestra Bridge 0.14 or later reads the certificate from the site’s setup bundle during “Orchestra Bridge: Set up” so no setting is needed. With an older extension (before 0.14), BEFORE prompting the agent, either run “Orchestra Bridge: Check now” and compare the fingerprint, or set ',
              h('code', null, 'orchestraBridge.caFile'),
              ' in VS Code to the same certificate path your credential file uses (never install the certificate in system certificate stores, launch VS Code with --use-system-ca, or set NODE_EXTRA_CA_CERTS):'),
            copyLine('orchestraBridge.caFile (Windows):', payload.certificateFile.windows,
              { ariaLabel: 'Copy the Windows orchestraBridge.caFile path', what: 'orchestraBridge.caFile path', os: 'windows' }),
            copyLine('orchestraBridge.caFile (macOS/Linux):', payload.certificateFile.posix,
              { ariaLabel: 'Copy the macOS/Linux orchestraBridge.caFile path', what: 'orchestraBridge.caFile path', os: 'posix' })) : null,
          copyLine('Resume prompt:', resume, { ariaLabel: 'Copy the resume prompt', what: 'Resume prompt' })))),
    h('div', { class: 'dialog-foot' }, h('button', { type: 'button', class: 'primary', onclick: () => { dialog.close(); dialog.remove(); } }, 'Done')));
  dialog.addEventListener('close', () => dialog.remove());
  document.body.appendChild(dialog);
  dialog.showModal();
  dialog.wizard = wireWizard(ctx, dialog, payload, {
    hasCertificate,
    initialLastSeenAt: (source && source.agent && source.agent.last_seen_at) || null,
    keepAliveOptions: (ctx && ctx.keepAliveOptions) || null,
  });
  return dialog;
}

// Standalone #/setup wizard view: resumes the active agent's setup wizard across reloads
// or session re-login using secretless sessionStorage state (kittrial-5bb.329 & .330).
// When no agent is specified in the URL or sessionStorage, asks which agent to set up
// without opening a modal on its own.
export async function setupWizardPage(ctx) {
  const saved = readWizardStorage();
  const requestedId = (ctx.query && typeof ctx.query.get === 'function' && ctx.query.get('agent')) || (saved && saved.agentId) || null;
  let agent = null;
  if (requestedId) {
    try { agent = await ctx.api.agent(requestedId); } catch { agent = null; }
  }
  if (!agent) {
    let items = [];
    try {
      const listed = ctx.api && typeof ctx.api.agents === 'function' ? await ctx.api.agents({ limit: 20 }) : null;
      items = (listed && listed.items) || [];
    } catch { items = []; }
    if (items.length) {
      return h('div', { class: 'stack', 'data-view': 'agent-setup-chooser', 'data-agent-chooser': 'true' },
        pageHead({ title: 'Set up an agent', lede: 'Choose which agent to set up in the three-step wizard.' }),
        h('div', { class: 'panel' },
          h('div', { class: 'panel-body stack' },
            h('p', { class: 'small' }, 'Which agent do you want to set up?'),
            h('ul', { class: 'open-items' }, items.map((a) => h('li', { class: 'copy-row', 'data-choose-agent': a.id },
              h('strong', null, a.display_name || a.name),
              h('button', {
                type: 'button',
                class: 'primary',
                onclick: (e) => {
                  if (ctx && typeof ctx.go === 'function') ctx.go(`/setup?agent=${encodeURIComponent(a.id)}`);
                  else reopenSetup(ctx, a.id, e.currentTarget);
                },
              }, `Set up ${a.display_name || a.name}`)))),
            h('div', { class: 'copy-row' }, h('a', { class: 'btn', href: ctx.href('/agents') }, 'Back to My agents')))));
    }
    return h('div', { class: 'stack' },
      pageHead({ title: 'Set up an agent', lede: 'Create an agent on My agents first, then follow the three-step wizard.' }),
      h('div', { class: 'panel' },
        empty('No agent selected', 'Go to My agents to add an agent and start the step-by-step setup wizard.'),
        h('div', { class: 'panel-body' }, h('a', { class: 'btn primary', href: ctx.href('/agents') }, 'Go to My agents'))));
  }
  const certificate = await agentCertificate(ctx);
  const dialog = setupDialog(ctx, { agent }, { secret: null, certificate });
  if (dialog.wizard && saved && saved.agentId === agent.id && saved.step) {
    dialog.wizard.showStep(saved.step);
  }
  return h('div', { class: 'stack', 'data-view': 'agent-setup-wizard' },
    pageHead({ title: `Set up ${agent.display_name || agent.name}`, lede: 'Step-by-step agent setup wizard.' }),
    h('div', { class: 'panel' },
      h('div', { class: 'panel-body' },
        h('p', { class: 'small' }, 'The step-by-step setup wizard is open for ', h('strong', null, agent.display_name || agent.name), '.'),
        h('div', { class: 'copy-row' },
          h('button', { type: 'button', class: 'primary', onclick: (e) => reopenSetup(ctx, agent.id, e.currentTarget) }, 'Reopen setup wizard'),
          h('a', { class: 'btn', href: ctx.href('/agents') }, 'Back to My agents')))));
}
