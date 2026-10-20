"""Real-bd end-to-end tests for the per-project claiming setting (kittrial-5bb.352).

Uses the same real web service + real endpoint + real bd harness as
``test_claim_held``: its mixins are reused (the module also brings the harness's
label-alias tests, which run here too), while these classes add the claiming tests.
The setting is set through the web route, and both claim routes (the HTTP route and
the host ``bd update --claim`` route), the listings that decide what is offered, and
the audit are read through the real stack.
"""
import json
import os
import sys
import unittest
from pathlib import Path

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT))
sys.path.insert(0, str(KIT / 'tests'))
import claiming
import record_json
import work
import test_claim_held as held
import test_http_review_fixes as fixes
import test_bd_label_aliases as rb


class ClaimingRealBdTests(held.Members, fixes.Harness, rb.RealBdLabelAliasTests):
    """The real web service, the real endpoint.py, a real bd, project ``pp``."""

    def make_backend(self):
        return held.http_service.EndpointBackend(
            sys.executable, str(KIT / 'endpoint.py'), str(self.root), service=self.service)

    def setUp(self):
        fixes.Harness.setUp(self)
        admin = self.admin_token()
        registered = self.request('POST', '/v1/projects',
                                  {'project_id': 'pp', 'name': 'PP'}, token=admin)
        self.assertIn(registered.status, (200, 201, 409), registered.data)
        self.members('pp')
        self.project = self.root / 'projects' / 'pp'
        # Every test starts from the default, open.
        (self.project / claiming.FILENAME).unlink(missing_ok=True)

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

    def set_claiming(self, who, value, token=None):
        return self.request('PUT', '/v1/projects/pp/claiming', {'claiming': value},
                            token=token or self.tokens[who])

    def host(self, args, actor='session-worker'):
        return rb.endpoint.execute(self.root, {'project': 'pp', 'actor': actor,
                                               'action': 'bd', 'args': args,
                                               'attachments': {}})

    def bd_row(self, task):
        found = json.loads(self.bd('show', task, '--json').stdout)
        found = found[0] if isinstance(found, list) else found
        return found.get('status'), found.get('assignee')

    def work_page(self, actor='session-worker'):
        """The client's own work projection over a real export, with the project directory.

        ``work`` is run by the client, not the endpoint, so this calls the same library
        function the client does, over the exact rows a real ``bd export --all`` gives,
        with the project directory the setting lives in.
        """
        exported = self.bd('export', '--all')
        self.assertEqual(0, exported.returncode, exported)
        rows = record_json.loads_rows(exported.stdout)
        return work.queue(rows, actor, ['--claimable', '--json'], journal=self.project)

    def test_both_claim_routes_refuse_an_unassigned_task_then_the_assignee_starts_it(self):
        task = self.new('claiming gate')
        set_as = self.set_claiming('alex', 'assigned')
        self.assertEqual(200, set_as.status, set_as.data)
        self.assertEqual('assigned', set_as.data.get('claiming'))
        # The web/HTTP claim route.
        refused = self.claim('casey', task)
        self.assertEqual(409, refused.status, refused.data)
        self.assertEqual(claiming.CLAIM_REFUSAL, held.message(refused))
        self.assertEqual(('open', None), self.bd_row(task))
        # The host route, through the real endpoint. The guard refuses before bd is
        # called, so the direct caller sees the ValueError the client turns into its
        # refusal sentence.
        with self.assertRaises(ValueError) as caught:
            self.host(['update', task, '--claim', '--json'])
        self.assertIn(claiming.CLAIM_REFUSAL, str(caught.exception))
        self.assertEqual(('open', None), self.bd_row(task))
        # The coordinator assigns it to casey; starting it is then not the refused
        # "claim of an unassigned task".
        assigned = self.request('POST', '/v1/projects/pp/tasks/%s/assign' % task,
                                {'expected_assignee': None, 'assignee': self.ids['casey'],
                                 'reason': 'hand the task out',
                                 'allow_pending_review': False, 'allow_held_slot': False},
                                token=self.tokens['alex'], key='assign-claiming-1')
        self.assertIn(assigned.status, (200, 201), assigned.data)
        started = self.claim('casey', task)
        self.assertIn(started.status, (200, 409), started.data)
        self.assertEqual(('in_progress', self.ids['casey']), self.bd_row(task))
        audit = self.request('GET', '/v1/projects/pp/audit?limit=100', token=self.tokens['alex'])
        actions = [(item.get('action'), item.get('outcome')) for item in audit.data['items']]
        self.assertIn(('projects.claiming', 'committed'), actions)

    def test_the_listings_drop_unassigned_work_and_carry_the_empty_sentence(self):
        task = self.new('claiming listing')
        # First, open: the task is offered.
        before = self.work_page()
        self.assertTrue(any(item.get('task') == task for item in before.get('items') or []),
                        before)
        # Flip the setting with NO tracker write, then read again over the SAME kind of
        # export: the mode is read fresh from the project directory every call, so the
        # tracker read cache (keyed on the dolt commit) cannot hide the change.
        self.assertEqual(200, self.set_claiming('alex', 'assigned').status)
        after = self.work_page()
        self.assertEqual(('assigned', 0), (after.get('claiming'), after.get('total')))
        self.assertEqual(claiming.EMPTY_SENTENCE, after.get('empty_message'))
        self.assertFalse(any(item.get('task') == task for item in after.get('items') or []))
        ready = self.host(['ready', '--json'])
        self.assertEqual(0, ready['returncode'], ready)
        self.assertEqual([], json.loads(ready['stdout']))
        # Back to open, the same rows are offered again.
        self.assertEqual(200, self.set_claiming('alex', 'open').status)
        again = self.work_page()
        self.assertTrue(any(item.get('task') == task for item in again.get('items') or []),
                        again)

    def test_only_owners_and_coordinators_may_set_it(self):
        refused = self.set_claiming('casey', 'assigned')
        self.assertEqual(403, refused.status, refused.data)
        self.assertEqual('open', self.work_page().get('claiming'))
        # A coordinator (casey promoted by the owner) may set it.
        promoted = self.request('PUT', '/v1/projects/pp/members/%s' % self.ids['casey'],
                                {'role': 'coordinator'}, token=self.tokens['alex'])
        self.assertEqual(200, promoted.status, promoted.data)
        allowed = self.set_claiming('casey', 'assigned')
        self.assertEqual(200, allowed.status, allowed.data)
        project = self.request('GET', '/v1/projects/pp', token=self.tokens['casey'])
        self.assertEqual('assigned', project.data['claiming']['claiming'])
        # The record names the actor that made the change (the account id the kit uses
        # for attribution everywhere else).
        self.assertEqual(self.ids['casey'], project.data['claiming']['set_by'])
        self.assertTrue(project.data['can_coordinate'])

    def test_a_worker_credential_cannot_set_it(self):
        secret = self.issue_credential(self.tokens['alex'], 'pp', label='w', actor='worker-a')['secret']
        refused = self.request('PUT', '/v1/projects/pp/claiming', {'claiming': 'assigned'},
                               token=secret)
        self.assertEqual(403, refused.status, refused.data)


if __name__ == '__main__':
    unittest.main()
