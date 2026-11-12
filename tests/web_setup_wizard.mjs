// The agent set-up wizard of web/js/views/agents.js, run under Node with the browser-shaped DOM
// shim (kittrial-5bb.348: one action per screen).
//
// argv: the shim URL, the api.js URL, the agents.js URL, the dom.js URL. There is no browser and no
// server: the transport answers the agent read, the certificate read, a credential issue and a role
// save. It prints one JSON line of named facts; tests/test_http_web.py requires every one true.
// What is checked: the screens of both paths in order with Back and Next; that a screen holds only
// its own action (another screen's command is not in the dialog at all); short text; worker chosen
// first; the one-line note and the issue button without a secret; the issued secret shown at once
// and never stored; an error shown inside the dialog and no toast behind it; a toast drawn inside an
// open modal; the check before Next; a reload resuming the screen; the curl flags; the folder path
// beside the save button.
const [shimUrl, apiUrl, agentsUrl, domUrl] = process.argv.slice(2);
const { Element } = await import(shimUrl);
Element.prototype.showModal = function showModal() { this.open = true; };
Element.prototype.close = function close() { this.open = false; return this.dispatch('close'); };

const store = new Map();
globalThis.sessionStorage = {
  getItem: (k) => (store.has(k) ? store.get(k) : null),
  setItem: (k, v) => store.set(k, String(v)),
  removeItem: (k) => store.delete(k),
};
let copiedText = null;
Object.defineProperty(globalThis, 'navigator', {
  value: {
    platform: 'Win32',
    userAgent: 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
    clipboard: { writeText: async (text) => { copiedText = String(text); } },
  },
  configurable: true,
});
const body = globalThis.document.body;
const listeners = new Map();
Object.assign(globalThis.document, {
  visibilityState: 'visible',
  addEventListener(type, fn) { if (!listeners.has(type)) listeners.set(type, new Set()); listeners.get(type).add(fn); },
  removeEventListener(type, fn) { if (listeners.has(type)) listeners.get(type).delete(fn); },
  dispatchEvent(event) { for (const fn of [...(listeners.get(event.type) || [])]) fn(event); return true; },
  // What a browser answers for the selector dom.js's toastLayer() uses.
  querySelectorAll(selector) {
    if (selector !== 'dialog[open]') throw new Error('unexpected selector ' + selector);
    return body.all((e) => e.tagName === 'DIALOG' && e.open === true);
  },
});
globalThis.location = { origin: 'https://127.0.0.1:8765', hash: '#/setup' };
let nextInterval = 1;
const cleared = [];
globalThis.setInterval = () => nextInterval++;
globalThis.clearInterval = (id) => { cleared.push(id); };
const pickerCalls = [];
globalThis.window = {
  location: globalThis.location,
  isSecureContext: true,
  showDirectoryPicker: async (opts) => {
    pickerCalls.push(opts);
    const missing = () => { const e = new Error('missing'); e.name = 'NotFoundError'; throw e; };
    return {
      name: 'kestrel',
      getDirectoryHandle: async (_n, o) => { if (!o || !o.create) missing(); return { getFileHandle: async () => ({ createWritable: async () => ({ write: async () => {}, close: async () => {} }) }) }; },
      getFileHandle: async () => missing(),
    };
  },
};

const { createApi } = await import(apiUrl);
const { setupWizardPage, WIZARD_STORAGE_KEY, clearWizardStorage, list: agentsList } = await import(agentsUrl);
const setupText = await import(new URL('../agentSetup.js', agentsUrl).href);
const { SESSION_IDLE_WARNING_EVENT } = await import(apiUrl);
const { toast } = await import(domUrl);

const SECRET = 'orc_TESTSECRET_348_value';
const server = {
  lastSeen: '2026-10-17T00:00:00Z',
  certificate: null,
  failIssue: false,
  issued: 0,
  roles: [],
  scopes: ['read', 'tasks', 'reviews'],
  credentials: [],
  revoked: [],
  posts: [],
  listNew: false,
  coordinateGrantableProjects: undefined,
  ownerMemberships: ['p1', 'p2'],
  agentProjects: ['p1'],
  coordinatorGrants: [],
  coordinatorRevokes: [],
  failCoordinatorGrant: false,
  failAgentReread: false,
  grantHasNoEffectOnReread: false,
  dropFromGrantableOnReread: false,
  onCoordinatorPutInFlight: null,
};
let sessionReads = 0;
let agentReads = 0;
let transportCalls = 0;
const api = createApi(async (method, path, _headers, text) => {
  transportCalls += 1;
  const sent = text ? JSON.parse(text) : undefined;
  if (method === 'GET' && path === '/v1/sessions/current') { sessionReads += 1; return { status: 200, data: { csrf_token: 'c' } }; }
  if (method === 'GET' && path === '/v1/service/certificate') {
    return server.certificate ? { status: 200, data: server.certificate } : { status: 404, data: { error: { code: 'not_found', message: 'none' } } };
  }
  if (method === 'GET' && path === '/v1/agents/agent_1') {
    agentReads += 1;
    if (server.failAgentReread) {
      return { status: 500, data: { error: { code: 'internal_error', message: 'Database read failed' }, request_id: 'req_reread_500' } };
    }
    const ownerMemberships = Array.isArray(server.ownerMemberships) ? server.ownerMemberships : ['p1', 'p2'];
    const coordinates = Array.isArray(server.coordinates)
      ? server.coordinates.filter((c) => c && ownerMemberships.includes(c.project))
      : undefined;
    const grantable = Array.isArray(server.coordinateGrantableProjects)
      ? server.coordinateGrantableProjects.filter((pid) => ownerMemberships.includes(pid))
      : server.coordinateGrantableProjects;
    return { status: 200, data: { id: 'agent_1', name: 'Kestrel', owner: 'u1', role: 'worker', scopes: server.scopes, scopes_source: 'stored',
      working_directory: 'C:\\Users\\olive\\kestrel', projects: server.agentProjects || ['p1'], last_seen_at: server.lastSeen, credentials: server.credentials,
      ...(coordinates !== undefined ? { coordinates } : {}),
      ...(grantable !== undefined ? { coordinate_grantable_projects: grantable } : {}) } };
  }
  const coordRoute = /^\/v1\/projects\/([a-z0-9_-]+)\/agents\/(agent_[0-9]+)\/coordinator$/.exec(path);
  if (method === 'PUT' && coordRoute) {
    if (typeof server.onCoordinatorPutInFlight === 'function') await server.onCoordinatorPutInFlight();
    if (server.failCoordinatorGrant) return { status: 403, data: { error: { code: 'forbidden', message: 'Project role does not permit this operation' } } };
    server.coordinatorGrants.push(coordRoute[1] + '/' + coordRoute[2]);
    if (!server.grantHasNoEffectOnReread) {
      const current = Array.isArray(server.coordinates) ? server.coordinates.slice() : [];
      if (!current.some((c) => c && c.project === coordRoute[1] && c.effective === true)) {
        current.push({ project: coordRoute[1], effective: true });
      }
      server.coordinates = current;
      if (Array.isArray(server.coordinateGrantableProjects)) {
        server.coordinateGrantableProjects = server.coordinateGrantableProjects.filter((p) => p !== coordRoute[1]);
      }
    } else if (server.dropFromGrantableOnReread && Array.isArray(server.coordinateGrantableProjects)) {
      server.coordinateGrantableProjects = server.coordinateGrantableProjects.filter((p) => p !== coordRoute[1]);
    }
    return {
      status: 200,
      data: {
        id: coordRoute[2],
        name: 'Kestrel',
        owner: 'u1',
        owner_display_name: 'Olive',
        actor: 'agent:' + coordRoute[2],
        enabled: true,
        last_seen_at: server.lastSeen,
        credentials: [],
        coordinator: {
          granted_by: 'u1',
          granted_by_display_name: 'Olive',
          granted_at: '2026-10-17T00:00:00Z',
          effective: !server.grantHasNoEffectOnReread,
          no_effect_because: server.grantHasNoEffectOnReread ? 'account-role' : null,
        },
        release: null,
      },
    };
  }
  if (method === 'DELETE' && coordRoute) {
    server.coordinatorRevokes.push(coordRoute[1] + '/' + coordRoute[2]);
    return { status: 200, data: {} };
  }
  if (method === 'POST' && path === '/v1/agents/agent_1/credentials') {
    if (server.failIssue) return { status: 0, data: { error: { code: 'network', message: 'The server could not be reached.' } } };
    server.issued += 1;
    server.posts.push(path);
    if (server.listNew) server.credentials.push({ id: 'cred_' + server.issued, label: 'the new one', scopes: ['read', 'tasks'], working: true, created_at: '2026-10-31T00:00:00Z' });
    return { status: 201, data: { credential: { id: 'cred_' + server.issued, secret: SECRET, scopes: ['read', 'tasks'] } } };
  }
  const revoke = /^\/v1\/agents\/(agent_[0-9]+)\/credentials\/(cred_[a-z0-9]+)\/revoke$/.exec(path);
  if (method === 'POST' && revoke) {
    server.revoked.push(revoke[1] + '/' + revoke[2]);
    const found = server.credentials.find((c) => c.id === revoke[2]);
    if (found) { found.revoked = true; found.working = false; }
    return { status: 200, data: {} };
  }
  if (method === 'PATCH' && path === '/v1/agents/agent_1') {
    if (server.failRole) return { status: 403, data: { error: { code: 'forbidden', message: 'Only the agent’s owner may change its role.' } } };
    server.roles.push(sent.role); return { status: 200, data: {} };
  }
  return { status: 404, data: { error: { code: 'not_found', message: 'no route ' + method + ' ' + path } } };
});
const ctx = {
  api,
  me: { id: 'u1', superuser: true },
  projects: [{ id: 'p1', name: 'Alpha' }, { id: 'p2', name: 'Beta' }, { id: 'p3', name: 'Gamma' }],
  query: { get: (k) => (k === 'agent' ? 'agent_1' : null) },
  href: (p) => '#' + p,
  go: () => {},
};

