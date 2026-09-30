// Requirements (BRD) and decisions: browse the current baseline, drill into each
// requirement's revision and the decisions, reviews and evidence behind it.
import { h, time } from '../dom.js';
import { pageHead, empty } from '../ui.js';
import { markdown, section, mentions } from '../md.js';

const STATE = { accepted: ['Accepted', 'ok'], draft: ['Draft', 'warn'], proposed: ['Proposed', 'accent'], rejected: ['Rejected', 'crit'] };
const stateChip = (s) => { const [label, tone] = STATE[s] || [s, '']; return h('span', { class: 'chip ' + tone }, label); };
const KIND = {
  'requirement-revision-v1': 'Requirement revision', review: 'Review', disposition: 'Review disposition', 'publication evidence': 'Publication evidence',
  plan: 'Plan', checkpoint: 'Checkpoint', 'acceptance report': 'Acceptance report', 'completion report': 'Completion report', correction: 'Correction',
  'current handoff': 'Handoff', 'blocked launch': 'Blocked launch', 'implementation contract': 'Implementation contract',
};

function crumbs(ctx, project, trail) {
  return [{ label: 'Projects', href: ctx.href('/projects') }, { label: project.name, href: ctx.href('/p/' + project.id) }].concat(trail);
}

// Turns a record ID mentioned in text into a link when this project knows it.
function linker(ctx, pid, known, keys = {}) {
  return (token) => {
    if (keys[token]) return h('a', { href: ctx.href(`/p/${pid}/requirements/${keys[token]}`), class: 'mono', title: keys[token] }, token);
    return known.has(token) ? h('a', { href: ctx.href(`/p/${pid}/records/${token}`), class: 'mono' }, token) : null;
  };
}

export async function brd(ctx, { pid }) {
  const [project, data] = await Promise.all([ctx.api.project(pid), ctx.api.requirements(pid)]);
  if (!data.baseline) {
    return h('div', { class: 'stack' }, pageHead({ crumbs: crumbs(ctx, project, [{ label: 'Requirements' }]), title: 'Requirements' }),
      h('div', { class: 'panel' }, empty('No requirements baseline yet', 'When the team records requirements and publishes a baseline, the business requirements document appears here.')));
  }
  const known = new Set(data.known || []);
  const link = linker(ctx, pid, known, data.keys);
  const b = data.baseline;
  const counts = data.items.reduce((acc, r) => { acc[r.acceptance_state] = (acc[r.acceptance_state] || 0) + 1; return acc; }, {});

  const baselineCard = h('section', { class: 'panel baseline', 'aria-labelledby': 'baseline-h' },
    h('div', { class: 'panel-head' },
      h('h2', { class: 'small', id: 'baseline-h' }, 'Baseline ', h('span', { class: 'mono' }, b.name)),
      stateChip(b.state)),
    h('div', { class: 'panel-body' },
      b.note ? h('p', null, b.note) : null,
      h('dl', { class: 'kv' },
        h('dt', null, 'Requirements'), h('dd', null, `${data.items.length} — ${Object.entries(counts).map(([k, v]) => `${v} ${k}`).join(', ')}`),
        b.manifest_sha256 ? [h('dt', null, 'Content hash'), h('dd', null, h('code', { class: 'hash', title: b.manifest_sha256 }, b.manifest_sha256))] : null,
        b.record ? [h('dt', null, 'Publication'), h('dd', null, link(b.record) || b.record)] : null,
        b.review ? [h('dt', null, 'Review'), h('dd', null, link(b.review) || b.review)] : null),
      h('div', { class: 'actions' },
        h('a', { class: 'btn', href: ctx.href(`/p/${pid}/decisions`) }, `Decisions (${data.decision_count || 0})`),
        b.review ? h('a', { class: 'btn', href: ctx.href(`/p/${pid}/records/${b.review}`) }, 'Review and dispositions') : null)));

  const narrative = data.narrative ? h('section', { class: 'panel', 'aria-labelledby': 'narr-h' },
    h('div', { class: 'panel-head' }, h('h2', { class: 'small', id: 'narr-h' }, 'Purpose, users and scope'),
      h('span', { class: 'small muted' }, h('a', { href: ctx.href(`/p/${pid}/requirements/${data.narrative.id}`) }, 'revision ', data.narrative.revision), ' · ', stateChip(data.narrative.acceptance_state))),
    h('div', { class: 'panel-body prose-md' }, markdown(data.narrative.description, { link }))) : null;

  const table = h('section', { class: 'panel', 'aria-labelledby': 'req-h' },
    h('div', { class: 'panel-head' }, h('h2', { class: 'small', id: 'req-h' }, 'Requirements ', h('span', { class: 'nav-count' }, data.items.length))),
    h('div', { class: 'table-wrap' }, h('table', null,
      h('thead', null, h('tr', null, h('th', { scope: 'col' }, 'ID'), h('th', { scope: 'col' }, 'Requirement'), h('th', { scope: 'col' }, 'State'), h('th', { scope: 'col', class: 'hide-narrow' }, 'Revision'))),
      h('tbody', null, data.items.map((r) => {
        const summary = section(r.description, 'Requirement') || '';
        return h('tr', { class: 'row-link', onclick: (e) => { if (e.target.tagName !== 'A') ctx.go(`/p/${pid}/requirements/${r.id}`); } },
          h('td', { class: 'mono' }, r.key),
          h('td', null, h('a', { class: 'title', href: ctx.href(`/p/${pid}/requirements/${r.id}`) }, r.title.replace(/^R\d+:\s*/, '')), h('div', { class: 'sub clamp' }, summary)),
          h('td', null, stateChip(r.acceptance_state)),
          h('td', { class: 'hide-narrow num' }, String(r.revision)));
      })))));

  return h('div', { class: 'stack' },
    pageHead({ crumbs: crumbs(ctx, project, [{ label: 'Requirements' }]), title: 'Business requirements',
      lede: 'The current baseline, published from the project’s requirement records. This page is a reading view; requirements change through recorded revisions and owner decisions, not by editing text here.' }),
    b.state !== 'accepted' ? h('div', { class: 'banner' }, h('strong', null, 'Not an accepted baseline. '), 'This is a draft for review. Treat criteria as proposals until an owner records acceptance.') : null,
    baselineCard, narrative, table);
}

