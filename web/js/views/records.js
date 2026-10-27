// Reference and capability records view: waiting queue, find/lookup, propose, revise,
// coordinator accept/retire/verify, and two-step revise-then-accept (kittrial-5bb.213).
import { h, mount, toast } from '../dom.js';
import { pageHead, empty, setFieldError, formValues, act, describe, errorState } from '../ui.js';

function field(label, control, hint) {
  const name = control && control.getAttribute ? control.getAttribute('name') : '';
  const id = (control && control.getAttribute && control.getAttribute('id')) || name || '';
  if (control && id && !control.getAttribute('id')) control.setAttribute('id', id);
  const error = id ? h('div', { class: 'error', id: id + '-error', hidden: true }) : null;
  return h('div', { class: 'field' },
    h('label', id ? { for: id } : null, label),
    control,
    hint ? h('div', { class: 'hint', id: id ? id + '-hint' : undefined }, hint) : null,
    error);
}

function textOf(val) {
  if (val === null || val === undefined) return '';
  if (typeof val === 'object') return String(val.text || '');
  return String(val);
}

function splitCsv(text) {
  return String(text || '').split(',').map((s) => s.trim()).filter(Boolean);
}

function parseRequirements(text) {
  return splitCsv(text).map((item) => {
    const [key, rev] = item.split('@');
    return rev && /^\d+$/.test(rev.trim())
      ? { key: key.trim(), revision: Number(rev.trim()) }
      : { key: key.trim() };
  });
}

function formatRequirements(list) {
  return (list || []).map((item) => {
    if (!item) return '';
    if (typeof item === 'string') return item;
    return item.revision ? `${item.key}@${item.revision}` : (item.key || '');
  }).filter(Boolean);
}

function splitLines(text) {
  return String(text || '').split(/\r?\n/).map((s) => s.trim()).filter(Boolean);
}

function stateBadge(state) {
  const s = state || 'draft-only';
  const tone = s === 'accepted' ? 'ok' : (s === 'superseded' || s === 'retired') ? 'muted' : 'warn';
  return h('span', { class: 'badge ' + tone, 'data-state-badge': s }, s);
}

function verificationBadge(status) {
  const s = status || 'unverified';
  const tone = (s === 'verified' || s === 'attested') ? 'ok' : (s === 'drifted' || s === 'failed') ? 'crit' : 'muted';
  return h('span', { class: 'badge ' + tone, 'data-verification-badge': s }, 'verification: ' + s);
}

function defaultReviewDate() {
  const d = new Date(Date.now() + 90 * 86400 * 1000);
  return d.toISOString().slice(0, 10);
}

function formatAuthority(auth) {
  if (!auth || typeof auth !== 'object') return '—';
  if (auth.type === 'repo-path') {
    return auth.path + (auth.anchor ? '#' + auth.anchor : '') + (auth.commit ? ' @ ' + auth.commit.slice(0, 12) : '');
  }
  if (auth.type === 'url') {
    return `${auth.url} (retrieved ${auth.retrieved || '—'})`;
  }
  if (auth.type === 'attestation') {
    return `attestation (${auth.basis || '—'}) by ${auth.by || '—'} on ${auth.observed || '—'}: ${textOf(auth.how)}`;
  }
  if (auth.type === 'decision') {
    return `decision ${auth.id || '—'}`;
  }
  if (auth.type === 'repo-document') {
    return auth.path + (auth.anchor ? '#' + auth.anchor : '') + ' @ ' + String(auth.commit || '').slice(0, 12)
      + ' (document sha256 ' + String(auth.sha256 || '').slice(0, 12) + ')';
  }
  return auth.type || '—';
}

// kittrial-5bb.418: a design.<scope> revision carries its document. The file's exact bytes are hashed and
// sent as text decoded strictly (no BOM stripped, no replacement), so the server's sha256 of the text it
// encodes again is the one in the authority. Refused here before anything is sent when it is not UTF-8.
export async function readDesignDocument(file) {
  if (!file) throw new Error('Choose the design document (the Markdown file at the pinned commit).');
  const bytes = new Uint8Array(await file.arrayBuffer());
  if (bytes.length > 1024 * 1024) throw new Error('A design document is at most 1 MiB.');
  let text;
  try {
    text = new TextDecoder('utf-8', { fatal: true, ignoreBOM: true }).decode(bytes);
  } catch (err) {
    throw new Error('The design document must be UTF-8 text.');
  }
  if (!globalThis.crypto || !globalThis.crypto.subtle) throw new Error('This browser cannot hash the document here (a secure connection is needed).');
  const digest = new Uint8Array(await globalThis.crypto.subtle.digest('SHA-256', bytes));
  const sha256 = Array.from(digest, (b) => b.toString(16).padStart(2, '0')).join('');
  return { text, sha256 };
}

function buildAuthorityFromForm(v, designDoc) {
  const type = (v.authority_type || 'repo-path').trim();
  const target = (v.authority_target || '').trim();
  if (type === 'url') {
    return {
      type: 'url',
      url: target,
      retrieved: (v.authority_date || new Date().toISOString().slice(0, 10)).trim(),
    };
  }
  if (type === 'attestation') {
    const out = {
      type: 'attestation',
      basis: (v.authority_basis || 'host-check').trim(),
      by: (v.authority_by || v.owner || '').trim(),
      observed: (v.authority_date || new Date().toISOString().slice(0, 10)).trim(),
      how: target,
    };
    if (v.authority_extra && v.authority_extra.trim()) out.source = v.authority_extra.trim();
    return out;
  }
  if (type === 'decision') {
    return { type: 'decision', id: target };
  }
  if (type === 'repo-document') {
    const out = { type: 'repo-document', path: target, commit: (v.authority_commit || '').trim(), sha256: designDoc ? designDoc.sha256 : '' };
    if (v.authority_extra && v.authority_extra.trim()) out.anchor = v.authority_extra.trim();
    return out;
  }
  const out = { type: 'repo-path', path: target };
  if (v.authority_extra && v.authority_extra.trim()) out.anchor = v.authority_extra.trim();
  if (v.authority_commit && v.authority_commit.trim()) out.commit = v.authority_commit.trim();
  return out;
}

