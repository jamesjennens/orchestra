"""The checkpoint template states the code's limits, and the history range where workers read it.

kittrial-5bb.341: a first checkpoint was refused because the served template did not state
the field limits or the ``lane_state`` shape, and a worker used ``history --limit 50`` after
reading a nearby ``--limit 50`` meant for checkpoint directions. These tests compare the
served template with the validators' own numbers, so a changed limit that is not carried
into the page fails here.
"""
import json
import re
import sys
import unittest
from pathlib import Path

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT))
import briefing
import lane_state
import onboarding

TEMPLATE = KIT / onboarding.DOCUMENTS['checkpoint-template']
PAYLOAD = KIT / 'templates' / 'CHECKPOINT.json'


def rows(text):
    """The Field/Limit table of the served template, as {field: value}."""
    return {field.strip(): value.strip() for field, value in
            re.findall(r'^\|\s*([A-Za-z_. ]+?)\s*\|\s*(.+?)\s*\|\s*$', text, re.M)}


def json_blocks(text):
    return re.findall(r'```json[ \t]*\n(.*?)\n```', text, re.S)


class CheckpointTemplateLimitTests(unittest.TestCase):
    def setUp(self):
        self.text = TEMPLATE.read_text(encoding='utf-8')
        self.table = rows(self.text)

    def test_the_served_document_is_the_catalogued_file(self):
        self.assertEqual(onboarding.DOCUMENTS['checkpoint-template'], 'templates/CHECKPOINT.md')
        self.assertEqual(onboarding.execute(KIT, KIT, 'example', 'worker', 'docs', ['checkpoint-template']),
                         self.text)

    def test_every_top_level_field_limit_is_the_codes_number(self):
        for field, limit in briefing.CHECKPOINT_FIELD_LIMITS:
            with self.subTest(field=field):
                self.assertIn(field, self.table, 'the template does not state a limit for %s' % field)
                self.assertIn(str(limit), self.table[field],
                              '%s states %r, the code says %d' % (field, self.table[field], limit))

    def test_every_open_item_limit_is_the_codes_number(self):
        # Literal keys with the code's numbers, so a changed constant changes this test.
        self.assertEqual((briefing.CHECKPOINT_ITEMS_MAX, briefing.CHECKPOINT_TEXT_LIMIT,
                          briefing.CHECKPOINT_SOURCE_LIMIT, briefing.CHECKPOINT_MAX_BYTES),
                         (100, 400, 240, 80000))
        self.assertIn('100 entries', self.table['open_items'])
        self.assertIn('400', self.table['open item text'])
        self.assertIn('240', self.table['open item source'])
        self.assertIn('100 entries', self.table['resolved'])
        self.assertIn('400', self.table['resolved reason'])
        self.assertIn('240', self.table['resolved evidence'])
        self.assertIn('80000', self.table['whole checkpoint'])
        self.assertEqual(lane_state.WAITING_ON_LIMIT, 96)
        self.assertIn('96', self.table['lane_state.waiting_on'])

    def test_the_payload_block_matches_the_shipped_template(self):
        blocks = json_blocks(self.text)
        self.assertGreaterEqual(len(blocks), 2, 'the page needs a lane_state example and the payload')
        payload = json.loads(blocks[-1])
        self.assertEqual(payload, json.loads(PAYLOAD.read_text(encoding='utf-8')))
        # The bare template states the same six limits in its own placeholders.
        for field, limit in briefing.CHECKPOINT_FIELD_LIMITS:
            self.assertIn(str(limit), payload[field],
                          'templates/CHECKPOINT.json does not state the %s limit' % field)
        self.assertEqual(set(payload), {'schema_version', 'task', 'previous', 'activity_cursor',
                                        'source_commit', 'branch', 'intent', 'acceptance', 'summary',
                                        'next_action', 'open_items', 'resolved', 'lane_state'})

    def test_the_lane_state_example_is_a_valid_sibling(self):
        example = json.loads(json_blocks(self.text)[0])
        self.assertEqual(set(example), {'schema_version', 'task', 'state'})
        self.assertEqual(example['state'], 'working')
        self.assertEqual(lane_state.problems(example, 'example-task'), [])
        self.assertTrue(any(state.startswith('waiting-') for state in lane_state.STATES))
        self.assertIn('is popped out before the checkpoint is validated', ' '.join(self.text.split()))

    def test_the_history_range_is_named_where_workers_are_told_to_read_history(self):
        for relative in ('docs/WORKER_START.md', 'docs/WORKER_GUIDE.md'):
            with self.subTest(document=relative):
                flat = ' '.join((KIT / relative).read_text(encoding='utf-8').split())
                match = re.search(r'history.{0,160}?1\.\.20', flat)
                self.assertIsNotNone(match, '%s tells workers to read history without the 1..20 range'
                                              % relative)
        self.assertEqual((briefing.HISTORY_LIMIT_MIN, briefing.HISTORY_LIMIT_MAX), (1, 20))

    def test_each_checkpoint_refusal_names_the_field_and_the_limit(self):
        base = dict(schema_version=1, task='task-1', previous=None, activity_cursor='x',
                    source_commit='', branch='', intent='i', acceptance='a', summary='s',
                    next_action='n', open_items=[], resolved=[])
        self.assertIn('summary: 1001 characters, the limit is 1000',
                      briefing.checkpoint_problems(dict(base, summary='x' * 1001), 'task-1'))
        item = dict(id='i1', kind='blocker', text='x' * 401, source='s')
        self.assertIn('open_items[0].text (id i1): 401 characters, the limit is 400',
                      briefing.checkpoint_problems(dict(base, open_items=[item]), 'task-1'))
        item = dict(id='i1', kind='blocker', text='t', source='s' * 241)
        self.assertIn('open_items[0].source (id i1): 241 characters, the limit is 240',
                      briefing.checkpoint_problems(dict(base, open_items=[item]), 'task-1'))
        items = [dict(id='i%d' % n, kind='blocker', text='t', source='s') for n in range(101)]
        self.assertIn('open_items: expected a list of at most 100 items',
                      briefing.checkpoint_problems(dict(base, open_items=items), 'task-1'))
        self.assertIn('Invalid lane state: waiting_on is longer than 96 characters',
                      lane_state.problems(dict(schema_version=1, task='task-1',
                                               state='waiting-on-review', waiting_on='x' * 97),
                                          'task-1'))


if __name__ == '__main__':
    unittest.main()
