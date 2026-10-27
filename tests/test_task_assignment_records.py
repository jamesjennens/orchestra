"""Assignment notice integrity and raw-forgery boundaries; no native acceptance claim."""
import copy
import json
import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import task_assignment as assignment
from reserved_comments import check_comment_body
from requirements import canonical_bytes, content_hash


class AssignmentRecords(unittest.TestCase):
    def request(self):
        return dict(schema_version=1, operation_id='assignment-operation-0001',
                    task='trial-1', expected_assignee='worker-old', assignee='worker-new',
                    reason='Finish the remaining coordinator slice',
                    allow_pending_review=False, allow_held_slot=False)

    def notice(self, request=None):
        return assignment.make_notice(request or self.request(), project='trial',
                                      assigned_by='http/coordinator',
                                      at='2026-10-14T16:40:00+00:00')

    def test_closed_request_refuses_body_attribution_and_false_boolean_flags(self):
        good = self.request()
        assignment.validate_request(good, 'trial-1')
        for field in ('actor', 'author', 'project', 'capability', 'path', 'at'):
            with self.subTest(field=field), self.assertRaises(ValueError):
                assignment.validate_request(dict(good, **{field: 'forged'}))
        for flag in ('allow_pending_review', 'allow_held_slot'):
            for value in ('false', 0, None):
                with self.subTest(flag=flag, value=value), self.assertRaises(ValueError):
                    assignment.validate_request(dict(good, **{flag: value}))
        with self.assertRaises(ValueError):
            assignment.validate_request(good, 'trial-other')

    def test_nullable_assignees_are_exact_and_noop_has_no_notice(self):
        for field in ('assignee', 'expected_assignee'):
            assignment.validate_request(dict(self.request(), **{field: None}))
            for value in ('', ' worker-old', '@attachment:0', ['worker-old']):
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    assignment.validate_request(dict(self.request(), **{field: value}))
        with self.assertRaises(ValueError):
            self.notice(dict(self.request(), assignee='worker-old'))

    def test_reason_is_bounded_literal_unicode_and_bad_text_refuses(self):
        reason = '<img src=x onerror=alert(1)> — finish this slice\nSecond line\twith literal data'
        notice = self.notice(dict(self.request(), reason=reason))
        self.assertEqual(reason, notice['reason'])
        for value in ('', ' ', 'x' * 1001, 'a\0b', '\ud800', 'hidden\u202eactor', 'hidden\u200bactor'):
            with self.subTest(value=repr(value)), self.assertRaises(ValueError):
                self.notice(dict(self.request(), reason=value))

    def test_notice_binds_native_author_project_and_task(self):
        notice = self.notice()
        comment = dict(text=assignment.body(notice), author='http/coordinator')
        self.assertEqual(notice, assignment.parse_comment(comment, project='trial', task='trial-1'))
        self.assertIsNone(assignment.parse_comment(comment, project='other', task='trial-1'))
        self.assertIsNone(assignment.parse_comment(comment, project='trial', task='trial-2'))
        self.assertIsNone(assignment.parse_comment(dict(comment, author='forged'), project='trial', task='trial-1'))
        self.assertEqual('This task was taken from you by http/coordinator: '+notice['reason'],
                         assignment.holder_message(notice, 'worker-old'))
        self.assertEqual('Assigned to you by http/coordinator: '+notice['reason'],
                         assignment.holder_message(notice, 'worker-new'))
        self.assertIsNone(assignment.holder_message(notice, 'unrelated'))

    def test_a_stored_notice_with_a_legacy_invisible_reason_still_reads(self):
        notice = self.notice()
        notice['reason'] = 'Stored before the expanded rule\u061c'
        notice['sha256'] = content_hash(notice)
        comment = dict(text=assignment.body(notice), author='http/coordinator')
        self.assertEqual(notice, assignment.parse_comment(comment, project='trial', task='trial-1'))
        with self.assertRaisesRegex(ValueError, 'plain text'):
            self.notice(dict(self.request(), reason='New write\u061c'))

    def test_mutated_hash_duplicate_fields_and_noncanonical_records_are_not_notices(self):
        notice = self.notice()
        changed = copy.deepcopy(notice);changed['reason'] = 'changed'
        for text in (assignment.PREFIX+canonical_bytes(changed).decode(),
                     assignment.PREFIX+json.dumps(notice, indent=2),
                     assignment.body(notice).replace('"schema_version":1', '"schema_version":1,"schema_version":1')):
            self.assertIsNone(assignment.parse_comment(dict(text=text, author='http/coordinator'), project='trial', task='trial-1'))
        wrong = dict(notice, at='2026-10-14T16:40:00');wrong['sha256'] = content_hash(wrong)
        self.assertIsNone(assignment.parse_comment(dict(text=assignment.PREFIX+canonical_bytes(wrong).decode(), author='http/coordinator'), project='trial', task='trial-1'))

    def test_raw_assignment_records_and_future_versions_refuse_all_prefix_variants(self):
        for version in ('v1', 'v99'):
            for leading, newline in (('', '\n'), ('\ufeff', '\r\n')):
                with self.subTest(version=version, leading=leading), self.assertRaises(ValueError):
                    check_comment_body(leading+'Kind: task-assignment-'+version+newline+'{}', 'test')
        check_comment_body('Ordinary prose mentions Kind: task-assignment-v1 later.', 'test')

    def test_notices_follow_only_a_chain_anchored_in_the_actual_native_holder(self):
        first = self.notice()
        second = self.notice(dict(self.request(), operation_id='assignment-operation-0002',
                                  expected_assignee='worker-new', assignee='worker-last'))
        rollback_forgery = self.notice(dict(self.request(), operation_id='assignment-operation-0003',
                                           expected_assignee='worker-last', assignee='forged-holder'))
        comments = [dict(id=str(i), author='http/coordinator', text=assignment.body(value))
                    for i, value in enumerate((first, second, rollback_forgery), 1)]
        issue = dict(id='trial-1', assignee='worker-last', comments=comments)
        self.assertEqual(2, assignment.notices(issue, 'trial', None)['total'])
        self.assertEqual(1, assignment.notices(issue, 'trial', 'worker-old')['total'])
        self.assertEqual(0, assignment.notices(issue, 'trial', 'forged-holder')['total'])
        self.assertEqual(0, assignment.notices(dict(issue, assignee='unrelated-holder'), 'trial', None)['total'])
        self.assertEqual(0, assignment.notices(issue, 'restored-project', None)['total'])



