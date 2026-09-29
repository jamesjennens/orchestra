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

// The create response minus anything secret. Works for the real service
// ({agent, setup, ...}) and the prototype's mock ({agent, server, owner_name, ...}).
export function secretlessPayload(created, fallbackServer) {
  const agent = created.agent || {};
  const setup = created.setup || {};
  const config = setup.config || {};
  const projects = (config.projects || agent.projects || []).map((p) => (typeof p === 'string' ? p : p.id));
  return Object.freeze({
    agentId: config.agent_id || agent.id,
    name: plain(config.name || agent.name || agent.display_name),
    owner: agent.owner_display_name || created.owner_name || 'your owner',
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
  return [
    `# ${payload.name}`,
    '',
    `You are ${payload.name}, an Orchestra agent owned by ${payload.owner}. Your settings are in ${CONFIG_PATH} (server_url, agent_id, projects). That file holds no secret.`,
    '',
    `1. Your secret is kept by your owner in VS Code secret storage or the operating system's credential store. Read it from there when you call Orchestra. Never write it to a file, a commit or a command line. (Only on a machine with no credential store: the ${payload.secretEnv} environment variable, set from that store.)`,
    `2. Ask for your next action: GET ${payload.server}/v1/agents/me/next with the header "Authorization: Bearer <your secret>". From a shell, let curl read that header from a config file only you can read (curl -K ~/.orchestra-agent-curlrc), never from the command line.`,
    '3. The reply says what to do next: feedback to address, work to continue, or tasks you could claim. Before coding, read the task brief it links to.',
    '4. Record a checkpoint when you stop, and deliver work for review through the API.',
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
    '5. Your secret is not in this message and must never be written to a file, a commit or a command line. I keep it in my credential store (VS Code secret storage or the operating system credential store) and provide it the way AGENT.md describes.',
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
