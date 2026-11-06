"""kittrial-5bb.522 slice 2: "Members & settings" is split into the project's People, Agents and Settings.

web/js/views/project.js, app.js and routes.js run under Node (tests/web_project_sections_522s2.mjs, with
tests/web_dom_shim.mjs) against a stubbed transport that records every request. One run gives all observations;
each rule has its own named test.

The rules (docs/design/PROJECT_CROSS_SECTION_DESIGN.md, sections 3.3, 6 and 7, slice 2):
- the panels move as they are: members on People, project agents on Agents, the rest on Settings;
- nothing about who may do what changes: no route call is added or dropped, and a non-owner is told in one
  sentence that the agents are the owners' to see (and no agents call is made);
- every route is kept: /p/{pid}/settings still opens Settings;
- the fix links of the hand-over walkthrough and the set-up page point at the new homes.
"""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
WEB = TESTS.parent / 'web' / 'js'
_RESULT = None

# The requests the ORIGINAL Members & settings page (slice 1's views/project.js, commit 045a758a) made for an
# owner, measured by running that file's `settings` view against the same recording stub as this test
# (`git show 045a758a:web/js/views/project.js`, run with the .mjs's optional last argument). Kept as a literal so
# that this test needs no old file.
OLD_SETTINGS_CALLS = [
    'GET /v1/projects/alpha',
    'GET /v1/projects/alpha/agents',
    'GET /v1/projects/alpha/audit',
    'GET /v1/projects/alpha/coordination',
    'GET /v1/projects/alpha/delegations',
    'GET /v1/projects/alpha/members',
    'GET /v1/projects/alpha/switches',
    'GET /v1/projects/alpha/worker-credentials',
]


def observations(case):
    global _RESULT
    if _RESULT is None:
        node = shutil.which('node')
        if not node:
            case.skipTest('node is not installed')
        done = subprocess.run(
            [node, str(TESTS / 'web_project_sections_522s2.mjs'), (TESTS / 'web_dom_shim.mjs').as_uri(),
             (WEB / 'app.js').as_uri(), (WEB / 'views' / 'project.js').as_uri(),
             (WEB / 'views' / 'handover.js').as_uri(), (WEB / 'views' / 'setup.js').as_uri(),
             (WEB / 'routes.js').as_uri()],
            capture_output=True, text=True, timeout=300)
        case.assertEqual(0, done.returncode, done.stderr[-3000:])
        _RESULT = json.loads(done.stdout.strip().splitlines()[-1])
    return _RESULT


class EachPanelIsOnItsOwnSection(unittest.TestCase):

    def test_the_members_panel_is_on_people_and_on_no_other_section(self):
        seen = observations(self)['owner']
        self.assertIn('members', seen['people']['panels'])
        self.assertNotIn('members', seen['agents']['panels'])
        self.assertNotIn('members', seen['settings']['panels'])
        self.assertEqual('People', seen['people']['title'])

    def test_the_project_agents_panel_is_on_agents_for_an_owner_and_on_no_other_section(self):
        seen = observations(self)['owner']
        self.assertIn('project-agents', seen['agents']['panels'])
        self.assertNotIn('project-agents', seen['people']['panels'])
        self.assertNotIn('project-agents', seen['settings']['panels'])
        self.assertEqual('Agents', seen['agents']['title'])

    def test_claiming_credentials_audit_and_archive_are_on_settings_and_on_no_other_section(self):
        seen = observations(self)['owner']
        self.assertEqual('Settings', seen['settings']['title'])
        self.assertIn('claiming', seen['settings']['panels'])
        for heading in ('Worker credentials', 'Recent activity (audit)', 'Archive project'):
            self.assertIn(heading, seen['settings']['headings'])
            self.assertNotIn(heading, seen['people']['headings'])
            self.assertNotIn(heading, seen['agents']['headings'])
        self.assertNotIn('claiming', seen['people']['panels'] + seen['agents']['panels'])

    def test_the_coordination_panel_is_on_people_and_agents_and_no_longer_on_settings(self):
        seen = observations(self)['owner']
        self.assertIn('coordination', seen['people']['panels'])
        self.assertIn('coordination', seen['agents']['panels'])
        self.assertNotIn('coordination', seen['settings']['panels'])


