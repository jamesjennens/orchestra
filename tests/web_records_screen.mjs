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
const app = {
  navigate: (url) => navigated.push(url),
  api: {
    project: async (pid) => ({ id: pid, name: 'Test Project', role: 'coordinator' }),
    references: async (_pid, query) => {
      if (query && query.waiting === 'true') {
        return {
          items: [
            {
              key: 'kit.guide',
              id: 'pp-1',
              state: 'accepted',
              waiting_acceptance: true,
              proposed_by: 'usr_1111111111111111',
              record: { revision: 2, title: 'Kit Guide', summary: 'Accepted v2', kind: 'runbook', scope: 'project' },
              proposed: { revision: 3, title: 'Kit Guide v3', summary: 'Revised draft v3', kind: 'runbook', scope: 'project' },
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
            record: { revision: 2, title: 'Kit Guide', summary: 'Accepted v2', kind: 'runbook', scope: 'project', Pointers: ['docs/OPERATIONS.md'] },
            proposed: { revision: 3, title: 'Kit Guide v3', summary: 'Revised draft v3', kind: 'runbook', scope: 'project', Pointers: ['docs/OPERATIONS.md'] },
          },
        ],
        can_propose: true,
        can_coordinate: true,
      };
    },
    capabilities: async (_pid, query) => {
      if (query && query.waiting === 'true') {
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
          },
        ],
        can_propose: true,
        can_coordinate: true,
      };
    },
  },
};

const recordsDom = await page(app, { pid: 'pp' });
assert.ok(recordsDom.textContent.includes('Waiting to be accepted'));
assert.ok(recordsDom.textContent.includes('2 waiting'));
assert.ok(recordsDom.textContent.includes('Accepted rev 2 · Draft rev 3 waiting'));
assert.ok(recordsDom.textContent.includes('kit.guide'));
assert.ok(recordsDom.textContent.includes('records.web-sync'));

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
assert.ok(refDom.textContent.includes('Pending draft revision 3 (waiting to be accepted)'));
assert.ok(refDom.textContent.includes('Two-step revise-then-accept'));
assert.ok(refDom.textContent.includes('path-syntax-only'));

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
assert.ok(capDom.textContent.includes('syntax-and-requirements'));
assert.ok(capDom.textContent.includes('Verify revision 2'));
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
