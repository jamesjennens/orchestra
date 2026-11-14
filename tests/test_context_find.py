"""Permission-first lexical recovery, independently asserted at the public result boundary."""
import copy
import hashlib
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]
import context_find as find
import briefing

PROJECT = 'sample'
ACTOR = 'alice/session1'


def row(task='sample-1', text='orthogonal project selection', status='open', comments=None, **fields):
    return dict({'id': task, 'title': 'Project selection', 'description': text,
                 'acceptance_criteria': '', 'status': status, 'issue_type': 'task', 'labels': [],
                 'comments': list(comments or []), 'created_by': ACTOR,
                 'created_at': '2026-01-02T03:04:05Z', 'updated_at': '2026-02-02T03:04:05Z'}, **fields)


def comment(text, cid='comment-1', author=ACTOR, **fields):
    return dict({'id': cid, 'text': text, 'author': author, 'created_at': '2026-01-02T03:04:05Z'}, **fields)


def search(rows, phrase='orthogonal', args=(), actor=ACTOR, kinds=find.KINDS, **kwargs):
    scope = {'project': PROJECT, 'projects': [PROJECT], 'actor': actor,
             'route': 'confined', 'principal': 'person:alice', 'kinds': list(kinds)}
    return find.search(rows, PROJECT, actor, find.parse_args([phrase, *args], actor), scope, **kwargs)


