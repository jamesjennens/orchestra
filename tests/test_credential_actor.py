"""A worker credential does not write under a name that is somebody else's on the host (kittrial-5bb.184).

A worker credential writes under the actor namespace its issuer chose, and the tracker's
rows carry that name and nothing else. A project owner issued one named as the host
coordinator's registered session and with it created a task, claimed one, contributed,
answered the changes requested of the real coordinator on the coordinator's own task and
wrote its checkpoint. Only the shape of another account's or agent's id was refused.

The rule is ``actor_names.collision``. It is applied where a credential is issued (the
route asks the host through the backend) and where it is used (the endpoint, for every
request the web service sends with a descriptor), so a credential issued before the rule,
or a name that was put on a list afterwards, is refused when it writes.
"""
import io
import json
import os
import sys
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT))
sys.path.insert(0, str(KIT / 'tests'))
import actor_names
import http_authority
import sessions
import test_http_review_fixes as fixes

SESSION_A = 'session-11111111-2222-3333-4444-555555555555'
SESSION_FREE = 'session-99999999-8888-7777-6666-555555555555'
HOST = {'sessions': [SESSION_A], 'operators': ['ops-lead', 'team/alice'], 'verifiers': ['verity']}
#: (what it is, the namespace asked for, the rule it breaks)
TAKEN = (
    ('a registered session actor', SESSION_A, actor_names.SESSION),
    ('a label under it', SESSION_A + '/night', actor_names.SESSION),
    ('the same in capitals', SESSION_A.upper(), actor_names.SESSION),
    ('a name of that shape that nobody registered', SESSION_FREE, actor_names.SESSION_SHAPED),
    ('that shape with a label and capitals', SESSION_FREE.upper() + '/x', actor_names.SESSION_SHAPED),
    ('an operator', 'ops-lead', actor_names.OPERATOR),
    ('a label under an operator', 'ops-lead/night', actor_names.OPERATOR),
    ('an operator in other letters', 'OPS-Lead', actor_names.OPERATOR),
    ('the head of a listed label', 'team', actor_names.OPERATOR),
    ('another label under that head', 'team/bob', actor_names.OPERATOR),
    ('a verifier', 'verity', actor_names.VERIFIER),
    ('a label under a verifier', 'Verity/2', actor_names.VERIFIER),
    ("the service's namespace", 'http', actor_names.SERVICE),
    ("the service's read actor", 'http/read', actor_names.SERVICE),
    ("the service's namespace in capitals", 'HTTP', actor_names.SERVICE),
)
#: The operator list itself takes no name with a slash (`recovery.identity`), so on a real host
#: the two rows about `team/alice` cannot arise; the rule is tested with them all the same.
REAL_TAKEN = tuple(row for row in TAKEN if not row[1].casefold().startswith('team'))
FREE = ('worker-a', 'worker-a/sub', 'ops-lead2', 'ops', 'verity.b', 'httpx', 'session-1', 'session', 'teams/alice',
        'session-11111111-2222-3333-4444-55555555555', 'xsession-11111111-2222-3333-4444-555555555555')


def message(response):
    return (response.data.get('error') or {}).get('message', '') if isinstance(response.data, dict) else ''


