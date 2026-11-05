// kittrial-5bb.519: the approve form of web/js/views/task.js under Node (no browser; tests/web_dom_shim.mjs),
// shown the answers the real server gave a person for work under a worker credential (shapes 1 and 3).
// argv: shim URL, task.js URL, JSON {"1": {status, message}, "3": {...}, "ok": {...}}.
const [shimUrl, taskUrl, answersJson] = process.argv.slice(2);
const { Element } = await import(shimUrl);
Element.prototype.showModal = function showModal() { this.open = true; };
Element.prototype.close = function close() { this.open = false; return this.dispatch('close'); };
const task = await import(taskUrl);
const answers = JSON.parse(answersJson);

const settle = (ms = 50) => new Promise((resolve) => setTimeout(resolve, ms));
const dialogs = () => document.body.all((e) => e.tagName === 'DIALOG');
const button = (root, label) => root.all((e) => e.tagName === 'BUTTON' && e.textContent === label)[0];

async function approve(answer) {
  const seen = { renders: 0 };
  const ctx = { api: { review: async () => {
      if (answer.status >= 400) throw Object.assign(new Error(answer.message), { status: answer.status, message: answer.message });
      return { ok: true };
    } },
    render() { seen.renders += 1; }, setDirty() {}, href: (path) => '#' + path };
  const form = task.reviewActions(ctx, 'p1', 't1', { id: 'con_1', revision: 2, commit: 'a'.repeat(40) }, { latest_id: 'c1' });
  const pending = button(form, 'Approve').dispatch('click');
  await settle();
  await button(dialogs().slice(-1)[0], 'Approve').dispatch('click');
  await pending;
  await settle();
  const banner = form.all((e) => e.attributes.role === 'alert' && e.className.includes('banner'))[0];
  return { hidden: Boolean(banner.hidden), text: banner.textContent, refused: banner.attributes['data-refused'] || null, renders: seen.renders };
}
const out = {};
for (const key of Object.keys(answers)) out[key] = await approve(answers[key]);
console.log(JSON.stringify(out).replace(/[^\x00-\x7f]/g, (c) => '\\u' + c.charCodeAt(0).toString(16).padStart(4, '0')));
