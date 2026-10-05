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
    `1. Ask for your next action: GET ${server}/v1/agents/me/next, for example curl.exe -fsS -K "${file.powershell}" ${server}/v1/agents/me/next`,
    '2. The reply says what to do next: feedback to address, work to continue, or tasks you could claim. Before coding, read the task brief it links to.',
    '3. Record a checkpoint when you stop, and deliver work for review through the API, always with curl -K as above. If an answer is needed before you can proceed, record an open item of kind blocker, not only a question. Only blockers and dependencies mark undelivered work as blocked; delivered work follows its review state.',
    `4. If a call returns 401, the secret file is missing or wrong: stop and ask your owner to check ${file.windows}.`,
    '5. Stop and ask your owner if anything is unclear.',
    '',
  ].join('\n');
}

// What the owner pastes into the agent's chat to resume it later.
export function resumePrompt(name) {
  return `You are ${plain(name)}, an Orchestra agent. Read ${GUIDE_PATH} in this folder and follow it: ask Orchestra for your next action and continue from there.`;
}

// What the owner pastes into the agent's chat once, in the agent's folder, to set it up.
export function setupPrompt(payload) {
  return [
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
    '',
    `Then continue: ${resumePrompt(payload.name)}`,
    '',
  ].join('\n');
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
