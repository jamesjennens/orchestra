"""When bd itself says no, that is a refusal and not an outcome nobody knows (kittrial-5bb.185).

A task with an empty title, a title of 600 characters, a change of a task that does not
exist: bd refused each, and the caller was told "the operation may have committed;
reconcile with the same idempotency key", for ever, with the key kept and an audit entry
of outcome `unknown` each time. kittrial-5bb.181 had closed one such case (a change that
changes nothing); these are the rest.

Three layers, each tested here:

* the routes check a title on create as on change, before the key is reserved;
* the endpoint recognises a refusal bd makes before it writes (``bd_refusals``), by its
  form and its sentence together, releases the operation identity and answers a refusal;
* the service answers 404 or 422 for it, and never says of a READ that it "may have
  committed".

The sentences in ``BD_SAID`` are bd 1.2.2's own, copied from a run against the real
binary; ``RealBdTests`` runs the same commands against a real bd when there is one and
compares the tracker before and after.
"""
import json
import os
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT))
sys.path.insert(0, str(KIT / 'tests'))
import bd_refusals
import http_authority
import http_service
import test_bd_label_aliases as rb
import test_http_review_fixes as fixes

WARNING = ('warning: beads.role not configured (GH#2950).\n  Fix: git config beads.role maintainer\n'
           '  Or:  git config beads.role contributor\n')
NOT_FOUND, INVALID = bd_refusals.NOT_FOUND, bd_refusals.INVALID


def json_error(text):
    return json.dumps({'error': text, 'schema_version': 1}, indent=2) + '\n'


#: (what was asked, argv with TASK for a row that exists, exit, stdout, stderr, what it is)
#: as bd 1.2.2 answered on 2026-10-07. ``None``: not known to be a refusal.
BD_SAID = (
    ('create, empty title', ['create', '', '--json'], 1, '',
     WARNING + 'Error: validation failed for issue : title is required\n', INVALID),
    ('create, 501 characters', ['create', 'y' * 501, '--json'], 1, '',
     WARNING + 'Error: validation failed for issue : title must be 500 characters or less (got 501)\n', INVALID),
    ('create, a title that looks like a flag', ['create', '--', '-x', '--json'], 1, '',
     'Error: title "-x" looks like a flag (starts with \'-\').\n  Run \'bd create --help\' for available options.\n'
     '  To use this title anyway, pass it explicitly: bd create --title="-x"\n', INVALID),
    ('create, priority 9', ['create', 'p', '--priority', '9', '--json'], 1, '',
     'Error: invalid priority "9" (expected 0-4 or P0-P4, not words like high/medium/low)\n', INVALID),
    ('update, empty title', ['update', 'TASK', '--title', '', '--json'], 1, json_error('title cannot be empty'), '', INVALID),
    ('update, a status bd does not have', ['update', 'TASK', '--status', 'bogus', '--json'], 1,
     json_error('invalid status "bogus" (built-in: open, in_progress, blocked, deferred, closed, pinned, hooked; '
                "or configure custom statuses via 'bd config set status.custom')"), '', INVALID),
    ('update, a task that does not exist', ['update', 'pp-nope', '--title', 'T', '--json'], 1, '',
     WARNING + 'Error resolving pp-nope: no issue found matching "pp-nope"\n', NOT_FOUND),
    ('close, a task that does not exist', ['close', 'pp-nope', '--json'], 1,
     json_error('resolving ID pp-nope: no issue found matching "pp-nope"'), WARNING, NOT_FOUND),
    ('show, a task that does not exist', ['show', 'pp-nope', '--json'], 1,
     json_error('no issues found matching the provided IDs'),
     WARNING + 'Error fetching pp-nope: no issue found matching "pp-nope"\n', NOT_FOUND),
    ('comments add, a task that does not exist', ['comments', 'add', 'pp-nope', 'text', '--json'], 1,
     json_error('resolving pp-nope: no issue found matching "pp-nope"'), WARNING, NOT_FOUND),
    ('comments add, empty text', ['comments', 'add', 'TASK', '', '--json'], 1, json_error('comment text cannot be empty'), '', INVALID),
    # Not known to be refusals: the database's own sentence, and words with exit 0.
    ('update, a title of 600 characters', ['update', 'TASK', '--title', 'x' * 600, '--json'], 1, '',
     "%s' is too large for column 'title'\n" % ('x' * 600), None),
    ('update, nothing', ['update', 'TASK', '--json'], 0, 'No updates specified\n', '', None),
)


