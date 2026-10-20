"""History paging is exact for any page size (kittrial-5bb.273 item 1, and item 2).

The canonical continuation cursor already points at the first entry the previous page did
not return, but ``EndpointBackend.task_history`` collected a page from that cursor and then
sliced it with the ABSOLUTE HTTP offset, so every continued page skipped a whole page of
entries (a five-comment task at ``limit=2`` returned entries 1-2 and then entry 5 only;
3 and 4 were never returned, and at ``limit=1`` it returned entry 1 then entry 3). A walk
of the whole history at any page size must reconstruct exactly what one whole read gives.

The case runs over ``EndpointBackend`` and the strict canonical stub
(``tests/canonical_endpoint_stub.py`` -> ``tools/strict_canonical_endpoint.py``), which runs
the real ``briefing.history_page`` snapshot cursor over an emulated bd. The in-process
backend only slices a list and never shows the fault, so it is not used here.

One comment is longer than the canonical body budget (4,000 encoded bytes per page), so a
page boundary falls inside a comment and the fragments differ between a whole read and a
paged one. They share ``entry_id`` and carry ``body_offset`` and ``continued``; joined in
offset order they must give the whole comment back at every page size (item 2).
"""
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))

import test_http_review_fixes as fixes  # noqa: E402  (the canonical endpoint harness)

#: Longer than the canonical history body budget (4,000 encoded bytes), so it fragments.
LONG = 'long-comment-' + ('x' * 5000)
COMMENTS = ['first comment', LONG, 'second comment', 'third comment']


def reassemble(items):
    """The history a reader reconstructs: fragments joined by ``entry_id`` in offset order.

    Entry order is first appearance, which is the page order of the walk that produced
    ``items``; fragment order within one entry is ``body_offset`` order. A whole read and a
    paged one may put the fragment boundary at a different character, so only the joined
    body is compared, never the fragment list.
    """
    fragments = {}
    order = []
    for entry in items:
        entry_id = entry['entry_id']
        if entry_id not in fragments:
            fragments[entry_id] = []
            order.append(entry_id)
        fragments[entry_id].append((entry.get('body_offset') or 0, entry.get('body') or ''))
    return [(entry_id, ''.join(text for _, text in sorted(fragments[entry_id])))
            for entry_id in order]


def only_own_tests(cls):
    """Borrow the canonical harness without running its own cases a second time."""
    for name in dir(fixes.CanonicalEndpointCase):
        if name.startswith('test_') and name not in cls.__dict__:
            setattr(cls, name, None)
    return cls


@only_own_tests
class HistoryPagingCase(fixes.CanonicalEndpointCase):
    """The canonical backend over the strict stub: real cursor, real page boundaries."""

    def seed_comments(self, task):
        """Put the comments in the canonical store the way bd would (one row, comments list)."""
        state_path = self.canonical_root / 'canonical.json'
        state = json.loads(state_path.read_text(encoding='utf-8'))
        row = next(row for row in state['rows'] if row['id'] == task)
        row['comments'] = [
            {'id': str(index + 1), 'author': 'alex',
             'created_at': '2026-02-01T00:00:%02dZ' % (index + 1), 'text': text}
            for index, text in enumerate(COMMENTS)]
        state_path.write_text(json.dumps(state), encoding='utf-8')

    def history(self, token, project, task, limit, cursor=None):
        query = '?limit=%d' % limit
        if cursor:
            query += '&cursor=' + cursor
        response = self.request('GET', '/v1/projects/%s/tasks/%s/history%s'
                                % (project, task, query), token=token)
        self.assertEqual(200, response.status, response.data)
        return response.data

    def test_continued_pages_reconstruct_the_whole_read_at_two_page_sizes(self):
        alex, project = self._setup_project()
        created = self.create_task(alex, project, 'a task with several comments',
                                   key='history-paging-0001')
        self.assertEqual(201, created.status, created.data)
        task = created.data['id']
        self.seed_comments(task)

        whole = self.history(alex, project, task, 100)
        expected = reassemble(whole['items'])
        self.assertEqual(COMMENTS, [body for _, body in expected])
        # The long comment DID fragment: the paged boundary case is really exercised here.
        self.assertGreater(len(whole['items']), len(COMMENTS))
        self.assertIsNone(whole['next_cursor'])
        self.assertEqual(len(COMMENTS), whole['total'])

        for size in (1, 2):
            with self.subTest(limit=size):
                walked, cursor, pages = [], None, 0
                while pages < 200:
                    pages += 1
                    page = self.history(alex, project, task, size, cursor)
                    self.assertEqual(len(COMMENTS), page['total'])
                    walked.extend(page['items'])
                    cursor = page.get('next_cursor')
                    if not cursor:
                        break
                else:
                    self.fail('the walk did not finish')
                self.assertEqual(expected, reassemble(walked),
                                 'a continued page at limit=%d skipped or reordered entries' % size)

    def test_a_continued_page_resumes_at_the_next_entry(self):
        """The defect as reported: limit=2 once gave entries 1-2 then entry 5, never 3 and 4."""
        alex, project = self._setup_project()
        created = self.create_task(alex, project, 'skip check', key='history-paging-0002')
        self.assertEqual(201, created.status, created.data)
        task = created.data['id']
        self.seed_comments(task)

        first = self.history(alex, project, task, 2)
        self.assertEqual('first comment', first['items'][0]['body'])
        self.assertTrue(first['next_cursor'])
        second = self.history(alex, project, task, 2, first['next_cursor'])
        bodies = [body for _, body in reassemble(first['items'] + second['items'])]
        # The first entry the first page did not return, and the long comment's remainder.
        self.assertIn('second comment', bodies)
        self.assertIn(LONG, bodies)


