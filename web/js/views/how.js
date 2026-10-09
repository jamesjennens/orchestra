// "How work is found": for every member of a project, a viewer included. Where a worker looks
// for work at each run and in what order, how whoever coordinates reaches a worker, and the
// prompts a worker is started and woken with (kittrial-5bb.226).
//
// Every text here is the kit's own, fetched from GET /v1/docs/NAME: the same files the client's
// `docs NAME` serves. The page retypes none of it. It only fills in the named values it knows
// (the server address, the project, the names of the reader's own agents) and says which are
// left to replace. No prompt contains a secret: the page never has one.
import { h, copyButton } from '../dom.js';
import { pageHead, errorState } from '../ui.js';
import { markdown } from '../md.js';
import * as setupText from '../agentSetup.js';

export const DOCUMENT = 'finding-work';
export const PROMPTS = { agent: 'poll-prompt-agent', first: 'worker-prompt', worker: 'poll-prompt' };

// Puts the values the page knows into a prompt. Returns the text and the named values still to replace.
export function fill(prompt, values) {
  let text = String(prompt || '');
  for (const [name, value] of Object.entries(values || {})) {
    if (typeof value === 'string' && value) text = text.split(name).join(value);
  }
  const left = [];
  for (const found of text.match(/\bREPLACE_[A-Z][A-Z_]*[A-Z]\b/g) || []) if (!left.includes(found)) left.push(found);
  return { text, left };
}

// The document without its first heading: the page has its own title.
export function body(text) {
  return String(text || '').replace(/^﻿?#\s+[^\n]*\n+/, '');
}

function promptBlock(key, title, doc, values, note) {
  const filled = fill(doc.prompt, values);
  return h('section', { class: 'setup-block', 'data-prompt': key, 'aria-label': title },
    h('div', { class: 'copy-row' }, h('h3', { class: 'small' }, title),
      copyButton('Copy', filled.text, { ariaLabel: 'Copy: ' + title, what: 'Prompt' })),
    h('p', { class: 'small muted' }, 'Served by the kit as ', h('code', null, doc.served_as), '. ', note || null),
    filled.left.length ? h('p', { class: 'small', 'data-left': filled.left.join(' ') }, 'Replace before use: ', filled.left.join(', '), '.') :
      h('p', { class: 'small muted', 'data-left': '' }, 'Nothing is left to replace.'),
    h('pre', { class: 'json' }, filled.text));
}

export async function page(ctx, { pid }) {
  const host = h('div', { class: 'stack' });
  const project = (ctx.projects || []).find((p) => p.id === pid) || { id: pid, name: pid };
  const head = () => pageHead({
    crumbs: [{ label: 'Projects', href: ctx.href('/projects') }, { label: project.name, href: ctx.href('/p/' + pid) }, { label: 'How work is found' }],
    title: 'How work is found',
    lede: 'Where a worker looks for work at each run, how to reach a worker, and the prompts to start and wake one. The text is the kit’s own; this page shows what it serves.',
  });
  async function draw() {
    let how, prompts, agents;
    try {
      [how, prompts] = await Promise.all([ctx.api.doc(DOCUMENT),
        Promise.all(Object.values(PROMPTS).map((name) => ctx.api.doc(name)))]);
    } catch (error) { host.replaceChildren(head(), errorState(error, draw)); return; }
    // The reader's own agents that work in this project. A reader who has none still sees the prompt.
    try { agents = ((await ctx.api.agents()).items || []); } catch { agents = []; }
    const mine = agents.filter((a) => a && ctx.me && a.owner === ctx.me.id && a.enabled !== false
      && (a.projects || []).some((p) => (typeof p === 'string' ? p : p && p.id) === pid));
    const [agentPoll, first, workerPoll] = prompts;
    // The address the service is configured with; else the one this page was loaded from.
    const server = how.server_url || location.origin;
    const forAgents = mine.length ? mine.map((agent) => {
      const name = agent.display_name || agent.name;
      return h('div', { class: 'stack', 'data-agent': agent.id },
        h('section', { class: 'setup-block', 'data-prompt': 'resume', 'aria-label': 'Resume prompt for ' + name },
          h('div', { class: 'copy-row' }, h('h3', { class: 'small' }, 'Resume ' + name + ' once, by hand'),
            copyButton('Copy', setupText.resumePrompt(name), { ariaLabel: 'Copy the resume prompt for ' + name, what: 'Prompt' })),
          h('p', { class: 'small muted' }, 'Paste it into the agent’s chat, opened in its folder. The folder needs its set-up files first (Agents page, Set up folder).'),
          h('pre', { class: 'json' }, setupText.resumePrompt(name))),
        promptBlock('agent', 'Recurring prompt for ' + name, agentPoll, { REPLACE_AGENT_NAME: name, REPLACE_SERVER_URL: server },
          'For a schedule that wakes the agent. It carries no state.'));
    }) : [h('p', { class: 'small muted', 'data-agents': '0' }, 'You have no agent in this project. Make one on the Agents page; its recurring prompt is this, with its name in it:'),
      promptBlock('agent', 'Recurring prompt for an agent', agentPoll, { REPLACE_SERVER_URL: server }, 'It carries no state.')];
    host.replaceChildren(head(),
      h('section', { class: 'panel', 'data-panel': 'how', 'aria-label': 'How work is found' },
        h('div', { class: 'panel-body prose-md' }, markdown(body(how.text), { headingBase: 2 }),
          h('p', { class: 'small muted' }, 'Served by the kit as ', h('code', null, how.served_as), '.'))),
      h('section', { class: 'panel', 'data-panel': 'agent-prompts' },
        h('div', { class: 'panel-head' }, h('h2', { class: 'small' }, 'Prompts for an agent made on the Agents page')),
        h('div', { class: 'panel-body stack' }, forAgents)),
      h('section', { class: 'panel', 'data-panel': 'worker-prompts' },
        h('div', { class: 'panel-head' }, h('h2', { class: 'small' }, 'Prompts for a worker over SSH')),
        h('div', { class: 'panel-body stack' },
          h('p', { class: 'small muted' }, 'For installations that have workers with their own actor and the kit’s client. An operator supplies the bootstrap command; the page does not know it.'),
          promptBlock('first', 'First prompt for a new worker', first, { REPLACE_PROJECT: pid }, 'Used once, when the worker is set up.'),
          promptBlock('worker', 'Recurring prompt for a worker', workerPoll, { REPLACE_PROJECT: pid }, 'For every later run. It carries no state.'))));
  }
  await draw();
  return host;
}
