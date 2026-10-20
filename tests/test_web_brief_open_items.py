"""The task page shows the complete unresolved list and joins long history entries.

kittrial-5bb.356 item 6: ``checkpoint.open_items`` is only the first ten the brief
returns, and a long comment arrives as several history fragments. The page must show
every ``carry_open_items`` entry with the true total (or say the list could not all be
read and offer a reload), join one entry's fragments into one item, fetch the next page
while the last fragment is still continued, and name the newer entries its history
button loads (the history is oldest first).
"""
import json
import shutil
import sys
import unittest
from pathlib import Path

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT / 'tests'))


class BriefOpenItemsPageTests(unittest.TestCase):
    def test_the_page_shows_every_item_and_joins_history_fragments(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('node is not installed')
        import test_http_web
        done = test_http_web.run_node_module(self, node, 'await import(process.argv[1])',
                                             (KIT / 'tests' / 'web_brief_open_items.mjs').as_uri(),
                                             (KIT / 'tests' / 'web_dom_shim.mjs').as_uri(),
                                             (KIT / 'web' / 'js' / 'api.js').as_uri(),
                                             (KIT / 'web' / 'js' / 'views' / 'task.js').as_uri())
        self.assertEqual(0, done.returncode, done.stderr[-3000:])
        seen = json.loads(done.stdout.strip().splitlines()[-1])

        # Every carry_open_items entry is listed, and the heading carries the recorded
        # total (5), not the two the first page of checkpoint.open_items holds.
        self.assertEqual(5, len(seen['openItems']), seen['openItems'])
        for number in range(1, 6):
            self.assertIn('Item %d' % number, ' '.join(seen['openItems']))
        self.assertTrue(seen['unresolvedHeading'], seen['openItems'])
        self.assertFalse(seen['completeHasReadAgain'])

        # The long comment is ONE item, both halves in order, even though its second
        # half was on the next page and the first page ended mid-body.
        self.assertEqual(2, seen['historyItems'], seen['historyText'])
        self.assertIn('First half. Second half.', seen['historyText'])
        self.assertEqual(2, seen['historyCalls'])
        # History is oldest first, so the button loads newer entries and says so.
        self.assertEqual(1, seen['showNewer'])
        self.assertEqual(0, seen['showOlder'])

        # A null complete list is not shown as if it were the whole list.
        self.assertTrue(seen['incompleteNotice'])
        self.assertEqual(1, seen['reloadButtons'])
        self.assertEqual(2, len(seen['incompleteItems']))


if __name__ == '__main__':
    unittest.main()