class RuleTests(unittest.TestCase):
    def test_what_is_somebody_elses(self):
        for label, name, rule in TAKEN:
            with self.subTest(name=label):
                self.assertEqual(actor_names.collision(name, **HOST), rule)

    def test_what_is_nobodys(self):
        for name in FREE:
            with self.subTest(name=name):
                self.assertIsNone(actor_names.collision(name, **HOST))
        # A credential without a namespace writes under its issuer's own account id.
        for nothing in (None, '', 7, ['ops-lead']):
            self.assertIsNone(actor_names.collision(nothing, **HOST))

    def test_with_no_host_only_the_shape_and_the_services_namespace_are_left(self):
        self.assertEqual(actor_names.collision(SESSION_A), actor_names.SESSION_SHAPED)
        self.assertEqual(actor_names.collision('http/read'), actor_names.SERVICE)
        self.assertIsNone(actor_names.collision('ops-lead'))
        # The service may have been started with another namespace; both are its own.
        self.assertEqual(actor_names.collision('web/read', service=('web', 'http')), actor_names.SERVICE)
        self.assertEqual(actor_names.collision('http', service=('web', 'http')), actor_names.SERVICE)
        self.assertIsNone(actor_names.collision('http', service=()))

    def test_names_that_are_not_names_on_a_list_take_nothing(self):
        self.assertIsNone(actor_names.collision('worker', sessions=[None, 7, ''], operators=[None, ''], verifiers=None))

    def test_the_head_is_what_the_review_workflow_compares(self):
        import review_workflow
        for name in ('Team/Alice', 'team', 'TEAM/bob/x', 'ops-lead'):
            self.assertEqual(actor_names.head(name), review_workflow.author_key(name))

    def test_inside_a_namespace(self):
        for actor, namespace, inside in (('w', 'w', True), ('w/a', 'w', True), ('w/a/b', 'w/a', True), ('w/a', 'w/', True),
                                         ('wa', 'w', False), ('w', 'w/a', False), ('W', 'w', False), ('w', '', False),
                                         ('w', None, False), (None, 'w', False)):
            with self.subTest(actor=actor, namespace=namespace):
                self.assertIs(actor_names.inside(actor, namespace), inside)

    def test_the_sentence_fits_what_the_service_hands_on_and_names_no_other_name(self):
        longest = 'a' * 64
        for rule in (actor_names.SESSION, actor_names.SESSION_SHAPED, actor_names.OPERATOR, actor_names.VERIFIER, actor_names.SERVICE):
            said = actor_names.refusal(longest, rule)
            self.assertLessEqual(len(said), 200)
            self.assertTrue(said.endswith(actor_names.REVOKE))
        self.assertIn('that name', actor_names.refusal('x\ny', actor_names.OPERATOR))
        self.assertNotIn('\n', actor_names.refusal('x\ny', actor_names.OPERATOR))


