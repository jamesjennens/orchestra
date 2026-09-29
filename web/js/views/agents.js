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
  if (await copyText(text)) toast(what + ' copied'); else toast('Select the text and press Ctrl+C to copy');
}

// The same instruction in both modes: the setup dialog writes .orchestra/AGENT.md and
// this prompt tells the agent to read exactly that file.
export function resumePrompt(agent) {
  return setupText.resumePrompt(agent.display_name || agent.name);
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

// ---- one-time setup dialog ---------------------------------------------------
//
// Shown once after creation. The secret appears only in its own box and its own copy
// button. Everything that can be saved to disk or pasted into the agent's chat is built
// by agentSetup.js from secretlessPayload(), which never reads the credential.

const canSaveToFolder = () => typeof window.showDirectoryPicker === 'function' && Boolean(window.isSecureContext);

// Writes .orchestra/agent.json and .orchestra/AGENT.md into a folder the user picks, and
// appends .orchestra/ to an EXISTING .gitignore that lacks it. Creates or rewrites
// nothing else. `files` is [[name, text], ...] built from the secretless payload.
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
    const addition = setupText.gitignoreAppend(await current.text());
    if (addition) {
      const out = await ignore.createWritable({ keepExistingData: true });
      await out.seek(current.size);
      await out.write(addition);
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

function setupDialog(created) {
  const secret = created.credential && created.credential.secret;
  const payload = setupText.secretlessPayload(created, location.origin);
  const files = [['agent.json', setupText.agentJson(payload)], ['AGENT.md', setupText.agentGuide(payload)]];
  const folder = payload.workingDirectory;
  const where = folder ? h('code', { class: 'path' }, folder) : 'the agent’s folder';

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
      h('span', { class: 'small muted', id: 'setup-save-hint' }, 'Pick ', where, '. Writes .orchestra/agent.json and .orchestra/AGENT.md, and adds .orchestra/ to an existing .gitignore. The secret is never saved.'));
  } else {
    save = h('p', { class: 'small muted', role: 'note' }, 'Saving directly needs Edge or Chrome on an https or localhost address. Use the copy buttons below instead.');
  }

  const prompt = setupText.setupPrompt(payload);
  const dialog = h('dialog', { class: 'dialog-wide', 'aria-labelledby': 'setup-title' });
  dialog.append(
    h('div', { class: 'dialog-body' },
      h('h2', { id: 'setup-title' }, `Set up ${payload.name}`),
      h('p', null, 'Put two small files in ', where, ': ', h('code', null, setupText.CONFIG_PATH), ' and ', h('code', null, setupText.GUIDE_PATH),
        '. Keep .orchestra/ out of Git. Neither file holds the secret: keep it in VS Code secret storage or your operating system’s credential store. It is shown only once, here.'),
      h('section', { class: 'setup-block', 'aria-label': 'Secret' },
        h('div', { class: 'copy-row' }, h('h3', { class: 'small' }, 'Secret (shown once)'), copyButton('Copy', secret, { ariaLabel: 'Copy the secret', what: 'Secret' })),
        h('div', { class: 'secret' }, secret)),
      h('section', { class: 'setup-block', 'aria-label': 'Save the setup files' }, save, status),
      h('section', { class: 'setup-block', 'aria-label': 'Agent prompt' },
        h('div', { class: 'copy-row' }, h('h3', { class: 'small' }, 'Or let the agent do it'), copyButton('Copy agent prompt', prompt, { what: 'Agent prompt' })),
        h('p', { class: 'small muted' }, 'Paste this into the agent’s own chat, opened in its folder. It creates both files, updates .gitignore in a Git repository, then continues. It does not contain the secret.'),
        h('details', null, h('summary', null, 'Show the prompt'), h('pre', { class: 'json' }, prompt))),
      fileBlock(setupText.CONFIG_PATH, files[0][1], folder, setupText.CONFIG_PATH),
      fileBlock(setupText.GUIDE_PATH, files[1][1], folder, setupText.GUIDE_PATH),
      created.setup && created.setup.setup_snippet ? h('details', null, h('summary', null, 'Shell commands instead (optional)'), h('pre', { class: 'json' }, created.setup.setup_snippet)) : null),
    h('div', { class: 'dialog-foot' }, h('button', { type: 'button', class: 'primary', onclick: () => { dialog.close(); dialog.remove(); } }, 'Done')));
  dialog.addEventListener('close', () => dialog.remove());
  document.body.appendChild(dialog);
  dialog.showModal();
}
