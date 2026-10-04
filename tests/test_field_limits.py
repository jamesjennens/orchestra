"""A text field over its limit is refused with the field, its length and the limit, and the
help lists the same limits the validators enforce (kittrial-5bb.97)."""
import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import briefing
import coordination
import field_limits
import handoff
import review_workflow
import work
import test_briefing
import test_coordination

TASK = 'task-1'


class CheckTextTests(unittest.TestCase):
    def test_each_fault_has_its_own_sentence_naming_the_field_and_the_limit(self):
        self.assertEqual(field_limits.check_text('x' * 5, 'summary', 5), 'x' * 5)
        with self.assertRaisesRegex(ValueError, r'^summary: 6 characters, the limit is 5$'):
            field_limits.check_text('x' * 6, 'summary', 5)
        with self.assertRaisesRegex(ValueError, r'^summary: must not be empty \(limit 5 characters\)$'):
            field_limits.check_text('  ', 'summary', 5)
        with self.assertRaisesRegex(ValueError, r'^summary: expected text \(limit 5 characters\)$'):
            field_limits.check_text(7, 'summary', 5)
        with self.assertRaisesRegex(ValueError, r'^summary: must not contain a NUL character'):
            field_limits.check_text('a\x00b', 'summary', 5)
        self.assertEqual(field_limits.check_text('', 'branch', 5, empty=True), '')
        self.assertEqual(field_limits.check_text('a\x00b', 'note', 5, nul=False), 'a\x00b')
        # The length is reported for over-long text even when it is also blank or holds a NUL.
        with self.assertRaisesRegex(ValueError, '9 characters, the limit is 5'):
            field_limits.check_text(' ' * 9, 'summary', 5)


