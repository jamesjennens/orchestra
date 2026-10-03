// Contributed requirement proposals (docs/REQUIREMENTS_GATHERING_DESIGN.md 5.3, 7.1, 8.6):
// the queue and the detail panel on the Reviews page, the propose form, and the
// "My contributions" panel on My work.
//
// Proposal text, rationale, evidence, questions and reasons are written by contributors
// and are untrusted. Everything here goes through h(), which sets text content only; an
// evidence entry becomes a link only when it is a plain https URL.
import { h, time } from '../dom.js';
import { empty, field, setFieldError, formValues, act, describe, errorState } from '../ui.js';

const STATES = {
  'submitted': ['Submitted', 'accent'],
  'under-review': ['Under review', 'accent'],
  'needs-info': ['Needs information', 'warn'],
  'escalated-to-owner': ['Escalated to the owner', 'warn'],
  'approved': ['Approved', 'ok'],
  'incorporated': ['Incorporated', 'ok'],
  'rejected': ['Rejected', 'plain'],
  'duplicate-of': ['Duplicate', 'plain'],
};
const NEXT_ACTOR = { coordinator: 'A coordinator', owner: 'The owner', submitter: 'The submitter', none: 'Nobody' };
// What happens next, in the web's own words (the server's next_action names the
// command-line tools an operator uses).
const NEXT_STEP = {
  'submitted': 'A coordinator starts the review.',
  'under-review': 'A coordinator decides what to do with it.',
  'needs-info': 'The submitter answers the question by revising the proposal.',
  'escalated-to-owner': 'The owner approves or rejects it.',
  'approved': 'A coordinator drafts the requirement change and records where it landed.',
};

// The open groups of the queue, in the order someone has to act (design 5.3).
const GROUPS = [
  ['submitted', 'Submitted', 'Waiting for a coordinator to pick it up.'],
  ['under-review', 'Under review', 'A coordinator is working out what to do with it.'],
  ['needs-info', 'Needs information', 'Waiting for the submitter to answer a question.'],
  ['escalated-to-owner', 'Escalated to the owner', 'Waiting for the owner to approve or reject.'],
  ['approved', 'Approved', 'Approved by the owner; the requirement change is still to be drafted and recorded.'],
];

// What a member with approval rights may record next, from each state. The server
// checks the same table; this only decides which choices the form offers.
const TRANSITIONS = {
  'submitted': [['under-review', 'Start review']],
  'under-review': [['needs-info', 'Ask the submitter a question'], ['escalated-to-owner', 'Escalate to the owner'],
    ['incorporated', 'Record as incorporated'], ['duplicate-of', 'Mark as a duplicate'], ['rejected', 'Reject']],
  'escalated-to-owner': [['approved', 'Approve (owner decision)'], ['rejected', 'Reject (owner decision)']],
  'approved': [['incorporated', 'Record as incorporated']],
};

export const proposalHref = (ctx, pid, key) => ctx.href(`/p/${pid}/reviews?proposal=${key}`);
export const unavailable = (error) => Boolean(error && [404, 501].includes(error.status));

export function stateChip(state) {
  const [label, tone] = STATES[state] || [state || 'Unknown', ''];
  return h('span', { class: 'chip ' + tone }, label);
}

// An excerpt object {text, omitted_chars, trust} as plain text.
function excerpt(value) {
  if (!value) return '';
  return value.text + (value.omitted_chars ? '…' : '');
}

function targetText(target) {
  if (!target) return 'Not stated';
  if (target.kind === 'requirement') return 'Requirement ' + target.requirement_key;
  if (target.kind === 'requirement-area') return 'Area ' + target.area;
  return 'A new requirement';
}

function who(item) {
  return item.submitter_name || String(item.submitter || '').replace(/^(account|person):/, '');
}

const days = (n) => (n === 1 ? '1 day' : `${n} days`);

// A plain https URL becomes a link; anything else stays inert text.
function evidenceNode(value) {
  const text = excerpt(value);
  let url = null;
  try { url = new URL(text); } catch { url = null; }
  if (url && url.protocol === 'https:' && !value.omitted_chars && !/\s/.test(text)) {
    return h('a', { href: url.href, rel: 'noopener noreferrer', target: '_blank' }, text);
  }
  return h('span', null, text);
}

