"""A task change that the route cannot carry out is refused, and a task is written under the caller's own name (kittrial-5bb.181).

Two faults of ``PATCH /v1/projects/P/tasks/ID`` and ``POST /v1/projects/P/tasks``, found by
running an office installation:

* a change that carried none of title, description and status (a priority, an assignee, a
  status other than open and closed, nothing at all) made the endpoint backend send bd an
  update with nothing in it. bd answers that with the words "No updates specified" and exit
  0; the service could not read the words, said "the operation may have committed; reconcile
  with the same idempotency key", kept the key, and answered the same for every retry. A
  field the route does not take beside one it takes was dropped without a word;
* both routes handed an ``actor`` from the body to the endpoint as it came, so a member's
  task was made, or changed, under any name without the shape of a web account.

The endpoint here is the strict stub, which answers an empty update as real bd 1.2.2 does
and records under which actor a row was made.
"""
import json
import shutil
import sys
import unittest
from pathlib import Path
from unittest import mock

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT))
sys.path.insert(0, str(KIT / 'tests'))
import http_service
import test_http_review_fixes as fixes

TAKES = 'A task change takes: title, description, status'
#: Bodies the route cannot carry out, with what the refusal must say.
REFUSED = (
    ('a priority alone', {'priority': 1}, 'does not take: priority. ' + TAKES),
    ('an assignee alone', {'assignee': 'somebody'}, 'does not take: assignee. ' + TAKES),
    ('labels alone', {'labels': ['a']}, 'does not take: labels. ' + TAKES),
    ('a title and a priority', {'title': 'renamed', 'priority': 1}, 'does not take: priority. ' + TAKES),
    ('two fields it does not take', {'priority': 1, 'assignee': 'x', 'title': 'renamed'},
     'does not take: assignee, priority. ' + TAKES),
    ('an empty object', {}, 'Nothing to change. ' + TAKES),
    ('a title that is null', {'title': None}, 'Nothing to change. ' + TAKES),
    ('only the version', {'version': 1}, 'Nothing to change. ' + TAKES),
    ('a status that is neither open nor closed', {'status': 'in_progress'}, 'Task status must be open or closed'),
    ('another such status', {'status': 'done'}, 'Task status must be open or closed'),
    ('a status that is not text', {'status': 1}, 'Task status must be open or closed'),
    ('an empty title', {'title': ''}, 'Task title must be text and not empty'),
    ('a blank title', {'title': '   '}, 'Task title must be text and not empty'),
    ('a title that is not text', {'title': 5}, 'Task title must be text and not empty'),
    ('a description that is not text', {'description': 5}, 'Task description must be text'),
)


def writes(asked):
    """The writes the endpoint was asked for: (command, actor) of each create and update."""
    return [(call.args[3][0], call.args[2]) for call in asked.call_args_list
            if call.args[0] == 'bd' and call.args[3][:1] in (['create'], ['update'])]


def message(response):
    return (response.data.get('error') or {}).get('message', '') if isinstance(response.data, dict) else ''


class Project:
    """A project with its owner (alex), a contributor (casey) and one task."""

    def project(self):
        admin = self.admin_token()
        self.alex, self.pid = self.setup_project()
        self.casey_id = self.create_account(admin, 'casey', 'casey-password-1')
        added = self.request('PUT', '/v1/projects/%s/members/%s' % (self.pid, self.casey_id),
                             {'role': 'contributor'}, token=self.alex)
        self.assertIn(added.status, (200, 201), added.data)
        self.casey = self.login('casey', 'casey-password-1')[0]
        self.other_id = self.create_account(admin, 'drew', 'drew-password-1')
        made = self.create_task(self.alex, self.pid, 'as made')
        self.assertEqual(201, made.status, made.data)
        self.tid = made.data['id']
        self.path = '/v1/projects/%s/tasks/%s' % (self.pid, self.tid)

    def audit(self):
        listed = self.request('GET', '/v1/projects/%s/audit?limit=100' % self.pid, token=self.alex)
        self.assertEqual(200, listed.status, listed.data)
        return listed.data['items']

    def keys(self):
        return self.service.store.records.count('idempotency') if hasattr(self.service.store.records, 'count') else None


