"""kittrial-5bb.515, release B (writers): the web service records an approval's basis as a field.

B is integrated only after A (tests/test_approval_basis.py) has been released and every reader the owner runs
has it; its one gate is the endpoint advertising ``approval_basis`` for ``approve`` in its machine-readable
``review`` help. Through the real routes of the web service (the in-process backend), the endpoint backend's
reading of the advertisement, and the real endpoint with a real bd (skipped where there is none).
"""
import json
import sys
import types
import unittest
import unittest.mock
from pathlib import Path

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT))
sys.path.insert(0, str(KIT / 'tests'))
import http_service
import review_workflow
import test_agent_approval as base
import test_coordinator_grant_real as real
import test_bd_label_aliases as rb
import test_http_agents
import work

API = http_service.ApiHandler
SENTENCE = API.SAME_PERSON_APPROVAL


def without_advertisement(help_payload=work.help_payload):
    """``work.help_payload`` as a kit before the readers' release serves it: no ``optional_fields``."""
    def served(action='work'):
        answer = dict(help_payload(action))
        answer.pop('optional_fields', None)
        return answer
    return served


class Gate(base.SamePerson):
    ADVERTISED = True

    def stored(self, task):
        return [r for r in self.backend.state['contributions'][task] if r['kind'] == 'approve'][-1]

    def test_an_advertising_endpoint_gets_the_field_and_no_sentence(self):
        task, contribution = self.deliver(self.other)
        self.assertEqual(201, self.approve(self.agent, task, contribution).status)
        record = self.stored(task)
        self.assertEqual('same-person-agent', record['approval_basis'])
        self.assertEqual('accepted', record['summary'])                          # the approver's own words only
        self.assertNotIn(SENTENCE, record['summary'])
        # The audit entry still holds the setting, who set it and when.
        event = [e for e in self.events('reviews.add') if e.get('same_person_coordinator')][-1]
        self.assertEqual((event['agent_approval'], event['agent_approval_set_by']), ('by-agent', 'default'))
        # And the task page's read carries it as its own block.
        shown = self.request('GET', self.base(task) + '/brief', token=self.tokens['olive']).data['review']
        self.assertEqual('same-person-agent', shown['approval_basis']['basis'])
        self.assertEqual(review_workflow.APPROVAL_BASES['same-person-agent'], shown['approval_basis']['text'])

    def test_an_endpoint_that_does_not_advertise_it_gets_the_sentence_and_no_field(self):
        task, contribution = self.deliver(self.other)
        with unittest.mock.patch.object(work, 'help_payload', without_advertisement()):
            self.assertEqual(201, self.approve(self.agent, task, contribution).status)
        record = self.stored(task)
        self.assertNotIn('approval_basis', record)
        self.assertEqual('accepted ' + SENTENCE, record['summary'])
        shown = self.request('GET', self.base(task) + '/brief', token=self.tokens['olive']).data['review']
        self.assertNotIn('approval_basis', shown)

    def credential_delivery(self):
        self.assertEqual(200, self.member('cora', 'owner', token=self.admin).status)
        worker, _ = self.worker_of('cora', 'lane-c')
        return self.deliver(worker)

    def test_a_worker_credential_approval_gets_its_own_value_and_no_sentence(self):
        # kittrial-5bb.519's basis: the value the route's basis names, with its own label on the brief.
        task, contribution = self.credential_delivery()
        self.assertEqual(201, self.approve(self.agent, task, contribution).status)
        record = self.stored(task)
        self.assertEqual('same-person-worker-credential', record['approval_basis'])
        self.assertEqual('accepted', record['summary'])
        self.assertNotIn(API.WORKER_CREDENTIAL_APPROVAL, record['summary'])
        event = [e for e in self.events('reviews.add') if e.get('same_person_coordinator')][-1]
        self.assertEqual(event['basis'], 'worker-credential')
        shown = self.request('GET', self.base(task) + '/brief', token=self.tokens['olive']).data['review']
        self.assertEqual(('same-person-worker-credential',
                          review_workflow.APPROVAL_BASES['same-person-worker-credential']),
                         (shown['approval_basis']['basis'], shown['approval_basis']['text']))

    def test_a_worker_credential_approval_without_the_advertisement_gets_its_sentence(self):
        task, contribution = self.credential_delivery()
        with unittest.mock.patch.object(work, 'help_payload', without_advertisement()):
            self.assertEqual(201, self.approve(self.agent, task, contribution).status)
        record = self.stored(task)
        self.assertNotIn('approval_basis', record)
        self.assertEqual('accepted ' + API.WORKER_CREDENTIAL_APPROVAL, record['summary'])

    def test_an_agent_delivers_then_the_task_goes_to_a_worker_credential_and_the_field_names_the_agent(self):
        # kittrial-5bb.515 r2 (S1): the delivering author decides the basis whenever it is of the approver's party.
        self.assertEqual(200, self.member('cora', 'owner', token=self.admin).status)
        self.worker_of('cora', 'lane-c')
        task, contribution = self.deliver(self.other)
        self.backend._task(self.project, task)['assignee'] = 'lane-c'
        self.assertEqual(201, self.approve(self.agent, task, contribution).status)
        self.assertEqual('same-person-agent', self.stored(task)['approval_basis'])
        event = [e for e in self.events('reviews.add') if e.get('same_person_coordinator')][-1]
        self.assertEqual(('agent', self.other_id, 'lane-c'), (event['basis'], event['author'], event['assignee']))

    def test_a_worker_credential_delivers_then_the_task_goes_to_an_agent_and_the_field_stays_the_credentials(self):
        task, contribution = self.credential_delivery()
        self.backend._task(self.project, task)['assignee'] = self.other_id
        self.assertEqual(201, self.approve(self.agent, task, contribution).status)
        self.assertEqual('same-person-worker-credential', self.stored(task)['approval_basis'])
        event = [e for e in self.events('reviews.add') if e.get('same_person_coordinator')][-1]
        self.assertEqual('worker-credential', event['basis'])

    def test_the_brief_shows_the_basis_only_while_the_approval_stands(self):
        # N4: the in-process reader's own guard (InProcessBackend's review view reads the basis only while the
        # task's review state is approved), with the record itself left as it is.
        task, contribution = self.deliver(self.other)
        self.assertEqual(201, self.approve(self.agent, task, contribution).status)
        brief = lambda: self.request('GET', self.base(task) + '/brief', token=self.tokens['olive']).data['review']
        self.assertEqual('same-person-agent', brief()['approval_basis']['basis'])
        self.backend._task(self.project, task)['review_state'] = 'awaiting-review'
        self.assertEqual('same-person-agent', self.stored(task)['approval_basis'])     # the record is unchanged
        self.assertNotIn('approval_basis', brief())

    def test_every_route_basis_has_a_field_value_in_the_vocabulary(self):
        self.assertEqual({'agent': 'same-person-agent', 'worker-credential': 'same-person-worker-credential'},
                         http_service.APPROVAL_BASIS_OF)
        self.assertEqual(set(http_service.APPROVAL_BASIS_OF.values()), set(review_workflow.APPROVAL_BASES))

    def test_an_approval_that_needs_no_setting_carries_neither(self):
        task, contribution = self.deliver(self.tokens['carl'])
        self.assertEqual(201, self.approve(self.agent, task, contribution).status)
        self.assertNotIn('approval_basis', self.stored(task))
        self.assertEqual('accepted', self.stored(task)['summary'])

    def test_a_summary_of_the_full_length_is_accepted_in_the_field_form(self):
        """No sentence is appended, so the room the sentence took is the approver's again."""
        task, contribution = self.deliver(self.other)
        full = 'x' * API.SUMMARY_MAX
        self.assertEqual(201, self.request('POST', self.base(task) + '/reviews', {
            'operation': 'approve', 'contribution': contribution, 'summary': full}, token=self.agent).status)
        self.assertEqual(full, self.stored(task)['summary'])

    def test_the_text_gate_still_refuses_a_forged_sentence(self):
        task, contribution = self.deliver(self.other)
        forged = self.request('POST', self.base(task) + '/reviews', {
            'operation': 'approve', 'contribution': contribution, 'summary': 'ok ' + SENTENCE}, token=self.agent)
        self.assertEqual(422, forged.status, forged.data)