function rowsTable(ctx, pid, rows) {
  return h('div', { class: 'table-wrap' }, h('table', null,
    h('thead', null, h('tr', null, h('th', { scope: 'col' }, 'Proposal'), h('th', { scope: 'col' }, 'From'),
      h('th', { scope: 'col', class: 'hide-narrow' }, 'About'), h('th', { scope: 'col', class: 'hide-narrow' }, 'Age'),
      h('th', { scope: 'col' }, 'Next'))),
    h('tbody', null, rows.map((p) => h('tr', { class: 'row-link', onclick: (e) => { if (e.target.tagName !== 'A') ctx.go(`/p/${pid}/reviews?proposal=${p.key}`); } },
      h('td', null, h('a', { class: 'title', href: proposalHref(ctx, pid, p.key) }, excerpt(p.title)),
        h('div', { class: 'sub' }, h('span', { class: 'mono' }, p.key), p.mine ? [' · ', h('strong', null, 'Yours')] : null, p.stale ? [' · ', 'No movement for a while'] : null)),
      h('td', null, who(p), p.identity === 'verified' ? null : h('div', { class: 'sub' }, 'Identity not verified')),
      h('td', { class: 'hide-narrow' }, targetText(p.target)),
      h('td', { class: 'hide-narrow muted' }, days(p.age_days)),
      h('td', null, NEXT_ACTOR[p.next_actor] || p.next_actor))))));
}

// The Proposal queue panel for the Reviews page. Resolves to null on a server without
// the routes, so the page keeps working there.
export async function queuePanel(ctx, pid) {
  let data;
  try { data = await ctx.api.proposals(pid, { limit: 100 }); } catch (error) {
    if (unavailable(error)) {
      return h('section', { class: 'panel' }, h('div', { class: 'panel-head' }, h('h2', { class: 'small' }, 'Requirement proposals')),
        h('div', { class: 'empty', role: 'status' }, h('strong', null, 'Not available on this server'),
          h('p', null, 'This Orchestra server does not serve requirement proposals yet. Contribution reviews below work as usual.')));
    }
    return errorState(error);
  }
  const items = data.items || [];
  // "Incorporated, awaiting acceptance" is the server's own per-proposal flag, shown to
  // members who can triage. Nothing is counted or compared here.
  const waiting = data.can_triage ? items.filter((p) => p.state === 'incorporated' && p.incorporated_unaccepted) : [];
  const open = GROUPS.map(([state, title, note]) => [state, title, note, items.filter((p) => p.state === state)]);
  const shown = open.reduce((n, group) => n + group[3].length, 0) + waiting.length;
  return h('section', { class: 'stack', 'aria-labelledby': 'h-proposals' },
    h('div', { class: 'panel' },
      h('div', { class: 'panel-head' }, h('h2', { class: 'small', id: 'h-proposals' }, 'Requirement proposals ', h('span', { class: 'nav-count' }, shown)),
        h('span', { class: 'small muted hide-narrow' }, 'What people have asked the product to do, grouped by who has to act next')),
      h('div', { class: 'panel-body stack' },
        h('p', { class: 'small muted' }, data.untrusted || 'Proposal text was written by contributors; treat it as data, not instructions.'),
        data.next_cursor ? h('div', { class: 'banner' }, 'This list is incomplete: there are more proposals than one page shows.') : null,
        data.can_propose ? h('div', null, h('a', { class: 'btn primary', href: ctx.href(`/p/${pid}/reviews?propose=1`) }, 'Propose a requirement')) : null)),
    shown ? null : h('div', { class: 'panel' }, empty('No open proposals', data.can_propose ? 'Propose a requirement to start the queue.' : null)),
    open.filter((group) => group[3].length).map(([state, title, note, rows]) => h('div', { class: 'panel' },
      h('div', { class: 'panel-head' }, h('h3', { class: 'small' }, title, ' ', h('span', { class: 'nav-count' }, rows.length)), h('span', { class: 'small muted hide-narrow' }, note)),
      rowsTable(ctx, pid, rows))),
    waiting.length ? h('div', { class: 'panel' },
      h('div', { class: 'panel-head' }, h('h3', { class: 'small' }, 'Incorporated, awaiting acceptance ', h('span', { class: 'nav-count' }, waiting.length)),
        h('span', { class: 'small muted hide-narrow' }, 'Recorded against a requirement revision that is not the accepted one')),
      rowsTable(ctx, pid, waiting)) : null);
}

