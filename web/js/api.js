// HTTP adapter for the Orchestra service (docs/HTTP_TRANSPORT_DESIGN.md).
// Browser rules: the session lives in an HttpOnly SameSite=Strict cookie the page never
// reads; the CSRF token is kept in memory only; every mutation carries an
// Idempotency-Key, and an uncertain outcome is retried with the SAME key so the server
// can replay the committed result instead of repeating the write.

export class ApiError extends Error {
  constructor(status, code, message, detail, requestId) {
    super(message || code || 'Request failed');
    this.status = status;
    this.code = code;
    this.detail = detail;
    this.requestId = requestId;
  }
  get uncertain() { return this.status === 0 || this.status >= 500; }
}

function newKey() {
  if (globalThis.crypto && crypto.randomUUID) return 'web-' + crypto.randomUUID();
  return 'web-' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 12);
}

function query(params) {
  const entries = Object.entries(params || {}).filter(([, v]) => v !== undefined && v !== null && v !== '');
  return entries.length ? '?' + new URLSearchParams(entries).toString() : '';
}

export function createApi(transport) {
  let csrf = null;

  async function call(method, path, { body, params, key } = {}) {
    const headers = { Accept: 'application/json' };
    const mutating = method !== 'GET';
    if (body !== undefined) headers['Content-Type'] = 'application/json';
    if (mutating && csrf) headers['X-CSRF-Token'] = csrf;
    if (mutating && key) headers['Idempotency-Key'] = key;
    const response = await transport(method, path + query(params), headers, body === undefined ? undefined : JSON.stringify(body));
    if (response.status === 204) return null;
    const data = response.data;
    if (response.status >= 200 && response.status < 300) return data;
    const error = (data && data.error) || {};
    throw new ApiError(response.status, error.code, error.message, error.detail, data && data.request_id);
  }

  // A mutation keeps one idempotency key across retries of the same user intent. The
  // intent is the request itself (method, path and body): while its outcome is unknown
  // (no answer, or a 5xx), sending the same request again reuses the key, so pressing
  // the same button twice replays the first write instead of repeating it. A definite
  // answer, or different content, ends the intent and the next send gets a new key.
  const uncertainKeys = new Map();
  const UNCERTAIN_KEYS_MAX = 50;

  async function mutate(method, path, body) {
    const intent = method + ' ' + path + ' ' + JSON.stringify(body === undefined ? null : body);
    const key = uncertainKeys.get(intent) || newKey();
    try {
      const result = await call(method, path, { body, key });
      uncertainKeys.delete(intent);
      return result;
    } catch (error) {
      if (error instanceof ApiError && error.uncertain) {
        uncertainKeys.delete(intent);
        uncertainKeys.set(intent, key);
        if (uncertainKeys.size > UNCERTAIN_KEYS_MAX) uncertainKeys.delete(uncertainKeys.keys().next().value);
        error.retry = () => mutate(method, path, body);
      } else {
        uncertainKeys.delete(intent);
      }
      throw error;
    }
  }

  return {
    // session
    async login(username, password) {
      const result = await call('POST', '/v1/sessions', { body: { username, password } });
      csrf = result.session && result.session.csrf_token;
      // The response also carries the bearer token for non-browser clients; the page
      // deliberately does not keep it — the HttpOnly cookie is the browser session.
      return result.user;
    },
    async current() {
      const result = await call('GET', '/v1/sessions/current');
      if (result && result.csrf_token) csrf = result.csrf_token;
      return result;
    },
    hasCsrf: () => Boolean(csrf),
    async logout() { try { await call('DELETE', '/v1/sessions/current'); } finally { csrf = null; } },
    redeemReset: (uid, reset_value, new_password) => call('POST', `/v1/accounts/${uid}/reset/redeem`, { body: { reset_value, new_password }, key: newKey() }),
    changePassword: (uid, current_password, new_password) => mutate('POST', `/v1/accounts/${uid}/password`, { current_password, new_password }),

    // accounts (superuser)
    accounts: () => call('GET', '/v1/accounts'),
    createAccount: (username, display_name) => mutate('POST', '/v1/accounts', { username, display_name }),
    issueReset: (uid) => mutate('POST', `/v1/accounts/${uid}/reset`, {}),
    disableAccount: (uid) => mutate('POST', `/v1/accounts/${uid}/disable`, {}),
    // Exact username lookup, scoped to the project being administered (owners only).
    lookup: (username, project) => call('GET', '/v1/accounts/lookup', { params: { username, project } }),

    // projects
    projects: () => call('GET', '/v1/projects'),
    project: (pid) => call('GET', `/v1/projects/${pid}`),
    createProject: (name) => mutate('POST', '/v1/projects', { name }),
    registerProject: (projectId, name) => mutate('POST', '/v1/projects', name ? { project_id: projectId, name } : { project_id: projectId }),
    // Create the project on the server as well (an account a superuser granted that, or a superuser).
    createHostProject: (projectId, name) => mutate('POST', '/v1/projects', name ? { project_id: projectId, name, create: true } : { project_id: projectId, create: true }),
    // Superuser only: creations that stopped half way, and who may create projects.
    projectCreations: () => call('GET', '/v1/project-creations'),
    setProjectGrant: (uid, limit) => mutate('PUT', `/v1/accounts/${uid}/project-grant`, { limit }),
    clearProjectGrant: (uid) => mutate('DELETE', `/v1/accounts/${uid}/project-grant`),
    // The project's onboarding text, set by an owner (guidance stays with the operator).
    onboarding: (pid) => call('GET', `/v1/projects/${pid}/onboarding`),
    setOnboarding: (pid, text) => mutate('PUT', `/v1/projects/${pid}/onboarding`, { text }),
    clearOnboarding: (pid) => mutate('DELETE', `/v1/projects/${pid}/onboarding`),
    archiveProject: (pid) => mutate('POST', `/v1/projects/${pid}/archive`, {}),
    // The setup steps of a project (owners and superusers), and where its repository is.
    projectSetup: (pid) => call('GET', `/v1/projects/${pid}/setup`),
    setRepository: (pid, repository) => mutate('PATCH', `/v1/projects/${pid}`, { repository }),
    // Superuser only: the records this server will not serve, to confirm or archive.
    unconfirmedProjects: () => call('GET', '/v1/projects/unconfirmed'),
    confirmProject: (pid) => mutate('POST', `/v1/projects/${pid}/confirm`, {}),
    members: (pid) => call('GET', `/v1/projects/${pid}/members`, { params: { limit: 100 } }),
    setMember: (pid, uid, role) => mutate('PUT', `/v1/projects/${pid}/members/${uid}`, { role }),
    removeMember: (pid, uid) => mutate('DELETE', `/v1/projects/${pid}/members/${uid}`),
    credentials: (pid) => call('GET', `/v1/projects/${pid}/worker-credentials`, { params: { limit: 100 } }),
    issueCredential: (pid, scopes, label) => mutate('POST', `/v1/projects/${pid}/worker-credentials`, { scopes, label }),
    revokeCredential: (pid, cid) => mutate('POST', `/v1/projects/${pid}/worker-credentials/${cid}/revoke`, {}),

    // work
    tasks: (pid, params) => call('GET', `/v1/projects/${pid}/tasks`, { params }),
    task: (pid, tid) => call('GET', `/v1/projects/${pid}/tasks/${tid}`),
    brief: (pid, tid) => call('GET', `/v1/projects/${pid}/tasks/${tid}/brief`),
    history: (pid, tid, params) => call('GET', `/v1/projects/${pid}/tasks/${tid}/history`, { params }),
    createTask: (pid, body) => mutate('POST', `/v1/projects/${pid}/tasks`, body),
    updateTask: (pid, tid, body) => mutate('PATCH', `/v1/projects/${pid}/tasks/${tid}`, body),
    claim: (pid, tid) => mutate('POST', `/v1/projects/${pid}/tasks/${tid}/claim`, {}),
    review: (pid, tid, body) => mutate('POST', `/v1/projects/${pid}/tasks/${tid}/reviews`, body),
    queue: (pid, params) => call('GET', `/v1/projects/${pid}/queue`, { params }),
    myWork: () => call('GET', '/v1/me/work'),
    agents: (params) => call('GET', '/v1/agents', { params }),
    // A document of the installed kit, as the client's `docs NAME` serves it (kittrial-5bb.226).
    docs: () => call('GET', '/v1/docs'),
    doc: (name) => call('GET', `/v1/docs/${name}`),
    createAgent: (body) => mutate('POST', '/v1/agents', body),
    // The service's own public certificate, for the My agents set-up dialog: a read with no log-in
    // that only an https service with its own certificate answers (kittrial-5bb.203).
    certificate: () => call('GET', '/v1/service/certificate'),
    updateAgent: (aid, body) => mutate('PATCH', `/v1/agents/${aid}`, body),
    agent: (aid) => call('GET', `/v1/agents/${aid}`),
    // A NEW credential for the agent; its secret is in this one response only.
    // With no list the new credential carries what the agent has; a list sets what it has from now on
    // (kittrial-5bb.208). The page sends a list only from "What it may do" on the agent's card.
    issueAgentCredential: (aid, scopes) => mutate('POST', `/v1/agents/${aid}/credentials`, scopes ? { label: 'web: new secret', scopes } : { label: 'web: new secret' }),
    revokeAgentCredential: (aid, cid) => mutate('POST', `/v1/agents/${aid}/credentials/${cid}/revoke`, {}),
    // requirement proposals (identity is the session's; the submitter is never sent)
    proposals: (pid, params) => call('GET', `/v1/projects/${pid}/proposals`, { params }),
    proposal: (pid, key) => call('GET', `/v1/projects/${pid}/proposals/${key}`, { params: { history: 50 } }),
    submitProposal: (pid, body) => mutate('POST', `/v1/projects/${pid}/proposals`, body),
    disposeProposal: (pid, key, body) => mutate('POST', `/v1/projects/${pid}/proposals/${key}/dispositions`, body),
    myContributions: (params) => call('GET', '/v1/me/contributions', { params }),
    feedback: (pid, params) => call('GET', `/v1/projects/${pid}/feedback`, { params }),
    addFeedback: (pid, body) => mutate('POST', `/v1/projects/${pid}/feedback`, body),
    requirements: (pid) => call('GET', `/v1/projects/${pid}/requirements`),
    requirement: (pid, rid) => call('GET', `/v1/projects/${pid}/requirements/${rid}`),
    brd: (pid) => call('GET', `/v1/projects/${pid}/brd`),
    createRequirement: (pid, body) => mutate('POST', `/v1/projects/${pid}/requirements`, body),
    reviseRequirement: (pid, rid, body) => mutate('PATCH', `/v1/projects/${pid}/requirements/${rid}`, body),
    acceptRequirement: (pid, rid, body) => mutate('POST', `/v1/projects/${pid}/requirements/${rid}/accept`, body),
    setRequirementsGovernance: (pid, body) => mutate('PUT', `/v1/projects/${pid}/requirements/governance`, body),
    decisions: (pid) => call('GET', `/v1/projects/${pid}/decisions`),
    record: (pid, id) => call('GET', `/v1/projects/${pid}/records/${id}`),
    audit: (pid, params) => call('GET', `/v1/projects/${pid}/audit`, { params }),
  };
}

// Real transport: same-origin fetch; cookies are sent automatically and never read.
export async function fetchTransport(method, path, headers, body) {
  let response;
  try {
    response = await fetch(path, { method, headers, body, credentials: 'same-origin', cache: 'no-store' });
  } catch (error) {
    // Network failure: the write may or may not have happened.
    return { status: 0, data: { error: { code: 'network', message: 'The server could not be reached. Your change may not have been saved; retry to check.' } } };
  }
  let data = null;
  const text = await response.text();
  if (text) { try { data = JSON.parse(text); } catch { data = null; } }
  return { status: response.status, data };
}
