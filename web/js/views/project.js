import { h, time, confirmDialog, secretDialog, toast, shortSha } from '../dom.js';
import { pageHead, reviewChip, statusChip, priority, empty, field, setFieldError, formValues, act, errorState, describe } from '../ui.js';

const PAGE = 10;
export const canWrite = (p) => ['owner', 'contributor', 'superuser'].includes(p.role);
export const isOwner = (p) => ['owner', 'superuser'].includes(p.role);

async function load(ctx, pid) {
  const project = await ctx.api.project(pid);
  return project;
}

function crumbs(ctx, project, extra) {
  return [{ label: 'Projects', href: ctx.href('/projects') }, { label: project.name, href: extra ? ctx.href('/p/' + project.id) : null }].concat(extra ? [{ label: extra }] : []);
}

function archivedBanner(project) {
  return project.archived ? h('div', { class: 'banner' }, 'This project is archived. Its records are kept and readable; new work cannot be added.') : null;
}

// A record this server will not serve (project.usable === false): its task pages answer
// 409, so no page offers them. Only reading, removing access and archiving are left.
export function unusableBanner(project) {
  return project.usable === false ? h('div', { class: 'banner crit', role: 'alert' }, project.unusable_reason || 'This project cannot be used on this server.',
    ' Nothing that grants access is accepted on it; removing a member, revoking a credential and archiving still work.') : null;
}

export async function overview(ctx, { pid }) {
  const project = await load(ctx, pid);
  if (project.usable === false) {
    return h('div', { class: 'stack' },
      pageHead({ crumbs: crumbs(ctx, project), title: project.name }),
      archivedBanner(project),
      unusableBanner(project),
      h('p', null, h('a', { href: ctx.href(`/p/${pid}/settings`) }, 'Members, credentials and archive')));
  }
  const state = { status: 'active', q: '', review: '', cursor: null, stack: [] };
  const tableHost = h('div', { class: 'panel' });

  const search = h('input', { type: 'search', id: 'task-search', placeholder: 'Search title, description or ID', 'aria-label': 'Search tasks' });
  const statusSeg = h('div', { class: 'seg', role: 'group', 'aria-label': 'Status' },
    [['active', 'Active'], ['closed', 'Closed'], ['', 'All']].map(([value, label]) => h('button', { type: 'button', 'aria-pressed': String(state.status === value), onclick: (e) => {
      state.status = value; state.cursor = null; state.stack = [];
      statusSeg.querySelectorAll('button').forEach((b) => b.setAttribute('aria-pressed', String(b === e.currentTarget)));
      draw();
    } }, label)));
  const reviewSel = h('select', { id: 'review-filter', 'aria-label': 'Review state', onchange: (e) => { state.review = e.target.value; state.cursor = null; state.stack = []; draw(); } },
    [['', 'Any review state'], ['awaiting-review', 'Awaiting review'], ['changes-requested', 'Changes requested'], ['approved', 'Approved · not integrated'], ['none', 'No contribution']].map(([v, l]) => h('option', { value: v }, l)));
  let debounce;
  search.addEventListener('input', () => { clearTimeout(debounce); debounce = setTimeout(() => { state.q = search.value.trim(); state.cursor = null; state.stack = []; draw(); }, 250); });

  async function draw() {
    tableHost.setAttribute('aria-busy', 'true');
    let page;
    try {
      page = await ctx.api.tasks(pid, { limit: PAGE, cursor: state.cursor, status: state.status, q: state.q, review_state: state.review });
    } catch (error) {
      tableHost.replaceChildren(errorState(error, draw));
      return;
    } finally { tableHost.removeAttribute('aria-busy'); }
    const rows = page.items;
    const start = state.stack.length * PAGE;
    // The server says when some rows' review state is unknown (closed tasks with a
    // finished review, or rows past its bounded read); those show their status only.
    const partial = page.review_states_complete === false
      ? h('p', { class: 'small muted', role: 'note' }, 'Review state is not known for some tasks shown here (closed tasks, or more tasks than one read covers); they show their status only.')
      : null;
    tableHost.replaceChildren(
      rows.length ? h('div', { class: 'table-wrap' }, h('table', null,
        h('caption', { class: 'visually-hidden' }, 'Tasks in ' + project.name),
        h('thead', null, h('tr', null, h('th', { scope: 'col' }, 'Task'), h('th', { scope: 'col' }, 'State'), h('th', { scope: 'col', class: 'hide-narrow' }, 'Assignee'), h('th', { scope: 'col', class: 'hide-narrow' }, 'Next action'), h('th', { scope: 'col', class: 'hide-narrow' }, 'Updated'))),
        h('tbody', null, rows.map((t) => h('tr', { class: 'row-link', onclick: (e) => { if (e.target.tagName !== 'A') ctx.go(`/p/${pid}/t/${t.id}`); } },
          h('td', null, h('a', { class: 'title', href: ctx.href(`/p/${pid}/t/${t.id}`) }, t.title), h('div', { class: 'sub' }, t.priority != null ? [priority(t.priority), ' · '] : null, h('span', { class: 'mono' }, t.id))),
          h('td', null, t.review_state && t.review_state !== 'none' ? reviewChip(t.review_state) : statusChip(t.status)),
          h('td', { class: 'hide-narrow' }, t.assignee_name || h('span', { class: 'muted' }, 'Unclaimed')),
          h('td', { class: 'hide-narrow' }, t.next_action ? t.next_action.text : h('span', { class: 'muted' }, '—')),
          h('td', { class: 'hide-narrow muted' }, t.updated_at ? time(t.updated_at) : '')))))) :
        empty(state.q || state.review ? 'No matching tasks' : 'No tasks yet', state.q || state.review ? 'Try a different search or filter.' : canWrite(project) && !project.archived ? 'Define the first task for this project.' : null),
      partial,
      h('div', { class: 'pager' },
        h('span', { class: 'num' }, page.total ? `${start + 1}–${start + rows.length} of ${page.total}` : ''),
        h('div', { class: 'actions' },
          h('button', { type: 'button', disabled: !state.stack.length, onclick: () => { state.cursor = state.stack.pop() || null; draw(); } }, 'Previous'),
          h('button', { type: 'button', disabled: !page.next_cursor, onclick: () => { state.stack.push(state.cursor); state.cursor = page.next_cursor; draw(); } }, 'Next'))));
  }
  draw();

  return h('div', { class: 'stack' },
    pageHead({ crumbs: crumbs(ctx, project), title: project.name, lede: project.description || null,
      actions: canWrite(project) && !project.archived ? h('a', { class: 'btn primary', href: ctx.href(`/p/${pid}/new`) }, 'New task') : null }),
    archivedBanner(project),
    unusableBanner(project),
    h('div', { class: 'toolbar' }, search, statusSeg, reviewSel),
    tableHost);
}

