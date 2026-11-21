// Drives all agent-id surfaces of web/js under Node (kittrial-5bb.583) against a real http_service
// started by tests/test_web_agent_ids_583.py. Prints one JSON object of observations.
//
// argv: shimUrl apiUrl uiUrl projectUrl agentsUrl coordinationUrl invitesUrl howUrl baseUrl pidA pidB ownerToken viewerToken agentAId agentBId xssAgentId ownerId
const [
  shimUrl, apiUrl, uiUrl, projectUrl, agentsUrl, coordinationUrl, invitesUrl, howUrl,
  base, pidA, pidB, ownerToken, viewerToken, agentAId, agentBId, xssAgentId, ownerId,
] = process.argv.slice(2);

await import(shimUrl);
// Provide minimal browser globals used by agents.js / dom.js / invites.js.
globalThis.location = { origin: base, href: base + '/' };
globalThis.history = { replaceState() {} };
globalThis.window = globalThis.window || globalThis;
globalThis.window.isSecureContext = true;
globalThis.window.showDirectoryPicker = async () => ({ name: 'picked', getDirectoryHandle: async () => ({}), getFileHandle: async () => ({}) });
const origCreateElement = document.createElement.bind(document);
document.createElement = (tag) => {
  const el = origCreateElement(tag);
  if (el.tagName === 'DIALOG') {
    el.open = false;
    el.showModal = () => { el.open = true; el.setAttribute('open', ''); };
    el.close = () => { el.open = false; el.removeAttribute('open'); };
  }
  if (el.tagName === 'TEXTAREA') {
    el.select = () => {};
  }
  return el;
};
const toastArea = document.createElement('div');
toastArea.setAttribute('id', 'toasts');
document.body.appendChild(toastArea);
document.querySelector = (sel) => {
  if (sel === 'dialog[open]') return document.body.all((e) => e.tagName === 'DIALOG' && 'open' in e.attributes)[0] || null;
  return document.body.querySelector(sel);
};

const { createApi } = await import(apiUrl);
const ui = await import(uiUrl);
const projectView = await import(projectUrl);
const agentsView = await import(agentsUrl);
const coordinationView = await import(coordinationUrl);
const invitesView = await import(invitesUrl);
const howView = await import(howUrl);

let inFlight = 0;
let apiCalls = [];
function makeCtx(token, me, projects) {
  const transport = async (method, path, headers, body) => {
    inFlight += 1;
    apiCalls.push({ method, path });
    let response, text;
    try {
      response = await fetch(base + path, { method, headers: { ...headers, Authorization: 'Bearer ' + token }, body });
      text = await response.text();
    } finally { inFlight -= 1; }
    let data = null;
    try { data = text ? JSON.parse(text) : null; } catch { data = null; }
    return { status: response.status, data };
  };
  return {
    api: createApi(transport),
    me,
    projects,
    href: (p) => '#' + p,
    go() {},
    query: new URLSearchParams(''),
    render() {},
    refreshProjects: async () => {},
  };
}

async function settle() {
  let quiet = 0;
  for (let i = 0; i < 1200 && quiet < 6; i += 1) {
    await new Promise((r) => setTimeout(r, 20));
    quiet = inFlight === 0 ? quiet + 1 : 0;
  }
}

function hasHideNarrowAncestor(node) {
  let cur = node;
  while (cur) {
    const cls = String((cur.className || '') + ' ' + (cur.attributes && cur.attributes.class || ''));
    if (/\bhide-narrow\b/.test(cls)) return true;
    cur = cur.parentNode;
  }
  return false;
}

function collectIdChips(root) {
  return root.all((e) => e.attributes && e.attributes['data-id-chip']).map((chip) => {
    const code = chip.all((e) => (e.tagName === 'CODE' && e.attributes['data-copyable-id']) || (e.tagName === 'SPAN' && /\bmono\b/.test(String(e.className || (e.attributes && e.attributes.class) || ''))))[0];
    const btn = chip.all((e) => e.tagName === 'BUTTON' && e.attributes['data-copy-id'])[0];
    return {
      id: chip.attributes['data-id-chip'],
      codeText: code ? code.textContent : null,
      copyId: btn ? btn.attributes['data-copy-id'] : null,
      inHideNarrow: hasHideNarrowAncestor(chip),
    };
  });
}

const out = {};
const ownerCtx = makeCtx(ownerToken, { id: ownerId, display_name: 'Olive', superuser: false }, [
  { id: pidA, name: 'Alpha', role: 'owner', archived: false, usable: true, can_coordinate: true },
]);

