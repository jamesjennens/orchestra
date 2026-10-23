// Runs the releases page of web/js/views/releases.js under Node with the small DOM shim
// (tests/web_dom_shim.mjs) and a stub API. No browser and no server: the page's render and
// its pre-flight answer are what the Python test reads. Observations are printed as one
// JSON object; the Python test asserts on text and element ids only, never on innerHTML.
//
// argv: shim URL, releases.js URL.
const [shimUrl, viewUrl] = process.argv.slice(2);
await import(shimUrl);
const view = await import(viewUrl);

const BASE = { id: 'pp', name: 'Alpha', role: 'owner', archived: false, can_coordinate: true };

function context(project, answer) {
  const calls = { query: [], deploy: [] };
  const api = {
    project: async () => project,
    releaseQuery: async (pid, body) => { calls.query.push({ pid, body }); return answer; },
    deployRelease: async (pid, body) => { calls.deploy.push({ pid, body }); return {}; },
    lifecycle: async () => ({ scope: {}, facts: {} }),
  };
  return { calls, ctx: {
    api, href: (path) => '#' + path, go() {}, query: {}, render() {},
    refreshProjects: async () => {},
  } };
}

const ids = (page) => page.all((e) => e.attributes && e.attributes.id)
  .map((e) => e.attributes.id);
const forms = (page) => page.all((e) => e.tagName === 'FORM');
const out = {};

// 1. An owner: the pre-flight and the one-group form are both offered.
{
  const { ctx } = context({ ...BASE }, { environment: 'production', reverted: [], live_releases: [] });
  const page = await view.page(ctx, { pid: 'pp' });
  out.owner = { text: page.textContent, fields: ids(page), forms: forms(page).length };
}

// 2. The pre-flight answer is inserted as text: a markup-looking task id is shown
//    literally and creates no element.
{
  const evil = '<img src=x onerror=alert(1)>';
  const answer = {
    environment: 'production',
    reverted: [{ task: evil, integration_commit: 'a'.repeat(40) }],
    live_releases: [{ task: 'task-1', release_id: 'release-1',
                      integration_commit: 'b'.repeat(40), liveness: 'live' }],
  };
  const { ctx, calls } = context({ ...BASE }, answer);
  const page = await view.page(ctx, { pid: 'pp' });
  const preflight = forms(page)[0];
  preflight.querySelector('#release-env').value = 'production';
  await preflight.dispatch('submit');
  out.query = {
    called: calls.query.length === 1,
    text: page.textContent,
    evil,
    images: page.all((e) => e.tagName === 'IMG').length,
  };
}

// 3. An archived project: the banner says records are readable, and no deploy form exists.
{
  const { ctx } = context({ ...BASE, archived: true }, { environment: 'production',
                                                          reverted: [], live_releases: [] });
  const page = await view.page(ctx, { pid: 'pp' });
  out.archived = {
    text: page.textContent,
    deploy: page.all((e) => e.attributes && e.attributes.id === 'deploy-env').length > 0,
  };
}

// 4. A coordinator who is not an owner: the pre-flight is readable, recording is not offered.
{
  const { ctx } = context({ ...BASE, role: 'coordinator' }, { environment: 'production',
                                                              reverted: [], live_releases: [] });
  const page = await view.page(ctx, { pid: 'pp' });
  out.coordinator = {
    text: page.textContent,
    deploy: page.all((e) => e.attributes && e.attributes.id === 'deploy-env').length > 0,
  };
}

console.log(JSON.stringify(out));
