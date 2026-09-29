// In-memory stand-in for the Orchestra HTTP service, used by the clickable prototype
// and browser tests. It mirrors the server's rules — membership 404s, viewer 403s,
// version 409s, idempotency replay — so the UI is exercised against the same
// behaviour it will meet in production. All names and content are synthetic.

const H = 3600e3;
const at = (hoursAgo) => new Date(Date.now() - hoursAgo * H).toISOString();
const sha = (seed) => {
  let x = 0; for (const c of seed) x = (x * 31 + c.charCodeAt(0)) >>> 0;
  let out = ''; while (out.length < 40) { x = (x * 1103515245 + 12345) >>> 0; out += x.toString(16).padStart(8, '0'); }
  return out.slice(0, 40);
};
const FACTS = ['implemented', 'tested', 'reviewed', 'integrated', 'deployed', 'live-verified'];
const facts = (spec = {}) => Object.fromEntries(FACTS.map((f) => [f, spec[f] || { value: 'unknown', note: null }]));
const yes = (note) => ({ value: 'yes', note });
const no = (note) => ({ value: 'no', note });

export function seed() {
  const users = {
    usr_morgan: { id: 'usr_morgan', username: 'morgan', display_name: 'Morgan Ellis', superuser: true, disabled: false, created_at: at(900) },
    usr_priya: { id: 'usr_priya', username: 'priya', display_name: 'Priya Raman', superuser: false, disabled: false, created_at: at(700) },
    usr_tomasz: { id: 'usr_tomasz', username: 'tomasz', display_name: 'Tomasz Nowak', superuser: false, disabled: false, created_at: at(650) },
    usr_kestrel: { id: 'usr_kestrel', username: 'kestrel', display_name: 'Kestrel (agent)', superuser: false, disabled: false, created_at: at(400),
      agent_of: 'usr_tomasz', tool: 'GitHub Copilot in VS Code', working_directory: 'C:\\Users\\tomasz\\agents\\portal-kestrel', last_seen_at: at(3) },
    usr_wren: { id: 'usr_wren', username: 'wren', display_name: 'Wren (agent)', superuser: false, disabled: false, created_at: at(380),
      agent_of: 'usr_priya', tool: 'GitHub Copilot in VS Code', working_directory: 'D:\\work\\agents\\invoice-wren', last_seen_at: at(5) },
    usr_lena: { id: 'usr_lena', username: 'lena', display_name: 'Lena Fischer', superuser: false, disabled: false, created_at: at(300) },
    usr_sam: { id: 'usr_sam', username: 'sam', display_name: 'Sam Okafor', superuser: false, disabled: true, created_at: at(800) },
  };
  const projects = {
    proj_portal: { id: 'proj_portal', name: 'Customer portal', created_by: 'usr_priya', created_at: at(600), archived: false, description: 'Self-service portal for orders, invoices and account settings.' },
    proj_invoice: { id: 'proj_invoice', name: 'Invoice export', created_by: 'usr_tomasz', created_at: at(420), archived: false, description: 'Nightly export of approved invoices to the accounting system.' },
    proj_handbook: { id: 'proj_handbook', name: 'Field handbook 2025', created_by: 'usr_priya', created_at: at(2400), archived: true, description: 'Last year’s field procedures. Archived; records kept.' },
  };
  const memberships = {
    proj_portal: { usr_priya: 'owner', usr_tomasz: 'contributor', usr_kestrel: 'contributor', usr_lena: 'viewer' },
    proj_invoice: { usr_tomasz: 'owner', usr_wren: 'contributor', usr_priya: 'contributor' },
    proj_handbook: { usr_priya: 'owner', usr_lena: 'contributor' },
  };
  const T = [];
  const task = (project_id, n, t) => T.push(Object.assign({
    id: `task_${project_id.slice(5, 9)}${String(n).padStart(3, '0')}`, project_id, description: '', status: 'open', assignee: null,
    version: 1, created_by: 'usr_priya', created_at: at(200), updated_at: at(100), priority: 2, review_state: 'none',
    contribution: null, requests: [], lifecycle: facts(), checkpoint: null, depends_on: [], attachments: [],
  }, t));

  task('proj_portal', 1, {
    title: 'Paginate order history', priority: 1, status: 'in_progress', assignee: 'usr_kestrel', created_at: at(96), updated_at: at(3), version: 4,
    description: 'Order history loads every order at once and times out for customers with more than ~2,000 orders. Page it (50 per page) with a stable cursor, keep the current sort, and keep the CSV export complete.',
    review_state: 'awaiting-review',
    contribution: { revision: 2, commit: sha('p1r2'), base_commit: sha('main-a'), summary: 'Cursor pagination on /orders with created_at+id keys; export still streams all rows. Revision 2 fixes the duplicate row at page boundaries and adds the empty-page case.', author: 'usr_kestrel', at: at(3), branch: 'contrib/portal-001-order-pages' },
    requests: [
      { id: 'rq1', text: 'Rows repeat at page boundaries when two orders share a timestamp.', status: 'resolved', by: 'usr_priya', resolution: 'Tie-break on order id; test added for equal timestamps.' },
      { id: 'rq2', text: 'Empty last page shows “Loading…” forever.', status: 'resolved', by: 'usr_priya', resolution: 'Explicit end-of-list state; test added.' },
    ],
    lifecycle: facts({ implemented: yes('Revision 2, 9 files'), tested: yes('412 passed, 0 failed (worker run)'), reviewed: no('Revision 2 awaiting review') }),
    checkpoint: { at: at(3), author: 'usr_kestrel', summary: 'Revision 2 delivered for review. Both requested changes addressed with tests.', next_action: 'Owner: review revision 2.', open_items: [{ id: 'export-memory', kind: 'question', text: 'CSV export of 50k rows peaks at 180 MB. Acceptable, or stream in chunks?' }] },
  });
  task('proj_portal', 2, {
    title: 'Password reset email links to expired template', priority: 1, status: 'in_progress', assignee: 'usr_tomasz', created_at: at(70), updated_at: at(20), version: 3,
    description: 'Reset emails still render the 2024 template and its link lands on a 404 page.',
    review_state: 'changes-requested',
    contribution: { revision: 1, commit: sha('p2r1'), base_commit: sha('main-a'), summary: 'Switched to the new template and link builder.', author: 'usr_tomasz', at: at(30), branch: 'contrib/portal-002-reset-email' },
    requests: [
      { id: 'rq3', text: 'Link host is hard-coded to staging; read it from configuration.', status: 'open', by: 'usr_priya' },
      { id: 'rq4', text: 'Add a test that fails if the template file is missing.', status: 'open', by: 'usr_priya' },
    ],
    lifecycle: facts({ implemented: yes('Revision 1'), tested: yes('Unit tests only'), reviewed: no('Changes requested') }),
    checkpoint: { at: at(20), author: 'usr_tomasz', summary: 'Reading the two requests; config key exists already.', next_action: 'Contributor: deliver revision 2.', open_items: [] },
  });
  task('proj_portal', 3, {
    title: 'Rate-limit sign-in attempts', priority: 1, status: 'in_progress', assignee: 'usr_priya', created_at: at(50), updated_at: at(8), version: 5,
    description: 'Throttle repeated failed sign-ins per account and per address; show a clear “try again in N minutes” message.',
    review_state: 'approved',
    contribution: { revision: 3, commit: sha('p3r3'), base_commit: sha('main-a'), summary: 'Sliding-window limiter, 5 attempts / 15 min, per account and per address.', author: 'usr_kestrel', at: at(12), branch: 'contrib/portal-003-throttle' },
    lifecycle: facts({ implemented: yes('Revision 3'), tested: yes('Full suite, 2 environments'), reviewed: yes('Approved by Priya Raman'), integrated: no('Waiting for merge slot') }),
    checkpoint: { at: at(8), author: 'usr_priya', summary: 'Approved. Integration held until the session-store migration lands.', next_action: 'Owner: integrate after “Migrate session store”.', open_items: [{ id: 'hold', kind: 'dependency', text: 'Integration hold: depends on the session store migration.' }] },
    depends_on: ['task_port004'],
  });
  task('proj_portal', 4, { title: 'Migrate session store to the shared database', priority: 2, status: 'open', created_at: at(48), updated_at: at(48), description: 'Sessions live in process memory; move them to the shared database so restarts do not sign everyone out.' });
  task('proj_portal', 5, { title: 'Accessibility pass on checkout', priority: 2, status: 'open', created_at: at(40), updated_at: at(40), description: 'Keyboard order, focus visibility and error announcements on the three checkout steps.' });
  task('proj_portal', 6, {
    title: 'Show invoice PDFs inline', priority: 3, status: 'closed', assignee: 'usr_kestrel', created_at: at(300), updated_at: at(140), version: 7,
    review_state: 'integrated',
    contribution: { revision: 1, commit: sha('p6r1'), base_commit: sha('main-0'), summary: 'Inline viewer with download fallback.', author: 'usr_kestrel', at: at(180), branch: 'contrib/portal-006-pdf' },
    lifecycle: facts({ implemented: yes(), tested: yes(), reviewed: yes('Approved'), integrated: yes('main ' + sha('main-a').slice(0, 9)), deployed: yes('Release 2.14'), 'live-verified': yes('Checked on production by Priya') }),
  });
  task('proj_portal', 7, { title: 'Address book: duplicate entries after import', priority: 2, status: 'open', created_at: at(30), updated_at: at(30), created_by: 'usr_lena' });
  task('proj_portal', 8, {
    title: 'Order search by reference', priority: 2, status: 'in_progress', assignee: 'usr_kestrel', created_at: at(26), updated_at: at(26), version: 2,
    description: 'Search orders by customer reference and PO number.',
    review_state: 'awaiting-review',
    contribution: { revision: 1, commit: sha('p8r1'), base_commit: sha('main-a'), summary: 'Prefix search on reference and PO number with an index.', author: 'usr_kestrel', at: at(26), branch: 'contrib/portal-008-search' },
    lifecycle: facts({ implemented: yes('Revision 1'), tested: yes('Worker run'), reviewed: no('Awaiting review since yesterday') }),
    checkpoint: { at: at(26), author: 'usr_kestrel', summary: 'Delivered for review.', next_action: 'Owner: review revision 1.', open_items: [] },
  });
  for (let n = 9; n <= 16; n += 1) {
    const titles = ['Translate account settings into German', 'Remove legacy fax field', 'Cookie banner wording', 'Retry failed card payments once', 'Sort invoices by due date', 'Clarify VAT number validation', 'Mobile layout for order detail', 'Printable delivery note'];
    task('proj_portal', n, { title: titles[n - 9], priority: 3, created_at: at(20 + n * 9), updated_at: at(20 + n * 9) });
  }

  task('proj_invoice', 1, {
    title: 'Skip invoices already exported', priority: 1, status: 'in_progress', assignee: 'usr_wren', created_at: at(60), updated_at: at(5), version: 3,
    review_state: 'awaiting-review', created_by: 'usr_tomasz',
    contribution: { revision: 1, commit: sha('i1r1'), base_commit: sha('main-i'), summary: 'Export ledger keyed by invoice number and checksum; re-runs skip unchanged invoices.', author: 'usr_wren', at: at(5), branch: 'contrib/invoice-001-dedupe' },
    lifecycle: facts({ implemented: yes('Revision 1'), tested: yes('Worker run'), reviewed: no() }),
    checkpoint: { at: at(5), author: 'usr_wren', summary: 'Delivered.', next_action: 'Owner: review revision 1.', open_items: [] },
  });
  task('proj_invoice', 2, { title: 'Email a summary after each nightly run', priority: 2, created_by: 'usr_tomasz', created_at: at(44), updated_at: at(44) });
  task('proj_handbook', 1, { title: 'Winter procedures chapter', status: 'closed', review_state: 'integrated', created_at: at(2300), updated_at: at(2000) });


  const reqRecords = {
    'cp-brd': { id: 'cp-brd', title: 'Customer portal requirements', status: 'open', type: 'epic', created_at: at(620), description: 'Requirements for the self-service customer portal, owned by Priya Raman.', comments: [] },
    'cp-brd.5': { id: 'cp-brd.5', title: 'Decision: customers see orders from all their sites', status: 'open', type: 'decision', created_at: at(560), comments: [],
      description: '## Decision\nA customer account sees orders placed by every site linked to it, not only the site that signed in. Authority: Priya Raman, 2025-11-04, after the pilot with a multi-site customer.\n\n## Rationale\nPilot customers kept phoning to ask about orders placed by a sister site. Site-only views caused most support calls.\n\n## Alternatives\nPer-site accounts only; an opt-in group view. Rejected: both add setup work for the customer.\n\n## Consequences\nPaged order history (cp-brd.2) must handle large multi-site accounts. Export must include the site name.' },
    'cp-brd.6': { id: 'cp-brd.6', title: 'Decision: no card details stored in the portal', status: 'closed', type: 'decision', created_at: at(540), comments: [],
      description: '## Decision\nThe portal never stores card numbers; payments go through the payment provider\u2019s hosted page. Authority: Morgan Ellis, 2025-11-20.\n\n## Rationale\nKeeps the portal out of card-data compliance scope.\n\n## Alternatives\nTokenised cards stored by us: rejected, compliance cost.\n\n## Consequences\nRetry of failed payments (cp-brd.4) must re-open the hosted page rather than charge silently.' },
    'cp-brd.7': { id: 'cp-brd.7', title: 'Review of portal requirements v1.0', status: 'closed', type: 'task', created_at: at(530), description: 'Independent review of cp-brd.1 to cp-brd.4 before acceptance.',
      comments: [
        { id: 'c1', author: 'Tomasz Nowak', at: at(529), kind: 'review', body: 'F1: cp-brd.2 does not say how many orders a page shows.\n\nF2: cp-brd.3 acceptance does not name a response time.' },
        { id: 'c2', author: 'Priya Raman', at: at(525), kind: 'disposition', body: 'F1 adopted: 50 per page. F2 adopted: 2 seconds for 95% of searches. Baseline v1.0 accepted with both changes.' },
      ] },
  };
  const req = (n, key, title, text, state = 'accepted') => {
    const id = 'cp-brd.' + n;
    reqRecords[id] = { id, title: key + ': ' + title, status: 'open', type: 'task', created_at: at(600), description: text, comments: [] };
    return { id, key, title: key + ': ' + title, revision: n === 2 ? 2 : 1, revisions: n === 2 ? 2 : 1, acceptance_state: state, sha256: (sha('req' + n) + sha('rq' + n)).slice(0, 64), description: text, recorded_at: at(524), recorded_by: 'Priya Raman' };
  };
  const requirements = {
    proj_portal: {
      baseline: { name: 'v1.0', state: 'accepted', manifest_sha256: (sha('base') + sha('line')).slice(0, 64), record: 'cp-brd.7', review: 'cp-brd.7', note: 'Accepted by Priya Raman after review cp-brd.7.' },
      narrative: null,
      items: [
        req(1, 'R01', 'Customers find any order quickly', '## Requirement\nA signed-in customer can find any order from any of their sites by reference, PO number or date.\n\n## Rationale\nSee decision cp-brd.5.\n\n## Acceptance criteria\nSearch by reference or PO returns in under 2 seconds for 95% of searches.'),
        req(2, 'R02', 'Order history stays usable for large accounts', '## Requirement\nOrder history is paged, 50 orders per page, in a stable order, for accounts with any number of orders.\n\n## Rationale\nMulti-site accounts (cp-brd.5) reach tens of thousands of orders.\n\n## Acceptance criteria\nNo duplicates or gaps across pages; CSV export contains every order.'),
        req(3, 'R03', 'Account settings in the customer\u2019s language', '## Requirement\nAccount settings are available in English and German.\n\n## Acceptance criteria\nEvery label and error message is translated.', 'draft'),
        req(4, 'R04', 'Failed card payments can be retried', '## Requirement\nA customer can retry a failed card payment once from the order page.\n\n## Rationale\nConstrained by cp-brd.6: no stored card details.\n\n## Acceptance criteria\nRetry re-opens the provider\u2019s hosted payment page.'),
      ],
      decisions: ['cp-brd.5', 'cp-brd.6'],
      records: reqRecords,
    },
  };
  const history = {};
  const feedback = {
    proj_portal: [
      { id: 'fb1', author: 'usr_lena', at: at(28), text: 'The order history page is very slow for the Hartmann account (about 4,000 orders).', status: 'open', task: 'task_port001', triage: 'Linked to “Paginate order history”.' },
      { id: 'fb2', author: 'usr_kestrel', at: at(9), text: 'Test fixtures for checkout assume a UK address; German postcodes fail validation.', status: 'open', task: null, triage: null },
      { id: 'fb3', author: 'usr_tomasz', at: at(120), text: 'Could review requests include the file and line they refer to?', status: 'resolved', task: null, triage: 'Added to the review checklist.' },
    ],
    proj_invoice: [], proj_handbook: [],
  };
  const audit = [];
  const credentials = {
    proj_portal: [{ id: 'cred_k1', label: 'Kestrel build runner', user_id: 'usr_kestrel', scopes: ['tasks', 'checkpoints', 'reviews'], created_at: at(390), expires_at: new Date(Date.now() + 60 * 24 * H).toISOString(), revoked: false }],
    proj_invoice: [], proj_handbook: [],
  };
  return { users, projects, memberships, tasks: T, history, feedback, audit, credentials, requirements, results: {}, session: null, seq: 100 };
}

