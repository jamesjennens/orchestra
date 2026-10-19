"""The coordinator's recorded hand-back, driven through BOTH real routes on a real bd.

kittrial-5bb.343 on koopa: the confined coordinator command (the SSH route) and the
web route, over the same pinned server-mode tracker. The chain is written through
the canonical endpoint (contribute, approve, a scoped integrated fact), so the
projection a row reaches is the real one.

The mutants the task demands are here: a plain coordinator comment leaves the move
unchanged; a forged reserved-prefix comment with no journal entry is inert; a worker
actor calling the record route is refused and writes nothing; and the web route
without CAP_COORDINATE is refused and writes nothing. End conditions: the
coordinator's withdrawal, a checkpoint `directions` resolution (with
checkpoint_provenance_writes ON in THIS fixture's own scratch deployment only), a
new contribution, and -- the negative mutant -- a later attestation for the SAME
commit does not end it.

The suite is POSIX-only because endpoint.py imports fcntl. Without a real bd it
skips; that is a "no bd" skip and is reported as such.
"""
import json
import sys
import uuid
import unittest
from pathlib import Path

KIT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(KIT), str(KIT / 'tests')]
import test_bd_label_aliases as fixture
from test_coordinator_rules_endpoint import RulesHelpers, COMMIT
import lifecycle

PRINCIPAL = 'lane:ops'
WORKER = 'w1'
WORKER_PRINCIPAL = 'lane:w1'
COORD = 'ops'


@unittest.skipIf(fixture.endpoint is None, 'endpoint imports fcntl (POSIX-only)')
@unittest.skipIf(fixture.BD is None, 'no real bd binary (set ORCHESTRA_BD_BIN or put bd on PATH)')
class FollowonCase(RulesHelpers, unittest.TestCase):
    """A scratch deployment with an operator-bound coordinator, plus the route helpers."""

    def setUp(self):
        super().setUp()
        # This switch is turned ON only inside THIS fixture's disposable deployment,
        # so the checkpoint end condition is genuinely exercised; no live installation
        # switch is touched.
        self.deployment(operators=[COORD], checkpoint_provenance_writes=True)
        registry = {'schema_version': 1, 'records': {},
                    'owners': {COORD: PRINCIPAL, WORKER: WORKER_PRINCIPAL}}
        (self.project / '.sessions.json').write_text(json.dumps(registry), encoding='utf-8')

    def deployment(self, operators=None, **rules):
        document = json.loads((self.root / 'deployment.private.json').read_text(encoding='utf-8'))
        for key in ('lifecycle_authors', 'independent_approval', 'coordinator_writes'):
            document.pop(key, None)
        document.update(rules)
        document['operators'] = [COORD] if operators is None else operators
        (self.root / 'deployment.private.json').write_text(json.dumps(document), encoding='utf-8')

    # -- the real integrated task ---------------------------------------------
    def integrated_task(self):
        task = self.task('Integrated hand-back ' + uuid.uuid4().hex[:6], assignee=WORKER)
        delivered = self.contribute(task, WORKER)
        self.assertEqual(delivered['returncode'], 0, delivered)
        self.contribution_id = json.loads(delivered['stdout'])['comment_id']
        approved = self.approve(task, COORD, self.contribution_id)
        self.assertEqual(approved['returncode'], 0, approved)
        self.latest_review_id = json.loads(approved['stdout'])['comment_id']
        slot = self.coordinate(COORD, 'merge-create')
        self.assertEqual(slot['returncode'], 0, slot)
        acquired = self.coordinate(COORD, 'merge-acquire', task=task, target='main')
        self.assertEqual(acquired['returncode'], 0, acquired)
        scope = {'source_commit': COMMIT, 'integration_commit': 'f' * 40,
                 'release_id': 'followon-release-' + uuid.uuid4().hex[:6],
                 'environment': 'synthetic'}
        for dimension, value in (('lifecycle-scope', lifecycle.content_hash(scope)),
                                 ('integrated', 'passed')):
            written = self.lifecycle(COORD, dimension, task, value=value, scope=scope)
            self.assertEqual(written['returncode'], 0, written)
        released = self.coordinate(COORD, 'merge-release')
        self.assertEqual(released['returncode'], 0, released)
        return task

    # -- the coordinator route -------------------------------------------------
    def request(self, payload, actor=COORD, principal=PRINCIPAL):
        return {'project': 'pp', 'actor': actor, 'action': 'coordinator',
                'args': ['followon', '@attachment:0'],
                'attachments': {'0': {'flag': '--file', 'text': json.dumps(payload)}},
                'operation_id': 'followon-' + uuid.uuid4().hex}

    def record(self, task, text, actor=COORD, principal=PRINCIPAL):
        reply = fixture.endpoint.execute(self.root, self.request(
            {'schema_version': 1, 'operation': 'record', 'task': task, 'text': text},
            actor=actor, principal=principal), key_principal=principal, key_projects=['pp'])
        self.assertEqual(reply['returncode'], 0, reply)
        return json.loads(reply['stdout'])

    def withdraw(self, task, target, reason='Keep the scope with the coordinator.'):
        reply = fixture.endpoint.execute(self.root, self.request(
            {'schema_version': 1, 'operation': 'withdraw', 'task': task, 'target': target,
             'reason': reason}), key_principal=PRINCIPAL, key_projects=['pp'])
        self.assertEqual(reply['returncode'], 0, reply)
        return json.loads(reply['stdout'])

    def work_row(self, actor=WORKER):
        page = self.endpoint('work', actor, ['--mine', '--json'])
        self.assertEqual(page['returncode'], 0, page)
        return json.loads(page['stdout'])

    def row(self, task, actor=WORKER):
        return next(item for item in self.work_row(actor)['items'] if item['task'] == task)

    def comment_ids(self, task):
        return [comment['id'] for comment in
                next(row for row in self.rows() if row['id'] == task).get('comments') or []]