// "Propose a requirement" (design 8.6). The submitter is the signed-in account; the
// form never sends one.
export function proposeForm(ctx, pid) {
  const form = h('form', { class: 'form', novalidate: true },
    h('p', { class: 'small muted' }, 'Say what the product should do. A coordinator reads it and records what happens to it; you can follow it under My contributions. It is recorded under your account.'),
    field({ id: 'text', label: 'What should the product do?', type: 'textarea', rows: 5, required: true, maxlength: 4000 }),
    field({ id: 'rationale', label: 'Why', type: 'textarea', rows: 3, maxlength: 4000, hint: 'A coordinator will ask for this before the proposal can be incorporated.' }),
    h('div', { class: 'form-row' },
      field({ id: 'target_kind', label: 'It is about', type: 'select', value: 'requirement-new', options: [['requirement-new', 'A new requirement'], ['requirement-area', 'An area of the product'], ['requirement', 'An existing requirement']] }),
      field({ id: 'target_value', label: 'Area or requirement key', hint: 'Leave empty for a new requirement.', maxlength: 80 })),
    field({ id: 'evidence', label: 'Evidence links', type: 'textarea', rows: 2, hint: 'One per line, optional.' }),
    h('div', { class: 'actions' }, h('button', { type: 'submit', class: 'primary' }, 'Submit proposal'),
      h('a', { class: 'btn', href: ctx.href(`/p/${pid}/reviews`) }, 'Cancel')));
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const values = formValues(form);
    if (!values.text.trim()) return setFieldError(form, 'text', 'Say what the product should do.');
    setFieldError(form, 'text', '');
    const value = values.target_value.trim();
    let target = { kind: 'requirement-new' };
    if (values.target_kind === 'requirement-area') target = { kind: 'requirement-area', area: value };
    if (values.target_kind === 'requirement') target = { kind: 'requirement', requirement_key: value };
    if (values.target_kind !== 'requirement-new' && !value) return setFieldError(form, 'target_value', 'Name the area or the requirement key.');
    setFieldError(form, 'target_value', '');
    const body = { target, text: values.text.trim(), rationale: values.rationale.trim() || null,
      evidence: values.evidence.split('\n').map((line) => line.trim()).filter(Boolean), attachments: [] };
    const made = await act(form.querySelector('button'), () => ctx.api.submitProposal(pid, body), {
      success: 'Proposal submitted',
      onError: (e) => { if (e.status === 422) { setFieldError(form, 'text', describe(e)); return true; } return false; } });
    if (made) ctx.go(`/p/${pid}/reviews?proposal=${made.key}`);
  });
  return h('section', { class: 'panel' }, h('div', { class: 'panel-head' }, h('h2', { class: 'small' }, 'Propose a requirement')),
    h('div', { class: 'panel-body' }, form));
}

function dispositionText(d) {
  const [label] = STATES[d.to_state] || [d.to_state];
  const by = d.actor_name || d.actor || 'someone';
  return `${label} · ${by}${d.role === 'owner' ? ' (owner decision)' : d.role === 'submitter' ? ' (the submitter answered)' : ''}`;
}

