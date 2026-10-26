"""A task description is at most 20,000 characters on the HTTP and the native route alike (kittrial-5bb.316).

HTTP and the native create/update refuse one character over the bound with the same sentence, so
nobody meets bd's "Argument list too long"; the documents and the limit tables agree.
"""
import json
import sys
import unittest
import uuid
from pathlib import Path

KIT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(KIT), str(KIT / 'tests')]
import test_bd_label_aliases as fixture
from test_coordinator_rules_endpoint import RulesHelpers
import test_http_review_fixes as fixes

BOUND = 20000
SAID = 'Task description is too long'


def read(name):
    return (KIT / name).read_text(encoding='utf-8')


class HttpBoundTests(fixes.EndpointCase):
    def project(self):
        self.alex, self.pid = self.setup_project()
        made = self.create_task(self.alex, self.pid, 'bound')
        self.assertEqual(201, made.status, made.data)
        self.tid = made.data['id']

    def test_http_create_and_update_accept_the_bound_and_refuse_one_over(self):
        self.project()
        base = '/v1/projects/%s/tasks' % self.pid
        for size, status in ((BOUND, 201), (BOUND + 1, 422)):
            with self.subTest(route='create', size=size):
                got = self.request('POST', base, {'title': 'c%d' % size, 'description': 'x' * size}, token=self.alex)
                self.assertEqual(status, got.status, got.data)
                if status == 422:
                    self.assertIn(SAID, got.data['error']['message'])
        for size, status in ((BOUND, 200), (BOUND + 1, 422)):
            with self.subTest(route='update', size=size):
                got = self.request('PATCH', '%s/%s' % (base, self.tid), {'description': 'x' * size}, token=self.alex)
                self.assertEqual(status, got.status, got.data)
                if status == 422:
                    self.assertIn(SAID, got.data['error']['message'])


@unittest.skipIf(fixture.endpoint is None, 'endpoint imports fcntl (POSIX-only)')
@unittest.skipIf(fixture.BD is None, 'no pinned native bd (set ORCHESTRA_BD_BIN)')
class NativeBoundTests(RulesHelpers, unittest.TestCase):
    def native(self, args, attachments=None):
        return self.endpoint('bd', 'w1', args, attachments)

    def attached(self, text):
        return {'0': {'flag': '--body-file', 'text': text}}

    def test_native_create_accepts_the_bound_and_refuses_one_over_by_every_spelling(self):
        tag = uuid.uuid4().hex[:6]
        spellings = {
            'inline': lambda t: (['create', 'n-inline-' + tag, '--description', t, '--json'], None),
            'short': lambda t: (['create', 'n-short-' + tag, '-d', t, '--json'], None),
            'equals': lambda t: (['create', 'n-equals-' + tag, '--description=' + t, '--json'], None),
            'joined short': lambda t: (['create', 'n-joined-' + tag, '-d' + t, '--json'], None),
            'short equals': lambda t: (['create', 'n-sheq-' + tag, '-d=' + t, '--json'], None),
            'attached': lambda t: (['create', 'n-att-' + tag, '@attachment:0', '--json'], self.attached(t)),
        }
        for name, build in spellings.items():
            with self.subTest(spelling=name, size=BOUND):
                args, att = build('x' * BOUND)
                done = self.native(args, att)
                self.assertEqual(0, done['returncode'], done)
                row = json.loads(done['stdout'])
                self.assertEqual(BOUND, len(row.get('description') or ''))
            with self.subTest(spelling=name, size=BOUND + 1):
                before = len(self.rows())
                args, att = build('x' * (BOUND + 1))
                with self.assertRaises(ValueError) as caught:
                    self.native(args, att)
                self.assertIn(SAID, str(caught.exception))
                self.assertEqual(before, len(self.rows()), 'a refused create wrote a row')

    def test_create_child_refuses_one_over_before_any_receipt_or_row(self):
        parent = self.task('parent')
        for size in (BOUND, BOUND + 1):
            with self.subTest(size=size):
                before = len(self.rows())
                body = dict(operation='create-child', parent=parent, title='child %d' % size,
                            request_id='child-%d-%s' % (size, uuid.uuid4().hex[:6]),
                            description='x' * size, type='task')
                if size == BOUND:
                    done = self.endpoint('coordinate', 'worker', [json.dumps(body)])
                    self.assertEqual(0, done['returncode'], done)
                else:
                    with self.assertRaises(ValueError) as caught:
                        self.endpoint('coordinate', 'worker', [json.dumps(body)])
                    self.assertIn(SAID, str(caught.exception))
                    self.assertEqual(before, len(self.rows()))
                    receipts = list(self.root.rglob('*%s*' % body['request_id']))
                    self.assertEqual([], receipts, 'a refused child left a receipt')

    def test_a_description_given_as_a_raw_file_path_is_still_not_reachable(self):
        for flag in ('--body-file', '-f', '--file'):
            with self.subTest(flag=flag):
                with self.assertRaises(ValueError) as caught:
                    self.native(['create', 'raw path', flag, '/etc/hostname', '--json'])
                self.assertIn('operator-only', str(caught.exception))

    def test_native_update_refuses_one_over_and_leaves_the_description(self):
        task = self.task('update bound')
        self.assertEqual(0, self.native(['update', task, '--description', 'x' * BOUND])['returncode'])
        with self.assertRaises(ValueError) as caught:
            self.native(['update', task, '@attachment:0'], self.attached('y' * (BOUND + 1)))
        self.assertIn(SAID, str(caught.exception))
        shown = json.loads(self.native(['show', task, '--json'])['stdout'])
        shown = shown[0] if isinstance(shown, list) else shown
        self.assertEqual('x' * BOUND, shown['description'])

    def test_a_text_after_double_dash_or_on_another_command_is_not_a_description(self):
        task = self.task('not a description')
        done = self.native(['comments', 'add', task, '--', '--description', 'z' * (BOUND + 5)])
        self.assertEqual(0, done['returncode'], done)


