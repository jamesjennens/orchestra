"""The project's merge slot is an internal record, never work (kittrial-5bb.113).

On real bd the slot is a row of type ``task`` with the id ``PROJECT-merge-slot`` and the
label ``gt:slot``. The kit skipped rows of type ``merge-slot``, which never matched, so the
slot was listed by ``work`` and offered to agents as a claimable task.
"""
import copy
import json
import sys
import unittest
from pathlib import Path

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT))
sys.path.insert(0, str(KIT / 'tests'))
import briefing
import coordination
import review_workflow
import work
import test_http_review_fixes as fixes
from test_briefing import PROJECT, TASK, checkpoint, rows
from test_work_queue import contribute

SLOT = PROJECT + '-merge-slot'
SENTENCE = "is the project's merge slot, an internal record, not a task"


def slot_row(slot_id=SLOT, **changes):
    """The row bd 1.2.2 makes for a merge slot."""
    row = dict(id=slot_id, title='Merge Slot', issue_type='task', status='open', assignee=None,
               labels=['gt:slot'], comments=[], description='', acceptance_criteria='')
    row.update(changes)
    return row


class PredicateTests(unittest.TestCase):
    def test_the_slot_as_bd_makes_it_and_the_older_type(self):
        self.assertTrue(coordination.is_merge_slot(slot_row()))
        self.assertTrue(coordination.is_merge_slot({'id': 'anything', 'issue_type': 'merge-slot'}))

    def test_the_rule_is_the_exact_id_alone(self):
        # The label is what an accident removes: without it the row is still the slot (review 01a10c0b).
        self.assertTrue(coordination.is_merge_slot(slot_row(labels=[])))
        self.assertTrue(coordination.is_merge_slot(slot_row(labels=['bug'])))
        # A row that only carries the label, or whose id only ends that way, is a task.
        self.assertFalse(coordination.is_merge_slot(slot_row(slot_id=PROJECT + '-abc')))
        self.assertFalse(coordination.is_merge_slot(slot_row(slot_id=PROJECT + '-x-merge-slot')))
        self.assertFalse(coordination.is_merge_slot(slot_row(slot_id=SLOT + '.1')))
        self.assertFalse(coordination.is_merge_slot(slot_row(slot_id='merge-slot-notes')))
        for value in (None, 'row', [], {'id': 7, 'labels': ['gt:slot']}, {'labels': ['gt:slot']}):
            self.assertFalse(coordination.is_merge_slot(value))

    def test_the_sentence_names_the_record(self):
        self.assertEqual(coordination.merge_slot_sentence(SLOT), SLOT + ' ' + SENTENCE)