function timelineItem(d) {
  const tone = { rejected: 'crit', 'needs-info': 'warn', 'escalated-to-owner': 'warn', approved: 'ok', incorporated: 'ok' }[d.to_state] || 'accent';
  return h('li', null, h('span', { class: 'dot ' + (d.standing === 'counted' ? tone : '') }),
    h('div', null,
      h('div', null, h('strong', null, dispositionText(d)), ' ', h('span', { class: 'small muted' }, time(d.at))),
      d.standing !== 'counted' ? h('div', { class: 'small muted' }, 'This entry does not count: its author had no authority to record it.') : null,
      d.question ? h('p', { class: 'prose' }, 'Question: ', excerpt(d.question)) : null,
      d.reason ? h('p', { class: 'prose' }, 'Reason: ', excerpt(d.reason)) : null,
      d.escalation ? h('p', { class: 'prose' }, 'Asked of ', d.escalation.owner_name || String(d.escalation.owner_identity).replace(/^(account|person):/, ''),
        d.escalation.due_by ? ` by ${d.escalation.due_by}` : '', d.escalation.question ? [': ', excerpt(d.escalation.question)] : null) : null,
      d.duplicate_of ? h('p', null, 'Duplicate of ', h('span', { class: 'mono' }, d.duplicate_of)) : null,
      d.decision ? h('p', { class: 'small muted' }, 'Decision ', h('span', { class: 'mono' }, d.decision.decision_id)) : null,
      d.incorporation ? h('p', { class: 'small muted' }, 'Requirement record ', h('span', { class: 'mono' }, d.incorporation.requirement_id), ' revision ', d.incorporation.requirement_revision) : null,
      d.withheld ? h('p', { class: 'small muted' }, 'The reason or question is shown to the submitter and to project owners.') : null));
}

// The triage form for a member with approval rights (never for their own proposal).
function triageForm(ctx, pid, view, owners) {
  const choices = TRANSITIONS[view.state] || [];
  if (!choices.length) return null;
  const extra = h('div', { class: 'stack' });
  const select = h('select', { id: 'to_state', name: 'to_state', 'aria-label': 'What happens to this proposal' },
    choices.map(([value, label]) => h('option', { value }, label)));
  const draw = () => {
    const to = select.value;
    const fields = [];
    if (to === 'needs-info') fields.push(field({ id: 'question', label: 'Question for the submitter', type: 'textarea', rows: 3, required: true, maxlength: 2000 }));
    if (to === 'rejected') fields.push(field({ id: 'reason', label: 'Reason', type: 'textarea', rows: 3, required: true, maxlength: 2000, hint: 'Shown to the submitter and to project owners.' }));
    if (to === 'duplicate-of') fields.push(field({ id: 'duplicate_of', label: 'Duplicate of (proposal key)', required: true, placeholder: 'p-0123456789ab', maxlength: 14 }));
    if (to === 'escalated-to-owner') {
      fields.push(field({ id: 'escalation_question', label: 'Question for the owner', type: 'textarea', rows: 3, required: true, maxlength: 2000 }));
      fields.push(h('div', { class: 'form-row' },
        field({ id: 'owner_identity', label: 'Who decides', type: 'select', options: owners.length ? owners : [['', 'No other owner in this project']] }),
        field({ id: 'due_by', label: 'Decide by (optional)', type: 'date' })));
    }
    if (to === 'approved' || (to === 'rejected' && view.state === 'escalated-to-owner')) {
      fields.push(field({ id: 'decision_id', label: 'Decision record', required: true, hint: 'The id of the decision issue an operator filed for this.', maxlength: 128 }));
    }
    if (to === 'incorporated') {
      fields.push(h('p', { class: 'small muted' }, 'Record where the proposal landed. Draft the requirement revision first; if it is not accepted yet, say draft.'));
      fields.push(h('div', { class: 'form-row' },
        field({ id: 'requirement_id', label: 'Requirement record id', required: true, maxlength: 128 }),
        field({ id: 'requirement_revision', label: 'Revision', type: 'number', value: '1', required: true })));
      fields.push(field({ id: 'requirement_sha256', label: 'Revision hash (sha256)', required: true, maxlength: 64 }));
      fields.push(h('div', { class: 'form-row' },
        field({ id: 'acceptance_state', label: 'That revision is', type: 'select', value: 'draft', options: [['draft', 'A draft'], ['accepted', 'Accepted']] }),
        field({ id: 'acceptance_decision_id', label: 'Acceptance decision id', hint: 'Only when accepted.', maxlength: 128 })));
    }
    extra.replaceChildren(...fields);
  };
  select.addEventListener('change', draw);
  draw();
  const form = h('form', { class: 'form', novalidate: true },
    h('div', { class: 'field' }, h('label', { for: 'to_state' }, 'What happens to this proposal'), select,
      h('div', { class: 'error', id: 'to_state-error', hidden: true })),
    extra,
    h('div', null, h('button', { type: 'submit', class: 'primary' }, 'Record')));
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const v = formValues(form);
    const to = v.to_state;
    const body = { previous: view.disposition_comment_id, proposal_sha256: view.sha256, to_state: to };
    if (view.state === 'escalated-to-owner') { body.operation = 'decide'; body.decision = { decision_id: (v.decision_id || '').trim() }; }
    if (to === 'needs-info') body.question = v.question;
    if (to === 'rejected' && v.reason !== undefined) body.reason = v.reason;
    if (to === 'duplicate-of') body.duplicate_of = (v.duplicate_of || '').trim();
    if (to === 'escalated-to-owner') body.escalation = { question: v.escalation_question, owner_identity: v.owner_identity, due_by: v.due_by || null };
    if (to === 'incorporated') {
      const accepted = v.acceptance_state === 'accepted';
      body.incorporation = { kind: 'requirement', requirement_id: (v.requirement_id || '').trim(), requirement_revision: Number(v.requirement_revision),
        requirement_sha256: (v.requirement_sha256 || '').trim(), acceptance_state: v.acceptance_state,
        acceptance_decision_id: accepted ? (v.acceptance_decision_id || '').trim() : null,
        manifest_baseline: null, manifest_sha256: null, change_classification: null };
    }
    setFieldError(form, 'to_state', '');
    const done = await act(form.querySelector('button[type=submit]'), () => ctx.api.disposeProposal(pid, view.key, body), {
      success: 'Recorded',
      onError: (e) => { if ([409, 422].includes(e.status)) { setFieldError(form, 'to_state', describe(e)); return true; } return false; } });
    if (done) ctx.render();
  });
  return h('section', { class: 'panel' }, h('div', { class: 'panel-head' }, h('h3', { class: 'small' }, view.state === 'escalated-to-owner' ? 'Owner decision' : 'Triage')),
    h('div', { class: 'panel-body' }, form));
}

