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

  // A mutation keeps one idempotency key across retries of the same user intent.
  function mutation(method, path, body) {
    const key = newKey();
    const run = () => call(method, path, { body, key });
    return { key, run };
  }

  async function mutate(method, path, body) {
    const op = mutation(method, path, body);
    try {
      return await op.run();
    } catch (error) {
      if (error instanceof ApiError && error.uncertain) error.retry = op.run;
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
    archiveProject: (pid) => mutate('POST', `/v1/projects/${pid}/archive`, {}),
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
    agents: () => call('GET', '/v1/agents'),
    createAgent: (body) => mutate('POST', '/v1/agents', body),
    updateAgent: (aid, body) => mutate('PATCH', `/v1/agents/${aid}`, body),
    agent: (aid) => call('GET', `/v1/agents/${aid}`),
    // A NEW credential for the agent; its secret is in this one response only.
    issueAgentCredential: (aid) => mutate('POST', `/v1/agents/${aid}/credentials`, { label: 'web: new secret' }),
    revokeAgentCredential: (aid, cid) => mutate('POST', `/v1/agents/${aid}/credentials/${cid}/revoke`, {}),
    feedback: (pid, params) => call('GET', `/v1/projects/${pid}/feedback`, { params }),
    addFeedback: (pid, body) => mutate('POST', `/v1/projects/${pid}/feedback`, body),
    requirements: (pid) => call('GET', `/v1/projects/${pid}/requirements`),
    requirement: (pid, rid) => call('GET', `/v1/projects/${pid}/requirements/${rid}`),
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
