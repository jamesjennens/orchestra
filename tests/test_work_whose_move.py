"""`work --mine` says whose move it is, in one sentence (kittrial-5bb.249).

The observed failure: a worker whose delivery got changes requested read the unchanged
review state at every run and reported "no change" for seventeen hours, because the queue
item carried a state and no sentence, and the recurring prompt told it to stop when
nothing had changed. These tests pin the two fields for each review state, the order that
puts the caller's own moves first, the count at the top of the answer, the reconciliation
with the caller's own checkpoint wait, the extended text escaping, and the five served
texts that carry the same rule.
"""
import copy
import re
import sys
import unittest
from pathlib import Path

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parent)]
import briefing
import review_workflow as rw
import work
from requirements import canonical_bytes
from test_briefing import rows, checkpoint, append_checkpoint, TASK, PROJECT
import test_work_checkpoint_wait
import test_work_queue

KIT = Path(__file__).resolve().parents[1]


def add_to(row, payload, comment_id, author='reviewer'):
    """One structured review record on ONE row, not on the fixture's first row."""
    row['comments'].append(dict(id=comment_id, author=author, created_at='2026-09-16T00:00:00Z',
                                text=rw.PREFIX + canonical_bytes(payload).decode()))


def contribution_for(task, operation_id):
    """A contribution record whose own `task` is the row it is written on."""
    return dict(test_work_queue.contribute(), task=task, operation_id=operation_id)


def delivered(row, comment_id='delivery', author='alice/session'):
    add_to(row, contribution_for(row['id'], 'send-' + str(row['id'])), comment_id, author)


def owned(data, actor='alice/session'):
    return work.queue(data, actor, ['--mine'])


class ReviewMoveTests(unittest.TestCase):
    """One review state, one move, one sentence, derived from the state alone."""

    def test_changes_requested_is_the_callers_work_with_the_item_count_and_the_way_back(self):
        data = test_work_queue.reviewed_rows()
        item = owned(data)['items'][0]
        self.assertEqual((item['review_state'], item['whose_move']), ('changes-requested', 'you'))
        for said in ('This is your work now', '1 outstanding review item', '`review %s`' % TASK,
                     'a revision that supersedes this contribution', 'respond to each item'):
            with self.subTest(said=said):
                self.assertIn(said, item['next'])

    def test_two_requested_items_are_counted_exactly_in_the_sentence(self):
        data = test_work_queue.reviewed_rows()
        add_to(data[0], dict(schema_version=1, operation='request-changes', operation_id='review-2',
                             task=TASK, previous='feedback', contribution='delivery', items=[
                                 dict(id='docs', text='Say why.')]), 'feedback-2', 'reviewer')
        item = owned(data)['items'][0]
        self.assertEqual(item['pending_review_items'], 2)
        self.assertIn('2 outstanding review items', item['next'])

    def test_awaiting_review_and_integration_are_somebody_elses_move(self):
        data = rows()
        delivered(data[0])
        item = owned(data)['items'][0]
        self.assertEqual((item['review_state'], item['whose_move']), ('awaiting-review', 'reviewer'))
        self.assertEqual(item['next'], "Nothing is for you to do: the next move is the reviewer's.")
        add_to(data[0], dict(schema_version=1, operation='approve', operation_id='approve-1', task=TASK,
                             previous='delivery', contribution='delivery', summary='Accepted'),
               'approval', 'reviewer')
        item = owned(data)['items'][0]
        self.assertEqual((item['review_state'], item['whose_move']), ('awaiting-integration', 'coordinator'))
        self.assertEqual(item['next'], "Nothing is for you to do: the next move is the coordinator's.")

    def test_an_undelivered_owned_task_is_the_callers_move(self):
        item = owned(rows())['items'][0]
        self.assertEqual((item['review_state'], item['whose_move']), ('none', 'you'))
        self.assertIn('no contribution has been delivered yet', item['next'])

    def test_a_row_that_is_not_the_callers_lane_stays_unknown(self):
        data = rows()
        data[0]['assignee'] = 'bob/session'
        item = work.queue(data, 'alice/session', [])['items'][0]
        self.assertEqual((item['whose_move'], item['next']), ('unknown', work.UNKNOWN_MOVE_TEXT))
        self.assertEqual(work.queue(data, 'alice/session', [])['your_move']['count'], 0)

    def test_a_malformed_chain_shows_no_move_rather_than_guessing_one(self):
        data = rows()
        data[0]['comments'].append(dict(id='bad', text=rw.PREFIX + '{}'))
        item = owned(data)['items'][0]
        self.assertEqual((item['review_state'], item['whose_move'], item['next']),
                         ('error', 'unknown', work.UNKNOWN_MOVE_TEXT))

    def test_a_named_reviewers_own_open_review_is_the_callers_move(self):
        data = rows()
        data[0]['assignee'] = 'bob/session'
        delivered(data[0], author='bob/session')
        add_to(data[0], dict(schema_version=1, operation='request-review', operation_id='ask-1', task=TASK,
                             previous='delivery', contribution='delivery', reviewer='alice/session',
                             summary='Please review'), 'ask', 'bob/session')
        page = owned(data)
        item = page['items'][0]
        self.assertEqual((item['review_request'], item['review_state'], item['whose_move']),
                         (True, 'awaiting-review', 'you'))
        self.assertEqual(item['next'],
                         'This is your review: read `review %s` and record your decision.' % TASK)
        # It IS one of the caller's tasks that needs action, so the count agrees.
        self.assertEqual(page['your_move'], {'count': 1, 'text': '1 of your tasks need your action'})

    def test_the_moves_are_the_four_documented_values_only(self):
        for state in work.WORK_STATES:
            for owned_flag in (True, False):
                move, text = work.review_move(state, TASK, owned_flag)
                with self.subTest(state=state, owned=owned_flag):
                    self.assertIn(move, work.WHOSE_MOVE)
                    self.assertIsInstance(text, str)
                    self.assertTrue(text)


