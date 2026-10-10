// Agent folder setup content: pure string building, no DOM and no secret.
//
// Everything here is built from the *secretless* payload returned by
// secretlessPayload(): it copies only the agent's identity, projects and server
// address out of the create response and never reads its credential. The save,
// copy and prompt paths in views/agents.js only ever receive this payload, so the
// one-time secret cannot reach a file or a prompt by construction
// (tests/test_http_web.py checks both the construction and the output).

export const CONFIG_PATH = '.orchestra/agent.json';
export const GUIDE_PATH = '.orchestra/AGENT.md';
export const GITIGNORE_LINE = '.orchestra/';
//: The VS Code workspace file the Orchestra Bridge extension reads (kittrial-5bb.203).
export const VSCODE_SETTINGS_PATH = '.vscode/settings.json';
//: What the extension does with new work. "prefill" wakes the agent and types the prompt into
//: Copilot Chat without sending it: the person still presses Enter, and the first wake after a
//: reload is asked for anyway. Nothing is ever sent into a chat unasked.
export const BRIDGE_ON_WORK = 'prefill';
//: The extension's two roles: one window is one role.
export const BRIDGE_ROLES = Object.freeze(['worker', 'reviewer']);

const plain = (name) => String(name || '').replace(/\s*\(agent[^)]*\)$/, '');

// File-name-safe slug of the agent name: lowercase [a-z0-9-], at most 40 characters.
// Must stay identical to agent_slug() in http_auth.py (tests compare them).
export function slug(name) {
  const value = String(name || '').toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '')
    .slice(0, 40).replace(/-+$/g, '');
  return value || 'agent';
}

// Where the owner keeps this agent's secret: a per-agent curl config file in the user
// profile, outside the agent folder (owner's practical update, 2026-09-29).
export function secretFile(name) {
  const file = `.orchestra-agent-${slug(name)}.curlrc`;
  return Object.freeze({
    name: file,
    windows: `%USERPROFILE%\\${file}`,
    powershell: `$env:USERPROFILE\\${file}`,
    posix: `~/${file}`,
  });
}

// The one line that file holds. The real secret is passed ONLY by the dialog that is
// showing a freshly issued secret; every other caller gets the placeholder.
export const SECRET_PLACEHOLDER = '<your secret>';
export function headerLine(secretValue = SECRET_PLACEHOLDER) {
  return `header = "Authorization: Bearer ${secretValue}"`;
}

// Where the service's own certificate is saved for the extension: beside the credential file in
// the user profile, never in the agent's folder or the repository. orchestraBridge.caFile names it
// with the `~` form, which the extension itself expands on every platform (its own rule).
export function certificateFile(name) {
  const file = `.orchestra-agent-${slug(name)}.crt`;
  return Object.freeze({
    name: file,
    windows: `%USERPROFILE%\\${file}`,
    powershell: `$env:USERPROFILE\\${file}`,
    posix: `~/${file}`,
  });
}

// The comment lines the extension and a careful person read out of the credential file
// (coordinator note of 2026-10-08): curl ignores comment lines, so they cost nothing there.
// The extension's own rule for the address line is `^#\s*server\s*=\s*(https?://\S+)\s*$`, so the
// address stands alone on its line and the fingerprint gets a line of its own.
export function credentialCommentLines(server, fingerprint) {
  const lines = [`# server = ${server}`];
  if (fingerprint) lines.push(`# server certificate sha256 = ${fingerprint}`);
  return lines;
}

