import { h, time, confirmDialog, shortSha, toast } from '../dom.js';
import { pageHead, reviewChip, statusChip, priority, empty, field, setFieldError, formValues, act, lifecycleStrip, describe, errorState } from '../ui.js';
import { canWrite, isOwner } from './project.js';

const ACTIONS = {
  'task-created': ['Created the task', ''], 'task-claimed': ['Claimed the task', 'accent'], 'task-updated': ['Edited the task', ''],
  'contribution': ['Delivered a contribution', 'accent'], 'change-requested': ['Requested a change', 'warn'], 'change-resolved': ['Change resolved', 'ok'],
  'review-approve': ['Approved', 'ok'], 'review-request-changes': ['Requested changes', 'warn'],
  'approve': ['Approved', 'ok'], 'request-changes': ['Requested changes', 'warn'], 'respond': ['Responded to review', ''], 'recommend': ['Recommended approval', ''],
  'checkpoint-added': ['Recorded a checkpoint', ''], 'checkpoint': ['Recorded a checkpoint', ''], 'comment': ['Commented', ''],
};

export async function create(ctx, { pid }) {
  const project = await ctx.api.project(pid);
  if (!canWrite(project) || project.archived || project.usable === false) {
    return h('div', { class: 'stack' }, pageHead({ title: 'New task' }), h('div', { class: 'banner crit', role: 'alert' }, project.usable === false ? (project.unusable_reason || 'This project cannot be used on this server.') : project.archived ? 'This project is archived.' : 'Viewers cannot create tasks. Ask a project owner for contributor access.'));
  }
  const form = h('form', { class: 'form', novalidate: true },
    field({ id: 'title', label: 'Title', hint: 'A short outcome, e.g. “Paginate order history”. 3–200 characters.', required: true, maxlength: 200 }),
    field({ id: 'description', label: 'Description and acceptance', type: 'textarea', rows: 8, hint: 'What needs to be true when this is done, and how someone can check it. Write it so a newcomer or an agent can start without asking.' }),
    field({ id: 'priority', label: 'Priority', type: 'select', value: '2', options: [['1', 'P1 — urgent'], ['2', 'P2 — normal'], ['3', 'P3 — when possible']] }),
    h('div', { class: 'actions' }, h('button', { type: 'submit', class: 'primary' }, 'Create task'), h('a', { class: 'btn', href: ctx.href('/p/' + pid) }, 'Cancel')));
  form.addEventListener('input', () => ctx.setDirty(true));
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const v = formValues(form);
    if (v.title.trim().length < 3) return setFieldError(form, 'title', 'Enter a title of at least 3 characters.');
    setFieldError(form, 'title', '');
    const created = await act(form.querySelector('button[type=submit]'), () => ctx.api.createTask(pid, { title: v.title.trim(), description: v.description.trim(), priority: Number(v.priority) }), { success: 'Task created' }).catch(() => null);
    if (created) { ctx.setDirty(false); ctx.go(`/p/${pid}/t/${created.id}`); }
  });
  return h('div', { class: 'stack' },
    pageHead({ crumbs: [{ label: 'Projects', href: ctx.href('/projects') }, { label: project.name, href: ctx.href('/p/' + pid) }, { label: 'New task' }], title: 'New task' }),
    h('section', { class: 'panel' }, h('div', { class: 'panel-body' }, form)));
}

