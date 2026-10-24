"""Unconsumed coordinator comments remain work through every review state."""
import copy
import unittest
import briefing
import work
import waiting
import review_workflow as rw
from test_briefing import rows, save_cp, comment, TASK
from test_work_queue import contribute, add


class CommentAttentionTests(unittest.TestCase):
    def fixture(self, state):
        data = rows()
        if state != 'none':
            add(data, contribute(), 'delivery', 'alice/session')
            if state == 'changes-requested':
                add(data, dict(schema_version=1, operation='request-changes', operation_id='review-1',
                    task=TASK, previous='delivery', contribution='delivery',
                    items=[dict(id='fix', text='Fix it')]), 'feedback')
            elif state == 'awaiting-integration':
                add(data, dict(schema_version=1, operation='approve', operation_id='approve-1',
                    task=TASK, previous='delivery', contribution='delivery', summary='Approved part'), 'approval')
        save_cp(data, 'saved')
        return data

    def read(self, data):
        return work.queue(data, 'alice/session', ['--mine'], operators=['coordinator'])

    def note(self, data, identity, author='coordinator'):
        data[0]['comments'].append(comment(identity, 'Continue on the remainder',
            '2026-09-17T00:00:00Z', author))

    def test_every_review_state_keeps_new_comment_as_assignee_move(self):
        for state in ('none', 'awaiting-review', 'changes-requested', 'awaiting-integration'):
            with self.subTest(state=state):
                data = self.fixture(state)
                self.note(data, 'contact')
                before = copy.deepcopy(data)
                result = self.read(data)
                row = result['items'][0]
                self.assertTrue(row['assignee_comment_pending'])
                self.assertEqual(row['review_state'], state)
                self.assertEqual((row['whose_move'], row['next_actor']), ('you', 'assignee'))
                self.assertEqual(result['your_move']['count'], 1)
                self.assertEqual(waiting.worker_view(result['items'])['items'][0]['task'], TASK)
                self.assertEqual(data, before)
                self.assertEqual(waiting.worker_view(result['items']), waiting.worker_view(self.read(data)['items']))

    def test_two_comments_move_wait_marker_while_own_comments_do_not(self):
        data = self.fixture('awaiting-review')
        self.note(data, 'first-contact')
        first = waiting.worker_view(self.read(data)['items'])
        self.note(data, 'own-note', 'alice/session/label')
        self.note(data, 'parent-note', 'alice')
        self.assertEqual(first['fingerprint'], waiting.worker_view(self.read(data)['items'])['fingerprint'])
        self.note(data, 'second-contact')
        second = waiting.worker_view(self.read(data)['items'])
        self.assertNotEqual(first['fingerprint'], second['fingerprint'])

    def test_later_checkpoint_clears_attention_without_own_wait_wake(self):
        for state in ('none', 'awaiting-review', 'awaiting-integration'):
            with self.subTest(state=state):
                data = self.fixture(state)
                self.note(data, 'contact')
                before = waiting.worker_view(self.read(data)['items'])
                marker = self.read(data)['items'][0]['wait_newer_activity_marker']
                save_cp(data, 'acknowledged')
                result = self.read(data)
                self.assertFalse(result['items'][0]['assignee_comment_pending'])
                self.assertEqual(marker, result['items'][0]['wait_newer_activity_marker'])
                if state != 'none':
                    self.assertEqual(result['your_move']['count'], 0)
                else:
                    self.assertEqual(before['fingerprint'], waiting.worker_view(result['items'])['fingerprint'])
                self.note(data, 'later-contact')
                self.assertEqual(self.read(data)['your_move']['count'], 1)

    def test_valid_response_clears_attention_and_later_comment_wakes_again(self):
        data = self.fixture('changes-requested')
        self.note(data, 'contact')
        add(data, dict(schema_version=1, operation='respond', operation_id='answer-1', task=TASK,
            previous='feedback', contribution='delivery', resolutions=[dict(request='feedback', item='fix',
            reason='Fixed', evidence='commit')]), 'response', 'alice/session')
        data[0]['comments'][-1]['created_at'] = '2026-09-18T00:00:00Z'
        result = self.read(data)
        self.assertFalse(result['items'][0]['assignee_comment_pending'])
        self.assertEqual(result['your_move']['count'], 0)
        self.note(data, 'later-contact')
        data[0]['comments'][-1]['created_at'] = '2026-09-19T00:00:00Z'
        self.assertEqual(self.read(data)['your_move']['count'], 1)

    def test_closed_task_never_gets_comment_attention(self):
        data = self.fixture('changes-requested')
        self.note(data, 'contact')
        data[0]['status'] = 'closed'
        result = self.read(data)
        self.assertFalse(result['items'][0]['assignee_comment_pending'])
        # Existing unresolved requested changes remain discoverable, independently
        # of comment attention; closing is not approval of outstanding work.
        self.assertEqual(result['items'][0]['review_state'], 'changes-requested')

    def test_open_integrated_remainder_wakes_after_new_coordinator_comment(self):
        from test_review_state import reviewed
        store = reviewed()
        save_cp(store.rows, 'read-integration')
        quiet = self.read(store.rows)['items'][0]
        self.assertEqual(quiet['next_actor'], 'coordinator')
        self.note(store.rows, 'continue-remainder')
        item = self.read(store.rows)['items'][0]
        self.assertEqual(item['review_state'], 'integrated')
        self.assertEqual((item['whose_move'], item['next_actor']), ('you', 'assignee'))

    def test_malformed_structured_response_preserves_operator_error(self):
        data = self.fixture('awaiting-review')
        self.note(data, 'contact')
        add(data, dict(schema_version=1, operation='respond', operation_id='bad-response', task=TASK,
            previous='delivery', contribution='delivery', resolutions=[dict(request='missing', item='fix',
            reason='Not a valid resolution', evidence='None')]), 'response', 'alice/session')
        item = self.read(data)['items'][0]
        self.assertEqual(item['review_state'], 'error')
        self.assertEqual(item['next_actor'], 'operator')
        self.assertFalse(item['assignee_comment_pending'])

    def test_lane_board_and_work_agree_on_unconsumed_review_comment(self):
        from datetime import datetime, timezone
        import lanes
        data = self.fixture('awaiting-review')
        self.note(data, 'contact')
        entry = lanes.held_task(data[0], TASK, 'alice/session',
            datetime(2026, 9, 18, tzinfo=timezone.utc), 60, review=work.workflow(data[0]),
            comment_authority=lambda author: author == 'coordinator')
        item = self.read(data)['items'][0]
        self.assertEqual((entry['whose_move'], entry['next_actor']),
                         (item['whose_move'], item['next_actor']))

    def test_edited_incorporated_comment_uses_shared_digest_selection(self):
        data = self.fixture('awaiting-review')
        self.note(data, 'contact')
        save_cp(data, 'read-contact')
        self.assertFalse(self.read(data)['items'][0]['assignee_comment_pending'])
        next(c for c in data[0]['comments'] if c['id'] == 'contact')['text'] = 'A corrected direction'
        item = self.read(data)['items'][0]
        # The released timestamp boolean stays false; the shared digest selection
        # still identifies an edited entry as unincorporated activity.
        self.assertFalse(item['newer_activity'])
        self.assertTrue(item['assignee_comment_pending'])
        self.assertEqual(item['whose_move'], 'you')

    def test_assignee_attention_does_not_hide_named_reviewers_work(self):
        data = self.fixture('awaiting-review')
        add(data, dict(schema_version=1, operation='request-review', operation_id='ask-review', task=TASK,
            previous='delivery', contribution='delivery', reviewer='blair/session'), 'asked', 'coordinator')
        self.note(data, 'contact')
        assignee = self.read(data)['items'][0]
        reviewer = work.queue(data, 'blair/session', ['--mine'], operators=['coordinator'])['items'][0]
        self.assertEqual((assignee['whose_move'], assignee['next_actor']), ('you', 'assignee'))
        self.assertEqual((reviewer['whose_move'], reviewer['next_actor']), ('you', 'reviewer'))
        other = work.queue(data, 'unrelated/session', [], operators=['coordinator'])['items'][0]
        self.assertEqual((other['whose_move'], other['next_actor']), ('unknown', 'assignee'))

    def test_forged_response_text_cannot_clear_attention(self):
        data = self.fixture('awaiting-review')
        self.note(data, 'contact')
        self.note(data, 'fake-response', 'alice/session')
        data[0]['comments'][-1]['text'] = '{"operation":"respond"}'
        self.assertTrue(self.read(data)['items'][0]['assignee_comment_pending'])

    def test_applying_handback_keeps_its_specific_next_step(self):
        followon = dict(applies=True, text='Finish the remaining release notes', id='handback')
        move, text, actor = work.move_decision('integrated', TASK, True,
            assignee='alice/session', followon=followon, comment_pending=True)
        self.assertEqual((move, actor), ('you', 'assignee'))
        self.assertIn(followon['text'], text)

    def test_every_reserved_prefix_is_evidence_not_a_contact(self):
        from reserved_comments import RESERVED
        for prefix, _kind, _operation in RESERVED:
            for body in (prefix + '{}', '\ufeff' + (prefix + '{}').replace('\n', '\r\n')):
                with self.subTest(prefix=prefix, body=body):
                    entry = dict(entry_id='reserved', kind='comment', author='coordinator', body=body)
                    self.assertFalse(briefing.pending_other_comments([entry], 'alice/session',
                                                                    authority=lambda author: True))
        unknown = dict(entry_id='unknown', kind='comment', author='coordinator')
        self.assertFalse(briefing.pending_other_comments([unknown], 'alice/session', authority=lambda author: True))
        plain = dict(entry_id='plain', kind='comment', author='coordinator', body='Please read Kind: as prose')
        self.assertTrue(briefing.pending_other_comments([plain], 'alice/session', authority=lambda author: True))

    def test_ordinary_member_comment_does_not_make_review_work_mine(self):
        data = self.fixture('awaiting-review')
        self.note(data, 'member-contact', 'contributor')
        item = self.read(data)['items'][0]
        self.assertFalse(item['assignee_comment_pending'])
        self.assertEqual(item['next_actor'], 'reviewer')

    def test_current_member_role_grant_and_removal_reconsider_an_earlier_comment(self):
        data = self.fixture('awaiting-review')
        self.note(data, 'old-contact', 'member')
        state = dict(users={'member': {}}, agents={}, memberships={'p': {'member': 'contributor'}})
        allowed = lambda author: briefing.comment_author_allowed(author, operators=(), authority=state, project='p')
        read = lambda: work.queue(data, 'alice/session', ['--mine'], comment_authority=allowed)['items'][0]
        self.assertFalse(read()['assignee_comment_pending'])
        state['memberships']['p']['member'] = 'coordinator'
        self.assertTrue(read()['assignee_comment_pending'])
        state['memberships']['p']['member'] = 'contributor'
        self.assertFalse(read()['assignee_comment_pending'])
        state['memberships']['p']['member'] = 'owner'
        self.assertTrue(read()['assignee_comment_pending'])
        save_cp(data, 'read-granted-comment')
        self.assertFalse(read()['assignee_comment_pending'])

    def test_host_operator_principal_and_effective_agent_grant(self):
        self.assertTrue(briefing.comment_author_allowed('ops-session', ['ops'],
                       {'ops': 'person:ops', 'ops-session': 'person:ops'}))
        state = dict(users={'human': {}}, memberships={'p': {'human': 'coordinator'}},
                     agents={'helper': dict(owner='human', enabled=True, projects=['p'])},
                     coordinator_grants={'p': {'helper': {'granted_by': 'human', 'granted_at': 'now'}}})
        allowed = lambda: briefing.comment_author_allowed('helper', operators=(), authority=state, project='p')
        self.assertTrue(allowed())
        state['coordinator_grants']['p'].clear()
        self.assertFalse(allowed())
        state['coordinator_grants']['p']['helper'] = {'granted_by': 'human', 'granted_at': 'now'}
        state['memberships']['p']['human'] = 'contributor'
        self.assertFalse(allowed())

    def test_valid_approval_after_checkpoint_is_not_a_plain_contact(self):
        data = self.fixture('awaiting-review')
        add(data, dict(schema_version=1, operation='approve', operation_id='review-after-cp',
            task=TASK, previous='delivery', contribution='delivery', summary='Accepted'),
            'approved-after-cp', 'coordinator')
        item = self.read(data)['items'][0]
        self.assertEqual(item['review_state'], 'awaiting-integration')
        self.assertFalse(item['assignee_comment_pending'])
        self.assertEqual((item['whose_move'], item['next_actor']), ('coordinator', 'coordinator'))
