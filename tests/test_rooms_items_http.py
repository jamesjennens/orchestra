"""Room items and acceptance over HTTP: routes, R7, records, states and the page (.388)."""
import http.client
import json
import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import http_service
import rooms
import test_http_agents as agents
from test_http_agents import unique_dir


class ItemsBackend(http_service.InProcessBackend):
    """The service-local backend plus the canonical records rooms slice 2 creates.

    The real records and the native decision are made by endpoint.py's canonical actions;
    this stub records the same calls and answers the same identities, so the HTTP route's
    authority, state machine and idempotency are exercised without a tracker.
    """

    PROPOSALS = True
    RECORDS = True

    def __init__(self, service):
        super().__init__(service)
        self.requirements = {}
        self.requirement_by_key = {}
        self.decisions = []
        self.decision_by_key = {}
        self.proposals = {}
        self.proposal_by_key = {}
        self.calls = []
        #: Round 5: what the endpoint's operation journal would hold, keyed like the real one
        #: by each PERSON's own operation (user, route, key, target) or a decision's own id.
        #: A test deletes an entry to stand for a journal row that expired.
        self.journal = {}
        self.jobs = [{'id': 'job_0001', 'title': 'The project job'}]
        #: Set by a test to make the next native write fail BEFORE it writes anything.
        self.fail_native = None
        #: Set by the concurrency test: both accept paths meet here, so a route that did
        #: not serialize the decision would let both create a record.
        self.accept_barrier = None
        #: Set by the archive-race test: the accept's native record write is inside the
        #: item-decision lock, so the test can hold it there while an archive arrives.
        self.record_entered = None
        self.record_release = None

    def rendezvous(self):
        barrier = self.accept_barrier
        if barrier is not None:
            try:
                barrier.wait(timeout=2)
            except threading.BrokenBarrierError:
                pass

    def record_gate(self):
        if self.record_entered is not None:
            self.record_entered.set()
        if self.record_release is not None:
            self.record_release.wait(timeout=10)

    def requirements_read(self, project_id, args):
        if args[:1] == ['get']:
            record = self.requirements.get(args[1])
            if record is None:
                raise ValueError('Requirement was not found')
            acceptance = None
            if record.get('decision_id'):
                acceptance = {'decision': {'decision_id': record['decision_id']}}
            return {'id': record['id'], 'acceptance': acceptance,
                    'current': {'title': record['title'], 'description': record['description']}}
        return {'items': list(self.requirements.values()), 'decisions': list(self.decisions),
                'jobs': [dict(job) for job in self.jobs]}

    def native_failure(self, what):
        if self.fail_native == what:
            self.fail_native = None
            raise http_service.HttpError(503, 'unavailable', 'injected native failure: nothing written')

    def prior_room_write(self, project_id, user_id, route=None, key=None, target=None,
                         operation_id=None, journal_route=None, session_bound=False):
        entry = self.journal.get(operation_id if operation_id is not None
                                 else (user_id, route, key, target))
        if entry is None:
            return ('uncertain', None)
        # A three-part entry names who wrote the row (round 6, s2): a row written by someone
        # else under this person's id is foreign, as EndpointBackend.prior_room_write says.
        if len(entry) == 3 and entry[2] != user_id:
            return ('foreign', None)
        return entry[:2]

    def owner_requirements(self, principal, project_id, action, fields, key, task=None):
        self.calls.append(('owner_requirements', action, key, task))
        self.native_failure('requirements.' + action)
        if action == 'create':
            self.rendezvous()
            self.record_gate()
            # The real endpoint keys a write by the PERSON's own operation id, so another
            # person's retry with the same key is a different write (round 5, review N1).
            known = self.requirement_by_key.get((principal.user_id, key))
            if known is not None:
                return dict(self.requirements[known])
            record = {'id': 'task_requirement_%d' % (len(self.requirements) + 1),
                      'revision': 1, 'sha256': 'a' * 64, 'title': fields['title'],
                      'description': fields['description'], 'decision_id': None,
                      'parent': fields.get('parent')}
            self.requirements[record['id']] = record
            self.requirement_by_key[(principal.user_id, key)] = record['id']
            self.journal[(principal.user_id, 'requirements.create', key, None)] = (
                'committed', dict(record))
            return dict(record)
        record = self.requirements.get(task)
        if record is None:
            raise ValueError('Requirement was not found')
        if record.get('decision_id') is None:
            record['decision_id'] = 'task_decision_%d' % (len(self.decisions) + 1)
            self.decisions.append(record['decision_id'])
            record['decision_pointer'] = fields.get('pointer')
        record['revision'] = 2
        record['sha256'] = 'b' * 64
        self.journal[(principal.user_id, 'requirements.accept', key, task)] = ('committed', dict(record))
        return dict(record)

    def room_decision(self, principal, project_id, fields, key=None):
        self.calls.append(('room_decision', fields['operation_id'], fields['evidence']))
        self.native_failure('room_decision')
        self.rendezvous()
        known = self.decision_by_key.get(fields['operation_id'])
        if known is not None:
            return {'decision_id': known, 'evidence': fields['evidence']}
        decision_id = 'task_room_decision_%d' % (len(self.decisions) + 1)
        self.decisions.append(decision_id)
        self.decision_by_key[fields['operation_id']] = decision_id
        self.journal[fields['operation_id']] = ('committed', {'decision_id': decision_id,
                                                              'evidence': fields['evidence']})
        return {'decision_id': decision_id, 'evidence': fields['evidence']}

    def invoke(self, route, principal, project_id, payload, key, target=None, authorize=None,
               capability=None):
        if route == 'proposals.submit':
            if authorize is not None:
                authorize()
            self.rendezvous()
            body = payload.get('body') if isinstance(payload, dict) else None
            if not isinstance(body, dict):
                raise ValueError('proposal body missing')
            self.native_failure('proposals.submit')
            operation = body['operation_id']
            known = self.proposal_by_key.get(operation)
            if known is not None:
                return dict(self.proposals[known])
            record = {'key': 'proposal_%d' % (len(self.proposals) + 1), 'revision': 1,
                      'sha256': 'c' * 64, 'state': 'submitted',
                      'evidence': list(body.get('evidence') or []),
                      'text': body.get('text'), 'rationale': body.get('rationale')}
            self.proposals[record['key']] = record
            self.proposal_by_key[operation] = record['key']
            self.journal[(principal.user_id, route, key, target)] = ('committed', dict(record))
            return dict(record)
        return super().invoke(route, principal, project_id, payload, key, target=target,
                              authorize=authorize, capability=capability)