export async function detail(ctx, { pid, tid }) {
  const [project, brief] = await Promise.all([ctx.api.project(pid), ctx.api.brief(pid, tid)]);
  const t = brief.task;
  const review = { requests: [], ...brief.review };
  const owner = isOwner(project);
  const writer = canWrite(project) && !project.archived;
  const mine = t.assignee === ctx.me.id;
  const reload = () => ctx.render();

  const actions = [];
  if (writer && !t.assignee && t.status !== 'closed') {
    actions.push(h('button', { type: 'button', class: 'primary', onclick: async (e) => {
      try { await act(e.currentTarget, () => ctx.api.claim(pid, tid), { success: 'You have claimed this task' }); } catch (error) { if (error.status === 409) reload(); return; }
      reload();
    } }, 'Claim task'));
  }
  if (writer) actions.push(h('button', { type: 'button', onclick: () => editPanel.hidden ? openEdit() : null }, 'Edit'));

  // ---- next action -------------------------------------------------------------
  const next = t.next_action;
  const nextBanner = next ? h('div', { class: 'banner info' },
    h('strong', null, 'Next: '), next.text,
    next.who === 'owner' ? h('span', { class: 'muted' }, owner ? ' — that’s you (project owner).' : ' — waiting on a project owner.') :
    next.who === 'assignee' ? h('span', { class: 'muted' }, mine ? ' — that’s you.' : ` — waiting on ${t.assignee_name || 'the assignee'}.`) :
    h('span', { class: 'muted' }, writer ? ' — any contributor can claim it.' : '')) : null;

  // ---- brief -------------------------------------------------------------------
  const cp = brief.checkpoint;
  const briefPanel = h('section', { class: 'panel', 'aria-labelledby': 'brief-h' },
    h('div', { class: 'panel-head' }, h('h2', { class: 'small', id: 'brief-h' }, 'Brief'), cp ? h('span', { class: 'small muted' }, 'Checkpoint ', time(cp.at), ' by ', cp.author_name || cp.author) : null),
    h('div', { class: 'panel-body' },
      t.description ? h('p', { class: 'prose' }, t.description) : h('p', { class: 'muted' }, 'No description yet.'),
      cp ? [h('h3', { class: 'small' }, 'Where it stands'), h('p', { class: 'prose' }, cp.summary), cp.next_action ? h('p', null, h('strong', null, 'Next step: '), cp.next_action) : null] : null,
      cp && cp.open_items && cp.open_items.length ? [h('h3', { class: 'small' }, 'Unresolved items ', h('span', { class: 'nav-count' }, cp.open_items.length)),
        h('ul', { class: 'open-items' }, cp.open_items.map((o) => h('li', null, h('span', { class: 'chip plain' }, o.kind), ' ', o.text)))] : null));

  // ---- contribution & review ---------------------------------------------------
  const c = review.contribution;
  const openRequests = review.requests.filter((r) => r.status === 'open');
  const reviewBody = h('div', { class: 'panel-body' });
  if (c) {
    reviewBody.append(
      h('dl', { class: 'kv' },
        h('dt', null, 'Revision'), h('dd', null, String(c.revision)),
        h('dt', null, 'Commit'), h('dd', null, h('code', { title: c.commit }, shortSha(c.commit)), c.branch ? h('span', { class: 'muted' }, ' on ', h('code', null, c.branch)) : null),
        c.base_commit ? [h('dt', null, 'Based on'), h('dd', null, h('code', { title: c.base_commit }, shortSha(c.base_commit)))] : null,
        h('dt', null, 'Delivered'), h('dd', null, time(c.at), ' by ', c.author_name || c.author)),
      h('p', { class: 'prose' }, c.summary));
  } else {
    reviewBody.append(empty('No contribution yet', t.assignee ? `${t.assignee_name} will deliver work here for review.` : 'Once someone claims this task, their delivered work appears here.'));
  }
  if (review.requests.length) {
    reviewBody.append(h('h3', { class: 'small' }, 'Requested changes'),
      h('ol', { class: 'request-list' }, review.requests.map((r) => h('li', null,
        h('span', { class: 'chip ' + (r.status === 'open' ? 'warn' : 'ok') }, r.status === 'open' ? 'Open' : 'Resolved'), ' ', r.text,
        r.resolution ? h('div', { class: 'small muted' }, 'Resolution: ', r.resolution) : null))));
  }
  // A requested change stays open until the contributor records a response for it (the
  // review "respond" step), even after a newer revision arrives. The assignee responds
  // here; everyone else is told who the requests are waiting on.
  const carried = c ? openRequests.filter((r) => r.contribution && r.contribution !== c.id) : [];
  const responder = mine && writer && c && openRequests.length > 0;
  if (carried.length && !responder) {
    reviewBody.append(h('div', { class: 'banner', role: 'note' },
      `Revision ${c.revision} arrived, but ${carried.length} requested change(s) from an earlier revision are still open. `,
      `Each stays open until ${t.assignee_name || 'the assignee'} responds to it.`));
  }
  // A reviewer's recommendation is advice to the owner; it never changes the review state.
  const advice = review.state === 'awaiting-review' ? review.recommendation : null;
  if (advice) reviewBody.append(recommendationNote(review, advice));
  if (owner && c && review.state === 'awaiting-review' && !project.archived) reviewBody.append(reviewActions(ctx, pid, tid, c, review));
  if (!owner && writer && !mine && c && c.author !== ctx.me.id && review.state === 'awaiting-review') reviewBody.append(recommendForm(ctx, pid, tid, c));
  if (responder) reviewBody.append(respondForm(ctx, pid, tid, c, review, openRequests, carried.length > 0));
  if (mine && writer && ['none', 'changes-requested'].includes(review.state) && t.status !== 'closed') reviewBody.append(deliverForm(ctx, pid, tid, c, openRequests.length, review, carried.length > 0));
  const reviewPanel = h('section', { class: 'panel', 'aria-labelledby': 'review-h' },
    h('div', { class: 'panel-head' }, h('h2', { class: 'small', id: 'review-h' }, 'Contribution & review'), reviewChip(review.state)), reviewBody);

  // ---- history (bounded pages) -----------------------------------------------
  const historyList = h('ul', { class: 'timeline' });
  const more = h('button', { type: 'button', hidden: true }, 'Show older');
  let cursor = null;
  async function loadHistory() {
    let page;
    try { page = await ctx.api.history(pid, tid, { limit: 20, cursor }); } catch (error) { historyList.replaceChildren(h('li', null, h('span'), h('p', { class: 'muted' }, describe(error)))); return; }
    for (const e of page.items) {
      // In-process events carry action/user/time; canonical entries carry kind/author/timestamp/body.
      const action = e.action || e.kind;
      const [label, tone] = ACTIONS[action] || [action, ''];
      const detail = e.detail || e.body;
      const who = [e.user_name, e.author_name, e.author, e.actor, e.user_id].find((v) => typeof v === 'string' && v) || '';
      historyList.append(h('li', null, h('span', { class: 'dot ' + tone, 'aria-hidden': 'true' }),
        h('div', null, h('div', { class: 'event-head' }, h('strong', null, who), h('span', null, label), time(e.time || e.timestamp)), detail ? h('p', { class: 'event-body prose' }, detail) : null)));
    }
    if (!page.items.length && !cursor) historyList.append(h('li', null, h('span'), h('p', { class: 'muted' }, 'No history yet.')));
    cursor = page.next_cursor; more.hidden = !cursor;
  }
  more.addEventListener('click', loadHistory);
  loadHistory();
  const historyPanel = h('section', { class: 'panel', 'aria-labelledby': 'hist-h' },
    h('div', { class: 'panel-head' }, h('h2', { class: 'small', id: 'hist-h' }, 'History')),
    h('div', { class: 'panel-body' }, historyList, h('div', null, more)));

  // ---- edit (version-checked) --------------------------------------------------
  const editPanel = h('section', { class: 'panel', hidden: true, 'aria-labelledby': 'edit-h' });
  function openEdit() {
    const conflict = h('div', { class: 'banner crit', role: 'alert', hidden: true });
    const form = h('form', { class: 'form panel-body', novalidate: true },
      conflict,
      field({ id: 'e-title', label: 'Title', value: t.title, maxlength: 200 }),
      field({ id: 'e-description', label: 'Description and acceptance', type: 'textarea', rows: 8, value: t.description }),
      h('div', { class: 'actions' }, h('button', { type: 'submit', class: 'primary' }, 'Save changes'), h('button', { type: 'button', onclick: () => { ctx.setDirty(false); editPanel.hidden = true; } }, 'Cancel')));
    form.addEventListener('input', () => ctx.setDirty(true));
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      const v = formValues(form);
      if (v['e-title'].trim().length < 3) return setFieldError(form, 'e-title', 'Enter a title of at least 3 characters.');
      try {
        await act(form.querySelector('button[type=submit]'), () => ctx.api.updateTask(pid, tid, { title: v['e-title'].trim(), description: v['e-description'], version: t.version }), {
          success: 'Saved',
          onError: (error) => {
            if (error.status !== 409) return false;
            conflict.replaceChildren('Someone else changed this task while you were editing. Your text is still below — copy what you need, then ',
              h('button', { type: 'button', class: 'link', onclick: () => { ctx.setDirty(false); reload(); } }, 'reload the current version'), '.');
            conflict.hidden = false; conflict.focus?.();
            return true;
          },
        });
      } catch { return; }
      if (conflict.hidden) { ctx.setDirty(false); reload(); }
    });
    editPanel.replaceChildren(h('div', { class: 'panel-head' }, h('h2', { class: 'small', id: 'edit-h' }, 'Edit task')), form);
    editPanel.hidden = false;
    form.querySelector('#e-title').focus();
  }

  // ---- side ----------------------------------------------------------------------
  const side = h('aside', { class: 'side', 'aria-label': 'Task details' },
    h('section', { class: 'panel' }, h('div', { class: 'panel-body' }, h('dl', { class: 'kv' },
      h('dt', null, 'Status'), h('dd', null, statusChip(t.status)),
      t.priority !== undefined && t.priority !== null ? [h('dt', null, 'Priority'), h('dd', null, priority(t.priority))] : null,
      h('dt', null, 'Assignee'), h('dd', null, t.assignee_name || h('span', { class: 'muted' }, 'Unclaimed')),
      h('dt', null, 'Created'), h('dd', null, time(t.created_at)),
      t.updated_at ? [h('dt', null, 'Updated'), h('dd', null, time(t.updated_at))] : null,
      h('dt', null, 'ID'), h('dd', null, h('code', null, t.id)),
      t.version !== undefined ? [h('dt', null, 'Version'), h('dd', { class: 'num' }, String(t.version))] : null))),
    brief.depends_on && brief.depends_on.length ? h('section', { class: 'panel' },
      h('div', { class: 'panel-head' }, h('h2', { class: 'small' }, 'Depends on')),
      h('ul', { class: 'panel-body open-items' }, brief.depends_on.map((d) => h('li', null, h('a', { href: ctx.href(`/p/${pid}/t/${d.id}`) }, d.title), ' ', statusChip(d.status))))) : null,
    h('p', { class: 'small muted' }, 'Viewing a task never marks it read, done or acknowledged.'));

  return h('div', { class: 'stack' },
    pageHead({ crumbs: [{ label: 'Projects', href: ctx.href('/projects') }, { label: project.name, href: ctx.href('/p/' + pid) }, { label: t.id }], title: t.title, actions }),
    h('div', { class: 'toolbar' }, statusChip(t.status), reviewChip(review.state), priority(t.priority)),
    lifecycleStrip(brief.lifecycle),
    nextBanner,
    h('div', { class: 'detail' }, h('div', { class: 'stack' }, editPanel, briefPanel, reviewPanel, historyPanel), side));
}

