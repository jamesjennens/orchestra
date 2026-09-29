// Personal agents: each belongs to the person who runs it. Nothing polls; when the
// owner opens this page they see which agent has something to do and which folder to
// open in VS Code to resume it. Agents themselves read work over the REST API.
import { h, time, toast, secretDialog } from '../dom.js';
import { pageHead, empty, field, setFieldError, formValues, act, errorState } from '../ui.js';

export const ATTENTION = {
  feedback: ['Has feedback to act on', 'warn'],
  idle: ['Could take more work', 'accent'],
  working: ['Working', ''],
  waiting: ['Waiting for review', 'ok'],
  blocked: ['Blocked', 'crit'],
};

// The server reports attention as { state, summary, counts }; the prototype's mock
// uses a flat word. One card shape serves both.
const SERVER_ATTENTION = { 'changes-requested': 'feedback', blocked: 'blocked', 'waiting-review': 'waiting', working: 'working', idle: 'idle' };
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
  try { await navigator.clipboard.writeText(text); toast(what + ' copied'); } catch { toast('Select the text and press Ctrl+C to copy'); }
}

const plain = (name) => String(name || '').replace(/\s*\(agent[^)]*\)$/, '');

export function resumePrompt(agent) {
  if (agent.server) return `You are ${plain(agent.display_name)}, an Orchestra agent. Read .orchestra/agent.json in this folder, read your secret from your credential store, and ask Orchestra for your next action at /v1/agents/me/next. Continue from there.`;
  return `You are ${plain(agent.display_name)}, an Orchestra agent. Read .orchestra/AGENT.md in this folder and follow it: ask Orchestra for your next action and continue from there.`;
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
        h('span', { class: 'resume-label' }, 'Start with'),
        h('span', { class: 'small prompt' }, resumePrompt(agent)),
        h('button', { type: 'button', class: 'ghost', onclick: () => copy(resumePrompt(agent), 'Prompt') }, 'Copy'))),
    compact ? null : h('div', { class: 'small muted' }, 'Projects: ', agent.projects.map((p) => `${p.name} (${p.role})`).join(', ') || 'none'));
}

export async function list(ctx) {
  const host = h('div', { class: 'stack' });
  async function load() {
    let data;
    try { data = await ctx.api.agents(); } catch (error) { host.replaceChildren(errorState(error, load)); return; }
    host.replaceChildren(data.items.length ? h('div', { class: 'agent-grid' }, data.items.map((a) => h('div', { class: 'stack' }, agentCard(ctx, a), editFolder(a)))) :
      h('div', { class: 'panel' }, empty('No agents yet', 'Add an agent for each assistant you run — for example a GitHub Copilot chat working in its own folder.')));
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
  load();

  const projects = ctx.projects.filter((p) => !p.archived && ['owner', 'contributor', 'superuser'].includes(p.role));
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
    const created = await act(form.querySelector('button[type=submit]'), () => ctx.api.createAgent({ name, tool: v['a-tool'].trim(), working_directory: v['a-dir'].trim(), projects: chosen }), { success: 'Agent added' }).catch(() => null);
    if (!created) return;
    form.reset();
    setupDialog(created);
    load();
  });

  return h('div', { class: 'stack' },
    pageHead({ title: 'My agents', lede: 'Agents you run on your own computer. Nothing checks in automatically: look here, see which agent has something to do, then open its folder in VS Code and start it.' }),
    host,
    h('section', { class: 'panel' }, h('div', { class: 'panel-head' }, h('h2', { class: 'small' }, 'Add an agent')), h('div', { class: 'panel-body' }, form)));
}

// Shown once after creation: the setup files for the agent's folder and its secret.
function setupDialog(created) {
  if (created.setup) return serverSetupDialog(created);
  const a = created.agent;
  const agentJson = JSON.stringify({ server: created.server, agent_id: a.id, name: plain(a.display_name), projects: a.projects.map((p) => p.id), token_env: 'ORCHESTRA_TOKEN' }, null, 2);
  const agentMd = `# ${plain(a.display_name)}\n\nYou are ${plain(a.display_name)}, an Orchestra agent owned by ${created.owner_name}.\n\n1. Read your identity from .orchestra/agent.json. The access token is in the environment variable ORCHESTRA_TOKEN — never write it to a file or commit it.\n2. GET ${created.server}/v1/agents/me/next with header "Authorization: Bearer $ORCHESTRA_TOKEN". It returns your next action: feedback to address, work to continue, or tasks you could claim.\n3. Before coding, read the task brief it links to. Record a checkpoint when you stop, and deliver work for review through the API.\n4. Stop and tell your owner if anything is unclear.`;
  const secret = created.credential.secret;
  secretDialog({
    title: `Set up ${plain(a.display_name)}`,
    body: `Create a folder .orchestra in ${a.working_directory || 'the agent’s folder'} with agent.json and AGENT.md (below), and keep it out of Git. Set ORCHESTRA_TOKEN to this token in the terminal or VS Code settings you run the agent from. The token is shown once.`,
    secret,
  });
  const dialog = document.querySelector('dialog:last-of-type .dialog-body');
  if (dialog) {
    dialog.append(
      h('h3', { class: 'small' }, '.orchestra/agent.json'), h('pre', { class: 'json' }, agentJson),
      h('h3', { class: 'small' }, '.orchestra/AGENT.md'), h('pre', { class: 'json' }, agentMd));
  }
}

// The server's own setup: a secretless config file and guidance; the secret is shown once.
function serverSetupDialog(created) {
  const setup = created.setup;
  const name = created.agent.name || created.agent.display_name;
  secretDialog({
    title: `Set up ${name}`,
    body: `${setup.guidance || ''} Save ${setup.config_path} (below) in the agent's folder and keep it out of Git.`,
    secret: created.credential.secret,
  });
  const dialog = document.querySelector('dialog:last-of-type .dialog-body');
  if (dialog) {
    dialog.append(h('h3', { class: 'small' }, setup.config_path), h('pre', { class: 'json' }, JSON.stringify(setup.config, null, 2)));
    if (setup.setup_snippet) dialog.append(h('h3', { class: 'small' }, 'Setup notes'), h('pre', { class: 'json' }, setup.setup_snippet));
  }
}
