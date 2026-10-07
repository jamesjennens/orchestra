"""One row the tracker returns that the kit does not read must not fail the web service (kittrial-5bb.169, item 1).

A row whose metadata nests a few thousand levels made `json.loads` raise RecursionError on
Python 3.10, which is not the ValueError the service's parser caught: the task list, the
page, brief and history of that row, the project's setup page and a change of the row all
answered 500. The service takes such an answer apart row by row, names the row it does
not read in the words of the endpoint's readers (kittrial-5bb.141), and says so on that
row's own routes.

Which row is not read is the kit's rule, counted (deeper than record_json.ROW_NESTING_MAX),
not what the interpreter running these tests can parse: Python 3.13 parses 3,000 levels,
and the first version, which waited for the RecursionError, marked nothing there. No test
here depends on where `json.loads` gives up: the rows are 751, 760 and 3,000 levels deep,
and the parser is also run with a `json.loads` that never gives up and with one that
gives up early.
"""
import json
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT))
sys.path.insert(0, str(KIT / 'tests'))
import http_service
import record_json
import test_http_agents
from http_service import _canonical_payload, _elements, _parse_native, _row_id

MAX = record_json.ROW_NESTING_MAX


def nested(levels):
    """A JSON value of that many levels of nesting."""
    return '{"k":' * levels + '1' + '}' * levels


# A row is one level itself, so metadata of N levels makes a row of N + 1.
DEEP = nested(3000)
JUST_OVER = nested(MAX)                 # the row is MAX + 1 levels: the first that is not read
AT_THE_BOUND = nested(MAX - 1)          # the row is exactly MAX levels: the last that is read
SENTENCE = ('Task %s exists, but its row cannot be read (it is malformed or nested too deeply). Ask an operator of the '
            'server to repair it.')


def row(number, metadata='{}', title=None):
    return json.dumps({'id': 'pp-%d' % number, 'title': title or 'Task %d' % number, 'status': 'open',
                       'labels': ['a]', '{b'], 'description': 'with "quotes", a \\ and ]}'})[:-1] + ', "metadata": %s}' % metadata


def listing(*rows, indent=True):
    return ('[\n  ' + ',\n  '.join(rows) + '\n]') if indent else '[' + ','.join(rows) + ']'


def marker(number):
    return {'id': 'pp-%d' % number, 'malformed': True, 'unreadable': True, 'error': 'Malformed issue row', 'status': 'unknown'}


def parses_any_depth(text):
    """A `json.loads` that never gives up, as on an interpreter with no limit near these depths."""
    import ast
    limit = sys.getrecursionlimit()
    sys.setrecursionlimit(200000)
    try:
        return REAL_LOADS(text)
    except RecursionError:
        # This interpreter cannot go that deep whatever its limit says: stand in with a value that only
        # says "parsed". The tests that use this compare markers and ids, never this value.
        return {'id': http_service._row_id(text), 'parsed_by': 'an interpreter without a limit'} if text.lstrip()[:1] == '{' else ast.literal_eval('[]')
    finally:
        sys.setrecursionlimit(limit)


REAL_LOADS = json.loads