class RecogniserTests(unittest.TestCase):
    def test_what_bd_said_is_read_as_what_it_is(self):
        for label, argv, code, stdout, stderr, kind in BD_SAID:
            with self.subTest(case=label):
                found = bd_refusals.refusal(code, stdout, stderr)
                self.assertEqual(kind, found[0] if found else None)
                if found:
                    self.assertNotIn('\n', found[1])
                    self.assertLessEqual(len(found[1]), bd_refusals.SHOWN)

    def test_the_form_alone_is_not_a_refusal(self):
        """A failure after a write could be printed in the same form: only bd's own sentences count."""
        for code, stdout, stderr in (
                (1, json_error('failed to commit transaction'), ''),
                (1, json_error('database is locked'), ''),
                (1, '', 'Error: failed to commit transaction\n'),
                (1, '', 'Error: dolt: connection refused\n'),
                (1, '', "Error 1105: branch not found\n"),
                (1, '', 'panic: runtime error\n')):
            with self.subTest(said=stdout or stderr):
                self.assertIsNone(bd_refusals.refusal(code, stdout, stderr))

    def test_the_sentence_alone_is_not_a_refusal(self):
        said = 'title cannot be empty'
        for code, stdout, stderr in (
                (0, json_error(said), ''),                                    # it says it succeeded
                (2, json_error(said), ''), (124, json_error(said), ''), (-9, '', 'Error: %s\n' % said),
                (1, json_error(said) + 'and more\n', ''),                      # not the object alone
                (1, json.dumps({'error': said}), ''),                          # not bd's object
                (1, json.dumps({'error': said, 'schema_version': 1, 'id': 'pp-1'}), ''),
                (1, json.dumps({'error': {'text': said}, 'schema_version': 1}), ''),
                (1, json.dumps([{'error': said, 'schema_version': 1}]), ''),
                (1, '{"id": "pp-1"}\n', 'Error: %s\n' % said),                 # something on standard output beside it
                (1, 'Created issue pp-1\n', 'Error: %s\n' % said),             # words there too: it may have written
                (1, '', 'Error: %s\nError: and another\n' % said),             # two errors
                (1, '', said + '\n'),                                          # no "Error: " before it
                (1, '', '  Error: %s\n' % said),
                (1, None, 'Error: %s\n' % said), (1, '', None), (True, '', 'Error: %s\n' % said) if False else (1.5, '', 'Error: %s\n' % said)):
            with self.subTest(code=code, stdout=str(stdout)[:40], stderr=str(stderr)[:40]):
                self.assertIsNone(bd_refusals.refusal(code, stdout, stderr))

    def test_a_sentence_that_only_contains_one_of_bds_is_judged_by_where_it_stands(self):
        self.assertIsNone(bd_refusals.refusal(1, '', 'Error: could not write: validation failed for issue\n'))
        self.assertIsNone(bd_refusals.refusal(1, '', 'Error: commit failed after invalid status was set\n'))
        self.assertIsNone(bd_refusals.refusal(1, json_error('wrote the row; its note cannot be empty, retrying'), ''))
        self.assertIsNone(bd_refusals.refusal(1, '[' * 3000, ''))                   # and never a RecursionError

    def test_other_lines_of_standard_error_do_not_hide_the_one_error(self):
        """bd prints a warning before it and hints after it; a hint may itself speak of errors."""
        said = WARNING + 'Error: title cannot be empty\n  See the list of Error codes with bd help errors\n  hint: Errors are final\n'
        self.assertEqual(bd_refusals.refusal(1, '', said), (INVALID, 'title cannot be empty'))

    def test_a_sentence_that_carries_the_callers_long_text_is_cut(self):
        title = '-' + 'x' * 450
        found = bd_refusals.refusal(1, '', 'Error: title "%s" looks like a flag (starts with \'-\').\n  Run \'bd create --help\'\n' % title)
        self.assertEqual((INVALID, bd_refusals.SHOWN), (found[0], len(found[1])))

    def test_the_envelope_is_a_refusal_with_its_kind_and_bds_sentence(self):
        answer = bd_refusals.envelope(INVALID, 'title cannot be empty')
        self.assertEqual(answer, {'returncode': 2, 'stdout': '', 'stderr': 'ValueError: bd refused: title cannot be empty\n',
                                  'refused': INVALID})