const tick = () => new Promise((r) => setTimeout(r, 0));
const openDialogs = () => body.all((e) => e.tagName === 'DIALOG' && e.open === true);
async function openWizard() {
  for (const d of openDialogs()) await d.close();
  await setupWizardPage(ctx);
  await tick();
  const dialogs = openDialogs().filter((d) => d.getAttribute('data-setup-wizard') === 'true');
  return dialogs[dialogs.length - 1];
}
const attr = (root, name, value) => root.all((e) => e.getAttribute(name) === value);
const commands = (d) => d.all((e) => e.getAttribute('data-command') !== null).map((e) => e.getAttribute('data-command'));
const button = (root, action) => attr(root, 'data-action', action)[0];
const screenOf = (d) => d.getAttribute('data-active-screen');
// A command's own text: the code element of its line (a long one is behind "Show the command").
const commandText = (d, what) => d.all((e) => e.getAttribute('data-command') === what)[0].all((e) => e.tagName === 'CODE')[0].textContent;
// The screen's own text, without what is behind "More about this".
function shownText(d) {
  const screen = d.all((e) => e.getAttribute('data-wizard-screen') !== null)[0];
  const parts = [];
  const walk = (node) => { for (const c of node.childNodes) { if (c.tagName === 'DETAILS') continue; if (c.tagName) walk(c); else parts.push(c.textContent); } };
  walk(screen);
  return parts.join(' ').replace(/\s+/g, ' ').trim();
}
function firstSentenceCount(d) {
  const screen = d.all((e) => e.getAttribute('data-wizard-screen') !== null)[0];
  const p = screen.all((e) => e.tagName === 'P')[0];
  const text = p ? p.textContent.trim() : '';
  return { sentences: text.split(/[.!?](?:\s|$)/).filter((x) => x.trim()).length, length: text.length };
}
async function walk(d) {
  const seen = [screenOf(d)];
  for (let i = 0; i < 12; i++) {
    const before = screenOf(d);
    await d.wizard.next();
    if (screenOf(d) === before) {
      // a checked screen: Continue without checking
      await button(d, 'skip-check').dispatch('click');
      await tick();
      if (screenOf(d) === before) break;
    }
    seen.push(screenOf(d));
    if (seen[seen.length - 1] === 'resume') break;
  }
  return seen;
}
function windowsChoice(d, name, value) { return d.all((e) => e.tagName === 'INPUT' && e.getAttribute('name') === name && e.value === value)[0]; }

const out = {};

// ---- 1. The short path (Bridge 0.14+), Windows, no certificate, opened without a secret --------
store.clear();
let d = await openWizard();
out.opensOnTheFirstScreen = screenOf(d) === 'system';
out.progressSaysStepOfFive = d.all((e) => e.getAttribute('data-wizard-progress') !== null)[0].textContent === 'Step 1 of 5'
  && d.all((e) => e.getAttribute('id') === 'setup-title')[0].textContent === 'Your computer';
out.shortPathPlan = JSON.stringify(d.wizard.plan()) === JSON.stringify(['system', 'role', 'secret', 'vscode', 'resume']);
out.systemScreenHasNoCommand = commands(d).length === 0;
out.backHiddenOnTheFirstScreen = button(d, 'wizard-back').hidden === true;
const texts = {};
const sentences = {};
await d.wizard.next();
out.roleScreenSecond = screenOf(d) === 'role';
out.workerPreselected = windowsChoice(d, 'setup-role', 'worker').checked === true && !windowsChoice(d, 'setup-role', 'reviewer').checked;
out.reviewerSaidInOneLine = /A reviewer is also offered other people’s deliveries to review\./.test(shownText(d));
await d.wizard.next();
out.secretScreenThird = screenOf(d) === 'secret';
out.noSecretOneLineWhy = attr(d, 'data-secret-note', 'true').length === 1
  && attr(d, 'data-secret-note', 'true')[0].textContent === 'This page cannot show the secret again. Issuing adds a NEW credential; the others keep working until revoked.';
out.issueButtonRightThere = Boolean(button(d, 'issue-secret'));
out.noSecretShownYet = !JSON.stringify(shownText(d)).includes('orc_') && attr(d, 'data-secret-file-text', 'true').length === 0;

// The issue fails (the office case: the server could not be reached): the error is INSIDE the
// dialog, on this screen, in full, and no toast is drawn behind the modal.
server.failIssue = true;
const toastsBefore = body.all((e) => e.getAttribute('class') === 'toast crit' || e.className === 'toast crit').length;
let issue = button(d, 'issue-secret').dispatch('click');
await tick();
let confirm = openDialogs().find((x) => x !== d);
out.confirmationIsAboveTheWizard = Boolean(confirm) && openDialogs().indexOf(confirm) > openDialogs().indexOf(d);
await confirm.all((e) => e.tagName === 'BUTTON' && e.textContent === 'Issue new secret')[0].dispatch('click');
await issue; await tick(); await tick();
const errorLine = attr(d, 'data-wizard-error', 'true')[0];
out.errorShownInsideTheDialog = errorLine.hidden === false && /The server could not be reached\./.test(errorLine.textContent) && d.all((e) => e === errorLine).length === 1;
out.errorHasNoNullText = !/null/.test(errorLine.textContent);
out.noToastBehindTheModal = body.all((e) => e.className === 'toast crit').filter((t) => !d.all((x) => x === t).length).length === toastsBefore;
out.stillOnTheSecretScreen = screenOf(d) === 'secret';