class SearchTests(unittest.TestCase):
    def test_task_description_is_recovered_on_closed_task_by_default(self):
        data = [row(status='closed')]
        result = search(data)
        self.assertEqual(1, result['total'])
        self.assertTrue(result['items'][0]['closed'])
        self.assertEqual(0, search(data, args=['--state', 'open'])['total'])
        self.assertEqual('data', result['items'][0]['trust'])
        self.assertEqual('task:sample-1', result['items'][0]['pointer'])

    def test_parent_title_is_not_a_false_comment_or_review_match(self):
        data = [row(text='', comments=[comment('An ACK without the keyword')], title='Orthogonal')]
        result = search(data, args=['--kind', 'comment'])
        self.assertEqual(0, result['total'])

    def test_reserved_record_comments_do_not_become_prose_on_either_reader_shape(self):
        for body in ('Kind: capability-entry-v99\northogonal', '\ufeffKind: owner-answer-v1\r\northogonal',
                     'Kind: lane-state-v1\northogonal', 'Kind: contribution-review-v1\northogonal'):
            with self.subTest(body=body):
                result = search([row(text='', comments=[comment(body)])], args=['--kind', 'comment'])
                self.assertEqual(0, result['total'])

    def test_hidden_matching_documents_cannot_change_visible_statistics(self):
        visible = row(comments=[comment('ordinary orthogonal')])
        before = search([visible], kinds=['task'])
        after = search([dict(visible, comments=visible['comments'] + [comment('hidden orthogonal', 'c-2')])], kinds=['task'])
        for key in ('total', 'coverage', 'items', 'cursor'):
            self.assertEqual(before[key], after[key], key)
        self.assertEqual(before['watermark']['generation'], after['watermark']['generation'])

    def test_hidden_reference_rows_do_not_consume_visible_computation_budget(self):
        def virtual_clock():
            ticks = iter(n / 100 for n in range(10000))
            return lambda: next(ticks)
        visible = row()
        hidden = [row('sample-hidden-%d' % n, labels=['reference', 'reference-key:hidden.%d' % n])
                  for n in range(300)]
        before = search([visible], kinds=['task'], clock=virtual_clock())
        after = search(hidden + [visible], kinds=['task'], clock=virtual_clock())
        for key in ('total', 'coverage', 'items', 'cursor', 'stale'):
            self.assertEqual(before[key], after[key], key)
        self.assertEqual(before['watermark']['generation'], after['watermark']['generation'])

    def test_malformed_labels_are_unknown_without_losing_visible_matches(self):
        result = search([row(), row('sample-bad', labels=[{}], malformed=True)])
        self.assertEqual(1, result['total'])
        self.assertEqual('partial', result['stale'])
        self.assertEqual(1, result['coverage']['skipped_malformed'])

    def test_long_comment_chunks_are_one_hit_and_hash_full_body(self):
        body = 'Prefix\n' + 'long data ' * 1100 + ' orthogonal report tail'
        chunks = [comment(body[:5000], body_offset=0, body_total_chars=len(body)),
                  comment(body[5000:], body_offset=5000, body_total_chars=len(body))]
        result = search([row(text='', comments=chunks)], args=['--kind', 'comment'])
        self.assertEqual(1, result['total'])
        item = result['items'][0]
        self.assertEqual(hashlib.sha256(body.encode()).hexdigest(), item['sha256'])
        self.assertIn('orthogonal', item['excerpt']['text'])
        self.assertLessEqual(len(item['excerpt']['text']), 200)

    def test_hidden_review_chunks_do_not_change_visible_comment_statistics(self):
        body = 'Kind: contribution-review-v1\n' + 'orthogonal protected content ' * 30
        chunks = [comment(body[:90], 'hidden-review', body_offset=0, body_total_chars=len(body)),
                  comment(body[90:], 'hidden-review', body_offset=90, body_total_chars=len(body))]
        visible = comment('orthogonal ordinary evidence')
        before = search([row(text='', comments=[visible])], kinds=['comment'])
        after = search([row(text='', comments=[visible, *chunks])], kinds=['comment'])
        for key in ('total', 'coverage', 'items', 'cursor', 'stale'):
            self.assertEqual(before[key], after[key], key)
        self.assertEqual(before['watermark']['generation'], after['watermark']['generation'])

    def test_duplicate_complete_comments_are_one_hit(self):
        c = comment('orthogonal')
        result = search([row(text='', comments=[c, copy.deepcopy(c)])], args=['--kind', 'comment'])
        self.assertEqual(1, result['total'])

    def test_me_means_exact_actor_across_all_readable_tasks(self):
        data = [row('sample-1', text='', status='closed', comments=[comment('orthogonal', author=ACTOR)]),
                row('sample-2', text='', comments=[comment('orthogonal', author='alice/session2')])]
        result = search(data, args=['--kind', 'comment', '--author', 'me'])
        self.assertEqual(['sample-1'], [item['task'] for item in result['items']])

    def test_dates_and_short_task_ids_are_scoped_and_cursor_bound(self):
        data = [row('sample-5bb.12'), row('sample-5bb.13')]
        self.assertEqual(1, search(data, args=['--task', '.12'])['total'])
        self.assertEqual(0, search(data, args=['--since', '2026-01-03'])['total'])
        page = search(data, args=['--limit', '1'])
        self.assertIsNotNone(page['cursor'])
        with self.assertRaisesRegex(ValueError, 'start the search again'):
            search(data, args=['--limit', '1', '--author', ACTOR, '--cursor', page['cursor']])
        with self.assertRaisesRegex(ValueError, 'start the search again'):
            search(data, args=['--limit', '1', '--cursor', page['cursor']], actor='alice/session2')

    def test_write_changes_visible_generation_and_refuses_old_cursor(self):
        data = [row('sample-1'), row('sample-2')]
        page = search(data, args=['--limit', '1'])
        data[0]['description'] += ' changed'
        with self.assertRaisesRegex(ValueError, 'start the search again'):
            search(data, args=['--limit', '1', '--cursor', page['cursor']])

    def test_five_default_twenty_max_and_no_missing_size_page(self):
        data = [row('sample-%02d' % n, text='orthogonal ' + '\U0001f600' * 150) for n in range(24)]
        self.assertEqual(5, len(search(data)['items']))
        got, cursor = [], None
        while True:
            result = search(data, args=['--limit', '20'] + (['--cursor', cursor] if cursor else []))
            self.assertLessEqual(len(find._bytes(result)), 16384)
            got.extend(item['pointer'] for item in result['items'])
            cursor = result['cursor']
            if cursor is None:
                break
        self.assertEqual(24, len(got))
        self.assertEqual(24, len(set(got)))
        with self.assertRaises(ValueError):
            search(data, args=['--limit', '21'])

    def test_answer_byte_cutoff_is_reported_without_losing_the_next_hit(self):
        data = [row('sample-%02d' % n, text='orthogonal ' + '\U0001f600' * 150) for n in range(24)]
        result = search(data, args=['--limit', '20'])
        self.assertLess(len(result['items']), 20)
        self.assertTrue(result['coverage']['truncated'])
        self.assertTrue(result['coverage']['page_truncated'])
        self.assertFalse(result['total_is_lower_bound'])
        self.assertEqual('current', result['stale'])
        next_page = search(data, args=['--limit', '20', '--cursor', result['cursor']])
        self.assertEqual(len(result['items']) + 1, next_page['items'][0]['rank'])

    def test_deterministic_long_text_windows_keep_a_usable_cursor(self):
        data = [row('sample-%02d' % n, text='background ' * 1000 + 'orthogonal') for n in range(7)]
        result = search(data, args=['--limit', '1'])
        self.assertEqual('partial', result['stale'])
        self.assertTrue(result['total_is_lower_bound'])
        self.assertEqual(7, result['coverage']['text_cutoffs'])
        self.assertIsNotNone(result['cursor'])
        next_page = search(data, args=['--limit', '1', '--cursor', result['cursor']])
        self.assertEqual(2, next_page['items'][0]['rank'])
        self.assertNotEqual(result['items'][0]['pointer'], next_page['items'][0]['pointer'])

    def test_every_kind_is_data_and_escapes_before_clip(self):
        dirty = '\x1b\x85\u202e\u2028' * 80 + 'orthogonal'
        result = search([row(text=dirty, title=dirty)])
        self.assertEqual('data', result['items'][0]['trust'])
        self.assertEqual(briefing.clip_context_label(briefing.plain_text(dirty), 200),
                         result['items'][0]['title'])
        for name in ('title', 'excerpt'):
            value = result['items'][0][name]['text']
            self.assertLessEqual(len(value), 200)
            self.assertNotRegex(value, '[\x00-\x1f\x7f-\x9f\u2028\u2029\u202a-\u202e]')
            self.assertNotRegex(value, r'\\u[0-9a-fA-F]{0,3}$')
        text = find.format_text(result)
        self.assertNotIn('\x1b', text)

    def test_find_never_follows_paths_or_pointers(self):
        data = [row(text='orthogonal file:secret/SHA https://example.invalid /private/key')]
        with mock.patch('builtins.open', side_effect=AssertionError('No file read')), \
                mock.patch('subprocess.run', side_effect=AssertionError('No process')):
            result = search(data)
        self.assertEqual(1, result['total'])

    def test_decision_marker_is_only_an_equal_score_prose_hint(self):
        data = [row('sample-1', text='', comments=[comment('orthogonal', 'c-1')]),
                row('sample-2', text='', comments=[comment('Decision: orthogonal', 'c-2')])]
        result = search(data, args=['--kind', 'comment'])
        self.assertEqual(2, result['total'])
        marked = next(item for item in result['items'] if item.get('ranking_hint'))
        self.assertEqual('comment', marked['kind'])
        self.assertEqual('data', marked['trust'])
        self.assertNotIn('accepted', marked)

    def test_time_budget_stops_nonmatching_corpus(self):
        ticks = iter([0, 0.5, 0.8, 1.2, 2.1] + [3] * 200)
        result = search([row('sample-%d' % n, text='nonmatching') for n in range(20)], clock=lambda: next(ticks))
        self.assertEqual('partial', result['stale'])
        self.assertTrue(result['total_is_lower_bound'])
        self.assertIsNone(result['cursor'])
        self.assertIn('computation-time', result['coverage']['cutoffs'])

    def test_document10001_reports_truncated(self):
        # A fixed clock isolates the document cap, not a latency measurement.
        data = [row('sample-%05d' % n, comments=[comment('orthogonal', 'c-%d' % n)])
                for n in range(5001)]
        result = search(data, clock=lambda: 0)
        self.assertEqual(10000, result['coverage']['documents'])
        self.assertIn('documents', result['coverage']['cutoffs'])
        self.assertTrue(result['total_is_lower_bound'])
        self.assertEqual('partial', result['stale'])
        self.assertIsNone(result['cursor'])

    def test_rows10001_reports_truncated_for_nonmatching_corpus(self):
        data = [row('sample-%05d' % n, text='background') for n in range(10001)]
        result = search(data, args=['--item-kind', 'question'], clock=lambda: 0)
        self.assertEqual(10000, result['coverage']['rows_examined'])
        self.assertIn('rows', result['coverage']['cutoffs'])
        self.assertTrue(result['total_is_lower_bound'])
        self.assertIsNone(result['cursor'])

    def test_comments50001_reports_truncated(self):
        data = [row(comments=[comment('background', 'c-%d' % n) for n in range(50001)])]
        result = search(data, args=['--kind', 'comment'], clock=lambda: 0)
        self.assertEqual(50000, result['coverage']['comments_examined'])
        self.assertIn('comments', result['coverage']['cutoffs'])
        self.assertTrue(result['total_is_lower_bound'])
        self.assertIsNone(result['cursor'])

    def test_8192byte_document_cutoff(self):
        result = search([row(text='background ' * 2000 + 'orthogonal')], clock=lambda: 0)
        self.assertEqual(1, result['total'])
        self.assertEqual(1, result['coverage']['text_cutoffs'])
        self.assertTrue(result['coverage']['truncated'])
        self.assertTrue(result['total_is_lower_bound'])
        self.assertEqual('partial', result['stale'])

    def test_exact_native_identifier_precedes_prose_collision(self):
        data = [row('sample-456', text='background'),
                row('sample-other', text='sample-456 ' * 15)]
        result = search(data, phrase='sample-456')
        self.assertTrue(result['items'][0]['exact'])
        self.assertEqual('task:sample-456', result['items'][0]['pointer'])


