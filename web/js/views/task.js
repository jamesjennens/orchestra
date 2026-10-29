import { h, time, confirmDialog, shortSha, toast } from '../dom.js';
import { markdown } from '../md.js';
import { pageHead, reviewChip, statusChip, priority, empty, field, setFieldError, formValues, act, describe, errorState } from '../ui.js';
import { canWrite, canApprove, isOwner } from './project.js';
import { lifecyclePanel } from './lifecycle.js';
import { taskFilesPanel } from './taskFiles.js';
import { childForm } from './children.js';

const ACTIONS = {
  'task-created': ['Created the task', ''], 'task-claimed': ['Claimed the task', 'accent'], 'task-updated': ['Edited the task', ''],
  'contribution': ['Delivered a contribution', 'accent'], 'change-requested': ['Requested a change', 'warn'], 'change-resolved': ['Change resolved', 'ok'],
  'review-approve': ['Approved', 'ok'], 'review-request-changes': ['Requested changes', 'warn'],
  'approve': ['Approved', 'ok'], 'request-changes': ['Requested changes', 'warn'], 'respond': ['Responded to review', ''], 'recommend': ['Recommended approval', ''],
  'checkpoint-added': ['Recorded a checkpoint', ''], 'checkpoint': ['Recorded a checkpoint', ''], 'comment': ['Commented', ''],
};

