import assert from 'node:assert/strict';
import './web_dom_shim.mjs';
import { matchRoute } from '../web/js/routes.js';
import { createApi } from '../web/js/api.js';
import { page, referencePage, capabilityPage } from '../web/js/views/records.js';

// 1. Verify route matching in routes.js
const checkRoute = (path, expectedName, expectedParams) => {
  const m = matchRoute(path);
  assert.equal(m.name, expectedName);
  assert.deepEqual({ ...m.params }, expectedParams);
};
checkRoute('/p/pp/records', 'records', { pid: 'pp' });
checkRoute('/p/pp/records/references/kit.guide', 'referenceRecord', { pid: 'pp', key: 'kit.guide' });
checkRoute('/p/pp/references/kit.guide', 'referenceRecord', { pid: 'pp', key: 'kit.guide' });
checkRoute('/p/pp/records/capabilities/records.web-sync', 'capabilityRecord', { pid: 'pp', key: 'records.web-sync' });
checkRoute('/p/pp/capabilities/records.web-sync', 'capabilityRecord', { pid: 'pp', key: 'records.web-sync' });

// 2. Verify createApi record methods
const calls = [];
const mockTransport = async (method, path, headers, body) => {
  calls.push({ method, path, headers, body: body ? JSON.parse(body) : null });
  return { status: 200, data: { ok: true } };
};
const apiClient = createApi(mockTransport);
await apiClient.references('pp', { waiting: 'true' });
await apiClient.findReferences('pp', 'guide');
await apiClient.proposeReference('pp', { key: 'k1' }, 'idem-1');
await apiClient.reviseReference('pp', 'k1', { expected_revision: 1 }, 'idem-2');
await apiClient.acceptReference('pp', 'k1', { expected_revision: 1, evidence: 'ok' }, 'idem-3');
await apiClient.retireReference('pp', 'k1', { expected_revision: 2, reason: 'old' }, 'idem-4');
await apiClient.capabilities('pp', { waiting: 'true' });
await apiClient.findCapabilities('pp', 'sync');
await apiClient.proposeCapability('pp', { key: 'c1' }, 'idem-5');
await apiClient.reviseCapability('pp', 'c1', { expected_revision: 1 }, 'idem-6');
await apiClient.acceptCapability('pp', 'c1', { expected_revision: 1, evidence: 'ok' }, 'idem-7');
await apiClient.retireCapability('pp', 'c1', { expected_revision: 2, reason: 'old' }, 'idem-8');
await apiClient.verifyCapability('pp', 'c1', { revision: 2, commit: 'a'.repeat(40) }, 'idem-9');
await apiClient.proposeCapabilityAlias('pp', 'c1', { alias: 'a1', rationale: 'r' }, 'idem-10');
await apiClient.rejectCapabilityAlias('pp', 'c1', { alias: 'a1', rationale: 'no' }, 'idem-11');
assert.equal(calls.length, 15);

