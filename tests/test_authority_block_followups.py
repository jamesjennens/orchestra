""".267: a valid read block cannot reach a host-only effect or reserve an identity."""
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

KIT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(KIT), str(KIT / 'tests')]
import http_authority
import lifecycle
import test_endpoint_authority_block as harness
import test_bd_label_aliases as fixture
from test_coordinator_rules_endpoint import RulesHelpers


class CapabilityShapeTests(unittest.TestCase):
    def test_nontext_capabilities_are_known_denials_for_sessions_and_credentials(self):
        state = harness.authority_state()
        for make in (lambda: harness.session_block(harness.OWNER, 'read'),
                     lambda: harness.agent_block('read')):
            for value in ([], ['read'], {}, 1, True, None):
                with self.subTest(via=make()['via'], value=value):
                    with self.assertRaises(http_authority.AuthorityDenied) as denied:
                        http_authority.decide(state, dict(make(), capability=value))
                    self.assertEqual(403, denied.exception.status)
                    self.assertIn('capability as text', denied.exception.message)


@harness.POSIX
class EndpointRefusalTests(harness.EndpointHarness):
    def test_unknown_actions_and_operations_cannot_reach_any_tracker_call(self):
        for action, args in (('unknown-future-action', []), ('work', []),
                             ('lifecycle', ['{}']), ('bd', ['close', harness.TASK]),
                             ('bd', ['reopen', harness.TASK]),
                             ('bd', ['comments', 'add', harness.TASK, 'remark']),
                             ('bd', ['dep', 'add', harness.TASK, 'pp-other']),
                             ('coordinate', [json.dumps({'operation': 'create-child'})]),
                             ('coordinate', [json.dumps({'operation': 'future-operation'})]),
                             ('web-merge-slot', [json.dumps({'operation': 'future-operation'})])):
            with self.subTest(action=action, args=args):
                request = {'project': harness.PROJECT, 'actor': harness.CONTRIBUTOR,
                           'action': action, 'args': args, 'operation_id': 'must-not-reserve'}
                answer = self.web(request, harness.session_block(harness.CONTRIBUTOR, 'read'))
                self.assertRefused(answer)
                self.assertIn('table does not allow this action or operation', answer['stderr'])
                self.assertEqual([], self.tracker.calls)
        self.assertEqual(['.beads'], sorted(x.name for x in (self.root / 'projects' / harness.PROJECT).iterdir()))

    def test_help_and_list_attachments_are_refused_before_reserving_review_identity(self):
        for mutation in ('help', 'attachments'):
            request = self.review_request(harness.OWNER)
            if mutation == 'help':
                request['args'].append('--help')
            else:
                request['attachments'] = list(request['attachments'].values())
            with self.subTest(mutation=mutation):
                answer = self.web(request, harness.session_block(harness.OWNER, 'reviews.approve'))
                self.assertRefused(answer)
                self.assertNotEqual(124, answer['returncode'])
                self.assertEqual([], self.tracker.calls)
                project = self.root / 'projects' / harness.PROJECT
                self.assertFalse(http_authority.journal_path(project).exists())
                self.assertEqual(['.beads'], sorted(x.name for x in project.iterdir()))

    def test_bad_capability_is_a_sentence_and_not_a_typeerror(self):
        answer = self.web(self.review_request(harness.OWNER),
                          harness.session_block(harness.OWNER, ['reviews.approve']))
        self.assertRefused(answer)
        self.assertIn('capability as text', answer['stderr'])
        self.assertEqual([], self.tracker.calls)

    def test_the_host_still_ignores_a_block_and_can_write_a_raw_comment(self):
        request = {'project': harness.PROJECT, 'actor': 'worker', 'action': 'bd',
                   'args': ['comments', 'add', harness.TASK, 'Host comment'],
                   'authority': harness.session_block(harness.OWNER, ['read'])}
        answer = harness.endpoint.execute(self.root, request)
        self.assertEqual(0, answer['returncode'], answer)
        self.assertEqual(1, len([command for _, command in self.tracker.calls
                                 if command[:2] == ['comments', 'add']]))