@unittest.skipIf(rb.endpoint is None, 'endpoint imports fcntl (POSIX-only)')
@unittest.skipIf(rb.BD is None, 'no real bd binary (set ORCHESTRA_BD_BIN or put bd on PATH)')
class RealBdTests(rb.RealBdLabelAliasTests):
    """The table against the real binary: what it says, and that the tracker is as it was."""

    def rows(self):
        return json.dumps(sorted(self.export_rows(), key=lambda row: row['id']), sort_keys=True)

    def test_every_refusal_in_the_table_is_still_one_and_wrote_nothing(self):
        task = json.loads(self.bd('create', 'A task', '--json').stdout)['id']
        for label, argv, code, stdout, stderr, kind in BD_SAID:
            with self.subTest(case=label):
                before = self.rows()
                done = self.bd(*[task if token == 'TASK' else token for token in argv])
                found = bd_refusals.refusal(done.returncode, done.stdout, done.stderr)
                self.assertEqual(code, done.returncode, (done.stdout, done.stderr))
                self.assertEqual(kind, found[0] if found else None, (done.stdout, done.stderr))
                if found:
                    self.assertEqual(before, self.rows(), 'bd said no and the tracker changed')

    def test_through_the_endpoint_a_refusal_is_a_refusal_and_keeps_no_identity(self):
        task = json.loads(self.bd('create', 'A task', '--json').stdout)['id']
        before = self.rows()
        for label, argv, expected in (('an empty title', ['update', task, '--title', '', '--json'], INVALID),
                                      ('a status bd does not have', ['update', task, '--status', 'bogus', '--json'], INVALID),
                                      ('a show of nothing', ['show', 'pp-nope', '--json'], NOT_FOUND)):
            with self.subTest(case=label):
                request = {'project': 'pp', 'actor': 'worker', 'action': 'bd', 'args': argv, 'attachments': {},
                           'operation_id': 'op-185-' + label.replace(' ', '-')}
                answer = rb.endpoint.execute(self.root, request)
                self.assertEqual((2, expected), (answer['returncode'], answer.get('refused')), answer)
                self.assertTrue(answer['stderr'].startswith('ValueError: bd refused: '), answer)
                self.assertNotIn('server_time', answer)
                # The identity was released: the same one carries a request bd takes.
                again = rb.endpoint.execute(self.root, dict(request, args=['update', task, '--title', 'T ' + label, '--json']))
                self.assertEqual(0, again['returncode'], again)
        self.assertNotEqual(before, self.rows())

    def test_what_is_not_known_to_be_a_refusal_stays_as_it_was(self):
        task = json.loads(self.bd('create', 'A task', '--json').stdout)['id']
        request = {'project': 'pp', 'actor': 'worker', 'action': 'bd', 'attachments': {},
                   'args': ['update', task, '--title', 'x' * 600, '--json'], 'operation_id': 'op-185-too-long'}
        answer = rb.endpoint.execute(self.root, request)
        self.assertEqual(1, answer['returncode'], answer)
        self.assertNotIn('refused', answer)
        again = rb.endpoint.execute(self.root, dict(request, args=['update', task, '--title', 'short', '--json']))
        self.assertNotEqual(0, again['returncode'], 'an identity whose outcome is unknown was used again')


def message(response):
    return (response.data.get('error') or {}).get('message', '') if isinstance(response.data, dict) else ''


class Project:
    def project(self):
        self.alex, self.pid = self.setup_project()
        self.tasks = '/v1/projects/%s/tasks' % self.pid
        made = self.create_task(self.alex, self.pid, 'as made')
        self.assertEqual(201, made.status, made.data)
        self.tid = made.data['id']
        self.path = self.tasks + '/' + self.tid

    def outcomes(self):
        listed = self.request('GET', '/v1/projects/%s/audit?limit=100' % self.pid, token=self.alex)
        return [entry['outcome'] for entry in listed.data['items']]

    def writes(self, asked):
        return [call.args[3][0] for call in asked.call_args_list if call.args[0] == 'bd' and call.args[3][:1] in (['create'], ['update'])]


