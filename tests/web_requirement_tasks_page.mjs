// Runs the project tasks page of web/js/views/project.js under Node against a stubbed
// transport: it answers the project read and the tasks list, so no http_service is
// needed. tests/web_dom_shim.mjs gives dom.js and ui.js what they use. Prints one JSON
// object of observations; tests/test_requirement_task_page.py asserts on it.
//
// argv: shim URL, api.js URL, project.js URL.
const [shimUrl, apiUrl, projectUrl] = process.argv.slice(2);
await import(shimUrl);
const { createApi } = await import(apiUrl);
const view = await import(projectUrl);

const PROJECT = { id: 'alpha', name: 'Alpha', role: 'owner', usable: true, archived: false, description: null };
// One ordinary task and one requirement record, exactly as the tasks-list route now
// returns them (kittrial-5bb.277): the requirement carries the marker and is not
// claimable, the ordinary row does not and is.
const TASK = { id: 'alpha-1', project_id: 'alpha', title: 'An ordinary task', status: 'open',
  review_state: 'none', requirement_record: false, claimable: true, next_action: { text: 'Anyone may claim it' } };
const REQUIREMENT = { id: 'alpha-2', project_id: 'alpha', title: 'A requirement record', status: 'open',
  review_state: 'none', requirement_record: true, claimable: false, next_action: { text: 'Not claimable work' } };

const calls = [];
function transport(method, path) {
  calls.push(method + ' ' + path);
  let data = null;
  if (path === '/v1/projects/alpha') data = PROJECT;
  else if (path.startsWith('/v1/projects/alpha/tasks')) data = { items: [TASK, REQUIREMENT], total: 2, next_cursor: null, review_states_complete: true };
  return Promise.resolve({ status: data === null ? 404 : 200, data });
}
const ctx = { api: createApi(transport), href: (p) => '#' + p, go() {}, query: {}, me: { superuser: false, display_name: 'Owner' },
  render() {}, refreshProjects: async () => {}, projects: [] };

let inFlight = 0;
const page = await view.overview(ctx, { pid: 'alpha' });
// Let the late panels (contributions, setup hint) settle: they add nothing here.
for (let i = 0; i < 40; i += 1) await new Promise((resolve) => setTimeout(resolve, 10));

const tableRows = () => page.all((e) => e.tagName === 'TR' && e.all((c) => c.tagName === 'TD').length);
const rowTexts = () => tableRows().map((tr) => tr.textContent);
const rowOf = (title) => tableRows().find((tr) => tr.textContent.includes(title)) || null;
const toggle = page.querySelector('#show-requirements');
const out = {
  text: page.textContent,
  toggle: toggle ? { id: toggle.attributes.id, type: toggle.attributes.type, checked: Boolean(toggle.checked) } : null,
  defaultRows: rowTexts(),
  defaultHasRequirement: rowTexts().some((t) => t.includes('A requirement record')),
  defaultLeftOutNote: page.all((e) => e.tagName === 'P' && e.textContent.includes('not shown here')).map((p) => p.textContent),
  defaultRequirementLinks: page.all((e) => e.tagName === 'A' && e.textContent === 'Edit on Requirements').length,
};
// Filter on: the requirement row returns, marked as a requirement and linked to its page.
toggle.checked = true;
await toggle.dispatch('change');
for (let i = 0; i < 40; i += 1) await new Promise((resolve) => setTimeout(resolve, 10));
const marked = rowOf('A requirement record');
out.filteredRows = rowTexts();
out.filteredHasRequirement = Boolean(marked);
out.filteredChip = marked ? marked.all((e) => e.tagName === 'SPAN' && e.textContent === 'Requirement').length : 0;
out.filteredDataAttribute = marked ? marked.attributes['data-requirement'] : null;
out.filteredRequirementLinks = page.all((e) => e.tagName === 'A' && e.textContent === 'Edit on Requirements').map((a) => a.attributes.href);
out.filteredLeftOutNote = page.all((e) => e.tagName === 'P' && e.textContent.includes('not shown here')).length;
// The ordinary task was never marked as a requirement.
out.ordinaryMarked = rowOf('An ordinary task') ? rowOf('An ordinary task').all((e) => e.tagName === 'SPAN' && e.textContent === 'Requirement').length : -1;
out.calls = calls;
console.log(JSON.stringify(out));