class DenialTests(unittest.TestCase):
    """``descriptor_actor_denial``: the endpoint's refusal at use, on the descriptor alone."""

    def setUp(self):
        self.tmp = fixes.unique_dir('cred-actor-')
        self.addCleanup(fixes.shutil.rmtree, self.tmp, ignore_errors=True)
        state = fixes.authority_state(project='probe')
        state['credentials'] = {
            'cred_w': {'user_id': 'usr_a', 'project_id': 'probe', 'actor': 'worker-a', 'revoked': False},
            'cred_ops': {'user_id': 'usr_a', 'project_id': 'probe', 'actor': 'ops-lead', 'revoked': False},
            'cred_none': {'user_id': 'usr_a', 'project_id': 'probe', 'actor': None, 'revoked': False},
            'cred_agent': {'user_id': 'usr_a', 'agent_id': 'agent_0123456789abcdef', 'actor': 'worker-a', 'revoked': False},
        }
        (self.tmp / 'authority.json').write_text(json.dumps(state), encoding='utf-8')
        self.config = http_authority.AuthorityConfig(str(self.tmp / 'authority.json'))
        self.asked = 0

    def reserved(self):
        self.asked += 1
        return dict(HOST)

    def denial(self, actor, credential=None, config='live', authority='given'):
        descriptor = {'via': 'credential' if credential else 'session', 'user_id': 'usr_a', 'session_hash': 'sess_a',
                      'project': 'probe', 'capability': 'tasks.write'}
        if credential:
            descriptor['credential_id'] = credential
        request = {'project': 'probe', 'actor': actor, 'action': 'bd', 'args': ['create', 'x']}
        if authority == 'given':
            request['authority'] = descriptor
        return http_authority.descriptor_actor_denial(request, self.config if config == 'live' else None, self.reserved)

    def refused(self, answer, words):
        self.assertIsNotNone(answer)
        self.assertEqual((126, 403), (answer['returncode'], answer.get('authority_status')), answer)
        self.assertIn(words, answer['stderr'])

    def test_a_credential_writes_inside_a_namespace_that_is_nobodys(self):
        for actor in ('worker-a', 'worker-a/sub'):
            self.assertIsNone(self.denial(actor, 'cred_w'))
        self.assertEqual(self.asked, 2)

    def test_a_credential_under_a_taken_name_is_refused_with_what_to_do(self):
        for actor in ('ops-lead', 'ops-lead/night'):
            answer = self.denial(actor, 'cred_ops')
            self.refused(answer, 'A worker credential cannot write as ops-lead: that is a name on the operator list. '
                                 'Revoke it and issue one under another name.')
            self.assertNotIn('team/alice', answer['stderr'])
            self.assertNotIn('verity', answer['stderr'])

    def test_a_name_outside_the_credentials_namespace_is_refused_and_the_host_is_not_asked(self):
        for actor, credential in (('ops-lead', 'cred_w'), ('worker-ab', 'cred_w'), ('worker', 'cred_w'), ('Worker-a', 'cred_w'),
                                  (SESSION_A, 'cred_w'), ('worker-a', 'cred_none'), ('anything', 'cred_missing')):
            with self.subTest(actor=actor, credential=credential):
                self.refused(self.denial(actor, credential), 'descriptor names')
        self.assertEqual(self.asked, 0)

    def test_an_account_and_an_agent_write_under_their_own_id_only(self):
        # A signed-in account (no credential in the descriptor): the hole kittrial-5bb.181 closed in the routes.
        for actor in (SESSION_A, 'ops-lead', 'worker-a', 'nobody', 'http/read'):
            with self.subTest(actor=actor):
                self.refused(self.denial(actor), 'The actor is not the account or agent')
        self.refused(self.denial('worker-a', 'cred_agent'), 'The actor is not the account or agent')
        # A session descriptor that carries a credential id is still a session.
        request = {'project': 'probe', 'actor': 'worker-a', 'action': 'bd', 'args': [],
                   'authority': {'via': 'session', 'user_id': 'usr_a', 'session_hash': 'sess_a', 'credential_id': 'cred_w'}}
        self.refused(http_authority.descriptor_actor_denial(request, self.config, self.reserved), 'The actor is not the account')
        self.assertEqual(self.asked, 0)

    def test_what_is_not_judged_here(self):
        # The account itself; a web-shaped name (http_actor_denial judges it); the SSH path
        # (no configuration); a request without a descriptor (the service's own reads).
        self.assertIsNone(self.denial('usr_a'))
        self.assertIsNone(self.denial('usr_0123456789abcdef'))
        self.assertIsNone(self.denial('agent_0123456789abcdef/x', 'cred_agent'))
        self.assertIsNone(self.denial('ops-lead', 'cred_ops', config=None))
        self.assertIsNone(self.denial('http/read', authority=None))
        self.assertIsNone(self.denial(SESSION_A, authority=None))
        self.assertEqual(self.asked, 0)

    def test_a_state_that_cannot_be_read_refuses(self):
        (self.tmp / 'authority.json').write_text('not json', encoding='utf-8')
        answer = self.denial('worker-a', 'cred_w')
        self.assertEqual(126, answer['returncode'], answer)


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = fixes.unique_dir('cred-registry-')
        self.addCleanup(fixes.shutil.rmtree, self.tmp, ignore_errors=True)

    def test_the_registered_actors_are_read_and_a_damaged_registry_is_not_read_as_empty(self):
        self.assertEqual(sessions.registered_actors(self.tmp), [])
        record = {'request_id': '11111111-2222-3333-4444-555555555555', 'actor': SESSION_A, 'name': 'Coordinator',
                  'created_at': '2026-10-07T00:00:00+00:00'}
        file = self.tmp / '.sessions.json'
        file.write_text(json.dumps({'schema_version': 1, 'records': {record['request_id']: record}}), encoding='utf-8')
        before = file.stat().st_mtime_ns
        self.assertEqual(sessions.registered_actors(self.tmp), [SESSION_A])
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), ['.sessions.json'])
        self.assertEqual(file.stat().st_mtime_ns, before)
        for damaged in ('', 'not json', '[]', '{"schema_version": 1}', '{"schema_version": 2, "records": {}}', b'\xff\xfe'):
            with self.subTest(damaged=damaged):
                file.write_bytes(damaged if isinstance(damaged, bytes) else damaged.encode('utf-8'))
                with self.assertRaises(ValueError):
                    sessions.registered_actors(self.tmp)


class HostNames:
    """What the stub endpoint's host "has": written where the stub reads it."""

    def host(self, **names):
        (self.canonical_root / 'reserved-actors.json').write_text(json.dumps(dict(HOST, **names)), encoding='utf-8')

    def project(self):
        admin = self.admin_token()
        self.alex, self.pid = self.setup_project()
        self.casey_id = self.create_account(admin, 'casey', 'casey-password-1')
        self.request('PUT', '/v1/projects/%s/members/%s' % (self.pid, self.casey_id), {'role': 'contributor'}, token=self.alex)
        self.casey = self.login('casey', 'casey-password-1')[0]
        self.credentials = '/v1/projects/%s/worker-credentials' % self.pid
        self.tasks = '/v1/projects/%s/tasks' % self.pid

    def listed(self):
        answer = self.request('GET', self.credentials + '?limit=100', token=self.alex)
        self.assertEqual(200, answer.status, answer.data)
        return answer.data['items']


