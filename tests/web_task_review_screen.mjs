// Runs the review form of web/js/views/task.js under Node (no browser; tests/web_dom_shim.mjs).
// The API is a stand-in that answers what the test says. Prints one JSON object of observations.
//
// argv: shim URL, task.js URL.
const [shimUrl, taskUrl] = process.argv.slice(2);
const { Element } = await import(shimUrl);
Element.prototype.showModal = function showModal() { this.open = true; };
Element.prototype.close = function close() { this.open = false; return this.dispatch('close'); };
const task = await import(taskUrl);

const settle = (ms = 50) => new Promise((resolve) => setTimeout(resolve, ms));
const dialogs = () => document.body.all((e) => e.tagName === 'DIALOG');
const button = (root, label) => root.all((e) => e.tagName === 'BUTTON' && e.textContent === label)[0];
const SENTENCE = 'This contribution was delivered by your own party: you, one of your agents, or a name that a worker credential you issued holds or has held. Somebody of another party who may approve here must approve it.';

async function approve(answer) {
  const seen = { renders: 0, sent: [] };
  const ctx = { api: { review: async (pid, tid, body) => { seen.sent.push(body.operation); return answer(); } },
    render() { seen.renders += 1; }, setDirty() {}, href: (path) => '#' + path };
  const form = task.reviewActions(ctx, 'p1', 't1', { id: 'con_1', revision: 2, commit: 'a'.repeat(40) }, { latest_id: 'c1' });
  const pending = button(form, 'Approve').dispatch('click');
  await settle();
  const asked = dialogs().slice(-1)[0];
  seen.asked = asked.textContent;
  await button(asked, 'Approve').dispatch('click');
  await pending;
  await settle();
  const banner = form.all((e) => e.attributes.role === 'alert' && e.className.includes('banner'))[0];
  seen.banner = { hidden: Boolean(banner.hidden), text: banner.textContent, refused: banner.attributes['data-refused'] || null };
  return seen;
}
const refuse = (status, message) => () => { throw Object.assign(new Error(message), { status, message }); };
const out = {
  refused: await approve(refuse(403, SENTENCE)),
  stale: await approve(refuse(409, 'A newer revision exists')),
  approved: await approve(() => ({ ok: true })),
};
console.log(JSON.stringify(out).replace(/[^\x00-\x7f]/g, (c) => '\\u' + c.charCodeAt(0).toString(16).padStart(4, '0')));