function err(status, code, message, detail) { return { status, data: { error: { code, message, detail }, request_id: 'req_mock' } }; }
const ok = (data, status = 200) => ({ status, data });

export function createMock(options = {}) {
  const db = options.db || seed();
  if (options.extra) {
    // A real project's requirements, loaded only into a private prototype copy.
    const x = options.extra; const p = x.project;
    db.projects[p.id] = { ...p, archived: false };
    db.memberships[p.id] = { usr_priya: 'owner', usr_tomasz: 'contributor', usr_kestrel: 'contributor', usr_lena: 'viewer' };
    db.feedback[p.id] = []; db.credentials[p.id] = [];
    db.requirements[p.id] = { baseline: x.baseline, narrative: x.narrative, items: x.requirements, decisions: x.decisions, records: x.records };
  }
  const latency = options.latency ?? 120;
  const routes = [];
  const on = (method, pattern, fn, anonymous = false) => routes.push([method, new RegExp('^' + pattern + '$'), fn, anonymous]);
  const me = () => db.session && db.users[db.session];
  const role = (pid) => (db.memberships[pid] || {})[db.session];
  const visible = (pid) => db.projects[pid] && (me().superuser || role(pid));
  const canWrite = (pid) => me().superuser || ['owner', 'contributor'].includes(role(pid));
  const isOwner = (pid) => me().superuser || role(pid) === 'owner';
  const name = (uid) => (db.users[uid] ? db.users[uid].display_name : uid);
  const log = (pid, action, tid, detail) => {
    db.audit.unshift({ time: new Date().toISOString(), user_id: db.session, project_id: pid, task: tid || null, action, outcome: 'committed', detail: detail || null });
    if (tid) (db.history[tid] = db.history[tid] || []).unshift({ time: new Date().toISOString(), action, user_id: db.session, detail: detail || null });
  };
  const projectView = (p) => ({ ...p, role: me().superuser && !role(p.id) ? 'superuser' : role(p.id), members: Object.keys(db.memberships[p.id] || {}) });
  const taskView = (t) => ({ ...t, assignee_name: t.assignee ? name(t.assignee) : null, next_action: nextAction(t) });
  const nextAction = (t) => {
    if (t.status === 'closed') return null;
    switch (t.review_state) {
      case 'awaiting-review': return { who: 'owner', text: `Review revision ${t.contribution.revision}` };
      case 'changes-requested': return { who: 'assignee', text: `Address ${t.requests.filter((r) => r.status === 'open').length} requested change(s)` };
      case 'approved': return { who: 'owner', text: 'Integrate' };
      default: return t.assignee ? { who: 'assignee', text: 'Deliver a contribution' } : { who: 'anyone', text: 'Claim this task' };
    }
  };
  const findTask = (pid, tid) => db.tasks.find((t) => t.project_id === pid && t.id === tid);
  // The real brief/queue name each revision by id and report the latest review record,
  // which the page sends back so a stale review is refused.
  const contributionId = (t) => (t.contribution ? `con_${t.id}_${t.contribution.revision}` : null);
  const contributionView = (t) => t.contribution && { ...t.contribution, id: contributionId(t), author_name: name(t.contribution.author) };
  const openCount = (t) => t.requests.filter((r) => r.status === 'open').length;
  const guardProject = (pid) => (visible(pid) ? null : err(404, 'not_found', 'Project not found'));

  on('POST', '/v1/sessions', (b) => {
    const user = Object.values(db.users).find((u) => u.username === (b.username || '').trim().toLowerCase());
    if (!user || user.disabled || b.password !== 'demo') return err(401, 'unauthenticated', 'Invalid credentials');
    db.session = user.id;
    return ok({ session: { csrf_token: 'mock-csrf', expires_at: new Date(Date.now() + 12 * H).toISOString() }, user: { id: user.id, username: user.username, display_name: user.display_name, superuser: user.superuser } }, 201);
  }, true);
  on('GET', '/v1/sessions/current', () => ok({ user: { id: me().id, username: me().username, display_name: me().display_name, superuser: me().superuser }, via: 'session', csrf_token: 'mock-csrf' }));
  on('DELETE', '/v1/sessions/current', () => { db.session = null; return { status: 204, data: null }; });

  on('GET', '/v1/accounts/lookup', (b, p, q) => {
    const user = Object.values(db.users).find((u) => u.username === String(q.get('username') || '').toLowerCase() && !u.disabled);
    return user ? ok({ id: user.id, username: user.username, display_name: user.display_name }) : err(404, 'not_found', 'No active account with that username');
  });
  on('GET', '/v1/accounts', () => (me().superuser ? ok({ items: Object.values(db.users) }) : err(403, 'forbidden', 'Superuser authority required')));
  on('POST', '/v1/accounts', (b) => {
    if (!me().superuser) return err(403, 'forbidden', 'Superuser authority required');
    const username = String(b.username || '').trim().toLowerCase();
    if (!/^[a-z0-9][a-z0-9._-]{1,63}$/.test(username)) return err(422, 'invalid_payload', 'Username must be 2–64 characters: letters, digits, dot, dash or underscore');
    if (Object.values(db.users).some((u) => u.username === username)) return err(409, 'conflict', 'That username is already taken');
    const id = 'usr_' + (db.seq += 1);
    db.users[id] = { id, username, display_name: b.display_name || username, superuser: false, disabled: false, created_at: new Date().toISOString() };
    log(null, 'accounts.create', null, username);
    return ok(db.users[id], 201);
  });
  on('POST', '/v1/accounts/(?<uid>[\\w-]+)/reset', (b, p) => {
    if (!me().superuser) return err(403, 'forbidden', 'Superuser authority required');
    if (!db.users[p.uid]) return err(404, 'not_found', 'Account not found');
    log(null, 'accounts.reset', null, db.users[p.uid].username);
    return ok({ id: p.uid, reset_value: 'rst_' + Math.random().toString(36).slice(2, 10) + Math.random().toString(36).slice(2, 10), expires_at: new Date(Date.now() + 0.5 * H).toISOString(), single_use: true }, 201);
  });
  on('POST', '/v1/accounts/(?<uid>[\\w-]+)/disable', (b, p) => {
    if (!me().superuser) return err(403, 'forbidden', 'Superuser authority required');
    const user = db.users[p.uid];
    if (!user) return err(404, 'not_found', 'Account not found');
    if (user.superuser && Object.values(db.users).filter((u) => u.superuser && !u.disabled).length === 1) return err(409, 'conflict', 'Cannot disable the last active superuser');
    user.disabled = true; log(null, 'accounts.disable', null, user.username);
    return ok({ id: user.id, disabled: true });
  });
  on('POST', '/v1/accounts/(?<uid>[\\w-]+)/password', (b, p) => {
    if (p.uid !== db.session && !me().superuser) return err(403, 'forbidden', 'You can only change your own password');
    if (p.uid === db.session && b.current_password !== 'demo') return err(401, 'unauthenticated', 'Current password is incorrect');
    if (String(b.new_password || '').length < 12) return err(422, 'invalid_payload', 'New password must be at least 12 characters');
    return ok({ id: p.uid, changed: true });
  });

  on('GET', '/v1/projects', () => ok({ items: Object.values(db.projects).filter((p) => visible(p.id)).map(projectView) }));
  on('POST', '/v1/projects', (b) => {
    const title = String(b.name || '').trim();
    if (title.length < 2 || title.length > 80) return err(422, 'invalid_payload', 'Project name must be 2–80 characters');
    const id = 'proj_' + (db.seq += 1);
    db.projects[id] = { id, name: title, created_by: db.session, created_at: new Date().toISOString(), archived: false, description: b.description || '' };
    db.memberships[id] = { [db.session]: 'owner' }; db.feedback[id] = []; db.credentials[id] = [];
    log(id, 'projects.create');
    return ok(projectView(db.projects[id]), 201);
  });
  on('GET', '/v1/projects/(?<pid>[\\w-]+)', (b, p) => guardProject(p.pid) || ok(projectView(db.projects[p.pid])));
  on('POST', '/v1/projects/(?<pid>[\\w-]+)/archive', (b, p) => {
    const g = guardProject(p.pid); if (g) return g;
    if (!isOwner(p.pid)) return err(403, 'forbidden', 'Only a project owner can archive it');
    db.projects[p.pid].archived = true; log(p.pid, 'projects.archive');
    return ok(projectView(db.projects[p.pid]));
  });
  on('GET', '/v1/projects/(?<pid>[\\w-]+)/members', (b, p) => guardProject(p.pid) || ok({ items: Object.entries(db.memberships[p.pid]).map(([uid, r]) => ({ user_id: uid, display_name: name(uid), username: db.users[uid].username, role: r, disabled: db.users[uid].disabled, agent_of_name: db.users[uid].agent_of ? name(db.users[uid].agent_of) : null })) }));
  on('PUT', '/v1/projects/(?<pid>[\\w-]+)/members/(?<uid>[\\w-]+)', (b, p) => {
    const g = guardProject(p.pid); if (g) return g;
    if (!isOwner(p.pid)) return err(403, 'forbidden', 'Only a project owner can change membership');
    if (!['viewer', 'contributor', 'owner'].includes(b.role)) return err(422, 'invalid_payload', 'Role must be viewer, contributor or owner');
    if (!db.users[p.uid]) return err(404, 'not_found', 'Account not found');
    const owners = Object.entries(db.memberships[p.pid]).filter(([, r]) => r === 'owner');
    if (owners.length === 1 && owners[0][0] === p.uid && b.role !== 'owner') return err(409, 'conflict', 'A project needs at least one owner');
    db.memberships[p.pid][p.uid] = b.role; log(p.pid, 'members.set', null, `${name(p.uid)} → ${b.role}`);
    return ok({ project: p.pid, user: p.uid, role: b.role });
  });
  on('DELETE', '/v1/projects/(?<pid>[\\w-]+)/members/(?<uid>[\\w-]+)', (b, p) => {
    const g = guardProject(p.pid); if (g) return g;
    if (!isOwner(p.pid)) return err(403, 'forbidden', 'Only a project owner can change membership');
    const owners = Object.entries(db.memberships[p.pid]).filter(([, r]) => r === 'owner');
    if (owners.length === 1 && owners[0][0] === p.uid) return err(409, 'conflict', 'A project needs at least one owner');
    delete db.memberships[p.pid][p.uid]; log(p.pid, 'members.remove', null, name(p.uid));
    return { status: 204, data: null };
  });
  on('GET', '/v1/projects/(?<pid>[\\w-]+)/worker-credentials', (b, p) => guardProject(p.pid) || (isOwner(p.pid) ? ok({ items: db.credentials[p.pid].map((c) => ({ ...c, user_name: name(c.user_id) })) }) : err(403, 'forbidden', 'Only a project owner can see credentials')));
  on('POST', '/v1/projects/(?<pid>[\\w-]+)/worker-credentials', (b, p) => {
    const g = guardProject(p.pid); if (g) return g;
    if (!isOwner(p.pid)) return err(403, 'forbidden', 'Only a project owner can issue credentials');
    const cred = { id: 'cred_' + (db.seq += 1), label: b.label || 'Worker credential', user_id: db.session, scopes: b.scopes || ['tasks'], created_at: new Date().toISOString(), expires_at: new Date(Date.now() + 30 * 24 * H).toISOString(), revoked: false };
    db.credentials[p.pid].push(cred); log(p.pid, 'credentials.issue', null, cred.label);
    return ok({ credential: { ...cred, secret: 'orc_' + Math.random().toString(36).slice(2) + Math.random().toString(36).slice(2) } }, 201);
  });
  on('POST', '/v1/projects/(?<pid>[\\w-]+)/worker-credentials/(?<cid>[\\w-]+)/revoke', (b, p) => {
    const g = guardProject(p.pid); if (g) return g;
    const cred = db.credentials[p.pid].find((c) => c.id === p.cid);
    if (!cred) return err(404, 'not_found', 'Credential not found');
    if (!isOwner(p.pid) && cred.user_id !== db.session) return err(403, 'forbidden', 'Only the owner or the credential holder can revoke it');
    cred.revoked = true; log(p.pid, 'credentials.revoke', null, cred.label);
    return { status: 204, data: null };
  });

  on('GET', '/v1/projects/(?<pid>[\\w-]+)/tasks', (b, p, q) => {
    const g = guardProject(p.pid); if (g) return g;
    const limit = Number(q.get('limit') || 25);
    if (!(limit >= 1 && limit <= 100)) return err(422, 'invalid_payload', 'limit must be 1..100');
    const offset = Number(q.get('cursor') || 0);
    let items = db.tasks.filter((t) => t.project_id === p.pid);
    const status = q.get('status'); const review = q.get('review_state'); const text = (q.get('q') || '').toLowerCase(); const mine = q.get('assignee');
    if (status) items = items.filter((t) => (status === 'active' ? t.status !== 'closed' : t.status === status));
    if (review) items = items.filter((t) => t.review_state === review);
    if (mine) items = items.filter((t) => t.assignee === mine);
    if (text) items = items.filter((t) => (t.title + ' ' + t.description + ' ' + t.id).toLowerCase().includes(text));
    items = items.slice().sort((a, c) => a.priority - c.priority || (a.updated_at < c.updated_at ? 1 : -1));
    const page = items.slice(offset, offset + limit).map(taskView);
    return ok({ items: page, total: items.length, next_cursor: offset + limit < items.length ? String(offset + limit) : null });
  });
  on('POST', '/v1/projects/(?<pid>[\\w-]+)/tasks', (b, p) => {
    const g = guardProject(p.pid); if (g) return g;
    if (!canWrite(p.pid)) return err(403, 'forbidden', 'Viewers cannot create tasks');
    if (db.projects[p.pid].archived) return err(409, 'conflict', 'This project is archived');
    const title = String(b.title || '').trim();
    if (title.length < 3 || title.length > 200) return err(422, 'invalid_payload', 'Title must be 3–200 characters');
    const t = { id: 'task_' + (db.seq += 1), project_id: p.pid, title, description: b.description || '', status: 'open', assignee: null, version: 1, created_by: db.session, created_at: new Date().toISOString(), updated_at: new Date().toISOString(), priority: Number(b.priority || 2), review_state: 'none', contribution: null, requests: [], lifecycle: facts(), checkpoint: null, depends_on: [], attachments: [] };
    db.tasks.push(t); log(p.pid, 'task-created', t.id, title);
    return ok(taskView(t), 201);
  });
  on('GET', '/v1/projects/(?<pid>[\\w-]+)/tasks/(?<tid>[\\w-]+)', (b, p) => { const g = guardProject(p.pid); if (g) return g; const t = findTask(p.pid, p.tid); return t ? ok(taskView(t)) : err(404, 'not_found', 'Task not found'); });
  on('GET', '/v1/projects/(?<pid>[\\w-]+)/tasks/(?<tid>[\\w-]+)/brief', (b, p) => {
    const g = guardProject(p.pid); if (g) return g; const t = findTask(p.pid, p.tid); if (!t) return err(404, 'not_found', 'Task not found');
    return ok({ task: taskView(t), checkpoint: t.checkpoint && { ...t.checkpoint, author_name: name(t.checkpoint.author) }, lifecycle: t.lifecycle, review: { state: t.review_state, contribution: contributionView(t), requests: t.requests, open_requests: openCount(t), latest_id: contributionId(t) }, depends_on: t.depends_on.map((id) => { const d = db.tasks.find((x) => x.id === id); return d ? { id, title: d.title, status: d.status } : { id, title: id, status: 'unknown' }; }) });
  });
  on('PATCH', '/v1/projects/(?<pid>[\\w-]+)/tasks/(?<tid>[\\w-]+)', (b, p) => {
    const g = guardProject(p.pid); if (g) return g; const t = findTask(p.pid, p.tid); if (!t) return err(404, 'not_found', 'Task not found');
    if (!canWrite(p.pid)) return err(403, 'forbidden', 'Viewers cannot edit tasks');
    if (b.version !== t.version) return err(409, 'conflict', 'Task was modified; reread it before updating', { expected_version: t.version });
    for (const k of ['title', 'description', 'priority']) if (b[k] !== undefined) t[k] = b[k];
    t.version += 1; t.updated_at = new Date().toISOString(); log(p.pid, 'task-updated', t.id);
    return ok(taskView(t));
  });
  on('POST', '/v1/projects/(?<pid>[\\w-]+)/tasks/(?<tid>[\\w-]+)/claim', (b, p) => {
    const g = guardProject(p.pid); if (g) return g; const t = findTask(p.pid, p.tid); if (!t) return err(404, 'not_found', 'Task not found');
    if (!canWrite(p.pid)) return err(403, 'forbidden', 'Viewers cannot claim tasks');
    if (t.assignee && t.assignee !== db.session) return err(409, 'conflict', `Already claimed by ${name(t.assignee)}`);
    t.assignee = db.session; t.status = 'in_progress'; t.version += 1; t.updated_at = new Date().toISOString(); log(p.pid, 'task-claimed', t.id);
    return ok(taskView(t));
  });
  on('POST', '/v1/projects/(?<pid>[\\w-]+)/tasks/(?<tid>[\\w-]+)/reviews', (b, p) => {
    const g = guardProject(p.pid); if (g) return g; const t = findTask(p.pid, p.tid); if (!t) return err(404, 'not_found', 'Task not found');
    if (b.operation === 'request-changes' || b.operation === 'approve') {
      if (!isOwner(p.pid)) return err(403, 'forbidden', 'Only a project owner can review');
      const stale = b.contribution !== undefined ? b.contribution !== contributionId(t) : b.contribution_revision !== t.contribution.revision;
      if (!t.contribution || stale || t.review_state !== 'awaiting-review') return err(409, 'conflict', 'This contribution changed or was already reviewed; reload to see the current revision');
      if (b.operation === 'approve') { t.review_state = 'approved'; t.lifecycle.reviewed = yes('Approved by ' + name(db.session)); }
      else {
        const items = (b.items || []).map((x) => String(typeof x === 'string' ? x : (x && x.text) || '').trim()).filter(Boolean);
        if (!items.length) return err(422, 'invalid_payload', 'Add at least one requested change');
        items.forEach((text, i) => t.requests.push({ id: 'rq' + (db.seq += 1) + i, text, status: 'open', by: db.session }));
        t.review_state = 'changes-requested'; t.lifecycle.reviewed = no('Changes requested');
      }
      t.version += 1; t.updated_at = new Date().toISOString(); log(p.pid, 'review-' + b.operation, t.id, b.summary);
      return ok(taskView(t), 201);
    }
    if (b.operation === 'contribute') {
      if (t.assignee !== db.session) return err(403, 'forbidden', 'Only the assignee can deliver a contribution');
      if (!/^[0-9a-f]{40}$/.test(b.commit || '')) return err(422, 'invalid_payload', 'Commit must be a full 40-character hash');
      if (!String(b.summary || '').trim()) return err(422, 'invalid_payload', 'Summarise what changed');
      const revision = t.contribution ? t.contribution.revision + 1 : 1;
      t.contribution = { revision, commit: b.commit, base_commit: b.base_commit || null, summary: b.summary.trim(), author: db.session, at: new Date().toISOString(), branch: b.branch || null };
      t.requests.forEach((r) => { if (r.status === 'open') { r.status = 'resolved'; r.resolution = 'Addressed in revision ' + revision; } });
      t.review_state = 'awaiting-review'; t.lifecycle.implemented = yes('Revision ' + revision); t.lifecycle.reviewed = no('Revision ' + revision + ' awaiting review');
      t.version += 1; t.updated_at = new Date().toISOString(); log(p.pid, 'contribution', t.id, 'Revision ' + revision);
      return ok(taskView(t), 201);
    }
    return err(422, 'invalid_payload', 'Unsupported review operation');
  });
  on('GET', '/v1/projects/(?<pid>[\\w-]+)/tasks/(?<tid>[\\w-]+)/history', (b, p, q) => {
    const g = guardProject(p.pid); if (g) return g; const t = findTask(p.pid, p.tid); if (!t) return err(404, 'not_found', 'Task not found');
    const synthetic = [];
    if (t.contribution) synthetic.push({ time: t.contribution.at, action: 'contribution', user_id: t.contribution.author, detail: `Revision ${t.contribution.revision} · ${t.contribution.commit.slice(0, 9)} — ${t.contribution.summary}` });
    t.requests.forEach((r) => synthetic.push({ time: t.contribution ? t.contribution.at : t.created_at, action: r.status === 'open' ? 'change-requested' : 'change-resolved', user_id: r.by, detail: r.text + (r.resolution ? ' — ' + r.resolution : '') }));
    if (t.assignee) synthetic.push({ time: t.created_at, action: 'task-claimed', user_id: t.assignee });
    synthetic.push({ time: t.created_at, action: 'task-created', user_id: t.created_by });
    const all = (db.history[t.id] || []).concat(synthetic).map((e) => ({ ...e, user_name: name(e.user_id) }));
    const limit = Math.min(Number(q.get('limit') || 20), 20); const offset = Number(q.get('cursor') || 0);
    return ok({ items: all.slice(offset, offset + limit), total: all.length, next_cursor: offset + limit < all.length ? String(offset + limit) : null });
  });
  on('GET', '/v1/projects/(?<pid>[\\w-]+)/queue', (b, p) => {
    const g = guardProject(p.pid); if (g) return g;
    const items = db.tasks.filter((t) => t.project_id === p.pid && ['awaiting-review', 'changes-requested', 'approved'].includes(t.review_state))
      .map((t) => ({ ...taskView(t), contribution: contributionView(t), open_requests: openCount(t) }));
    return ok({ items, total: items.length, next_cursor: null, complete: true });
  });
  on('GET', '/v1/me/work', () => {
    const mine = []; const toReview = [];
    for (const t of db.tasks) {
      if (!visible(t.project_id) || t.status === 'closed' || db.projects[t.project_id].archived) continue;
      const v = { ...taskView(t), project_name: db.projects[t.project_id].name };
      if (t.assignee === db.session) mine.push(v);
      if (isOwner(t.project_id) && (role(t.project_id) === 'owner') && ['awaiting-review', 'approved'].includes(t.review_state)) toReview.push(v);
    }
    return ok({ assigned: mine, to_review: toReview, agents: agentsOf(db.session).map((a) => agentView(a, me())) });
  });
  on('GET', '/v1/projects/(?<pid>[\\w-]+)/feedback', (b, p) => guardProject(p.pid) || ok({ items: db.feedback[p.pid].map((f) => ({ ...f, author_name: name(f.author) })), total: db.feedback[p.pid].length, next_cursor: null }));
  on('POST', '/v1/projects/(?<pid>[\\w-]+)/feedback', (b, p) => {
    const g = guardProject(p.pid); if (g) return g;
    if (!canWrite(p.pid)) return err(403, 'forbidden', 'Viewers cannot submit feedback');
    const text = String(b.text || '').trim();
    if (text.length < 1 || text.length > 2000) return err(422, 'invalid_payload', 'Feedback text must be 1–2000 characters');
    const f = { id: 'fb' + (db.seq += 1), author: db.session, at: new Date().toISOString(), text, status: 'open', task: b.task || null, triage: null };
    db.feedback[p.pid].unshift(f); log(p.pid, 'feedback.add');
    return ok({ ...f, author_name: name(f.author) }, 201);
  });
  on('GET', '/v1/projects/(?<pid>[\\w-]+)/audit', (b, p) => guardProject(p.pid) || (isOwner(p.pid) ? ok({ items: db.audit.filter((a) => a.project_id === p.pid).map((a) => ({ ...a, user_name: name(a.user_id) })), next_cursor: null }) : err(403, 'forbidden', 'Only a project owner can read the audit log')));

  // ---- personal agents (proposed routes; kittrial-5bb.22) ----------------------
  const RANK = { viewer: 0, contributor: 1, owner: 2 };
  const agentView = (a, viewer) => {
    const projectsOf = Object.entries(db.memberships).filter(([pid, m]) => m[a.id] && !db.projects[pid].archived).map(([pid, m]) => ({ id: pid, name: db.projects[pid].name, role: m[a.id] }));
    const open = db.tasks.filter((t) => t.assignee === a.id && t.status !== 'closed' && !db.projects[t.project_id].archived);
    const items = [];
    for (const t of open) {
      if (t.review_state === 'changes-requested') items.push({ project_id: t.project_id, task_id: t.id, title: t.title, kind: 'feedback', text: `${t.requests.filter((r) => r.status === 'open').length} requested change(s) to address` });
      else if (t.review_state === 'awaiting-review') items.push({ project_id: t.project_id, task_id: t.id, title: t.title, kind: 'waiting', text: `revision ${t.contribution.revision} waiting for review` });
      else if (t.review_state !== 'approved') items.push({ project_id: t.project_id, task_id: t.id, title: t.title, kind: 'working', text: 'claimed, no delivery yet' });
    }
    const claimable = db.tasks.filter((t) => !t.assignee && t.status === 'open' && projectsOf.some((p) => p.id === t.project_id)).length;
    const attention = items.some((i) => i.kind === 'feedback') ? 'feedback' : items.some((i) => i.kind === 'working') ? 'working' : items.length ? 'waiting' : 'idle';
    // The folder path can reveal local usernames: only the owner and superusers see it.
    const ownerView = Boolean(viewer && (viewer.id === a.agent_of || viewer.superuser));
    return { id: a.id, display_name: a.display_name, owner_id: a.agent_of, owner_name: name(a.agent_of), tool: a.tool, working_directory: ownerView ? a.working_directory : undefined,
      last_seen_at: a.last_seen_at || null, projects: projectsOf, items: items.sort((x, y) => (x.kind === 'feedback' ? -1 : y.kind === 'feedback' ? 1 : 0)), attention, claimable, disabled: a.disabled || db.users[a.agent_of].disabled };
  };
  const agentsOf = (uid) => Object.values(db.users).filter((u) => u.agent_of === uid);
  on('GET', '/v1/agents', () => ok({ items: agentsOf(db.session).map((a) => agentView(a, me())) }));
  on('POST', '/v1/agents', (b) => {
    const title = String(b.name || '').trim();
    if (title.length < 2 || title.length > 40) return err(422, 'invalid_payload', 'Agent name must be 2–40 characters');
    const projectsWanted = Array.isArray(b.projects) ? b.projects : [];
    for (const pid of projectsWanted) {
      if (!visible(pid)) return err(404, 'not_found', 'Project not found');
      if (!(me().superuser || RANK[role(pid)] >= RANK.contributor)) return err(403, 'forbidden', 'You need contributor access to add an agent to ' + db.projects[pid].name);
    }
    const id = 'usr_' + (db.seq += 1);
    const username = title.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '') + '-' + id.slice(4);
    db.users[id] = { id, username, display_name: title + ' (agent)', superuser: false, disabled: false, created_at: new Date().toISOString(), agent_of: db.session, tool: b.tool || null, working_directory: b.working_directory || null, last_seen_at: null };
    for (const pid of projectsWanted) db.memberships[pid][id] = 'contributor';
    log(projectsWanted[0] || null, 'agents.create', null, title);
    return ok({ agent: agentView(db.users[id], me()), owner_name: me().display_name, server: 'https://orchestra.example.invalid', credential: { id: 'cred_' + (db.seq += 1), secret: 'orc_' + Math.random().toString(36).slice(2) + Math.random().toString(36).slice(2) } }, 201);
  });
  const ownAgent = (aid) => { const a = db.users[aid]; return a && a.agent_of && (a.agent_of === db.session || me().superuser) ? a : null; };
  on('GET', '/v1/agents/(?<aid>[\\w-]+)', (b, p) => { const a = ownAgent(p.aid); return a ? ok({ ...agentView(a, me()), credentials: a.credentials || [] }) : err(404, 'not_found', 'Agent not found'); });
  on('POST', '/v1/agents/(?<aid>[\\w-]+)/credentials', (b, p) => {
    const a = ownAgent(p.aid); if (!a) return err(404, 'not_found', 'Agent not found');
    const cred = { id: 'cred_' + (db.seq += 1), label: b.label || 'agent', created_at: new Date().toISOString(), revoked: false };
    a.credentials = (a.credentials || []).concat([cred]);
    return ok({ agent: a.id, credential: { ...cred, secret: 'orc_' + Math.random().toString(36).slice(2) + Math.random().toString(36).slice(2), secret_available: true } }, 201);
  });
  on('POST', '/v1/agents/(?<aid>[\\w-]+)/credentials/(?<cid>[\\w-]+)/revoke', (b, p) => {
    const a = ownAgent(p.aid); const c = a && (a.credentials || []).find((x) => x.id === p.cid);
    if (!c) return err(404, 'not_found', 'Credential not found');
    c.revoked = true; return { status: 204, data: null };
  });
  on('PATCH', '/v1/agents/(?<aid>[\\w-]+)', (b, p) => {
    const a = db.users[p.aid];
    if (!a || !a.agent_of || (a.agent_of !== db.session && !me().superuser)) return err(404, 'not_found', 'Agent not found');
    for (const k of ['working_directory', 'tool']) if (b[k] !== undefined) a[k] = String(b[k]).slice(0, 300) || null;
    return ok(agentView(a, me()));
  });

  // ---- requirements & decisions (proposed read routes) ------------------------
  const R = (pid) => db.requirements && db.requirements[pid];
  const textOf = (rec) => [rec.title, rec.description].concat((rec.comments || []).map((c) => (typeof c.body === 'string' ? c.body : JSON.stringify(c.body)))).join('\n');
  const keysOf = (data) => Object.fromEntries(data.items.filter((r) => r.key).map((r) => [r.key, r.id]));
  const idsIn = (text, known, keys = {}) => { const out = new Set(); const re = /\b(kittrial-[a-z0-9]+(?:\.\d+)*|[a-z]+-[a-z0-9]{3}(?:\.\d+)+|R\d{2})\b/g; let m; while ((m = re.exec(text))) { const id = keys[m[1]] || m[1]; if (known[id]) out.add(id); } return [...out]; };
  const brief = (rec) => ({ id: rec.id, title: rec.title, status: rec.status });
  const firstSentence = (text, name) => { const m = new RegExp('##\\s+' + name + '\\s*\\n+([^\\n]+)', 'i').exec(text || ''); return m ? m[1].split(/(?<=\.)\s/)[0].slice(0, 220) : ''; };
  const connections = (data, id) => {
    const recs = data.records; const self = recs[id];
    const keys = keysOf(data);
    const references = self ? idsIn(textOf(self), recs, keys).filter((x) => x !== id).map((x) => brief(recs[x])) : [];
    const mentioned_by = Object.values(recs).filter((r) => r.id !== id && idsIn(textOf(r), recs, keys).includes(id)).map(brief);
    return { references, mentioned_by };
  };
  on('GET', '/v1/projects/(?<pid>[\\w-]+)/requirements', (b, p) => {
    const g = guardProject(p.pid); if (g) return g;
    const data = R(p.pid);
    if (!data) return ok({ baseline: null, narrative: null, items: [], known: [], decision_count: 0 });
    return ok({ baseline: data.baseline, narrative: data.narrative, items: data.items, known: Object.keys(data.records), keys: keysOf(data), decision_count: data.decisions.length });
  });
  on('GET', '/v1/projects/(?<pid>[\\w-]+)/requirements/(?<rid>[\\w.-]+)', (b, p) => {
    const g = guardProject(p.pid); if (g) return g;
    const data = R(p.pid); if (!data) return err(404, 'not_found', 'Requirement not found');
    const item = data.items.find((r) => r.id === p.rid) || (data.narrative && data.narrative.id === p.rid ? data.narrative : null);
    if (!item) return err(404, 'not_found', 'Requirement not found');
    const decs = data.decisions.map((id) => data.records[id]).filter(Boolean).map((d) => ({ ...brief(d), why: firstSentence(d.description, 'Decision'), mentions: idsIn(textOf(d), data.records, keysOf(data)) }));
    const direct = decs.filter((d) => d.mentions.includes(item.id));
    const c = connections(data, item.id);
    return ok({ requirement: item, known: Object.keys(data.records), keys: keysOf(data), decisions: { direct, baseline: decs.filter((d) => !direct.includes(d)) }, mentioned_by: c.mentioned_by.filter((m) => !data.decisions.includes(m.id)) });
  });
  on('GET', '/v1/projects/(?<pid>[\\w-]+)/decisions', (b, p) => {
    const g = guardProject(p.pid); if (g) return g;
    const data = R(p.pid);
    const items = data ? data.decisions.map((id) => data.records[id]).filter(Boolean).map((d) => ({ ...brief(d), created_at: d.created_at, summary: firstSentence(d.description, 'Decision') })).sort((a, c) => (a.created_at < c.created_at ? 1 : -1)) : [];
    return ok({ items, total: items.length, next_cursor: null });
  });
  on('GET', '/v1/projects/(?<pid>[\\w-]+)/records/(?<id>[\\w.-]+)', (b, p) => {
    const g = guardProject(p.pid); if (g) return g;
    const data = R(p.pid); const rec = data && data.records[p.id];
    if (!rec) return err(404, 'not_found', 'Record not found');
    const kind = data.decisions.includes(rec.id) ? 'decision' : data.items.some((r) => r.id === rec.id) || (data.narrative && data.narrative.id === rec.id) ? 'requirement' : 'record';
    return ok({ record: rec, kind, known: Object.keys(data.records), keys: keysOf(data), ...connections(data, rec.id) });
  });

  return async function transport(method, url, headers, body) {
    await new Promise((resolve) => setTimeout(resolve, latency));
    const parsed = new URL(url, 'http://mock.invalid');
    const payload = body ? JSON.parse(body) : {};
    for (const [verb, pattern, fn, anonymous] of routes) {
      if (verb !== method) continue;
      const match = pattern.exec(parsed.pathname);
      if (!match) continue;
      if (!anonymous && !me()) return err(401, 'unauthenticated', 'Sign in to continue');
      if (!anonymous && me().disabled) return err(401, 'unauthenticated', 'This account is disabled');
      const key = headers['Idempotency-Key'];
      if (method !== 'GET' && !anonymous && headers['X-CSRF-Token'] !== 'mock-csrf') return err(403, 'forbidden', 'CSRF token missing or invalid');
      const cacheKey = key && [db.session, method, parsed.pathname, key].join(' ');
      if (cacheKey && db.results[cacheKey]) return db.results[cacheKey];
      const result = fn(payload, match.groups || {}, parsed.searchParams);
      if (cacheKey && result.status < 400) db.results[cacheKey] = result;
      return JSON.parse(JSON.stringify(result));
    }
    return err(404, 'not_found', 'No such route');
  };
}