#: kittrial-5bb.353: 150 entries, one of them long enough to fragment, placed so its fragments
#: straddle the canonical 20-entry page boundary and the HTTP page boundary of most walks below.
MANY = ['comment %03d' % index for index in range(150)]
MANY[75] = 'long-comment-75-' + ('y' * 9000)


@only_own_tests
class RecentHistoryCase(HistoryPagingCase):
    """``recent=N``: the newest entries first, a cursor back to the oldest (kittrial-5bb.353).

    Office agents could not read a coordinator's newest comment: the history route paged only
    oldest first, and a client that read the first page of a long history never saw the end.
    """

    def seed(self, task, texts, start=1, stamp='2026-02-01T%02d:%02d:%02dZ'):
        state_path = self.canonical_root / 'canonical.json'
        state = json.loads(state_path.read_text(encoding='utf-8'))
        row = next(row for row in state['rows'] if row['id'] == task)
        row.setdefault('comments', [])
        for offset, text in enumerate(texts):
            n = start + offset
            row['comments'].append({'id': 'seeded-%d' % n, 'author': 'alex', 'text': text,
                                    'created_at': stamp % (n // 3600, (n // 60) % 60, n % 60)})
        state_path.write_text(json.dumps(state), encoding='utf-8')

    def read(self, token, project, task, query, cursor=None, status=200):
        path = '/v1/projects/%s/tasks/%s/history?%s' % (project, task, query)
        if cursor:
            path += '&cursor=' + cursor
        response = self.request('GET', path, token=token)
        self.assertEqual(status, response.status, response.data)
        return response.data

    def walk(self, token, project, task, query):
        items, cursor, pages = [], None, 0
        while pages < 400:
            pages += 1
            page = self.read(token, project, task, query, cursor)
            items.extend(page['items'])
            cursor = page.get('next_cursor')
            if not cursor:
                return items, page
        self.fail('the walk did not finish')

    def a_task(self, key):
        alex, project = self._setup_project()
        created = self.create_task(alex, project, 'a task with a long history', key=key)
        self.assertEqual(201, created.status, created.data)
        return alex, project, created.data['id']

    def test_the_newest_come_first_and_the_cursor_reaches_the_oldest_exactly(self):
        alex, project, task = self.a_task('history-recent-0001')
        self.seed(task, MANY)
        oldest, _ = self.walk(alex, project, task, 'limit=100')
        expected = reassemble(oldest)
        self.assertEqual(MANY, [body for _, body in expected])
        first = self.read(alex, project, task, 'recent=20')
        self.assertEqual('newest-first', first['order'])
        self.assertFalse(first['truncated'])
        self.assertEqual(150, first['total'])
        self.assertEqual(['comment 149', 'comment 148', 'comment 147'], [e['body'] for e in first['items'][:3]])
        self.assertTrue(first['next_cursor'])
        for size in (7, 20, 100):
            with self.subTest(recent=size):
                walked, last = self.walk(alex, project, task, 'recent=%d' % size)
                newest_first = reassemble(walked)
                # No skip, no repeat: every entry once, newest first, the long one whole.
                self.assertEqual(len(MANY), len(newest_first))
                self.assertEqual(list(reversed(expected)), newest_first)
                self.assertEqual('comment 000', newest_first[-1][1])
                self.assertIsNone(last['next_cursor'])

    def test_recent_and_oldest_first_pages_cover_the_same_entries(self):
        # The coordinator's addition: the two directions read the same set, fragment for fragment.
        alex, project, task = self.a_task('history-recent-0002')
        self.seed(task, MANY)
        newest, _ = self.walk(alex, project, task, 'recent=13')
        oldest, _ = self.walk(alex, project, task, 'limit=13')
        self.assertEqual(sorted(reassemble(newest)), sorted(reassemble(oldest)))
        self.assertEqual({e['entry_id'] for e in newest}, {e['entry_id'] for e in oldest})

    def test_a_long_comment_keeps_its_fragments_together_and_in_order_newest_first(self):
        alex, project, task = self.a_task('history-recent-0003')
        self.seed(task, MANY)
        walked, _ = self.walk(alex, project, task, 'recent=3')
        long_id = next(e['entry_id'] for e in walked if e['body'].startswith('long-comment-75-'))
        positions = [i for i, e in enumerate(walked) if e['entry_id'] == long_id]
        self.assertGreater(len(positions), 1, 'the long comment did not fragment')
        self.assertEqual(list(range(positions[0], positions[0] + len(positions))), positions)   # together
        offsets = [walked[i]['body_offset'] for i in positions]
        self.assertEqual(sorted(offsets), offsets)                                              # in body order
        self.assertEqual(MANY[75], ''.join(walked[i]['body'] for i in positions))

    def test_a_cursor_continues_only_its_own_direction(self):
        alex, project, task = self.a_task('history-recent-0004')
        self.seed(task, MANY[:30])
        newest = self.read(alex, project, task, 'recent=5')
        oldest = self.read(alex, project, task, 'limit=5')
        refused = self.read(alex, project, task, 'limit=5', newest['next_cursor'], status=409)
        self.assertIn('Cursor is stale or belongs to a different query', json.dumps(refused))
        self.read(alex, project, task, 'recent=5', oldest['next_cursor'], status=409)
        self.read(alex, project, task, 'recent=6', newest['next_cursor'], status=409)

    def test_one_sentence_refusals(self):
        alex, project, task = self.a_task('history-recent-0005')
        for query, said in (('recent=0', 'recent must be between 1 and 100'),
                            ('recent=101', 'recent must be between 1 and 100'),
                            ('recent=x', 'recent must be an integer'),
                            ('recent=5&limit=5', 'Give limit or recent, not both'),
                            ('since_checkpoint=yes', 'since_checkpoint must be true or false')):
            with self.subTest(query=query):
                response = self.request('GET', '/v1/projects/%s/tasks/%s/history?%s' % (project, task, query), token=alex)
                self.assertGreaterEqual(response.status, 400, response.data)
                self.assertIn(said, json.dumps(response.data))

    def test_since_checkpoint_selects_what_the_newest_checkpoint_did_not_incorporate(self):
        alex, project, task = self.a_task('history-recent-0006')
        # Before the checkpoint on the harness's clock (it writes the checkpoint at 2026-01-01), and after it.
        self.seed(task, ['before %d' % n for n in range(5)], stamp='2025-06-01T%02d:%02d:%02dZ')
        self.assertEqual(200, self.request('POST', '/v1/projects/%s/tasks/%s/claim' % (project, task), {}, token=alex).status)
        cursor = self.read(alex, project, task, 'limit=5')['activity_cursor']
        checkpoint = self.request(
            'POST', '/v1/projects/%s/tasks/%s/checkpoints' % (project, task),
            {'schema_version': 1, 'previous': None, 'activity_cursor': cursor, 'source_commit': fixes.COMMIT,
             'branch': 'contrib/test', 'intent': 'a baseline', 'acceptance': 'a baseline',
             'summary': 'the baseline', 'next_action': 'read what came after', 'open_items': [], 'resolved': []},
            token=alex)
        self.assertEqual(201, checkpoint.status, checkpoint.data)
        self.seed(task, ['after %d' % n for n in range(4)], start=100, stamp='2099-01-01T%02d:%02d:%02dZ')
        for query, order in (('since_checkpoint=true&limit=50', 'oldest-first'),
                             ('since_checkpoint=true&recent=2', 'newest-first')):
            with self.subTest(query=query):
                walked, last = self.walk(alex, project, task, query)
                bodies = [body for _, body in reassemble(walked)]
                self.assertEqual(order, last['order'])
                self.assertTrue(last['since_checkpoint'])
                self.assertFalse([b for b in bodies if b.startswith('before ')], bodies)
                afters = [b for b in bodies if b.startswith('after ')]
                self.assertEqual(['after 0', 'after 1', 'after 2', 'after 3'][::(-1 if order == 'newest-first' else 1)], afters)


import test_http_service as service_harness  # noqa: E402  (the in-process backend harness)


class InProcessRecentCase(service_harness.ServerHarness):
    """The in-process backend has no continuation cursor: only the first page is reachable.

    The coordinator's addition to kittrial-5bb.353: when more entries exist than the answer
    returns, ``recent`` (or an oldest-first page) must SAY so (``truncated`` and one
    sentence), never look complete.
    """

    def task_with_events(self, count):
        admin = self.admin_token()
        self.create_account(admin, 'alex', 'alex-password-1')
        alex = self.login('alex', 'alex-password-1')[0]
        project = self.create_project(alex, 'Alpha')
        task = self.create_task(alex, project, 'many events').data['id']
        events = self.backend.state.setdefault('events', [])
        for n in range(count):
            events.append({'time': '2026-02-01T00:%02d:%02dZ' % (n // 60, n % 60), 'project': project,
                           'task': task, 'action': 'note-%03d' % n, 'user_id': None, 'actor': 'alex'})
        return alex, project, task

    def get(self, token, project, task, query):
        response = self.request('GET', '/v1/projects/%s/tasks/%s/history?%s' % (project, task, query), token=token)
        self.assertEqual(200, response.status, response.data)
        return response.data

    def test_recent_is_newest_first_and_says_what_it_cannot_reach(self):
        alex, project, task = self.task_with_events(130)
        page = self.get(alex, project, task, 'recent=100')
        notes = [e['action'] for e in page['items'] if e.get('action', '').startswith('note-')]
        self.assertEqual('newest-first', page['order'])
        self.assertEqual('note-129', notes[0])
        self.assertEqual(sorted(notes, reverse=True), notes)
        self.assertIsNone(page['next_cursor'])
        self.assertTrue(page['truncated'])
        hidden = page['total'] - len(page['items'])
        self.assertGreater(hidden, 0)
        self.assertEqual('%d of %d entries are not in this answer and this server cannot page to them: '
                         "read them with the client's history command." % (hidden, page['total']),
                         page['truncated_note'])

    def test_a_page_that_holds_everything_is_not_truncated(self):
        alex, project, task = self.task_with_events(10)
        for query, order in (('recent=100', 'newest-first'), ('limit=100', 'oldest-first')):
            with self.subTest(query=query):
                page = self.get(alex, project, task, query)
                self.assertEqual(order, page['order'])
                self.assertFalse(page['truncated'])
                self.assertNotIn('truncated_note', page)
                self.assertEqual(page['total'], len(page['items']))

    def test_since_checkpoint_keeps_what_came_after_the_newest_checkpoint(self):
        alex, project, task = self.task_with_events(3)
        events = self.backend.state['events']
        events.append({'time': '2026-02-02T00:00:00Z', 'project': project, 'task': task,
                       'action': 'checkpoint-added', 'user_id': None, 'actor': 'alex'})
        events.append({'time': '2026-02-03T00:00:00Z', 'project': project, 'task': task,
                       'action': 'note-after', 'user_id': None, 'actor': 'blair'})
        page = self.get(alex, project, task, 'since_checkpoint=true&recent=10')
        self.assertEqual(['note-after'], [e['action'] for e in page['items']])
        self.assertEqual('2026-02-02T00:00:00Z', page['checkpoint_timestamp'])


if __name__ == '__main__':
    unittest.main()
