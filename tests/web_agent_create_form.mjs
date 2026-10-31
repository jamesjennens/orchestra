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
let releaseAgents; let releaseAccounts;
const agentsGate = new Promise((r) => { releaseAgents = r; });
const accountsGate = new Promise((r) => { releaseAccounts = r; });
const cred = (id, working = true) => ({ id, label: 'web: ' + id, scopes: ['read'], working, revoked: !working, created_at: '2026-10-01T00:00:00Z' });
const sample = [
  { id: 'agent_one', name: 'Kestrel', display_name: 'Kestrel', owner: 'u1', enabled: true, scopes: ['read'], scopes_source: 'stored', projects: [], working_directory: 'C:\\k', credentials: [cred('c1')] },
  { id: 'agent_three', name: 'Wren', display_name: 'Wren', owner: 'u1', enabled: true, scopes: ['read'], scopes_source: 'stored', projects: [], working_directory: 'C:\\w', credentials: [cred('c2'), cred('c3'), cred('c4'), cred('c5', false)] },
  { id: 'agent_unknown', name: 'Lark', display_name: 'Lark', owner: 'u1', enabled: true, scopes: null, scopes_source: 'none', projects: [], working_directory: 'C:\\l', credentials: [cred('c6')] },
  { id: 'agent_inferred', name: 'Pipit', display_name: 'Pipit', owner: 'u1', enabled: true, scopes: ['read'], scopes_source: 'inferred', projects: [], working_directory: 'C:\\p', credentials: [cred('c7')] },
  { id: 'agent_made', name: 'Swift', display_name: 'Swift', owner: 'u1', created_by: 'u2', created_by_display_name: 'Sam', enabled: true, scopes: ['read'], scopes_source: 'stored', projects: [], working_directory: 'C:\\s', credentials: [cred('c8')] },
];
const api = {
  agents: async () => { await agentsGate; return { items: sample, total: sample.length, next_cursor: null }; },
  accounts: async () => { await accountsGate; return { items: [{ id: 'u1', username: 'olive' }, { id: 'u2', username: 'sam' }] }; },
  agentHistory: async () => ({ items: [] }),
  createAgent: async (body) => { created.push(body); return { agent: { id: 'agent_1', name: body.name, tool: body.tool || null, projects: [] }, credential: { id: 'cred_n', secret: 'orc_NEW_SECRET' } }; },
  agentCertificate: async () => null,
  agent: async (id) => sample.find((a) => a.id === id),
};
const ctx = { api, me: { id: 'u1', superuser: true }, projects: [], href: (p) => '#' + p, go() {}, query: {}, render() {}, refreshProjects: async () => {} };
const tick = () => new Promise((r) => setTimeout(r, 20));
const early = await agents.list(ctx);
const shape = (page) => [...page.childNodes].map((c) => (c.tagName || '') + ':' + (c.getAttribute && (c.getAttribute('data-agents-host') !== null ? 'host' : c.getAttribute('class')) || ''));
const out = {};
out.structureAtOnce = early.all((e) => e.getAttribute('data-agents-loading') !== null).length === 1
  && early.querySelector('#a-name') !== null && early.querySelector('#a-owner') !== null;
out.ownerChoiceStartsWithYourAccount = early.querySelector('#a-owner').all((e) => e.tagName === 'OPTION').length === 1;
const orderBefore = shape(early).join('|');
releaseAgents(); releaseAccounts(); await tick(); await tick();
out.sameOrderAfterTheAgentsArrive = shape(early).join('|') === orderBefore;
out.placeholderReplacedInPlace = early.all((e) => e.getAttribute('data-agents-loading') !== null).length === 0
  && early.all((e) => e.getAttribute('data-agent') !== null).length === sample.length;
out.accountsFillTheOwnerChoiceInPlace = early.querySelector('#a-owner').all((e) => e.tagName === 'OPTION').length === 2;
const row = (id) => early.all((e) => e.getAttribute('data-agent') === id)[0];
const details = (id) => row(id).all((e) => e.getAttribute('data-agent-details') === id)[0];
const outside = (id) => { const d = details(id); return row(id).textContent.replace(d.textContent, ''); };
out.oneWorkingCredentialKeepsDetailsClosed = details('agent_one').getAttribute('open') === null;
out.severalWorkingCredentialsOpenDetails = details('agent_three').getAttribute('open') !== null;
out.rowSaysHowManyWork = /3 working credentials/.test(row('agent_three').all((e) => e.getAttribute('data-credential-count') !== null)[0].textContent);
out.everyWorkingCredentialHasItsRevoke = ['c2', 'c3', 'c4'].every((c) => row('agent_three').all((e) => e.getAttribute('data-credential') === c)[0]
  .all((e) => e.tagName === 'BUTTON' && e.textContent === 'Revoke').length === 1)
  && row('agent_three').all((e) => e.getAttribute('data-credential') === 'c5')[0].all((e) => e.tagName === 'BUTTON' && e.textContent === 'Revoke').length === 0;
out.rowKeepsOnlyTheKeyAction = outside('agent_one').includes('Set up folder') && !outside('agent_one').includes('Resume prompt') && !outside('agent_one').includes('Projects:');
out.detailsHoldTheRest = details('agent_one').textContent.includes('Resume prompt') && details('agent_one').textContent.includes('Projects:');
// What the closed Details would hide must open them (the instruction is not cosmetic).
const opens = (id, needle) => details(id).getAttribute('open') !== null && details(id).textContent.includes(needle);
out.unknownScopesOpenDetailsWithTheInstruction = opens('agent_unknown', 'Nothing says what this agent may do');
out.inferredScopesOpenDetailsWithTheNotice = opens('agent_inferred', 'inferred from working credentials');
out.aDifferentCreatorOpensDetailsWithTheNote = opens('agent_made', 'That superuser saw the first secret');
const page = early;
const tool = page.querySelector('#a-tool');
const dir = page.querySelector('#a-dir');
Object.assign(out, {
  toolValue: tool.value,
  toolPlaceholder: tool.attributes.placeholder || '',
  toolHint: page.querySelector('#a-tool-hint').textContent,
  dirHint: page.querySelector('#a-dir-hint').textContent,
  pageText: page.textContent,
});
page.querySelector('#a-name').value = 'Kestrel';
const form = page.all((e) => e.tagName === 'FORM' && e.querySelector('#a-name'))[0];
await form.dispatch('submit');
await new Promise((r) => setTimeout(r, 300));
out.sentTool = created.length ? created[0].tool : null;
const dialog = document.body.all((e) => e.tagName === 'DIALOG')[0];
const dialogText = dialog ? dialog.textContent : '';
out.createDialogSaysYouNeedNotSaveItNow = /You do not need to save this now/.test(dialogText) && /Set up folder/.test(dialogText) && /coordinator grant/.test(dialogText);
out.createDialogSaysSaveAndNeverPasteInAChat = /Save this secret now, in the file named below\./.test(dialogText) && /Never paste it into a chat\./.test(dialogText);
console.log(JSON.stringify(out));
