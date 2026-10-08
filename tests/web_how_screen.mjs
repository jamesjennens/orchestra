// Runs the page "How work is found" (web/js/views/how.js) under Node against a real http_service
// (started by tests/test_finding_work.py). No browser: tests/web_dom_shim.mjs gives dom.js and
// ui.js what they use. Prints one JSON object of observations; the Python test asserts on it.
//
// argv: shim URL, api.js URL, how.js URL, base URL, project id, then name=userId=token triples.
const [shimUrl, apiUrl, howUrl, base, project, ...people] = process.argv.slice(2);
await import(shimUrl);
globalThis.window = globalThis.window || { isSecureContext: false };
globalThis.location = globalThis.location || { origin: 'http://page.origin.example' };
const { createApi } = await import(apiUrl);
const view = await import(howUrl);

let failing = null;
function person(spec) {
  const [name, id, token] = spec.split('=');
  const transport = async (method, path, headers, body) => {
    if (failing && path.includes(failing)) return { status: 503, data: { error: { code: 'unavailable', message: 'The document could not be read.' } } };
    const response = await fetch(base + path, { method, headers: { ...headers, Authorization: 'Bearer ' + token }, body });
    const text = await response.text();
    let data = null;
    try { data = text ? JSON.parse(text) : null; } catch { data = null; }
    return { status: response.status, data };
  };
  return [name, { api: createApi(transport), href: (path) => '#' + path, go() {}, query: {}, render() {},
    me: { id }, projects: [{ id: project, name: 'Alpha', role: name === 'vera' ? 'viewer' : 'owner' }] }];
}
const who = Object.fromEntries(people.map(person));
const out = {};

function seen(page) {
  const panel = (name) => page.all((e) => e.tagName === 'SECTION' && e.attributes['data-panel'] === name)[0];
  const how = panel('how');
  const blocks = (root) => root.all((e) => e.tagName === 'SECTION' && e.attributes['data-prompt']).map((block) => ({
    prompt: block.attributes['data-prompt'], title: block.all((e) => e.tagName === 'H3')[0].textContent,
    text: block.all((e) => e.tagName === 'PRE')[0].textContent,
    left: (block.all((e) => e.attributes['data-left'] !== undefined)[0] || { attributes: {} }).attributes['data-left'],
    served: block.all((e) => e.tagName === 'CODE').map((c) => c.textContent),
    buttons: block.all((e) => e.tagName === 'BUTTON').map((b) => b.textContent) }));
  return {
    title: page.all((e) => e.tagName === 'H1')[0].textContent,
    headings: how.all((e) => /^H[2-6]$/.test(e.tagName)).map((e) => e.tagName + ' ' + e.textContent),
    howText: how.textContent,
    agents: panel('agent-prompts').all((e) => e.attributes['data-agent']).length,
    noAgents: panel('agent-prompts').all((e) => e.attributes['data-agents'] === '0').length,
    agentBlocks: blocks(panel('agent-prompts')), workerBlocks: blocks(panel('worker-prompts')),
  };
}

// 1. Its owner, who has an agent in the project: the text, and the prompts with the agent's name.
out.owner = seen(await view.page(who.alex, { pid: project }));
// 2. A viewer, who has none: the same text, and the agent prompt with the name left to replace.
out.viewer = seen(await view.page(who.vera, { pid: project }));
// 3. A document that cannot be read is said; nothing is shown half.
failing = '/v1/docs/poll-prompt';
{
  const page = await view.page(who.alex, { pid: project });
  out.unreadable = { panels: page.all((e) => e.tagName === 'SECTION' && e.attributes['data-panel']).length, text: page.textContent };
}
failing = null;
// 4. What the page puts into a prompt, and what it leaves.
out.fill = [
  view.fill('A REPLACE_ONE b REPLACE_TWO c REPLACE_ONE', { REPLACE_ONE: 'x' }),
  view.fill('A REPLACE_ONE', { REPLACE_ONE: '' }),
  view.fill('nothing here', { REPLACE_ONE: 'x' }),
  view.fill(null, null),
];
out.body = view.body('# Title\n\nFirst paragraph.\n\n## Next\n');
console.log(JSON.stringify(out).replace(/[^\x00-\x7f]/g, (c) => '\\u' + c.charCodeAt(0).toString(16).padStart(4, '0')));