export function contextPointers(item) {
  const pointers = Array.isArray(item.pointers) ? item.pointers : [];
  if (!pointers.length) return null;
  return h('ul', { class: 'small', 'data-context-pointers': 'true' },
    pointers.map(p => h('li', null, h('code', null, p.pointer), ' — ', p.state,
      Array.isArray(p.flags) && p.flags.length ? [' — flags: ', p.flags.join(', ')] : null,
      p.section && typeof p.section === 'object' ? [' — section ', String(p.section.anchor), ' ', String(p.section.state)] : null,
      ' (untrusted data)', p.label ? [' ', p.label.text] : null,
      p.follow ? h('div', null, 'Follow: ', h('code', null, p.follow)) : null)),
    item.pointers_omitted ? h('li', null, String(item.pointers_omitted), ' pointers unresolved by bounds.') : null);
}

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
  // Who decides on a contribution here: an owner or a coordinator (kittrial-5bb.209).
  const owner = canApprove(project);
  const writer = canWrite(project) && !project.archived;
  const mine = t.assignee === ctx.me.id;
  const reload = () => ctx.render();

  const actions = [];
  // Claim only what the server calls claimable (kittrial-5bb.277 review item 2): a
  // requirement record, an epic, a decision or the "Accept requirement" row arrives with
  // claimable false, and the claim route refuses it with 409. `!== false` keeps the
  // button for a server that does not send the field yet.
  if (writer && !t.assignee && t.status !== 'closed' && t.claimable !== false) {
    actions.push(h('button', { type: 'button', class: 'primary', onclick: async (e) => {
      try { await act(e.currentTarget, () => ctx.api.claim(pid, tid), { success: 'You have claimed this task' }); } catch (error) { if (error.status === 409) reload(); return; }
      reload();
    } }, 'Claim task'));
  }
  if (writer) actions.push(h('button', { type: 'button', onclick: () => editPanel.hidden ? openEdit() : null }, 'Edit'));

  // ---- next action (summary strip; the sentence a reader needs is first) ------
  // t.next_action.who says which party acts (owner/assignee/anyone); the strip names
  // the party (kittrial-5bb.411 slice 2: mock-up web-ux/task.html).
  const next = t.next_action;
  const whoName = t.assignee_name || 'the assignee';
  const whoActs = next
    ? next.who === 'owner'
      ? (owner ? (isOwner(project) ? 'you (project owner)' : 'you (coordinator)') : 'a project owner or coordinator')
      : next.who === 'assignee' ? (mine ? 'you' : whoName) : 'any contributor'
    : null;
  const summaryStrip = h('section', { class: 'summary', 'aria-label': 'Where this task stands' },
    h('div', { class: 'chips' }, statusChip(t.status), reviewChip(review.state),
      t.priority !== undefined && t.priority !== null ? h('span', { class: 'chip plain', title: 'Priority' }, 'P' + t.priority) : null),
    h('div', { class: 'meta' },
      t.updated_at ? h('span', null, 'Updated ', time(t.updated_at)) : null,
      h('span', { class: 'mono' }, t.id)),
    next ? h('p', { class: 'next' }, h('b', null, 'Next: '), next.text, ' ',
      h('span', { class: 'who-acts' + (whoActs === 'you' || whoActs === 'you (project owner)' || whoActs === 'you (coordinator)' ? ' you' : '') }, whoActs)) : null);
  // The integration-evidence link stays one banner below the strip for approved work.
  // The facts panel starts hidden inside the closed Owner-tools fold (and until the
  // lifecycle read lands), so the link opens the fold first (b1.3): otherwise the
  // hash jump lands on a hidden node and nothing visibly happens.
  let ownerToolsFold = null;
  const evidenceLink = h('a', { href: '#lifecycle-facts' }, 'Record integration evidence');
  evidenceLink.addEventListener('click', () => { if (ownerToolsFold) ownerToolsFold.open = true; });
  const nextBanner = (next && ctx.api.lifecycle && ['approved', 'awaiting-integration'].includes(t.review_state))
    ? h('div', { class: 'banner info' }, evidenceLink) : null;

  // ---- brief -------------------------------------------------------------------
  const cp = brief.checkpoint;
  // checkpoint.open_items is only the first page the brief shows; carry_open_items is
  // the complete list, in order (kittrial-5bb.356). A null carry list means the server
  // could not read all of them: show what it has and offer a reload rather than let
  // the first page read as the whole list.
  const carry = Array.isArray(brief.carry_open_items) ? brief.carry_open_items : null;
  const shownItems = cp && Array.isArray(cp.open_items) ? cp.open_items : [];
  const pointerItems = new Map(Object.entries((cp && cp.pointer_items) || {}));
  const resolvedPointers = new Map(Object.entries((cp && cp.resolved_pointer_items) || {}));
  const openItems = carry || shownItems;
  const openTotal = cp && Number.isInteger(cp.open_items_total) ? cp.open_items_total : openItems.length;
  const incomplete = brief.carry_open_items === null;
  const briefPanel = h('section', { class: 'panel', 'aria-labelledby': 'brief-h' },
    h('div', { class: 'panel-head' }, h('h2', { class: 'small', id: 'brief-h' }, 'Brief'), cp ? h('span', { class: 'small muted' }, 'Checkpoint ', time(cp.at), ' by ', cp.author_name || cp.author) : null),
    h('div', { class: 'panel-body' },
      // Slice 2: the description moves to the "What is wanted" panel above; the
      // checkpoint keeps its own panel (summary, next step, carried items).
      cp ? [h('h3', { class: 'small' }, 'Where it stands'), h('p', { class: 'prose' }, cp.summary), cp.next_action ? h('p', null, h('strong', null, 'Next step: '), cp.next_action) : null] : null,
      cp ? changesSinceCheckpoint(brief.changes_since_checkpoint) : null,
      cp && openItems.length ? [h('h3', { class: 'small' }, 'Unresolved items ', h('span', { class: 'nav-count' }, String(openTotal))),
        h('ul', { class: 'open-items' }, openItems.map((o) => h('li', null, h('span', { class: 'chip plain' }, o.kind), ' ', o.text,
          h('div', { class: 'small muted' }, 'Source: ', o.source), contextPointers(pointerItems.get(o.id) || {}))))] : null,
      cp && Array.isArray(cp.resolved) && cp.resolved.length ? [h('h3', { class: 'small' }, 'Resolved items'),
        h('ul', null, cp.resolved.map(o => h('li', null, o.id, ': ', o.reason,
          h('div', { class: 'small muted' }, 'Evidence: ', o.evidence), contextPointers(resolvedPointers.get(o.id) || {})))),
        cp.resolved_omitted ? h('p', { class: 'small muted' }, String(cp.resolved_omitted), ' more resolved items are not shown.') : null] : null,
      incomplete ? h('div', { class: 'banner', role: 'note' }, 'The complete list of unresolved items could not all be read. ',
        h('button', { type: 'button', class: 'link', onclick: () => reload() }, 'Reload the task'), ' to try again.') : null));

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
  // History is oldest first, so the next page holds NEWER entries: the button says so.
  // A long comment is several entries sharing entry_id, each with body_offset and
  // continued (kittrial-5bb.356): join them into one item, fetching the following page
  // while the last fragment still says more of the same body follows.
  const historyList = h('ol', { class: 'timeline' });
  const more = h('button', { type: 'button', hidden: true }, 'Show newer');
  let cursor = null;
  let loading = false;
  const entries = [];
  const KIND_VERB = {
    'task-created': 'created the task', 'task-claimed': 'claimed the task',
    'task-updated': 'edited the task',
    'contribution': 'delivered a contribution', 'change-requested': 'requested a change',
    'change-resolved': 'resolved a change',
    'review-approve': 'approved', 'review-request-changes': 'requested changes',
    'approve': 'approved', 'request-changes': 'requested changes',
    'respond': 'responded to review', 'recommend': 'recommended approval',
    'checkpoint-added': 'recorded a checkpoint', 'checkpoint': 'recorded a checkpoint',
    'comment': 'commented',
  };
  // A non-body history event reads as the ACTIONS label for its action (the old code
  // did: kittrial-5bb.411 review item b1.1). Unknown keys fall back to the raw key so
  // a new server action still reads as something instead of 'Event'.
  // A history entry's [object Object] author is an author id or object rendered as
  // text (WEB_UX_REVIEW s2.2): prefer the name the server resolved for it.
  function entryName(e) {
    const direct = [e.user_name, e.author_name, e.assignee_name, e.actor_name].find((v) => typeof v === 'string' && v);
    if (direct) return direct;
    for (const key of ['author', 'actor', 'user', 'user_id']) {
      const v = e[key];
      if (typeof v === 'string' && v) return v;
      if (v && typeof v === 'object' && typeof v.text === 'string' && v.text) return v.text;
    }
    return 'Someone';
  }
  const byKindEntry = new Map();
  function addEntry(e) {
    if (typeof e.entry_id === 'string' && e.entry_id && typeof e.body === 'string') {
      let target = byKindEntry.get(e.entry_id);
      if (!target) { target = { ...e, body: '' }; entries.push(target); byKindEntry.set(e.entry_id, target); }
      const offset = Number.isInteger(e.body_offset) ? e.body_offset : target.body.length;
      // A page boundary may repeat no text but must never drop any: add only the part
      // of this fragment past what the joined body already holds.
      target.body += offset >= target.body.length ? e.body : e.body.slice(target.body.length - offset);
      target.continued = Boolean(e.continued);
      return;
    }
    entries.push({ ...e });
  }
  function drawHistory() {
    historyList.replaceChildren(...entries.map((e) => {
      // Activity: comments read as comments; machine records as one-line record
      // events with the raw JSON folded (WEB_UX_REVIEW s2.2).
      // A non-body history event reads as the ACTIONS label (b1.1: the old code
      // did); unknown actions fall back to the raw key so a new server action
      // still reads as something instead of 'Event'.
      const who = entryName(e);
      const when = h('span', { class: 'small muted' }, (e.action ? '' : ' · '), time(e.time || e.timestamp));
      const kindVerb = e.action ? e.action : (KIND_VERB[e.kind] || e.kind);
      if (typeof e.body === 'string') {
        return h('li', null, h('span', { class: 'dot', 'aria-hidden': 'true' }),
          h('div', null, h('div', { class: 'event-head' }, h('strong', null, who), h('span', null, ' ', kindVerb), ' ', when),
            h('p', { class: 'event-body prose' }, e.body)));
      }
      const tone = (ACTIONS[e.action || ''] || [])[1] || '';
      const label = e.action ? ((ACTIONS[e.action] || [])[0] || e.action) : (kindVerb || 'Event');
      const detail = e.detail;
      let raw = null;
      try { raw = JSON.stringify(e, null, 2); } catch { raw = String(e.kind || e.action || 'record'); }
      return h('li', null, h('span', { class: 'dot ' + tone, 'aria-hidden': 'true' }),
        h('div', { class: 'record-event' }, h('div', { class: 'event-head' }, h('strong', null, who), h('span', null, ' ', label), ' ', when),
          detail ? h('p', null, String(detail)) : null,
          h('details', null, h('summary', null, 'Show the raw record'), h('pre', null, raw))));
    }));
    if (!entries.length) historyList.append(h('li', null, h('span'), h('p', { class: 'muted' }, 'No history yet.')));
  }
  async function loadHistory() {
    if (loading) return;
    loading = true;
    try {
      let page;
      try { page = await ctx.api.history(pid, tid, { limit: 20, cursor }); } catch (error) { historyList.append(h('li', null, h('span'), h('p', { class: 'muted' }, describe(error)))); return; }
      for (const e of page.items) addEntry(e);
      cursor = page.next_cursor;
      // The last fragment is continued: the rest of that body is on the next page.
      let guard = 0;
      while (entries.length && entries[entries.length - 1].continued && cursor && guard < 50) {
        guard += 1;
        try { page = await ctx.api.history(pid, tid, { limit: 20, cursor }); } catch (error) { break; }
        for (const e of page.items) addEntry(e);
        cursor = page.next_cursor;
      }
      drawHistory();
      more.hidden = !cursor;
    } finally { loading = false; }
  }
  // Activity first: comments read as comments, machine records as one-line
  // record events with the raw JSON folded (WEB_UX_REVIEW s2.2).
  const activityPanel = h('section', { class: 'panel', 'aria-labelledby': 'activity-h' },
    h('div', { class: 'panel-head' }, h('h2', { id: 'activity-h' }, 'Activity')),
    h('div', { class: 'panel-body' }, historyList, h('div', null, more)));
  more.addEventListener('click', loadHistory);
  loadHistory();

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
  // Slice 2 side column (mock-up web-ux/task.html): Details, one-line completion
  // facts until any is known, Files, then the Owner tools fold.
  const FACT_PAIRS = [
    ['implemented', 'Implemented'], ['tested', 'Tested'], ['reviewed', 'Reviewed'],
    ['integrated', 'Integrated'], ['deployed', 'Deployed'], ['live-verified', 'Live-verified'],
  ];
  const knownFacts = brief.lifecycle
    ? FACT_PAIRS.filter(([key]) => brief.lifecycle[key] && brief.lifecycle[key].value && brief.lifecycle[key].value !== 'unknown')
    : [];
  const completionLine = knownFacts.length
    ? knownFacts.map(([key, label]) => `${label}: ${brief.lifecycle[key].value}`).join(' · ')
    : 'None recorded yet. They are recorded against a contribution once one is delivered.';
  const side = h('aside', { class: 'side', 'aria-label': 'Task facts and tools' },
    h('section', { class: 'panel' }, h('div', { class: 'panel-head' }, h('h2', { class: 'small' }, 'Details')),
      h('div', { class: 'panel-body' }, h('dl', { class: 'kv' },
        h('dt', null, 'Status'), h('dd', null, statusChip(t.status)),
        t.priority !== undefined && t.priority !== null ? [h('dt', null, 'Priority'), h('dd', null, priority(t.priority))] : null,
        h('dt', null, 'Assignee'), h('dd', null, t.assignee_name || h('span', { class: 'muted' }, 'Unclaimed')),
        h('dt', null, 'Created'), h('dd', null, time(t.created_at)),
        t.updated_at ? [h('dt', null, 'Updated'), h('dd', null, time(t.updated_at))] : null,
        h('dt', null, 'ID'), h('dd', { class: 'mono' }, t.id),
        t.version !== undefined ? [h('dt', null, 'Version'), h('dd', { class: 'num' }, String(t.version))] : null))),
    h('section', { class: 'panel' }, h('div', { class: 'panel-head' }, h('h2', { class: 'small' }, 'Completion facts')),
      h('div', { class: 'panel-body' }, h('p', { class: knownFacts.length ? 'small' : 'small muted' }, completionLine))),
    brief.depends_on && brief.depends_on.length ? h('section', { class: 'panel' },
      h('div', { class: 'panel-head' }, h('h2', { class: 'small' }, 'Depends on')),
      h('ul', { class: 'panel-body open-items' }, brief.depends_on.map((d) => h('li', null, h('a', { href: ctx.href(`/p/${pid}/t/${d.id}`) }, d.title), ' ', statusChip(d.status))))) : null,
    taskFilesPanel(ctx, { pid, tid, task: t, project }),
    // kittrial-5bb.434: a coordinator adds a child task or a decision here (null for everybody else).
    childForm(ctx, project, t, (made) => { if (made && made.id) ctx.go(`/p/${pid}/t/${made.id}`); else reload(); }),
    (ownerToolsFold =h('details', { class: 'fold' }, h('summary', null, 'Owner tools'),
      h('div', { class: 'fold-body' },
        brief.lifecycle ? h('div', { id: 'lifecycle-facts' }, lifecyclePanel(ctx, project, t)) : null,
        owner && !project.archived ? assignmentForm(ctx, pid, tid, t) : null,
        brief.assignment_notices ? h('div', { class: 'banner info' }, brief.assignment_notices.items.map(item => h('p', { class: 'prose' }, item.message))) : null))),
    h('p', { class: 'small muted' }, 'Viewing a task never marks it read, done or acknowledged.'));

  // ---- main column + checkpoint fold + activity --------------------------------
  // Checkpoint form folded until wanted; the Activity panel shows comments first
  // and machine records as one-line events with the raw JSON folded.
  const checkpointFold = writer
    ? h('details', { class: 'fold' }, h('summary', null, 'Record a checkpoint ', h('span', { class: 'fold-hint' }, t.assignee_name ? `for ${t.assignee_name}, the assignee` : 'for the assignee')),
      h('div', { class: 'fold-body' }, checkpointForm(ctx, pid, tid, brief))) : null;
  const wantedPanel = h('section', { class: 'panel', 'aria-labelledby': 'wanted-h' },
    h('div', { class: 'panel-head' }, h('h2', { id: 'wanted-h' }, 'What is wanted')),
    h('div', { class: 'panel-body md' }, markdown(t.description || '')));
  return h('div', { class: 'stack' },
    pageHead({ crumbs: [{ label: 'Projects', href: ctx.href('/projects') }, { label: project.name, href: ctx.href('/p/' + pid) }, { label: t.id }], title: t.title, actions }),
    summaryStrip,
    // kittrial-5bb.425 part 2: the task's own req: and design: tokens, resolved by the server (TRACEABILITY_DESIGN §5.2).
    brief.description_pointers ? h('section', { class: 'panel', 'aria-label': 'What this task cites', 'data-description-pointers': '1' },
      h('h3', { class: 'small' }, 'Cites'), contextPointers(brief.description_pointers)) : null,
    nextBanner,
    h('div', { class: 'detail' },
      h('div', { class: 'stack' }, editPanel, wantedPanel, briefPanel, reviewPanel, activityPanel, checkpointFold, writer ? commentForm(ctx, pid, tid) : null),
      side));
}