class CanonicalTests(unittest.TestCase):
    """The views and writes over SSH."""

    def test_work_does_not_list_the_slot(self):
        data = rows()
        data[0]['assignee'] = None
        data[0]['status'] = 'open'
        with_slot = data + [slot_row()]
        listed = [item['task'] for item in work.queue(with_slot, 'alice/session', [])['items']]
        self.assertEqual(listed, [TASK])
        self.assertEqual(work.queue(with_slot, 'alice/session', [])['total'], 1)
        # A slot that lost its label is still not work.
        self.assertEqual([item['task'] for item in work.queue(data + [slot_row(labels=[])], 'alice/session', [])['items']],
                         [TASK])
        # A task that only carries the label is still work.
        labelled = copy.deepcopy(data[0]); labelled.update(id=PROJECT + '-zzz', labels=['gt:slot'])
        listed = [item['task'] for item in work.queue(data + [labelled], 'alice/session', [])['items']]
        self.assertEqual(sorted(listed), sorted([TASK, PROJECT + '-zzz']))

    def test_a_claimed_slot_is_not_the_claimers_work_either(self):
        data = rows() + [slot_row(assignee='alice/session', status='in_progress')]
        self.assertEqual([item['task'] for item in work.queue(data, 'alice/session', ['--mine'])['items']], [TASK])

    def test_a_checkpoint_on_the_slot_is_refused_before_any_write(self):
        data = [slot_row(status='in_progress', assignee='alice/session')]
        payload = dict(schema_version=1, task=SLOT, previous=None,
                       activity_cursor=briefing.activity_cursor(briefing.snapshot(data, PROJECT, SLOT)),
                       source_commit='', branch='', intent='i', acceptance='a', summary='s', next_action='n',
                       open_items=[], resolved=[])
        with self.assertRaises(ValueError) as caught:
            briefing.save_checkpoint(data, PROJECT, SLOT, payload, 'alice/session', lambda argv: self.fail('write'))
        self.assertIn(SENTENCE, str(caught.exception))
        self.assertIn('it takes no checkpoint', str(caught.exception))

    def test_a_review_record_on_the_slot_is_refused_before_any_write(self):
        data = [slot_row(status='in_progress', assignee='alice/session')]
        payload = dict(contribute(), task=SLOT)
        with self.assertRaises(ValueError) as caught:
            review_workflow.execute(data, SLOT, 'alice/session', payload, lambda argv: self.fail('write'))
        self.assertIn(SENTENCE, str(caught.exception))
        self.assertIn('it takes no review record', str(caught.exception))
        # An ordinary task is not affected by the check.
        ordinary = rows()
        written = []

        def run(argv):
            written.append(argv)
            return json.dumps({'id': 'c9', 'created_at': '2026-10-05T00:00:00Z'})
        review_workflow.execute(ordinary, TASK, 'alice/session', contribute(), run)
        self.assertEqual(len(written), 1)

    def test_the_checkpoint_of_an_ordinary_task_is_not_affected(self):
        data = rows()
        written = []

        def run(argv):
            written.append(argv)
            return json.dumps({'id': 'c2'})
        briefing.save_checkpoint(data, PROJECT, TASK, checkpoint(data), 'alice/session', run)
        self.assertEqual(len(written), 1)


@unittest.skipIf(sys.platform == 'win32', 'endpoint.py uses the POSIX coordination lock')
class RawGuardTests(unittest.TestCase):
    """`bd update --claim` and the other status moves, through the endpoint's raw path."""

    def test_a_status_or_assignee_move_on_the_slot_is_refused(self):
        import endpoint
        from unittest.mock import patch
        ordinary = {'id': PROJECT + '-1', 'issue_type': 'task', 'labels': [], 'comments': []}
        for argv in (['update', SLOT, '--claim'], ['update', SLOT, '--status', 'closed'], ['close', SLOT],
                     ['reopen', SLOT], ['update', SLOT, '--assignee', 'bob']):
            with self.subTest(argv=argv), patch.object(endpoint, '_native_anchor_rows',
                                                       lambda root, path, actor, tokens: {SLOT: (SLOT, slot_row())}):
                with self.assertRaises(ValueError) as caught:
                    endpoint._guard_record_anchor_status(Path('/srv/rt'), Path('/srv/rt/projects/x'), argv, 'alice')
                self.assertIn(SENTENCE, str(caught.exception))
                self.assertIn('only through `coordinate`', str(caught.exception))
        with patch.object(endpoint, '_native_anchor_rows',
                          lambda root, path, actor, tokens: {PROJECT + '-1': (PROJECT + '-1', ordinary)}):
            endpoint._guard_record_anchor_status(Path('/srv/rt'), Path('/srv/rt/projects/x'),
                                                 ['update', PROJECT + '-1', '--claim'], 'alice')


