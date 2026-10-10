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
//: The extension's two roles: one window is one role. The prompt names the role so the person
//: chooses the same in the extension's own "Orchestra Bridge: Set up"; the kit itself writes no
//: workspace settings (kittrial-5bb.203 review 01a12408, item 2).
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

// Where the service's own certificate is saved for the person who clicks the dialog's Save button:
// beside the credential file in the user profile, never in the agent's folder or the repository.
// The kit does not name it in any setting any more (kittrial-5bb.203 review 01a12408, item 2): the
// extension shows the server's fingerprint on first contact and asks the person to trust it.
export function certificateFile(name) {
  const file = `.orchestra-agent-${slug(name)}.crt`;
  return Object.freeze({
    name: file,
    windows: `%USERPROFILE%\\${file}`,
    powershell: `$env:USERPROFILE\\${file}`,
    posix: `~/${file}`,
  });
}

// The comment lines the extension and a careful person read out of the credential file. curl
// ignores comment lines, so they cost nothing there. The extension's own rule for the address is
// `^#\s*server\s*=\s*(https?://\S+)\s*$`, so the address stands alone on its line and the
// fingerprint gets a line of its own.
export function credentialCommentLines(server, fingerprint) {
  const lines = [`# server = ${server}`];
  if (fingerprint) lines.push(`# server certificate sha256 = ${fingerprint}`);
  return lines;
}

// The command that makes the credential file's content exactly those comment lines followed by
// whatever it already held (its one `header = "Authorization: Bearer ..."` line). It is a rewrite,
// not an append: any earlier comment line is removed first, so a second set-up replaces it instead
// of adding a second copy, and each comment ends with a newline, so the dialog's Notepad path --
// which saves no final newline -- cannot glue a comment onto the header line (the extension's rule
// then matches nothing; kittrial-5bb.203 review 01a12408, item 3a). The POSIX form writes a
// same-directory temporary file, restricts it to its owner, and moves it over the original, so the
// secret is never printed and never reaches a command line or a process argument list.
const CREDENTIAL_COMMENT_PATTERN = "'^#[[:space:]]*server([[:space:]]+certificate[[:space:]]+sha256)?[[:space:]]*='";
const POWERSHELL_COMMENT_PATTERN = "'(?m)^#[ \\t]*server([ \\t]+certificate[ \\t]+sha256)?[ \\t]*=[^\\r\\n]*\\r?\\n?'";
export function credentialFileCommand(file, comments) {
  const quoted = comments.map((line) => `'${line}'`).join(' ');
  const powershellText = comments.map((line) => line.replace(/`/g, '``').replace(/\$/g, '`$').replace(/"/g, '`"'))
    .join('`r`n') + '`r`n';
  const powershell = '$p="' + file.powershell + '"; $c=[IO.File]::ReadAllText($p); '
    + '$c=[Text.RegularExpressions.Regex]::Replace($c,' + POWERSHELL_COMMENT_PATTERN + ",''); "
    + '[IO.File]::WriteAllText($p,"' + powershellText + '" + $c)';
  const posix = `printf '%s\\n' ${quoted} > ${file.posix}.new && `
    + `grep -vE ${CREDENTIAL_COMMENT_PATTERN} ${file.posix} >> ${file.posix}.new && `
    + `chmod 600 ${file.posix}.new && mv ${file.posix}.new ${file.posix} || rm -f ${file.posix}.new`;
  return Object.freeze({ powershell, posix });
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
// Beyond the two small files in .orchestra/, the prompt makes the credential file's content what
// the VS Code bridge extension reads (kittrial-5bb.203): the `# server = ADDRESS` line (and the
// fingerprint line when the service has its own certificate) become the file's first lines, each
// on its own line, replacing any earlier copy. It writes NO workspace settings: since extension
// 0.9.0 the extension's own "Orchestra Bridge: Set up" command writes them from that address line
// and shows the server certificate's fingerprint for the person to compare, so no certificate
// file and no bridge setting naming one is part of the set-up at all (coordinator decision on
// review 01a12408, item 2). `role` names what this window does (the extension's worker or
// reviewer) so the person picks the same there; `certificate` is the route's answer
// (GET /v1/service/certificate) or null when the service has none (plain http, the tunnel).
export function setupPrompt(payload, { role = 'worker', certificate = null, bridgeServer = null } = {}) {
  const fingerprint = (certificate && certificate.sha256) || null;
  const address = bridgeServer || payload.server;
  const comments = credentialCommentLines(address, fingerprint);
  const command = credentialFileCommand(payload.secretFile, comments);
  const roleWord = BRIDGE_ROLES.includes(role) ? role : 'worker';
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
    `6. Make the credential file ${payload.secretFile.windows} (macOS/Linux ${payload.secretFile.posix}) hold these comment lines as its FIRST lines, each on its own line ending with a newline, followed by whatever it already held (its header line):`,
    '~~~',
    ...comments,
    '~~~',
    '   Run exactly one of these commands. Each one rewrites the file in place -- it never prints or copies what the file holds -- and removes any earlier copy of these comment lines first, so running it a second time leaves exactly the same content:',
    `   - PowerShell: ${command.powershell}`,
    `   - macOS/Linux: ${command.posix}`,
    '   The first line is the address the extension reads, so nobody types it. If the credential file does not exist yet, stop here and ask me for step 1 of the dialog first.',
    `7. This window's role is ${roleWord}: when you run "Orchestra Bridge: Set up" in VS Code, choose ${roleWord} (the extension's own set-up writes the workspace settings from the address line above; the kit writes no settings file).`,
    `Then continue: ${resumePrompt(payload.name)}`,
    '',
  ];
  if (fingerprint) {
    steps.push(`The service's own certificate fingerprint is ${fingerprint}. The extension shows it when it first reaches this server; compare it with the value the operator reads on the server itself, as the dialog says.`, '');
  }
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
