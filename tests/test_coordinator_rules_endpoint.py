"""End-to-end tests through the endpoint for the coordinator rules (kittrial-5bb.197).

Review request 01a12894, item 5: nothing drove the endpoint with a rule ON to a refusal,
so mutants that dropped a setting between the endpoint and the libraries survived. These
tests run the REAL ``endpoint.py`` (imported as a module, exactly as the forced command
runs it) against a REAL pinned bd in server mode, with the three installation rules set
in ``deployment.private.json``: each refusal is taken through ``endpoint.execute`` and
the tracker rows are compared before and after.

The web-route class at the end drives the REAL HTTP service and its real
``EndpointBackend`` over the same project, for the two web cases of item 2: an owner's
approval through the web route under ``coordinator_writes``, and an owner's approval of
their OWN contribution under ``independent_approval``.

A real bd is used when one is available (``ORCHESTRA_BD_BIN``, or ``bd`` on PATH with a
sibling ``dolt``); the classes skip otherwise. The suite is POSIX-only because
``endpoint.py`` imports fcntl.
"""
import json
import secrets
import sys
import unittest
import uuid
from pathlib import Path

KIT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(KIT), str(KIT / 'tests')]
import test_bd_label_aliases as fixture  # noqa: E402

SCOPE = {'source_commit': 'a' * 40, 'integration_commit': 'b' * 40, 'release_id': '', 'environment': ''}
COMMIT = 'd' * 40
BASE = 'e' * 40


@unittest.skipIf(fixture.endpoint is None, 'endpoint imports fcntl (POSIX-only)')
@unittest.skipIf(fixture.BD is None, 'no real bd binary (set ORCHESTRA_BD_BIN or put bd on PATH)')
class RulesHelpers:
    """A fresh bound view of the real-bd fixture plus the endpoint helpers."""

    @classmethod
    def setUpClass(cls):
        cls.runtime = type('RulesRuntime', (fixture.RealBdLabelAliasTests,), {})
        cls.runtime.setUpClass()

    @classmethod
    def tearDownClass(cls):
        cls.runtime.tearDownClass()

    def setUp(self):
        self.r = self.runtime(methodName='runTest')
        self.root = self.r.root
        self.project = self.r.project
        for key, value in fixture.admin.PROJECT_SETTINGS:
            result = self.r.bd('config', 'set', key, value)
            self.assertEqual(result.returncode, 0, result.stderr)
        self._ensure_merge_slot()
        # A baseline the previous test cannot leak into: valid operators, rules off, and
        # no damaged registry or stale merge context left behind by another test.
        for leftover in ('.sessions.json', '.merge-context.json'):
            try:
                (self.project / leftover).unlink()
            except FileNotFoundError:
                pass
        self.deployment()

    # -- helpers ---------------------------------------------------------------
    def deployment(self, operators=None, **rules):
        document = json.loads((self.root / 'deployment.private.json').read_text(encoding='utf-8'))
        # The installation switches too (1024s): a test that turned one on must not leak it into the next test
        # on this shared runtime (kittrial-5bb.434's switches read saw review_workflow_writes left on).
        for key in ('lifecycle_authors', 'independent_approval', 'coordinator_writes',
                    'review_workflow_writes', 'checkpoint_provenance_writes', 'web_lifecycle_mark_writes'):
            document.pop(key, None)
        document.update(rules)
        document['operators'] = ['ops'] if operators is None else operators
        (self.root / 'deployment.private.json').write_text(json.dumps(document), encoding='utf-8')

    def _ensure_merge_slot(self):
        shown = self.r.bd('show', 'pp-merge-slot', '--json')
        if shown.returncode:
            made = self.r.bd('create', 'Merge Slot', '--id', 'pp-merge-slot', '--labels', 'gt:slot', '--json')
            self.assertEqual(made.returncode, 0, made.stderr)

    def rows(self):
        return self.r.export_rows()

    def task(self, title='Rules task', assignee=None):
        args = ['create', title]
        if assignee:
            args += ['--assignee', assignee]
        args.append('--json')
        result = self.r.bd(*args)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)['id']

    def endpoint(self, action, actor, args, attachments=None):
        return fixture.endpoint.execute(self.root, {
            'project': 'pp', 'actor': actor, 'action': action, 'args': args,
            'attachments': attachments or {}})

    def post_review(self, task, actor, body):
        return self.endpoint('review', actor, [task, '@attachment:0'],
                             {'0': {'flag': '--file', 'text': json.dumps(body)}})

    def contribute(self, task, actor, commit=COMMIT, base=BASE, previous=None, supersedes=None):
        return self.post_review(task, actor, {
            'schema_version': 1, 'operation': 'contribute', 'operation_id': 'c-' + secrets.token_hex(8),
            'task': task, 'previous': previous, 'repository': 'ssh://git/example', 'commit': commit,
            'base_commit': base, 'delivery': {'kind': 'bundle', 'path': 'server:/rev.bundle',
                                              'sha256': 'f' * 64},
            'summary': 'Delivered and verified', 'supersedes': supersedes})

    def approve(self, task, actor, contribution):
        return self.post_review(task, actor, {
            'schema_version': 1, 'operation': 'approve', 'operation_id': 'a-' + secrets.token_hex(8),
            'task': task, 'previous': contribution, 'contribution': contribution,
            'summary': 'Reviewed and approved'})

    def lifecycle(self, actor, dimension, task, value='passed', scope=None, operation_id=None):
        body = {'schema_version': 1, 'operation_id': operation_id or 'l-' + secrets.token_hex(8),
                'task': task, 'dimension': dimension, 'value': value, 'scope': dict(scope or SCOPE),
                'evidence': ['commit:' + 'b' * 40], 'provenance': 'performed', 'actor': actor}
        if dimension == 'lifecycle-scope':
            from lifecycle import content_hash
            body['value'] = content_hash(body['scope'])
        return self.endpoint('lifecycle', actor, [json.dumps(body)])

    def coordinate(self, actor, operation, task=None, target=None):
        body = {'operation': operation}
        if task is not None:
            body.update({'task': task, 'target': target})
        return self.endpoint('coordinate', actor, [json.dumps(body)])

    def seeded_task(self, assignee='w1', actor='w1'):
        """A task with a recorded scope, so a single fact can be written for it."""
        task = self.task('rules ' + uuid.uuid4().hex[:8], assignee=assignee)
        self.lifecycle(actor, 'lifecycle-scope', task)
        return task