// 3. Render the Records page (`page`) and verify waiting queue + two-step badge
const navigated = [];
const assertRecordsBreadcrumb = (dom, page) => {
  const nav = dom.all((e) => e.attributes['aria-label'] === 'Breadcrumb')[0];
  assert.ok(nav, `${page} should render the breadcrumb navigation`);
  assert.equal(nav.textContent, 'Records', `${page} should render the Records breadcrumb`);
  const link = nav.all((e) => e.tagName === 'A')[0];
  assert.ok(link, `${page} should render the Records breadcrumb as a link`);
  assert.equal(link.attributes.href, '/p/pp/records');
};
const assertLede = (dom, expected, page) => {
  const ledes = dom.all((e) => e.tagName === 'P' && e.className === 'lede');
  assert.equal(ledes.length, 1, `${page} should render one lede`);
  assert.equal(ledes[0].textContent, expected, `${page} should render the expected lede`);
};
const app = {
  navigate: (url) => navigated.push(url),
  projects: [{ id: 'pp', name: 'Test Project' }],
  api: {
    project: async (pid) => ({ id: pid, name: 'Test Project', role: 'coordinator' }),
    coordination: async (_pid) => ({ people: [{ display_name: 'Dana', disabled: false }], holds: true }),
    delegations: async (_pid) => ({ names: {}, granted_agents: [], items: [] }),
    references: async (_pid, query) => {
      if (query && (query.waiting === 'true' || query.waiting === 1)) {
        return {
          items: [
            {
              key: 'kit.guide',
              id: 'pp-1',
              state: 'accepted',
              waiting_acceptance: true,
              proposed_by: 'usr_1111111111111111',
              record: { revision: 2, title: 'Kit Guide', summary: 'Accepted v2', kind: 'runbook', scope: 'project', authority: { type: 'repo-path', path: 'docs/OLD.md', commit: 'b'.repeat(40) } },
              proposed: { revision: 3, title: 'Kit Guide v3', summary: 'Revised draft v3', kind: 'runbook', scope: 'project', authority: { type: 'attestation', basis: 'host-check', by: 'person:maintainer', observed: '2026-10-25', how: 'Checked the board.' } },
              proposed_authority: { type: 'attestation', basis: 'host-check', by: 'person:maintainer', observed: '2026-10-25', how: 'Checked the board.' },
              authority: { type: 'attestation', basis: 'host-check', by: 'person:maintainer', observed: '2026-10-25', how: 'Checked the board.' },
              viewer_may_accept: true,
              viewer_may_accept_reason: '',
            },
          ],
          can_propose: true,
          can_coordinate: true,
        };
      }
      return {
        items: [
          {
            key: 'kit.guide',
            id: 'pp-1',
            state: 'accepted',
            waiting_acceptance: true,
            proposed_by: 'usr_1111111111111111',
            record: { revision: 2, title: 'Kit Guide', summary: 'Accepted v2', kind: 'runbook', scope: 'project', Pointers: ['docs/OPERATIONS.md'], authority: { type: 'repo-path', path: 'docs/OLD.md', commit: 'b'.repeat(40) } },
            proposed: { revision: 3, title: 'Kit Guide v3', summary: 'Revised draft v3', kind: 'runbook', scope: 'project', Pointers: ['docs/OPERATIONS.md'], authority: { type: 'attestation', basis: 'host-check', by: 'person:maintainer', observed: '2026-10-25', how: 'Checked the board.' } },
            proposed_authority: { type: 'attestation', basis: 'host-check', by: 'person:maintainer', observed: '2026-10-25', how: 'Checked the board.' },
            authority: { type: 'attestation', basis: 'host-check', by: 'person:maintainer', observed: '2026-10-25', how: 'Checked the board.' },
            viewer_may_accept: true,
            viewer_may_accept_reason: '',
          },
        ],
        can_propose: true,
        can_coordinate: true,
      };
    },
    capabilities: async (_pid, query) => {
      if (query && (query.waiting === 'true' || query.waiting === 1)) {
        return {
          items: [
            {
              key: 'records.web-sync',
              id: 'pp-2',
              state: 'draft',
              waiting_acceptance: true,
              proposed_by: 'usr_1111111111111111',
              verification_state: 'unverified',
              record: null,
              proposed: { revision: 1, title: 'Web Sync', summary: 'Draft cap', kind: 'workflow', scope: 'project' },
              viewer_may_accept: false,
              viewer_may_accept_reason: 'Only the project coordinator may accept.',
            },
          ],
          can_propose: true,
          can_coordinate: true,
        };
      }
      return {
        items: [
          {
            key: 'records.web-sync',
            id: 'pp-2',
            state: 'draft',
            waiting_acceptance: true,
            proposed_by: 'usr_1111111111111111',
            verification_state: 'unverified',
            record: null,
            proposed: { revision: 1, title: 'Web Sync', summary: 'Draft cap', kind: 'workflow', scope: 'project', points_to: ['http_service.py'] },
            viewer_may_accept: true,
            viewer_may_accept_reason: '',
          },
        ],
        can_propose: true,
        can_coordinate: true,
      };
    },
  },
};