// What happened on the task after its checkpoint (kittrial-5bb.354): the entries newest
// first (author, time, new/edited/late, the clipped body) and the changed task fields with
// their previous value where the tracker records one. Every value is the server's text and
// goes into the page as text, never as markup.
export function changesSinceCheckpoint(section) {
  if (!section || !section.checkpoint) return null;
  const entries = Array.isArray(section.entries) ? section.entries : [];
  const fields = Array.isArray(section.fields) ? section.fields : [];
  if (!section.total && !fields.length) {
    return h('p', { class: 'small muted', 'data-changes': 'none' }, 'Nothing has changed since this checkpoint.',
      section.fields_note ? ' ' + section.fields_note : '');
  }
  const label = { title: 'Title', description: 'Description', acceptance_criteria: 'Acceptance' };
  const clipped = (value) => value && value.omitted_chars ? ` … (${value.omitted_chars} more characters)` : '';
  return h('div', { class: 'changes', 'data-changes': 'listed' },
    h('h3', { class: 'small' }, 'Changes since this checkpoint ', h('span', { class: 'nav-count' }, String(section.total || 0))),
    entries.length ? h('ul', { class: 'changes-entries' }, entries.map((e) => h('li', null,
      h('span', { class: 'chip plain' }, e.state), ' ', e.own ? h('span', { class: 'chip plain' }, 'yours') : null, ' ',
      (e.author && e.author.text) || 'unknown', ' · ', time(e.timestamp), ': ',
      (e.body && e.body.text) || '', clipped(e.body)))) : null,
    section.omitted ? h('p', { class: 'small muted' }, `${section.omitted} more are not shown; the history below has them all.`) : null,
    fields.length ? h('ul', { class: 'changes-fields' }, fields.map((f) => h('li', null,
      h('strong', null, (label[f.field] || f.field) + ' changed'),
      f.changed_at ? [' · ', time(f.changed_at)] : null, ' (who made the change is not recorded). Now: ',
      (f.current && f.current.text) || '', clipped(f.current),
      f.previous ? ['. Before: ', f.previous.text || '', clipped(f.previous)] : '. The previous value is not recorded.'))) : null,
    section.fields_note ? h('p', { class: 'small muted' }, section.fields_note) : null,
    section.coverage === 'unknown' || section.coverage === 'windowed'
      ? h('p', { class: 'small muted' }, section.note) : null);
}