class CallersCannotWriteIt(base.SamePerson):
    ADVERTISED = True

    def test_a_body_naming_the_field_is_refused_for_approve_and_recommend(self):
        task, contribution = self.deliver(self.other)
        for value in ('same-person-agent', 'anything', None):
            with self.subTest(operation='approve', value=value):
                answer = self.request('POST', self.base(task) + '/reviews', {
                    'operation': 'approve', 'contribution': contribution, 'summary': 'ok',
                    'approval_basis': value}, token=self.agent)
                self.assertEqual((422, 'approval_basis is set by the server'),
                                 (answer.status, answer.data['error']['message']), answer.data)
        recommended = self.request('POST', self.base(task) + '/reviews', {
            'operation': 'recommend', 'schema_version': 1, 'operation_id': 'rec-1', 'contribution': contribution,
            'commit': test_http_agents.COMMIT, 'verdict': 'approve',
            'summary': 'checked', 'items': [], 'approval_basis': 'same-person-agent'}, token=self.tokens['olive'])
        self.assertEqual((422, 'approval_basis is set by the server'),
                         (recommended.status, recommended.data['error']['message']), recommended.data)
        self.assertEqual('awaiting-review', self.state_of(task))                  # nothing was written


    def test_the_field_is_not_one_a_caller_may_send(self):
        # S2: approval_basis is not in the caller-allowed set; the route hands its value over under a key no
        # JSON body can spell.
        self.assertNotIn('approval_basis', http_service.EndpointBackend.REVIEW_OPTIONAL_FIELDS.get('approve', ()))
        self.assertIsInstance(http_service.SERVER_APPROVAL_BASIS, tuple)

    def test_without_the_dedicated_check_a_body_naming_the_field_is_refused_as_unknown_and_writes_nothing(self):
        # S2: the reviewer's mutant M3 (the dedicated 422 removed); the second barrier still refuses it.
        task, contribution = self.deliver(self.other)
        with unittest.mock.patch.object(API, '_refuse_caller_approval_basis', staticmethod(lambda payload: None)):
            answer = self.request('POST', self.base(task) + '/reviews', {
                'operation': 'approve', 'contribution': contribution, 'summary': 'ok',
                'approval_basis': 'same-person-agent'}, token=self.agent)
        self.assertEqual(422, answer.status, answer.data)
        self.assertTrue(answer.data['error']['message'].startswith('Unsupported review payload field'), answer.data)
        self.assertEqual('awaiting-review', self.state_of(task))
        self.assertEqual([], [r for r in self.backend.state['contributions'][task] if r['kind'] == 'approve'])