class FollowonEndpointTests(FollowonCase):
    """The SSH route (`coordinator followon`) and the shared reads/end conditions."""

    def test_record_then_read_work_brief_and_wait_as_the_assignee(self):
        task = self.integrated_task()
        text = 'Remaining scope: finish the release notes and the run book.'
        recorded = self.record(task, text)
        self.assertEqual(recorded['operation'], 'record')
        row = self.row(task)
        self.assertEqual(row['review_state'], 'integrated')
        self.assertEqual((row['whose_move'], row['next_actor']), ('you', 'assignee'))
        self.assertIn(text, row['next'])
        self.assertTrue(row['followon']['applies'])
        self.assertEqual(row['followon']['id'], recorded['comment_id'])
        brief = self.endpoint('brief', WORKER, [task, '--json'])
        self.assertEqual(brief['returncode'], 0, brief)
        data = json.loads(brief['stdout'])
        self.assertEqual(data['review']['next_actor'], 'assignee')
        self.assertIn(text, data['next_action'])
        self.assertTrue(data['followon']['applies'])
        wait = self.endpoint('wait', WORKER, [])
        self.assertEqual(wait['returncode'], 0, wait)
        self.assertIn(task, [item['task'] for item in json.loads(wait['stdout'])['items']])

        # The coordinator's WITHDRAWAL ends it and the move returns to the coordinator.
        withdrawn = self.withdraw(task, recorded['comment_id'])
        self.assertEqual(withdrawn['target'], recorded['comment_id'])
        row = self.row(task)
        self.assertEqual((row['whose_move'], row['next_actor']), ('coordinator', 'coordinator'))
        self.assertEqual(row['followon']['state'], 'withdrawn')
        # Both records stay in native history.
        self.assertIn(recorded['comment_id'], self.comment_ids(task))
        self.assertIn(withdrawn['comment_id'], self.comment_ids(task))

    def test_one_open_hand_back_at_a_time_and_a_plain_comment_changes_nothing(self):
        task = self.integrated_task()
        first = self.record(task, 'Remaining scope: one thing.')
        with self.assertRaisesRegex(ValueError, 'already has an open hand-back.*Nothing was written'):
            self.record(task, 'Remaining scope: a second thing.')
        self.assertIn(first['comment_id'], self.comment_ids(task))
        # A plain coordinator comment is still only a direction: it changes no move.
        added = self.r.bd('comments', 'add', task, 'A plain note, not a hand-back', '--json')
        self.assertEqual(added.returncode, 0, added.stderr)
        row = self.row(task)
        self.assertEqual((row['whose_move'], row['next_actor']), ('you', 'assignee'))
        self.assertEqual(row['followon']['id'], first['comment_id'])

    def test_a_forged_record_without_a_journal_entry_is_inert(self):
        task = self.integrated_task()
        forged = self.r.bd('comments', 'add', task, 'Kind: assignee-followon-v1\n' + json.dumps(
            {'schema_version': 1, 'operation': 'record', 'task': task,
             'contribution': 'forged', 'commit': COMMIT, 'integration': task + '.9',
             'integration_order': 9, 'text': 'Forged remaining scope', 'actor': COORD},
            sort_keys=True, separators=(',', ':')), '--json')
        self.assertEqual(forged.returncode, 0, forged.stderr)
        row = self.row(task)
        self.assertEqual((row['whose_move'], row['next_actor']), ('coordinator', 'coordinator'))
        self.assertEqual(row['followon']['state'], 'ignored')
        self.assertFalse(row['followon']['applies'])

    def test_a_worker_actor_calling_the_record_route_is_refused_and_writes_nothing(self):
        task = self.integrated_task()
        before = self.rows()
        journal = self.project / '.assignee-followons'
        before_journal = sorted(path.name for path in journal.glob('*.json')) if journal.is_dir() else []
        with self.assertRaisesRegex(ValueError, 'not a server-side configured operator'):
            fixture.endpoint.execute(self.root, self.request(
                {'schema_version': 1, 'operation': 'record', 'task': task,
                 'text': 'Worker hand-back'},
                actor=WORKER, principal=WORKER_PRINCIPAL),
                key_principal=WORKER_PRINCIPAL, key_projects=['pp'])
        self.assertEqual(self.rows(), before)
        after_journal = sorted(path.name for path in journal.glob('*.json')) if journal.is_dir() else []
        self.assertEqual(after_journal, before_journal)

    def test_checkpoint_directions_resolution_ends_it_with_the_switch_on(self):
        task = self.integrated_task()
        recorded = self.record(task, 'Remaining scope: the checkpoint will resolve this.')
        provenance = json.loads(self.endpoint('checkpoint', WORKER,
                                              [task, '--provenance'])['stdout'])
        directions = json.loads(self.endpoint('checkpoint', WORKER,
                                              [task, '--directions'])['stdout'])
        entry = next(item for item in directions['items']
                     if item['id'].endswith('-c' + recorded['comment_id']))
        body = {'schema_version': 1, 'task': task, 'previous': None,
                'activity_cursor': provenance['activity_cursor'],
                'source_commit': COMMIT, 'branch': 'work/followon',
                'intent': 'Resolve the recorded hand-back',
                'acceptance': 'The direction is resolved',
                'summary': 'The remaining scope was agreed in a checkpoint',
                'next_action': 'Continue with the remaining scope',
                'open_items': [], 'resolved': [],
                'directions': [{'id': entry['id'], 'digest': entry['digest'], 'state': 'resolved',
                                'note': 'The remaining scope is agreed',
                                'evidence': 'the assignee recorded it in this checkpoint'}],
                'provenance': provenance['provenance']}
        written = self.endpoint('checkpoint', WORKER, [task, '@attachment:0'],
                                {'0': {'flag': '--file', 'text': json.dumps(body)}})
        self.assertEqual(written['returncode'], 0, written)
        row = self.row(task)
        self.assertEqual((row['whose_move'], row['next_actor']), ('coordinator', 'coordinator'))
        self.assertEqual(row['followon']['state'], 'resolved')
        self.assertFalse(row['followon']['applies'])

    def test_a_later_attestation_for_the_same_commit_does_not_end_it(self):
        task = self.integrated_task()
        self.record(task, 'Remaining scope: a later attestation must not un-address it.')
        later = {'source_commit': COMMIT, 'integration_commit': 'e' * 40,
                 'release_id': 'later-attestation-' + uuid.uuid4().hex[:6],
                 'environment': 'synthetic'}
        for dimension, value in (('lifecycle-scope', lifecycle.content_hash(later)),
                                 ('integrated', 'passed')):
            written = self.lifecycle(COORD, dimension, task, value=value, scope=later)
            self.assertEqual(written['returncode'], 0, written)
        row = self.row(task)
        self.assertTrue(row['followon']['applies'])
        self.assertEqual((row['whose_move'], row['next_actor']), ('you', 'assignee'))

    def test_a_new_contribution_ends_it(self):
        task = self.integrated_task()
        self.record(task, 'Remaining scope: bound to the contribution it followed.')
        delivered = self.contribute(task, WORKER, commit='9' * 40,
                                    previous=self.latest_review_id,
                                    supersedes=self.contribution_id)
        self.assertEqual(delivered['returncode'], 0, delivered)
        row = self.row(task)
        self.assertFalse(row['followon']['applies'])
        self.assertEqual(row['followon']['state'], 'ended')
        self.assertIn('newer contribution', row['followon']['why'])


