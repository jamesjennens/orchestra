"""An integrated revision does not finish an intentionally open task."""
import sys
from pathlib import Path
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import review_state
import work

class IntegratedOpenMoveTests(unittest.TestCase):
    def test_owned_open_task_calls_its_assignee_to_continue(self):
        move, sentence, actor = work.move_decision(
            'integrated', 'example-task', True, assignee='worker-a', closed=False)
        self.assertEqual((move, actor), ('you', 'assignee'))
        self.assertIn('remaining scope if any', sentence)
        self.assertIn('otherwise checkpoint that nothing remains for you', sentence)
        self.assertIn('coordinator', sentence)
        self.assertIn('review example-task', sentence)

    def test_other_caller_does_not_become_the_assignee(self):
        move, sentence, actor = work.move_decision(
            'integrated', 'example-task', False, assignee='worker-a', closed=False)
        self.assertEqual((move, actor), ('unknown', 'assignee'))
        self.assertEqual(sentence, work.UNKNOWN_MOVE_TEXT)

    def test_shared_projection_and_public_move_wrapper_agree(self):
        self.assertEqual(review_state.next_actor('integrated', 'worker-a', False), 'assignee')
        self.assertEqual(work.review_move('integrated', 'example-task', True)[0], 'you')

    def test_closed_task_keeps_history_without_inviting_more_work(self):
        move, sentence, actor = work.move_decision(
            'integrated', 'example-task', True, assignee='worker-a', closed=True)
        self.assertEqual((move, actor), ('unknown', 'none'))
        self.assertIn('closed', sentence)

    def test_unassigned_integrated_task_does_not_invent_a_holder(self):
        move, sentence, actor = work.move_decision(
            'integrated', 'example-task', False, assignee=None, closed=False, claimable=False)
        self.assertEqual((move, actor), ('unknown', 'unknown'))

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
