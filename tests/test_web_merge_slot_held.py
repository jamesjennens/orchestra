"""The web merge slot against a HELD task, on the real stack (kittrial-5bb.272 item 2a).

The check no review of kittrial-5bb.10 ran: `coordination.py`'s merge-acquire refuses a
task whose contribution is held, and the web route reaches that same refusal through the
same `coordination.apply_native`. This file runs the real web service in front of the real
`endpoint.py` and a real bd, delivers and approves a task over the web, records the hold
HOST-side (through the canonical review action the web itself builds for the ordinary
operations - the host review transport; the web route refuses `hold` itself), and then
takes the merge slot through the web route: the acquisition is refused with the hold
sentence and the slot stays free.
"""
import json
import sys
import unittest
from pathlib import Path

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT))
sys.path.insert(0, str(KIT / 'tests'))
import review_workflow
import test_coordinator_grant_real as stack

#: The host operator that records the hold; listed in deployment.private.json by the test.
HOLD_OPERATOR = 'hold-host-operator'


def only_its_own_tests(cls):
    """Borrow the real stack of tests/test_coordinator_grant_real.py and run none of its tests again."""
    for base in (stack.RealStackTests,):
        for name in dir(base):
            if name.startswith('test_') and name not in cls.__dict__:
                setattr(cls, name, None)
    return cls


@only_its_own_tests
@unittest.skipIf(getattr(stack.rb, 'endpoint', None) is None, 'endpoint imports fcntl (POSIX-only)')
@unittest.skipIf(getattr(stack.rb, 'BD', None) is None,
                 'no real bd binary (set ORCHESTRA_BD_BIN or put bd on PATH)')
class WebMergeSlotHeldTests(stack.RealStackTests):
    """alex owns pp, casey and drew contribute; the hold is written by a host operator."""

    def host_record(self, task, body):
        """Record one review operation through the canonical action, as the host does.

        The web route refuses `hold` and `release-hold` (host-only, kittrial-5bb.10 item
        6); this is the same `('review', project, [task, '@attachment:0'], ...)` action
        `reviews.add` builds for the ordinary operations, sent without a web authority -
        the host review transport.
        """
        reply = self.backend._endpoint('review', 'pp', HOLD_OPERATOR, [task, '@attachment:0'],
                                       {'0': {'flag': '--file', 'text': json.dumps(body)}})
        self.assertEqual(0, reply.get('returncode'), reply.get('stderr'))
        return json.loads(reply['stdout'])

    def host_hold(self, task, contribution, previous, reason='Held for the release check'):
        return self.host_record(task, dict(schema_version=1, operation='hold',
                                           operation_id='op-hold-%s' % task, task=task,
                                           previous=previous, contribution=contribution, reason=reason))

    def list_operator(self):
        deployment = self.root / 'deployment.private.json'
        kept = deployment.read_text(encoding='utf-8')
        self.addCleanup(deployment.write_text, kept, 'utf-8')
        values = json.loads(kept) if kept.strip() else {}
        deployment.write_text(json.dumps(dict(values, operators=[HOLD_OPERATOR],
                                              review_workflow_writes=True)), encoding='utf-8')

    def latest_workflow_comment(self, task):
        """The newest chain comment's id, as `previous` wants it (from the real tracker)."""
        found = json.loads(self.bd('comments', task, '--json').stdout) or []
        chain = [entry for entry in found
                 if (entry.get('text') or '').startswith(review_workflow.PREFIX)]
        self.assertTrue(chain, 'no review chain comment on %s' % task)
        return chain[-1]['id']

    def test_the_web_merge_slot_refuses_to_acquire_for_a_held_task(self):
        task, contribution = self.deliver('casey')
        self.assertEqual(201, self.approve(self.tokens['alex'], task, contribution).status)
        self.assertEqual('awaiting-integration', self.review(task)['state'])
        # The host operator is listed and the write switch is on, or the hold cannot be
        # written at all.
        self.list_operator()
        self.host_hold(task, contribution, self.latest_workflow_comment(task))
        self.assertEqual('held', self.review(task)['state'])

        # The web merge slot refuses to acquire for the held task, with the hold said.
        acquired = self.request('POST', '/v1/projects/pp/merge-slot/acquire',
                                {'task_id': task, 'target': 'main'},
                                token=self.tokens['alex'], key='op-acquire-held-1')
        self.assertEqual(409, acquired.status, acquired.data)
        self.assertIn('held', str(acquired.data).lower())
        # Nothing was taken: the slot reads free.
        slot = self.request('GET', '/v1/projects/pp/merge-slot', token=self.tokens['alex'])
        self.assertEqual(200, slot.status, slot.data)
        self.assertTrue(slot.data.get('available'), slot.data)

    def test_the_slot_still_acquires_after_the_hold_is_released(self):
        # The refusal is the hold's, not the route's: release it host-side and the same
        # caller can take the slot through the web route. The slot is the project's ONE,
        # so this test leaves it free again for its sibling.
        task, contribution = self.deliver('casey')
        self.assertEqual(201, self.approve(self.tokens['alex'], task, contribution).status)
        self.list_operator()
        hold = self.host_hold(task, contribution, self.latest_workflow_comment(task))
        held = self.review(task)
        self.assertEqual('held', held['state'])
        release = dict(schema_version=1, operation='release-hold', operation_id='op-release-%s' % task,
                       task=task, previous=self.latest_workflow_comment(task), contribution=contribution,
                       hold=hold['comment_id'], reason='Cleared after the check')
        self.host_record(task, release)
        self.assertEqual('awaiting-integration', self.review(task)['state'])
        try:
            acquired = self.request('POST', '/v1/projects/pp/merge-slot/acquire',
                                    {'task_id': task, 'target': 'main'},
                                    token=self.tokens['alex'], key='op-acquire-held-2')
            self.assertEqual(200, acquired.status, acquired.data)
            self.assertTrue(acquired.data['result']['acquired'], acquired.data)
        finally:
            self.request('POST', '/v1/projects/pp/merge-slot/release', {},
                         token=self.tokens['alex'], key='op-release-slot-2')


if __name__ == '__main__':
    unittest.main()
