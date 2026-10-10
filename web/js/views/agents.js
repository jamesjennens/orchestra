// Personal agents: each belongs to the person who runs it. Nothing polls; when the
// owner opens this page they see which agent has something to do and which folder to
// open in VS Code to resume it. Agents themselves read work over the REST API.
import { h, time, toast, copyText, copyButton, confirmDialog } from '../dom.js';
import { pageHead, empty, field, setFieldError, formValues, act, errorState } from '../ui.js';
import * as setupText from '../agentSetup.js';

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
export const SCOPES = [['read', 'Read'], ['tasks', 'Claim and change tasks'], ['checkpoints', 'Write checkpoints'],
  ['reviews', 'Deliver and review'], ['feedback', 'Send feedback'], ['proposals', 'Propose requirements']];
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
    host.replaceChildren(items.length ? h('div', { class: 'agent-grid' }, items.map((a) => h('div', { class: 'stack', 'data-agent': a.id }, agentCard(ctx, a), editFolder(a), manages(ctx, a) ? editProjects(a) : null, manages(ctx, a) ? editScopes(a) : null))) :
      h('div', { class: 'panel' }, empty('No agents yet', 'Add an agent for each assistant you run — for example a GitHub Copilot chat working in its own folder.')));
    host.append(h('div', { class: 'copy-row', 'data-agent-pages': '' },
      h('span', { class: 'small muted' }, `${items.length} shown of ${data.total} agents`),
      previous.length ? h('button', { type: 'button', onclick: () => { cursor = previous.pop(); load(); } }, 'Previous agents') : null,
      data.next_cursor ? h('button', { type: 'button', onclick: () => { previous.push(cursor); cursor = data.next_cursor; load(); } }, 'Next agents') : null));
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
      if (working && agent.enabled !== false) row.append(h('button', { type: 'button', class: 'danger', 'aria-label': 'Revoke credential ' + (c.label || c.id), onclick: async (event) => {
        if (!(await confirmDialog({ title: 'Revoke this credential?', body: 'Anything still using its secret stops working immediately.', confirmLabel: 'Revoke', danger: true }))) return;
        try { await act(event.currentTarget, () => ctx.api.revokeAgentCredential(agent.id, c.id), { success: 'Credential revoked' }); } catch { return; }
        load();
      } }, 'Revoke'));
      return row;
    });
    // Ticked: what it has; with nothing known, reading alone. Only its own account may give it more.
    const ticked = new Set(known ? has : ['read']);
    const form = h('form', { class: 'form', novalidate: true },
      h('fieldset', { class: 'checks' }, h('legend', null, 'What it may do'),
        SCOPES.map(([scope, label]) => h('label', { class: 'check', for: `sc-${agent.id}-${scope}` },
          h('input', { type: 'checkbox', id: `sc-${agent.id}-${scope}`, name: 'scope', value: scope, checked: ticked.has(scope) ? true : null,
            disabled: !own && !has.includes(scope) ? true : null }), label)),
        h('p', { class: 'small muted' }, own ? inferredWarning + 'Saving issues a new secret that carries exactly these, and a later new secret carries them too. A credential it already has keeps what it allows until you revoke it above.' :
          'Only the agent’s own account may give it more than it has; you may take something away. Saving issues a new secret.')),
      h('p', { class: 'error', role: 'alert', hidden: true }),
      h('div', null, h('button', { type: 'submit' }, 'Save and issue a new secret')));
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      const problem = form.querySelector('p[role=alert]');
      const chosen = [...form.querySelectorAll('input')].filter((i) => i.getAttribute('name') === 'scope' && i.checked && !i.disabled).map((i) => i.value);
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
    return ctx.projects.filter((p) => !p.archived && ['owner', 'contributor', 'superuser'].includes(p.role));
  }
  const projects = grantable();
  const form = h('form', { class: 'form', novalidate: true },
    h('div', { class: 'form-row' },
      field({ id: 'a-name', label: 'Agent name', hint: 'How colleagues will see it, e.g. “Kestrel”.', required: true, maxlength: 40 }),
      field({ id: 'a-tool', label: 'Runs in', value: 'GitHub Copilot in VS Code' })),
    field({ id: 'a-dir', label: 'Folder on your computer', placeholder: 'C:\\Users\\you\\agents\\kestrel', hint: 'The folder you open in VS Code to run this agent. Its setup file goes here. Only you and administrators can see it.' }),
    h('fieldset', { class: 'checks' }, h('legend', null, 'Projects it can work on'),
      projects.length ? projects.map((p) => h('label', { class: 'check', for: 'ap-' + p.id }, h('input', { type: 'checkbox', id: 'ap-' + p.id, name: 'project', value: p.id }), p.name)) :
        h('p', { class: 'small muted' }, 'You need contributor access to a project first.'),
      h('p', { class: 'small muted' }, 'It works as a contributor and can never do more than you can in that project.')),
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
    const created = await act(form.querySelector('button[type=submit]'), () => ctx.api.createAgent({ name, tool: v['a-tool'].trim(), working_directory: v['a-dir'].trim(), projects: chosen }), {
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
// from the secretless payload.
async function saveSetupFiles(files) {
  let root;
  try {
    root = await window.showDirectoryPicker({ id: 'orchestra-agent', mode: 'readwrite' });
  } catch (error) {
    if (error && error.name === 'AbortError') return { cancelled: true };
    throw error;
  }
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
function copyLine(label, text, { ariaLabel, what, note } = {}) {
  return h('div', { class: 'setup-line' },
    h('div', { class: 'copy-row' }, h('span', { class: 'small' }, label), copyButton('Copy', text, { ariaLabel: ariaLabel || 'Copy: ' + label, what: what || label })),
    h('code', { class: 'path' }, text),
    note ? h('p', { class: 'small muted' }, note) : null);
}

// Step 1's numbered instructions. `line` is the header line to paste: the one built
// with the real secret (only from secretSection) or the placeholder line.
function secretSteps(payload, line, { withSecret }) {
  const file = payload.secretFile;
  const test = setupText.testCommands(payload);
  return h('ol', { class: 'setup-substeps' },
    h('li', null, 'Open Notepad (on macOS or Linux, a text editor).'),
    h('li', null, copyLine(withSecret ? 'Paste this one line (it contains the secret):' : 'Paste this one line, with your secret in place of <your secret>:', line,
      { ariaLabel: withSecret ? 'Copy the header line with the secret' : 'Copy the header line template', what: 'Header line' })),
    h('li', null,
      copyLine('File > Save As, set “Save as type” to “All files (*.*)”, and use this file name:', file.windows,
        { ariaLabel: 'Copy the Windows secret file name', what: 'File name', note: 'Windows file dialogs understand %USERPROFILE%. Keep the name exactly, with no .txt at the end.' }),
      copyLine('On macOS or Linux, save it as this file instead, then restrict it:', file.posix, { ariaLabel: 'Copy the macOS/Linux secret file name', what: 'File name' }),
      copyLine('…and run:', test.posixChmod, { ariaLabel: 'Copy the chmod command', what: 'Command' })),
    h('li', null,
      copyLine('Test it in PowerShell:', test.powershell, { ariaLabel: 'Copy the PowerShell test command', what: 'Command' }),
      copyLine('On macOS or Linux:', test.posix, { ariaLabel: 'Copy the macOS/Linux test command', what: 'Command' }),
      h('p', { class: 'small muted' }, 'Success prints the agent’s details as JSON (its id and name). “The requested URL returned error: 401” means the secret or the header line is wrong. “cannot read config from …” (curl exit code 26) means the file name is wrong or Notepad added .txt.')),
    h('li', null, 'Never put the secret on a command line, in the agent folder, in agent.json, in AGENT.md or in the setup prompt. Those only name the file above.'));
}

// The one-time secret, in its own box with its own copy button, and the step-1 line
// built from it. This is the ONLY place a secret is rendered; nothing else in the
// dialog receives it.
function secretSection(secret, heading, payload) {
  return h('div', { class: 'setup-block' },
    h('div', { class: 'copy-row' }, h('h4', { class: 'small' }, heading), copyButton('Copy', secret, { ariaLabel: 'Copy the secret', what: 'Secret' })),
    h('div', { class: 'secret' }, secret),
    h('p', { class: 'small muted' }, 'It is not shown again. Store it now:'),
    secretSteps(payload, setupText.headerLine(secret), { withSecret: true }));
}

// Reopened dialog: no secret. The secret was shown once when its credential was
// issued; if it is lost, issue a NEW credential (the old secret is never re-shown).
// ": read, tasks" for a list of scopes, or "" when the server sent none (an older service).
function scopeList(scopes) {
  return Array.isArray(scopes) && scopes.length ? ': ' + scopes.join(', ') : '';
}
function reissueSection(ctx, agent, payload) {
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
    section.replaceChildren(secretSection(credential.secret, 'New secret (shown once)', payload),
      h('p', { class: 'small', 'data-scopes': (credential.scopes || []).join(' ') }, 'This credential carries' + (scopeList(credential.scopes) || ': nothing beyond reading') + '.'),
      olderCredentials(ctx, agent, credential.id));
  } }, 'Issue a new secret');
  section.append(
    h('p', { class: 'small muted' }, 'The secret was shown once, when the agent’s credential was issued, and cannot be shown again. If you stored it as below, nothing more is needed here. If it is lost, issue a new one.'),
    h('div', { class: 'copy-row' }, issue),
    h('details', null, h('summary', null, 'How the secret is stored'), secretSteps(payload, setupText.headerLine(), { withSecret: false })));
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

// `source` is the create response ({agent, setup, ...}) or {agent} from GET
// /v1/agents/{id}; `secret` is present only straight after creation; `certificate` is the
// service's own certificate (GET /v1/service/certificate) or null when it has none.
function setupDialog(ctx, source, { secret = null, certificate = null } = {}) {
  const payload = setupText.secretlessPayload(source, location.origin);
  const files = [['agent.json', setupText.agentJson(payload)], ['AGENT.md', setupText.agentGuide(payload)]];
  const folder = payload.workingDirectory;
  const where = folder ? h('code', { class: 'path' }, folder) : 'the agent’s folder';
  // The address the extension must use is the one this page is connected to, not the configured
  // public address, which may be a name that does not resolve from this person's network.
  const bridgeServer = location.origin;
  let role = 'worker';
  const prompt = setupText.setupPrompt(payload, { role, certificate, bridgeServer });
  const promptText = (chosen) => setupText.setupPrompt(payload, { role: chosen, certificate, bridgeServer });

  const status = h('p', { class: 'small', role: 'status', hidden: true });
  let save;
  if (canSaveToFolder()) {
    save = h('div', { class: 'copy-row' },
      h('button', { type: 'button', class: 'primary', 'aria-describedby': 'setup-save-hint', onclick: async (event) => {
        const button = event.currentTarget;
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
  const roleChoice = h('label', { class: 'small', for: 'setup-role' }, 'What this agent does in this window: ',
    h('select', { id: 'setup-role', onchange: (event) => { role = event.target.value; drawPrompt(promptText(role)); } },
      h('option', { value: 'worker' }, 'works on tasks and answers reviews'),
      h('option', { value: 'reviewer' }, 'reviews other agents’ deliveries')));
  const certificateBlock = certificate ? h('section', { class: 'setup-block', 'aria-label': 'The service’s own certificate' },
    h('div', { class: 'copy-row' }, h('h4', { class: 'small' }, 'The service’s own certificate'),
      copyButton('Copy', certificate.sha256, { ariaLabel: 'Copy the certificate fingerprint', what: 'Fingerprint' }),
      h('button', { type: 'button', onclick: () => saveCertificate(certificate, payload.certificateFile) }, 'Save certificate')),
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
  const step = (number, title, ...body) => h('li', { class: 'setup-step', 'aria-label': `Step ${number}: ${title}` },
    h('h3', { class: 'small' }, `${number}. ${title}`), ...body);
  const dialog = h('dialog', { class: 'dialog-wide', 'aria-labelledby': 'setup-title' });
  dialog.append(
    h('div', { class: 'dialog-body' },
      h('h2', { id: 'setup-title' }, `Set up ${payload.name}`),
      h('p', { class: 'small muted' }, 'Three steps. Each has its own Copy buttons.'),
      h('ol', { class: 'setup-steps' },
        step(1, 'Store the secret',
          h('p', { class: 'small' }, 'The agent reads its secret from one small file in your user profile, never from its own folder: ', h('code', null, payload.secretFile.windows), '.'),
          secret ? secretSection(secret, 'Secret (shown once)', payload) : reissueSection(ctx, source.agent || {}, payload)),
        step(2, 'Set up the folder',
          h('p', { class: 'setup-intro' }, SETUP_INTRO),
          h('p', { class: 'small' }, 'The folder gets two small files, ', h('code', null, setupText.CONFIG_PATH), ' and ', h('code', null, setupText.GUIDE_PATH),
            ', in ', where, '. Keep .orchestra/ out of Git. Neither file holds the secret; they only name the file from step 1.'),
          h('p', { class: 'small', 'data-writes': 'setup' }, SETUP_WRITES_LINE),
          roleChoice,
          certificateBlock,
          h('section', { class: 'setup-block', 'aria-label': 'Save the setup files' }, save, status),
          promptSection,
          fileBlock(setupText.CONFIG_PATH, files[0][1], folder, setupText.CONFIG_PATH),
          fileBlock(setupText.GUIDE_PATH, files[1][1], folder, setupText.GUIDE_PATH),
          source.setup && source.setup.setup_snippet ? h('details', null, h('summary', null, 'Shell commands instead (optional)'), h('pre', { class: 'json' }, source.setup.setup_snippet)) : null),
        step(3, 'Start the agent',
          h('p', { class: 'small' }, 'Open ', where, ' in VS Code (File > Open Folder…), open the agent’s chat, and paste the resume prompt. Do this after steps 1 and 2.'),
          h('p', { class: 'small', 'data-once': 'true' }, SETUP_ONCE_LINE),
          folder ? copyLine('Folder:', folder, { ariaLabel: 'Copy the agent folder path', what: 'Folder path' }) : null,
          copyLine('Resume prompt:', resume, { ariaLabel: 'Copy the resume prompt', what: 'Resume prompt' })))),
    h('div', { class: 'dialog-foot' }, h('button', { type: 'button', class: 'primary', onclick: () => { dialog.close(); dialog.remove(); } }, 'Done')));
  dialog.addEventListener('close', () => dialog.remove());
  document.body.appendChild(dialog);
  dialog.showModal();
}