const recordsDom = await page(app, { pid: 'pp' });
await new Promise((resolve) => setTimeout(resolve, 0));
await new Promise((resolve) => setTimeout(resolve, 0));
assertLede(recordsDom, 'Test Project · Reference catalog & capability registry', 'Records page');
assert.ok(recordsDom.textContent.includes('Waiting to be accepted'));
assert.ok(recordsDom.textContent.includes('2 waiting'));
assert.ok(recordsDom.textContent.includes('Accepted rev 2 · Draft rev 3 waiting'));
assert.ok(recordsDom.textContent.includes('kit.guide'));
assert.ok(recordsDom.textContent.includes('records.web-sync'));
// Slice 4 (kittrial-5bb.420): the button follows ONLY the server's viewer_may_accept,
// the authority cell reads the draft's authority, and propose forms sit folded.
assert.ok(recordsDom.textContent.includes('Who acts'));
assert.ok(recordsDom.textContent.includes('Who accepts here:'));
assert.ok(recordsDom.textContent.includes('Dana'));
assert.ok(recordsDom.textContent.includes('Review & accept'));
const refRow = recordsDom.all((e) => e.attributes['data-reference-row'] === 'kit.guide')[0];
assert.ok(refRow, 'waiting draft kit.guide renders in the catalog');
assert.ok(refRow.textContent.includes('attestation (host-check)'),
  'catalog authority cell prints the waiting draft attestation, not the old accepted authority');
const acceptButtons = recordsDom.all((e) => e.tagName === 'A' && e.textContent === 'Review & accept');
assert.ok(acceptButtons.length >= 1, `expected gated Review & accept links, saw ${acceptButtons.length}`);
const viewButtons = recordsDom.all((e) => e.tagName === 'A' && e.textContent === 'View');
assert.ok(viewButtons.length >= 1, `expected a View link where the server says the reader may not act, saw ${viewButtons.length}`);
const whoCells = recordsDom.all((e) => e.className.split(/\s+/).includes('who-acts'));
assert.ok(whoCells.length >= 2, `expected Who acts cells, saw ${whoCells.length}`);
const proposeFold = recordsDom.all((e) => e.attributes['data-propose-fold'] === '1')[0];
assert.ok(proposeFold, 'propose forms fold behind details.fold');
const acceptedPanel = recordsDom.all((e) => e.attributes['data-accepted-records'] === '1')[0];
assert.ok(acceptedPanel, 'single Accepted records panel with the kind filter');
const kindFilter = recordsDom.all((e) => e.attributes['data-accepted-kind-filter'] === '1')[0];
assert.ok(kindFilter, 'kind filter for the accepted list');
assert.equal(kindFilter.all((e) => e.tagName === 'BUTTON').length, 3);

const recordsErrorApp = {
  ...app,
  api: { ...app.api, references: async () => { throw new Error('records unavailable'); } },
};
const recordsErrorDom = await page(recordsErrorApp, { pid: 'pp' });
assertLede(recordsErrorDom, 'Test Project', 'Records error page');
assert.ok(recordsErrorDom.textContent.includes('records unavailable'));

// 4. Render Reference Detail (`referencePage`) with two-step revise-then-accept & self_accept_blocked
let acceptedRefPayload = null;
const refApp = {
  navigate: (url) => navigated.push(url),
  render: async () => {},
  api: {
    project: async (pid) => ({ id: pid, name: 'Test Project', role: 'coordinator' }),
    reference: async () => ({
      key: 'kit.guide',
      task: 'pp-1',
      state: 'accepted',
      waiting_acceptance: true,
      can_propose: true,
      can_coordinate: true,
      self_accept_blocked: false,
      proposed_by: 'usr_1111111111111111',
      accepted_by: 'usr_2222222222222222',
      pointers_validation: {
        checked: 'path-syntax-only',
        checkout_validated: false,
        note: 'The web service has no repository checkout; relative repo paths in authority.path are syntax-checked only.',
      },
      record: {
        revision: 2,
        sha256: '2'.repeat(64),
        title: 'Kit Guide',
        statement: 'Accepted v2 statement',
        authority: { type: 'repo-path', path: 'docs/OPERATIONS.md', commit: 'a'.repeat(40) },
        owner: 'person:maintainer',
        review_by: '2026-12-31',
        tags: ['ops'],
        decisions: [],
      },
      proposed: {
        revision: 3,
        sha256: '3'.repeat(64),
        title: 'Kit Guide v3',
        statement: 'Revised draft v3 statement',
        authority: { type: 'repo-path', path: 'docs/OPERATIONS.md', commit: 'b'.repeat(40) },
        owner: 'person:maintainer',
        review_by: '2026-12-31',
        tags: ['ops'],
        decisions: [],
      },
    }),
    acceptReference: async (_pid, _key, payload) => {
      acceptedRefPayload = payload;
      return { revision: 4, state: 'accepted' };
    },
  },
};