export function checkpointForm(ctx, pid, tid, initialBrief) {
  let context = initialBrief;
  let contextReadFailed = false;
  const status = h('div', { class: 'banner crit', role: 'alert', hidden: true });
  const carried = h('div', { class: 'stack' });
  const added = h('div', { class: 'stack' });
  let count = 0;
  const template = initialBrief.checkpoint_template;
  const fields = ['intent', 'acceptance', 'summary', 'next_action', 'source_commit', 'branch'];
  const labels = ['Intent', 'Acceptance', 'Where it stands', 'Next action', 'Source commit', 'Branch'];
  const form = h('form', { class: 'form', novalidate: true },
    h('h3', { class: 'small' }, 'Record a checkpoint'), status,
    h('p', { class: 'hint' }, 'Carry each unresolved item unchanged or explicitly resolve it with a reason and evidence. Saving records your position; it does not complete work.'),
    ...fields.map((name, i) => field({ id: 'cp-' + name, label: labels[i],
      type: i < 4 ? 'textarea' : 'text', rows: 3, required: i < 4,
      value: template?.body?.[name] || '' })), carried, added,
    h('button', { type: 'button', onclick: () => {
      const n = count++;
      added.append(h('fieldset', null, h('legend', null, 'New unresolved item'),
        field({ id: `new-${n}-id`, label: 'Item ID', required: true }),
        field({ id: `new-${n}-kind`, label: 'Kind', type: 'select', options: (template?.open_item?.kind || []).map(k => [k, k]) }),
        field({ id: `new-${n}-text`, label: 'Unresolved work', type: 'textarea', required: true }),
        field({ id: `new-${n}-source`, label: 'Source', required: true })));
      ctx.setDirty(true);
    } }, 'Add unresolved item'),
    field({ id: 'cp-lane', label: 'Lane state', type: 'select', options: [
      ['', 'Leave lane state unchanged'], ['working', 'Working'], ['waiting-on-coordinator', 'Waiting on coordinator'],
      ['waiting-on-review', 'Waiting on review'], ['waiting-on-user', 'Waiting on user'], ['idle', 'Idle']] }),
    field({ id: 'cp-waiting', label: 'Waiting on', hint: 'For a waiting state, name coordinator, reviewer, user, or an exact actor ID.' }),
    field({ id: 'cp-expect', label: 'Expected by', hint: 'Optional time with timezone, for example 2026-10-14T12:00:00Z.' }),
    h('div', { class: 'actions' }, h('button', { type: 'submit', class: 'primary' }, 'Save checkpoint'),
      h('button', { type: 'button', onclick: async () => {
        try { context = await ctx.api.brief(pid, tid); contextReadFailed = false; showContext(); }
        catch (error) { contextReadFailed = true; save.disabled = true; status.replaceChildren(describe(error)); status.hidden = false; }
      } }, 'Refresh checkpoint context')));
  const save = form.querySelector('button[type=submit]');
  function complete() {
    const t = context.checkpoint_template;
    return !contextReadFailed && t && t.carried_open_items !== null && Number.isInteger(t.carried_open_items)
      && Array.isArray(t.body?.open_items) && t.body.open_items.length === t.carried_open_items
      && t.body.previous === (context.checkpoint?.id ?? null)
      && t.body.activity_cursor != null && Object.hasOwn(t.body, 'previous');
  }
  function showContext() {
    carried.replaceChildren();
    save.disabled = !complete();
    if (!complete()) {
      status.replaceChildren('The complete checkpoint context could not be read. Refresh it before saving; your text is kept.');
      status.hidden = false; return;
    }
    status.hidden = true;
    context.checkpoint_template.body.open_items.forEach((item, i) => carried.append(h('fieldset', null,
      h('legend', null, item.id), h('p', { class: 'prose' }, item.text), h('p', { class: 'hint' }, 'Source: ', item.source),
      field({ id: `carry-${i}`, label: 'Disposition', type: 'select', options: [['carry', 'Carry unchanged'], ['resolve', 'Resolve explicitly']] }),
      field({ id: `why-${i}`, label: 'Resolution reason', type: 'textarea' }),
      field({ id: `proof-${i}`, label: 'Resolution evidence' }))));
  }
  showContext();
  form.addEventListener('input', () => ctx.setDirty(true));
  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (!complete()) return showContext();
    const v = formValues(form);
    const body = JSON.parse(JSON.stringify(context.checkpoint_template.body));
    for (const name of fields) body[name] = v['cp-' + name];
    body.open_items = []; body.resolved = [];
    context.checkpoint_template.body.open_items.forEach((item, i) => {
      if (v[`carry-${i}`] === 'resolve') body.resolved.push({ id: item.id, reason: v[`why-${i}`], evidence: v[`proof-${i}`] });
      else body.open_items.push({ ...item });
    });
    for (let n = 0; n < count; n++) body.open_items.push({ id: v[`new-${n}-id`], kind: v[`new-${n}-kind`], text: v[`new-${n}-text`], source: v[`new-${n}-source`] });
    if (v['cp-lane']) {
      body.lane_state = { schema_version: 1, task: tid, state: v['cp-lane'] };
      if (v['cp-waiting']) body.lane_state.waiting_on = v['cp-waiting'];
      if (v['cp-expect']) body.lane_state.expect_by = v['cp-expect'];
    }
    status.hidden = true;
    try {
      await act(save, () => ctx.api.checkpoint(pid, tid, body), { success: 'Checkpoint recorded', onError: error => {
        status.replaceChildren(describe(error), error.status === 409 ? ' Refresh the checkpoint context and reconcile before retrying. Your text is kept.' : ' Your text is kept.');
        status.hidden = false; return true;
      } });
    } catch { return; }
    if (status.hidden) { ctx.setDirty(false); ctx.render(); }
  });
  return h('section', { class: 'panel' }, h('div', { class: 'panel-body' }, form));
}