class ReviewRecordTests(unittest.TestCase):
    def base(self, operation, **extra):
        payload = dict(schema_version=1, operation=operation, operation_id='op-1', task=TASK, previous=None)
        payload.update(extra)
        return payload

    def contribution(self, **changes):
        payload = self.base('contribute', repository='ssh://git.example/project', commit='a' * 40,
                            base_commit='b' * 40, summary='Evidence', supersedes=None, follows=None,
                            delivery=dict(kind='remote', remote='ssh://git.example/project', branch='fix'))
        payload.update(changes)
        return payload

    def refused(self, payload, message):
        with self.assertRaises(ValueError) as caught:
            review_workflow.validate(payload, TASK)
        self.assertEqual(str(caught.exception), message)

    def test_the_limits_are_the_documented_ones(self):
        # Literal numbers: a changed limit must change this test, the help and the contract.
        self.assertEqual(review_workflow.TEXT_LIMITS, {
            'summary': 1200, 'repository': 1000, 'remote': 1000, 'branch': 300, 'bundle path': 1000,
            'review text': 1000, 'resolution reason': 1000, 'resolution evidence': 1000, 'assignee snapshot': 300,
            'decline reason': 1000, 'withdraw reason': 1000})
        self.assertEqual((review_workflow.ITEMS_MAX, review_workflow.PAYLOAD_MAX_BYTES), (20, 24000))

    def test_a_field_at_its_limit_passes_and_one_over_is_refused_with_its_length(self):
        review_workflow.validate(self.contribution(summary='x' * 1200), TASK)
        self.refused(self.contribution(summary='x' * 1201), 'summary: 1201 characters, the limit is 1200')
        self.refused(self.contribution(summary=' '), 'summary: must not be empty (limit 1200 characters)')
        self.refused(self.contribution(repository='x' * 1001), 'repository: 1001 characters, the limit is 1000')
        delivery = dict(kind='remote', remote='ssh://git.example/project', branch='b' * 301)
        self.refused(self.contribution(delivery=delivery), 'branch: 301 characters, the limit is 300')
        delivery = dict(kind='bundle', path='p' * 1001, sha256='c' * 64)
        self.refused(self.contribution(delivery=delivery), 'bundle path: 1001 characters, the limit is 1000')
        approve = self.base('approve', contribution='c-1', summary='x' * 1431)
        self.refused(approve, 'summary: 1431 characters, the limit is 1200')

    def test_an_over_long_review_item_is_named_by_index_and_id(self):
        items = [dict(id='first', text='short'), dict(id='second', text='short'),
                 dict(id='docs-and-small', text='x' * 1105)]
        request = self.base('request-changes', contribution='c-1', items=items)
        self.refused(request, 'items[2] (id docs-and-small) review text: 1105 characters, the limit is 1000')
        items[2]['text'] = 'x' * 1000
        review_workflow.validate(request, TASK)
        items[0]['text'] = ''
        self.refused(request, 'items[0] (id first) review text: must not be empty (limit 1000 characters)')

    def test_an_over_long_resolution_is_named_by_index_and_item(self):
        resolutions = [dict(request='r-1', item='first', reason='done', evidence='commit'),
                       dict(request='r-1', item='second', reason='x' * 1001, evidence='commit')]
        respond = self.base('respond', contribution='c-1', resolutions=resolutions)
        self.refused(respond, 'resolutions[1] (item second) resolution reason: 1001 characters, the limit is 1000')
        resolutions[1].update(reason='done', evidence='e' * 2000)
        self.refused(respond, 'resolutions[1] (item second) resolution evidence: 2000 characters, the limit is 1000')
        resolutions[1]['evidence'] = 'e' * 1000
        review_workflow.validate(respond, TASK)

    def test_the_operations_added_by_kittrial_5bb_94_use_the_same_wording(self):
        request = self.base('request-changes', contribution='c-1', summary='x' * 1201,
                            items=[dict(id='first', text='ok', severity='note')])
        self.refused(request, 'summary: 1201 characters, the limit is 1200')
        request.update(summary='x' * 1200, items=[dict(id='first', text='ok'),
                                                   dict(id='second', text='x' * 1001, severity='note')])
        self.refused(request, 'items[1] (id second) review text: 1001 characters, the limit is 1000')
        request['items'][1]['text'] = 'x' * 1000
        review_workflow.validate(request, TASK)
        for operation, extra, field in (
                ('withdraw', {}, 'withdraw reason'),
                ('decline-review', {'request': 'r-1'}, 'decline reason'),
                ('resolve-item', {'request': 'r-1', 'item': 'first'}, 'resolution reason')):
            with self.subTest(operation=operation):
                payload = self.base(operation, contribution='c-1', reason='x' * 1001, **extra)
                self.refused(payload, '%s: 1001 characters, the limit is 1000' % field)
                self.refused(dict(payload, reason=''), '%s: must not be empty (limit 1000 characters)' % field)
                review_workflow.validate(dict(payload, reason='x' * 1000), TASK)
        ask = self.base('request-review', contribution='c-1', reviewer='reviewer@example', summary='x' * 1201)
        self.refused(ask, 'summary: 1201 characters, the limit is 1200')
        review_workflow.validate(dict(ask, summary='x' * 1200), TASK)

    def test_the_count_and_the_payload_size_say_their_numbers(self):
        items = [dict(id='i%d' % n, text='t') for n in range(21)]
        self.refused(self.base('request-changes', contribution='c-1', items=items), 'Require 1..20 review items')
        items = [dict(id='i%d' % n, text='x' * 1000) for n in range(20)] + []
        resolutions = [dict(request='r-1', item='i%d' % n, reason='x' * 1000, evidence='y' * 1000) for n in range(20)]
        with self.assertRaisesRegex(ValueError, r'^Review workflow payload: \d{5} canonical bytes, the limit is 24000 '):
            review_workflow.validate(self.base('respond', contribution='c-1', resolutions=resolutions), TASK)