class RoomsItemsHttpTests(agents.AgentHarness):
    backend_class = ItemsBackend

    def setUp(self):
        super().setUp()
        self.rooms_root = unique_dir('rooms-items-http-')
        self.addCleanup(__import__('shutil').rmtree, self.rooms_root, ignore_errors=True)
        self.backend.files_root = str(self.rooms_root)
        self.limit = dict(rooms.LIMIT_DEFAULTS)
        self.backend.rooms_limits = lambda **kw: dict(self.limit)
        self.admin = self.admin_token()
        self.pid = self.create_project(self.admin, 'Rooms')
        self.other_pid = self.create_project(self.admin, 'Elsewhere')
        self.tokens = {}
        self.ids = {}
        for name, role, project in (('owner', 'owner', self.pid), ('coord', 'coordinator', self.pid),
                                    ('worker', 'contributor', self.pid), ('viewer', 'viewer', self.pid),
                                    ('outsider', 'coordinator', self.other_pid)):
            self.ids[name] = self.create_account(self.admin, name, name + '-password-1')
            put = self.request('PUT', '/v1/projects/%s/members/%s' % (project, self.ids[name]),
                               {'role': role}, token=self.admin)
            self.assertIn(put.status, (200, 201), put.data)
            self.tokens[name] = self.login(name, name + '-password-1')[0]
        self.room = self.create_room(self.tokens['coord'], 'Planning')

    # -- helpers -----------------------------------------------------------------------------------
    def path(self, suffix='', rid=None):
        base = '/v1/projects/%s/rooms' % self.pid
        if rid or suffix:
            base += '/' + (rid or self.room['id'])
        return base + suffix

    def create_room(self, token, name):
        made = self.request('POST', '/v1/projects/%s/rooms' % self.pid, {'name': name}, token=token)
        self.assertEqual(201, made.status, made.data)
        return made.data

    def post_item(self, token, kind='requirement', title='Add export', text='We need CSV export.',
                  key='item-key-1', rid=None, **extra):
        body = dict(item_kind=kind, title=title, text=text, **extra)
        return self.request('POST', self.path('/items', rid), body, token=token, key=key)

    def accept(self, token, sequence=1, key='accept-key-1', rid=None, body=None):
        return self.request('POST', self.path('/items/%d/accept' % sequence, rid), body or {},
                            token=token, key=key)

    def decision(self, token, sequence=1, key='decide-key-1'):
        return self.request('POST', self.path('/items/%d/reject' % sequence), {'reason': 'no'},
                            token=token, key=key)

    def read(self, token=None, rid=None):
        return self.request('GET', self.path('', rid or self.room['id']),
                            token=token or self.tokens['viewer'])

    def items(self, token=None, rid=None):
        return self.read(token, rid).data['items']

    def audit(self, action):
        return [event for event in self.service.state['audit'] if event.get('action') == action]

    # -- round 5 (request 01a16bd3): one record per item across callers; no stuck items ----------
    def lines(self):
        return rooms.load_lines(Path(self.rooms_root) / self.pid, self.room['id'])

    def kinds(self, kind):
        return [line for line in self.lines() if line['kind'] == kind]

    def fail_after_native(self, token, key, sequence=1, body=None):
        """An accept whose native write lands but whose room append fails (the review's failure)."""
        real_append = rooms._append
        state = {'failed': False}
        def flaky(project, room_id, line, limits_values):
            if line['kind'] == 'item-event' and not state['failed']:
                state['failed'] = True
                raise OSError(5, 'injected append failure after the native write')
            return real_append(project, room_id, line, limits_values)
        with mock.patch.object(rooms, '_append', flaky):
            failed = self.accept(token, sequence=sequence, key=key, body=body)
        self.assertEqual(500, failed.status, failed.data)
        return failed

    def cancel(self, token, sequence=1, key='cancel-key-1', reason='stuck acceptance'):
        return self.request('POST', self.path('/items/%d/intent/cancel' % sequence),
                            {'reason': reason}, token=token, key=key)

    def moderate(self, token, sequence=1, key='moderate-key-1', reason='off topic'):
        return self.request('POST', self.path('/messages/%d/moderate' % sequence),
                            {'reason': reason}, token=token, key=key)

    def test_r5_owner_fails_then_a_coordinator_retry_adopts_the_one_record(self):
        # Review N1: alex's accept fails after the record; drew's retry must not write a
        # second requirement and decision pair. Drew's retry reads alex's attempt from the
        # operation journal and adopts that record.
        self.post_item(self.tokens['worker'])
        self.fail_after_native(self.tokens['owner'], 'n1-owner')
        self.assertEqual(1, len(self.backend.requirements))
        retried = self.accept(self.tokens['coord'], key='n1-coord')
        self.assertEqual(200, retried.status, retried.data)
        self.assertEqual(1, len(self.backend.requirements), 'one requirement, not two')
        self.assertEqual(1, len(self.backend.decisions), 'one decision, not two')
        self.assertEqual(list(self.backend.requirements), [retried.data['record']['id']])
        self.assertEqual(1, len(self.kinds('item-event')))
        self.assertEqual([], self.kinds('item-intent-attempt'),
                         'adopting writes nothing, so no attempt is logged')

    def test_r5_a_created_unaccepted_requirement_is_accepted_by_the_retrying_editor(self):
        self.post_item(self.tokens['worker'])
        self.backend.fail_native = 'requirements.accept'
        failed = self.accept(self.tokens['owner'], key='half-owner')
        self.assertEqual(503, failed.status, failed.data)
        created = list(self.backend.requirements)
        self.assertEqual(1, len(created))
        retried = self.accept(self.tokens['coord'], key='half-coord')
        self.assertEqual(200, retried.status, retried.data)
        self.assertEqual(created, list(self.backend.requirements))
        self.assertEqual(created[0], retried.data['record']['id'])
        self.assertEqual(1, len(self.backend.decisions))

    def test_r5_a_decision_retry_by_another_coordinator_adopts_the_decision(self):
        self.post_item(self.tokens['worker'], kind='decision', title='Use CSV', text='CSV first.')
        self.fail_after_native(self.tokens['owner'], 'dec-owner')
        retried = self.accept(self.tokens['coord'], key='dec-coord')
        self.assertEqual(200, retried.status, retried.data)
        self.assertEqual(1, len(self.backend.decisions))
        self.assertEqual(self.backend.decisions[0], retried.data['record']['decision_id'])

    def test_r5_editor_fails_then_a_contributor_is_refused(self):
        # Review N4: the intent is a requirement, so a contributor cannot complete it as a
        # proposal beside it.
        self.post_item(self.tokens['worker'])
        self.fail_after_native(self.tokens['owner'], 'n4-owner')
        refused = self.accept(self.tokens['worker'], key='n4-worker')
        self.assertEqual(403, refused.status, refused.data)
        self.assertEqual({}, self.backend.proposals)
        self.assertEqual(1, len(self.backend.requirements))

    def test_r5_contributor_fails_then_an_editor_must_cancel_before_accepting(self):
        # Review N5 and Q2: a proposal intent is finished only by its author; an editor
        # cancels it (naming the proposal it wrote) and accepts afresh.
        self.post_item(self.tokens['worker'], title='A candidate', text='Contributor text.')
        self.fail_after_native(self.tokens['worker'], 'n5-worker')
        self.assertEqual(1, len(self.backend.proposals))
        refused = self.accept(self.tokens['owner'], key='n5-owner')
        self.assertEqual(409, refused.status, refused.data)
        self.assertEqual({}, self.backend.requirements)
        cancelled = self.cancel(self.tokens['owner'])
        self.assertEqual(200, cancelled.status, cancelled.data)
        self.assertEqual(list(self.backend.proposals), cancelled.data['records'])
        self.assertFalse(cancelled.data['record_may_exist'])
        accepted = self.accept(self.tokens['owner'], key='n5-owner-after')
        self.assertEqual(200, accepted.status, accepted.data)
        self.assertEqual('requirement', accepted.data['record']['kind'])

    def test_r5_a_demoted_editor_is_refused_not_turned_into_a_proposal(self):
        # Review N3.
        self.post_item(self.tokens['worker'])
        self.fail_after_native(self.tokens['coord'], 'n3-coord')
        put = self.request('PUT', '/v1/projects/%s/members/%s' % (self.pid, self.ids['coord']),
                           {'role': 'contributor'}, token=self.admin)
        self.assertIn(put.status, (200, 201), put.data)
        refused = self.accept(self.tokens['coord'], key='n3-coord-retry')
        self.assertEqual(403, refused.status, refused.data)
        self.assertEqual({}, self.backend.proposals)
        self.assertEqual(1, len(self.backend.requirements))

    def test_r5_an_expired_journal_row_is_uncertain_never_nothing_written(self):
        # The coordinator's condition: a missing journal row for a logged attempt is
        # uncertain (409, then a coordinator cancel), never "nothing was written".
        self.post_item(self.tokens['worker'])
        self.fail_after_native(self.tokens['owner'], 'exp-owner')
        self.backend.journal.clear()
        refused = self.accept(self.tokens['coord'], key='exp-coord')
        self.assertEqual(409, refused.status, refused.data)
        self.assertEqual(1, len(self.backend.requirements), 'no second record on an unknown outcome')
        cancelled = self.cancel(self.tokens['coord'])
        self.assertEqual(200, cancelled.status, cancelled.data)
        self.assertTrue(cancelled.data['record_may_exist'])
        self.assertTrue(cancelled.data['identity'].startswith('room-'))

    def test_r5_a_planted_record_carrying_the_pointer_is_never_adopted(self):
        # The coordinator's condition: only a record the room path itself wrote (found in the
        # endpoint journal under an attempt's own operation id) can match.
        self.post_item(self.tokens['worker'])
        evidence = 'orchestra:room/%s#1' % self.room['id']
        self.backend.requirements['planted'] = {
            'id': 'planted', 'revision': 1, 'sha256': 'a' * 64, 'title': 'Add export',
            'description': 'We need CSV export.\n\n' + evidence, 'decision_id': None, 'parent': None}
        self.backend.fail_native = 'requirements.create'
        failed = self.accept(self.tokens['owner'], key='plant-owner')
        self.assertEqual(503, failed.status, failed.data)
        refused = self.accept(self.tokens['coord'], key='plant-coord')
        self.assertEqual(409, refused.status, refused.data)
        # Round 6 (b1): the author's own attempt has no journal row either, so its retry is
        # unknown too; the way out is a cancel, never adopting the planted record.
        own = self.accept(self.tokens['owner'], key='plant-owner-retry')
        self.assertEqual(409, own.status, own.data)
        self.assertEqual(200, self.cancel(self.tokens['owner'], key='plant-cancel').status)
        accepted = self.accept(self.tokens['owner'], key='plant-owner-after')
        self.assertEqual(200, accepted.status, accepted.data)
        self.assertNotEqual('planted', accepted.data['record']['id'])

    def test_r5_moderation_wins_over_a_failed_native_write(self):
        # Review N6 and M41: nothing was written; moderation cancels the intent and the
        # item's text is no longer served; a later retry of that acceptance is refused.
        self.post_item(self.tokens['worker'], title='Abusive', text='Abusive text here.')
        self.backend.fail_native = 'requirements.create'
        failed = self.accept(self.tokens['owner'], key='n6-owner')
        self.assertEqual(503, failed.status, failed.data)
        self.assertIsNotNone(rooms.pending_accept_intent(self.lines(), 1))
        moderated = self.moderate(self.tokens['coord'])
        self.assertEqual(201, moderated.status, moderated.data)
        self.assertIsNone(rooms.pending_accept_intent(self.lines(), 1))
        cancels = self.kinds('item-intent-cancel')
        self.assertEqual(['moderated'], [line['reason'] for line in cancels])
        self.assertTrue(cancels[0]['record_may_exist'])
        self.assertNotIn('Abusive', json.dumps(self.read().data))
        retried = self.accept(self.tokens['owner'], key='n6-owner')
        self.assertIn(retried.status, (409,), retried.data)
        self.assertEqual({}, self.backend.requirements)

    def test_r5_a_contributors_failed_accept_never_blocks_the_owners_moderation(self):
        # Review N7.
        self.post_item(self.tokens['worker'], title='Candidate', text='Some text.')
        self.backend.fail_native = 'proposals.submit'
        failed = self.accept(self.tokens['worker'], key='n7-worker')
        self.assertEqual(503, failed.status, failed.data)
        moderated = self.moderate(self.tokens['owner'], key='n7-moderate')
        self.assertEqual(201, moderated.status, moderated.data)
        rejected = self.decision(self.tokens['owner'], key='n7-reject')
        self.assertEqual(409, rejected.status, rejected.data)
        self.assertEqual('moderated', rejected.data['error']['code'])

    def test_r5_moderation_with_no_pending_intent_is_unchanged(self):
        self.post_item(self.tokens['worker'])
        moderated = self.moderate(self.tokens['coord'])
        self.assertEqual(201, moderated.status, moderated.data)
        self.assertEqual([], self.kinds('item-intent-cancel'))
        self.assertEqual(1, len(self.kinds('tombstone')))

    def test_r5_archive_cancels_pending_intents(self):
        # Review N2: archiving no longer leaves a pending, unmoderatable item behind.
        self.post_item(self.tokens['worker'])
        self.fail_after_native(self.tokens['owner'], 'n2-owner')
        archived = self.request('POST', self.path('/archive'), {}, token=self.tokens['owner'],
                                key='n2-archive')
        self.assertEqual(200, archived.status, archived.data)
        cancels = self.kinds('item-intent-cancel')
        self.assertEqual(['room archived'], [line['reason'] for line in cancels])
        self.assertEqual(list(self.backend.requirements), cancels[0]['records'])
        self.assertIsNone(rooms.pending_accept_intent(self.lines(), 1))
        moderated = self.moderate(self.tokens['coord'], key='n2-moderate')
        self.assertEqual(201, moderated.status, moderated.data)

    def test_r5_a_retry_names_the_parent_the_intent_recorded(self):
        # Review N9: the intent records the chosen parent; a retry without one uses it, and
        # a retry naming another parent is refused.
        self.backend.jobs.append({'id': 'job_0002', 'title': 'Second job'})
        self.post_item(self.tokens['worker'])
        self.fail_after_native(self.tokens['owner'], 'n9-owner', body={'parent': 'job_0002'})
        intent = self.kinds('item-intent')[0]
        self.assertEqual('job_0002', intent['parent'])
        other = self.accept(self.tokens['owner'], key='n9-other', body={'parent': 'job_0001'})
        self.assertEqual(409, other.status, other.data)
        retried = self.accept(self.tokens['coord'], key='n9-coord')
        self.assertEqual(200, retried.status, retried.data)
        self.assertEqual('job_0002', self.backend.requirements[retried.data['record']['id']]['parent'])

    def test_r5_cancel_rights(self):
        # Q3: owners and coordinators cancel any intent; an intent's author cancels only their
        # own proposal-kind intent; nobody else.
        self.post_item(self.tokens['worker'])
        self.fail_after_native(self.tokens['owner'], 'rights-owner')
        self.assertEqual(403, self.cancel(self.tokens['worker'], key='cancel-worker').status)
        self.assertEqual(403, self.cancel(self.tokens['viewer'], key='cancel-viewer').status)
        self.assertEqual(200, self.cancel(self.tokens['coord'], key='cancel-coord').status)
        self.assertEqual(409, self.cancel(self.tokens['coord'], key='cancel-again').status)
        self.post_item(self.tokens['worker'], title='Second', text='Second text.', key='item-key-2')
        second = self.items()[-1]['sequence']
        self.fail_after_native(self.tokens['worker'], 'rights-worker', sequence=second)
        own = self.cancel(self.tokens['worker'], sequence=second, key='cancel-own-1')
        self.assertEqual(200, own.status, own.data)

    def test_r5_a_cancelled_identity_is_never_accepted_and_a_fresh_one_is(self):
        # M46 and the cancel rule: the old identity is refused at every layer; the item's
        # next acceptance uses a new identity (the cancel count is part of it).
        self.post_item(self.tokens['worker'])
        self.fail_after_native(self.tokens['owner'], 'cid-owner')
        old = self.kinds('item-intent')[0]['identity']
        self.assertEqual(200, self.cancel(self.tokens['coord']).status)
        project = Path(self.rooms_root) / self.pid
        actor = {'id': self.ids['owner'], 'name': 'owner', 'role': 'owner'}
        record = {'kind': 'requirement', 'id': 'x', 'revision': 1, 'sha256': 'a' * 64,
                  'decision_id': None, 'evidence': 'orchestra:room/%s#1' % self.room['id']}
        with self.assertRaises(rooms.Refusal) as caught:
            rooms.intend_accept(project, self.room['id'], 1, old, actor, self.limit,
                                'requirement', 'a' * 16)
        self.assertEqual('cancelled', caught.exception.code)
        with self.assertRaises(rooms.Refusal) as caught:
            rooms.accept_item(project, self.room['id'], 1, old, actor, self.limit, record)
        self.assertEqual('cancelled', caught.exception.code)
        fresh = rooms.accept_identity(self.lines(), self.room['id'], self.items()[0])
        self.assertNotEqual(old, fresh)

    def test_r5_an_intent_replays_only_its_own_identity(self):
        # M46: while an intent is pending, the same identity replays and any other is refused.
        self.post_item(self.tokens['worker'])
        self.fail_after_native(self.tokens['owner'], 'replay-owner')
        intent = self.kinds('item-intent')[0]
        project = Path(self.rooms_root) / self.pid
        actor = {'id': self.ids['owner'], 'name': 'owner', 'role': 'owner'}
        again = rooms.intend_accept(project, self.room['id'], 1, intent['identity'], actor,
                                    self.limit, 'requirement', intent['content'], intent['parent'])
        self.assertTrue(again['replayed'])
        with self.assertRaises(rooms.Refusal) as caught:
            rooms.intend_accept(project, self.room['id'], 1, intent['identity'] + '-other', actor,
                                self.limit, 'requirement', intent['content'])
        self.assertEqual('accepting', caught.exception.code)

    def test_r5_the_accept_identity_follows_the_content(self):
        # M29: an accept identity without the content digest would let an acceptance after
        # an amendment reconcile on the record of the old text.
        self.post_item(self.tokens['worker'])
        before = rooms.accept_identity(self.lines(), self.room['id'], self.items()[0])
        amended = self.request('POST', self.path('/items/1/amend'),
                               {'title': 'Add export', 'text': 'We need CSV export, with headers.'},
                               token=self.tokens['owner'], key='m29-amend')
        self.assertEqual(200, amended.status, amended.data)
        after = rooms.accept_identity(self.lines(), self.room['id'], self.items()[0])
        self.assertNotEqual(before, after)
        accepted = self.accept(self.tokens['owner'], key='m29-accept')
        self.assertEqual(after, self.kinds('item-intent')[0]['identity'])
        self.assertIn('with headers', self.backend.requirements[accepted.data['record']['id']]['description'])

    def test_r5_supersede_is_refused_while_an_acceptance_is_pending(self):
        # M42: a new item cannot supersede one whose acceptance is in flight.
        self.post_item(self.tokens['worker'])
        self.fail_after_native(self.tokens['owner'], 'sup-owner')
        superseding = self.post_item(self.tokens['worker'], title='Better', text='Better text.',
                                     key='item-key-sup', supersedes=1)
        self.assertEqual(409, superseding.status, superseding.data)
        self.assertEqual('accepting', superseding.data['error']['code'])

    def test_r5_a_line_under_the_accept_identity_cannot_stick_the_item(self):
        # Review N8 and N8b: a line written under the accept identity (or its intent id), as
        # a pre-round-5 room could hold, neither blocks the acceptance nor makes the item
        # read as decided.
        self.post_item(self.tokens['worker'])
        identity = rooms.accept_identity(self.lines(), self.room['id'], self.items()[0])
        project = Path(self.rooms_root) / self.pid
        author = {'id': self.ids['worker'], 'name': 'worker', 'role': 'contributor'}
        for operation in (identity, 'intent-' + identity):
            last = self.lines()[-1]['sequence']
            rooms._append(project, self.room['id'],
                          {'schema_version': 1, 'sequence': last + 1, 'operation_id': operation,
                           'kind': 'message', 'author': author, 'created_at': rooms.now_iso(),
                           'text': 'planted'}, self.limit)
        accepted = self.accept(self.tokens['owner'], key='n8-accept')
        self.assertEqual(200, accepted.status, accepted.data)
        self.assertEqual('accepted', accepted.data['state'])
        self.assertEqual(1, len(self.backend.requirements))

    def test_r5_a_second_persons_attempt_is_logged_so_a_third_retry_adopts_it(self):
        # M59: the coordinator writes after the owner's attempt is recorded as having written
        # nothing; the coordinator's attempt is logged BEFORE its write, so when its room
        # append fails, the owner's retry finds the coordinator's record instead of writing
        # a second one.
        self.post_item(self.tokens['worker'])
        self.backend.fail_native = 'requirements.create'
        self.assertEqual(503, self.accept(self.tokens['owner'], key='m59-owner').status)
        base = rooms.intent_base(self.kinds('item-intent')[0])
        self.backend.journal[(self.ids['owner'], 'requirements.create',
                              base + '-requirement-create', None)] = ('none', None)
        self.fail_after_native(self.tokens['coord'], 'm59-coord')
        attempts = self.kinds('item-intent-attempt')
        self.assertEqual([self.ids['coord']], [line['author']['id'] for line in attempts])
        retried = self.accept(self.tokens['owner'], key='m59-owner-retry')
        self.assertEqual(200, retried.status, retried.data)
        self.assertEqual(1, len(self.backend.requirements), 'adopted, not written twice')

    def test_r5_the_rooms_module_refuses_a_reserved_key_too(self):
        # M61: the module refuses the room's own namespaces whatever route reaches it, not
        # only behind the HTTP check.
        project = Path(self.rooms_root) / self.pid
        author = {'id': self.ids['worker'], 'name': 'worker', 'role': 'contributor'}
        for prefix in rooms.RESERVED_KEY_PREFIXES:
            with self.assertRaises(rooms.Refusal) as caught:
                rooms.post(project, self.room['id'], 'hello', prefix + 'module-key', author,
                           self.limit)
            self.assertEqual(422, caught.exception.status)
            with self.assertRaises(rooms.Refusal):
                rooms.post_item(project, self.room['id'], 'question', 'Q', 'Why?',
                                prefix + 'module-key', author, self.limit)
        self.assertEqual([], self.lines())

    def test_r5_a_moderated_items_intent_parent_is_not_exported(self):
        # M66: the parent an editor chose is the item's data; once the item is moderated the
        # JSON-lines export drops it from the intent line too.
        self.backend.jobs.append({'id': 'job_0002_secret', 'title': 'Second job'})
        self.post_item(self.tokens['worker'])
        self.fail_after_native(self.tokens['owner'], 'm66-owner', body={'parent': 'job_0002_secret'})
        self.assertEqual(201, self.moderate(self.tokens['coord'], key='m66-moderate').status)
        exported = self.request('GET', self.path('/export?format=jsonl'), token=self.tokens['owner'])
        self.assertEqual(200, exported.status)
        text = exported.data if isinstance(exported.data, str) else json.dumps(exported.data)
        self.assertIn('item-intent', text)
        self.assertNotIn('job_0002_secret', text)

    def test_an_index_without_its_items_cache_is_rebuilt_not_served_empty(self):
        # M7 (a survivor since round 1): an index written by slice 1 has no items cache; a
        # read rebuilds it from the lines instead of serving "no items".
        self.post_item(self.tokens['worker'])
        project = Path(self.rooms_root) / self.pid
        index_path = rooms._index_path(project)
        index = json.loads(index_path.read_text(encoding='utf-8'))
        index['rooms'][self.room['id']].pop('items')
        index_path.write_text(json.dumps(index), encoding='utf-8')
        self.assertEqual(['Add export'], [item['title'] for item in self.items()])

    def test_a_named_parent_is_refused_when_the_project_has_no_jobs(self):
        # M37 (a survivor since round 3): with no jobs at all, a named parent is still not a
        # job, so the accept is refused before any record is written.
        self.backend.jobs = []
        self.post_item(self.tokens['worker'])
        refused = self.accept(self.tokens['owner'], key='no-jobs-parent', body={'parent': 'job_0001'})
        self.assertEqual(422, refused.status, refused.data)
        self.assertEqual({}, self.backend.requirements)

    def test_an_item_replay_must_match_its_parent(self):
        # The same client operation id with another parent is a different item, refused,
        # never answered as a replay of the first. (Round 3's M38 mutates the AMEND replay
        # instead; an amendment's operation id already digests its parent, so that mutant is
        # equivalent.)
        project = Path(self.rooms_root) / self.pid
        author = {'id': self.ids['worker'], 'name': 'worker', 'role': 'contributor'}
        rooms.post_item(project, self.room['id'], 'requirement', 'T', 'Body.', 'item-replay-1',
                        author, self.limit, parent='job_0001')
        with self.assertRaises(rooms.Refusal) as caught:
            rooms.post_item(project, self.room['id'], 'requirement', 'T', 'Body.', 'item-replay-1',
                            author, self.limit)
        self.assertEqual('operation_reused', caught.exception.code)

    def test_r5_at_most_five_people_try_one_intent(self):
        # M68: after five different people, a sixth attempt is refused until a coordinator
        # cancels the intent, so the earlier attempts a retry must check stay bounded.
        self.post_item(self.tokens['worker'])
        self.fail_after_native(self.tokens['owner'], 'limit-owner')
        identity = self.kinds('item-intent')[0]['identity']
        project = Path(self.rooms_root) / self.pid
        for number in range(2, rooms.MAX_ATTEMPT_AUTHORS + 1):
            rooms.record_attempt(project, self.room['id'], 1, identity,
                                 {'id': 'person-%d' % number, 'name': 'p%d' % number,
                                  'role': 'coordinator'}, self.limit)
        with self.assertRaises(rooms.Refusal) as caught:
            rooms.record_attempt(project, self.room['id'], 1, identity,
                                 {'id': 'person-6', 'name': 'p6', 'role': 'coordinator'}, self.limit)
        self.assertEqual('accepting', caught.exception.code)
        self.assertEqual(rooms.MAX_ATTEMPT_AUTHORS,
                         len(rooms.intent_authors(self.lines(), identity)))

    def test_r5_a_reserved_key_prefix_is_refused_on_every_room_write_route(self):
        # The coordinator's condition on the plan: every room write route, messages included.
        self.post_item(self.tokens['worker'])
        routes = [('/v1/projects/%s/rooms' % self.pid, {'name': 'Another'}),
                  (self.path('/messages'), {'text': 'hello'}),
                  (self.path('/items'), {'item_kind': 'question', 'title': 'Q', 'text': 'Why?'}),
                  (self.path('/items/1/accept'), {}),
                  (self.path('/items/1/reject'), {'reason': 'no'}),
                  (self.path('/items/1/amend'), {'title': 'T', 'text': 'Body.'}),
                  (self.path('/items/1/answer'), {'text': 'Because.'}),
                  (self.path('/items/1/intent/cancel'), {'reason': 'stuck'}),
                  (self.path('/messages/1/moderate'), {'reason': 'off topic'}),
                  (self.path('/archive'), {})]
        for prefix in rooms.RESERVED_KEY_PREFIXES:
            for path, body in routes:
                answer = self.request('POST', path, body, token=self.tokens['owner'],
                                      key=prefix + 'client-key-1')
                self.assertEqual(422, answer.status, (path, answer.data))
        self.assertEqual(1, len(self.lines()), 'nothing was written')
        self.assertEqual('open', self.read().data['room']['state'])

    # -- round 6 (request 01a16e15) --------------------------------------------------------------
    def test_r6_the_same_persons_retry_adopts_its_own_record(self):
        # B1: the caller's own earlier attempt is reconciled through the journal too, so a
        # retry by the same person after a failed append adopts that record.
        self.post_item(self.tokens['worker'], kind='decision', title='Use CSV', text='CSV first.')
        self.fail_after_native(self.tokens['owner'], 'own-first')
        retried = self.accept(self.tokens['owner'], key='own-retry')
        self.assertEqual(200, retried.status, retried.data)
        self.assertEqual(1, len(self.backend.decisions))
        self.assertEqual(1, len([call for call in self.backend.calls if call[0] == 'room_decision']),
                         'the retry adopted the record instead of writing again')

    def test_r6_a_missing_own_row_is_unknown_never_a_second_write(self):
        # V2: the same person's journal rows are gone between the attempts: 409, one record.
        self.post_item(self.tokens['worker'], kind='decision', title='Use CSV', text='CSV first.')
        self.fail_after_native(self.tokens['owner'], 'own-gone-first')
        self.backend.journal.clear()
        refused = self.accept(self.tokens['owner'], key='own-gone-retry')
        self.assertEqual(409, refused.status, refused.data)
        self.assertIn('your', refused.data['error']['message'])
        self.assertEqual(1, len(self.backend.decisions))

    def test_r6_a_journal_row_written_by_someone_else_is_not_adopted(self):
        # S2: a row under an earlier attempt's id that another person wrote is not that
        # attempt's record; it counts as "wrote nothing there", so it is never adopted.
        self.post_item(self.tokens['worker'])
        self.backend.fail_native = 'requirements.create'
        self.assertEqual(503, self.accept(self.tokens['owner'], key='foreign-owner').status)
        intent = self.kinds('item-intent')[0]
        key = rooms.intent_base(intent) + '-requirement-create'
        self.backend.requirements['casey'] = {'id': 'casey', 'revision': 1, 'sha256': 'a' * 64,
                                             'title': 'x', 'description': 'x', 'decision_id': None}
        self.backend.journal[(self.ids['owner'], 'requirements.create', key, None)] = (
            'committed', dict(self.backend.requirements['casey']), self.ids['worker'])
        retried = self.accept(self.tokens['coord'], key='foreign-coord')
        self.assertEqual(200, retried.status, retried.data)
        self.assertNotEqual('casey', retried.data['record']['id'])

    def test_r6_intent_operation_ids_carry_a_nonce_no_export_serves(self):
        # S2: the native operation ids of an intent include a random nonce; no read or export
        # serves it, so a member cannot compute them before the write.
        self.post_item(self.tokens['worker'])
        accepted = self.accept(self.tokens['owner'], key='nonce-accept')
        self.assertEqual(200, accepted.status, accepted.data)
        intent = self.kinds('item-intent')[0]
        self.assertRegex(intent['nonce'], r'\A[0-9a-f]{16}\Z')
        keys = [call[2] for call in self.backend.calls if call[0] == 'owner_requirements']
        self.assertTrue(keys and all(intent['nonce'] in key for key in keys), keys)
        for fmt in ('jsonl', 'markdown'):
            exported = self.request('GET', self.path('/export?format=' + fmt), token=self.tokens['owner'])
            text = exported.data if isinstance(exported.data, str) else json.dumps(exported.data)
            self.assertNotIn(intent['nonce'], text)
        self.assertNotIn(intent['nonce'], json.dumps(self.read(self.tokens['owner']).data))

    def held_room_lock(self):
        """Patch the room lock so a test can tell whether a call runs while it is held."""
        state = {'held': 0}
        original = http_service.ApiHandler._rooms_lock
        import contextlib
        @contextlib.contextmanager
        def counted(handler, project):
            with original(handler, project):
                state['held'] += 1
                try:
                    yield
                finally:
                    state['held'] -= 1
        return state, mock.patch.object(http_service.ApiHandler, '_rooms_lock', counted)

    def test_r6_moderation_and_archive_resolve_outcomes_outside_the_room_lock(self):
        # S1: the room lock is the project's .coordination.lock, which the endpoint takes for
        # its reads; resolving an outcome under it stalled moderation until the endpoint
        # timed out. No attempt's outcome is resolved while the room lock is held.
        self.post_item(self.tokens['worker'])
        self.fail_after_native(self.tokens['owner'], 's1-owner')
        self.post_item(self.tokens['worker'], title='Second', text='Second text.', key='item-key-2')
        second = self.items()[-1]['sequence']
        self.fail_after_native(self.tokens['owner'], 's1-owner-2', sequence=second)
        state, patch = self.held_room_lock()
        original = http_service.ApiHandler._room_prior_record
        seen = []
        def watched(handler, *args, **kwargs):
            seen.append(state['held'])
            return original(handler, *args, **kwargs)
        with patch, mock.patch.object(http_service.ApiHandler, '_room_prior_record', watched):
            self.assertEqual(201, self.moderate(self.tokens['coord'], key='s1-moderate').status)
            archived = self.request('POST', self.path('/archive'), {}, token=self.tokens['owner'],
                                    key='s1-archive')
            self.assertEqual(200, archived.status, archived.data)
        self.assertTrue(seen)
        self.assertEqual([0] * len(seen), seen, 'an outcome was resolved under the room lock')
        cancels = self.kinds('item-intent-cancel')
        self.assertEqual(2, len(cancels))
        self.assertTrue(all(line['records'] for line in cancels), cancels)

    def test_r6_the_accept_append_holds_the_room_lock(self):
        # N1 (M12): message posts take only the room lock, so the accept's append must hold
        # it too, or the two appends race on the same segment.
        self.post_item(self.tokens['worker'])
        state, patch = self.held_room_lock()
        original = rooms.accept_item
        seen = []
        def watched(*args, **kwargs):
            seen.append(state['held'])
            return original(*args, **kwargs)
        with patch, mock.patch.object(rooms, 'accept_item', watched):
            self.assertEqual(200, self.accept(self.tokens['owner'], key='lock-accept').status)
        self.assertEqual([1], seen)

    def test_r6_a_demoted_author_of_a_requirement_intent_cannot_cancel_it(self):
        # N3 (V8): an author cancels only its own PROPOSAL intent; a former editor who wrote a
        # requirement intent and has been demoted is refused.
        self.post_item(self.tokens['worker'])
        self.fail_after_native(self.tokens['coord'], 'n3-coord')
        put = self.request('PUT', '/v1/projects/%s/members/%s' % (self.pid, self.ids['coord']),
                           {'role': 'contributor'}, token=self.admin)
        self.assertIn(put.status, (200, 201), put.data)
        self.assertEqual(403, self.cancel(self.tokens['coord'], key='n3-cancel').status)

    def test_r6_an_author_demoted_to_viewer_cannot_cancel_its_proposal(self):
        # N4: the author still needs the proposal authority when it cancels.
        self.post_item(self.tokens['worker'], title='Candidate', text='Some text.')
        self.fail_after_native(self.tokens['worker'], 'n4-worker')
        put = self.request('PUT', '/v1/projects/%s/members/%s' % (self.pid, self.ids['worker']),
                           {'role': 'viewer'}, token=self.admin)
        self.assertIn(put.status, (200, 201), put.data)
        self.assertEqual(403, self.cancel(self.tokens['worker'], key='n4-cancel').status)

    def test_r6_an_already_accepted_requirement_is_adopted_without_accepting_again(self):
        # N3 (V18): the earlier attempt created the requirement and its acceptance landed,
        # but the acceptance's journal row is gone: the retry reads the requirement, sees it
        # accepted and adopts it without a second accept.
        self.post_item(self.tokens['worker'])
        self.fail_after_native(self.tokens['owner'], 'v18-owner')
        intent = self.kinds('item-intent')[0]
        accept_key = rooms.intent_base(intent) + '-requirement-accept'
        for entry in [entry for entry in self.backend.journal if isinstance(entry, tuple)
                      and entry[1] == 'requirements.accept' and entry[2] == accept_key]:
            del self.backend.journal[entry]
        before = len([call for call in self.backend.calls if call[1] == 'accept'])
        retried = self.accept(self.tokens['coord'], key='v18-coord')
        self.assertEqual(200, retried.status, retried.data)
        self.assertEqual(before, len([call for call in self.backend.calls if call[1] == 'accept']))
        self.assertEqual(1, len(self.backend.requirements))

    # -- routes and authority ----------------------------------------------------------------------
    def test_item_post_and_read_are_members_only(self):
        posted = self.post_item(self.tokens['worker'])
        self.assertEqual(201, posted.status, posted.data)
        self.assertEqual(('item', 'proposed', 'requirement'),
                         (posted.data['kind'], posted.data['state'], posted.data['item_kind']))
        read = self.read(self.tokens['viewer'])
        self.assertEqual(['Add export'], [item['title'] for item in read.data['items']])
        self.assertIn('orchestra:room/%s#1' % self.room['id'], read.data['items'][0]['evidence'])
        self.assertEqual(403, self.post_item(self.tokens['viewer'], key='viewer-item').status)
        outsider = self.post_item(self.tokens['outsider'], key='outsider-item', rid=self.room['id'])
        self.assertIn(outsider.status, (403, 404), outsider.data)
        self.assertEqual('committed', self.audit('rooms.item.post')[-1]['outcome'])

    def test_requirement_owner_accept_creates_a_requirement_and_its_decision(self):
        self.post_item(self.tokens['worker'])
        accepted = self.accept(self.tokens['owner'])
        self.assertEqual(200, accepted.status, accepted.data)
        self.assertEqual('accepted', accepted.data['state'])
        record = accepted.data['record']
        self.assertEqual('requirement', record['kind'])
        self.assertIn('orchestra:room/%s#1' % self.room['id'], record['evidence'])
        requirement = self.backend.requirements[record['id']]
        self.assertIn(record['evidence'], requirement['description'])
        self.assertEqual(record['evidence'], requirement['decision_pointer'])
        self.assertEqual(record['decision_id'], requirement['decision_id'])
        self.assertEqual('committed', self.audit('rooms.item.accept')[-1]['outcome'])

    def test_contributor_accept_is_a_proposal_not_a_requirement(self):
        self.post_item(self.tokens['worker'], title='A candidate', text='Contributor text.')
        accepted = self.accept(self.tokens['worker'])
        self.assertEqual(200, accepted.status, accepted.data)
        record = accepted.data['record']
        self.assertEqual('proposal', record['kind'])
        self.assertEqual([record['evidence']], self.backend.proposals[record['id']]['evidence'])
        self.assertIn('orchestra:room/%s#1' % self.room['id'], record['evidence'])
        self.assertEqual({}, self.backend.requirements)

    def test_a_requirement_parent_must_be_an_existing_job(self):
        # Review n1: a contributor-chosen parent is not trusted; an editor's accept checks
        # that the named parent is one of the project's jobs before the record write.
        self.post_item(self.tokens['worker'], parent='not-a-job')
        refused = self.accept(self.tokens['owner'])
        self.assertEqual(422, refused.status, refused.data)
        self.assertEqual({}, self.backend.requirements)
        self.assertEqual([], self.backend.decisions)

    def test_decision_owner_or_coordinator_only(self):
        self.post_item(self.tokens['worker'], kind='decision', title='Use CSV',
                       text='We will emit CSV first.')
        refused = self.accept(self.tokens['worker'])
        self.assertEqual(403, refused.status, refused.data)
        accepted = self.accept(self.tokens['coord'])
        self.assertEqual(200, accepted.status, accepted.data)
        self.assertEqual('decision', accepted.data['record']['kind'])
        self.assertTrue(self.backend.decisions)
        self.assertEqual(accepted.data['record']['decision_id'], self.backend.decisions[-1])

    def test_accept_twice_converges_on_one_record_and_one_event(self):
        self.post_item(self.tokens['worker'])
        first = self.accept(self.tokens['owner'], key='accept-key-a')
        second = self.accept(self.tokens['owner'], key='accept-key-a')
        self.assertEqual(200, first.status, first.data)
        self.assertEqual(200, second.status, second.data)
        self.assertEqual((first.data['sequence'], first.data['state']),
                         (second.data['sequence'], second.data['state']))
        events = [line for line in rooms.load_lines(Path(self.rooms_root) / self.pid, self.room['id'])
                  if line['kind'] == 'item-event']
        self.assertEqual(1, len(events))
        self.assertEqual(1, len(self.backend.decisions), 'one native decision, not two')
        # A DIFFERENT client key after the item is decided is a new decision attempt: refused,
        # and (review b1) it counts no second record or event.
        third = self.accept(self.tokens['owner'], key='accept-key-b')
        self.assertEqual(409, third.status, third.data)
        self.assertEqual(1, len(events))
        self.assertEqual(1, len(self.backend.requirements))
        self.assertEqual(1, len(self.backend.decisions))
        self.assertEqual({}, self.backend.proposals)

    def test_concurrent_accepts_create_only_one_record(self):
        # Review b2: a contributor accept (a proposal) and an owner accept (a requirement)
        # running together used to create two records. The item-decision lock makes the
        # second one see the decided item and write nothing.
        self.post_item(self.tokens['worker'])
        self.backend.accept_barrier = threading.Barrier(2)
        answers = []
        def run(token, key):
            answers.append((key, self.accept(token, key=key)))
        threads = [threading.Thread(target=run, args=(self.tokens['worker'], 'concurrent-worker')),
                   threading.Thread(target=run, args=(self.tokens['owner'], 'concurrent-owner'))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        self.assertEqual(2, len(answers), 'both concurrent requests answered')
        self.assertEqual([200, 409], sorted(answer.status for _key, answer in answers))
        events = [line for line in rooms.load_lines(Path(self.rooms_root) / self.pid, self.room['id'])
                  if line['kind'] == 'item-event']
        self.assertEqual(1, len(events), 'one decision line')
        winner = next(answer for _key, answer in answers if answer.status == 200).data
        if winner['record']['kind'] == 'proposal':
            self.assertEqual(1, len(self.backend.proposals))
            self.assertEqual({}, self.backend.requirements)
            self.assertEqual([], self.backend.decisions)
        else:
            self.assertEqual(1, len(self.backend.requirements))
            self.assertEqual(1, len(self.backend.decisions))
            self.assertEqual({}, self.backend.proposals)

    def test_a_viewer_cannot_amend_or_reject_another_members_question(self):
        # Review b3: CAP_READ for a question is not a right to amend or reject it.
        self.post_item(self.tokens['worker'], kind='question', title='Delimiter?',
                       text='Comma or semicolon?')
        for route, body in (('/amend', {'title': 'Viewer title', 'text': 'Viewer text'}),
                            ('/reject', {'reason': 'viewer says no'})):
            refused = self.request('POST', self.path('/items/1' + route), body,
                                    token=self.tokens['viewer'], key='viewer-' + route[1:])
            self.assertEqual(403, refused.status, (route, refused.data))
        self.assertEqual(('Delimiter?', 'Comma or semicolon?'),
                         (self.items()[0]['title'], self.items()[0]['text']))
        # The author may amend and reject their own question.
        amended = self.request('POST', self.path('/items/1/amend'),
                               {'title': 'Semicolon?', 'text': 'Semicolon first.'},
                               token=self.tokens['worker'], key='author-amend')
        self.assertEqual(200, amended.status, amended.data)
        rejected = self.request('POST', self.path('/items/1/reject'), {'reason': 'resolved'},
                                token=self.tokens['worker'], key='author-reject')
        self.assertEqual(200, rejected.status, rejected.data)
        # A coordinator or owner may amend and reject anyone's question.
        second = self.post_item(self.tokens['worker'], kind='question', title='Other?',
                                text='Other text.', key='item-key-2')
        self.assertEqual(201, second.status, second.data)
        owner = self.request('POST', self.path('/items/%d/amend' % second.data['sequence']),
                             {'title': 'Owner title', 'text': 'Owner text'},
                             token=self.tokens['owner'], key='owner-amend')
        self.assertEqual(200, owner.status, owner.data)

    def test_a_moderated_items_amended_title_is_not_served(self):
        # Review b4: an amendment's title is user text too and must be redacted.
        self.post_item(self.tokens['worker'], text='Original body.')
        amended = self.request('POST', self.path('/items/1/amend'),
                               {'title': 'SECRET amended title', 'text': 'SECRET amended body'},
                               token=self.tokens['owner'], key='amend-secret')
        self.assertEqual(200, amended.status, amended.data)
        moderated = self.request('POST', self.path('/messages/1/moderate'),
                                 {'reason': 'private'}, token=self.tokens['coord'], key='mod-secret')
        self.assertEqual(201, moderated.status, moderated.data)
        read = self.read(self.tokens['viewer'])
        blob = json.dumps(read.data)
        self.assertNotIn('SECRET amended title', blob)
        self.assertNotIn('SECRET amended body', blob)
        self.assertTrue(read.data['items'][0]['moderated'])
        events = [message for message in read.data['messages'] if message['kind'] == 'item-event']
        self.assertTrue(any(event.get('redacted') and event.get('title') is None
                            for event in events), events)

    def test_an_archived_project_refuses_every_item_decision(self):
        # Review s1: the item routes must apply slice 1's archived-project refusal.
        self.post_item(self.tokens['worker'])
        self.post_item(self.tokens['worker'], kind='question', title='Q?', text='Q text.',
                       key='item-key-2')
        self.service.state['projects'][self.pid]['archived'] = True
        self.store.save()
        for route, sequence, body in (('/accept', 1, {}), ('/reject', 1, {'reason': 'no'}),
                                      ('/amend', 1, {'title': 't', 'text': 'x'}),
                                      ('/answer', 2, {'text': 'an answer'})):
            refused = self.request('POST', self.path('/items/%d%s' % (sequence, route)), body,
                                    token=self.tokens['owner'], key='archived-' + route[1:])
            self.assertEqual(409, refused.status, (route, refused.data))
        self.assertEqual({}, self.backend.requirements)
        self.assertEqual({}, self.backend.proposals)
        self.assertEqual([], self.backend.decisions)
        self.assertEqual(200, self.read(self.tokens['viewer']).status)

    def test_an_outsider_gets_no_item_existence_oracle(self):
        # Review n2: membership is checked before the item is read.
        self.post_item(self.tokens['worker'])
        for sequence in (1, 99):
            refused = self.request('POST', self.path('/items/%d/amend' % sequence),
                                    {'title': 'x', 'text': 'y'}, token=self.tokens['outsider'],
                                    key='outsider-%d' % sequence)
            self.assertIn(refused.status, (403, 404), refused.data)
            self.assertNotIn('No such item', json.dumps(refused.data))
        missing = self.request('POST', self.path('/items/99/amend'),
                               {'title': 'x', 'text': 'y'}, token=self.tokens['owner'],
                               key='member-missing')
        self.assertEqual(404, missing.status, missing.data)
        self.assertIn('No such item', missing.data['error']['message'])

    def test_r7_an_agent_credential_is_refused_before_any_grant(self):
        self.post_item(self.tokens['worker'])
        agent_id, secret, _data = self.agent_secret(self.tokens['owner'], projects=[self.pid])
        # Even with the coordinator grant stored live, R7 refuses the credential first.
        self.service.state['coordinator_grants'] = {
            self.pid: {agent_id: {'granted_by': self.ids['owner'],
                                  'granted_at': '2026-10-23T00:00:00Z'}}}
        self.store.save()
        refused = self.accept(secret, key='agent-accept')
        self.assertEqual(403, refused.status, refused.data)
        self.assertIn('signed-in person', refused.data['error']['message'])
        for route in ('/reject', '/amend'):
            body = {'reason': 'x'} if route == '/reject' else {'title': 't', 'text': 'x'}
            answer = self.request('POST', self.path('/items/1' + route), body, token=secret)
            self.assertEqual(403, answer.status, answer.data)

    def test_question_answer_is_a_member_action_with_who_and_when(self):
        self.post_item(self.tokens['worker'], kind='question', title='Delimiter?',
                       text='Comma or semicolon?')
        answered = self.request('POST', self.path('/items/1/answer'), {'text': 'Comma.'},
                                token=self.tokens['viewer'], key='answer-key-1')
        self.assertEqual(200, answered.status, answered.data)
        self.assertEqual('accepted', answered.data['state'])
        self.assertEqual('viewer', answered.data['answered_by']['name'])
        self.assertTrue(answered.data['answered_at'])
        accept = self.accept(self.tokens['owner'])
        self.assertEqual(400, accept.status, accept.data)

    def test_moderated_item_cannot_be_decided_and_its_text_is_not_served(self):
        self.post_item(self.tokens['worker'], text='SECRET room item text.')
        moderated = self.request('POST', self.path('/messages/1/moderate'),
                                 {'reason': 'private'}, token=self.tokens['coord'], key='moderate-1')
        self.assertEqual(201, moderated.status, moderated.data)
        for route, body in (('/accept', {}), ('/reject', {'reason': 'x'}),
                            ('/amend', {'title': 't', 'text': 'x'})):
            answer = self.request('POST', self.path('/items/1' + route), body,
                                  token=self.tokens['owner'], key='after-mod-' + route[1:])
            self.assertEqual(409, answer.status, (route, answer.data))
        read = self.read(self.tokens['viewer'])
        self.assertNotIn('SECRET room item text.', json.dumps(read.data))
        self.assertTrue(read.data['items'][0]['moderated'])
        index = (Path(self.rooms_root) / self.pid / rooms.FOLDER / rooms.INDEX).read_text(encoding='utf-8')
        self.assertNotIn('SECRET room item text.', index)

    def test_reject_and_amend_then_decide(self):
        self.post_item(self.tokens['worker'])
        amended = self.request('POST', self.path('/items/1/amend'),
                               {'title': 'Add CSV export now', 'text': 'With a header row.'},
                               token=self.tokens['owner'], key='amend-key-1')
        self.assertEqual(200, amended.status, amended.data)
        self.assertEqual(('amended', 'Add CSV export now'),
                         (amended.data['state'], amended.data['title']))
        rejected = self.request('POST', self.path('/items/1/reject'),
                                {'reason': 'not this release'}, token=self.tokens['owner'],
                                key='reject-key-1')
        self.assertEqual(200, rejected.status, rejected.data)
        self.assertEqual('rejected', rejected.data['state'])
        final = self.accept(self.tokens['owner'], key='accept-final')
        self.assertEqual(409, final.status, final.data)

    def test_amend_twice_with_different_text_then_accept_latest(self):
        # Review b1/r3: amend's identity follows the content it writes, so a second
        # amendment applies instead of answering 409 operation_reused.
        self.post_item(self.tokens['worker'])
        first = self.request('POST', self.path('/items/1/amend'),
                             {'title': 'First title', 'text': 'First body.'},
                             token=self.tokens['owner'], key='amend-once')
        self.assertEqual(200, first.status, first.data)
        second = self.request('POST', self.path('/items/1/amend'),
                              {'title': 'Second title', 'text': 'Second body.'},
                              token=self.tokens['owner'], key='amend-twice')
        self.assertEqual(200, second.status, second.data)
        self.assertEqual(('amended', 'Second title', 'Second body.'),
                         (second.data['state'], second.data['title'], second.data['text']))
        accepted = self.accept(self.tokens['owner'], key='accept-amended')
        self.assertEqual(200, accepted.status, accepted.data)
        self.assertEqual('accepted', accepted.data['state'])
        record = accepted.data['record']
        self.assertIn('Second body.',
                      self.backend.requirements[record['id']]['description'])
        amendments = [event for event in self.items()[0]['history']
                      if event['event'] == 'amend']
        self.assertEqual(['First title', 'Second title'],
                         [event['title'] for event in amendments])

    def test_archive_waits_for_an_accept_inside_its_record_write(self):
        # Review b2/r3: an archive takes the item-decision lock BEFORE the room lock, so it
        # cannot slip into the accept's native record write. Either the accept finishes and
        # is recorded, or nothing is written; here the accept got the lock first.
        self.post_item(self.tokens['worker'])
        entered = threading.Event()
        release = threading.Event()
        self.backend.record_entered = entered
        self.backend.record_release = release
        answers = {}
        def run_accept():
            answers['accept'] = self.accept(self.tokens['owner'], key='race-accept')
        def run_archive():
            answers['archive'] = self.request('POST', self.path('/archive'), {'reason': 'racing'},
                                              token=self.tokens['coord'], key='race-archive')
        worker = threading.Thread(target=run_accept)
        worker.start()
        archiver = None
        try:
            self.assertTrue(entered.wait(10), 'the accept reached its record write')
            archiver = threading.Thread(target=run_archive)
            archiver.start()
            archiver.join(1)
            self.assertTrue(archiver.is_alive(), 'the archive waited for the item-decision lock')
        finally:
            release.set()
            worker.join(30)
            if archiver is not None:
                archiver.join(30)
        self.assertEqual(200, answers['accept'].status, answers['accept'].data)
        self.assertEqual(200, answers['archive'].status, answers['archive'].data)
        self.assertEqual(1, len(self.backend.requirements), 'one requirement, written before the archive')
        self.assertEqual(1, len(self.backend.decisions))
        events = [line for line in rooms.load_lines(Path(self.rooms_root) / self.pid, self.room['id'])
                  if line['kind'] == 'item-event']
        self.assertEqual(1, len(events), 'the acceptance was recorded before the room was archived')
        self.assertEqual('accepted', self.items()[0]['state'])

    def test_accept_after_a_failed_append_keeps_one_record_and_refuses_amend(self):
        # Review b1/r4: the route writes an accept intent naming the native identity BEFORE
        # the record write. When the room append fails AFTER the native write, the intent is
        # still pending: an amendment is refused (409 accepting), a retry of the same
        # identity reconciles on that record, and the item counts one record and one event.
        self.post_item(self.tokens['worker'], kind='decision', title='Use CSV',
                       text='We will emit CSV first.')
        real_append = rooms._append
        state = {'failed': False}
        def flaky(project, room_id, line, limits_values):
            if line['kind'] == 'item-event' and not state['failed']:
                state['failed'] = True
                raise OSError(5, 'injected append failure after the native write')
            return real_append(project, room_id, line, limits_values)
        with mock.patch.object(rooms, '_append', flaky):
            failed = self.accept(self.tokens['owner'], key='accept-fail')
        self.assertEqual(500, failed.status, failed.data)
        self.assertEqual(1, len(self.backend.decisions), 'the native record was written')
        self.assertEqual('proposed', self.items()[0]['state'])
        intent = [line for line in rooms.load_lines(Path(self.rooms_root) / self.pid,
                                                    self.room['id'])
                  if line['kind'] == 'item-intent']
        self.assertEqual(1, len(intent), 'the intent names the pending record')
        amended = self.request('POST', self.path('/items/1/amend'),
                               {'title': 'Amended export', 'text': 'Amended body with a header row.'},
                               token=self.tokens['owner'], key='amend-after-fail')
        self.assertEqual(409, amended.status, amended.data)
        self.assertEqual('accepting', amended.data['error']['code'])
        self.assertEqual('Use CSV', self.items()[0]['title'])
        accepted = self.accept(self.tokens['owner'], key='accept-after-fail')
        self.assertEqual(200, accepted.status, accepted.data)
        self.assertEqual('accepted', accepted.data['state'])
        self.assertEqual({}, self.backend.requirements, 'a decision item makes no requirement')
        self.assertEqual(1, len(self.backend.decisions), 'the retry reconciled on one decision')
        events = [line for line in rooms.load_lines(Path(self.rooms_root) / self.pid,
                                                    self.room['id'])
                  if line['kind'] == 'item-event']
        self.assertEqual(1, len(events), 'one accepted event, not two')
        self.assertEqual('accepted', self.items()[0]['state'])

    def test_a_failed_intent_append_writes_no_native_record(self):
        # Review b1/r4: the intent append happens before the native write, so a failure
        # there leaves nothing in the tracker and the item can still be amended and decided.
        self.post_item(self.tokens['worker'])
        real_append = rooms._append
        state = {'failed': False}
        def flaky(project, room_id, line, limits_values):
            if line['kind'] == 'item-intent' and not state['failed']:
                state['failed'] = True
                raise OSError(5, 'injected intent append failure')
            return real_append(project, room_id, line, limits_values)
        with mock.patch.object(rooms, '_append', flaky):
            failed = self.accept(self.tokens['owner'], key='accept-intent-fail')
        self.assertEqual(500, failed.status, failed.data)
        self.assertEqual({}, self.backend.requirements)
        self.assertEqual([], self.backend.decisions)
        amended = self.request('POST', self.path('/items/1/amend'),
                               {'title': 'Amended export', 'text': 'Amended body.'},
                               token=self.tokens['owner'], key='amend-after-intent-fail')
        self.assertEqual(200, amended.status, amended.data)
        accepted = self.accept(self.tokens['owner'], key='accept-after-intent-fail')
        self.assertEqual(200, accepted.status, accepted.data)
        self.assertEqual(1, len(self.backend.requirements))

    def test_amend_back_to_an_earlier_text_applies_and_an_exact_retry_replays(self):
        # Review s1/r4: the amendment identity names the previous line, so A, B, then A is
        # a real change (it used to answer 200 replayed and leave the item at B), while the
        # exact same request against the same current line still replays.
        self.post_item(self.tokens['worker'])
        first = self.request('POST', self.path('/items/1/amend'),
                             {'title': 'A title', 'text': 'A body.'},
                             token=self.tokens['owner'], key='amend-a1-key')
        self.assertEqual(200, first.status, first.data)
        second = self.request('POST', self.path('/items/1/amend'),
                              {'title': 'B title', 'text': 'B body.'},
                              token=self.tokens['owner'], key='amend-b-key')
        self.assertEqual(200, second.status, second.data)
        back = self.request('POST', self.path('/items/1/amend'),
                            {'title': 'A title', 'text': 'A body.'},
                            token=self.tokens['owner'], key='amend-a2-key')
        self.assertEqual(200, back.status, back.data)
        self.assertEqual(('A title', 'A body.'), (back.data['title'], back.data['text']))
        self.assertEqual('A body.', self.items()[0]['text'])
        events = [line for line in rooms.load_lines(Path(self.rooms_root) / self.pid,
                                                    self.room['id'])
                  if line['kind'] == 'item-event']
        self.assertEqual(3, len(events), 'A, B and back to A are three applied amendments')
        # A retry of the very same request key replays the stored answer.
        retry = self.request('POST', self.path('/items/1/amend'),
                             {'title': 'A title', 'text': 'A body.'},
                             token=self.tokens['owner'], key='amend-a2-key')
        self.assertEqual(200, retry.status, retry.data)
        self.assertEqual(3, len([line for line in rooms.load_lines(
            Path(self.rooms_root) / self.pid, self.room['id'])
            if line['kind'] == 'item-event']), 'a replay writes no fourth amendment')
        # The same content through a fresh client key is a replay too, not a fourth event.
        fresh = self.request('POST', self.path('/items/1/amend'),
                             {'title': 'A title', 'text': 'A body.'},
                             token=self.tokens['owner'], key='amend-a-fresh-key')
        self.assertEqual(200, fresh.status, fresh.data)
        self.assertEqual(3, len([line for line in rooms.load_lines(
            Path(self.rooms_root) / self.pid, self.room['id'])
            if line['kind'] == 'item-event']), 'an unchanged amendment is a replay')

    def test_a_moderated_items_parent_is_not_served(self):
        # Review n1/r3: the parent job is user-chosen item data too; moderation clears it in
        # the room read and the index cache.
        self.post_item(self.tokens['worker'], parent='job_0001')
        moderated = self.request('POST', self.path('/messages/1/moderate'),
                                 {'reason': 'private'}, token=self.tokens['coord'], key='mod-parent')
        self.assertEqual(201, moderated.status, moderated.data)
        read = self.read(self.tokens['viewer'])
        self.assertNotIn('job_0001', json.dumps(read.data))
        self.assertIsNone(read.data['items'][0]['parent'])
        index = (Path(self.rooms_root) / self.pid / rooms.FOLDER / rooms.INDEX).read_text(
            encoding='utf-8')
        self.assertNotIn('job_0001', index)

    def test_an_editor_can_choose_the_parent_on_an_amendment(self):
        # Review n2/r3: an editor can correct the parent job by amending; an explicit accept
        # parent wins over the stored one, and a parent that is not a job is refused.
        self.post_item(self.tokens['worker'], parent='job_0001')
        self.backend.requirements_read = lambda pid, args: {
            'items': [], 'decisions': [],
            'jobs': [{'id': 'job_0001', 'title': 'One'}, {'id': 'job_0002', 'title': 'Two'}]}
        wrong = self.request('POST', self.path('/items/1/amend'),
                             {'title': 'Export', 'text': 'Body.', 'parent': 'not-a-job'},
                             token=self.tokens['owner'], key='amend-wrong-parent')
        self.assertEqual(422, wrong.status, wrong.data)
        amended = self.request('POST', self.path('/items/1/amend'),
                               {'title': 'Export', 'text': 'Body.', 'parent': 'job_0002'},
                               token=self.tokens['owner'], key='amend-parent')
        self.assertEqual(200, amended.status, amended.data)
        self.assertEqual('job_0002', amended.data['parent'])
        accepted = self.accept(self.tokens['owner'], key='accept-parent-choice',
                               body={'parent': 'job_0002'})
        self.assertEqual(200, accepted.status, accepted.data)
        record = accepted.data['record']
        self.assertEqual('job_0002', self.backend.requirements[record['id']]['parent'])

    def test_an_editor_can_choose_the_parent_on_accept(self):
        # Review test gap: the accept route's explicit parent choice, not only the amend.
        self.post_item(self.tokens['worker'], parent='job_0001')
        self.backend.requirements_read = lambda pid, args: {
            'items': [], 'decisions': [],
            'jobs': [{'id': 'job_0001', 'title': 'One'}, {'id': 'job_0002', 'title': 'Two'}]}
        accepted = self.accept(self.tokens['owner'], key='accept-parent-at-accept',
                               body={'parent': 'job_0002'})
        self.assertEqual(200, accepted.status, accepted.data)
        record = accepted.data['record']
        self.assertEqual('job_0002', self.backend.requirements[record['id']]['parent'])

    def test_an_editor_accept_with_no_jobs_is_refused(self):
        # Review test gap: a requirement needs an existing job; an empty job list is not a
        # reason to make one up.
        self.post_item(self.tokens['worker'])
        self.backend.requirements_read = lambda pid, args: {
            'items': [], 'decisions': [], 'jobs': []}
        refused = self.accept(self.tokens['owner'], key='accept-no-jobs')
        self.assertEqual(422, refused.status, refused.data)
        self.assertEqual('invalid_payload', refused.data['error']['code'])
        self.assertEqual({}, self.backend.requirements)

    def test_a_moderated_items_amended_parent_is_not_served(self):
        # Review test gap/n2/r4: an amendment's parent is user-chosen item data too; after
        # moderation the room read, the index cache and both exports drop it.
        self.post_item(self.tokens['worker'], parent='job_0001')
        self.backend.requirements_read = lambda pid, args: {
            'items': [], 'decisions': [],
            'jobs': [{'id': 'job_0001', 'title': 'One'}, {'id': 'job_0002', 'title': 'Two'}]}
        amended = self.request('POST', self.path('/items/1/amend'),
                               {'title': 'Export', 'text': 'Body.', 'parent': 'job_0002'},
                               token=self.tokens['owner'], key='amend-then-mod-parent')
        self.assertEqual(200, amended.status, amended.data)
        moderated = self.request('POST', self.path('/messages/1/moderate'),
                                 {'reason': 'private'}, token=self.tokens['coord'],
                                 key='mod-amend-parent')
        self.assertEqual(201, moderated.status, moderated.data)
        read = self.read(self.tokens['viewer'])
        self.assertNotIn('job_0002', json.dumps(read.data))
        self.assertIsNone(read.data['items'][0]['parent'])
        index = (Path(self.rooms_root) / self.pid / rooms.FOLDER / rooms.INDEX).read_text(
            encoding='utf-8')
        self.assertNotIn('job_0002', index)
        for wanted in ('jsonl', 'markdown'):
            exported = self.request('GET', self.path('/export?format=' + wanted),
                                    token=self.tokens['viewer'])
            self.assertEqual(200, exported.status, exported.data)
            self.assertNotIn('job_0002', exported.data['text'])

    def test_a_symlinked_room_or_item_lock_is_refused(self):
        # Review n3/r3: a lock is never opened through a symlink (as admin.py checks the
        # coordination lock for lifecycle writes).
        project = Path(self.rooms_root) / self.pid
        target = project / 'elsewhere'
        coordination = project / '.coordination.lock'
        coordination.unlink(missing_ok=True)
        coordination.symlink_to(target)
        refused = self.read(self.tokens['viewer'])
        self.assertEqual(500, refused.status, refused.data)
        self.assertEqual('damaged', refused.data['error']['code'])
        coordination.unlink()
        self.post_item(self.tokens['worker'])
        item_lock = project / '.item-decisions.lock'
        item_lock.unlink(missing_ok=True)
        item_lock.symlink_to(target)
        refused = self.accept(self.tokens['owner'], key='symlink-accept')
        self.assertEqual(500, refused.status, refused.data)
        self.assertEqual('damaged', refused.data['error']['code'])
        self.assertEqual({}, self.backend.requirements)

    def test_supersede_and_bounds(self):
        first = self.post_item(self.tokens['worker'])
        self.assertEqual(201, first.status, first.data)
        second = self.post_item(self.tokens['worker'], title='v2', text='Second version.',
                                key='item-key-2', supersedes=1)
        self.assertEqual(201, second.status, second.data)
        items = self.items()
        self.assertEqual('superseded', items[0]['state'])
        self.assertEqual(409, self.accept(self.tokens['owner'], sequence=1, key='accept-old-1').status)
        self.assertEqual(422, self.post_item(self.tokens['worker'], title='x' * 201,
                                             key='long-title').status)
        self.assertEqual(422, self.post_item(self.tokens['worker'], text='x' * 4001,
                                             key='long-text').status)
        reserved = self.post_item(self.tokens['worker'], text='Kind: task-checkpoint-v1\n{}',
                                  key='reserved')
        self.assertEqual(422, reserved.status, reserved.data)
        self.assertEqual('reserved_text', reserved.data['error']['code'])

    def test_archived_room_and_project_refuse_item_writes(self):
        self.post_item(self.tokens['worker'])
        self.assertEqual(200, self.request('POST', self.path('/archive'), {},
                                           token=self.tokens['owner']).status)
        posted = self.post_item(self.tokens['worker'], title='late', key='late-item')
        self.assertEqual(409, posted.status, posted.data)
        self.assertEqual(409, self.accept(self.tokens['owner'], key='late-accept').status)

    def test_every_decision_route_is_csrf_protected(self):
        self.post_item(self.tokens['worker'])
        login = self.request('POST', '/v1/sessions',
                             {'username': 'owner', 'password': 'owner-password-1'})
        cookie = next(value for key, value in login.header_list
                      if key.lower() == 'set-cookie').split(';')[0]
        csrf = login.data['session']['csrf_token']

        def post(path, cookie_value=None, csrf_value=None, key=None):
            client = http.client.HTTPConnection('127.0.0.1', self.port, timeout=15)
            headers = {'Content-Type': 'application/json'}
            if cookie_value:
                headers['Cookie'] = cookie_value
            if csrf_value:
                headers['X-CSRF-Token'] = csrf_value
            if key:
                headers['Idempotency-Key'] = key
            try:
                client.request('POST', path, body='{}', headers=headers)
                response = client.getresponse()
                return agents.Response(response.status, response.getheaders(), response.read())
            finally:
                client.close()

        refused = post(self.path('/items/1/accept'), cookie_value=cookie, key='csrf-refused')
        self.assertEqual(403, refused.status, refused.data)
        allowed = post(self.path('/items/1/accept'), cookie_value=cookie, csrf_value=csrf,
                       key='csrf-allowed')
        self.assertEqual(200, allowed.status, allowed.data)


if __name__ == '__main__':
    unittest.main()


class RoomsItemsPageTests(unittest.TestCase):
    def test_item_panel_renders_text_only_and_follows_authority(self):
        import shutil
        node = shutil.which('node')
        if not node:
            self.skipTest('node is not installed')
        import test_http_web
        kit = Path(__file__).resolve().parents[1]
        done = test_http_web.run_node_module(
            self, node, 'await import(process.argv[1])',
            (kit / 'tests' / 'web_rooms_items.mjs').as_uri(),
            (kit / 'tests' / 'web_dom_shim.mjs').as_uri(),
            (kit / 'web' / 'js' / 'views' / 'rooms.js').as_uri())
        self.assertEqual(0, done.returncode, done.stderr[-3000:])
        seen = json.loads(done.stdout.strip().splitlines()[-1])
        for role in ('viewer', 'contributor', 'coordinator'):
            self.assertIn('<img src=x onerror=alert(1)>', seen[role]['text'])
            self.assertIn('javascript:alert(1)', seen[role]['text'])
            self.assertEqual(0, seen[role]['images'])
            self.assertEqual(0, seen[role]['js_links'])
        # A contributor may accept a requirement (it becomes a proposal); an owner/
        # coordinator sees the same button; a viewer only answers the open question.
        self.assertIn('Accept', seen['contributor']['buttons'])
        self.assertIn('Accept', seen['coordinator']['buttons'])
        self.assertNotIn('Accept', seen['viewer']['buttons'])
        self.assertIn('Answer', seen['viewer']['buttons'])
        self.assertEqual(0, seen['viewer']['forms'])
        self.assertEqual(2, seen['contributor']['forms'])
        self.assertGreaterEqual(seen['coordinator']['forms'], 3)
        self.assertIn('orchestra:room/r-0123456789ab#1', seen['contributor']['text'])
        self.assertIn('amended', seen['contributor']['text'])
        # Review b1/r3: the page offers Amend on an already amended item (the server applies
        # a second amendment now), and a moderated item's parent job is never shown.
        self.assertIn('Amend', seen['coordinator']['buttons'])
        for role in ('viewer', 'contributor', 'coordinator'):
            self.assertNotIn('job_0001_secret', seen[role]['text'])


if __name__ == '__main__':
    unittest.main()
