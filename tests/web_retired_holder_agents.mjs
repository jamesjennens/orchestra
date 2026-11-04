// Drives web/js/views/agents.js and web/js/views/project.js under Node against a real http_service
// started by tests/test_retired_holder_lanes.py (kittrial-5bb.510).
//
// argv: shim URL, api.js URL, agents.js URL, project.js URL, base URL, project id,
//       ownerSpec (name=id=token), nonOwnerSpec (name=id=token), extraTaskForAgent2
const [shimUrl, apiUrl, agentsUrl, projectUrl, base, projectId, ownerSpec, otherSpec, extraTask] = process.argv.slice(2);
const { Element } = await import(shimUrl);
Element.prototype.showModal = function showModal() { this.open = true; };
Element.prototype.close = function close() { this.open = false; return this.dispatch('close'); };
globalThis.window = globalThis.window || { isSecureContext: false };
globalThis.location = globalThis.location || { origin: base };

const { createApi } = await import(apiUrl);
const agentsView = await import(agentsUrl);
const projectView = await import(projectUrl);

const sent = [];
let inFlight = 0;

function makeCtx(spec, role = 'owner') {
  const [name, id, token] = spec.split('=');
  const transport = async (method, path, headers, body) => {
    if (method !== 'GET') {
      sent.push([name, method, path, body ? JSON.parse(body) : null]);
    }
    inFlight += 1;
    let response, text;
    try {
      response = await fetch(base + path, {
        method,
        headers: { ...headers, Authorization: 'Bearer ' + token },
        body,
      });
      text = await response.text();
    } finally {
      inFlight -= 1;
    }
    let data = null;
    try { data = text ? JSON.parse(text) : null; } catch { data = null; }
    return { status: response.status, data };
  };
  return {
    token,
    api: createApi(transport),
    href: (p) => '#' + p,
    go() {},
    query: {},
    render() {},
    refreshProjects: async () => {},
    me: { id, superuser: false, display_name: name },
    projects: [{ id: projectId, name: 'Alpha', role }],
  };
}

const ownerCtx = makeCtx(ownerSpec, 'owner');
const otherCtx = makeCtx(otherSpec, 'contributor');

const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
async function settle() {
  let quiet = 0;
  for (let i = 0; i < 600 && quiet < 6; i += 1) {
    await pause(15);
    quiet = inFlight === 0 ? quiet + 1 : 0;
  }
}

const agentBlock = (page, name) =>
  page.all((e) => e.tagName === 'DIV' && e.attributes['data-agent'])
    .find((e) => e.all((x) => x.tagName === 'H3')[0].textContent === name);

const actionBtn = (root, action) =>
  root ? root.all((e) => e.tagName === 'BUTTON' && e.attributes['data-action'] === action)[0] : null;

const out = {};

// 1. Non-owner views the Agents page (even if injected with an agent record from another owner,
//    ownedBy is false so no disable/release controls are rendered).
{
  const fakeOtherCtx = {
    ...otherCtx,
    api: {
      ...otherCtx.api,
      agents: async () => ({
        items: [{
          id: 'agent_foreign',
          name: 'ForeignAgent',
          owner: ownerCtx.me.id,
          enabled: true,
          projects: [projectId],
          scopes: ['read', 'tasks'],
          scopes_source: 'set',
          credentials: [],
        }],
        total: 1,
        next_cursor: null,
      }),
      agentHistory: async () => ({ items: [] }),
    },
  };
  const otherPage = await agentsView.list(fakeOtherCtx);
  await settle();
  const foreignBlock = agentBlock(otherPage, 'ForeignAgent');
  out.nonOwnerHasDisable = Boolean(actionBtn(foreignBlock, 'disable-agent'));
  out.nonOwnerHasRelease = Boolean(actionBtn(foreignBlock, 'release-claims'));
}

// 2. Owner opens Agents page: disables 'DeclineAgent' and declines the release ('Keep claims').
const page = await agentsView.list(ownerCtx);
document.body.append(page);
await settle();

const declineBlock1 = agentBlock(page, 'DeclineAgent');
out.ownerHasDisableInitially = Boolean(actionBtn(declineBlock1, 'disable-agent'));
await actionBtn(declineBlock1, 'disable-agent').dispatch('click');
await settle();

const declineBlock2 = agentBlock(page, 'DeclineAgent');
out.declinePromptText = declineBlock2.textContent;
out.declineClaimTasks = declineBlock2.all((e) => e.attributes && e.attributes['data-claim-task'])
  .map((e) => e.attributes['data-claim-task']);
out.declineHasReleaseBtn = Boolean(actionBtn(declineBlock2, 'release-claims'));
out.declineHasKeepBtn = Boolean(actionBtn(declineBlock2, 'decline-release'));

// Decline the release: disable stays, claims remain held, and Lanes page shows holder_retired.
await actionBtn(declineBlock2, 'decline-release').dispatch('click');
await settle();

const declineBlock3 = agentBlock(page, 'DeclineAgent');
out.afterDeclineText = declineBlock3.textContent;
out.afterDeclineHasReleaseBtn = Boolean(actionBtn(declineBlock3, 'release-claims'));

// Render the Lanes panel for the project after declining: DeclineAgent's held task is flagged holder_retired.
const lanesPanelAfterDecline = projectView.lanesPanel(ownerCtx, { id: projectId, name: 'Alpha', role: 'owner', usable: true });
document.body.append(lanesPanelAfterDecline);
await settle();
out.lanesAfterDeclineChips = lanesPanelAfterDecline.all((e) => e.tagName === 'SPAN' && e.attributes['data-flag'])
  .map((e) => e.attributes['data-flag'] + ': ' + e.textContent);
out.lanesAfterDeclineText = lanesPanelAfterDecline.textContent;

// 3. Owner disables 'ReleaseAgent': dry run shows its initial held task.
//    Before clicking 'Release its claims', the owner assigns extraTask to ReleaseAgent,
//    then clicks 'Release its claims' -> only the dry-run task is released!
const releaseBlock1 = agentBlock(page, 'ReleaseAgent');
const releaseAgentId = releaseBlock1.attributes['data-agent'];
await actionBtn(releaseBlock1, 'disable-agent').dispatch('click');
await settle();

const releaseBlock2 = agentBlock(page, 'ReleaseAgent');
out.releasePromptTasks = releaseBlock2.all((e) => e.attributes && e.attributes['data-claim-task'])
  .map((e) => e.attributes['data-claim-task']);

if (extraTask) {
  await fetch(`${base}/v1/projects/${projectId}/tasks/${extraTask}/assign`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Authorization: 'Bearer ' + ownerCtx.token,
      'Idempotency-Key': 'extra-assign-after-dry-run',
    },
    body: JSON.stringify({
      expected_assignee: null,
      assignee: releaseAgentId,
      reason: 'assigned after dry run',
      allow_pending_review: false,
      allow_held_slot: false,
    }),
  });
}

await actionBtn(releaseBlock2, 'release-claims').dispatch('click');
await settle();

const releaseBlock3 = agentBlock(page, 'ReleaseAgent');
out.afterReleaseText = releaseBlock3.textContent;
out.sent = sent;

console.log(JSON.stringify(out));
