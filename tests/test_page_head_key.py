"""The page header (kittrial-5bb.525 slice b): title, key number and next action, per page.

tests/web_page_head_key.mjs runs the real views under Node (the DOM shim) against the
prototype's mock service, signed in as the project owner. It reads each expected figure from
the service itself. This module asserts, for My work, Tasks, a task (waiting for review, and
not), My agents and Members & settings:
- the header is there once, with one h1 that names the page (#page-title);
- the key number equals the server's figure and names its source; a page with no honest
  number shows none;
- the next action is the page's one primary action and leads where the work is.
Skips when Node is not installed.
"""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

KIT = Path(__file__).resolve().parent.parent
SCRIPT = KIT / 'tests' / 'web_page_head_key.mjs'
SHIM = KIT / 'tests' / 'web_dom_shim.mjs'
JS = KIT / 'web' / 'js'
_RUN = {}


def observed(case):
    if 'data' not in _RUN:
        node = shutil.which('node')
        if not node:
            case.skipTest('node is not installed')
        proc = subprocess.run([node, str(SCRIPT), SHIM.as_uri(), JS.as_uri() + '/'],
                              capture_output=True, text=True, timeout=120)
        case.assertEqual(0, proc.returncode, proc.stderr[-2000:])
        _RUN['data'] = json.loads(proc.stdout.strip().splitlines()[-1])
    return _RUN['data']


class PageHeadKey(unittest.TestCase):

    def head(self, page):
        data = observed(self)[page]
        head = data['head']
        self.assertTrue(head['found'], page)
        self.assertEqual(1, head['headers'], page)
        self.assertEqual(1, len(head['h1']), page)
        self.assertEqual('page-title', head['h1Id'], page)
        self.assertLessEqual(sum(a['primary'] for a in head['actions']), 1, head['actions'])
        return data, head

    def assertKey(self, key, num, label, source):
        self.assertIsNotNone(key)
        self.assertTrue(key['shown'], key)
        self.assertEqual((num, label, source), (key['num'], key['label'], key['source']))

    def test_my_work(self):
        data, head = self.head('myWork')
        self.assertEqual(['My work'], head['h1'])
        self.assertFalse(data['expected']['incomplete'])
        n = data['expected']['num']
        self.assertKey(head['key'], n, 'waits on you' if n == '1' else 'wait on you', 'my-work:revisions+to_review+assigned')
        self.assertTrue(head['actions'], 'My work must offer its next action')
        self.assertEqual('Open the oldest review', head['actions'][0]['text'])
        self.assertTrue(head['actions'][0]['primary'])
        self.assertEqual(data['expected']['oldestReview'], head['actions'][0]['next'])
        self.assertTrue(head['actions'][0]['href'].endswith('/t/' + data['expected']['oldestReview']))

    def test_tasks_key_is_the_servers_total_not_the_page(self):
        data, head = self.head('tasks')
        self.assertEqual(['Tasks'], head['h1'])
        self.assertIn(data['expected']['project'], data['crumbs'])
        expected = data['expected']
        # The fixture must hold more open tasks than one page, or this test could not tell the two apart.
        self.assertNotEqual(expected['active'], str(expected['activeRows']))
        self.assertKey(head['key'], expected['active'], 'open', 'page.total')
        self.assertKey(data['afterClosed'], expected['closed'], 'closed', 'page.total')
        self.assertEqual([('New task', True)], [(a['text'], a['primary']) for a in head['actions']])

    def test_tasks_rows_that_wait_on_you_are_marked(self):
        # slice (c): the accent bar marks exactly the rows whose next step is the reader's
        data = observed(self)['tasks']
        seen = data['rowsSeen']
        self.assertTrue(seen)
        self.assertTrue(any(r['mine'] for r in seen), 'the fixture must hold a row that waits on the owner')
        self.assertTrue(any(not r['mine'] for r in seen), 'and one that does not')
        for row in seen:
            expected = data['rowsExpected'][row['id']]
            self.assertEqual((expected, expected, expected), (row['mine'], row['flag'], row['hidden']), row)

    def test_task_waiting_for_review(self):
        data, head = self.head('taskWaiting')
        expected = data['expected']
        self.assertEqual([expected['title']], head['h1'])
        self.assertKey(head['key'], expected['num'], 'waiting for your review', 'review.contribution.at')
        first = head['actions'][0]
        self.assertEqual((expected['next'], True, 'review'), (first['text'], first['primary'], first['next']))
        self.assertIn('review-h', data['ids'])  # where the action takes the reader

    def test_task_with_nothing_waiting_shows_no_number(self):
        data, head = self.head('taskPlain')
        self.assertEqual([data['expected']['title']], head['h1'])
        self.assertIsNone(head['key'])
        self.assertNotIn('review', [a['next'] for a in head['actions']])

    def test_my_agents(self):
        data, head = self.head('agents')
        self.assertEqual(['My agents'], head['h1'])
        n = data['expected']['num']
        self.assertKey(head['key'], n, 'agent' if n == '1' else 'agents', 'agents.total')
        self.assertEqual([('Add an agent', True, 'add-agent')], [(a['text'], a['primary'], a['next']) for a in head['actions']])
        self.assertIn('a-name', data['ids'])

    def test_members_and_settings(self):
        data, head = self.head('settings')
        self.assertEqual(['Members & settings'], head['h1'])
        self.assertEqual(data['expected']['num'], head['key']['num'])
        self.assertTrue(head['key']['shown'])
        self.assertEqual('member' if data['expected']['num'] == '1' else 'members', head['key']['label'])
        self.assertIn(head['key']['source'], ('members.total', 'members.items'))
        self.assertEqual([('Add a member', True, 'add-member')], [(a['text'], a['primary'], a['next']) for a in head['actions']])
        self.assertIn('m-username', data['ids'])


if __name__ == '__main__':
    unittest.main()
