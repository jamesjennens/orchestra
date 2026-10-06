"""One row the tracker returns that cannot be read must not fail the web service (kittrial-5bb.169, item 1).

A row whose metadata nests a few thousand levels makes `json.loads` raise RecursionError,
which is not the ValueError the service's parser caught: the task list, the page, brief and
history of that row, the project's setup page and a change of the row all answered 500.
The service now takes such an answer apart row by row, names the row it cannot read in the
words of the endpoint's readers (kittrial-5bb.141), and says so on that row's own routes.
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
import test_http_agents
from http_service import _canonical_payload, _elements, _parse_native, _row_head

DEEP = '{"k":' * 3000 + '1' + '}' * 3000
SENTENCE = ('Task %s exists, but its row cannot be read (it is malformed or nested too deeply). Ask an operator of the '
            'server to repair it.')


def row(number, metadata='{}', title=None):
    return json.dumps({'id': 'pp-%d' % number, 'title': title or 'Task %d' % number, 'status': 'open',
                       'labels': ['a]', '{b'], 'description': 'with "quotes", a \\ and ]}'})[:-1] + ', "metadata": %s}' % metadata


def listing(*rows, indent=True):
    return ('[\n  ' + ',\n  '.join(rows) + '\n]') if indent else '[' + ','.join(rows) + ']'


def marker(number, title=True):
    made = {'id': 'pp-%d' % number, 'malformed': True, 'unreadable': True, 'error': 'Malformed issue row', 'status': 'unknown'}
    if title:
        made['title'] = 'Task %d' % number
    return made


class ParserTests(unittest.TestCase):
    def test_json_loads_itself_fails_on_such_a_row_which_is_what_the_service_met(self):
        with self.assertRaises(RecursionError):
            json.loads(listing(row(1), row(2, DEEP)))

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

    def test_a_text_that_parses_is_what_json_loads_makes_of_it(self):
        for text in (listing(row(1), row(2)), '[]', '{"a": 1}', '"text"', '7', 'null', '[1, [2, [3]], "]"]'):
            with self.subTest(text=text[:20]):
                self.assertEqual(_parse_native(text), json.loads(text))

    def test_a_text_that_is_not_json_is_refused_as_before(self):
        for text in ('nonsense', '[{"id": "pp-1"}', '[{"id": "pp-1"}] and more', '', '[' + row(1, DEEP), row(1, DEEP)[:-5],
                     listing(row(1), row(2, DEEP)) + ' trailing'):
            with self.subTest(text=text[:30]), self.assertRaises(ValueError):
                _parse_native(text)

    def test_the_head_of_a_row_is_read_from_its_own_top_level_only(self):
        self.assertEqual(_row_head('{"labels": ["id"], "x": {"id": "inner", "title": "inner"}, "id": "outer", "title": "T"}'),
                         {'id': 'outer', 'title': 'T'})
        self.assertEqual(_row_head('{"title": "A \\"quoted\\" one", "id": "pp-1"}'), {'title': 'A "quoted" one', 'id': 'pp-1'})
        # An id that is not a string is not an id; a key named in a value is not a key.
        self.assertEqual(_row_head('{"id": 7, "note": "id", "title": {"id": "x"}}'), {})
        self.assertEqual(_row_head('{"note": "id", "then": "pp-9"}'), {})
        self.assertEqual(_row_head('[]'), {})
        self.assertEqual(_row_head(''), {})

    def test_a_row_whose_id_cannot_be_read_is_marked_without_one(self):
        nameless = '{"metadata": %s, "id": 7}' % DEEP
        parsed = _parse_native(listing(row(1), nameless))
        self.assertEqual(parsed[1], {'id': None, 'malformed': True, 'unreadable': True, 'error': 'Malformed issue row',
                                     'status': 'unknown'})
        long_title = _parse_native(row(3, DEEP, title='t' * 500))
        self.assertEqual(len(long_title['title']), 200)

    def test_the_elements_of_an_array_are_found_without_recursion(self):
        text = listing(row(1), row(2, DEEP), row(3))
        spans = _elements(text)
        self.assertEqual(len(spans), 3)
        self.assertEqual(json.loads(text[spans[0][0]:spans[0][1]])['id'], 'pp-1')
        self.assertIsNone(_elements('[{"a": 1}'))
        self.assertIsNone(_elements('[{"a": 1}] x'))
        self.assertIsNone(_elements('{"a": [1]}}'))
        self.assertEqual(_elements('[]'), [])
        self.assertEqual(_elements('[1, "two", null]'), [])

    def test_two_thousand_rows_with_one_unreadable_cost_a_scan_and_not_more(self):
        text = '[' + ','.join(row(n, DEEP if n == 700 else '{}') for n in range(2000)) + ']'
        started = time.monotonic()
        parsed = _parse_native(text)
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual((len(parsed), [r['id'] for r in parsed if r.get('malformed')]), (2000, ['pp-700']))

    def test_the_services_parser_uses_it_for_whole_texts_and_for_lines(self):
        self.assertEqual(_canonical_payload(listing(row(1), row(2, DEEP)))[1], marker(2))
        # NDJSON: a line of its own that cannot be read is marked like a row.
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
        extra = rows if rows is not None else [dict(marker(1), id=self.bad, title='The row that cannot be read')]

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
        self.assertEqual((by_id[self.bad]['unreadable'], by_id[self.bad]['status'], by_id[self.bad]['title'],
                          by_id[self.bad]['error']), (True, 'unknown', 'The row that cannot be read', 'Malformed issue row'))
        self.assertNotIn('unreadable', by_id[self.good])
        self.assertEqual((listed.data['unreadable'], listed.data['unparseable'], listed.data['total']), ([self.bad], 0, 2))
        self.assertNotIn('review_states_unavailable', listed.data)
        # Without such a row the answer has none of the new fields.
        plain = self.request('GET', self.base + '/tasks', token=self.admin).data
        for field in ('unreadable', 'unparseable', 'review_states_unavailable'):
            self.assertNotIn(field, plain)

    def test_a_row_without_an_id_is_counted_and_not_listed(self):
        nameless = {'id': None, 'malformed': True, 'unreadable': True, 'error': 'Malformed issue row', 'status': 'unknown'}
        with self.unreadable([nameless, dict(marker(1), id=self.bad)]):
            listed = self.request('GET', self.base + '/tasks', token=self.admin).data
        self.assertEqual((sorted(item['id'] for item in listed['items']), listed['unreadable'], listed['unparseable'],
                          listed['total']), (sorted([self.good, self.bad]), [self.bad], 1, 2))

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
        self.assertIn("(page.unreadable || []).length + (page.unparseable || 0)", source)
        self.assertIn("t.title || t.id", source)


if __name__ == '__main__':
    unittest.main()