// The issue succeeds: the new secret's lines are on this screen at once.
server.failIssue = false;
issue = button(d, 'issue-secret').dispatch('click');
await tick();
confirm = openDialogs().find((x) => x !== d);
await confirm.all((e) => e.tagName === 'BUTTON' && e.textContent === 'Issue new secret')[0].dispatch('click');
await issue; await tick(); await tick();
const lines = attr(d, 'data-secret-file-text', 'true')[0];
out.issuedSecretShownAtOnce = Boolean(lines) && lines.textContent === '# server = https://127.0.0.1:8765\nheader = "Authorization: Bearer ' + SECRET + '"\n';
out.errorClearedAfterSuccess = errorLine.hidden === true;
out.secretScreenNamesTheFile = commands(d).join() === 'file name' && commandText(d, 'file name') === '%USERPROFILE%\\.orchestra-agent-kestrel.curlrc';
out.secretNeverStored = ![...store.values()].some((v) => v.includes(SECRET)) && !JSON.stringify([...store.values()]).includes('orc_');
texts.secret = shownText(d); sentences.secret = firstSentenceCount(d);

// The check before Next on the VS Code screen (.329): nothing seen since the screen opened.
await d.wizard.next();
out.vscodeScreenFourth = screenOf(d) === 'vscode';
out.secretAbsentFromOtherScreens = !JSON.stringify(d.all(() => true).map((e) => e.textContent)).includes(SECRET);
out.vscodeScreenHasFolderAndCommand = JSON.stringify(commands(d)) === JSON.stringify(['folder path', 'VS Code command']);
texts.vscode = shownText(d); sentences.vscode = firstSentenceCount(d);
// kittrial-5bb.545: on the Bridge path the proxy line comes BEFORE "Orchestra Bridge: Set up" (document order),
// names this page's host, and is not behind "More about this".
const noProxyLine = (root) => attr(root, 'data-bridge-no-proxy', 'true');
const setUpLine = d.all((e) => e.getAttribute('data-command') === 'VS Code command')[0];
const inOrder = d.all(() => true);
out.bridgeNoProxyLineBeforeSetUp = noProxyLine(d).length === 1 && Boolean(setUpLine)
  && inOrder.indexOf(noProxyLine(d)[0]) < inOrder.indexOf(setUpLine)
  && !d.all((e) => e.tagName === 'DETAILS').some((x) => noProxyLine(x).length)
  && noProxyLine(d)[0].textContent === 'If this computer uses a proxy, first add 127.0.0.1 to VS Code\'s Http: No Proxy setting (File > Preferences > Settings, search “No Proxy”; on macOS Code > Settings).';
await d.wizard.next();
const check = attr(d, 'data-wizard-check-status', 'true')[0];
out.checkHoldsNextUntilSeen = screenOf(d) === 'vscode' && check.hidden === false && button(d, 'skip-check').hidden === false
  && /Orchestra has not seen Kestrel since this screen opened\. Run “Orchestra Bridge: Set up” in VS Code, then click Next again\./.test(check.textContent);
server.lastSeen = new Date(Date.now() + 1000).toISOString();
await d.wizard.next();
out.checkPassesOnceSeen = screenOf(d) === 'resume';
// kittrial-5bb.364: on the Bridge path the Bridge gives the agent its prompt, so nothing is pasted here,
// and the screen says what the waiting count includes.
out.bridgeResumeHasNoPromptToPaste = JSON.stringify(commands(d)) === JSON.stringify([])
  && attr(d, 'data-bridge-prompt', 'true').length === 1
  && /Orchestra Bridge starts the agent with its own prompt: nothing needs pasting here\./.test(shownText(d));
out.bridgeResumeSaysWhatWaitingCounts = attr(d, 'data-waiting-count', 'true').length === 1
  && /own tasks plus open tasks nobody holds, unless the project’s “How work is handed out” setting \(Members & settings\) is “Only coordinators assign work”/.test(shownText(d));
out.nextHiddenAndDonePrimaryOnTheLast = button(d, 'wizard-next').hidden === true && button(d, 'wizard-done').className === 'primary';
texts.resume = shownText(d); sentences.resume = firstSentenceCount(d);
const back = [];
for (let i = 0; i < 4; i++) { await d.wizard.back(); back.push(screenOf(d)); }
out.backRetracesTheShortPath = JSON.stringify(back) === JSON.stringify(['vscode', 'secret', 'role', 'system']);

// A reload resumes the stored screen for the same agent (and never a secret).
d.wizard.show('vscode');
d = await openWizard();
out.reloadResumesTheScreen = screenOf(d) === 'vscode';
out.reloadShowsNoSecret = !JSON.stringify(d.all(() => true).map((e) => e.textContent)).includes(SECRET);
// A step stored by the released three-step wizard maps to its screen on this path.
store.set(WIZARD_STORAGE_KEY, JSON.stringify({ agentId: 'agent_1', step: 2, os: 'windows', role: 'worker' }));
d = await openWizard();
out.oldStepTwoMapsToVsCodeOnTheShortPath = screenOf(d) === 'vscode';
store.set(WIZARD_STORAGE_KEY, JSON.stringify({ agentId: 'agent_1', step: 2, os: 'windows', role: 'worker', bridge: false }));
d = await openWizard();
out.oldStepTwoMapsToFilesOnTheLongPath = screenOf(d) === 'files';

// ---- 2. The longer path (an older extension), Linux, with the service's own certificate ---------
store.clear();
server.certificate = { certificate: '-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n', sha256: 'AB:CD:EF' };
server.lastSeen = '2026-10-17T00:00:00Z';
d = await openWizard();
const linuxRadio = windowsChoice(d, 'setup-os', 'linux');
await linuxRadio.dispatch('change');
await windowsChoice(d, 'setup-bridge', 'no').dispatch('change');
out.choiceKeepsTheSameRadioSoFocusStays = windowsChoice(d, 'setup-os', 'linux') === linuxRadio;
out.longPathPlanWithCertificateOnPosix = JSON.stringify(d.wizard.plan()) === JSON.stringify(
  ['system', 'role', 'cert-save', 'cert-move', 'secret', 'rewrite', 'test', 'files', 'vscode', 'resume']);
out.progressCountsTheChosenPath = d.all((e) => e.getAttribute('data-wizard-progress') !== null)[0].textContent === 'Step 1 of 10';
const longSeen = await walk(d);
out.longPathWalksInOrder = JSON.stringify(longSeen) === JSON.stringify(d.wizard.plan());
const perScreen = {};
for (const screen of d.wizard.plan()) { d.wizard.show(screen); perScreen[screen] = commands(d); texts[screen] = shownText(d); sentences[screen] = firstSentenceCount(d); }
out.eachScreenOnlyItsOwnCommand = JSON.stringify(perScreen) === JSON.stringify({
  system: [], role: [], 'cert-save': [], 'cert-move': ['certificate move command'], secret: [], rewrite: ['rewrite command'],
  test: ['self-check command'], files: [], vscode: ['folder path', 'VS Code command'], resume: ['resume prompt'] });
d.wizard.show('resume');
out.longResumeKeepsThePromptAndTheCount = JSON.stringify(commands(d)) === JSON.stringify(['resume prompt'])
  && attr(d, 'data-bridge-prompt', 'true').length === 0 && attr(d, 'data-waiting-count', 'true').length === 1
  && /Paste this into the agent’s chat in VS Code\./.test(shownText(d));
d.wizard.show('cert-save');
out.saveCertificateIsTheOneButton = Boolean(button(d, 'save-certificate')) && commands(d).length === 0;
d.wizard.show('cert-move');
out.moveCommandIsThePosixOne = /^if \[ -f "\$HOME\/Downloads\/orchestra-agent-kestrel\.crt" \]/.test(commandText(d, 'certificate move command'));
d.wizard.show('test');
const selfCheck = commandText(d, 'self-check command');
const selfCheckLine = d.all((e) => e.getAttribute('data-command') === 'self-check command')[0];
out.longCommandIsCopyAndCollapsed = selfCheckLine.getAttribute('data-long-command') === 'true'
  && selfCheckLine.all((e) => e.tagName === 'DETAILS' && !e.open).length === 1
  && selfCheckLine.all((e) => e.tagName === 'DETAILS')[0].all((e) => e.tagName === 'CODE').length === 1;