// Review bodies carry the canonical fields (the contribution id being judged and the
// latest review record as ``previous``), so a stale page is refused with a 409 instead
// of judging a revision the reviewer never saw.
export function reviewActions(ctx, pid, tid, contribution, review) {
  const target = { contribution: contribution.id, previous: review.latest_id ?? null, contribution_revision: contribution.revision, contribution_commit: contribution.commit };
  const status = h('div', { class: 'banner crit', role: 'alert', hidden: true });
  const form = h('form', { class: 'form', novalidate: true },
    h('h3', { class: 'small' }, `Review revision ${contribution.revision}`),
    status,
    field({ id: 'r-items', label: 'Requested changes', type: 'textarea', rows: 4, hint: 'One change per line. Leave empty to approve.' }),
    h('div', { class: 'actions' },
      h('button', { type: 'submit', name: 'request', class: '' }, 'Request changes'),
      h('button', { type: 'button', class: 'primary', onclick: async (e) => {
        if (!(await confirmDialog({ title: `Approve revision ${contribution.revision}?`, body: `Commit ${shortSha(contribution.commit)} will be marked reviewed and approved if the server accepts it; a server set to require another party refuses an approval of your own party's work. It is not integrated or deployed until those steps are recorded separately.`, confirmLabel: 'Approve' }))) return;
        await submit(e.currentTarget, { operation: 'approve', ...target, summary: `Approved revision ${contribution.revision} in the web interface` }, 'Approved');
      } }, 'Approve')));
  async function submit(button, body, message) {
    try {
      await act(button, () => ctx.api.review(pid, tid, body), { success: message, onError: (error) => {
        // A refusal that says who must act instead (403: the approver's own party delivered it, or holds
        // the task) stays on the page; as a toast it was gone in seconds (kittrial-5bb.199 review).
        if (error.status === 403) { status.replaceChildren(error.message || 'Not permitted.'); status.setAttribute('data-refused', '403'); status.hidden = false; return true; }
        if (error.status !== 409) return false;
        status.replaceChildren('A newer revision arrived or someone else already reviewed this. ', h('button', { type: 'button', class: 'link', onclick: () => ctx.render() }, 'Reload to see it'), '.');
        status.hidden = false; return true;
      } });
    } catch { return; }
    if (status.hidden) { ctx.setDirty(false); ctx.render(); }
  }
  form.addEventListener('input', () => ctx.setDirty(true));
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const items = formValues(form)['r-items'].split('\n').map((s) => s.trim()).filter(Boolean);
    if (!items.length) return setFieldError(form, 'r-items', 'Describe at least one change, or use Approve.');
    setFieldError(form, 'r-items', '');
    await submit(form.querySelector('button[name=request]'), { operation: 'request-changes', ...target, items: items.map((text, i) => ({ id: 'item-' + (i + 1), text })) }, 'Changes requested');
  });
  return form;
}