class RuleTests(unittest.TestCase):
    """The rule is the kit's, from one place, and does not depend on the interpreter."""

    def test_the_bound_is_the_one_constant_the_endpoints_readers_use(self):
        self.assertEqual(record_json.ROW_NESTING_MAX, 750)
        source = (KIT / 'http_service.py').read_text(encoding='utf-8')
        for judged in ('record_json.nesting(text, record_json.ROW_NESTING_MAX) <= record_json.ROW_NESTING_MAX',
                       'record_json.nesting(piece, record_json.ROW_NESTING_MAX) > record_json.ROW_NESTING_MAX'):
            self.assertEqual(source.count(judged), 1, judged)                # a whole answer, and each row of it
        self.assertNotIn('750', source[source.index('def _row('):source.index('def _canonical_payload')])
        self.assertEqual(record_json.nesting(nested(10), 5), 6)
        self.assertEqual(record_json.nesting(nested(5), 5), 0)                # not counted when it cannot exceed
        self.assertEqual((record_json.nesting(nested(64)), record_json.nesting(nested(65))), (0, 65))    # the default is what it was

    def test_a_row_at_the_bound_is_read_and_one_level_deeper_is_not(self):
        at, over = row(1, AT_THE_BOUND), row(2, JUST_OVER)
        self.assertEqual((record_json.nesting(at, MAX), record_json.nesting(over, MAX)), (MAX, MAX + 1))
        parsed = _parse_native(listing(at, over, row(3)))
        self.assertEqual([r.get('malformed') for r in parsed], [None, True, None])
        self.assertEqual(parsed[1], marker(2))
        self.assertEqual(parsed[0]['title'], 'Task 1')
        self.assertEqual(_parse_native(over), marker(2))
        self.assertEqual(_parse_native(at)['id'], 'pp-1')
        self.assertEqual(_parse_native(listing(at))[0]['id'], 'pp-1')

    def test_a_row_this_interpreter_could_parse_is_not_read_all_the_same(self):
        """760 levels: every supported interpreter parses it. The first version read it; 3.13 read 3,000."""
        over = row(2, nested(760))
        self.assertEqual(REAL_LOADS(over)['id'], 'pp-2')                    # it CAN be parsed here
        self.assertEqual(_parse_native(listing(row(1), over)), [REAL_LOADS(row(1)), marker(2)])
        self.assertEqual(_parse_native(over), marker(2))

    def test_the_same_rows_are_marked_whatever_the_interpreter_can_parse(self):
        texts = {'3000': listing(row(1), row(2, DEEP), row(3)), 'just over': listing(row(1, JUST_OVER), row(2)),
                 'alone': row(7, DEEP), 'all': listing(row(1, DEEP), row(2, JUST_OVER))}
        expected = {'3000': [None, True, None], 'just over': [True, None], 'alone': [True], 'all': [True, True]}

        def gives_up_early(text):
            if record_json.nesting(text, 100) > 100:
                raise RecursionError('maximum recursion depth exceeded')
            return REAL_LOADS(text)
        for name, loads in (('this interpreter', REAL_LOADS), ('one that never gives up', parses_any_depth),
                            ('one that gives up at 100 levels', gives_up_early)):
            for label, text in texts.items():
                with self.subTest(interpreter=name, text=label), mock.patch.object(http_service.json, 'loads', loads):
                    parsed = _parse_native(text)
                    parsed = parsed if isinstance(parsed, list) else [parsed]
                    self.assertEqual([r.get('malformed') for r in parsed], expected[label])

    def test_an_interpreter_that_gives_up_below_the_bound_marks_the_row_instead_of_failing(self):
        """The second line of defence: a row within the kit's bound that this interpreter still cannot parse."""
        shallow = row(2, nested(200))

        def gives_up_early(text):
            if record_json.nesting(text, 100) > 100:
                raise RecursionError('maximum recursion depth exceeded')
            return REAL_LOADS(text)
        with mock.patch.object(http_service.json, 'loads', gives_up_early):
            self.assertEqual(_parse_native(listing(row(1), shallow, row(3))), [REAL_LOADS(row(1)), marker(2), REAL_LOADS(row(3))])
            self.assertEqual(_parse_native(shallow), marker(2))


