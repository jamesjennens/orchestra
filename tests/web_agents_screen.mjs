// Runs the Agents page of web/js/views/agents.js under Node against a real http_service (started by
// tests/test_http_agents.py). No browser: tests/web_dom_shim.mjs gives dom.js and ui.js what they use.
// Prints one JSON object of observations; the Python test asserts on it.
//
// argv: shim URL, api.js URL, agents.js URL, base URL, project id, then name=userId=token triples.
const [shimUrl, apiUrl, agentsUrl, base, project, ...people] = process.argv.slice(2);
const { Element } = await import(shimUrl);
// What the page's dialogs need beyond the shim: a dialog opens, closes and can be replaced.
Element.prototype.showModal = function showModal() { this.open = true; };
Element.prototype.close = function close() { this.open = false; return this.dispatch('close'); };
Element.prototype.replaceWith = function replaceWith(other) {
  const parent = this.parentNode;
  if (parent) { parent.childNodes = parent.childNodes.map((c) => (c === this ? other : c)); other.parentNode = parent; this.parentNode = null; }
};
globalThis.window = globalThis.window || { isSecureContext: false };
globalThis.location = globalThis.location || { origin: base };
const { createApi } = await import(apiUrl);
const view = await import(agentsUrl);

const sent = [];
function person(spec) {
  const [name, id, token] = spec.split('=');
  const transport = async (method, path, headers, body) => {
    if (method !== 'GET') sent.push([name, method, path.replace(/agent_[0-9a-f]+/, 'AGENT').replace(/cred_[0-9a-f]+/, 'CRED'), body ? JSON.parse(body) : null]);
    const response = await fetch(base + path, { method, headers: { ...headers, Authorization: 'Bearer ' + token }, body });
    const text = await response.text();
    let data = null;
    try { data = text ? JSON.parse(text) : null; } catch { data = null; }
    return { status: response.status, data };
  };
  return [name, { api: createApi(transport), href: (path) => '#' + path, go() {}, query: {}, render() {}, refreshProjects: async () => {},
    me: { id, superuser: name === 'admin' }, projects: [{ id: project, name: 'Alpha', role: 'contributor' }] }];
}
const who = Object.fromEntries(people.map(person));
const out = {};
const settle = (ms = 500) => new Promise((resolve) => setTimeout(resolve, ms));
const dialogs = () => document.body.all((e) => e.tagName === 'DIALOG');
const closeDialogs = async () => { for (const d of dialogs()) { await d.close(); d.remove(); } };
const button = (root, label) => root.all((e) => e.tagName === 'BUTTON' && e.textContent === label)[0];
// Presses something that asks for confirmation, answers the dialog with `answer`, and waits for the page.
async function pressAndConfirm(control, type, answer) {
  const pending = control.dispatch(type);
  await settle(50);
  const asked = dialogs().slice(-1)[0];
  const said = asked ? asked.textContent : null;
  if (asked) await button(asked, answer).dispatch('click');
  await pending;
  await settle();
  return said;
}
const block = (page, name) => page.all((e) => e.tagName === 'DIV' && e.attributes['data-agent']).find((e) => e.all((x) => x.tagName === 'H3')[0].textContent === name);
function seen(page, name) {
  const root = block(page, name);
  const line = root.all((e) => e.attributes['data-scopes'] !== undefined && e.attributes['data-scopes-source'] !== undefined)[0];
  const banner = root.all((e) => e.attributes['data-scopes-differ'])[0];
  const panel = root.all((e) => e.attributes['data-panel'] === 'agent-scopes')[0];
  return {
    scopes: line ? line.attributes['data-scopes'] : null, source: line ? line.attributes['data-scopes-source'] : null,
    line: line ? line.textContent : null, differ: banner ? banner.attributes['data-scopes-differ'] : null,
    summary: panel ? panel.all((e) => e.tagName === 'SUMMARY')[0].textContent : null,
    rows: panel ? panel.all((e) => e.tagName === 'LI' && e.attributes['data-credential']).map((li) => ({
      working: li.attributes['data-working'], differs: li.attributes['data-differs'], text: li.textContent,
      buttons: li.all((e) => e.tagName === 'BUTTON').map((b) => b.textContent) })) : null,
    boxes: panel ? Object.fromEntries(panel.all((e) => e.tagName === 'INPUT').map((i) => [i.value, [Boolean(i.checked), Boolean(i.disabled)]])) : null,
  };
}
const boxes = (page, name) => Object.fromEntries(block(page, name).all((e) => e.tagName === 'INPUT' && e.attributes.name === 'scope').map((i) => [i.value, i]));
const form = (page, name) => block(page, name).all((e) => e.attributes['data-panel'] === 'agent-scopes')[0].all((e) => e.tagName === 'FORM')[0];
async function open(ctx) { const page = await view.list(ctx); await settle(); return page; }