out.selfCheckBypassesTheProxyForThisHostOnly = selfCheck.includes('--noproxy 127.0.0.1 --max-time 20') && !selfCheck.includes('--noproxy "*"');
out.selfCheckSaysTheProxySentence = selfCheck.includes('a proxy probably answered; check HTTPS_PROXY in this shell, or ask IT to let this computer reach 127.0.0.1 directly.');
out.testScreenSaysPasteAndPressEnter = shownText(d).includes('Paste this into a terminal and press Enter (pasting waits for Enter to run). It checks the file, calls Orchestra, and prints the agent’s details.');
await d.wizard.next();
out.testScreenChecksBeforeNext = screenOf(d) === 'test'
  && /Run the command above, then click Next again\./.test(attr(d, 'data-wizard-check-status', 'true')[0].textContent);
d.wizard.show('files');
const beside = attr(d, 'data-folder-beside', 'true')[0];
out.folderPathBesideTheSaveButton = Boolean(beside) && beside.all((e) => e.getAttribute('data-action') === 'save-files').length === 1
  && beside.all((e) => e.getAttribute('data-folder-path') === 'C:\\Users\\olive\\kestrel').length === 1;
d.wizard.show('vscode');
out.oldExtensionToldAboutCaFileBehindMore = d.all((e) => e.getAttribute('data-bridge-cafile') === 'true').length === 1
  && d.all((e) => e.tagName === 'DETAILS').some((x) => x.all((e) => e.getAttribute('data-bridge-cafile') === 'true').length === 1);
// kittrial-5bb.545: off the Bridge path there is no proxy line, and no element mentions No Proxy.
out.noProxyLineAbsentOffTheBridgePath = screenOf(d) === 'vscode' && noProxyLine(d).length === 0
  && !d.all(() => true).some((e) => /No Proxy/.test(e.textContent));
// The longer path on POSIX without a certificate restricts the file instead of rewriting it.
server.certificate = null;
d = await openWizard();
d.wizard.show('system');   // it resumed the stored screen; the choices are on the first one
await windowsChoice(d, 'setup-bridge', 'no').dispatch('change');
out.longPathWithoutCertificateRestricts = JSON.stringify(d.wizard.plan()) === JSON.stringify(
  ['system', 'role', 'secret', 'restrict', 'test', 'files', 'vscode', 'resume']);
await windowsChoice(d, 'setup-os', 'windows').dispatch('change');
await windowsChoice(d, 'setup-bridge', 'yes').dispatch('change');
out.shortPathOnWindowsHasNoChmod = !d.wizard.plan().includes('restrict');
await windowsChoice(d, 'setup-os', 'macos').dispatch('change');
out.shortPathOnMacRestricts = JSON.stringify(d.wizard.plan()) === JSON.stringify(['system', 'role', 'secret', 'restrict', 'vscode', 'resume']);

// ---- 3. Minimal text: at most two short sentences before "More about this" on every screen ------
out.atMostTwoSentencesPerScreen = Object.values(sentences).every((s) => s.sentences <= 2 && s.length <= 170);
out.noScreenShowsNull = Object.values(texts).every((t) => !/\bnull\b|\bundefined\b/.test(t));

// ---- 4. The role is saved; an error saving it is shown inside the dialog ------------------------
d = await openWizard();
d.wizard.show('role');
await windowsChoice(d, 'setup-role', 'reviewer').dispatch('change');
await tick();
out.roleSavedOnOrchestra = server.roles[server.roles.length - 1] === 'reviewer';
server.failRole = true;
await windowsChoice(d, 'setup-role', 'worker').dispatch('change');
await tick(); await tick();
const roleError = attr(d, 'data-wizard-error', 'true')[0];
out.roleErrorShownInsideTheDialog = roleError.hidden === false && roleError.textContent.includes('Only the agent’s owner may change its role.');
server.failRole = false;

// ---- 5. Kept from the three-step wizard's test: the keep-alive and its warning (.330), the online
//         line on the last screen (only while visible), the picker's remembered folder (.329), and
//         the stored state cleared on sign-out ---------------------------------------------------
const reads = () => sessionReads;
const before = reads();
globalThis.document.dispatchEvent({ type: SESSION_IDLE_WARNING_EVENT, detail: { remainingSeconds: 90 } });
const banner = attr(d, 'data-wizard-idle-warning', 'true')[0];
out.idleWarningShownInTheWizard = banner.hidden === false && /about 2 min \(90s\)/.test(banner.textContent);
await button(d, 'wizard-keep-alive').dispatch('click');
await tick();
out.keepSessionOpenTouchesTheSession = reads() > before && banner.hidden === true;
d.wizard.show('resume');
server.lastSeen = new Date(Date.now() + 5000).toISOString();
globalThis.document.visibilityState = 'hidden';
const callsHidden = agentReads;
const hiddenPoll = await d.wizard.pollOnce();
out.hiddenTabDoesNotPoll = hiddenPoll === false && agentReads === callsHidden;
globalThis.document.visibilityState = 'visible';
await d.wizard.pollOnce();
const online = attr(d, 'data-online-status', 'true')[0];
out.onlineLineSaysConnected = online.getAttribute('data-online-state') === 'connected' && /Connected: Kestrel has reached Orchestra/.test(online.textContent);
// kittrial-5bb.360 (review of .348): once it said connected, the check stops: no more agent reads.
const readsAfterSuccess = agentReads;
const pollAgain = [await d.wizard.pollOnce(), await d.wizard.pollOnce()];
out.onlineCheckStopsAfterSuccess = pollAgain.every((x) => x === false) && agentReads === readsAfterSuccess;
// An agent never seen when the wizard opened, then seen (on the VS Code screen, before this one):
// the last screen says connected (the real-browser walk-through found it stuck on "Waiting").
server.lastSeen = null;
d = await openWizard();
server.lastSeen = '2026-10-18T00:00:00Z';
d.wizard.show('resume');
await d.wizard.pollOnce();
const onlineAgain = attr(d, 'data-online-status', 'true')[0];
out.onlineWhenFirstSeenBeforeTheLastScreen = onlineAgain.getAttribute('data-online-state') === 'connected';
d.wizard.show('system');
await windowsChoice(d, 'setup-bridge', 'no').dispatch('change');
d.wizard.show('files');
await button(d, 'save-files').dispatch('click');
await tick(); await tick();
await button(d, 'save-files').dispatch('click');
await tick(); await tick();
out.pickerRemembersTheFolder = pickerCalls.length === 2 && !pickerCalls[0].startIn && Boolean(pickerCalls[1].startIn);
out.savedFilesSaidOnTheScreen = /Saved in kestrel: \.orchestra\/agent\.json, \.orchestra\/AGENT\.md\./.test(attr(d, 'data-save-status', 'true')[0].textContent);
clearWizardStorage();
out.signOutClearsTheStoredScreen = !store.has(WIZARD_STORAGE_KEY);

// ---- kittrial-5bb.360: an address that may not go into a command is never used in one -----------
const goodOrigin = globalThis.location.origin;
globalThis.location.origin = 'https://h/$(touch PWNED)';
store.clear();
d = await openWizard();
d.wizard.show('vscode');
const refusedLine = attr(d, 'data-server-refused', 'true')[0];
out.refusedAddressSaidAndNotUsed = Boolean(refusedLine) && /cannot be used in a command/.test(refusedLine.textContent)
  && !JSON.stringify(d.all(() => true).map((e) => e.textContent)).includes('touch PWNED');
globalThis.location.origin = goodOrigin;
d = await openWizard();
out.goodAddressHasNoRefusedLine = attr(d, 'data-server-refused', 'true').length === 0;