const refDom = await referencePage(refApp, { pid: 'pp', key: 'kit.guide' });
assertRecordsBreadcrumb(refDom, 'reference success page');
assertLede(refDom, 'Reference record · accepted', 'Reference success page');
assert.ok(refDom.textContent.includes('Pending draft revision 3 (waiting to be accepted)'));
assert.ok(refDom.textContent.includes('Two-step revise-then-accept'));
assert.ok(refDom.textContent.includes('path-syntax-only'));

const refErrorApp = {
  ...refApp,
  api: { ...refApp.api, reference: async () => { throw new Error('reference unavailable'); } },
};
const refErrorDom = await referencePage(refErrorApp, { pid: 'pp', key: 'kit.guide' });
assertRecordsBreadcrumb(refErrorDom, 'reference error page');

const acceptForm = refDom.all((e) => e.attributes['data-accept-reference-form'] === '1')[0];
assert.ok(acceptForm, 'Accept reference form should be present');
const evInput = acceptForm.all((e) => e.attributes.name === 'evidence')[0];
evInput.value = 'Checked docs/OPERATIONS.md.';
await acceptForm.dispatch('submit');
assert.equal(acceptedRefPayload.revision, 3);
assert.equal(acceptedRefPayload.record_sha256, '3'.repeat(64));
assert.equal(acceptedRefPayload.evidence, 'Checked docs/OPERATIONS.md.');

// 5. Render Capability Detail (`capabilityPage`) with verify and self_verify_blocked
let verifiedCapPayload = null;
const capApp = {
  navigate: (url) => navigated.push(url),
  render: async () => {},
  api: {
    project: async (pid) => ({ id: pid, name: 'Test Project', role: 'coordinator' }),
    capability: async () => ({
      key: 'records.web-sync',
      task: 'pp-2',
      state: 'accepted',
      verification: { state: 'unverified' },
      waiting_acceptance: false,
      can_propose: true,
      can_coordinate: true,
      self_accept_blocked: false,
      self_verify_blocked: false,
      proposed_by: 'usr_1111111111111111',
      accepted_by: 'usr_2222222222222222',
      pointers_validation: {
        checked: 'syntax-and-requirements',
        requirements_validated: true,
        checkout_validated: false,
        verification_state: 'unverified',
        note: 'The web service has no repository checkout; repo paths are syntax-checked at acceptance.',
      },
      record: {
        revision: 2,
        sha256: '4'.repeat(64),
        name: 'Web Sync',
        summary: 'Accepted capability',
        owner: 'person:maintainer',
        tags: ['web'],
        aliases: [],
        requirements: [],
        anchors: ['docs/HTTP_DEPLOYMENT.md#records'],
        code: ['http_service.py::ApiHandler.references_list'],
        tests: ['tests/test_web_records.py::StrictWebRecordsTests'],
      },
      proposed: null,
      aliases_pending: [{ alias: { text: 'web.sync.old' }, actor: 'usr_1111111111111111', submitter: { person: 'account:usr_1111111111111111', identity: 'verified' } }],
    }),
    verifyCapability: async (_pid, _key, payload) => {
      verifiedCapPayload = payload;
      return { verification_state: 'attested' };
    },
  },
};

