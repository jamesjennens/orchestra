import { h, time, confirmDialog, shortSha, toast } from '../dom.js';
import { pageHead, reviewChip, statusChip, priority, empty, field, setFieldError, formValues, act, lifecycleStrip, describe, errorState } from '../ui.js';
import { canWrite, isOwner } from './project.js';

const ACTIONS = {
  'task-created': ['Created the task', ''], 'task-claimed': ['Claimed the task', 'accent'], 'task-updated': ['Edited the task', ''],
  'contribution': ['Delivered a contribution', 'accent'], 'change-requested': ['Requested a change', 'warn'], 'change-resolved': ['Change resolved', 'ok'],
  'review-approve': ['Approved', 'ok'], 'review-request-changes': ['Requested changes', 'warn'],
};

export async function create(ctx, { pid }) {
  const project = await ctx.api.project(pid);
  if (!canWrite(project) || project.archived) {
    return h('div', { class: 'stack' }, pageHead({ title: 'New task' }), h('div', { class: 'banner crit', role: 'alert' }, project.archived ? 'This project is archived.' : 'Viewers cannot create tasks. Ask a project owner for contributor access.'));
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
  const review = brief.review;
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
  if (owner && c && review.state === 'awaiting-review' && !project.archived) reviewBody.append(reviewActions(ctx, pid, tid, c));
  if (mine && writer && ['none', 'changes-requested'].includes(review.state) && t.status !== 'closed') reviewBody.append(deliverForm(ctx, pid, tid, c, openRequests.length));
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
      const [label, tone] = ACTIONS[e.action] || [e.action, ''];
      historyList.append(h('li', null, h('span', { class: 'dot ' + tone, 'aria-hidden': 'true' }),
        h('div', null, h('div', { class: 'event-head' }, h('strong', null, e.user_name || e.user_id), h('span', null, label), time(e.time)), e.detail ? h('p', { class: 'event-body prose' }, e.detail) : null)));
    }
    if (!page.items.length && !cursor) historyList.append(h('li', null, h('span'), h('p', { class: 'muted' }, 'No history yet.')));
    cursor = page.next_cursor; more.hidden = !cursor;
  }
  more.addEventListener('click', loadHistory);
  loadHistory();
  const historyPanel = h('section', { class: 'panel', 'aria-labelledby': 'hist-h' },
    h('div', { class: 'panel-head' }, h('h2', { class: 'small', id: 'hist-h' }, 'History'), h('span', { class: 'small muted' }, 'Newest first')),
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
      h('dt', null, 'Priority'), h('dd', null, priority(t.priority)),
      h('dt', null, 'Assignee'), h('dd', null, t.assignee_name || h('span', { class: 'muted' }, 'Unclaimed')),
      h('dt', null, 'Created'), h('dd', null, time(t.created_at)),
      h('dt', null, 'Updated'), h('dd', null, time(t.updated_at)),
      h('dt', null, 'ID'), h('dd', null, h('code', null, t.id)),
      h('dt', null, 'Version'), h('dd', { class: 'num' }, String(t.version))))),
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

function reviewActions(ctx, pid, tid, contribution) {
  const status = h('div', { class: 'banner crit', role: 'alert', hidden: true });
  const form = h('form', { class: 'form', novalidate: true },
    h('h3', { class: 'small' }, `Review revision ${contribution.revision}`),
    status,
    field({ id: 'r-items', label: 'Requested changes', type: 'textarea', rows: 4, hint: 'One change per line. Leave empty to approve.' }),
    h('div', { class: 'actions' },
      h('button', { type: 'submit', name: 'request', class: '' }, 'Request changes'),
      h('button', { type: 'button', class: 'primary', onclick: async (e) => {
        if (!(await confirmDialog({ title: `Approve revision ${contribution.revision}?`, body: `Commit ${shortSha(contribution.commit)} will be marked reviewed and approved. It is not integrated or deployed until those steps are recorded separately.`, confirmLabel: 'Approve' }))) return;
        await submit(e.currentTarget, { operation: 'approve', contribution_revision: contribution.revision, contribution_commit: contribution.commit }, 'Approved');
      } }, 'Approve')));
  async function submit(button, body, message) {
    try {
      await act(button, () => ctx.api.review(pid, tid, body), { success: message, onError: (error) => {
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
    await submit(form.querySelector('button[name=request]'), { operation: 'request-changes', contribution_revision: contribution.revision, contribution_commit: contribution.commit, items }, 'Changes requested');
  });
  return form;
}

function deliverForm(ctx, pid, tid, previous, openCount) {
  const details = h('details', { class: 'deliver', open: openCount > 0 || undefined });
  const form = h('form', { class: 'form', novalidate: true },
    openCount ? h('p', { class: 'small muted' }, `This revision should address the ${openCount} open request(s) above.`) : null,
    h('div', { class: 'form-row' },
      field({ id: 'd-branch', label: 'Branch', value: previous && previous.branch ? previous.branch : '', placeholder: 'contrib/…' }),
      field({ id: 'd-commit', label: 'Commit (full hash)', placeholder: '40 hex characters', maxlength: 40 })),
    field({ id: 'd-summary', label: 'What changed', type: 'textarea', rows: 4, hint: 'Summarise the change and how you tested it.' }),
    h('div', null, h('button', { type: 'submit', class: 'primary' }, previous ? `Deliver revision ${previous.revision + 1}` : 'Deliver for review')));
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const v = formValues(form);
    const commit = v['d-commit'].trim().toLowerCase();
    if (!/^[0-9a-f]{40}$/.test(commit)) return setFieldError(form, 'd-commit', 'Paste the full 40-character commit hash.');
    setFieldError(form, 'd-commit', '');
    if (!v['d-summary'].trim()) return setFieldError(form, 'd-summary', 'Summarise what changed.');
    setFieldError(form, 'd-summary', '');
    const done = await act(form.querySelector('button[type=submit]'), () => ctx.api.review(pid, tid, { operation: 'contribute', commit, branch: v['d-branch'].trim() || null, summary: v['d-summary'].trim() }), { success: 'Delivered for review' }).catch(() => null);
    if (done) ctx.render();
  });
  details.append(h('summary', null, previous ? 'Deliver a revision' : 'Deliver your contribution'), form);
  return details;
}