class TitleCase(Project, fixes.EndpointCase):
    BAD = (('no title', {}, 'Task title must be text and not empty'),
           ('an empty title', {'title': ''}, 'Task title must be text and not empty'),
           ('a blank title', {'title': '   \t'}, 'Task title must be text and not empty'),
           ('a title that is null', {'title': None}, 'Task title must be text and not empty'),
           ('a title that is not text', {'title': 7}, 'Task title must be text and not empty'),
           ('a title that is a list', {'title': ['x']}, 'Task title must be text and not empty'),
           ('501 characters', {'title': 'y' * 501}, 'Task title must be 500 characters or less (it has 501)'),
           ('600 characters', {'title': 'y' * 600}, 'Task title must be 500 characters or less (it has 600)'),
           ('a description that is not text', {'title': 'fine', 'description': 5}, 'Task description must be text'))

    def test_a_title_bd_would_refuse_is_refused_on_create_before_anything_is_kept_or_sent(self):
        self.project()
        rows = len(self.canonical_rows())
        for number, (label, body, said) in enumerate(self.BAD):
            with self.subTest(body=label):
                key = 'create-key-%04d' % number
                with mock.patch.object(self.backend, '_run', wraps=self.backend._run) as asked:
                    first = self.request('POST', self.tasks, body, token=self.alex, key=key)
                    again = self.request('POST', self.tasks, body, token=self.alex, key=key)
                self.assertEqual((422, 422, said, said), (first.status, again.status, message(first), message(again)), first.data)
                self.assertEqual([], self.writes(asked))
                corrected = self.request('POST', self.tasks, {'title': 'corrected %d' % number}, token=self.alex, key=key)
                self.assertEqual(201, corrected.status, corrected.data)
        self.assertEqual(rows + len(self.BAD), len(self.canonical_rows()))
        self.assertNotIn('unknown', self.outcomes())

    def test_the_same_on_a_change(self):
        self.project()
        for number, (label, body, said) in enumerate(self.BAD[1:]):
            if 'title' not in body or body.get('title') is None:
                continue
            with self.subTest(body=label):
                key = 'change-key-%04d' % number
                refused = self.request('PATCH', self.path, body, token=self.alex, key=key)
                self.assertEqual((422, said), (refused.status, message(refused)), refused.data)
                self.assertEqual(200, self.request('PATCH', self.path, {'title': 'corrected %d' % number},
                                                   token=self.alex, key=key).status)
        self.assertNotIn('unknown', self.outcomes())

    def test_the_longest_title_bd_takes_is_taken(self):
        self.project()
        made = self.request('POST', self.tasks, {'title': 'y' * 500}, token=self.alex)
        self.assertEqual(201, made.status, made.data)
        changed = self.request('PATCH', self.path, {'title': 'z' * 500}, token=self.alex)
        self.assertEqual(200, changed.status, changed.data)
        self.assertEqual(http_service.ApiHandler.TASK_TITLE_MAX, 500)


class BelowTheRouteCase(Project, fixes.EndpointCase):
    """Without the route's check, bd's own refusal comes back as one, and the key is free."""

    def unchecked(self):
        return mock.patch.object(http_service.ApiHandler, '_task_title', lambda self, title: None)

    def test_a_refusal_of_bds_on_create_is_422_with_its_sentence_and_nothing_is_kept(self):
        self.project()
        with self.unchecked():
            for number, (title, said) in enumerate((('', 'title is required'), ('y' * 600, 'title must be 500 characters or less (got 600)'))):
                with self.subTest(title=title[:5]):
                    key = 'below-create-%d' % number
                    first = self.request('POST', self.tasks, {'title': title}, token=self.alex, key=key)
                    again = self.request('POST', self.tasks, {'title': title}, token=self.alex, key=key)
                    self.assertEqual((422, 422), (first.status, again.status), first.data)
                    self.assertEqual('invalid_payload', first.data['error']['code'])
                    self.assertIn('bd refused: validation failed for issue : ' + said, json.dumps(first.data))
                    corrected = self.request('POST', self.tasks, {'title': 'corrected %d' % number}, token=self.alex, key=key)
                    self.assertEqual(201, corrected.status, corrected.data)
        self.assertNotIn('unknown', self.outcomes())

    def test_a_refusal_of_bds_on_a_change_too(self):
        self.project()
        with self.unchecked():
            refused = self.request('PATCH', self.path, {'title': ''}, token=self.alex, key='below-change-1')
            self.assertEqual(422, refused.status, refused.data)
            self.assertIn('bd refused: title cannot be empty', json.dumps(refused.data))
            self.assertEqual(200, self.request('PATCH', self.path, {'title': 'afterwards'}, token=self.alex,
                                               key='below-change-1').status)
        self.assertNotIn('unknown', self.outcomes())

    def test_what_is_not_known_to_be_a_refusal_is_still_an_outcome_nobody_knows(self):
        """The database's own sentence for a title of 600 characters on a change: not one of bd's refusals."""
        self.project()
        with self.unchecked():
            first = self.request('PATCH', self.path, {'title': 'y' * 600}, token=self.alex, key='below-unknown-1')
        self.assertEqual((503, 'uncertain'), (first.status, first.data['error']['code']), first.data)
        self.assertIn('unknown', self.outcomes())