class RefusedChangeCase(Project, fixes.EndpointCase):
    def row(self):
        return {key: value for key, value in next(r for r in self.canonical_rows() if r['id'] == self.tid).items()
                if key in ('title', 'description', 'status', 'assignee', 'labels')}

    def test_a_change_the_route_cannot_carry_out_is_refused_and_nothing_is_sent_kept_or_written(self):
        self.project()
        before = self.row()
        entries = len(self.audit())
        for number, (label, body, said) in enumerate(REFUSED):
            with self.subTest(body=label):
                key = 'refused-key-%04d' % number
                with mock.patch.object(self.backend, '_run', wraps=self.backend._run) as asked:
                    first = self.request('PATCH', self.path, body, token=self.alex, key=key)
                    again = self.request('PATCH', self.path, body, token=self.alex, key=key)
                self.assertEqual((422, 422), (first.status, again.status), first.data)
                self.assertEqual('invalid_payload', first.data['error']['code'])
                self.assertIn(said, message(first))
                self.assertEqual(message(first), message(again))
                self.assertEqual([], writes(asked), 'the endpoint was asked to write')
                self.assertEqual(before, self.row())
                # The key was not kept: the corrected request is carried out under it.
                corrected = self.request('PATCH', self.path, {'title': 'corrected %d' % number}, token=self.alex, key=key)
                self.assertEqual(200, corrected.status, corrected.data)
                self.assertEqual('corrected %d' % number, self.row()['title'])
                before = self.row()
        outcomes = [entry['outcome'] for entry in self.audit()[entries:] if entry['action'] == 'tasks.update']
        self.assertNotIn('unknown', outcomes)
        self.assertEqual(len(REFUSED), outcomes.count('committed'))

    def test_the_refusal_names_the_fields_as_a_list_too(self):
        self.project()
        refused = self.request('PATCH', self.path, {'priority': 1, 'labels': [], 'title': 'x'}, token=self.alex)
        self.assertEqual(422, refused.status)
        self.assertEqual({'unsupported': ['labels', 'priority'], 'takes': ['title', 'description', 'status']},
                         refused.data['error']['detail'])
        nothing = self.request('PATCH', self.path, {}, token=self.alex)
        self.assertEqual({'takes': ['title', 'description', 'status']}, nothing.data['error']['detail'])

    def test_a_field_name_that_is_not_a_plain_word_is_not_said_back(self):
        self.project()
        refused = self.request('PATCH', self.path, {'<script>': 1, 'title': 'x'}, token=self.alex)
        self.assertEqual(422, refused.status)
        self.assertNotIn('<script>', message(refused))
        self.assertIn('<non-identifier name>', message(refused))

    def test_what_the_route_takes_is_carried_out_as_before(self):
        self.project()
        for body, expected in (({'title': 'new title'}, ('title', 'new title')),
                               ({'description': 'new text'}, ('description', 'new text')),
                               ({'status': 'closed'}, ('status', 'closed')),
                               ({'status': 'open'}, ('status', 'open')),
                               # the version is taken (the in-process backend checks it) and not read here
                               ({'title': 'with a version', 'version': 7}, ('title', 'with a version')),
                               ({'title': 'kept', 'description': None, 'status': None}, ('title', 'kept')),
                               ({'description': ''}, ('description', ''))):
            with self.subTest(body=body):
                changed = self.request('PATCH', self.path, body, token=self.alex)
                self.assertEqual(200, changed.status, changed.data)
                self.assertEqual(expected[1], self.row()[expected[0]])

    def test_the_backend_never_sends_an_update_with_nothing_in_it(self):
        """The same refusal below the route, for any other caller of the backend."""
        self.project()
        principal = mock.Mock(actor=None, user_id='usr_0000000000000000')
        for payload in ({'task_id': self.tid}, {'task_id': self.tid, 'status': 'in_progress'},
                        {'task_id': self.tid, 'title': None, 'description': None}):
            with self.subTest(payload=payload):
                with self.assertRaises(http_service.HttpError) as refused:
                    self.backend._command('tasks.update', principal, self.pid, payload, 'op-1')
                self.assertEqual(422, refused.exception.status)
                self.assertIn('Nothing to change', refused.exception.message)
        action, project, args, attachments = self.backend._command(
            'tasks.update', principal, self.pid, {'task_id': self.tid, 'title': 'T'}, 'op-1')
        self.assertEqual(['update', self.tid, '--title', 'T', '--json'], args)

    def test_without_the_route_check_the_backend_refuses_and_the_key_is_free(self):
        self.project()
        with mock.patch.object(http_service.ApiHandler, '_task_change', http_service.ApiHandler._task_payload):
            refused = self.request('PATCH', self.path, {'priority': 1}, token=self.alex, key='below-the-route-1')
        self.assertEqual(422, refused.status, refused.data)
        self.assertIn('Nothing to change', message(refused))
        corrected = self.request('PATCH', self.path, {'title': 'afterwards'}, token=self.alex, key='below-the-route-1')
        self.assertEqual(200, corrected.status, corrected.data)
        self.assertNotIn('unknown', [entry['outcome'] for entry in self.audit()])

    def test_what_bd_says_to_an_empty_update_is_not_an_answer_the_service_can_read(self):
        """The cause, kept as a fact: the words are read as an outcome that is not known."""
        with self.assertRaises(http_service.HttpError) as unread:
            http_service._canonical_payload('No updates specified\n')
        self.assertEqual(503, unread.exception.status)

    def test_a_refusal_comes_before_the_task_is_looked_for(self):
        self.project()
        missing = '/v1/projects/%s/tasks/%s' % (self.pid, 'kittrial-5bb.999')
        refused = self.request('PATCH', missing, {'priority': 1}, token=self.alex)
        self.assertEqual(422, refused.status)
        self.assertIn('does not take: priority', message(refused))
        # A body the route takes goes on to the task, which is not there.
        unknown = self.request('PATCH', missing, {'title': 'x'}, token=self.alex)
        self.assertGreaterEqual(unknown.status, 400)
        self.assertNotIn('A task change takes', message(unknown))

    def test_a_viewer_is_refused_as_a_viewer_whatever_the_body(self):
        self.project()
        changed = self.request('PUT', '/v1/projects/%s/members/%s' % (self.pid, self.casey_id), {'role': 'viewer'},
                               token=self.alex)
        self.assertIn(changed.status, (200, 201), changed.data)
        self.assertEqual(403, self.request('PATCH', self.path, {'priority': 1}, token=self.casey).status)