// --- 1. Agents in this project + Who coordinates (/p/{pidA}/agents) ---
const projAgentsPage = await projectView.projectAgents(ownerCtx, { pid: pidA });
await settle();
const paPanel = projAgentsPage.all((e) => e.attributes && e.attributes['data-panel'] === 'project-agents')[0];
const coordPanel = projAgentsPage.all((e) => e.attributes && e.attributes['data-panel'] === 'coordination')[0];
out.projectAgents = {
  text: paPanel.textContent,
  chips: collectIdChips(paPanel),
  scriptTags: paPanel.all((e) => e.tagName === 'SCRIPT').length,
  rows: () => paPanel.all((e) => e.tagName === 'TR' && e.attributes['data-agent']).map((tr) => tr.attributes['data-agent']),
};
out.coordination = {
  text: coordPanel.textContent,
  chips: collectIdChips(coordPanel),
  scriptTags: coordPanel.all((e) => e.tagName === 'SCRIPT').length,
};

// Search / filter on Agents in this project (cases 3, 4, 6 + condition 4)
const paFilter = paPanel.all((e) => e.tagName === 'INPUT' && e.attributes['data-filter'] === 'project-agents')[0];
const runFilter = async (input, q, getRows) => {
  input.value = q;
  await input.dispatch('input');
  return getRows();
};
const bareA = agentAId.replace(/^agent_/, '');
const bareOwner = ownerId.replace(/^usr_/, '');
out.projectAgentsSearch = {
  fullId: await runFilter(paFilter, agentAId, out.projectAgents.rows),
  noPrefix: await runFilter(paFilter, bareA, out.projectAgents.rows),
  hex4: await runFilter(paFilter, bareA.slice(0, 4), out.projectAgents.rows),
  hex2: await runFilter(paFilter, bareA.slice(0, 2), out.projectAgents.rows),
  hex3: await runFilter(paFilter, bareA.slice(0, 3), out.projectAgents.rows),
  byName: await runFilter(paFilter, 'Osprey', out.projectAgents.rows),
  byAccountFull: await runFilter(paFilter, ownerId, out.projectAgents.rows),
  byAccountBare: await runFilter(paFilter, bareOwner, out.projectAgents.rows),
  byAccountHex4: await runFilter(paFilter, bareOwner.slice(0, 4), out.projectAgents.rows),
  byAccountHex2: await runFilter(paFilter, bareOwner.slice(0, 2), out.projectAgents.rows),
  byXssText: await runFilter(paFilter, '<script>', out.projectAgents.rows),
  cleared: await runFilter(paFilter, '', out.projectAgents.rows),
};
delete out.projectAgents.rows;

// --- 2. My agents cards (/agents) ---
const myAgentsPage = await agentsView.list(ownerCtx);
await settle();
const myFilter = myAgentsPage.all((e) => e.tagName === 'INPUT' && e.attributes['data-filter'] === 'agents')[0];
const myAgentRows = () => myAgentsPage.all((e) => e.tagName === 'DIV' && e.attributes['data-agent']).map((d) => d.attributes['data-agent']);
out.myAgents = {
  text: myAgentsPage.textContent,
  chips: collectIdChips(myAgentsPage),
  scriptTags: myAgentsPage.all((e) => e.tagName === 'SCRIPT').length,
  search: {
    fullId: await runFilter(myFilter, agentAId, myAgentRows),
    noPrefix: await runFilter(myFilter, bareA, myAgentRows),
    hex4: await runFilter(myFilter, bareA.slice(0, 4), myAgentRows),
    hex2: await runFilter(myFilter, bareA.slice(0, 2), myAgentRows),
    byName: await runFilter(myFilter, 'Osprey', myAgentRows),
    byAccountFull: await runFilter(myFilter, ownerId, myAgentRows),
    cleared: await runFilter(myFilter, '', myAgentRows),
  },
};

// --- 3. Single-agent surfaces: /setup chooser, /setup wizard page + dialog, /p/{pidA}/how ---
const chooserPage = await agentsView.setupWizardPage(ownerCtx);
await settle();
const wizardCtx = { ...ownerCtx, query: new URLSearchParams('agent=' + agentAId) };
const wizardPage = await agentsView.setupWizardPage(wizardCtx);
await settle();
const openDialog = document.body.all((e) => e.tagName === 'DIALOG')[0];
out.setupChooser = { text: chooserPage.textContent, chips: collectIdChips(chooserPage) };
out.setupWizardPage = { text: wizardPage.textContent, chips: collectIdChips(wizardPage) };
out.setupDialog = { text: openDialog ? openDialog.textContent : '', chips: openDialog ? collectIdChips(openDialog) : [] };