// ---- kittrial-5bb.545: the proxy line names the host this page is connected to, whatever it is ----
const hostsNamed = {};
for (const [origin, host] of [['https://orchestra.office.example:7443', 'orchestra.office.example'], ['https://[2001:db8::7]:8443', '2001:db8::7']]) {
  globalThis.location.origin = origin;
  store.clear();
  d = await openWizard();
  d.wizard.show('vscode');
  hostsNamed[origin] = noProxyLine(d).length === 1 && noProxyLine(d)[0].textContent.startsWith('If this computer uses a proxy, first add ' + host + ' to VS Code\'s Http: No Proxy setting (')
    && !noProxyLine(d)[0].textContent.includes('127.0.0.1');
}
globalThis.location.origin = goodOrigin;
store.clear();
out.noProxyLineNamesTheHostOfThisPage = Object.values(hostsNamed).length === 2 && Object.values(hostsNamed).every(Boolean);
// The sentence, pinned for one sample server, for the Bridge's copy.
out.noProxySentencePinned = setupText.bridgeNoProxySentence('https://office.example:7443')
  === 'If this computer uses a proxy, first add office.example to VS Code\'s Http: No Proxy setting (File > Preferences > Settings, search “No Proxy”; on macOS Code > Settings).';

// ---- 6. Site-wide: a toast raised while a modal is open is drawn inside it (kittrial-5bb.348) ----
// With a confirmation open above the wizard, the toast goes into the TOP dialog.
const confirmOpen = (await import(domUrl)).confirmDialog({ title: 'Sure?', body: 'x' });
await tick();
const topDialog = openDialogs().find((x) => x !== d);
toast('A message above the confirmation', 'crit');
out.toastDrawnInTheTopmostDialog = topDialog.all((e) => e.className === 'toast crit' && e.textContent === 'A message above the confirmation').length === 1
  && d.all((e) => e.textContent === 'A message above the confirmation').length === 0;
await topDialog.all((e) => e.tagName === 'BUTTON' && e.textContent === 'Cancel')[0].dispatch('click');
await confirmOpen;
toast('A message while the wizard is open', 'crit');
const inside = d.all((e) => e.className === 'toast crit' && e.textContent === 'A message while the wizard is open');
out.toastDrawnInsideTheOpenModal = inside.length === 1;
for (const x of openDialogs()) await x.close();
toast('A message with no dialog open');
out.toastWithoutAModalGoesToTheBody = body.childNodes && [...body.children].some((c) => c.className === 'toasts'
  && c.all((e) => e.textContent === 'A message with no dialog open').length === 1);

// ---- kittrial-5bb.387: an agent holding an effective coordinator grant is set up as a coordinator -----
store.clear();
server.coordinates = [{ project: 'p1', effective: true }];
const rolesBefore = server.roles.length;
d = await openWizard();
await d.wizard.next();
out.coordinatorRoleScreen = screenOf(d) === 'role' && attr(d, 'data-role-coordinator', 'true').length === 1
  && /Kestrel holds the coordinator grant for Alpha: it is set up as a coordinator\./.test(shownText(d));
const coordinatorRoleText = d.all(() => true).map((e) => e.textContent).join('\n');
out.coordinatorRoleKeepsGrantLocation = coordinatorRoleText.includes('The grant is given here or on the project’s Members & settings page, and removed on the project’s Members & settings page.')
  && coordinatorRoleText.includes('Without a grant that has effect, this screen offers worker and reviewer (and coordinator when you may grant it), and AGENT.md is the worker’s.');
out.coordinatorSetupHintAbsentWithGrant = attr(d, 'data-role-coordinator-help', 'true').length === 0;
out.coordinatorHasNoRoleChoice = !windowsChoice(d, 'setup-role', 'worker') && !windowsChoice(d, 'setup-role', 'reviewer');
await d.wizard.next();
out.coordinatorRoleNotSaved = server.roles.length === rolesBefore;
// Without an effective grant, the worker/reviewer screen explains where to grant coordinator access.
store.clear();
server.coordinates = undefined;
d = await openWizard();
await d.wizard.next();
out.coordinatorSetupHintPresentWithoutGrant = attr(d, 'data-role-coordinator-help', 'true').length === 1
  && attr(d, 'data-role-coordinator-help', 'true')[0].textContent
    === "To set this agent up as a project's coordinator, give it the coordinator grant on that project's Members & settings page first, then open this set-up again. The agent's owner must also be a member of that project, or this set-up still writes the worker's files.";
// A grant without effect still uses the non-coordinator screen and its grant hint.
store.clear();
server.coordinates = [{ project: 'p1', effective: false }];
d = await openWizard();
await d.wizard.next();
out.noEffectGrantIsAWorker = attr(d, 'data-role-coordinator', 'true').length === 0 && windowsChoice(d, 'setup-role', 'worker').checked === true;
out.noEffectGrantShowsCoordinatorHint = attr(d, 'data-role-coordinator-help', 'true').length === 1;
server.coordinates = undefined;
for (const open of openDialogs()) await open.close();   // a dialog left open keeps its online check going

// ---- kittrial-5bb.521: making the agent a coordinator from the wizard's role screen ----------------
async function readFilesGuideAndPrompt(dialog) {
  const prevScreen = screenOf(dialog);
  dialog.wizard.show('system');
  const prevRuns = ['yes', 'no', 'none'].find((v) => windowsChoice(dialog, 'setup-bridge', v)?.checked) || 'yes';
  await windowsChoice(dialog, 'setup-bridge', 'no').dispatch('change');
  dialog.wizard.show('files');
  copiedText = null;
  const copyBtn = dialog.all((e) => e.tagName === 'BUTTON' && e.textContent === 'Copy setup prompt (creates the files)')[0];
  await copyBtn.dispatch('click');
  await tick();
  const prompt = copiedText;
  const screenText = dialog.all(() => true).map((e) => e.textContent).join('\n');
  dialog.wizard.show('system');
  await windowsChoice(dialog, 'setup-bridge', prevRuns).dispatch('change');
  dialog.wizard.show(prevScreen);
  return { prompt, screenText };
}
store.clear();
server.coordinates = undefined;
server.coordinateGrantableProjects = [];
d = await openWizard();
await d.wizard.next();
out.nonOwnerHasNoCoordinatorChoice = !windowsChoice(d, 'setup-role', 'coordinator:p1') && !button(d, 'grant-coordinator')
  && attr(d, 'data-role-coordinator-help', 'true').length === 1;
// m1-nonmember-superuser: a project (p3) where the agent's owner is not a member is omitted from
// coordinate_grantable_projects (the same owner-member rule as coordinates), even for a superuser.
store.clear();
server.ownerMemberships = ['p1', 'p2'];
server.agentProjects = ['p3'];
server.coordinateGrantableProjects = ['p3'];
d = await openWizard();
await d.wizard.next();
const nonMemberOnlyHasNoChoice = !windowsChoice(d, 'setup-role', 'coordinator:p3')
  && !button(d, 'grant-coordinator')
  && attr(d, 'data-role-coordinator-help', 'true').length === 1
  && attr(d, 'data-role-coordinator-help', 'true')[0].textContent.includes("The agent's owner must also be a member of that project, or this set-up still writes the worker's files.");
store.clear();
server.agentProjects = ['p1', 'p2', 'p3'];
server.coordinateGrantableProjects = ['p1', 'p2', 'p3'];
d = await openWizard();
await d.wizard.next();
out.nonMemberSuperuserProjectOmittedFromWizard = nonMemberOnlyHasNoChoice
  && Boolean(windowsChoice(d, 'setup-role', 'coordinator:p1'))
  && Boolean(windowsChoice(d, 'setup-role', 'coordinator:p2'))
  && !windowsChoice(d, 'setup-role', 'coordinator:p3')
  && !shownText(d).includes('Coordinator of Gamma');
server.agentProjects = ['p1'];
out.ownerSeesCoordinatorChoiceForEachGrantableProject = Boolean(windowsChoice(d, 'setup-role', 'coordinator:p1'))
  && Boolean(windowsChoice(d, 'setup-role', 'coordinator:p2'))
  && shownText(d).includes('Coordinator of Alpha')
  && shownText(d).includes('Coordinator of Beta')
  && attr(d, 'data-role-coordinator-help', 'true').length === 0
  && !button(d, 'grant-coordinator');
