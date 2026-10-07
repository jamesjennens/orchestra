// Runs the feedback page of web/js/views/project.js under Node with an api that answers as
// told (tests/test_task_change_fields.py FeedbackPageTests). No browser and no server:
// tests/web_dom_shim.mjs gives dom.js and ui.js what they use. Prints one JSON object.
//
// argv: shim URL, project.js URL.
const [shimUrl, projectUrl] = process.argv.slice(2);
await import(shimUrl);
const project = await import(projectUrl);

async function page(role, feedback) {
  let asked = 0;
  const ctx = { href: (path) => '#' + path, go() {}, api: {
    project: async () => ({ id: 'alpha', name: 'Alpha', role, archived: false }),
    feedback: async () => { asked += 1; return feedback(); },
    addFeedback: async () => { throw Object.assign(new Error('not built'), { status: 501 }); } } };
  const root = await project.feedback(ctx, { pid: 'alpha' });
  await new Promise((resolve) => setTimeout(resolve, 20));
  return { text: root.textContent, forms: root.all((e) => e.tagName === 'FORM').length,
    buttons: root.all((e) => e.tagName === 'BUTTON').map((b) => b.textContent),
    status: root.all((e) => e.attributes.role === 'status').length,
    alerts: root.all((e) => e.attributes.role === 'alert').length, asked };
}
const refuse = (status) => () => { throw Object.assign(new Error('refused'), { status, message: 'refused' }); };
const out = {
  notBuilt: await page('contributor', refuse(501)),
  notBuiltViewer: await page('viewer', refuse(501)),
  broken: await page('contributor', refuse(500)),
  empty: await page('contributor', () => ({ items: [], total: 0 })),
  emptyViewer: await page('viewer', () => ({ items: [], total: 0 })),
};
console.log(JSON.stringify(out));
