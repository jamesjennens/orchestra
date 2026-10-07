"""kittrial-5bb.183: the chosen priority is stored, and a field a write route does not take is refused.

Item 1 (priority). The web page's new-task form sends ``priority`` (web/js/views/task.js);
the endpoint backend accepted the request, answered 201 and dropped the field, so the task
was made with bd's default. Task create and task change now take priority for real (an
integer 0..4, refused outside that), and the endpoint is asked for it.

Item 2 (the one table). Task create, claim, checkpoint, member, worker credential, agent
and account accepted a field the route does not take and dropped it silently. Each refuses
it now with 422, naming the field and the fields the route takes, in the shape a task
change has used since kittrial-5bb.181, and never answers 5xx or "may have committed" for
it. The named clients in this repository (web/js/api.js, http_client.py) send only fields
the route takes, so none is broken.
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT))
sys.path.insert(0, str(KIT / 'tests'))
import http_service
import test_http_review_fixes as fixes
import test_task_change_fields as task_fields


def message(response):
    data = response.data if isinstance(response.data, dict) else {}
    return (data.get('error') or {}).get('message', '')


def detail(response):
    data = response.data if isinstance(response.data, dict) else {}
    return (data.get('error') or {}).get('detail')


class PriorityThroughTheRoute(task_fields.Project, fixes.EndpointCase):
    """The endpoint backend is asked for the priority the caller chose (item 1)."""

    def stored(self, tid):
        return next(row for row in self.canonical_rows() if row['id'] == tid)

    def test_create_stores_the_priority_the_caller_chose(self):
        self.project()
        for chosen in (0, 1, 3, 4):
            with self.subTest(priority=chosen):
                made = self.request('POST', '/v1/projects/%s/tasks' % self.pid,
                                    {'title': 'p%d task' % chosen, 'priority': chosen},
                                    token=self.alex)
                self.assertEqual(201, made.status, made.data)
                self.assertEqual(chosen, self.stored(made.data['id'])['priority'])
                # The read the task page renders shows the stored value.
                shown = self.request('GET', '/v1/projects/%s/tasks/%s'
                                     % (self.pid, made.data['id']), token=self.alex)
                self.assertEqual(200, shown.status, shown.data)
                self.assertEqual(chosen, shown.data.get('priority'), shown.data)

    def test_create_without_a_priority_uses_the_native_default(self):
        self.project()
        made = self.request('POST', '/v1/projects/%s/tasks' % self.pid, {'title': 'default'},
                            token=self.alex)
        self.assertEqual(201, made.status, made.data)
        self.assertEqual(2, self.stored(made.data['id'])['priority'])

    def test_change_takes_the_priority(self):
        self.project()
        changed = self.request('PATCH', self.path, {'priority': 4}, token=self.alex)
        self.assertEqual(200, changed.status, changed.data)
        self.assertEqual(4, self.stored(self.tid)['priority'])
        both = self.request('PATCH', self.path, {'title': 'renamed', 'priority': 0}, token=self.alex)
        self.assertEqual(200, both.status, both.data)
        self.assertEqual((0, 'renamed'),
                         (self.stored(self.tid)['priority'], self.stored(self.tid)['title']))

    def test_a_priority_outside_zero_to_four_is_refused_and_nothing_is_written(self):
        self.project()
        before = self.stored(self.tid)
        for number, bad in enumerate((5, -1, '1', 1.5, True, [1])):
            with self.subTest(priority=bad):
                with mock.patch.object(self.backend, '_run', wraps=self.backend._run) as asked:
                    refused = self.request('PATCH', self.path, {'priority': bad}, token=self.alex,
                                           key='priority-%02d' % number)
                self.assertEqual(422, refused.status, refused.data)
                self.assertIn('Task priority must be an integer 0-4', message(refused))
                self.assertEqual([], task_fields.writes(asked), 'the endpoint was asked to write')
                self.assertEqual(before, self.stored(self.tid))

    def test_create_refuses_a_priority_outside_zero_to_four_and_creates_nothing(self):
        self.project()
        before = len(self.canonical_rows())
        for number, bad in enumerate((5, -1, '1', 1.5)):
            with self.subTest(priority=bad):
                refused = self.request('POST', '/v1/projects/%s/tasks' % self.pid,
                                       {'title': 'no', 'priority': bad}, token=self.alex,
                                       key='create-priority-%02d' % number)
                self.assertEqual(422, refused.status, refused.data)
                self.assertIn('Task priority must be an integer 0-4', message(refused))
        self.assertEqual(before, len(self.canonical_rows()))

    def test_a_null_priority_on_create_is_absent_on_this_backend_too(self):
        """Both backends read ``priority: null`` as absent (review item 4, finding F4)."""
        self.project()
        made = self.request('POST', '/v1/projects/%s/tasks' % self.pid,
                            {'title': 'null priority', 'priority': None}, token=self.alex)
        self.assertEqual(201, made.status, made.data)
        self.assertEqual(2, self.stored(made.data['id'])['priority'])
        shown = self.request('GET', '/v1/projects/%s/tasks/%s' % (self.pid, made.data['id']),
                             token=self.alex)
        self.assertEqual(2, shown.data.get('priority'), shown.data)


class UnknownFieldTableOnTheEndpointBackend(task_fields.Project, fixes.EndpointCase):
    """Task create, claim and checkpoint refuse a field they do not take (item 2)."""

    def assertRefused(self, response, where, unknown, takes):
        self.assertEqual(422, response.status, response.data)
        self.assertEqual('invalid_payload', response.data['error']['code'])
        said = message(response)
        self.assertIn('%s does not take: %s.' % (where, ', '.join(sorted(unknown))), said)
        self.assertIn('%s takes: %s' % (where, ', '.join(takes)), said)
        self.assertEqual({'unsupported': sorted(unknown), 'takes': list(takes)}, detail(response))

    def test_task_create_refuses_a_field_it_does_not_take(self):
        self.project()
        rows = len(self.canonical_rows())
        with mock.patch.object(self.backend, '_run', wraps=self.backend._run) as asked:
            refused = self.request('POST', '/v1/projects/%s/tasks' % self.pid,
                                   {'title': 'x', 'status': 'open', 'assignee': 'me'},
                                   token=self.alex, key='unknown-create-1')
        self.assertRefused(refused, 'A task creation', ['assignee', 'status'],
                           http_service.ApiHandler.TASK_CREATE_FIELDS)
        self.assertEqual([], asked.call_args_list, 'the endpoint was asked to write')
        self.assertEqual(rows, len(self.canonical_rows()))
        corrected = self.request('POST', '/v1/projects/%s/tasks' % self.pid, {'title': 'x'},
                                 token=self.alex, key='unknown-create-1')
        self.assertEqual(201, corrected.status, corrected.data)
        self.assertEqual(rows + 1, len(self.canonical_rows()))

    def test_claim_refuses_a_field_it_does_not_take(self):
        self.project()
        before = next(row for row in self.canonical_rows() if row['id'] == self.tid)
        with mock.patch.object(self.backend, '_run', wraps=self.backend._run) as asked:
            refused = self.request('POST', self.path + '/claim',
                                   {'actor': None, 'assignee': 'me'}, token=self.casey,
                                   key='unknown-claim-1')
        self.assertRefused(refused, 'A task claim', ['assignee'],
                           http_service.ApiHandler.TASK_CLAIM_FIELDS)
        self.assertEqual([], asked.call_args_list, 'the endpoint was asked to write')
        self.assertEqual(before, next(row for row in self.canonical_rows() if row['id'] == self.tid))
        corrected = self.request('POST', self.path + '/claim', {}, token=self.casey,
                                 key='unknown-claim-1')
        self.assertEqual(200, corrected.status, corrected.data)

    def test_a_checkpoint_refuses_a_field_the_record_does_not_carry(self):
        self.project()
        self.assertEqual(200, self.request('POST', self.path + '/claim', {}, token=self.alex).status)
        brief = self.request('GET', self.path + '/brief', token=self.alex)
        self.assertEqual(200, brief.status, brief.data)
        body = dict(brief.data['checkpoint_template']['body'])
        body.update(intent='do the task', acceptance='the checks pass', summary='stopped',
                    next_action='carry on', made_up=1)
        with mock.patch.object(self.backend, '_run', wraps=self.backend._run) as asked:
            refused = self.request('POST', self.path + '/checkpoints', body, token=self.alex,
                                   key='unknown-checkpoint-1')
        self.assertRefused(refused, 'A checkpoint', ['made_up'],
                           http_service.ApiHandler.CHECKPOINT_BODY_FIELDS)
        self.assertEqual([], asked.call_args_list, 'the endpoint was asked to write')
        self.assertIsNone(self.request('GET', self.path + '/brief', token=self.alex).data['checkpoint'])
        body.pop('made_up')
        corrected = self.request('POST', self.path + '/checkpoints', body, token=self.alex,
                                 key='unknown-checkpoint-1')
        self.assertEqual(201, corrected.status, corrected.data)

    def test_a_caller_with_no_right_keeps_the_404_on_every_task_route(self):
        """A refusal must never come before the caller's right is checked (review item 4, O1).

        ``drew`` is an account of the installation and not a member of this project. Every
        one of these routes answers its unchanged 404 for a body it takes; the same body
        with a field the route does not take must answer exactly the same, never the 422
        field list that belongs to a caller who may write.
        """
        self.project()
        drew = self.login('drew', 'drew-password-1')[0]
        cases = (('task create', 'POST', '/v1/projects/%s/tasks' % self.pid,
                  {'title': 'x'}, {'title': 'x', 'assignee': 'me'}),
                 ('claim', 'POST', self.path + '/claim', {}, {'assignee': 'me'}),
                 ('task change', 'PATCH', self.path, {'title': 'x'},
                  {'title': 'x', 'assignee': 'me'}))
        for where, method, path, taken, unknown in cases:
            with self.subTest(route=where):
                plain = self.request(method, path, taken, token=drew)
                guessing = self.request(method, path, unknown, token=drew)
                self.assertEqual(404, plain.status, plain.data)
                self.assertEqual(plain.status, guessing.status, guessing.data)
                self.assertEqual(message(plain), message(guessing))
                self.assertNotIn('does not take', message(guessing))


class UnknownFieldTableOnTheServiceRoutes(fixes.Harness):
    """Member, worker credential, agent and account refuse a field they do not take (item 2)."""

    def team(self):
        admin = self.admin_token()
        self.alex_id = self.create_account(admin, 'alex', 'alex-password-1')
        self.alex = self.login('alex', 'alex-password-1')[0]
        self.pid = self.create_project(self.alex, 'Alpha')
        return admin

    def assertRefused(self, response, where, unknown, takes):
        self.assertEqual(422, response.status, response.data)
        self.assertEqual('invalid_payload', response.data['error']['code'])
        said = message(response)
        self.assertIn('%s does not take: %s.' % (where, ', '.join(sorted(unknown))), said)
        self.assertIn('%s takes: %s' % (where, ', '.join(takes)), said)
        self.assertEqual({'unsupported': sorted(unknown), 'takes': list(takes)}, detail(response))

    def test_member_change_refuses_a_field_it_does_not_take(self):
        admin = self.team()
        other = self.create_account(admin, 'casey', 'casey-password-1')
        path = '/v1/projects/%s/members/%s' % (self.pid, other)
        refused = self.request('PUT', path, {'role': 'viewer', 'admin': True}, token=self.alex,
                               key='member-unknown-1')
        self.assertRefused(refused, 'A member change', ['admin'],
                           http_service.ApiHandler.MEMBER_FIELDS)
        corrected = self.request('PUT', path, {'role': 'viewer'}, token=self.alex, key='member-unknown-1')
        self.assertIn(corrected.status, (200, 201), corrected.data)

    def test_worker_credential_refuses_a_field_it_does_not_take(self):
        self.team()
        path = '/v1/projects/%s/worker-credentials' % self.pid
        refused = self.request('POST', path,
                               {'label': 'w', 'scopes': ['read'], 'namespace': 'worker-a'},
                               token=self.alex, key='credential-unknown-1')
        self.assertRefused(refused, 'A worker credential issue', ['namespace'],
                           http_service.ApiHandler.CREDENTIAL_ISSUE_FIELDS)
        corrected = self.request('POST', path, {'label': 'w', 'scopes': ['read']},
                                 token=self.alex, key='credential-unknown-1')
        self.assertEqual(201, corrected.status, corrected.data)
        self.assertIn('secret', corrected.data['credential'])

    def test_agent_creation_refuses_a_field_it_does_not_take(self):
        self.team()
        refused = self.request('POST', '/v1/agents',
                               {'name': 'Kestrel', 'working_directory': '/home/alex/k',
                                'colour': 'red'}, token=self.alex, key='agent-unknown-1')
        self.assertRefused(refused, 'An agent creation', ['colour'],
                           http_service.ApiHandler.AGENT_CREATE_FIELDS)
        corrected = self.request('POST', '/v1/agents',
                                 {'name': 'Kestrel', 'working_directory': '/home/alex/k'},
                                 token=self.alex, key='agent-unknown-1')
        self.assertEqual(201, corrected.status, corrected.data)

    def test_agent_change_refuses_a_field_it_does_not_take(self):
        self.team()
        made = self.request('POST', '/v1/agents', {'name': 'Kestrel'}, token=self.alex)
        self.assertEqual(201, made.status, made.data)
        aid = made.data['agent']['id']
        path = '/v1/agents/%s' % aid
        refused = self.request('PATCH', path,
                               {'working_directory': '/home/alex/k', 'colour': 'red'},
                               token=self.alex, key='agent-change-unknown-1')
        self.assertRefused(refused, 'An agent change', ['colour'],
                           http_service.ApiHandler.AGENT_UPDATE_FIELDS)
        corrected = self.request('PATCH', path, {'working_directory': '/home/alex/k'},
                                 token=self.alex, key='agent-change-unknown-1')
        self.assertEqual(200, corrected.status, corrected.data)
        self.assertEqual('/home/alex/k', corrected.data['working_directory'])

    def test_agent_change_by_a_caller_with_no_right_keeps_the_404(self):
        """The refusal is answered after the record's ownership is judged (review item 1)."""
        admin = self.team()
        made = self.request('POST', '/v1/agents', {'name': 'Kestrel'}, token=self.alex)
        self.assertEqual(201, made.status, made.data)
        path = '/v1/agents/%s' % made.data['agent']['id']
        self.create_account(admin, 'casey', 'casey-password-1')
        casey = self.login('casey', 'casey-password-1')[0]
        plain = self.request('PATCH', path, {'notes': 'n'}, token=casey)
        guessing = self.request('PATCH', path, {'notes': 'n', 'surprise': 1}, token=casey)
        self.assertEqual(404, plain.status, plain.data)
        self.assertEqual(plain.status, guessing.status, guessing.data)
        self.assertEqual(message(plain), message(guessing))
        self.assertNotIn('does not take', message(guessing))
        # The owner still gets the field list for the same body.
        owner = self.request('PATCH', path, {'notes': 'n', 'surprise': 1}, token=self.alex)
        self.assertRefused(owner, 'An agent change', ['surprise'],
                           http_service.ApiHandler.AGENT_UPDATE_FIELDS)

    def test_agent_creation_for_a_project_the_caller_cannot_see_keeps_the_404(self):
        """The grant is judged before the field list is answered (review item 1, case B)."""
        admin = self.team()
        self.create_account(admin, 'casey', 'casey-password-1')
        casey = self.login('casey', 'casey-password-1')[0]
        hidden = self.create_project(casey, 'Hidden')
        body = {'name': 'Intruder', 'projects': [hidden]}
        plain = self.request('POST', '/v1/agents', body, token=self.alex)
        guessing = self.request('POST', '/v1/agents', dict(body, surprise=1), token=self.alex)
        self.assertEqual(404, plain.status, plain.data)
        self.assertEqual(plain.status, guessing.status, guessing.data)
        self.assertEqual(message(plain), message(guessing))
        self.assertNotIn('does not take', message(guessing))
        # A project the caller can see, with the same unknown field, is the route's 422.
        refused = self.request('POST', '/v1/agents',
                               {'name': 'Kestrel', 'projects': [self.pid], 'surprise': 1},
                               token=self.alex)
        self.assertRefused(refused, 'An agent creation', ['surprise'],
                           http_service.ApiHandler.AGENT_CREATE_FIELDS)

    def test_account_creation_refuses_a_field_it_does_not_take(self):
        admin = self.team()
        refused = self.request('POST', '/v1/accounts',
                               {'username': 'zoe', 'display_name': 'Zoe', 'superuser': True},
                               token=admin, key='account-unknown-1')
        self.assertRefused(refused, 'An account creation', ['superuser'],
                           http_service.ApiHandler.ACCOUNT_CREATE_FIELDS)
        corrected = self.request('POST', '/v1/accounts', {'username': 'zoe', 'display_name': 'Zoe'},
                                 token=admin, key='account-unknown-1')
        self.assertEqual(201, corrected.status, corrected.data)
        self.assertEqual('zoe', corrected.data['username'])
        self.assertFalse(corrected.data['superuser'])

    def test_the_known_clients_bodies_still_work(self):
        """The bodies web/js/api.js and http_client.py send today are accepted."""
        admin = self.team()
        other = self.create_account(admin, 'casey', 'casey-password-1')
        self.assertIn(self.request('PUT', '/v1/projects/%s/members/%s' % (self.pid, other),
                                   {'role': 'contributor'}, token=self.alex).status, (200, 201))
        self.assertEqual(201, self.request('POST', '/v1/projects/%s/worker-credentials' % self.pid,
                                           {'label': 'Worker credential',
                                            'scopes': ['tasks', 'checkpoints', 'reviews']},
                                           token=self.alex).status)
        self.assertEqual(201, self.request('POST', '/v1/agents',
                                           {'name': 'Kestrel', 'tool': 'Copilot',
                                            'working_directory': '/home/alex/k',
                                            'projects': [self.pid]}, token=self.alex).status)
        self.assertEqual(201, self.request('POST', '/v1/accounts',
                                           {'username': 'zoe', 'display_name': 'Zoe'},
                                           token=admin).status)


if __name__ == '__main__':
    unittest.main()