const rolesBeforeCoordSelect = server.roles.length;
const transportBeforeSelectP1 = transportCalls;
const coordP1Radio = windowsChoice(d, 'setup-role', 'coordinator:p1');
await coordP1Radio.dispatch('change');
await tick();
out.selectingCoordinatorRadioShowsButtonWithoutGranting = transportCalls === transportBeforeSelectP1
  && server.coordinatorGrants.length === 0
  && server.roles.length === rolesBeforeCoordSelect
  && windowsChoice(d, 'setup-role', 'coordinator:p1') === coordP1Radio
  && globalThis.document.activeElement === coordP1Radio
  && Boolean(button(d, 'grant-coordinator'))
  && button(d, 'grant-coordinator').textContent === 'Make coordinator of Alpha';
// S6: Next is held with one line while 'Coordinator of P' is chosen, until the button is clicked or another role is chosen.
const heldNext = await d.wizard.next();
const roleStatus = attr(d, 'data-wizard-check-status', 'true')[0];
out.coordinatorChoiceHoldsNextWithOneLine = heldNext === false
  && screenOf(d) === 'role'
  && roleStatus.hidden === false
  && roleStatus.textContent === 'Click “Make coordinator of Alpha” first, or choose another role before continuing.';
const transportBeforeSwitchP2 = transportCalls;
const coordP2Radio = windowsChoice(d, 'setup-role', 'coordinator:p2');
await coordP2Radio.dispatch('change');
await tick();
out.switchingCoordinatorProjectUpdatesButtonWithoutWriting = transportCalls === transportBeforeSwitchP2
  && server.coordinatorGrants.length === 0
  && server.coordinatorRevokes.length === 0
  && windowsChoice(d, 'setup-role', 'coordinator:p2') === coordP2Radio
  && globalThis.document.activeElement === coordP2Radio
  && roleStatus.hidden === true
  && Boolean(button(d, 'grant-coordinator'))
  && button(d, 'grant-coordinator').textContent === 'Make coordinator of Beta';
const workerRadio = windowsChoice(d, 'setup-role', 'worker');
await workerRadio.dispatch('change');
await tick();
out.switchingBackToWorkerHidesButtonAndRevokesNothing = !button(d, 'grant-coordinator')
  && server.coordinatorGrants.length === 0
  && server.coordinatorRevokes.length === 0
  && windowsChoice(d, 'setup-role', 'worker') === workerRadio
  && globalThis.document.activeElement === workerRadio
  && windowsChoice(d, 'setup-role', 'worker').checked === true;
await windowsChoice(d, 'setup-role', 'coordinator:p1').dispatch('change');
await tick();
// Note (2): A radio change during a refused PUT re-enables the newly created button.
server.failCoordinatorGrant = true;
let switchedBtnDuringRefusedPut = null;
server.onCoordinatorPutInFlight = async () => {
  await windowsChoice(d, 'setup-role', 'coordinator:p2').dispatch('change');
  switchedBtnDuringRefusedPut = button(d, 'grant-coordinator');
};
await button(d, 'grant-coordinator').dispatch('click');
await tick(); await tick();
server.onCoordinatorPutInFlight = null;
const grantError = attr(d, 'data-wizard-error', 'true')[0];
out.radioChangeDuringRefusedPutReenablesNewButton = Boolean(switchedBtnDuringRefusedPut)
  && button(d, 'grant-coordinator') === switchedBtnDuringRefusedPut
  && switchedBtnDuringRefusedPut.textContent === 'Make coordinator of Beta'
  && switchedBtnDuringRefusedPut.disabled === false;
const refusedFiles = await readFilesGuideAndPrompt(d);
out.refusedCoordinatorGrantShowsErrorAndLeavesWorkerGuide = grantError.hidden === false
  && grantError.textContent.includes('Project role does not permit this operation')
  && attr(d, 'data-role-coordinator', 'true').length === 0
  && Boolean(refusedFiles.prompt)
  && refusedFiles.prompt.includes('role is worker')
  && refusedFiles.prompt.includes('Claim exactly one that nobody holds')
  && !refusedFiles.prompt.includes('## You coordinate')
  && refusedFiles.screenText.includes('Claim exactly one that nobody holds')
  && !refusedFiles.screenText.includes('## You coordinate');
server.failCoordinatorGrant = false;
await windowsChoice(d, 'setup-role', 'coordinator:p1').dispatch('change');
await tick();
// S3 & s3-refusal-prompt: If PUT answers 200 (e.g. replay after revoke) but the re-read GET /v1/agents/{aid}
// has no effective grant, the wizard says so in one line and keeps the worker guide/prompt.
server.grantHasNoEffectOnReread = true;
await button(d, 'grant-coordinator').dispatch('click');
await tick(); await tick();
const noEffectFiles = await readFilesGuideAndPrompt(d);
out.rereadWithoutEffectiveGrantSaysSoInOneLine = grantError.hidden === false
  && grantError.textContent === 'Kestrel does not hold an active coordinator grant for Alpha.'
  && attr(d, 'data-role-coordinator', 'true').length === 0
  && Boolean(noEffectFiles.prompt)
  && noEffectFiles.prompt.includes('role is worker')
  && noEffectFiles.prompt.includes('Claim exactly one that nobody holds')
  && !noEffectFiles.prompt.includes('## You coordinate')
  && noEffectFiles.screenText.includes('Claim exactly one that nobody holds')
  && !noEffectFiles.screenText.includes('## You coordinate');
// Note (1) & kittrial-5bb.541 (1, 3): When P is no longer grantable after the re-read, state.coordinatorProject
// is cleared, the screen re-renders, the error says this set-up cannot tell and directs to Members & settings,
// and the files screen keeps the worker's AGENT.md and setup prompt.
server.dropFromGrantableOnReread = true;
await button(d, 'grant-coordinator').dispatch('click');
await tick(); await tick();
server.dropFromGrantableOnReread = false;
server.grantHasNoEffectOnReread = false;
const errorShownAfterDrop = grantError.hidden === false
  && grantError.textContent === "This set-up cannot tell whether Kestrel holds a coordinator grant for Alpha. Check the project's Members & settings page.";
const choiceRemovedAfterDrop = !windowsChoice(d, 'setup-role', 'coordinator:p1')
  && Boolean(windowsChoice(d, 'setup-role', 'coordinator:p2'))
  && !button(d, 'grant-coordinator')
  && windowsChoice(d, 'setup-role', 'worker').checked === true;
const droppedFiles = await readFilesGuideAndPrompt(d);
const advancedAfterDrop = await d.wizard.next();
out.rereadWithoutGrantableProjectClearsChoiceAndRerenders = errorShownAfterDrop
  && choiceRemovedAfterDrop
  && advancedAfterDrop === true
  && roleStatus.hidden === true
  && screenOf(d) !== 'role'
  && Boolean(droppedFiles.prompt)
  && droppedFiles.prompt.includes('role is worker')
  && droppedFiles.prompt.includes('Claim exactly one that nobody holds')
  && !droppedFiles.prompt.includes('## You coordinate')
  && droppedFiles.screenText.includes('Claim exactly one that nobody holds')
  && !droppedFiles.screenText.includes('## You coordinate');
for (const open of openDialogs()) await open.close();
store.clear();
server.coordinateGrantableProjects = ['p1', 'p2'];
d = await openWizard();
await d.wizard.next();
await windowsChoice(d, 'setup-role', 'coordinator:p1').dispatch('change');
await tick();
const grantErrorAfterReopen = attr(d, 'data-wizard-error', 'true')[0];
const roleStatusAfterReopen = attr(d, 'data-wizard-check-status', 'true')[0];
// Note (3) & kittrial-5bb.541 (4): If the re-read fails after a committed PUT, don't say 'could not confirm
// whether this was saved'; say the grant may be saved and re-read before retrying, and Next agrees.
server.grantHasNoEffectOnReread = true;
server.failAgentReread = true;
await button(d, 'grant-coordinator').dispatch('click');
await tick(); await tick();
server.failAgentReread = false;
server.grantHasNoEffectOnReread = false;
const heldAfterFailedReread = await d.wizard.next();
out.failedRereadAfterCommittedPutSaysMayBeSavedAndRereadBeforeRetrying = grantErrorAfterReopen.hidden === false
  && grantErrorAfterReopen.textContent.includes('The coordinator grant for Alpha may be saved, but re-reading Kestrel failed. Re-open this set-up to re-read before retrying.')
  && grantErrorAfterReopen.textContent.includes('req_reread_500')
  && !grantErrorAfterReopen.textContent.includes('could not confirm whether this was saved')
  && heldAfterFailedReread === false
  && screenOf(d) === 'role'
  && roleStatusAfterReopen.hidden === false
  && roleStatusAfterReopen.textContent === 'The coordinator grant for Alpha may be saved, but re-reading Kestrel failed. Re-open this set-up to re-read, or choose another role before continuing.'
  && !roleStatusAfterReopen.textContent.includes('Click “Make coordinator');