// Step 8 ('files') order + Step 'test' troubleshooting text (select older/non-Bridge path to include 'test' & 'files')
if (openDialog && openDialog.wizard) {
  openDialog.wizard.show('system');
  const olderLabel = openDialog.all((e) => e.attributes && e.attributes['data-choice'] === 'setup-bridge:no')[0];
  const olderBridge = olderLabel ? olderLabel.all((e) => e.tagName === 'INPUT')[0] : null;
  if (olderBridge) await olderBridge.dispatch('change');
  openDialog.wizard.show('files');
  const beside = openDialog.all((e) => e.attributes && e.attributes['data-folder-beside'] === 'true')[0];
  const children = beside ? Array.from(beside.children).map((c) => ({
    tag: c.tagName,
    text: c.textContent,
    action: c.attributes['data-action'] || null,
    folderPath: c.attributes['data-folder-path'] || null,
    stepLabel: c.attributes['data-step-label'] || null,
  })) : [];
  openDialog.wizard.show('test');
  const connectToNode = openDialog.all((e) => e.attributes && e.attributes['data-curl-error'] === 'connect-to')[0];
  out.step8 = { besideText: beside ? beside.textContent : '', children };
  out.troubleshooting = {
    constant: agentsView.CURL_ERROR_LINES['connect-to'] || null,
    rendered: connectToNode ? connectToNode.textContent : null,
  };
  openDialog.wizard.stopPoll();
  openDialog.wizard.stopKeepAlive();
  openDialog.remove();
}

const howPage = await howView.page(ownerCtx, { pid: pidA });
await settle();
out.howPage = { text: howPage.textContent, chips: collectIdChips(howPage) };

// --- 4. Lanes board (/p/{pidA}/lanes) ---
const lanesPage = await projectView.lanesPage(ownerCtx, { pid: pidA });
await settle();
const lanesFilter = lanesPage.all((e) => e.tagName === 'INPUT' && e.attributes['data-filter'] === 'lanes')[0];
const laneRows = () => lanesPage.all((e) => e.tagName === 'DIV' && e.attributes['data-lane']).map((d) => d.attributes['data-lane']);
out.lanes = {
  text: lanesPage.textContent,
  chips: collectIdChips(lanesPage),
  search: lanesFilter ? {
    fullId: await runFilter(lanesFilter, agentAId, laneRows),
    noPrefix: await runFilter(lanesFilter, bareA, laneRows),
    hex4: await runFilter(lanesFilter, bareA.slice(0, 4), laneRows),
    hex2: await runFilter(lanesFilter, bareA.slice(0, 2), laneRows),
    byName: await runFilter(lanesFilter, 'Osprey', laneRows),
    cleared: await runFilter(lanesFilter, '', laneRows),
  } : null,
};

// --- 4b. Account (usr_...) lanes stay free of usr_ text (kittrial-5bb.504 + .583 r2) ---
const usrLaneCtx = {
  ...ownerCtx,
  api: createApi(async (method, path, headers, body) => {
    if (path.startsWith(`/v1/projects/${pidA}/lanes`)) {
      return {
        status: 200,
        data: {
          scope: 'all',
          scope_basis: 'project-role',
          scope_role: 'owner',
          can_nudge: false,
          items: [
            { lane: ownerId, actor: ownerId, display_name: 'Olive', idle: true, held: [], last_write_age_seconds: 12 },
            { lane: 'usr_deadbeef00001111', actor: 'usr_deadbeef00001111', display_name: '', idle: true, held: [], last_write_age_seconds: 34 },
            { lane: agentAId, actor: agentAId, display_name: 'Osprey', idle: true, held: [], last_write_age_seconds: 56 },
          ],
          total: 3,
          next_offset: null,
        },
      };
    }
    const response = await fetch(base + path, { method, headers: { ...headers, Authorization: 'Bearer ' + ownerToken }, body });
    const text = await response.text();
    let data = null;
    try { data = text ? JSON.parse(text) : null; } catch { data = null; }
    return { status: response.status, data };
  }),
};
const usrLanesPage = await projectView.lanesPage(usrLaneCtx, { pid: pidA });
await settle();
const usrLaneBlocks = usrLanesPage.all((e) => e.tagName === 'DIV' && e.attributes['data-lane']).map((blk) => {
  const strong = blk.all((e) => e.tagName === 'STRONG')[0];
  return {
    lane: blk.attributes['data-lane'],
    text: blk.textContent,
    strongText: strong ? strong.textContent : '',
    strongTitle: strong ? (strong.attributes.title || null) : null,
    chips: collectIdChips(blk),
  };
});
out.usrLanes = { blocks: usrLaneBlocks };

