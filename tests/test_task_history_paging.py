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


if __name__ == '__main__':
    unittest.main()