export async function requirement(ctx, { pid, rid }) {
  const [project, data] = await Promise.all([ctx.api.project(pid), ctx.api.requirement(pid, rid)]);
  const r = data.requirement;
  const known = new Set(data.known || []);
  const link = linker(ctx, pid, known, data.keys);
  const parts = [['Requirement', null], ['Rationale', null], ['Acceptance criteria', null], ['Acceptance state', null]].map(([name]) => [name, section(r.description, name)]);
  const hasSections = parts.some(([, v]) => v);
  const body = hasSections ? parts.filter(([, v]) => v).map(([name, v]) => h('section', { class: 'req-part' }, h('h3', { class: 'small' }, name), markdown(v, { link }))) : [markdown(r.description, { link })];

  const decisionList = (items, note) => items.length ? h('ul', { class: 'open-items' }, items.map((d) => h('li', null,
    h('a', { href: ctx.href(`/p/${pid}/decisions/${d.id}`) }, d.title.replace(/^Decision:\s*/, '')), ' ', h('span', { class: 'chip ' + (d.status === 'closed' ? 'ok' : 'accent') }, d.status === 'closed' ? 'Settled' : 'In force'),
    d.why ? h('div', { class: 'small muted' }, d.why) : null))) : h('p', { class: 'small muted' }, note);

  return h('div', { class: 'stack' },
    pageHead({ crumbs: crumbs(ctx, project, [{ label: 'Requirements', href: ctx.href(`/p/${pid}/requirements`) }, { label: r.key || r.id }]), title: r.title }),
    h('div', { class: 'toolbar' }, stateChip(r.acceptance_state), h('span', { class: 'small muted' }, 'Revision ', r.revision, ' · recorded ', time(r.recorded_at), ' by ', r.recorded_by)),
    r.acceptance_state !== 'accepted' ? h('div', { class: 'banner' }, 'Draft wording. The owner approved the direction, not necessarily this text or every criterion.') : null,
    h('div', { class: 'detail' },
      h('div', { class: 'stack' },
        h('section', { class: 'panel' }, h('div', { class: 'panel-body prose-md' }, body)),
        h('section', { class: 'panel', 'aria-labelledby': 'why-h' },
          h('div', { class: 'panel-head' }, h('h2', { class: 'small', id: 'why-h' }, 'How we got here')),
          h('div', { class: 'panel-body' },
            h('h3', { class: 'small' }, 'Decisions that name this requirement'),
            decisionList(data.decisions.direct, 'No recorded decision names this requirement directly. Its rationale cites the owner’s direction; decisions below govern the whole baseline.'),
            h('h3', { class: 'small' }, 'Decisions governing the whole baseline'),
            decisionList(data.decisions.baseline, 'None recorded.'),
            data.mentioned_by.length ? [h('h3', { class: 'small' }, 'Other records that mention it'),
              h('ul', { class: 'open-items' }, data.mentioned_by.map((m) => h('li', null, link(m.id) || m.id, ' ', m.title)))] : null))),
      h('aside', { class: 'side', 'aria-label': 'Revision details' },
        h('section', { class: 'panel' }, h('div', { class: 'panel-body' }, h('dl', { class: 'kv' },
          h('dt', null, 'Key'), h('dd', { class: 'mono' }, r.key || '—'),
          h('dt', null, 'Record'), h('dd', null, link(r.id) || r.id),
          h('dt', null, 'Revision'), h('dd', { class: 'num' }, String(r.revision), r.revisions > 1 ? ` of ${r.revisions}` : ''),
          h('dt', null, 'State'), h('dd', null, stateChip(r.acceptance_state)),
          h('dt', null, 'Content hash'), h('dd', null, h('code', { class: 'hash', title: r.sha256 }, r.sha256))))),
        h('p', { class: 'small muted' }, 'The content hash identifies this exact wording. A changed requirement gets a new revision; this one stays reproducible.'))));
}