class HttpTests(fixes.EndpointCase):
    """The web service on the endpoint backend (the strict stub runs the kit's own work view)."""

    def setUp(self):
        super().setUp()
        self.admin = self.admin_token()
        self.project = self.create_project(self.admin, 'Alpha')
        user_id = self.create_account(self.admin, 'alex', 'alex-password-1')
        self.request('PUT', '/v1/projects/%s/members/%s' % (self.project, user_id), {'role': 'contributor'},
                     token=self.admin)
        self.alex = self.login('alex', 'alex-password-1')[0]
        made = self.request('POST', '/v1/agents', {'name': 'Kestrel', 'working_directory': '/home/alex/k',
                                                   'projects': [self.project]}, token=self.alex)
        self.agent = made.data['credential']['secret']
        self.task = self.create_task(self.admin, self.project, 'a real task').data['id']
        self.slot = '%s-merge-slot' % self.project
        path = self.canonical_root / 'canonical.json'
        state = json.loads(path.read_text(encoding='utf-8'))
        state['rows'].append(slot_row(self.slot, project=self.project))
        path.write_text(json.dumps(state), encoding='utf-8')
        self.base = '/v1/projects/%s/tasks/%s' % (self.project, self.slot)

    def test_the_slot_is_in_no_list_and_is_never_offered(self):
        listed = [row['id'] for row in self.request('GET', '/v1/projects/%s/tasks' % self.project,
                                                    token=self.alex).data['items']]
        self.assertEqual(listed, [self.task])
        nxt = self.request('GET', '/v1/agents/me/next', token=self.agent).data
        self.assertEqual([(a['kind'], a['task']) for a in nxt['next_actions']], [('claimable-task', self.task)])
        self.assertEqual(nxt['attention']['counts']['claimable'], 1)
        queue = self.request('GET', '/v1/projects/%s/queue' % self.project, token=self.admin).data['items']
        self.assertNotIn(self.slot, [row['id'] for row in queue])
        work_page = self.request('GET', '/v1/me/work', token=self.alex).data
        self.assertNotIn(self.slot, json.dumps(work_page))

    def test_reading_it_as_a_task_answers_not_found(self):
        for path in ('', '/brief', '/history'):
            with self.subTest(route=path or '/'):
                answer = self.request('GET', self.base + path, token=self.alex)
                self.assertEqual(404, answer.status, answer.data)
                self.assertNotIn('Merge Slot', json.dumps(answer.data))

    def test_every_write_on_it_is_refused_with_a_sentence_and_nothing_is_written(self):
        before = json.dumps([r for r in self.canonical_rows() if r['id'] == self.slot], sort_keys=True)
        writes = (('POST', '/claim', {}), ('PATCH', '', {'title': 'mine now'}),
                  ('POST', '/checkpoints', {'summary': 's', 'next_action': 'n', 'intent': 'i', 'acceptance': 'a'}),
                  ('POST', '/reviews', dict(fixes.CONTRIBUTION, operation='contribute', schema_version=1,
                                            operation_id='op-slot-1', previous=None)))
        for token in (self.alex, self.agent):
            for method, path, body in writes:
                with self.subTest(route=path or 'PATCH', agent=token == self.agent):
                    answer = self.request(method, self.base + path, body, token=token)
                    self.assertEqual(409, answer.status, answer.data)
                    self.assertEqual(answer.data['error']['message'], self.slot + ' ' + SENTENCE)
        self.assertEqual(json.dumps([r for r in self.canonical_rows() if r['id'] == self.slot], sort_keys=True), before)
        # A real task beside it is claimed as before.
        self.assertEqual(200, self.request('POST', '/v1/projects/%s/tasks/%s/claim' % (self.project, self.task), {},
                                           token=self.agent).status)

    def test_a_slot_that_lost_its_label_is_still_the_slot_on_every_route(self):
        # The rule is the id alone: nothing lists it, reads answer 404 and writes 409 as before.
        path = self.canonical_root / 'canonical.json'
        state = json.loads(path.read_text(encoding='utf-8'))
        for row in state['rows']:
            if row['id'] == self.slot:
                row['labels'] = []
        path.write_text(json.dumps(state), encoding='utf-8')
        nxt = self.request('GET', '/v1/agents/me/next', token=self.agent).data
        self.assertEqual([(a['kind'], a['task']) for a in nxt['next_actions']], [('claimable-task', self.task)])
        self.assertEqual(nxt['attention']['counts']['claimable'], 1)
        queue = self.request('GET', '/v1/projects/%s/queue' % self.project, token=self.admin).data['items']
        self.assertNotIn(self.slot, [row['id'] for row in queue])
        listed = [row['id'] for row in self.request('GET', '/v1/projects/%s/tasks' % self.project,
                                                    token=self.alex).data['items']]
        self.assertEqual(listed, [self.task])
        self.assertEqual(404, self.request('GET', self.base, token=self.alex).status)
        self.assertEqual(409, self.request('PATCH', self.base, {'title': 'mine now'}, token=self.alex).status)
        self.assertEqual(409, self.request('POST', self.base + '/claim', {}, token=self.agent).status)


    def test_an_endpoint_that_lists_the_slot_in_work_does_not_get_it_offered(self):
        # An endpoint older than kittrial-5bb.113 listed the slot as an ordinary open task.
        run = self.backend._run

        def older(action, project_id, actor, args, *rest, **kwargs):
            answer = run(action, project_id, actor, args, *rest, **kwargs)
            if action == 'work' and isinstance(answer, dict):
                listed = dict(answer['items'][0], task=self.slot, title='Merge Slot', owner=None, status='open',
                              review_state='none', contribution_id=None) if answer.get('items') else None
                if listed:
                    answer = dict(answer, items=[listed] + list(answer['items']))
            return answer
        self.backend._run = older
        self.addCleanup(setattr, self.backend, '_run', run)
        nxt = self.request('GET', '/v1/agents/me/next', token=self.agent).data
        self.assertEqual([(a['kind'], a['task']) for a in nxt['next_actions']], [('claimable-task', self.task)])
        queue = self.request('GET', '/v1/projects/%s/queue' % self.project, token=self.admin).data['items']
        self.assertNotIn(self.slot, [row['id'] for row in queue])
        self.assertEqual(nxt['attention']['counts']['claimable'], 1)