const capDom = await capabilityPage(capApp, { pid: 'pp', key: 'records.web-sync' });
assertRecordsBreadcrumb(capDom, 'capability success page');
assertLede(capDom, 'Capability record · accepted', 'Capability success page');
assert.ok(capDom.textContent.includes('syntax-and-requirements'));
assert.ok(capDom.textContent.includes('Verify revision 2'));

const capErrorApp = {
  ...capApp,
  api: { ...capApp.api, capability: async () => { throw new Error('capability unavailable'); } },
};
const capErrorDom = await capabilityPage(capErrorApp, { pid: 'pp', key: 'records.web-sync' });
assertRecordsBreadcrumb(capErrorDom, 'capability error page');

const verifyForm = capDom.all((e) => e.attributes['data-verify-capability-form'] === '1')[0];
assert.ok(verifyForm, 'Verify capability form should be present');
verifyForm.all((e) => e.attributes.name === 'commit')[0].value = 'f'.repeat(40);
await verifyForm.dispatch('submit');
assert.equal(verifiedCapPayload.revision, 2);
assert.equal(verifiedCapPayload.record_sha256, '4'.repeat(64));
assert.equal(verifiedCapPayload.commit, 'f'.repeat(40));
assert.equal(verifiedCapPayload.results.length, 3);
assert.deepEqual(verifiedCapPayload.results[0], {
  pointer: 'http_service.py::ApiHandler.references_list',
  resolved: true,
  reason: null,
});

// 6. kittrial-5bb.603: full 1,000-char, 199/200/201-char, multi-byte, multi-line Markdown
// capability summaries in web find & detail views, plus explicit truncation marker (built
// strictly from omitted_chars and key) when omitted_chars > 0.
const makeSummary = (len, prefix, tail) => prefix + 'x'.repeat(len - prefix.length - tail.length) + tail;
const sum1000 = makeSummary(1000, 'Web 1000: ', ' WEB-END-1000.');
const sum199 = makeSummary(199, 'Web 199: ', ' END199.');
const sum200 = makeSummary(200, 'Web 200: ', ' END200.');
const sum201 = makeSummary(201, 'Web 201: ', ' END201.');
const sumMdMb = '# LGB Basis\n- **Hold** `net_roll`\n漢字 🚀 cafe\u0301 <script>alert(1)</script>';
const fakeTruncProse = 'Prefix text (record prose mentions truncated: 99999 fake)';

const searchApp = {
  navigate: () => {},
  render: async () => {},
  projects: [{ id: 'pp', name: 'Test Project' }],
  api: {
    project: async (pid) => ({ id: pid, name: 'Test Project', role: 'member' }),
    coordination: async () => ({ people: [{ display_name: 'Dana', disabled: false }], holds: true }),
    delegations: async () => ({ names: {}, granted_agents: [], items: [] }),
    references: async () => ({ items: [], can_propose: false, can_coordinate: false }),
    capabilities: async () => ({ items: [], can_propose: false, can_coordinate: false }),
    findReferences: async () => ({ items: [], records: [] }),
    findCapabilities: async () => ({
      items: [
        { key: 'flag.basis-hold-net-roll', state: 'accepted', verification: 'verified', owner: 'person:maintainer', name: { text: 'Basis Hold', omitted_chars: 0 }, summary: { text: sum1000, omitted_chars: 0 } },
        { key: 'ops.len-199', state: 'accepted', verification: 'verified', owner: 'person:maintainer', name: { text: 'Len 199', omitted_chars: 0 }, summary: { text: sum199, omitted_chars: 0 } },
        { key: 'ops.len-200', state: 'accepted', verification: 'verified', owner: 'person:maintainer', name: { text: 'Len 200', omitted_chars: 0 }, summary: { text: sum200, omitted_chars: 0 } },
        { key: 'ops.len-201', state: 'accepted', verification: 'verified', owner: 'person:maintainer', name: { text: 'Len 201', omitted_chars: 0 }, summary: { text: sum201, omitted_chars: 0 } },
        { key: 'ops.md-mb', state: 'accepted', verification: 'verified', owner: 'person:maintainer', name: { text: 'MD MB', omitted_chars: 0 }, summary: { text: sumMdMb, omitted_chars: 0 } },
        { key: 'ops.manual-basis-entry', state: 'accepted', verification: 'verified', owner: 'person:maintainer', name: { text: 'Truncated Example', omitted_chars: 0 }, summary: { text: fakeTruncProse, omitted_chars: 812 } },
      ],
    }),
  },
};

