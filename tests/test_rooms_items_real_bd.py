"""Real-bd end-to-end tests for room items and acceptance (kittrial-5bb.388).

The real web service, the real ``endpoint.py`` (the service-only ``room-decision`` action
under the project's coordination lock and live authority) and a real bd.  The adversarial
cases that need no tracker are in ``test_rooms_items_http.py``; here the created records are
read back with the existing readers and validators, and the pointer is checked in the fields
the kit already has.
"""
import json
import sys
import unittest
from pathlib import Path

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT))
sys.path.insert(0, str(KIT / 'tests'))
import test_claim_held as held
import test_http_review_fixes as fixes
import test_bd_label_aliases as rb
import requirement_owner_records as owner_records
import requirement_records as records
import record_json


class RoomsItemsRealBdTests(held.Members, fixes.Harness, rb.RealBdLabelAliasTests):
    """Project ``pp``: alex (owner), casey and drew (contributors), vic (viewer)."""

    def make_backend(self):
        return held.http_service.EndpointBackend(
            sys.executable, str(KIT / 'endpoint.py'), str(self.root), service=self.service)

    def setUp(self):
        fixes.Harness.setUp(self)
        admin = self.admin_token()
        registered = self.request('POST', '/v1/projects', {'project_id': 'pp', 'name': 'PP'},
                                  token=admin)
        self.assertIn(registered.status, (200, 201, 409), registered.data)
        self.members('pp')
        # Requirements in a scratch project default to governed editing; a room requirement
        # acceptance uses the simple-mode B1 routes, so the fixture switches pp to simple.
        mode = self.request('GET', '/v1/projects/pp/requirements/governance',
                            token=self.tokens['alex'])
        self.assertEqual(200, mode.status, mode.data)
        simple = self.request('PUT', '/v1/projects/pp/requirements/governance',
                              {'mode': 'simple', 'expected_revision': mode.data['revision'],
                               'expected_sha256': mode.data['sha256']},
                              token=self.tokens['alex'], key='simple-mode-key')
        self.assertEqual(200, simple.status, simple.data)
        # A requirement hangs under an existing job; the fixture has one epic.
        made = self.bd('create', 'Room job', '--type', 'epic', '--json')
        self.assertEqual(0, made.returncode, made.stderr)
        self.job = json.loads(made.stdout)['id']
        if 'vic' not in self.tokens:
            self.ids['vic'] = self.create_account(admin, 'vic', 'vic-password-1')
            added = self.request('PUT', '/v1/projects/pp/members/%s' % self.ids['vic'],
                                 {'role': 'viewer'}, token=admin)
            self.assertIn(added.status, (200, 201), added.data)
            self.tokens['vic'] = self.login('vic', 'vic-password-1')[0]
        self.project = self.root / 'projects' / 'pp'

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        try:
            rb.admin.provision_merge_slot(cls.root, 'pp')
        except BaseException:
            cls.tearDownClass()
            raise

    @classmethod
    def tearDownClass(cls):
        for signum, previous in getattr(cls, '_signals', {}).items():
            rb.signal.signal(signum, previous)
        cls._signals = {}
        rb.RealBdLabelAliasTests.__dict__['_stop_server'].__func__(cls)
        rb.remove_scratch(cls._tmp)

    # -- helpers -----------------------------------------------------------------------------------
    def room(self, name='Planning'):
        made = self.request('POST', '/v1/projects/pp/rooms', {'name': name}, token=self.tokens['alex'])
        self.assertEqual(201, made.status, made.data)
        return made.data['id']

    def item(self, rid, who='casey', kind='requirement', title='Add export',
             text='We need CSV export.', key='item-key-0001', parent=None, **extra):
        body = dict(item_kind=kind, title=title, text=text, **extra)
        if parent is not None:
            body['parent'] = parent
        made = self.request('POST', '/v1/projects/pp/rooms/%s/items' % rid, body,
                            token=self.tokens[who], key=key)
        return made

    def decide(self, rid, who, sequence=1, operation='accept', key='decide-key-0001', body=None):
        return self.request('POST', '/v1/projects/pp/rooms/%s/items/%d/%s' % (rid, sequence, operation),
                            body or {}, token=self.tokens[who], key=key)

    def row(self, task_id):
        answer = self.bd('show', task_id, '--json')
        self.assertEqual(0, answer.returncode, answer.stderr)
        row = json.loads(answer.stdout)
        return row[0] if isinstance(row, list) else row

    def pointer(self, rid, sequence=1):
        return 'orchestra:room/%s#%d' % (rid, sequence)

    # -- the tests ---------------------------------------------------------------------------------
    def test_requirement_accept_creates_the_existing_records_with_the_pointer(self):
        rid = self.room()
        posted = self.item(rid, parent=self.job)
        self.assertEqual(201, posted.status, posted.data)
        accepted = self.decide(rid, 'alex', key='accept-key-0001')
        self.assertEqual(200, accepted.status, accepted.data)
        record = accepted.data['record']
        self.assertEqual('requirement', record['kind'])
        self.assertTrue(record['id'])
        self.assertTrue(record['decision_id'])
        rows = self.export_rows()
        requirement = next(row for row in rows if row.get('id') == record['id'])
        self.assertIn(self.pointer(rid), requirement.get('description') or '')
        self.assertTrue(records.existing_revisions(requirement))
        self.assertTrue(owner_records.existing_acceptances(requirement))
        decision = next(row for row in rows if row.get('id') == record['decision_id'])
        self.assertIn(self.pointer(rid), decision.get('description') or '')
        self.assertIn('## Decision', decision.get('description') or '')
        self.assertIn('## Rationale', decision.get('description') or '')
        self.assertIn('## Alternatives Considered', decision.get('description') or '')
        self.assertTrue(any(str(label).startswith('owner-requirement-decision:')
                            for label in decision.get('labels') or []))
        # The read answer serves the item, its state and the same pointer.
        read = self.request('GET', '/v1/projects/pp/rooms/%s' % rid, token=self.tokens['vic'])
        self.assertEqual(200, read.status, read.data)
        self.assertEqual('accepted', read.data['items'][0]['state'])
        self.assertEqual(record, read.data['items'][0]['record'])

    def test_contributor_accept_is_a_proposal_with_the_pointer(self):
        rid = self.room('Proposals')
        self.assertEqual(201, self.item(rid, who='casey', title='Candidate',
                                        text='A contributor candidate.', key='item-key-0002',
                                        parent=self.job).status)
        accepted = self.decide(rid, 'casey', key='accept-key-0002')
        self.assertEqual(200, accepted.status, accepted.data)
        record = accepted.data['record']
        self.assertEqual('proposal', record['kind'])
        self.assertIn(self.pointer(rid), record['evidence'])
        proposal = self.request('GET', '/v1/projects/pp/proposals/%s' % record['id'],
                                token=self.tokens['alex'])
        self.assertEqual(200, proposal.status, proposal.data)
        self.assertIn(self.pointer(rid), json.dumps(proposal.data))

    def test_a_decision_item_needs_a_live_owner_or_coordinator(self):
        rid = self.room('Decisions')
        self.assertEqual(201, self.item(rid, who='casey', kind='decision', title='Use CSV',
                                        text='Emit CSV first, then JSON.',
                                        key='item-key-0003').status)
        refused = self.decide(rid, 'casey', key='accept-key-0003')
        self.assertEqual(403, refused.status, refused.data)
        accepted = self.decide(rid, 'alex', key='accept-key-0004')
        self.assertEqual(200, accepted.status, accepted.data)
        record = accepted.data['record']
        self.assertEqual('decision', record['kind'])
        decision = self.row(record['decision_id'])
        self.assertIn(self.pointer(rid), decision.get('description') or '')
        self.assertIn('## Decision', decision.get('description') or '')
        # R7: an agent credential is refused before any grant is read.
        created = self.request('POST', '/v1/agents', {
            'name': 'Kestrel', 'working_directory': '/home/priya/work/kestrel',
            'projects': ['pp']}, token=self.tokens['alex'])
        self.assertEqual(201, created.status, created.data)
        secret = created.data['credential']['secret']
        agent = self.request('POST', '/v1/projects/pp/rooms/%s/items/1/accept' % rid, {},
                             token=secret, key='agent-accept-1')
        self.assertEqual(403, agent.status, agent.data)

    def test_the_created_rows_keep_their_existing_shape(self):
        rid = self.room('Shape')
        self.assertEqual(201, self.item(rid, kind='requirement', title='Shape check',
                                        text='No new record shape.', key='item-key-0004',
                                        parent=self.job).status)
        accepted = self.decide(rid, 'alex', key='accept-key-0005')
        self.assertEqual(200, accepted.status, accepted.data)
        record = accepted.data['record']
        rows = self.export_rows()
        requirement = next(row for row in rows if row.get('id') == record['id'])
        # The existing readers and validators still read the row; no `source` field is added,
        # and the pointer appears in the existing description.
        self.assertTrue(records.existing_revisions(requirement))
        self.assertTrue(owner_records.existing_acceptances(requirement))
        self.assertNotIn('source', requirement)
        self.assertIn(self.pointer(rid), requirement.get('description') or '')


if __name__ == '__main__':
    unittest.main()


# The label-alias module's own tests are exercised by that module; a room-item fixture
# borrows its real-bd server only, so those test methods are named here rather than rerun.
for _name in ('test_create_label_spellings_refuse_every_reserved_value',
              'test_ordinary_labels_still_reach_real_bd',
              'test_update_label_spellings_refuse_every_reserved_value'):
    if hasattr(RoomsItemsRealBdTests, _name):
        setattr(RoomsItemsRealBdTests, _name,
                unittest.skip('covered by tests/test_bd_label_aliases.py')(getattr(RoomsItemsRealBdTests, _name)))