class TaskActorCase(Project, fixes.EndpointCase):
    """An actor in the body of a task create or a task change is the caller's own, as on a claim."""

    def created_by(self, title):
        return [row.get('created_by') for row in self.canonical_rows() if row.get('title') == title]

    def names(self):
        return (('a host session name', 'session-coordinator'), ('another account', self.other_id),
                ('the owner of the project', self.request('GET', '/v1/sessions/current', token=self.alex).data['user']['id']),
                ('a name nobody registered', 'nobody-registered'), ('not a name', 7), ('an empty name', ''))

    def test_a_member_cannot_have_a_task_made_under_another_name(self):
        self.project()
        rows = len(self.canonical_rows())
        for label, name in self.names():
            with self.subTest(actor=label):
                made = self.request('POST', '/v1/projects/%s/tasks' % self.pid, {'title': 'forged', 'actor': name},
                                    token=self.casey)
                claim = self.request('POST', self.path + '/claim', {'actor': name}, token=self.casey)
                # Refused exactly as a claim under that name is refused.
                self.assertIn(made.status, (403, 422), made.data)
                self.assertEqual((claim.status, message(claim)), (made.status, message(made)))
        self.assertEqual(rows, len(self.canonical_rows()))
        self.assertEqual([], self.created_by('forged'))

    def test_a_member_cannot_have_a_task_changed_under_another_name(self):
        self.project()
        for label, name in self.names():
            with self.subTest(actor=label):
                with mock.patch.object(self.backend, '_run', wraps=self.backend._run) as asked:
                    changed = self.request('PATCH', self.path, {'title': 'forged', 'actor': name}, token=self.casey)
                claim = self.request('POST', self.path + '/claim', {'actor': name}, token=self.casey)
                self.assertIn(changed.status, (403, 422), changed.data)
                self.assertEqual((claim.status, message(claim)), (changed.status, message(changed)))
                self.assertEqual([], writes(asked))
        self.assertEqual('as made', next(r for r in self.canonical_rows() if r['id'] == self.tid)['title'])

    def test_the_actor_the_endpoint_is_asked_under_is_the_callers_own(self):
        self.project()
        for body in ({}, {'actor': None}, {'actor': self.casey_id}):
            with self.subTest(body=body):
                with mock.patch.object(self.backend, '_run', wraps=self.backend._run) as asked:
                    made = self.request('POST', '/v1/projects/%s/tasks' % self.pid, dict(body, title='mine'), token=self.casey)
                    self.assertEqual(201, made.status, made.data)
                    changed = self.request('PATCH', '/v1/projects/%s/tasks/%s' % (self.pid, made.data['id']),
                                           dict(body, title='mine still'), token=self.casey)
                    self.assertEqual(200, changed.status, changed.data)
                self.assertEqual([('create', self.casey_id), ('update', self.casey_id)], writes(asked))
        self.assertEqual({self.casey_id}, set(self.created_by('mine still')))
        mine = [entry for entry in self.audit() if entry['action'] in ('tasks.create', 'tasks.update')
                and entry['outcome'] == 'committed' and entry['user_id'] == self.casey_id]
        self.assertEqual(6, len(mine))

    def test_a_worker_credential_writes_inside_its_own_namespace_only(self):
        self.project()
        credential = self.issue_credential(self.alex, self.pid, label='worker', actor='worker-a')
        token = credential['secret']
        tasks = '/v1/projects/%s/tasks' % self.pid
        for name in ('worker-a', 'worker-a/sub'):
            with self.subTest(actor=name):
                made = self.request('POST', tasks, {'title': 'by ' + name, 'actor': name}, token=token)
                self.assertEqual(201, made.status, made.data)
                self.assertEqual([name], self.created_by('by ' + name))
                changed = self.request('PATCH', tasks + '/' + made.data['id'], {'description': 'd', 'actor': name}, token=token)
                self.assertEqual(200, changed.status, changed.data)
        for name in ('worker-b', 'worker-ab', 'session-coordinator', self.casey_id):
            with self.subTest(actor=name):
                made = self.request('POST', tasks, {'title': 'outside', 'actor': name}, token=token)
                changed = self.request('PATCH', self.path, {'title': 'outside', 'actor': name}, token=token)
                self.assertEqual((403, 403), (made.status, changed.status), (made.data, changed.data))
                self.assertIn('outside the credential attribution namespace', message(made))
        self.assertEqual([], self.created_by('outside'))