// The submitter revises while the proposal is submitted or a coordinator has asked.
function reviseForm(ctx, pid, view) {
  const form = h('form', { class: 'form', novalidate: true },
    view.state === 'needs-info' ? h('p', { class: 'small' }, 'A coordinator asked a question. Answer it by revising your proposal; it then goes back to review.') : h('p', { class: 'small muted' }, 'You can revise this until a coordinator picks it up.'),
    field({ id: 'text', label: 'What should the product do?', type: 'textarea', rows: 5, required: true, maxlength: 4000, value: view.text.text }),
    field({ id: 'rationale', label: 'Why', type: 'textarea', rows: 3, maxlength: 4000, value: view.rationale ? view.rationale.text : '' }),
    h('div', null, h('button', { type: 'submit', class: 'primary' }, 'Save revision')));
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const v = formValues(form);
    if (!v.text.trim()) return setFieldError(form, 'text', 'Say what the product should do.');
    const body = { key: view.key, revision: view.revision + 1, expected_sha256: view.sha256, target: view.target,
      text: v.text.trim(), rationale: v.rationale.trim() || null,
      evidence: (view.evidence || []).map((item) => item.text), attachments: (view.attachments || []).map((a) => ({ name: a.name.text, sha256: a.sha256 })) };
    const done = await act(form.querySelector('button'), () => ctx.api.submitProposal(pid, body), {
      success: 'Revision saved',
      onError: (e) => { if ([409, 422].includes(e.status)) { setFieldError(form, 'text', describe(e)); return true; } return false; } });
    if (done) ctx.render();
  });
  return h('section', { class: 'panel' }, h('div', { class: 'panel-head' }, h('h3', { class: 'small' }, 'Revise your proposal')),
    h('div', { class: 'panel-body' }, form));
}