class MissingTaskCase(Project, fixes.EndpointCase):
    def test_a_task_that_does_not_exist_is_404_on_every_route_and_nothing_is_kept(self):
        self.project()
        missing = self.tasks + '/kittrial-5bb.999'
        rows = json.dumps(self.canonical_rows(), sort_keys=True)
        checkpoint = {'intent': 'i', 'acceptance': 'a', 'summary': 's', 'next_action': 'n', 'previous': None, 'activity_cursor': 'x'}
        contribute = dict(fixes.CONTRIBUTION, operation='contribute', schema_version=1, operation_id='op-missing', previous=None)
        for number, (what, method, where, body) in enumerate((
                ('read', 'GET', missing, None), ('change', 'PATCH', missing, {'title': 'x'}),
                ('claim', 'POST', missing + '/claim', {}), ('checkpoint', 'POST', missing + '/checkpoints', checkpoint),
                ('review', 'POST', missing + '/reviews', contribute))):
            with self.subTest(route=what):
                key = None if body is None else 'missing-key-%d' % number
                first = self.request(method, where, body, token=self.alex, key=key)
                again = self.request(method, where, body, token=self.alex, key=key)
                self.assertEqual((404, 404), (first.status, again.status), first.data)
                self.assertEqual(('not_found', 'Task not found'), (first.data['error']['code'], message(first)))
        self.assertEqual(rows, json.dumps(self.canonical_rows(), sort_keys=True))
        self.assertNotIn('unknown', self.outcomes())
        # A key that named a missing task is free for a task that exists.
        self.assertEqual(200, self.request('PATCH', self.path, {'title': 'x'}, token=self.alex, key='missing-key-1').status)


class ReadingTests(unittest.TestCase):
    """A request that could not have written is never said to "may have committed"."""

    checked = staticmethod(lambda reply, **more: http_service.EndpointBackend._checked(reply, 'bd', **more))

    def test_a_read_that_fails_is_unavailable_and_says_that_nothing_was_changed(self):
        for code in (1, 3, 124, 255):
            with self.subTest(code=code):
                with self.assertRaises(http_service.HttpError) as failed:
                    self.checked({'returncode': code, 'stdout': '', 'stderr': 'dolt: connection refused\n'}, reading=True)
                error = failed.exception
                self.assertEqual((503, 'unavailable', http_service.EndpointBackend.UNREAD, True),
                                 (error.status, error.code, error.message, getattr(error, 'nothing_done', False)))
                self.assertNotIn('unknown', error.message)
                self.assertNotIn('committed', error.message)

    def test_a_write_that_fails_the_same_way_is_still_an_outcome_nobody_knows(self):
        for code in (1, 3, 124, 255):
            with self.subTest(code=code):
                with self.assertRaises(http_service.HttpError) as failed:
                    self.checked({'returncode': code, 'stdout': '', 'stderr': 'dolt: connection refused\n'})
                self.assertEqual((503, 'uncertain', False), (failed.exception.status, failed.exception.code,
                                                             getattr(failed.exception, 'nothing_done', False)))

    def test_a_refusal_of_bds_is_404_or_422_reading_or_writing(self):
        for reading in (True, False):
            with self.assertRaises(http_service.HttpError) as missing:
                self.checked(bd_refusals.envelope(NOT_FOUND, 'no issues found matching the provided IDs'), reading=reading)
            self.assertEqual((404, 'Task not found'), (missing.exception.status, missing.exception.message))
            with self.assertRaises(http_service.HttpError) as invalid:
                self.checked(bd_refusals.envelope(INVALID, 'title cannot be empty'), reading=reading)
            self.assertEqual((422, 'ValueError: bd refused: title cannot be empty'), (invalid.exception.status, invalid.exception.detail))
        # The other refusals of the endpoint (its own guards) are as they were.
        with self.assertRaises(http_service.HttpError) as guard:
            self.checked({'returncode': 2, 'stdout': '', 'stderr': 'ValueError: Refusing update\n'}, reading=True)
        self.assertEqual(422, guard.exception.status)

    def test_busy_and_authority_answers_are_as_they_were(self):
        for code, status in ((75, 503), (126, 403)):
            with self.assertRaises(http_service.HttpError) as answer, mock.patch.object(http_service.EndpointBackend, '_log_busy'):
                self.checked({'returncode': code, 'stdout': '', 'stderr': 'x\n'}, reading=True)
            self.assertEqual(status, answer.exception.status)
            self.assertNotEqual('unavailable', answer.exception.code)