class InProcessCase(Project, fixes.Harness):
    """The backend of the tests and the local preview refuses the same bodies."""

    def setup_project(self):
        admin = self.admin_token()
        self.create_account(admin, 'alex', 'alex-password-1')
        alex = self.login('alex', 'alex-password-1')[0]
        return alex, self.create_project(alex, 'Alpha')

    def test_the_same_bodies_are_refused_with_the_same_words(self):
        self.project()
        for label, body, said in REFUSED:
            with self.subTest(body=label):
                refused = self.request('PATCH', self.path, dict(body, version=1) if body else body, token=self.alex,
                                       key='in-process-%d' % len(label))
                self.assertEqual(422, refused.status, refused.data)
                self.assertIn(said, message(refused))
        task = self.request('GET', self.path, token=self.alex).data
        self.assertEqual(('as made', 1), (task['title'], task['version']))

    def test_a_change_with_its_version_is_carried_out(self):
        self.project()
        changed = self.request('PATCH', self.path, {'title': 'renamed', 'version': 1}, token=self.alex)
        self.assertEqual(200, changed.status, changed.data)
        self.assertEqual(('renamed', 2), (changed.data['title'], changed.data['version']))

    def test_an_actor_in_the_body_is_bound_here_too(self):
        self.project()
        for route, method, body in (('/v1/projects/%s/tasks' % self.pid, 'POST', {'title': 'forged'}),
                                    (self.path, 'PATCH', {'title': 'forged', 'version': 1})):
            with self.subTest(route=method):
                refused = self.request(method, route, dict(body, actor='session-coordinator'), token=self.casey)
                self.assertEqual(403, refused.status, refused.data)