const searchDom = await page(searchApp, { pid: 'pp' });
const findForm = searchDom.all((e) => e.attributes['data-records-find-form'] === '1')[0];
assert.ok(findForm, 'Records find form should be present');
findForm.all((e) => e.attributes.name === 'q')[0].value = 'basis';
await findForm.dispatch('submit');

const rowSummary = (key) => searchDom.all((e) => e.attributes['data-capability-summary'] === key)[0];
for (const [k, expected] of [
  ['flag.basis-hold-net-roll', sum1000],
  ['ops.len-199', sum199],
  ['ops.len-200', sum200],
  ['ops.len-201', sum201],
  ['ops.md-mb', sumMdMb],
]) {
  const el = rowSummary(k);
  assert.ok(el, `capability summary element missing for ${k}`);
  assert.ok(el.className.split(/\s+/).includes('prose'), `summary element for ${k} must have prose class`);
  assert.equal(el.textContent, expected, `summary for ${k} must render whole`);
}
// No <script> element created from Markdown/HTML in summary.
assert.equal(searchDom.all((e) => e.tagName === 'SCRIPT').length, 0);

// Truncated capability row MUST show explicit marker built strictly from omitted_chars and key.
const truncRow = searchDom.all((e) => e.attributes['data-capability-row'] === 'ops.manual-basis-entry')[0];
assert.ok(truncRow, 'truncated capability row should render');
const truncMarkers = truncRow.all((e) => e.attributes['data-truncated-marker'] === '1');
assert.equal(truncMarkers.length, 1, 'truncated capability row must render one truncation marker');
assert.equal(
  truncMarkers[0].textContent,
  'truncated: 812 more characters; capability get ops.manual-basis-entry',
);
// Whole rows must NOT render a truncation marker.
const wholeRow = searchDom.all((e) => e.attributes['data-capability-row'] === 'flag.basis-hold-net-roll')[0];
assert.equal(wholeRow.all((e) => e.attributes['data-truncated-marker'] === '1').length, 0);

// Capability detail page: 1,000-char multi-line summary renders whole with .prose,
// and an oversized/truncated response renders the explicit marker.
const detailLongApp = {
  ...capApp,
  api: {
    ...capApp.api,
    capability: async () => ({
      ...(await capApp.api.capability()),
      key: 'ops.manual-basis-entry',
      record: {
        ...(await capApp.api.capability()).record,
        summary: { text: sum1000 + '\n' + sumMdMb, omitted_chars: 0 },
      },
      proposed: {
        ...(await capApp.api.capability()).record,
        revision: 3,
        summary: { text: fakeTruncProse, omitted_chars: 405 },
      },
    }),
  },
};
const detailLongDom = await capabilityPage(detailLongApp, { pid: 'pp', key: 'ops.manual-basis-entry' });
const detailSumEl = detailLongDom.all((e) => e.attributes['data-capability-summary'] === 'ops.manual-basis-entry')[0];
assert.ok(detailSumEl, 'capability detail summary element should render');
assert.ok(detailSumEl.className.split(/\s+/).includes('prose'), 'capability detail summary must have prose class');
assert.equal(detailSumEl.textContent, sum1000 + '\n' + sumMdMb);
const detailMarkers = detailLongDom.all((e) => e.attributes['data-truncated-marker'] === '1');
assert.equal(detailMarkers.length, 1, 'truncated proposed summary must render marker');
assert.equal(
  detailMarkers[0].textContent,
  'truncated: 405 more characters; capability get ops.manual-basis-entry',
);