class DocumentsAgreeTests(unittest.TestCase):
    def test_the_bound_in_code_and_in_the_documents_is_one_number(self):
        import briefing
        text = {n: read(n) for n in ('docs/CLI_CONTRACT.md', 'docs/HTTP_DEPLOYMENT.md', 'templates/CHECKPOINT.md')}
        self.assertIn('20,000 characters', text['docs/CLI_CONTRACT.md'])
        self.assertIn('20,000 characters', text['docs/HTTP_DEPLOYMENT.md'])
        if fixture.endpoint is not None:
            self.assertEqual(BOUND, fixture.endpoint.DESCRIPTION_MAX)
        self.assertIn('%d entries' % briefing.CHECKPOINT_ITEMS_MAX, text['templates/CHECKPOINT.md'])
        self.assertIn('<= %d each' % briefing.CHECKPOINT_ITEMS_MAX, text['docs/CLI_CONTRACT.md'])

    def test_every_route_uses_the_one_constant(self):
        import field_limits
        self.assertEqual(BOUND, field_limits.DESCRIPTION_MAX)
        self.assertEqual(field_limits.DESCRIPTION_TOO_LONG.split(':')[0], SAID)
        for name in ('http_service.py', 'endpoint.py', 'coordination.py', 'requirement_http.py'):
            with self.subTest(module=name):
                text = read(name)
                self.assertIn('DESCRIPTION_MAX', text)
                self.assertNotRegex(text, r'[>=<(,] ?20000\b', 'a literal 20000 bound')
                self.assertNotIn("invalid('Task description is too long')", text)

    def test_the_documents_name_the_way_around_each_limit(self):
        for name in ('docs/CLI_CONTRACT.md', 'templates/CHECKPOINT.md'):
            with self.subTest(document=name):
                self.assertIn('id prefix', read(name))
                self.assertIn('repository document', read(name))
        self.assertIn('repository document', read('docs/HTTP_DEPLOYMENT.md'))

    def test_the_open_item_refusal_says_what_to_do(self):
        import briefing
        items = [{'id': 'i%d' % n, 'kind': 'question', 'text': 't', 'source': 's'}
                 for n in range(briefing.CHECKPOINT_ITEMS_MAX + 1)]
        problems = briefing.checkpoint_problems({'open_items': items}, 'kittrial-5bb.1')
        said = ' '.join(problems)
        self.assertIn('at most %d items' % briefing.CHECKPOINT_ITEMS_MAX, said)
        self.assertIn('one open item per theme', said)
        self.assertEqual([], [p for p in briefing.checkpoint_problems({'open_items': items[:1]}, 'kittrial-5bb.1') if 'group the items' in p])


if __name__ == '__main__':
    unittest.main()
