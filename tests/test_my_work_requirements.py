"""kittrial-5bb.433: My work lists only the requirement records that still ask something of their assignee.

The rule is the server's: requirement_records.assigned_state reads the record's own ledger, the canonical work
queue carries it on each requirement row, GET /v1/me/work leaves out a record accepted with nothing pending
(or ended by its owner), and next_action says where one waiting for acceptance is accepted.
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

KIT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(KIT), str(KIT / 'tests')]

import http_service
import requirement_records as rr
import work
from export_requirements import REVISION_PREFIX
from test_http_web import TeamHarness
from test_requirement_records import Native

ACCEPT = dict(owners=['owner-a'], approvers=['owner-a'], policy='any-owner', evidence='review')
WAITING = "Requirement waiting for acceptance: accept it on the project's Requirements page"
EDIT = "Requirement record: edit it on the project's Requirements page"


class Records(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.project = Path(temp.name)
        self.native = Native()
        self.native.seed('job-1', issue_type='job')

    def draft(self, key, op):
        self.native.actor = 'alice'
        return rr.apply_native(dict(schema_version=1, kind='requirement', title=key, key=key, description='S.',
                                    acceptance_state='draft', operation_id=op, operation='draft', parent='job-1'),
                               'alice', self.native, self.project)['id']

    def revise(self, rid, key, op, revision, accepted):
        # Both go through the operator path with evidence: an edit after acceptance is a demotion to draft.
        self.native.actor = 'operator'
        rr.apply_native(dict(schema_version=1, kind='requirement', title=key, key=key, description='S %d.' % revision,
                             operation_id='%s-%d' % (op, revision), operation='revise', task=rid, revision=revision,
                             acceptance_state='accepted' if accepted else 'draft',
                             acceptance=dict(ACCEPT, decision_id='d-%s-%d' % (key, revision))),
                        'operator', self.native, self.project, operator=True, operators=['operator'])

    def state(self, rid, operators=('operator',)):
        return rr.assigned_state(self.native.row(rid), list(operators))


class AssignedStateTests(Records):
    def test_a_first_draft_is_waiting_for_acceptance(self):
        self.assertEqual(self.state(self.draft('R01', 'op1')), 'awaiting-acceptance')

    def test_an_accepted_record_with_nothing_pending_is_accepted(self):
        rid = self.draft('R01', 'op1')
        self.revise(rid, 'R01', 'op1', 2, accepted=True)
        self.assertEqual(self.state(rid), 'accepted')

    def test_an_edit_after_acceptance_is_waiting_again(self):
        # The coordinator's condition: an accepted record with a pending edit stays on My work.
        rid = self.draft('R01', 'op1')
        self.revise(rid, 'R01', 'op1', 2, accepted=True)
        self.revise(rid, 'R01', 'op1', 3, accepted=False)
        self.assertEqual(self.state(rid), 'awaiting-acceptance')

    def test_an_owner_ended_record_reads_its_terminal_state(self):
        rid = self.draft('R01', 'op1')
        self.revise(rid, 'R01', 'op1', 2, accepted=True)
        for ended in ('withdrawn', 'superseded'):
            with self.subTest(ended=ended), patch('requirement_owner_records.state_ledger',
                                                  return_value=({'state': ended}, None)):
                self.assertEqual(self.state(rid), ended)

    def test_an_unreadable_or_unproven_ledger_is_unknown(self):
        rid = self.draft('R01', 'op1')
        self.revise(rid, 'R01', 'op1', 2, accepted=True)
        self.native.row(rid)['comments'].append(dict(id='bad', text=REVISION_PREFIX + '{not json', author='alice',
                                                     created_at='2026-09-25T00:00:00Z'))
        self.assertEqual(self.state(rid), 'unknown')
        self.assertEqual(rr.assigned_state({'id': 'x', 'labels': ['requirement'], 'comments': []}), 'unknown')

    def test_an_accepted_revision_its_evidence_does_not_prove_is_unknown(self):
        rid = self.draft('R01', 'op1')
        self.revise(rid, 'R01', 'op1', 2, accepted=True)
        # The same resolved_acceptance the requirements read uses decides; refused there, it never leaves My work.
        with patch.object(rr, 'resolved_acceptance', return_value=False):
            self.assertEqual(self.state(rid), 'unknown')

    def test_the_state_labels_are_never_read(self):
        rid = self.draft('R01', 'op1')
        row = self.native.row(rid)
        row['labels'] = [label for label in row['labels'] if not label.startswith('requirement:')] + ['requirement:accepted']
        self.assertEqual(self.state(rid), 'awaiting-acceptance')


class QueueTests(Records):
    def test_the_work_queue_carries_the_state_on_requirement_rows_only(self):
        accepted = self.draft('R01', 'op1')
        self.revise(accepted, 'R01', 'op1', 2, accepted=True)
        waiting = self.draft('R02', 'op2')
        self.native.seed('plain-1', title='A task')
        items = {item['task']: item for item in work.queue(self.native.rows, 'alice', [], operators=['operator'])['items']}
        self.assertEqual(items[accepted]['requirement_state'], 'accepted')
        self.assertEqual(items[waiting]['requirement_state'], 'awaiting-acceptance')
        self.assertIsNone(items['plain-1']['requirement_state'])
        self.assertTrue(items[accepted]['requirement_record'])
        self.assertFalse(items['plain-1']['requirement_record'])

    def test_the_canonical_backend_hands_the_state_on(self):
        # The endpoint backend reads `work --json` pages; here the page is work.queue's own answer.
        accepted = self.draft('R01', 'op1')
        self.revise(accepted, 'R01', 'op1', 2, accepted=True)
        waiting = self.draft('R02', 'op2')
        self.native.seed('plain-1', title='A task')
        page = dict(work.queue(self.native.rows, 'alice', [], operators=['operator']), next_offset=None)
        backend = object.__new__(http_service.EndpointBackend)
        backend.actor_namespace = 'web'
        with patch.object(http_service.EndpointBackend, '_run', return_value=page, create=True):
            items = {item['id']: item for item in backend.review_queue('pp')['items']}
        self.assertEqual((items[accepted]['requirement_record'], items[accepted]['requirement_state']), (True, 'accepted'))
        self.assertEqual(items[waiting]['requirement_state'], 'awaiting-acceptance')
        self.assertEqual((items['plain-1']['requirement_record'], items['plain-1']['requirement_state']), (False, None))


class NextActionTests(unittest.TestCase):
    def row(self, **extra):
        return dict({'status': 'open', 'review_state': 'none', 'assignee': 'u-owner'}, **extra)

    def test_a_record_waiting_for_acceptance_says_where_it_is_accepted(self):
        self.assertEqual(http_service.next_action(self.row(requirement_record=True, requirement_state='awaiting-acceptance')),
                         {'who': 'owner', 'text': WAITING})

    def test_any_other_record_says_where_it_is_edited_never_deliver(self):
        for row in (self.row(requirement_record=True, requirement_state='unknown'),
                    self.row(labels=['requirement', 'requirement:accepted']),
                    self.row(labels=['brd-section'])):
            with self.subTest(row=row):
                self.assertEqual(http_service.next_action(row), {'who': 'owner', 'text': EDIT})

    def test_an_ordinary_assigned_task_is_unchanged(self):
        self.assertEqual(http_service.next_action(self.row()), {'who': 'assignee', 'text': 'Deliver a contribution'})


class MyWorkTests(TeamHarness):
    """GET /v1/me/work over the in-process backend, its queue read standing in for the canonical one."""

    def test_my_work_leaves_out_settled_records_and_the_count_matches(self):
        me = self.ids['olive']
        rows = [('t-accepted', True, 'accepted'), ('t-withdrawn', True, 'withdrawn'), ('t-superseded', True, 'superseded'),
                ('t-waiting', True, 'awaiting-acceptance'), ('t-unknown', True, 'unknown'), ('t-plain', False, None),
                ('t-plain-stated', False, 'accepted')]
        items = [http_service.queue_item(self.project, {'id': tid, 'title': tid, 'status': 'open', 'assignee': me},
                                         'none', None, 0, requirement_record=record, requirement_state=state)
                 for tid, record, state in rows]
        with patch.object(self.backend, 'review_queue', return_value={'items': items, 'complete': True}):
            answer = self.request('GET', '/v1/me/work', token=self.olive)
        self.assertEqual(answer.status, 200, answer.data)
        assigned = {row['id']: row for row in answer.data['assigned']}
        self.assertEqual(sorted(assigned), ['t-plain', 't-plain-stated', 't-unknown', 't-waiting'])
        self.assertEqual(len(answer.data['assigned']), 4)
        self.assertEqual(assigned['t-waiting']['next_action']['text'], WAITING)
        self.assertEqual(assigned['t-unknown']['next_action']['text'], EDIT)
        self.assertEqual(assigned['t-plain']['next_action']['text'], 'Deliver a contribution')
        self.assertTrue(assigned['t-waiting']['requirement_record'])


if __name__ == '__main__':
    unittest.main()
