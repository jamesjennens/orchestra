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
Object.defineProperty(globalThis, 'navigator', {
  value: { platform: 'Win32', userAgent: 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)' }, configurable: true,
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
const { setupWizardPage, WIZARD_STORAGE_KEY, clearWizardStorage } = await import(agentsUrl);
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
};
let sessionReads = 0;
let agentReads = 0;
const api = createApi(async (method, path, _headers, text) => {
  const sent = text ? JSON.parse(text) : undefined;
  if (method === 'GET' && path === '/v1/sessions/current') { sessionReads += 1; return { status: 200, data: { csrf_token: 'c' } }; }
  if (method === 'GET' && path === '/v1/service/certificate') {
    return server.certificate ? { status: 200, data: server.certificate } : { status: 404, data: { error: { code: 'not_found', message: 'none' } } };
  }
  if (method === 'GET' && path === '/v1/agents/agent_1') {
    agentReads += 1;
    return { status: 200, data: { id: 'agent_1', name: 'Kestrel', owner: 'u1', role: 'worker', scopes: server.scopes, scopes_source: 'stored',
      working_directory: 'C:\\Users\\olive\\kestrel', projects: ['p1'], last_seen_at: server.lastSeen, credentials: [],
      ...(server.coordinates ? { coordinates: server.coordinates } : {}) } };
  }
  if (method === 'POST' && path === '/v1/agents/agent_1/credentials') {
    if (server.failIssue) return { status: 0, data: { error: { code: 'network', message: 'The server could not be reached.' } } };
    server.issued += 1;
    return { status: 201, data: { credential: { id: 'cred_' + server.issued, secret: SECRET, scopes: ['read', 'tasks'] } } };
  }
  if (method === 'PATCH' && path === '/v1/agents/agent_1') {
    if (server.failRole) return { status: 403, data: { error: { code: 'forbidden', message: 'Only the agent’s owner may change its role.' } } };
    server.roles.push(sent.role); return { status: 200, data: {} };
  }
  return { status: 404, data: { error: { code: 'not_found', message: 'no route ' + method + ' ' + path } } };
});
const ctx = { api, me: { id: 'u1' }, query: { get: (k) => (k === 'agent' ? 'agent_1' : null) }, href: (p) => '#' + p, go: () => {} };

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
  && attr(d, 'data-secret-note', 'true')[0].textContent === 'The secret is shown only once, when it is issued; issue a new one to continue.';
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
  && /Kestrel holds the coordinator grant for p1: it is set up as a coordinator\./.test(shownText(d));
out.coordinatorHasNoRoleChoice = !windowsChoice(d, 'setup-role', 'worker') && !windowsChoice(d, 'setup-role', 'reviewer');
await d.wizard.next();
out.coordinatorRoleNotSaved = server.roles.length === rolesBefore;
// A grant without effect: the worker's screen, as before.
store.clear();
server.coordinates = [{ project: 'p1', effective: false }];
d = await openWizard();
await d.wizard.next();
out.noEffectGrantIsAWorker = attr(d, 'data-role-coordinator', 'true').length === 0 && windowsChoice(d, 'setup-role', 'worker').checked === true;
server.coordinates = undefined;
for (const open of openDialogs()) await open.close();   // a dialog left open keeps its online check going

console.log(JSON.stringify(out));