export async function page(ctx, { pid }) {
  const project = (ctx.projects || []).find((p) => p.id === pid) || { id: pid, name: pid };
  const safeHref = (path) => (ctx && typeof ctx.href === 'function' ? ctx.href(path) : '#' + path);
  const initialQ = (ctx.query && ctx.query.get && ctx.query.get('q')) || '';
  const initialState = (ctx.query && ctx.query.get && ctx.query.get('state')) || '';

  let refsResp;
  let capsResp;
  let waitingRefsResp;
  let waitingCapsResp;
  try {
    [refsResp, capsResp, waitingRefsResp, waitingCapsResp] = await Promise.all([
      ctx.api.references(pid, initialState ? { state: initialState } : {}),
      ctx.api.capabilities(pid, initialState ? { state: initialState } : {}),
      ctx.api.references(pid, { waiting: 1 }),
      ctx.api.capabilities(pid, { waiting: 1 }),
    ]);
  } catch (error) {
    return h('div', null, pageHead({ title: 'Records', subtitle: project.name }), errorState(error, () => ctx.render()));
  }

  const canPropose = Boolean(refsResp.can_propose || capsResp.can_propose);
  const canCoordinate = Boolean(refsResp.can_coordinate || capsResp.can_coordinate);
  const approvalByAnotherParty = Boolean(refsResp.approval_by_another_party || capsResp.approval_by_another_party);
  const defaultOwner = ctx.me && ctx.me.id ? 'account:' + ctx.me.id : 'person:maintainer';

  const waitingRefs = waitingRefsResp.items || waitingRefsResp.records || [];
  const waitingCaps = waitingCapsResp.items || waitingCapsResp.records || [];
  const totalWaiting = waitingRefs.length + waitingCaps.length;

  // Who accepts the waiting drafts in THIS project (kittrial-5bb.407): the people whose role
  // coordinates, and the coordinator agents a person owner's delegation covers. Filled after the
  // page draws; a server without either read leaves the line out.
  function acceptorsLine() {
    const line = h('p', { class: 'small', 'data-acceptors': 'pending' });
    if (!ctx.api || typeof ctx.api.coordination !== 'function' || typeof ctx.api.delegations !== 'function') return line;
    Promise.all([ctx.api.coordination(pid), ctx.api.delegations(pid)]).then(([who, delegated]) => {
      const people = (who.people || []).filter((p) => !p.disabled).map((p) => p.display_name);
      const names = delegated.names || {};
      const agents = (delegated.granted_agents || []).filter((a) => a.covered).map((a) => names[a.agent] || a.agent);
      const escalate = (delegated.items || []).filter((i) => i.in_force && i.delegation)
        .map((i) => i.delegation.escalate).filter(Boolean);
      line.setAttribute('data-acceptors', 'ready');
      line.replaceChildren('Who accepts here: ',
        people.length ? people.join(', ') : 'no person coordinator',
        agents.length ? '; agents within a delegation: ' + agents.join(', ') : '; no agent (no delegation in force)',
        '.', escalate.length ? ' Escalated to the owner: ' + escalate.join(' · ') : '');
    }).catch(() => { line.remove(); });
    return line;
  }

  function renderWaitingRow(item, family) {
    const rec = item.record || {};
    const draft = item.proposed || rec;
    const isRevisionToAccepted = item.state === 'accepted' && Boolean(item.proposed);
    const href = family === 'reference'
      ? `/p/${pid}/references/${encodeURIComponent(item.key)}`
      : `/p/${pid}/capabilities/${encodeURIComponent(item.key)}`;
    const dTitle = textOf(draft.title || item.title || rec.title);
    const dStmt = textOf(draft.statement || item.statement || rec.statement);
    const dName = textOf(draft.name || item.name || rec.name);
    const dSum = textOf(draft.summary || item.summary || rec.summary);
    const titleOrSummary = family === 'reference'
      ? (dTitle || dStmt || '—')
      : (dName ? `${dName} — ${dSum}` : (dSum || '—'));
    return h('tr', { 'data-waiting-key': item.key, 'data-waiting-family': family },
      h('td', null, h('span', { class: 'badge' }, family)),
      h('td', null, h('a', { class: 'mono', href: safeHref(href) }, item.key)),
      h('td', null,
        isRevisionToAccepted
          ? h('span', { class: 'badge warn', 'data-two-step-pending': '1' },
            `Accepted rev ${rec.revision} · Draft rev ${draft.revision} waiting`)
          : stateBadge(item.state || 'draft-only')),
      h('td', null, titleOrSummary),
      h('td', { class: 'small muted' }, item.proposed_by_name || item.proposer_name || item.proposed_by || '—'),
      h('td', { class: 'right' },
        h('a', { class: 'btn small', href: safeHref(href) }, canCoordinate ? 'Review & accept' : 'Open')));
  }

  const waitingPanel = h('section', { class: 'panel', 'data-waiting-queue': '1' },
    h('div', { class: 'panel-head' },
      h('h2', null, 'Waiting to be accepted'),
      h('span', { class: 'badge ' + (totalWaiting ? 'warn' : 'muted'), 'data-waiting-count': String(totalWaiting) },
        totalWaiting ? `${totalWaiting} waiting` : 'None waiting')),
    h('p', { class: 'small muted' },
      'New draft records and pending draft revisions to already-accepted keys wait here until a coordinator accepts them with evidence.',
      approvalByAnotherParty
        ? ' This project requires approval by another party: a coordinator may not accept or verify a draft proposed by their own account or its agents.'
        : ' In this project a coordinator may accept a draft they proposed themselves; both proposer and acceptor are recorded.'),
    acceptorsLine(),
    totalWaiting === 0
      ? empty('Nothing waiting to be accepted', 'All reference and capability drafts have been handled.')
      : h('div', { class: 'table-wrap' },
        h('table', { class: 'table' },
          h('thead', null, h('tr', null,
            h('th', null, 'Kind'),
            h('th', null, 'Key'),
            h('th', null, 'Stage'),
            h('th', null, 'Title / Summary'),
            h('th', null, 'Proposed by'),
            h('th', null))),
          h('tbody', null,
            waitingRefs.map((item) => renderWaitingRow(item, 'reference')),
            waitingCaps.map((item) => renderWaitingRow(item, 'capability'))))));

  const findResultsBox = h('div', { 'data-find-results': '1' });

  function renderReferenceTable(items) {
    if (!items.length) return empty('No reference records match', 'Propose a reference record or adjust the filter.');
    return h('div', { class: 'table-wrap' },
      h('table', { class: 'table' },
        h('thead', null, h('tr', null,
          h('th', null, 'Key'),
          h('th', null, 'State'),
          h('th', null, 'Authority'),
          h('th', null, 'Owner / Review by'),
          h('th', null, 'Title & Statement'),
          h('th', null, 'Proposer / Acceptor'))),
        h('tbody', null, items.map((item) => {
          const rec = item.record || item.proposed || item;
          const auth = rec.authority || item.authority;
          const href = `/p/${pid}/references/${encodeURIComponent(item.key)}`;
          const proposerLabel = item.accepted_proposer_name || item.proposed_by_name || item.proposer_name || item.proposed_by || '—';
          const acceptorLabel = item.accepted_by_name || item.acceptor_name || item.accepted_by || '';
          const stmt = textOf(rec.statement || item.statement);
          return h('tr', { 'data-reference-row': item.key },
            h('td', null, h('a', { class: 'mono', href: safeHref(href) }, item.key)),
            h('td', null,
              stateBadge(item.state),
              item.proposed ? h('span', { class: 'badge warn', 'data-pending-draft-badge': '1' }, `draft rev ${item.proposed.revision}`) : null),
            h('td', { class: 'mono small' }, formatAuthority(auth)),
            h('td', { class: 'mono small' }, `${rec.owner || item.owner || '—'} · ${rec.review_by || item.review_by || '—'}`),
            h('td', null,
              h('strong', null, textOf(rec.title || item.title) || '—'),
              stmt ? h('div', { class: 'small muted' }, stmt) : null),
            h('td', { class: 'small muted' },
              `Proposed by ${proposerLabel}`,
              acceptorLabel ? ` · Accepted by ${acceptorLabel}` : ''));
        }))));
  }

  function renderCapabilityTable(items) {
    if (!items.length) return empty('No capability records match', 'Propose a capability record or adjust the filter.');
    return h('div', { class: 'table-wrap' },
      h('table', { class: 'table' },
        h('thead', null, h('tr', null,
          h('th', null, 'Key'),
          h('th', null, 'State'),
          h('th', null, 'Verification'),
          h('th', null, 'Owner'),
          h('th', null, 'Name & Summary'),
          h('th', null, 'Proposer / Acceptor'))),
        h('tbody', null, items.map((item) => {
          const rec = item.record || item.proposed || item;
          const ver = typeof item.verification === 'string'
            ? item.verification
            : ((item.verification && item.verification.state) || 'unverified');
          const href = `/p/${pid}/capabilities/${encodeURIComponent(item.key)}`;
          const proposerLabel = item.accepted_proposer_name || item.proposed_by_name || item.proposer_name || item.proposed_by || '—';
          const acceptorLabel = item.accepted_by_name || item.acceptor_name || item.accepted_by || '';
          const sum = textOf(rec.summary || item.summary);
          return h('tr', { 'data-capability-row': item.key },
            h('td', null, h('a', { class: 'mono', href: safeHref(href) }, item.key)),
            h('td', null,
              stateBadge(item.state),
              item.proposed ? h('span', { class: 'badge warn', 'data-pending-draft-badge': '1' }, `draft rev ${item.proposed.revision}`) : null),
            h('td', null, verificationBadge(ver)),
            h('td', { class: 'mono small' }, rec.owner || item.owner || '—'),
            h('td', null,
              h('strong', null, textOf(rec.name || item.name) || '—'),
              sum ? h('div', { class: 'small muted' }, sum) : null),
            h('td', { class: 'small muted' },
              `Proposed by ${proposerLabel}`,
              acceptorLabel ? ` · Accepted by ${acceptorLabel}` : ''));
        }))));
  }

  const refTableSlot = h('div', { 'data-reference-list': '1' }, renderReferenceTable(refsResp.items || refsResp.records || []));
  const capTableSlot = h('div', { 'data-capability-list': '1' }, renderCapabilityTable(capsResp.items || capsResp.records || []));

  const findForm = h('form', {
    class: 'stack',
    'data-records-find-form': '1',
    onsubmit: async (e) => {
      e.preventDefault();
      const v = formValues((e.currentTarget || e.target));
      const q = (v.q || '').trim();
      try {
        let foundRefs;
        let foundCaps;
        if (q) {
          [foundRefs, foundCaps] = await Promise.all([
            ctx.api.findReferences(pid, { q }),
            ctx.api.findCapabilities(pid, { q }),
          ]);
        } else {
          const refParams = {};
          const capParams = {};
          if (v.state) { refParams.state = v.state; capParams.state = v.state; }
          if (v.tag && v.tag.trim()) { refParams.tag = v.tag.trim(); capParams.tag = v.tag.trim(); }
          if (v.owner && v.owner.trim()) { refParams.owner = v.owner.trim(); capParams.owner = v.owner.trim(); }
          if (v.authority && v.authority.trim()) refParams.authority = v.authority.trim();
          [foundRefs, foundCaps] = await Promise.all([
            ctx.api.references(pid, refParams),
            ctx.api.capabilities(pid, capParams),
          ]);
        }
        const rItems = foundRefs.items || foundRefs.records || [];
        const cItems = foundCaps.items || foundCaps.records || [];
        mount(refTableSlot, renderReferenceTable(rItems));
        mount(capTableSlot, renderCapabilityTable(cItems));
        mount(findResultsBox, h('p', { class: 'small muted', 'data-find-summary': '1' },
          `Found ${rItems.length} reference(s) and ${cItems.length} capability record(s).`));
      } catch (err) {
        toast(describe(err), 'crit');
      }
    },
  },
    h('div', { class: 'row wrap' },
      field('Search phrase', h('input', { name: 'q', value: initialQ, placeholder: 'Key, title, statement, alias, keyword…' })),
      field('State', h('select', { name: 'state' },
        h('option', { value: '', selected: !initialState }, 'All states'),
        h('option', { value: 'accepted', selected: initialState === 'accepted' }, 'Accepted'),
        h('option', { value: 'draft-only', selected: initialState === 'draft-only' }, 'Draft only'),
        h('option', { value: 'superseded', selected: initialState === 'superseded' }, 'Superseded'))),
      field('Authority (references)', h('select', { name: 'authority' },
        h('option', { value: '' }, 'Any authority'),
        h('option', { value: 'repository' }, 'repository'),
        h('option', { value: 'url' }, 'url'),
        h('option', { value: 'attested' }, 'attested'),
        h('option', { value: 'decision' }, 'decision'))),
      field('Tag', h('input', { name: 'tag', placeholder: 'e.g. web' })),
      field('Owner', h('input', { name: 'owner', placeholder: defaultOwner }))),
    h('div', { class: 'actions' },
      h('button', { type: 'submit', class: 'primary' }, 'Find records'),
      h('button', {
        type: 'button',
        onclick: async () => {
          findForm.reset();
          const [r, c] = await Promise.all([ctx.api.references(pid, {}), ctx.api.capabilities(pid, {})]);
          mount(refTableSlot, renderReferenceTable(r.items || r.records || []));
          mount(capTableSlot, renderCapabilityTable(c.items || c.records || []));
          mount(findResultsBox, null);
        },
      }, 'Reset')),
    findResultsBox);

  const findPanel = h('section', { class: 'panel', 'data-records-find': '1' },
    h('div', { class: 'panel-head' }, h('h2', null, 'Find & filter records')),
    findForm);

  // Propose Reference form
  const proposeRefError = h('p', { class: 'field-error', hidden: true });
  const proposeRefForm = canPropose ? h('form', {
    class: 'stack',
    'data-propose-reference-form': '1',
    onsubmit: async (e) => {
      e.preventDefault();
      const form = (e.currentTarget || e.target);
      const v = formValues(form);
      proposeRefError.hidden = true;
      const key = (v.key || '').trim();
      const title = (v.title || '').trim();
      const statement = (v.statement || '').trim();
      const owner = (v.owner || defaultOwner).trim();
      const review_by = (v.review_by || defaultReviewDate()).trim();
      const tags = splitCsv(v.tags);
      const decisions = splitCsv(v.decisions);
      if (!key) { setFieldError(form, 'key', 'Enter a reference key (e.g. ops.deploy-window).'); return; }
      if (!title) { setFieldError(form, 'title', 'Enter a title.'); return; }
      if (!statement) { setFieldError(form, 'statement', 'Enter the reference statement.'); return; }
      if (!v.authority_target || !v.authority_target.trim()) {
        setFieldError(form, 'authority_target', 'Enter the repository path, URL, observation, or decision ID.');
        return;
      }
      const btn = form.querySelector('button');
      await act(btn, async () => {
        try {
          const fileInput = form.querySelector('input[name=document_file]');
          const designDoc = (v.authority_type || '').trim() === 'repo-document'
            ? await readDesignDocument(fileInput && fileInput.files && fileInput.files[0]) : null;
          const authority = buildAuthorityFromForm(v, designDoc);
          const body = { key, title, statement, authority, owner, review_by, tags, decisions };
          if (designDoc) body.document = designDoc.text;
          await ctx.api.proposeReference(pid, body);
          toast('Reference draft proposed.', 'ok');
          ctx.go(`/p/${pid}/references/${encodeURIComponent(key)}`);
        } catch (err) {
          proposeRefError.textContent = describe(err);
          proposeRefError.hidden = false;
        }
      });
    },
  },
    h('h3', null, 'Propose a reference record'),
    h('div', { class: 'row wrap' },
      field('Key', h('input', { name: 'key', placeholder: 'ops.deploy-window', required: true }), 'Format: <area>.<slug>'),
      field('Title', h('input', { name: 'title', placeholder: 'Production deploy window', required: true })),
      field('Owner', h('input', { name: 'owner', value: defaultOwner, required: true })),
      field('Review by (YYYY-MM-DD)', h('input', { name: 'review_by', value: defaultReviewDate(), required: true }))),
    h('div', { class: 'row wrap' },
      field('Authority type', h('select', { name: 'authority_type' },
        h('option', { value: 'repo-path' }, 'repo-path (repository file)'),
        h('option', { value: 'url' }, 'url (https URL)'),
        h('option', { value: 'attestation' }, 'attestation (host-check / owner-statement)'),
        h('option', { value: 'decision' }, 'decision (native decision issue)'),
        h('option', { value: 'repo-document' }, 'repo-document (design document, for design.<scope>)'))),
      field('Authority target (path / URL / how / decision id)', h('input', {
        name: 'authority_target',
        placeholder: 'docs/HTTP_DEPLOYMENT.md',
        required: true,
      })),
      field('Pinned commit (40-hex, for repo-path)', h('input', { name: 'authority_commit', class: 'mono', placeholder: '40-char commit sha' })),
      field('Anchor or source (optional)', h('input', { name: 'authority_extra', placeholder: 'section-anchor' })),
      field('Design document (Markdown, for repo-document)', h('input', { name: 'document_file', type: 'file', accept: '.md,text/markdown,text/plain' }))),
    h('div', { class: 'row wrap' },
      field('Tags (comma-separated)', h('input', { name: 'tags', placeholder: 'ops, web' })),
      field('Decisions (comma-separated issue IDs, optional)', h('input', { name: 'decisions', placeholder: '' }))),
    field('Statement', h('textarea', { name: 'statement', rows: 2, placeholder: 'State the operational fact and authority…', required: true })),
    proposeRefError,
    h('div', { class: 'actions' },
      h('button', { type: 'submit', class: 'primary' }, 'Propose reference draft'))) : null;

  // Propose Capability form
  const proposeCapError = h('p', { class: 'field-error', hidden: true });
  const proposeCapForm = canPropose ? h('form', {
    class: 'stack',
    'data-propose-capability-form': '1',
    onsubmit: async (e) => {
      e.preventDefault();
      const form = (e.currentTarget || e.target);
      const v = formValues(form);
      proposeCapError.hidden = true;
      const key = (v.key || '').trim();
      const name = (v.name || '').trim();
      const summary = (v.summary || '').trim();
      const owner = (v.owner || defaultOwner).trim();
      const code = splitLines(v.code);
      const tests = splitLines(v.tests);
      const anchors = splitLines(v.anchors);
      const requirements = parseRequirements(v.requirements);
      const aliases = splitCsv(v.aliases);
      const tags = splitCsv(v.tags);
      if (!key) { setFieldError(form, 'key', 'Enter a capability key (e.g. records.web-catalog).'); return; }
      if (!name) { setFieldError(form, 'name', 'Enter a capability name.'); return; }
      if (!summary) { setFieldError(form, 'summary', 'Enter a summary.'); return; }
      if (!code.length && !anchors.length) {
        setFieldError(form, 'code', 'Enter at least one code pointer or design anchor.');
        return;
      }
      const btn = form.querySelector('button');
      await act(btn, async () => {
        try {
          await ctx.api.proposeCapability(pid, {
            key,
            name,
            aliases,
            summary,
            requirements,
            anchors,
            code,
            tests,
            owner,
            tags,
          });
          toast('Capability draft proposed.', 'ok');
          ctx.go(`/p/${pid}/capabilities/${encodeURIComponent(key)}`);
        } catch (err) {
          proposeCapError.textContent = describe(err);
          proposeCapError.hidden = false;
        }
      });
    },
  },
    h('h3', null, 'Propose a capability record'),
    h('div', { class: 'row wrap' },
      field('Key', h('input', { name: 'key', placeholder: 'records.web-catalog', required: true }), 'Format: <area>.<slug>'),
      field('Name', h('input', { name: 'name', placeholder: 'Web records catalog', required: true })),
      field('Owner', h('input', { name: 'owner', value: defaultOwner, required: true }))),
    field('Summary', h('textarea', { name: 'summary', rows: 2, placeholder: 'What this capability is for and how it works…', required: true })),
    h('div', { class: 'row wrap' },
      field('Code pointers (path::symbol, one per line)', h('textarea', { name: 'code', rows: 2, placeholder: 'http_service.py::ApiHandler.references_list', required: true })),
      field('Test pointers (path::symbol, one per line)', h('textarea', { name: 'tests', rows: 2, placeholder: 'tests/test_web_records.py::StrictWebRecordsTests' })),
      field('Design anchors (path#heading, one per line)', h('textarea', { name: 'anchors', rows: 2, placeholder: 'docs/HTTP_DEPLOYMENT.md#reference-and-capability-records' }))),
    h('div', { class: 'row wrap' },
      field('Aliases (comma-separated)', h('input', { name: 'aliases', placeholder: 'web records, reference catalog web' })),
      field('Requirement keys (comma-separated)', h('input', { name: 'requirements', placeholder: '' })),
      field('Tags (comma-separated)', h('input', { name: 'tags', placeholder: 'web, records' }))),
    proposeCapError,
    h('div', { class: 'actions' },
      h('button', { type: 'submit', class: 'primary' }, 'Propose capability draft'))) : null;

  const refPanel = h('section', { class: 'panel', 'data-reference-catalog': '1' },
    h('div', { class: 'panel-head' },
      h('h2', null, 'Reference catalog'),
      h('span', { class: 'badge muted' }, `${(refsResp.items || refsResp.records || []).length} shown`)),
    refTableSlot,
    proposeRefForm ? h('div', { class: 'divider' }) : null,
    proposeRefForm);

  const capPanel = h('section', { class: 'panel', 'data-capability-catalog': '1' },
    h('div', { class: 'panel-head' },
      h('h2', null, 'Capability registry'),
      h('span', { class: 'badge muted' }, `${(capsResp.items || capsResp.records || []).length} shown`)),
    capTableSlot,
    proposeCapForm ? h('div', { class: 'divider' }) : null,
    proposeCapForm);

  return h('div', { class: 'stack', 'data-records-page': '1' },
    pageHead({
      title: 'Records',
      subtitle: `${project.name} · Reference catalog & capability registry`,
    }),
    waitingPanel,
    findPanel,
    refPanel,
    capPanel);
}