class AssignmentPolicy(unittest.TestCase):
    def fixture(self):
        payload = dict(schema_version=1, operation_id='assign-policy-0001', task='trial-1',
                       expected_assignee='old', assignee='new', reason='Finish this slice',
                       allow_pending_review=False, allow_held_slot=False)
        row = dict(id='trial-1', assignee='old', status='open', labels=[], comments=[], issue_type='task')
        review = dict(review_state='none', contribution=None, pending_requests=['kept'])
        slot = dict(available=True, holder=None, context=None, missing=False, damaged=False)
        return payload, row, review, slot

    def check(self, parts, eligibility=lambda actor: True):
        return assignment.check_change(*parts, eligibility)

    def test_current_assignee_comparison_and_live_target_eligibility_precede_effect(self):
        parts = self.fixture();before = copy.deepcopy(parts)
        called = []
        result = self.check(parts, lambda actor: called.append(actor) or True)
        self.assertTrue(result['changed']);self.assertEqual(['new'], called)
        self.assertEqual(before, parts)
        parts[1]['assignee'] = 'later'
        called.clear()
        with self.assertRaises(assignment.AssignmentError) as raised:
            self.check(parts, lambda actor: called.append(actor) or True)
        self.assertEqual(409, raised.exception.status);self.assertFalse(called)
        parts = self.fixture()
        for verdict, status in [(False, 409), (None, 503), ('yes', 503)]:
            with self.subTest(verdict=verdict), self.assertRaises(assignment.AssignmentError) as raised:
                self.check(parts, lambda actor: verdict)
            self.assertEqual(status, raised.exception.status)

    def test_pending_review_displacement_requires_named_flag_and_retains_items(self):
        for state in ('awaiting-review', 'changes-requested'):
            parts = self.fixture();parts[2].update(review_state=state, contribution={'commit':'source'})
            with self.subTest(state=state), self.assertRaises(assignment.AssignmentError) as raised:
                self.check(parts)
            self.assertEqual(409, raised.exception.status)
            parts[0]['allow_pending_review'] = True;before = copy.deepcopy(parts)
            self.assertTrue(self.check(parts)['displaced_pending_review']);self.assertEqual(before, parts)
            self.assertEqual(['kept'], parts[2]['pending_requests'])

    def test_slot_displacement_needs_its_own_flag_and_does_not_release_the_slot(self):
        parts = self.fixture();parts[3].update(available=False, holder='old',
                                            context=dict(holder='old', task='trial-1', target='source'))
        with self.assertRaises(assignment.AssignmentError) as raised:self.check(parts)
        self.assertEqual(409, raised.exception.status)
        parts[0]['allow_pending_review'] = True
        with self.assertRaises(assignment.AssignmentError):self.check(parts)
        parts[0]['allow_held_slot'] = True;before = copy.deepcopy(parts)
        result = self.check(parts);self.assertTrue(result['displaced_held_slot']);self.assertTrue(result['slot_retained'])
        self.assertEqual(before, parts)

    def test_both_pending_states_require_both_flags_and_unknown_context_is_not_overrideable(self):
        parts = self.fixture();parts[2].update(review_state='changes-requested', contribution={'commit':'source'})
        parts[3].update(available=False, holder='old', context=dict(holder='old', task='trial-1'))
        for flag in ('allow_pending_review', 'allow_held_slot'):
            parts[0][flag] = True
            if not all(parts[0][f] for f in ('allow_pending_review', 'allow_held_slot')):
                with self.assertRaises(assignment.AssignmentError):self.check(parts)
        self.assertTrue(self.check(parts)['changed'])
        parts[3]['context'] = None
        with self.assertRaises(assignment.AssignmentError) as raised:self.check(parts)
        self.assertEqual(503, raised.exception.status)

    def test_unassign_noop_closed_status_and_incomplete_native_reads(self):
        parts = self.fixture();parts[0]['assignee'] = None
        self.assertIsNone(self.check(parts)['assignee'])
        parts = self.fixture();parts[0]['assignee'] = 'old'
        self.assertFalse(self.check(parts)['changed'])
        parts = self.fixture();parts[1]['status'] = 'closed';before = copy.deepcopy(parts)
        self.assertTrue(self.check(parts)['changed']);self.assertEqual(before, parts)
        for index, patch in [(1, {'malformed':True}), (2, {'review_state':'error'}),
                             (3, {'missing':True}), (3, {'damaged':True}),
                             (3, {'available':False})]:
            broken = self.fixture();broken[index].update(patch)
            with self.subTest(index=index, patch=patch), self.assertRaises(assignment.AssignmentError) as raised:
                self.check(broken)
            self.assertEqual(503, raised.exception.status)

    def test_internal_rows_and_unreadable_anchor_comments_cannot_be_assigned(self):
        for patch, status in [({'issue_type':'decision'},422),
                              ({'labels':['gt:slot']},422),
                              ({'labels':['capability'], 'comments':None},503)]:
            parts = self.fixture();parts[1].update(patch)
            with self.subTest(patch=patch):
                with self.assertRaises(assignment.AssignmentError) as raised:
                    self.check(parts)
                self.assertEqual(status, raised.exception.status)


