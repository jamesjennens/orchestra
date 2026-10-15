"""The tasks-list route marks requirement records and says they are not claimable work.

kittrial-5bb.277 review item 2: the reported incident was the Tasks page, where
requirement rows came back exactly like open tasks -- no marker, no claimability -- so
they read as work an agent could claim. The route that feeds that page,
``GET /v1/projects/{pid}/tasks`` (``http_service.py`` ``tasks_list`` ->
``Service._task_view``), now carries ``requirement_record`` and ``claimable`` on every
row, from the same two predicates the queue, the agent attention list and the claim
route use (``work.requirement_record`` / ``work.claimable``).

The canonical case exists because the reviewer asked for one test on the real
endpoint binding, not only on the in-process backend (review item 3, part 3): the
route's rows are the endpoint's, and the marker is a presentation field over them.
"""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_http_agents import AgentHarness
from test_http_review_fixes import EndpointCase


class TasksListMarkerTests(AgentHarness):
    """The in-process binding: the row, its own route, and the refusal stay in step."""

    def setUp(self):
        super().setUp()
        admin = self.admin_token()
        self.alex = self.create_account(admin, 'alex', 'alex-password-1')
        self.alex_token = self.login('alex', 'alex-password-1')[0]
        # Alex owns the project, so this case has one actor throughout: the account that
        # makes the tasks is the one that reads and claims them.
        self.project = self.create_project(self.alex_token, 'Alpha')
        self.tasks = '/v1/projects/%s/tasks' % self.project

    def new(self, title):
        made = self.request('POST', self.tasks, {'title': title}, token=self.alex_token)
        self.assertEqual(201, made.status, made.data)
        return made.data['id']

    def label(self, task, *labels):
        self.backend.state['tasks'][task]['labels'] = list(labels)

    def rows(self):
        listed = self.request('GET', self.tasks + '?limit=100', token=self.alex_token)
        self.assertEqual(200, listed.status, listed.data)
        return {row['id']: row for row in listed.data['items']}

    def row(self, task):
        got = self.request('GET', '%s/%s' % (self.tasks, task), token=self.alex_token)
        self.assertEqual(200, got.status, got.data)
        return got.data

    def test_the_tasks_list_and_the_task_route_carry_the_marker_and_claimability(self):
        plain = self.new('plain')
        only = self.rows()
        self.assertIs(True, only[plain]['claimable'])
        self.assertIs(False, only[plain]['requirement_record'])
        # The label is what decides: the same row, once it holds the controlled
        # requirement label, is marked and is no longer claimable, in both routes.
        self.label(plain, 'requirement', 'requirement:draft')
        listed = self.rows()[plain]
        self.assertIs(True, listed['requirement_record'])
        self.assertIs(False, listed['claimable'])
        self.assertIs(True, self.row(plain)['requirement_record'])
        self.assertIs(False, self.row(plain)['claimable'])

    def test_a_brd_section_is_marked_the_same_way_and_a_plain_task_is_left_alone(self):
        section = self.new('a BRD section')
        other = self.new('an ordinary task')
        self.label(section, 'brd-section', 'requirement:draft')
        rows = self.rows()
        self.assertIs(True, rows[section]['requirement_record'])
        self.assertIs(False, rows[section]['claimable'])
        self.assertIs(False, rows[other]['requirement_record'])
        self.assertIs(True, rows[other]['claimable'])

    def test_the_claim_refusal_names_the_requirements_page_and_the_host_claim_that_stays(self):
        # Review item 3, part 2: the sentence names the web Requirements page too, and
        # says the one claim that still works (an explicit host claim by a coordinator),
        # so it is not read as "nothing can claim this" (kittrial-5bb.92 review item 1).
        task = self.new('a requirement')
        self.label(task, 'requirement', 'requirement:draft')
        refused = self.request('POST', '%s/%s/claim' % (self.tasks, task), {}, token=self.alex_token)
        self.assertEqual(409, refused.status, refused.data)
        message = refused.data['error']['message']
        self.assertIn('requirement record', message)
        self.assertIn('not a unit of work, so it cannot be claimed', message)
        self.assertIn('Requirements page', message)
        self.assertIn('requirement_records.py draft|revise', message)
        self.assertIn('admin.py requirement-apply', message)
        self.assertIn('bd update ID --claim', message)


class CanonicalTasksListMarkerTests(EndpointCase):
    """The canonical endpoint binding: the same fields on the endpoint's own rows."""

    def test_the_endpoint_tasks_list_carries_the_marker_and_claimability(self):
        alex, project = self.setup_project()
        tasks = '/v1/projects/%s/tasks' % project
        requirement = self.create_task(alex, project, 'a requirement')
        self.assertEqual(201, requirement.status, requirement.data)
        plain = self.create_task(alex, project, 'plain')
        self.assertEqual(201, plain.status, plain.data)
        # The endpoint's own row holds the label the requirement_records.py tool writes.
        path = self.canonical_root / 'canonical.json'
        state = json.loads(path.read_text(encoding='utf-8'))
        next(row for row in state['rows']
             if row['id'] == requirement.data['id'])['labels'] = ['requirement', 'requirement:draft']
        path.write_text(json.dumps(state), encoding='utf-8')
        listed = self.request('GET', tasks + '?limit=100', token=alex)
        self.assertEqual(200, listed.status, listed.data)
        rows = {row['id']: row for row in listed.data['items']}
        marked = rows[requirement.data['id']]
        self.assertIs(True, marked['requirement_record'])
        self.assertIs(False, marked['claimable'])
        free = rows[plain.data['id']]
        self.assertIs(False, free['requirement_record'])
        self.assertIs(True, free['claimable'])


if __name__ == '__main__':
    unittest.main()