class OrderAndCountTests(unittest.TestCase):
    """The caller's own moves come first, and the answer says how many."""

    def fixture(self):
        data = test_work_queue.reviewed_rows()                       # changes-requested, ours
        undelivered = copy.deepcopy(data[0])
        undelivered.update(id='aaa-undelivered', comments=[], labels=[])   # no contribution
        waiting = copy.deepcopy(data[0])
        waiting.update(id='zzz-waiting', comments=[], labels=[])
        delivered(waiting, author='alice/session')                   # awaiting-review
        data.extend([undelivered, waiting])
        return data

    def test_the_callers_own_moves_sort_first_and_the_count_leads_the_answer(self):
        page = owned(self.fixture())
        self.assertEqual([item['task'] for item in page['items']], [TASK, 'aaa-undelivered', 'zzz-waiting'])
        self.assertEqual([item['whose_move'] for item in page['items']], ['you', 'you', 'reviewer'])
        self.assertEqual(page['your_move'],
                         {'count': 2, 'text': '2 of your tasks need your action'})
        # The count is the queue's, not the page's: a page of one still says two.
        first = work.queue(self.fixture(), 'alice/session', ['--mine', '--limit', '1'])
        self.assertEqual([item['task'] for item in first['items']], [TASK])
        self.assertEqual(first['your_move']['count'], 2)
        self.assertEqual(list(first)[:3], ['owner', 'total', 'your_move'])

    def test_the_review_state_order_survives_inside_the_callers_own_moves(self):
        # A revision request still leads the caller's own undelivered work.
        page = owned(self.fixture())
        self.assertEqual(page['items'][0]['review_state'], 'changes-requested')