export async function reviews(ctx, { pid }) {
  const project = await load(ctx, pid);
  const data = await ctx.api.queue(pid);
  // The disposable server calls an approved contribution "approved"; the canonical
  // review projection calls it "awaiting-integration". Both land in one group.
  const groups = [
    [['awaiting-review', 'legacy-review-ready'], 'Awaiting review', 'An owner needs to approve or request changes. These stay listed until someone acts, however old.'],
    [['changes-requested'], 'Changes requested', 'Waiting on the contributor to deliver a revision.'],
    [['approved', 'awaiting-integration'], 'Approved · not yet integrated', 'Accepted work that still needs integrating. Holds and dependencies are shown on each task.'],
    [['error'], 'Needs operator attention', 'The review record could not be read cleanly; an operator has to repair it.'],
  ];
  const openRequests = (t) => (t.open_requests ?? (t.requests || []).filter((r) => r.status === 'open').length);
  return h('div', { class: 'stack' },
    pageHead({ crumbs: crumbs(ctx, project, 'Reviews'), title: 'Reviews', lede: 'Every contribution in flight, grouped by who has to act next.' }),
    data.complete === false ? h('div', { class: 'banner' }, 'This list is incomplete: the project has more contributions in flight than one read covers.') : null,
    groups.map(([keys, title, note]) => {
      const key = keys[0];
      const rows = data.items.filter((t) => keys.includes(t.review_state));
      if (key === 'error' && !rows.length) return null;
      return h('section', { class: 'panel', 'aria-labelledby': 'g-' + key },
        h('div', { class: 'panel-head' }, h('h2', { class: 'small', id: 'g-' + key }, title, ' ', h('span', { class: 'nav-count' }, rows.length)), h('span', { class: 'small muted hide-narrow' }, note)),
        rows.length ? h('div', { class: 'table-wrap' }, h('table', null,
          h('thead', null, h('tr', null, h('th', { scope: 'col' }, 'Task'), h('th', { scope: 'col' }, 'Contribution'), h('th', { scope: 'col', class: 'hide-narrow' }, 'Open requests'), h('th', { scope: 'col', class: 'hide-narrow' }, 'Waiting since'))),
          h('tbody', null, rows.map((t) => h('tr', { class: 'row-link', onclick: (e) => { if (e.target.tagName !== 'A') ctx.go(`/p/${pid}/t/${t.id}`); } },
            h('td', null, h('a', { class: 'title', href: ctx.href(`/p/${pid}/t/${t.id}`) }, t.title), h('div', { class: 'sub' }, t.assignee_name || 'Unassigned')),
            h('td', null, t.contribution ? [t.contribution.revision ? h('span', null, 'Revision ', t.contribution.revision, ' · ') : null, h('code', null, shortSha(t.contribution.commit))] : '—'),
            h('td', { class: 'hide-narrow num' }, String(openRequests(t))),
            h('td', { class: 'hide-narrow muted' }, (t.contribution && t.contribution.at) || t.updated_at ? time((t.contribution && t.contribution.at) || t.updated_at) : '')))))) :
          empty('Nothing here', null));
    }));
}