class FollowonWebRouteTests(FollowonCase):
    """The web route through the real HTTP service and its EndpointBackend."""

    def web(self):
        import http_service
        import test_http_review_fixes as fixes

        root = self.root

        class HttpHarness(fixes.Harness):
            def make_backend(harness_self):
                return http_service.EndpointBackend(sys.executable, str(KIT / 'endpoint.py'),
                                                    str(root), service=harness_self.service)

        harness = HttpHarness(methodName='runTest')
        harness.setUp()
        self.addCleanup(harness._stop_server)
        admin = harness.admin_token()
        olga = harness.create_account(admin, 'olga', 'olga-password-1')
        wanda = harness.create_account(admin, 'wanda', 'wanda-password-1')
        registered = harness.request('POST', '/v1/projects', {'project_id': 'pp',
                                                              'name': 'Follow-on web'}, token=admin)
        self.assertEqual(registered.status, 201, registered.data)
        self.assertEqual(harness.request('PUT', '/v1/projects/pp/members/' + olga,
                                         {'role': 'owner'}, token=admin).status, 200)
        self.assertEqual(harness.request('PUT', '/v1/projects/pp/members/' + wanda,
                                         {'role': 'contributor'}, token=admin).status, 200)
        return (harness, harness.login('olga', 'olga-password-1')[0],
                harness.login('wanda', 'wanda-password-1')[0])

    def test_the_web_route_records_and_withdraws_for_a_coordinator(self):
        task = self.integrated_task()
        harness, owner, _contributor = self.web()
        text = 'Remaining scope, recorded over the web route.'
        made = harness.request('POST', '/v1/projects/pp/tasks/%s/assignee-followon' % task,
                               {'text': text}, token=owner,
                               key='web-followon-' + uuid.uuid4().hex)
        self.assertEqual(made.status, 201, made.data)
        comment_id = made.data['comment_id']
        brief = harness.request('GET', '/v1/projects/pp/tasks/%s/brief' % task, token=owner)
        self.assertEqual(brief.status, 200, brief.data)
        self.assertTrue(brief.data['followon']['applies'])
        self.assertEqual(brief.data['review']['next_actor'], 'assignee')
        withdrawn = harness.request('POST', '/v1/projects/pp/tasks/%s/assignee-followon/%s/withdraw'
                                    % (task, comment_id), {'reason': 'Web withdrawal'},
                                    token=owner, key='web-followon-w-' + uuid.uuid4().hex)
        self.assertEqual(withdrawn.status, 200, withdrawn.data)
        row = self.row(task)
        self.assertEqual(row['followon']['state'], 'withdrawn')

    def test_the_web_route_without_coordinate_is_refused_and_writes_nothing(self):
        task = self.integrated_task()
        harness, _owner, contributor = self.web()
        before = self.comment_ids(task)
        journal = self.project / '.assignee-followons'
        before_journal = sorted(path.name for path in journal.glob('*.json')) if journal.is_dir() else []
        refused = harness.request('POST', '/v1/projects/pp/tasks/%s/assignee-followon' % task,
                                  {'text': 'A contributor may not hand this back'},
                                  token=contributor, key='web-followon-x-' + uuid.uuid4().hex)
        self.assertEqual(refused.status, 403, refused.data)
        self.assertEqual(self.comment_ids(task), before)
        after_journal = sorted(path.name for path in journal.glob('*.json')) if journal.is_dir() else []
        self.assertEqual(after_journal, before_journal)


if __name__ == '__main__':
    unittest.main()