class CheckpointWaitReconciliationTests(unittest.TestCase):
    """`next` and `checkpoint_wait` read as one thing (kittrial-5bb.219 addition c)."""

    def fixture(self):
        case = test_work_checkpoint_wait.CheckpointWaitTests(
            'test_missing_invalid_and_forked_histories_remain_distinct_and_unknown_safe')
        return case.fixture()

    def test_a_wait_of_the_callers_own_checkpoint_is_quoted_and_is_not_a_move_of_its_own(self):
        page = owned(self.fixture())
        item = page['items'][0]
        self.assertTrue(item['checkpoint_wait']['active'])
        self.assertEqual(item['whose_move'], 'unknown')
        self.assertEqual(item['next'],
                         'You are waiting for a person, by your own checkpoint: '
                         '"Read the coordinator answer, then run the approved checks."')
        # A task the caller cannot act on never inflates the answer's own-move count.
        self.assertEqual(page['your_move'], {'count': 0, 'text': '0 of your tasks need your action'})

    def test_a_resolved_wait_gives_the_move_back_without_quoting_anything(self):
        data = self.fixture()
        append_checkpoint(data, 'resolved-cp', checkpoint(
            data, open_items=[], resolved=[dict(id='approval', reason='Verified.', evidence='answer')]))
        item = owned(data)['items'][0]
        self.assertFalse(item['checkpoint_wait']['active'])
        self.assertEqual(item['whose_move'], 'you')
        self.assertIn('no contribution has been delivered yet', item['next'])


class EscapedTextTests(unittest.TestCase):
    """(kittrial-5bb.219 addition a) the escaping the documents promise is the escaping done."""

    def test_c0_c1_del_separators_and_bidi_controls_are_escaped(self):
        for character in ('\x00', '\x07', '\x1b', '\x7f', '\x85', '\x9b', '\u2028', '\u2029',
                          '\u202a', '\u202e', '\u2066', '\u2069'):
            with self.subTest(character=repr(character)):
                self.assertEqual(briefing.plain_text('a%sb' % character), 'a\\u%04xb' % ord(character))
        # Tab and newline are legitimate text and stay as they are.
        self.assertEqual(briefing.plain_text('a\tb\nc'), 'a\tb\nc')

    def test_a_checkpoint_text_with_those_characters_reaches_no_answer_raw(self):
        data = rows()
        item = dict(id='wait', kind='blocker', text='person:\u202e (asked \x9b)',
                    source='\u2028plan')
        append_checkpoint(data, 'own-cp', checkpoint(
            data, open_items=[item], next_action='Ask \x1b[31mthe coordinator\u202e first.'))
        row = owned(data)['items'][0]
        for raw in ('\u202e', '\x9b', '\u2028', '\x1b'):
            with self.subTest(raw=repr(raw)):
                self.assertNotIn(raw, repr(row))
        self.assertIn('\\u202e', repr(row))


class ServedRuleTests(unittest.TestCase):
    """The five texts the task names carry the same rule (kittrial-5bb.249 item 2)."""

    TEXTS = ('templates/WORKER_POLL_PROMPT.md', 'templates/WORKER_PROMPT.md',
             'templates/AGENT_POLL_PROMPT.md', 'docs/FINDING_WORK.md', 'docs/WORKER_GUIDE.md')

    def test_every_named_text_says_changes_requested_is_the_workers_own_work(self):
        for name in self.TEXTS:
            text = ' '.join((KIT / name).read_text(encoding='utf-8').split())
            lower = text.lower()
            with self.subTest(text=name):
                self.assertIn('is your work until you deliver a revision', text)
                self.assertIn('never applies', lower)
                self.assertIn('nothing changed', lower)
                self.assertIn('reviewer or the coordinator', text)

    def test_the_recurring_prompts_no_longer_stop_on_nothing_changed_alone(self):
        for name in ('templates/WORKER_POLL_PROMPT.md', 'templates/AGENT_POLL_PROMPT.md'):
            prompt = ' '.join((KIT / name).read_text(encoding='utf-8').split())
            with self.subTest(prompt=name):
                # The old bare instruction is now scoped by the sentence before it.
                self.assertNotIn('If nothing changed since your last run, say so in one line and stop.',
                                 prompt)
                self.assertIn('"nothing changed" never applies to it', prompt)

    def test_the_document_and_the_guide_point_at_the_queue_fields(self):
        for name in ('docs/FINDING_WORK.md', 'docs/WORKER_GUIDE.md'):
            text = ' '.join((KIT / name).read_text(encoding='utf-8').split())
            with self.subTest(text=name):
                self.assertIn('whose_move', text)
                self.assertIn('next', text)


if __name__ == '__main__':
    unittest.main()
