// The design page (kittrial-5bb.418; TRACEABILITY_DESIGN §5.2): a design.<scope> entry's stored
// document rendered as text, with the anchors the server derived, each section's req:/design: tokens,
// its plan phases and its verification state. Every node is built with h() as text; nothing in the
// document can become markup, a link or a script (§7, "the stored document leaks or injects").
import { h } from '../dom.js';
import { pageHead, errorState } from '../ui.js';

const FENCE = /^ {0,3}(`{3,}|~{3,})/;
const BOM = /^﻿/;

// The document as text blocks. The server says which line each of its index's headings is on
// (`heading_lines`, from the same capabilities.markdown_headings that builds the index), and a line is a
// heading here exactly when the server says so: its level and title are the server's, never a second
// match of the heading rule (review of part 2, N4 and n1). Lines are split as the server splits them: on
// LF only, every trailing CR dropped (Python's rstrip of CR); a lone CR or U+2028 splits nothing. Fences
// are shown verbatim.
export function renderDocument(text, headings, headingLines) {
  const root = h('div', { class: 'md design-document', 'data-design-document': '1' });
  const lines = String(text || '').replace(BOM, '').split('\n').map((line) => line.replace(/\r+$/, ''));
  const byLine = new Map();
  (headingLines || []).forEach((number, n) => { if ((headings || [])[n]) byLine.set(number, headings[n]); });
  let para = []; let fence = null; let code = []; let lineNumber = 0;
  const flush = () => { if (para.length) { root.append(h('p', null, para.join(' '))); para = []; } };
  for (const raw of lines) {
    lineNumber += 1;
    const marker = FENCE.exec(raw);
    if (fence !== null) {
      if (marker && marker[1][0] === fence[0] && marker[1].length >= fence.length) {
        root.append(h('pre', null, h('code', null, code.join('\n')))); fence = null; code = [];
      } else code.push(raw);
      continue;
    }
    if (marker) { flush(); fence = marker[1]; continue; }
    const item = byLine.get(lineNumber) || null;
    if (item) {
      flush();
      const level = Math.min(6, Math.max(1, Number(item.level) || 1) + 1);
      root.append(h('h' + level, { class: 'md-h', id: 'design-' + item.anchor }, String(item.title || '')));
      if (item.tokens && item.tokens.length) {
        root.append(h('p', { class: 'small muted', 'data-section-tokens': item.anchor },
          'Cites: ', item.tokens.map((token, n) => [n ? ', ' : '', h('code', null, token)])));
      }
      continue;
    }
    if (!raw.trim()) { flush(); continue; }
    para.push(raw.trim());
  }
  if (fence !== null) root.append(h('pre', null, h('code', null, code.join('\n'))));
  flush();
  return root;
}

function verificationLine(v) {
  if (!v) return 'not checked (a draft revision)';
  if (v.state === 'verified') return `verified by ${v.verified_by} (${v.verified_at})`;
  if (v.state === 'design-drift') {
    return `design-drift: ${v.reason}` + (v.trusted ? ' (a trusted verifier)' : ` (reported by ${(v.reported_by || []).join(', ')}; trusted: false)`);
  }
  return 'unverified';
}

export async function page(ctx, { pid, scope }) {
  let design;
  try {
    design = await ctx.api.design(pid, scope);
  } catch (error) {
    return h('div', null, pageHead({ title: 'design.' + scope, crumbs: [['Records', `/p/${pid}/records`]] }),
      errorState(error, () => ctx.render()));
  }
  const front = (design.index && design.index.front) || {};
  const phases = (design.index && design.index.plan_phases) || [];
  return h('div', { class: 'stack', 'data-design-page': design.key },
    pageHead({ title: design.key, lede: design.title, crumbs: [['Records', `/p/${pid}/records`], [design.key, `/p/${pid}/references/${encodeURIComponent(design.key)}`]] }),
    h('section', { class: 'panel' },
      h('dl', { class: 'kv' },
        h('dt', null, 'Revision'), h('dd', null, String(design.revision), ' (', design.accepted ? 'accepted' : design.acceptance_state, ')'),
        h('dt', null, 'Document'), h('dd', { class: 'mono' }, design.authority.path, ' @ ', String(design.authority.commit).slice(0, 12)),
        h('dt', null, 'sha256'), h('dd', { class: 'mono' }, design.authority.sha256),
        h('dt', null, 'Verification'), h('dd', { 'data-verification': (design.verification || {}).state || 'draft' }, verificationLine(design.verification)),
        h('dt', null, 'Index'), h('dd', { 'data-index-state': design.index_state }, design.index_state),
        h('dt', null, 'Front block'), h('dd', null, `scope ${front.scope}, status ${front.status} (the author's claim)`)),
      phases.length ? h('div', null, h('h3', { class: 'small' }, 'Plan phases'),
        h('ul', null, phases.map((phase) => h('li', null, h('code', null, phase.anchor),
          phase.epic ? [' — epic ', h('a', { href: ctx.href ? ctx.href(`/p/${pid}/t/${phase.epic}`) : `#/p/${pid}/t/${phase.epic}` }, phase.epic)] : ' — no epic yet')))) : null,
      h('p', { class: 'small muted' }, 'The stored document, shown as text (untrusted data).')),
    h('section', { class: 'panel' }, renderDocument(design.document, design.index && design.index.headings, design.heading_lines)));
}
