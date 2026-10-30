import { h } from '../dom.js';
import { act, describe } from '../ui.js';

// An author's attestation of a decision's exact text (kittrial-5bb.454 r2). A decision counts only with one, and any
// later edit leaves it stale. This shows what is being attested, exactly as stored, and how it reads now; the server
// checks who is attesting, and refuses if the text changed since this page read it.
const READS = {
  counts: 'Counts', closed: 'Closed: binds nothing', 'not-attested': 'Not attested: binds nothing',
  'attestation-stale': 'Stale: the text changed after the attestation, so it binds nothing',
  'attester-not-authorised': 'The attester no longer holds the standing, so it binds nothing',
};

export function attestationPanel(read, attest) {
  const said = h('p', { class: 'small muted', 'data-attest-error': '' });
  const button = attest ? h('button', { type: 'button', class: 'btn', onclick: async () => {
    await act(button, () => attest(read.digest), {
      success: 'Attested',
      onError: (error) => { said.textContent = describe(error); return true; },
    });
  } }, 'Attest this text') : null;
  return h('section', { class: 'panel', 'aria-labelledby': 'attest-h' },
    h('div', { class: 'panel-head' }, h('h2', { class: 'small', id: 'attest-h' }, 'Attestation')),
    h('div', { class: 'panel-body stack' },
      h('p', { 'data-attestation': read.attestation }, READS[read.attestation] || read.attestation,
        read.attested_as ? ` (attested as ${read.attested_as})` : ''),
      h('p', { class: 'small muted' }, 'You attest exactly this:'),
      h('p', { class: 'small' }, `${read.type}: ${read.title}`),
      h('pre', { class: 'small' }, read.description),
      read.attestation === 'counts' && read.adopt_line ? h('div', { class: 'stack', 'data-pin-lines': '' },
        h('p', { class: 'small muted' }, 'An area that adopts this text has this line in its own decision. It pins this text: if this decision is attested with other text, the adoption reads stale until the area adopts again.'),
        h('pre', { class: 'small' }, read.adopt_line),
        h('p', { class: 'small muted' }, 'An owner who lets a coordinator adopt it has this line in their own decision:'),
        h('pre', { class: 'small' }, read.authorise_line)) : null,
      button, said));
}
