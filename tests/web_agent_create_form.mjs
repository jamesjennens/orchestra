// The Add-an-agent form of web/js/views/agents.js under Node with a stub api (no service).
// argv: shim URL, agents.js URL. Prints one JSON object of observations.
const [shimUrl, agentsUrl] = process.argv.slice(2);
const { Element, nodeList } = await import(shimUrl);
const originalQueryAll = Element.prototype.querySelectorAll;
Element.prototype.querySelectorAll = function (selector) {
  if (selector === 'input[name=project]:checked') return nodeList(this.all((e) => e.tagName === 'INPUT' && e.attributes.name === 'project' && e.checked));
  return originalQueryAll.call(this, selector);
};
Element.prototype.showModal = function showModal() { this.open = true; };
Element.prototype.close = function close() { this.open = false; };
Element.prototype.reset = function reset() { for (const e of this.all((x) => x.tagName === 'INPUT')) e.value = ''; };
globalThis.window = { isSecureContext: false };
globalThis.location = { origin: 'http://stub.invalid' };
const agents = await import(agentsUrl);
const created = [];
const api = {
  agents: async () => ({ items: [], total: 0, next_cursor: null }),
  createAgent: async (body) => { created.push(body); return { agent: { id: 'agent_1', name: body.name, tool: body.tool || null, projects: [] }, credential: null }; },
  agentCertificate: async () => null,
};
const ctx = { api, me: { id: 'u1', superuser: false }, projects: [], href: (p) => '#' + p, go() {}, query: {}, render() {}, refreshProjects: async () => {} };
const page = await agents.list(ctx);
await new Promise((r) => setTimeout(r, 100));
const tool = page.querySelector('#a-tool');
const dir = page.querySelector('#a-dir');
const out = {
  toolValue: tool.value,
  toolPlaceholder: tool.attributes.placeholder || '',
  toolHint: page.querySelector('#a-tool-hint').textContent,
  dirHint: page.querySelector('#a-dir-hint').textContent,
  pageText: page.textContent,
};
page.querySelector('#a-name').value = 'Kestrel';
const form = page.all((e) => e.tagName === 'FORM' && e.querySelector('#a-name'))[0];
await form.dispatch('submit');
await new Promise((r) => setTimeout(r, 300));
out.sentTool = created.length ? created[0].tool : null;
console.log(JSON.stringify(out));