// --- 5. Invite list (/p/{pidA}/people invitePanel) + inviteSetupBlock ---
const invPanel = invitesView.invitePanel(ownerCtx, { id: pidA, name: 'Alpha', archived: false });
await settle();
const invFilter = invPanel.all((e) => e.tagName === 'INPUT' && e.attributes['data-filter'] === 'invites')[0];
const invRows = () => invPanel.all((e) => e.tagName === 'TR' && e.attributes['data-invite']).map((tr) => tr.attributes['data-agent-id'] || '');
const redeemedAgentId = invRows().find((id) => id.startsWith('agent_')) || '';
const bareInv = redeemedAgentId.replace(/^agent_/, '');
out.invites = {
  text: invPanel.textContent,
  chips: collectIdChips(invPanel),
  redeemedAgentId,
  search: invFilter && redeemedAgentId ? {
    fullId: await runFilter(invFilter, redeemedAgentId, invRows),
    noPrefix: await runFilter(invFilter, bareInv, invRows),
    hex4: await runFilter(invFilter, bareInv.slice(0, 4), invRows),
    hex2: await runFilter(invFilter, bareInv.slice(0, 2), invRows),
    cleared: await runFilter(invFilter, '', invRows),
  } : null,
};

const invSetup = await agentsView.inviteSetupBlock(ownerCtx, { agent: { id: agentAId, name: 'Osprey', projects: [pidA] } }, 'orch_test_secret_123');
out.inviteSetupBlock = { text: invSetup.textContent, chips: collectIdChips(invSetup) };

// --- 6. Audit rows (/p/{pidA}/settings drawAudit) ---
apiCalls = [];
const settingsPage = await projectView.settings(ownerCtx, { pid: pidA });
await settle();
const auditPanel = settingsPage.all((e) => e.attributes && e.attributes['data-panel'] === 'audit')[0];
const auditFilter = auditPanel.all((e) => e.tagName === 'INPUT' && e.attributes['data-filter'] === 'audit')[0];
const auditRows = () => auditPanel.all((e) => e.tagName === 'TR' && e.attributes['data-audit-action']).map((tr) => tr.textContent);
out.audit = {
  text: auditPanel.textContent,
  chips: collectIdChips(auditPanel),
  // Condition 3: audit view must NEVER call /v1/agents/{id} to enrich rows.
  agentLookupCalls: apiCalls.filter((c) => /^\/v1\/agents\b/.test(c.path)).map((c) => c.path),
  search: auditFilter ? {
    fullId: await runFilter(auditFilter, agentAId, auditRows),
    noPrefix: await runFilter(auditFilter, bareA, auditRows),
    hex4: await runFilter(auditFilter, bareA.slice(0, 4), auditRows),
    hex2: await runFilter(auditFilter, bareA.slice(0, 2), auditRows),
    cleared: await runFilter(auditFilter, '', auditRows),
  } : null,
};

// --- 7. Copy button: Clipboard API + fallback without Clipboard API (case 2) ---
let copiedViaClipboard = [];
Object.defineProperty(globalThis, 'navigator', {
  configurable: true,
  writable: true,
  value: {
    clipboard: {
      async writeText(t) { copiedViaClipboard.push(t); },
    },
  },
});
const testBadge = ui.idBadge(agentAId, { what: 'Agent ID' });
document.body.appendChild(testBadge);
const copyBtn = testBadge.all((e) => e.tagName === 'BUTTON')[0];
await copyBtn.dispatch('click');

// Fallback without Clipboard API (execCommand succeeds)
globalThis.window.isSecureContext = false;
Object.defineProperty(globalThis, 'navigator', { configurable: true, writable: true, value: {} });
let execValues = [];
let execReturn = true;
document.execCommand = (cmd) => {
  const ta = document.body.all((e) => e.tagName === 'TEXTAREA')[0];
  execValues.push({ cmd, value: ta ? ta.value : null });
  return execReturn;
};
await copyBtn.dispatch('click');
const remainingTextareasAfterSuccess = document.body.all((e) => e.tagName === 'TEXTAREA').length;

// Fallback without Clipboard API (execCommand fails -> blocked toast)
execReturn = false;
await copyBtn.dispatch('click');
const blockedToast = document.body.all((e) => e.attributes && e.attributes.role === 'status').map((n) => n.textContent).join(' ');
const remainingTextareasAfterFail = document.body.all((e) => e.tagName === 'TEXTAREA').length;
testBadge.remove();

out.copy = {
  clipboardValues: copiedViaClipboard,
  execValues,
  remainingTextareasAfterSuccess,
  remainingTextareasAfterFail,
  blockedToast,
};

process.stdout.write(JSON.stringify(out) + '\n', () => process.exit(0));