@unittest.skipIf(fixture.endpoint is None, 'endpoint imports fcntl (POSIX-only)')
@unittest.skipIf(fixture.BD is None, 'no real bd binary (set ORCHESTRA_BD_BIN or put bd on PATH)')
class NativeBlockRefusalTests(RulesHelpers, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory(prefix='authority-block-followups-')
        self.addCleanup(self.tmp.cleanup)
        self.store = Path(self.tmp.name) / 'authority.json'
        state = harness.authority_state()
        state['memberships']['pp'][harness.CONTRIBUTOR] = 'viewer'
        self.store.write_text(json.dumps(state), encoding='utf-8')
        self.config = http_authority.AuthorityConfig(str(self.store))

    def snapshot(self):
        # Preserve the actual export text, not only an expected label or a stub's calls.
        export = self.r.bd('export', '--all')
        self.assertEqual(0, export.returncode, export.stderr)
        sidecars = {str(x.relative_to(self.project)): x.read_bytes()
                    for x in self.project.rglob('*') if x.is_file()
                    and '.beads' not in x.relative_to(self.project).parts}
        return export.stdout, sidecars, self.store.read_bytes()

    def request(self, action, args, attachments=None, actor=harness.CONTRIBUTOR, capability='read'):
        return fixture.endpoint.execute(self.root, {
            'project': 'pp', 'actor': actor, 'action': action, 'args': args,
            'attachments': {} if attachments is None else attachments,
            'operation_id': 'untrusted-%d' % time.time_ns(),
            'authority': harness.session_block(actor, capability)},
            authority_config=self.config, require_authority=True)

    def test_x7_valid_viewer_block_has_zero_effect_for_every_host_only_action(self):
        task = self.task('X7 target', 'other-party')
        other = self.task('X7 dependency')
        # A real, unapproved contribution by somebody else, as in the reported attack.
        self.assertEqual(0, self.contribute(task, 'other-party')['returncode'])
        scope = dict(source_commit='d' * 40, integration_commit='e' * 40, release_id='', environment='')
        payloads = []
        for dimension in ('lifecycle-scope', 'reviewed', 'integrated'):
            payloads.append(dict(schema_version=1, operation_id='x7-' + dimension, task=task,
                                 dimension=dimension,
                                 value=lifecycle.content_hash(scope) if dimension == 'lifecycle-scope' else 'passed',
                                 scope=scope, evidence=['synthetic attack'], provenance='reported',
                                 actor=harness.CONTRIBUTOR))
        for body in payloads:
            lifecycle.validate_payload(body)
        cases = [('bd', ['close', task]), ('bd', ['reopen', task]),
                 ('bd', ['comments', 'add', task, 'Viewer remark']),
                 ('bd', ['dep', 'add', task, other]),
                 ('coordinate', [json.dumps({'operation': 'create-child', 'parent': task,
                                             'title': 'Viewer child', 'request_id': 'x7-child',
                                             'description': '', 'type': 'task'})]),
                 ('coordinate', [json.dumps({'operation': 'merge-acquire', 'task': task, 'target': 'main'})])]
        cases += [('lifecycle', [json.dumps(body)]) for body in payloads]
        for action, args in cases:
            with self.subTest(action=action, args=args):
                before = self.snapshot()
                answer = self.request(action, args)
                self.assertEqual((126, 403), (answer.get('returncode'), answer.get('authority_status')), answer)
                self.assertEqual(before, self.snapshot())
                self.assertFalse(http_authority.journal_path(self.project).exists())

    def test_forged_review_help_and_attachments_have_zero_native_and_journal_effects(self):
        task = self.task('Malformed web review', 'other-party')
        contributed = json.loads(self.contribute(task, 'other-party')['stdout'])
        body = dict(schema_version=1, operation='approve', operation_id='malformed-review', task=task,
                    previous=contributed['comment_id'], contribution=contributed['comment_id'], summary='Review')
        attachments = {'0': {'flag': '--file', 'text': json.dumps(body)}}
        for args, carried in (([task, '@attachment:0', '--help'], attachments),
                              ([task, '@attachment:0'], list(attachments.values()))):
            with self.subTest(args=args, attachments=type(carried).__name__):
                before = self.snapshot()
                answer = self.request('review', args, carried, harness.OWNER, 'reviews.approve')
                self.assertEqual((126, 403), (answer.get('returncode'), answer.get('authority_status')), answer)
                self.assertEqual(before, self.snapshot())
                self.assertFalse(http_authority.journal_path(self.project).exists())

    def test_same_host_close_and_comment_need_no_web_table_entry(self):
        task = self.task('Host control')
        for args in (['close', task, '--json'], ['reopen', task, '--json'],
                     ['comments', 'add', task, 'Ordinary host remark', '--json']):
            answer = self.endpoint('bd', 'worker', args)
            self.assertEqual(0, answer['returncode'], answer)
        row = next(x for x in self.rows() if x['id'] == task)
        self.assertEqual('open', row['status'])
        self.assertTrue(any(x['text'] == 'Ordinary host remark' for x in row['comments']))
        # This is the same complete create-child shape the denied viewer sent,
        # so its refusal cannot be attributed to missing required fields.
        body = dict(operation='create-child', parent=task, title='Host child',
                    request_id='host-child-' + str(time.time_ns()), description='', type='task')
        answer = self.endpoint('coordinate', 'worker', [json.dumps(body)])
        self.assertEqual(0, answer['returncode'], answer)
        child = json.loads(answer['stdout'])['id']
        self.assertTrue(any(x['id'] == child and x['title'] == 'Host child' for x in self.rows()))

    def test_unmarked_worker_descriptor_is_checked_before_the_table_refusal(self):
        task = self.task('Worker descriptor control')
        state = json.loads(self.store.read_text())
        moment = time.time()
        state['credentials']['worker-token'] = {
            'id': 'worker-token', 'user_id': harness.OWNER, 'project_id': 'pp',
            'actor': 'ordinary-worker', 'revoked': False, 'expires_at': moment + 3600,
            'created_at': '2026-10-01T00:00:00Z', 'scopes': ['tasks']}
        self.store.write_text(json.dumps(state), encoding='utf-8')
        before = self.snapshot()
        descriptor = dict(via='credential', user_id=harness.OWNER, credential_id='worker-token',
                          session_hash=None, project='pp', capability='read', now=moment + 5)
        with mock.patch.object(fixture.admin, 'run_bd', wraps=fixture.admin.run_bd) as native_run:
            answer = fixture.endpoint.execute(self.root, {
                'project': 'pp', 'actor': 'ordinary-worker', 'action': 'bd',
                'args': ['close', task], 'operation_id': 'worker-must-not-reserve',
                'authority': descriptor}, authority_config=self.config, require_authority=True)
        self.assertEqual((126, 403), (answer.get('returncode'), answer.get('authority_status')), answer)
        # The comparison itself has no reads, but the preceding namespace check
        # makes this export before it can certify the worker's first request.
        self.assertEqual(1, native_run.call_count)
        self.assertEqual(['export', '--all'], native_run.call_args.args[2])
        self.assertEqual(before, self.snapshot())


if __name__ == '__main__':
    unittest.main()
