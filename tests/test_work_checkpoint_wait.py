"""Owned checkpoint waits survive the compact recurring work read."""
import copy
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parent)]
import briefing
import work
from test_briefing import rows, checkpoint, append_checkpoint, TASK
import test_work_queue


class CheckpointWaitTests(unittest.TestCase):
    def fixture(self, **changes):
        data = rows()
        item = dict(id='approval', kind='blocker',
                    text='Waiting for person:coordinator to verify the plan.', source='plan-comment')
        append_checkpoint(data, 'own-cp', checkpoint(data, open_items=[item],
                          next_action='Read the coordinator answer, then run the approved checks.', **changes))
        return data

    def read(self, data, actor='alice/session'):
        row = work.queue(data, actor, ['--mine'])['items'][0]
        self.assertIn('checkpoint_wait', row)
        return row['checkpoint_wait']

    def test_own_literal_person_action_and_source_are_visible_without_another_read(self):
        data = self.fixture()
        before = copy.deepcopy(data)
        with patch.object(briefing, 'checkpoint_state', wraps=briefing.checkpoint_state) as parse:
            for _ in range(2):
                wait = self.read(data)
                self.assertEqual(wait['status'], 'recorded')
                self.assertEqual((wait['checkpoint'], wait['author'], wait['is_current'], wait['active']),
                                 ('own-cp', 'alice/session', True, True))
                self.assertEqual(wait['items'][0]['text']['text'],
                                 'Waiting for person:coordinator to verify the plan.')
                self.assertEqual(wait['items'][0]['source']['text'], 'plan-comment')
                self.assertIn('coordinator answer', wait['next_action']['text'])
            self.assertEqual(parse.call_count, 2)  # One linked parse per queue read.
        self.assertEqual(data, before)

    def test_nonblocking_items_do_not_become_waits_or_invent_a_recipient(self):
        data = rows()
        items = [dict(id=k, kind=k, text='Discuss with the owner.', source='discussion')
                 for k in ('question', 'decision', 'correction')]
        items.append(dict(id='dep', kind='dependency', text='Need the missing artifact.', source='build'))
        append_checkpoint(data, 'own-cp', checkpoint(data, open_items=items))
        wait = self.read(data)
        self.assertEqual([x['id'] for x in wait['items']], ['dep'])
        self.assertEqual((wait['total'], wait['omitted']), (1, 0))
        self.assertNotIn('person', wait['items'][0])
        self.assertEqual(wait['items'][0]['text']['text'], 'Need the missing artifact.')

    def test_latest_own_checkpoint_is_explicitly_historical_after_another_author(self):
        data = self.fixture()
        append_checkpoint(data, 'other-cp', checkpoint(data, open_items=[],
                          resolved=[dict(id='approval', reason='Verified.', evidence='answer')]))
        data[0]['comments'][-1]['author'] = 'bob/session'
        wait = self.read(data)
        self.assertEqual(wait['checkpoint'], 'own-cp')
        self.assertFalse(wait['is_current'])
        self.assertFalse(wait['active'])
        self.assertEqual(wait['total'], 1)
        self.assertEqual(work.queue(data, 'alice/session', ['--mine'])['items'][0]['blocking_items'], 0)

    def test_newest_own_checkpoint_resolves_wait_instead_of_retaining_older_text(self):
        data = self.fixture()
        append_checkpoint(data, 'resolved-cp', checkpoint(data, open_items=[],
                          resolved=[dict(id='approval', reason='Verified.', evidence='answer')],
                          next_action='Implement the acknowledged plan.'))
        wait = self.read(data)
        self.assertEqual((wait['checkpoint'], wait['total'], wait['items']), ('resolved-cp', 0, []))
        self.assertEqual(wait['next_action']['text'], 'Implement the acknowledged plan.')

    def test_reassignment_and_exact_http_actor_isolation_never_relabel_another_wait(self):
        data = self.fixture()
        data[0]['assignee'] = 'bob/session'
        self.assertEqual(self.read(data, 'bob/session')['status'], 'none')
        data[0]['assignee'] = 'http/agent/b'
        data[0]['comments'][-1]['author'] = 'http/agent/a'
        self.assertEqual(self.read(data, 'http/agent/b')['status'], 'none')
        # An operator's unfiltered queue is not an owned resume projection.
        wait = work.queue(data, 'operator', [])['items'][0]['checkpoint_wait']
        self.assertEqual(wait['status'], 'not-owned')
        self.assertEqual(wait['items'], [])

    def test_missing_invalid_and_forked_histories_remain_distinct_and_unknown_safe(self):
        self.assertEqual(self.read(rows())['status'], 'none')
        for fork in (False, True):
            data = self.fixture()
            if fork:
                extra = copy.deepcopy(data[0]['comments'][-1]); extra['id'] = 'fork'
                data[0]['comments'].append(extra)
            else:
                data[0]['comments'].append(dict(id='bad', author='alice/session', text=briefing.PREFIX+'{}'))
            wait = self.read(data)
            self.assertEqual((wait['status'], wait['total'], wait['active']), ('unknown', None, None))
            self.assertEqual(wait['items'], [])
            self.assertIsNone(wait['next_action'])

    def test_bounded_projection_escapes_terminal_controls_and_reports_every_omission(self):
        data = rows()
        items = [dict(id='wait-'+str(i), kind='blocker', text='\x1b'*400 if i < 3 else 'Need an answer.',
                      source='\x07'*240 if i < 3 else 'plan')
                 for i in range(100)]
        append_checkpoint(data, 'own-cp', checkpoint(data, open_items=items, next_action='\x1b'*600))
        wait = self.read(data)
        self.assertEqual((len(wait['items']), wait['total'], wait['omitted']), (3, 100, 97))
        self.assertNotIn('\x1b', str(wait)); self.assertNotIn('\x07', str(wait))
        self.assertEqual(len(wait['items'][0]['text']['text']), 400)
        self.assertEqual(wait['items'][0]['text']['omitted_chars'], 2000)
        self.assertEqual(len(wait['next_action']['text']), 600)
        self.assertEqual(wait['next_action']['omitted_chars'], 3000)

    def test_delivered_review_retains_authored_wait_as_context_without_activating_it(self):
        data = self.fixture()
        test_work_queue.add(data, test_work_queue.contribute(), 'delivery', 'alice/session')
        row = work.queue(data, 'alice/session', ['--mine'])['items'][0]
        self.assertEqual(row['review_state'], 'awaiting-review')
        self.assertFalse(row['checkpoint_wait']['active'])
        self.assertEqual(row['checkpoint_wait']['total'], 1)


if __name__ == '__main__':
    unittest.main()