class FeedbackNotBuiltCase(Project, fixes.EndpointCase):
    """The same run found that the feedback routes answer 501 on the endpoint backend: said, not built."""

    def test_the_endpoint_backend_answers_501_to_reading_and_sending_and_keeps_no_key(self):
        self.project()
        path = '/v1/projects/%s/feedback' % self.pid
        self.assertEqual(501, self.request('GET', path, token=self.alex).status)
        for _ in range(2):
            sent = self.request('POST', path, {'category': 'bug', 'text': 'It does not work.'}, token=self.alex, key='feedback-key-1')
            self.assertEqual(501, sent.status, sent.data)
            self.assertEqual('not_implemented', sent.data['error']['code'])
        self.assertNotIn('unknown', [entry['outcome'] for entry in self.audit()])


class FeedbackPageTests(unittest.TestCase):
    """The page says that the server keeps no feedback, and offers no form that can only fail."""

    def test_a_server_without_feedback_is_said_and_any_other_failure_is_still_a_failure(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('node is not installed')
        import test_http_web
        done = test_http_web.run_node_module(self, node, 'await import(process.argv[1])',
                                             (KIT / 'tests' / 'web_feedback_screen.mjs').as_uri(),
                                             (KIT / 'tests' / 'web_dom_shim.mjs').as_uri(),
                                             (KIT / 'web' / 'js' / 'views' / 'project.js').as_uri())
        self.assertEqual(0, done.returncode, done.stderr[-3000:])
        seen = json.loads(done.stdout.strip().splitlines()[-1])
        for name in ('notBuilt', 'notBuiltViewer'):
            with self.subTest(page=name):
                page = seen[name]
                self.assertIn('Feedback is not available on this server', page['text'])
                self.assertIn('does not keep feedback yet', page['text'])
                self.assertEqual((0, [], 0, 1), (page['forms'], page['buttons'], page['alerts'], page['asked']))
                self.assertGreaterEqual(page['status'], 1)
        # Another failure is a failure: it is shown as one, with a way to try again, and the form stays.
        self.assertNotIn('not available on this server', seen['broken']['text'])
        self.assertEqual((1, 1), (seen['broken']['alerts'], seen['broken']['forms']))
        self.assertIn('Try again', seen['broken']['buttons'])
        # A server that keeps feedback: the list, and the form for whoever may write.
        self.assertIn('No feedback yet', seen['empty']['text'])
        self.assertEqual((1, ['Submit feedback']), (seen['empty']['forms'], seen['empty']['buttons']))
        self.assertEqual((0, []), (seen['emptyViewer']['forms'], seen['emptyViewer']['buttons']))


if __name__ == '__main__':
    unittest.main()