class IssueCase(HostNames, fixes.EndpointCase):
    def test_a_name_that_is_somebody_elses_is_refused_and_nothing_is_kept(self):
        self.project()
        self.host()
        for number, (label, name, rule) in enumerate(TAKEN):
            with self.subTest(name=label):
                key = 'issue-key-%04d' % number
                refused = self.request('POST', self.credentials, {'label': 'w', 'actor': name}, token=self.alex, key=key)
                self.assertEqual(422, refused.status, refused.data)
                self.assertEqual('A worker credential cannot write as %s: that is %s. Choose another name.' % (name, rule),
                                 message(refused))
                self.assertEqual({'actor': name, 'rule': rule}, refused.data['error']['detail'])
                self.assertNotIn('secret', json.dumps(refused.data))
                # Nothing was kept, the key included: the same key issues one under a free name.
                self.assertEqual([], [item for item in self.listed() if item['actor'] == name])
                issued = self.request('POST', self.credentials, {'label': 'w', 'actor': 'free-%d' % number}, token=self.alex, key=key)
                self.assertEqual(201, issued.status, issued.data)
        self.assertEqual(len(TAKEN), len(self.listed()))

    def test_the_refusal_names_the_rule_and_none_of_the_hosts_names(self):
        self.project()
        self.host()
        refused = self.request('POST', self.credentials, {'actor': 'team'}, token=self.alex)
        self.assertEqual(422, refused.status)
        for name in ('alice', 'ops-lead', 'verity', SESSION_A):
            self.assertNotIn(name, json.dumps(refused.data))

    def test_a_name_that_is_nobodys_is_issued_and_writes(self):
        self.project()
        self.host()
        for name in FREE:
            with self.subTest(name=name):
                issued = self.request('POST', self.credentials, {'label': 'w', 'actor': name}, token=self.alex)
                self.assertEqual(201, issued.status, issued.data)
                made = self.request('POST', self.tasks, {'title': 'by ' + name}, token=issued.data['credential']['secret'])
                self.assertEqual(201, made.status, made.data)
                self.assertEqual([name], [row.get('created_by') for row in self.canonical_rows() if row['title'] == 'by ' + name])
        self.assertEqual([None] * len(FREE), [item['actor_refused'] for item in self.listed()])

    def test_one_without_a_name_and_one_under_the_issuers_own_id_are_issued_and_the_host_is_not_asked_for_the_first(self):
        self.project()
        self.host()
        with mock.patch.object(self.backend, 'actor_standing', wraps=self.backend.actor_standing) as asked:
            issued = self.request('POST', self.credentials, {'label': 'w'}, token=self.alex)
            self.assertEqual(201, issued.status, issued.data)
            self.assertEqual(0, asked.call_count)
        own = self.request('GET', '/v1/sessions/current', token=self.alex).data['user']['id']
        self.assertEqual(201, self.request('POST', self.credentials, {'actor': own}, token=self.alex).status)
        self.assertEqual(422, self.request('POST', self.credentials, {'actor': self.casey_id}, token=self.alex).status)

    def test_only_who_may_issue_one_is_told_whether_a_name_is_taken(self):
        """A member who may not issue a credential is refused as that, whatever the name: no probing of the lists."""
        self.project()
        self.host()
        with mock.patch.object(self.backend, 'actor_standing', wraps=self.backend.actor_standing) as asked:
            for name in ('ops-lead', 'worker-a'):
                refused = self.request('POST', self.credentials, {'actor': name}, token=self.casey)
                self.assertEqual(403, refused.status, refused.data)
                self.assertNotIn('operator', json.dumps(refused.data))
            outsider = self.login_new('drew')
            for name in ('ops-lead', 'worker-a'):
                self.assertEqual(404, self.request('POST', self.credentials, {'actor': name}, token=outsider).status)
            self.assertEqual(0, asked.call_count)

    def login_new(self, name):
        self.create_account(self.admin_token(), name, name + '-password-1')
        return self.login(name, name + '-password-1')[0]

    def test_a_name_with_a_bad_shape_is_refused_as_before_and_the_host_is_not_asked(self):
        self.project()
        with mock.patch.object(self.backend, 'actor_standing', wraps=self.backend.actor_standing) as asked:
            for name in ('-x', 'a b', 'x' * 65, 7, ''):
                with self.subTest(name=name):
                    refused = self.request('POST', self.credentials, {'actor': name}, token=self.alex)
                    self.assertEqual(422, refused.status, refused.data)
                    self.assertIn('Invalid credential actor namespace', message(refused))
            self.assertEqual(0, asked.call_count)

    def test_when_the_host_cannot_say_nothing_is_issued(self):
        self.project()
        (self.canonical_root / 'reserved-actors.json').write_text('not json', encoding='utf-8')
        refused = self.request('POST', self.credentials, {'actor': 'worker-a'}, token=self.alex, key='cannot-say-1')
        self.assertGreaterEqual(refused.status, 400, refused.data)
        self.assertEqual([], self.listed_without_asking())
        self.host()
        self.assertEqual(201, self.request('POST', self.credentials, {'actor': 'worker-a'}, token=self.alex, key='cannot-say-1').status)

    def listed_without_asking(self):
        return [c for c in self.service.state['credentials'].values() if c.get('project_id') == self.pid]