export async function referencePage(ctx, { pid, key }) {
  let entry;
  try {
    entry = await ctx.api.reference(pid, key);
  } catch (error) {
    return h('div', null,
      pageHead({ title: key, crumbs: [{ label: 'Records', href: `/p/${pid}/records` }] }),
      errorState(error, () => ctx.render()));
  }

  const rec = entry.record || null;
  const proposed = entry.proposed || null;
  const shown = rec || proposed || {};
  const latest = proposed || rec || {};
  const canPropose = Boolean(entry.can_propose);
  const canCoordinate = Boolean(entry.can_coordinate);
  const acceptance = entry.acceptance || null;
  const pointersVal = entry.pointers_validation || null;
  const auth = shown.authority || {};

  const detailCard = h('section', { class: 'panel', 'data-reference-detail': key },
    h('div', { class: 'panel-head' },
      h('div', { class: 'row' },
        h('h2', { class: 'mono' }, entry.key),
        stateBadge(entry.state),
        h('span', { class: 'badge muted' }, `rev ${shown.revision || 1}`)),
      h('span', { class: 'mono small muted', 'data-record-sha256': shown.sha256 || '' }, shown.sha256 ? `sha256: ${shown.sha256}` : '')),
    h('h3', null, textOf(shown.title) || '—'),
    h('p', null, textOf(shown.statement) || '—'),
    h('dl', { class: 'meta-grid' },
      h('dt', null, 'Native anchor'), h('dd', { class: 'mono' }, entry.native_id || entry.task || '—'),
      h('dt', null, 'Authority kind'), h('dd', { class: 'mono' }, entry.authority_kind || auth.type || '—'),
      h('dt', null, 'Authority'), h('dd', { class: 'mono', 'data-points-to': auth.path || auth.url || auth.id || '' }, formatAuthority(auth)),
      // kittrial-5bb.418: a design document is read on its own page, as text, before it is accepted.
      auth.type === 'repo-document' && String(entry.key || '').startsWith('design.') ? [
        h('dt', null, 'Design document'),
        h('dd', { 'data-design-link': '1' }, h('a', { href: ctx.href ? ctx.href(`/p/${pid}/designs/${encodeURIComponent(String(entry.key).slice(7))}`) : `#/p/${pid}/designs/${encodeURIComponent(String(entry.key).slice(7))}` }, 'Read the stored document'),
          entry.verification ? ' — ' + entry.verification.state : '')] : null,
      h('dt', null, 'Owner'), h('dd', { class: 'mono' }, shown.owner || '—'),
      h('dt', null, 'Review by'), h('dd', { class: 'mono' }, `${shown.review_by || '—'} (${entry.due || 'ok'})`),
      h('dt', null, 'Tags'), h('dd', null, (shown.tags || []).join(', ') || '—'),
      h('dt', null, 'Decisions'), h('dd', { class: 'mono' }, (shown.decisions || []).join(', ') || '—'),
      h('dt', null, 'Proposed by'), h('dd', { 'data-proposer-name': entry.accepted_proposer_name || entry.proposed_by_name || '' },
        entry.accepted_proposer_name || entry.proposed_by_name || entry.accepted_proposer || entry.proposed_by || '—'),
      h('dt', null, 'Accepted by'), h('dd', { 'data-acceptor-name': entry.accepted_by_name || '' },
        entry.accepted_by_name || entry.accepted_by || (acceptance && acceptance.operator) || 'Not yet accepted')),
    pointersVal ? h('p', { class: 'small muted', 'data-pointers-validation': pointersVal.checked || '' },
      `${pointersVal.checked}: ${pointersVal.note || ''}`) : null,
    acceptance ? h('div', { class: 'callout', 'data-acceptance-evidence': '1' },
      h('strong', null, 'Coordinator acceptance evidence'),
      h('p', { class: 'small mono' },
        `Decision: ${acceptance.decision_id || '—'} · Bound sha256: ${acceptance.record_sha256 || '—'} · Policy: ${acceptance.policy || '—'}`),
      h('p', { class: 'small' }, `Evidence: ${textOf(acceptance.evidence) || '—'}`)) : null);

  const isTwoStep = entry.state === 'accepted' && Boolean(proposed);
  const pendingDraftCard = isTwoStep ? h('section', { class: 'panel', 'data-pending-revision': String(proposed.revision) },
    h('div', { class: 'panel-head' },
      h('h2', null, `Pending draft revision ${proposed.revision} (waiting to be accepted)`),
      h('span', { class: 'badge warn' }, 'Two-step revise-then-accept')),
    h('p', { class: 'small muted' },
      `Revision ${rec.revision} remains the accepted reference in force until a coordinator accepts draft revision ${proposed.revision}.`),
    h('dl', { class: 'meta-grid' },
      h('dt', null, 'Draft sha256'), h('dd', { class: 'mono', 'data-proposed-sha256': proposed.sha256 }, proposed.sha256),
      h('dt', null, 'Proposed by'), h('dd', null, entry.proposed_by_name || entry.proposed_by || '—'),
      h('dt', null, 'Title'), h('dd', null, textOf(proposed.title) || '—'),
      h('dt', null, 'Statement'), h('dd', null, textOf(proposed.statement) || '—'),
      h('dt', null, 'Authority'), h('dd', { class: 'mono' }, formatAuthority(proposed.authority)),
      h('dt', null, 'Review by'), h('dd', { class: 'mono' }, proposed.review_by || '—'))) : null;

  const latestAuth = latest.authority || {};
  const defaultAuthTarget = latestAuth.path || latestAuth.url || textOf(latestAuth.how) || latestAuth.id || '';
  const defaultAuthExtra = latestAuth.anchor || textOf(latestAuth.source) || '';
  const defaultAuthCommit = latestAuth.commit || '';

  const reviseError = h('p', { class: 'field-error', hidden: true });
  const reviseForm = canPropose && entry.state !== 'superseded' ? h('section', { class: 'panel', 'data-revise-reference-panel': '1' },
    h('div', { class: 'panel-head' },
      h('h2', null, entry.state === 'accepted'
        ? 'Propose a draft revision to this accepted reference (Step 1 of 2)'
        : 'Revise draft reference')),
    entry.state === 'accepted'
      ? h('p', { class: 'small muted' },
        'Revising an accepted key writes a new draft revision and leaves the current accepted revision in force until a coordinator accepts the draft.')
      : null,
    h('form', {
      class: 'stack',
      'data-revise-reference-form': '1',
      onsubmit: async (e) => {
        e.preventDefault();
        const form = (e.currentTarget || e.target);
        const v = formValues(form);
        reviseError.hidden = true;
        const btn = form.querySelector('button');
        await act(btn, async () => {
          try {
            const fileInput = form.querySelector('input[name=document_file]');
            const designDoc = (v.authority_type || '').trim() === 'repo-document'
              ? await readDesignDocument(fileInput && fileInput.files && fileInput.files[0]) : null;
            const body = {
              revision: Number(latest.revision || 1) + 1,
              expected_sha256: latest.sha256,
              title: (v.title || '').trim(),
              statement: (v.statement || '').trim(),
              authority: buildAuthorityFromForm(v, designDoc),
              owner: (v.owner || latest.owner || '').trim(),
              review_by: (v.review_by || latest.review_by || '').trim(),
              tags: splitCsv(v.tags),
              decisions: splitCsv(v.decisions),
            };
            if (designDoc) body.document = designDoc.text;
            await ctx.api.reviseReference(pid, key, body);
            toast('Draft revision recorded.', 'ok');
            await ctx.render();
          } catch (err) {
            reviseError.textContent = describe(err);
            reviseError.hidden = false;
          }
        });
      },
    },
      h('div', { class: 'row wrap' },
        field('Title', h('input', { name: 'title', value: textOf(latest.title), required: true })),
        field('Owner', h('input', { name: 'owner', value: latest.owner || '', required: true })),
        field('Review by (YYYY-MM-DD)', h('input', { name: 'review_by', value: latest.review_by || defaultReviewDate(), required: true }))),
      h('div', { class: 'row wrap' },
        field('Authority type', h('select', { name: 'authority_type' },
          ['repo-path', 'url', 'attestation', 'decision', 'repo-document'].map((t) =>
            h('option', { value: t, selected: (latestAuth.type || 'repo-path') === t }, t)))),
        field('Authority target', h('input', { name: 'authority_target', value: defaultAuthTarget, required: true })),
        field('Pinned commit (40-hex)', h('input', { name: 'authority_commit', class: 'mono', value: defaultAuthCommit })),
        field('Anchor or source (optional)', h('input', { name: 'authority_extra', value: defaultAuthExtra })),
        field('Design document (Markdown, for repo-document)', h('input', { name: 'document_file', type: 'file', accept: '.md,text/markdown,text/plain' }))),
      h('div', { class: 'row wrap' },
        field('Tags (comma-separated)', h('input', { name: 'tags', value: (latest.tags || []).join(', ') })),
        field('Decisions (comma-separated)', h('input', { name: 'decisions', value: (latest.decisions || []).join(', ') }))),
      field('Statement', h('textarea', { name: 'statement', rows: 2, required: true }, textOf(latest.statement))),
      reviseError,
      h('div', { class: 'actions' },
        h('button', { type: 'submit', class: 'primary' },
          `Record draft revision ${Number(latest.revision || 1) + 1}`)))) : null;

  const acceptError = h('p', { class: 'field-error', hidden: true });
  const retireError = h('p', { class: 'field-error', hidden: true });
  const targetDraft = proposed || rec || {};
  const hasDraftToAccept = Boolean(entry.waiting_acceptance || proposed || entry.state === 'draft-only');

  const coordinatorPanel = canCoordinate ? h('section', { class: 'panel', 'data-coordinator-reference': '1' },
    h('div', { class: 'panel-head' },
      h('h2', null, 'Coordinator actions'),
      entry.approval_by_another_party
        ? h('span', { class: 'badge warn' }, 'Approval by another party required')
        : h('span', { class: 'badge muted' }, 'Self-acceptance permitted')),
    entry.self_accept_blocked
      ? h('div', { class: 'callout warn', 'data-self-accept-blocked': '1' },
        h('strong', null, 'Approval by another party is enabled'),
        h('p', { class: 'small' },
          'You (or your account or one of its agents) proposed the draft revision waiting to be accepted. Another coordinator must accept this revision.'))
      : null,
    hasDraftToAccept ? h('form', {
      class: 'stack',
      'data-accept-reference-form': '1',
      onsubmit: async (e) => {
        e.preventDefault();
        const form = (e.currentTarget || e.target);
        const v = formValues(form);
        acceptError.hidden = true;
        const btn = form.querySelector('button');
        await act(btn, async () => {
          try {
            const body = {
              revision: Number(v.revision || targetDraft.revision),
              record_sha256: (v.record_sha256 || targetDraft.sha256 || '').trim(),
              evidence: (v.evidence || '').trim(),
            };
            if (v.decision_id && v.decision_id.trim()) body.decision_id = v.decision_id.trim();
            await ctx.api.acceptReference(pid, key, body);
            toast('Reference revision accepted.', 'ok');
            await ctx.render();
          } catch (err) {
            acceptError.textContent = describe(err);
            acceptError.hidden = false;
          }
        });
      },
    },
      h('h3', null, `Accept draft revision ${targetDraft.revision}`),
      h('div', { class: 'row wrap' },
        field('Revision to accept', h('input', { name: 'revision', value: String(targetDraft.revision || 1), readonly: true })),
        field('Content sha256', h('input', { name: 'record_sha256', class: 'mono', value: targetDraft.sha256 || '', readonly: true })),
        field('Decision issue ID (required for attested references)', h('input', { name: 'decision_id', placeholder: 'e.g. dec-1' }))),
      field('Acceptance evidence', h('textarea', {
        name: 'evidence',
        rows: 2,
        placeholder: 'Reviewed against repository docs and verified accuracy…',
        required: true,
      })),
      acceptError,
      h('div', { class: 'actions' },
        h('button', { type: 'submit', class: 'primary' }, `Accept revision ${targetDraft.revision}`))) : null,
    entry.state !== 'superseded' ? h('form', {
      class: 'stack',
      'data-retire-reference-form': '1',
      onsubmit: async (e) => {
        e.preventDefault();
        const form = (e.currentTarget || e.target);
        const v = formValues(form);
        retireError.hidden = true;
        const btn = form.querySelector('button');
        await act(btn, async () => {
          try {
            await ctx.api.retireReference(pid, key, {
              revision: Number(latest.revision || 1),
              record_sha256: latest.sha256,
              successor: (v.successor || '').trim(),
              evidence: (v.evidence || '').trim(),
            });
            toast('Reference retired.', 'ok');
            await ctx.render();
          } catch (err) {
            retireError.textContent = describe(err);
            retireError.hidden = false;
          }
        });
      },
    },
      h('h3', null, 'Retire reference'),
      h('div', { class: 'row wrap' },
        field('Successor reference key', h('input', { name: 'successor', placeholder: 'ops.new-reference', required: true })),
        field('Retirement evidence / reason', h('input', { name: 'evidence', placeholder: 'Superseded by new reference…', required: true }))),
      retireError,
      h('div', { class: 'actions' },
        h('button', { type: 'submit', class: 'danger' }, 'Retire reference'))) : null) : null;

  return h('div', { class: 'stack', 'data-reference-page': key },
    pageHead({
      title: entry.key,
      subtitle: `Reference record · ${entry.state}`,
      crumbs: [{ label: 'Records', href: `/p/${pid}/records` }],
    }),
    detailCard,
    pendingDraftCard,
    reviseForm,
    coordinatorPanel);
}