server.coordinatorGrants.length = 0;
// Note 1 (r2): Button is disabled while PUT is in flight so a double-click sends only one PUT.
let seenDisabledDuringFlight = false;
const grantBtn = button(d, 'grant-coordinator');
server.onCoordinatorPutInFlight = async () => {
  seenDisabledDuringFlight = grantBtn.disabled === true;
  await grantBtn.dispatch('click');
};
const readsBeforeGrant = agentReads;
await grantBtn.dispatch('click');
await tick(); await tick();
server.onCoordinatorPutInFlight = null;
out.clickingMakeCoordinatorGrantsAndShowsHeldReadOnly = seenDisabledDuringFlight === true
  && JSON.stringify(server.coordinatorGrants) === JSON.stringify(['p1/agent_1'])
  && agentReads === readsBeforeGrant + 1
  && server.coordinatorRevokes.length === 0
  && attr(d, 'data-role-coordinator', 'true').length === 1
  && /Kestrel holds the coordinator grant for Alpha: it is set up as a coordinator\./.test(shownText(d))
  && !button(d, 'grant-coordinator')
  && !windowsChoice(d, 'setup-role', 'worker');
d.wizard.show('system');
await windowsChoice(d, 'setup-bridge', 'no').dispatch('change');
d.wizard.show('files');
copiedText = null;
const copyPromptBtn = d.all((e) => e.tagName === 'BUTTON' && e.textContent === 'Copy setup prompt (creates the files)')[0];
await copyPromptBtn.dispatch('click');
await tick();
const filesScreenText = d.all(() => true).map((e) => e.textContent).join('\n');
out.clickingMakeCoordinatorUpdatesGuideAndPromptFromStart = Boolean(copiedText)
  && copiedText.includes('## You coordinate')
  && copiedText.includes('You hold the coordinator grant for p1.')
  && copiedText.includes('role is coordinator')
  && filesScreenText.includes('## You coordinate')
  && filesScreenText.includes('You hold the coordinator grant for p1.');
server.coordinateGrantableProjects = undefined;
for (const open of openDialogs()) await open.close();

// ---- kittrial-5bb.456: what the agent runs in, the secret screens and the credentials -------------
store.clear();
d = await openWizard();
out.firstScreenAsksWhatTheAgentRunsIn = /What does this agent run in\?/.test(shownText(d));
out.threeRoutesNamedPlainly = ['yes', 'no', 'none'].every((v) => windowsChoice(d, 'setup-bridge', v))
  && /No Bridge: Claude Code, Codex, a command-line agent/.test(d.textContent);
out.bridgeRoutePreselected = windowsChoice(d, 'setup-bridge', 'yes').checked === true;
await windowsChoice(d, 'setup-bridge', 'none').dispatch('change');
out.noBridgePlanHasNoVsCodeScreen = !d.wizard.plan().includes('vscode') && d.wizard.plan().at(-1) === 'resume';
out.noBridgeRouteStoredWithoutASecret = JSON.parse(store.get(WIZARD_STORAGE_KEY)).runs === 'none';
out.noBridgeWalkSkipsVsCode = !(await walk(d)).includes('vscode');
out.noBridgeResumeSaysPasteIntoTheChat = /Paste this into the agent’s chat/.test(shownText(d)) && /never the secret/.test(shownText(d));
out.olderRouteStillHasVsCode = setupText.screenPlan({ bridge: false, os: 'windows', vscode: true }).includes('vscode');
// An older stored state (only the boolean) maps to a route: true to Bridge, false to older.
store.clear();
store.set(WIZARD_STORAGE_KEY, JSON.stringify({ agentId: 'agent_1', screen: 'role', bridge: false, os: 'windows', role: 'worker' }));
d = await openWizard();
out.oldStoredResumesTheStoredScreen = screenOf(d) === 'role';
await d.wizard.show('system');
out.oldStoredFalseMeansOlderBridge = windowsChoice(d, 'setup-bridge', 'no').checked === true && d.wizard.plan().includes('vscode');
store.clear();
store.set(WIZARD_STORAGE_KEY, JSON.stringify({ agentId: 'agent_1', screen: 'role', bridge: true, os: 'windows', role: 'worker' }));
d = await openWizard();
await d.wizard.show('system');
out.oldStoredTrueMeansBridge = windowsChoice(d, 'setup-bridge', 'yes').checked === true;
// Reopening or reloading on the secret screen never issues a secret by itself.
store.clear();
store.set(WIZARD_STORAGE_KEY, JSON.stringify({ agentId: 'agent_1', screen: 'secret', runs: 'bridge', bridge: true, os: 'windows', role: 'worker' }));
const issuedBefore = server.issued;
d = await openWizard();
out.reloadOnTheSecretScreenIssuesNothing = screenOf(d) === 'secret' && server.issued === issuedBefore && attr(d, 'data-secret-file-text', 'true').length === 0;
out.noSecretScreenSaysItCannotBeShownAgain = /This page cannot show the secret again\./.test(shownText(d)) && /the agent keeps using it/.test(shownText(d));
// Issue a new secret three times: the page says how many credentials work and offers to revoke the older ones.
server.credentials.push({ id: 'cred_old1', label: 'one', scopes: ['read'], working: true, created_at: '2026-10-01T00:00:00Z' },
  { id: 'cred_old2', label: 'two', scopes: ['read'], working: true, created_at: '2026-10-02T00:00:00Z' });
store.clear();
d = await openWizard();
await d.wizard.show('secret');
out.noSecretScreenCountsWorkingCredentials = /It has 2 working credentials now\./.test(shownText(d));
let issueNew = button(d, 'issue-secret').dispatch('click');
await tick();
confirm = openDialogs().find((x) => x !== d);
await confirm.all((e) => e.tagName === 'BUTTON' && e.textContent === 'Issue new secret')[0].dispatch('click');
await issueNew; await tick(); await tick();
const olderBlock = attr(d, 'data-older-credentials', '2')[0];
out.issuedScreenSaysHowManyWorkNow = Boolean(olderBlock) && /This agent now has 3 working credentials/.test(olderBlock.textContent);
out.olderCredentialsOfferedOutsideMoreAbout = Boolean(olderBlock) && !d.all((e) => e.tagName === 'DETAILS' && e.all((x) => x === olderBlock).length).length;
out.saveNowAndNeverInAChatOnTheSecretScreen = /Save this secret now, in the file named below\. Never paste it into a chat\./.test(shownText(d));
out.issuedSecretIsNotTheCreateNotice = attr(d, 'data-secret-later', 'true').length === 0;
const revokeAll = attr(d, 'data-action', 'revoke-older')[0];
let pendingRevoke = revokeAll.dispatch('click');
await tick();
confirm = openDialogs().find((x) => x !== d);
out.revokeAllAsksAndNamesTheCount = /Revoke the 2 older credentials\?/.test(confirm.textContent);
await confirm.all((e) => e.tagName === 'BUTTON' && e.textContent === 'Cancel')[0].dispatch('click');
await pendingRevoke; await tick();
out.cancelledRevokeAllRevokesNothing = server.revoked.length === 0;
pendingRevoke = revokeAll.dispatch('click');
await tick();
confirm = openDialogs().find((x) => x !== d);
await confirm.all((e) => e.tagName === 'BUTTON' && e.textContent === 'Revoke 2')[0].dispatch('click');
await pendingRevoke; await tick(); await tick();
out.revokeAllRevokesOnlyThisAgentsOlderCredentials = JSON.stringify(server.revoked) === JSON.stringify(['agent_1/cred_old1', 'agent_1/cred_old2']);
out.revokedRowsShowRevoked = d.all((e) => e.tagName === 'SPAN' && e.textContent === 'Revoked').length === 2;
// After a single revocation the sentence, the count and the revoke-all label follow; the credential
// just issued is never offered or revoked, even when the snapshot already lists it.
server.revoked.length = 0;
server.listNew = true;
server.credentials.push({ id: 'cred_a', label: 'A', scopes: ['read'], working: true, created_at: '2026-10-03T00:00:00Z' },
  { id: 'cred_b', label: 'B', scopes: ['read'], working: true, created_at: '2026-10-04T00:00:00Z' },
  { id: 'cred_c', label: 'C', scopes: ['read'], working: true, created_at: '2026-10-05T00:00:00Z' });
