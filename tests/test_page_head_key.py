"""The page header (kittrial-5bb.525 slice b): title, key number and next action, per page.

tests/web_page_head_key.mjs runs the real views under Node (the DOM shim) against the
prototype's mock service, signed in as the project owner. It reads each expected figure from
the service itself. This module asserts, for My work, Tasks, a task (waiting for review, and
not), My agents and People (Members & settings before .522 slice 2):
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

    # My work's fixture (web_page_head_key.mjs, FIXTURE_ROWS). The reader owns Alpha and contributes to Beta.
    # Which rows wait on the reader, from each row's own facts:
    #   a1  the reader's own delivery in Alpha, awaiting review: waits (the reader reviews in Alpha); it is
    #       in assigned AND to_review, and counts once
    #   b1  the reader's delivery in Beta, awaiting review: waits on Beta's owner, not on the reader
    #   b2  the reader's task in Beta, nothing delivered: waits (deliver)
    #   b3  sent back to the reader in Beta: waits (address the changes)
    #   a2  sam's delivery in Alpha, awaiting review: waits (review)
    #   a3  sam's approved work in Alpha: waits (integrate)
    #   a5  a requirement in Alpha waiting for acceptance: waits (accept)
    #   a6  a requirement record in Alpha that only says where it is edited: waits on no one
    #   a7  the reader's own delivery in Alpha, awaiting review, that the server did NOT list in to_review (the
    #       party rule: an approval by the reader's own party would be refused): waits on no one here (r3, FIXP)
    #   a8  an epic assigned to the reader: its step says "Deliver", but an epic is not work (r3, FIXA)
    # So 6 rows wait: a1, b2, b3, a2, a3, a5. The oldest review among them is a2 (48 hours).
    MY_WORK_WAITING = 6

    def test_my_work_counts_each_task_that_waits_on_you_once(self):
        data, head = self.head('myWork')
        self.assertEqual(['My work'], head['h1'])
        # the fixture holds what the rule must handle: an own task in both lists, a delivery waiting on its owner
        self.assertIn('a1', data['fixture']['assigned'])
        self.assertIn('a1', data['fixture']['to_review'])
        self.assertIn('b1', data['fixture']['assigned'])
        self.assertIn('a7', data['fixture']['assigned'])
        self.assertNotIn('a7', data['fixture']['to_review'])
        self.assertIn('a8', data['fixture']['assigned'])
        self.assertKey(head['key'], str(self.MY_WORK_WAITING), 'wait on you', 'my-work:distinct-next-step-is-yours')
        self.assertTrue(head['actions'], 'My work must offer its next action')
        first = head['actions'][0]
        self.assertEqual(('Open the oldest review', True, 'a2'), (first['text'], first['primary'], first['next']))
        self.assertTrue(first['href'].endswith('/p/alpha/t/a2'))

    def test_my_work_shows_no_number_when_its_read_is_incomplete(self):
        # the reviewer's MB1: the same lists in a truncated read are not the whole, so no key
        data, head = self.head('myWorkTruncated')
        self.assertIsNone(head['key'])

    def test_tasks_key_is_the_servers_total_not_the_page(self):
        data, head = self.head('tasks')
        self.assertEqual(['Tasks'], head['h1'])
        self.assertIn(data['expected']['project'], data['crumbs'])
        expected = data['expected']
        # The fixture must hold more open tasks than one page, or this test could not tell the two apart.
        self.assertNotEqual(expected['active'], str(expected['activeRows']))
        self.assertKey(head['key'], expected['active'], 'open items', 'page.total')
        self.assertKey(data['afterClosed'], expected['closed'], 'closed item' if expected['closed'] == '1' else 'closed items', 'page.total')
        # the tab names the project again: 'Tasks · <project>' (app.js reads data-tab-title)
        self.assertEqual('Tasks · Customer portal', head['tabTitle'])
        # kittrial-5bb.581: the owner also sees the invite action beside it, never as the primary one.
        self.assertEqual([('New task', True), ('Invite someone or add an agent', False)],
                         [(a['text'], a['primary']) for a in head['actions']])

    def test_tasks_failed_reread_after_a_filter_clears_the_key(self):
        data = observed(self)['tasksFailedReread']
        self.assertTrue(data['before']['shown'])
        self.assertTrue(data['failed']['key'] is None or not data['failed']['key']['shown'], data['failed'])
        self.assertEqual(1, data['failed']['error'])
        # the reviewer's ME1: the failed list offers Try again (else it stays failed)
        self.assertTrue(data['failed']['retry'], 'the failed Tasks read offers no Try again')
        # Try again reads Closed and shows its count
        self.assertKey(data['retried'], data['expected']['closed'], 'closed item' if data['expected']['closed'] == '1' else 'closed items', 'page.total')

    def test_aria_busy_is_on_the_region_being_loaded(self):
        # e r2: the agents' region is busy while read, the loading message is not; neither stays busy after
        data = observed(self)['agentsBusy']
        self.assertEqual({'host': 'true', 'status': [None], 'loading': 1}, data['during'])
        self.assertEqual({'host': None, 'status': [], 'loading': 0}, data['after'])
        self.assertEqual({'host': None, 'status': [], 'loading': 0}, data['afterFailure'])

    def test_one_item_reads_in_the_singular(self):
        # r3: "1 open item", "1 closed item", "1 item in all", "1 matching item"
        self.assertEqual({'open': 'open item', 'closed': 'closed item', 'all': 'item in all', 'matching': 'matching item'},
                         observed(self)['oneItem'])

    def test_tasks_drops_an_answer_that_lands_out_of_order(self):
        data = observed(self)['tasksOutOfOrder']
        self.assertNotEqual(data['expected']['all'], data['expected']['closed'])
        self.assertKey(data['key'], data['expected']['all'], 'items in all', 'page.total')
        self.assertEqual(data['expected']['allIds'], data['rowIds'])

    def test_my_agents_failed_reread_clears_the_key(self):
        # the reviewer's MB2: a re-read that fails must not leave the previous count
        data = observed(self)['agentsFailedReread']
        self.assertKey(data['before'], data['expected']['num'], 'agent' if data['expected']['num'] == '1' else 'agents', 'agents.total')
        self.assertTrue(data['after'] is None or not data['after']['shown'], data['after'])
        self.assertEqual(1, data['error'])

    # Tasks rows (web_page_head_key.mjs, ROWS), read by Alpha's owner (olga), a coordinator (nia) and a
    # contributor (cara). For each row and reader: is it marked as waiting on the reader, and whom does the
    # Who acts next cell name. Each from the row's own facts:
    #   r_rev     sam's delivery, awaiting review: an owner step that asks to act -> the owner and the
    #             coordinator review (You); the contributor cannot (An owner or coordinator)
    #   r_int     sam's approved work: integrate -> the same as r_rev
    #   r_epic    an epic: its owner step only says what it is -> nobody (No one), never marked
    #   r_reqrec  a requirement record that says where it is edited -> No one
    #   r_reqacc  a requirement waiting for acceptance: an owner step that asks to act -> as r_rev
    #   r_held    a held integration: released on the coordination host only -> never marked here
    #   r_held_open a held integration with no assignee: still released on the coordination host only, so
    #             never marked here (only its held state tells it apart from work to assign)
    #   r_epic_assign / r_decision_assign  an epic, and a decision (label 'decision'), under assigned claiming: the
    #             server gives them the coordinator's step, but they are records, not work -> No one (r3, FIXA)
    #   r_assign  unassigned under assigned claiming: an owner or coordinator assigns -> You for the owner
    #             and the coordinator; An owner or coordinator for the contributor
    #   r_mine    cara's task to deliver -> You for cara; her name for the others
    #   r_back    sent back to cara -> the same as r_mine
    #   r_claim   a claim -> Anyone, marked for no one
    #   r_blocked blocked by a dependency: the owner step only says so -> No one
    #   r_error   needs operator attention -> An operator, marked for no one
    #   r_unknown no next step known -> no name (unknown), not marked
    OWNER_STEP, CARA = 'An owner or coordinator', 'Cara Diaz'
    ROW_FACTS = {
        #            owner              coordinator        contributor
        'r_rev':    (('You', True),     ('You', True),     (OWNER_STEP, False)),
        'r_int':    (('You', True),     ('You', True),     (OWNER_STEP, False)),
        'r_epic':   (('No one', False), ('No one', False), ('No one', False)),
        'r_reqrec': (('No one', False), ('No one', False), ('No one', False)),
        'r_reqacc': (('You', True),     ('You', True),     (OWNER_STEP, False)),
        'r_held':   (('A coordinator, on the coordination host', False),) * 3,
        'r_held_open': (('A coordinator, on the coordination host', False),) * 3,
        'r_epic_assign': (('No one', False),) * 3,
        'r_decision_assign': (('No one', False),) * 3,
        'r_assign': (('You', True),     ('You', True),     (OWNER_STEP, False)),
        'r_mine':   ((CARA, False),     (CARA, False),     ('You', True)),
        'r_back':   ((CARA, False),     (CARA, False),     ('You', True)),
        'r_claim':  (('Anyone', False),) * 3,
        'r_blocked': (('No one', False),) * 3,
        'r_error':  (('An operator', False),) * 3,
        'r_unknown': (('unknown', False),) * 3,
    }
    READERS = ('owner', 'coordinator', 'contributor')

    def test_tasks_rows_that_wait_on_you_are_marked(self):
        # the bar, the hidden words and the name come from one answer, for each reader
        seen = observed(self)['rows']
        for column, reader in enumerate(self.READERS):
            self.assertEqual(sorted(self.ROW_FACTS), sorted(seen[reader]), reader)
            for row, facts in self.ROW_FACTS.items():
                name, mine = facts[column]
                got = seen[reader][row]
                with self.subTest(reader=reader, row=row):
                    self.assertEqual((mine, mine, mine), (got['mine'], got['flag'], got['hidden']))
                    self.assertEqual(name, got['name'])
                    # "You" is the accent word, and only on the reader's own rows
                    self.assertEqual(['You'] if mine else [], got['accent'])
                    if name != 'unknown':
                        self.assertTrue(got['cell'].startswith(name + ' · '), got['cell'])

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

    def test_people(self):
        # the member count moved with the members panel to People (.522 slice 2)
        data, head = self.head('people')
        self.assertEqual(['People'], head['h1'])
        self.assertEqual(data['expected']['num'], head['key']['num'])
        self.assertTrue(head['key']['shown'])
        self.assertEqual('member' if data['expected']['num'] == '1' else 'members', head['key']['label'])
        self.assertIn(head['key']['source'], ('members.total', 'members.items'))
        # kittrial-5bb.581: the owner also sees the invite action beside it, never as the primary one.
        self.assertEqual([('Add a member', True, 'add-member'), ('Invite someone or add an agent', False, None)],
                         [(a['text'], a['primary'], a['next']) for a in head['actions']])
        self.assertIn('m-username', data['ids'])

    def test_settings_has_no_key(self):
        data, head = self.head('settings')
        self.assertEqual(['Settings'], head['h1'])
        self.assertIsNone(head['key'])


if __name__ == '__main__':
    unittest.main()