// What a reviewer recommended for the current revision. Shown as advice: the owner's
// decision is still to make, and the page says so.
function recommendationNote(review, advice) {
  const others = (review.recommendations || []).filter((r) => r.id !== advice.id).map((r) => r.author_name || r.author);
  // One child, so the banner's row layout does not split the sentence into columns.
  return h('div', { class: 'banner', role: 'note', id: 'recommendation' }, h('div', null,
    h('p', null, h('strong', null, 'Recommended for approval'), ' by ', advice.author_name || advice.author, ' ', time(advice.at),
      ' for commit ', h('code', { title: advice.commit }, shortSha(advice.commit)), '. An owner still decides.'),
    h('p', { class: 'prose' }, advice.summary),
    advice.items && advice.items.length ? h('ul', null, advice.items.map((item) => h('li', null, item.text))) : null,
    others.length ? h('p', { class: 'small muted' }, 'Also recommended by ', others.join(', '), '.') : null));
}

// A member who can review but cannot approve records a recommendation for the owner.
// It names the contribution and commit on the page, so a newer revision is refused (409).
function recommendForm(ctx, pid, tid, contribution) {
  const status = h('div', { class: 'banner crit', role: 'alert', hidden: true });
  const form = h('form', { class: 'form', novalidate: true },
    h('h3', { class: 'small' }, `Recommend revision ${contribution.revision} for approval`),
    status,
    field({ id: 'rec-summary', label: 'What you checked', type: 'textarea', rows: 3, required: true, maxlength: 1200, hint: 'The owner reads this before deciding. Say what you checked and what you did not.' }),
    field({ id: 'rec-items', label: 'Notes for the owner', type: 'textarea', rows: 3, hint: 'Optional. One note per line. To ask for changes, an owner requests them.' }),
    h('div', null, h('button', { type: 'submit', class: 'primary' }, 'Recommend approval')));
  form.addEventListener('input', () => ctx.setDirty(true));
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const values = formValues(form);
    const summary = values['rec-summary'].trim();
    if (!summary) return setFieldError(form, 'rec-summary', 'Say what you checked.');
    setFieldError(form, 'rec-summary', '');
    const items = values['rec-items'].split('\n').map((s) => s.trim()).filter(Boolean).map((text, i) => ({ id: 'note-' + (i + 1), text }));
    const body = { operation: 'recommend', contribution: contribution.id, commit: contribution.commit, verdict: 'approve', summary, items };
    try {
      await act(form.querySelector('button[type=submit]'), () => ctx.api.review(pid, tid, body), { success: 'Recommendation recorded', onError: (error) => {
        if (error.status !== 409) return false;
        status.replaceChildren('A newer revision arrived or the contribution was already reviewed. ', h('button', { type: 'button', class: 'link', onclick: () => { ctx.setDirty(false); ctx.render(); } }, 'Reload to see it'), '.');
        status.hidden = false; return true;
      } });
    } catch { return; }
    if (status.hidden) { ctx.setDirty(false); ctx.render(); }
  });
  return form;
}

