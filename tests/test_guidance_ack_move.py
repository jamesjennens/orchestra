"""kittrial-5bb.325: an unacknowledged guidance version is a MOVE, not only a flag.

The same one item has to reach work --mine, brief, session resume and the wait
projection, name the current version and the command, and never copy the guidance
text. These are the cheap unit-level checks; the real-bd module
(test_guidance_ack_move_real_bd) drives the same reads through the canonical endpoint,
the client `wait` loop and the HTTP agent routes.
"""
import json
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT))
sys.path.insert(0, str(KIT / 'tests'))
import briefing
import guidance
import sessions
import waiting
from work import queue
import test_guidance as fixture

TASK = fixture.TASK
PROJECT = fixture.PROJECT
TEXT = 'Follow the design note before changing anything.'


class GuidanceMoveItemTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project = Path(self.tmp.name) / 'project'
        self.project.mkdir()

    def rows(self):
        return fixture.rows()

    def work(self, actor='worker-1'):
        return queue(self.rows(), actor, ['--mine', '--limit', '50'],
                     journal=self.project, reference_attention=False)

    def wait_rows(self, actor='worker-1'):
        """What the endpoint's wait read projects: the queue rows plus the one item."""
        payload = self.work(actor)
        rows = list(payload.get('items') or [])
        item = (payload.get('guidance') or {}).get('item')
        if isinstance(item, dict):
            rows.append(item)
        return rows

    def test_no_guidance_means_no_item_anywhere(self):
        self.assertIsNone(guidance.state(self.project, 'worker-1')['item'])
        self.assertIsNone(self.work()['guidance']['item'])
        brief = briefing.brief(self.rows(), PROJECT, TASK, journal=self.project, actor='worker-1')
        self.assertIsNone(brief['guidance']['item'])
        self.assertNotIn('Guidance action:', briefing.format_brief(brief))
        self.assertFalse([row for row in waiting.worker_view(self.wait_rows())['items']
                          if row.get('task') == 'guidance'])

    def test_a_fresh_actor_gets_the_item_on_every_reader(self):
        first = guidance.write_guidance(self.project, TEXT, 'operator-1')
        for name, block in (('state', guidance.state(self.project, 'worker-1')),
                            ('work', self.work()['guidance']),
                            ('brief', briefing.brief(self.rows(), PROJECT, TASK,
                                                     journal=self.project, actor='worker-1')['guidance'])):
            with self.subTest(reader=name):
                item = block['item']
                self.assertEqual(first['version'], item['guidance_version'])
                self.assertEqual('guidance', item['task'])
                self.assertEqual('you', item['whose_move'])
                self.assertEqual('worker-1', item['next_actor'])
                self.assertIn(first['version'], item['command'])
                self.assertIn('guidance get', item['command'])
                self.assertIn('guidance ack --version', item['command'])
                self.assertIn(guidance.ITEM_TITLE, item['text'])
                # The item carries the version and the command, never the text.
                self.assertNotIn(TEXT, json.dumps(item))

    def test_brief_renders_the_action_line_with_version_and_command(self):
        first = guidance.write_guidance(self.project, TEXT, 'operator-1')
        brief = briefing.brief(self.rows(), PROJECT, TASK, journal=self.project, actor='worker-1')
        rendered = briefing.format_brief(brief)
        self.assertIn('Guidance action:', rendered)
        self.assertIn(first['version'], rendered)
        self.assertIn('guidance ack --version', rendered)
        self.assertNotIn(TEXT, rendered)

    def test_session_resume_carries_the_item(self):
        fixture.session_registry(self.project)
        first = guidance.write_guidance(self.project, TEXT, 'operator-1')
        answer = sessions.execute(self.project, PROJECT, ['resume', '--request-id', str(uuid.uuid4())],
                                  lambda: '',
                                  actor=fixture.SESSION_ACTOR)
        item = answer['guidance']['item']
        self.assertEqual(first['version'], item['guidance_version'])
        self.assertIn(first['version'], item['command'])
        self.assertNotIn(TEXT, json.dumps(answer))

    def test_only_the_ack_of_the_current_version_clears_it_and_a_new_set_returns_it(self):
        first = guidance.write_guidance(self.project, TEXT, 'operator-1')
        second = guidance.write_guidance(self.project, 'Second guidance.', 'operator-1')
        # An actor whose last acknowledgement is an OLD version still has the item, and
        # the item names the CURRENT version and command.
        fixture.inject_stale_ack(self.project, 'worker-1', first['version'])
        item = guidance.state(self.project, 'worker-1')['item']
        self.assertIsNotNone(item)
        self.assertEqual(second['version'], item['guidance_version'])
        self.assertIn(second['version'], item['command'])
        # The current version does clear it, for that actor only.
        guidance.acknowledge(self.project, 'worker-1', second['version'])
        self.assertIsNone(guidance.state(self.project, 'worker-1')['item'])
        self.assertIsNotNone(guidance.state(self.project, 'worker-3')['item'])
        guidance.acknowledge(self.project, 'worker-3', second['version'])
        self.assertIsNone(guidance.state(self.project, 'worker-3')['item'])
        # A new set returns it, naming the new version.
        third = guidance.write_guidance(self.project, 'Third guidance.', 'operator-1')
        item = guidance.state(self.project, 'worker-1')['item']
        self.assertEqual(third['version'], item['guidance_version'])

    def test_withheld_guidance_keeps_todays_attention_and_gets_no_item(self):
        # Text with no audit record: unbound, withheld, attention true as before.
        (self.project / 'GUIDANCE.md').write_text(TEXT, encoding='utf-8')
        block = guidance.state(self.project, 'worker-1')
        self.assertTrue(block['unbound'])
        self.assertTrue(block['attention'])
        self.assertIsNone(block['item'])
        self.assertIsNone(self.work()['guidance']['item'])

    def test_the_fingerprint_changes_with_the_version_and_clears_after_the_ack(self):
        first = guidance.write_guidance(self.project, TEXT, 'operator-1')
        before = waiting.worker_view(self.wait_rows())
        self.assertEqual(['guidance'], [row['task'] for row in before['items']
                                        if row['task'] == 'guidance'])
        self.assertTrue(waiting.is_fingerprint(before['fingerprint']))
        guidance.acknowledge(self.project, 'worker-1', first['version'])
        after_ack = waiting.worker_view(self.wait_rows())
        self.assertNotEqual(before['fingerprint'], after_ack['fingerprint'])
        self.assertFalse([row for row in after_ack['items'] if row['task'] == 'guidance'])
        second = guidance.write_guidance(self.project, 'A new instruction.', 'operator-1')
        after_set = waiting.worker_view(self.wait_rows())
        self.assertNotEqual(after_ack['fingerprint'], after_set['fingerprint'])
        row = next(row for row in after_set['items'] if row['task'] == 'guidance')
        self.assertEqual(second['version'], row['guidance_version'])

    def test_the_ack_alone_clears_it_even_with_other_moves_in_the_queue(self):
        guidance.write_guidance(self.project, TEXT, 'operator-1')
        guidance.acknowledge(self.project, 'worker-1', guidance.version_of(TEXT))
        payload = self.work()
        self.assertIsNone(payload['guidance']['item'])
        # The task row is still there, so the queue itself did not change shape.
        self.assertIn(TASK, [row['task'] for row in payload['items']])


if __name__ == '__main__':
    unittest.main()