class ParserTests(unittest.TestCase):
    def test_the_readable_rows_are_parsed_and_the_other_is_named_wherever_it_stands(self):
        for position in (0, 1, 2):
            rows = [row(n, DEEP if index == position else '{}') for index, n in enumerate((1, 2, 3))]
            for indent in (True, False):
                with self.subTest(position=position, indent=indent):
                    parsed = _parse_native(listing(*rows, indent=indent))
                    self.assertEqual([r['id'] for r in parsed], ['pp-1', 'pp-2', 'pp-3'])
                    self.assertEqual(parsed[position], marker(position + 1))
                    for index, readable in enumerate(parsed):
                        if index != position:
                            self.assertEqual((readable['title'], readable['labels'], readable.get('malformed')),
                                             ('Task %d' % (index + 1), ['a]', '{b'], None))

    def test_several_unreadable_rows_and_all_of_them(self):
        parsed = _parse_native(listing(row(1, DEEP), row(2), row(3, DEEP)))
        self.assertEqual([r.get('malformed') for r in parsed], [True, None, True])
        self.assertEqual(_parse_native(listing(row(1, DEEP), row(2, DEEP))), [marker(1), marker(2)])

    def test_one_row_alone_as_bd_show_may_print_it(self):
        self.assertEqual(_parse_native(row(7, DEEP)), marker(7))
        self.assertEqual(_parse_native(listing(row(7, DEEP))), [marker(7)])

    def test_a_text_within_the_bound_is_what_json_loads_makes_of_it(self):
        for text in (listing(row(1), row(2)), '[]', '{"a": 1}', '"text"', '7', 'null', '[1, [2, [3]], "]"]',
                     listing(row(1, nested(500)), row(2))):
            with self.subTest(text=text[:20]):
                self.assertEqual(_parse_native(text), json.loads(text))

    def test_a_text_that_is_not_json_is_refused_as_before(self):
        for text in ('nonsense', '[{"id": "pp-1"}', '[{"id": "pp-1"}] and more', '', '[' + row(1, DEEP), row(1, DEEP)[:-5],
                     listing(row(1), row(2, DEEP)) + ' trailing'):
            with self.subTest(text=text[:30]), self.assertRaises(ValueError):
                _parse_native(text)

    def test_a_deep_answer_that_is_not_a_clean_list_of_rows_is_refused(self):
        """Review: beside a deep row the scan passed over text between the rows that json.loads refuses,
        and a deep error object became a marker row."""
        deep, plain = row(2, DEEP), row(1)
        for label, text in (
                ('a word between the rows', '[%s junk, %s]' % (plain, deep)),
                ('a word after a row', '[%s, %s junk]' % (plain, deep)),
                ('two commas', '[%s,, %s]' % (plain, deep)),
                ('no comma', '[%s %s]' % (plain, deep)),
                ('a comma before the first', '[, %s, %s]' % (plain, deep)),
                ('a comma after the last', '[%s, %s,]' % (plain, deep)),
                ('a number among the rows', '[%s, 7, %s]' % (plain, deep)),
                ('a string among the rows', '[%s, "x", %s]' % (plain, deep)),
                ('a list among the rows', '[%s, [1], %s]' % (plain, deep)),
                ('a deep list among the rows', '[%s, %s]' % (plain, '[' * 3000 + ']' * 3000)),
                ('closed with a brace', '[%s, %s}' % (plain, deep)),
                ('a readable row that is not JSON', '[{"id": "pp-1", oops}, %s]' % deep),
                ('a deep error object in a list', '[%s, {"error": %s}]' % (plain, DEEP)),
                ('a deep error object alone', '{"error": %s}' % DEEP),
                ('a deep object whose id is not a string', '{"id": 7, "metadata": %s}' % DEEP),
                ('a deep object whose id is not a tracker id', '{"id": "pp 1\\n<b>", "metadata": %s}' % DEEP),
                ('a deep object whose id is nested', '{"x": {"id": "pp-1"}, "metadata": %s}' % DEEP),
                ('a deep value that is not an object', '[' * 3000 + ']' * 3000),
                ('two answers', '%s %s' % (deep, deep))):
            with self.subTest(text=label), self.assertRaises(ValueError):
                _parse_native(text)
        # And over the service's parser such an answer is what a malformed answer always was.
        with self.assertRaises(http_service.HttpError) as refused:
            _canonical_payload('[%s junk, %s]' % (plain, deep))
        self.assertEqual(refused.exception.status, 503)

    def test_the_marker_carries_the_id_and_nothing_else_of_the_row(self):
        """Review: it carried the stored title too, the first 200 characters, control characters included."""
        hostile = row(3, DEEP, title='A title\x00\x1b[31m with\ncontrol characters ' + 't' * 500)
        self.assertEqual(_parse_native(hostile), marker(3))
        self.assertEqual(_parse_native(listing(row(1), hostile))[1], marker(3))
        self.assertEqual(sorted(marker(3)), ['error', 'id', 'malformed', 'status', 'unreadable'])

    def test_the_id_of_a_row_is_read_from_its_own_top_level_only(self):
        self.assertEqual(_row_id('{"labels": ["id"], "x": {"id": "inner"}, "id": "outer", "title": "T"}'), 'outer')
        self.assertEqual(_row_id('{"title": "A \\"quoted\\" id", "id": "pp-1"}'), 'pp-1')
        self.assertEqual(_row_id('{"metadata": %s, "id": "kittrial-5bb.169"}' % DEEP), 'kittrial-5bb.169')
        # An id that is not a string, or not of a tracker id's shape, is not an id; a key named in a value is not a key.
        for text in ('{"id": 7, "note": "id", "title": {"id": "x"}}', '{"note": "id", "then": "pp-9"}', '{"id": ""}',
                     '{"id": "-pp"}', '{"id": "pp/1"}', '{"id": "%s"}' % ('p' * 200), '{"id": "pp-1\\n"}', '[]', ''):
            with self.subTest(text=text[:30]):
                self.assertIsNone(_row_id(text))

    def test_the_elements_of_an_array_are_found_without_recursion(self):
        text = listing(row(1), row(2, DEEP), row(3))
        spans = _elements(text)
        self.assertEqual(len(spans), 3)
        self.assertEqual(json.loads(text[spans[0][0]:spans[0][1]])['id'], 'pp-1')
        self.assertEqual(_elements('[]'), [])
        spaced = '  [ {"a": [1, {"b": "]"}]} ,\n {"c": 2} ]\n'
        self.assertEqual([spaced[start:end] for start, end in _elements(spaced)], ['{"a": [1, {"b": "]"}]}', '{"c": 2}'])
        for text in ('[{"a": 1}', '[{"a": 1}] x', '{"a": [1]}}', '[1, "two", null]', '[{"a": 1} {"b": 2}]', '[{"a": 1},]', 'x [{"a": 1}]'):
            with self.subTest(text=text):
                self.assertIsNone(_elements(text))

    def test_two_thousand_rows_with_one_unreadable_cost_a_scan_and_not_more(self):
        text = '[' + ','.join(row(n, DEEP if n == 700 else '{}') for n in range(2000)) + ']'
        started = time.monotonic()
        parsed = _parse_native(text)
        self.assertLess(time.monotonic() - started, 30)
        self.assertEqual((len(parsed), [r['id'] for r in parsed if r.get('malformed')]), (2000, ['pp-700']))

    def test_the_services_parser_uses_it_for_whole_texts_and_for_lines(self):
        self.assertEqual(_canonical_payload(listing(row(1), row(2, DEEP)))[1], marker(2))
        # NDJSON: a line of its own that is not read is marked like a row.
        self.assertEqual(_canonical_payload('a note\n' + row(4, DEEP)), marker(4))
        with self.assertRaises(http_service.HttpError) as refused:
            _canonical_payload('no json here')
        self.assertEqual(refused.exception.status, 503)
        reply = {'returncode': 0, 'stdout': listing(row(1), row(2, DEEP), row(3)), 'stderr': ''}
        self.assertEqual([r.get('malformed') for r in http_service.EndpointBackend._checked(reply)], [None, True, None])