export function commentForm(ctx, pid, tid) {
  const status = h('div', { class: 'banner crit', role: 'alert', hidden: true });
  const form = h('form', { class: 'form', novalidate: true },
    h('h3', { class: 'small' }, 'Add a comment'), status,
    field({ id: 'comment-text', label: 'Comment', type: 'textarea', rows: 4, required: true,
      hint: 'Stored as written under your identity. A comment does not complete or acknowledge work.' }),
    h('button', { type: 'submit', class: 'primary' }, 'Post comment'));
  form.addEventListener('input', () => ctx.setDirty(true));
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const text = formValues(form)['comment-text'];
    if (!text.trim()) return setFieldError(form, 'comment-text', 'Enter a comment.');
    setFieldError(form, 'comment-text', '');
    status.hidden = true;
    try {
      await act(form.querySelector('button[type=submit]'), () => ctx.api.comment(pid, tid, text), {
        success: 'Comment posted', onError: (error) => {
          status.replaceChildren(describe(error)); status.hidden = false; return true;
        }
      });
    } catch { return; }
    // The input survives refused/unknown outcomes. The API keeps the exact retry key.
    if (status.hidden) { ctx.setDirty(false); ctx.render(); }
  });
  return h('section', { class: 'panel' }, h('div', { class: 'panel-body' }, form));
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