export async function decisions(ctx, { pid }) {
  const [project, data] = await Promise.all([ctx.api.project(pid), ctx.api.decisions(pid)]);
  return h('div', { class: 'stack' },
    pageHead({ crumbs: crumbs(ctx, project, [{ label: 'Requirements', href: ctx.href(`/p/${pid}/requirements`) }, { label: 'Decisions' }]), title: 'Decisions',
      lede: 'Choices the team recorded, with the authority, rationale and alternatives behind them. Newest first.' }),
    data.items.length ? h('section', { class: 'panel' }, h('ul', { class: 'decision-list' }, data.items.map((d) => h('li', null,
      h('div', { class: 'toolbar' }, h('a', { class: 'title', href: ctx.href(`/p/${pid}/decisions/${d.id}`) }, d.title.replace(/^Decision:\s*/, '')),
        h('span', { class: 'chip ' + (d.status === 'closed' ? 'ok' : 'accent') }, d.status === 'closed' ? 'Settled' : 'In force')),
      d.summary ? h('p', { class: 'small muted clamp' }, d.summary) : null,
      h('div', { class: 'small muted' }, h('span', { class: 'mono' }, d.id), ' · recorded ', time(d.created_at)))))) :
      h('div', { class: 'panel' }, empty('No decisions recorded', 'Decisions appear here once someone records one with its rationale.')));
}

export async function decision(ctx, { pid, did }) {
  const [project, data] = await Promise.all([ctx.api.project(pid), ctx.api.record(pid, did)]);
  const d = data.record;
  const known = new Set(data.known || []);
  const link = linker(ctx, pid, known, data.keys);
  const parts = ['Decision', 'Rationale', 'Alternatives', 'Alternatives Considered', 'Consequences', 'Scope'].map((name) => [name, section(d.description, name)]).filter(([, v]) => v);
  return h('div', { class: 'stack' },
    pageHead({ crumbs: crumbs(ctx, project, [{ label: 'Decisions', href: ctx.href(`/p/${pid}/decisions`) }, { label: d.id }]), title: d.title.replace(/^Decision:\s*/, '') }),
    h('div', { class: 'toolbar' }, h('span', { class: 'chip ' + (d.status === 'closed' ? 'ok' : 'accent') }, d.status === 'closed' ? 'Settled' : 'In force'), h('span', { class: 'small muted' }, 'Recorded ', time(d.created_at))),
    h('div', { class: 'detail' },
      h('div', { class: 'stack' },
        h('section', { class: 'panel' }, h('div', { class: 'panel-body prose-md decision-body' },
          parts.length ? parts.map(([name, v]) => h('section', { class: 'req-part' }, h('h3', { class: 'small' }, name.replace(' Considered', '')), markdown(v, { link }))) : markdown(d.description, { link }))),
        recordComments(d, link)),
      h('aside', { class: 'side', 'aria-label': 'Connections' },
        data.references.length ? h('section', { class: 'panel' }, h('div', { class: 'panel-head' }, h('h2', { class: 'small' }, 'Refers to')),
          h('ul', { class: 'panel-body open-items' }, data.references.map((m) => h('li', null, link(m.id) || m.id, h('div', { class: 'small muted' }, m.title))))) : null,
        data.mentioned_by.length ? h('section', { class: 'panel' }, h('div', { class: 'panel-head' }, h('h2', { class: 'small' }, 'Mentioned by')),
          h('ul', { class: 'panel-body open-items' }, data.mentioned_by.map((m) => h('li', null, link(m.id) || m.id, h('div', { class: 'small muted' }, m.title))))) : null)));
}

