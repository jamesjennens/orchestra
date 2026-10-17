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

  // Only lifecycle request bodies are stored here: no login, reset, grant or
  // credential request can reach this cache. Keys are saved BEFORE the write,
  // scoped to the authenticated actor, and survive a tab reload.
  let lifecycleActor = null;
  let lifecycleKeys = new Map();
  let lifecycleStorageFault = false;
  function lifecycleStorageKey() { return 'orchestra-lifecycle-retries-v1:' + encodeURIComponent(lifecycleActor); }
  function loadLifecycleActor(actor) {
    if (typeof actor !== 'string' || !actor || actor === lifecycleActor) return;
    lifecycleActor = actor; lifecycleKeys = new Map(); lifecycleStorageFault = false;
    try {
      const text = sessionStorage.getItem(lifecycleStorageKey());
      if (!text) return;
      if (text.length > 256000) { lifecycleStorageFault = true; return; }
      const entries = JSON.parse(text);
      if (!Array.isArray(entries) || entries.length > 50) { lifecycleStorageFault = true; return; }
      for (const entry of entries) {
        if (!Array.isArray(entry) || entry.length !== 2 || typeof entry[0] !== 'string'
            || entry[0].length > 200000 || !entry[0].startsWith('POST /v1/projects/')
            || !entry[0].includes('/lifecycle/') || !/^[A-Za-z0-9_-]{8,128}$/.test(entry[1])) { lifecycleStorageFault = true; return; }
      }
      lifecycleKeys = new Map(entries);
    } catch { lifecycleStorageFault = true; }
  }
  function saveLifecycleKeys() {
    const text = JSON.stringify([...lifecycleKeys]);
    if (lifecycleKeys.size > 50 || text.length > 256000)
      throw new ApiError(409, 'pending_lifecycle_limit', 'Reconcile saved lifecycle requests before starting another.', null, null);
    try { sessionStorage.setItem(lifecycleStorageKey(), text); }
    catch { throw new ApiError(409, 'retry_storage_unavailable', 'This browser cannot save the retry key. Restore tab storage before recording lifecycle facts.', null, null); }
  }
  async function readLifecycle(pid, tid) {
    const result = await call('GET', `/v1/projects/${encodeURIComponent(pid)}/tasks/${encodeURIComponent(tid)}/lifecycle`);
    loadLifecycleActor(result.current_actor);
    return result;
  }
  async function writeLifecycle(pid, tid, suffix, body) {
    if (!lifecycleActor) await readLifecycle(pid, tid);
    if (!lifecycleActor) throw new ApiError(409, 'lifecycle_actor_unknown', 'Read the current lifecycle state before recording a fact.', null, null);
    if (lifecycleStorageFault) throw new ApiError(409, 'retry_storage_unreadable', 'Saved lifecycle retry keys cannot be read. Reconcile the current facts before clearing tab storage.', null, null);
    const path = `/v1/projects/${encodeURIComponent(pid)}/tasks/${encodeURIComponent(tid)}/lifecycle/${suffix}`;
    function sorted(value) {
      if (Array.isArray(value)) return value.map(sorted);
      if (value && typeof value === 'object') return Object.fromEntries(Object.keys(value).sort().map(k => [k, sorted(value[k])]));
      return value;
    }
    const fixedBody = sorted(JSON.parse(JSON.stringify(body)));
    const intent = 'POST ' + path + ' ' + JSON.stringify(fixedBody);
    const key = lifecycleKeys.get(intent) || newKey();
    const existing = lifecycleKeys.has(intent);
    lifecycleKeys.set(intent, key);
    try { saveLifecycleKeys(); }
    catch (error) { if (!existing) lifecycleKeys.delete(intent); throw error; }
    try {
      const result = await call('POST', path, { body: fixedBody, key });
      lifecycleKeys.delete(intent);
      try { saveLifecycleKeys(); }
      catch {
        lifecycleKeys.set(intent, key);
        throw new ApiError(503, 'uncertain', 'The write completed but its retry key could not be cleared. Retry the identical request to reconcile it.', null, null);
      }
      return result;
    } catch (caught) {
      const error = caught instanceof ApiError ? caught : new ApiError(0, 'network',
        'The lifecycle response was lost. Retry the identical request to reconcile it.', null, null);
      if (error instanceof ApiError && error.uncertain) {
        error.retry = () => writeLifecycle(pid, tid, suffix, fixedBody);
      } else {
        lifecycleKeys.delete(intent);
        try { saveLifecycleKeys(); } catch { /* Keep the original native refusal sentence. */ }
      }
      throw error;
    }
  }
  function pendingLifecycle(pid, tid) {
    const prefix = `POST /v1/projects/${encodeURIComponent(pid)}/tasks/${encodeURIComponent(tid)}/lifecycle/`;
    const items = [];
    for (const [intent] of lifecycleKeys) {
      if (!intent.startsWith(prefix)) continue;
      const rest = intent.slice(prefix.length), space = rest.indexOf(' ');
      const suffix = rest.slice(0, space);
      if (space < 0 || !/^(scope|facts\/(implemented|tested|reviewed|integrated|deployed|live-verified))$/.test(suffix)) continue;
      try {
        const body = JSON.parse(rest.slice(space + 1));
        if (!body || typeof body !== 'object' || Array.isArray(body)) { lifecycleStorageFault = true; continue; }
        items.push({ operation: suffix, body, retry: () => writeLifecycle(pid, tid, suffix, body) });
      } catch { /* Damaged requests are never replayed by guessing their body. */ }
    }
    return items;
  }

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
    assignmentRoster: (pid, params) => call('GET', `/v1/projects/${encodeURIComponent(pid)}/assignment-roster`, { params }),
    assignTask: (pid, tid, body) => mutate('POST', `/v1/projects/${encodeURIComponent(pid)}/tasks/${encodeURIComponent(tid)}/assign`, body),
    async logout() { try { await call('DELETE', '/v1/sessions/current'); } finally {
      csrf = null;
      if (lifecycleActor) { try { sessionStorage.removeItem(lifecycleStorageKey()); } catch {} }
      lifecycleActor = null; lifecycleKeys = new Map();
    } },
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
    lifecycle: readLifecycle,
    pendingLifecycle,
    setLifecycleScope: (pid, tid, body) => writeLifecycle(pid, tid, 'scope', body),
    recordLifecycleFact: (pid, tid, dimension, body) => writeLifecycle(pid, tid, 'facts/' + encodeURIComponent(dimension), body),
    createProject: (name) => mutate('POST', '/v1/projects', { name }),
    registerProject: (projectId, name) => mutate('POST', '/v1/projects', name ? { project_id: projectId, name } : { project_id: projectId }),
    // Create the project on the server as well (an account a superuser granted that, or a superuser).
    createHostProject: (projectId, name) => mutate('POST', '/v1/projects', name ? { project_id: projectId, name, create: true } : { project_id: projectId, create: true }),
    // Superuser only: creations that stopped half way, and who may create projects.
    projectCreations: () => call('GET', '/v1/project-creations'),
    setProjectGrant: (uid, limit) => mutate('PUT', `/v1/accounts/${uid}/project-grant`, { limit }),
    clearProjectGrant: (uid) => mutate('DELETE', `/v1/accounts/${uid}/project-grant`),
    // Guidance reads never acknowledge; acknowledgements name the exact displayed version.
    guidance: (pid) => call('GET', `/v1/projects/${pid}/guidance`),
    guidanceStatus: (pid, offset = 0, limit = 50) => call('GET', `/v1/projects/${pid}/guidance/status`, { params: { offset, limit } }),
    ackGuidance: (pid, version) => mutate('POST', `/v1/projects/${pid}/guidance/ack`, { version }),
    setGuidance: (pid, text) => mutate('PUT', `/v1/projects/${pid}/guidance`, { text }),
    clearGuidance: (pid) => mutate('DELETE', `/v1/projects/${pid}/guidance`, {}),
    memberOnboarding: (pid) => call('GET', `/v1/projects/${pid}/onboarding/read`),
    // Coordinate edit routes withhold operator text; the member read returns the whole document.
    onboarding: (pid) => call('GET', `/v1/projects/${pid}/onboarding`),
    setOnboarding: (pid, text) => mutate('PUT', `/v1/projects/${pid}/onboarding`, { text }),
    clearOnboarding: (pid) => mutate('DELETE', `/v1/projects/${pid}/onboarding`),
    archiveProject: (pid) => mutate('POST', `/v1/projects/${pid}/archive`, {}),
    // The setup steps of a project (owners and superusers), and where its repository is.
    projectSetup: (pid) => call('GET', `/v1/projects/${pid}/setup`),
    mergeSlot: (pid) => call('GET', `/v1/projects/${pid}/merge-slot`),
    acquireMergeSlot: (pid, task_id, target) => mutate('POST', `/v1/projects/${pid}/merge-slot/acquire`, { task_id, target }),
    releaseMergeSlot: (pid) => mutate('POST', `/v1/projects/${pid}/merge-slot/release`, {}),
    forceReleaseMergeSlot: (pid, expected_holder, reason, expected_task) => mutate('POST', `/v1/projects/${pid}/merge-slot/force-release`,
      expected_task ? { expected_holder, reason, expected_task } : { expected_holder, reason }),
    createMergeSlot: (pid) => mutate('POST', `/v1/projects/${pid}/merge-slot/create`, {}),
    setRepository: (pid, repository) => mutate('PATCH', `/v1/projects/${pid}`, { repository }),
    // Superuser only: the records this server will not serve, to confirm or archive.
    unconfirmedProjects: () => call('GET', '/v1/projects/unconfirmed'),
    confirmProject: (pid) => mutate('POST', `/v1/projects/${pid}/confirm`, {}),
    members: (pid) => call('GET', `/v1/projects/${pid}/members`, { params: { limit: 100 } }),
    setMember: (pid, uid, role) => mutate('PUT', `/v1/projects/${pid}/members/${uid}`, { role }),
    // The agents that work in a project, as its owner sees them, and the coordinator grant (kittrial-5bb.209).
    projectAgents: (pid) => call('GET', `/v1/projects/${pid}/agents`),
    grantCoordinator: (pid, aid) => mutate('PUT', `/v1/projects/${pid}/agents/${aid}/coordinator`, {}),
    revokeCoordinator: (pid, aid) => mutate('DELETE', `/v1/projects/${pid}/agents/${aid}/coordinator`),
    // Who coordinates the project and the rules that bind them, for any member; read only (kittrial-5bb.198).
    coordination: (pid) => call('GET', `/v1/projects/${pid}/coordination`),
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
    comment: (pid, tid, text) => mutate('POST', `/v1/projects/${pid}/tasks/${tid}/comments`, { text }),
    checkpoint: (pid, tid, body) => mutate('POST', `/v1/projects/${pid}/tasks/${tid}/checkpoints`, body),
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
    extendAgentCredential: (aid, cid) => mutate('POST', `/v1/agents/${aid}/credentials/${cid}/extend`, {}),
    extendProjectAgentCredential: (pid, aid, cid) => mutate('POST', `/v1/projects/${pid}/agents/${aid}/credentials/${cid}/extend`, {}),
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
    // Many requirements in one action (slice 10). Per item on the server: a refused
    // item changes nothing and does not roll back the rest, and the answer says which.
    acceptRequirements: (pid, body) => mutate('POST', `/v1/projects/${pid}/requirements/accept-batch`, body),
    withdrawRequirement: (pid, rid, body) => mutate('POST', `/v1/projects/${pid}/requirements/${rid}/withdraw`, body),
    supersedeRequirement: (pid, rid, body) => mutate('POST', `/v1/projects/${pid}/requirements/${rid}/supersede`, body),
    finishRequirementTransition: (pid, rid, body) => mutate('POST', `/v1/projects/${pid}/requirements/${rid}/transition/finish`, body),
    abandonRequirementTransition: (pid, rid, body) => mutate('POST', `/v1/projects/${pid}/requirements/${rid}/transition/abandon`, body),
    requirementRecoveries: (pid) => call('GET', `/v1/projects/${pid}/requirements/recoveries`),
    clearRequirementCreation: (pid, body) => mutate('POST', `/v1/projects/${pid}/requirements/recoveries/clear`, body),
    setRequirementsGovernance: (pid, body) => mutate('PUT', `/v1/projects/${pid}/requirements/governance`, body),
    decisions: (pid) => call('GET', `/v1/projects/${pid}/decisions`),
    record: (pid, id) => call('GET', `/v1/projects/${pid}/records/${id}`),
    // reference and capability records (kittrial-5bb.213)
    references: (pid, params) => call('GET', `/v1/projects/${encodeURIComponent(pid)}/references`, { params }),
    findReferences: (pid, params) => call('GET', `/v1/projects/${encodeURIComponent(pid)}/references/find`, { params }),
    reference: (pid, key) => call('GET', `/v1/projects/${encodeURIComponent(pid)}/references/${encodeURIComponent(key)}`),
    proposeReference: (pid, body) => mutate('POST', `/v1/projects/${encodeURIComponent(pid)}/references`, body),
    reviseReference: (pid, key, body) => mutate('PATCH', `/v1/projects/${encodeURIComponent(pid)}/references/${encodeURIComponent(key)}`, body),
    acceptReference: (pid, key, body) => mutate('POST', `/v1/projects/${encodeURIComponent(pid)}/references/${encodeURIComponent(key)}/accept`, body),
    retireReference: (pid, key, body) => mutate('POST', `/v1/projects/${encodeURIComponent(pid)}/references/${encodeURIComponent(key)}/retire`, body),
    capabilities: (pid, params) => call('GET', `/v1/projects/${encodeURIComponent(pid)}/capabilities`, { params }),
    findCapabilities: (pid, params) => call('GET', `/v1/projects/${encodeURIComponent(pid)}/capabilities/find`, { params }),
    capability: (pid, key) => call('GET', `/v1/projects/${encodeURIComponent(pid)}/capabilities/${encodeURIComponent(key)}`),
    proposeCapability: (pid, body) => mutate('POST', `/v1/projects/${encodeURIComponent(pid)}/capabilities`, body),
    reviseCapability: (pid, key, body) => mutate('PATCH', `/v1/projects/${encodeURIComponent(pid)}/capabilities/${encodeURIComponent(key)}`, body),
    acceptCapability: (pid, key, body) => mutate('POST', `/v1/projects/${encodeURIComponent(pid)}/capabilities/${encodeURIComponent(key)}/accept`, body),
    retireCapability: (pid, key, body) => mutate('POST', `/v1/projects/${encodeURIComponent(pid)}/capabilities/${encodeURIComponent(key)}/retire`, body),
    verifyCapability: (pid, key, body) => mutate('POST', `/v1/projects/${encodeURIComponent(pid)}/capabilities/${encodeURIComponent(key)}/verify`, body),
    proposeCapabilityAlias: (pid, key, body) => mutate('POST', `/v1/projects/${encodeURIComponent(pid)}/capabilities/${encodeURIComponent(key)}/aliases`, body),
    rejectCapabilityAlias: (pid, key, body) => mutate('POST', `/v1/projects/${encodeURIComponent(pid)}/capabilities/${encodeURIComponent(key)}/aliases/reject`, body),
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