class UseCase(HostNames, fixes.EndpointCase):
    """A credential that has such a name already: issued before the rule, or the name was listed afterwards."""

    def old_credential(self, name):
        self.host(sessions=[], operators=[], verifiers=[])
        issued = self.request('POST', self.credentials, {'label': 'old', 'actor': name}, token=self.alex)
        if issued.status != 201:
            # The service's own namespace is refused whatever the host has: plant the record as an old kit left it.
            issued = self.request('POST', self.credentials, {'label': 'old', 'actor': 'placeholder-name'}, token=self.alex)
            self.assertEqual(201, issued.status, issued.data)
            with self.service.store.lock:
                self.service.state['credentials'][issued.data['credential']['id']]['actor'] = name
                self.service.store.save()
        self.host()
        return issued.data['credential']

    def test_every_write_is_refused_and_nothing_is_written_while_reads_go_on(self):
        self.project()
        task = self.create_task(self.alex, self.pid, 'a task').data['id']
        path = self.tasks + '/' + task
        for number, (label, name, rule) in enumerate(TAKEN):
            with self.subTest(name=label):
                secret = self.old_credential(name)['secret']
                rows = json.dumps(self.canonical_rows(), sort_keys=True)
                said = 'A worker credential cannot write as %s: that is %s. Revoke it and issue one under another name.' % (name, rule)
                checkpoint = {'intent': 'i', 'acceptance': 'a', 'summary': 's', 'next_action': 'n', 'previous': None,
                              'activity_cursor': 'x'}
                contribute = dict(fixes.CONTRIBUTION, operation='contribute', schema_version=1, operation_id='op-%d' % number,
                                  previous=None)
                for what, method, where, body in (('create', 'POST', self.tasks, {'title': 'forged'}),
                                                  ('change', 'PATCH', path, {'title': 'forged'}),
                                                  ('claim', 'POST', path + '/claim', {}),
                                                  ('checkpoint', 'POST', path + '/checkpoints', checkpoint),
                                                  ('review', 'POST', path + '/reviews', contribute)):
                    with self.subTest(write=what):
                        refused = self.request(method, where, body, token=secret, key='use-%d-%s' % (number, what))
                        self.assertEqual(403, refused.status, (what, refused.data))
                        self.assertEqual(said, message(refused))
                self.assertEqual(rows, json.dumps(self.canonical_rows(), sort_keys=True))
                # It still reads; and its owner is told in the list.
                self.assertEqual(200, self.request('GET', self.tasks, token=secret).status)
                mine = [item for item in self.listed() if item['actor'] == name]
                self.assertEqual([said], [item['actor_refused'] for item in mine])
        self.assertNotIn('unknown', [e['outcome'] for e in self.request(
            'GET', '/v1/projects/%s/audit?limit=100' % self.pid, token=self.alex).data['items']])

    def test_a_label_under_a_taken_namespace_is_refused_too(self):
        self.project()
        secret = self.old_credential('ops-lead')['secret']
        refused = self.request('POST', self.tasks, {'title': 'forged', 'actor': 'ops-lead/night'}, token=secret)
        self.assertEqual(403, refused.status, refused.data)
        self.assertIn('that is a name on the operator list', message(refused))
        self.assertEqual([], [row for row in self.canonical_rows() if row['title'] == 'forged'])

    def test_a_credential_under_a_free_name_goes_on_writing_and_one_whose_name_is_listed_later_stops(self):
        self.project()
        self.host()
        issued = self.request('POST', self.credentials, {'actor': 'night-shift'}, token=self.alex).data['credential']
        self.assertEqual(201, self.request('POST', self.tasks, {'title': 'one'}, token=issued['secret']).status)
        self.host(operators=['ops-lead', 'night-shift'])                # `operators add night-shift`, afterwards
        refused = self.request('POST', self.tasks, {'title': 'two'}, token=issued['secret'])
        self.assertEqual(403, refused.status, refused.data)
        self.assertEqual(['one'], [row['title'] for row in self.canonical_rows()])
        self.host()
        self.assertEqual(201, self.request('POST', self.tasks, {'title': 'three'}, token=issued['secret']).status)

    def test_a_revoked_one_is_not_marked_and_a_listing_that_names_none_does_not_ask_the_host(self):
        self.project()
        credential = self.old_credential('ops-lead')
        self.assertEqual([True], [bool(item['actor_refused']) for item in self.listed()])
        revoked = self.request('POST', '%s/%s/revoke' % (self.credentials, credential['id']), {}, token=self.alex)
        self.assertIn(revoked.status, (200, 204), revoked.data)
        with mock.patch.object(self.backend, 'actor_standing', wraps=self.backend.actor_standing) as asked:
            self.assertEqual([None], [item['actor_refused'] for item in self.listed()])
            self.assertEqual(0, asked.call_count)

    def test_a_signed_in_account_and_an_agent_are_untouched(self):
        self.project()
        self.host()
        self.assertEqual(201, self.request('POST', self.tasks, {'title': 'by the owner'}, token=self.alex).status)
        self.assertEqual(201, self.request('POST', self.tasks, {'title': 'by a member'}, token=self.casey).status)
        made = self.request('POST', '/v1/agents', {'name': 'Kestrel', 'working_directory': '/home/casey/kestrel',
                                                  'projects': [self.pid]}, token=self.casey)
        self.assertEqual(201, made.status, made.data)
        agent = made.data['credential']['secret']
        self.assertEqual(201, self.request('POST', self.tasks, {'title': 'by an agent'}, token=agent).status)


