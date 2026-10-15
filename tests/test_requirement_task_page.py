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
        self.assertIn('An ordinary task', seen['defaultRows'])
        self.assertNotIn('A requirement record', seen['defaultRows'])
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


if __name__ == '__main__':
    unittest.main()
