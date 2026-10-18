"""An integrated contribution on an open task is the coordinator's move (kittrial-5bb.336).

The first revision of this answer (kittrial-5bb.211 amendment 3) made it the assignee's
move - "continue the remaining scope" - which read every integrated-but-open row as the
worker's own work, burying real tasks and, on jjbp, eleven such rows buried one new task.
The row cannot tell remaining scope from finished work; closing the task or recording what
remains is the coordinator's judgement.
"""
import sys
from pathlib import Path
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import review_state
import work

class IntegratedOpenMoveTests(unittest.TestCase):
    def test_owned_open_task_is_the_coordinators_move(self):
        move, sentence, actor = work.move_decision(
            'integrated', 'example-task', True, assignee='worker-a', closed=False)
        self.assertEqual((move, actor), ('coordinator', 'coordinator'))
        self.assertIn('the coordinator closes the task or records what remains', sentence)
        self.assertIn('review example-task', sentence)

    def test_other_caller_hears_the_same_role(self):
        move, sentence, actor = work.move_decision(
            'integrated', 'example-task', False, assignee='worker-a', closed=False)
        self.assertEqual((move, actor), ('unknown', 'coordinator'))
        self.assertEqual(sentence, work.UNKNOWN_MOVE_TEXT)

    def test_shared_projection_and_public_move_wrapper_agree(self):
        self.assertEqual(review_state.next_actor('integrated', 'worker-a', False), 'coordinator')
        self.assertEqual(review_state.next_actor('integrated', None, False), 'coordinator')
        self.assertEqual(work.review_move('integrated', 'example-task', True)[0], 'coordinator')

    def test_closed_task_keeps_history_without_inviting_more_work(self):
        move, sentence, actor = work.move_decision(
            'integrated', 'example-task', True, assignee='worker-a', closed=True)
        self.assertEqual((move, actor), ('unknown', 'none'))
        self.assertIn('closed', sentence)

    def test_an_unassigned_integrated_row_is_the_coordinators_too(self):
        # kittrial-5bb.336: the move does not depend on a holder being known - the
        # coordinator closes it or records what remains whoever held it.
        move, sentence, actor = work.move_decision(
            'integrated', 'example-task', False, assignee=None, closed=False, claimable=False)
        self.assertEqual((move, actor), ('unknown', 'coordinator'))

    def test_pending_review_remains_the_reviewers_move(self):
        move, sentence, actor = work.move_decision(
            'awaiting-review', 'example-task', True, assignee='worker-a', closed=False)
        self.assertEqual((move, actor), ('reviewer', 'reviewer'))

    def test_pending_integration_remains_the_coordinators_move(self):
        move, sentence, actor = work.move_decision(
            'awaiting-integration', 'example-task', True, assignee='worker-a', closed=False)
        self.assertEqual((move, actor), ('coordinator', 'coordinator'))

if __name__ == '__main__':
    unittest.main()