// 1. Its own account opens the page: an agent with scopes on record, one harmed by an earlier renewal
//    (two working credentials that differ, nothing on record) and one with nothing that says.
{
  const page = await open(who.alex);
  out.first = { Kestrel: seen(page, 'Kestrel'), Wren: seen(page, 'Wren'), Lone: seen(page, 'Lone') };

  // 2. The harmed one: the credential that allows the four is revoked there, without a new secret first.
  //    Answered "Cancel" first: nothing is sent.
  const anyRow = () => block(page, 'Wren').all((e) => e.tagName === 'LI' && e.attributes['data-credential']).find((li) => li.textContent.includes('tasks'));
  await pressAndConfirm(button(anyRow(), 'Revoke'), 'click', 'Cancel');
  out.cancelRevokeSent = sent.length;
  const wrong = block(page, 'Wren').all((e) => e.tagName === 'LI' && e.attributes['data-credential']).find((li) => li.textContent.includes('tasks'));
  out.revokeAsked = await pressAndConfirm(button(wrong, 'Revoke'), 'click', 'Revoke');
  out.afterRevoke = seen(page, 'Wren');

  // 3. Nothing ticked: said, and nothing is sent.
  const before = sent.length;
  for (const input of Object.values(boxes(page, 'Lone'))) input.checked = false;
  await form(page, 'Lone').dispatch('submit');
  await settle(50);
  out.noneTicked = { said: form(page, 'Lone').all((e) => e.attributes.role === 'alert')[0].textContent, sent: sent.length - before, dialogs: dialogs().length };

  // 4. The one with nothing that says: reading is ticked alone; saved, it has a new secret and its scopes.
  const lone = await open(who.alex);
  out.loneAsked = await pressAndConfirm(form(lone, 'Lone'), 'submit', 'Issue new secret');
  out.loneDialog = dialogs().map((d) => d.textContent).filter((t) => t.includes('Secret (shown once)')).length;
  await closeDialogs();
  out.afterChoice = seen(lone, 'Lone');

  // 5. Its account gives Kestrel more; "Cancel" sends nothing.
  boxes(lone, 'Kestrel').tasks.checked = true;
  const cancelled = sent.length;
  await pressAndConfirm(form(lone, 'Kestrel'), 'submit', 'Cancel');
  out.cancelSent = sent.length - cancelled;
  boxes(lone, 'Kestrel').tasks.checked = true;
  out.widenAsked = await pressAndConfirm(form(lone, 'Kestrel'), 'submit', 'Issue new secret');
  await closeDialogs();
  out.afterWiden = seen(lone, 'Kestrel');
}
// 6. A superuser who is not its account: what it does not have cannot be ticked; taking away can.
{
  const page = await open(who.admin);
  out.admin = seen(page, 'Kestrel');
  boxes(page, 'Kestrel').tasks.checked = false;
  // Ticked by hand all the same: a disabled box is not sent.
  boxes(page, 'Kestrel').reviews.checked = true;
  await pressAndConfirm(form(page, 'Kestrel'), 'submit', 'Issue new secret');
  await closeDialogs();
  out.afterNarrow = seen(page, 'Kestrel');
}
// 7. "Set up folder": a plain new secret says what it carries; where nothing says, it sends to the card.
{
  const page = await open(who.alex);
  for (const name of ['Kestrel', 'Dove']) {
    await button(block(page, name), 'Set up folder').dispatch('click');
    await settle();
    const dialog = dialogs().slice(-1)[0];
    const issue = button(dialog, 'Issue a new secret');
    const entry = { needed: dialog.all((e) => e.attributes['data-scopes-needed']).length, issue: Boolean(issue) };
    if (issue) {
      entry.asked = await pressAndConfirm(issue, 'click', 'Issue new secret');
      entry.carries = dialog.all((e) => e.attributes['data-scopes'] !== undefined).map((e) => [e.attributes['data-scopes'], e.textContent]);
      entry.older = dialog.all((e) => e.tagName === 'LI' && e.textContent.includes('allows')).length;
    }
    out['setup' + name] = entry;
    await closeDialogs();
  }
}
// 8. Records the page must not fall over: no lists, lists of the wrong things, an older service.
out.odd = [
  {}, { scopes: null, scopes_source: 'unknown' }, { scopes: ['read'], credentials: 'x' }, { scopes: ['read'], credentials: [null, 5, { scopes: 'x', working: true }, { working: true }] },
  { scopes_source: 'unknown', credentials: [{ scopes: ['read'], revoked: false }, { scopes: 7, revoked: false }] }, { scopes: 'read', scopes_differ: 5, credentials: {} },
].map((raw) => { try { const line = view.scopesLine(raw); return [view.otherCredentials(raw).length, line ? line.attributes['data-scopes'] : null]; } catch (error) { return 'threw: ' + error.message; } });
out.sent = sent;
// ASCII only: the page's text has typographic quotes, and a Windows pipe is not UTF-8.
console.log(JSON.stringify(out).replace(/[^\x00-\x7f]/g, (c) => '\\u' + c.charCodeAt(0).toString(16).padStart(4, '0')));