// The detail panel at /p/{pid}/reviews?proposal=<key>.
export async function detailPanel(ctx, pid, key) {
  let view;
  try { view = await ctx.api.proposal(pid, key); } catch (error) { return errorState(error); }
  const back = h('a', { class: 'btn', href: ctx.href(`/p/${pid}/reviews`) }, 'Back to the queue');
  if (view.state === 'malformed' || view.state === 'unsupported') {
    return h('section', { class: 'panel' }, h('div', { class: 'panel-head' }, h('h2', { class: 'small' }, 'Proposal ', h('span', { class: 'mono' }, key)), back),
      h('div', { class: 'empty', role: 'alert' }, h('strong', null, 'This proposal cannot be read'), h('p', null, view.coverage || 'An operator has to repair it.')));
  }
  let owners = [];
  if (view.can_triage && view.state === 'under-review') {
    try {
      const members = await ctx.api.members(pid);
      owners = members.items.filter((m) => m.role === 'owner' && !m.disabled && m.user_id !== ctx.me.id).map((m) => ['account:' + m.user_id, m.display_name]);
    } catch { owners = []; }
  }
  const linked = view.linked_requirement;
  const canRevise = view.mine && ['submitted', 'needs-info'].includes(view.state);
  // The owner decision comes from a different person than the one who escalated.
  const escalatedByMe = view.state === 'escalated-to-owner' && view.disposition && view.disposition.actor === ctx.me.id;
  // A later revision written by someone other than the submitter (the server names it).
  const broken = (view.warnings || []).find((w) => w.code === 'identity-broken');
  return h('section', { class: 'stack', 'aria-labelledby': 'h-proposal' },
    broken ? h('div', { class: 'banner', role: 'alert' },
      'This text may not be the submitter’s: a later revision was written by someone else, so the proposal reads as not verified. ',
      h('span', { class: 'small muted' }, String(broken.detail || ''))) : null,
    h('div', { class: 'panel' },
      h('div', { class: 'panel-head' }, h('h2', { class: 'small', id: 'h-proposal' }, 'Proposal ', h('span', { class: 'mono' }, view.key), ' ', stateChip(view.state)), back),
      h('div', { class: 'panel-body stack' },
        h('p', { class: 'small muted' }, view.untrusted),
        h('p', { class: 'prose' }, excerpt(view.text)),
        h('dl', { class: 'kv' },
          h('dt', null, 'From'), h('dd', null, who(view), view.mine ? ' (you)' : '', view.submitted_by_agent ? ' · submitted by their agent' : '',
            view.identity === 'verified' ? '' : ' · identity not verified'),
          h('dt', null, 'About'), h('dd', null, targetText(view.target)),
          h('dt', null, 'Submitted'), h('dd', null, time(view.submitted_at), ` · revision ${view.revision}`),
          h('dt', null, 'Next'), h('dd', null, NEXT_STEP[view.state] || 'Nothing: this proposal is closed.'),
          view.rationale ? [h('dt', null, 'Why'), h('dd', { class: 'prose' }, excerpt(view.rationale))] : null,
          view.evidence && view.evidence.length ? [h('dt', null, 'Evidence'), h('dd', null, h('ul', null, view.evidence.map((item) => h('li', null, evidenceNode(item)))))] : null,
          view.attachments && view.attachments.length ? [h('dt', null, 'Attachments'), h('dd', null, view.attachments.map((a) => h('div', null, excerpt(a.name), ' ', h('span', { class: 'mono small muted' }, String(a.sha256).slice(0, 12)))))] : null,
          view.supersedes ? [h('dt', null, 'Replaces'), h('dd', null, h('a', { href: proposalHref(ctx, pid, view.supersedes) }, view.supersedes))] : null,
          view.superseded_by && view.superseded_by.length ? [h('dt', null, 'Replaced by'), h('dd', null, view.superseded_by.map((k, i) => [i ? ', ' : null, h('a', { href: proposalHref(ctx, pid, k) }, k)]))] : null,
          linked ? [h('dt', null, 'Requirement'), h('dd', null, h('span', { class: 'mono' }, linked.id), ` revision ${linked.revision} · `,
            linked.acceptance_state === 'accepted' ? 'accepted' : `not the accepted requirement (${linked.acceptance_state})`)] : null))),
    h('div', { class: 'panel' },
      h('div', { class: 'panel-head' }, h('h3', { class: 'small' }, 'What has happened ', h('span', { class: 'nav-count' }, view.timeline_total))),
      view.timeline.length ? h('div', { class: 'panel-body' }, h('ul', { class: 'timeline' }, view.timeline.map(timelineItem))) : empty('Nothing yet', 'No coordinator has picked this up.')),
    canRevise ? reviseForm(ctx, pid, view) : null,
    view.can_triage && !view.mine && !escalatedByMe ? triageForm(ctx, pid, view, owners) : null,
    view.can_triage && escalatedByMe ? h('div', { class: 'banner' }, 'You escalated this proposal: a different owner has to decide it.') : null,
    view.can_triage && view.mine && TRANSITIONS[view.state] ? h('div', { class: 'banner' }, 'This is your own proposal: another owner has to triage it.') : null);
}