class CheckpointTests(unittest.TestCase):
    def refused(self, message, **changes):
        data = test_briefing.rows()
        with self.assertRaises(ValueError) as caught:
            briefing.validate_checkpoint(test_briefing.checkpoint(data, **changes), test_briefing.TASK)
        self.assertEqual(str(caught.exception), message)

    def test_the_limits_are_the_documented_ones(self):
        self.assertEqual(dict(briefing.CHECKPOINT_FIELD_LIMITS), {
            'source_commit': 128, 'branch': 200, 'intent': 600, 'acceptance': 1000, 'summary': 1000,
            'next_action': 600})
        self.assertEqual((briefing.CHECKPOINT_TEXT_LIMIT, briefing.CHECKPOINT_SOURCE_LIMIT), (400, 240))

    def test_each_top_level_field_at_its_limit_passes_and_one_over_names_its_length(self):
        data = test_briefing.rows()
        for field, limit in briefing.CHECKPOINT_FIELD_LIMITS:
            with self.subTest(field=field):
                briefing.validate_checkpoint(test_briefing.checkpoint(data, **{field: 'x' * limit}), test_briefing.TASK)
                self.refused('%s: %d characters, the limit is %d' % (field, limit + 1, limit),
                             **{field: 'x' * (limit + 1)})
        self.refused('next_action: must not be empty (limit 600 characters)', next_action=' ')
        self.refused('next_action: expected text (limit 600 characters)', next_action=None)
        # The two fields that may be empty still may.
        briefing.validate_checkpoint(test_briefing.checkpoint(data, source_commit='', branch=''), test_briefing.TASK)

    def test_an_over_long_item_is_named_by_index_and_id(self):
        item = dict(id='blocked-on-keys', kind='blocker', text='x' * 401, source='review')
        self.refused('open_items[1].text (id blocked-on-keys): 401 characters, the limit is 400',
                     open_items=[dict(id='first', kind='question', text='ok', source='me'), item])
        self.refused('open_items[0].source (id blocked-on-keys): 241 characters, the limit is 240',
                     open_items=[dict(item, text='ok', source='s' * 241)])
        self.refused('resolved[0].reason (id done): 401 characters, the limit is 400',
                     resolved=[dict(id='done', reason='r' * 401, evidence='e')])


class HandoffTests(unittest.TestCase):
    def transfer(self, **changes):
        payload = dict(schema_version=1, operation_id='move-1', task='trial-task', from_actor='alice',
                       to_actor='bob', reason='Review follow-up', approval='The owner transfers it')
        payload.update(changes)
        return payload

    def test_reason_and_approval_name_their_length(self):
        self.assertEqual(handoff.TEXT_LIMITS, {'reason': 1000, 'approval': 1000})
        handoff.validate(self.transfer(reason='r' * 1000, approval='a' * 1000))
        for field in ('reason', 'approval'):
            with self.assertRaisesRegex(ValueError, '^%s: 1001 characters, the limit is 1000$' % field):
                handoff.validate(self.transfer(**{field: 'x' * 1001}))
            with self.assertRaisesRegex(ValueError, r'^%s: must not be empty' % field):
                handoff.validate(self.transfer(**{field: ''}))
        request = dict(schema_version=1, operation='request', task='trial-task', from_actor='alice', to_actor='bob',
                       request_id='11111111-1111-1111-1111-111111111111', reason='x' * 1200)
        with self.assertRaisesRegex(ValueError, '^reason: 1200 characters, the limit is 1000$'):
            handoff.validate_request(request)
        handoff.validate_request(dict(request, reason='x' * 1000))

    def test_a_disposition_names_a_bad_choice_and_a_long_reason_separately(self):
        with tempfile.TemporaryDirectory() as folder:
            payload = dict(schema_version=1, operation='disposition', operation_id='d-1', task='trial-task',
                           request_id='11111111-1111-1111-1111-111111111111', disposition='maybe',
                           reason='because', supersedes=None)
            with self.assertRaisesRegex(ValueError, 'expected accept, decline, withdraw or supersede'):
                handoff.disposition(Path(folder), 'alice', payload, None)
            with self.assertRaisesRegex(ValueError, '^reason: 1001 characters, the limit is 1000$'):
                handoff.disposition(Path(folder), 'alice', dict(payload, disposition='decline', reason='x' * 1001), None)


