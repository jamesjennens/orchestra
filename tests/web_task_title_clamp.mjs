// Render both task-list pages with a 233-character title. The full title must stay
// available as text and in the title attribute while CSS limits its visible height.
//
// argv: shim URL, api.js URL, project.js URL, work.js URL, app.css URL.
import { readFile } from 'node:fs/promises';
const [shimUrl, apiUrl, projectUrl, workUrl, cssUrl] = process.argv.slice(2);
await import(shimUrl);
const { createApi } = await import(apiUrl);
const projectView = await import(projectUrl);
const workView = await import(workUrl);

const title = ('Task <b> & "quoted" title. ').padEnd(233, 'x').slice(0, 233);
const task = { id: 'alpha-long-title', project_id: 'alpha', project_name: 'Alpha', title,
  status: 'open', review_state: 'none', next_action: { text: 'Anyone may claim it' } };
const project = { id: 'alpha', name: 'Alpha', role: 'owner', usable: true, archived: false, description: null };
function transport(_method, path) {
  let data = null;
  if (path === '/v1/projects/alpha') data = project;
  else if (path.startsWith('/v1/projects/alpha/tasks')) {
    data = { items: [task], total: 1, next_cursor: null, review_states_complete: true };
  }
  return Promise.resolve({ status: data === null ? 404 : 200, data });
}
const projectCtx = { api: createApi(transport), href: (path) => '#' + path, go() {}, query: {},
  me: { superuser: false, display_name: 'Owner' }, render() {}, refreshProjects: async () => {}, projects: [] };
const projectPage = await projectView.overview(projectCtx, { pid: 'alpha' });
for (let i = 0; i < 40; i += 1) await new Promise((resolve) => setTimeout(resolve, 10));

const workCtx = { projects: [project], href: (path) => '#' + path, go() {}, render() {},
  me: { id: 'owner', display_name: 'Owner' },
  api: {
    myWork: async () => ({ assigned: [task], to_review: [], agents: [], truncated: false }),
    myContributions: async () => ({ items: [], total: 0, next_cursor: null }),
  } };
const workPage = await workView.home(workCtx);

function titleLink(page) {
  return page.all((element) => element.tagName === 'A'
    && String(element.className || element.attributes.class || '').split(/\s+/).includes('task-list-title'))[0] || null;
}
const projectTitle = titleLink(projectPage);
const workTitle = titleLink(workPage);
const css = await readFile(new URL(cssUrl), 'utf8');
const clampRule = css.match(/\.task-list-title\s*\{([^}]*)\}/)?.[1] || '';
const projectTable = projectPage.all((element) => element.tagName === 'TABLE'
  && String(element.className || element.attributes.class || '').split(/\s+/).includes('task-list-project'))[0] || null;
const workTable = workPage.all((element) => element.tagName === 'TABLE'
  && String(element.className || element.attributes.class || '').split(/\s+/).includes('task-list-work'))[0] || null;
const out = {
  titleLength: title.length,
  title,
  projectTitle: projectTitle?.textContent,
  workTitle: workTitle?.textContent,
  projectAttribute: projectTitle?.attributes.title,
  workAttribute: workTitle?.attributes.title,
  projectClamped: Boolean(projectTitle && projectTitle.attributes.title === title
    && projectTitle.textContent === title && projectTable && /-webkit-line-clamp:\s*3/.test(clampRule)),
  workClamped: Boolean(workTitle && workTitle.attributes.title === title
    && workTitle.textContent === title && workTable && /-webkit-line-clamp:\s*3/.test(clampRule)),
  desktopSizing: /@media\s*\(min-width:\s*761px\)/.test(css)
    && /\.task-list-project\s*\{\s*min-width:\s*900px;\s*\}/.test(css)
    && /\.task-list-work\s*\{\s*min-width:\s*1000px;\s*\}/.test(css),
};
if (!out.projectClamped || !out.workClamped || !out.desktopSizing) {
  throw new Error('Task title clamp or desktop table sizing is missing: ' + JSON.stringify(out));
}
console.log(JSON.stringify(out));