export function assignmentForm(ctx, pid, tid, task) {
  const form = h('form', { class: 'form', novalidate: true });
  const feedback = h('p', { role: 'status', class: 'small muted' }, 'Load the current project roster to assign this task.');
  const rosterButton = h('button', { type: 'button' }, 'Load assignment roster');
  let complete = false;
  let unknown = null;
  const holder = field({ id: 'assignee', label: 'Assign to', type: 'select', options: [['', 'Unassigned']] });
  const reason = field({ id: 'reason', label: 'Reason', type: 'textarea', rows: 3, required: true, maxlength: 1000 });
  const reviewOverride = h('input', { id: 'allow_pending_review', type: 'checkbox' });
  const slotOverride = h('input', { id: 'allow_held_slot', type: 'checkbox' });
  const submit = h('button', { type: 'submit', class: 'primary', disabled: true }, 'Assign task');
  form.append(rosterButton, holder, reason,
    h('label', null, reviewOverride, ' Allow changing a holder with pending review; review items stay open'),
    h('label', null, slotOverride, ' Allow changing a holder who holds this task’s merge slot; the slot stays held'),
    feedback, submit);
  rosterButton.addEventListener('click', async () => {
    complete = false; submit.disabled = true; rosterButton.disabled = true;
    try {
      const actors = []; const seen = new Set(); let cursor = null;
      do {
        const page = await ctx.api.assignmentRoster(pid, { limit: 100, ...(cursor ? { cursor } : {}) });
        if (!Array.isArray(page.items) || page.more && !page.next_cursor) throw new Error('The project roster is incomplete.');
        actors.push(...page.items); cursor = page.next_cursor;
        if (cursor && seen.has(cursor)) throw new Error('The project roster did not advance.');
        if (cursor) seen.add(cursor);
      } while (cursor);
      const select = form.querySelector('select');
      select.replaceChildren(h('option', { value: '' }, 'Unassigned'), ...actors.map(actor => h('option', { value: actor.actor }, `${actor.name} (${actor.actor})`)));
      if (task.assignee && actors.some(actor => actor.actor === task.assignee)) select.value = task.assignee;
      complete = true; feedback.textContent = 'Changing the assignee grants no access. Current eligibility and the holder are checked when you submit.';
    } catch (error) { feedback.textContent = error.message; }
    finally { rosterButton.disabled = false; submit.disabled = !complete; }
  });
  form.addEventListener('input', () => { ctx.setDirty(true); });
  form.addEventListener('submit', async event => {
    event.preventDefault(); if (!complete || submit.disabled) return;
    const values = formValues(form);
    const payload = unknown || { expected_assignee: task.assignee || null, assignee: values.assignee || null,
      reason: values.reason, allow_pending_review: Boolean(reviewOverride.checked), allow_held_slot: Boolean(slotOverride.checked) };
    if (!payload.reason.trim() || payload.reason.length > 1000) { feedback.textContent = 'Enter a reason of 1–1000 characters.'; return; }
    submit.disabled = true;
    try {
      await ctx.api.assignTask(pid, tid, payload); unknown = null; ctx.setDirty(false);
      Promise.resolve().then(() => ctx.render()).catch(error => { feedback.textContent = 'Assignment saved. Refresh failed: ' + error.message; });
    } catch (error) {
      feedback.textContent = error.message;
      if (error.uncertain) {
        unknown = payload; submit.textContent = 'Reconcile identical assignment';
        for (const tag of ['input', 'textarea', 'select']) form.querySelectorAll(tag).forEach(input => { input.disabled = true; });
        rosterButton.disabled = true;
      } else {
        unknown = null;
        if (error.status === 409) { complete = false; feedback.textContent += ' Reload the task to compare the current holder; your reason is retained here.'; }
      }
    } finally { submit.disabled = !complete; }
  });
  return h('section', { class: 'panel' }, h('div', { class: 'panel-head' }, h('h2', { class: 'small' }, 'Assign task')), h('div', { class: 'panel-body' }, form));
}
