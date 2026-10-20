// Personal agents: each belongs to the person who runs it. Nothing polls; when the
// owner opens this page they see which agent has something to do and which folder to
// open in VS Code to resume it. Agents themselves read work over the REST API.
import { h, time, toast, copyText, copyButton, confirmDialog } from '../dom.js';
import { pageHead, empty, field, setFieldError, formValues, act, errorState, describe } from '../ui.js';
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
function manages(ctx, raw) {
  const owner = raw.owner || raw.owner_id || raw.agent_of;
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
    const choices = grantable();
    const details = h('details', { class: 'deliver', open: granted.size ? null : true });
    const form = h('form', { class: 'form', novalidate: true },
      h('fieldset', { class: 'checks' }, h('legend', null, 'Projects it can work on'),
        choices.length ? choices.map((p) => h('label', { class: 'check', for: `gp-${agent.id}-${p.id}` },
          h('input', { type: 'checkbox', id: `gp-${agent.id}-${p.id}`, name: 'project', value: p.id, checked: granted.has(p.id) ? true : null }), p.name)) :
          h('p', { class: 'small muted' }, 'You need contributor access to a project first.')),
      h('div', null, h('button', { type: 'submit' }, 'Save projects')));
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      // Keep grants this page cannot show (e.g. a project hidden from this list).
      const shown = new Set(choices.map((p) => p.id));
      const chosen = [...granted].filter((id) => !shown.has(id))
        .concat([...form.querySelectorAll('input[name=project]:checked')].map((i) => i.value));
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
// Shown once after creation, and from the agent's card. The secret appears only on the "Save your
// secret" screen, in the lines to paste and behind their Copy button. Everything that can be saved to disk or pasted into the agent's chat is built
// by agentSetup.js from secretlessPayload(), which never reads the credential.

export const SETUP_PROMPT_LABEL = 'Copy setup prompt (creates the files)';

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

// One line per curl error the person may see when the test fails (kittrial-5bb.275 & .328).
export const CURL_ERROR_LINES = Object.freeze({
  2: 'curl exit 2 / cacert does not exist: either <your-name> was left in the credential file (run the command on “Add the certificate path”) or the certificate is still in Downloads without its leading dot (run the command on “Move the certificate”).',
  60: 'curl exit 60: the certificate is not in this machine’s trust store — save the service’s certificate and keep the cacert line in the credential file.',
  35: 'curl exit 35: the TLS handshake failed (wrong address or a proxy speaking plain HTTP to the https port).',
  26: 'curl exit 26: cannot read the config file — the file name is wrong or Notepad added .txt.',
  401: 'HTTP 401: the secret or the header line is wrong.',
});

// ": read, tasks" for a list of scopes, or "" when the server sent none (an older service).
function scopeList(scopes) {
  return Array.isArray(scopes) && scopes.length ? ': ' + scopes.join(', ') : '';
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
    // Secretless: the agent, the path (Bridge 0.14+ or not), the screen, the system and the role.
    sessionStorage.setItem(WIZARD_STORAGE_KEY, JSON.stringify({
      agentId: state.agentId,
      screen: state.screen,
      bridge: state.bridge !== false,
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

// ---- the set-up wizard: one action per screen (kittrial-5bb.348) --------------
//
// James at the office (2026-10-20): "Minimize the amount of instructions and text visible at any
// time to just the task at hand." Each screen shows a short title, at most two short sentences,
// its ONE action (a button, a choice, or one command with its Copy button), and Back / Next; any
// explanation is behind a collapsed "More about this". Only the current screen is in the dialog:
// another screen's commands are not hidden, they are absent. The screens and their order are
// setupText.screenPlan(): a computer whose VS Code has Orchestra Bridge 0.14 or later takes the
// short path (its Set up fetches and checks the certificate, tests the connection and writes the
// agent's files), any other the longer one.
//
// The secret: it is shown once, on the "Save your secret" screen only, inside the lines the person
// pastes into the credential file (setupText.credentialFileLines). It is never stored (the wizard's
// sessionStorage holds the agent id, the path, the screen, the system and the role), never written
// to the folder files or the prompts, and never put on a command line: the commands only name the
// file.

const SCREEN_TITLES = Object.freeze({
  system: 'Your computer',
  role: 'What this agent does',
  'cert-save': 'Save the certificate',
  'cert-move': 'Move the certificate',
  secret: 'Save your secret',
  restrict: 'Restrict the file',
  rewrite: 'Add the certificate path',
  test: 'Test it',
  files: 'Save the agent’s files',
  vscode: 'Set up in VS Code',
  resume: 'Start the agent',
});
//: The numbered step of the released three-step wizard, mapped to a screen of the new one, so a
//: wizard left open across the upgrade resumes near where it was.
function oldStepScreen(step, bridge) {
  return { 1: 'secret', 2: bridge ? 'vscode' : 'files', 3: 'resume' }[step] || null;
}
//: Screens whose Next first checks that the agent reached Orchestra during the screen (.329).
const CHECKED_SCREENS = Object.freeze(['test', 'vscode']);

// What the fingerprint is for, and what comparing it does and does not prove (review 01a12408,
// items 2 and 4): behind "More about this" on the certificate screen, and on the VS Code screen
// when the service has its own certificate.
function fingerprintNotes(certificate) {
  return [
    h('p', { class: 'small muted' }, 'SHA-256 fingerprint: ', h('code', { class: 'path', 'data-fingerprint': certificate.sha256 }, certificate.sha256)),
    h('p', { class: 'small muted' }, 'In VS Code run “Orchestra Bridge: Set up”: it will show this fingerprint when it first reaches the server — compare it before you trust it.'),
    h('p', { class: 'small muted' }, 'What that comparison proves: the extension reached the same endpoint this page did. What it does not: this page and this string came over the same connection, so anyone who can serve this page can serve his own certificate and his own fingerprint. The independent source is you, on the server: run openssl x509 -in <the certificate file> -noout -fingerprint -sha256 there and compare that value.'),
  ];
}

function more(...body) {
  return h('details', { class: 'small', 'data-more': 'true' }, h('summary', null, 'More about this'), ...body);
}

function osWords(os) {
  return setupText.osKind(os) === 'posix'
    ? { shell: 'a terminal', editor: 'a text editor', palette: os === 'macos' ? 'Cmd+Shift+P' : 'Ctrl+Shift+P' }
    : { shell: 'PowerShell', editor: 'Notepad', palette: 'Ctrl+Shift+P' };
}

// One command for the chosen system, with its Copy button. A long command (the self-check, a
// rewrite) would fill the screen, so it is the Copy button with the text behind "Show the command"
// (kittrial-5bb.348: minimal text on a screen).
const LONG_COMMAND = 160;
function commandLine(text, what) {
  if (text.length <= LONG_COMMAND) {
    return h('div', { class: 'setup-line', 'data-command': what },
      h('div', { class: 'copy-row' }, h('code', { class: 'path' }, text), copyButton('Copy', text, { ariaLabel: 'Copy the ' + what, what })));
  }
  // Then the Copy button IS the screen's one action: a primary button that says what it copies.
  const copyLong = copyButton('Copy the command', text, { ariaLabel: 'Copy the ' + what, what });
  copyLong.className = 'primary';
  return h('div', { class: 'setup-line', 'data-command': what, 'data-long-command': 'true' },
    h('div', { class: 'copy-row' }, copyLong),
    h('details', { class: 'small' }, h('summary', null, 'Show the command'), h('code', { class: 'path' }, text)));
}

// The body of the "Save your secret" screen with a secret in hand. `secretValue` is the fresh
// secret: the dialog's own (straight after creation) or a credential issued on this screen. This is
// the ONLY place a secret is rendered: in the lines to paste, behind their Copy button.
function secretLinesBlock(payload, secretValue, os) {
  const meta = PAYLOAD_META.get(payload) || {};
  const address = meta.bridgeServer || payload.server;
  const fingerprint = (meta.certificate && meta.certificate.sha256) || null;
  const lines = setupText.credentialFileLines(address, fingerprint, secretValue);
  const file = setupText.osSecretFile(payload.secretFile, os);
  const posix = setupText.osKind(os) === 'posix';
  return h('div', { class: 'stack', 'data-secret-lines': 'true' },
    h('p', { class: 'small' }, `Open ${osWords(os).editor}, paste these lines, and save them as the file below${posix ? '' : ' (Save as type: All files)'}. They are shown only now.`),
    h('div', { class: 'copy-row' }, h('pre', { class: 'json secret', 'data-secret-file-text': 'true' }, lines),
      copyButton('Copy', lines, { ariaLabel: 'Copy the credential file text with the secret', what: 'Credential file text' })),
    commandLine(file, 'file name'),
    more(h('p', { class: 'small muted' }, 'The first lines tell Orchestra Bridge which server to use (and which certificate to expect); the last line is the secret. Keep the file name exactly, with no .txt at the end. Never put the secret in the agent’s folder, a commit or a command line.')));
}

// The "Save your secret" screen without a secret in hand: one line saying why, and the issue
// button right there (kittrial-5bb.348). A new secret replaces this with secretLinesBlock at once.
function issueBlock(ctx, agent, payload, wizard) {
  const block = h('div', { class: 'stack', 'data-secret-missing': 'true' });
  if (agent.scopes === null || (agent.scopes_source === 'inferred' && agent.owner !== (ctx.me && ctx.me.id))) {
    // Nothing says what the agent may do, so the server refuses a plain renewal: the choice is on its card.
    block.append(h('p', { class: 'small', role: 'status', 'data-scopes-needed': 'true' },
      'The secret is shown only once, when it is issued, and a new one cannot be issued here: choose under “What it may do” on the agent’s card.'));
    return block;
  }
  const issue = h('button', { type: 'button', class: 'primary', 'data-action': 'issue-secret', onclick: async () => {
    wizard.clearError();
    const ok = await confirmDialog({
      title: 'Issue a new secret?',
      body: `A new credential is created for ${agent.name || agent.display_name} and its secret is shown once. ${agent.scopes_source === 'inferred' ? `These scopes are inferred from working credentials and are not confirmed. Issuing this secret confirms them as this agent’s stored scopes${scopeList(agent.scopes)}.` : Array.isArray(agent.scopes) ? `It carries what the agent has now${scopeList(agent.scopes)}.` : 'This older service does not report the agent’s scopes. Check the new credential’s scopes before using it.'} The agent's current credential keeps working until you revoke it.`,
      confirmLabel: 'Issue new secret',
    });
    if (!ok) return;
    const result = await act(issue, () => ctx.api.issueAgentCredential(agent.id), { success: 'New secret issued', onError: wizard.showError });
    if (!result) return;
    const credential = result.credential || {};
    if (!credential.secret) {
      wizard.showError(new Error('The new credential was created, but its secret cannot be shown again (secrets are shown only once). Issue another one and revoke this one.'));
      return;
    }
    wizard.secretIssued(credential.secret, credential, agent);
  } }, 'Issue a new secret');
  block.append(
    h('p', { class: 'small', 'data-secret-note': 'true' }, 'The secret is shown only once, when it is issued; issue a new one to continue.'),
    h('div', { class: 'copy-row' }, issue));
  return block;
}

function setupDialog(ctx, source, { secret = null, certificate = null } = {}) {
  const payload = setupText.secretlessPayload(source, location.origin);
  const hasCertificate = Boolean(certificate);
  const files = [['agent.json', setupText.agentJson(payload)], ['AGENT.md', setupText.agentGuide(payload, { hasCertificate })]];
  const folder = payload.workingDirectory;
  // The address the extension must use is the one this page is connected to, not the configured
  // public address, which may be a name that does not resolve from this person's network.
  const bridgeServer = location.origin;
  PAYLOAD_META.set(payload, { certificate, bridgeServer });
  const agent = (source && source.agent) || {};
  const saved = readWizardStorage();
  const same = saved && saved.agentId === payload.agentId ? saved : null;
  const state = {
    os: (same && same.os) || setupText.detectOs(),
    bridge: same && typeof same.bridge === 'boolean' ? same.bridge : true,
    role: (same && setupText.BRIDGE_ROLES.includes(same.role) && same.role) || (agent.role === 'reviewer' ? 'reviewer' : 'worker'),
    screen: 'system',
    stepStartedAt: Date.now(),
    checkVerified: false,
    // The fresh secret, while this dialog is open: never stored, never put anywhere but the
    // "Save your secret" screen. Straight after creation it is the dialog's own.
    secretValue: secret,
    issued: null,
  };
  if (!setupText.OS_CHOICES.some(([v]) => v === state.os)) state.os = 'windows';
  const prompt = setupText.setupPrompt(payload, { role: state.role, certificate, bridgeServer });
  const plan = () => setupText.screenPlan({ bridge: state.bridge, os: state.os, hasCertificate });
  if (state.secretValue) {
    // A fresh secret exists only while this dialog is open: open where it is saved (Back reaches the
    // system and role screens), never two clicks away from it.
    state.screen = 'secret';
  } else if (same) {
    const wanted = same.screen || oldStepScreen(same.step, state.bridge);
    if (wanted && plan().includes(wanted)) state.screen = wanted;
  }

  const dialog = h('dialog', { class: 'dialog-wide', 'aria-labelledby': 'setup-title', 'data-setup-wizard': 'true' });
  const progress = h('span', { class: 'chip accent', 'data-wizard-progress': 'true' });
  const title = h('h2', { id: 'setup-title' });
  const screenNode = h('div', { class: 'stack', 'data-wizard-screen': '' });
  const errorLine = h('p', { class: 'small error-text', role: 'alert', 'data-wizard-error': 'true', hidden: true });
  const checkStatus = h('p', { class: 'small banner', role: 'status', 'data-wizard-check-status': 'true', 'data-check-state': 'idle', hidden: true });
  const idleWarningBanner = h('div', { class: 'small banner warn copy-row', role: 'status', 'data-wizard-idle-warning': 'true', hidden: true });
  const onlineStatus = h('p', { class: 'small banner', role: 'status', 'data-online-status': 'true', 'data-online-state': 'waiting' },
    `Waiting for ${payload.name} to connect from VS Code or curl…`);

  // Every error raised while the wizard is open is shown HERE, on the current screen, in full
  // (describe() and the request id), never behind the modal (kittrial-5bb.348).
  const wizard = {
    showError(error) {
      const parts = [describe(error) || (error && error.message) || 'Request failed.'];
      if (error && error.requestId) parts.push(h('span', { class: 'mono' }, ' (request ' + error.requestId + ')'));
      errorLine.replaceChildren(...parts);   // never a null child: a browser shows it as "null"
      errorLine.hidden = false;
      return true;   // handled: act() shows no toast and does not rethrow
    },
    clearError() { errorLine.hidden = true; errorLine.replaceChildren(); },
    secretIssued(value, credential, owner) {
      state.secretValue = value;
      state.issued = { credential, agent: owner };
      render();
    },
  };

  const backBtn = h('button', { type: 'button', 'data-action': 'wizard-back', onclick: () => go(-1) }, 'Back');
  const skipBtn = h('button', { type: 'button', 'data-action': 'skip-check', hidden: true, onclick: () => { state.checkVerified = true; go(1); } }, 'Continue without checking');
  const nextBtn = h('button', { type: 'button', class: 'primary', 'data-action': 'wizard-next', onclick: () => next() }, 'Next');
  const doneBtn = h('button', { type: 'button', 'data-action': 'wizard-done', onclick: () => finish() }, 'Done');

  dialog.append(
    h('div', { class: 'dialog-body' },
      h('div', { class: 'copy-row' }, progress), title, idleWarningBanner, screenNode, errorLine, checkStatus),
    h('div', { class: 'dialog-foot' }, skipBtn, backBtn, nextBtn, doneBtn));

  // ---- the screens -------------------------------------------------------------------------
  function choice(name, value, label, checked, onchange) {
    return h('label', { class: 'small', 'data-choice': name + ':' + value },
      h('input', { type: 'radio', name, value, checked, onchange }), ' ', label);
  }
  const screens = {
    system: () => [
      h('p', { class: 'small' }, 'Choose this computer’s system, and say whether its VS Code has Orchestra Bridge 0.14 or later.'),
      h('div', { class: 'copy-row', 'data-os-choice': 'true' }, setupText.OS_CHOICES.map(([value, label]) =>
        choice('setup-os', value, label, state.os === value, () => { state.os = value; persist(); showProgress(); }))),
      h('div', { class: 'copy-row', 'data-bridge-choice': 'true' },
        choice('setup-bridge', 'yes', 'Orchestra Bridge 0.14 or later', state.bridge, () => { state.bridge = true; persist(); showProgress(); }),
        choice('setup-bridge', 'no', 'An older extension, or none', !state.bridge, () => { state.bridge = false; persist(); showProgress(); })),
      more(h('p', { class: 'small muted' }, 'To see the version: in VS Code open Extensions (Ctrl+Shift+X) and find Orchestra Bridge. With 0.14 or later its own “Orchestra Bridge: Set up” checks the certificate, tests the connection and writes the agent’s files, so this wizard skips those screens.')),
    ],
    role: () => [
      h('p', { class: 'small' }, 'Most agents work on tasks. A reviewer is also offered other people’s deliveries to review.'),
      h('div', { class: 'copy-row', 'data-role-choice': 'true' },
        choice('setup-role', 'worker', 'Works on tasks', state.role === 'worker', () => saveRole('worker')),
        choice('setup-role', 'reviewer', 'Reviews deliveries', state.role === 'reviewer', () => saveRole('reviewer'))),
      more(h('p', { class: 'small muted' }, `This is saved as ${payload.name}’s role on Orchestra. A worker keeps the “Deliver and review” scope, which it needs to deliver its own work; only the reviewer role adds other people’s deliveries to its list.`)),
    ],
    'cert-save': () => [
      h('p', { class: 'small' }, 'This server has its own certificate. Save it; the next screen moves it into place.'),
      h('div', { class: 'copy-row' }, h('button', { type: 'button', class: 'primary', 'data-action': 'save-certificate',
        onclick: () => saveCertificate(certificate, payload.certificateFile) }, 'Save certificate')),
      more(...fingerprintNotes(certificate)),
    ],
    'cert-move': () => {
      const move = setupText.certificateMoveCommands(payload.certificateFile);
      const posix = setupText.osKind(state.os) === 'posix';
      return [
        h('p', { class: 'small' }, `Run this in ${osWords(state.os).shell}. It moves the saved certificate from Downloads into your user profile.`),
        commandLine(posix ? move.posix : move.powershell, 'certificate move command'),
        more(h('p', { class: 'small muted' }, move.note)),
      ];
    },
    secret: () => [
      state.secretValue
        ? secretLinesBlock(payload, state.secretValue, state.os)
        : issueBlock(ctx, agent, payload, wizard),
      state.issued ? more(h('p', { class: 'small', 'data-scopes': ((state.issued.credential && state.issued.credential.scopes) || []).join(' ') },
        'This credential carries' + (scopeList(state.issued.credential && state.issued.credential.scopes) || ': nothing beyond reading') + '.'),
      olderCredentials(ctx, state.issued.agent, state.issued.credential && state.issued.credential.id)) : null,
    ],
    restrict: () => [
      h('p', { class: 'small' }, 'Run this in a terminal, so that only you can read the file.'),
      commandLine(setupText.testCommands(payload).posixChmod, 'chmod command'),
    ],
    rewrite: () => {
      const meta = PAYLOAD_META.get(payload) || {};
      const comments = setupText.credentialCommentLines(meta.bridgeServer || payload.server, (certificate && certificate.sha256) || null);
      const rewrite = setupText.credentialFileCommand(payload.secretFile, comments, { certFile: payload.certificateFile, hasCertificate });
      const posix = setupText.osKind(state.os) === 'posix';
      return [
        h('p', { class: 'small' }, `Run this in ${osWords(state.os).shell}. It writes the certificate’s full path into the file, so that curl accepts the server.`),
        commandLine(posix ? rewrite.posix : rewrite.powershell, 'rewrite command'),
        more(h('p', { class: 'small muted' }, 'It rewrites only that one file, in place, and prints nothing; the secret stays where it is. Running it twice leaves the same content. If you cannot run it, type your own full path instead of <your-name>:'),
          h('pre', { class: 'json' }, setupText.credentialFileBody(null, payload.certificateFile, { windows: !posix, hasCertificate: true }).trimEnd())),
      ];
    },
    test: () => {
      const posix = setupText.osKind(state.os) === 'posix';
      const check = setupText.checkCommands(payload, { hasCertificate });
      const plain = setupText.testCommands(payload);
      return [
        h('p', { class: 'small' }, `Run this in ${osWords(state.os).shell}. It checks the file, then calls Orchestra; it prints the agent’s details when it works.`),
        commandLine(posix ? check.posix : check.powershell, 'self-check command'),
        more(h('p', { class: 'small muted' }, 'The plain test, without the checks:'),
          h('code', { class: 'path' }, posix ? plain.posix : plain.powershell),
          ...Object.entries(CURL_ERROR_LINES).map(([code, text]) => h('p', { class: 'small muted', 'data-curl-error': code }, text))),
      ];
    },
    files: () => [
      h('p', { class: 'small' }, 'Pick the agent’s folder. Two small files are written there; neither holds the secret.'),
      saveFilesRow(),
      more(h('p', { class: 'small muted' }, 'It writes .orchestra/agent.json and .orchestra/AGENT.md and adds .orchestra/ to an existing .gitignore. Or paste this prompt into the agent’s own chat, opened in its folder; it does the same and puts the server address into your credential file:'),
        h('div', { class: 'copy-row' }, copyButton(SETUP_PROMPT_LABEL, () => setupText.setupPrompt(payload, { role: state.role, certificate, bridgeServer }), { what: 'Setup prompt' })),
        fileBlock(setupText.CONFIG_PATH, files[0][1], folder, setupText.CONFIG_PATH),
        fileBlock(setupText.GUIDE_PATH, files[1][1], folder, setupText.GUIDE_PATH)),
    ],
    vscode: () => [
      h('p', { class: 'small' }, folder ? 'Open this folder in VS Code (File > Open Folder), then run the command below (' : 'Open the agent’s folder in VS Code (File > Open Folder), then run the command below (',
        h('kbd', null, osWords(state.os).palette), ').'),
      folder ? commandLine(folder, 'folder path') : null,
      commandLine('Orchestra Bridge: Set up', 'VS Code command'),
      more(h('p', { class: 'small muted' }, 'It chooses this agent from your credential file, confirms the server, trusts the certificate by its fingerprint, tests the connection, writes .orchestra/agent.json and .orchestra/AGENT.md and turns watching on. Next checks that the agent reached Orchestra.'),
        hasCertificate ? fingerprintNotes(certificate) : null,
        !state.bridge && hasCertificate ? h('p', { class: 'small muted', 'data-bridge-cafile': 'true' },
          'With an extension before 0.14, run “Orchestra Bridge: Check now” and compare the fingerprint, or set orchestraBridge.caFile to ',
          h('code', null, setupText.osCertificateFile(payload.certificateFile, state.os)),
          '. Never install the certificate in the system certificate store, start VS Code with --use-system-ca, or set NODE_EXTRA_CA_CERTS.') : null),
    ],
    resume: () => [
      h('p', { class: 'small' }, 'Paste this into the agent’s chat in VS Code.'),
      commandLine(setupText.resumePrompt(payload.name), 'resume prompt'),
      onlineStatus,
      more(h('p', { class: 'small muted', 'data-screen-reader-tip': 'true' }, 'If VS Code makes sounds while the agent works, click the screen reader button in the bottom right of VS Code to switch it off.')),
    ],
  };

  // The one button of the "Save the agent's files" screen, with the agent's recorded folder path
  // printed BESIDE it (kittrial-5bb.348), so the person picks the right folder.
  function saveFilesRow() {
    if (!canSaveToFolder()) {
      return h('p', { class: 'small muted', role: 'note' }, 'Saving directly needs Edge or Chrome on an https or localhost address: use “More about this” below.');
    }
    const saveStatus = h('p', { class: 'small', role: 'status', 'data-save-status': 'true', hidden: true });
    const button = h('button', { type: 'button', class: 'primary', 'data-action': 'save-files', onclick: async () => {
      wizard.clearError();
      button.disabled = true;
      try {
        const result = await saveSetupFiles(files);
        if (!result.cancelled) {
          saveStatus.textContent = `Saved in ${result.folder}: ${result.written.join(', ')}.`;
          saveStatus.hidden = false;
        }
      } catch (error) {
        wizard.showError(error && (error.name === 'NotAllowedError' || error.name === 'SecurityError')
          ? new Error('The browser did not allow writing to that folder. Choose a folder you can write to, or use “More about this” below.')
          : new Error('Could not save the files (' + ((error && error.message) || 'unknown error') + '). Use “More about this” below instead.'));
      } finally { button.disabled = false; }
    } }, 'Save to agent folder…');
    return h('div', { class: 'stack' },
      h('div', { class: 'copy-row', 'data-folder-beside': 'true' }, button,
        folder ? h('code', { class: 'path', 'data-folder-path': folder }, folder) : h('span', { class: 'small muted' }, 'the agent’s folder'),
        folder ? copyButton('Copy', folder, { ariaLabel: 'Copy the agent folder path', what: 'Folder path' }) : null),
      saveStatus);
  }

  async function saveRole(role) {
    state.role = role;
    persist();
    wizard.clearError();
    if (ctx && ctx.api && typeof ctx.api.updateAgent === 'function' && payload.agentId) {
      try { await ctx.api.updateAgent(payload.agentId, { role }); }
      catch (error) { wizard.showError(error); }
    }
  }

  // ---- moving between screens ----------------------------------------------------------------
  function persist() {
    touchKeepAlive();
    writeWizardStorage({ agentId: payload.agentId, screen: state.screen, bridge: state.bridge, os: state.os, role: state.role });
  }

  // The badge counts the screens of the chosen path; the heading below it is the screen's one title
  // (no title said twice). A choice on the first screen changes the count without rebuilding the
  // screen, so the keyboard focus stays on the choice.
  function showProgress() {
    const list = plan();
    progress.textContent = `Step ${list.indexOf(state.screen) + 1} of ${list.length}`;
  }

  function render() {
    const list = plan();
    if (!list.includes(state.screen)) state.screen = list[0];
    const index = list.indexOf(state.screen);
    dialog.setAttribute('data-active-screen', state.screen);
    showProgress();
    title.textContent = SCREEN_TITLES[state.screen];
    screenNode.setAttribute('data-wizard-screen', state.screen);
    // A browser inserts the text "null" for a null child: the screens leave out what does not apply.
    screenNode.replaceChildren(...screens[state.screen]().filter((node) => node !== null && node !== undefined && node !== false));
    backBtn.hidden = index === 0;
    nextBtn.hidden = index === list.length - 1;
    doneBtn.className = index === list.length - 1 ? 'primary' : '';
    syncPoll();
  }

  function go(delta) {
    const list = plan();
    const index = Math.max(0, Math.min(list.length - 1, list.indexOf(state.screen) + delta));
    state.screen = list[index];
    state.stepStartedAt = Date.now();
    state.checkVerified = false;
    wizard.clearError();
    checkStatus.hidden = true;
    skipBtn.hidden = true;
    persist();
    render();
  }

  async function next() {
    if (CHECKED_SCREENS.includes(state.screen) && !state.checkVerified) {
      if (!(await runCheck())) return false;
    }
    go(1);
    return true;
  }

  function finish() {
    stopPoll();
    stopKeepAlive();
    if (dialog.pageMode && ctx && typeof ctx.go === 'function') ctx.go('/agents');
    else { dialog.close(); dialog.remove(); }
  }

  // ---- the check before Next (.329): the agent reached Orchestra during this screen ----------
  let baselineLastSeen = agent.last_seen_at || null;
  function isNewerThanStart(lastSeen) {
    if (!lastSeen) return false;
    const seenStr = String(lastSeen);
    if (baselineLastSeen && seenStr === String(baselineLastSeen)) return false;
    const seenMs = Date.parse(seenStr);
    if (Number.isNaN(seenMs) || seenMs < state.stepStartedAt) return false;
    if (baselineLastSeen) {
      const baseMs = Date.parse(String(baselineLastSeen));
      if (!Number.isNaN(baseMs) && seenMs <= baseMs) return false;
    }
    return true;
  }
  async function runCheck() {
    touchKeepAlive();
    let latest = null;
    if (ctx && ctx.api && typeof ctx.api.agent === 'function' && payload.agentId) {
      try { latest = await ctx.api.agent(payload.agentId); } catch (error) { wizard.showError(error); return false; }
    }
    const seen = latest && latest.last_seen_at ? String(latest.last_seen_at) : null;
    if (isNewerThanStart(seen)) {
      state.checkVerified = true;
      checkStatus.hidden = true;
      skipBtn.hidden = true;
      return true;
    }
    const what = state.screen === 'test' ? 'Run the command above' : 'Run “Orchestra Bridge: Set up” in VS Code';
    checkStatus.textContent = `Orchestra has not seen ${payload.name} since this screen opened. ${what}, then click Next again.`;
    checkStatus.setAttribute('data-check-state', 'pending');
    checkStatus.hidden = false;
    skipBtn.hidden = false;
    return false;
  }

  // ---- the online line on the last screen: a 3-second check, only while it is open and visible -
  let pollTimer = null;
  let pollDone = false;
  const isVisible = () => typeof document === 'undefined' || document.visibilityState === undefined || document.visibilityState === 'visible';
  function stopPoll() { if (pollTimer !== null) { clearInterval(pollTimer); pollTimer = null; } }
  async function pollOnce() {
    if (pollDone || state.screen !== 'resume' || !isVisible() || (!dialog.pageMode && !dialog.open)) { stopPoll(); return false; }
    if (!ctx || !ctx.api || typeof ctx.api.agent !== 'function' || !payload.agentId) return false;
    try {
      const latest = await ctx.api.agent(payload.agentId);
      // Online once the agent has been seen since the wizard opened: a last_seen_at that differs from
      // the one it had then (none at all counts), not only one newer than this screen.
      if (latest && latest.last_seen_at && (isNewerThanStart(latest.last_seen_at)
          || String(latest.last_seen_at) !== String(baselineLastSeen || ''))) {
        pollDone = true;
        stopPoll();
        onlineStatus.setAttribute('data-online-state', 'connected');
        onlineStatus.textContent = `Connected: ${payload.name} has reached Orchestra (last active ${latest.last_seen_at}).`;
        return true;
      }
    } catch { /* a transient error: the next poll tries again */ }
    return false;
  }
  function syncPoll() {
    if (pollDone || state.screen !== 'resume' || !isVisible() || (!dialog.pageMode && !dialog.open)) { stopPoll(); return; }
    if (pollTimer === null) { pollOnce(); pollTimer = setInterval(pollOnce, 3000); }
  }
  const onVisibilityChange = () => syncPoll();

  // ---- the keep-alive (.330): the session stays signed in while the wizard is open -----------
  let lastKeepAliveAt = 0;
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
  function showIdleWarning(detail) {
    const raw = typeof detail === 'number' ? detail : (detail && detail.remainingSeconds);
    const secs = typeof raw === 'number' && raw > 0 ? raw : 120;
    const mins = Math.max(1, Math.round(secs / 60));
    idleWarningBanner.replaceChildren(
      h('span', null, `Your session will expire in about ${mins} min (${secs}s) from inactivity — continue set-up to stay signed in.`),
      h('button', { type: 'button', 'data-action': 'wizard-keep-alive', onclick: async () => { await touchKeepAlive({ force: true }); idleWarningBanner.hidden = true; } }, 'Keep session open'));
    idleWarningBanner.hidden = false;
  }
  const onIdleWarningEvent = (event) => showIdleWarning(event && event.detail);
  let keepAliveHandle = null;
  function stopKeepAlive() {
    if (keepAliveHandle && typeof keepAliveHandle.stop === 'function') { try { keepAliveHandle.stop(); } catch { /* ignore */ } }
    keepAliveHandle = null;
    if (typeof document !== 'undefined' && typeof document.removeEventListener === 'function') {
      document.removeEventListener(SESSION_IDLE_WARNING_EVENT, onIdleWarningEvent);
      document.removeEventListener('visibilitychange', onVisibilityChange);
    }
  }

  dialog.addEventListener('close', () => { stopPoll(); stopKeepAlive(); dialog.remove(); });
  document.body.appendChild(dialog);
  dialog.showModal();
  if (typeof document !== 'undefined' && typeof document.addEventListener === 'function') {
    document.addEventListener(SESSION_IDLE_WARNING_EVENT, onIdleWarningEvent);
    document.addEventListener('visibilitychange', onVisibilityChange);
  }
  if (ctx && ctx.api && typeof ctx.api.keepAliveWhileVisible === 'function') {
    keepAliveHandle = ctx.api.keepAliveWhileVisible({ ...((ctx && ctx.keepAliveOptions) || {}), onWarning: showIdleWarning });
  }
  persist();
  render();
  dialog.wizard = {
    screen: () => state.screen,
    plan,
    show(screen) { if (plan().includes(screen)) { state.screen = screen; state.stepStartedAt = Date.now(); state.checkVerified = false; persist(); render(); } },
    next, back: () => go(-1), runCheck, pollOnce, stopPoll, stopKeepAlive, showError: wizard.showError,
    prompt,
  };
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
        pageHead({ title: 'Set up an agent', lede: 'Choose which agent to set up.' }),
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
      pageHead({ title: 'Set up an agent', lede: 'Create an agent on My agents first, then set it up.' }),
      h('div', { class: 'panel' },
        empty('No agent selected', 'Go to My agents to add an agent and start the step-by-step setup wizard.'),
        h('div', { class: 'panel-body' }, h('a', { class: 'btn primary', href: ctx.href('/agents') }, 'Go to My agents'))));
  }
  const certificate = await agentCertificate(ctx);
  // The dialog itself resumes the stored screen for this agent (kittrial-5bb.348).
  setupDialog(ctx, { agent }, { secret: null, certificate });
  return h('div', { class: 'stack', 'data-view': 'agent-setup-wizard' },
    pageHead({ title: `Set up ${agent.display_name || agent.name}`, lede: 'Step-by-step agent setup wizard.' }),
    h('div', { class: 'panel' },
      h('div', { class: 'panel-body' },
        h('p', { class: 'small' }, 'The step-by-step setup wizard is open for ', h('strong', null, agent.display_name || agent.name), '.'),
        h('div', { class: 'copy-row' },
          h('button', { type: 'button', class: 'primary', onclick: (e) => reopenSetup(ctx, agent.id, e.currentTarget) }, 'Reopen setup wizard'),
          h('a', { class: 'btn', href: ctx.href('/agents') }, 'Back to My agents')))));
}