class AnotherPersonOnAgents(unittest.TestCase):

    def test_a_non_owner_reads_one_sentence_and_no_agents_call_is_made(self):
        seen = observations(self)['nonOwnerAgents']
        self.assertEqual('Only this project’s owners see its agents here. Your own agents are under My agents.',
                         seen['sentence'])
        self.assertEqual(['#/agents'], seen['link'])
        self.assertNotIn('project-agents', seen['panels'])
        self.assertEqual([], [c for c in seen['calls'] if c.endswith('/beta/agents')])


class RoutesAreKept(unittest.TestCase):

    def test_the_old_settings_route_still_renders_settings(self):
        seen = observations(self)
        self.assertEqual('settings', seen['routeNames'][2])
        self.assertEqual('Settings', seen['routes']['/p/alpha/settings']['title'])
        self.assertIn('claiming', seen['routes']['/p/alpha/settings']['panels'])

    def test_the_new_routes_open_their_views(self):
        seen = observations(self)
        self.assertEqual(['people', 'projectAgents', 'settings', 'agents'], seen['routeNames'])
        self.assertEqual('People', seen['routes']['/p/alpha/people']['title'])
        self.assertEqual('Agents', seen['routes']['/p/alpha/agents']['title'])
        self.assertIn('members', seen['routes']['/p/alpha/people']['panels'])
        self.assertIn('project-agents', seen['routes']['/p/alpha/agents']['panels'])


class TheRail(unittest.TestCase):

    def test_the_rail_lists_people_agents_settings_and_no_members_and_settings(self):
        rail = observations(self)['rail']
        labels = [label for _href, label in rail]
        self.assertEqual(['People', 'Agents', 'Settings'], labels[labels.index('People'):labels.index('People') + 3])
        self.assertEqual(['#/p/alpha/people', '#/p/alpha/agents', '#/p/alpha/settings'],
                         [href for href, label in rail if label in ('People', 'Agents', 'Settings')])
        self.assertNotIn('Members & settings', labels)
        self.assertIn('How work is found', labels)


class TheLinks(unittest.TestCase):

    def test_the_handover_fix_routes_point_at_the_new_homes(self):
        fix = observations(self)['fixRoutes']
        self.assertEqual('/p/p1/people', fix['members'])
        self.assertEqual('/p/p1/agents', fix['settings'])
        self.assertEqual(('/agents', '/setup', '/p/p1/handover'), (fix['myAgents'], fix['agentSetup'], fix['note']))

    def test_the_setup_members_step_links_to_people(self):
        self.assertEqual('/p/p1/people', observations(self)['setupMembers'])


class NoRouteCallChanges(unittest.TestCase):

    def test_people_agents_and_settings_make_together_the_calls_the_old_page_made(self):
        seen = observations(self)
        self.assertEqual(OLD_SETTINGS_CALLS, seen['ownerUnionCalls'])

    def test_each_section_reads_only_what_its_panels_need(self):
        # Stronger than the union: a call moved onto the wrong section (or one added there) shows here.
        owner = observations(self)['owner']
        self.assertEqual(['GET /v1/projects/alpha', 'GET /v1/projects/alpha/coordination', 'GET /v1/projects/alpha/members'],
                         owner['people']['calls'])
        self.assertEqual(['GET /v1/projects/alpha', 'GET /v1/projects/alpha/agents', 'GET /v1/projects/alpha/coordination'],
                         owner['agents']['calls'])
        self.assertEqual(['GET /v1/projects/alpha', 'GET /v1/projects/alpha/audit', 'GET /v1/projects/alpha/delegations',
                          'GET /v1/projects/alpha/switches', 'GET /v1/projects/alpha/worker-credentials'],
                         owner['settings']['calls'])

    def test_every_call_is_a_read(self):
        # Opening a section writes nothing; the writes stay behind the same buttons as before.
        for call in observations(self)['ownerUnionCalls']:
            self.assertTrue(call.startswith('GET '), call)


if __name__ == '__main__':
    unittest.main()