// Set after a revision is delivered while requests are still open, so the re-rendered
// page leads with the respond step for exactly that task.
let respondPrompt = null;

// The review "respond" step: the assignee marks each open requested change resolved
// with a short note. It is one canonical ``respond`` record naming the request record
// and item of each resolution, plus shared evidence (by default the current revision).
function respondForm(ctx, pid, tid, contribution, review, open, delivered) {
  const prompted = respondPrompt === tid;
  respondPrompt = null;
  const status = h('div', { class: 'banner crit', role: 'alert', hidden: true });
  const more = (review.open_requests || 0) - open.length;
  const evidence = `Revision ${contribution.revision}, commit ${contribution.commit}${contribution.branch ? ` on ${contribution.branch}` : ''}`;
  const rows = open.map((r, i) => h('li', { class: 'respond-item' },
    h('label', { class: 'check', for: `rs-${i}` }, h('input', { type: 'checkbox', id: `rs-${i}`, name: `rs-${i}`, checked: true }), ' Resolved: ', h('span', null, r.text)),
    field({ id: `rn-${i}`, label: 'Note for the reviewer', maxlength: 1000, placeholder: 'What you changed, or why no change is needed' })));
  const form = h('form', { class: 'form respond', novalidate: true, 'aria-labelledby': 'respond-h' },
    h('h3', { class: 'small', id: 'respond-h' }, 'Respond to the requested changes'),
    h('div', { class: 'banner' + (prompted ? ' info' : ''), role: prompted ? 'status' : 'note' },
      delivered ? `You delivered revision ${contribution.revision}. The ${open.length} request(s) below stay open until you respond: mark each one resolved with a short note.`
        : 'Deliver a revision that addresses these first, or mark a request resolved now if it needs no change. Each stays open until you respond.'),
    status,
    h('ol', { class: 'request-list' }, rows),
    more > 0 ? h('p', { class: 'small muted' }, `${more} more open request(s) are not shown; respond to these first, then the rest appear.`) : null,
    field({ id: 'r-evidence', label: 'Evidence', value: evidence, maxlength: 1000, hint: 'Where the reviewer can check the resolutions. Defaults to the current revision.' }),
    h('div', null, h('button', { type: 'submit', class: 'primary' }, 'Record response')));
  form.addEventListener('input', () => ctx.setDirty(true));
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const v = formValues(form);
    const proof = (v['r-evidence'] || '').trim();
    if (!proof) return setFieldError(form, 'r-evidence', 'Say where the reviewer can check this.');
    setFieldError(form, 'r-evidence', '');
    const resolutions = [];
    for (let i = 0; i < open.length; i += 1) {
      if (!form.querySelector(`#rs-${i}`).checked) continue;
      const note = (v[`rn-${i}`] || '').trim();
      if (!note) return setFieldError(form, `rn-${i}`, 'Add a short note, or untick this request.');
      setFieldError(form, `rn-${i}`, '');
      resolutions.push({ request: open[i].request, item: open[i].id, reason: note, evidence: proof });
    }
    if (!resolutions.length) { status.replaceChildren('Tick at least one request to resolve.'); status.hidden = false; return; }
    status.hidden = true;
    const body = { operation: 'respond', contribution: contribution.id, previous: review.latest_id ?? null, resolutions };
    try {
      await act(form.querySelector('button[type=submit]'), () => ctx.api.review(pid, tid, body), { success: resolutions.length === 1 ? 'Response recorded' : `${resolutions.length} responses recorded`, onError: (error) => {
        if (error.status !== 409) return false;
        status.replaceChildren('The task changed since you opened it (a newer revision or review, or a request already resolved). ', h('button', { type: 'button', class: 'link', onclick: () => { ctx.setDirty(false); ctx.render(); } }, 'Reload to see it'), '.');
        status.hidden = false; return true;
      } });
    } catch { return; }
    if (status.hidden) { ctx.setDirty(false); ctx.render(); }
  });
  // After the page render has scrolled to the top and focused the title.
  if (prompted) setTimeout(() => { form.scrollIntoView({ block: 'start' }); const first = form.querySelector('#rn-0'); if (first) first.focus(); });
  return form;
}

