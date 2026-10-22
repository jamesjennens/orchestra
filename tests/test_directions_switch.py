"""kittrial-5bb.284: a direction that cannot be dispositioned is not reported as outstanding.

A disposition can only be written through a checkpoint's ``directions`` field, and
``save_checkpoint`` refuses that field while ``checkpoint_provenance_writes`` is off (the
default on every installation). These tests pin the readers' answer in both settings and for
a closed task. They are library fixtures built from the ``test_briefing`` helpers: they run
no endpoint and no bd, so the canonical endpoint wiring (the endpoint reading the switch and
passing it into ``work``/``brief``) is pinned by ``tests/test_review_workflow_additive.py``.
"""
import unittest

import briefing as b
from test_briefing import PROJECT, TASK, STAMP, rows, comment, checkpoint, append_checkpoint


def fixture(status='in_progress'):
    """A legacy checkpoint plus a same-second later comment by another actor.

    The comment is the fixture ``tests/test_checkpoint_review3.py`` already uses as a
    direction: another actor's ordinary comment, tied with the checkpoint's timestamp.
    """
    data = rows()
    data[0]['status'] = status
    append_checkpoint(data, 'legacy', checkpoint(data))
    data[0]['comments'].append(comment('later', 'Instruction', STAMP, author='coordinator'))
    return data


class DirectionsSwitchTests(unittest.TestCase):
    def test_switch_off_reports_no_outstanding_direction_and_says_why(self):
        data = fixture()
        # Control: the same fixture is one outstanding direction while the switch is ON.
        self.assertEqual(b.brief(data, PROJECT, TASK)['directions']['total'], 1)
        read = b.brief(data, PROJECT, TASK, provenance_writes=False)
        self.assertEqual(read['directions']['total'], 0)
        self.assertEqual(read['directions']['items'], [])
        self.assertEqual(read['directions']['check'], 'off')
        self.assertEqual(read['directions']['reason'], 'switch-off')
        self.assertEqual(read['directions_check']['state'], 'off')
        self.assertIn('checkpoint_provenance_writes', read['directions_check']['note'])
        # Review B1: the block states the check, and no next_action prefix is added.
        self.assertNotIn('DIRECTIONS CHECK OFF', read['next_action'])
        self.assertNotIn('CLOSED TASK', read['next_action'])
        self.assertNotIn('OUTSTANDING DIRECTIONS', read['next_action'])
        self.assertEqual(b.checkpoint_queue_fields(data, data[0], provenance_writes=False)['unresolved_directions'], 0)
        self.assertEqual(b.checkpoint_queue_fields(data, data[0])['unresolved_directions'], 1)
        page = b.direction_page(data, PROJECT, TASK, provenance_writes=False)
        self.assertEqual((page['total'], page['items'], page['check'], page['reason']),
                         (0, [], 'off', 'switch-off'))
        self.assertEqual(b.direction_page(data, PROJECT, TASK)['total'], 1)

    def test_closed_task_lists_none_even_with_the_switch_on(self):
        data = fixture('closed')
        read = b.brief(data, PROJECT, TASK)
        self.assertEqual(read['directions']['total'], 0)
        # Review B1: a closed task is not reported as the switch being off. The
        # directions block's own `check` label says `closed` too, not only
        # `directions_check` (kittrial-5bb.284 review S3, folded into
        # kittrial-5bb.287); the same holds for the `newer` block and the
        # `checkpoint --directions` page, and no next_action prefix is added.
        self.assertEqual(read['directions']['reason'], 'closed')
        self.assertEqual(read['directions']['check'], 'closed')
        self.assertEqual(read['newer']['unresolved_directions']['check'], 'closed')
        self.assertEqual(read['directions_check']['state'], 'closed')
        self.assertEqual(read['directions_check']['reason'], 'closed')
        self.assertIn('CLOSED TASK', read['directions_check']['note'])
        self.assertNotIn('checkpoint_provenance_writes', read['directions_check']['note'])
        self.assertNotIn('DIRECTIONS CHECK OFF', read['next_action'])
        self.assertEqual(b.checkpoint_queue_fields(data, data[0])['unresolved_directions'], 0)
        page = b.direction_page(data, PROJECT, TASK)
        self.assertEqual((page['total'], page['check'], page['reason']), (0, 'closed', 'closed'))

    def test_the_check_label_names_the_reason_that_applies(self):
        """kittrial-5bb.287 review S3: the reader's `check` says `closed` for a closed
        task; `off` names the installation switch only."""
        self.assertEqual(b.directions_check_state('switch-off'), 'off')
        self.assertEqual(b.directions_check_state('closed'), 'closed')

    def test_nothing_to_acknowledge_leaves_next_action_unchanged(self):
        """Review B1: a checkpointed task with no comment to acknowledge reads the same
        next action off and on; the off state lives in the directions_check block."""
        data = rows()
        append_checkpoint(data, 'legacy', checkpoint(data))
        off = b.brief(data, PROJECT, TASK, provenance_writes=False)
        on = b.brief(data, PROJECT, TASK)
        self.assertEqual(off['next_action'], on['next_action'])
        self.assertNotIn('DIRECTIONS CHECK OFF', off['next_action'])
        self.assertNotIn('CLOSED TASK', off['next_action'])
        self.assertEqual(off['directions_check']['state'], 'off')
        self.assertEqual(off['directions_check']['reason'], 'switch-off')
        self.assertNotIn('directions_check', on)

    def test_switch_on_is_byte_for_byte_unchanged(self):
        data = fixture()
        read = b.brief(data, PROJECT, TASK)
        self.assertEqual(read['directions']['total'], 1)
        self.assertNotIn('directions_check', read)
        self.assertNotIn('check', read['directions'])
        self.assertNotIn('DIRECTIONS CHECK OFF', read['next_action'])
        # The queue row is unchanged too: no new key is added while the switch is on.
        self.assertNotIn('directions_check', b.checkpoint_queue_fields(data, data[0]))
        self.assertEqual(b.direction_page(data, PROJECT, TASK)['total'], 1)

    def test_unresolved_directions_guard_is_the_mechanism(self):
        data = fixture()
        state = b.checkpoint_state(data[0])
        context = b.direction_context(data[0], state)
        entries = b.direction_entries(b.snapshot(data, PROJECT, TASK), context)
        dispositions = b.chain_dispositions(state['history'])
        self.assertEqual(len(b.unresolved_directions(entries, dispositions, context[0])), 1)
        self.assertEqual(b.unresolved_directions(entries, dispositions, context[0], False), [])


if __name__ == '__main__':
    unittest.main()