class InProcessCase(fixes.Harness):
    """Without a host there are no lists: the shape of a session actor and the service's namespace are left."""

    def test_issue(self):
        admin = self.admin_token()
        self.create_account(admin, 'alex', 'alex-password-1')
        alex = self.login('alex', 'alex-password-1')[0]
        pid = self.create_project(alex, 'Alpha')
        path = '/v1/projects/%s/worker-credentials' % pid
        for name, rule in ((SESSION_A, actor_names.SESSION_SHAPED), ('http/read', actor_names.SERVICE)):
            refused = self.request('POST', path, {'actor': name}, token=alex)
            self.assertEqual(422, refused.status, refused.data)
            self.assertIn(rule, message(refused))
        for name in ('ops-lead', 'worker-a'):
            self.assertEqual(201, self.request('POST', path, {'actor': name}, token=alex).status)
        listed = self.request('GET', path, token=alex).data['items']
        self.assertEqual([None, None], [item['actor_refused'] for item in listed])


@unittest.skipUnless(os.name == 'posix', 'endpoint.py and admin.py import fcntl; POSIX only')
class RealEndpointCase(unittest.TestCase):
    """The endpoint itself, with its own registry and its own lists."""

    def setUp(self):
        import importlib.util
        self.tmp = fixes.unique_dir('cred-endpoint-')
        self.addCleanup(fixes.shutil.rmtree, self.tmp, ignore_errors=True)
        self.root = self.tmp / 'root'
        self.project = self.root / 'projects' / 'probe'
        (self.root / 'bin').mkdir(parents=True)
        (self.project / '.beads').mkdir(parents=True)
        (self.project / '.beads' / 'metadata.json').write_text('{}', encoding='utf-8')
        (self.root / 'deployment.private.json').write_text(json.dumps(
            {'password': 'probe', 'operators': ['ops-lead'], 'verifiers': HOST['verifiers']}), encoding='utf-8')
        record = {'request_id': '11111111-2222-3333-4444-555555555555', 'actor': SESSION_A, 'name': 'Coordinator',
                  'created_at': '2026-10-07T00:00:00+00:00'}
        (self.project / '.sessions.json').write_text(json.dumps(
            {'schema_version': 1, 'records': {record['request_id']: record}}), encoding='utf-8')
        self.marker = self.tmp / 'bd-ran'
        bd = self.root / 'bin' / 'bd'
        bd.write_text('#!/bin/sh\necho ran >> %s\necho \'{"id":"probe-1","title":"x"}\'\nexit 0\n' % self.marker, encoding='utf-8')
        bd.chmod(0o755)
        spec = importlib.util.spec_from_file_location('real_endpoint_184', str(KIT / 'endpoint.py'))
        self.endpoint = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.endpoint)
        self.state = fixes.authority_state(project='probe')
        self.config_path = self.tmp / 'authority.json'
        self.config = http_authority.AuthorityConfig(str(self.config_path))

    def credential(self, name):
        self.state['credentials'] = {'cred_x': {
            'id': 'cred_x', 'user_id': 'usr_a', 'project_id': 'probe', 'actor': name, 'revoked': False,
            'scopes': ['tasks', 'checkpoints', 'reviews', 'feedback'], 'expires_at': time.time() + 3600}}
        self.config_path.write_text(json.dumps(self.state), encoding='utf-8')
        return {'via': 'credential', 'user_id': 'usr_a', 'credential_id': 'cred_x', 'project': 'probe',
                'capability': 'tasks.write', 'now': time.time() + 5}

    def write(self, actor, descriptor, number):
        request = {'project': 'probe', 'actor': actor, 'action': 'bd', 'args': ['create', 'x'],
                   'operation_id': 'op-184-%s' % number, 'authority': descriptor}
        return self.endpoint.execute(self.root, request, authority_config=self.config, require_authority=True)

    def test_the_host_says_which_names_are_taken_and_never_which_names_it_has(self):
        names = [name for _, name, _ in REAL_TAKEN] + list(FREE)
        answer = self.endpoint.execute(self.root, {'project': 'probe', 'actor': 'http/read', 'action': 'actor-standing',
                                                   'args': names})
        self.assertEqual(0, answer['returncode'], answer)
        found = json.loads(answer['stdout'])['names']
        self.assertEqual({name: rule for _, name, rule in REAL_TAKEN}, {name: found[name] for _, name, _ in REAL_TAKEN})
        self.assertEqual([None] * len(FREE), [found[name] for name in FREE])
        # Only the names that were asked about are in the answer, as often as they were asked.
        for mine in ('ops-lead', 'verity', SESSION_A):
            self.assertEqual(answer['stdout'].count(mine), sum(name.count(mine) for name in names))
        self.assertFalse(self.marker.exists(), 'the tracker was asked')
        for bad in ([], ['x'] * 201, ['x', 7], [''], ['x' * 97]):
            with self.subTest(bad=str(bad)[:30]), self.assertRaises(ValueError):
                self.endpoint.execute(self.root, {'project': 'probe', 'actor': 'http/read', 'action': 'actor-standing', 'args': bad})

    def test_a_write_under_a_taken_name_is_refused_before_anything_runs(self):
        for number, (label, name, rule) in enumerate(REAL_TAKEN):
            with self.subTest(name=label):
                refused = self.write(name, self.credential(name), number)
                self.assertEqual((126, 403), (refused['returncode'], refused.get('authority_status')), refused)
                self.assertEqual('A worker credential cannot write as %s: that is %s. Revoke it and issue one under another name.\n'
                                 % (name, rule), refused['stderr'])
        self.assertFalse(self.marker.exists(), 'the native command ran')
        self.assertFalse((self.project / '.http-operations.sqlite3').exists(), 'an operation identity was reserved')

    def test_a_write_under_a_free_name_runs_and_one_outside_the_namespace_does_not(self):
        done = self.write('worker-a/sub', self.credential('worker-a'), 'free')
        self.assertEqual(0, done['returncode'], done)
        self.assertTrue(self.marker.exists())
        self.marker.unlink()
        for number, actor in enumerate(('worker-b', 'ops-lead', SESSION_A, 'worker')):
            refused = self.write(actor, self.credential('worker-a'), 'out-%d' % number)
            self.assertEqual((126, 403), (refused['returncode'], refused.get('authority_status')), refused)
        refused = self.write('ops-lead', fixes.authority_descriptor('usr_a', 'sess_a', project='probe'), 'session')
        self.assertEqual((126, 403), (refused['returncode'], refused.get('authority_status')), refused)
        self.assertFalse(self.marker.exists(), 'the native command ran')

    def test_the_ssh_path_is_as_it_was(self):
        """No launch configuration: the coordinator itself, and anybody, writes under the name it gives."""
        for actor in (SESSION_A, 'ops-lead', 'worker-a'):
            done = self.endpoint.execute(self.root, {'project': 'probe', 'actor': actor, 'action': 'bd', 'args': ['create', 'x']})
            self.assertEqual(0, done['returncode'], done)

    def test_a_registry_that_cannot_be_read_refuses_the_credential_and_not_the_account(self):
        (self.project / '.sessions.json').write_text('not json', encoding='utf-8')
        with self.assertRaises(ValueError):
            self.write('worker-a', self.credential('worker-a'), 'damaged')
        self.assertFalse(self.marker.exists())

    def test_the_host_command_lists_them_and_changes_nothing(self):
        import admin
        state = {'users': {'usr_a': {'id': 'usr_a', 'username': 'alex'}}, 'credentials': {
            'cred_1': {'user_id': 'usr_a', 'project_id': 'probe', 'label': 'one', 'actor': 'ops-lead/night', 'revoked': False,
                       'created_at': '2026-10-01T00:00:00Z', 'last_used': 1791000000.0, 'expires_at': 1793000000.0},
            'cred_2': {'user_id': 'usr_a', 'project_id': 'probe', 'label': 'two', 'actor': 'worker-a', 'revoked': False},
            'cred_3': {'user_id': 'usr_a', 'project_id': 'probe', 'label': 'three', 'actor': SESSION_A, 'revoked': True},
            'cred_4': {'user_id': 'usr_a', 'project_id': 'probe', 'label': 'no name', 'actor': None, 'revoked': False},
            'cred_5': {'user_id': 'usr_a', 'agent_id': 'agent_0123456789abcdef', 'actor': 'ops-lead', 'revoked': False},
            'cred_6': {'user_id': 'usr_gone', 'project_id': 'elsewhere', 'label': 'six', 'actor': 'verity', 'revoked': False}}}
        path = self.tmp / 'state.json'
        path.write_text(json.dumps(state), encoding='utf-8')
        rows = [{'id': 'probe-1', 'assignee': 'Worker-A/sub', 'created_by': 'usr_a', 'comments': [{'author': 'ops-lead'}]}]
        before = {p: p.stat().st_mtime_ns for p in list(self.root.rglob('*')) + [path] if p.is_file()}
        with mock.patch.object(admin, 'run_bd', return_value='\n'.join(json.dumps(row) for row in rows)):
            report = admin.credential_actors(self.root, path)
        self.assertEqual(before, {p: p.stat().st_mtime_ns for p in list(self.root.rglob('*')) + [path] if p.is_file()})
        found = {item['credential']: item for item in report['credentials']}
        self.assertEqual(sorted(found), ['cred_1', 'cred_2', 'cred_3', 'cred_6'])
        self.assertEqual((4, 2), (report['worker_credentials_with_a_name'], report['colliding_and_not_revoked']))
        self.assertEqual((actor_names.OPERATOR, True, True, 'alex', '2026-10-03T04:00:00Z'),
                         tuple(found['cred_1'][key] for key in ('collides', 'refused_when_it_writes', 'tracker_rows',
                                                                'issued_by_username', 'last_used')))
        self.assertEqual((None, False, True), tuple(found['cred_2'][key] for key in ('collides', 'refused_when_it_writes', 'tracker_rows')))
        self.assertEqual((actor_names.SESSION, True), (found['cred_3']['collides'], found['cred_3']['revoked']))
        # A project that is not on this host: the lists still apply; its tracker cannot be read, and that is said.
        self.assertEqual((actor_names.VERIFIER, False, None, None),
                         tuple(found['cred_6'][key] for key in ('collides', 'project_on_host', 'tracker_rows', 'issued_by_username')))
        for missing in (self.tmp / 'nothing.json', self.root):
            with self.assertRaises(ValueError):
                admin.credential_actors(self.root, missing)
        path.write_text('[]', encoding='utf-8')
        with self.assertRaises(ValueError):
            admin.credential_actors(self.root, path)

    def test_the_command_line_prints_the_list_and_says_how_many(self):
        import admin
        path = self.tmp / 'state.json'
        path.write_text(json.dumps({'users': {}, 'credentials': {
            'cred_1': {'user_id': 'usr_a', 'project_id': 'probe', 'label': 'one', 'actor': 'verity', 'revoked': False}}}), encoding='utf-8')
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(admin, 'run_bd', return_value=''), mock.patch.object(
                sys, 'argv', ['admin.py', '--root', str(self.root), 'credential-actors', '--state', str(path)]), \
                redirect_stdout(out), redirect_stderr(err):
            admin.main()
        self.assertEqual(1, json.loads(out.getvalue())['colliding_and_not_revoked'])
        self.assertIn('1 worker credential(s) write under a name', err.getvalue())
        self.assertIn('Nothing was changed', err.getvalue())


if __name__ == '__main__':
    unittest.main()