// The workspace settings the bridge extension reads, as plain JSON (never a secret: the extension
// reads the secret from the credential file in the user profile).
// `projects` is the one project whose code is in that window; with several projects the prompt
// leaves the kit's usual REPLACE_PROJECT_ID placeholder and says so, because empty means "every
// project the agent is granted" to the extension (coordinator note of 2026-10-08).
export function bridgeSettings(payload, { role = 'worker', server, projects, caFile = null } = {}) {
  const granted = Array.isArray(projects) ? projects : payload.projects;
  const settings = {
    'orchestraBridge.serverUrl': server || payload.server,
    'orchestraBridge.agentName': slug(payload.name),
    'orchestraBridge.enabled': true,
    'orchestraBridge.onWork': BRIDGE_ON_WORK,
    'orchestraBridge.role': BRIDGE_ROLES.includes(role) ? role : 'worker',
    'orchestraBridge.projects': granted.length === 1 ? [granted[0]] : granted.length ? ['REPLACE_PROJECT_ID'] : [],
  };
  if (caFile) settings['orchestraBridge.caFile'] = caFile;
  return settings;
}

// Merge those keys into whatever .vscode/settings.json already holds, changing nothing else.
// A file that is not valid JSON (VS Code also accepts comments, which JSON.parse does not) is
// left exactly as it is, with a sentence: guessing at a person's settings is worse than stopping.
export function mergeSettings(existingText, addition) {
  const body = String(existingText === null || existingText === undefined ? '' : existingText);
  if (!body.trim()) return { text: JSON.stringify(addition, null, 2) + '\n', refused: false, created: true };
  let current;
  try { current = JSON.parse(body); } catch (error) { current = undefined; }
  if (!current || typeof current !== 'object' || Array.isArray(current)) {
    return { text: null, refused: true, created: false };
  }
  return { text: JSON.stringify({ ...current, ...addition }, null, 2) + '\n', refused: false, created: false };
}

//: Said when .vscode/settings.json is there but is not valid JSON: the file is left alone.
export const SETTINGS_REFUSED_SENTENCE = 'The existing .vscode/settings.json is not valid JSON, so it was left exactly as it is; nothing was written into it.';

// Commands that prove the stored secret works (they never contain it).
export function testCommands(payload) {
  const file = payload.secretFile;
  return Object.freeze({
    powershell: `curl.exe -fsS -K "${file.powershell}" ${payload.server}/v1/agents/me`,
    posixChmod: `chmod 600 ${file.posix}`,
    posix: `curl -fsS -K ${file.posix} ${payload.server}/v1/agents/me`,
  });
}

// The create response minus anything secret. Works for the real service
// ({agent, setup, ...}) and the prototype's mock ({agent, server, owner_name, ...}).
export function secretlessPayload(created, fallbackServer) {
  const agent = created.agent || {};
  const setup = created.setup || {};
  const config = setup.config || {};
  const projects = (config.projects || agent.projects || []).map((p) => (typeof p === 'string' ? p : p.id));
  const name = plain(config.name || agent.name || agent.display_name);
  return Object.freeze({
    agentId: config.agent_id || agent.id,
    name,
    secretFile: secretFile(name),
    certificateFile: certificateFile(name),
    owner: agent.owner_display_name || created.owner_name || agent.owner_name || 'your owner',
    server: config.server_url || created.server || fallbackServer || '<ORCHESTRA_SERVER_URL>',
    projects: Object.freeze(projects.slice()),
    secretEnv: setup.secret_env_var || 'ORCHESTRA_AGENT_SECRET',
    workingDirectory: agent.working_directory || null,
  });
}

export function agentJson(payload) {
  return JSON.stringify({ server_url: payload.server, agent_id: payload.agentId, projects: payload.projects, name: payload.name }, null, 2) + '\n';
}

export function agentGuide(payload) {
  const file = payload.secretFile;
  const server = payload.server;
  return [
    `# ${payload.name}`,
    '',
    `You are ${payload.name}, an Orchestra agent owned by ${payload.owner}. Your settings are in ${CONFIG_PATH} (server_url, agent_id, projects). That file holds no secret.`,
    '',
    '## Your secret',
    '',
    `Your owner keeps your secret in a curl config file in their user profile: ${file.windows} on Windows, ${file.posix} on macOS/Linux. It holds one line, ${headerLine()}. Hand that file to curl with -K on every call. Never open, print or copy that file, and never put the secret on a command line, in this folder, in a commit or in any other file.`,
    '',
    '## Every call',
    '',
    `- Windows (PowerShell): curl.exe -fsS -K "${file.powershell}" ${server}/v1/...`,
    `- macOS/Linux: curl -fsS -K ${file.posix} ${server}/v1/...`,
    '',
    '## Steps',
    '',
    'At every run, in this order:',
    '',
    ...runSteps(server, file),
    `6. If a call returns 401, the secret file is missing or wrong: stop and ask your owner to check ${file.windows}.`,
    '7. Stop and ask your owner if anything is unclear.',
    '',
    `The whole order, for every kind of worker, is the kit's document "How work is found": GET ${server}/v1/docs/finding-work`,
    '',
  ].join('\n');
}