class CoordinatorRulesEndpointTests(RulesHelpers, unittest.TestCase):
    def test_legacy_self_approvals_cannot_authorize_facts_or_merge_slot(self):
        # Approval written with the rule OFF stays readable, but cannot authorize
        # a guarded write after it is enabled. Test author and current assignee.
        for subject in ('author','assignee'):
            self.deployment()
            author='w1' if subject=='author' else 'w2'
            task=self.seeded_task(assignee=author,actor=author)
            made=self.contribute(task,author)
            contribution=json.loads(made['stdout'])['comment_id']
            # Neither half can mask the other: for the author case, w1 is
            # still the author/approver but no longer the task's assignee.
            if subject=='author':
                reassigned=self.r.bd('update',task,'--assignee','w3','--json')
                self.assertEqual(reassigned.returncode,0,reassigned.stderr)
            if subject=='assignee':
                reassigned=self.r.bd('update',task,'--assignee','w1','--json')
                self.assertEqual(reassigned.returncode,0,reassigned.stderr)
            self.approve(task,'w1',contribution)
            self.deployment(independent_approval=True)
            for dimension in ('reviewed','integrated'):
                before=self.rows()
                with self.assertRaisesRegex(ValueError,'independent_approval.*'+subject):
                    self.lifecycle('ops',dimension,task)
                self.assertEqual(self.rows(),before)
            before=self.rows()
            with self.assertRaisesRegex(ValueError,'independent_approval.*'+subject):
                self.coordinate('ops','merge-acquire',task=task,target='main@'+'c'*40)
            self.assertEqual(self.rows(),before)

    def test_corrupt_registry_refuses_independent_writes_but_exact_assignee_can_record_scope(self):
        self.deployment(lifecycle_authors=True,independent_approval=True)
        task=self.task('corrupt registry '+uuid.uuid4().hex[:8],assignee='w1')
        (self.project/'.sessions.json').write_text('{',encoding='utf-8')
        self.lifecycle('w1','lifecycle-scope',task)
        self.lifecycle('w1','implemented',task)
        self.lifecycle('w1','tested',task)
        made=self.contribute(task,'w1')
        contribution=json.loads(made['stdout'])['comment_id']
        before=self.rows()
        with self.assertRaisesRegex(ValueError,'independent_approval.*registry cannot be read'):
            self.approve(task,'ops',contribution)
        self.assertEqual(self.rows(),before)
        # Valid empty ownership allows a different name; strict failures are not
        # confused with an installation that has not bound actors yet.
        (self.project/'.sessions.json').write_text(json.dumps({'schema_version':1,'records':{}}))
        self.approve(task,'ops',contribution)
        (self.project/'.sessions.json').write_text('{',encoding='utf-8')
        for dimension in ('reviewed','integrated'):
            before=self.rows()
            with self.assertRaisesRegex(ValueError,'independent_approval.*registry cannot be read'):
                self.lifecycle('ops',dimension,task)
            self.assertEqual(self.rows(),before)
        before=self.rows()
        with self.assertRaisesRegex(ValueError,'independent_approval.*registry cannot be read'):
            self.coordinate('ops','merge-acquire',task=task,target='main@'+'c'*40)
        self.assertEqual(self.rows(),before)

    def test_no_contribution_cannot_authorize_facts_or_merge_slot(self):
        self.deployment(independent_approval=True)
        task=self.seeded_task(assignee='w1',actor='w1')
        for dimension in ('reviewed','integrated'):
            before=self.rows()
            with self.assertRaisesRegex(ValueError,'independent_approval.*no contribution'):
                self.lifecycle('ops',dimension,task)
            self.assertEqual(self.rows(),before)
        before=self.rows()
        with self.assertRaisesRegex(ValueError,'independent_approval.*no contribution'):
            self.coordinate('ops','merge-acquire',task=task,target='main@'+'c'*40)
        self.assertEqual(self.rows(),before)

    # -- (a) lifecycle_authors through the endpoint ----------------------------
    def test_endpoint_honours_lifecycle_authors(self):
        self.deployment(lifecycle_authors=True)
        task = self.seeded_task(assignee='w1')
        before = self.rows()
        with self.assertRaises(ValueError) as caught:
            self.lifecycle('w1', 'reviewed', task)
        self.assertIn('lifecycle_authors', str(caught.exception))
        self.assertEqual(self.rows(), before)          # zero native rows changed
        allowed = self.lifecycle('ops', 'reviewed', task)
        self.assertEqual(json.loads(allowed['stdout'])['changed'], True)

    def test_endpoint_forwards_the_rule_warning_on_the_lifecycle_path(self):
        # M23: the endpoint must forward the coordinator-rule configuration warning.
        self.deployment(lifecycle_authors='not-a-bool')
        task = self.seeded_task(assignee='w1')
        answer = self.lifecycle('w1', 'implemented', task)
        self.assertIn('WARNING: deployment lifecycle_authors', answer['stderr'])
        self.assertIn('not true or false', answer['stderr'])

    # -- (c) coordinator_writes through the endpoint ---------------------------
    def test_endpoint_honours_coordinator_writes_on_approve(self):
        self.deployment(coordinator_writes=True)
        task = self.task('cw approve ' + uuid.uuid4().hex[:8], assignee='w1')
        made = self.contribute(task, 'w1')
        contribution = json.loads(made['stdout'])['comment_id']
        before = self.rows()
        with self.assertRaises(ValueError) as caught:
            self.approve(task, 'w2', contribution)
        self.assertIn('coordinator_writes', str(caught.exception))
        self.assertEqual(self.rows(), before)
        allowed = self.approve(task, 'ops', contribution)
        self.assertEqual(json.loads(allowed['stdout'])['review_state'], 'awaiting-integration')

    def test_endpoint_honours_coordinator_writes_on_merge_acquire(self):
        self.deployment(coordinator_writes=True)
        task = self.task('cw merge ' + uuid.uuid4().hex[:8], assignee='w1')
        before = self.rows()
        with self.assertRaises(ValueError) as caught:
            self.coordinate('w2', 'merge-acquire', task=task, target='main@' + 'c' * 40)
        from review_workflow import CoordinatorRuleRefusal
        self.assertIsInstance(caught.exception, CoordinatorRuleRefusal)
        self.assertIn('coordinator_writes', str(caught.exception))
        self.assertEqual(self.rows(), before)
        acquired = self.coordinate('ops', 'merge-acquire', task=task, target='main@' + 'c' * 40)
        self.assertTrue(json.loads(acquired['stdout'])['acquired'])
        self.coordinate('ops', 'merge-release')

    # -- (b) independent_approval through the endpoint -------------------------
    def test_endpoint_honours_independent_approval_on_approve(self):
        self.deployment(independent_approval=True)
        task = self.task('ia approve ' + uuid.uuid4().hex[:8], assignee='w1')
        made = self.contribute(task, 'w1')
        contribution = json.loads(made['stdout'])['comment_id']
        before = self.rows()
        with self.assertRaises(ValueError) as caught:
            self.approve(task, 'w1', contribution)
        self.assertIn('independent_approval', str(caught.exception))
        self.assertEqual(self.rows(), before)
        allowed = self.approve(task, 'w2', contribution)
        self.assertEqual(json.loads(allowed['stdout'])['review_state'], 'awaiting-integration')

    def test_endpoint_refuses_a_same_principal_approval(self):
        # Item 1's wiring: with independent_approval ALONE the endpoint must still read
        # the registry, so a second actor of the author's principal is one party.
        self.deployment(independent_approval=True)
        (self.project / '.sessions.json').write_text(
            json.dumps({'schema_version': 1, 'records': {},
                        'owners': {'w2': 'lane:kit', 'w2b': 'lane:kit'}}), encoding='utf-8')
        task = self.task('principal ' + uuid.uuid4().hex[:8], assignee='w2')
        made = self.contribute(task, 'w2')
        contribution = json.loads(made['stdout'])['comment_id']
        before = self.rows()
        with self.assertRaises(ValueError) as caught:
            self.approve(task, 'w2b', contribution)
        self.assertIn('independent_approval', str(caught.exception))
        self.assertEqual(self.rows(), before)
        # An actor of another principal (or of none) may approve.
        other = self.task('principal two ' + uuid.uuid4().hex[:8], assignee='w2')
        second = self.contribute(other, 'w2')
        second_id = json.loads(second['stdout'])['comment_id']
        allowed = self.approve(other, 'w9', second_id)
        self.assertEqual(json.loads(allowed['stdout'])['review_state'], 'awaiting-integration')

    def test_endpoint_honours_the_second_refusal_of_rule_three(self):
        """Part (b): reviewed/integrated need another party's approval of THIS contribution."""
        self.deployment(independent_approval=True)
        task = self.seeded_task(assignee='w1', actor='w1')
        made = self.contribute(task, 'w1')
        contribution = json.loads(made['stdout'])['comment_id']
        before = self.rows()
        with self.assertRaises(ValueError) as caught:
            self.lifecycle('w1', 'reviewed', task)
        self.assertIn('independent_approval', str(caught.exception))
        self.assertEqual(self.rows(), before)
        # An older contribution's approval must not carry over a superseding one.
        approved = self.approve(task, 'w2', contribution)
        approve_id = json.loads(approved['stdout'])['comment_id']
        superseding = self.contribute(task, 'w1', commit='1' * 40,
                                      previous=approve_id, supersedes=contribution)
        self.assertEqual(json.loads(superseding['stdout'])['review_state'], 'awaiting-review')
        with self.assertRaises(ValueError) as caught:
            self.lifecycle('w1', 'integrated', task)
        self.assertIn('independent_approval', str(caught.exception))
        # A party other than the author and the assignee approves the CURRENT contribution.
        current = json.loads(superseding['stdout'])['comment_id']
        self.approve(task, 'w2', current)
        allowed = self.lifecycle('w1', 'integrated', task)
        self.assertEqual(json.loads(allowed['stdout'])['changed'], True)

    def test_endpoint_honours_independent_approval_on_merge_acquire(self):
        self.deployment(independent_approval=True)
        task = self.task('ia merge ' + uuid.uuid4().hex[:8], assignee='w1')
        made = self.contribute(task, 'w1')
        contribution = json.loads(made['stdout'])['comment_id']
        before = self.rows()
        with self.assertRaises(ValueError) as caught:
            self.coordinate('w1', 'merge-acquire', task=task, target='main@' + 'c' * 40)
        from review_workflow import CoordinatorRuleRefusal
        self.assertIsInstance(caught.exception, CoordinatorRuleRefusal)
        self.assertIn('independent_approval', str(caught.exception))
        self.assertEqual(self.rows(), before)
        self.approve(task, 'w2', contribution)
        acquired = self.coordinate('w1', 'merge-acquire', task=task, target='main@' + 'c' * 40)
        self.assertTrue(json.loads(acquired['stdout'])['acquired'])
        self.coordinate('w1', 'merge-release')

    # -- item 4: all-off is main; a rule must not stop reads -------------------
    def test_all_off_serves_coordinate_with_a_malformed_operator_list(self):
        # item 4a: with the rules OFF a malformed `operators` value must not be read.
        document = json.loads((self.root / 'deployment.private.json').read_text(encoding='utf-8'))
        document.pop('lifecycle_authors', None); document.pop('independent_approval', None)
        document.pop('coordinator_writes', None); document['operators'] = 5
        (self.root / 'deployment.private.json').write_text(json.dumps(document), encoding='utf-8')
        task = self.task('all off ' + uuid.uuid4().hex[:8], assignee='w1')
        checked = self.coordinate('w1', 'merge-check')
        self.assertIsInstance(json.loads(checked['stdout']), dict)
    def test_a_damaged_registry_does_not_stop_reads_or_a_listed_operator(self):
        # item 4c: only the two writes that need the registry are refused, and a plain
        # listed operator does not need it at all.
        self.deployment(coordinator_writes=True)
        task = self.task('registry ' + uuid.uuid4().hex[:8], assignee='w1')
        (self.project / '.sessions.json').write_text('{ this is not JSON', encoding='utf-8')
        read = self.endpoint('work', 'w1', ['--json'])
        self.assertEqual(read['returncode'], 0, read['stderr'])
        shown = self.endpoint('review', 'w1', [task])
        self.assertEqual(shown['returncode'], 0, shown['stderr'])
        made = self.contribute(task, 'w1')
        contribution = json.loads(made['stdout'])['comment_id']
        allowed = self.approve(task, 'ops', contribution)
        self.assertEqual(json.loads(allowed['stdout'])['review_state'], 'awaiting-integration')
        acquired = self.coordinate('ops', 'merge-acquire', task=task, target='main@' + 'c' * 40)
        self.assertTrue(json.loads(acquired['stdout'])['acquired'])
        self.coordinate('ops', 'merge-release')
        # An unlisted actor still needs the registry: the write is refused, and the
        # sentence says how to turn the rule off.
        other = self.task('registry two ' + uuid.uuid4().hex[:8], assignee='w3')
        second = self.contribute(other, 'w3')
        second_id = json.loads(second['stdout'])['comment_id']
        with self.assertRaises(ValueError) as caught:
            self.approve(other, 'w4', second_id)
        self.assertIn('coordinator-rules off --rule coordinator_writes', str(caught.exception))