store.clear();
d = await openWizard();
await d.wizard.show('secret');
issueNew = button(d, 'issue-secret').dispatch('click');
await tick();
confirm = openDialogs().find((x) => x !== d);
await confirm.all((e) => e.tagName === 'BUTTON' && e.textContent === 'Issue new secret')[0].dispatch('click');
await issueNew; await tick(); await tick();
const newId = 'cred_' + server.issued;
const rows = () => d.all((e) => e.tagName === 'BUTTON' && /^Revoke credential /.test(e.getAttribute('aria-label') || ''));
out.justIssuedCredentialIsNotOffered = rows().length === 3 && !rows().some((b) => b.getAttribute('aria-label').includes('the new one'));
let block = attr(d, 'data-older-credentials', '3')[0];
out.threeOlderCredentialsListed = Boolean(block) && /This agent now has 4 working credentials/.test(block.textContent);
let one = rows().find((b) => b.getAttribute('aria-label') === 'Revoke credential A').dispatch('click');
await tick();
confirm = openDialogs().find((x) => x !== d);
await confirm.all((e) => e.tagName === 'BUTTON' && e.textContent === 'Revoke')[0].dispatch('click');
await one; await tick(); await tick();
block = attr(d, 'data-older-credentials', '2')[0];
out.afterOneRevokeSentenceAndCountFollow = Boolean(block) && /This agent now has 3 working credentials/.test(block.textContent)
  && attr(d, 'data-credential-total', '3').length === 1 && attr(d, 'data-older-credentials', '3').length === 0;
const allNow = attr(d, 'data-action', 'revoke-older')[0];
out.afterOneRevokeTheAllButtonCountsTwo = Boolean(allNow) && allNow.textContent === 'Revoke the 2 older credentials'
  && allNow.getAttribute('aria-label') === 'Revoke the 2 older credentials';
pendingRevoke = allNow.dispatch('click');
await tick();
confirm = openDialogs().find((x) => x !== d);
out.afterOneRevokeTheConfirmationCountsTwo = /Revoke the 2 older credentials\?/.test(confirm.textContent)
  && confirm.all((e) => e.tagName === 'BUTTON' && e.textContent === 'Revoke 2').length === 1;
await confirm.all((e) => e.tagName === 'BUTTON' && e.textContent === 'Revoke 2')[0].dispatch('click');
await pendingRevoke; await tick(); await tick();
out.revokeAllAfterOneRevokesTheRestButNeverTheNewOne = JSON.stringify(server.revoked) === JSON.stringify(['agent_1/cred_a', 'agent_1/cred_b', 'agent_1/cred_c'])
  && !server.revoked.some((r) => r.endsWith('/' + newId));
out.afterRevokeAllOnlyTheNewOneIsSaid = /Only the new credential works now\./.test(shownText(d)) && attr(d, 'data-action', 'revoke-older').length === 0
  && attr(d, 'data-credential-total', '1').length === 1;
const rulePayload = setupText.secretlessPayload({ agent: { id: 'agent_1', name: 'Kestrel', owner: 'u1', owner_display_name: 'Olive', role: 'worker', projects: [] } }, 'https://127.0.0.1:8765');
const promptText = setupText.setupPrompt(rulePayload, { role: 'worker' });
out.pastedSecretRuleInTheGuideAndPrompt = setupText.agentGuide(rulePayload, {}).includes(setupText.PASTED_SECRET_RULE)
  && promptText.includes(setupText.PASTED_SECRET_RULE)
  && promptText.split('\n').filter((line) => line.startsWith('5. ') && line.includes(setupText.PASTED_SECRET_RULE)).length === 1
  && /do not use it and do not save it: say that it is now exposed and ask your owner to issue a new one/.test(setupText.PASTED_SECRET_RULE);
// ---- kittrial-5bb.550 r2 (review S2): the proxy line names the host this page uses, on the create path too ----
// The server's create answer carries a configured public address (setup.config.server_url) that differs from
// location.origin; the Bridge is given location.origin, so the line must name its host, with macOS chosen.
{
  for (const open of openDialogs()) await open.close();
  store.clear();
  const PUBLIC = 'https://orchestra.public.example';
  const createdAgent = { id: 'agent_9', name: 'Heron', owner: 'u1', projects: [], working_directory: '/home/olive/heron', scopes: ['read', 'tasks'], scopes_source: 'stored', credentials: [] };
  const answer = { agent: createdAgent, credential: { id: 'cred_9', secret: SECRET }, setup: { config: { agent_id: 'agent_9', name: 'Heron', server_url: PUBLIC, projects: [] } } };
  const overrides = {
    agents: async () => ({ items: [], total: 0, next_cursor: null }),
    accounts: async () => ({ items: [] }),
    agentHistory: async () => ({ items: [] }),
    createAgent: async () => answer,
    agentCertificate: async () => null,
    agent: async () => createdAgent,
  };
  const stubApi = new Proxy(api, { get: (target, key) => (key in overrides ? overrides[key] : target[key]) });
  const originalQueryAll = Element.prototype.querySelectorAll;
  Element.prototype.querySelectorAll = function (selector) {
    if (selector === 'input[name=project]:checked') return this.all((e) => e.tagName === 'INPUT' && e.getAttribute('name') === 'project' && e.checked);
    return originalQueryAll.call(this, selector);
  };
  const page = await agentsList({ ...ctx, api: stubApi, projects: [], render() {}, refreshProjects: async () => {} });
  await new Promise((r) => setTimeout(r, 50));
  page.querySelector('#a-name').value = 'Heron';
  await page.all((e) => e.tagName === 'FORM' && e.querySelector('#a-name'))[0].dispatch('submit');
  await new Promise((r) => setTimeout(r, 300));
  Element.prototype.querySelectorAll = originalQueryAll;
  const wizards = openDialogs().filter((x) => x.getAttribute('data-setup-wizard') === 'true');
  d = wizards[wizards.length - 1];
  d.wizard.show('system');
  await windowsChoice(d, 'setup-os', 'macos').dispatch('change');
  await windowsChoice(d, 'setup-bridge', 'yes').dispatch('change');
  d.wizard.show('vscode');
  const line = noProxyLine(d);
  // The precondition: the wizard's own payload for this answer does carry the configured public address.
  out.noProxyLineNamesThisPagesHostOnCreateWithAConfiguredAddress = setupText.secretlessPayload(answer, location.origin).server === PUBLIC
    && wizards.length === 1 && line.length === 1
    && line[0].textContent.startsWith('If this computer uses a proxy, first add 127.0.0.1 to VS Code\'s Http: No Proxy setting (')
    && !line[0].textContent.includes('orchestra.public.example');
  for (const open of openDialogs()) await open.close();
  store.clear();
}

for (const open of openDialogs()) await open.close();

console.log(JSON.stringify(out));