class AssignmentRoster(unittest.TestCase):
    def state(self):
        return dict(projects={'trial':{},'other':{}}, users={
            'owner':dict(id='owner',display_name='Owner',disabled=False),
            'worker':dict(id='worker',display_name='Worker',disabled=False),
            'viewer':dict(id='viewer',display_name='Viewer',disabled=False),
            'foreign':dict(id='foreign',display_name='Foreign',disabled=False),
            'super':dict(id='super',display_name='Superuser',superuser=True,disabled=False)},
            memberships={'trial':{'owner':'owner','worker':'contributor','viewer':'viewer'},
                         'other':{'foreign':'owner'}}, agents={
            'agent-one':dict(id='agent-one',name='Agent',enabled=True,projects=['trial'])},
            credentials={
            'agent-token':dict(user_id='worker',agent_id='agent-one',actor='agent-one',
                               expires_at=2000,revoked=False,scopes=['tasks'],secret_hash='SECRET-HASH'),
            'worker-token':dict(user_id='worker',project_id='trial',actor='registered-worker',name='Worker token',
                                expires_at=2000,revoked=False,scopes=['tasks'],secret_hash='SECRET-TOKEN')})

    def registry(self):
        return dict(records={'registered':dict(actor='session-existing',name='Host worker',
                                             secret='HOST-SECRET')})

    def roster(self, state=None, allowed=lambda *args: True):
        return assignment.eligible_roster(state or self.state(), 'trial', self.registry(),
                                          now=1000, worker_actor_allowed=allowed)

    def test_roster_is_project_scoped_exact_and_secret_free(self):
        values=self.roster();actors={x['actor'] for x in values}
        self.assertEqual({'owner','worker','super','agent-one','registered-worker','session-existing'},actors)
        self.assertNotIn('viewer',actors);self.assertNotIn('foreign',actors)
        self.assertTrue(all(set(x)=={'actor','name','kind'} for x in values))
        self.assertNotIn('SECRET',json.dumps(values));self.assertNotIn('password',json.dumps(values))

    def test_disabled_removed_demoted_agent_project_and_credentials_are_rechecked(self):
        mutations=[lambda s:s['users']['worker'].update(disabled=True),
                   lambda s:s['memberships']['trial'].pop('worker'),
                   lambda s:s['memberships']['trial'].update(worker='viewer')]
        for mutate in mutations:
            state=self.state();mutate(state)
            self.assertTrue({'worker','agent-one','registered-worker'}.isdisjoint({x['actor'] for x in self.roster(state)}))
        for mutate in [lambda s:s['agents']['agent-one'].update(enabled=False),
                       lambda s:s['agents']['agent-one'].update(projects=['other']),
                       lambda s:s['credentials']['agent-token'].update(revoked=True),
                       lambda s:s['credentials']['agent-token'].update(expires_at=1000),
                       lambda s:s['credentials']['agent-token'].update(scopes=['read'])]:
            state=self.state();mutate(state)
            self.assertNotIn('agent-one',{x['actor'] for x in self.roster(state)})

    def test_worker_namespace_requires_actual_current_canonical_standing(self):
        self.assertNotIn('registered-worker',{x['actor'] for x in self.roster(allowed=lambda *args:False)})
        for allowed in (None,lambda *args:None):
            with self.assertRaises(assignment.AssignmentError) as raised:self.roster(allowed=allowed)
            self.assertEqual(503,raised.exception.status)
        self.assertTrue(assignment.standing_allowed(None))
        self.assertFalse(assignment.standing_allowed(dict(returncode=126,authority_status=403)))
        for fault in ('tracker','unreadable-rows','merge-slot','configuration'):
            with self.subTest(fault=fault), self.assertRaises(assignment.AssignmentError) as raised:
                assignment.standing_allowed(dict(returncode=2,fault=fault,stderr='PRIVATE-SECRET'))
            self.assertEqual(503,raised.exception.status)
            self.assertNotIn('PRIVATE-SECRET',str(raised.exception))
        state=self.state();state['credentials']['worker-token']['project_id']='other'
        self.assertNotIn('registered-worker',{x['actor'] for x in self.roster(state)})

    def test_backend_roster_preserves_canonical_unavailable_status_as_a_read_refusal(self):
        from http_service import EndpointBackend, HttpError
        backend = object.__new__(EndpointBackend)
        backend._endpoint = lambda *args, **kwargs: dict(returncode=2, stdout='',
            stderr='The project session roster cannot be read.', assignment_status=503)
        with self.assertRaises(HttpError) as raised:
            backend._run('assignment-roster', 'trial', 'owner', [], authority={'capability':'coordinate'})
        self.assertEqual(503, raised.exception.status)
        self.assertTrue(raised.exception.nothing_done)

    def test_missing_unreadable_roster_fails_closed_and_does_not_create_identities(self):
        state=self.state();before=copy.deepcopy(state);self.roster(state);self.assertEqual(before,state)
        with self.assertRaises(assignment.AssignmentError):
            assignment.eligible_roster(state,'trial',None,now=1000)
        state['memberships']['trial']=[]
        with self.assertRaises(assignment.AssignmentError):self.roster(state)
        state=self.state();agent=state['agents'].pop('agent-one');state['agents']['bad!actor']=agent
        state['credentials']['agent-token'].update(agent_id='bad!actor',actor='bad!actor')
        with self.assertRaises(assignment.AssignmentError) as raised:self.roster(state)
        self.assertEqual(503,raised.exception.status)
        from http_authority import CAP_COORDINATE, required_capability
        self.assertEqual(CAP_COORDINATE,required_capability(dict(action='assignment-roster',args=[])))


if __name__ == '__main__':
    unittest.main()