class HelpTests(unittest.TestCase):
    """The help lists the validators' own numbers: one changed without the other fails here."""

    def numbers(self, limits):
        return {name: int(value.split()[1]) for name, value in limits.items() if value.startswith('<= ')
                and value.endswith(' characters')}

    def test_review_help(self):
        limits = work.help_payload('review')['limits']
        self.assertEqual(self.numbers(limits), review_workflow.TEXT_LIMITS)
        self.assertEqual(limits['items / resolutions'], '1..20 per record')
        self.assertEqual(limits['payload'], '<= 24 KB canonical bytes')
        self.assertEqual(limits['summary'], '<= 1200 characters')
        self.assertEqual(limits['review text'], '<= 1000 characters')
        self.assertTrue(any('names which one' in note for note in work.help_payload('review')['notes']))

    def test_checkpoint_help(self):
        limits = work.help_payload('checkpoint')['limits']
        self.assertEqual({name: number for name, number in self.numbers(limits).items()
                          if name in dict(briefing.CHECKPOINT_FIELD_LIMITS)}, dict(briefing.CHECKPOINT_FIELD_LIMITS))
        self.assertEqual(limits['next_action'], '<= 600 characters')
        self.assertEqual(limits['item text/reason'], '<= 400 characters')
        self.assertEqual(limits['open_items'], '<= 100')

    def test_handoff_help(self):
        self.assertEqual(work.help_payload('handoff')['limits'],
                         {'reason': '<= 1000 characters', 'approval': '<= 1000 characters'})

    def test_every_help_limit_is_enforced_at_exactly_that_number(self):
        # The help's number, read back and used as the input length: it passes; one more fails.
        review = ReviewRecordTests()
        limit = self.numbers(work.help_payload('review')['limits'])['summary']
        review_workflow.validate(review.contribution(summary='x' * limit), TASK)
        with self.assertRaises(ValueError):
            review_workflow.validate(review.contribution(summary='x' * (limit + 1)), TASK)
        limit = self.numbers(work.help_payload('handoff')['limits'])['reason']
        handoffs = HandoffTests()
        handoff.validate(handoffs.transfer(reason='x' * limit))
        with self.assertRaises(ValueError):
            handoff.validate(handoffs.transfer(reason='x' * (limit + 1)))
        limit = self.numbers(work.help_payload('checkpoint')['limits'])['next_action']
        data = test_briefing.rows()
        briefing.validate_checkpoint(test_briefing.checkpoint(data, next_action='x' * limit), test_briefing.TASK)
        with self.assertRaises(ValueError):
            briefing.validate_checkpoint(test_briefing.checkpoint(data, next_action='x' * (limit + 1)),
                                         test_briefing.TASK)


class MergeRequestTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.project = Path(temp.name)
        self.native = test_coordination.Native()

    def refused(self, payload):
        with self.assertRaises(ValueError) as caught:
            coordination.apply_native(copy.deepcopy(payload), 'alice', self.native, self.project)
        return str(caught.exception)

    def test_merge_check_with_a_target_names_the_unexpected_field(self):
        message = self.refused({'operation': 'merge-check', 'target': 'main@commit1'})
        self.assertIn('Invalid merge request fields for merge-check: unexpected field(s): target.', message)
        self.assertIn('merge-acquire takes operation, task and target', message)
        self.assertIn('take only operation', message)
        message = self.refused({'operation': 'merge-release', 'task': 'sample-task', 'target': 'main@commit1'})
        self.assertIn('for merge-release: unexpected field(s): target, task.', message)
        message = self.refused({'operation': 'merge-acquire', 'task': 'sample-task'})
        self.assertIn('for merge-acquire: missing field(s): target.', message)
        message = self.refused({'operation': 'merge-acquire', 'task': 'sample-task', 'holder': 'bob'})
        self.assertIn('unexpected field(s): holder; missing field(s): target.', message)
        self.assertEqual(self.native.count('merge-slot'), 0)

    def test_the_error_cannot_echo_a_large_payload(self):
        payload = {'operation': 'merge-check'}
        payload.update({'field-%03d-%s' % (index, 'x' * 500): 1 for index in range(200)})
        message = self.refused(payload)
        self.assertLess(len(message), 900)
        self.assertEqual(message.count('field-'), 8)


@unittest.skipIf(sys.platform == 'win32', 'endpoint.py uses the POSIX coordination lock')
class CommentsListTests(unittest.TestCase):
    def test_comments_list_is_refused_with_a_sentence_and_no_native_call(self):
        import endpoint
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            project = root / 'projects' / 'example'
            (project / '.beads').mkdir(parents=True)
            (project / '.beads' / 'metadata.json').write_text('{}', encoding='utf-8')
            with patch.object(endpoint.subprocess, 'run', side_effect=AssertionError('no native call expected')):
                for args in (['comments', 'list'], ['comments', 'list', '--json'], ['comments', 'list', 'example-1']):
                    with self.subTest(args=args), self.assertRaises(ValueError) as caught:
                        endpoint.execute(root, {'project': 'example', 'actor': 'alice', 'action': 'bd', 'args': args})
                    self.assertIn('There is no `comments list`. Use `comments TASK`', str(caught.exception))


if __name__ == '__main__':
    unittest.main()
