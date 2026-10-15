// Runs the task detail page of web/js/views/task.js under Node against a stubbed
// transport (the project read, the task brief and the history), so no http_service is
// needed. It checks the part of kittrial-5bb.277 review item 2 that the second review
// left open: the detail page must not offer "Claim task" on a row the server calls not
// claimable, and must still offer it on ordinary unclaimed work.
// tests/web_dom_shim.mjs gives dom.js and ui.js what they use. Prints one JSON object of
// observations; tests/test_requirement_task_page.py asserts on it.
//
// argv: shim URL, api.js URL, task.js URL.
const [shimUrl, apiUrl, taskUrl] = process.argv.slice(2);
await import(shimUrl);
const { createApi } = await import(apiUrl);
const view = await import(taskUrl);

const PROJECT = { id: 'alpha', name: 'Alpha', role: 'owner', usable: true, archived: false };

function row(extra) {
  return Object.assign({
    id: 'alpha-1', project_id: 'alpha', title: 'A row', status: 'open', review_state: 'none',
    assignee: null, priority: 1, version: 1, description: null,
    requirement_record: false, claimable: true,
    next_action: { who: 'anyone', text: 'Claim this task' },
  }, extra);
}
// The two rows exactly as the tasks-list/task route now returns them: the requirement
// carries the marker, is not claimable and is pointed at its page; the ordinary row is
// claimable and is offered as a claim.
const REQUIREMENT = row({
  id: 'alpha-1', title: 'A requirement record', requirement_record: true, claimable: false,
  next_action: { who: 'owner', text: "Requirement record: edit it on the project's Requirements page" },
});
const ORDINARY = row({ id: 'alpha-2', title: 'An ordinary task' });

function makeCtx(task) {
  const transport = (method, path) => {
    let data = null;
    if (path === '/v1/projects/alpha') data = PROJECT;
    else if (path === '/v1/projects/alpha/tasks/' + task.id + '/brief') {
      data = { task, review: { state: 'none', requests: [] }, checkpoint: null };
    } else if (path.indexOf('/v1/projects/alpha/tasks/' + task.id + '/history') === 0) {
      data = { items: [], next_cursor: null };
    }
    return Promise.resolve({ status: data === null ? 404 : 200, data });
  };
  return { api: createApi(transport), href: (p) => '#' + p, go() {}, query: {},
    me: { id: 'someone-else', superuser: false, display_name: 'Owner' }, render() {}, setDirty() {} };
}
const buttons = (page, text) => page.all((e) => e.tagName === 'BUTTON' && e.textContent === text).length;
const settle = async () => { for (let i = 0; i < 30; i += 1) await new Promise((resolve) => setTimeout(resolve, 5)); };

const requirement = await view.detail(makeCtx(REQUIREMENT), { pid: 'alpha', tid: 'alpha-1' });
await settle();
const ordinary = await view.detail(makeCtx(ORDINARY), { pid: 'alpha', tid: 'alpha-2' });
await settle();

console.log(JSON.stringify({
  requirementClaims: buttons(requirement, 'Claim task'),
  requirementEdits: buttons(requirement, 'Edit'),
  requirementText: requirement.textContent,
  ordinaryClaims: buttons(ordinary, 'Claim task'),
}));