// What an agent does at every run, in its own words: the order of the kit's document "How work is
// found" (docs/FINDING_WORK.md) for an agent with a web credential. tests/test_finding_work.py pins
// this order to the order GET /v1/agents/me/next gives.
export const RUN_ORDER = ['guidance', 'own-tasks', 'ready-list', 'held', 'checkpoint'];
export function runSteps(server, file) {
  return [
    '1. The coordinator\'s standing guidance does not reach you over this API yet. If your owner gave you guidance for a project, follow it first.',
    `2. Your own tasks: GET ${server}/v1/agents/me/next, for example curl.exe -fsS -K "${file.powershell}" ${server}/v1/agents/me/next. The reply lists your own tasks first: review feedback to address, then a task you left blocked, then one in progress. Act on the first. Before you act, read the task brief it links to; it does not carry comments yet, so if you expect word from your coordinator on that task, read its history too (the same address ending in /history).`,
    '3. Only when none of your own tasks needs action: the tasks the same reply says you could claim. Claim exactly one that nobody holds, and work on that one until it is delivered.',
    '4. A task assigned to somebody else is held, and so is one somebody else claimed: leave it. A parent or coordination task is background for a task you picked, not an inbox: nothing is posted there for you.',
    '5. Record a checkpoint when you stop, with what you need to continue, and deliver work for review through the API, always with curl -K as above. If an answer is needed before you can proceed, record an open item of kind blocker, not only a question. Only blockers and dependencies mark undelivered work as blocked; delivered work follows its review state.',
  ];
}

// What the owner pastes into the agent's chat to resume it later.
export function resumePrompt(name) {
  return `You are ${plain(name)}, an Orchestra agent. Read ${GUIDE_PATH} in this folder and follow it: ask Orchestra for your next action and continue from there.`;
}