// "My contributions" on My work (design 7.1). Null on a server without the route.
export async function myContributionsPanel(ctx) {
  let data;
  try { data = await ctx.api.myContributions(); } catch (error) {
    if (unavailable(error)) return null;
    return h('section', { class: 'panel' }, h('div', { class: 'panel-head' }, h('h2', { class: 'small' }, 'My contributions')),
      h('div', { class: 'panel-body' }, h('p', { class: 'small muted' }, describe(error))));
  }
  const items = data.items || [];
  // The route returns the newest page first; `next_cursor` means there are older ones.
  const unread = data.unavailable && data.unavailable.length;
  const older = data.truncated || data.next_cursor;
  return h('section', { class: 'panel', 'aria-labelledby': 'h-contributions' },
    h('div', { class: 'panel-head' }, h('h2', { class: 'small', id: 'h-contributions' }, 'My contributions ', h('span', { class: 'nav-count' }, data.total)),
      h('span', { class: 'small muted hide-narrow' }, 'Requirement proposals you submitted. Looking at them changes nothing.')),
    unread || older ? h('div', { class: 'panel-body' }, h('p', { class: 'small muted' },
      older ? `Showing your newest ${items.length} of ${data.total}. ` : '',
      unread ? 'Some projects could not be read just now.' : '')) : null,
    items.length ? h('div', { class: 'table-wrap' }, h('table', null,
      h('thead', null, h('tr', null, h('th', { scope: 'col' }, 'Proposal'), h('th', { scope: 'col', class: 'hide-narrow' }, 'Project'), h('th', { scope: 'col' }, 'State'), h('th', { scope: 'col' }, 'What happens next'))),
      h('tbody', null, items.map((p) => h('tr', { class: 'row-link', onclick: (e) => { if (e.target.tagName !== 'A') ctx.go(`/p/${p.project}/reviews?proposal=${p.key}`); } },
        h('td', null, h('a', { class: 'title', href: proposalHref(ctx, p.project, p.key) }, excerpt(p.title)),
          h('div', { class: 'sub' }, h('span', { class: 'mono' }, p.key), ' · ', days(p.age_days), ' old')),
        h('td', { class: 'hide-narrow' }, p.project_name || p.project),
        h('td', null, stateChip(p.state)),
        h('td', null, NEXT_STEP[p.state] || (p.disposition && p.disposition.reason ? 'Reason: ' + excerpt(p.disposition.reason) : 'Nothing: it is closed.'))))))) :
      empty('Nothing proposed yet', 'Propose a requirement from a project’s Reviews page.'));
}

// A one-line strip for the project page when the caller has proposals there.
export async function myStrip(ctx, pid) {
  let data;
  try { data = await ctx.api.proposals(pid, { limit: 100 }); } catch { return null; }
  const mine = (data.items || []).filter((p) => p.mine);
  const open = mine.filter((p) => !['incorporated', 'rejected', 'duplicate-of'].includes(p.state));
  const waiting = open.filter((p) => p.next_actor === 'submitter');
  if (!mine.length && !data.can_propose) return null;
  return h('div', { class: 'banner info' },
    mine.length ? `You have ${mine.length} requirement proposal${mine.length === 1 ? '' : 's'} here, ${open.length} still open` + (waiting.length ? `, ${waiting.length} waiting for your answer. ` : '. ') : 'Want the product to do something? ',
    h('a', { href: ctx.href(`/p/${pid}/reviews`) }, mine.length ? 'See them on Reviews' : 'Propose a requirement on Reviews'));
}