class RouteTests(test_http_agents.AgentHarness):
    def setUp(self):
        super().setUp()
        self.admin = self.admin_token()
        self.project = self.create_project(self.admin, 'Alpha')
        self.good = self.request('POST', '/v1/projects/%s/tasks' % self.project, {'title': 'A readable task'},
                                 token=self.admin).data['id']
        self.bad = 'task_cannot_be_read'
        self.base = '/v1/projects/%s' % self.project

    def unreadable(self, rows=None):
        """The backend as it answers when the tracker returned a row the service could not read."""
        real_read, real_get = self.backend.read_tasks, self.backend.get_task
        extra = rows if rows is not None else [dict(marker(1), id=self.bad)]

        def read_tasks(project_id):
            snapshot = real_read(project_id)
            items = snapshot['items'] + [dict(r) for r in extra]
            return {'items': items, 'total': len(items)}

        def get_task(project_id, task_id):
            for candidate in extra:
                if candidate.get('id') == task_id:
                    return dict(candidate)
            return real_get(project_id, task_id)
        return mock.patch.multiple(self.backend, read_tasks=read_tasks, get_task=get_task)

    def test_the_list_answers_with_the_readable_rows_and_names_the_other(self):
        with self.unreadable():
            listed = self.request('GET', self.base + '/tasks', token=self.admin)
        self.assertEqual(listed.status, 200, listed.data)
        by_id = {item['id']: item for item in listed.data['items']}
        self.assertEqual(set(by_id), {self.good, self.bad})
        self.assertEqual((by_id[self.bad]['unreadable'], by_id[self.bad]['status'], by_id[self.bad].get('title'),
                          by_id[self.bad]['error']), (True, 'unknown', None, 'Malformed issue row'))
        self.assertNotIn('unreadable', by_id[self.good])
        self.assertEqual((listed.data['unreadable'], listed.data['total']), ([self.bad], 2))
        self.assertNotIn('review_states_unavailable', listed.data)
        self.assertNotIn('unparseable', listed.data)
        # Without such a row the answer has none of the new fields.
        plain = self.request('GET', self.base + '/tasks', token=self.admin).data
        for field in ('unreadable', 'unparseable', 'review_states_unavailable'):
            self.assertNotIn(field, plain)

    def test_paging_and_filters_keep_naming_it(self):
        with self.unreadable():
            first = self.request('GET', self.base + '/tasks?limit=1', token=self.admin).data
            filtered = self.request('GET', self.base + '/tasks?q=readable', token=self.admin).data
            closed = self.request('GET', self.base + '/tasks?status=closed', token=self.admin).data
        self.assertEqual((len(first['items']), first['total'], first['unreadable']), (1, 2, [self.bad]))
        self.assertTrue(first['next_cursor'])
        self.assertEqual(filtered['unreadable'], [self.bad])
        self.assertEqual((closed['items'], closed['unreadable']), ([], [self.bad]))

    def test_the_list_answers_without_review_states_when_that_read_fails_beside_an_unreadable_row(self):
        failing = mock.patch.object(http_service.ApiHandler, '_with_review_states',
                                    side_effect=http_service.uncertain('Canonical command timed out; outcome may be unknown'))
        with self.unreadable(), failing:
            listed = self.request('GET', self.base + '/tasks', token=self.admin)
            filtered = self.request('GET', self.base + '/tasks?q=readable', token=self.admin)
        for answer in (listed, filtered):
            self.assertEqual(answer.status, 200, answer.data)
            self.assertEqual((answer.data['review_states_unavailable'], answer.data['review_states_complete'],
                              answer.data['unreadable']), (True, False, [self.bad]))
            # Every row says that its review state is not known: the key is there, with null (a mutant of the review).
            self.assertTrue(answer.data['items'])
            for item in answer.data['items']:
                self.assertIn('review_state', item)
                if item['id'] == self.bad:
                    self.assertIsNone(item['review_state'])
        # With every row readable the same failure is what it was: nothing is hidden behind this.
        with failing:
            self.assertEqual(self.request('GET', self.base + '/tasks', token=self.admin).status, 503)

    def test_the_rows_own_routes_say_that_it_cannot_be_read(self):
        with self.unreadable():
            answers = {
                'the page': self.request('GET', '%s/tasks/%s' % (self.base, self.bad), token=self.admin),
                'the history': self.request('GET', '%s/tasks/%s/history' % (self.base, self.bad), token=self.admin),
                'a change': self.request('PATCH', '%s/tasks/%s' % (self.base, self.bad), {'title': 'x', 'version': 1},
                                         token=self.admin),
                'a claim': self.request('POST', '%s/tasks/%s/claim' % (self.base, self.bad), {}, token=self.admin),
            }
            readable = self.request('GET', '%s/tasks/%s' % (self.base, self.good), token=self.admin)
        for label, answer in answers.items():
            with self.subTest(route=label):
                self.assertEqual((answer.status, answer.data['error']['code'], answer.data['error']['message'],
                                  answer.data['error']['detail']),
                                 (409, 'unreadable_row', SENTENCE % self.bad, {'task': self.bad, 'state': 'unreadable'}))
        self.assertEqual(readable.status, 200)

    def test_the_brief_of_an_unreadable_row_says_so_and_another_briefs_failure_is_what_it_was(self):
        failure = http_service.uncertain('Canonical command timed out; outcome may be unknown')
        gets = []
        real = self.backend.get_task
        with self.unreadable():
            wrapped = self.backend.get_task

            def counting(project_id, task_id):
                gets.append(task_id)
                return wrapped(project_id, task_id)
            with mock.patch.object(self.backend, 'get_task', counting), \
                    mock.patch.object(self.backend, 'task_brief', side_effect=failure):
                bad = self.request('GET', '%s/tasks/%s/brief' % (self.base, self.bad), token=self.admin)
                good = self.request('GET', '%s/tasks/%s/brief' % (self.base, self.good), token=self.admin)
            self.assertEqual((bad.status, bad.data['error']['code'], bad.data['error']['message']),
                             (409, 'unreadable_row', SENTENCE % self.bad))
            self.assertEqual((good.status, good.data['error']['code']), (503, 'uncertain'))
            # A brief that answers costs no second read of the row.
            del gets[:]
            with mock.patch.object(self.backend, 'get_task', counting):
                self.assertEqual(self.request('GET', '%s/tasks/%s/brief' % (self.base, self.good), token=self.admin).status, 200)
            self.assertEqual(gets, [])
        self.assertIs(self.backend.get_task.__func__ if hasattr(self.backend.get_task, '__func__') else real, real.__func__
                      if hasattr(real, '__func__') else real)

    def test_the_page_marks_the_row_and_says_how_many(self):
        source = (KIT / 'web' / 'js' / 'views' / 'project.js').read_text(encoding='utf-8')
        self.assertIn("t.unreadable ? h('span', { class: 'chip crit' }, 'Cannot be read')", source)
        self.assertIn("const unread = (page.unreadable || []).length;", source)
        self.assertNotIn('unparseable', source)
        self.assertIn("t.title || t.id", source)                   # the marker has no title: the page shows its id


if __name__ == '__main__':
    unittest.main()