function deliverForm(ctx, pid, tid, previous, openCount, review, delivered) {
  // Once a revision answering the open requests is in, the respond step leads and this
  // form stays folded away.
  const details = h('details', { class: 'deliver', open: (openCount > 0 && !delivered) || undefined });
  const form = h('form', { class: 'form', novalidate: true },
    openCount ? h('p', { class: 'small muted' }, `This revision should address the ${openCount} open request(s) above. After delivering it, respond to each request.`) : null,
    h('div', { class: 'form-row' },
      field({ id: 'd-repository', label: 'Repository', value: previous && previous.repository ? previous.repository : '', placeholder: 'git@host:team/repo.git', hint: 'Where the reviewer fetches your branch.' }),
      field({ id: 'd-branch', label: 'Branch', value: previous && previous.branch ? previous.branch : '', placeholder: 'contrib/…' })),
    h('div', { class: 'form-row' },
      field({ id: 'd-commit', label: 'Commit (full hash)', placeholder: '40 hex characters', maxlength: 40 }),
      field({ id: 'd-base', label: 'Based on (full hash)', placeholder: 'the commit you started from', maxlength: 40 })),
    field({ id: 'd-summary', label: 'What changed', type: 'textarea', rows: 4, hint: 'Summarise the change and how you tested it.' }),
    h('div', null, h('button', { type: 'submit', class: 'primary' }, previous ? `Deliver revision ${previous.revision + 1}` : 'Deliver for review')));
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const v = formValues(form);
    const repository = v['d-repository'].trim();
    const branch = v['d-branch'].trim();
    if (!repository) return setFieldError(form, 'd-repository', 'Say where the reviewer can fetch the work.');
    setFieldError(form, 'd-repository', '');
    if (!branch) return setFieldError(form, 'd-branch', 'Name the branch that holds the work.');
    setFieldError(form, 'd-branch', '');
    const commit = v['d-commit'].trim().toLowerCase();
    if (!/^[0-9a-f]{40}$/.test(commit)) return setFieldError(form, 'd-commit', 'Paste the full 40-character commit hash.');
    setFieldError(form, 'd-commit', '');
    const base = v['d-base'].trim().toLowerCase();
    if (!/^[0-9a-f]{40}$/.test(base)) return setFieldError(form, 'd-base', 'Paste the full 40-character hash of the commit you started from.');
    setFieldError(form, 'd-base', '');
    if (!v['d-summary'].trim()) return setFieldError(form, 'd-summary', 'Summarise what changed.');
    setFieldError(form, 'd-summary', '');
    const body = {
      operation: 'contribute', repository, commit, base_commit: base, branch, summary: v['d-summary'].trim(),
      delivery: { kind: 'remote', remote: repository, branch },
      supersedes: previous ? previous.id ?? null : null, previous: review.latest_id ?? null,
    };
    const done = await act(form.querySelector('button[type=submit]'), () => ctx.api.review(pid, tid, body), { success: openCount ? 'Delivered. Now respond to the open request(s).' : 'Delivered for review' }).catch(() => null);
    if (done) {
      if (openCount) respondPrompt = tid;
      ctx.render();
    }
  });
  details.append(h('summary', null, previous ? 'Deliver a revision' : 'Deliver your contribution'), form);
  return details;
}