class BackendsWriteOnlyTheRoutesKey(base.SamePerson):
    """S2's third barrier: below the route, each backend writes only the value under SERVER_APPROVAL_BASIS, never a
    string key ``approval_basis`` (which the route refuses twice before any backend could see it)."""

    def principal(self):
        return types.SimpleNamespace(actor=self.agent_id, user_id=self.ids['cora'], agent_id=self.agent_id,
                                     via='credential')

    def approve_payload(self, task, contribution, **extra):
        return dict({'task_id': task, 'operation': 'approve', 'contribution': contribution, 'summary': 'ok'}, **extra)

    def test_the_in_process_backend_ignores_a_string_key_and_writes_the_routes(self):
        task, contribution = self.deliver(self.other)
        self.backend._review_add(self.principal(), self.project,
                                 self.approve_payload(task, contribution, approval_basis='same-person-agent'))
        record = [r for r in self.backend.state['contributions'][task] if r['kind'] == 'approve'][-1]
        self.assertNotIn('approval_basis', record)
        task, contribution = self.deliver(self.other)
        payload = self.approve_payload(task, contribution)
        payload[http_service.SERVER_APPROVAL_BASIS] = 'same-person-agent'
        self.backend._review_add(self.principal(), self.project, payload)
        record = [r for r in self.backend.state['contributions'][task] if r['kind'] == 'approve'][-1]
        self.assertEqual('same-person-agent', record['approval_basis'])

    def test_the_endpoint_command_carries_only_the_routes_key(self):
        backend = object.__new__(http_service.EndpointBackend)
        backend.actor_namespace = 'http'
        payload = {'task_id': 'pp-1', 'operation': 'approve', 'schema_version': 1, 'operation_id': 'op-1',
                   'previous': '1', 'contribution': '1', 'summary': 'ok'}

        def sent(extra):
            action, _, _, attachments = backend._command('reviews.add', self.principal(), 'pp', {**payload, **extra},
                                                         'op-1')
            self.assertEqual('review', action)
            return json.loads(attachments['0']['text'])
        self.assertNotIn('approval_basis', sent({'approval_basis': 'same-person-agent'}))
        self.assertEqual('same-person-agent',
                         sent({http_service.SERVER_APPROVAL_BASIS: 'same-person-agent'})['approval_basis'])