class ReaderTests(unittest.TestCase):
    """Readers that walk every row skip the slot, even one that somehow carries a review chain."""

    def chained(self, **changes):
        from requirements import canonical_bytes
        row = slot_row(status='in_progress', assignee='alice/session', **changes)
        records = [('delivery', dict(contribute(), task=row['id'])),
                   ('asked', dict(schema_version=1, operation='request-review', operation_id='ask-1', task=row['id'],
                                  previous='delivery', contribution='delivery', reviewer='bob'))]
        row['comments'] = [dict(id=cid, author='alice/session', created_at='2026-10-05T00:00:0%dZ' % index,
                                text=review_workflow.PREFIX + canonical_bytes(payload).decode())
                           for index, (cid, payload) in enumerate(records)]
        return row

    def test_the_newest_contribution_reader_skips_the_slot(self):
        import lifecycle
        # The same row under an id that only ends like the slot's is an ordinary task and is read.
        near = PROJECT + '-x-merge-slot'
        self.assertEqual(lifecycle.current_contribution_commits([self.chained(slot_id=near)]), {near: 'a' * 40})
        self.assertEqual(lifecycle.current_contribution_commits([self.chained()]), {})
        self.assertEqual(lifecycle.current_contribution_commits([self.chained(labels=[])]), {})

    def test_the_open_review_request_count_skips_the_slot(self):
        near = PROJECT + '-x-merge-slot'
        self.assertEqual(review_workflow.open_review_requests_by([self.chained(slot_id=near)], 'alice/session'), 1)
        self.assertEqual(review_workflow.open_review_requests_by([self.chained()], 'alice/session'), 0)
        self.assertEqual(review_workflow.open_review_requests_by([self.chained(labels=[])], 'alice/session'), 0)


if __name__ == '__main__':
    unittest.main()