function recordComments(rec, link) {
  if (!rec.comments || !rec.comments.length) return null;
  return h('section', { class: 'panel', 'aria-labelledby': 'rc-h' },
    h('div', { class: 'panel-head' }, h('h2', { class: 'small', id: 'rc-h' }, 'Record history'), h('span', { class: 'small muted' }, 'Oldest first')),
    h('ul', { class: 'timeline panel-body' }, rec.comments.map((c) => {
      const label = KIND[c.kind] || (c.kind ? c.kind[0].toUpperCase() + c.kind.slice(1) : 'Comment');
      let body;
      if (c.body && typeof c.body === 'object') {
        body = c.body.description ? [h('p', { class: 'small muted' }, `${c.body.title || ''} — revision ${c.body.revision}, ${c.body.acceptance_state}`), markdown(c.body.description, { link })] : h('pre', { class: 'json' }, JSON.stringify(c.body, null, 2));
      } else {
        body = markdown(String(c.body || ''), { link, headingBase: 4 });
      }
      const long = typeof c.body === 'string' && c.body.length > 1200;
      const content = h('div', { class: 'event-body prose-md' + (long ? ' collapsible' : '') }, body);
      const toggle = long ? h('button', { type: 'button', class: 'link', onclick: (e) => { content.classList.toggle('expanded'); e.currentTarget.textContent = content.classList.contains('expanded') ? 'Show less' : 'Show all'; } }, 'Show all') : null;
      return h('li', null, h('span', { class: 'dot ' + (/review|disposition/.test(c.kind || '') ? 'warn' : /acceptance|evidence/.test(c.kind || '') ? 'ok' : 'accent'), 'aria-hidden': 'true' }),
        h('div', null, h('div', { class: 'event-head' }, h('span', { class: 'chip plain' }, label), h('strong', null, c.author), time(c.at)), content, toggle));
    })));
}

export async function record(ctx, { pid, id }) {
  const [project, data] = await Promise.all([ctx.api.project(pid), ctx.api.record(pid, id)]);
  const d = data.record;
  if (data.kind === 'decision') return decision(ctx, { pid, did: id });
  if (data.kind === 'requirement') return requirement(ctx, { pid, rid: id });
  const known = new Set(data.known || []);
  const link = linker(ctx, pid, known, data.keys);
  return h('div', { class: 'stack' },
    pageHead({ crumbs: crumbs(ctx, project, [{ label: 'Requirements', href: ctx.href(`/p/${pid}/requirements`) }, { label: d.id }]), title: d.title }),
    h('div', { class: 'toolbar' }, h('span', { class: 'chip ' + (d.status === 'closed' ? 'ok' : 'accent') }, d.status === 'closed' ? 'Closed' : 'Open'), h('span', { class: 'small muted' }, d.type || 'record', ' · created ', time(d.created_at))),
    h('div', { class: 'detail' },
      h('div', { class: 'stack' },
        d.description ? h('section', { class: 'panel' }, h('div', { class: 'panel-body prose-md' }, markdown(d.description, { link }))) : null,
        recordComments(d, link)),
      h('aside', { class: 'side', 'aria-label': 'Connections' },
        data.references.length ? h('section', { class: 'panel' }, h('div', { class: 'panel-head' }, h('h2', { class: 'small' }, 'Refers to')),
          h('ul', { class: 'panel-body open-items' }, data.references.map((m) => h('li', null, link(m.id) || m.id, h('div', { class: 'small muted' }, m.title))))) : null,
        data.mentioned_by.length ? h('section', { class: 'panel' }, h('div', { class: 'panel-head' }, h('h2', { class: 'small' }, 'Mentioned by')),
          h('ul', { class: 'panel-body open-items' }, data.mentioned_by.map((m) => h('li', null, link(m.id) || m.id, h('div', { class: 'small muted' }, m.title))))) : null)));
}

export { mentions };