class EndpointBackendReadsTheAdvertisement(unittest.TestCase):
    def backend(self, reply):
        backend = object.__new__(http_service.EndpointBackend)
        backend.actor_namespace = 'http'
        backend._endpoint = lambda action, project, actor, args, *rest, **more: reply(action, args)
        return backend

    @staticmethod
    def answer(payload):
        return {'returncode': 0, 'stdout': json.dumps(payload) + '\n', 'stderr': ''}

    def test_only_a_help_that_lists_it_for_approve_counts(self):
        listed = work.help_payload('review')
        for name, payload, expected in (
                ('listed', listed, True),
                ('a kit before the field', without_advertisement()('review'), False),
                ('listed for another operation', dict(listed, optional_fields={'contribute': ['approval_basis']}), False),
                ('not a list', dict(listed, optional_fields={'approve': 'approval_basis'}), False),
                ('not an object', dict(listed, optional_fields=['approve']), False)):
            with self.subTest(name):
                seen = []
                backend = self.backend(lambda action, args, payload=payload: seen.append((action, args)) or self.answer(payload))
                self.assertIs(expected, backend.approval_basis_advertised('pp'))
                self.assertEqual([('review', ['--help'])], seen)                   # a read, nothing written

    def test_an_endpoint_that_cannot_be_read_does_not_advertise(self):
        backend = self.backend(lambda action, args: {'returncode': 2, 'stdout': '', 'stderr': 'boom'})
        self.assertFalse(backend.approval_basis_advertised('pp'))

    def test_any_exception_from_the_help_read_does_not_advertise(self):
        # N3: not only an HttpError: an OSError (or any other failure) of the help read falls back to the sentence.
        for error in (OSError('no such endpoint'), RuntimeError('boom'), ValueError('bad')):
            with self.subTest(error=type(error).__name__):
                def raises(action, args, error=error):
                    raise error
                self.assertFalse(self.backend(raises).approval_basis_advertised('pp'))


def only_its_own_tests(cls):
    """Borrows the real stack of tests/test_coordinator_grant_real.py and runs none of that file's tests again."""
    for name in dir(real.RealStackTests):
        if name.startswith('test_') and name not in cls.__dict__:
            setattr(cls, name, None)
    return cls