class TypedReaderTests(unittest.TestCase):
    def checkpoint_rows(self, **changes):
        import test_briefing as fixture
        data = fixture.rows()
        payload = fixture.checkpoint(data, summary='orthogonal current checkpoint', **changes)
        fixture.append_checkpoint(data, 'checkpoint-current', payload)
        return data

    def test_checkpoint_question_filter_uses_current_typed_items(self):
        import test_briefing as fixture
        data = self.checkpoint_rows(open_items=[{'id': 'q1', 'kind': 'question',
                     'source': 'task:trial-task', 'text': 'orthogonal question'}])
        result = search(data, args=['--kind', 'checkpoint', '--item-kind', 'question'])
        self.assertEqual(1, result['total'])
        self.assertEqual('comment:checkpoint-current', result['items'][0]['pointer'])
        payload = fixture.checkpoint(data, summary='orthogonal settled', open_items=[],
                  resolved=[{'id': 'q1', 'reason': 'Answered', 'evidence': 'comment:answer'}])
        fixture.append_checkpoint(data, 'checkpoint-next', payload)
        self.assertEqual(0, search(data, args=['--kind', 'checkpoint', '--item-kind', 'question'])['total'])
        current = search(data, args=['--kind', 'checkpoint'])
        self.assertEqual(['comment:checkpoint-next'], [i['pointer'] for i in current['items']])

    def test_broken_newest_checkpoint_is_partial_without_old_fallback(self):
        data = self.checkpoint_rows()
        data[0]['comments'].append(comment(briefing.PREFIX + '{bad', 'damaged'))
        result = search(data, args=['--kind', 'checkpoint'])
        self.assertEqual(0, result['total'])
        self.assertEqual('partial', result['stale'])
        self.assertTrue(result['total_is_lower_bound'])

    def test_deep_large_or_duplicate_checkpoint_json_is_unknown_without_old_fallback(self):
        for source in ('[' * 300 + '0' + ']' * 300,
                       '{"summary":"orthogonal","summary":"different"}',
                       '{"summary":"' + 'x' * 2000000):
            with self.subTest(length=len(source)):
                data = self.checkpoint_rows()
                data[0]['comments'].append(comment(briefing.PREFIX + source, 'damaged-json'))
                result = search(data, args=['--kind', 'checkpoint'])
                self.assertEqual(0, result['total'])
                self.assertEqual('partial', result['stale'])
                self.assertTrue(result['total_is_lower_bound'])
                self.assertIsNone(result['cursor'])

    def test_incomplete_checkpoint_chunks_do_not_expose_older_checkpoint(self):
        data = self.checkpoint_rows()
        body = briefing.PREFIX + '{"summary":"orthogonal new checkpoint"}'
        data[0]['comments'].append(comment(body[:35], 'incomplete-new',
                                           body_offset=0, body_total_chars=len(body)))
        result = search(data, args=['--kind', 'checkpoint'])
        self.assertEqual(0, result['total'])
        self.assertEqual('partial', result['stale'])
        self.assertIsNone(result['cursor'])

    def test_task_related_context_is_typed_same_task_and_not_counted(self):
        import test_briefing as fixture
        data = self.checkpoint_rows()
        data[0]['description'] = 'orthogonal project selection'
        fixture.add_review(data, fixture.contribution('a' * 40), 'delivery')
        fixture.add_review(data, fixture.approval('delivery'), 'approval', author='reviewer')
        data[0]['comments'].append(comment('This looks like a report but does not match', 'prose-report'))
        result = search(data, args=['--kind', 'task'])
        self.assertEqual(1, result['total'])
        related = result['items'][0]['related_context']
        self.assertEqual(['comment:checkpoint-current', 'comment:delivery', 'comment:approval'],
                         [p['pointer'] for p in related])
        self.assertTrue(all(p['trust'] == 'data' and p['relation'] == 'related-context' for p in related))
        self.assertNotIn('prose-report', json.dumps(related))

    def test_related_context_does_not_bypass_kind_permission(self):
        data = self.checkpoint_rows()
        data[0]['description'] = 'orthogonal'
        result = search(data, args=['--kind', 'task'], kinds=['task'])
        self.assertNotIn('related_context', result['items'][0])
        self.assertEqual(0, result['coverage']['comments_examined'])

    def test_full_validated_review_ledger_is_not_the_twenty_round_display(self):
        import test_briefing as fixture
        data = fixture.rows()
        previous = None
        for number in range(24):
            payload = fixture.contribution(('%040x' % (number + 1)))
            payload.update(operation_id='send-%d' % number, previous=previous, supersedes=previous,
                           summary='orthogonal oldest evidence' if number == 0 else 'New revision')
            current = 'delivery-%d' % number
            fixture.add_review(data, payload, current)
            previous = current
        result = search(data, args=['--kind', 'review'])
        self.assertEqual(1, result['total'])
        self.assertEqual('comment:delivery-0', result['items'][0]['pointer'])

    def test_ack_never_becomes_approval(self):
        result = search([row(text='', comments=[comment('ACK orthogonal APPROVE')])], args=['--kind', 'comment'])
        self.assertEqual(1, result['total'])
        item = result['items'][0]
        self.assertEqual('comment', item['kind'])
        self.assertEqual('open', item['state'])
        self.assertNotIn('contribution', item)
        self.assertNotIn('commit', item)

    def test_merge_slot_event_gate_and_unreadable_rows_never_become_task_prose(self):
        from coordination import MERGE_SLOT_SUFFIX
        data = [row('sample' + MERGE_SLOT_SUFFIX), row('sample-event', issue_type='event'),
                row('sample-gate', issue_type='gate'), row('sample-bad', malformed=True)]
        result = search(data, args=['--kind', 'task'])
        self.assertEqual(0, result['total'])
        self.assertTrue(result['total_is_lower_bound'])

    def test_legacy_requirement_types_use_only_their_own_reader(self):
        for kind in ('requirement', 'brd-section'):
            with self.subTest(kind=kind):
                data = [row(issue_type=kind, comments=[comment('orthogonal requirement evidence')])]
                self.assertEqual(0, search(data, kinds=['task', 'comment'])['total'])