// What the owner pastes into the agent's chat once, in the agent's folder, to set it up.
//
// Beyond the two small files in .orchestra/, the prompt now writes what the VS Code bridge
// extension reads (kittrial-5bb.203): the workspace settings, the service's own certificate
// beside the credential file with orchestraBridge.caFile naming it, and the address and
// fingerprint as comment lines in the credential file (curl ignores comments; the extension
// reads the address from them, so nothing is typed). `role` is the extension's role for the
// window (worker or reviewer); `certificate` is the service's own PEM with its fingerprint, or
// null when the service has none (plain http, or the first-install tunnel).
export function setupPrompt(payload, { role = 'worker', certificate = null, bridgeServer = null,
  projects = null, caFile = null } = {}) {
  const pem = typeof certificate === 'string' ? certificate : (certificate && certificate.certificate) || null;
  const fingerprint = (certificate && certificate.sha256) || null;
  const file = payload.certificateFile || certificateFile(payload.name);
  const granted = Array.isArray(projects) ? projects : payload.projects;
  const address = bridgeServer || payload.server;
  const comments = credentialCommentLines(address, fingerprint);
  const settings = bridgeSettings(payload, { role, server: address, projects: granted,
    caFile: pem ? (caFile || file.posix) : null });
  const steps = [
    `Set up this folder for the Orchestra agent "${payload.name}".`,
    '',
    '1. Create the folder .orchestra here if it does not exist.',
    `2. Create ${CONFIG_PATH} with exactly this content:`,
    '~~~json',
    agentJson(payload).trimEnd(),
    '~~~',
    `3. Create ${GUIDE_PATH} with exactly this content:`,
    '~~~markdown',
    agentGuide(payload).trimEnd(),
    '~~~',
    `4. If this folder is a git repository and its .gitignore does not already list ${GITIGNORE_LINE}, add the line ${GITIGNORE_LINE} to .gitignore. Do not change any other file.`,
    `5. Your secret is not in this message. I keep it in ${payload.secretFile.windows} (on macOS/Linux ${payload.secretFile.posix}); you hand that file to curl with -K, as AGENT.md says. Never open, print or copy that file, and never write the secret to a file, a commit or a command line.`,
    `6. This folder is opened in VS Code: merge these settings into ${VSCODE_SETTINGS_PATH}, creating that file if it does not exist. Keep every setting already in it exactly as it is and change nothing else in the file; the secret never goes into any settings file. If the file exists and is not valid JSON, leave it exactly as it is and say so instead of guessing.`,
    '~~~json',
    JSON.stringify(settings, null, 2),
    '~~~',
  ];
  if (granted.length > 1) {
    steps.push(`   This agent is granted ${granted.length} projects (${granted.join(', ')}), and a window is woken only for the projects it names: put the project whose code is in THIS folder in place of REPLACE_PROJECT_ID. Use one folder and one VS Code window per project.`);
  } else if (!granted.length) {
    steps.push('   This agent has no project yet: grant it one on the Agents page and set orchestraBridge.projects in this window to it.');
  }
  if (pem) {
    steps.push(
      `7. This service presents its own certificate (https). Save it beside the credential file as ${file.windows} on Windows, ${file.posix} on macOS/Linux, with exactly this content:`,
      '~~~pem',
      pem.trimEnd(),
      '~~~',
      `   That is the same certificate the settings above name in orchestraBridge.caFile, and the fingerprint ${fingerprint || '(not shown)'} is what the extension shows when it asks whether to trust it: compare the two. It is the public certificate only, what the TLS handshake already gives; it holds no key and no secret.`);
  }
  steps.push(
    `${pem ? 8 : 7}. Add these comment lines at the end of the credential file (curl ignores comment lines, and the extension reads the address from the first one, so nobody types it). Append to the file without opening, printing or copying what it already holds:`,
    '',
    `   - PowerShell: Add-Content -LiteralPath "${payload.secretFile.powershell}" -Value ${comments.map((line) => `'${line}'`).join(',')}`,
    `   - macOS/Linux: printf '%s\\n' ${comments.map((line) => `'${line}'`).join(' ')} >> ${payload.secretFile.posix}`,
    '');
  steps.push(`Then continue: ${resumePrompt(payload.name)}`, '');
  return steps.join('\n');
}

// Full destination path of a setup file inside the recorded working directory:
// backslashes for a Windows-looking folder (drive letter or backslash), forward
// slashes otherwise; the relative path (Windows style) when none is recorded.
export function isWindowsPath(folder) {
  return /^[A-Za-z]:([\\/]|$)/.test(folder) || folder.includes('\\');
}

export function destinationPath(folder, relative) {
  const dir = String(folder || '').trim();
  if (!dir) return { path: relative.replace(/\//g, '\\'), relative: true };
  const windows = isWindowsPath(dir);
  const sep = windows ? '\\' : '/';
  const base = dir.replace(/[\\/]+$/, '');
  return { path: base + sep + (windows ? relative.replace(/\//g, '\\') : relative), relative: false };
}

// .gitignore: only ever an append of one line to a file that already exists.
export function listsOrchestra(text) {
  return String(text || '').split(/\r?\n/).some((line) => /^\/?\.orchestra\/?\s*$/.test(line.trim()));
}

export function gitignoreAppend(text) {
  if (listsOrchestra(text)) return '';
  const body = String(text || '');
  const eol = body.includes('\r\n') ? '\r\n' : '\n';
  return (body && !body.endsWith('\n') ? eol : '') + GITIGNORE_LINE + eol;
}