@unittest.skipIf(rb.endpoint is None, 'endpoint imports fcntl (POSIX-only)')
@unittest.skipIf(rb.BD is None, 'no real bd binary (set ORCHESTRA_BD_BIN or put bd on PATH)')
@only_its_own_tests
class RealWriterTests(real.RealStackTests):
    """The writer path through EndpointBackend, the real endpoint.py and a real bd."""

    def egret_task(self):
        self.assertEqual(200, self.grant().status)
        made = self.request('POST', '/v1/agents', {'name': 'Egret', 'working_directory': '/home/x/egret', 'projects': ['pp']},
                            token=self.tokens['cora'])
        self.assertEqual(201, made.status, made.data)
        self.tokens['egret'] = made.data['credential']['secret']
        return self.deliver('egret')

    def written(self, task, before):
        found = self.comments(task)[len(before):]
        self.assertEqual(1, len(found), found)
        return json.loads(found[0][1].split('\n', 1)[1])

    def test_the_endpoint_advertises_and_bd_holds_the_field_not_the_sentence(self):
        task, contribution = self.egret_task()
        self.assertTrue(self.backend.approval_basis_advertised('pp'))
        before = self.comments(task)
        approved = self.approve(self.agent, task, contribution)
        self.assertEqual(201, approved.status, approved.data)
        record = self.written(task, before)
        self.assertEqual('same-person-agent', record['approval_basis'])
        self.assertEqual('Accepted', record['summary'])
        shown = self.review(task)
        self.assertEqual('awaiting-integration', shown['state'])
        self.assertEqual('same-person-agent', shown['approval_basis']['basis'])

    def test_an_endpoint_not_advertising_gets_the_sentence_and_bd_holds_no_field(self):
        task, contribution = self.egret_task()
        real_endpoint = self.backend._endpoint

        def old_endpoint(action, project, actor, args, *rest, **more):
            reply = real_endpoint(action, project, actor, args, *rest, **more)
            if action == 'review' and args == ['--help']:
                payload = json.loads(reply['stdout'])
                payload.pop('optional_fields', None)
                reply = dict(reply, stdout=json.dumps(payload) + '\n')
            return reply
        before = self.comments(task)
        with unittest.mock.patch.object(self.backend, '_endpoint', old_endpoint):
            approved = self.approve(self.agent, task, contribution)
        self.assertEqual(201, approved.status, approved.data)
        record = self.written(task, before)
        self.assertNotIn('approval_basis', record)
        self.assertEqual('Accepted ' + SENTENCE, record['summary'])
        self.assertNotIn('approval_basis', self.review(task))

    def test_an_agent_delivers_then_reassigned_to_a_worker_credential_and_bd_holds_same_person_agent(self):
        # S1 through the real endpoint and bd: the task is reassigned in bd itself, as an operator would.
        task, contribution = self.egret_task()
        self.assertEqual(200, self.request('PUT', '/v1/projects/pp/members/%s' % self.ids['cora'], {'role': 'owner'},
                                           token=self.admin_token()).status)
        made = self.request('POST', '/v1/projects/pp/worker-credentials', {'label': 'worker', 'actor': 'lane-c'},
                            token=self.tokens['cora'])
        self.assertEqual(201, made.status, made.data)
        self.bd('update', task, '--assignee', 'lane-c')
        before = self.comments(task)
        approved = self.approve(self.agent, task, contribution)
        self.assertEqual(201, approved.status, approved.data)
        record = self.written(task, before)
        self.assertEqual('same-person-agent', record['approval_basis'])
        self.assertEqual('same-person-agent', self.review(task)['approval_basis']['basis'])

    def test_without_the_dedicated_check_a_caller_naming_the_field_writes_nothing(self):
        # S2 through the real endpoint and bd, with the dedicated 422 removed (the reviewer's M3).
        task, contribution = self.egret_task()
        before = self.comments(task)
        with unittest.mock.patch.object(API, '_refuse_caller_approval_basis', staticmethod(lambda payload: None)):
            refused = self.request('POST', '%s/%s/reviews' % (self.tasks, task), dict(
                operation='approve', schema_version=1, operation_id='op-forged-2', previous=contribution,
                contribution=contribution, summary='Accepted', approval_basis='same-person-agent'), token=self.agent)
        self.assertEqual(422, refused.status, refused.data)
        self.assertEqual(before, self.comments(task))
        self.assertEqual('awaiting-review', self.review(task)['state'])

    def test_a_caller_naming_the_field_writes_nothing(self):
        task, contribution = self.egret_task()
        before = self.comments(task)
        refused = self.request('POST', '%s/%s/reviews' % (self.tasks, task), dict(
            operation='approve', schema_version=1, operation_id='op-forged', previous=contribution,
            contribution=contribution, summary='Accepted', approval_basis='same-person-agent'), token=self.agent)
        self.assertEqual(422, refused.status, refused.data)
        self.assertEqual(before, self.comments(task))


if __name__ == '__main__':
    unittest.main()
