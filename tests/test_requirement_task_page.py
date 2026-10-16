"""The web Tasks page leaves requirement records out and marks one that is shown.

kittrial-5bb.277 review item 2: the marker on the tasks-list route is only useful if
the page that feeds the incident uses it. ``web/js/views/project.js`` now filters
``requirement_record`` rows out of the default task view, says how many were left out,
and offers a "Show requirements" filter that brings them back marked as a requirement
with a link to the project's Requirements page, where they are edited.
"""
import json
import shutil
import sys
import unittest
from pathlib import Path

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT / 'tests'))


class RequirementTasksPageTests(unittest.TestCase):
    def test_requirement_rows_are_out_of_the_default_task_view_and_marked_when_shown(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('node is not installed')
        import test_http_web
        done = test_http_web.run_node_module(self, node, 'await import(process.argv[1])',
                                             (KIT / 'tests' / 'web_requirement_tasks_page.mjs').as_uri(),
                                             (KIT / 'tests' / 'web_dom_shim.mjs').as_uri(),
                                             (KIT / 'web' / 'js' / 'api.js').as_uri(),
                                             (KIT / 'web' / 'js' / 'views' / 'project.js').as_uri())
        self.assertEqual(0, done.returncode, done.stderr[-3000:])
        seen = json.loads(done.stdout.strip().splitlines()[-1])

        # The default view: the ordinary task is there, the requirement row is not, and
        # the page says one was left out rather than reading as if there were no work.
        # Each element of `defaultRows` is ONE ROW'S WHOLE TEXT (title + id + status +
        # next action), so the title is matched as a substring of a row, never as a list
        # element: the list-membership form could never pass and was the assertion that
        # failed CI on all four jobs (kittrial-5bb.277 third review, item 2).
        default_rows = seen['defaultRows']
        self.assertTrue(any('An ordinary task' in row for row in default_rows), default_rows)
        self.assertFalse(any('A requirement record' in row for row in default_rows), default_rows)
        self.assertFalse(seen['defaultHasRequirement'])
        self.assertEqual(1, len(seen['defaultLeftOutNote']))
        self.assertIn('1 requirement record is not shown here', seen['defaultLeftOutNote'][0])
        self.assertIn('Show requirements', seen['text'])
        self.assertIsNotNone(seen['toggle'])
        self.assertEqual(('show-requirements', 'checkbox', False),
                         (seen['toggle']['id'], seen['toggle']['type'], seen['toggle']['checked']))
        self.assertEqual(0, seen['defaultRequirementLinks'])

        # The filter on: the requirement row is shown, marked as a requirement, carries the
        # marker attribute and links to the Requirements page, and the note is gone.
        self.assertTrue(seen['filteredHasRequirement'], seen['filteredRows'])
        self.assertEqual(1, seen['filteredChip'])
        self.assertEqual('true', seen['filteredDataAttribute'])
        self.assertIn('#/p/alpha/requirements', seen['filteredRequirementLinks'])
        self.assertEqual(0, seen['filteredLeftOutNote'])
        # The ordinary task is never marked as a requirement.
        self.assertEqual(0, seen['ordinaryMarked'])

    def test_the_task_page_does_not_offer_a_claim_on_a_row_the_server_calls_unclaimable(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('node is not installed')
        import test_http_web
        done = test_http_web.run_node_module(self, node, 'await import(process.argv[1])',
                                             (KIT / 'tests' / 'web_requirement_task_claim.mjs').as_uri(),
                                             (KIT / 'tests' / 'web_dom_shim.mjs').as_uri(),
                                             (KIT / 'web' / 'js' / 'api.js').as_uri(),
                                             (KIT / 'web' / 'js' / 'views' / 'task.js').as_uri())
        self.assertEqual(0, done.returncode, done.stderr[-3000:])
        seen = json.loads(done.stdout.strip().splitlines()[-1])

        # Review item 2, the last part: a row the server calls not claimable gets no claim
        # button, and the banner points at the Requirements page instead of inviting a
        # claim the route would refuse with 409.
        self.assertEqual(0, seen['requirementClaims'])
        self.assertIn('Requirement record: edit it', seen['requirementText'])
        self.assertIn("project's Requirements page", seen['requirementText'])
        # Edit stays: a requirement record is still editable from its own task page.
        self.assertEqual(1, seen['requirementEdits'])
        # An ordinary unclaimed task keeps its claim button.
        self.assertEqual(1, seen['ordinaryClaims'])


if __name__ == '__main__':
    unittest.main()