@unittest.skipIf(fixture.endpoint is None, 'endpoint imports fcntl (POSIX-only)')
@unittest.skipIf(fixture.BD is None, 'no real bd binary (set ORCHESTRA_BD_BIN or put bd on PATH)')
class CoordinatorRulesWebRouteTests(RulesHelpers, unittest.TestCase):
    """The web route's own two cases (item 2), through the real HTTP service."""

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
        oscar = harness.create_account(admin, 'oscar', 'oscar-password-1')
        registered = harness.request('POST', '/v1/projects', {'project_id': 'pp', 'name': 'Rules web'}, token=admin)
        self.assertEqual(registered.status, 201, registered.data)
        for account in (olga, oscar):
            member = harness.request('PUT', '/v1/projects/pp/members/' + account, {'role': 'owner'}, token=admin)
            self.assertEqual(member.status, 200, member.data)
        return harness, harness.login('olga', 'olga-password-1')[0], harness.login('oscar', 'oscar-password-1')[0]

    def web_contribution(self, harness, token, task):
        body = {'operation': 'contribute', 'schema_version': 1, 'operation_id': 'w-' + secrets.token_hex(8),
                'previous': None, 'repository': 'ssh://git/example', 'commit': COMMIT, 'base_commit': BASE,
                'supersedes': None,
                'delivery': {'kind': 'bundle', 'path': 'server:/rev.bundle', 'sha256': 'f' * 64},
                'summary': 'Delivered over the web'}
        return harness.request('POST', '/v1/projects/pp/tasks/%s/reviews' % task, body, token=token)

    def test_actual_http_registry_refusal_keeps_rule_and_recovery_sentence_once(self):
        self.deployment(independent_approval=True)
        harness,olga,_oscar=self.web()
        task=self.seeded_task(assignee='w1',actor='w1')
        made=self.contribute(task,'w1'); contribution=json.loads(made['stdout'])['comment_id']
        (self.project/'.sessions.json').write_text('{',encoding='utf-8')
        before=self.rows()
        response=harness.request('POST','/v1/projects/pp/tasks/%s/reviews'%task,
            dict(operation='approve',schema_version=1,operation_id='bad-reg-'+secrets.token_hex(8),
                 previous=contribution,contribution=contribution,summary='Review accepts'),token=olga)
        self.assertEqual(response.status,422,response.data)
        text=response.data['error']['message']
        self.assertIn('independent_approval',text)
        self.assertIn('--rule independent_approval --actor OPERATOR',text)
        self.assertEqual(text.lower().count('nothing was changed'),1)
        self.assertNotIn(str(self.project),json.dumps(response.data))
        self.assertEqual(self.rows(),before)

    def test_actual_web_brief_wait_is_caller_relative_and_matches_canonical_brief(self):
        import work
        harness,olga,oscar=self.web(); admin=harness.admin_token()
        extra={}
        for name in ('third','named','coordinator'):
            user=harness.create_account(admin,name,name+'-password-1')
            member=harness.request('PUT','/v1/projects/pp/members/'+user,
                {'role':'coordinator' if name=='coordinator' else 'contributor'},token=admin)
            self.assertEqual(member.status,200,member.data)
            extra[name]=(user,harness.login(name,name+'-password-1')[0])
        made=harness.request('POST','/v1/projects/pp/tasks',{'title':'caller wait matrix'},token=olga)
        self.assertEqual(made.status,201,made.data); task=made.data['id']
        claim=harness.request('POST','/v1/projects/pp/tasks/'+task+'/claim',{},token=olga)
        self.assertEqual(claim.status,200,claim.data)
        delivered=self.web_contribution(harness,olga,task)
        self.assertEqual(delivered.status,201,delivered.data)
        first=harness.request('GET','/v1/projects/pp/tasks/'+task+'/brief',token=olga)
        self.assertEqual(first.status,200,first.data)
        author=first.data['review']['contribution']['author']
        # Deliver as the assigned author, then hand off: author and current
        # assignee are distinct callers without bypassing contribute's ownership.
        assignee=harness.login('oscar','oscar-password-1')[2]['id']
        changed=self.r.bd('update',task,'--assignee',assignee,'--json')
        self.assertEqual(changed.returncode,0,changed.stderr)
        contribution=first.data['review']['contribution']['id']
        document=json.loads((self.root/'deployment.private.json').read_text())
        document['review_workflow_writes']=True
        (self.root/'deployment.private.json').write_text(json.dumps(document))
        asked=harness.request('POST','/v1/projects/pp/tasks/'+task+'/reviews',
            dict(schema_version=1,operation='request-review',operation_id='named-'+secrets.token_hex(8),
                 previous=contribution,contribution=contribution,reviewer=extra['named'][0],summary='Please review'),
            token=oscar)
        self.assertEqual(asked.status,201,asked.data)
        callers=[('author',author,olga),('assignee',assignee,oscar),
                 ('third',extra['third'][0],extra['third'][1]),
                 ('coordinator',extra['coordinator'][0],extra['coordinator'][1]),
                 ('named',extra['named'][0],extra['named'][1])]
        for enabled in (False,True):
            self.deployment(independent_approval=enabled)
            before=self.rows()
            for role,actor,token in callers:
                with self.subTest(rule=enabled,caller=role):
                    response=harness.request('GET','/v1/projects/pp/tasks/'+task+'/brief',token=token)
                    self.assertEqual(response.status,200,response.data)
                    # HTTP actor names are reserved to the service, so compare
                    # its result to the shared canonical reader, not a forged SSH
                    # request under the account's name.
                    import briefing, sessions
                    canonical=briefing.brief(self.rows(),'pp',task,actor=actor,operators=['ops'],
                        journal=self.project,independent_approval=enabled,
                        owners=lambda:sessions.owners(self.project))['review']['waiting_for_approval']
                    self.assertEqual(response.data['review']['waiting_for_approval'],canonical)
                    expected=work.APPROVAL_WAIT if enabled and role in ('author','assignee') else work.review_move(
                        'awaiting-review',task,role=='assignee',named_reviewer=role=='named')[1]
                    self.assertEqual(canonical,expected)
                    self.assertLess(len(canonical),240)
            self.assertEqual(self.rows(),before)

    def test_an_owner_approves_through_the_web_route_under_coordinator_writes(self):
        self.deployment(coordinator_writes=True)
        harness, olga, _oscar = self.web()
        # A HOST contribution, on a task assigned to a host actor.
        task = self.seeded_task(assignee='w1', actor='w1')
        made = self.contribute(task, 'w1')
        contribution = json.loads(made['stdout'])['comment_id']
        approved = harness.request('POST', '/v1/projects/pp/tasks/%s/reviews' % task,
                                   {'operation': 'approve', 'schema_version': 1,
                                    'operation_id': 'wa-' + secrets.token_hex(8),
                                    'previous': contribution, 'contribution': contribution,
                                    'summary': 'Owner accepts'}, token=olga)
        self.assertEqual(201, approved.status, approved.data)

    def test_an_owner_approving_their_own_web_contribution_is_refused_by_name(self):
        self.deployment(independent_approval=True, coordinator_writes=True)
        harness, olga, oscar = self.web()
        created = harness.request('POST', '/v1/projects/pp/tasks', {'title': 'web own'}, token=olga)
        self.assertEqual(201, created.status, created.data)
        task = created.data['id']
        claimed = harness.request('POST', '/v1/projects/pp/tasks/%s/claim' % task, {}, token=olga)
        self.assertEqual(200, claimed.status, claimed.data)
        made = self.web_contribution(harness, olga, task)
        self.assertEqual(201, made.status, made.data)
        contribution = made.data.get('comment_id') or (made.data.get('contribution') or {}).get('comment_id')
        own = harness.request('POST', '/v1/projects/pp/tasks/%s/reviews' % task,
                              {'operation': 'approve', 'schema_version': 1,
                               'operation_id': 'wo-' + secrets.token_hex(8),
                               'previous': contribution, 'contribution': contribution,
                               'summary': 'Own approval'}, token=olga)
        self.assertEqual(422, own.status, own.data)
        # The person sees this sentence in message, not only the diagnostic detail.
        self.assertIn('independent_approval', own.data['error']['message'])
        self.assertIn('--rule independent_approval --actor OPERATOR', own.data['error']['message'])
        other = harness.request('POST', '/v1/projects/pp/tasks/%s/reviews' % task,
                                {'operation': 'approve', 'schema_version': 1,
                                 'operation_id': 'wx-' + secrets.token_hex(8),
                                 'previous': contribution, 'contribution': contribution,
                                 'summary': 'Another owner accepts'}, token=oscar)
        self.assertEqual(201, other.status, other.data)


if __name__ == '__main__':
    unittest.main()