class ReadInsideAWriteCase(Project, fixes.EndpointCase):
    def test_a_read_that_fails_before_a_write_answers_unavailable_and_keeps_no_key(self):
        self.project()
        real = self.backend._endpoint

        def failing(action, project, actor, args, *rest, **more):
            if action == 'bd' and args[:1] == ['show']:
                return {'returncode': 1, 'stdout': '', 'stderr': 'dolt: connection refused\n'}
            return real(action, project, actor, args, *rest, **more)
        with mock.patch.object(self.backend, '_endpoint', failing):
            read = self.request('GET', self.path, token=self.alex)
            change = self.request('PATCH', self.path, {'title': 'x'}, token=self.alex, key='read-fails-1')
        for answer in (read, change):
            self.assertEqual((503, 'unavailable', http_service.EndpointBackend.UNREAD),
                             (answer.status, answer.data['error']['code'], message(answer)), answer.data)
        self.assertEqual(200, self.request('PATCH', self.path, {'title': 'afterwards'}, token=self.alex, key='read-fails-1').status)
        self.assertNotIn('unknown', self.outcomes())


class SmallOnesCase(Project, fixes.EndpointCase):
    """From the review of kittrial-5bb.181."""

    def test_the_sentence_of_a_task_change_lists_the_three_fields_and_ends_there(self):
        self.project()
        takes = 'A task change takes: title, description, status'
        for body in ({'priority': 1}, {}, {'title': None}, {'version': 1}, {'priority': 1, 'title': 'x'}):
            with self.subTest(body=body):
                refused = self.request('PATCH', self.path, body, token=self.alex)
                self.assertEqual(422, refused.status, refused.data)
                self.assertTrue(message(refused).endswith(takes), message(refused))
                self.assertEqual(['title', 'description', 'status'], refused.data['error']['detail']['takes'])
                self.assertNotIn('version', message(refused))
                self.assertNotIn('actor', message(refused))

    def test_a_long_or_odd_field_name_is_not_handed_back_in_the_detail_either(self):
        self.project()
        long, odd = 'k' * 300, '<script>alert(1)</script>'
        refused = self.request('PATCH', self.path, {long: 1, odd: 2, 'priority': 3, 'title': 'x'}, token=self.alex)
        self.assertEqual(422, refused.status, refused.data)
        whole = json.dumps(refused.data)
        self.assertNotIn(long, whole)
        self.assertNotIn('alert', whole)
        self.assertEqual(sorted(refused.data['error']['detail']['unsupported']),
                         ['<non-identifier name>', '<non-identifier name>', 'priority'])
        many = {'field_%02d' % n: n for n in range(40)}
        refused = self.request('PATCH', self.path, many, token=self.alex)
        self.assertEqual(http_service.UNSUPPORTED_FIELDS_SHOWN, len(refused.data['error']['detail']['unsupported']))


if __name__ == '__main__':
    unittest.main()