export async function feedback(ctx, { pid }) {
  const project = await load(ctx, pid);
  const list = h('div', { class: 'panel' });
  async function draw() {
    let data;
    try { data = await ctx.api.feedback(pid); } catch (error) { list.replaceChildren(errorState(error, draw)); return; }
    list.replaceChildren(data.items.length ? h('ul', { class: 'timeline panel-body', 'aria-label': 'Feedback' }, data.items.map((f) => h('li', null,
      h('span', { class: 'dot ' + (f.status === 'resolved' ? 'ok' : 'warn'), 'aria-hidden': 'true' }),
      h('div', null,
        h('div', { class: 'event-head' }, h('strong', null, f.author_name || f.actor || ''), time(f.at || f.created_at),
          f.status ? h('span', { class: 'chip ' + (f.status === 'open' ? 'warn' : 'ok') }, f.status === 'open' ? 'Open' : 'Resolved') : null),
        h('p', { class: 'event-body prose' }, f.text),
        f.triage ? h('p', { class: 'small muted' }, 'Triage: ', f.triage) : null)))) :
      empty('No feedback yet', 'Problems, ideas and friction reported by the team appear here.'));
  }
  draw();
  const form = h('form', { class: 'form', novalidate: true },
    field({ id: 'fb-text', label: 'What happened, or what would help?', type: 'textarea', maxlength: 2000, hint: 'Up to 2,000 characters. Visible to project members.' }),
    h('div', null, h('button', { type: 'submit', class: 'primary' }, 'Submit feedback')));
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const text = formValues(form)['fb-text'].trim();
    if (!text) return setFieldError(form, 'fb-text', 'Write something first.');
    setFieldError(form, 'fb-text', '');
    const saved = await act(form.querySelector('button'), () => ctx.api.addFeedback(pid, { text }), { success: 'Feedback submitted' });
    if (saved) { form.reset(); draw(); }
  });
  return h('div', { class: 'stack' },
    pageHead({ crumbs: crumbs(ctx, project, 'Feedback'), title: 'Feedback', lede: 'Reading feedback does not resolve it. Open items stay open until someone triages them.' }),
    canWrite(project) && !project.archived ? h('section', { class: 'panel' }, h('div', { class: 'panel-head' }, h('h2', { class: 'small' }, 'Add feedback')), h('div', { class: 'panel-body' }, form)) : null,
    list);
}