export async function capabilityPage(ctx, { pid, key }) {
  let entry;
  try {
    entry = await ctx.api.capability(pid, key);
  } catch (error) {
    return h('div', null,
      pageHead({ title: key, crumbs: [{ label: 'Records', href: `/p/${pid}/records` }] }),
      errorState(error, () => ctx.render()));
  }

  const rec = entry.record || null;
  const proposed = entry.proposed || null;
  const shown = rec || proposed || {};
  const latest = proposed || rec || {};
  const canPropose = Boolean(entry.can_propose);
  const canCoordinate = Boolean(entry.can_coordinate);
  const acceptance = entry.acceptance || null;
  const verification = entry.verification || {};
  const pointersVal = entry.pointers_validation || null;
  const pendingAliases = entry.aliases_pending || [];

  const detailCard = h('section', { class: 'panel', 'data-capability-detail': key },
    h('div', { class: 'panel-head' },
      h('div', { class: 'row' },
        h('h2', { class: 'mono' }, entry.key),
        stateBadge(entry.state),
        verificationBadge(verification.state),
        h('span', { class: 'badge muted' }, `rev ${shown.revision || 1}`)),
      h('span', { class: 'mono small muted', 'data-record-sha256': shown.sha256 || '' }, shown.sha256 ? `sha256: ${shown.sha256}` : '')),
    h('h3', null, textOf(shown.name) || '—'),
    h('p', null, textOf(shown.summary) || '—'),
    h('dl', { class: 'meta-grid' },
      h('dt', null, 'Native anchor'), h('dd', { class: 'mono' }, entry.native_id || entry.task || '—'),
      h('dt', null, 'Owner'), h('dd', { class: 'mono' }, shown.owner || '—'),
      h('dt', null, 'Code pointers'), h('dd', { class: 'mono small', 'data-capability-code': (shown.code || []).join(',') },
        (shown.code || []).join(', ') || '—'),
      h('dt', null, 'Test pointers'), h('dd', { class: 'mono small' }, (shown.tests || []).join(', ') || '—'),
      h('dt', null, 'Design anchors'), h('dd', { class: 'mono small' }, (shown.anchors || []).join(', ') || '—'),
      h('dt', null, 'Requirements'), h('dd', { class: 'mono small' }, formatRequirements(shown.requirements).join(', ') || '—'),
      h('dt', null, 'Aliases'), h('dd', { 'data-capability-aliases': (shown.aliases || []).join(',') },
        (shown.aliases || []).join(', ') || '—'),
      h('dt', null, 'Pending aliases'), h('dd', { 'data-pending-aliases': pendingAliases.map((a) => textOf(a.alias)).join(',') },
        pendingAliases.length ? pendingAliases.map((a) => `${textOf(a.alias)} (${a.actor_name || a.actor || (a.submitter && a.submitter.person) || 'unverified'})`).join(', ') : 'None'),
      h('dt', null, 'Tags'), h('dd', null, (shown.tags || []).join(', ') || '—'),
      h('dt', null, 'Proposed by'), h('dd', { 'data-proposer-name': entry.accepted_proposer_name || entry.proposed_by_name || '' },
        entry.accepted_proposer_name || entry.proposed_by_name || entry.accepted_proposer || entry.proposed_by || '—'),
      h('dt', null, 'Accepted by'), h('dd', { 'data-acceptor-name': entry.accepted_by_name || '' },
        entry.accepted_by_name || entry.accepted_by || (acceptance && acceptance.operator) || 'Not yet accepted')),
    pointersVal ? h('p', { class: 'small muted', 'data-pointers-validation': pointersVal.checked || '' },
      `${pointersVal.checked}: ${pointersVal.note || ''}`) : null,
    acceptance ? h('div', { class: 'callout', 'data-acceptance-evidence': '1' },
      h('strong', null, 'Coordinator acceptance evidence'),
      h('p', { class: 'small mono' },
        `Decision: ${acceptance.decision_id || '—'} · Bound sha256: ${acceptance.record_sha256 || '—'} · Policy: ${acceptance.policy || '—'}`),
      h('p', { class: 'small' }, `Evidence: ${textOf(acceptance.evidence) || '—'}`)) : null,
    verification && verification.verifier ? h('div', { class: 'callout', 'data-verification-evidence': '1' },
      h('strong', null, `Latest verification (${verification.state})`),
      h('p', { class: 'small mono' },
        `Commit: ${verification.commit || '—'} · Checked at: ${verification.checked_at || '—'} · Verifier: ${verification.verifier_name || verification.verifier}`)) : null);

  const isTwoStep = entry.state === 'accepted' && Boolean(proposed);
  const pendingDraftCard = isTwoStep ? h('section', { class: 'panel', 'data-pending-revision': String(proposed.revision) },
    h('div', { class: 'panel-head' },
      h('h2', null, `Pending draft revision ${proposed.revision} (waiting to be accepted)`),
      h('span', { class: 'badge warn' }, 'Two-step revise-then-accept')),
    h('p', { class: 'small muted' },
      `Revision ${rec.revision} remains the accepted capability in force until a coordinator accepts draft revision ${proposed.revision}.`),
    h('dl', { class: 'meta-grid' },
      h('dt', null, 'Draft sha256'), h('dd', { class: 'mono', 'data-proposed-sha256': proposed.sha256 }, proposed.sha256),
      h('dt', null, 'Proposed by'), h('dd', null, entry.proposed_by_name || entry.proposed_by || '—'),
      h('dt', null, 'Name'), h('dd', null, textOf(proposed.name) || '—'),
      h('dt', null, 'Summary'), h('dd', null, textOf(proposed.summary) || '—'),
      h('dt', null, 'Code pointers'), h('dd', { class: 'mono small' }, (proposed.code || []).join(', ') || '—'))) : null;

  const reviseError = h('p', { class: 'field-error', hidden: true });
  const reviseForm = canPropose && entry.state !== 'superseded' ? h('section', { class: 'panel', 'data-revise-capability-panel': '1' },
    h('div', { class: 'panel-head' },
      h('h2', null, entry.state === 'accepted'
        ? 'Propose a draft revision to this accepted capability (Step 1 of 2)'
        : 'Revise draft capability')),
    entry.state === 'accepted'
      ? h('p', { class: 'small muted' },
        'Revising an accepted capability writes a new draft revision and leaves the current accepted revision in force until a coordinator accepts the draft.')
      : null,
    h('form', {
      class: 'stack',
      'data-revise-capability-form': '1',
      onsubmit: async (e) => {
        e.preventDefault();
        const form = (e.currentTarget || e.target);
        const v = formValues(form);
        reviseError.hidden = true;
        const btn = form.querySelector('button');
        await act(btn, async () => {
          try {
            await ctx.api.reviseCapability(pid, key, {
              revision: Number(latest.revision || 1) + 1,
              expected_sha256: latest.sha256,
              name: (v.name || '').trim(),
              aliases: splitCsv(v.aliases),
              summary: (v.summary || '').trim(),
              requirements: parseRequirements(v.requirements),
              anchors: splitLines(v.anchors),
              code: splitLines(v.code),
              tests: splitLines(v.tests),
              owner: (v.owner || latest.owner || '').trim(),
              tags: splitCsv(v.tags),
            });
            toast('Capability draft revision recorded.', 'ok');
            await ctx.render();
          } catch (err) {
            reviseError.textContent = describe(err);
            reviseError.hidden = false;
          }
        });
      },
    },
      h('div', { class: 'row wrap' },
        field('Name', h('input', { name: 'name', value: textOf(latest.name), required: true })),
        field('Owner', h('input', { name: 'owner', value: latest.owner || '', required: true }))),
      field('Summary', h('textarea', { name: 'summary', rows: 2, required: true }, textOf(latest.summary))),
      h('div', { class: 'row wrap' },
        field('Code pointers (one per line)', h('textarea', { name: 'code', rows: 2 }, (latest.code || []).join('\n'))),
        field('Test pointers (one per line)', h('textarea', { name: 'tests', rows: 2 }, (latest.tests || []).join('\n'))),
        field('Design anchors (one per line)', h('textarea', { name: 'anchors', rows: 2 }, (latest.anchors || []).join('\n')))),
      h('div', { class: 'row wrap' },
        field('Aliases (comma-separated)', h('input', { name: 'aliases', value: (latest.aliases || []).join(', ') })),
        field('Requirement keys (comma-separated)', h('input', { name: 'requirements', value: formatRequirements(latest.requirements).join(', ') })),
        field('Tags (comma-separated)', h('input', { name: 'tags', value: (latest.tags || []).join(', ') }))),
      reviseError,
      h('div', { class: 'actions' },
        h('button', { type: 'submit', class: 'primary' },
          `Record draft revision ${Number(latest.revision || 1) + 1}`)))) : null;

  // Member Alias proposal form
  const aliasError = h('p', { class: 'field-error', hidden: true });
  const aliasPanel = canPropose && entry.state !== 'superseded' ? h('section', { class: 'panel', 'data-capability-alias-panel': '1' },
    h('div', { class: 'panel-head' }, h('h2', null, 'Propose capability alias')),
    h('form', {
      class: 'stack',
      'data-propose-alias-form': '1',
      onsubmit: async (e) => {
        e.preventDefault();
        const form = (e.currentTarget || e.target);
        const v = formValues(form);
        aliasError.hidden = true;
        const btn = form.querySelector('button');
        await act(btn, async () => {
          try {
            const payload = { alias: (v.alias || '').trim() };
            if (v.evidence && v.evidence.trim()) payload.evidence = v.evidence.trim();
            await ctx.api.proposeCapabilityAlias(pid, key, payload);
            toast('Capability alias proposed.', 'ok');
            await ctx.render();
          } catch (err) {
            aliasError.textContent = describe(err);
            aliasError.hidden = false;
          }
        });
      },
    },
      h('div', { class: 'row wrap' },
        field('Alias phrase', h('input', { name: 'alias', placeholder: 'reference catalog web', required: true })),
        field('Evidence pointer (optional)', h('input', { name: 'evidence', placeholder: 'docs/HTTP_DEPLOYMENT.md#records' }))),
      aliasError,
      h('div', { class: 'actions' },
        h('button', { type: 'submit' }, 'Propose alias')))) : null;

  // Coordinator panel: Accept, Verify, Retire, Reject alias
  const acceptError = h('p', { class: 'field-error', hidden: true });
  const verifyError = h('p', { class: 'field-error', hidden: true });
  const retireError = h('p', { class: 'field-error', hidden: true });
  const rejectAliasError = h('p', { class: 'field-error', hidden: true });
  const targetDraft = proposed || rec || {};
  const hasDraftToAccept = Boolean(entry.waiting_acceptance || proposed || entry.state === 'draft-only');

  const coordinatorPanel = canCoordinate ? h('section', { class: 'panel', 'data-coordinator-capability': '1' },
    h('div', { class: 'panel-head' },
      h('h2', null, 'Coordinator actions'),
      entry.approval_by_another_party
        ? h('span', { class: 'badge warn' }, 'Approval by another party required')
        : h('span', { class: 'badge muted' }, 'Self-acceptance permitted')),
    entry.self_accept_blocked
      ? h('div', { class: 'callout warn', 'data-self-accept-blocked': '1' },
        h('strong', null, 'Approval by another party is enabled for acceptance'),
        h('p', { class: 'small' },
          'You (or your account or one of its agents) proposed the draft revision waiting to be accepted. Another coordinator must accept this revision.'))
      : null,
    entry.self_verify_blocked
      ? h('div', { class: 'callout warn', 'data-self-verify-blocked': '1' },
        h('strong', null, 'Approval by another party is enabled for verification'),
        h('p', { class: 'small' },
          'You (or your account or one of its agents) proposed the current revision. Another coordinator must verify this capability.'))
      : null,
    hasDraftToAccept ? h('form', {
      class: 'stack',
      'data-accept-capability-form': '1',
      onsubmit: async (e) => {
        e.preventDefault();
        const form = (e.currentTarget || e.target);
        const v = formValues(form);
        acceptError.hidden = true;
        const btn = form.querySelector('button');
        await act(btn, async () => {
          try {
            await ctx.api.acceptCapability(pid, key, {
              revision: Number(v.revision || targetDraft.revision),
              record_sha256: (v.record_sha256 || targetDraft.sha256 || '').trim(),
              evidence: (v.evidence || '').trim(),
            });
            toast('Capability revision accepted.', 'ok');
            await ctx.render();
          } catch (err) {
            acceptError.textContent = describe(err);
            acceptError.hidden = false;
          }
        });
      },
    },
      h('h3', null, `Accept draft revision ${targetDraft.revision}`),
      h('div', { class: 'row wrap' },
        field('Revision to accept', h('input', { name: 'revision', value: String(targetDraft.revision || 1), readonly: true })),
        field('Content sha256', h('input', { name: 'record_sha256', class: 'mono', value: targetDraft.sha256 || '', readonly: true }))),
      field('Acceptance evidence', h('textarea', {
        name: 'evidence',
        rows: 2,
        placeholder: 'Reviewed capability summary, pointers, and requirements…',
        required: true,
      })),
      acceptError,
      h('div', { class: 'actions' },
        h('button', { type: 'submit', class: 'primary' }, `Accept revision ${targetDraft.revision}`))) : null,
    entry.state !== 'superseded' ? h('form', {
      class: 'stack',
      'data-verify-capability-form': '1',
      onsubmit: async (e) => {
        e.preventDefault();
        const form = (e.currentTarget || e.target);
        const v = formValues(form);
        verifyError.hidden = true;
        const btn = form.querySelector('button');
        await act(btn, async () => {
          try {
            const passed = v.passed !== 'false';
            const pointers = [...(shown.code || []), ...(shown.tests || []), ...(shown.anchors || [])];
            const results = pointers.map((pointer) => ({
              pointer,
              resolved: passed,
              reason: passed ? null : 'missing-pointer',
            }));
            await ctx.api.verifyCapability(pid, key, {
              revision: Number(v.revision || shown.revision),
              record_sha256: (v.record_sha256 || shown.sha256 || '').trim(),
              commit: (v.commit || 'a'.repeat(40)).trim(),
              checked_at: (v.checked_at || new Date().toISOString().replace(/\.\d{3}Z$/, 'Z')).trim(),
              source: (v.source || 'ast').trim(),
              passed,
              results,
            });
            toast('Capability verification recorded.', 'ok');
            await ctx.render();
          } catch (err) {
            verifyError.textContent = describe(err);
            verifyError.hidden = false;
          }
        });
      },
    },
      h('h3', null, `Verify revision ${shown.revision}`),
      h('div', { class: 'row wrap' },
        field('Revision', h('input', { name: 'revision', value: String(shown.revision || 1), readonly: true })),
        field('Content sha256', h('input', { name: 'record_sha256', class: 'mono', value: shown.sha256 || '', readonly: true }))),
      h('div', { class: 'row wrap' },
        field('Tested commit (40-hex)', h('input', { name: 'commit', class: 'mono', value: 'a'.repeat(40), required: true })),
        field('Checked at (UTC ISO-8601)', h('input', { name: 'checked_at', value: new Date().toISOString().replace(/\.\d{3}Z$/, 'Z'), required: true })),
        field('Source', h('select', { name: 'source' },
          ['ast', 'graph'].map((s) => h('option', { value: s }, s)))),
        field('Outcome', h('select', { name: 'passed' },
          h('option', { value: 'true' }, 'Passed (all pointers resolved)'),
          h('option', { value: 'false' }, 'Failed (drifted)')))),
      verifyError,
      h('div', { class: 'actions' },
        h('button', { type: 'submit', class: 'primary' }, 'Record verification'))) : null,
    entry.state !== 'superseded' ? h('form', {
      class: 'stack',
      'data-retire-capability-form': '1',
      onsubmit: async (e) => {
        e.preventDefault();
        const form = (e.currentTarget || e.target);
        const v = formValues(form);
        retireError.hidden = true;
        const btn = form.querySelector('button');
        await act(btn, async () => {
          try {
            await ctx.api.retireCapability(pid, key, {
              revision: Number(latest.revision || 1),
              record_sha256: latest.sha256,
              successor: (v.successor || '').trim(),
              evidence: (v.evidence || '').trim(),
            });
            toast('Capability retired.', 'ok');
            await ctx.render();
          } catch (err) {
            retireError.textContent = describe(err);
            retireError.hidden = false;
          }
        });
      },
    },
      h('h3', null, 'Retire capability'),
      h('div', { class: 'row wrap' },
        field('Successor capability key', h('input', { name: 'successor', placeholder: 'records.new-capability', required: true })),
        field('Retirement evidence / reason', h('input', { name: 'evidence', placeholder: 'Replaced by successor capability…', required: true }))),
      retireError,
      h('div', { class: 'actions' },
        h('button', { type: 'submit', class: 'danger' }, 'Retire capability'))) : null,
    h('form', {
      class: 'stack',
      'data-reject-alias-form': '1',
      onsubmit: async (e) => {
        e.preventDefault();
        const form = (e.currentTarget || e.target);
        const v = formValues(form);
        rejectAliasError.hidden = true;
        const btn = form.querySelector('button');
        await act(btn, async () => {
          try {
            await ctx.api.rejectCapabilityAlias(pid, key, {
              alias: (v.alias || '').trim(),
              reason: (v.reason || '').trim(),
            });
            toast('Capability alias rejected.', 'ok');
            await ctx.render();
          } catch (err) {
            rejectAliasError.textContent = describe(err);
            rejectAliasError.hidden = false;
          }
        });
      },
    },
      h('h3', null, 'Reject a capability alias'),
      h('div', { class: 'row wrap' },
        field('Alias to reject', h('input', { name: 'alias', placeholder: 'old alias phrase', required: true })),
        field('Reason', h('input', { name: 'reason', placeholder: 'Why this alias should not resolve…', required: true }))),
      rejectAliasError,
      h('div', { class: 'actions' },
        h('button', { type: 'submit' }, 'Reject alias')))) : null;

  return h('div', { class: 'stack', 'data-capability-page': key },
    pageHead({
      title: entry.key,
      subtitle: `Capability record · ${entry.state}`,
      crumbs: [{ label: 'Records', href: `/p/${pid}/records` }],
    }),
    detailCard,
    pendingDraftCard,
    reviseForm,
    aliasPanel,
    coordinatorPanel);
}
