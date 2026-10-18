"""Real bd: display names resolve only when a NEW assignment is written (kittrial-5bb.333).

The review of the first contribution (01a14ca8) showed that resolving a STORED assignee
against a roster anyone can extend lets a newcomer take over an existing actor's task (P1)
and redirects an operator's raw assignment (P5). These tests are written as attacks that
must fail, and they prove the two halves separately:

* the WRITE resolves only a value that is not an actor that has ever acted (and refuses an
  unknown, ambiguous or case-variant name before any write);
* the READ compares the stored assignee with exact equality, even when a hand-written
  registry (bypassing the registration refusal) contains an impostor with that name.
"""
import json
import sys
import unittest
import uuid
from pathlib import Path

KIT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(KIT), str(KIT / 'tests')]
import test_bd_label_aliases as fixture
from test_coordinator_rules_endpoint import RulesHelpers
import admin


@unittest.skipIf(fixture.endpoint is None, 'endpoint imports fcntl (POSIX-only)')
@unittest.skipIf(fixture.BD is None, 'no real bd binary (set ORCHESTRA_BD_BIN)')
class AssigneeNameEndpointTests(RulesHelpers, unittest.TestCase):
    NAME = 'worker-one'

    def setUp(self):
        super().setUp()
        # The tracker is shared across the class, and a row that stores a display name
        # correctly makes that name an actor that has acted, so every test uses its own
        # name: the check under test is about NEW assignments, not about a name an earlier
        # test left in the tracker.
        self.NAME = 'worker-' + uuid.uuid4().hex[:10]

    def write_registry(self, entries):
        """Write the registry directly, so a test can bypass the registration refusal."""
        records = {}
        for actor, name in entries:
            request_id = str(uuid.uuid4())
            records[request_id] = {'request_id': request_id, 'actor': actor,
                                   'name': name, 'created_at': '2026-10-18T00:00:00Z'}
        (self.project / '.sessions.json').write_text(
            json.dumps({'schema_version': 1, 'records': records}), encoding='utf-8')

    def a_session(self):
        return 'session-' + str(uuid.uuid4())

    def row(self, task):
        return next(row for row in self.rows() if row['id'] == task)

    # -- the write: resolution happens here and only here ------------------------------

    def test_raw_operator_assign_resolves_a_unique_session_display_name(self):
        actor = self.a_session()
        self.write_registry([(actor, self.NAME)])
        task = self.task('assigned by name')
        reply = self.endpoint('bd', 'ops', ['update', task, '--assignee', self.NAME])
        self.assertEqual(0, reply['returncode'], reply)
        self.assertIn(actor, reply['stderr'])
        self.assertEqual(actor, self.row(task)['assignee'])

    def test_raw_operator_assign_refuses_an_unknown_name_before_writing(self):
        self.write_registry([(self.a_session(), self.NAME)])
        task = self.task('unchanged holder', assignee='w1')
        before = self.row(task)
        with self.assertRaises(ValueError) as caught:
            self.endpoint('bd', 'ops', ['update', task, '--assignee', 'nobody-at-all'])
        self.assertIn('nobody-at-all', str(caught.exception))
        self.assertIn('actor id', str(caught.exception))
        self.assertEqual(before, self.row(task))

    def test_raw_operator_assign_refuses_a_case_variant(self):
        # Kills the M2 mutant (case-insensitive matching) through the real endpoint.
        self.write_registry([(self.a_session(), self.NAME)])
        task = self.task('case row', assignee='w1')
        before = self.row(task)
        for variant in ('WORKER-ONE', self.NAME + ' ', 'WORKER-one'):
            with self.subTest(variant=variant):
                with self.assertRaises(ValueError):
                    self.endpoint('bd', 'ops', ['update', task, '--assignee', variant])
                self.assertEqual(before, self.row(task))

    def test_p5_operator_assign_to_a_known_actor_keeps_that_actor(self):
        """P5: a task set to legacy actor `w1` must stay `w1`, not the session named `w1`."""
        intruder = self.a_session()
        # A task already assigned to w1 makes w1 an actor that has acted in this project.
        legacy = self.task('the legacy owner row', assignee='w1')
        # Hand-write the registry: bypass the registration refusal to test the write path.
        self.write_registry([(intruder, 'w1')])
        target = self.task('to assign')
        reply = self.endpoint('bd', 'ops', ['update', target, '--assignee', 'w1'])
        self.assertEqual(0, reply['returncode'], reply)
        self.assertEqual('w1', self.row(target)['assignee'])
        self.assertNotEqual(intruder, self.row(target)['assignee'])
        self.assertEqual('w1', self.row(legacy)['assignee'])

    def test_registering_a_name_that_is_a_known_actor_is_refused(self):
        """The P1 setup through the API is refused: no session may be named like an actor."""
        self.task('the legacy owner row', assignee='w1')
        before = (self.project / '.sessions.json').read_text(encoding='utf-8') \
            if (self.project / '.sessions.json').exists() else None
        with self.assertRaises(ValueError) as caught:
            self.endpoint('session', 'w2', ['register', '--name', 'w1',
                                            '--request-id', str(uuid.uuid4())])
        self.assertIn('actor id', str(caught.exception))
        after = (self.project / '.sessions.json').read_text(encoding='utf-8') \
            if (self.project / '.sessions.json').exists() else None
        self.assertEqual(before, after)

    def test_two_sessions_may_share_a_name_and_assignment_by_it_is_refused(self):
        """A readable name may be shared; assigning BY that ambiguous name is refused."""
        for actor in ('w1', 'w2'):
            reply = self.endpoint('session', actor, ['register', '--name', 'shared-name',
                                                    '--request-id', str(uuid.uuid4())])
            self.assertEqual(0, reply['returncode'], reply)
        registry = json.loads((self.project / '.sessions.json').read_text(encoding='utf-8'))
        records = list(registry['records'].values())
        self.assertEqual(2, len(records))
        self.assertEqual({'shared-name'}, {record['name'] for record in records})
        self.assertEqual(2, len({record['actor'] for record in records}))
        task = self.task('ambiguous name row')
        before = self.row(task)
        with self.assertRaises(ValueError) as caught:
            self.endpoint('bd', 'ops', ['update', task, '--assignee', 'shared-name'])
        self.assertIn('shared-name', str(caught.exception))
        self.assertEqual(before, self.row(task))

    # -- the read: exact equality even with an impostor in the registry -----------------

    def test_p1_an_impostor_session_named_w1_is_refused_and_the_real_w1_is_accepted(self):
        intruder = self.a_session()
        # Bypass the registration refusal: write the impostor straight into the registry.
        self.write_registry([(intruder, 'w1')])
        task = self.task('legacy owner w1', assignee='w1')
        with self.assertRaises(ValueError) as caught:
            self.contribute(task, intruder)
        self.assertIn('assigned to w1', str(caught.exception))
        # The rightful legacy actor still owns its row.
        self.assertEqual(0, self.contribute(self.task('legacy owner w1 again', assignee='w1'),
                                            'w1')['returncode'])
        # And the impostor's queue does not list w1's rows.
        reply = self.endpoint('work', intruder, ['--mine'])
        data = json.loads(reply['stdout'])
        self.assertNotIn(task, [item['task'] for item in data['items']])

    def test_p2_an_owner_stays_accepted_after_a_second_identity_takes_its_name(self):
        owner = self.a_session()
        other = self.a_session()
        # Two identities carry the same display name (allowed: a readable name may be
        # shared). The stored row holds the owner's ACTOR id.
        self.write_registry([(owner, 'shared-name'), (other, 'shared-name')])
        task = self.task('owned by actor id', assignee=owner)
        self.assertEqual(0, self.contribute(task, owner)['returncode'])
        # A stranger whose display name is the same is still a stranger.
        with self.assertRaises(ValueError):
            self.contribute(self.task('stranger', assignee=owner), 'w2')

    def test_work_mine_labels_the_actor_with_its_display_name(self):
        actor = self.a_session()
        self.write_registry([(actor, self.NAME)])
        task = self.task('to assign in this test')
        self.endpoint('bd', 'ops', ['update', task, '--assignee', self.NAME])
        reply = self.endpoint('work', actor, ['--mine'])
        self.assertEqual(0, reply['returncode'], reply)
        item = next(item for item in json.loads(reply['stdout'])['items'] if item['task'] == task)
        self.assertEqual(actor, item['owner'])
        self.assertEqual(self.NAME, item['owner_name'])
        self.assertEqual('you', item['whose_move'])

    # -- admin report: candidates, never a write-back recommendation --------------------

    def test_assignee_names_report_lists_candidates_and_never_recommends_a_rewrite(self):
        actor = self.a_session()
        self.write_registry([(actor, self.NAME)])
        named = self.task('a row holding a display name', assignee=self.NAME)
        legacy = self.task('a legacy actor row', assignee='w1')
        report, clean = admin.assignee_name_report(self.root, ['pp'])
        self.assertFalse(clean)
        listed = report['projects']['pp']['rows']
        entry = next(item for item in listed if item['id'] == named)
        self.assertEqual(self.NAME, entry['assignee'])
        self.assertEqual([actor], entry['candidates'])
        self.assertTrue(entry['needs_decision'])
        # No single resolved actor and no repair instruction: a person decides.
        self.assertNotIn('resolved_actor', entry)
        self.assertNotIn('resolved', report['projects']['pp'])
        # A legacy actor id that has acted is still a mismatch of registered identities; it
        # is listed with no candidates for a person to decide, never rewritten.
        legacy_entry = next(item for item in listed if item['id'] == legacy)
        self.assertEqual([], legacy_entry['candidates'])
        self.assertTrue(legacy_entry['needs_decision'])


if __name__ == '__main__':
    unittest.main()