from test_reference_records import ReferenceCase, OPERATOR


class ReferenceReaderTests(ReferenceCase):
    def test_reference_uses_selected_revision_not_raw_record_prose(self):
        self.propose(statement='orthogonal draft evidence')
        result = search(self.native.rows, args=['--kind', 'ref'], operators=[OPERATOR], project_path=self.project)
        self.assertEqual(1, result['total'])
        self.assertEqual('ref:calendar.trading', result['items'][0]['pointer'])
        self.assertEqual('data', result['items'][0]['trust'])
        self.assertEqual('draft', result['items'][0]['trust_class'])
        self.assertEqual(0, search(self.native.rows, args=['--kind', 'comment'])['total'])
        self.assertEqual(0, search(self.native.rows, args=['--kind', 'task'])['total'])
        self.accept(1, self.sha(1))
        accepted = search(self.native.rows, args=['--kind', 'ref'], operators=[OPERATOR], project_path=self.project)
        self.assertEqual('accepted-record', accepted['items'][0]['trust_class'])
        self.revise(expected_sha256=self.sha(2), revision=3, statement='No matching word in pending draft')
        selected = search(self.native.rows, args=['--kind', 'ref'], operators=[OPERATOR], project_path=self.project)
        self.assertEqual(accepted['items'][0]['sha256'], selected['items'][0]['sha256'])

    def test_hidden_reference_does_not_change_task_counts_scores_watermark_or_coverage(self):
        self.propose(statement='orthogonal hidden reference')
        visible = row()
        before = search([visible], kinds=['task'])
        after = search([visible] + self.native.rows, kinds=['task'])
        for key in ('items', 'coverage', 'total'):
            self.assertEqual(before[key], after[key], key)
        self.assertEqual(before['watermark']['generation'], after['watermark']['generation'])

    def test_inert_acceptance_is_not_promoted(self):
        self.propose(statement='orthogonal')
        self.accept(1, self.sha(1))
        result = search(self.native.rows, args=['--kind', 'ref'], operators=[], project_path=self.project)
        self.assertEqual('draft', result['items'][0]['trust_class'])


if __name__ == '__main__':
    unittest.main()