export async function settings(ctx, { pid }) {
  const project = await load(ctx, pid);
  const owner = isOwner(project);
  const membersHost = h('div', { class: 'panel' });
  const credsHost = h('div');
  const auditHost = h('div');

  async function drawMembers() {
    let data;
    try { data = await ctx.api.members(pid); } catch (error) { membersHost.replaceChildren(errorState(error, drawMembers)); return; }
    const rows = data.items.slice().sort((a, b) => a.display_name.localeCompare(b.display_name));
    membersHost.replaceChildren(
      h('div', { class: 'panel-head' }, h('h2', { class: 'small' }, 'Members ', h('span', { class: 'nav-count' }, rows.length)), h('span', { class: 'small muted hide-narrow' }, 'Viewers read · contributors claim and deliver · owners review and manage')),
      h('div', { class: 'table-wrap' }, h('table', null,
        h('thead', null, h('tr', null, h('th', { scope: 'col' }, 'Person'), h('th', { scope: 'col' }, 'Role'), owner ? h('th', { scope: 'col' }, h('span', { class: 'visually-hidden' }, 'Actions')) : null)),
        h('tbody', null, rows.map((m) => {
          const select = h('select', { 'aria-label': 'Role for ' + m.display_name, disabled: !owner || project.archived },
            ['viewer', 'contributor', 'owner'].map((r) => h('option', { value: r, selected: r === m.role }, r[0].toUpperCase() + r.slice(1))));
          select.addEventListener('change', async () => {
            try { await act(select, () => ctx.api.setMember(pid, m.user_id, select.value), { success: `${m.display_name} is now ${select.value}` }); }
            catch { select.value = m.role; }
            drawMembers();
          });
          return h('tr', null,
            h('td', null, h('div', { class: 'title' }, m.display_name), h('div', { class: 'sub' }, '@' + m.username, m.agent_of_name ? ' · agent of ' + m.agent_of_name : '', m.disabled ? ' · account disabled' : '')),
            h('td', null, owner ? select : h('span', { class: 'role' }, m.role)),
            owner ? h('td', null, h('button', { type: 'button', class: 'danger', disabled: project.archived, onclick: async (e) => {
              const ok = await confirmDialog({ title: `Remove ${m.display_name}?`, body: 'They lose access to this project immediately, including any worker credentials they issued. Their past contributions stay in the record.', confirmLabel: 'Remove', danger: true });
              if (!ok) return;
              try { await act(e.currentTarget, () => ctx.api.removeMember(pid, m.user_id), { success: 'Removed' }); } catch { /* shown */ }
              drawMembers();
            } }, 'Remove')) : null);
        })))),
      owner && !project.archived ? addMemberForm() : null);
  }

  function addMemberForm() {
    const form = h('form', { class: 'panel-body', novalidate: true },
      h('div', { class: 'form-row' },
        field({ id: 'm-username', label: 'Add by username', placeholder: 'e.g. lena', required: true }),
        field({ id: 'm-role', label: 'Role', type: 'select', value: 'contributor', options: [['viewer', 'Viewer'], ['contributor', 'Contributor'], ['owner', 'Owner']] })),
      h('div', null, h('button', { type: 'submit', class: 'primary' }, 'Add member')));
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      const v = formValues(form);
      const username = v['m-username'].trim().replace(/^@/, '');
      if (!username) return setFieldError(form, 'm-username', 'Enter a username.');
      let account;
      try { account = await ctx.api.lookup(username, pid); } catch (error) {
        return setFieldError(form, 'm-username', error.status === 404 ? 'No active account has that username.' : describe(error));
      }
      setFieldError(form, 'm-username', '');
      try { await act(form.querySelector('button[type=submit]'), () => ctx.api.setMember(pid, account.id, v['m-role']), { success: `${account.display_name} added as ${v['m-role']}` }); } catch { return; }
      drawMembers();
    });
    return form;
  }

  async function drawCreds() {
    if (!owner) { credsHost.replaceChildren(); return; }
    let data;
    try { data = await ctx.api.credentials(pid); } catch (error) { credsHost.replaceChildren(errorState(error, drawCreds)); return; }
    const issue = h('button', { type: 'button', disabled: project.archived, onclick: async (e) => {
      const result = await act(e.currentTarget, () => ctx.api.issueCredential(pid, ['tasks', 'checkpoints', 'reviews'], 'Worker credential')).catch(() => null);
      if (!result) return;
      secretDialog({ title: 'Worker credential issued', body: 'Give this to the agent or script that will work on this project. It is shown once and never again. It can do no more than you can, and stops working if you lose access.', secret: result.credential.secret });
      drawCreds();
    } }, 'Issue credential');
    credsHost.replaceChildren(h('section', { class: 'panel' },
      h('div', { class: 'panel-head' }, h('h2', { class: 'small' }, 'Worker credentials'), issue),
      data.items.length ? h('div', { class: 'table-wrap' }, h('table', null,
        h('thead', null, h('tr', null, h('th', { scope: 'col' }, 'Label'), h('th', { scope: 'col' }, 'Issued by'), h('th', { scope: 'col', class: 'hide-narrow' }, 'Scopes'), h('th', { scope: 'col', class: 'hide-narrow' }, 'Expires'), h('th', { scope: 'col' }, h('span', { class: 'visually-hidden' }, 'Actions')))),
        h('tbody', null, data.items.map((c) => h('tr', null,
          h('td', null, c.label, c.revoked ? h('div', { class: 'sub' }, 'Revoked') : null),
          h('td', null, c.user_name || c.user_id || ''),
          h('td', { class: 'hide-narrow mono small' }, c.scopes.join(', ')),
          h('td', { class: 'hide-narrow muted' }, time(c.expires_at)),
          h('td', null, c.revoked ? null : h('button', { type: 'button', class: 'danger', onclick: async (e) => {
            if (!(await confirmDialog({ title: 'Revoke this credential?', body: `“${c.label}” stops working immediately, including for requests already queued.`, confirmLabel: 'Revoke', danger: true }))) return;
            try { await act(e.currentTarget, () => ctx.api.revokeCredential(pid, c.id), { success: 'Credential revoked' }); } catch { /* shown */ }
            drawCreds();
          } }, 'Revoke'))))))) :
        empty('No worker credentials', 'Agents and scripts use a credential instead of a password. Issue one per worker.')));
  }

  async function drawAudit() {
    if (!owner) { auditHost.replaceChildren(); return; }
    let data;
    try { data = await ctx.api.audit(pid, { limit: 20 }); } catch (error) { auditHost.replaceChildren(errorState(error, drawAudit)); return; }
    auditHost.replaceChildren(h('section', { class: 'panel' },
      h('div', { class: 'panel-head' }, h('h2', { class: 'small' }, 'Recent activity (audit)')),
      data.items.length ? h('div', { class: 'table-wrap' }, h('table', null,
        h('thead', null, h('tr', null, h('th', { scope: 'col' }, 'When'), h('th', { scope: 'col' }, 'Who'), h('th', { scope: 'col' }, 'Action'), h('th', { scope: 'col', class: 'hide-narrow' }, 'Detail'))),
        h('tbody', null, data.items.map((a) => h('tr', null, h('td', { class: 'muted' }, time(a.time)), h('td', null, a.user_name || a.user_id || ''), h('td', { class: 'mono small' }, a.action, a.outcome && a.outcome !== 'committed' ? ' · ' + a.outcome : ''), h('td', { class: 'hide-narrow' }, a.detail || '')))))) :
        empty('No administrative activity yet', null)));
  }

  drawMembers(); drawCreds(); drawAudit();

  const danger = owner && !project.archived ? h('section', { class: 'panel' },
    h('div', { class: 'panel-head' }, h('h2', { class: 'small' }, 'Archive project')),
    h('div', { class: 'panel-body' },
      h('p', { class: 'muted' }, 'Archiving stops new work and hides the project from active lists. Nothing is deleted: tasks, reviews and history stay readable to members.'),
      h('div', null, h('button', { type: 'button', class: 'danger', onclick: async (e) => {
        if (!(await confirmDialog({ title: `Archive “${project.name}”?`, body: 'Members keep read access to all records. New tasks, claims and contributions will be refused.', confirmLabel: 'Archive project', danger: true }))) return;
        try { await act(e.currentTarget, () => ctx.api.archiveProject(pid), { success: 'Project archived' }); } catch { return; }
        await ctx.refreshProjects(); ctx.render();
      } }, 'Archive project')))) : null;

  return h('div', { class: 'stack' },
    pageHead({ crumbs: crumbs(ctx, project, 'Members & settings'), title: 'Members & settings', lede: owner ? 'Manage who can see and change this project.' : 'Only project owners can change membership.' }),
    archivedBanner(project),
    unusableBanner(project),
    membersHost, credsHost, auditHost, danger);
}
