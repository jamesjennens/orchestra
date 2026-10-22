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

    def test_recent_counts_logical_entries_when_long_comments_fragment(self):
        alex, project, task = self.a_task('history-recent-0007')
        texts = ['short %02d' % i for i in range(15)]
        texts[14] = 'long-14-' + ('a' * 9000)
        texts[11] = 'long-11-' + ('b' * 9000)
        self.seed(task, texts)
        first = self.read(alex, project, task, 'recent=10')
        first_logical = reassemble(first['items'])
        # 10 logical entries, even though two of them split into 3 fragments each (14 items total).
        self.assertEqual(10, len(first_logical))
        self.assertGreater(len(first['items']), 10)
        self.assertFalse(first['items'][-1]['continued'])
        self.assertEqual([texts[i] for i in range(14, 4, -1)], [body for _, body in first_logical])
        self.assertTrue(first['next_cursor'])
        second = self.read(alex, project, task, 'recent=10', first['next_cursor'])
        second_logical = reassemble(second['items'])
        self.assertEqual(5, len(second_logical))
        self.assertEqual([texts[i] for i in range(4, -1, -1)], [body for _, body in second_logical])
        self.assertIsNone(second['next_cursor'])

    def test_cursor_is_signed_tampering_is_refused_and_unsigned_restarts_the_read(self):
        import base64
        from http_auth import Store, Service, request_hash
        from http_service import CURSOR_BAD_SIGNATURE, cursor_scope
        alex, project, task = self.a_task('history-recent-0008')
        self.seed(task, MANY[:25])
        first = self.read(alex, project, task, 'recent=5')
        cursor = first['next_cursor']
        self.assertTrue(cursor)
        padded = cursor + '=' * (-len(cursor) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(padded.encode('ascii')))

        # 1. Editing the offset in the cursor payload is refused with one sentence (409).
        edited_offset = dict(decoded, o=2)
        tampered = base64.urlsafe_b64encode(
            json.dumps(edited_offset, separators=(',', ':'), sort_keys=True).encode('utf-8')
        ).decode('ascii').rstrip('=')
        refused = self.read(alex, project, task, 'recent=5', tampered, status=409)
        self.assertEqual(CURSOR_BAD_SIGNATURE, refused['error']['message'])

        # 2. Editing the signature itself is refused with the same one sentence (409).
        bad_sig = dict(decoded, s=('A' if decoded['s'][0] != 'A' else 'B') + decoded['s'][1:])
        tampered_sig = base64.urlsafe_b64encode(
            json.dumps(bad_sig, separators=(',', ':'), sort_keys=True).encode('utf-8')
        ).decode('ascii').rstrip('=')
        refused_sig = self.read(alex, project, task, 'recent=5', tampered_sig, status=409)
        self.assertEqual(CURSOR_BAD_SIGNATURE, refused_sig['error']['message'])

        # 3. Old unsigned cursor (both legacy {'u','p','q','o'} and stripped-'s') restarts the read.
        alex_id = next(uid for uid, u in self.service.state['users'].items() if u.get('username') == 'alex')
        legacy_payload = {
            'u': alex_id,
            'p': project,
            'q': request_hash(cursor_scope({'recent': '5'}))[:16],
            'o': 5,
        }
        legacy_cursor = base64.urlsafe_b64encode(
            json.dumps(legacy_payload, separators=(',', ':')).encode('utf-8')
        ).decode('ascii').rstrip('=')
        restarted = self.read(alex, project, task, 'recent=5', legacy_cursor, status=200)
        self.assertEqual([e['entry_id'] for e in first['items']],
                         [e['entry_id'] for e in restarted['items']])

        stripped_payload = {k: v for k, v in decoded.items() if k != 's'}
        stripped_cursor = base64.urlsafe_b64encode(
            json.dumps(stripped_payload, separators=(',', ':')).encode('utf-8')
        ).decode('ascii').rstrip('=')
        restarted_stripped = self.read(alex, project, task, 'recent=5', stripped_cursor, status=200)
        self.assertEqual([e['entry_id'] for e in first['items']],
                         [e['entry_id'] for e in restarted_stripped['items']])

        # 4. A backup/reload of the store preserves cursor_key so existing cursors stay valid;
        # rotating cursor_key refuses old signed cursors and only requires restarting the read.
        self.service.store.save()
        reloaded = Service(Store(self.service.store.path))
        self.assertEqual(self.service.cursor_key(), reloaded.cursor_key())
        continued = self.read(alex, project, task, 'recent=5', cursor, status=200)
        self.assertEqual(['comment 019', 'comment 018', 'comment 017', 'comment 016', 'comment 015'],
                         [e['body'] for e in continued['items']])

        self.service.state['cursor_key'] = 'f' * 64
        rotated_refused = self.read(alex, project, task, 'recent=5', cursor, status=409)
        self.assertEqual(CURSOR_BAD_SIGNATURE, rotated_refused['error']['message'])
        fresh = self.read(alex, project, task, 'recent=5', status=200)
        fresh_continued = self.read(alex, project, task, 'recent=5', fresh['next_cursor'], status=200)
        self.assertEqual([e['entry_id'] for e in continued['items']],
                         [e['entry_id'] for e in fresh_continued['items']])

    def test_cursor_stays_compact_for_longest_valid_project_and_task_ids(self):
        from http_service import MAX_CURSOR, make_cursor, read_cursor
        alex, project, _ = self.a_task('history-recent-0008')
        long_task = 't' + ('a' * 127)
        self.assertEqual(128, len(long_task))
        state_path = self.canonical_root / 'canonical.json'
        state = json.loads(state_path.read_text(encoding='utf-8'))
        template = dict(state['rows'][0], id=long_task, title='longest valid task id',
                        comments=[{'id': '%s-c%04d' % (long_task, i), 'author': 'alex',
                                   'created_at': '2025-06-01T00:00:%02dZ' % (i + 1),
                                   'text': 'before %d' % i} for i in range(6)])
        state['rows'].append(template)
        state_path.write_text(json.dumps(state), encoding='utf-8')
        self.assertEqual(200, self.request('POST', '/v1/projects/%s/tasks/%s/claim' % (project, long_task), {}, token=alex).status)
        act_cursor = self.read(alex, project, long_task, 'limit=5')['activity_cursor']
        cp = self.request(
            'POST', '/v1/projects/%s/tasks/%s/checkpoints' % (project, long_task),
            {'schema_version': 1, 'previous': None, 'activity_cursor': act_cursor, 'source_commit': fixes.COMMIT,
             'branch': 'contrib/test', 'intent': 'baseline', 'acceptance': 'baseline',
             'summary': 'baseline', 'next_action': 'continue', 'open_items': [], 'resolved': []},
            token=alex)
        self.assertEqual(201, cp.status, cp.data)
        self.seed(long_task, ['after %d' % i for i in range(10)], start=100, stamp='2099-01-01T%02d:%02d:%02dZ')

        for query in ('recent=3', 'limit=3', 'since_checkpoint=true&recent=3'):
            with self.subTest(query=query):
                first = self.read(alex, project, long_task, query)
                cursor = first['next_cursor']
                self.assertTrue(cursor)
                self.assertLess(len(cursor), MAX_CURSOR // 2,
                                'cursor length %d is not well under MAX_CURSOR=%d' % (len(cursor), MAX_CURSOR))
                second = self.read(alex, project, long_task, query, cursor)
                self.assertTrue(second['items'])

        # Direct check with both a 128-char project ID and a 128-char task ID.
        long_project = 'p' + ('b' * 127)
        principal = self.service.authenticate(alex)
        direct = make_cursor(principal, long_project + '/' + long_task, {'recent': '20'}, 20,
                             first['next_cursor'], key=self.service.cursor_key())
        self.assertLess(len(direct), MAX_CURSOR)
        unpacked = read_cursor(principal, long_project + '/' + long_task, {'recent': '20'}, direct,
                               key=self.service.cursor_key())
        self.assertEqual(20, unpacked['o'])

    def test_cursor_key_never_appears_in_route_answers_or_audit(self):
        import base64
        alex, project, task = self.a_task('history-recent-0009')
        self.seed(task, MANY[:10])
        secret = self.service.cursor_key()
        self.assertGreaterEqual(len(secret), 64)
        admin = self.admin_token()
        for path, tok in (
            ('/healthz', None),
            ('/v1/sessions/current', alex),
            ('/v1/me/work', alex),
            ('/v1/projects', alex),
            ('/v1/projects/%s' % project, alex),
            ('/v1/projects/%s/tasks?limit=1' % project, alex),
            ('/v1/projects/%s/tasks/%s' % (project, task), alex),
            ('/v1/projects/%s/tasks/%s/brief' % (project, task), alex),
            ('/v1/projects/%s/tasks/%s/history?recent=3' % (project, task), alex),
            ('/v1/projects/%s/queue' % project, alex),
            ('/v1/projects/%s/audit' % project, admin),
            ('/v1/agents', alex),
            ('/v1/docs', alex),
        ):
            response = self.request('GET', path, token=tok)
            self.assertEqual(200, response.status, (path, response.data))
            serialized = json.dumps(response.data)
            self.assertNotIn(secret, serialized, path)
            next_cursor = response.data.get('next_cursor') if isinstance(response.data, dict) else None
            if next_cursor:
                padded = next_cursor + '=' * (-len(next_cursor) % 4)
                raw = base64.urlsafe_b64decode(padded.encode('ascii')).decode('utf-8')
                self.assertNotIn(secret, raw, path)
        self.assertNotIn(secret, json.dumps(self.service.state['audit']))


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

    def test_recent_counts_logical_entries_and_keeps_fragments_in_body_order(self):
        alex, project, task = self.task_with_events(0)
        events = [e for e in self.backend.state['events'] if not (e.get('project') == project and e.get('task') == task)]
        for n in range(12):
            if n == 10:
                for frag_idx, text in enumerate(('part-0-', 'part-1-', 'part-2')):
                    events.append({'time': '2026-02-01T00:00:10Z', 'project': project, 'task': task,
                                   'action': 'note-010', 'entry_id': 'e-10', 'body': text,
                                   'body_offset': frag_idx * 7, 'continued': frag_idx < 2,
                                   'user_id': None, 'actor': 'alex'})
            else:
                events.append({'time': '2026-02-01T00:00:%02dZ' % n, 'project': project, 'task': task,
                               'action': 'note-%03d' % n, 'entry_id': 'e-%d' % n, 'body': 'body-%d' % n,
                               'body_offset': 0, 'continued': False, 'user_id': None, 'actor': 'alex'})
        self.backend.state['events'] = events
        page = self.get(alex, project, task, 'recent=10')
        logical = reassemble(page['items'])
        self.assertEqual(10, len(logical))
        self.assertEqual(12, len(page['items']))
        self.assertEqual(12, page['total'])
        self.assertEqual('part-0-part-1-part-2', dict(logical)['e-10'])
        self.assertTrue(page['truncated'])
        self.assertIn('2 of 12 entries are not in this answer', page['truncated_note'])


if __name__ == '__main__':
    unittest.main()
